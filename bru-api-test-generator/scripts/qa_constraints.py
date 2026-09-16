#!/usr/bin/env python3
"""Shared machine-enforced constraints for Bruno QA generation and execution."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

sys.dont_write_bytecode = True

from check_artifact_safety import scan as scan_secrets
from manifest_io import first_list, load_data


RULES_VERSION = 1
REVIEW_RE = re.compile(r"review-[A-Za-z0-9_.-]+", re.IGNORECASE)
META_NAME_RE = re.compile(r"(?ms)^\s*meta\s*\{.*?^\s*name:\s*([^\r\n}]+).*?^\s*\}")


DEFAULT_RULES: tuple[dict[str, Any], ...] = (
    {"id": "module-owner-unique", "stages": ["generation", "materialization", "pre-execution", "post-execution"], "required": True},
    {"id": "endpoint-unique", "stages": ["generation", "materialization", "pre-execution", "post-execution"], "required": True},
    {"id": "case-id-unique", "stages": ["generation", "materialization", "pre-execution", "post-execution"], "required": True},
    {"id": "bru-registered", "stages": ["materialization", "pre-execution", "post-execution"], "required": True},
    {"id": "request-parseable", "stages": ["generation", "materialization", "pre-execution", "post-execution"], "required": True},
    {"id": "review-reason-required", "stages": ["generation", "materialization", "pre-execution", "post-execution"], "required": True},
    {"id": "review-placeholder-authorized", "stages": ["generation", "materialization", "pre-execution", "post-execution"], "required": True},
    {"id": "registered-case-no-drift", "stages": ["pre-execution", "post-execution"], "required": True},
    {"id": "manual-change-preserved", "stages": ["generation", "materialization"], "required": True},
    {"id": "module-worker-boundary", "stages": ["generation", "materialization", "pre-execution", "post-execution"], "required": True},
    {"id": "business-code-immutable", "stages": ["generation", "materialization", "pre-execution", "post-execution"], "required": True},
    {"id": "qa-assets-secret-free", "stages": ["generation", "materialization", "pre-execution", "post-execution"], "required": True},
    {"id": "source-domain-rules", "stages": ["generation", "materialization", "pre-execution", "post-execution"], "required": True},
)

DATABASE_STEP_REASONS = {
    "setup": "missing_prerequisite_api",
    "assertion": "missing_response_state",
}
DATABASE_STEP_FIELDS = {
    "phase", "reason", "engine", "script", "evidence", "expected",
    "cleanup", "cleanup_not_required_reason",
}


def database_steps(case: dict[str, Any]) -> list[dict[str, Any]]:
    value = case.get("database_steps", [])
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def database_access_errors(case: dict[str, Any]) -> list[str]:
    case_id = str(case.get("id", "<unknown>"))
    value = case.get("database_steps")
    if value is None:
        return []
    if not isinstance(value, list) or not value:
        return [f"case {case_id} database_steps must be a non-empty list"]
    errors: list[str] = []
    for index, step in enumerate(value):
        label = f"case {case_id} database_steps[{index}]"
        if not isinstance(step, dict):
            errors.append(f"{label} must be an object")
            continue
        unknown = sorted(set(step) - DATABASE_STEP_FIELDS)
        if unknown:
            errors.append(f"{label} contains unsupported field(s): {', '.join(unknown)}")
        phase = str(step.get("phase", ""))
        if phase not in DATABASE_STEP_REASONS:
            errors.append(f"{label}.phase must be setup or assertion")
        elif step.get("reason") != DATABASE_STEP_REASONS[phase]:
            errors.append(f"{label}.reason must be {DATABASE_STEP_REASONS[phase]} for phase {phase}")
        if not str(step.get("engine", "")).strip():
            errors.append(f"{label}.engine must name the database technology")
        if not str(step.get("script", "")).strip():
            errors.append(f"{label}.script must contain executable Bruno JavaScript")
        evidence = step.get("evidence")
        if not (
            isinstance(evidence, str) and evidence.strip()
            or isinstance(evidence, list) and any(str(item).strip() for item in evidence)
        ):
            errors.append(f"{label}.evidence must cite the source schema or repository logic")
        if phase == "assertion" and not (
            isinstance(step.get("expected"), dict) and step["expected"]
        ):
            errors.append(f"{label}.expected must declare the exact database result")
        if phase == "assertion" and not re.search(r"\btest\s*\(", str(step.get("script", ""))):
            errors.append(f"{label}.script must register a Bruno test(...) observation")
        if phase == "setup" and not str(step.get("cleanup", "")).strip() and not str(
            step.get("cleanup_not_required_reason", "")
        ).strip():
            errors.append(f"{label} must declare cleanup or cleanup_not_required_reason")
    return errors


def rules_path(qa_root: Path) -> Path:
    return qa_root / "constraints" / "rules.yaml"


def qa_root_for_contracts(contracts_root: Path) -> Path:
    resolved = contracts_root.resolve()
    for candidate in (resolved, *resolved.parents):
        if candidate.name == "contracts":
            return candidate.parent
    return resolved.parent


def _render_yaml(value: Any) -> str:
    try:
        import yaml  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise ValueError("QA constraints require PyYAML") from exc
    return yaml.safe_dump(value, allow_unicode=True, sort_keys=False)


def ensure_rule_library(qa_root: Path) -> Path:
    """Create the project-owned rule manifest once; never silently weaken it."""

    path = rules_path(qa_root)
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_render_yaml({"version": RULES_VERSION, "rules": list(DEFAULT_RULES)}), encoding="utf-8")
    errors = rule_library_errors(path)
    if errors:
        raise ValueError("; ".join(errors))
    return path


def rule_library_errors(path: Path) -> list[str]:
    if not path.is_file():
        return [f"machine constraint library is missing: {path}"]
    document = load_data(path)
    if not isinstance(document, dict) or document.get("version") != RULES_VERSION:
        return [f"machine constraint library has unsupported version: {path}"]
    configured = {
        str(item.get("id")): item
        for item in document.get("rules", [])
        if isinstance(item, dict) and item.get("id")
    }
    errors: list[str] = []
    for required in DEFAULT_RULES:
        actual = configured.get(str(required["id"]))
        if actual is None:
            errors.append(f"required machine constraint is missing: {required['id']}")
        elif actual.get("required") is not True or actual.get("enabled", True) is not True:
            errors.append(f"required machine constraint is disabled: {required['id']}")
        elif not set(required["stages"]).issubset(set(actual.get("stages", []))):
            errors.append(f"required machine constraint has incomplete stages: {required['id']}")
    unknown = sorted(set(configured) - {str(item["id"]) for item in DEFAULT_RULES})
    errors.extend(f"unknown machine constraint: {rule_id}" for rule_id in unknown)
    return errors


def _module_documents(qa_root: Path, module: str | None = None) -> list[tuple[str, Path, dict[str, Any], dict[str, Any]]]:
    contracts_root = qa_root / "contracts"
    module_map_path = contracts_root / "module-map.yaml"
    module_map = load_data(module_map_path) if module_map_path.is_file() else {}
    metadata = {
        str(item.get("id")): item
        for item in (module_map.get("modules", []) if isinstance(module_map, dict) else [])
        if isinstance(item, dict) and item.get("id")
    }
    records: list[tuple[str, Path, dict[str, Any], dict[str, Any]]] = []
    modules_root = contracts_root / "modules"
    if (contracts_root / "endpoints.yaml").is_file():
        directories = [contracts_root]
    else:
        search_root = modules_root if modules_root.is_dir() else contracts_root
        directories = sorted(
            path for path in search_root.iterdir()
            if path.is_dir() and (path / "endpoints.yaml").is_file()
        ) if search_root.is_dir() else []
    for directory in directories:
        endpoint_path = directory / "endpoints.yaml"
        case_path = directory / "cases.yaml"
        endpoint_doc = load_data(endpoint_path) if endpoint_path.is_file() else {}
        case_doc = load_data(case_path) if case_path.is_file() else {}
        module_id = str(endpoint_doc.get("module", case_doc.get("module", directory.name))) if isinstance(endpoint_doc, dict) else directory.name
        info = metadata.get(module_id, {})
        aliases = {
            module_id, directory.name, str(info.get("name", "")), str(info.get("directory", "")),
            *[str(tag) for tag in info.get("swagger_tags", []) if str(tag)],
        }
        if module and module not in aliases:
            continue
        records.append((module_id, directory, endpoint_doc if isinstance(endpoint_doc, dict) else {}, case_doc if isinstance(case_doc, dict) else {}))
    return records


def _walk_review_values(value: Any, path: str = "$") -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        for match in REVIEW_RE.finditer(value):
            yield path, match.group(0)
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _walk_review_values(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_review_values(child, f"{path}[{index}]")


def review_reason_map(case: dict[str, Any]) -> dict[str, str]:
    raw = case.get("review_reasons", {})
    if isinstance(raw, dict):
        return {str(key): str(value).strip() for key, value in raw.items() if str(value).strip()}
    if isinstance(raw, list):
        return {
            str(item.get("placeholder") or item.get("path")): str(item.get("reason", "")).strip()
            for item in raw
            if isinstance(item, dict)
            and (item.get("placeholder") or item.get("path"))
            and str(item.get("reason", "")).strip()
        }
    return {}


def review_reason_errors(case: dict[str, Any]) -> list[str]:
    case_id = str(case.get("id", "<unknown>"))
    placeholders = list(_walk_review_values({
        "request": case.get("request"),
        "expected": case.get("expected"),
        "assertions": case.get("assertions"),
    }))
    reasons = review_reason_map(case)
    errors: list[str] = []
    for value_path, placeholder in placeholders:
        if placeholder not in reasons and value_path not in reasons:
            errors.append(f"case {case_id} review placeholder {placeholder} at {value_path} has no review_reasons entry")
    if case.get("review_required") is True and not reasons and not str(case.get("review_reason", "")).strip():
        errors.append(f"case {case_id} requires manual confirmation but has no reason")
    return errors


def _normalized_field(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", value).casefold()


def _review_authorization_errors(
    qa_root: Path,
    records: list[tuple[str, Path, dict[str, Any], dict[str, Any]]],
) -> list[str]:
    available: dict[str, str] = {}
    source_paths = [qa_root / "constraints" / "source-rules.yaml"]
    source_paths.extend(directory / "source-rules.yaml" for _, directory, _, _ in records)
    for path in source_paths:
        document = load_data(path) if path.is_file() else {}
        for rule in document.get("field_rules", []) if isinstance(document, dict) else []:
            if not isinstance(rule, dict):
                continue
            constraints = rule.get("constraints", {}) if isinstance(rule.get("constraints"), dict) else {}
            reusable = (
                rule.get("example") is not None
                or constraints.get("default") is not None
                or bool(constraints.get("enum"))
            )
            if reusable:
                for name in rule.get("field_names", []):
                    available[_normalized_field(str(name))] = f"source rule {rule.get('id', '<unknown>')}"
    config_path = qa_root / "execution" / "config.yaml"
    if config_path.is_file():
        try:
            from execution_config import environment_file, load_bruno_environment, load_execution_config

            config = load_execution_config(config_path)
            environment_path = environment_file(config_path, config)
            for name, value in load_bruno_environment(environment_path).items():
                if str(value).strip():
                    available[_normalized_field(name)] = f"local environment variable {name}"
        except (OSError, ValueError, TypeError):
            pass
    errors: list[str] = []
    for _, _, _, case_doc in records:
        for case in first_list(case_doc, "cases"):
            case_id = str(case.get("id", "<unknown>"))
            for value_path, placeholder in _walk_review_values(case.get("request", {}), "$.request"):
                names = {
                    _normalized_field(placeholder.removeprefix("review-")),
                    _normalized_field(re.split(r"[.\[]", value_path)[-1].rstrip("]")),
                }
                source = next((available[name] for name in names if name in available), None)
                if source:
                    errors.append(
                        f"case {case_id} review placeholder {placeholder} is unauthorized because {source} provides a value"
                    )
    return errors


def needs_manual_confirmation(case: dict[str, Any]) -> bool:
    return bool(
        case.get("review_required") is True
        or case.get("manual_review") is True
        or list(_walk_review_values({
            "request": case.get("request"),
            "expected": case.get("expected"),
            "assertions": case.get("assertions"),
        }))
    )


def _request_errors(case: dict[str, Any]) -> list[str]:
    case_id = str(case.get("id", "<unknown>"))
    request = case.get("request", {})
    if not isinstance(request, dict):
        return [f"case {case_id} request must be an object"]
    body = request.get("body")
    body_type = str(request.get("body_type") or request.get("content_type") or "").lower()
    if isinstance(body, str) and ("json" in body_type or body.lstrip().startswith(("{", "["))):
        try:
            json.loads(body)
        except json.JSONDecodeError as exc:
            return [f"case {case_id} request JSON is not parseable: {exc}"]
    for field in ("query", "headers", "path_parameters"):
        if field in request and not isinstance(request[field], dict):
            return [f"case {case_id} request.{field} must be an object"]
    return []


def _meta_name(content: str) -> str | None:
    match = META_NAME_RE.search(content)
    return match.group(1).strip() if match else None


def _file_sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def module_lock_document(qa_root: Path, module: str) -> tuple[Path, dict[str, Any]]:
    records = _module_documents(qa_root, module)
    if len(records) != 1:
        raise ValueError(f"module is not unique: {module}")
    module_id, directory, _, case_doc = records[0]
    bru_root = _module_bru_root(qa_root, directory)
    cases = {
        str(case.get("id")): {
            "case_sha256": hashlib.sha256(
                json.dumps(case, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest(),
            "bru_sha256": _file_sha(bru_root / str(case.get("bru", ""))),
        }
        for case in first_list(case_doc, "cases")
        if case.get("id")
    }
    path = directory / "module-lock.yaml"
    return path, {
        "version": 1,
        "module": module_id,
        "endpoints_sha256": _file_sha(directory / "endpoints.yaml"),
        "logic_sha256": _file_sha(directory / "logic.yaml"),
        "cases": cases,
    }


def write_module_lock(qa_root: Path, module: str) -> Path:
    path, document = module_lock_document(qa_root, module)
    path.write_text(_render_yaml(document), encoding="utf-8")
    return path


def check_module_lock(qa_root: Path, module: str) -> list[str]:
    try:
        path, expected = module_lock_document(qa_root, module)
        actual = load_data(path) if path.is_file() else None
    except (OSError, ValueError, TypeError) as exc:
        return [f"cannot validate module lock: {exc}"]
    return [] if actual == expected else [f"module lock is missing or stale: {path}"]


def _module_bru_root(qa_root: Path, directory: Path) -> Path:
    candidate = qa_root / "bruno" / directory.name
    canonical = (qa_root / "contracts" / "modules").is_dir()
    return candidate if canonical or candidate.is_dir() else qa_root / "bruno"


def _registered_bru_errors(qa_root: Path, records: list[tuple[str, Path, dict[str, Any], dict[str, Any]]]) -> list[str]:
    errors: list[str] = []
    registered: dict[str, str] = {}
    expected_paths: set[Path] = set()
    for module_id, directory, _, case_doc in records:
        module_bru = _module_bru_root(qa_root, directory)
        for case in first_list(case_doc, "cases"):
            case_id = str(case.get("id", ""))
            bru = str(case.get("bru", ""))
            if not case_id:
                continue
            if not bru:
                errors.append(f"case {case_id} is not registered to a .bru file")
                continue
            path = (module_bru / bru).resolve()
            expected_paths.add(path)
            if not path.is_file():
                errors.append(f"case {case_id} registered .bru file is missing: {path}")
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="strict")
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(f"case {case_id} .bru file cannot be read: {exc}")
                continue
            actual = _meta_name(content)
            if actual != case_id:
                errors.append(f"case {case_id} .bru meta.name is {actual or '<missing>'}")
            previous = registered.get(case_id)
            if previous and previous != str(path):
                errors.append(f"case {case_id} is registered by multiple .bru files")
            registered[case_id] = str(path)
    scoped_roots = {_module_bru_root(qa_root, directory) for _, directory, _, _ in records}
    for root in scoped_roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.bru"):
            if path.resolve() in expected_paths:
                continue
            try:
                name = _meta_name(path.read_text(encoding="utf-8", errors="strict"))
            except (OSError, UnicodeDecodeError):
                name = None
            if name:
                errors.append(f"unregistered Bruno request: {path}")
    return errors


def _field_values(value: Any, found: dict[str, list[Any]]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            found.setdefault(str(key).casefold(), []).append(child)
            _field_values(child, found)
    elif isinstance(value, list):
        for child in value:
            _field_values(child, found)


def _rule_ids(value: Any) -> set[str]:
    if isinstance(value, dict):
        current = {str(value["x-qa-rule-id"])} if value.get("x-qa-rule-id") else set()
        return current.union(*(_rule_ids(child) for child in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_rule_ids(child) for child in value), set())
    return set()


def _request_rule_ids(endpoint: dict[str, Any]) -> set[str]:
    return _rule_ids({
        "parameters": endpoint.get("parameters", []),
        "request_body": endpoint.get("request_body", {}),
    })


def _concrete(value: Any) -> bool:
    return not isinstance(value, (dict, list)) and not (
        isinstance(value, str) and ("{{" in value or REVIEW_RE.search(value))
    )


def _value_constraint_errors(case_id: str, field: str, value: Any, constraints: dict[str, Any]) -> list[str]:
    if isinstance(value, str) and ("{{" in value or REVIEW_RE.search(value)):
        return []
    errors: list[str] = []
    expected_type = constraints.get("type")
    type_matches = {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "array": isinstance(value, list),
        "object": isinstance(value, dict),
    }
    if expected_type in type_matches and not type_matches[expected_type]:
        errors.append(f"case {case_id} field {field} violates source type {expected_type}")
        return errors
    if isinstance(value, str):
        if constraints.get("minLength") is not None and len(value) < int(constraints["minLength"]):
            errors.append(f"case {case_id} field {field} is shorter than source minLength {constraints['minLength']}")
        if constraints.get("maxLength") is not None and len(value) > int(constraints["maxLength"]):
            errors.append(f"case {case_id} field {field} is longer than source maxLength {constraints['maxLength']}")
        if constraints.get("pattern"):
            try:
                if re.fullmatch(str(constraints["pattern"]), value) is None:
                    errors.append(f"case {case_id} field {field} violates source pattern")
            except re.error as exc:
                errors.append(f"source constraint pattern for {field} is invalid: {exc}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if constraints.get("minimum") is not None and value < constraints["minimum"]:
            errors.append(f"case {case_id} field {field} is below source minimum {constraints['minimum']}")
        if constraints.get("maximum") is not None and value > constraints["maximum"]:
            errors.append(f"case {case_id} field {field} is above source maximum {constraints['maximum']}")
    if isinstance(constraints.get("enum"), list) and value not in constraints["enum"]:
        errors.append(f"case {case_id} field {field} is outside source enum")
    return errors


def _source_rule_errors(qa_root: Path, records: list[tuple[str, Path, dict[str, Any], dict[str, Any]]]) -> list[str]:
    documents: list[dict[str, Any]] = []
    global_path = qa_root / "constraints" / "source-rules.yaml"
    if global_path.is_file():
        loaded = load_data(global_path)
        if isinstance(loaded, dict):
            documents.append(loaded)
    for _, directory, _, _ in records:
        path = directory / "source-rules.yaml"
        if path.is_file():
            loaded = load_data(path)
            if isinstance(loaded, dict):
                documents.append(loaded)
    rules_by_id = {
        str(item.get("id")): item
        for document in documents
        for item in document.get("field_rules", [])
        if isinstance(item, dict) and item.get("id") and isinstance(item.get("constraints"), dict)
    }
    errors: list[str] = []
    unique_values: dict[tuple[str, str], dict[str, str]] = {}
    for _, _, endpoint_doc, case_doc in records:
        endpoints = {
            str(endpoint.get("id")): endpoint
            for endpoint in first_list(endpoint_doc, "endpoints")
            if isinstance(endpoint, dict) and endpoint.get("id")
        }
        for case in first_list(case_doc, "cases"):
            if str(case.get("scenario", "")).lower() != "success":
                continue
            endpoint_id = str(case.get("endpoint_id", ""))
            endpoint = endpoints.get(endpoint_id, {})
            active_rules = {
                rule_id: rules_by_id[rule_id]
                for rule_id in _request_rule_ids(endpoint)
                if rule_id in rules_by_id
            }
            values: dict[str, list[Any]] = {}
            _field_values(case.get("request", {}), values)
            for rule_id, rule in active_rules.items():
                constraints = rule.get("constraints", {})
                names = [str(name).casefold() for name in rule.get("field_names", [])]
                matched = [(name, value) for name in names for value in values.get(name, [])]
                case_id = str(case.get("id", "<unknown>"))
                field = str(rule.get("field") or next(iter(names), rule_id))
                if constraints.get("required") is True and not matched:
                    errors.append(f"case {case_id} is missing source-required field {field}")
                for _, value in matched:
                    errors.extend(_value_constraint_errors(case_id, field, value, constraints))
                    if (
                        constraints.get("unique") is True
                        and str(endpoint.get("method", "GET")).upper() not in {"GET", "HEAD", "OPTIONS"}
                        and _concrete(value)
                    ):
                        key = (endpoint_id, rule_id)
                        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
                        previous = unique_values.setdefault(key, {}).get(rendered)
                        if previous and previous != case_id:
                            errors.append(
                                f"success cases {previous} and {case_id} reuse source-unique field {field}"
                            )
                        unique_values[key][rendered] = case_id
    return errors


def _schema_value_errors(case_id: str, path: str, value: Any, schema: Any) -> list[str]:
    if not isinstance(schema, dict):
        return []
    errors = _value_constraint_errors(case_id, path, value, schema)
    if isinstance(value, dict):
        properties = schema.get("properties", {}) if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required", []) if isinstance(schema.get("required"), list) else []
        for name in required:
            if name not in value:
                errors.append(f"case {case_id} response {path} is missing source-required field {name}")
        for name, child in properties.items():
            if name in value:
                errors.extend(_schema_value_errors(case_id, f"{path}.{name}", value[name], child))
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, child in enumerate(value):
            errors.extend(_schema_value_errors(case_id, f"{path}[{index}]", child, schema["items"]))
    return errors


def _latest_evidence_documents(
    qa_root: Path,
    records: list[tuple[str, Path, dict[str, Any], dict[str, Any]]],
    module_scoped: bool = False,
) -> list[dict[str, Any]]:
    module_ids = {module_id for module_id, _, _, _ in records}
    roots: list[Path]
    if module_scoped:
        roots = [qa_root / "evidence" / "modules" / module_id for module_id in sorted(module_ids)]
    else:
        roots = [qa_root / "evidence" / "global"]
    documents: list[dict[str, Any]] = []
    for root in roots:
        paths = sorted(root.glob("*-evidence.json")) if root.is_dir() else []
        if paths:
            loaded = load_data(paths[-1])
            if isinstance(loaded, dict):
                documents.append(loaded)
    return documents


def _source_response_errors(
    qa_root: Path,
    records: list[tuple[str, Path, dict[str, Any], dict[str, Any]]],
    module_scoped: bool = False,
) -> list[str]:
    endpoints: dict[str, dict[str, Any]] = {}
    cases: dict[str, dict[str, Any]] = {}
    for _, _, endpoint_doc, case_doc in records:
        endpoints.update({
            str(endpoint.get("id")): endpoint
            for endpoint in first_list(endpoint_doc, "endpoints")
            if isinstance(endpoint, dict) and endpoint.get("id")
        })
        cases.update({
            str(case.get("id")): case
            for case in first_list(case_doc, "cases")
            if isinstance(case, dict) and case.get("id")
        })
    errors: list[str] = []
    for evidence in _latest_evidence_documents(qa_root, records, module_scoped):
        passed = set(evidence.get("passed", []))
        observations = evidence.get("cases", {}) if isinstance(evidence.get("cases"), dict) else {}
        for case_id in passed:
            case = cases.get(str(case_id), {})
            endpoint = endpoints.get(str(case.get("endpoint_id", "")), {})
            observation = observations.get(case_id, {}) if isinstance(observations.get(case_id), dict) else {}
            actual = observation.get("actual", {}) if isinstance(observation.get("actual"), dict) else {}
            status = actual.get("http_status")
            body = actual.get("body")
            responses = endpoint.get("responses", {}) if isinstance(endpoint.get("responses"), dict) else {}
            response = responses.get(str(status), responses.get(status, {}))
            content = response.get("content", {}) if isinstance(response, dict) and isinstance(response.get("content"), dict) else {}
            media = next((item for item in content.values() if isinstance(item, dict)), None)
            schema = media.get("schema") if isinstance(media, dict) else None
            if isinstance(schema, dict) and (_rule_ids(schema) or schema.get("x-source-evidence")):
                errors.extend(_schema_value_errors(str(case_id), "$", body, schema))
    return errors


def _git_repository(path: Path) -> Path | None:
    completed = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        check=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return Path(completed.stdout.strip()).resolve() if completed.returncode == 0 and completed.stdout.strip() else None


def _path_marker(path: Path) -> str:
    if not path.exists():
        return "<deleted>"
    if path.is_symlink():
        return "symlink:" + str(path.readlink())
    if not path.is_file():
        return "<directory>"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_workspace_state(repository: Path) -> dict[str, Any]:
    head = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    changed = subprocess.run(
        ["git", "-C", str(repository), "diff", "--name-only", "--no-renames", "-z", "HEAD"],
        check=False, capture_output=True,
    )
    untracked = subprocess.run(
        ["git", "-C", str(repository), "ls-files", "--others", "--exclude-standard", "-z"],
        check=False, capture_output=True,
    )
    if head.returncode or changed.returncode or untracked.returncode:
        raise ValueError(f"cannot capture Git workspace state under {repository}")
    names = {
        item.decode("utf-8", errors="surrogateescape")
        for output in (changed.stdout, untracked.stdout)
        for item in output.split(b"\0")
        if item
    }
    names = {name for name in names if "/worker-assignments/" not in f"/{name.replace(chr(92), '/')}"}
    return {
        "repository": str(repository),
        "head": head.stdout.strip(),
        "files": {name: _path_marker(repository / name) for name in sorted(names)},
    }


def worker_snapshot_path(qa_root: Path, module: str) -> Path:
    records = _module_documents(qa_root.resolve(), module)
    if len(records) != 1:
        raise ValueError(f"module is not unique: {module}")
    module_id = records[0][0]
    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", module_id).strip("-") or "module"
    return qa_root.resolve() / "contracts" / "worker-assignments" / f"{safe_id}.yaml"


def write_worker_snapshot(qa_root: Path, module: str) -> Path:
    qa_root = qa_root.resolve()
    repository = _git_repository(qa_root)
    if repository is None:
        raise ValueError("module-worker boundary enforcement requires a Git repository")
    records = _module_documents(qa_root, module)
    if len(records) != 1:
        raise ValueError(f"module is not unique: {module}")
    path = worker_snapshot_path(qa_root, module)
    document = {
        "version": 1,
        "module": records[0][0],
        "directory": records[0][1].name,
        "baseline": _git_workspace_state(repository),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_yaml(document), encoding="utf-8")
    return path


def worker_changed_paths(qa_root: Path, module: str) -> list[Path]:
    path = worker_snapshot_path(qa_root, module)
    if not path.is_file():
        raise ValueError(f"module-worker snapshot is missing: {path}")
    document = load_data(path)
    baseline = document.get("baseline", {}) if isinstance(document, dict) else {}
    repository = Path(str(baseline.get("repository", ""))).resolve()
    current = _git_workspace_state(repository)
    if current["head"] != baseline.get("head"):
        raise ValueError("module-worker changed the Git HEAD; commits are coordinator-owned")
    before = baseline.get("files", {}) if isinstance(baseline.get("files"), dict) else {}
    after = current.get("files", {}) if isinstance(current.get("files"), dict) else {}
    changed = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
    return [repository / name for name in changed]


def validate_worker_snapshot(qa_root: Path, module: str, stage: str) -> list[str]:
    try:
        changed = worker_changed_paths(qa_root, module)
    except (OSError, ValueError, TypeError) as exc:
        return [str(exc)]
    return validate_stage(qa_root, stage, module=module, actor="module-worker", changed_paths=changed)


def _manual_change_errors(qa_root: Path, records: list[tuple[str, Path, dict[str, Any], dict[str, Any]]]) -> list[str]:
    state_path = qa_root / "contracts" / "generation-state.yaml"
    state = load_data(state_path) if state_path.is_file() else {}
    baselines = state.get("cases", {}) if isinstance(state, dict) and isinstance(state.get("cases"), dict) else {}
    errors: list[str] = []
    for _, _, _, case_doc in records:
        for case in first_list(case_doc, "cases"):
            case_id = str(case.get("id", ""))
            baseline = baselines.get(case_id, {}) if isinstance(baselines.get(case_id), dict) else {}
            if baseline.get("manual_review") is not True or not baseline.get("fingerprint"):
                continue
            current = {
                key: value for key, value in case.items()
                if key not in {"bru", "bru_file", "file_name", "manual_review"}
            }
            fingerprint = hashlib.sha256(
                json.dumps(current, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            if fingerprint != baseline["fingerprint"]:
                errors.append(f"manually maintained case {case_id} changed without coordinator reconciliation")
    return errors


def _boundary_errors(qa_root: Path, actor: str, module: str | None, changed_paths: Iterable[Path] | None) -> list[str]:
    if actor != "module-worker":
        return []
    if not module:
        return ["module-worker validation requires a module"]
    if changed_paths is None:
        return ["module-worker validation requires explicit changed paths"]
    records = _module_documents(qa_root, module)
    if len(records) != 1:
        return [f"module-worker module is not unique: {module}"]
    module_id, directory, _, _ = records[0]
    allowed = (
        qa_root / "contracts" / "modules" / directory.name,
        qa_root / "bruno" / directory.name,
        qa_root / "results" / "modules" / module_id,
        qa_root / "evidence" / "modules" / module_id,
        qa_root / "logs" / "modules" / module_id,
    )
    errors: list[str] = []
    for raw in changed_paths:
        path = raw.resolve()
        if not any(path == root.resolve() or root.resolve() in path.parents for root in allowed):
            errors.append(f"module-worker {module_id} changed coordinator-owned path: {path}")
    return errors


def validate_stage(
    qa_root: Path,
    stage: str,
    module: str | None = None,
    actor: str = "coordinator",
    changed_paths: Iterable[Path] | None = None,
) -> list[str]:
    """Run the same rule definitions at every lifecycle stage."""

    qa_root = qa_root.resolve()
    path = rules_path(qa_root)
    errors = rule_library_errors(path)
    if errors:
        return errors
    document = load_data(path)
    active = {
        str(item.get("id"))
        for item in document.get("rules", [])
        if isinstance(item, dict) and stage in item.get("stages", [])
    }
    records = _module_documents(qa_root, module)
    identity_records = _module_documents(qa_root)
    if "module-owner-unique" in active:
        module_ids = [module_id for module_id, _, _, _ in identity_records]
        for module_id in sorted(set(module_ids)):
            if module_ids.count(module_id) > 1:
                errors.append(f"module id {module_id} is owned by multiple directories")
    endpoint_ids: dict[str, str] = {}
    endpoint_keys: dict[str, str] = {}
    case_ids: dict[str, str] = {}
    for module_id, _, endpoint_doc, case_doc in identity_records:
        for endpoint in first_list(endpoint_doc, "endpoints"):
            endpoint_id = str(endpoint.get("id", ""))
            endpoint_key = f"{str(endpoint.get('method', '')).upper()} {endpoint.get('path')}"
            if "module-owner-unique" in active and endpoint.get("module") not in {None, "", module_id}:
                errors.append(f"endpoint {endpoint_id} declares module {endpoint.get('module')} but is owned by {module_id}")
            if "endpoint-unique" in active:
                if endpoint_id in endpoint_ids:
                    errors.append(f"endpoint id {endpoint_id} is duplicated in {endpoint_ids[endpoint_id]} and {module_id}")
                if endpoint_key in endpoint_keys:
                    errors.append(f"endpoint {endpoint_key} is duplicated in {endpoint_keys[endpoint_key]} and {module_id}")
                endpoint_ids[endpoint_id] = module_id
                endpoint_keys[endpoint_key] = module_id
        for case in first_list(case_doc, "cases"):
            case_id = str(case.get("id", ""))
            if "case-id-unique" in active:
                if case_id in case_ids:
                    errors.append(f"case id {case_id} is duplicated in {case_ids[case_id]} and {module_id}")
                case_ids[case_id] = module_id
    for _, _, _, case_doc in records:
        for case in first_list(case_doc, "cases"):
            if "request-parseable" in active:
                errors.extend(_request_errors(case))
                errors.extend(database_access_errors(case))
            if "review-reason-required" in active:
                errors.extend(review_reason_errors(case))
    if "review-placeholder-authorized" in active:
        errors.extend(_review_authorization_errors(qa_root, records))
    if "bru-registered" in active:
        errors.extend(_registered_bru_errors(qa_root, records))
    if "source-domain-rules" in active:
        errors.extend(_source_rule_errors(qa_root, records))
        if stage == "post-execution":
            errors.extend(_source_response_errors(qa_root, records, module_scoped=module is not None))
    if "manual-change-preserved" in active:
        errors.extend(_manual_change_errors(qa_root, records))
    if "registered-case-no-drift" in active:
        if module:
            errors.extend(check_module_lock(qa_root, module))
        else:
            try:
                from qa_lock import check as check_qa_lock

                errors.extend(check_qa_lock(qa_root / "contracts"))
            except (OSError, ValueError, TypeError) as exc:
                errors.append(f"cannot validate registered case drift: {exc}")
    if "qa-assets-secret-free" in active:
        targets = [
            path for path in (
                qa_root / "contracts", qa_root / "constraints", qa_root / "bruno",
                qa_root / "evidence", qa_root / "results",
            )
            if path.exists()
        ]
        errors.extend(f"secret-free constraint: {item}" for item in scan_secrets(targets))
    if "module-worker-boundary" in active or "business-code-immutable" in active:
        errors.extend(_boundary_errors(qa_root, actor, module, changed_paths))
    return list(dict.fromkeys(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("qa_root", type=Path)
    parser.add_argument("--stage", required=True, choices=("generation", "materialization", "pre-execution", "post-execution"))
    parser.add_argument("--module")
    parser.add_argument("--actor", choices=("coordinator", "module-worker"), default="coordinator")
    parser.add_argument("--changed-path", action="append", type=Path, default=[])
    parser.add_argument("--init", action="store_true")
    args = parser.parse_args()
    if args.init:
        ensure_rule_library(args.qa_root)
    errors = validate_stage(args.qa_root, args.stage, args.module, args.actor, args.changed_path or None)
    for error in errors:
        print(f"ERROR: {error}")
    if not errors:
        print(f"QA constraints passed: stage={args.stage} module={args.module or 'all'}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
