#!/usr/bin/env python3
"""Internal cross-platform Bruno execution pipeline used by run.bat and run.sh."""

from __future__ import annotations

import argparse
import copy
import json
import math
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
from qa_paths import (
    BRUNO,
    CONSTRAINTS,
    CONTRACTS,
    EXECUTION,
    GLOBAL_EVIDENCE,
    GLOBAL_RESULTS,
    LOGS,
    MODULE_EVIDENCE,
    MODULE_RESULTS,
    migrate_legacy_layout,
)
from qa_constraints import (
    check_module_lock,
    database_steps,
    needs_manual_confirmation,
    validate_stage,
    validate_worker_snapshot,
    worker_snapshot_path,
)
from command_execution import command_argv
from materialize_missing_bru import request_url


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
    root = qa_root / LOGS / "modules" / safe_scope if module else qa_root / LOGS
    return root / f"{timestamp}-run-bruno-{safe_scope}.log"


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


def scope_cases(contracts_root: Path, module: str | None) -> list[dict[str, Any]]:
    modules_root = contracts_root / "modules"
    directories = [modules_root / module] if module else sorted(
        path for path in modules_root.iterdir() if path.is_dir()
    )
    cases: list[dict[str, Any]] = []
    for directory in directories:
        cases_path = directory / "cases.yaml"
        endpoints_path = directory / "endpoints.yaml"
        endpoint_document = load_data(endpoints_path) if endpoints_path.is_file() else {}
        module_id = str(endpoint_document.get("module", directory.name)) if isinstance(endpoint_document, dict) else directory.name
        endpoints = {
            str(item.get("id")): item for item in first_list(endpoint_document, "endpoints")
        }
        if cases_path.is_file():
            for raw in first_list(load_data(cases_path), "cases"):
                case = dict(raw)
                case["_module"] = module_id
                case["_module_directory"] = directory.name
                case["_endpoint"] = endpoints.get(str(case.get("endpoint_id")), {})
                cases.append(case)
    return cases


def requires_developer_sandbox(cases: list[dict[str, Any]]) -> bool:
    return any(database_steps(case) for case in cases)


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


FAILURE_CATEGORY_LABELS = {
    "generation_failure": "生成失败",
    "insufficient_data": "数据不足",
    "environment_unavailable": "环境不可用",
    "endpoint_unreachable": "接口不可达",
    "request_failure": "请求失败",
    "assertion_failure": "响应断言失败",
    "insufficient_source_evidence": "源码证据不足",
    "manual_confirmation": "需要人工确认",
}


def _request_summary(case: dict[str, Any]) -> dict[str, Any]:
    endpoint = case.get("_endpoint", {}) if isinstance(case.get("_endpoint"), dict) else {}
    request = case.get("request", {}) if isinstance(case.get("request"), dict) else {}
    body = request.get("body")
    return {
        "method": str(endpoint.get("method", "")),
        "path": str(request.get("path") or endpoint.get("path", "")),
        "query_fields": sorted(request.get("query", {})) if isinstance(request.get("query"), dict) else [],
        "body_fields": sorted(body) if isinstance(body, dict) else [],
        "body_type": request.get("body_type") or request.get("content_type"),
    }


def result_report(
    cases: list[dict[str, Any]],
    evidence: dict[str, Any] | None,
    scope: str,
    default_category: str = "request_failure",
    default_reason: str = "用例未执行",
) -> dict[str, Any]:
    executed = set(evidence.get("executed", [])) if evidence else set()
    passed = set(evidence.get("passed", [])) if evidence else set()
    observations = evidence.get("cases", {}) if evidence and isinstance(evidence.get("cases"), dict) else {}
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    not_executed: list[dict[str, Any]] = []
    manual: list[dict[str, Any]] = []
    for case in cases:
        case_id = str(case.get("id", "<unknown>"))
        endpoint = case.get("_endpoint", {}) if isinstance(case.get("_endpoint"), dict) else {}
        observation = observations.get(case_id, {}) if isinstance(observations.get(case_id), dict) else {}
        confirmation = needs_manual_confirmation(case)
        if case_id in passed:
            status, category, reason = "passed", None, None
        elif case_id not in executed:
            status = "not_executed"
            reason = (
                str(case.get("review_reason") or "; ".join(str(value) for value in case.get("review_reasons", {}).values()))
                if confirmation
                else default_reason
            )
            category = (
                "insufficient_source_evidence"
                if confirmation and ("源码" in reason or "source" in reason.lower())
                else "insufficient_data" if confirmation else default_category
            )
        else:
            status = "failed"
            actual = observation.get("actual", {}) if isinstance(observation.get("actual"), dict) else {}
            category = "request_failure" if actual.get("http_status") is None else "assertion_failure"
            reason = str(observation.get("failure_reason") or "请求或断言未通过")
        row = {
            "module": str(case.get("_module", "")),
            "case_id": case_id,
            "interface": f"{str(endpoint.get('method', '')).upper()} {endpoint.get('path', '')}".strip(),
            "request_summary": _request_summary(case),
            "expected": {
                **(case.get("expected", {}) if isinstance(case.get("expected"), dict) else {}),
                "assertions": case.get("assertions", []),
            },
            "actual": observation.get("actual"),
            "status": status,
            "failure_category": category,
            "failure_categories": ([category, "manual_confirmation"] if confirmation and category else [category] if category else []),
            "failure_category_label": FAILURE_CATEGORY_LABELS.get(str(category)) if category else None,
            "failure_reason": reason,
            "needs_manual_confirmation": confirmation,
        }
        rows.append(row)
        if status == "failed":
            failures.append(row)
        elif status == "not_executed":
            not_executed.append(row)
        if confirmation:
            manual.append(row)
    modules: list[dict[str, Any]] = []
    for module_id in sorted({str(row["module"]) for row in rows}):
        scoped = [row for row in rows if row["module"] == module_id]
        modules.append({
            "module": module_id,
            "status": "passed" if scoped and all(
                row["status"] == "passed" and not row["needs_manual_confirmation"] for row in scoped
            ) else "attention_required",
            "total": len(scoped),
            "executed": sum(row["status"] != "not_executed" for row in scoped),
            "passed": sum(row["status"] == "passed" for row in scoped),
            "failed": sum(row["status"] == "failed" for row in scoped),
            "not_executed": sum(row["status"] == "not_executed" for row in scoped),
        })
    return {
        "version": 1,
        "generated_at": datetime.now().astimezone().isoformat(),
        "scope": scope,
        "summary": {
            "total": len(rows),
            "executed": len(executed & {str(case.get('id')) for case in cases}),
            "passed": len(passed & {str(case.get('id')) for case in cases}),
            "failed": sum(row["status"] == "failed" for row in rows),
            "not_executed": sum(row["status"] == "not_executed" for row in rows),
        },
        "modules": modules,
        "failures": failures,
        "not_executed": not_executed,
        "manual_confirmation": manual,
        "cases": rows,
    }


def _report_from_rows(rows: list[dict[str, Any]], scope: str) -> dict[str, Any]:
    modules: list[dict[str, Any]] = []
    for module_id in sorted({str(row.get("module", "")) for row in rows}):
        scoped = [row for row in rows if str(row.get("module", "")) == module_id]
        modules.append({
            "module": module_id,
            "status": "passed" if scoped and all(
                row.get("status") == "passed" and row.get("needs_manual_confirmation") is not True
                for row in scoped
            ) else "attention_required",
            "total": len(scoped),
            "executed": sum(row.get("status") != "not_executed" for row in scoped),
            "passed": sum(row.get("status") == "passed" for row in scoped),
            "failed": sum(row.get("status") == "failed" for row in scoped),
            "not_executed": sum(row.get("status") == "not_executed" for row in scoped),
        })
    return {
        "version": 1,
        "generated_at": datetime.now().astimezone().isoformat(),
        "scope": scope,
        "summary": {
            "total": len(rows),
            "executed": sum(row.get("status") != "not_executed" for row in rows),
            "passed": sum(row.get("status") == "passed" for row in rows),
            "failed": sum(row.get("status") == "failed" for row in rows),
            "not_executed": sum(row.get("status") == "not_executed" for row in rows),
        },
        "modules": modules,
        "failures": [row for row in rows if row.get("status") == "failed"],
        "not_executed": [row for row in rows if row.get("status") == "not_executed"],
        "manual_confirmation": [row for row in rows if row.get("needs_manual_confirmation") is True],
        "cases": rows,
    }


def aggregate_module_results(qa_root: Path) -> tuple[Path, Path, dict[str, Any]]:
    """Merge the latest independent module reports and evidence into one global result."""

    qa_root = qa_root.resolve()
    migrate_legacy_layout(qa_root)
    contracts_root = qa_root / CONTRACTS
    cases = scope_cases(contracts_root, None)
    by_module: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        by_module.setdefault(str(case.get("_module", "")), []).append(case)
    rows: list[dict[str, Any]] = []
    reconciliation_errors: list[str] = []
    source_reports: list[str] = []
    merged_evidence: dict[str, Any] = {
        "version": 1,
        "executed": [],
        "passed": [],
        "cases": {},
        "module_evidence": [],
    }
    for module_id, module_cases in sorted(by_module.items()):
        result_root = qa_root / MODULE_RESULTS / module_id
        candidates = sorted(result_root.glob("*-result.json")) if result_root.is_dir() else []
        latest = candidates[-1] if candidates else None
        expected_ids = {str(case.get("id")) for case in module_cases}
        if latest is None:
            reconciliation_errors.append(f"module {module_id} has no result report")
            rows.extend(result_report(
                module_cases, None, f"module:{module_id}", "insufficient_data", "模块尚未执行",
            )["cases"])
            continue
        document = load_data(latest)
        source_reports.append(latest.relative_to(qa_root).as_posix())
        module_rows = {
            str(row.get("case_id")): row
            for row in document.get("cases", [])
            if isinstance(row, dict) and row.get("case_id")
        } if isinstance(document, dict) else {}
        if set(module_rows) != expected_ids:
            missing = sorted(expected_ids - set(module_rows))
            stale = sorted(set(module_rows) - expected_ids)
            if missing:
                reconciliation_errors.append(f"module {module_id} report is missing cases: {', '.join(missing)}")
            if stale:
                reconciliation_errors.append(f"module {module_id} report contains stale cases: {', '.join(stale)}")
        fallback = {
            str(row["case_id"]): row
            for row in result_report(
                module_cases, None, f"module:{module_id}", "insufficient_data", "模块报告缺少当前用例",
            )["cases"]
        }
        rows.extend(copy.deepcopy(module_rows.get(case_id, fallback[case_id])) for case_id in sorted(expected_ids))
        evidence_reference = document.get("execution_evidence") if isinstance(document, dict) else None
        evidence_path = qa_root / str(evidence_reference) if evidence_reference else None
        if evidence_path and evidence_path.is_file():
            evidence = load_data(evidence_path)
            merged_evidence["executed"] = list(dict.fromkeys([
                *merged_evidence["executed"], *evidence.get("executed", []),
            ]))
            merged_evidence["passed"] = list(dict.fromkeys([
                *merged_evidence["passed"], *evidence.get("passed", []),
            ]))
            if isinstance(evidence.get("cases"), dict):
                overlap = set(merged_evidence["cases"]) & set(evidence["cases"])
                if overlap:
                    reconciliation_errors.append(
                        "module evidence repeats case ids: " + ", ".join(sorted(overlap))
                    )
                merged_evidence["cases"].update(copy.deepcopy(evidence["cases"]))
            merged_evidence["module_evidence"].append(evidence_path.relative_to(qa_root).as_posix())
        elif any(row.get("status") != "not_executed" for row in module_rows.values()):
            reconciliation_errors.append(f"module {module_id} report has no readable execution evidence")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    evidence_root = qa_root / GLOBAL_EVIDENCE
    result_root = qa_root / GLOBAL_RESULTS
    evidence_root.mkdir(parents=True, exist_ok=True)
    result_root.mkdir(parents=True, exist_ok=True)
    evidence_path = evidence_root / f"{timestamp}-evidence.json"
    evidence_path.write_text(json.dumps(merged_evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_observed_rules(
        qa_root,
        cases,
        merged_evidence,
        evidence_path.relative_to(qa_root).as_posix(),
        qa_root / CONSTRAINTS / "observed-rules.yaml",
    )
    report = _report_from_rows(rows, "aggregated-modules")
    report["execution_evidence"] = evidence_path.relative_to(qa_root).as_posix()
    report["source_module_reports"] = source_reports
    constraint_errors = validate_stage(qa_root, "post-execution")
    all_errors = list(dict.fromkeys([*reconciliation_errors, *constraint_errors]))
    if all_errors:
        report["reconciliation_errors"] = all_errors
        report["status"] = "failed"
    else:
        required_incomplete = any(
            row.get("status") != "passed" or row.get("needs_manual_confirmation") is True
            for row in rows
        )
        report["status"] = "failed" if required_incomplete else "verified"
    report_path = result_root / f"{timestamp}-result.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report_path, evidence_path, report


def _response_shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _response_shape(child) for key, child in value.items()}
    if isinstance(value, list):
        return {"type": "array", "items": _response_shape(value[0]) if value else None}
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"


def _reusable_values(value: Any, path: str = "$") -> dict[str, Any]:
    result: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            if re.search(r"password|secret|token|authorization|cookie|key", str(key), re.IGNORECASE):
                continue
            result.update(_reusable_values(child, f"{path}.{key}"))
    elif isinstance(value, list):
        if value:
            result.update(_reusable_values(value[0], f"{path}[0]"))
    elif value != "<redacted>" and value is not None:
        result[path] = value
    return result


def write_observed_rules(
    qa_root: Path,
    cases: list[dict[str, Any]],
    evidence: dict[str, Any],
    evidence_reference: str,
    observed_path: Path,
) -> None:
    case_map = {str(case.get("id")): case for case in cases}
    observations = []
    evidence_cases = evidence.get("cases", {}) if isinstance(evidence.get("cases"), dict) else {}
    for case_id in evidence.get("passed", []):
        value = evidence_cases.get(case_id, {}) if isinstance(evidence_cases.get(case_id), dict) else {}
        actual = value.get("actual") if isinstance(value.get("actual"), dict) else {}
        case = case_map.get(str(case_id), {})
        endpoint_id = str(case.get("endpoint_id", ""))
        body = actual.get("body") if isinstance(actual, dict) else None
        observations.append({
            "case_id": str(case_id),
            "endpoint_id": endpoint_id,
            "response": actual,
            "response_shape": _response_shape(body),
            "reusable_values": _reusable_values(body),
            "evidence_file": evidence_reference,
            "evidence": {
                "source_kind": "runtime",
                "file": evidence_reference,
                "symbol": str(case_id),
                "line": 1,
                "endpoint_scope": [endpoint_id],
                "confidence": "high",
            },
        })
    try:
        import yaml  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise ValueError("observed evidence requires PyYAML") from exc
    observed_path.parent.mkdir(parents=True, exist_ok=True)
    previous = load_data(observed_path) if observed_path.is_file() else {}
    previous_items = previous.get("observations", []) if isinstance(previous, dict) else []
    merged = {
        (str(item.get("endpoint_id")), str(item.get("case_id"))): item
        for item in previous_items
        if isinstance(item, dict)
    }
    merged.update({
        (str(item.get("endpoint_id")), str(item.get("case_id"))): item
        for item in observations
    })
    observed_path.write_text(
        yaml.safe_dump({"version": 1, "observations": list(merged.values())}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def persist_execution_artifacts(
    qa_root: Path,
    cases: list[dict[str, Any]],
    evidence: dict[str, Any] | None,
    module: str | None,
    default_category: str = "request_failure",
    default_reason: str = "用例未执行",
) -> tuple[Path, Path | None, dict[str, Any]]:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    module_id = str(cases[0].get("_module")) if module and cases else (module or "unknown")
    result_root = qa_root / MODULE_RESULTS / module_id if module else qa_root / GLOBAL_RESULTS
    evidence_root = qa_root / MODULE_EVIDENCE / module_id if module else qa_root / GLOBAL_EVIDENCE
    result_root.mkdir(parents=True, exist_ok=True)
    report = result_report(
        cases,
        evidence,
        f"module:{module_id}" if module else "all",
        default_category,
        default_reason,
    )
    report_path = result_root / f"{timestamp}-result.json"
    evidence_output: Path | None = None
    if evidence is not None:
        evidence_root.mkdir(parents=True, exist_ok=True)
        evidence_output = evidence_root / f"{timestamp}-evidence.json"
        evidence_output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        evidence_reference = evidence_output.relative_to(qa_root).as_posix()
        report["execution_evidence"] = evidence_reference
        observed_path = (
            qa_root / CONTRACTS / "modules" / str(cases[0].get("_module_directory")) / "observed-rules.yaml"
            if module and cases
            else qa_root / CONSTRAINTS / "observed-rules.yaml"
        )
        write_observed_rules(qa_root, cases, evidence, evidence_reference, observed_path)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report_path, evidence_output, report


def persist_post_execution_constraints(
    report_path: Path,
    report: dict[str, Any],
    errors: list[str],
    result_code: int,
) -> int:
    """Attach post-execution reconciliation failures to the immutable run report."""

    if errors:
        report["constraint_errors"] = list(dict.fromkeys(errors))
        report["status"] = "failed"
        result_code = result_code or 1
    else:
        report["status"] = "verified" if result_code == 0 else "failed"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result_code


def render_case_summary(cases: list[dict[str, Any]], evidence: dict[str, Any] | None) -> tuple[int, int, int, list[str], list[str]]:
    executed = set(evidence.get("executed", [])) if evidence else set()
    passed = set(evidence.get("passed", [])) if evidence else set()
    case_ids = {str(case.get("id")) for case in cases}
    failed: list[str] = []
    not_executed: list[str] = []
    for case in cases:
        case_id = str(case.get("id", "<unknown>"))
        title = str(case.get("title") or case_id)
        if case_id in passed:
            print(f"[PASS] {case_id} - {title}")
        else:
            reason = "failed" if case_id in executed else "not executed"
            print(f"[FAIL] {case_id} - {title} ({reason})")
            (failed if case_id in executed else not_executed).append(case_id)
    return len(cases), len(executed & case_ids), len(passed & case_ids), failed, not_executed


def print_final_summary(
    total_cases: int,
    executed_cases: int,
    passed_cases: int,
    failed_cases: list[str],
    not_executed_cases: list[str],
    manual_confirmation_cases: list[str],
    version_warnings: list[str],
    execution_log: Path,
) -> None:
    print("Execution summary:")
    print(
        f"  total={total_cases} executed={executed_cases} success={passed_cases} "
        f"failed={len(failed_cases)} not_executed={len(not_executed_cases)}"
    )
    print(f"  failed_cases={','.join(failed_cases) if failed_cases else 'none'}")
    print(f"  not_executed_cases={','.join(not_executed_cases) if not_executed_cases else 'none'}")
    print(
        "  manual_confirmation="
        + (",".join(manual_confirmation_cases) if manual_confirmation_cases else "none")
    )
    if version_warnings:
        print(red_warning("Version warnings:"))
        for warning in version_warnings:
            print(red_warning(warning))
    else:
        print("  version_warnings=none")
    print(f"  log={execution_log}")


def finish_failed_attempt(
    qa_root: Path,
    cases: list[dict[str, Any]],
    module: str | None,
    category: str,
    reason: str,
    version_warnings: list[str],
    execution_log: Path,
    return_code: int = 1,
) -> int:
    report_path, _, report = persist_execution_artifacts(
        qa_root, cases, None, module, category, reason,
    )
    summary = report["summary"]
    print_final_summary(
        summary["total"], summary["executed"], summary["passed"], [],
        [str(case.get("id")) for case in cases],
        [str(case.get("id")) for case in cases if needs_manual_confirmation(case)],
        version_warnings, execution_log,
    )
    print(f"  result_report={report_path}")
    return return_code


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
    contracts_root = qa_root / CONTRACTS
    bruno_root = qa_root / BRUNO
    config_path = qa_root / EXECUTION / "config.yaml"
    openapi = next(
        (path for path in (contracts_root / "openapi.json", contracts_root / "openapi.yaml", contracts_root / "openapi.yml") if path.is_file()),
        contracts_root / "openapi.json",
    )
    try:
        selected_directory = module_directory(contracts_root, args.module) if args.module else None
        cases = scope_cases(contracts_root, selected_directory)
    except (OSError, ValueError, SystemExit) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return finish_failed_attempt(
            qa_root, [], args.module, "generation_failure", str(exc), [], execution_log, 2,
        )
    gate_errors = validate_stage(qa_root, "run", module=args.module)
    if gate_errors:
        for error in gate_errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return finish_failed_attempt(
            qa_root, cases, args.module, "generation_failure", "; ".join(gate_errors), [], execution_log, 2,
        )
    if args.module:
        try:
            snapshot = worker_snapshot_path(qa_root, args.module)
        except ValueError:
            snapshot = None
        if snapshot and snapshot.is_file():
            worker_errors = validate_worker_snapshot(qa_root, args.module, "pre-execution")
            if worker_errors:
                return finish_failed_attempt(
                    qa_root, cases, args.module, "generation_failure", "; ".join(worker_errors), [], execution_log, 2,
                )

    try:
        config = load_execution_config(config_path)
        env_path = environment_file(config_path, config)
        environment = load_bruno_environment_document(env_path)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return finish_failed_attempt(
            qa_root, cases, args.module, "environment_unavailable", str(exc), [], execution_log, 2,
        )

    scope_name = f"module {args.module}" if args.module else "all modules"
    print(f"Execution started: scope={scope_name} environment={config['active_environment']}")
    print(f"Log: {execution_log}")

    print("[1/6] Checking collection and QA lock")
    route = representative_route(contracts_root, selected_directory)
    if route is None:
        scope = args.module or "all modules"
        reason = f"no representative route is available for {scope}"
        print(f"ERROR: {reason}", file=sys.stderr)
        return finish_failed_attempt(
            qa_root, cases, args.module, "generation_failure", reason, [], execution_log, 2,
        )

    qa_lock_errors = check_module_lock(qa_root, args.module) if args.module else check_qa_lock(contracts_root)
    if qa_lock_errors:
        for error in qa_lock_errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return finish_failed_attempt(
            qa_root, cases, args.module, "generation_failure", "; ".join(qa_lock_errors), [], execution_log, 2,
        )

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
        reason = detail or f"local business version check exited with {version_check.returncode}"
        print(f"ERROR: {reason}", file=sys.stderr)
        return finish_failed_attempt(
            qa_root, cases, args.module, "generation_failure", reason, [], execution_log, version_check.returncode,
        )
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
            report_path, _, report = persist_execution_artifacts(
                qa_root, cases, None, args.module, "generation_failure", "静态生成或约束校验失败",
            )
            summary = report["summary"]
            print_final_summary(
                summary["total"], summary["executed"], summary["passed"], [],
                [str(case.get("id")) for case in cases],
                [str(case.get("id")) for case in cases if needs_manual_confirmation(case)],
                version_warnings, execution_log,
            )
            print(f"  result_report={report_path}")
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
            "--cli-timeout", str(args.cli_timeout if args.cli_timeout is not None else config["cli_timeout"]),
            "--public-method", route[0],
            "--public-path", route[1],
            "--public-status", "200-403,405-499",
            "--output", str(preflight_path),
            "--qa-root", str(qa_root),
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
            preflight_errors = [str(item) for item in preflight_report.get("errors", [])] if isinstance(preflight_report, dict) else []
            category = "endpoint_unreachable" if any("reach" in item.lower() or "connect" in item.lower() for item in preflight_errors) else "environment_unavailable"
            report_path, _, report = persist_execution_artifacts(
                qa_root, cases, None, args.module, category,
                "; ".join(preflight_errors) or "运行环境预检失败",
            )
            summary = report["summary"]
            print_final_summary(
                summary["total"], summary["executed"], summary["passed"], [],
                [str(case.get("id")) for case in cases],
                [str(case.get("id")) for case in cases if needs_manual_confirmation(case)],
                version_warnings, execution_log,
            )
            print(f"  result_report={report_path}")
            return preflight_result.returncode

        runtime_env_path = temporary / "runtime-environment.bru"
        runtime_env_path.write_text(render_runtime_environment(environment), encoding="utf-8")
        bruno_command = ["run", selected_directory or "."]
        bruno_command.extend([
            "--env-file", str(runtime_env_path),
            "--env-var", f"{RUNTIME_CONFIG_ENV}={runtime_payload(config, environment)}",
            "--reporter-json", str(raw_report),
            "--reporter-skip-all-headers",
            "-r",
        ])
        if requires_developer_sandbox(cases):
            bruno_command.extend(["--sandbox", "developer"])
        try:
            resolved_bruno_command = command_argv(args.bruno_cli, *bruno_command)
        except FileNotFoundError:
            reason = f"Bruno CLI executable was not found: {args.bruno_cli}"
            print(f"ERROR: {reason}", file=sys.stderr)
            return finish_failed_attempt(
                qa_root, cases, args.module, "environment_unavailable", reason,
                version_warnings, execution_log, 2,
            )
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
            total_cases, executed_cases, passed_cases, failed_cases, not_executed = render_case_summary(cases, None)
            report_path, _, _ = persist_execution_artifacts(
                qa_root, cases, None, args.module, "request_failure", "Bruno 未生成 JSON 执行报告",
            )
            print_final_summary(
                total_cases, executed_cases, passed_cases, failed_cases, not_executed,
                [str(case.get("id")) for case in cases if needs_manual_confirmation(case)],
                version_warnings, execution_log,
            )
            print(f"  result_report={report_path}")
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
            total_cases, executed_cases, passed_cases, failed_cases, not_executed = render_case_summary(cases, None)
            report_path, _, _ = persist_execution_artifacts(
                qa_root, cases, None, args.module, "generation_failure", "执行报告归一化失败",
            )
            print_final_summary(
                total_cases, executed_cases, passed_cases, failed_cases, not_executed,
                [str(case.get("id")) for case in cases if needs_manual_confirmation(case)],
                version_warnings, execution_log,
            )
            print(f"  result_report={report_path}")
            return normalize_result.returncode

        evidence = load_data(evidence_path)
        print("Case results:")
        total_cases, executed_cases, passed_cases, failed_cases, not_executed = render_case_summary(cases, evidence)

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
                f"{status_key}={coverage_report.get(status_key)} "
                f"executed={coverage_report.get('executed_cases', 0)} passed={coverage_report.get('passed_cases', 0)}"
            )
        required_case_ids = {
            str(case.get("id")) for case in cases if not needs_manual_confirmation(case)
        }
        required_execution_failed = bool(required_case_ids - set(evidence.get("passed", [])))
        if bruno_result.returncode:
            level = "ERROR" if required_execution_failed else "WARNING"
            print(
                f"{level}: Bruno exited with code {bruno_result.returncode}; "
                "console output was suppressed because it may contain credentials",
                file=sys.stderr,
            )
        completion_key = "module_completion_ok" if args.module else "completion_ok"
        result_code = 0
        if required_execution_failed or coverage_code or not coverage_report or coverage_report.get(completion_key) is not True:
            result_code = coverage_code or bruno_result.returncode or 1

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

        report_path, evidence_output, report = persist_execution_artifacts(
            qa_root, cases, evidence, args.module,
        )
        constraint_errors = validate_stage(
            qa_root,
            "post-execution",
            module=args.module,
        )
        if args.module and snapshot and snapshot.is_file():
            constraint_errors.extend(validate_worker_snapshot(qa_root, args.module, "post-execution"))
            constraint_errors = list(dict.fromkeys(constraint_errors))
        result_code = persist_post_execution_constraints(
            report_path, report, constraint_errors, result_code,
        )
        if constraint_errors:
            for error in constraint_errors:
                print(f"ERROR: {error}", file=sys.stderr)
        print_final_summary(
            total_cases, executed_cases, passed_cases, failed_cases, not_executed,
            [str(item["case_id"]) for item in report["manual_confirmation"]],
            version_warnings, execution_log,
        )
        print(f"  result_report={report_path}")
        if evidence_output:
            print(f"  execution_evidence={evidence_output}")
    return result_code


def positive_cli_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive number of seconds") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise argparse.ArgumentTypeError("must be a positive number of seconds")
    return timeout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run every Bruno case by default, or select one module with --module.",
    )
    parser.add_argument("--qa-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--module", help="module id, display name, OpenAPI Tag, or directory")
    parser.add_argument("--bruno-cli", default="bru", help=argparse.SUPPRESS)
    parser.add_argument(
        "--cli-timeout", type=positive_cli_timeout,
        help="seconds allowed for the Bruno CLI version probe (overrides execution config; default: 60)",
    )
    args = parser.parse_args(argv)
    scripts_root = Path(__file__).resolve().parent
    qa_root = (args.qa_root or scripts_root.parent).resolve()
    migrate_legacy_layout(qa_root)
    log_scope = args.module
    if args.module:
        try:
            directory = module_directory(qa_root / CONTRACTS, args.module)
            document = load_data(qa_root / CONTRACTS / "modules" / directory / "endpoints.yaml")
            if isinstance(document, dict):
                log_scope = str(document.get("module", directory))
        except (OSError, ValueError, TypeError):
            pass
    execution_log = log_path(qa_root, log_scope)
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
