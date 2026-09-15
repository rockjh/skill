#!/usr/bin/env python3
"""Public CLI for Bruno QA initialization, generation, checks, and execution."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True

from execution_config import initialize_execution_layout
from analyze_source_logic import apply_candidates, scan as scan_source_logic
from materialize_missing_bru import materialize
from parse_openapi import (
    extract,
    generate_module_map,
    load_document,
    render_manifest,
    write_partitioned,
)
from qa_lock import check as check_qa_lock
from qa_lock import refresh_generation_state_cases
from qa_lock import write as write_qa_lock
from scripts_manager import check_scripts, sync_scripts


SCRIPTS_ROOT = Path(__file__).resolve().parent


def qa_root_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--qa-root", type=Path, default=Path("qa"))


def shared_cli_mode(qa_root: Path) -> bool:
    config_path = qa_root / "execution" / "config.yaml"
    if not config_path.is_file():
        return False
    try:
        from execution_config import load_execution_config

        return load_execution_config(config_path)["tooling"] == "shared-cli"
    except ValueError as exc:
        raise ValueError(f"cannot parse {config_path}: {exc}") from exc


def script_bundle_errors(qa_root: Path) -> list[str]:
    return [] if shared_cli_mode(qa_root) else check_scripts(qa_root, SCRIPTS_ROOT)


def init_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="mno-bruno-qa init")
    qa_root_argument(parser)
    parser.add_argument("--shared-cli", action="store_true", help="keep only QA assets and use the installed CLI")
    args = parser.parse_args(argv)
    changed = initialize_execution_layout(args.qa_root, local_scripts=False if args.shared_cli else None)
    (args.qa_root / "contracts" / "modules").mkdir(parents=True, exist_ok=True)
    print(f"initialized {args.qa_root} ({len(changed)} file(s) changed)")
    return 0


def generate_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="mno-bruno-qa generate")
    qa_root_argument(parser)
    parser.add_argument("--openapi", type=Path)
    parser.add_argument("--module-map", type=Path)
    parser.add_argument("--incremental", action="store_true")
    parser.add_argument("--no-seed-cases", action="store_true")
    parser.add_argument("--source-root", action="append", type=Path, default=[])
    parser.add_argument("--coverage-profile", choices=("contract-draft", "full-matrix"))
    parser.add_argument("--shared-cli", action="store_true", help="do not copy project-local Python scripts")
    args = parser.parse_args(argv)
    qa_root = args.qa_root.resolve()
    initialize_execution_layout(qa_root, local_scripts=False if args.shared_cli else None)
    from execution_config import load_execution_config

    execution_config = load_execution_config(qa_root / "execution" / "config.yaml")
    coverage_profile = args.coverage_profile or execution_config["coverage_profile"]
    contracts = qa_root / "contracts"
    contracts.mkdir(parents=True, exist_ok=True)
    source_spec = (args.openapi or (contracts / "openapi.json")).resolve()
    if not source_spec.is_file():
        parser.error(f"offline OpenAPI document does not exist: {source_spec}")
    saved_spec = contracts / ("openapi.yaml" if source_spec.suffix.lower() in {".yaml", ".yml"} else "openapi.json")
    if source_spec != saved_spec.resolve() and (not saved_spec.is_file() or source_spec.read_bytes() != saved_spec.read_bytes()):
        shutil.copy2(source_spec, saved_spec)
    manifest = extract(saved_spec, load_document(saved_spec))
    module_map = (args.module_map or (contracts / "module-map.yaml")).resolve()
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
    if args.source_root:
        missing = [str(path) for path in args.source_root if not path.is_dir()]
        if missing:
            parser.error("source root(s) do not exist: " + ", ".join(missing))
        candidates = scan_source_logic(args.source_root)
        unresolved = apply_candidates(candidates, contracts)
        if unresolved:
            for candidate_id in unresolved:
                print(
                    f"ERROR: source candidate {candidate_id} cannot be uniquely mapped to an endpoint",
                    file=sys.stderr,
                )
            return 2
        refresh_generation_state_cases(contracts)
    materialize(contracts, qa_root / "bruno", execution_config_path=qa_root / "execution" / "config.yaml")
    write_qa_lock(contracts)
    print(
        f"generated endpoints={len(manifest['endpoints'])} changed_modules={len(summary['changed_modules'])} "
        f"skipped_modules={len(summary['skipped_modules'])}"
    )
    for endpoint_id in summary["deleted_endpoint_ids"]:
        print(f"REVIEW: remove the .bru file for deleted endpoint {endpoint_id}", file=sys.stderr)
    for case_id in summary["manual_review_cases"]:
        print(f"REVIEW: preserved manually modified case {case_id}", file=sys.stderr)
    return 0


def coverage_command(argv: list[str], reconcile: bool) -> int:
    parser = argparse.ArgumentParser(prog=f"mno-bruno-qa {'reconcile' if reconcile else 'check'}")
    qa_root_argument(parser)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", action="store_true")
    scope.add_argument("--module")
    parser.add_argument("--results", type=Path)
    parser.add_argument("--preflight-results", type=Path)
    args = parser.parse_args(argv)
    if reconcile and (not args.results or not args.preflight_results):
        parser.error("reconcile requires --results and --preflight-results")
    qa_root = args.qa_root.resolve()
    openapi = next(
        (path for path in (qa_root / "contracts" / "openapi.json", qa_root / "contracts" / "openapi.yaml", qa_root / "contracts" / "openapi.yml") if path.is_file()),
        qa_root / "contracts" / "openapi.json",
    )
    try:
        script_errors = script_bundle_errors(qa_root)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    lock_errors = check_qa_lock(qa_root / "contracts")
    if script_errors or lock_errors:
        for error in [*script_errors, *lock_errors]:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    command = [
        sys.executable,
        str(SCRIPTS_ROOT / "check_api_coverage.py"),
        str(qa_root / "contracts"),
        str(qa_root / "bruno"),
        "--openapi", str(openapi),
        "--require-scenarios",
        "--require-auth",
        "--execution-config", str(qa_root / "execution" / "config.yaml"),
    ]
    if args.module:
        command.extend(["--module", args.module])
    else:
        command.append("--all")
    if args.results:
        command.extend(["--results", str(args.results)])
    if args.preflight_results:
        command.extend(["--preflight-results", str(args.preflight_results)])
    return subprocess.run(command, check=False).returncode


def run_command(argv: list[str]) -> int:
    extra = list(argv)
    if "-h" in extra or "--help" in extra:
        return subprocess.run([sys.executable, str(SCRIPTS_ROOT / "run_bruno.py"), "--help"], check=False).returncode
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
    return subprocess.run([sys.executable, str(SCRIPTS_ROOT / "run_bruno.py"), *extra], check=False).returncode


def materialize_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="mno-bruno-qa materialize")
    qa_root_argument(parser)
    parser.add_argument("--module")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    qa_root = args.qa_root.resolve()
    changed = materialize(
        qa_root / "contracts",
        qa_root / "bruno",
        dry_run=args.check,
        execution_config_path=qa_root / "execution" / "config.yaml",
        module_filter=args.module,
        sync_index=not args.module,
        check=args.check,
    )
    print(f"{'would materialize' if args.check else 'materialized'} {len(changed)} artifact(s)")
    return 1 if args.check and changed else 0


def preflight_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="mno-bruno-qa preflight", add_help=False)
    qa_root_argument(parser)
    known, remaining = parser.parse_known_args(argv)
    qa_root = known.qa_root.resolve()
    contracts = qa_root / "contracts"
    openapi = next(
        (path for path in (contracts / "openapi.json", contracts / "openapi.yaml", contracts / "openapi.yml") if path.is_file()),
        contracts / "openapi.json",
    )
    config = qa_root / "execution" / "config.yaml"
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
    with tempfile.TemporaryDirectory(prefix="qa-preflight-") as directory:
        static_path = Path(directory) / "static-coverage.json"
        check = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS_ROOT / "check_api_coverage.py"),
                str(contracts),
                str(qa_root / "bruno"),
                "--openapi", str(openapi),
                "--require-scenarios",
                "--require-auth",
                "--execution-config", str(config),
                "--all",
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
            if check.stderr:
                print(check.stderr.strip(), file=sys.stderr)
            print("ERROR: static coverage did not produce JSON", file=sys.stderr)
            return check.returncode or 1
        static_path.write_text(json.dumps(static_report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
        if check.returncode or static_report.get("static_ok") is not True:
            print("ERROR: static coverage validation failed", file=sys.stderr)
            return check.returncode or 1
        options = {value for value in remaining if value.startswith("--")}
        command = [sys.executable, str(SCRIPTS_ROOT / "runtime_preflight.py"), *remaining]
        if "--execution-config" not in options:
            command.extend(["--execution-config", str(config)])
        if "--openapi" not in options:
            command.extend(["--openapi", str(openapi)])
        if "--static-results" not in options:
            command.extend(["--static-results", str(static_path)])
        return subprocess.run(command, check=False).returncode


def scripts_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="mno-bruno-qa scripts")
    parser.add_argument("action", choices=("sync", "check"))
    qa_root_argument(parser)
    args = parser.parse_args(argv)
    if shared_cli_mode(args.qa_root):
        if args.action == "sync":
            print("shared-cli is active; no project-local scripts need synchronization")
        else:
            print("shared-cli is active; installed mno-bruno-qa provides the scripts")
        return 0
    if args.action == "sync":
        changed = sync_scripts(args.qa_root, SCRIPTS_ROOT)
        print(f"synchronized {len(changed)} file(s)")
        return 0
    errors = check_scripts(args.qa_root, SCRIPTS_ROOT)
    for error in errors:
        print(f"ERROR: {error}")
    if not errors:
        print("scripts are current")
    return 0 if not errors else 1


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="mno-bruno-qa")
    commands = ("init", "generate", "materialize", "check", "run", "preflight", "reconcile", "scripts")
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
    if command in {"check", "reconcile"}:
        return coverage_command(remainder, command == "reconcile")
    if command == "run":
        return run_command(remainder)
    if command == "preflight":
        if "-h" in remainder or "--help" in remainder:
            return subprocess.run([sys.executable, str(SCRIPTS_ROOT / "runtime_preflight.py"), "--help"], check=False).returncode
        return preflight_command(remainder)
    return scripts_command(remainder)


if __name__ == "__main__":
    raise SystemExit(main())
