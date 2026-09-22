#!/usr/bin/env python3
"""Public CLI for Bruno QA initialization, generation, checks, and execution."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

sys.dont_write_bytecode = True

from .redaction import redact
from .api_test_execution_config import initialize_execution_layout
from .api_test_design_rules import (
    apply_to_contracts,
    build_rules,
    discover,
    understanding_errors,
    summary as design_summary,
)
from .api_test_materialize_missing_bru import materialize
from .api_test_mock_data import command as mock_data_operation
from .api_test_mock_data import derive_mock_data_contracts, write_discovery
from .api_test_parse_openapi import (
    extract,
    constraint_obligations,
    generate_module_map,
    load_document,
    render_manifest,
    write_partitioned,
)
from .api_test_qa_lock import check as check_qa_lock
from .api_test_qa_lock import refresh_generation_state_cases
from .api_test_qa_lock import write as write_qa_lock
from .api_test_qa_paths import (
    BRUNO,
    CONSTRAINTS,
    CONTRACTS,
    EXECUTION,
    GLOBAL_RESULTS,
    LOGS,
    RESULTS,
)
from .api_test_constraints import (
    ensure_rule_library,
    validate_stage,
    validate_worker_snapshot,
    worker_snapshot_path,
    write_worker_snapshot,
)
from .api_test_manifest_io import load_data
from .api_test_value_resolution import (
    apply_environment_values,
    write_value_resolutions,
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
        "dltk.api_test_check_version_compatibility",
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
    parser = argparse.ArgumentParser(prog="dltk api-test init")
    qa_root_argument(parser)
    parser.add_argument("--design-root", action="append", type=Path, default=[])
    parser.add_argument("--design-file", action="append", type=Path, default=[])
    args = parser.parse_args(argv)
    qa_root = args.qa_root.resolve()
    design = discover(qa_root.parent, design_roots=args.design_root, design_files=args.design_file)
    candidates = "\n".join(f"  - {path}" for path in design.candidates) or "  (none)"
    if not design.files:
        print(
            "ERROR: design documents are required; provide --design-root/--design-file. Candidates:\n" + candidates,
            file=sys.stderr,
        )
        return 2
    if not args.design_root and not args.design_file and len(design.candidates) > 1:
        print("ERROR: multiple design roots found; choose one with --design-root:\n" + candidates, file=sys.stderr)
        return 2
    changed = initialize_execution_layout(qa_root, local_scripts=False)
    ensure_rule_library(qa_root)
    (qa_root / CONTRACTS / "modules").mkdir(parents=True, exist_ok=True)
    initial_artifacts = {
        qa_root / CONSTRAINTS / "observed-rules.yaml": {"version": 1, "observations": []},
        qa_root / CONSTRAINTS / "design-rules.yaml": {
            "version": 1,
            "source": "design",
            "documents": [],
            "parser_diagnostics": [],
            "rules": [],
            "understanding": [],
            "mapping": {"items": [], "counts": {
                "exact": 0, "parameter_alias": 0, "semantic_candidate": 0,
                "design_without_openapi": 0, "openapi_without_design": 0, "multiple_candidates": 0,
            }},
            "exclusions": [],
            "flows": [],
            "flow_candidates": [],
            "manual_confirmations": [],
            "understanding_status": "incomplete",
            "design_fingerprint": "0" * 64,
            "openapi_fingerprint": "",
            "coverage": {"openapi_endpoints": 0, "documented_endpoints": 0, "excluded_endpoints": 0},
        },
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


def _design_fingerprint(files: list[Path] | tuple[Path, ...]) -> str:
    values = []
    for path in files:
        values.append(hashlib.sha256(path.read_bytes()).hexdigest())
    return hashlib.sha256("".join(values).encode("utf-8")).hexdigest()


def _write_design_understanding(qa_root: Path, document: dict[str, object]) -> Path:
    path = qa_root / CONSTRAINTS / "design-rules.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_manifest(document, path), encoding="utf-8")
    return path


def _design_report(
    qa_root: Path,
    design_rules: dict[str, object],
    *,
    status: str,
    failures: list[str] | None = None,
    execution: str = "not_started",
) -> Path | None:
    mapping = design_rules.get("mapping", {}) if isinstance(design_rules.get("mapping"), dict) else {}
    counts = mapping.get("counts", {}) if isinstance(mapping, dict) and isinstance(mapping.get("counts"), dict) else {}
    formal_cases: list[dict[str, object]] = []
    protocol_cases: list[dict[str, object]] = []
    pending_case_records: list[dict[str, object]] = []
    contracts = qa_root / CONTRACTS / "modules"
    if contracts.is_dir():
        for module_dir in contracts.glob("*/"):
            cases_path = module_dir / "cases.yaml"
            if not cases_path.is_file():
                continue
            try:
                document = load_data(cases_path)
            except (OSError, ValueError, SystemExit):
                # The generation gate already reports malformed manifests; the
                # audit report must remain writable even when one case file is
                # unreadable.
                continue
            for case in document.get("cases", []) if isinstance(document, dict) else []:
                if not isinstance(case, dict):
                    continue
                waiting = case.get("review_required") is True or bool(case.get("manual_confirmation"))
                record = {
                    "id": str(case.get("id", "")),
                    "endpoint_id": str(case.get("endpoint_id", "")),
                    "scenario": str(case.get("scenario", "")),
                    "source": str(case.get("source", "")),
                    "design_rule_ids": list(case.get("design_rule_ids", [])) if isinstance(case.get("design_rule_ids"), list) else [],
                    "bru": str(case.get("bru", case.get("bru_file", case.get("file_name", "")))),
                }
                if waiting:
                    record["reason"] = case.get("review_reason") or case.get("manual_confirmation")
                    pending_case_records.append(record)
                elif case.get("source") == "design":
                    formal_cases.append(record)
                elif case.get("source") == "openapi":
                    protocol_cases.append(record)
    confirmations = design_rules.get("manual_confirmations", []) if isinstance(design_rules.get("manual_confirmations"), list) else []
    pending_confirmations = len(confirmations)
    uncovered = {
        key: int(counts.get(key, 0) or 0)
        for key in ("design_without_openapi", "openapi_without_design", "multiple_candidates")
    }
    failure_text = "; ".join(dict.fromkeys(str(item) for item in (failures or []) if str(item).strip()))
    understanding_rows = design_rules.get("understanding", []) if isinstance(design_rules.get("understanding"), list) else []
    capability_terms = (
        "concurrent", "concurrency", "bounded final-state polling", "fault injection",
        "runner", "execution mechanism",
    )
    unsupported_rows = [
        {
            "rule_id": item.get("rule_id"),
            "unknown": list(item.get("unknown", [])) if isinstance(item.get("unknown"), list) else [],
            "design_source": item.get("design_source"),
        }
        for item in understanding_rows
        if isinstance(item, dict)
        and item.get("can_generate") is False
        and any(
            term in " ".join(str(value).casefold() for value in item.get("unknown", []))
            for term in capability_terms
        )
    ]
    flow_candidates = design_rules.get("flow_candidates", []) if isinstance(design_rules.get("flow_candidates"), list) else []
    pending_candidates = [
        item for item in flow_candidates
        if isinstance(item, dict) and item.get("can_generate") is False
    ]
    mapping_items = mapping.get("items", []) if isinstance(mapping.get("items"), list) else []
    uncovered_items = [
        item for item in mapping_items
        if isinstance(item, dict) and item.get("category") in uncovered
    ]
    # Pending cases are intentionally excluded: they are candidates awaiting
    # confirmation, not executable tests that merely have not run yet.
    unexecuted = [*formal_cases, *protocol_cases]
    report = {
        "version": 1,
        "source": "design",
        "status": status,
        "execution": execution,
        "matrix_path": str(qa_root / CONSTRAINTS / "design-rules.yaml"),
        "formal_tests": formal_cases,
        "protocol_tests": protocol_cases,
        "pending_cases": pending_case_records,
        "pending_confirmations": confirmations,
        "pending_flow_candidates": pending_candidates,
        "unsupported": unsupported_rows,
        "uncovered_mapping": uncovered_items,
        "unexecuted": unexecuted,
        "gate_failures": list(dict.fromkeys(str(item) for item in (failures or []) if str(item).strip())),
    }
    report_path: Path | None = None
    try:
        report_path = qa_root / RESULTS / "design-generation-report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(redact(report), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        report_path = None
    formal = len(formal_cases)
    protocol = len(protocol_cases)
    pending_cases = len(pending_case_records)
    unsupported = len(unsupported_rows)
    print(
        "design_understanding "
        f"status={status} rules={len(design_rules.get('rules', [])) if isinstance(design_rules.get('rules'), list) else 0} "
        f"formal_cases={formal} protocol_cases={protocol} pending_cases={pending_cases} "
        f"pending_confirmations={pending_confirmations} "
        f"flow_candidates={len(pending_candidates)} "
        f"unsupported={unsupported} uncovered={json.dumps(uncovered, ensure_ascii=False, sort_keys=True)} execution={execution} unexecuted={len(unexecuted)}"
    )
    if failure_text:
        print(f"gate_failures={failure_text}")
    print(f"formal_test_ids={json.dumps([item.get('id') for item in formal_cases], ensure_ascii=False)}")
    print(f"protocol_test_ids={json.dumps([item.get('id') for item in protocol_cases], ensure_ascii=False)}")
    print(f"pending_confirmation_ids={json.dumps([item.get('rule_id') for item in confirmations if isinstance(item, dict)], ensure_ascii=False)}")
    print(f"unsupported_ids={json.dumps([item.get('rule_id') for item in unsupported_rows], ensure_ascii=False)}")
    print(f"unexecuted_ids={json.dumps([item.get('id') for item in unexecuted], ensure_ascii=False)}")
    if report_path:
        print(f"report={report_path}")
    return report_path


def understand_command(argv: list[str]) -> int:
    """Complete and persist the design-understanding phase."""

    parser = argparse.ArgumentParser(prog="dltk api-test understand")
    qa_root_argument(parser)
    parser.add_argument("--openapi", type=Path)
    parser.add_argument("--design-root", action="append", type=Path, default=[])
    parser.add_argument("--design-file", action="append", type=Path, default=[])
    args = parser.parse_args(argv)
    qa_root = args.qa_root.resolve()
    design = discover(qa_root.parent, design_roots=args.design_root, design_files=args.design_file)
    if not design.files:
        print("ERROR: design documents are required; provide --design-root/--design-file", file=sys.stderr)
        _design_report(qa_root, {}, status="blocked", failures=["no design source"])
        return 2
    source_spec = args.openapi.resolve() if args.openapi else next(
        (path for path in (qa_root / CONTRACTS / "openapi.json", qa_root / CONTRACTS / "openapi.yaml", qa_root / CONTRACTS / "openapi.yml") if path.is_file()),
        None,
    )
    if source_spec is None or not source_spec.is_file():
        print("ERROR: a valid local OpenAPI contract is required for design understanding", file=sys.stderr)
        _design_report(qa_root, {}, status="blocked", failures=["OpenAPI contract is missing"])
        return 2
    try:
        source_document = load_document(source_spec)
        manifest = extract(source_spec, source_document)
        document, errors = build_rules(qa_root.parent, design.files, manifest, qa_root=qa_root)
    except (OSError, ValueError, TypeError, SystemExit) as exc:
        print(f"ERROR: cannot complete design understanding: {exc}", file=sys.stderr)
        _design_report(qa_root, {}, status="blocked", failures=[str(exc)])
        return 2
    _write_design_understanding(qa_root, document)
    failures = [*errors, *understanding_errors(document)]
    _design_report(qa_root, document, status="complete" if not failures else "blocked", failures=failures)
    return 2 if failures else 0


def generate_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="dltk api-test generate")
    qa_root_argument(parser)
    parser.add_argument("--openapi", type=Path)
    parser.add_argument("--module-map", type=Path)
    parser.add_argument("--incremental", action="store_true")
    parser.add_argument("--no-seed-cases", action="store_true")
    parser.add_argument("--source-root", action="append", type=Path, default=[])
    parser.add_argument("--design-root", action="append", type=Path, default=[])
    parser.add_argument("--design-file", action="append", type=Path, default=[])
    parser.add_argument("--coverage-profile", choices=("contract-draft", "full-matrix"))
    parser.add_argument("--base-url", action="append", default=[])
    parser.add_argument("--port", action="append", type=int, default=[])
    parser.add_argument("--path", action="append", dest="openapi_paths", default=[])
    parser.add_argument("--timeout", type=float, default=2.0)
    args = parser.parse_args(argv)
    qa_root = args.qa_root.resolve()
    design = discover(qa_root.parent, design_roots=args.design_root, design_files=args.design_file)
    if not design.files:
        candidates = "\n".join(f"  - {path}" for path in design.candidates) or "  (none)"
        print(
            "ERROR: design documents are required; provide --design-root/--design-file. Candidates:\n" + candidates,
            file=sys.stderr,
        )
        _design_report(qa_root, {}, status="blocked", failures=["no design source"])
        return 2
    if not args.design_root and not args.design_file and len(design.candidates) > 1:
        candidates = "\n".join(f"  - {path}" for path in design.candidates)
        print("ERROR: multiple design roots found; choose one with --design-root:\n" + candidates, file=sys.stderr)
        _design_report(qa_root, {}, status="blocked", failures=["multiple design roots require an explicit selector"])
        return 2
    initialize_execution_layout(qa_root, local_scripts=False)
    ensure_rule_library(qa_root)
    if (qa_root / ".dltk.lock.json").is_file():
        version_code = version_gate(qa_root, "--phase", "before-generate")
        if version_code:
            return version_code
    from .api_test_execution_config import environment_file, load_bruno_environment, load_execution_config

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
            "-m", "dltk.api_test_fetch_local_openapi",
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
            _design_report(qa_root, {}, status="blocked", failures=[f"OpenAPI fetch failed with exit code {fetch_code}"])
            return fetch_code
    if not source_spec.is_file():
        message = f"offline OpenAPI document does not exist: {source_spec}"
        print(f"ERROR: {message}", file=sys.stderr)
        _design_report(qa_root, {}, status="blocked", failures=[message])
        return 2
    saved_spec = contracts / ("openapi.yaml" if source_spec.suffix.lower() in {".yaml", ".yml"} else "openapi.json")
    if source_spec != saved_spec.resolve() and (not saved_spec.is_file() or source_spec.read_bytes() != saved_spec.read_bytes()):
        shutil.copy2(source_spec, saved_spec)
    try:
        source_document = load_document(saved_spec)
        manifest = extract(saved_spec, source_document)
    except (OSError, ValueError, TypeError, SystemExit) as exc:
        message = f"invalid OpenAPI contract: {exc}"
        print(f"ERROR: {message}", file=sys.stderr)
        _design_report(qa_root, {}, status="blocked", failures=[message])
        return 2
    design_path = qa_root / CONSTRAINTS / "design-rules.yaml"
    existing_design = load_data(design_path) if design_path.is_file() else {}
    current_fingerprint = _design_fingerprint(design.files)
    reusable = (
        isinstance(existing_design, dict)
        and existing_design.get("understanding_status") == "complete"
        and existing_design.get("design_fingerprint") == current_fingerprint
        and str(existing_design.get("openapi_fingerprint", "")) == str(manifest.get("source", {}).get("sha256", ""))
    )
    if reusable:
        design_rules = existing_design
        design_errors = understanding_errors(design_rules)
    else:
        design_rules, design_errors = build_rules(
            qa_root.parent,
            design.files,
            manifest,
            qa_root=qa_root,
        )
        _write_design_understanding(qa_root, design_rules)
        design_errors = [*design_errors, *understanding_errors(design_rules)]
    if design_errors:
        for error in design_errors:
            print(f"ERROR: {error}", file=sys.stderr)
        _design_report(qa_root, design_rules, status="blocked", failures=design_errors)
        return 2
    if args.source_root:
        missing = [str(path) for path in args.source_root if not path.is_dir()]
        if missing:
            parser.error("source root(s) do not exist: " + ", ".join(missing))
        # Source discovery is execution preparation only.  It is never applied
        # to endpoint schemas, expected results, or business logic.
        write_discovery(qa_root, args.source_root)
    environment_path = environment_file(qa_root / EXECUTION / "config.yaml", execution_config)
    if environment_path.is_file():
        apply_environment_values(manifest, load_bruno_environment(environment_path))
    for endpoint in manifest.get("endpoints", []):
        if isinstance(endpoint, dict):
            endpoint["obligations"] = constraint_obligations(endpoint)
    module_map = args.module_map.resolve() if args.module_map else contracts / "module-map.yaml"
    if not module_map.is_file():
        module_map.parent.mkdir(parents=True, exist_ok=True)
        module_map.write_text(render_manifest(generate_module_map(manifest), module_map), encoding="utf-8")
        print(f"created {module_map}; review Tag ownership before relying on generated coverage")
    try:
        summary = write_partitioned(
            manifest,
            module_map,
            contracts / "modules",
            seed_cases=not args.no_seed_cases,
            incremental=args.incremental,
            coverage_profile=coverage_profile,
            design_sha256=design_summary(design_rules)["sha256"],
        )
        apply_to_contracts(contracts, design_rules)
        if (contracts / "generation-state.yaml").is_file():
            refresh_generation_state_cases(contracts)
        derive_mock_data_contracts(qa_root)
        if (contracts / "generation-state.yaml").is_file():
            refresh_generation_state_cases(contracts)
        write_value_resolutions(qa_root)
        materialize(contracts, qa_root / BRUNO, execution_config_path=qa_root / EXECUTION / "config.yaml")
        write_value_resolutions(qa_root)
        write_qa_lock(contracts)
        version_lock = contracts / "version-lock.yaml"
        if version_lock.is_file():
            lock = load_document(version_lock)
            if isinstance(lock, dict):
                lock = dict(lock)
                lock["openapi"] = {
                    "file": str(saved_spec.relative_to(contracts)),
                    "sha256": manifest.get("source", {}).get("sha256"),
                }
                lock["design"] = design_summary(design_rules)
                version_lock.write_text(render_manifest(lock, version_lock), encoding="utf-8")
    except (OSError, TypeError, ValueError, SystemExit) as exc:
        message = f"generation failed: {exc}"
        print(f"ERROR: {message}", file=sys.stderr)
        _design_report(qa_root, design_rules, status="failed", failures=[message])
        return 2
    constraint_errors = [
        *validate_stage(qa_root, "generation"),
        *validate_stage(qa_root, "materialization"),
    ]
    if constraint_errors:
        for error in dict.fromkeys(constraint_errors):
            print(f"ERROR: {error}", file=sys.stderr)
        _design_report(qa_root, design_rules, status="blocked", failures=constraint_errors)
        return 2
    generated_cases = 0
    protocol_cases = 0
    pending_cases = 0
    for module_dir in sorted((contracts / "modules").glob("*/")):
        case_path = module_dir / "cases.yaml"
        if not case_path.is_file():
            continue
        case_document = load_document(case_path)
        for case in case_document.get("cases", []) if isinstance(case_document, dict) else []:
            if not isinstance(case, dict):
                continue
            is_pending = case.get("review_required") is True or case.get("manual_confirmation")
            if case.get("source") == "design" and not is_pending:
                generated_cases += 1
            elif case.get("source") == "openapi":
                protocol_cases += 1
            if is_pending:
                pending_cases += 1
    pending_rules = len(design_rules.get("manual_confirmations", []))
    flow_candidates = len(design_rules.get("flow_candidates", []))
    mapping_counts = design_rules.get("mapping", {}).get("counts", {}) if isinstance(design_rules.get("mapping"), dict) else {}
    uncovered = {
        key: int(mapping_counts.get(key, 0) or 0)
        for key in ("design_without_openapi", "openapi_without_design", "multiple_candidates")
    }
    print(
        "design_understanding "
        "status=complete "
        f"rules={len(design_rules.get('rules', []))} "
        f"formal_cases={generated_cases} protocol_cases={protocol_cases} "
        f"pending_cases={pending_cases} pending_confirmations={pending_rules} flow_candidates={sum(1 for item in design_rules.get('flow_candidates', []) if isinstance(item, dict) and not item.get('can_generate'))} "
        f"unsupported={sum(1 for item in design_rules.get('understanding', []) if isinstance(item, dict) and item.get('can_generate') is False)} "
        f"uncovered={json.dumps(uncovered, ensure_ascii=False, sort_keys=True)} execution=not_started unexecuted=all"
    )
    print(
        f"generated endpoints={len(manifest['endpoints'])} changed_modules={len(summary['changed_modules'])} "
        f"skipped_modules={len(summary['skipped_modules'])}"
    )
    for endpoint_id in summary["deleted_endpoint_ids"]:
        print(f"REVIEW: remove the .bru file for deleted endpoint {endpoint_id}", file=sys.stderr)
    for case_id in summary["manual_review_cases"]:
        print(f"REVIEW: preserved manually modified case {case_id}", file=sys.stderr)
    _design_report(qa_root, design_rules, status="complete", execution="not_started")
    if is_loopback_openapi(source_document):
        print("OpenAPI came from a running local service; starting the required execution attempt")
        execution_code = run_command(["--qa-root", str(qa_root)])
        _design_report(
            qa_root,
            design_rules,
            status="complete" if execution_code == 0 else "failed",
            execution="completed" if execution_code == 0 else "failed",
            failures=[] if execution_code == 0 else [f"execution failed with exit code {execution_code}"],
        )
        return execution_code
    return 0


def coverage_command(argv: list[str], reconcile: bool) -> int:
    parser = argparse.ArgumentParser(prog=f"dltk api-test {'reconcile' if reconcile else 'check'}")
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
        "-m", "dltk.api_test_check_api_coverage",
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
    if (qa_root / ".dltk.lock.json").is_file():
        command.append("--allow-draft-version")
    return run_child(command)


def run_command(argv: list[str]) -> int:
    extra = list(argv)
    if "-h" in extra or "--help" in extra:
        return run_child(
            [sys.executable, "-m", "dltk.api_test_run_bruno", "--help"], relay_summary=False
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
    return run_child([sys.executable, "-m", "dltk.api_test_run_bruno", *extra])


def materialize_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="dltk api-test materialize")
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
    parser = argparse.ArgumentParser(prog=f"dltk api-test {command}")
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
    parser = argparse.ArgumentParser(prog="dltk api-test aggregate")
    qa_root_argument(parser)
    args = parser.parse_args(argv)
    try:
        from .api_test_run_bruno import aggregate_module_results

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
    parser = argparse.ArgumentParser(prog="dltk api-test preflight", add_help=False)
    qa_root_argument(parser)
    known, remaining = parser.parse_known_args(argv)
    qa_root = known.qa_root.resolve()
    if (qa_root / ".dltk.lock.json").is_file():
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
            "-m", "dltk.api_test_check_api_coverage",
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
    command = [sys.executable, "-m", "dltk.api_test_runtime_preflight", *remaining]
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
    parser = argparse.ArgumentParser(prog="dltk api-test scripts")
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
    from .api_test_tool_version import CONTRACT_SCHEMA_VERSION

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
    parser = argparse.ArgumentParser(prog=f"dltk api-test mock-data-{action}")
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
    parser = argparse.ArgumentParser(prog="dltk api-test")
    commands = (
        "init", "understand", "generate", "materialize", "check", "run", "preflight", "reconcile",
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
    if command == "understand":
        return understand_command(remainder)
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
                [sys.executable, "-m", "dltk.api_test_runtime_preflight", "--help"], relay_summary=False
            )
        return preflight_command(remainder)
    return scripts_command(remainder)


if __name__ == "__main__":
    raise SystemExit(main())
