#!/usr/bin/env python3
"""Internal cross-platform Bruno execution pipeline used by run.bat and run.sh."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

from execution_config import (
    RUNTIME_CONFIG_ENV,
    environment_file,
    load_bruno_environment_document,
    load_execution_config,
    render_runtime_environment,
    resolved_environment_headers,
    runtime_payload,
)
from manifest_io import first_list, load_data
from qa_lock import check as check_qa_lock
from command_execution import command_argv
from materialize_missing_bru import request_url


RISK_CLASSES = {"read-only", "isolated-write", "destructive", "external-side-effect"}
ANSI_RED = "\033[31m"
ANSI_RESET = "\033[0m"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class Tee:
    """Mirror console output to a plain-text execution log."""

    def __init__(self, console: Any, log: Any) -> None:
        self.console = console
        self.log = log
        self.encoding = getattr(console, "encoding", "utf-8")

    def write(self, value: str) -> int:
        self.console.write(value)
        self.log.write(ANSI_RE.sub("", value))
        self.log.flush()
        return len(value)

    def flush(self) -> None:
        self.console.flush()
        self.log.flush()

    def isatty(self) -> bool:
        return bool(getattr(self.console, "isatty", lambda: False)())


def log_path(qa_root: Path, module: str | None) -> Path:
    scope = module or "all"
    safe_scope = re.sub(r'[<>:"/\\|?*\s]+', "-", scope).strip("-.") or "all"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return qa_root / "logs" / f"{timestamp}-run-bruno-{safe_scope}.log"


def red_warning(message: str) -> str:
    return f"{ANSI_RED}WARNING: {message}{ANSI_RESET}"


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
        endpoints = first_list(load_data(endpoint_path), "endpoints")
        cases_path = directory / "cases.yaml"
        cases = first_list(load_data(cases_path), "cases") if cases_path.is_file() else []
        cases_by_endpoint: dict[str, list[dict[str, Any]]] = {}
        for case in cases:
            cases_by_endpoint.setdefault(str(case.get("endpoint_id")), []).append(case)
        for endpoint in endpoints:
            method = str(endpoint.get("method", "")).upper()
            path = str(endpoint.get("path", ""))
            if "{" in path:
                for case in cases_by_endpoint.get(str(endpoint.get("id")), []):
                    candidate = request_url(endpoint, case.get("request", {}) if isinstance(case.get("request"), dict) else {})
                    candidate = re.sub(r"^\{\{(?:baseUrl|BASE_URL)\}\}", "", candidate).split("?", 1)[0]
                    if "{{" not in candidate:
                        path = candidate
                        break
            if not path.startswith("/") or "{" in path or "{{" in path:
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
                risk = "read-only" if str(endpoint.get("method", "GET")).upper() in {"GET", "HEAD", "OPTIONS"} else "unconfirmed"
            if risk not in RISK_CLASSES:
                raise ValueError(f"case {case.get('id')} has invalid risk class {risk}")
            risks.add(risk)
    return risks


def scope_cases(contracts_root: Path, module: str | None) -> list[dict[str, Any]]:
    modules_root = contracts_root / "modules"
    directories = [modules_root / module] if module else sorted(
        path for path in modules_root.iterdir() if path.is_dir()
    )
    cases: list[dict[str, Any]] = []
    for directory in directories:
        path = directory / "cases.yaml"
        if path.is_file():
            cases.extend(first_list(load_data(path), "cases"))
    return cases


def is_remote_url(value: str) -> bool:
    hostname = (urllib.parse.urlsplit(value).hostname or "").casefold()
    return hostname not in {"localhost", "127.0.0.1", "::1"}


def nested_value(document: Any, path: str) -> Any:
    value = document
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def versions_match(expected: str, actual: str) -> bool:
    expected_value = expected.strip().casefold().removeprefix("v")
    actual_value = actual.strip().casefold().removeprefix("v")
    if expected_value == actual_value:
        return True
    return min(len(expected_value), len(actual_value)) >= 7 and (
        expected_value.startswith(actual_value) or actual_value.startswith(expected_value)
    )


def locked_business_version(contracts_root: Path) -> str | None:
    path = contracts_root / "version-lock.yaml"
    if not path.is_file():
        return None
    document = load_data(path)
    business = document.get("business", {}) if isinstance(document, dict) else {}
    value = business.get("commit") if isinstance(business, dict) else None
    return str(value) if value else None


def target_version_warnings(
    environment: dict[str, dict[str, str]],
    contracts_root: Path,
    timeout: float = 3.0,
) -> list[str]:
    variables = environment.get("vars", {})
    base_url = str(variables.get("baseUrl") or variables.get("BASE_URL") or "").rstrip("/")
    if not base_url:
        return []
    version_path = str(variables.get("versionPath") or variables.get("versionUrl") or "").strip()
    if not version_path:
        return ([
            "remote target version was not checked; configure versionPath and optionally "
            "expectedVersion/versionJsonPath/versionHeader in the active Bruno environment"
        ] if is_remote_url(base_url) else [])

    version_url = urllib.parse.urljoin(f"{base_url}/", version_path)
    if urllib.parse.urlsplit(version_url)[:2] != urllib.parse.urlsplit(base_url)[:2]:
        return ["versionPath must resolve to the same origin as baseUrl"]
    headers = {"Accept": "application/json", **resolved_environment_headers(environment)}
    request = urllib.request.Request(version_url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            payload = response.read(1024 * 1024)
            response_headers = {name.casefold(): value for name, value in response.headers.items()}
    except urllib.error.HTTPError as exc:
        return [f"target version endpoint {version_url} returned HTTP {exc.code}"]
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        return [f"target version endpoint {version_url} could not be read: {exc}"]
    if not 200 <= status < 300:
        return [f"target version endpoint {version_url} returned HTTP {status}"]

    header_name = str(variables.get("versionHeader") or "").strip()
    json_path = str(variables.get("versionJsonPath") or "").strip()
    actual: Any = response_headers.get(header_name.casefold()) if header_name else None
    document: Any = None
    if actual is None:
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            document = payload.decode("utf-8", errors="replace").strip()
        paths = [json_path] if json_path else [
            "git.commit.id", "git.commit.id.abbrev", "build.version", "version", "commit", "sha",
        ]
        for candidate in paths:
            if candidate:
                actual = nested_value(document, candidate)
                if actual is not None:
                    break
        if actual is None and isinstance(document, str):
            actual = document
    if actual is None or isinstance(actual, (dict, list)):
        selector = "versionHeader" if header_name else "versionJsonPath"
        return [f"target version endpoint did not expose a scalar version; configure {selector}"]

    expected = str(variables.get("expectedVersion") or locked_business_version(contracts_root) or "").strip()
    if not expected:
        return [f"target reported version {actual}, but no expectedVersion or business lock is available"]
    if not versions_match(expected, str(actual)):
        return [f"target version mismatch: expected {expected}, got {actual} from {version_url}"]
    return []


def render_case_summary(cases: list[dict[str, Any]], evidence: dict[str, Any] | None) -> tuple[int, int, list[str]]:
    executed = set(evidence.get("executed", [])) if evidence else set()
    passed = set(evidence.get("passed", [])) if evidence else set()
    case_ids = {str(case.get("id")) for case in cases}
    failed: list[str] = []
    for case in cases:
        case_id = str(case.get("id", "<unknown>"))
        title = str(case.get("title") or case_id)
        if case_id in passed:
            print(f"[PASS] {case_id} - {title}")
        else:
            reason = "failed" if case_id in executed else "not executed"
            print(f"[FAIL] {case_id} - {title} ({reason})")
            failed.append(case_id)
    return len(cases), len(passed & case_ids), failed


def print_final_summary(
    total_cases: int,
    passed_cases: int,
    failed_cases: list[str],
    version_warnings: list[str],
    execution_log: Path,
) -> None:
    print("Execution summary:")
    print(f"  total={total_cases} success={passed_cases} failed={len(failed_cases)}")
    print(f"  failed_cases={','.join(failed_cases) if failed_cases else 'none'}")
    if version_warnings:
        print(red_warning("Version warnings:"))
        for warning in version_warnings:
            print(red_warning(warning))
    else:
        print("  version_warnings=none")
    print(f"  log={execution_log}")


def run_json(command: list[str], output: Path, cwd: Path | None = None) -> tuple[int, dict[str, Any] | None]:
    completed = subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True, encoding="utf-8")
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError:
        document = None
    if document is not None:
        output.write_text(json.dumps(document, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    return completed.returncode, document


def execute(args: argparse.Namespace, qa_root: Path, execution_log: Path) -> int:
    scripts_root = Path(__file__).resolve().parent
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
        cases = scope_cases(contracts_root, selected_directory)
    except (OSError, ValueError, SystemExit) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    scope_name = f"module {args.module}" if args.module else "all modules"
    print(f"Execution started: scope={scope_name} environment={config['active_environment']}")
    print(f"Log: {execution_log}")
    print(f"Risks included: {','.join(sorted(risks)) or 'none'}")

    print("[1/6] Checking collection and QA lock")
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

    print("[2/6] Checking local and target versions")
    version_warnings: list[str] = []
    version_check = subprocess.run(
        [
            sys.executable,
            str(scripts_root / "check_version_compatibility.py"),
            str(app_root),
            str(contracts_root),
            "--phase", "before-execute",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if version_check.returncode:
        detail = (version_check.stdout or version_check.stderr).strip()
        version_warnings.append(detail or f"local business version check exited with {version_check.returncode}")
    version_warnings.extend(target_version_warnings(environment, contracts_root))
    for warning in version_warnings:
        print(red_warning(warning))

    with tempfile.TemporaryDirectory(prefix="qa-bruno-") as directory:
        temporary = Path(directory)
        static_path = temporary / "static-coverage.json"
        preflight_path = temporary / "preflight.json"
        raw_report = temporary / "bruno-report.json"
        evidence_path = temporary / "execution-evidence.json"
        coverage_path = temporary / "execution-evidence.coverage.json"

        print("[3/6] Validating static coverage")
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
        for warning in static_report.get("warnings", []):
            text = str(warning)
            if text and text not in version_warnings:
                version_warnings.append(text)
                print(red_warning(text))

        print("[4/6] Running runtime preflight")
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
        preflight_result = subprocess.run(
            preflight_command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        preflight_report = load_data(preflight_path) if preflight_path.is_file() else {}
        for warning in preflight_report.get("warnings", []) if isinstance(preflight_report, dict) else []:
            text = str(warning)
            if text and text not in version_warnings:
                version_warnings.append(text)
                print(red_warning(text))
        if preflight_result.returncode:
            for error in preflight_report.get("errors", []) if isinstance(preflight_report, dict) else []:
                print(f"ERROR: {error}", file=sys.stderr)
            print("ERROR: runtime preflight failed", file=sys.stderr)
            return preflight_result.returncode

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
        try:
            resolved_bruno_command = command_argv(args.bruno_cli, *bruno_command)
        except FileNotFoundError:
            print(f"ERROR: Bruno CLI executable was not found: {args.bruno_cli}", file=sys.stderr)
            return 2
        print(f"[5/6] Running {len(cases)} Bruno case(s)")
        bruno_result = subprocess.run(
            resolved_bruno_command, cwd=bruno_root, check=False, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
        if not raw_report.is_file():
            print(
                f"ERROR: Bruno exited with code {bruno_result.returncode} without a JSON report; "
                "console output was suppressed because it may contain credentials",
                file=sys.stderr,
            )
            print("Case results:")
            total_cases, passed_cases, failed_cases = render_case_summary(cases, None)
            print_final_summary(total_cases, passed_cases, failed_cases, version_warnings, execution_log)
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
            print("Case results:")
            total_cases, passed_cases, failed_cases = render_case_summary(cases, None)
            print_final_summary(total_cases, passed_cases, failed_cases, version_warnings, execution_log)
            return normalize_result.returncode

        evidence = load_data(evidence_path)
        print("Case results:")
        total_cases, passed_cases, failed_cases = render_case_summary(cases, evidence)

        print("[6/6] Reconciling execution evidence")
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
            status_key = "module_status" if args.module else "status"
            print(
                f"scope={scope_name} environment={config['active_environment']} "
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
        result_code = 0
        if bruno_result.returncode or coverage_code or not coverage_report or coverage_report.get(completion_key) is not True:
            result_code = bruno_result.returncode or coverage_code or 1

        if not args.module and result_code == 0:
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
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if lock_result.returncode:
                detail = (lock_result.stdout or lock_result.stderr).strip()
                warning = detail or f"version lock update exited with {lock_result.returncode}"
                if warning not in version_warnings:
                    version_warnings.append(warning)

        print_final_summary(total_cases, passed_cases, failed_cases, version_warnings, execution_log)
    return result_code


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run every Bruno case by default, or select one module with --module.",
    )
    parser.add_argument("--qa-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--module", help="module id, display name, OpenAPI Tag, or directory")
    parser.add_argument("--bruno-cli", default="bru", help=argparse.SUPPRESS)
    args = parser.parse_args()
    scripts_root = Path(__file__).resolve().parent
    qa_root = (args.qa_root or scripts_root.parent).resolve()
    execution_log = log_path(qa_root, args.module)
    execution_log.parent.mkdir(parents=True, exist_ok=True)
    original_stdout, original_stderr = sys.stdout, sys.stderr
    with execution_log.open("x", encoding="utf-8") as log:
        sys.stdout = Tee(original_stdout, log)
        sys.stderr = Tee(original_stderr, log)
        try:
            return execute(args, qa_root, execution_log)
        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr


if __name__ == "__main__":
    raise SystemExit(main())
