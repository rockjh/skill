#!/usr/bin/env python3
"""Collect reviewable HTTP logic candidates across common source languages.

This is deliberately a candidate scanner, not a branch-coverage engine. It
lets the contract workflow continue for non-Java repositories and for projects
that have no source access; adapters can add deeper framework-specific logic.
"""

from __future__ import annotations

import argparse
import copy
import fnmatch
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

from manifest_io import first_list, load_data
from parse_openapi import business_case_title, response_business_code_path
from analyze_java_logic import scan as scan_java_logic


LANGUAGE_BY_SUFFIX = {
    ".java": "java",
    ".go": "go",
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".cs": "csharp",
    ".fs": "fsharp",
    ".php": "php",
    ".rs": "rust",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".rb": "ruby",
    ".scala": "scala",
    ".swift": "swift",
    ".ex": "elixir",
    ".exs": "elixir",
    ".dart": "dart",
}

ENTRYPOINT_RE = re.compile(
    r"@(Get|Post|Put|Patch|Delete|Request)Mapping\b|"
    r"@(?:app|router)\.(get|post|put|patch|delete)\b|"
    r"\.(?:get|post|put|patch|delete)\s*\(|"
    r"\b(?:HandleFunc|HttpGet|HttpPost|HttpPut|HttpPatch|HttpDelete)\b",
    re.IGNORECASE,
)
BRANCH_RE = re.compile(
    r"\b(?:if|else\s+if|switch|case|catch|throw|return|raise|abort|forbidden|unauthor|duplicate|exists|not\s*found|error|fail|status|code)\b",
    re.IGNORECASE,
)
BUSINESS_EXCEPTION_RE = re.compile(
    r"\b(?:throw\s+new|raise\s+|BusinessException|BizException|DomainException|business[_ ]?error)\b",
    re.IGNORECASE,
)
ERROR_CODE_RE = re.compile(r"(?<!\d)([1-9]\d{3,8})(?!\d)")
SYMBOLIC_ERROR_CODE_RE = re.compile(r"[\"']([A-Z][A-Z0-9_.-]{2,})[\"']")
REQUIRED_HEADER_RE = re.compile(
    r"(?:RequestHeader|getHeader|get_header|headers?\.get|requireHeader|require_header)\s*\(\s*"
    r"(?:(?:value|name)\s*=\s*)?[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)
AUTHORIZATION_RE = re.compile(r"(?:PreAuthorize|RequiresPermissions|Secured|permission|hasRole|hasAuthority)", re.IGNORECASE)


def candidate_id(kind: str, path: Path, line_no: int, evidence: str) -> str:
    key = f"{kind}|{path}|{line_no}|{evidence}"
    return f"{kind.upper()}_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]}"


def scan(
    roots: list[Path],
    include_patterns: list[str] | None = None,
    exception_type: str | None = None,
    error_code_type: str | None = None,
) -> dict[str, Any]:
    java_result = scan_java_logic(roots, exception_type, error_code_type)
    candidates: list[dict[str, Any]] = [
        item for item in java_result.get("candidates", [])
        if not include_patterns
        or any(
            fnmatch.fnmatchcase(Path(str(item.get("file", ""))).as_posix(), pattern)
            or fnmatch.fnmatchcase(Path(str(item.get("file", ""))).name, pattern)
            for pattern in include_patterns
        )
    ]
    seen: set[tuple[str, str, str]] = set()
    languages: set[str] = {"java"} if candidates else set()
    for root in roots:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in LANGUAGE_BY_SUFFIX or path.suffix.lower() == ".java":
                continue
            if include_patterns and not any(fnmatch.fnmatchcase(path.as_posix(), pattern) or fnmatch.fnmatchcase(path.name, pattern) for pattern in include_patterns):
                continue
            if any(part.lower() in {".git", "qa", "target", "build", "node_modules", "vendor", ".venv", "venv", "test", "tests"} for part in path.parts):
                continue
            language = LANGUAGE_BY_SUFFIX[path.suffix.lower()]
            languages.add(language)
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for line_no, line in enumerate(lines, start=1):
                excerpt = " ".join(line.strip().split())[:300]
                if REQUIRED_HEADER_RE.search(line):
                    kind = "required_header"
                elif ENTRYPOINT_RE.search(line):
                    kind = "normal_entrypoint"
                elif AUTHORIZATION_RE.search(line):
                    kind = "authorization"
                elif BRANCH_RE.search(line):
                    kind = "observable_branch"
                elif BUSINESS_EXCEPTION_RE.search(line):
                    kind = "business_exception"
                else:
                    continue
                key = (kind, path.resolve().as_posix(), excerpt)
                if key in seen:
                    continue
                seen.add(key)
                candidate: dict[str, Any] = {
                        "id": candidate_id(kind, path, line_no, excerpt),
                        "kind": kind,
                        "file": str(path),
                        "line": line_no,
                        "language": language,
                        "source_excerpt": excerpt,
                        "needs_case": True,
                        "source_evidence": {
                            "source_kind": kind,
                            "file": str(path),
                            "symbol": path.stem,
                            "line": line_no,
                            "endpoint_scope": [],
                            "confidence": "low",
                        },
                    }
                codes = [*ERROR_CODE_RE.findall(line), *SYMBOLIC_ERROR_CODE_RE.findall(line)]
                if codes:
                    candidate["expected_business_codes"] = list(dict.fromkeys(codes))
                header = REQUIRED_HEADER_RE.search(line)
                if header:
                    candidate["required_header"] = header.group(1)
                if BUSINESS_EXCEPTION_RE.search(line):
                    candidate["source_kind"] = "business_exception"
                candidate["coverage_required"] = (
                    kind in {"required_header", "authorization", "business_exception"}
                    or BUSINESS_EXCEPTION_RE.search(line) is not None
                    or bool(codes)
                )
                candidates.append(candidate)
    return {
        "version": 2,
        "source": {"roots": [str(root) for root in roots], "languages": sorted(languages)},
        "java": {
            "exception_family": java_result.get("exception_family"),
            "mapping_annotation_count": java_result.get("mapping_annotation_count", 0),
            "entrypoint_count": java_result.get("entrypoint_count", 0),
            "exception_profile": java_result.get("exception_profile", {"version": 1, "handlers": []}),
        },
        "errors": list(java_result.get("errors", [])),
        "candidates": candidates,
    }


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def apply_candidates(
    result: dict[str, Any],
    contracts_root: Path,
    module_filter: str | None = None,
) -> list[str]:
    """Add source-backed draft logic to the best matching module without inventing cases."""

    try:
        import yaml  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise ValueError("source enhancement requires PyYAML") from exc
    modules_root = contracts_root / "modules"
    modules: list[tuple[Path, dict[str, Any], list[dict[str, Any]]]] = []
    for module_dir in sorted(path for path in modules_root.iterdir() if path.is_dir()):
        endpoints_path = module_dir / "endpoints.yaml"
        if endpoints_path.is_file():
            document = load_data(endpoints_path)
            module_id = str(document.get("module", module_dir.name)) if isinstance(document, dict) else module_dir.name
            if module_filter and module_filter not in {module_id, module_dir.name}:
                continue
            modules.append((module_dir, document, first_list(document, "endpoints")))
    endpoint_records = [
        (module_dir, document, endpoint)
        for module_dir, document, endpoints in modules
        for endpoint in endpoints
    ]
    exception_profile = copy.deepcopy(
        result.get("java", {}).get("exception_profile", {"version": 1, "handlers": []})
        if isinstance(result.get("java"), dict) else {"version": 1, "handlers": []}
    )
    endpoint_by_key = {
        f"{str(endpoint.get('method', '')).upper()} {endpoint.get('path')}": str(endpoint.get("id"))
        for _, _, endpoint in endpoint_records
    }
    for handler in exception_profile.get("handlers", []) if isinstance(exception_profile, dict) else []:
        if not isinstance(handler, dict):
            continue
        evidence = handler.get("evidence") if isinstance(handler.get("evidence"), dict) else {}
        evidence["endpoint_scope"] = [
            endpoint_by_key.get(str(value), str(value))
            for value in evidence.get("endpoint_scope", [])
            if endpoint_by_key.get(str(value), str(value))
        ]
        handler["evidence"] = evidence
    (contracts_root / "exception-profile.yaml").write_text(
        yaml.safe_dump(exception_profile, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    security_path = contracts_root / "security-profile.yaml"
    security_profile = load_data(security_path) if security_path.is_file() else {}
    unresolved: list[str] = []
    for candidate in result.get("candidates", []):
        if not isinstance(candidate, dict) or candidate.get("coverage_required") is not True:
            continue
        haystack = _normalized(
            f"{candidate.get('file', '')} {candidate.get('source_excerpt', candidate.get('evidence', ''))}"
        )
        endpoint_keys = {
            str(value).strip()
            for value in candidate.get("endpoint_keys", [])
            if str(value).strip()
        }
        operation_hints = {
            _normalized(str(value))
            for value in candidate.get("endpoint_operation_ids", [])
            if str(value).strip()
        }
        endpoint_matches = []
        if endpoint_keys:
            endpoint_matches = [
                (module_dir, document, endpoint)
                for module_dir, document, endpoint in endpoint_records
                if (
                    f"{str(endpoint.get('method', '')).upper()} {endpoint.get('path')}" in endpoint_keys
                    or f"* {endpoint.get('path')}" in endpoint_keys
                )
            ]
        elif operation_hints:
            endpoint_matches = [
                (module_dir, document, endpoint)
                for module_dir, document, endpoint in endpoint_records
                if _normalized(str(endpoint.get("operation_id", ""))) in operation_hints
            ]
        else:
            endpoint_matches = [
                (module_dir, document, endpoint)
                for module_dir, document, endpoint in endpoint_records
                if any(
                    token and token in haystack
                    for token in (
                        _normalized(str(endpoint.get("operation_id", ""))),
                        _normalized(str(endpoint.get("path", ""))),
                    )
                )
            ]
        if not endpoint_matches and len(endpoint_records) == 1:
            endpoint_matches = endpoint_records
        if len(endpoint_matches) != 1:
            unresolved.append(str(candidate.get("id")))
            continue
        module_dir, document, endpoint = endpoint_matches[0]
        candidate_evidence = candidate.get("source_evidence")
        if isinstance(candidate_evidence, dict):
            candidate_evidence["endpoint_scope"] = [str(endpoint.get("id"))]
        source_scenario = {
            "validation": "validation",
            "authorization": "authorization",
            "business_exception": "business_error",
            "observable_branch": "business_error",
            "file_exception": "file",
        }.get(str(candidate.get("kind")))
        if source_scenario:
            matrix = endpoint.get("scenario_matrix") if isinstance(endpoint.get("scenario_matrix"), dict) else {}
            matrix[source_scenario] = {
                "applicable": True,
                "status": "inferred",
                "reason": f"API 可达源码存在 {candidate.get('kind')} 证据",
            }
            endpoint["scenario_matrix"] = matrix
            Path(module_dir / "endpoints.yaml").write_text(
                yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
        logic_path = module_dir / "logic.yaml"
        logic_document = load_data(logic_path) if logic_path.is_file() else {}
        items = first_list(logic_document, "logic")
        if any(item.get("source_candidate_id") == candidate.get("id") for item in items):
            continue
        codes = candidate.get("expected_business_codes", [])
        logic_id = f"SOURCE_{candidate['id']}"
        linked_case_ids: list[str] = []
        required_header = candidate.get("required_header")
        if endpoint is not None and isinstance(required_header, str):
            responses = endpoint.get("responses", {}) if isinstance(endpoint.get("responses"), dict) else {}
            header_profile = next(
                (
                    value for value in security_profile.values()
                    if isinstance(value, dict)
                    and str(value.get("header", "")).casefold() == required_header.casefold()
                ),
                {},
            )
            probe = header_profile.get("probe_result", {}) if isinstance(header_profile, dict) else {}
            error_status = probe.get("missing_header_status") if isinstance(probe, dict) else None
            if error_status is None and required_header.casefold() == "authorization":
                error_status = probe.get("no_token_status") if isinstance(probe, dict) else None
            if error_status == 200 and header_profile.get("type") == "audit-context":
                matrix = endpoint.get("scenario_matrix") if isinstance(endpoint.get("scenario_matrix"), dict) else {}
                matrix["authentication"] = {
                    "applicable": False,
                    "status": "confirmed",
                    "reason": str(probe.get("reason") or f"{required_header} is audit context, not authentication"),
                }
                endpoint["scenario_matrix"] = matrix
                Path(module_dir / "endpoints.yaml").write_text(
                    yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
                    encoding="utf-8",
                )
                candidate["coverage_required"] = False
                candidate["resolution"] = "confirmed-not-applicable"
                continue
            if not isinstance(error_status, int):
                unresolved.append(str(candidate.get("id")))
                continue
            if error_status is not None:
                scenario = "authentication" if header_profile.get("type") == "authentication" else "validation"
                header_id = re.sub(r"[^A-Za-z0-9]+", "_", required_header).strip("_").upper()
                case_id = f"{endpoint['id']}_MISSING_{header_id}"
                cases_path = module_dir / "cases.yaml"
                cases_document = load_data(cases_path) if cases_path.is_file() else {}
                cases = first_list(cases_document, "cases")
                if not any(str(item.get("id")) == case_id for item in cases):
                    title = business_case_title(endpoint, scenario, f"缺少 {required_header} Header")
                    cases.append({
                        "id": case_id,
                        "title": title,
                        "description": f"验证缺少必需的 {required_header} Header 时请求被明确拒绝。",
                        "endpoint_id": endpoint["id"],
                        "scenario": scenario,
                        "review_required": True,
                        "status": "draft",
                        "review_reason": "源码和安全配置未提供精确错误响应体",
                        "manual_confirmation": {
                            "automation_blocker": "ControllerAdvice or security configuration does not provide an exact response assertion",
                            "search_records": [{
                                "source_kind": str(candidate.get("kind") or "source"),
                                "file": str(candidate.get("file") or "source"),
                                "symbol": str(candidate.get("symbol") or candidate.get("id")),
                                "line": int(candidate.get("line") or 1),
                                "endpoint_scope": [str(endpoint.get("id"))],
                                "confidence": "high",
                            }],
                        },
                        "logic_ids": [logic_id],
                        "request": {"omit_common_headers": [required_header]},
                        "expected": {"http_status": error_status},
                        "assertions": [{"path": "$", "exists": True}],
                    })
                    updated_cases = dict(cases_document) if isinstance(cases_document, dict) else {}
                    updated_cases.update({
                        "version": 1,
                        "module": str(document.get("module", module_dir.name)),
                        "swagger_tag": document.get("swagger_tag"),
                        "cases": cases,
                    })
                    cases_path.write_text(yaml.safe_dump(updated_cases, allow_unicode=True, sort_keys=False), encoding="utf-8")
                linked_case_ids.append(case_id)
                endpoint["case_ids"] = list(dict.fromkeys([*endpoint.get("case_ids", []), case_id]))
                matrix = endpoint.get("scenario_matrix") if isinstance(endpoint.get("scenario_matrix"), dict) else {}
                matrix[scenario] = {
                    "applicable": True,
                    "status": "inferred",
                    "reason": f"源码要求 {required_header} Header；探针缺失状态为 {error_status}",
                }
                endpoint["scenario_matrix"] = matrix
                Path(module_dir / "endpoints.yaml").write_text(
                    yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
                    encoding="utf-8",
                )
        if endpoint is not None and codes:
            responses = endpoint.get("responses", {}) if isinstance(endpoint.get("responses"), dict) else {}
            profile_handlers = exception_profile.get("handlers", []) if isinstance(exception_profile, dict) else []
            profile_status = next((
                item.get("http_status") for item in profile_handlers
                if isinstance(item, dict)
                and (
                    not item.get("business_codes")
                    or any(str(code) in {str(value) for value in item.get("business_codes", [])} for code in codes)
                )
            ), None)
            error_status = profile_status if isinstance(profile_status, int) else next(
                (int(status) for status in responses if str(status) in {"400", "401", "403", "409", "422"}),
                None,
            )
            if error_status is not None:
                for code in codes:
                    code_token = re.sub(r"[^A-Za-z0-9]+", "_", str(code)).strip("_")
                    case_id = f"{endpoint['id']}_BUSINESS_ERROR_{code_token}"
                    cases_path = module_dir / "cases.yaml"
                    cases_document = load_data(cases_path) if cases_path.is_file() else {}
                    cases = first_list(cases_document, "cases")
                    if not any(str(item.get("id")) == case_id for item in cases):
                        title = business_case_title(endpoint, "business_error", f"业务码 {code}", error_status)
                        cases.append({
                            "id": case_id,
                            "title": title,
                            "description": f"验证源码分支返回业务错误码 {code}。测试数据和触发条件需人工确认。",
                            "endpoint_id": endpoint["id"],
                            "scenario": "business_error",
                            "review_required": True,
                            "status": "draft",
                            "review_reason": "源码给出业务错误码，但缺少可复现的触发请求数据",
                            "manual_confirmation": {
                                "automation_blocker": "No executable trigger value was found in source, SQL, tests, configuration, or runtime evidence",
                                "search_records": [{
                                    "source_kind": str(candidate.get("kind") or "source"),
                                    "file": str(candidate.get("file") or "source"),
                                    "symbol": str(candidate.get("symbol") or candidate.get("id")),
                                    "line": int(candidate.get("line") or 1),
                                    "endpoint_scope": [str(endpoint.get("id"))],
                                    "confidence": "high",
                                }],
                            },
                            "logic_ids": [logic_id],
                            "expected": {
                                "http_status": error_status,
                                "business_code": str(code),
                                "business_code_path": response_business_code_path(endpoint, error_status) or "$.code",
                            },
                            "assertions": [{
                                "path": response_business_code_path(endpoint, error_status) or "$.code",
                                "equals": str(code),
                            }],
                        })
                        updated_cases = dict(cases_document) if isinstance(cases_document, dict) else {}
                        updated_cases.update({
                            "version": 1,
                            "module": str(document.get("module", module_dir.name)),
                            "swagger_tag": document.get("swagger_tag"),
                            "cases": cases,
                        })
                        cases_path.write_text(yaml.safe_dump(updated_cases, allow_unicode=True, sort_keys=False), encoding="utf-8")
                    linked_case_ids.append(case_id)
                    endpoint["case_ids"] = list(dict.fromkeys([*endpoint.get("case_ids", []), case_id]))
                matrix = endpoint.get("scenario_matrix") if isinstance(endpoint.get("scenario_matrix"), dict) else {}
                matrix["business_error"] = {
                    "applicable": True,
                    "status": "inferred",
                    "reason": "源码扫描发现业务错误码：" + ", ".join(str(code) for code in codes),
                }
                endpoint["scenario_matrix"] = matrix
                Path(module_dir / "endpoints.yaml").write_text(
                    yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
                    encoding="utf-8",
                )
        if not linked_case_ids:
            scenario = {
                "validation": "validation",
                "authorization": "authorization",
                "required_header": "authentication",
                "business_exception": "business_error",
                "observable_branch": "business_error",
                "file_exception": "file",
            }.get(str(candidate.get("kind")))
            cases_path = module_dir / "cases.yaml"
            cases_document = load_data(cases_path) if cases_path.is_file() else {}
            cases = first_list(cases_document, "cases")
            matching_cases = [
                case for case in cases
                if str(case.get("endpoint_id")) == str(endpoint.get("id"))
                and str(case.get("scenario")) == scenario
            ]
            fixture_type = str(candidate.get("fixture_type", ""))
            if fixture_type:
                matching_cases = [case for case in matching_cases if str(case.get("fixture_type", "")) == fixture_type]
            if len(matching_cases) == 1:
                linked_case_ids.append(str(matching_cases[0]["id"]))
                matching_cases[0]["logic_ids"] = list(dict.fromkeys([
                    *matching_cases[0].get("logic_ids", []),
                    logic_id,
                ]))
                updated_cases = dict(cases_document) if isinstance(cases_document, dict) else {}
                updated_cases["cases"] = cases
                cases_path.write_text(
                    yaml.safe_dump(updated_cases, allow_unicode=True, sort_keys=False),
                    encoding="utf-8",
                )
            elif fixture_type and scenario == "file":
                success = next((
                    case for case in cases
                    if str(case.get("endpoint_id")) == str(endpoint.get("id"))
                    and str(case.get("scenario")) == "success"
                ), None)
                handler = next((
                    item for item in exception_profile.get("handlers", [])
                    if isinstance(item, dict) and isinstance(item.get("http_status"), int)
                ), None)
                if success and handler:
                    variable = re.sub(r"[^A-Za-z0-9]+", "_", str(endpoint.get("id"))).strip("_").upper()
                    variable = f"{variable}_FILE_{fixture_type.replace('-', '_').upper()}"

                    def replace_file(value: Any) -> Any:
                        if isinstance(value, str) and re.fullmatch(r"\{\{[A-Z0-9_]*FILE[A-Z0-9_]*\}\}", value):
                            return "{{" + variable + "}}"
                        if isinstance(value, dict):
                            return {key: replace_file(child) for key, child in value.items()}
                        if isinstance(value, list):
                            return [replace_file(child) for child in value]
                        return value

                    case_id = f"{endpoint['id']}_FILE_{fixture_type.replace('-', '_').upper()}"
                    path_value = str(handler.get("business_code_path") or "")
                    case = {
                        "id": case_id,
                        "title": business_case_title(endpoint, "file", fixture_type, int(handler["http_status"])),
                        "description": f"验证源码可达文件异常分支：{fixture_type}",
                        "endpoint_id": endpoint["id"],
                        "scenario": "file",
                        "fixture_type": fixture_type,
                        "status": "runnable",
                        "review_required": False,
                        "logic_ids": [logic_id],
                        "request": replace_file(copy.deepcopy(success.get("request", {}))),
                        "expected": {"http_status": int(handler["http_status"])},
                        "assertions": [],
                    }
                    if codes and path_value:
                        case["expected"].update({"business_code": str(codes[0]), "business_code_path": path_value})
                        case["assertions"] = [{"path": path_value, "equals": str(codes[0])}]
                    cases.append(case)
                    linked_case_ids.append(case_id)
                    updated_cases = dict(cases_document) if isinstance(cases_document, dict) else {}
                    updated_cases["cases"] = cases
                    cases_path.write_text(yaml.safe_dump(updated_cases, allow_unicode=True, sort_keys=False), encoding="utf-8")
        if not linked_case_ids:
            unresolved.append(str(candidate.get("id")))
            continue
        items.append({
            "id": logic_id,
            "status": "inferred",
            "source": "source-scan",
            "source_candidate_id": candidate["id"],
            "source_symbol": candidate.get("symbol") or f"{Path(str(candidate.get('file'))).name}:{candidate.get('line')}",
            "endpoint_id": endpoint.get("id") if endpoint else None,
            "condition": candidate.get("source_excerpt", candidate.get("evidence")),
            "required_header": required_header,
            "expected_business_code": codes[0] if codes else None,
            "fixture_type": candidate.get("fixture_type"),
            "case_ids": linked_case_ids,
            "evidence": {
                "source_kind": str(candidate.get("kind") or "source"),
                "file": str(candidate.get("file") or "source"),
                "symbol": str(candidate.get("symbol") or candidate.get("id")),
                "line": int(candidate.get("line") or 1),
                "endpoint_scope": [str(endpoint.get("id"))] if endpoint else [],
                "confidence": "high",
            },
        })
        updated = dict(logic_document) if isinstance(logic_document, dict) else {}
        updated["logic"] = items
        logic_path.write_text(yaml.safe_dump(updated, allow_unicode=True, sort_keys=False), encoding="utf-8")
    ledger = dict(result)
    ledger["unresolved_candidate_ids"] = unresolved
    ledger_path = (
        modules[0][0] / "source-logic-candidates.yaml"
        if module_filter and len(modules) == 1
        else contracts_root / "source-logic-candidates.yaml"
    )
    ledger_path.write_text(yaml.safe_dump(ledger, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return unresolved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_roots", type=Path, nargs="+")
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--include", action="append", help="source-file glob owned by the selected module or endpoint; may be repeated")
    parser.add_argument("--exception-type")
    parser.add_argument("--error-code-type")
    parser.add_argument("--contracts-root", type=Path, help="apply coverage-required candidates to module logic.yaml")
    parser.add_argument("--module", help="limit writes to one module directory")
    args = parser.parse_args()
    missing = [str(root) for root in args.source_roots if not root.is_dir()]
    if missing:
        parser.error(f"source root(s) do not exist: {', '.join(missing)}")
    result = scan(args.source_roots, args.include, args.exception_type, args.error_code_type)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    scan_errors = [str(error) for error in result.get("errors", [])]
    for error in scan_errors:
        print(f"ERROR: {error}", file=sys.stderr)
    if args.contracts_root and not scan_errors:
        unresolved = apply_candidates(result, args.contracts_root, args.module)
        if unresolved:
            for candidate_id in unresolved:
                print(
                    f"ERROR: source candidate {candidate_id} cannot be uniquely mapped to an endpoint",
                    file=sys.stderr,
                )
            return 2
    print(f"wrote {len(result['candidates'])} cross-language logic candidates to {args.output}")
    return 2 if scan_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
