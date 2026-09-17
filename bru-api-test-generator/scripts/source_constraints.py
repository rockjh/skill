#!/usr/bin/env python3
"""Extract reusable request/response constraints and examples from project source."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

from manifest_io import first_list, load_data
from qa_paths import CONSTRAINTS, CONTRACTS, migrate_legacy_layout


SOURCE_SUFFIXES = {".java", ".kt", ".kts", ".sql", ".properties", ".yaml", ".yml", ".json"}
FIELD_RE = re.compile(
    r"(?ms)(?P<annotations>(?:^\s*@[^\n]+\n)*)^\s*(?:private|protected|public)?\s*"
    r"(?:static\s+)?(?:final\s+)?(?P<type>[A-Za-z_$][\w$<>,.? ]*)\s+"
    r"(?P<name>[A-Za-z_$][\w$]*)\s*(?:=\s*(?P<default>[^;\n]+))?;"
)
KOTLIN_FIELD_RE = re.compile(
    r"(?ms)(?P<annotations>(?:^\s*@[^\n]+\n)*)^\s*(?:private|protected|public|internal)?\s*"
    r"(?:lateinit\s+)?(?:val|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*:\s*"
    r"(?P<type>[A-Za-z_$][\w$<>,.? ]*)(?:\s*=\s*(?P<default>[^\n]+))?$"
)
ANNOTATION_RE = re.compile(r"@(?:field:|get:)?(NotNull|NotBlank|NotEmpty|Size|Length|Pattern|Min|Max|DecimalMin|DecimalMax|Positive|PositiveOrZero|Column)\b(?:\(([^)]*)\))?")
JSON_EXAMPLE_RE = re.compile(r'["\']([A-Za-z_$][\w$]*)["\']\s*:\s*["\']([^"\']+)["\']')
SETTER_EXAMPLE_RE = re.compile(r"\.(?:set)?([A-Z][A-Za-z0-9_$]*)\s*\(\s*[\"']([^\"']+)[\"']\s*\)")
ERROR_CODE_RE = re.compile(
    r"\b([A-Z][A-Z0-9_]{2,})\s*\(\s*(?:\"([^\"]+)\"|'([^']+)'|([A-Za-z0-9_.-]+))"
)
SECRET_NAME_RE = re.compile(r"(?:password|passwd|secret|token|authorization|cookie|access.?key|private.?key)", re.IGNORECASE)
CONFIG_NAME_RE = re.compile(r"(?:application|bootstrap|config|settings)", re.IGNORECASE)
CONTROLLER_METHOD_RE = re.compile(
    r"(?ms)(?P<annotations>(?:^\s*@[^\n]+\n)+)\s*"
    r"(?:public|protected|private)?\s*(?:static\s+)?"
    r"(?P<return>[A-Za-z_$][\w$<>,.? \[\]]*)\s+"
    r"(?P<name>[A-Za-z_$][\w$]*)\s*\("
)
CONTROLLER_PARAMETERS_RE = re.compile(
    r"(?:@[\w.]+(?:\([^)]*\))?\s+)*(?:final\s+)?([A-Z][\w$<>.?]*)\s+([a-zA-Z_$][\w$]*)"
)


def _source_role(path: Path) -> str:
    name = path.name.lower()
    value = path.as_posix().lower()
    if path.suffix.lower() == ".sql" or "flyway" in value or re.search(r"/v\d+.*__", value):
        return "flyway"
    if "test" in value or name.endswith(("test.java", "tests.java", "spec.kt")):
        return "test"
    if path.suffix.lower() in {".properties", ".yaml", ".yml", ".json"}:
        return "config"
    if "controller" in name:
        return "controller"
    if "application" in name or "usecase" in name:
        return "application"
    if "service" in name:
        return "domain_service"
    if name.endswith(("dto.java", "request.java", "command.java", "output.java", "response.java", "vo.java")):
        return "dto"
    if "entity" in name or "po.java" in name or "model" in value:
        return "entity"
    if "repository" in name or "mapper" in name or "dao" in name:
        return "repository"
    if "exception" in name or "errorcode" in name or "error_code" in name:
        return "exception_enum"
    if "config" in name:
        return "config"
    return "source"


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _brace_depth(text: str, offset: int) -> int:
    prefix = re.sub(
        r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
        lambda match: " " * len(match.group(0)),
        text[:offset],
        flags=re.DOTALL,
    )
    return prefix.count("{") - prefix.count("}")


def _literal(value: str | None) -> Any:
    if value is None:
        return None
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    if text.lower() in {"true", "false"}:
        return text.lower() == "true"
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    return None


def _arg(arguments: str, name: str) -> str | None:
    named = re.search(rf"\b{re.escape(name)}\s*=\s*([^,]+)", arguments)
    if named:
        return named.group(1).strip().strip('"\'')
    if name == "value":
        positional = arguments.split(",", 1)[0].strip()
        return positional.strip('"\'') if positional else None
    return None


def _java_type(java_type: str) -> tuple[str | None, str | None]:
    compact = re.sub(r"\s+", "", java_type)
    if any(token in compact for token in ("List<", "Set<", "Collection<", "[]")):
        return "array", None
    base = re.sub(r"<.*", "", compact).removesuffix("[]").removesuffix("?")
    if base in {"Integer", "Long", "Short", "BigInteger", "int", "long", "short"}:
        return "integer", None
    if base in {"Double", "Float", "BigDecimal", "double", "float"}:
        return "number", None
    if base in {"Boolean", "boolean"}:
        return "boolean", None
    if base in {"LocalDateTime", "OffsetDateTime", "ZonedDateTime", "Instant", "Date"}:
        return "string", "date-time"
    if base == "LocalDate":
        return "string", "date"
    if base in {"String", "CharSequence", "char"}:
        return "string", None
    return None, None


def _config_entries(path: Path, text: str) -> list[tuple[str, Any, int]]:
    entries: list[tuple[str, Any, int]] = []
    if path.suffix.lower() == ".properties":
        for line_no, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "!")) or "=" not in stripped:
                continue
            key, raw = (part.strip() for part in stripped.split("=", 1))
            value: Any = _literal(raw)
            if value is None:
                placeholder = re.fullmatch(r"\$\{[^}:]+(?::([^}]+))?\}", raw)
                if placeholder and not placeholder.group(1):
                    continue
                fallback = placeholder.group(1) if placeholder else raw
                value = _literal(fallback)
                if value is None:
                    value = fallback
            entries.append((key, value, line_no))
        return entries
    if not CONFIG_NAME_RE.search(path.name):
        return entries
    try:
        document = load_data(path)
    except (OSError, ValueError, TypeError):
        return entries

    def walk(value: Any, keys: list[str]) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, [*keys, str(key)])
        elif value is not None and not isinstance(value, (list, dict)) and keys:
            if isinstance(value, str):
                placeholder = re.fullmatch(r"\$\{[^}:]+(?::([^}]+))?\}", value)
                if placeholder:
                    if not placeholder.group(1):
                        return
                    value = _literal(placeholder.group(1))
                    if value is None:
                        value = placeholder.group(1)
            entries.append((".".join(keys), value, _line(text, max(text.find(keys[-1]), 0))))

    walk(document, [])
    return entries


def _config_field_name(key: str) -> str:
    leaf = re.split(r"[.]", key)[-1]
    parts = [part for part in re.split(r"[-_]", leaf) if part]
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:]) if parts else leaf


def _merge_rule(target: dict[str, Any], constraints: dict[str, Any], evidence: dict[str, Any], example: Any = None) -> None:
    current = target.setdefault("constraints", {})
    current.update({key: value for key, value in constraints.items() if value is not None})
    target.setdefault("evidence", []).append(evidence)
    if example is not None and not SECRET_NAME_RE.search(str(target.get("field", ""))):
        target.setdefault("example", example)


def extract_source_constraints(roots: list[Path]) -> dict[str, Any]:
    fields: dict[str, dict[str, Any]] = {}
    enum_values: dict[str, list[str]] = {}
    examples: dict[str, Any] = {}
    error_codes: list[dict[str, Any]] = []
    source_inventory: dict[str, list[str]] = {}
    response_structures: list[dict[str, Any]] = []
    endpoint_response_hints: list[dict[str, Any]] = []
    for root in roots:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            if any(part.lower() in {".git", "qa", "target", "build", "node_modules", ".venv", "venv"} for part in path.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="strict")
            except (OSError, UnicodeDecodeError):
                continue
            role = _source_role(path)
            source_inventory.setdefault(role, []).append(str(path))
            if role == "config":
                for config_key, value, line_no in _config_entries(path, text):
                    if SECRET_NAME_RE.search(config_key):
                        continue
                    name = _config_field_name(config_key)
                    key = name.casefold()
                    rule = fields.setdefault(
                        key,
                        {"id": f"source-field-{key}", "field_names": [name], "field": name, "constraints": {}, "evidence": []},
                    )
                    kind = (
                        "boolean" if isinstance(value, bool)
                        else "integer" if isinstance(value, int)
                        else "number" if isinstance(value, float)
                        else "string"
                    )
                    _merge_rule(
                        rule,
                        {"type": kind, "default": value},
                        {"file": str(path), "line": line_no, "source_kind": "config", "symbol": config_key},
                    )
            for match in ERROR_CODE_RE.finditer(text):
                error_codes.append({
                    "name": match.group(1),
                    "code": next(value for value in match.groups()[1:] if value is not None),
                    "evidence": {
                        "file": str(path), "line": _line(text, match.start()), "source_kind": role,
                        "symbol": match.group(1), "confidence": "high",
                    },
                })
            for enum in re.finditer(r"(?s)\benum\s+([A-Za-z_$][\w$]*)[^\{]*\{(.*?)\}", text):
                values = re.findall(r"(?:^|,)\s*([A-Z][A-Z0-9_]*)\b", enum.group(2))
                if values:
                    enum_values[enum.group(1)] = list(dict.fromkeys(values))
            if role == "test":
                for name, value in JSON_EXAMPLE_RE.findall(text):
                    if not SECRET_NAME_RE.search(name):
                        examples.setdefault(name.casefold(), value)
                for name, value in SETTER_EXAMPLE_RE.findall(text):
                    field = name[0].lower() + name[1:]
                    if not SECRET_NAME_RE.search(field):
                        examples.setdefault(field.casefold(), value)
            if path.suffix.lower() == ".sql":
                for column in re.finditer(
                    r"(?mi)^\s*[`\"]?([A-Za-z_][\w]*)[`\"]?\s+"
                    r"(varchar|char|text|bigint|int|integer|timestamp|datetime|date)\s*(?:\((\d+)\))?"
                    r"([^,\n]*)",
                    text,
                ):
                    sql_name, sql_type, length, modifiers = column.groups()
                    camel_name = re.sub(r"_([a-z])", lambda item: item.group(1).upper(), sql_name.lower())
                    key = camel_name.casefold()
                    rule = fields.setdefault(
                        key,
                        {"id": f"source-field-{key}", "field_names": [camel_name, sql_name], "field": camel_name, "constraints": {}, "evidence": []},
                    )
                    constraints: dict[str, Any] = {
                        "type": "integer" if sql_type.lower() in {"bigint", "int", "integer"} else "string",
                    }
                    if length:
                        constraints["maxLength"] = int(length)
                    if re.search(r"\bNOT\s+NULL\b", modifiers, re.IGNORECASE):
                        constraints["required"] = True
                    if re.search(r"\bUNIQUE\b", modifiers, re.IGNORECASE):
                        constraints["unique"] = True
                    default = re.search(r"\bDEFAULT\s+([^\s,]+)", modifiers, re.IGNORECASE)
                    if default:
                        value = _literal(default.group(1))
                        if value is not None:
                            constraints["default"] = value
                    if sql_type.lower() in {"timestamp", "datetime", "date"}:
                        constraints["format"] = "date" if sql_type.lower() == "date" else "date-time"
                    _merge_rule(
                        rule,
                        constraints,
                        {"file": str(path), "line": _line(text, column.start()), "source_kind": "flyway", "symbol": sql_name},
                    )
                for unique in re.finditer(r"(?mi)\bUNIQUE\s+(?:KEY\s+[`\"]?[\w-]+[`\"]?\s*)?\(([^)]+)\)", text):
                    for raw_name in unique.group(1).split(","):
                        sql_name = raw_name.strip().strip('`"').split()[0]
                        camel_name = re.sub(r"_([a-z])", lambda item: item.group(1).upper(), sql_name.lower())
                        key = camel_name.casefold()
                        rule = fields.setdefault(
                            key,
                            {"id": f"source-field-{key}", "field_names": [camel_name, sql_name], "field": camel_name, "constraints": {}, "evidence": []},
                        )
                        _merge_rule(
                            rule,
                            {"unique": True},
                            {"file": str(path), "line": _line(text, unique.start()), "source_kind": "flyway", "symbol": sql_name},
                        )
            if path.suffix.lower() not in {".java", ".kt", ".kts"}:
                continue
            if role == "controller":
                for method in CONTROLLER_METHOD_RE.finditer(text):
                    if "Mapping" not in method.group("annotations"):
                        continue
                    closing = text.find(")", method.end())
                    parameters = text[method.end():closing] if closing >= 0 else ""
                    endpoint_response_hints.append({
                        "operation_id": method.group("name"),
                        "return_type": re.sub(r"\s+", "", method.group("return")),
                        "request_types": list(dict.fromkeys(
                            value.removesuffix("?").split("<", 1)[0]
                            for value, _ in CONTROLLER_PARAMETERS_RE.findall(parameters)
                        )),
                        "evidence": {
                            "file": str(path),
                            "line": _line(text, method.start()),
                            "source_kind": "controller",
                            "symbol": method.group("name"),
                            "confidence": "high",
                        },
                    })
            field_matches = [match for match in FIELD_RE.finditer(text) if _brace_depth(text, match.start("type")) <= 1]
            if path.suffix.lower() in {".kt", ".kts"}:
                field_matches.extend(
                    match for match in KOTLIN_FIELD_RE.finditer(text)
                    if _brace_depth(text, match.start("type")) <= 1
                )
            class_match = re.search(r"\b(?:class|record|data\s+class)\s+([A-Za-z_$][\w$]*)", text)
            owner_type = class_match.group(1) if class_match else path.stem
            structure_fields: list[str] = []
            structure_field_types: dict[str, str] = {}
            for match in field_matches:
                name = match.group("name")
                if SECRET_NAME_RE.search(name):
                    continue
                key = f"{owner_type.casefold()}::{name.casefold()}"
                rule = fields.setdefault(key, {
                    "id": f"source-field-{owner_type.casefold()}-{name.casefold()}",
                    "owner_type": owner_type,
                    "field_names": [name],
                    "field": name,
                    "constraints": {},
                    "evidence": [],
                })
                annotations = match.group("annotations") or ""
                java_type = match.group("type")
                constraints: dict[str, Any] = {}
                kind, value_format = _java_type(java_type)
                if kind:
                    constraints["type"] = kind
                if value_format:
                    constraints["format"] = value_format
                required = False
                for annotation, arguments in ANNOTATION_RE.findall(annotations):
                    if annotation in {"NotNull", "NotBlank", "NotEmpty"}:
                        required = True
                    elif annotation in {"Size", "Length"}:
                        minimum = _arg(arguments, "min")
                        maximum = _arg(arguments, "max")
                        if minimum and minimum.isdigit():
                            constraints["minLength"] = int(minimum)
                        if maximum and maximum.isdigit():
                            constraints["maxLength"] = int(maximum)
                    elif annotation == "Pattern":
                        pattern = _arg(arguments, "regexp") or _arg(arguments, "value")
                        if pattern:
                            constraints["pattern"] = pattern.replace("\\\\", "\\")
                    elif annotation in {"Min", "DecimalMin"}:
                        value = _arg(arguments, "value")
                        if value is not None:
                            constraints["minimum"] = float(value) if "." in value else int(value)
                    elif annotation in {"Max", "DecimalMax"}:
                        value = _arg(arguments, "value")
                        if value is not None:
                            constraints["maximum"] = float(value) if "." in value else int(value)
                    elif annotation == "Positive":
                        constraints["minimum"] = 1
                    elif annotation == "PositiveOrZero":
                        constraints["minimum"] = 0
                    elif annotation == "Column":
                        length = _arg(arguments, "length")
                        if length and length.isdigit():
                            constraints["maxLength"] = int(length)
                        if _arg(arguments, "nullable") == "false":
                            required = True
                        if _arg(arguments, "unique") == "true":
                            constraints["unique"] = True
                if required:
                    constraints["required"] = True
                raw_default = _literal(match.group("default"))
                if raw_default is not None:
                    constraints["default"] = raw_default
                simple_type = re.sub(r"<.*", "", java_type).strip().split()[-1]
                simple_type = simple_type.removesuffix("?")
                rule["_enum_type"] = simple_type
                if simple_type in enum_values:
                    constraints["enum"] = enum_values[simple_type]
                _merge_rule(
                    rule,
                    constraints,
                    {
                        "file": str(path), "line": _line(text, match.start()),
                        "source_kind": role, "symbol": f"{owner_type}.{name}", "confidence": "high",
                    },
                    examples.get(name.casefold()),
                )
                structure_fields.append(name)
                structure_field_types[name] = re.sub(r"\s+", "", java_type)
            if class_match and structure_fields and re.search(
                r"(?:Output|Response|Result|CommonResponse|VO)$", class_match.group(1)
            ):
                response_structures.append({
                    "id": f"source-response-{class_match.group(1).casefold()}",
                    "type_name": class_match.group(1),
                    "field_names": list(dict.fromkeys(structure_fields)),
                    "field_types": copy.deepcopy(structure_field_types),
                    "evidence": {
                        "file": str(path),
                        "line": _line(text, class_match.start()),
                        "source_kind": role,
                        "symbol": class_match.group(1),
                        "confidence": "high",
                    },
                })
    for rule in fields.values():
        enum_type = rule.pop("_enum_type", None)
        if enum_type in enum_values:
            rule.setdefault("constraints", {})["enum"] = enum_values[enum_type]
        field_key = str(rule.get("field", "")).casefold()
        if field_key in examples and "example" not in rule:
            rule["example"] = examples[field_key]
    rules = [copy.deepcopy(source_rule) for _, source_rule in sorted(fields.items())]
    response_rules = []
    for structure in response_structures:
        response_rules.append({
            **structure,
                    "fields": [
                        {
                            "name": name,
                            "source_type": structure.get("field_types", {}).get(name),
                            "constraints": copy.deepcopy(fields.get(
                                f"{str(structure.get('type_name', '')).casefold()}::{name.casefold()}", {}
                            ).get("constraints", {})),
                            "example": copy.deepcopy(fields.get(
                                f"{str(structure.get('type_name', '')).casefold()}::{name.casefold()}", {}
                            ).get("example")),
                        }
                for name in structure["field_names"]
            ],
        })
    response_type_names = {str(item["type_name"]) for item in response_rules}
    endpoint_response_rules: list[dict[str, Any]] = []
    for hint in endpoint_response_hints:
        referenced = [
            token for token in re.findall(r"[A-Za-z_$][\w$]*", str(hint["return_type"]))
            if token in response_type_names
        ]
        if not referenced:
            continue
        endpoint_response_rules.append({
            "operation_id": hint["operation_id"],
            "response_type": referenced[0],
            "type_arguments": referenced[1:],
            "evidence": hint["evidence"],
        })
    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_roots": [str(root.resolve()) for root in roots],
        "source_inventory": source_inventory,
        "field_rules": rules,
        "error_codes": error_codes,
        "response_rules": response_rules,
        "endpoint_response_rules": endpoint_response_rules,
        "controller_bindings": endpoint_response_hints,
    }


def _field_rules(document: dict[str, Any], endpoint: dict[str, Any]) -> dict[str, dict[str, Any]]:
    endpoint_id = str(endpoint.get("id", ""))
    endpoint_key = f"{str(endpoint.get('method', '')).upper()} {endpoint.get('path')}"
    operation = str(endpoint.get("operation_id", ""))
    return {
        str(name).casefold(): item
        for item in document.get("field_rules", [])
        if isinstance(item, dict)
        and (
            endpoint_id in {str(value) for value in item.get("endpoint_scope", [])}
            or endpoint_key in {str(value) for value in item.get("endpoint_scope", [])}
            or operation == str(item.get("operation", ""))
        )
        for name in item.get("field_names", [])
    }


def _apply_schema(schema: Any, rules: dict[str, dict[str, Any]], property_name: str | None = None) -> None:
    if not isinstance(schema, dict):
        return
    if property_name and property_name.casefold() in rules:
        rule = rules[property_name.casefold()]
        for key, value in rule.get("constraints", {}).items():
            if key == "required":
                continue
            schema.setdefault(key, copy.deepcopy(value))
        if rule.get("example") is not None and "example" not in schema and "default" not in schema:
            schema["example"] = copy.deepcopy(rule["example"])
        schema.setdefault("x-qa-rule-id", rule.get("id"))
        if rule.get("evidence"):
            schema.setdefault("x-source-evidence", copy.deepcopy(rule["evidence"]))
    properties = schema.get("properties", {}) if isinstance(schema.get("properties"), dict) else {}
    required = list(schema.get("required", [])) if isinstance(schema.get("required"), list) else []
    for name, child in properties.items():
        _apply_schema(child, rules, str(name))
        rule = rules.get(str(name).casefold())
        if rule and rule.get("constraints", {}).get("required") is True and name not in required:
            required.append(name)
    if required:
        schema["required"] = required
    _apply_schema(schema.get("items"), rules, property_name)


def _source_type_name(value: str | None) -> str:
    if not value:
        return ""
    tokens = re.findall(r"[A-Za-z_$][\w$]*", value)
    return tokens[-1] if tokens else ""


def _response_schema(
    type_name: str,
    response_rules: dict[str, dict[str, Any]],
    type_arguments: list[str] | None = None,
    seen: set[str] | None = None,
) -> dict[str, Any] | None:
    rule = response_rules.get(type_name)
    if not rule or type_name in (seen or set()):
        return None
    seen = {*set(seen or set()), type_name}
    properties: dict[str, Any] = {}
    required: list[str] = []
    for field in rule.get("fields", []):
        if not isinstance(field, dict) or not field.get("name"):
            continue
        name = str(field["name"])
        constraints = copy.deepcopy(field.get("constraints", {})) if isinstance(field.get("constraints"), dict) else {}
        is_required = constraints.pop("required", False) is True
        constraints.pop("unique", None)
        if field.get("example") is not None and "example" not in constraints and "default" not in constraints:
            constraints["example"] = copy.deepcopy(field["example"])
        source_type = str(field.get("source_type") or "")
        nested_type = _source_type_name(source_type)
        if nested_type in {"T", "R", "E"} and type_arguments:
            nested_type = type_arguments[0]
        nested = _response_schema(nested_type, response_rules, [], seen)
        if constraints.get("type") == "array":
            item_candidates = [
                token for token in re.findall(r"[A-Za-z_$][\w$]*", source_type)
                if token in response_rules
            ]
            item_schema = _response_schema(item_candidates[-1], response_rules, [], seen) if item_candidates else None
            if item_schema:
                constraints["items"] = item_schema
        elif nested:
            constraints = {**nested, **constraints}
        properties[name] = constraints or {"type": "object"}
        if is_required:
            required.append(name)
    if not properties:
        return None
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "x-source-evidence": copy.deepcopy(rule.get("evidence")),
    }
    if required:
        schema["required"] = required
    return schema


def _merge_schema(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if key == "properties" and isinstance(value, dict):
            properties = target.setdefault("properties", {})
            if isinstance(properties, dict):
                for name, child in value.items():
                    if name not in properties:
                        properties[name] = copy.deepcopy(child)
                    elif isinstance(properties[name], dict) and isinstance(child, dict):
                        _merge_schema(properties[name], child)
        elif key == "required" and isinstance(value, list):
            target[key] = list(dict.fromkeys([*(target.get(key, []) if isinstance(target.get(key), list) else []), *value]))
        else:
            target.setdefault(key, copy.deepcopy(value))


def apply_constraints_to_manifest(manifest: dict[str, Any], constraints: dict[str, Any]) -> None:
    response_rules = {
        str(item.get("type_name")): item
        for item in constraints.get("response_rules", [])
        if isinstance(item, dict) and item.get("type_name")
    }
    endpoint_response_rules = {
        str(item.get("operation_id")): item
        for item in constraints.get("endpoint_response_rules", [])
        if isinstance(item, dict) and item.get("operation_id") and item.get("response_type")
    }
    for endpoint in manifest.get("endpoints", []):
        if not isinstance(endpoint, dict):
            continue
        rules = _field_rules(constraints, endpoint)
        for parameter in endpoint.get("parameters", []):
            if not isinstance(parameter, dict):
                continue
            name = str(parameter.get("name", ""))
            schema = parameter.get("schema") if isinstance(parameter.get("schema"), dict) else parameter
            _apply_schema(schema, rules, name)
            rule = rules.get(name.casefold())
            if rule and rule.get("constraints", {}).get("required") is True:
                parameter["required"] = True
        body = endpoint.get("request_body")
        if isinstance(body, dict):
            content = body.get("content", {}) if isinstance(body.get("content"), dict) else {}
            if content:
                for media in content.values():
                    if isinstance(media, dict):
                        _apply_schema(media.get("schema"), rules)
            else:
                _apply_schema(body.get("schema", body), rules)
        responses = endpoint.get("responses", {}) if isinstance(endpoint.get("responses"), dict) else {}
        response_hint = endpoint_response_rules.get(str(endpoint.get("operation_id", "")))
        for status, response in responses.items():
            if not isinstance(response, dict):
                continue
            content = response.get("content", {}) if isinstance(response.get("content"), dict) else {}
            if response_hint and str(status).isdigit() and 200 <= int(status) < 300:
                inferred = _response_schema(
                    str(response_hint["response_type"]),
                    response_rules,
                    [str(item) for item in response_hint.get("type_arguments", [])],
                )
                if inferred:
                    if not content:
                        content = response.setdefault("content", {"application/json": {}})
                    media = next((item for item in content.values() if isinstance(item, dict)), None)
                    if media is not None:
                        schema = media.setdefault("schema", {})
                        if isinstance(schema, dict):
                            _merge_schema(schema, inferred)
                            schema.setdefault("x-source-evidence", copy.deepcopy(response_hint.get("evidence")))
            for media in content.values():
                if isinstance(media, dict):
                    _apply_schema(media.get("schema"), rules)


def _drop_redacted(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: cleaned
            for key, child in value.items()
            if (cleaned := _drop_redacted(child)) != "<redacted>"
        }
    if isinstance(value, list):
        return [_drop_redacted(child) for child in value]
    return value


def apply_observed_constraints(manifest: dict[str, Any], observed: dict[str, Any]) -> None:
    """Reuse successful, redacted local responses as exact response examples."""

    endpoints = {
        str(endpoint.get("id")): endpoint
        for endpoint in manifest.get("endpoints", [])
        if isinstance(endpoint, dict) and endpoint.get("id")
    }
    for item in observed.get("observations", []):
        if not isinstance(item, dict):
            continue
        endpoint = endpoints.get(str(item.get("endpoint_id", "")))
        response = item.get("response") if isinstance(item.get("response"), dict) else {}
        status = response.get("http_status")
        body = _drop_redacted(response.get("body"))
        if endpoint is None or status is None or body is None:
            continue
        responses = endpoint.get("responses", {}) if isinstance(endpoint.get("responses"), dict) else {}
        target = responses.get(str(status), responses.get(status))
        if not isinstance(target, dict):
            continue
        content = target.get("content", {}) if isinstance(target.get("content"), dict) else {}
        media = next((value for value in content.values() if isinstance(value, dict)), None)
        if media is not None:
            media.setdefault("example", body)
            media.setdefault("x-execution-evidence", item.get("evidence_file"))
        else:
            target.setdefault("example", body)
            target.setdefault("x-execution-evidence", item.get("evidence_file"))


def apply_environment_values(manifest: dict[str, Any], variables: dict[str, str]) -> None:
    """Use named local variables as request templates without persisting their values."""

    available = {
        re.sub(r"[^A-Za-z0-9]", "", str(name)).casefold(): str(name)
        for name, value in variables.items()
        if str(value).strip()
    }

    def apply_schema(schema: Any, property_name: str | None = None) -> None:
        if not isinstance(schema, dict):
            return
        normalized = re.sub(r"[^A-Za-z0-9]", "", property_name or "").casefold()
        variable = available.get(normalized)
        if variable and not any(key in schema for key in ("example", "default", "enum", "const")):
            schema["example"] = "{{" + variable + "}}"
            schema["x-local-environment"] = variable
        properties = schema.get("properties", {}) if isinstance(schema.get("properties"), dict) else {}
        for name, child in properties.items():
            apply_schema(child, str(name))
        apply_schema(schema.get("items"), property_name)

    for endpoint in manifest.get("endpoints", []):
        if not isinstance(endpoint, dict):
            continue
        for parameter in endpoint.get("parameters", []):
            if not isinstance(parameter, dict):
                continue
            schema = parameter.get("schema") if isinstance(parameter.get("schema"), dict) else parameter
            apply_schema(schema, str(parameter.get("name", "")))
        body = endpoint.get("request_body")
        if not isinstance(body, dict):
            continue
        content = body.get("content", {}) if isinstance(body.get("content"), dict) else {}
        if content:
            for media in content.values():
                if isinstance(media, dict):
                    apply_schema(media.get("schema"))
        else:
            apply_schema(body.get("schema", body))


def _schema_field_names(value: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(value, dict):
        properties = value.get("properties")
        if isinstance(properties, dict):
            names.update(str(name).casefold() for name in properties)
        for child in value.values():
            names.update(_schema_field_names(child))
    elif isinstance(value, list):
        for child in value:
            names.update(_schema_field_names(child))
    return names


def scope_source_constraints(document: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    """Bind each source field rule to endpoints before it can affect generation."""

    endpoints = [item for item in manifest.get("endpoints", []) if isinstance(item, dict)]
    bindings = [item for item in document.get("controller_bindings", []) if isinstance(item, dict)]
    scoped: list[dict[str, Any]] = []
    for original in document.get("field_rules", []):
        if not isinstance(original, dict):
            continue
        existing_scope = {
            str(value) for value in original.get("endpoint_scope", [])
            if str(value).strip()
        }
        if existing_scope:
            rule = copy.deepcopy(original)
            for evidence in rule.get("evidence", []):
                if isinstance(evidence, dict):
                    evidence.setdefault("endpoint_scope", sorted(existing_scope))
                    evidence.setdefault("confidence", "high")
            scoped.append(rule)
            continue
        names = {str(name).casefold() for name in original.get("field_names", [])}
        owner = str(original.get("owner_type", ""))
        matches: list[dict[str, Any]] = []
        if owner:
            operations = {
                str(binding.get("operation_id"))
                for binding in bindings
                if owner in {
                    *[str(value) for value in binding.get("request_types", [])],
                    *re.findall(r"[A-Za-z_$][\w$]*", str(binding.get("return_type", ""))),
                }
            }
            matches = [endpoint for endpoint in endpoints if str(endpoint.get("operation_id")) in operations]
        if not matches:
            candidates = []
            for endpoint in endpoints:
                declared = {
                    str(parameter.get("name", "")).casefold()
                    for parameter in endpoint.get("parameters", [])
                    if isinstance(parameter, dict)
                }
                declared.update(_schema_field_names(endpoint.get("request_body")))
                declared.update(_schema_field_names(endpoint.get("responses")))
                if declared & names:
                    candidates.append(endpoint)
            if owner and len(candidates) == 1:
                matches = candidates
            elif not owner:
                matches = candidates
        for endpoint in matches:
            rule = copy.deepcopy(original)
            endpoint_id = str(endpoint.get("id"))
            endpoint_key = f"{str(endpoint.get('method', '')).upper()} {endpoint.get('path')}"
            rule["id"] = (
                str(original.get("id"))
                if len(matches) == 1
                else f"{original.get('id')}-{endpoint_id.casefold()}"
            )
            rule["endpoint_scope"] = [endpoint_id]
            rule["operation"] = str(endpoint.get("operation_id") or endpoint_key)
            rule["call_chain"] = [
                str(binding.get("operation_id"))
                for binding in bindings
                if str(binding.get("operation_id")) == str(endpoint.get("operation_id"))
            ]
            for evidence in rule.get("evidence", []):
                if isinstance(evidence, dict):
                    evidence.setdefault("endpoint_scope", [endpoint_id])
                    evidence.setdefault("confidence", "high")
            scoped.append(rule)
        if not matches:
            rule = copy.deepcopy(original)
            rule.setdefault("endpoint_scope", [])
            for evidence in rule.get("evidence", []):
                if isinstance(evidence, dict):
                    evidence.setdefault("endpoint_scope", [])
                    evidence.setdefault("confidence", "low")
            scoped.append(rule)
    result = copy.deepcopy(document)
    result["field_rules"] = scoped
    all_endpoint_ids = [str(endpoint.get("id")) for endpoint in endpoints if endpoint.get("id")]
    for item in result.get("error_codes", []):
        evidence = item.get("evidence") if isinstance(item, dict) and isinstance(item.get("evidence"), dict) else None
        if evidence is not None:
            evidence.setdefault("endpoint_scope", all_endpoint_ids)
            evidence.setdefault("confidence", "medium")
    for item in result.get("response_rules", []):
        if not isinstance(item, dict):
            continue
        type_name = str(item.get("type_name", ""))
        operations = {
            str(binding.get("operation_id"))
            for binding in bindings
            if type_name in re.findall(r"[A-Za-z_$][\w$]*", str(binding.get("return_type", "")))
        }
        scope = [str(endpoint.get("id")) for endpoint in endpoints if str(endpoint.get("operation_id")) in operations]
        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else None
        if evidence is not None:
            evidence.setdefault("endpoint_scope", scope)
            evidence.setdefault("confidence", "high")
    for collection in (result.get("endpoint_response_rules", []), result.get("controller_bindings", [])):
        for item in collection if isinstance(collection, list) else []:
            if not isinstance(item, dict):
                continue
            scope = [
                str(endpoint.get("id")) for endpoint in endpoints
                if str(endpoint.get("operation_id")) == str(item.get("operation_id"))
            ]
            evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else None
            if evidence is not None:
                evidence.setdefault("endpoint_scope", scope)
                evidence.setdefault("confidence", "high")
    return result


def _walk_request_fields(value: Any, prefix: str = "request") -> list[tuple[str, Any]]:
    fields: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}"
            if isinstance(child, (dict, list)):
                fields.extend(_walk_request_fields(child, path))
            else:
                fields.append((path, child))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            path = f"{prefix}[{index}]"
            if isinstance(child, (dict, list)):
                fields.extend(_walk_request_fields(child, path))
            else:
                fields.append((path, child))
    return fields


def write_value_resolutions(qa_root: Path, source_rules: dict[str, Any]) -> list[Path]:
    """Persist endpoint-scoped request values and the evidence used to select them."""

    paths: list[Path] = []
    contracts = qa_root / CONTRACTS
    openapi = next(
        (path for path in (contracts / "openapi.json", contracts / "openapi.yaml", contracts / "openapi.yml") if path.is_file()),
        contracts / "openapi.json",
    )
    modules = contracts / "modules"
    if not modules.is_dir():
        return paths
    for directory in sorted(path for path in modules.iterdir() if path.is_dir()):
        endpoint_doc = load_data(directory / "endpoints.yaml")
        case_doc = load_data(directory / "cases.yaml") if (directory / "cases.yaml").is_file() else {}
        endpoints = {str(item.get("id")): item for item in first_list(endpoint_doc, "endpoints") if item.get("id")}
        fields: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for case in first_list(case_doc, "cases"):
            endpoint_id = str(case.get("endpoint_id", ""))
            if endpoint_id not in endpoints:
                continue
            for field_path, value in _walk_request_fields(case.get("request", {})):
                key = (endpoint_id, field_path)
                if key in seen:
                    continue
                seen.add(key)
                leaf = re.split(r"[.\[]", field_path)[-1].rstrip("]").casefold()
                rule = next((
                    item for item in source_rules.get("field_rules", [])
                    if isinstance(item, dict)
                    and endpoint_id in {str(scope) for scope in item.get("endpoint_scope", [])}
                    and leaf in {str(name).casefold() for name in item.get("field_names", [])}
                ), None)
                source_evidence = next((
                    item for item in (rule.get("evidence", []) if isinstance(rule, dict) else [])
                    if isinstance(item, dict)
                ), None)
                evidence = copy.deepcopy(source_evidence) if source_evidence else {
                    "source_kind": "openapi",
                    "file": str(openapi),
                    "symbol": f"{endpoint_id}:{field_path}",
                    "line": 1,
                    "endpoint_scope": [endpoint_id],
                    "confidence": "medium",
                }
                evidence["endpoint_scope"] = [endpoint_id]
                fields.append({
                    "endpoint_id": endpoint_id,
                    "field_path": field_path,
                    "status": "manual_confirmation" if isinstance(value, str) and value.startswith("review-") else "resolved",
                    "value": value,
                    "evidence": evidence,
                })
        output = directory / "value-resolution.yaml"
        output.write_text(
            _yaml_dump({"version": 1, "module": endpoint_doc.get("module", directory.name), "fields": fields}),
            encoding="utf-8",
        )
        paths.append(output)
    return paths


def _yaml_dump(value: Any) -> str:
    try:
        import yaml  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise ValueError("source constraints require PyYAML") from exc
    return yaml.safe_dump(value, allow_unicode=True, sort_keys=False)


def write_source_constraints(
    qa_root: Path,
    roots: list[Path],
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    document = extract_source_constraints(roots)
    if manifest is not None:
        document = scope_source_constraints(document, manifest)
    path = qa_root / CONSTRAINTS / "source-rules.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        previous = load_data(path)
        if isinstance(previous, dict):
            previous_stable = {key: value for key, value in previous.items() if key != "generated_at"}
            current_stable = {key: value for key, value in document.items() if key != "generated_at"}
            if previous_stable == current_stable:
                document["generated_at"] = previous.get("generated_at", document["generated_at"])
    rendered = _yaml_dump(document)
    if not path.is_file() or path.read_text(encoding="utf-8", errors="strict") != rendered:
        path.write_text(rendered, encoding="utf-8")
    return document


def source_rules_fingerprint(document: dict[str, Any]) -> str:
    stable = {key: value for key, value in document.items() if key != "generated_at"}
    return hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_roots", type=Path, nargs="+")
    parser.add_argument("--qa-root", type=Path, default=Path("qa"))
    parser.add_argument("--manifest", type=Path, help="optional extracted OpenAPI manifest to enrich in place")
    args = parser.parse_args()
    migrate_legacy_layout(args.qa_root)
    missing = [str(root) for root in args.source_roots if not root.is_dir()]
    if missing:
        parser.error("source root(s) do not exist: " + ", ".join(missing))
    constraints = write_source_constraints(args.qa_root, args.source_roots)
    if args.manifest:
        manifest = load_data(args.manifest)
        if not isinstance(manifest, dict):
            parser.error("manifest must contain an object")
        apply_constraints_to_manifest(manifest, constraints)
        args.manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"wrote source constraints: fields={len(constraints['field_rules'])} "
        f"error_codes={len(constraints['error_codes'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
