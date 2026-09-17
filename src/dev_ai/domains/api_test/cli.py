#!/usr/bin/env python3
"""Public CLI for Bruno QA initialization, generation, checks, and execution."""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import subprocess
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

sys.dont_write_bytecode = True

from ...core.redaction import redact
from .execution_config import initialize_execution_layout
from .analyze_source_logic import apply_candidates, scan as scan_source_logic
from .materialize_missing_bru import materialize
from .mock_data import command as mock_data_operation
from .mock_data import derive_mock_data_contracts, write_discovery
from .parse_openapi import (
    extract,
    constraint_obligations,
    generate_module_map,
    load_document,
    render_manifest,
    write_partitioned,
)
from .qa_lock import check as check_qa_lock
from .qa_lock import refresh_generation_state_cases
from .qa_lock import write as write_qa_lock
from .qa_paths import (
    BRUNO,
    CONSTRAINTS,
    CONTRACTS,
    EXECUTION,
    GLOBAL_RESULTS,
    LOGS,
)
from .constraints import (
    ensure_rule_library,
    validate_stage,
    validate_worker_snapshot,
    worker_snapshot_path,
    write_worker_snapshot,
)
from .source_constraints import (
    apply_constraints_to_manifest,
    apply_environment_values,
    apply_observed_constraints,
    scope_source_constraints,
    write_value_resolutions,
    write_source_constraints,
)


def qa_root_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--qa-root", type=Path, default=Path("qa"))


def shared_cli_mode(qa_root: Path) -> bool:
    return True


def script_bundle_errors(qa_root: Path) -> list[str]:
    return []


def run_child(command: list[str], *, relay_summary: bool = True) -> int:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    lines = [line for line in (completed.stdout or "").splitlines() if line.strip()]
    progress = lines[:-1] if relay_summary else []
    for line in progress:
        print(line, file=sys.stderr)
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    if relay_summary and lines:
        print(lines[-1])
    elif completed.stdout:
        sys.stdout.write(completed.stdout)
    return completed.returncode


def version_gate(
    qa_root: Path,
    *options: str,
    business_repo: Path | None = None,
    rules: Path | None = None,
) -> int:
    qa_root = qa_root.resolve()
    command = [
        sys.executable,
        "-m",
        "dev_ai.domains.api_test.check_version_compatibility",
        str((business_repo or qa_root.parent).resolve()),
        str(qa_root / CONTRACTS),
        *options,
    ]
    if rules is not None:
        command.extend(["--rules", str(rules.resolve())])
    return run_child(command)


def is_loopback_openapi(document: dict[str, object]) -> bool:
    provenance = document.get("provenance", {})
    source_url = str(provenance.get("source_url", "")) if isinstance(provenance, dict) else ""
    host = urllib.parse.urlsplit(source_url).hostname
    return bool(host and host.lower() in {"localhost", "127.0.0.1", "::1"})


def init_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="dev-ai api-test init")
    qa_root_argument(parser)
    args = parser.parse_args(argv)
    qa_root = args.qa_root.resolve()
    changed = initialize_execution_layout(qa_root, local_scripts=False)
    ensure_rule_library(qa_root)
    (qa_root / CONTRACTS / "modules").mkdir(parents=True, exist_ok=True)
    initial_artifacts = {
        qa_root / CONSTRAINTS / "source-rules.yaml": {
            "version": 1, "source_roots": [], "source_inventory": {}, "field_rules": [],
            "error_codes": [], "response_rules": [], "endpoint_response_rules": [], "controller_bindings": [],
        },
        qa_root / CONSTRAINTS / "observed-rules.yaml": {"version": 1, "observations": []},
        qa_root / CONTRACTS / "exception-profile.yaml": {"version": 1, "handlers": []},
        qa_root / CONTRACTS / "fixtures" / "generated" / "manifest.yaml": {"version": 1, "fixtures": []},
    }
    for path, document in initial_artifacts.items():
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(render_manifest(document, path), encoding="utf-8")
            changed.append(path)
    inventory_path = qa_root / CONSTRAINTS / "mock-data.yaml"
    if not inventory_path.is_file():
        write_discovery(qa_root, [])
        changed.append(inventory_path)
    version_lock = qa_root / CONTRACTS / "version-lock.yaml"
    if not version_lock.is_file():
        code = version_gate(qa_root, "--init")
        if code:
            return code
        changed.append(version_lock)
    print(f"initialized {qa_root} ({len(changed)} file(s) changed)")
    return 0


def generate_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="dev-ai api-test generate")
    qa_root_argument(parser)
    parser.add_argument("--openapi", type=Path)
    parser.add_argument("--module-map", type=Path)
    parser.add_argument("--incremental", action="store_true")
    parser.add_argument("--no-seed-cases", action="store_true")
    parser.add_argument("--source-root", action="append", type=Path, default=[])
    parser.add_argument("--exception-type")
    parser.add_argument("--error-code-type")
    parser.add_argument("--coverage-profile", choices=("contract-draft", "full-matrix"))
    parser.add_argument("--base-url", action="append", default=[])
    parser.add_argument("--port", action="append", type=int, default=[])
    parser.add_argument("--path", action="append", dest="openapi_paths", default=[])
    parser.add_argument("--timeout", type=float, default=2.0)
    args = parser.parse_args(argv)
    qa_root = args.qa_root.resolve()
    initialize_execution_layout(qa_root, local_scripts=False)
    ensure_rule_library(qa_root)
    if (qa_root / ".dev-ai.lock.json").is_file():
        version_code = version_gate(qa_root, "--phase", "before-generate")
        if version_code:
            return version_code
    from .execution_config import environment_file, load_bruno_environment, load_execution_config

    execution_config = load_execution_config(qa_root / EXECUTION / "config.yaml")
    coverage_profile = args.coverage_profile or execution_config["coverage_profile"]
    contracts = qa_root / CONTRACTS
    contracts.mkdir(parents=True, exist_ok=True)
    source_spec = args.openapi.resolve() if args.openapi else next(
        (path for path in (contracts / "openapi.json", contracts / "openapi.yaml", contracts / "openapi.yml") if path.is_file()),
        contracts / "openapi.json",
    )
    fetch_requested = bool(args.base_url or args.port or args.openapi_paths)
    if fetch_requested or not source_spec.is_file():
        fetch_command = [
            sys.executable,
            "-m", "dev_ai.domains.api_test.fetch_local_openapi",
            "--project-root", str(qa_root.parent),
            "--output", str(source_spec),
            "--timeout", str(args.timeout),
        ]
        for value in args.base_url:
            fetch_command.extend(["--base-url", value])
        for value in args.port:
            fetch_command.extend(["--port", str(value)])
        for value in args.openapi_paths:
            fetch_command.extend(["--path", value])
        fetch_code = run_child(fetch_command)
        if fetch_code:
            return fetch_code
    if not source_spec.is_file():
        parser.error(f"offline OpenAPI document does not exist: {source_spec}")
    saved_spec = contracts / ("openapi.yaml" if source_spec.suffix.lower() in {".yaml", ".yml"} else "openapi.json")
    if source_spec != saved_spec.resolve() and (not saved_spec.is_file() or source_spec.read_bytes() != saved_spec.read_bytes()):
        shutil.copy2(source_spec, saved_spec)
    source_document = load_document(saved_spec)
    manifest = extract(saved_spec, source_document)
    source_constraints_path = qa_root / CONSTRAINTS / "source-rules.yaml"
    if args.source_root:
        missing = [str(path) for path in args.source_root if not path.is_dir()]
        if missing:
            parser.error("source root(s) do not exist: " + ", ".join(missing))
        source_constraints = write_source_constraints(qa_root, args.source_root, manifest)
        write_discovery(qa_root, args.source_root)
        apply_constraints_to_manifest(manifest, source_constraints)
    elif source_constraints_path.is_file():
        source_constraints = scope_source_constraints(load_document(source_constraints_path), manifest)
        source_constraints_path.write_text(render_manifest(source_constraints, source_constraints_path), encoding="utf-8")
        apply_constraints_to_manifest(manifest, source_constraints)
    else:
        source_constraints = {
            "version": 1,
            "generated_at": datetime.now().isoformat(),
            "source_roots": [],
            "source_inventory": {},
            "field_rules": [],
            "error_codes": [],
            "response_rules": [],
            "endpoint_response_rules": [],
            "controller_bindings": [],
        }
        source_constraints_path.parent.mkdir(parents=True, exist_ok=True)
        source_constraints_path.write_text(render_manifest(source_constraints, source_constraints_path), encoding="utf-8")
    observed_paths = [qa_root / CONSTRAINTS / "observed-rules.yaml"]
    observed_paths.extend((contracts / "modules").glob("*/observed-rules.yaml"))
    for observed_path in observed_paths:
        if observed_path.is_file():
            observed = load_document(observed_path)
            apply_observed_constraints(manifest, observed)
    environment_path = environment_file(qa_root / EXECUTION / "config.yaml", execution_config)
    if environment_path.is_file():
        apply_environment_values(manifest, load_bruno_environment(environment_path))
    for endpoint in manifest.get("endpoints", []):
        if isinstance(endpoint, dict):
            endpoint["obligations"] = constraint_obligations(endpoint)
    source_candidates: dict[str, object] | None = None
    if args.source_root:
        source_candidates = scan_source_logic(args.source_root, None, args.exception_type, args.error_code_type)
        if source_candidates.get("errors"):
            for error in source_candidates["errors"]:
                print(f"ERROR: {error}", file=sys.stderr)
            return 2
        profile = source_candidates.get("java", {}).get("exception_profile", {}) if isinstance(source_candidates.get("java"), dict) else {}
        handlers = profile.get("handlers", []) if isinstance(profile, dict) else []
        for endpoint in manifest.get("endpoints", []):
            if not isinstance(endpoint, dict):
                continue
            endpoint_key = f"{str(endpoint.get('method', '')).upper()} {endpoint.get('path')}"
            handler = next((
                item for item in handlers
                if isinstance(item, dict)
                and endpoint_key in {
                    str(value) for value in (
                        item.get("evidence", {}).get("endpoint_scope", [])
                        if isinstance(item.get("evidence"), dict) else []
                    )
                }
            ), None)
            if handler:
                endpoint["x-exception-profile"] = copy.deepcopy(handler)
    module_map = args.module_map.resolve() if args.module_map else contracts / "module-map.yaml"
    if not module_map.is_file():
        module_map.parent.mkdir(parents=True, exist_ok=True)
        module_map.write_text(render_manifest(generate_module_map(manifest), module_map), encoding="utf-8")
        print(f"created {module_map}; review Tag ownership before relying on generated coverage")
    summary = write_partitioned(
        manifest,
        module_map,
        contracts / "modules",
        seed_cases=not args.no_seed_cases,
        incremental=args.incremental,
        coverage_profile=coverage_profile,
    )
    if source_candidates is not None:
        unresolved = apply_candidates(source_candidates, contracts)
        if unresolved:
            for candidate_id in unresolved:
                print(
                    f"ERROR: source candidate {candidate_id} cannot be uniquely mapped to an endpoint",
                    file=sys.stderr,
                )
            return 2
        refresh_generation_state_cases(contracts)
    exception_profile = contracts / "exception-profile.yaml"
    if not exception_profile.is_file():
        exception_profile.write_text(
            render_manifest({"version": 1, "handlers": []}, exception_profile),
            encoding="utf-8",
        )
    derive_mock_data_contracts(qa_root)
    if (contracts / "generation-state.yaml").is_file():
        refresh_generation_state_cases(contracts)
    write_value_resolutions(qa_root, source_constraints)
    materialize(contracts, qa_root / BRUNO, execution_config_path=qa_root / EXECUTION / "config.yaml")
    write_value_resolutions(qa_root, source_constraints)
    write_qa_lock(contracts)
    constraint_errors = [
        *validate_stage(qa_root, "generation"),
        *validate_stage(qa_root, "materialization"),
    ]
    if constraint_errors:
        for error in dict.fromkeys(constraint_errors):
            print(f"ERROR: {error}", file=sys.stderr)
        return 2
    print(
        f"generated endpoints={len(manifest['endpoints'])} changed_modules={len(summary['changed_modules'])} "
        f"skipped_modules={len(summary['skipped_modules'])}"
    )
    for endpoint_id in summary["deleted_endpoint_ids"]:
        print(f"REVIEW: remove the .bru file for deleted endpoint {endpoint_id}", file=sys.stderr)
    for case_id in summary["manual_review_cases"]:
        print(f"REVIEW: preserved manually modified case {case_id}", file=sys.stderr)
    if is_loopback_openapi(source_document):
        print("OpenAPI came from a running local service; starting the required execution attempt")
        return run_command(["--qa-root", str(qa_root)])
    return 0


def coverage_command(argv: list[str], reconcile: bool) -> int:
    parser = argparse.ArgumentParser(prog=f"dev-ai api-test {'reconcile' if reconcile else 'check'}")
    qa_root_argument(parser)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", action="store_true")
    scope.add_argument("--module")
    parser.add_argument("--results", type=Path)
    parser.add_argument("--preflight-results", type=Path)
    parser.add_argument("--write-status", action="store_true")
    args = parser.parse_args(argv)
    if reconcile and (not args.results or not args.preflight_results):
        parser.error("reconcile requires --results and --preflight-results")
    qa_root = args.qa_root.resolve()
    contracts = qa_root / CONTRACTS
    openapi = next(
        (path for path in (contracts / "openapi.json", contracts / "openapi.yaml", contracts / "openapi.yml") if path.is_file()),
        contracts / "openapi.json",
    )
    try:
        script_errors = script_bundle_errors(qa_root)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    lock_errors = check_qa_lock(contracts)
    if script_errors or lock_errors:
        for error in [*script_errors, *lock_errors]:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    command = [
        sys.executable,
        "-m", "dev_ai.domains.api_test.check_api_coverage",
        str(contracts),
        str(qa_root / BRUNO),
        "--openapi", str(openapi),
        "--require-scenarios",
        "--require-auth",
        "--execution-config", str(qa_root / EXECUTION / "config.yaml"),
    ]
    if args.module:
        command.extend(["--module", args.module])
    else:
        command.append("--all")
    if args.results:
        command.extend(["--results", str(args.results.resolve())])
    if args.preflight_results:
        command.extend(["--preflight-results", str(args.preflight_results.resolve())])
    if args.write_status:
        command.append("--write-status")
    if (qa_root / ".dev-ai.lock.json").is_file():
        command.append("--allow-draft-version")
    return run_child(command)


def run_command(argv: list[str]) -> int:
    extra = list(argv)
    if "-h" in extra or "--help" in extra:
        return run_child(
            [sys.executable, "-m", "dev_ai.domains.api_test.run_bruno", "--help"], relay_summary=False
        )
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--qa-root", type=Path, default=Path("qa"))
    known, remaining = parser.parse_known_args(extra)
    try:
        errors = script_bundle_errors(known.qa_root.resolve())
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    extra = ["--qa-root", str(known.qa_root.resolve()), *remaining]
    return run_child([sys.executable, "-m", "dev_ai.domains.api_test.run_bruno", *extra])


def materialize_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="dev-ai api-test materialize")
    qa_root_argument(parser)
    parser.add_argument("--module")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    qa_root = args.qa_root.resolve()
    ensure_rule_library(qa_root)
    generation_errors = validate_stage(qa_root, "generation", module=args.module)
    if generation_errors:
        for error in generation_errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    changed = materialize(
        qa_root / CONTRACTS,
        qa_root / BRUNO,
        dry_run=args.check,
        execution_config_path=qa_root / EXECUTION / "config.yaml",
        module_filter=args.module,
        sync_index=not args.module,
        check=args.check,
    )
    print(f"{'would materialize' if args.check else 'materialized'} {len(changed)} artifact(s)")
    if args.check and changed:
        return 1
    if not args.check:
        errors = validate_stage(qa_root, "materialization", module=args.module)
        if args.module:
            try:
                snapshot = worker_snapshot_path(qa_root, args.module)
            except ValueError:
                snapshot = None
            if snapshot and snapshot.is_file():
                errors.extend(validate_worker_snapshot(qa_root, args.module, "materialization"))
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        if errors:
            return 1
    return 0


def worker_command(argv: list[str], start: bool) -> int:
    command = "worker-start" if start else "worker-check"
    parser = argparse.ArgumentParser(prog=f"dev-ai api-test {command}")
    qa_root_argument(parser)
    parser.add_argument("--module", required=True)
    if not start:
        parser.add_argument(
            "--stage",
            choices=("generation", "materialization", "pre-execution", "post-execution"),
            default="generation",
        )
    args = parser.parse_args(argv)
    ensure_rule_library(args.qa_root)
    if start:
        try:
            path = write_worker_snapshot(args.qa_root, args.module)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(f"created module-worker snapshot: {path}")
        return 0
    errors = validate_worker_snapshot(args.qa_root, args.module, args.stage)
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    if not errors:
        print(f"module-worker boundary passed: module={args.module} stage={args.stage}")
    return 1 if errors else 0


def aggregate_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="dev-ai api-test aggregate")
    qa_root_argument(parser)
    args = parser.parse_args(argv)
    try:
        from .run_bruno import aggregate_module_results

        report_path, evidence_path, report = aggregate_module_results(args.qa_root)
    except (OSError, ValueError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    summary = report["summary"]
    print(
        f"aggregated total={summary['total']} executed={summary['executed']} passed={summary['passed']} "
        f"failed={summary['failed']} not_executed={summary['not_executed']} status={report['status']}"
    )
    print(f"result_report={report_path}")
    print(f"execution_evidence={evidence_path}")
    return 0 if report["status"] == "verified" else 1


def preflight_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="dev-ai api-test preflight", add_help=False)
    qa_root_argument(parser)
    known, remaining = parser.parse_known_args(argv)
    qa_root = known.qa_root.resolve()
    if (qa_root / ".dev-ai.lock.json").is_file():
        version_code = version_gate(qa_root, "--phase", "before-execute", "--allow-draft")
        if version_code:
            return version_code
    contracts = qa_root / CONTRACTS
    openapi = next(
        (path for path in (contracts / "openapi.json", contracts / "openapi.yaml", contracts / "openapi.yml") if path.is_file()),
        contracts / "openapi.json",
    )
    config = qa_root / EXECUTION / "config.yaml"
    try:
        script_errors = script_bundle_errors(qa_root)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if script_errors:
        for error in script_errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    lock_errors = check_qa_lock(contracts)
    if lock_errors:
        for error in lock_errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    result_root = qa_root / GLOBAL_RESULTS
    log_root = qa_root / LOGS
    result_root.mkdir(parents=True, exist_ok=True)
    log_root.mkdir(parents=True, exist_ok=True)
    static_path = result_root / f"{timestamp}-static-coverage.json"
    report_path = result_root / f"{timestamp}-preflight.json"
    log_path = log_root / f"{timestamp}-preflight.log"
    check = subprocess.run(
        [
            sys.executable,
            "-m", "dev_ai.domains.api_test.check_api_coverage",
            str(contracts),
            str(qa_root / BRUNO),
            "--openapi", str(openapi),
            "--require-scenarios",
            "--require-auth",
            "--execution-config", str(config),
            "--all",
            "--allow-draft-version",
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    try:
        static_report = json.loads(check.stdout)
    except json.JSONDecodeError:
        static_report = None
    if static_report is None:
        log_path.write_text(redact((check.stdout or "") + (check.stderr or "")), encoding="utf-8")
        if check.stderr:
            print(check.stderr.strip(), file=sys.stderr)
        print("ERROR: static coverage did not produce JSON", file=sys.stderr)
        return check.returncode or 1
    static_report = redact(static_report)
    static_path.write_text(json.dumps(static_report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    if check.returncode or static_report.get("static_ok") is not True:
        log_path.write_text(redact((check.stdout or "") + (check.stderr or "")), encoding="utf-8")
        print("ERROR: static coverage validation failed", file=sys.stderr)
        return check.returncode or 1
    options = {value.split("=", 1)[0] for value in remaining if value.startswith("--")}
    command = [sys.executable, "-m", "dev_ai.domains.api_test.runtime_preflight", *remaining]
    if "--qa-root" not in options:
        command.extend(["--qa-root", str(qa_root)])
    if "--execution-config" not in options:
        command.extend(["--execution-config", str(config)])
    if "--openapi" not in options:
        command.extend(["--openapi", str(openapi)])
    if "--static-results" not in options:
        command.extend(["--static-results", str(static_path)])
    if "--output" not in options:
        command.extend(["--output", str(report_path)])
    completed = subprocess.run(
        command, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    log_path.write_text(redact((completed.stdout or "") + (completed.stderr or "")), encoding="utf-8")
    sys.stdout.write(completed.stdout or "")
    sys.stderr.write(completed.stderr or "")
    return completed.returncode


def scripts_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="dev-ai api-test scripts")
    parser.add_argument(
        "action",
        nargs="?",
        choices=("status", "version-init", "version-check", "version-complete"),
        default="status",
    )
    qa_root_argument(parser)
    parser.add_argument("--business-repo", type=Path)
    parser.add_argument("--phase", choices=("before-generate", "before-execute"))
    parser.add_argument("--completion-report", type=Path)
    parser.add_argument("--tests-adapted", action="store_true")
    parser.add_argument("--rules", type=Path)
    args = parser.parse_args(argv)
    from .tool_version import CONTRACT_SCHEMA_VERSION

    if args.action == "status":
        if args.phase or args.completion_report or args.tests_adapted or args.rules or args.business_repo:
            parser.error("version options require a version-* action")
        print(f"shared runtime active; api-test schema={CONTRACT_SCHEMA_VERSION}")
        return 0
    options: list[str]
    if args.action == "version-init":
        if args.phase or args.completion_report or args.tests_adapted or args.rules:
            parser.error("version-init accepts only project path options")
        options = ["--init"]
    elif args.action == "version-check":
        if args.completion_report or args.tests_adapted:
            parser.error("version-check does not accept completion options")
        options = ["--phase", args.phase or "before-generate"]
    else:
        if args.phase:
            parser.error("version-complete always uses the complete phase")
        if args.completion_report is None:
            parser.error("version-complete requires --completion-report")
        options = ["--phase", "complete", "--write", "--completion-report", str(args.completion_report.resolve())]
        if args.tests_adapted:
            options.append("--tests-adapted")
    return version_gate(
        args.qa_root,
        *options,
        business_repo=args.business_repo,
        rules=args.rules,
    )


def mock_data_command(argv: list[str], *, clean: bool) -> int:
    action = "clean" if clean else "generate"
    parser = argparse.ArgumentParser(prog=f"dev-ai api-test mock-data-{action}")
    qa_root_argument(parser)
    parser.add_argument(
        "--module", action="append", default=[],
        help="module id, display name, directory, or OpenAPI Tag; repeat for multiple modules",
    )
    parser.add_argument("--run-id", help="run ledger id (cleanup target, or optional generation id)")
    parser.add_argument(
        "--allow-cleanup" if clean else "--allow-write",
        action="store_true",
        help="explicit authorization required in non-interactive environments",
    )
    args = parser.parse_args(argv)
    return mock_data_operation(
        args.qa_root,
        action=action,
        modules=args.module or None,
        run_id=args.run_id,
        allowed=args.allow_cleanup if clean else args.allow_write,
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="dev-ai api-test")
    commands = (
        "init", "generate", "materialize", "check", "run", "preflight", "reconcile",
        "aggregate", "worker-start", "worker-check", "mock-data-generate", "mock-data-clean", "scripts",
    )
    parser.add_argument("command", nargs="?", choices=commands)
    if not argv or argv[0] in {"-h", "--help"}:
        parser.parse_args(argv)
        return 0
    if argv[0] not in commands:
        parser.error(f"invalid choice: {argv[0]!r} (choose from {', '.join(commands)})")
    command, remainder = argv[0], argv[1:]
    if command == "init":
        return init_command(remainder)
    if command == "generate":
        return generate_command(remainder)
    if command == "materialize":
        return materialize_command(remainder)
    if command == "aggregate":
        return aggregate_command(remainder)
    if command in {"mock-data-generate", "mock-data-clean"}:
        return mock_data_command(remainder, clean=command == "mock-data-clean")
    if command in {"worker-start", "worker-check"}:
        return worker_command(remainder, command == "worker-start")
    if command in {"check", "reconcile"}:
        return coverage_command(remainder, command == "reconcile")
    if command == "run":
        return run_command(remainder)
    if command == "preflight":
        if "-h" in remainder or "--help" in remainder:
            return run_child(
                [sys.executable, "-m", "dev_ai.domains.api_test.runtime_preflight", "--help"], relay_summary=False
            )
        return preflight_command(remainder)
    return scripts_command(remainder)


if __name__ == "__main__":
    raise SystemExit(main())
