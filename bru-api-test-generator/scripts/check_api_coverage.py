#!/usr/bin/env python3
"""Reconcile modular endpoint, logic, case, and flow manifests with Bruno files.

The checker intentionally validates both directions: manifests must map to
unique Bruno files, and every Bruno file must be registered. When an offline
OpenAPI document is supplied, the endpoint union is checked against it too.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

try:
    from manifest_io import first_list, list_at, load_data
except ImportError:  # Keep a copied checker runnable without the helper module.
    def load_data(path: Path) -> Any:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            return json.loads(text)
        try:
            import yaml  # type: ignore[import-not-found]
        except ModuleNotFoundError as exc:
            raise SystemExit("YAML manifests require PyYAML") from exc
        return yaml.safe_load(text)

    def first_list(document: Any, key: str) -> list[dict[str, Any]]:
        if isinstance(document, dict) and isinstance(document.get(key), list):
            return [item for item in document[key] if isinstance(item, dict)]
        if isinstance(document, list):
            return [item for item in document if isinstance(item, dict)]
        return []

    def list_at(document: Any, key: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        if isinstance(document, dict):
            found.extend(item for item in document.get(key, []) if isinstance(item, dict))
            for child in document.values():
                found.extend(list_at(child, key))
        elif isinstance(document, list):
            for child in document:
                found.extend(list_at(child, key))
        return found
try:
    from parse_openapi import extract, load_document
except ImportError:  # The checker is also intended to be copied on its own.
    def load_document(path: Path) -> dict[str, Any]:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            loaded = json.loads(text)
        else:
            try:
                import yaml  # type: ignore[import-not-found]
            except ModuleNotFoundError as exc:
                raise SystemExit("YAML OpenAPI input requires PyYAML") from exc
            loaded = yaml.safe_load(text)
        if not isinstance(loaded, dict):
            raise ValueError("OpenAPI document must contain an object")
        return loaded

    def stable_id(method: str, path: str, operation: dict[str, Any]) -> str:
        operation_id = operation.get("operationId")
        raw = f"{operation_id}_{method}_{path}" if operation_id else f"{method}_{path}"
        return re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_").upper() or "ENDPOINT"

    def extract(path: Path, document: dict[str, Any]) -> dict[str, Any]:
        paths = document.get("paths")
        if not isinstance(paths, dict):
            raise ValueError("OpenAPI document has no object-valued paths field")
        methods = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
        endpoints = []
        for route, path_item in paths.items():
            if not isinstance(route, str) or not isinstance(path_item, dict):
                continue
            for method, operation in path_item.items():
                if method.lower() in methods and isinstance(operation, dict):
                    endpoints.append({
                        "id": stable_id(method.upper(), route, operation),
                        "method": method.upper(),
                        "path": route,
                    })
        return {"endpoints": sorted(endpoints, key=lambda item: (item["path"], item["method"]))}

from execution_config import (
    COLLECTION_END_MARKER,
    COLLECTION_MARKER,
    HEADER_NAME_RE,
    environment_file,
    load_bruno_environment_document,
    load_execution_config,
)
from qa_lock import check as check_qa_lock
from materialize_missing_bru import request_url as materialized_request_url

SCENARIO_CATEGORIES = (
    "success",
    "authentication",
    "authorization",
    "validation",
    "business_error",
    "query",
    "safety",
    "file",
)
RISK_CLASSES = {"read-only", "isolated-write", "destructive", "external-side-effect"}
STRICT_REPORT_VERSION = 2
STRICT_REPORT_FIELDS = (
    "scenario_matrix_checked",
    "constraint_obligations_checked",
    "exact_assertions_checked",
    "variables_checked",
    "source_mapping_checked",
    "qa_lock_checked",
)
VARIABLE_RE = re.compile(r"\{\{([^{}]+)\}\}")

INTEGRITY_SUFFIXES = {".yaml", ".yml", ".md", ".bru"}
MOJIBAKE_RE = re.compile(r"(?:\ufffd|(?:Ã|Â|å|æ|ç)[\x80-\xBF]|â(?:€|™|œ|€�))")
CASE_DOCS_START = "<!-- AUTO_CASES_START -->"
CASE_DOCS_END = "<!-- AUTO_CASES_END -->"
CHINESE_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
BUSINESS_FILE_RE = re.compile(r"^\d{2,}-.+\.bru$", re.IGNORECASE)
GENERIC_CASE_TITLES = {"请求成功", "操作成功", "成功", "请求失败", "参数校验失败"}


def is_business_request(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    return path.name.lower() != "collection.bru" and "environments" not in {
        part.lower() for part in relative.parts[:-1]
    }


def normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9\u3400-\u4dbf\u4e00-\u9fff]+", "", value.casefold())


def valid_business_filename(
    path: Path,
    case_id: str,
    title: str,
    sequence: int | None = None,
) -> bool:
    if not BUSINESS_FILE_RE.fullmatch(path.name):
        return False
    number, description = path.stem.split("-", 1)
    expected_title = re.sub(r"[\\/\x00-\x1f<>:\"|?*]", "", title).strip(" .")
    return bool(
        description.strip()
        and CHINESE_RE.search(description)
        and normalized_name(description) != normalized_name(case_id)
        and description == expected_title
        and (sequence is None or int(number) == sequence)
    )


def bru_meta_name(content: str) -> str | None:
    block = re.search(r"(?ms)^\s*meta\s*\{(.*?)^\s*\}", content)
    if block is None:
        block = re.search(r"(?s)\bmeta\s*\{(.*?)\}", content)
    if block is None:
        return None
    match = re.search(r"(?:^|\s)name:\s*([^\r\n}]+)", block.group(1))
    return match.group(1).strip() if match else None


def bru_meta_sequence(content: str) -> int | None:
    block = re.search(r"(?ms)^\s*meta\s*\{(.*?)^\s*\}", content)
    if block is None:
        return None
    match = re.search(r"(?m)^\s*seq:\s*(\d+)\s*$", block.group(1))
    return int(match.group(1)) if match else None


def bru_meta_tags(content: str) -> set[str]:
    block = re.search(r"(?ms)^\s*meta\s*\{(.*?)^\s*\}", content)
    if block is None:
        return set()
    match = re.search(r"(?m)^\s*tags:\s*\[([^]]*)\]\s*$", block.group(1))
    return {item.strip() for item in match.group(1).split(",") if item.strip()} if match else set()


def is_http_request_content(content: str) -> bool:
    meta = re.search(r"(?ms)^\s*meta\s*\{(.*?)^\s*\}", content)
    if meta is None or not re.search(r"(?m)^\s*type:\s*http\s*$", meta.group(1), re.IGNORECASE):
        return False
    return bool(re.search(r"(?mi)^\s*(?:get|post|put|patch|delete|head|options|trace)\s*\{", content))


def iter_integrity_files(*roots: Path):
    """Yield scoped text artifacts without following repository metadata."""

    seen: set[Path] = set()
    for root in roots:
        if root.is_file():
            candidates = [root]
        elif root.is_dir():
            candidates = root.rglob("*")
        else:
            continue
        for path in candidates:
            if not path.is_file() or path.suffix.lower() not in INTEGRITY_SUFFIXES:
                continue
            if ".git" in path.parts or path.resolve() in seen:
                continue
            seen.add(path.resolve())
            yield path


def text_integrity_errors(*roots: Path) -> list[str]:
    """Reject replacement characters, decode failures, and common mojibake."""

    errors: list[str] = []
    for path in iter_integrity_files(*roots):
        raw = b""
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            line_no = raw[:exc.start].count(b"\n") + 1
            errors.append(f"UTF-8 integrity failure {path}:{line_no}: {exc}")
            continue
        except OSError as exc:
            errors.append(f"UTF-8 integrity failure {path}: {exc}")
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            match = MOJIBAKE_RE.search(line)
            if match:
                errors.append(f"UTF-8 integrity failure {path}:{line_no}: {match.group(0)!r}")
    return errors


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def case_fingerprint(case: dict[str, Any]) -> str:
    """Fingerprint the observable request/scenario/assertion contract."""

    payload = {
        "endpoint_id": case.get("endpoint_id"),
        "scenario": case.get("scenario", case.get("scenarios")),
        "request": case.get("request"),
        "assertions": case.get("assertions"),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def case_completion_errors(case: dict[str, Any]) -> list[str]:
    case_id = str(case.get("id", "<unknown>"))
    errors: list[str] = []
    if case.get("review_required") is True or case.get("manual_review") is True or case.get("status") == "draft":
        errors.append(f"case {case_id} is still draft or requires review")
    assertions = case.get("assertions")
    precise_keys = {
        "equals", "eq", "contains", "matches", "type", "length", "minimum",
        "maximum", "nullable", "is_null", "equals_variable", "items", "item_type",
    }
    if not isinstance(assertions, list) or not any(
        isinstance(item, dict) and bool(precise_keys & set(item))
        for item in assertions
    ):
        errors.append(f"case {case_id} has no exact assertion eligible for verified completion")
    if isinstance(assertions, list) and any(
        isinstance(item, dict) and item.get("exists") is True and not _assertion_is_exact(item)
        for item in assertions
    ):
        errors.append(f"case {case_id} contains an existence-only assertion")
    return errors


def _assertion_is_exact(assertion: Any) -> bool:
    return isinstance(assertion, dict) and bool({
        "equals", "eq", "contains", "matches", "length", "minimum", "maximum",
        "equals_variable", "items", "item_type",
    } & set(assertion))


def success_assertion_errors(case: dict[str, Any]) -> list[str]:
    if not case_covers_scenario(case, "success"):
        return []
    case_id = str(case.get("id", "<unknown>"))
    assertions = [item for item in case.get("assertions", []) if isinstance(item, dict)]
    expected = case.get("expected", {}) if isinstance(case.get("expected"), dict) else {}
    business_path = str(expected.get("business_code_path", "$.code"))
    business_value = expected.get("business_code")
    business_assertions = [
        item for item in assertions
        if str(item.get("path")) in {"$.status", "$.errorCode", "$.code", business_path}
        and ("equals" in item or "eq" in item)
    ]
    if business_value is not None:
        business_assertions.append({"path": business_path, "equals": business_value})
    success_values = {0, "0", 200, "200", True, "true", "ok", "success", "SUCCESS", "OK"}
    if not business_assertions or any(
        item.get("equals", item.get("eq")) not in success_values
        for item in business_assertions
    ):
        return [f"success case {case_id} has no exact business-success assertion"]
    business_paths = {str(item.get("path")) for item in business_assertions}
    if not any(_assertion_is_exact(item) and str(item.get("path")) not in business_paths for item in assertions):
        return [f"success case {case_id} has no exact result-field or request/response assertion"]
    return []


def query_assertion_errors(case: dict[str, Any]) -> list[str]:
    if not case_covers_scenario(case, "query"):
        return []
    expected = case.get("expected", {}) if isinstance(case.get("expected"), dict) else {}
    status = expected.get("http_status")
    if isinstance(status, int) and status >= 400:
        return []
    case_id = str(case.get("id", "<unknown>"))
    assertions = [item for item in case.get("assertions", []) if isinstance(item, dict)]
    if "__NO_MATCH__" in canonical_json(case.get("request", {})) and not any(
        item.get("length") == 0 for item in assertions
    ):
        return [f"query case {case_id} has no exact empty-result assertion"]
    result_roots = ("$.data", "$.result", "$.items", "$.list", "$.records", "$.content", "$.page", "$.total")
    if not any(
        _assertion_is_exact(item)
        and (
            str(item.get("path", "")) == "$"
            or any(str(item.get("path", "")).startswith(root) for root in result_roots)
        )
        for item in assertions
    ):
        return [f"query case {case_id} has no exact result or pagination assertion"]
    return []


def _variables(value: Any) -> set[str]:
    if isinstance(value, str):
        return {item.strip() for item in VARIABLE_RE.findall(value) if item.strip() and not item.strip().startswith("$")}
    if isinstance(value, dict):
        return set().union(*(_variables(item) for item in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_variables(item) for item in value), set())
    return set()


def case_context_errors(
    case: dict[str, Any],
    available_variables: set[str],
    captured_variables: set[str],
) -> list[str]:
    case_id = str(case.get("id", "<unknown>"))
    errors: list[str] = []
    variables = _variables({
        "request": case.get("request"),
        "assertions": case.get("assertions"),
        "expected": case.get("expected"),
    })
    missing = sorted(variables - available_variables - captured_variables)
    if missing:
        errors.append(f"case {case_id} references undefined variable(s): {', '.join(missing)}")
    request_text = json.dumps(case.get("request", {}), ensure_ascii=False, sort_keys=True)
    if re.search(r"review-[A-Za-z0-9_.-]+", request_text, re.IGNORECASE):
        errors.append(f"case {case_id} contains review-* placeholder data")
    endpoint_path = str(case.get("resolved_endpoint_path", ""))
    request = case.get("request", {}) if isinstance(case.get("request"), dict) else {}
    request_path = str(request.get("path") or endpoint_path)
    for name in re.findall(r"(?<!\{)\{([^{}]+)\}(?!\})", request_path):
        if name not in request.get("path_parameters", {}):
            errors.append(f"case {case_id} has unresolved path parameter: {name}")
    return errors


def case_covers_scenario(case: dict[str, Any], category: str) -> bool:
    direct = str(case.get("scenario") or case.get("category") or "").strip().lower()
    if direct == category:
        return True
    scenarios = case.get("scenarios")
    return (
        isinstance(scenarios, dict)
        and isinstance(scenarios.get(category), dict)
        and scenarios[category].get("applicable") is True
    )


def exclusion_status(item: dict[str, Any]) -> str:
    return str(item.get("status", "approved")).strip().lower() or "approved"


def is_approved_exclusion(item: dict[str, Any]) -> bool:
    return exclusion_status(item) == "approved" and bool(str(item.get("reason", "")).strip())


def ids(items: list[dict[str, Any]]) -> set[str]:
    return {str(item["id"]) for item in items if item.get("id")}


def flow_required(endpoints_doc: Any, endpoints: list[dict[str, Any]]) -> bool:
    """Require an ordered flow only when the manifest explicitly opts in."""

    if isinstance(endpoints_doc, dict) and endpoints_doc.get("flow_required") is True:
        return True
    return any(
        endpoint.get("flow_required") is True or bool(str(endpoint.get("flow_kind", "")).strip())
        for endpoint in endpoints
    )


def flow_execution_errors(flow_items: list[dict[str, Any]], results: dict[str, Any]) -> list[str]:
    """Require a minimal execution record for every declared module flow.

    The dedicated flow validator still checks captures and cleanup shape in
    detail. The coverage gate must at least reject missing or failed flow
    evidence before it can claim a verified collection.
    """

    actual_flows = results.get("flows") if isinstance(results.get("flows"), dict) else {}
    errors: list[str] = []
    for flow in flow_items:
        flow_id = str(flow.get("id", ""))
        actual = actual_flows.get(flow_id)
        if not isinstance(actual, dict):
            errors.append(f"flow {flow_id} has no execution evidence")
            continue
        if actual.get("status") != "passed":
            errors.append(f"flow {flow_id} status is {actual.get('status')}")
        expected_ids = [step.get("case_id") for step in flow.get("steps", []) if isinstance(step, dict)]
        actual_ids = [step.get("case_id") for step in actual.get("steps", []) if isinstance(step, dict)]
        if actual_ids != expected_ids:
            errors.append(f"flow {flow_id} executed order {actual_ids}, expected {expected_ids}")
        creates = any(
            isinstance(step, dict) and str(step.get("operation", "")).lower() == "create"
            for step in flow.get("steps", [])
        )
        if creates and not actual.get("cleanup_verified") and not str(flow.get("cleanup", "")).strip():
            errors.append(f"flow {flow_id} has no verified cleanup evidence")
    return errors


def bruno_assertion_paths(json_path: Any) -> set[str]:
    """Return accepted Bruno expressions for a JSON, text, header, or cookie assertion."""

    value = str(json_path or "")
    if isinstance(json_path, dict):
        target = str(json_path.get("target") or json_path.get("kind") or "").strip().lower()
        value = str(json_path.get("path") or "")
        if target in {"header", "response.header", "headers", "response.headers"}:
            name = value.removeprefix("$response.headers.").removeprefix("$.headers.")
            return {f"res.headers.{name}", f"res.headers['{name}']"} if name else set()
        if target in {"cookie", "response.cookie", "cookies", "response.cookies"}:
            name = value.removeprefix("$response.cookies.").removeprefix("$.cookies.")
            return {f"res.cookies.{name}", f"res.cookies['{name}']"} if name else set()
        if target in {"text", "body_text", "response.body", "raw", "xml", "binary", "file"}:
            return {"res.body"}
    if value == "$":
        return {"res.body"}
    if value.startswith("$."):
        return {"res.body" + value[1:]}
    if value.startswith("$response.headers."):
        header = value[len("$response.headers."):]
        return {f"res.headers['{header}']", f"res.headers.{header}"}
    return set()


def assertion_requirements(assertion: dict[str, Any]) -> list[tuple[str, list[str]]]:
    expressions = bruno_assertion_paths(assertion)
    if not expressions:
        return []
    requirements: list[tuple[str, list[str]]] = []

    def value(key: str) -> str:
        return json.dumps(assertion[key], ensure_ascii=False, separators=(",", ": "))

    operators = {
        "equals": ("eq", lambda: value("equals")),
        "eq": ("eq", lambda: value("eq")),
        "contains": ("contains", lambda: value("contains")),
        "matches": ("matches", lambda: value("matches")),
        "length": ("length", lambda: str(assertion["length"])),
        "minimum": ("gte", lambda: value("minimum")),
        "maximum": ("lte", lambda: value("maximum")),
        "equals_variable": ("eq", lambda: "{{" + str(assertion["equals_variable"]) + "}}"),
    }
    for key, (operator, rendered) in operators.items():
        if key in assertion:
            requirements.append((key, [f"{expression}: {operator} {rendered()}" for expression in expressions]))
    if assertion.get("exists") is True:
        requirements.append(("exists", [f"{expression}: exists" for expression in expressions]))
    type_operator = {
        "string": "isString", "number": "isNumber", "integer": "isNumber",
        "boolean": "isBoolean", "array": "isArray", "object": "isObject",
    }.get(str(assertion.get("type", "")).lower())
    if type_operator:
        requirements.append(("type", [f"{expression}: {type_operator}" for expression in expressions]))
    if assertion.get("nullable") is False:
        requirements.append(("nullable", [f"{expression}: isNotNull" for expression in expressions]))
    if assertion.get("is_null") is True:
        requirements.append(("is_null", [f"{expression}: isNull" for expression in expressions]))
    capture = assertion.get("capture_as") or assertion.get("capture")
    if capture:
        name = json.dumps(str(capture))
        requirements.append(("capture", [f"bru.setVar({name}, {expression})" for expression in expressions]))
    items = assertion.get("items")
    item_type = items.get("type") if isinstance(items, dict) else assertion.get("item_type")
    if item_type:
        requirements.append(("items.type", [f"{expression}.forEach" for expression in expressions]))
    if str(assertion.get("type", "")).lower() == "integer":
        requirements.append(("integer", [f"Number.isInteger({expression})" for expression in expressions]))
    return requirements


def module_directory_name(module: Any, module_id: str) -> str:
    value = module.get("directory", module_id) if isinstance(module, dict) else module_id
    directory = str(value).strip()
    if not directory or directory in {".", ".."} or Path(directory).name != directory:
        raise ValueError(f"module {module_id} directory must be one safe path segment")
    tags = module.get("swagger_tags") if isinstance(module, dict) else None
    if isinstance(tags, list) and len(tags) == 1 and isinstance(tags[0], str):
        expected = unicodedata.normalize("NFC", tags[0]).strip()
        expected = re.sub(r"[\\/\x00-\x1f<>:\"|?*]+", "-", expected)
        expected = re.sub(r"\s+", "-", expected).strip(" .-") or module_id
        if directory != expected:
            raise ValueError(
                f"module {module_id} directory must come from Swagger tag {tags[0]!r}: expected {expected!r}"
            )
    return directory


def module_dirs(contracts_root: Path, module_map: Any = None) -> list[tuple[str, Path]]:
    if (contracts_root / "endpoints.yaml").is_file():
        return [(contracts_root.name, contracts_root)]
    modules_root = contracts_root / "modules"
    if not modules_root.is_dir():
        modules_root = contracts_root
    directory_to_id = {}
    if isinstance(module_map, dict) and isinstance(module_map.get("modules"), list):
        for module in module_map["modules"]:
            if isinstance(module, dict) and module.get("id"):
                module_id = str(module["id"])
                try:
                    directory_to_id[module_directory_name(module, module_id)] = module_id
                except ValueError:
                    continue
    found = [
        (directory_to_id.get(path.name, path.name), path)
        for path in sorted(modules_root.iterdir())
        if path.is_dir() and (path / "endpoints.yaml").is_file()
    ]
    if not found:
        raise SystemExit(f"no module endpoints.yaml files found below {contracts_root}")
    return found


def module_tag_from_document(document: Any, module_id: str) -> str | None:
    if not isinstance(document, dict):
        return None
    modules = document.get("modules")
    if not isinstance(modules, list):
        return None
    for module in modules:
        if isinstance(module, dict) and str(module.get("id")) == module_id:
            tags = module.get("swagger_tags")
            if isinstance(tags, list) and len(tags) == 1 and isinstance(tags[0], str) and tags[0].strip():
                return tags[0].strip()
            return str(module.get("tag") or module.get("name") or module_id).strip()
    return None


def fallback_module_for_endpoint(endpoint: dict[str, Any], modules: list[dict[str, Any]]) -> str | None:
    operation_id = str(endpoint.get("operation_id") or "")
    path = str(endpoint.get("path") or "")
    matches: list[str] = []
    for module in modules:
        if not isinstance(module, dict) or not module.get("id"):
            continue
        ids = module.get("operation_ids", [])
        prefixes = module.get("path_prefixes", [])
        ids = [ids] if isinstance(ids, str) else ids
        prefixes = [prefixes] if isinstance(prefixes, str) else prefixes
        if operation_id and operation_id in {str(item) for item in ids if item is not None}:
            matches.append(str(module["id"]))
        elif any(path == str(prefix) or path.startswith(str(prefix).rstrip("/") + "/") for prefix in prefixes):
            matches.append(str(module["id"]))
    if len(matches) == 1:
        return matches[0]
    defaults = [str(module["id"]) for module in modules if isinstance(module, dict) and module.get("id") and module.get("default") is True]
    if len(defaults) == 1:
        return defaults[0]
    if len(modules) == 1 and isinstance(modules[0], dict) and modules[0].get("id"):
        return str(modules[0]["id"])
    return None


def validate_tag_partition(
    module_map: Any,
    endpoint_records: list[tuple[str, dict[str, Any]]],
    offline_endpoints: list[dict[str, Any]],
) -> list[str]:
    """Validate Tag ownership, or the explicit fallback owner for untagged operations."""

    errors: list[str] = []
    # Keep this initialized even when module-map.yaml is absent or malformed.
    # Untagged operations still need a deterministic error instead of leaking
    # an UnboundLocalError while the checker is constructing that error.
    modules: list[dict[str, Any]] = []
    map_tags: dict[str, str] = {}
    module_to_tag: dict[str, str] = {}
    module_ids: set[str] = set()
    if isinstance(module_map, dict):
        modules = module_map.get("modules", [])
        if not isinstance(modules, list):
            errors.append("module-map.yaml modules must be a list")
            modules = []
        else:
            for module in modules:
                if not isinstance(module, dict):
                    errors.append("module-map.yaml contains a non-object module")
                    continue
                module_id = str(module.get("id", ""))
                if module_id in module_ids:
                    errors.append(f"module-map.yaml repeats module id {module_id}")
                module_ids.add(module_id)
                if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", module_id):
                    errors.append(f"module id {module_id} is not a stable ASCII slug")
                tags = module.get("swagger_tags")
                if tags is not None and (
                    not isinstance(tags, list)
                    or len(tags) > 1
                    or any(not isinstance(tag, str) or not tag.strip() for tag in tags)
                ):
                    errors.append(f"module {module_id} swagger_tags must contain zero or one non-empty value")
                    continue
                if isinstance(tags, list) and len(tags) == 1:
                    tag = tags[0].strip()
                    if "name" in module and module.get("name") != tag:
                        errors.append(f"module {module_id} name does not preserve Swagger tag {tag!r}")
                    if tag in map_tags and map_tags[tag] != module_id:
                        errors.append(f"Swagger tag {tag!r} is assigned to modules {map_tags[tag]} and {module_id}")
                    map_tags[tag] = module_id
                    module_to_tag[module_id] = tag
                else:
                    module_to_tag[module_id] = str(module.get("tag") or module.get("name") or module_id).strip()
    elif endpoint_records:
        errors.append("module-map.yaml is required for strict Swagger-tag partitioning")

    offline_by_id = {str(item.get("id")): item for item in offline_endpoints if item.get("id")}
    offline_tags = {
        str(tag).strip()
        for item in offline_endpoints
        for tag in (item.get("tags") or [])
        if isinstance(tag, str) and tag.strip()
    }
    errors.extend(
        f"Swagger tag {tag!r} is not assigned to a module"
        for tag in sorted(offline_tags - set(map_tags))
    )
    errors.extend(
        f"module-map.yaml tag {tag!r} is not present in OpenAPI"
        for tag in sorted(set(map_tags) - offline_tags)
    )
    for module_id, endpoint in endpoint_records:
        endpoint_id = str(endpoint.get("id", ""))
        offline = offline_by_id.get(endpoint_id)
        tags = endpoint.get("tags")
        if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag.strip() for tag in tags):
            errors.append(f"endpoint {endpoint_id} has invalid Swagger tags")
            continue
        tags = list(dict.fromkeys(tag.strip() for tag in tags))
        source_tags = offline.get("tags", []) if offline else tags
        if not source_tags:
            owner = fallback_module_for_endpoint(endpoint, modules if isinstance(module_map, dict) else [])
            if owner != module_id:
                errors.append(
                    f"untagged operation {endpoint.get('method')} {endpoint.get('path')} belongs to module "
                    f"{owner or '<unmapped>'}, not {module_id}"
                )
        elif set(tags) != set(str(tag).strip() for tag in source_tags):
            errors.append(f"endpoint {endpoint_id} tags do not match offline OpenAPI tags")
        primary = endpoint.get("primary_tag") or endpoint.get("swagger_tag")
        if len(tags) == 1:
            primary = primary or tags[0]
        if not isinstance(primary, str) or not primary.strip():
            errors.append(f"endpoint {endpoint_id} has no unique primary_tag")
            continue
        primary = primary.strip()
        if source_tags and primary not in tags:
            errors.append(f"endpoint {endpoint_id} primary_tag {primary!r} is not declared in tags")
        owner = map_tags.get(primary) if source_tags else module_id
        if not source_tags:
            owner = fallback_module_for_endpoint(endpoint, modules if isinstance(module_map, dict) else [])
        if owner != module_id:
            errors.append(f"endpoint {endpoint_id} Tag {primary!r} belongs to module {owner or '<unmapped>'}, not {module_id}")
        module_tag = module_to_tag.get(module_id)
        if module_tag and module_tag != primary:
            errors.append(f"endpoint {endpoint_id} Tag {primary!r} does not match module {module_id} Tag {module_tag!r}")
        if endpoint.get("module") and str(endpoint.get("module")) != module_id:
            errors.append(f"endpoint {endpoint_id} declares module {endpoint.get('module')} but is stored under {module_id}")

    mapped_ids = {module_id for module_id, _ in endpoint_records}
    errors.extend(
        f"module-map.yaml module {module_id} has no endpoints.yaml inventory"
        for module_id in sorted(set(module_to_tag) - mapped_ids)
    )
    return errors


def validate_cross_module_flows(contracts_root: Path, case_modules: dict[str, str]) -> list[str]:
    """Keep journeys spanning Tags in the coordinator-owned flow manifest."""

    path = contracts_root / "flows" / "cross-module.yaml"
    if not path.is_file():
        return []
    try:
        document = load_data(path)
    except SystemExit as exc:
        return [str(exc)]
    errors: list[str] = []
    for flow in first_list(document, "flows"):
        flow_id = str(flow.get("id", ""))
        modules = set()
        for step in flow.get("steps", []):
            case_id = str(step.get("case_id", ""))
            if case_id not in case_modules:
                errors.append(f"cross-module flow {flow_id} references unknown case {case_id}")
            else:
                modules.add(case_modules[case_id])
        if len(modules) < 2:
            errors.append(f"cross-module flow {flow_id} does not span multiple Tag modules")
    return errors


def validate_module_artifact(
    path: Path,
    module_id: str,
    module_tag: str | None,
    endpoint_ids: set[str],
    collection_key: str,
) -> list[str]:
    if not path.is_file():
        return [f"module {module_id} is missing {path.name}"]
    document = load_data(path)
    errors: list[str] = []
    if isinstance(document, dict) and document.get("module") and str(document.get("module")) != module_id:
        errors.append(f"{path.name} declares module {document.get('module')} but is stored under {module_id}")
    if module_tag and isinstance(document, dict) and document.get("swagger_tag") != module_tag:
        errors.append(f"{path.name} Swagger tag does not match module {module_id}")
    for item in first_list(document, collection_key):
        endpoint_id = item.get("endpoint_id")
        if endpoint_id and str(endpoint_id) not in endpoint_ids:
            errors.append(f"{path.name} references endpoint outside module {module_id}: {endpoint_id}")
    return errors


def validate_module_documentation(path: Path, cases: list[dict[str, Any]]) -> list[str]:
    """Require one human-readable Chinese-first documentation block per case."""

    if not path.is_file():
        return [f"module documentation is missing: {path}"]
    try:
        content = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as exc:
        return [f"cannot read module documentation {path}: {exc}"]
    errors: list[str] = []
    for marker in ("业务范围：", "## 模块内容", "## 接口清单", CASE_DOCS_START, CASE_DOCS_END):
        if marker not in content:
            errors.append(f"{path.name} is missing required section marker {marker}")
    documented_ids = re.findall(r"<!-- CASE_START: ([^\s>]+) -->", content)
    declared_ids = {str(case.get("id")) for case in cases if case.get("id")}
    if cases and not re.search(r"```mermaid\s*\n\s*sequenceDiagram\b", content):
        errors.append(f"{path.name} has no module-level Mermaid sequenceDiagram")
    for case_id in sorted(declared_ids):
        if documented_ids.count(case_id) != 1:
            errors.append(f"case {case_id} must have exactly one documentation block in {path.name}")
            continue
        match = re.search(
            rf"<!-- CASE_START: {re.escape(case_id)} -->\s*(.*?)\s*<!-- CASE_END: {re.escape(case_id)} -->",
            content,
            re.DOTALL,
        )
        if match is None:
            errors.append(f"case {case_id} documentation block is not closed correctly in {path.name}")
            continue
        block = match.group(1)
        if not re.search(r"(?m)^#{2,6}\s+\S", block):
            errors.append(f"case {case_id} documentation has no title")
        if not re.search(r"(?m)^简短描述：\s*\S", block):
            errors.append(f"case {case_id} documentation has no short description")
        case = next((item for item in cases if str(item.get("id")) == case_id), {})
        if case.get("flow_required") is True:
            if not re.search(r"```mermaid\s*\n\s*sequenceDiagram\b", block):
                errors.append(f"flow case {case_id} documentation has no Mermaid sequenceDiagram swimlane")
            elif len(re.findall(r"(?m)^\s*participant\s+", block)) < 2 or "->>" not in block:
                errors.append(f"flow case {case_id} Mermaid swimlane has insufficient participants or messages")
    for case_id in sorted(set(documented_ids) - declared_ids):
        errors.append(f"{path.name} documents unknown case {case_id}")
    return errors


def validate_contracts_readme(contracts_root: Path, modules: list[tuple[str, Path]]) -> list[str]:
    path = contracts_root / "README.md"
    if not path.is_file():
        return [f"missing contracts module overview: {path}"]
    try:
        content = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as exc:
        return [f"cannot read contracts module overview {path}: {exc}"]
    errors: list[str] = []
    for module_id, _ in modules:
        start_marker = f"<!-- MODULE_START: {module_id} -->"
        end_marker = f"<!-- MODULE_END: {module_id} -->"
        if content.count(start_marker) != 1 or content.count(end_marker) != 1:
            errors.append(f"README.md must contain exactly one overview block for module {module_id}")
            continue
        match = re.search(
            rf"<!-- MODULE_START: {re.escape(module_id)} -->\s*(.*?)\s*<!-- MODULE_END: {re.escape(module_id)} -->",
            content,
            re.DOTALL,
        )
        if match is None:
            errors.append(f"README.md has no overview for module {module_id}")
            continue
        block = match.group(1)
        if not re.search(r"(?m)^- 业务范围：\s*\S", block):
            errors.append(f"README.md module {module_id} has no business scope")
        if not re.search(r"(?m)^- 包含内容：\s*\S", block):
            errors.append(f"README.md module {module_id} has no content summary")
        if "CASES.md" not in block:
            errors.append(f"README.md module {module_id} has no module documentation link")
    return errors


def referenced_components(value: Any) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str):
            match = re.match(r"^#/components/(schemas|parameters|responses)/([^/]+)$", ref)
            if match:
                found.add((match.group(1), match.group(2)))
            match = re.match(r"^#/(parameters|responses)/([^/]+)$", ref)
            if match:
                found.add((match.group(1), match.group(2)))
            match = re.match(r"^#/definitions/([^/]+)$", ref)
            if match:
                found.add(("schemas", match.group(1)))
        for child in value.values():
            found.update(referenced_components(child))
    elif isinstance(value, list):
        for child in value:
            found.update(referenced_components(child))
    return found


def bruno_request(content: str) -> dict[str, Any]:
    match = re.search(
        r"(?mis)^\s*(get|post|put|patch|delete|head|options|trace)\s*\{(.*?)^\s*\}",
        content,
    )
    if match is None:
        return {}
    block = match.group(2)
    url = re.search(r"(?m)^\s*url:\s*(\S+)\s*$", block)
    body_type = re.search(r"(?m)^\s*body:\s*(\S+)\s*$", block)
    return {
        "method": match.group(1).upper(),
        "url": url.group(1) if url else "",
        "body_type": body_type.group(1).lower() if body_type else "none",
    }


def bruno_body(content: str, kind: str) -> str | None:
    match = re.search(rf"(?mi)^\s*body:{re.escape(kind)}\s*\{{", content)
    if match is None:
        return None
    start = content.find("{", match.start())
    depth = 0
    quote = ""
    escaped = False
    for index in range(start, len(content)):
        char = content[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return content[start + 1:index].strip()
    return None


def parse_json_body(value: Any, source: str) -> Any:
    """Parse JSON strictly so malformed Bruno bodies are never reported as missing fields."""

    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{source} is invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc


def bruno_headers(content: str) -> dict[str, str]:
    match = re.search(r"(?mis)^\s*headers\s*\{(.*?)^\s*\}", content)
    if match is None:
        return {}
    result = {}
    for line in match.group(1).splitlines():
        item = re.match(r"\s*([^:]+):\s*(.*)$", line)
        if item:
            result[item.group(1).strip()] = item.group(2).strip()
    return result


def normalized_body(value: Any, kind: str, rendered: bool = False) -> Any:
    if kind == "json":
        return parse_json_body(str(value), "Bruno body") if rendered else parse_json_body(value, "manifest request.body")
    if kind in {"form-urlencoded", "multipart-form", "file"}:
        if rendered:
            pairs = {}
            for line in str(value).splitlines():
                match = re.match(r"\s*([^:]+):\s*(.*)$", line)
                if match:
                    pairs[match.group(1).strip()] = match.group(2).strip()
            return pairs
        if isinstance(value, dict):
            pairs = {}
            for key, item in value.items():
                if isinstance(item, dict):
                    item = item.get("file") or item.get("path") or ""
                    item = ("@" if kind == "multipart-form" else "@file(") + str(item) + ("" if kind == "multipart-form" else ")")
                elif kind == "file" and not str(item).startswith("@file("):
                    item = f"@file({item})"
                pairs[str(key)] = str(item)
            return pairs
        if kind == "file":
            item = str(value)
            return {"file": item if item.startswith("@file(") else f"@file({item})"}
    if kind == "graphql" and isinstance(value, dict):
        return str(value.get("query", "")).strip()
    return str(value if value is not None else "").strip()


def case_risk(case: dict[str, Any], endpoint: dict[str, Any] | None = None) -> str:
    declared = str(case.get("risk", "")).strip().lower()
    if declared:
        return declared
    method = str((endpoint or {}).get("method", "GET")).upper()
    return "read-only" if method in {"GET", "HEAD", "OPTIONS"} else "unconfirmed"


def normalized_request_path(url: str) -> str:
    value = re.sub(r"^\{\{[^}]+\}\}", "", url).split("?", 1)[0]
    value = re.sub(r"^https?://[^/]+", "", value, flags=re.IGNORECASE)
    return re.sub(r"\{\{([^}]+)\}\}", r"{\1}", value) or "/"


def case_files(
    cases: list[dict[str, Any]],
    bru_root: Path,
    endpoints: dict[str, dict[str, Any]] | None = None,
) -> tuple[set[str], set[Path], dict[str, Path], list[str]]:
    if not bru_root.is_dir():
        return set(), set(), {}, [f"Bruno directory does not exist: {bru_root}"]
    errors: list[str] = []
    candidates = [path for path in bru_root.rglob("*.bru") if is_business_request(path, bru_root)]
    contents: dict[Path, str] = {}
    known_files: dict[Path, Path] = {}
    for source in candidates:
        path = source.resolve()
        try:
            content = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError) as exc:
            errors = [f"UTF-8 integrity failure {path}: {exc}"]
            return set(), set(), {}, errors
        if is_http_request_content(content):
            known_files[path] = source
            contents[path] = content
    for path, content in contents.items():
        if re.search(r"\burl:\s+https?://(?!\{\{)", content, re.IGNORECASE):
            errors.append(f"Bruno file {path} hard-codes a URL; use the baseUrl environment variable")
        if not re.search(r"\burl:\s*\{\{(?:baseUrl|BASE_URL)\}\}(?:/|\?|\s|$)", content):
            errors.append(f"Bruno file {path} does not use the baseUrl environment variable")
    covered: set[str] = set()
    used_paths: set[Path] = set()
    case_paths: dict[str, Path] = {}
    for position, case in enumerate(cases, 1):
        case_id = str(case.get("id", ""))
        if not case_id:
            errors.append("case without id")
            continue
        configured = case.get("bru") or case.get("bru_file") or case.get("file_name")
        matched = None
        if isinstance(configured, str):
            candidate = (bru_root / configured).resolve()
            try:
                candidate.relative_to(bru_root.resolve())
            except ValueError:
                errors.append(f"case {case_id} points outside Bruno directory: {configured}")
            if candidate in known_files:
                matched = candidate
            else:
                errors.append(f"case {case_id} configured Bruno file does not exist: {configured}")
                continue
        if matched is None:
            marker = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(case_id)}(?![A-Za-z0-9_])")
            candidates = sorted(
                path for path, content in contents.items()
                if marker.search(content) or marker.search(path.stem)
            )
            if len(candidates) > 1:
                errors.append(
                    f"case {case_id} ambiguously matches .bru files: "
                    + ", ".join(str(item) for item in candidates)
                )
            matched = candidates[0] if candidates else None
        if matched is None:
            errors.append(f"case {case_id} has no matching .bru file")
            continue
        if matched in used_paths:
            errors.append(f"case {case_id} reuses Bruno file already mapped to another case: {matched}")
        used_paths.add(matched)
        case_paths[case_id] = matched
        explicit_sequence = case.get("sequence", case.get("seq"))
        try:
            expected_sequence = int(explicit_sequence) if explicit_sequence is not None else None
        except (TypeError, ValueError):
            expected_sequence = -1
        relative_business_path = known_files[matched].relative_to(bru_root)
        if relative_business_path.parent != Path(".") or not valid_business_filename(
            known_files[matched],
            case_id,
            str(case.get("title", "")),
            expected_sequence,
        ):
            errors.append(
                f"case {case_id} Bruno filename must match its sequence and sanitized Chinese case.title: "
                f"{known_files[matched].name}"
            )
        actual_meta_name = bru_meta_name(contents[matched])
        if actual_meta_name != case_id:
            errors.append(
                f"case {case_id} Bruno meta.name is {actual_meta_name or '<missing>'}, expected {case_id}"
            )
        filename_sequence = int(known_files[matched].stem.split("-", 1)[0])
        meta_sequence = bru_meta_sequence(contents[matched])
        if meta_sequence is not None and meta_sequence != filename_sequence:
            errors.append(
                f"case {case_id} Bruno meta.seq is {meta_sequence}, expected filename sequence {filename_sequence}"
            )
        endpoint = (endpoints or {}).get(str(case.get("endpoint_id")))
        if endpoint:
            risk = case_risk(case, endpoint)
            tags = bru_meta_tags(contents[matched])
            if risk not in tags:
                errors.append(f"case {case_id} Bruno meta.tags is missing risk {risk}")
            obsolete = sorted(tag for tag in tags if tag.startswith("plan-"))
            if obsolete:
                errors.append(f"case {case_id} Bruno meta.tags contains obsolete plan tag(s): {', '.join(obsolete)}")
            actual_request = bruno_request(contents[matched])
            expected_method = str(endpoint.get("method", "GET")).upper()
            request = case.get("request") if isinstance(case.get("request"), dict) else {}
            try:
                expected_url = materialized_request_url(endpoint, request)
            except ValueError as exc:
                errors.append(f"case {case_id} query serialization is invalid: {exc}")
                expected_url = ""
            expected_path = normalized_request_path(expected_url) if expected_url else str(request.get("path") or endpoint.get("path") or "/")
            if actual_request.get("method") != expected_method:
                errors.append(f"case {case_id} Bruno method is {actual_request.get('method')}, expected {expected_method}")
            if normalized_request_path(str(actual_request.get("url", ""))) != expected_path:
                errors.append(
                    f"case {case_id} Bruno URL path is {normalized_request_path(str(actual_request.get('url', '')))}, "
                    f"expected {expected_path}"
                )
            actual_url = str(actual_request.get("url", ""))
            actual_query = urllib.parse.parse_qsl(urllib.parse.urlsplit(actual_url).query, keep_blank_values=True)
            expected_query = urllib.parse.parse_qsl(urllib.parse.urlsplit(expected_url).query, keep_blank_values=True) if expected_url else []
            if actual_query != expected_query:
                errors.append(
                    f"case {case_id} Bruno query is {actual_query}, expected {expected_query}"
                )
            expected_headers = request.get("headers") if isinstance(request.get("headers"), dict) else {}
            actual_headers = bruno_headers(contents[matched])
            for key, value in expected_headers.items():
                if actual_headers.get(str(key)) != str(value):
                    errors.append(
                        f"case {case_id} Bruno header {key} is {actual_headers.get(str(key))!r}, expected {str(value)!r}"
                    )
            omitted_headers = request.get("omit_common_headers", [])
            if omitted_headers is not None and (
                not isinstance(omitted_headers, list)
                or any(
                    not isinstance(name, str) or not HEADER_NAME_RE.fullmatch(name.strip())
                    for name in omitted_headers
                )
            ):
                errors.append(f"case {case_id} request.omit_common_headers must be a list of Header names")
            elif omitted_headers:
                if "bru-api-test-generator: omit-common-headers" not in contents[matched]:
                    errors.append(f"case {case_id} does not remove its omitted common Headers at request level")
                for name in omitted_headers:
                    if json.dumps(name, ensure_ascii=False) not in contents[matched]:
                        errors.append(f"case {case_id} does not remove common Header {name}")
            expected_body_type = str(request.get("body_type") or request.get("content_type") or "").lower()
            body = request.get("body")
            endpoint_body = endpoint.get("request_body") if isinstance(endpoint.get("request_body"), dict) else {}
            endpoint_content = endpoint_body.get("content") if isinstance(endpoint_body.get("content"), dict) else {}
            if not expected_body_type and endpoint_content:
                expected_body_type = str(next(iter(endpoint_content))).lower()
            if not expected_body_type and body is not None:
                expected_body_type = "json"
            if expected_body_type:
                aliases = {
                    "application/json": "json",
                    "multipart/form-data": "multipart-form",
                    "application/x-www-form-urlencoded": "form-urlencoded",
                    "text/plain": "text",
                    "application/xml": "xml",
                }
                expected_body_type = aliases.get(expected_body_type, expected_body_type)
                if actual_request.get("body_type") != expected_body_type:
                    errors.append(
                        f"case {case_id} Bruno body type is {actual_request.get('body_type')}, expected {expected_body_type}"
                    )
            actual_kind = str(actual_request.get("body_type", "none"))
            if actual_kind != "none":
                actual_body = bruno_body(contents[matched], actual_kind)
                if actual_body is None:
                    errors.append(f"case {case_id} Bruno body:{actual_kind} block is missing or unterminated")
                else:
                    try:
                        rendered_body = normalized_body(actual_body, actual_kind, True)
                        manifest_body = normalized_body(body, actual_kind)
                    except ValueError as exc:
                        errors.append(f"case {case_id} {exc}")
                    else:
                        if rendered_body != manifest_body:
                            errors.append(f"case {case_id} Bruno body content does not match manifest request")
                        if isinstance(manifest_body, dict) and isinstance(rendered_body, dict):
                            for key in manifest_body:
                                if key not in rendered_body:
                                    errors.append(f"case {case_id} Bruno body is missing request field {key}")
                if actual_kind == "graphql" and isinstance(body, dict) and body.get("variables") is not None:
                    actual_variables = bruno_body(contents[matched], "graphql:vars")
                    if actual_variables is None:
                        errors.append(f"case {case_id} Bruno GraphQL variables block is missing or unterminated")
                    else:
                        try:
                            variables = normalized_body(actual_variables, "json", True)
                        except ValueError as exc:
                            errors.append(f"case {case_id} GraphQL variables {exc}")
                        else:
                            if variables != body.get("variables"):
                                errors.append(f"case {case_id} Bruno GraphQL variables do not match manifest request")
        if "assert {" not in contents[matched]:
            errors.append(f"case {case_id} Bruno file has no assert block: {matched}")
        covered.add(case_id)
        assertions = case.get("assertions")
        if not isinstance(assertions, list) or not any(
            isinstance(item, dict)
            and (
                any(key in item for key in ("equals", "eq", "contains", "matches", "type", "length", "minimum", "maximum", "nullable", "is_null", "equals_variable"))
                or (
                    case.get("review_required") is True
                    and item.get("exists") is True
                    and str(item.get("path", "")) not in {"$.code", "$.status", "$.http_status"}
                )
            )
            for item in assertions
        ):
            errors.append(f"case {case_id} has no precise response assertion beyond status/code/exists")
        for assertion in assertions if isinstance(assertions, list) else []:
            if not isinstance(assertion, dict):
                continue
            expected_paths = bruno_assertion_paths(assertion)
            if expected_paths and not any(path_expr in contents[matched] for path_expr in expected_paths):
                errors.append(
                    f"case {case_id} manifest assertion {assertion.get('path')} "
                    "is not represented in its Bruno assert block"
                )
                continue
            for requirement, alternatives in assertion_requirements(assertion):
                if not any(fragment in contents[matched] for fragment in alternatives):
                    errors.append(
                        f"case {case_id} manifest assertion {assertion.get('path')} "
                        f"is missing {requirement} in Bruno"
                    )
        captures = case.get("captures", [])
        if isinstance(captures, dict):
            captures = [{"name": name, "path": path} for name, path in captures.items()]
        for capture in captures if isinstance(captures, list) else []:
            if not isinstance(capture, dict) or not capture.get("name"):
                continue
            expressions = bruno_assertion_paths({"path": capture.get("path", "$")})
            name = json.dumps(str(capture["name"]))
            if not any(f"bru.setVar({name}, {expression})" in contents[matched] for expression in expressions):
                errors.append(f"case {case_id} capture {capture['name']} is not represented in Bruno")
        expected = case.get("expected") if isinstance(case.get("expected"), dict) else {}
        business_code_path = expected.get("business_code_path", "$.code")
        business_expression = next(iter(bruno_assertion_paths({"path": business_code_path})), "res.body.code")
        for field, expression in (("http_status", "res.status"), ("business_code", business_expression)):
            if field not in expected or expected[field] is None:
                continue
            literal = re.escape(str(expected[field]))
            serialized = re.escape(json.dumps(expected[field], ensure_ascii=False, separators=(",", ":")))
            expected_value = rf"(?:{literal}|{serialized}|\"{literal}\"|'{literal}')"
            if not re.search(rf"(?m)^\s*{re.escape(expression)}\s*:\s*eq\s+{expected_value}\s*$", contents[matched]):
                errors.append(
                    f"case {case_id} expected {field}={expected[field]} is not asserted exactly in Bruno"
                )
    errors.extend(f"unregistered Bruno file: {path}" for path in sorted(set(known_files) - used_paths))
    return covered, used_paths, case_paths, errors


def collection_runtime_errors(bru_root: Path, request_roots: list[Path] | None = None) -> list[str]:
    if not bru_root.is_dir():
        return [f"Bruno directory does not exist: {bru_root}"]
    errors: list[str] = []
    collection_path = bru_root / "collection.bru"
    if not collection_path.is_file():
        errors.append(f"Bruno collection runtime config does not exist: {collection_path}")
    else:
        try:
            collection = collection_path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"UTF-8 integrity failure {collection_path}: {exc}")
        else:
            for marker in (
                COLLECTION_MARKER,
                COLLECTION_END_MARKER,
                "__QA_EXECUTION_CONFIG",
                "req.getHeader",
                "req.setHeader",
                "req.getPath",
                'value === ""',
                "runtimeConfig.headers",
                "CryptoJS.SHA256(signText)",
                'setCommonHeader("sign"',
                'setCommonHeader("timestamp"',
                'setCommonHeader("accesskey"',
            ):
                if marker not in collection:
                    errors.append(f"Bruno collection runtime config is incomplete; missing {marker}")
    roots = request_roots or [bru_root]
    for path in (path for root in roots if root.is_dir() for path in root.rglob("*.bru")):
        if not is_business_request(path, bru_root):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"UTF-8 integrity failure {path}: {exc}")
            continue
        if not is_http_request_content(content):
            continue
        if "bru-api-test-generator: auth-start" in content:
            errors.append(f"Bruno file {path} still contains a legacy per-request authentication script")
    return errors


def manifest_cases(document: Any) -> list[dict[str, Any]]:
    cases = list_at(document, "cases")
    if cases:
        return cases
    return document if isinstance(document, list) else []


def execution_evidence(document: Any) -> dict[str, Any]:
    """Accept normalized evidence or a Bruno JSON report.

    Bruno's top-level result status can remain ``pass`` when an assertion
    failed, so raw reports are considered passed only when every assertion and
    test result is explicitly passed.
    """

    if isinstance(document, dict) and isinstance(document.get("executed"), list):
        executed = [str(item) for item in document.get("executed", []) if str(item)]
        passed = [str(item) for item in document.get("passed", []) if str(item)]
        return {
            **document,
            "executed": list(dict.fromkeys(executed)),
            "passed": list(dict.fromkeys(item for item in passed if item in executed)),
        }
    if isinstance(document, dict):
        reports = [document]
    else:
        reports = document if isinstance(document, list) else []
    items: list[dict[str, Any]] = []
    for report in reports:
        if isinstance(report, dict) and isinstance(report.get("results"), list):
            items.extend(item for item in report["results"] if isinstance(item, dict))
    executed: list[str] = []
    passed: list[str] = []
    for item in items:
        test = item.get("test") if isinstance(item.get("test"), dict) else {}
        filename = str(test.get("filename", ""))
        name = str(item.get("name") or (Path(filename).stem if filename else ""))
        if not name:
            continue
        executed.append(name)
        assertions = item.get("assertionResults")
        tests = item.get("testResults")
        assertion_ok = isinstance(assertions, list) and all(
            isinstance(value, dict) and str(value.get("status", "")).lower() in {"pass", "passed", "success"}
            for value in assertions
        )
        test_ok = isinstance(tests, list) and all(
            isinstance(value, dict) and str(value.get("status", "")).lower() in {"pass", "passed", "success"}
            for value in tests
        )
        has_observations = bool(assertions or tests)
        if (
            str(item.get("status", "")).lower() in {"pass", "passed", "success"}
            and not item.get("error")
            and has_observations
            and assertion_ok
            and test_ok
        ):
            passed.append(name)
    return {
        "executed": list(dict.fromkeys(executed)),
        "passed": list(dict.fromkeys(passed)),
    }


def check_module(
    module_id: str,
    module_dir: Path,
    bru_root: Path,
    results: dict[str, Any] | None,
    require_scenarios: bool = False,
    module_tag: str | None = None,
    strict_bru_modules: bool = False,
    bru_module_name: str | None = None,
    selected_case_ids: set[str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    endpoints_doc = load_data(module_dir / "endpoints.yaml")
    cases_doc = load_data(module_dir / "cases.yaml") if (module_dir / "cases.yaml").exists() else endpoints_doc
    logic_doc = load_data(module_dir / "logic.yaml") if (module_dir / "logic.yaml").exists() else {}
    flows_doc = load_data(module_dir / "flows.yaml") if (module_dir / "flows.yaml").exists() else {}
    exclusions_doc = load_data(module_dir / "exclusions.yaml") if (module_dir / "exclusions.yaml").exists() else {}

    endpoints = first_list(endpoints_doc, "endpoints")
    cases = manifest_cases(cases_doc)
    exclusions = list_at(endpoints_doc, "exclusions") + list_at(exclusions_doc, "exclusions")
    excluded_endpoint_ids = {
        str(item.get("endpoint_id"))
        for item in exclusions
        if item.get("endpoint_id") and str(item.get("reason", "")).strip()
    }
    approved_excluded_endpoint_ids = {
        str(item.get("endpoint_id"))
        for item in exclusions
        if item.get("endpoint_id") and is_approved_exclusion(item)
    }
    pending_excluded_endpoint_ids = excluded_endpoint_ids - approved_excluded_endpoint_ids
    endpoint_ids = ids(endpoints)
    case_ids = ids(cases)
    declared_module_tags = {
        str(value).strip()
        for document in (endpoints_doc, cases_doc, logic_doc, flows_doc, exclusions_doc)
        for value in ([document.get("swagger_tag")] if isinstance(document, dict) and document.get("swagger_tag") else [])
        if str(value).strip()
    }
    if module_tag and declared_module_tags and declared_module_tags != {module_tag}:
        errors.append(f"module {module_id} artifacts disagree on Swagger tag: {sorted(declared_module_tags)}")
    if module_tag and not declared_module_tags:
        errors.append(f"module {module_id} does not declare its Swagger tag")
    if not endpoint_ids:
        errors.append("endpoints.yaml has no endpoints")
    if not case_ids and endpoint_ids - approved_excluded_endpoint_ids:
        errors.append("cases.yaml has no cases")
    errors.extend(validate_module_documentation(module_dir / "CASES.md", cases))
    if module_tag:
        errors.extend(
            validate_module_artifact(
                module_dir / "parameters.yaml",
                module_id,
                module_tag,
                endpoint_ids,
                "parameters",
            )
        )
        parameter_doc = load_data(module_dir / "parameters.yaml") if (module_dir / "parameters.yaml").is_file() else {}
        definition_doc = load_data(module_dir / "definitions.yaml") if (module_dir / "definitions.yaml").is_file() else {}
        response_doc = load_data(module_dir / "responses.yaml") if (module_dir / "responses.yaml").is_file() else {}
        local_components_by_kind = {
            "schemas": set(definition_doc.get("definitions", {}).keys()) if isinstance(definition_doc, dict) and isinstance(definition_doc.get("definitions"), dict) else set(),
            "parameters": set(parameter_doc.get("definitions", {}).keys()) if isinstance(parameter_doc, dict) and isinstance(parameter_doc.get("definitions"), dict) else set(),
            "responses": set(response_doc.get("definitions", {}).keys()) if isinstance(response_doc, dict) and isinstance(response_doc.get("definitions"), dict) else set(),
        }
        for endpoint in endpoints:
            for kind, name in referenced_components(endpoint).copy():
                if name not in local_components_by_kind.get(kind, set()):
                    errors.append(f"endpoint {endpoint.get('id')} references non-local {kind} definition {name}")
        errors.extend(
            validate_module_artifact(
                module_dir / "responses.yaml",
                module_id,
                module_tag,
                endpoint_ids,
                "responses",
            )
        )
        errors.extend(
            validate_module_artifact(
                module_dir / "definitions.yaml",
                module_id,
                module_tag,
                endpoint_ids,
                "definitions",
            )
        )

    cases_by_id = {str(case["id"]): case for case in cases if case.get("id")}
    obligation_ids = {
        str(obligation.get("id"))
        for endpoint in endpoints
        for obligation in endpoint.get("obligations", [])
        if isinstance(obligation, dict) and obligation.get("id")
    }
    covered_obligation_ids = {
        str(coverage_id)
        for case in cases
        for coverage_id in (case.get("coverage_ids", []) if isinstance(case.get("coverage_ids"), list) else [])
    }
    excluded_obligation_ids = {
        str(coverage_id)
        for exclusion in exclusions
        if is_approved_exclusion(exclusion)
        for coverage_id in (
            exclusion.get("coverage_ids", [])
            if isinstance(exclusion.get("coverage_ids"), list)
            else [exclusion.get("coverage_id")]
        )
        if coverage_id
    }
    errors.extend(
        f"constraint obligation {obligation_id} has no case or approved exclusion"
        for obligation_id in sorted(obligation_ids - covered_obligation_ids - excluded_obligation_ids)
    )
    errors.extend(
        f"case or exclusion references unknown constraint obligation {obligation_id}"
        for obligation_id in sorted((covered_obligation_ids | excluded_obligation_ids) - obligation_ids)
    )
    fingerprint_ids: dict[str, str] = {}
    duplicate_fingerprints: set[str] = set()
    for case in cases:
        case_id = str(case.get("id", ""))
        if not case_id:
            continue
        fingerprint = case_fingerprint(case)
        previous = fingerprint_ids.get(fingerprint)
        if previous and previous != case_id and fingerprint not in duplicate_fingerprints:
            errors.append(f"cases {previous} and {case_id} duplicate endpoint/scenario/request/assertions")
            duplicate_fingerprints.add(fingerprint)
        fingerprint_ids[fingerprint] = case_id
    for case in cases:
        if not str(case.get("endpoint_id", "")).strip():
            errors.append(f"case {case.get('id')} has no endpoint_id")
        title = str(case.get("title", "")).strip()
        if not title or not CHINESE_RE.search(title):
            errors.append(f"case {case.get('id')} must declare a Chinese business title")
        elif title in GENERIC_CASE_TITLES:
            errors.append(f"case {case.get('id')} uses generic title {title!r}; include the endpoint business action")
        expected = case.get("expected") if isinstance(case.get("expected"), dict) else {}
        if expected.get("http_status") is None:
            errors.append(f"case {case.get('id')} has no expected.http_status")
        endpoint_id = case.get("endpoint_id")
        if endpoint_id and endpoint_id not in endpoint_ids:
            errors.append(f"case {case.get('id')} points to unknown endpoint {endpoint_id}")
        if module_tag and case.get("swagger_tag") and str(case.get("swagger_tag")).strip() != module_tag:
            errors.append(f"case {case.get('id')} Swagger tag does not match module {module_id}")
        endpoint_for_case = next((item for item in endpoints if item.get("id") == endpoint_id), None)
        risk = case_risk(case, endpoint_for_case)
        if require_scenarios and not str(case.get("risk", "")).strip():
            errors.append(f"case {case.get('id')} has no confirmed risk class")
        if risk not in RISK_CLASSES:
            errors.append(f"case {case.get('id')} has invalid risk class {risk}")
        if require_scenarios:
            errors.extend(case_completion_errors(case))
            errors.extend(success_assertion_errors(case))
            errors.extend(query_assertion_errors(case))
    for endpoint in endpoints:
        endpoint_cases = endpoint.get("case_ids", [])
        if not endpoint_cases:
            endpoint_cases = [item.get("id") for item in list_at(endpoint, "cases")]
        if not endpoint_cases and str(endpoint.get("id")) not in approved_excluded_endpoint_ids:
            errors.append(f"endpoint {endpoint.get('id')} has no case_ids")
        for case_id in endpoint_cases:
            case = cases_by_id.get(str(case_id))
            if case is None:
                errors.append(f"endpoint {endpoint.get('id')} references unknown case {case_id}")
            elif case.get("endpoint_id") and case.get("endpoint_id") != endpoint.get("id"):
                errors.append(f"case {case_id} points to endpoint {case.get('endpoint_id')}, not {endpoint.get('id')}")

        endpoint_case_objects = [
            cases_by_id[str(case_id)]
            for case_id in endpoint_cases
            if str(case_id) in cases_by_id
        ]
        if require_scenarios and str(endpoint.get("id")) not in approved_excluded_endpoint_ids:
            endpoint_decisions = endpoint.get("scenario_matrix") or endpoint.get("scenarios")
            for category in SCENARIO_CATEGORIES:
                decisions: list[dict[str, Any]] = []
                if isinstance(endpoint_decisions, dict) and isinstance(endpoint_decisions.get(category), dict):
                    decisions.append(endpoint_decisions[category])
                for case in endpoint_case_objects:
                    scenarios = case.get("scenarios")
                    if isinstance(scenarios, dict) and isinstance(scenarios.get(category), dict):
                        decisions.append(scenarios[category])
                if not decisions:
                    errors.append(f"endpoint {endpoint.get('id')} has no scenario decision for {category}")
                    continue
                applicable = [item for item in decisions if item.get("applicable") is True]
                inapplicable = [item for item in decisions if item.get("applicable") is False]
                if not applicable and not inapplicable:
                    errors.append(f"endpoint {endpoint.get('id')} has invalid scenario decision for {category}")
                for decision in decisions:
                    if decision.get("status") not in {"inferred", "confirmed"}:
                        errors.append(
                            f"endpoint {endpoint.get('id')} scenario {category} must have status inferred or confirmed"
                        )
                if inapplicable and any(not str(item.get("reason", "")).strip() for item in inapplicable):
                    errors.append(f"endpoint {endpoint.get('id')} scenario {category}=false has no reason")
                if applicable and not any(case_covers_scenario(case, category) for case in endpoint_case_objects):
                    approved_scenario_exclusion = any(
                        is_approved_exclusion(item)
                        and str(item.get("endpoint_id")) == str(endpoint.get("id"))
                        and str(item.get("scenario")) == category
                        for item in exclusions
                    )
                    if not approved_scenario_exclusion:
                        errors.append(
                            f"endpoint {endpoint.get('id')} scenario {category}=true has no dedicated linked case or approved exclusion"
                        )

        success_decision = None
        endpoint_decisions = endpoint.get("scenario_matrix") or endpoint.get("scenarios")
        if isinstance(endpoint_decisions, dict) and isinstance(endpoint_decisions.get("success"), dict):
            success_decision = endpoint_decisions["success"]
        success_cases = [case for case in endpoint_case_objects if case_covers_scenario(case, "success")]
        if require_scenarios and (
            str(endpoint.get("id")) not in approved_excluded_endpoint_ids
            and (success_decision is None or success_decision.get("applicable") is not False)
            and not success_cases
        ):
            errors.append(f"endpoint {endpoint.get('id')} has no success case")

    bruno_name = bru_module_name or module_id
    module_bru_root = bru_root / bruno_name if strict_bru_modules else (
        bru_root / bruno_name if (bru_root / bruno_name).is_dir() else bru_root
    )
    covered_cases, _, case_paths, file_errors = case_files(
        cases,
        module_bru_root,
        {str(item.get("id")): item for item in endpoints},
    )
    errors.extend(file_errors)

    # A query parameter whose schema is an object must be represented as the
    # framework expects (usually flattened fields), not as one stringified
    # variable. The latter is a common source of false 400s in generated tests.
    endpoints_by_id = {str(item.get("id")): item for item in endpoints}
    for case in cases:
        case_id = str(case.get("id", ""))
        endpoint = endpoints_by_id.get(str(case.get("endpoint_id")))
        path = case_paths.get(case_id)
        if not endpoint or not path:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"UTF-8 integrity failure {path}: {exc}")
            continue
        for parameter in endpoint.get("parameters", []):
            if not isinstance(parameter, dict) or parameter.get("in") != "query":
                continue
            schema = parameter.get("schema")
            if not isinstance(schema, dict) or not (schema.get("$ref") or schema.get("properties")):
                continue
            name = re.escape(str(parameter.get("name", "")))
            if name and re.search(rf"[?&]{name}=\{{\{{[^}}]+\}}\}}", content):
                errors.append(
                    f"case {case_id} stringifies object query parameter {parameter.get('name')}; "
                    "flatten fields or document supported serialization"
                )

    logic_items = first_list(logic_doc, "logic") or (logic_doc if isinstance(logic_doc, list) else [])
    for logic in logic_items:
        if module_tag and logic.get("swagger_tag") and str(logic.get("swagger_tag")).strip() != module_tag:
            errors.append(f"logic {logic.get('id')} Swagger tag does not match module {module_id}")
        if not logic.get("case_ids"):
            errors.append(f"logic {logic.get('id')} has no linked case_ids")
        if not str(logic.get("source_symbol") or logic.get("source") or "").strip():
            errors.append(f"logic {logic.get('id')} has no source_symbol")
        if not str(logic.get("condition", "")).strip():
            errors.append(f"logic {logic.get('id')} has no observable condition")
        for case_id in logic.get("case_ids", []):
            if case_id not in case_ids:
                errors.append(f"logic {logic.get('id')} references unknown case {case_id}")
        expected_business_code = logic.get("expected_business_code")
        if expected_business_code is not None and not any(
            str((cases_by_id.get(str(case_id), {}).get("expected") or {}).get("business_code"))
            == str(expected_business_code)
            for case_id in logic.get("case_ids", [])
        ):
            errors.append(
                f"logic {logic.get('id')} business code {expected_business_code} has no matching case expectation"
            )

    flow_items = first_list(flows_doc, "flows") or (flows_doc if isinstance(flows_doc, list) else [])
    requires_ordered_flow = flow_required(endpoints_doc, endpoints)
    flow_exclusions = [
        item for item in exclusions
        if str(item.get("kind", item.get("type", ""))).lower() in {"flow", "cleanup"}
        or item.get("flow_id")
    ]
    approved_flow_exclusion = any(
        is_approved_exclusion(item) and str(item.get("cleanup_plan", item.get("reset_procedure", ""))).strip()
        for item in flow_exclusions
    )
    if not flow_items and requires_ordered_flow:
        if not flow_exclusions:
            errors.append("module declares flow_required operations but flows.yaml declares no flow or exclusion")
        elif any(is_approved_exclusion(item) for item in flow_exclusions) and not approved_flow_exclusion:
            errors.append("approved flow exclusion must include a cleanup/reset plan")
    for flow in flow_items:
        captures: set[str] = set()
        seen_steps: set[str] = set()
        for index, step in enumerate(flow.get("steps", []), start=1):
            case_id = step.get("case_id")
            if case_id in seen_steps:
                errors.append(f"flow {flow.get('id')} repeats case {case_id} at step {index}")
            seen_steps.add(str(case_id))
            if case_id not in case_ids:
                errors.append(f"flow {flow.get('id')} step {index} references unknown case {case_id}")
            uses = [step["uses"]] if isinstance(step.get("uses"), str) else step.get("uses", [])
            for used in uses:
                if used not in captures:
                    errors.append(f"flow {flow.get('id')} step {index} uses uncaptured value {used}")
            captured = step.get("capture")
            captures.update([captured] if isinstance(captured, str) else [str(item) for item in captured or []])

    for exclusion in exclusions:
        endpoint_id = exclusion.get("endpoint_id")
        if endpoint_id and str(endpoint_id) not in endpoint_ids:
            errors.append(f"exclusion references unknown endpoint {endpoint_id}")
    for exclusion in exclusions:
        if not str(exclusion.get("reason", "")).strip():
            errors.append(f"exclusion {exclusion.get('method')} {exclusion.get('path')} has no reason")

    if results is not None:
        executed = set(results.get("executed", []))
        passed = set(results.get("passed", executed))
        required_case_ids = {
            case_id for case_id, case in cases_by_id.items()
            if str(case.get("endpoint_id")) not in approved_excluded_endpoint_ids
            and (selected_case_ids is None or case_id in selected_case_ids)
        }
        errors.extend(f"case {case_id} was not executed" for case_id in sorted(required_case_ids - executed))
        errors.extend(f"case {case_id} failed" for case_id in sorted(required_case_ids & executed - passed))
        selected_flows = [
            flow for flow in flow_items
            if all(
                step.get("case_id") in cases_by_id
                and (selected_case_ids is None or step.get("case_id") in selected_case_ids)
                for step in flow.get("steps", [])
                if isinstance(step, dict)
            )
        ]
        if selected_flows:
            errors.extend(flow_execution_errors(selected_flows, results))

    endpoints_with_cases = 0
    for endpoint in endpoints:
        endpoint_case_ids = [
            case_id for case_id in (endpoint.get("case_ids") or [item.get("id") for item in list_at(endpoint, "cases")])
            if str(case_id) in cases_by_id
        ]
        if endpoint_case_ids or any(
            str(case.get("endpoint_id")) == str(endpoint.get("id")) for case in cases
        ):
            endpoints_with_cases += 1
    happy_path_endpoints = sum(
        any(
            case_covers_scenario(case, "success")
            for case in cases
            if str(case.get("endpoint_id")) == str(endpoint.get("id"))
        )
        for endpoint in endpoints
        if str(endpoint.get("id")) not in approved_excluded_endpoint_ids
    )
    required_success_endpoints = sum(
        str(endpoint.get("id")) not in approved_excluded_endpoint_ids for endpoint in endpoints
    )
    success_case_count = sum(
        1
        for case in cases
        if case_covers_scenario(case, "success")
    )
    verified_flows = 0
    if results and isinstance(results.get("flows"), dict):
        verified_flows = sum(
            isinstance(value, dict) and value.get("status") == "passed"
            for value in results["flows"].values()
        )
    return {
        "endpoints": len(endpoint_ids),
        "inventory_endpoints": len(endpoint_ids),
        "endpoints_with_cases": endpoints_with_cases,
        "excluded_endpoints": len(excluded_endpoint_ids),
        "pending_exclusions": len(pending_excluded_endpoint_ids),
        "generated_cases": len(case_ids),
        "cases": len(case_ids),
        "bru_cases": len(covered_cases),
        "executed_cases": len(set(results.get("executed", []))) if results else 0,
        "passed_cases": len(set(results.get("passed", []))) if results else 0,
        "happy_path_endpoints": happy_path_endpoints,
        "pending_success_endpoints": max(required_success_endpoints - happy_path_endpoints, 0),
        "verified_flows": verified_flows,
        "registered_cases": len(case_ids),
        "successful_cases": len(
            {
                case_id
                for case_id in (set(results.get("passed", [])) if results else set())
                if case_id in case_ids
            }
        ),
        "success_cases": success_case_count,
        "swagger_tag": module_tag,
    }, errors


def sync_global_index(path: Path, status: str, totals: dict[str, int], module_reports: dict[str, dict[str, Any]]) -> None:
    """Keep the generated index counts and state aligned with reconciliation."""

    if not path.is_file():
        return
    index = load_data(path)
    if not isinstance(index, dict):
        return
    index["generation_status"] = status
    index["inventory_endpoints"] = totals.get("inventory_endpoints", 0)
    index["generated_cases"] = totals.get("generated_cases", 0)
    index["blocked_modules"] = sum(
        value.get("status") == "blocked" for value in module_reports.values()
    )
    by_id = {
        str(item.get("id")): item
        for item in index.get("modules", [])
        if isinstance(item, dict) and item.get("id")
    }
    for module_id, report in module_reports.items():
        entry = by_id.get(module_id)
        if entry is None:
            continue
        entry["endpoint_count"] = report.get("endpoints", 0)
        entry["case_count"] = report.get("cases", 0)
    try:
        import yaml  # type: ignore[import-not-found]

        rendered = yaml.safe_dump(index, allow_unicode=True, sort_keys=False)
    except ModuleNotFoundError:
        rendered = json.dumps(index, ensure_ascii=True, indent=2) + "\n"
    path.write_text(rendered, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("contracts_root", type=Path)
    parser.add_argument("bru_root", type=Path)
    parser.add_argument("--results", type=Path, help="normalized evidence or raw Bruno JSON execution report")
    parser.add_argument(
        "--preflight-results",
        type=Path,
        help="normalized runtime preflight report; a passed report marks the collection runnable",
    )
    parser.add_argument(
        "--openapi",
        type=Path,
        help="offline OpenAPI/Swagger document (defaults to contracts_root/openapi.json when present; required for completion)",
    )
    parser.add_argument(
        "--require-scenarios",
        action="store_true",
        help="require a decision for every case-matrix category on every endpoint",
    )
    parser.add_argument(
        "--require-auth",
        action="store_true",
        help="require valid execution config and collection-level runtime injection",
    )
    parser.add_argument(
        "--execution-config",
        type=Path,
        help="qa/execution/config.yaml (the only shared runtime config)",
    )
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all", action="store_true", help="check all modules and all registered cases")
    scope.add_argument("--module", help="check one module id, display name, Tag, or directory")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--write-status", action="store_true", help="update generated counts/status in index.yaml")
    args = parser.parse_args()

    results = execution_evidence(load_data(args.results)) if args.results else None
    preflight = load_data(args.preflight_results) if args.preflight_results else None
    preflight_ok = (
        isinstance(preflight, dict)
        and preflight.get("version") == 2
        and preflight.get("check_profile") == "full-matrix-strict"
        and preflight.get("static_report_version") == STRICT_REPORT_VERSION
        and preflight.get("static_ready") is True
        and preflight.get("context_ready") is True
        and preflight.get("execution_ready") is True
        and preflight.get("status") == "runnable"
        and not preflight.get("errors")
    )
    execution_config_path = args.execution_config or (args.contracts_root.parent / "execution" / "config.yaml")
    execution_config = None
    execution_config_error: str | None = None
    available_variables: set[str] = set()
    environment_errors: list[str] = []
    environment_headers: dict[str, str] = {}
    if execution_config_path.is_file():
        try:
            execution_config = load_execution_config(execution_config_path)
            environment = load_bruno_environment_document(environment_file(execution_config_path, execution_config))
            available_variables = {
                name for name, value in environment["vars"].items() if str(value).strip()
            }
            environment_headers = environment["headers"]
            for name in sorted(available_variables & {
                "UPLOAD_FILE", "EMPTY_UPLOAD_FILE", "INVALID_EXTENSION_FILE",
                "INVALID_MIME_FILE", "OVERSIZED_UPLOAD_FILE",
            }):
                if not Path(environment["vars"][name]).is_file():
                    environment_errors.append(f"environment fixture variable {name} is not a file: {environment['vars'][name]}")
        except ValueError as exc:
            execution_config_error = str(exc)
    errors: list[str] = text_integrity_errors(args.contracts_root, args.bru_root)
    warnings: list[str] = []
    qa_lock_errors: list[str] = []
    business_version_checked = False
    if args.require_scenarios:
        qa_lock_errors = check_qa_lock(args.contracts_root)
        errors.extend(f"QA lock is not current: {error}" for error in qa_lock_errors)
        business_version_checked = True
        version_check = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve().parent / "check_version_compatibility.py"),
                str(args.contracts_root.parent.parent),
                str(args.contracts_root),
                "--phase", "before-execute",
            ],
            check=False, capture_output=True, text=True, encoding="utf-8",
        )
        if version_check.returncode:
            detail = (version_check.stdout or version_check.stderr).strip()
            warnings.append(f"business version lock is not current: {detail or version_check.returncode}")
    if args.results:
        errors.extend(check_qa_lock(args.contracts_root))
        if args.openapi is None:
            errors.append("completion requires explicit --openapi")
        if not args.require_scenarios:
            errors.append("completion requires --require-scenarios")
        if not args.require_auth:
            errors.append("completion requires --require-auth")
        if args.execution_config is None:
            errors.append("completion requires explicit --execution-config")
        if not args.preflight_results:
            errors.append("completion requires --preflight-results")
        elif not preflight_ok:
            errors.append("runtime preflight did not pass")
        if not execution_config_path.is_file():
            errors.append(f"completion requires execution config: {execution_config_path}")
    if args.require_auth:
        if not execution_config_path.is_file():
            errors.append(f"--require-auth requires execution config: {execution_config_path}")
        elif execution_config_error:
            errors.append(f"invalid execution config {execution_config_path}: {execution_config_error}")
        elif execution_config is None:
            errors.append(f"invalid execution config {execution_config_path}")
        errors.extend(environment_errors)
    totals = {
        "endpoints": 0,
        "inventory_endpoints": 0,
        "endpoints_with_cases": 0,
        "excluded_endpoints": 0,
        "pending_exclusions": 0,
        "generated_cases": 0,
        "cases": 0,
        "registered_cases": 0,
        "bru_cases": 0,
        "executed_cases": 0,
        "passed_cases": 0,
        "successful_cases": 0,
        "success_cases": 0,
        "happy_path_endpoints": 0,
        "pending_success_endpoints": 0,
        "verified_flows": 0,
    }
    declared: dict[str, dict[str, Any]] = {}
    module_map_path = args.contracts_root / "module-map.yaml"
    module_map_doc: Any = load_data(module_map_path) if module_map_path.is_file() else None
    modules = module_dirs(args.contracts_root, module_map_doc)
    if args.module:
        metadata = {
            str(item.get("id")): item
            for item in (module_map_doc.get("modules", []) if isinstance(module_map_doc, dict) else [])
            if isinstance(item, dict) and item.get("id")
        }
        modules = [
            (module_id, module_dir)
            for module_id, module_dir in modules
            if args.module in {
                module_id,
                module_dir.name,
                str(metadata.get(module_id, {}).get("name", "")),
                str(metadata.get(module_id, {}).get("tag", "")),
                *[str(tag) for tag in metadata.get(module_id, {}).get("swagger_tags", [])],
            }
        ]
        if not modules:
            errors.append(f"unknown module: {args.module}")
    if args.require_auth:
        roots = [args.bru_root / module_dir.name for _, module_dir in modules] if args.module else None
        errors.extend(collection_runtime_errors(args.bru_root, roots))
    endpoint_records: list[tuple[str, dict[str, Any]]] = []
    all_case_ids: dict[str, str] = {}
    all_case_fingerprints: dict[str, str] = {}
    required_case_ids_global: set[str] = set()
    selected_case_ids_by_module: dict[str, set[str]] = {}
    all_flow_ids: dict[str, str] = {}
    all_logic_ids: dict[str, str] = {}
    source_candidate_links: set[str] = set()
    offline_inventory_count: int | None = None
    contract_provenance_unverified = False
    global_captured_variables: set[str] = set()
    for _, directory in modules:
        case_document = load_data(directory / "cases.yaml") if (directory / "cases.yaml").is_file() else {}
        for case in manifest_cases(case_document):
            captures = case.get("captures", [])
            if isinstance(captures, dict):
                global_captured_variables.update(str(name) for name in captures)
            elif isinstance(captures, list):
                global_captured_variables.update(
                    str(item.get("name")) for item in captures if isinstance(item, dict) and item.get("name")
                )
            global_captured_variables.update(
                str(item.get("capture_as") or item.get("capture"))
                for item in (case.get("assertions", []) if isinstance(case.get("assertions"), list) else [])
                if isinstance(item, dict) and (item.get("capture_as") or item.get("capture"))
            )
        flow_document = load_data(directory / "flows.yaml") if (directory / "flows.yaml").is_file() else {}
        global_captured_variables.update(
            str(value)
            for flow in first_list(flow_document, "flows")
            for step in (flow.get("steps", []) if isinstance(flow.get("steps"), list) else [])
            if isinstance(step, dict)
            for value in ([step.get("capture")] if isinstance(step.get("capture"), str) else step.get("capture", []) or [])
            if value
        )
    for module_id, module_dir in modules:
        endpoint_doc = load_data(module_dir / "endpoints.yaml")
        endpoint_lookup = {str(item.get("id")): item for item in first_list(endpoint_doc, "endpoints")}
        if args.require_auth:
            required_headers = {
                str(parameter.get("name"))
                for endpoint in endpoint_lookup.values()
                for parameter in endpoint.get("parameters", [])
                if isinstance(parameter, dict)
                and parameter.get("in") == "header"
                and parameter.get("required") is True
            }
            if any(endpoint.get("security") for endpoint in endpoint_lookup.values()):
                required_headers.add("Authorization")
            for header in sorted(required_headers):
                template = environment_headers.get(header)
                if not template:
                    errors.append(f"required environment Header is missing: {header}")
                    continue
                missing = sorted(_variables(template) - available_variables)
                if missing:
                    errors.append(
                        f"required environment Header {header} references undefined or empty variable(s): {', '.join(missing)}"
                    )
        for endpoint in endpoint_lookup.values():
            if endpoint.get("module") and str(endpoint.get("module")) != module_id:
                errors.append(
                    f"endpoint {endpoint.get('id')} declares module {endpoint.get('module')} "
                    f"but is stored under {module_id}"
                )
            endpoint_records.append((module_id, endpoint))
        case_path = module_dir / "cases.yaml"
        if case_path.is_file():
            module_cases = manifest_cases(load_data(case_path))
        else:
            module_cases = manifest_cases(endpoint_doc)
        if module_cases:
            module_exclusions = list_at(endpoint_doc, "exclusions")
            exclusions_path = module_dir / "exclusions.yaml"
            if exclusions_path.is_file():
                module_exclusions += list_at(load_data(exclusions_path), "exclusions")
            approved_endpoint_ids = {
                str(item.get("endpoint_id"))
                for item in module_exclusions
                if item.get("endpoint_id") and is_approved_exclusion(item)
            }
            eligible_case_ids = [
                str(case.get("id"))
                for case in module_cases
                if case.get("id")
                and str(case.get("endpoint_id")) not in approved_endpoint_ids
            ]
            selected_case_ids_by_module[module_id] = set(eligible_case_ids)
            required_case_ids_global.update(eligible_case_ids)
            captured_variables = {
                str(capture.get("name"))
                for case in module_cases
                for capture in (
                    case.get("captures", [])
                    if isinstance(case.get("captures"), list)
                    else [
                        {"name": name}
                        for name in case.get("captures", {})
                    ] if isinstance(case.get("captures"), dict) else []
                )
                if isinstance(capture, dict) and capture.get("name")
            }
            captured_variables.update(
                str(assertion.get("capture_as") or assertion.get("capture"))
                for case in module_cases
                for assertion in (case.get("assertions", []) if isinstance(case.get("assertions"), list) else [])
                if isinstance(assertion, dict) and (assertion.get("capture_as") or assertion.get("capture"))
            )
            captured_variables.update(global_captured_variables)
            for case in module_cases:
                case_id = str(case.get("id", ""))
                if not case_id:
                    continue
                previous = all_case_ids.get(case_id)
                if previous:
                    errors.append(f"case id {case_id} is duplicated in modules {previous} and {module_id}")
                all_case_ids[case_id] = module_id
                fingerprint = case_fingerprint(case)
                previous_fingerprint = all_case_fingerprints.get(fingerprint)
                if previous_fingerprint and previous_fingerprint != case_id:
                    errors.append(
                        f"cases {previous_fingerprint} and {case_id} duplicate endpoint/scenario/request/assertions"
                    )
                all_case_fingerprints[fingerprint] = case_id
                if args.require_scenarios:
                    endpoint = endpoint_lookup.get(str(case.get("endpoint_id")), {})
                    checked_case = dict(case)
                    checked_case["resolved_endpoint_path"] = endpoint.get("path", "")
                    errors.extend(case_context_errors(checked_case, available_variables, captured_variables))
                if args.results and case_id in selected_case_ids_by_module[module_id]:
                    errors.extend(case_completion_errors(case))
        flow_path = module_dir / "flows.yaml"
        if flow_path.is_file():
            flow_doc = load_data(flow_path)
            for flow in first_list(flow_doc, "flows"):
                flow_id = str(flow.get("id", ""))
                if flow_id and flow_id in all_flow_ids:
                    errors.append(f"flow id {flow_id} is duplicated in modules {all_flow_ids[flow_id]} and {module_id}")
                if flow_id:
                    all_flow_ids[flow_id] = module_id
        logic_path = module_dir / "logic.yaml"
        if logic_path.is_file():
            logic_doc = load_data(logic_path)
            for logic in first_list(logic_doc, "logic"):
                logic_id = str(logic.get("id", ""))
                if logic_id and logic_id in all_logic_ids:
                    errors.append(f"logic id {logic_id} is duplicated in modules {all_logic_ids[logic_id]} and {module_id}")
                if logic_id:
                    all_logic_ids[logic_id] = module_id
                if logic.get("source_candidate_id"):
                    source_candidate_links.add(str(logic["source_candidate_id"]))

    if not args.module:
        errors.extend(validate_cross_module_flows(args.contracts_root, all_case_ids))
        candidates_path = args.contracts_root / "source-logic-candidates.yaml"
        if candidates_path.is_file():
            candidate_document = load_data(candidates_path)
            candidates = first_list(candidate_document, "candidates")
            if isinstance(candidate_document, dict):
                errors.extend(
                    f"source scan failed: {error}"
                    for error in candidate_document.get("errors", [])
                    if str(error).strip()
                )
                java = candidate_document.get("java", {}) if isinstance(candidate_document.get("java"), dict) else {}
                if java.get("mapping_annotation_count", 0) and not java.get("entrypoint_count", 0):
                    errors.append("source contains Mapping annotations but the scanner recognized 0 entrypoints")
            for candidate in candidates:
                if candidate.get("coverage_required") is True and str(candidate.get("id")) not in source_candidate_links:
                    errors.append(
                        f"source logic candidate {candidate.get('id')} has no logic.yaml entry or linked case"
                    )

    endpoint_ids = [str(endpoint.get("id", "")) for _, endpoint in endpoint_records]
    duplicate_endpoint_ids = sorted({item for item in endpoint_ids if item and endpoint_ids.count(item) > 1})
    errors.extend(
        f"endpoint id {endpoint_id} is duplicated across module manifests"
        for endpoint_id in duplicate_endpoint_ids
    )

    openapi_path = args.openapi
    openapi_sha256: str | None = None
    if openapi_path is None:
        candidate = args.contracts_root / "openapi.json"
        if candidate.is_file():
            openapi_path = candidate
    if args.results and openapi_path is None:
        errors.append("completion requires an offline OpenAPI document; pass --openapi")
    global_contracts = (args.contracts_root / "modules").is_dir()
    if global_contracts and not args.module:
        errors.extend(validate_contracts_readme(args.contracts_root, modules))
    offline_endpoints: list[dict[str, Any]] = []
    if openapi_path is not None:
        if not openapi_path.is_file():
            errors.append(f"offline OpenAPI document does not exist: {openapi_path}")
        else:
            openapi_sha256 = hashlib.sha256(openapi_path.read_bytes()).hexdigest()
            try:
                offline_document = load_document(openapi_path)
                provenance = offline_document.get("provenance") if isinstance(offline_document, dict) else None
                version_lock = load_data(args.contracts_root / "version-lock.yaml")
                locked_business = version_lock.get("business", {}) if isinstance(version_lock, dict) and isinstance(version_lock.get("business"), dict) else {}
                locked_sha = locked_business.get("commit")
                contract_provenance_unverified = not (
                    isinstance(provenance, dict)
                    and provenance.get("status") in {"verified", "current"}
                    and provenance.get("application_sha")
                    and provenance.get("application_pid")
                    and locked_sha
                    and str(provenance.get("application_sha")) == str(locked_sha)
                )
                offline = extract(openapi_path, offline_document)
                offline_endpoints = offline["endpoints"]
                offline_inventory_count = len(offline["endpoints"])
                if global_contracts and not args.module:
                    offline_by_id = {str(item["id"]): item for item in offline["endpoints"]}
                    manifest_by_id = {
                        str(item.get("id")): item
                        for _, item in endpoint_records
                        if item.get("id")
                    }
                    errors.extend(
                        f"offline endpoint {endpoint_id} is not assigned to a module"
                        for endpoint_id in sorted(set(offline_by_id) - set(manifest_by_id))
                    )
                    errors.extend(
                        f"manifest endpoint {endpoint_id} is not present in offline OpenAPI"
                        for endpoint_id in sorted(set(manifest_by_id) - set(offline_by_id))
                    )
                    offline_keys = {
                        (item["method"], item["path"]): str(item["id"])
                        for item in offline["endpoints"]
                    }
                    manifest_keys: dict[tuple[str, str], str] = {}
                    for _, item in endpoint_records:
                        key = (
                            str(item.get("method", "")).upper(),
                            str(item.get("path", "")),
                        )
                        if key in manifest_keys and manifest_keys[key] != str(item.get("id")):
                            errors.append(f"manifest has duplicate endpoint operation {key[0]} {key[1]}")
                        manifest_keys[key] = str(item.get("id"))
                    errors.extend(
                        f"offline operation {method} {path} is not assigned to a module"
                        for method, path in sorted(set(offline_keys) - set(manifest_keys))
                    )
                    errors.extend(
                        f"manifest operation {method} {path} is not present in offline OpenAPI"
                        for method, path in sorted(set(manifest_keys) - set(offline_keys))
                    )
                elif args.module:
                    offline_keys = {
                        (str(item.get("method", "")).upper(), str(item.get("path", "")))
                        for item in offline["endpoints"]
                    }
                    errors.extend(
                        f"manifest operation {item.get('method')} {item.get('path')} is not present in offline OpenAPI"
                        for _, item in endpoint_records
                        if (str(item.get("method", "")).upper(), str(item.get("path", ""))) not in offline_keys
                    )
                    offline_inventory_count = len(endpoint_records)
            except (SystemExit, ValueError, TypeError) as exc:
                errors.append(f"cannot reconcile offline OpenAPI {openapi_path}: {exc}")
    if offline_endpoints and not args.module:
        errors.extend(validate_tag_partition(module_map_doc, endpoint_records, offline_endpoints))
    if args.preflight_results and openapi_sha256 and (
        not isinstance(preflight, dict) or preflight.get("openapi_sha256") != openapi_sha256
    ):
        errors.append("runtime preflight OpenAPI fingerprint does not match the checked contract")
    index_path = args.contracts_root / "index.yaml"
    if not index_path.is_file() and global_contracts and not args.module:
        errors.append(f"missing global index.yaml: {index_path}")
    if index_path.is_file():
        index = load_data(index_path)
        index_modules = first_list(index, "modules")
        index_ids = [str(item.get("id", "")) for item in index_modules]
        duplicate_index_ids = sorted({item for item in index_ids if item and index_ids.count(item) > 1})
        errors.extend(f"index.yaml repeats module id {module_id}" for module_id in duplicate_index_ids)
        declared = {str(item.get("id")): item for item in index_modules}
        actual = {module_id for module_id, _ in modules}
        if not args.module:
            errors.extend(f"index.yaml lists missing module {module_id}" for module_id in sorted(set(declared) - actual))
            errors.extend(f"module {module_id} is missing from index.yaml" for module_id in sorted(actual - set(declared)))

    module_reports = {}
    module_tags = {
        module_id: module_tag_from_document(module_map_doc, module_id)
        for module_id, _ in modules
    }
    if isinstance(module_map_doc, dict) and isinstance(module_map_doc.get("modules"), list):
        inverse_tags = {
            str(module.get("id")): (
                str(module.get("swagger_tags", [""])[0]).strip()
                if isinstance(module.get("swagger_tags"), list) and len(module.get("swagger_tags")) == 1
                else str(module.get("tag") or module.get("name") or module.get("id") or "").strip()
            )
            for module in module_map_doc.get("modules", [])
            if isinstance(module, dict)
            and module.get("id")
        }
        module_tags.update(inverse_tags)
    for module_id, module_dir in modules:
        report, module_errors = check_module(
            module_id,
            module_dir,
            args.bru_root,
            results,
            require_scenarios=args.require_scenarios,
            module_tag=module_tags.get(module_id),
            strict_bru_modules=(args.contracts_root / "modules").is_dir(),
            bru_module_name=module_dir.name,
            selected_case_ids=selected_case_ids_by_module.get(module_id, set()),
        )
        module_reports[module_id] = report
        for key in totals:
            totals[key] += report[key]
        errors.extend(f"[{module_id}] {error}" for error in module_errors)
        report["status"] = "blocked" if module_errors else (
            "verified" if results else ("runnable" if preflight_ok else "draft")
        )
        if module_id in declared:
            expected = declared[module_id]
            count_keys = {"endpoint_count": "endpoints", "case_count": "cases"}
            for index_key, report_key in count_keys.items():
                if index_key in expected and expected[index_key] != report[report_key]:
                    errors.append(
                        f"[{module_id}] index.yaml {index_key}={expected[index_key]} but actual={report[report_key]}"
                    )

    if offline_inventory_count is not None:
        totals["inventory_endpoints"] = offline_inventory_count
    if args.require_scenarios and contract_provenance_unverified:
        warnings.append("offline OpenAPI provenance or business version is not aligned")
    runtime_error_re = re.compile(r"(?:^|\]) case [^ ]+ (?:was not executed|failed)$")
    errors = list(dict.fromkeys(errors))
    static_errors = [error for error in errors if not runtime_error_re.search(error)]
    static_ok = not static_errors
    module_completion_ok = False
    module_status: str | None = None
    if args.results:
        required_case_ids = required_case_ids_global
        executed = set(results.get("executed", [])) if results else set()
        passed = set(results.get("passed", [])) if results else set()
        unknown_executed = executed - set(all_case_ids)
        errors.extend(f"execution evidence references unknown case {case_id}" for case_id in sorted(unknown_executed))
        scope_completion_ok = static_ok and not errors and required_case_ids.issubset(executed) and required_case_ids.issubset(passed) \
            and not totals["pending_exclusions"]
        if args.module:
            module_completion_ok = scope_completion_ok
            module_status = "verified" if module_completion_ok else "blocked"
            completion_ok = False
            status = "draft"
        else:
            completion_ok = scope_completion_ok
            status = "verified" if completion_ok else "blocked"
    elif preflight_ok and static_ok:
        completion_ok = False
        status = "runnable"
    elif args.preflight_results and static_ok:
        completion_ok = False
        status = "blocked"
    else:
        completion_ok = False
        status = "draft" if static_ok else "blocked"
    if args.module and not args.results:
        module_status = status
        status = "draft"
    report = {
        **totals,
        "report_version": STRICT_REPORT_VERSION,
        "check_profile": "full-matrix-strict" if args.require_scenarios and args.require_auth else "custom",
        "scenario_matrix_checked": bool(args.require_scenarios),
        "constraint_obligations_checked": bool(args.require_scenarios),
        "exact_assertions_checked": bool(args.require_scenarios),
        "variables_checked": bool(args.require_scenarios and args.require_auth),
        "source_mapping_checked": bool(args.require_scenarios),
        "qa_lock_checked": bool(args.require_scenarios),
        "business_version_checked": business_version_checked,
        "modules": module_reports,
        "blocked_modules": sum(value.get("status") == "blocked" for value in module_reports.values()),
        "errors": errors,
        "warnings": list(dict.fromkeys(warnings)),
        "static_ok": static_ok,
        "static_ready": static_ok,
        "context_ready": bool(isinstance(preflight, dict) and preflight.get("context_ready") is True),
        "execution_ready": bool(isinstance(preflight, dict) and preflight.get("execution_ready") is True),
        "completion_ok": completion_ok,
        "status": status,
        "ok": completion_ok,
        **({
            "module_completion_ok": module_completion_ok,
            "module_status": module_status,
        } if args.module else {}),
        "contract_provenance_unverified": contract_provenance_unverified,
        "execution_scope": "module" if args.module else "all",
        "module_scope": args.module,
        "openapi_sha256": openapi_sha256,
        "by_tag": {
            module_id: {
                "swagger_tag": report.get("swagger_tag"),
                "registered_endpoints": report.get("endpoints", 0),
                "registered_cases": report.get("registered_cases", report.get("cases", 0)),
                "successful_cases": report.get("successful_cases", 0),
                "success_cases": report.get("success_cases", 0),
                "excluded_endpoints": report.get("excluded_endpoints", 0),
                "executed_cases": report.get("executed_cases", 0),
                "passed_cases": report.get("passed_cases", 0),
            }
            for module_id, report in module_reports.items()
        },
    }
    if not args.module and args.write_status:
        sync_global_index(args.contracts_root / "index.yaml", status, totals, module_reports)
    if args.as_json:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    else:
        print(
            f"status={status} modules={len(modules)} inventory_endpoints={totals['inventory_endpoints']} "
            f"endpoints_with_cases={totals['endpoints_with_cases']} generated_cases={totals['generated_cases']} "
            f"executed_cases={totals['executed_cases']} passed_cases={totals['passed_cases']} "
            f"happy_path_endpoints={totals['happy_path_endpoints']} pending_success_endpoints={totals['pending_success_endpoints']} "
            f"verified_flows={totals['verified_flows']}"
        )
        for error in errors:
            print(f"ERROR: {error}")
        if errors:
            print(f"coverage check failed: {len(errors)} error(s)")
        elif args.module and module_completion_ok:
            print("module coverage check passed: verified")
        elif completion_ok:
            print("coverage check passed: verified")
        else:
            print(f"coverage check is static-valid but incomplete: {status}")
    passed_completion = module_completion_ok if args.module else completion_ok
    return 0 if static_ok and status != "blocked" and (not args.results or passed_completion) else 1


if __name__ == "__main__":
    raise SystemExit(main())
