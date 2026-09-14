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


def representative_route(contracts_root: Path, module: str | None) -> tuple[str, str] | None:
    modules_root = contracts_root / "modules"
    directories = [modules_root / module] if module else sorted(path for path in modules_root.iterdir() if path.is_dir())
    candidates: list[tuple[str, str]] = []
    for directory in directories:
        endpoint_path = directory / "endpoints.yaml"
        if not endpoint_path.is_file():
            continue
        for endpoint in first_list(load_data(endpoint_path), "endpoints"):
            method = str(endpoint.get("method", "")).upper()
            path = str(endpoint.get("path", ""))
            if method in {"GET", "HEAD"} and path.startswith("/") and "{" not in path:
                candidates.append((method, path))
    return candidates[0] if candidates else None


def coverage_command(
    scripts_root: Path,
    contracts_root: Path,
    bruno_root: Path,
    openapi: Path,
    config_path: Path,
    module: str | None,
    risks: set[str],
    plan_name: str | None = None,
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
    if plan_name:
        command.extend(["--plan", plan_name])
    else:
        for risk in sorted(risks):
            command.extend(["--risk", risk])
    if risks & {"destructive", "external-side-effect"}:
        command.append("--allow-dangerous")
    if module:
        command.extend(["--module", module])
    if results:
        command.extend(["--results", str(results)])
    if preflight:
        command.extend(["--preflight-results", str(preflight)])
    return command


def execution_scope(
    plans_path: Path,
    plan_name: str | None,
    requested_risks: list[str] | None,
) -> tuple[set[str], str | None, dict[str, Any]]:
    if plan_name and requested_risks:
        raise ValueError("--plan and --risk cannot be combined")
    if not plan_name:
        return set(requested_risks or ["read-only"]), None, {}
    document = load_data(plans_path)
    plans = document.get("plans", {}) if isinstance(document, dict) else {}
    plan = plans.get(plan_name) if isinstance(plans, dict) else None
    if not isinstance(plan, dict):
        raise ValueError(f"unknown execution plan: {plan_name}")
    risks = plan.get("risks")
    if not isinstance(risks, list) or not risks or any(str(risk) not in RISK_CLASSES for risk in risks):
        raise ValueError(f"execution plan {plan_name} has invalid risks")
    maximum = plan.get("max_cases_per_module")
    if maximum is not None and (not isinstance(maximum, int) or maximum < 1):
        raise ValueError(f"execution plan {plan_name} max_cases_per_module must be a positive integer")
    return {str(risk) for risk in risks}, plan_name, plan


def confirmation_error(
    risks: set[str],
    plan: dict[str, Any],
    confirm_write: bool,
    confirm_destructive: bool,
    confirm_external: bool,
) -> str | None:
    if "isolated-write" in risks and not confirm_write:
        return "isolated-write execution requires --confirm-write"
    if "destructive" in risks and (not confirm_write or not confirm_destructive):
        return "destructive execution requires --confirm-write and --confirm-destructive"
    if "external-side-effect" in risks and not confirm_external:
        return "external-side-effect execution requires --confirm-external"
    if plan.get("require_confirm") is True:
        if (risks & {"isolated-write", "destructive"}) and not confirm_write:
            return "this plan requires --confirm-write"
        if "destructive" in risks and not confirm_destructive:
            return "this plan requires --confirm-destructive"
        if "external-side-effect" in risks and not confirm_external:
            return "this plan requires --confirm-external"
    return None


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
    parser.add_argument("--module", help="module id, display name, OpenAPI Tag, or directory")
    parser.add_argument("--risk", action="append", choices=sorted(RISK_CLASSES), help="risk class; defaults to read-only")
    parser.add_argument("--plan", help="plan name from qa/execution/plans.yaml")
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
        risks, plan_name, plan = execution_scope(
            qa_root / "execution" / "plans.yaml", args.plan, args.risk
        )
        confirmation = confirmation_error(
            risks,
            plan,
            args.confirm_write,
            args.confirm_destructive,
            args.confirm_external,
        )
        if confirmation:
            raise ValueError(confirmation)
        selected_directory = module_directory(contracts_root, args.module) if args.module else None
    except (OSError, ValueError, SystemExit) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    route = representative_route(contracts_root, selected_directory)
    if route is None:
        scope = args.module or "all modules"
        print(f"ERROR: no read-only representative route is available for {scope}", file=sys.stderr)
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
                risks,
                plan_name,
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
        bruno_command = ["run"]
        if selected_directory:
            bruno_command.append(selected_directory)
        bruno_command.extend([
            "--env-file", str(runtime_env_path),
            "--env-var", f"{RUNTIME_CONFIG_ENV}={runtime_payload(config, environment)}",
            "--tags", f"plan-{plan_name}" if plan_name else ",".join(sorted(risks)),
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
                risks,
                plan_name,
                evidence_path,
                preflight_path,
            ),
            coverage_path,
        )
        if coverage_report:
            scope = f"module {args.module}" if args.module else "all modules"
            print(
                f"scope={scope} environment={config['active_environment']} status={coverage_report.get('status')} "
                f"risks={','.join(sorted(risks))} executed={coverage_report.get('executed_cases', 0)} "
                f"passed={coverage_report.get('passed_cases', 0)}"
            )
        if bruno_result.returncode:
            print(
                f"ERROR: Bruno exited with code {bruno_result.returncode}; "
                "console output was suppressed because it may contain credentials",
                file=sys.stderr,
            )
        if bruno_result.returncode or coverage_code or not coverage_report or coverage_report.get("completion_ok") is not True:
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
