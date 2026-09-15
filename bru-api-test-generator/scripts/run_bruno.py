#!/usr/bin/env python3
"""Internal cross-platform Bruno execution pipeline used by run.bat and run.sh."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

from execution_config import (
    RUNTIME_CONFIG_ENV,
    environment_file,
    load_bruno_environment_document,
    load_execution_config,
    render_runtime_environment,
    runtime_payload,
)
from manifest_io import first_list, load_data
from qa_lock import check as check_qa_lock


RISK_CLASSES = {"read-only", "isolated-write", "destructive", "external-side-effect"}


def module_directory(contracts_root: Path, requested: str) -> str:
    module_map_path = contracts_root / "module-map.yaml"
    module_map = load_data(module_map_path) if module_map_path.is_file() else {}
    metadata = {
        str(item.get("id")): item
        for item in (module_map.get("modules", []) if isinstance(module_map, dict) else [])
        if isinstance(item, dict) and item.get("id")
    }
    index_path = contracts_root / "index.yaml"
    index = load_data(index_path) if index_path.is_file() else {}
    for entry in first_list(index, "modules"):
        module_id = str(entry.get("id", ""))
        directory = str(entry.get("directory", module_id))
        details = metadata.get(module_id, {})
        names = {
            module_id,
            directory,
            str(entry.get("name", "")),
            str(entry.get("swagger_tag", "")),
            str(details.get("name", "")),
            str(details.get("tag", "")),
            *[str(tag) for tag in details.get("swagger_tags", [])],
        }
        if requested in names:
            return directory
    raise ValueError(f"unknown module: {requested}")


def representative_route(
    contracts_root: Path,
    module: str | None,
) -> tuple[str, str] | None:
    modules_root = contracts_root / "modules"
    directories = [modules_root / module] if module else sorted(path for path in modules_root.iterdir() if path.is_dir())
    candidates: list[tuple[str, str]] = []
    fallback: list[tuple[str, str]] = []
    for directory in directories:
        endpoint_path = directory / "endpoints.yaml"
        if not endpoint_path.is_file():
            continue
        for endpoint in first_list(load_data(endpoint_path), "endpoints"):
            method = str(endpoint.get("method", "")).upper()
            path = str(endpoint.get("path", ""))
            if not path.startswith("/") or "{" in path:
                continue
            if method in {"GET", "HEAD"}:
                candidates.append((method, path))
            elif method in {"POST", "PUT", "PATCH", "DELETE"}:
                fallback.append((method, path))
    if candidates:
        return candidates[0]
    return fallback[0] if fallback else None


def coverage_command(
    scripts_root: Path,
    contracts_root: Path,
    bruno_root: Path,
    openapi: Path,
    config_path: Path,
    module: str | None,
    results: Path | None = None,
    preflight: Path | None = None,
) -> list[str]:
    command = [
        sys.executable,
        str(scripts_root / "check_api_coverage.py"),
        str(contracts_root),
        str(bruno_root),
        "--openapi", str(openapi),
        "--require-scenarios",
        "--require-auth",
        "--execution-config", str(config_path),
        "--json",
    ]
    if module:
        command.extend(["--module", module])
    else:
        command.append("--all")
    if results:
        command.extend(["--results", str(results)])
    if preflight:
        command.extend(["--preflight-results", str(preflight)])
    return command


def scope_risks(contracts_root: Path, module: str | None) -> set[str]:
    modules_root = contracts_root / "modules"
    directories = [modules_root / module] if module else sorted(
        path for path in modules_root.iterdir() if path.is_dir()
    )
    risks: set[str] = set()
    for directory in directories:
        cases_path = directory / "cases.yaml"
        endpoints_path = directory / "endpoints.yaml"
        if not cases_path.is_file():
            continue
        endpoints = {
            str(item.get("id")): item
            for item in first_list(load_data(endpoints_path), "endpoints")
        } if endpoints_path.is_file() else {}
        for case in first_list(load_data(cases_path), "cases"):
            risk = str(case.get("risk", "")).strip().lower()
            if not risk:
                endpoint = endpoints.get(str(case.get("endpoint_id")), {})
                risk = "read-only" if str(endpoint.get("method", "GET")).upper() in {"GET", "HEAD", "OPTIONS"} else "isolated-write"
            if risk not in RISK_CLASSES:
                raise ValueError(f"case {case.get('id')} has invalid risk class {risk}")
            risks.add(risk)
    return risks


def confirmation_error(
    risks: set[str],
    confirm_write: bool,
    confirm_destructive: bool,
    confirm_external: bool,
) -> str | None:
    required: list[str] = []
    if risks & {"isolated-write", "destructive"} and not confirm_write:
        required.append("--confirm-write")
    if "destructive" in risks and not confirm_destructive:
        required.append("--confirm-destructive")
    if "external-side-effect" in risks and not confirm_external:
        required.append("--confirm-external")
    if not required:
        return None
    selected = " / ".join(sorted(risks - {"read-only"}))
    return (
        f"selected scope includes {selected} cases. "
        f"Rerun with {' '.join(required)}."
    )


def run_json(command: list[str], output: Path, cwd: Path | None = None) -> tuple[int, dict[str, Any] | None]:
    completed = subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True, encoding="utf-8")
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError:
        document = None
    if document is not None:
        output.write_text(json.dumps(document, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    return completed.returncode, document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qa-root", type=Path, help=argparse.SUPPRESS)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", action="store_true", help="run every business request")
    scope.add_argument("--module", help="module id, display name, OpenAPI Tag, or directory")
    parser.add_argument("--confirm-write", action="store_true")
    parser.add_argument("--confirm-destructive", action="store_true")
    parser.add_argument("--confirm-external", action="store_true")
    parser.add_argument("--bruno-cli", default="bru", help=argparse.SUPPRESS)
    args = parser.parse_args()

    scripts_root = Path(__file__).resolve().parent
    qa_root = (args.qa_root or scripts_root.parent).resolve()
    app_root = qa_root.parent
    contracts_root = qa_root / "contracts"
    bruno_root = qa_root / "bruno"
    config_path = qa_root / "execution" / "config.yaml"
    openapi = next(
        (path for path in (contracts_root / "openapi.json", contracts_root / "openapi.yaml", contracts_root / "openapi.yml") if path.is_file()),
        contracts_root / "openapi.json",
    )
    try:
        config = load_execution_config(config_path)
        env_path = environment_file(config_path, config)
        environment = load_bruno_environment_document(env_path)
        selected_directory = module_directory(contracts_root, args.module) if args.module else None
        risks = scope_risks(contracts_root, selected_directory)
        confirmation = confirmation_error(
            risks,
            args.confirm_write,
            args.confirm_destructive,
            args.confirm_external,
        )
        if confirmation:
            raise ValueError(confirmation)
    except (OSError, ValueError, SystemExit) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    route = representative_route(contracts_root, selected_directory)
    if route is None:
        scope = args.module or "all modules"
        print(f"ERROR: no representative route is available for {scope}", file=sys.stderr)
        return 2

    qa_lock_errors = check_qa_lock(contracts_root)
    if qa_lock_errors:
        for error in qa_lock_errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2

    version_check = subprocess.run(
        [
            sys.executable,
            str(scripts_root / "check_version_compatibility.py"),
            str(app_root),
            str(contracts_root),
            "--phase", "before-execute",
        ],
        check=False,
    )
    if version_check.returncode:
        return version_check.returncode

    with tempfile.TemporaryDirectory(prefix="qa-bruno-") as directory:
        temporary = Path(directory)
        static_path = temporary / "static-coverage.json"
        preflight_path = temporary / "preflight.json"
        raw_report = temporary / "bruno-report.json"
        evidence_path = temporary / "execution-evidence.json"
        coverage_path = temporary / "execution-evidence.coverage.json"

        static_code, static_report = run_json(
            coverage_command(
                scripts_root,
                contracts_root,
                bruno_root,
                openapi,
                config_path,
                args.module,
            ),
            static_path,
        )
        if static_code or not static_report or static_report.get("static_ok") is not True:
            print("ERROR: static coverage validation failed", file=sys.stderr)
            return static_code or 1

        preflight_command = [
            sys.executable,
            str(scripts_root / "runtime_preflight.py"),
            "--execution-config", str(config_path),
            "--env-file", str(env_path),
            "--openapi", str(openapi),
            "--static-results", str(static_path),
            "--bruno-cli", args.bruno_cli,
            "--public-method", route[0],
            "--public-path", route[1],
            "--public-status", "200-403,405-499",
            "--output", str(preflight_path),
        ]
        preflight_code = subprocess.run(preflight_command, check=False).returncode
        if preflight_code:
            print("ERROR: runtime preflight failed", file=sys.stderr)
            return preflight_code

        runtime_env_path = temporary / "runtime-environment.bru"
        runtime_env_path.write_text(render_runtime_environment(environment), encoding="utf-8")
        bruno_command = ["run", selected_directory or "."]
        bruno_command.extend([
            "--env-file", str(runtime_env_path),
            "--env-var", f"{RUNTIME_CONFIG_ENV}={runtime_payload(config, environment)}",
            "--reporter-json", str(raw_report),
            "--reporter-skip-all-headers",
            "--reporter-skip-body",
            "-r",
        ])
        bruno_result = subprocess.run(
            [args.bruno_cli, *bruno_command],
            cwd=bruno_root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if not raw_report.is_file():
            print(
                f"ERROR: Bruno exited with code {bruno_result.returncode} without a JSON report; "
                "console output was suppressed because it may contain credentials",
                file=sys.stderr,
            )
            return bruno_result.returncode or 1

        normalize_result = subprocess.run(
            [
                sys.executable,
                str(scripts_root / "normalize_bruno_report.py"),
                str(raw_report),
                "--output", str(evidence_path),
                "--environment", config["active_environment"],
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if normalize_result.returncode:
            print("ERROR: Bruno report normalization failed", file=sys.stderr)
            return normalize_result.returncode

        coverage_code, coverage_report = run_json(
            coverage_command(
                scripts_root,
                contracts_root,
                bruno_root,
                openapi,
                config_path,
                args.module,
                evidence_path,
                preflight_path,
            ),
            coverage_path,
        )
        if coverage_report:
            scope = f"module {args.module}" if args.module else "all modules"
            status_key = "module_status" if args.module else "status"
            print(
                f"scope={scope} environment={config['active_environment']} "
                f"{status_key}={coverage_report.get(status_key)} risks={','.join(sorted(risks))} "
                f"executed={coverage_report.get('executed_cases', 0)} passed={coverage_report.get('passed_cases', 0)}"
            )
        if bruno_result.returncode:
            print(
                f"ERROR: Bruno exited with code {bruno_result.returncode}; "
                "console output was suppressed because it may contain credentials",
                file=sys.stderr,
            )
        completion_key = "module_completion_ok" if args.module else "completion_ok"
        if bruno_result.returncode or coverage_code or not coverage_report or coverage_report.get(completion_key) is not True:
            return bruno_result.returncode or coverage_code or 1

        if not args.module:
            lock_result = subprocess.run(
                [
                    sys.executable,
                    str(scripts_root / "check_version_compatibility.py"),
                    str(app_root),
                    str(contracts_root),
                    "--phase", "complete",
                    "--completion-report", str(coverage_path),
                    "--write",
                    "--tests-adapted",
                ],
                check=False,
            )
            if lock_result.returncode:
                return lock_result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
