#!/usr/bin/env python3
"""Shared machine-enforced constraints for Bruno QA generation and execution."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable

sys.dont_write_bytecode = True

from .check_artifact_safety import scan as scan_secrets
from .manifest_io import first_list, load_data
from .qa_paths import (
    BRUNO,
    CONSTRAINTS,
    CONTRACTS,
    GLOBAL_EVIDENCE,
    MODULE_EVIDENCE,
    MODULE_RESULTS,
    LOGS,
    RESULTS,
)


RULES_VERSION = 2
REVIEW_RE = re.compile(r"review-[A-Za-z0-9_.-]+", re.IGNORECASE)
META_NAME_RE = re.compile(r"(?ms)^\s*meta\s*\{.*?^\s*name:\s*([^\r\n}]+).*?^\s*\}")
VARIABLE_RE = re.compile(r"\{\{([^{}]+)\}\}")
FORBIDDEN_GIT_RE = re.compile(
    r"(?:^|[;&|]\s*|\b)git\s+(?:log|show|blame|diff|rev-parse)\b",
    re.IGNORECASE,
)
ALL_STAGES = (
    "generation",
    "materialization",
    "check",
    "pre-execution",
    "run",
    "post-execution",
)
EVIDENCE_FIELDS = ("source_kind", "file", "symbol", "line", "endpoint_scope", "confidence")


MANDATORY_RULES: tuple[dict[str, Any], ...] = (
    {"id": "SRC-001", "stages": list(ALL_STAGES), "required": True},
    {"id": "RES-001", "stages": list(ALL_STAGES), "required": True},
    {"id": "MAN-001", "stages": list(ALL_STAGES), "required": True},
    {"id": "MAN-002", "stages": list(ALL_STAGES), "required": True},
    {"id": "SCN-001", "stages": list(ALL_STAGES), "required": True},
    {"id": "FILE-001", "stages": ["materialization", "pre-execution", "run"], "required": True},
    {"id": "LOGIC-001", "stages": ["generation", "materialization"], "required": True},
    {"id": "ASSERT-001", "stages": list(ALL_STAGES), "required": True},
    {"id": "MAT-001", "stages": ["materialization", "pre-execution", "run"], "required": True},
    {"id": "RUN-001", "stages": ["pre-execution", "run"], "required": True},
    {"id": "OBS-001", "stages": ["post-execution"], "required": True},
    {"id": "DESIGN-001", "stages": list(ALL_STAGES), "required": True},
)


DEFAULT_RULES: tuple[dict[str, Any], ...] = (*MANDATORY_RULES,
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
)

DEFAULT_MANUAL_CONFIRMATION = {
    "max_count": 0,
    "max_ratio": 0.0,
    "available_evidence_count": 0,
}

DATABASE_STEP_REASONS = {
    "setup": "missing_prerequisite_api",
    "assertion": "missing_response_state",
}
DATABASE_STEP_FIELDS = {
    "phase", "reason", "engine", "script", "evidence", "expected",
    "cleanup", "cleanup_verification", "id",
    "data_source", "depends_on", "estimated_records", "idempotent", "ownership",
    "precheck", "setup_verification", "transport", "dependent_case_ids", "runtime_variables",
}
MOCK_DATA_NAMESPACE_ENV = "DEV_AI_DATA_NAMESPACE"
DANGEROUS_DATABASE_RE = re.compile(
    r"(?is)\b(?:TRUNCATE|(?:CREATE|DROP)\s+(?:TABLE|DATABASE|SCHEMA)|FLUSHALL|FLUSHDB)\b|"
    r"\bDELETE\s+FROM\s+[`\"\[]?[A-Za-z0-9_.-]+[`\"\]]?\s*(?:;|$)|"
    r"\.(?:deleteMany|updateMany|remove)\s*\(\s*\{\s*\}\s*[,)]|"
    r"\bmatch_all\b"
)
ASSERTION_WRITE_RE = re.compile(
    r"(?is)\b(?:INSERT|UPDATE|DELETE|MERGE)\b|\bREPLACE\s+INTO\b|"
    r"\.(?:insertOne|insertMany|updateOne|updateMany|replaceOne|deleteOne|deleteMany|remove|"
    r"set|del|hset|hmset|index|create|delete)\s*\("
)
RELATIONAL_ENGINES = {"mysql", "mariadb", "postgresql", "postgres", "oracle", "sqlserver", "mssql"}
SQL_PARAMETER_RE = re.compile(r"\?|\$\d+|:[A-Za-z0-9_]+|@[A-Za-z_][A-Za-z0-9_]*")


def mock_data_ready_env(case_id: str) -> str:
    suffix = re.sub(r"[^A-Za-z0-9]+", "_", case_id).strip("_").upper()
    return f"DEV_AI_MOCK_DATA_READY_{suffix or 'CASE'}"


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
        else:
            expected_reason = "prerequisite_api" if phase == "setup" and step.get("transport") == "api" else DATABASE_STEP_REASONS[phase]
            if step.get("reason") != expected_reason:
                errors.append(f"{label}.reason must be {expected_reason} for phase {phase}")
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
        if phase == "assertion" and ASSERTION_WRITE_RE.search(str(step.get("script", ""))):
            errors.append(f"{label}.script must be read-only")
        if phase == "setup" and not str(step.get("cleanup", "")).strip():
            errors.append(f"{label} must declare cleanup")
        if phase == "setup":
            if step.get("idempotent") is not True:
                errors.append(f"{label}.idempotent must be true")
            estimated = step.get("estimated_records")
            if isinstance(estimated, bool) or not isinstance(estimated, int) or estimated <= 0:
                errors.append(f"{label}.estimated_records must be a positive integer")
            ownership = step.get("ownership")
            if not isinstance(ownership, dict):
                errors.append(f"{label}.ownership must declare the run-owned resource and selector")
            else:
                unknown_ownership = sorted(set(ownership) - {"namespace_env", "resource", "selector"})
                if unknown_ownership:
                    errors.append(f"{label}.ownership contains unsupported field(s): {', '.join(unknown_ownership)}")
                if ownership.get("namespace_env") != MOCK_DATA_NAMESPACE_ENV:
                    errors.append(f"{label}.ownership.namespace_env must be {MOCK_DATA_NAMESPACE_ENV}")
                if not str(ownership.get("resource", "")).strip():
                    errors.append(f"{label}.ownership.resource must name the owned table, collection, index, or keyspace")
                if not str(ownership.get("selector", "")).strip():
                    errors.append(f"{label}.ownership.selector must describe the exact namespaced selector")
            for field in ("precheck", "script", "setup_verification", "cleanup"):
                content = str(step.get(field, ""))
                if field in {"precheck", "setup_verification"} and not content.strip():
                    errors.append(f"{label}.{field} must be an executable read-only ownership check")
                    continue
                if content and MOCK_DATA_NAMESPACE_ENV not in content:
                    errors.append(f"{label}.{field} must use {MOCK_DATA_NAMESPACE_ENV}")
                if DANGEROUS_DATABASE_RE.search(content):
                    errors.append(f"{label}.{field} contains an unbounded or destructive database operation")
                if field in {"precheck", "setup_verification"} and ASSERTION_WRITE_RE.search(content):
                    errors.append(f"{label}.{field} must be read-only")
                engine = str(step.get("engine", "")).casefold()
                if engine in RELATIONAL_ENGINES and re.search(r"(?i)\b(?:INSERT|DELETE|SELECT|UPDATE|MERGE)\b", content):
                    if not SQL_PARAMETER_RE.search(content):
                        errors.append(f"{label}.{field} must use parameter binding")
                    if field == "script" and re.search(r"(?i)\b(?:UPDATE|MERGE)\b", content):
                        errors.append(f"{label}.script must not modify an existing row")
                    if field == "script" and re.search(r"(?i)\bINSERT\b", content) and not re.search(
                        r"(?is)\bINSERT\s+IGNORE\b|\bON\s+CONFLICT\b.*\bDO\s+NOTHING\b|\b(?:IF|WHERE)\s+NOT\s+EXISTS\b",
                        content,
                    ):
                        errors.append(f"{label}.script insert must reuse conflicts without updating existing rows")
            script = str(step.get("script", ""))
            engine = str(step.get("engine", "")).casefold()
            if engine in {"mongodb", "mongo"} and re.search(r"\$set\s*:", script) and "$setOnInsert" not in script:
                errors.append(f"{label}.script must use $setOnInsert instead of modifying an existing document")
            if engine in {"elasticsearch", "opensearch"} and re.search(r"\.index\s*\(", script):
                errors.append(f"{label}.script must use create semantics instead of overwriting an existing document")
            if engine == "redis" and re.search(r"\.set\s*\(", script) and not re.search(r"\bNX\b|\bnx\s*:\s*true", script):
                errors.append(f"{label}.script must use Redis NX semantics")
            verification = str(step.get("cleanup_verification", "")).strip()
            if not verification:
                errors.append(f"{label}.cleanup_verification must prove the owned data no longer exists")
            elif MOCK_DATA_NAMESPACE_ENV not in verification:
                errors.append(f"{label}.cleanup_verification must use {MOCK_DATA_NAMESPACE_ENV}")
            elif DANGEROUS_DATABASE_RE.search(verification):
                errors.append(f"{label}.cleanup_verification contains an unbounded or destructive database operation")
            elif ASSERTION_WRITE_RE.search(verification):
                errors.append(f"{label}.cleanup_verification must be read-only")
            elif (
                str(step.get("engine", "")).casefold() in RELATIONAL_ENGINES
                and re.search(r"(?i)\bSELECT\b", verification)
                and not SQL_PARAMETER_RE.search(verification)
            ):
                errors.append(f"{label}.cleanup_verification must use parameter binding")
            dependencies = step.get("depends_on", [])
            if not isinstance(dependencies, list) or any(not str(value).strip() for value in dependencies):
                errors.append(f"{label}.depends_on must be a list of setup step ids")
    return errors


def rules_path(qa_root: Path) -> Path:
    return qa_root / CONSTRAINTS / "rules.yaml"


def qa_root_for_contracts(contracts_root: Path) -> Path:
    resolved = contracts_root.resolve()
    for candidate in (resolved, *resolved.parents):
        if candidate.name == "contracts":
            return candidate.parent.parent if candidate.parent.name == "data" else candidate.parent
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
        path.write_text(_render_yaml({
            "version": RULES_VERSION,
            "manual_confirmation": dict(DEFAULT_MANUAL_CONFIRMATION),
            "rules": list(DEFAULT_RULES),
        }), encoding="utf-8")
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
    manual = document.get("manual_confirmation", {}) if isinstance(document, dict) else {}
    if manual != DEFAULT_MANUAL_CONFIRMATION:
        errors.append(
            "manual_confirmation budget must be max_count=0, max_ratio=0.0, "
            "available_evidence_count=0"
        )
    return errors


def _module_documents(qa_root: Path, module: str | None = None) -> list[tuple[str, Path, dict[str, Any], dict[str, Any]]]:
    contracts_root = qa_root / CONTRACTS
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
    available: dict[str, list[tuple[set[str], str]]] = {}
    config_path = qa_root / "execution" / "config.yaml"
    if config_path.is_file():
        try:
            from .execution_config import environment_file, load_bruno_environment, load_execution_config

            config = load_execution_config(config_path)
            environment_path = environment_file(config_path, config)
            for name, value in load_bruno_environment(environment_path).items():
                if str(value).strip():
                    available.setdefault(_normalized_field(name), []).append((set(), f"local environment variable {name}"))
        except (OSError, ValueError, TypeError):
            pass
    errors: list[str] = []
    for _, _, _, case_doc in records:
        for case in first_list(case_doc, "cases"):
            case_id = str(case.get("id", "<unknown>"))
            endpoint_id = str(case.get("endpoint_id", ""))
            for value_path, placeholder in _walk_review_values(case.get("request", {}), "$.request"):
                names = {
                    _normalized_field(placeholder.removeprefix("review-")),
                    _normalized_field(re.split(r"[.\[]", value_path)[-1].rstrip("]")),
                }
                source = next((
                    description
                    for name in names
                    for scope, description in available.get(name, [])
                    if not scope or endpoint_id in scope
                ), None)
                if source:
                    errors.append(
                        _rule_error(
                            "RES-001",
                            f"case {case_id} review placeholder {placeholder} is unauthorized because {source} provides a value",
                        )
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


def _rule_error(rule_id: str, message: str) -> str:
    return f"[{rule_id}] {message}"


def _scope_values(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value} if value.strip() else set()
    if isinstance(value, list):
        return {str(item) for item in value if str(item).strip()}
    return set()


def evidence_errors(value: Any, label: str, require_scope: bool = True) -> list[str]:
    if not isinstance(value, dict):
        return [f"{label} evidence must be an object"]
    errors: list[str] = []
    for field in EVIDENCE_FIELDS:
        if field == "endpoint_scope" and not require_scope:
            continue
        item = value.get(field)
        if field == "line":
            if not isinstance(item, int) or item < 1:
                errors.append(f"{label} evidence.line must be a positive integer")
        elif field == "endpoint_scope":
            if not _scope_values(item):
                errors.append(f"{label} evidence.endpoint_scope must name an endpoint or operation")
        elif not str(item or "").strip():
            errors.append(f"{label} evidence.{field} is required")
    if str(value.get("source_kind", "")).casefold() == "git_history":
        errors.append(f"{label} evidence uses forbidden source_kind git_history")
    return errors


def _structured_values(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _structured_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _structured_values(child)


def _source_provenance_errors(qa_root: Path) -> list[str]:
    errors: list[str] = []
    roots = [qa_root / CONSTRAINTS, qa_root / CONTRACTS]
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            if path.suffix.lower() not in {".yaml", ".yml", ".json", ".bru", ".md", ".txt"}:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="strict")
            except (OSError, UnicodeDecodeError):
                continue
            if FORBIDDEN_GIT_RE.search(text):
                errors.append(_rule_error("SRC-001", f"forbidden Git history command found in {path}"))
            if path.suffix.lower() in {".yaml", ".yml", ".json"}:
                try:
                    document = load_data(path)
                except (OSError, ValueError, TypeError):
                    continue
                if any(
                    isinstance(item, dict)
                    and str(item.get("source_kind", "")).casefold() == "git_history"
                    for item in _structured_values(document)
                ):
                    errors.append(_rule_error("SRC-001", f"git_history evidence found in {path}"))
    return errors


def _design_rule_errors(
    qa_root: Path,
    records: list[tuple[str, Path, dict[str, Any], dict[str, Any]]],
) -> list[str]:
    path = qa_root / CONSTRAINTS / "design-rules.yaml"
    retired = [
        root / name
        for root in [
            qa_root / CONSTRAINTS,
            *(directory for _, directory, _, _ in records),
        ]
        for name in ("source-rules.yaml", "source-rules.yml", "source-rules.json")
        if (root / name).is_file()
    ]
    retired_errors = [
        _rule_error("DESIGN-001", f"{item.name} is retired; business rules must come from design-rules.yaml")
        for item in retired
    ]
    if not path.is_file():
        return [*retired_errors, _rule_error("DESIGN-001", f"design-rules.yaml is missing: {path}")]
    document = load_data(path)
    if not isinstance(document, dict) or document.get("source") != "design":
        return [*retired_errors, _rule_error("DESIGN-001", "design-rules.yaml must declare source: design")]
    rules = [item for item in document.get("rules", []) if isinstance(item, dict)]
    rules_by_id = {str(item.get("id")): item for item in rules if item.get("id")}
    covered = {str(item.get("endpoint_id")) for item in rules if item.get("endpoint_id")}
    exclusions = [item for item in document.get("exclusions", []) if isinstance(item, dict)]
    def approved_exclusion(item: dict[str, Any]) -> bool:
        if "approved" in item:
            return item.get("approved") is True
        return str(item.get("status", "")).strip().casefold() == "approved"

    approved = {
        str(endpoint_id)
        for item in exclusions if approved_exclusion(item)
        for endpoint_id in (
            item.get("endpoint_ids", [])
            if isinstance(item.get("endpoint_ids"), list)
            else [item.get("endpoint_id")]
        )
        if endpoint_id
    }
    errors: list[str] = list(retired_errors)
    rule_ids = [str(item.get("id")) for item in rules if item.get("id")]
    for rule_id in sorted({item for item in rule_ids if rule_ids.count(item) > 1}):
        errors.append(_rule_error("DESIGN-001", f"design rule ID is duplicated: {rule_id}"))
    flows = [item for item in document.get("flows", []) if isinstance(item, dict)]
    flow_ids = [str(item.get("id")) for item in flows if item.get("id")]
    for flow_id in sorted({item for item in flow_ids if flow_ids.count(item) > 1}):
        errors.append(_rule_error("DESIGN-001", f"design flow ID is duplicated: {flow_id}"))
    flows_by_rule: dict[str, list[dict[str, Any]]] = {}
    for flow in flows:
        flow_id = str(flow.get("id", "<unknown>"))
        steps = flow.get("steps", []) if isinstance(flow.get("steps"), list) else []
        if flow.get("mode") != "sequential" or len(steps) < 2:
            errors.append(_rule_error("DESIGN-001", f"design flow {flow_id} is not an executable sequential flow"))
        for step in steps:
            if not isinstance(step, dict):
                errors.append(_rule_error("DESIGN-001", f"design flow {flow_id} contains an invalid step"))
                continue
            rule_id = str(step.get("rule_id", ""))
            if rule_id not in rules_by_id:
                errors.append(_rule_error("DESIGN-001", f"design flow {flow_id} references unknown rule {rule_id}"))
            else:
                flows_by_rule.setdefault(rule_id, []).append(flow)
    for rule in rules:
        rule_id = str(rule.get("id", "<unknown>"))
        if rule.get("manual_confirmation"):
            errors.append(_rule_error("DESIGN-001", f"design rule {rule_id} still requires manual confirmation"))
        for message in evidence_errors(rule.get("evidence"), f"design rule {rule_id}"):
            errors.append(_rule_error("DESIGN-001", message))
        if (
            rule.get("async") is True
            or rule.get("scenario") == "safety"
            or bool(rule.get("idempotency"))
            or bool(rule.get("retries"))
            or bool(rule.get("external_failures"))
        ) and rule_id not in flows_by_rule:
            errors.append(_rule_error("DESIGN-001", f"design rule {rule_id} has no executable design flow"))
        if rule.get("concurrency"):
            errors.append(_rule_error(
                "DESIGN-001",
                f"design rule {rule_id} requires real concurrent execution and cannot use a sequential flow",
            ))
    # Only request-shape and multipart constraints are independently provable
    # from OpenAPI.  Query/auth/authz outcomes require reviewed design rules.
    openapi_scenarios = {"validation", "file"}
    endpoint_ids = {
        str(endpoint.get("id"))
        for _, _, endpoint_doc, _ in records
        for endpoint in first_list(endpoint_doc, "endpoints")
        if endpoint.get("id")
    }
    for endpoint_id in sorted(endpoint_ids - covered - approved):
        errors.append(_rule_error("DESIGN-001", f"endpoint {endpoint_id} has no design rule or approved exclusion"))
    for _, directory, _, case_doc in records:
        for case in first_list(case_doc, "cases"):
            if str(case.get("endpoint_id")) in approved:
                continue
            if str(case.get("scenario")) in openapi_scenarios:
                if case.get("source") != "openapi" or not case.get("openapi_trace"):
                    errors.append(_rule_error("DESIGN-001", f"protocol case {case.get('id')} has no OpenAPI traceability"))
            elif case.get("source") != "design" or not case.get("design_rule_ids"):
                errors.append(_rule_error("DESIGN-001", f"business case {case.get('id')} has no design traceability"))
            else:
                for rule_id in case.get("design_rule_ids", []):
                    rule = rules_by_id.get(str(rule_id))
                    if rule is None:
                        errors.append(_rule_error(
                            "DESIGN-001", f"business case {case.get('id')} references unknown design rule {rule_id}",
                        ))
                    elif str(rule.get("endpoint_id")) != str(case.get("endpoint_id")):
                        errors.append(_rule_error(
                            "DESIGN-001",
                            f"business case {case.get('id')} references design rule {rule_id} for another endpoint",
                        ))
        logic_doc = load_data(directory / "logic.yaml") if (directory / "logic.yaml").is_file() else {}
        for logic in first_list(logic_doc, "logic"):
            if logic.get("source") != "design" or not logic.get("design_rule_id"):
                errors.append(_rule_error("DESIGN-001", f"logic {logic.get('id')} must come from design"))
            elif str(logic.get("design_rule_id")) not in rules_by_id:
                errors.append(_rule_error(
                    "DESIGN-001",
                    f"logic {logic.get('id')} references unknown design rule {logic.get('design_rule_id')}",
                ))
            if logic.get("source_candidate_id") or logic.get("observed_rule_id"):
                errors.append(_rule_error("DESIGN-001", f"logic {logic.get('id')} references non-design evidence"))
            evidence = logic.get("evidence", []) if isinstance(logic.get("evidence"), list) else []
            if any(isinstance(item, dict) and item.get("source_kind") != "design" for item in evidence):
                errors.append(_rule_error("DESIGN-001", f"logic {logic.get('id')} contains non-design evidence"))
            if logic.get("async") is True and (
                not str(logic.get("acceptance_status", "")).strip()
                or not str(logic.get("final_status", "")).strip()
            ):
                errors.append(_rule_error(
                    "DESIGN-001",
                    f"async logic {logic.get('id')} must distinguish acceptance_status and final_status",
                ))
    return errors


def _manual_confirmation_errors(
    records: list[tuple[str, Path, dict[str, Any], dict[str, Any]]],
) -> list[str]:
    errors: list[str] = []
    for _, _, _, case_doc in records:
        for case in first_list(case_doc, "cases"):
            if not needs_manual_confirmation(case):
                continue
            case_id = str(case.get("id", "<unknown>"))
            confirmation = case.get("manual_confirmation")
            if not isinstance(confirmation, dict):
                errors.append(_rule_error("MAN-001", f"case {case_id} has no manual_confirmation record"))
                continue
            if not str(confirmation.get("automation_blocker", "")).strip():
                errors.append(_rule_error("MAN-001", f"case {case_id} does not explain why it cannot be automated"))
            records_value = confirmation.get("search_records")
            if not isinstance(records_value, list) or not records_value:
                errors.append(_rule_error("MAN-001", f"case {case_id} has no complete search_records"))
                continue
            for index, evidence in enumerate(records_value):
                errors.extend(
                    _rule_error("MAN-001", message)
                    for message in evidence_errors(evidence, f"case {case_id} search_records[{index}]")
                )
    return errors


def _manual_budget_errors(
    records: list[tuple[str, Path, dict[str, Any], dict[str, Any]]],
    budget: dict[str, Any],
    post_execution: bool = False,
) -> list[str]:
    cases = [case for _, _, _, document in records for case in first_list(document, "cases")]
    manual = [case for case in cases if needs_manual_confirmation(case)]
    count = len(manual)
    ratio = count / len(cases) if cases else 0.0
    errors: list[str] = []
    if count > int(budget.get("max_count", -1)):
        errors.append(_rule_error("MAN-002", f"manual confirmation count {count} exceeds {budget.get('max_count')}"))
    if ratio > float(budget.get("max_ratio", -1)):
        errors.append(_rule_error("MAN-002", f"manual confirmation ratio {ratio:.4f} exceeds {budget.get('max_ratio')}"))
    if int(budget.get("available_evidence_count", -1)) != 0:
        errors.append(_rule_error("MAN-002", "available_evidence_count must remain 0"))
    if post_execution and count:
        errors.append(_rule_error("MAN-002", "verified status requires zero manual confirmations"))
    return errors


def _scenario_errors(records: list[tuple[str, Path, dict[str, Any], dict[str, Any]]]) -> list[str]:
    errors: list[str] = []
    for _, directory, endpoint_doc, case_doc in records:
        exclusions_path = directory / "exclusions.yaml"
        exclusions_doc = load_data(exclusions_path) if exclusions_path.is_file() else {}
        excluded_endpoint_ids = {
            str(item.get("endpoint_id"))
            for item in first_list(exclusions_doc, "exclusions")
            if item.get("endpoint_id")
            and str(item.get("status", "")).strip().casefold() == "approved"
            and str(item.get("reason", "")).strip()
        }
        cases_by_endpoint: dict[str, list[dict[str, Any]]] = {}
        for case in first_list(case_doc, "cases"):
            cases_by_endpoint.setdefault(str(case.get("endpoint_id", "")), []).append(case)
        for endpoint in first_list(endpoint_doc, "endpoints"):
            endpoint_id = str(endpoint.get("id", "<unknown>"))
            if endpoint_id in excluded_endpoint_ids:
                continue
            matrix = endpoint.get("scenario_matrix", {}) if isinstance(endpoint.get("scenario_matrix"), dict) else {}
            endpoint_cases = cases_by_endpoint.get(endpoint_id, [])
            for scenario, decision in matrix.items():
                if not isinstance(decision, dict):
                    continue
                matching = [case for case in endpoint_cases if str(case.get("scenario")) == str(scenario)]
                if decision.get("applicable") is False and matching:
                    errors.append(_rule_error(
                        "SCN-001",
                        f"endpoint {endpoint_id} scenario {scenario} is inapplicable but has cases: "
                        + ", ".join(str(case.get("id")) for case in matching),
                    ))
                if decision.get("applicable") is True and not matching:
                    errors.append(_rule_error("SCN-001", f"endpoint {endpoint_id} applicable scenario {scenario} has no case"))
    return errors


def _is_upload_endpoint(endpoint: dict[str, Any]) -> bool:
    body = endpoint.get("request_body", {}) if isinstance(endpoint.get("request_body"), dict) else {}
    content = body.get("content", {}) if isinstance(body.get("content"), dict) else {}
    if "multipart/form-data" in content:
        return True
    return any(
        isinstance(item, dict) and item.get("format") == "binary"
        for item in _structured_values(body)
    )


def _fixture_document(qa_root: Path) -> tuple[Path, dict[str, Any]]:
    path = qa_root / CONTRACTS / "fixtures" / "generated" / "manifest.yaml"
    document = load_data(path) if path.is_file() else {}
    return path, document if isinstance(document, dict) else {}


def _environment_variables(qa_root: Path) -> dict[str, str]:
    config_path = qa_root / "execution" / "config.yaml"
    if not config_path.is_file():
        return {}
    try:
        from .execution_config import environment_file, load_bruno_environment, load_execution_config

        config = load_execution_config(config_path)
        return load_bruno_environment(environment_file(config_path, config))
    except (OSError, ValueError, TypeError):
        return {}


def _file_fixture_errors(
    qa_root: Path,
    records: list[tuple[str, Path, dict[str, Any], dict[str, Any]]],
    include_consistency: bool,
) -> list[str]:
    path, document = _fixture_document(qa_root)
    fixtures = [item for item in document.get("fixtures", []) if isinstance(item, dict)]
    variables = _environment_variables(qa_root)
    errors: list[str] = []
    uploads: dict[str, dict[str, Any]] = {}
    cases: list[dict[str, Any]] = []
    for _, _, endpoint_doc, case_doc in records:
        uploads.update({
            str(endpoint.get("id")): endpoint
            for endpoint in first_list(endpoint_doc, "endpoints")
            if endpoint.get("id") and _is_upload_endpoint(endpoint)
        })
        cases.extend(first_list(case_doc, "cases"))
    if uploads and not path.is_file():
        errors.append(_rule_error("FILE-001", f"fixture manifest is missing: {path}"))
        return errors
    for endpoint_id in uploads:
        legal = [
            item for item in fixtures
            if item.get("type") == "legal" and endpoint_id in _scope_values(item.get("endpoint_scope"))
        ]
        if len(legal) != 1:
            errors.append(_rule_error("FILE-001", f"upload endpoint {endpoint_id} must have exactly one dedicated legal fixture"))
            continue
        variable = str(legal[0].get("variable", ""))
        if not variable or variable in {"UPLOAD_FILE", "FILE"}:
            errors.append(_rule_error("FILE-001", f"upload endpoint {endpoint_id} uses a generic fixture variable"))
        elif variable not in variables:
            errors.append(_rule_error("FILE-001", f"upload endpoint {endpoint_id} fixture variable {variable} is missing"))
        success_cases = [
            case for case in cases
            if str(case.get("endpoint_id")) == endpoint_id and str(case.get("scenario")) == "success"
        ]
        if not any(variable in VARIABLE_RE.findall(json.dumps(case.get("request", {}), ensure_ascii=False)) for case in success_cases):
            errors.append(_rule_error("FILE-001", f"upload endpoint {endpoint_id} success case does not use {variable}"))
    for case in cases:
        if str(case.get("scenario")) != "file":
            continue
        endpoint_id = str(case.get("endpoint_id", ""))
        if endpoint_id not in uploads:
            errors.append(_rule_error("SCN-001", f"non-upload endpoint {endpoint_id} has file case {case.get('id')}"))
    if not include_consistency:
        return errors
    for fixture in fixtures:
        label = f"fixture {fixture.get('id', '<unknown>')}"
        scope = _scope_values(fixture.get("endpoint_scope"))
        if len(scope) != 1 or not scope.issubset(uploads):
            errors.append(_rule_error("MAT-001", f"{label} must belong to exactly one upload endpoint"))
        fixture_path = qa_root / str(fixture.get("path", ""))
        if not fixture_path.is_file():
            errors.append(_rule_error("MAT-001", f"{label} file is missing: {fixture_path}"))
        elif fixture.get("sha256") != hashlib.sha256(fixture_path.read_bytes()).hexdigest():
            errors.append(_rule_error("MAT-001", f"{label} checksum does not match"))
        variable = str(fixture.get("variable", ""))
        if variable and variable not in variables:
            errors.append(_rule_error("MAT-001", f"{label} variable {variable} is missing from the active environment"))
        elif variable:
            configured_path = Path(variables[variable])
            resolved_path = configured_path.resolve() if configured_path.is_absolute() else (qa_root / BRUNO / configured_path).resolve()
            if fixture_path.resolve() != resolved_path:
                errors.append(_rule_error("MAT-001", f"{label} variable {variable} points to {resolved_path}"))
        for message in evidence_errors(fixture.get("evidence"), label):
            errors.append(_rule_error("MAT-001", message))
    fixture_keys = {
        (next(iter(_scope_values(item.get("endpoint_scope"))), ""), str(item.get("type", ""))): str(item.get("variable", ""))
        for item in fixtures
    }
    for case in cases:
        fixture_type = str(case.get("fixture_type", ""))
        if not fixture_type:
            continue
        endpoint_id = str(case.get("endpoint_id", ""))
        expected_variable = fixture_keys.get((endpoint_id, fixture_type))
        referenced = set(VARIABLE_RE.findall(json.dumps(case.get("request", {}), ensure_ascii=False)))
        if not expected_variable or expected_variable not in referenced:
            errors.append(_rule_error(
                "MAT-001",
                f"case {case.get('id')} fixture type {fixture_type} is inconsistent with the fixture manifest",
            ))
    return errors


def _logic_errors(records: list[tuple[str, Path, dict[str, Any], dict[str, Any]]]) -> list[str]:
    errors: list[str] = []
    for _, directory, _, case_doc in records:
        cases = {str(case.get("id")): case for case in first_list(case_doc, "cases") if case.get("id")}
        logic_doc = load_data(directory / "logic.yaml") if (directory / "logic.yaml").is_file() else {}
        for logic in first_list(logic_doc, "logic"):
            evidence = logic.get("evidence", []) if isinstance(logic.get("evidence"), list) else []
            if not evidence:
                errors.append(_rule_error("LOGIC-001", f"logic {logic.get('id', '<unknown>')} has no design evidence"))
            for index, item in enumerate(evidence):
                for message in evidence_errors(item, f"logic {logic.get('id', '<unknown>')} evidence[{index}]"):
                    errors.append(_rule_error("LOGIC-001", message))
            linked = [cases.get(str(case_id)) for case_id in logic.get("case_ids", [])]
            if not linked or any(case is None for case in linked):
                errors.append(_rule_error("LOGIC-001", f"logic {logic.get('id')} has unknown or missing case IDs"))
            executable = [case for case in linked if case and not needs_manual_confirmation(case)]
            if not executable:
                errors.append(_rule_error("LOGIC-001", f"design logic {logic.get('id')} has no executable case"))
    return errors


def success_assertion_errors(case: dict[str, Any]) -> list[str]:
    if str(case.get("scenario", "")).casefold() != "success":
        return []
    case_id = str(case.get("id", "<unknown>"))
    expected = case.get("expected", {}) if isinstance(case.get("expected"), dict) else {}
    assertions = [item for item in case.get("assertions", []) if isinstance(item, dict)] if isinstance(case.get("assertions"), list) else []
    exact = [item for item in assertions if "equals" in item or "eq" in item or "equals_variable" in item or "length" in item]
    business_paths = {"$.errorCode", "$.code", "$.status", "$.businessCode"}
    has_business = expected.get("business_code") is not None or any(str(item.get("path")) in business_paths for item in exact)
    envelope = business_paths | {"$.message", "$.msg", "$.errorMsg", "$"}
    has_result = any(str(item.get("path", "")) not in envelope for item in exact) or any(
        step.get("phase") == "assertion" and isinstance(step.get("expected"), dict) and step.get("expected")
        for step in database_steps(case)
    )
    errors: list[str] = []
    if not has_business:
        errors.append(f"success case {case_id} has no exact business-code assertion")
    if not has_result:
        errors.append(f"success case {case_id} has no exact result assertion")
    return errors


def _success_assertion_rule_errors(records: list[tuple[str, Path, dict[str, Any], dict[str, Any]]]) -> list[str]:
    return [
        _rule_error("ASSERT-001", error)
        for _, _, _, case_doc in records
        for case in first_list(case_doc, "cases")
        for error in success_assertion_errors(case)
    ]


def _value_resolution_errors(records: list[tuple[str, Path, dict[str, Any], dict[str, Any]]]) -> list[str]:
    errors: list[str] = []
    for _, directory, endpoint_doc, _ in records:
        path = directory / "value-resolution.yaml"
        if first_list(endpoint_doc, "endpoints") and not path.is_file():
            errors.append(_rule_error("RES-001", f"value resolution is missing: {path}"))
            continue
        document = load_data(path) if path.is_file() else {}
        for item in document.get("fields", []) if isinstance(document, dict) else []:
            if not isinstance(item, dict):
                continue
            for message in evidence_errors(item.get("evidence"), f"value resolution {item.get('field_path', '<unknown>')}"):
                errors.append(_rule_error("RES-001", message))
            if item.get("value_source") not in {"config", "fixture", "support-source"}:
                errors.append(_rule_error(
                    "RES-001",
                    f"value resolution {item.get('field_path', '<unknown>')} has forbidden source {item.get('value_source')}",
                ))
            if item.get("status") == "resolved" and str(item.get("value", "")).startswith("review-"):
                errors.append(_rule_error("RES-001", f"resolved field {item.get('field_path')} still uses review placeholder"))
    return errors


def _materialization_consistency_errors(
    qa_root: Path,
    records: list[tuple[str, Path, dict[str, Any], dict[str, Any]]],
) -> list[str]:
    errors = [_rule_error("MAT-001", item) for item in _registered_bru_errors(qa_root, records)]
    variables = _environment_variables(qa_root)
    for _, _, _, case_doc in records:
        for case in first_list(case_doc, "cases"):
            case_id = str(case.get("id", "<unknown>"))
            referenced = set(VARIABLE_RE.findall(json.dumps(case.get("request", {}), ensure_ascii=False)))
            missing = sorted(name for name in referenced if not str(variables.get(name, "")).strip())
            if missing:
                errors.append(_rule_error("MAT-001", f"case {case_id} references missing variable(s): {', '.join(missing)}"))
    errors.extend(_file_fixture_errors(qa_root, records, include_consistency=True))
    return errors


def _observed_errors(
    qa_root: Path,
    records: list[tuple[str, Path, dict[str, Any], dict[str, Any]]],
    module_scoped: bool,
) -> list[str]:
    passed: set[str] = set()
    for document in _latest_evidence_documents(qa_root, records, module_scoped):
        passed.update(str(value) for value in document.get("passed", []))
    if not passed:
        return []
    paths = [directory / "observed-rules.yaml" for _, directory, _, _ in records] if module_scoped else [qa_root / CONSTRAINTS / "observed-rules.yaml"]
    observations: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        document = load_data(path)
        for item in document.get("observations", []) if isinstance(document, dict) else []:
            if not isinstance(item, dict):
                continue
            observations[str(item.get("case_id"))] = item
            for message in evidence_errors(item.get("evidence"), f"observation {item.get('case_id', '<unknown>')}"):
                errors.append(_rule_error("OBS-001", message))
    missing = sorted(passed - set(observations))
    if missing:
        errors.append(_rule_error("OBS-001", f"successful cases have no observed evidence: {', '.join(missing)}"))
    return errors


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
    candidate = qa_root / BRUNO / directory.name
    canonical = (qa_root / CONTRACTS / "modules").is_dir()
    return candidate if canonical or candidate.is_dir() else qa_root / BRUNO


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


def _latest_evidence_documents(
    qa_root: Path,
    records: list[tuple[str, Path, dict[str, Any], dict[str, Any]]],
    module_scoped: bool = False,
) -> list[dict[str, Any]]:
    module_ids = {module_id for module_id, _, _, _ in records}
    roots: list[Path]
    if module_scoped:
        roots = [qa_root / MODULE_EVIDENCE / module_id for module_id in sorted(module_ids)]
    else:
        roots = [qa_root / GLOBAL_EVIDENCE]
    documents: list[dict[str, Any]] = []
    for root in roots:
        paths = sorted(root.glob("*-evidence.json")) if root.is_dir() else []
        if paths:
            loaded = load_data(paths[-1])
            if isinstance(loaded, dict):
                documents.append(loaded)
    return documents


def _path_marker(path: Path) -> str:
    if not path.exists():
        return "<deleted>"
    if path.is_symlink():
        return "symlink:" + str(path.readlink())
    if not path.is_file():
        return "<directory>"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _workspace_state(root: Path) -> dict[str, Any]:
    """Capture current files without consulting version-control history."""

    root = root.resolve()
    files = {
        path.relative_to(root).as_posix(): _path_marker(path)
        for path in root.rglob("*")
        if path.is_file()
        and ".git" not in path.parts
        and "__pycache__" not in path.parts
        and "/worker-assignments/" not in f"/{path.relative_to(root).as_posix()}"
    }
    return {"root": str(root), "files": dict(sorted(files.items()))}


def worker_snapshot_path(qa_root: Path, module: str) -> Path:
    records = _module_documents(qa_root.resolve(), module)
    if len(records) != 1:
        raise ValueError(f"module is not unique: {module}")
    module_id = records[0][0]
    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", module_id).strip("-") or "module"
    return qa_root.resolve() / CONTRACTS / "worker-assignments" / f"{safe_id}.yaml"


def write_worker_snapshot(qa_root: Path, module: str) -> Path:
    qa_root = qa_root.resolve()
    records = _module_documents(qa_root, module)
    if len(records) != 1:
        raise ValueError(f"module is not unique: {module}")
    path = worker_snapshot_path(qa_root, module)
    document = {
        "version": 1,
        "module": records[0][0],
        "directory": records[0][1].name,
        "baseline": _workspace_state(qa_root.parent),
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
    root = Path(str(baseline.get("root", ""))).resolve()
    if not str(baseline.get("root", "")).strip() or not root.is_dir():
        raise ValueError("module-worker filesystem snapshot has an invalid root")
    current = _workspace_state(root)
    before = baseline.get("files", {}) if isinstance(baseline.get("files"), dict) else {}
    after = current.get("files", {}) if isinstance(current.get("files"), dict) else {}
    changed = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
    return [root / name for name in changed]


def validate_worker_snapshot(qa_root: Path, module: str, stage: str) -> list[str]:
    try:
        changed = worker_changed_paths(qa_root, module)
    except (OSError, ValueError, TypeError) as exc:
        return [str(exc)]
    return validate_stage(qa_root, stage, module=module, actor="module-worker", changed_paths=changed)


def _manual_change_errors(qa_root: Path, records: list[tuple[str, Path, dict[str, Any], dict[str, Any]]]) -> list[str]:
    state_path = qa_root / CONTRACTS / "generation-state.yaml"
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
        qa_root / CONTRACTS / "modules" / directory.name,
        qa_root / BRUNO / directory.name,
        qa_root / MODULE_RESULTS / module_id,
        qa_root / MODULE_EVIDENCE / module_id,
        qa_root / LOGS / "modules" / module_id,
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
    budget = document.get("manual_confirmation", {}) if isinstance(document, dict) else {}
    if "SRC-001" in active:
        errors.extend(_source_provenance_errors(qa_root))
    if "MAN-001" in active:
        errors.extend(_manual_confirmation_errors(records))
    if "MAN-002" in active:
        errors.extend(_manual_budget_errors(records, budget, post_execution=stage == "post-execution"))
    if "SCN-001" in active:
        errors.extend(_scenario_errors(records))
    if "RES-001" in active:
        errors.extend(_review_authorization_errors(qa_root, records))
        errors.extend(_value_resolution_errors(records))
    if "FILE-001" in active:
        errors.extend(_file_fixture_errors(qa_root, records, include_consistency=False))
    if "LOGIC-001" in active:
        errors.extend(_logic_errors(records))
    if "ASSERT-001" in active:
        errors.extend(_success_assertion_rule_errors(records))
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
    if "review-placeholder-authorized" in active and "RES-001" not in active:
        errors.extend(_review_authorization_errors(qa_root, records))
    if "bru-registered" in active:
        errors.extend(_registered_bru_errors(qa_root, records))
    if "DESIGN-001" in active:
        errors.extend(_design_rule_errors(qa_root, records))
    if "MAT-001" in active:
        errors.extend(_materialization_consistency_errors(qa_root, records))
    if "OBS-001" in active:
        errors.extend(_observed_errors(qa_root, records, module_scoped=module is not None))
    if "manual-change-preserved" in active:
        errors.extend(_manual_change_errors(qa_root, records))
    if "registered-case-no-drift" in active:
        if module:
            errors.extend(check_module_lock(qa_root, module))
        else:
            try:
                from .qa_lock import check as check_qa_lock

                errors.extend(check_qa_lock(qa_root / CONTRACTS))
            except (OSError, ValueError, TypeError) as exc:
                errors.append(f"cannot validate registered case drift: {exc}")
    if "qa-assets-secret-free" in active:
        targets = [
            path for path in (
                qa_root / CONTRACTS, qa_root / CONSTRAINTS, qa_root / BRUNO,
                qa_root / RESULTS,
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
    parser.add_argument("--stage", required=True, choices=ALL_STAGES)
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
