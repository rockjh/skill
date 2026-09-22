"""Discover and operate source-backed, run-owned API test data."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from .artifacts import write_json
from .redaction import redact
from .api_test_execution_config import (
    environment_file,
    load_bruno_environment_document,
    load_execution_config,
    render_headers_block,
    render_runtime_environment,
    resolved_environment_headers,
)
from .api_test_constraints import database_access_errors, mock_data_ready_env
from .api_test_manifest_io import first_list, load_data
from .api_test_qa_paths import BRUNO, CONSTRAINTS, CONTRACTS, EXECUTION


INVENTORY_FILE = "mock-data.yaml"
MODULE_PLAN_FILE = "mock-data.yaml"
RESULT_DIRECTORY = Path("results") / "mock-data"
RUN_NAMESPACE_ENV = "DLTK_DATA_NAMESPACE"
READY_ENV = "DLTK_MOCK_DATA_READY"
STEP_EXISTS_ENV = "DLTK_STEP_EXISTS"
SCRIPT_RESULT_MARKER = "__DLTK_MOCK_DATA_RESULT__"
PROTECTED_ENVIRONMENTS = {"prod", "production", "prd", "live"}
SAFE_ENVIRONMENTS = {
    "local", "localhost", "dev", "development", "test", "testing",
    "qa", "integration", "int", "sit", "uat",
}
ENGINE_ALIASES = {
    "postgres": "postgresql", "postgresql": "postgresql", "mysql": "mysql",
    "mariadb": "mariadb", "oracle": "oracle", "sqlserver": "sqlserver",
    "mssql": "sqlserver", "mongodb": "mongodb", "mongo": "mongodb",
    "elasticsearch": "elasticsearch", "opensearch": "opensearch", "redis": "redis",
}
DEFAULT_PORTS = {
    "mysql": 3306, "mariadb": 3306, "postgresql": 5432, "oracle": 1521,
    "sqlserver": 1433, "mongodb": 27017, "elasticsearch": 9200,
    "opensearch": 9200, "redis": 6379,
}
CONFIG_SUFFIXES = {".properties", ".yaml", ".yml", ".json", ".toml", ".env", ".xml"}
SECRET_FIELD_RE = re.compile(r"(?:password|passwd|secret|token|api.?key|credential)", re.IGNORECASE)
PLACEHOLDER_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)(?::[^}]*)?\}$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
URL_RE = re.compile(
    r"(?P<url>jdbc:oracle:thin:@[^\s\"'<>]+|"
    r"(?:jdbc:)?(?:mysql|mariadb|postgresql|oracle|sqlserver|mongodb(?:\+srv)?|redis|rediss)"
    r":(?://|@)[^\s\"'<>]+)",
    re.IGNORECASE,
)
DEPENDENCY_ENGINES = {
    "mysql-connector": "mysql", "mariadb": "mariadb", "org.postgresql": "postgresql",
    "postgresql": "postgresql", "ojdbc": "oracle", "mssql-jdbc": "sqlserver",
    "mongodb": "mongodb", "elasticsearch": "elasticsearch", "opensearch": "opensearch",
    "redis": "redis",
}
DATABASE_CONFIG_KEY_RE = re.compile(
    r"(?:^|[._-])(?:datasource|database|jdbc|mongo(?:db)?|redis|elastic(?:search)?|opensearch)(?:[._-]|$)",
    re.IGNORECASE,
)


class MockDataError(ValueError):
    """A mock-data safety, contract, or execution failure."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_name(value: str, fallback: str = "primary") -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()
    return normalized or fallback


def _env_prefix(source_id: str, engine: str) -> str:
    source = re.sub(r"[^A-Za-z0-9]+", "_", source_id).strip("_").upper()
    engine_name = re.sub(r"[^A-Za-z0-9]+", "_", engine).strip("_").upper()
    return engine_name if source_id == engine else f"{source}_{engine_name}"


def _source_evidence(path: Path, line: int, symbol: str) -> dict[str, Any]:
    return {
        "file": str(path), "line": line, "symbol": symbol,
        "source_kind": "support-source", "support_only": True,
    }


def _config_entries(path: Path) -> list[tuple[str, str, int]]:
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return []
    entries: list[tuple[str, str, int]] = []
    if path.suffix.lower() in {".properties", ".env", ".toml"}:
        for line_no, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "!", ";", "[")) or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            entries.append((key.strip(), value.strip().strip("\"'"), line_no))
        return entries
    if path.suffix.lower() == ".xml":
        for match in re.finditer(r"<(?P<key>[A-Za-z0-9_.-]+)>\s*(?P<value>[^<]+)\s*</", text):
            entries.append((match.group("key"), match.group("value").strip(), text.count("\n", 0, match.start()) + 1))
        return entries
    try:
        document = load_data(path)
    except (OSError, TypeError, ValueError):
        return []

    def walk(value: Any, keys: list[str]) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, [*keys, str(key)])
        elif value is not None and not isinstance(value, (dict, list)):
            key = ".".join(keys)
            entries.append((key, str(value), text.count("\n", 0, max(text.find(keys[-1]), 0)) + 1))

    walk(document, [])
    return entries


def _engine_from_value(key: str, value: str) -> str | None:
    lowered = f"{key} {value}".casefold()
    for alias, engine in ENGINE_ALIASES.items():
        if re.search(rf"(?:^|[^a-z]){re.escape(alias)}(?:[^a-z]|$)", lowered):
            return engine
    return None


def _data_source_id(key: str, engine: str) -> str:
    pieces = [piece for piece in re.split(r"[._-]+", key.casefold()) if piece]
    before_field = pieces[:-1]
    generic = {
        "spring", "quarkus", "micronaut", "data", "datasource", "database", "db",
        "jdbc", "hikari", "config", "services", "datastores", "connections", engine,
    }
    candidates = [piece for piece in before_field if piece not in generic]
    return _safe_name(candidates[-1] if candidates else engine)


def _connection_parts(value: str, engine: str) -> dict[str, Any]:
    raw = value.removeprefix("jdbc:")
    if engine == "oracle" and raw.startswith("oracle:thin:@"):
        host_port_db = raw.split("@", 1)[1]
        match = re.match(r"(?://)?([^:/]+)(?::(\d+))?(?:[/:]([^?]+))?", host_port_db)
        return {
            key: item for key, item in {
                "host": match.group(1) if match else None,
                "port": int(match.group(2)) if match and match.group(2) else DEFAULT_PORTS[engine],
                "database": match.group(3) if match else None,
            }.items() if item not in {None, ""}
        }
    if engine == "sqlserver":
        match = re.match(r"sqlserver://([^:;]+)(?::(\d+))?(.*)$", raw, re.IGNORECASE)
        options = match.group(3) if match else ""
        database = re.search(r"(?i);(?:databaseName|database)=([^;]+)", options)
        return {
            key: item for key, item in {
                "host": match.group(1) if match else None,
                "port": int(match.group(2)) if match and match.group(2) else DEFAULT_PORTS[engine],
                "database": database.group(1) if database else None,
            }.items() if item not in {None, ""}
        }
    parsed = urllib.parse.urlsplit(raw)
    database = parsed.path.lstrip("/").split("/", 1)[0] if parsed.path else ""
    return {
        key: item for key, item in {
            "host": parsed.hostname,
            "port": parsed.port or DEFAULT_PORTS.get(engine),
            "database": database,
            "scheme": parsed.scheme or None,
            "ssl": urllib.parse.parse_qs(parsed.query).get("ssl", [None])[0],
        }.items() if item not in {None, ""}
    }


def discover_data_sources(roots: list[Path]) -> list[dict[str, Any]]:
    """Find configured database technologies without retaining credential values."""

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    dependency_hits: dict[str, Path] = {}
    files = [
        path for root in roots for path in sorted(root.rglob("*"))
        if path.is_file() and not any(
            part.casefold() in {".git", "qa", "build", "dist", "target", "node_modules", ".venv", "venv"}
            for part in path.parts
        )
    ]
    for path in files:
        if path.name not in {"pom.xml", "build.gradle", "build.gradle.kts", "package.json", "requirements.txt"}:
            continue
        try:
            dependency_text = path.read_text(encoding="utf-8", errors="strict").casefold()
        except (OSError, UnicodeDecodeError):
            dependency_text = ""
        for marker, engine in DEPENDENCY_ENGINES.items():
            if marker in dependency_text:
                dependency_hits.setdefault(engine, path)
    fallback_engine = next(iter(dependency_hits), None) if len(dependency_hits) == 1 else None
    for path in files:
        if path.suffix.lower() not in CONFIG_SUFFIXES:
            continue
        entries = _config_entries(path)
        contexts = [
            (key.rsplit(".", 1)[0].casefold(), engine)
            for key, value, _line_no in entries
            if (engine := _engine_from_value(key, value)) is not None
        ]
        for key, value, line_no in entries:
            engine = _engine_from_value(key, value)
            if not engine:
                key_prefix = key.rsplit(".", 1)[0].casefold()
                matches = [
                    (prefix, candidate) for prefix, candidate in contexts
                    if key_prefix == prefix or key_prefix.startswith(prefix + ".") or prefix.startswith(key_prefix + ".")
                ]
                engine = max(matches, key=lambda item: len(item[0]))[1] if matches else None
            if not engine and fallback_engine and DATABASE_CONFIG_KEY_RE.search(key):
                engine = fallback_engine
            if not engine:
                continue
            pieces = [piece for piece in re.split(r"[._-]+", key.casefold()) if piece]
            field = pieces[-1] if pieces else "url"
            source_id = _data_source_id(key, engine)
            item = grouped.setdefault((source_id, engine), {
                "id": source_id,
                "engine": engine,
                "settings": {},
                "setting_envs": {},
                "credentials": {},
                "evidence": [],
            })
            item["evidence"].append(_source_evidence(path, line_no, key))
            placeholder = PLACEHOLDER_RE.fullmatch(value)
            prefix = _env_prefix(source_id, engine)
            if SECRET_FIELD_RE.search(field) or field in {"username", "user"}:
                name = placeholder.group(1) if placeholder else f"{prefix}_{'USER' if field in {'username', 'user'} else 'PASSWORD'}"
                item["credentials"]["username" if field in {"username", "user"} else "password"] = {"env": name}
                continue
            if field in {"url", "uri", "uris", "connectionstring", "connection_string"}:
                if placeholder:
                    item["setting_envs"]["connection_url"] = placeholder.group(1)
                    continue
                match = URL_RE.search(value)
                if match:
                    try:
                        item["settings"].update(_connection_parts(match.group("url"), engine))
                        if re.search(r"://[^/@\s]+:[^/@\s]+@", match.group("url")):
                            item["credentials"].setdefault("username", {"env": f"{prefix}_USER"})
                            item["credentials"].setdefault("password", {"env": f"{prefix}_PASSWORD"})
                    except ValueError:
                        item["configuration_status"] = "unparsed_connection"
                elif engine in {"elasticsearch", "opensearch"}:
                    try:
                        parsed = urllib.parse.urlsplit(value.split(",", 1)[0])
                        if parsed.hostname:
                            item["settings"].update({
                                "host": parsed.hostname,
                                "port": parsed.port or DEFAULT_PORTS[engine],
                                "scheme": parsed.scheme or "http",
                            })
                    except ValueError:
                        item["configuration_status"] = "unparsed_connection"
                continue
            normalized_field = {
                "dbname": "database", "database-name": "database", "schema-name": "schema",
                "timezone": "timezone", "sslmode": "ssl",
            }.get(field, field)
            if normalized_field in {"host", "port", "database", "schema", "index", "scheme", "ssl", "timezone"}:
                if placeholder:
                    item["setting_envs"][normalized_field] = placeholder.group(1)
                else:
                    item["settings"][normalized_field] = int(value) if normalized_field == "port" and value.isdigit() else value
    configured_engines = {engine for _, engine in grouped}
    for engine, path in dependency_hits.items():
        if engine in configured_engines:
            continue
        source_id = engine
        grouped[(source_id, engine)] = {
            "id": source_id,
            "engine": engine,
            "settings": {"port": DEFAULT_PORTS[engine]},
            "credentials": {},
            "evidence": [_source_evidence(path, 1, path.name)],
            "configuration_status": "dependency_only",
        }
    result: list[dict[str, Any]] = []
    for (_, engine), item in sorted(grouped.items()):
        prefix = _env_prefix(str(item["id"]), engine)
        settings = item["settings"]
        setting_envs = item["setting_envs"]
        environment: dict[str, dict[str, Any]] = {}
        for field in ("host", "port", "database", "schema", "index", "scheme", "timezone", "ssl"):
            if field in settings:
                environment[field] = {"env": f"{prefix}_{field.upper()}", "default": settings[field]}
            elif field in setting_envs:
                environment[field] = {"env": setting_envs[field]}
        if "connection_url" in setting_envs:
            environment["connection_url"] = {"env": setting_envs["connection_url"]}
        for field, reference in item["credentials"].items():
            environment[field] = reference
        if "password" not in environment:
            environment["password"] = {"env": f"{prefix}_PASSWORD"}
        item["environment"] = environment
        item.pop("settings", None)
        item.pop("setting_envs", None)
        item.pop("credentials", None)
        item["evidence"] = list({json.dumps(value, sort_keys=True): value for value in item["evidence"]}.values())
        result.append(item)
    return result


def _split_columns(body: str) -> list[str]:
    result: list[str] = []
    start = depth = 0
    for index, char in enumerate(body):
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            result.append(body[start:index].strip())
            start = index + 1
    result.append(body[start:].strip())
    return [value for value in result if value]


def discover_entities(roots: list[Path]) -> list[dict[str, Any]]:
    """Extract only explicit DDL relationships and constraints."""

    entities: list[dict[str, Any]] = []
    create_re = re.compile(
        r"(?is)\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>[`\"\[]?[A-Za-z0-9_.-]+[`\"\]]?)\s*\((?P<body>.*?)\)\s*;"
    )
    for root in roots:
        for path in sorted(root.rglob("*.sql")):
            if any(part.casefold() in {".git", "qa", "build", "dist", "target"} for part in path.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="strict")
            except (OSError, UnicodeDecodeError):
                continue
            for match in create_re.finditer(text):
                name = match.group("name").strip("`\"[]")
                columns: list[dict[str, Any]] = []
                references: list[dict[str, str]] = []
                unique_keys: list[list[str]] = []
                primary_key: list[str] = []
                for definition in _split_columns(match.group("body")):
                    normalized = " ".join(definition.split())
                    foreign_key = re.match(
                        r"(?is)(?:CONSTRAINT\s+\S+\s+)?FOREIGN\s+KEY\s*\(([^)]+)\)\s*"
                        r"REFERENCES\s+([`\"\[]?[\w.-]+[`\"\]]?)\s*\(([^)]+)\)",
                        normalized,
                    )
                    if foreign_key:
                        local_fields = [value.strip().strip("`\"[]") for value in foreign_key.group(1).split(",")]
                        target_fields = [value.strip().strip("`\"[]") for value in foreign_key.group(3).split(",")]
                        references.extend({
                            "field": local,
                            "entity": foreign_key.group(2).strip("`\"[]"),
                            "target_field": target,
                        } for local, target in zip(local_fields, target_fields))
                        continue
                    constraint = re.match(r"(?is)(?:CONSTRAINT\s+\S+\s+)?(PRIMARY\s+KEY|UNIQUE)\s*\(([^)]+)\)", normalized)
                    if constraint:
                        fields = [field.strip().strip("`\"[]") for field in constraint.group(2).split(",")]
                        if constraint.group(1).upper().startswith("PRIMARY"):
                            primary_key = fields
                        else:
                            unique_keys.append(fields)
                        continue
                    column = re.match(r"(?is)^([`\"\[]?[A-Za-z_][\w$-]*[`\"\]]?)\s+([^\s,]+)(.*)$", normalized)
                    if not column:
                        continue
                    column_name = column.group(1).strip("`\"[]")
                    tail = column.group(3)
                    default = re.search(r"(?i)\bDEFAULT\s+([^\s,]+)", tail)
                    field: dict[str, Any] = {
                        "name": column_name,
                        "type": column.group(2),
                        "required": "NOT NULL" in tail.upper(),
                        **({"default": default.group(1)} if default else {}),
                    }
                    if re.search(r"(?i)\b(?:AUTO_INCREMENT|IDENTITY|GENERATED\b.*\bIDENTITY)\b", tail) or re.search(
                        r"(?i)\b(?:BIGSERIAL|SERIAL|SMALLSERIAL)\b", column.group(2)
                    ):
                        field["generated"] = True
                    length = re.search(r"(?i)\b(?:VAR)?CHAR\s*\(\s*(\d+)\s*\)", column.group(2))
                    if length:
                        field["max_length"] = int(length.group(1))
                    columns.append(field)
                    reference = re.search(r"(?i)\bREFERENCES\s+([`\"\[]?[\w.-]+[`\"\]]?)\s*\(([^)]+)\)", tail)
                    if reference:
                        references.append({
                            "field": column_name,
                            "entity": reference.group(1).strip("`\"[]"),
                            "target_field": reference.group(2).strip().strip("`\"[]"),
                        })
                    if "PRIMARY KEY" in tail.upper():
                        primary_key.append(column_name)
                    if re.search(r"(?i)\bUNIQUE\b", tail):
                        unique_keys.append([column_name])
                logical_delete = next((
                    column["name"] for column in columns
                    if column["name"].casefold() in {"deleted", "is_deleted", "deleted_at", "delete_flag", "del_flag"}
                ), None)
                for field in columns:
                    check = re.search(
                        rf"(?is)\bCHECK\s*\(\s*[`\"\[]?{re.escape(str(field['name']))}[`\"\]]?\s+IN\s*\(([^)]+)\)\s*\)",
                        match.group("body"),
                    )
                    if check:
                        field["enum"] = [value.strip().strip("\"'") for value in check.group(1).split(",")]
                entities.append({
                    "name": name,
                    "source_type": "ddl",
                    "fields": columns,
                    "primary_key": list(dict.fromkeys(primary_key)),
                    "unique_keys": unique_keys,
                    "references": references,
                    **({"logical_delete_field": logical_delete} if logical_delete else {}),
                    "evidence": [_source_evidence(path, text.count("\n", 0, match.start()) + 1, name)],
                })
    known = {str(entity["name"]).casefold() for entity in entities}
    annotated_re = re.compile(
        r"(?s)(?P<annotations>(?:\s*@(?:Entity|Table|Document|RedisHash)[^\n]*\n)+)"
        r"\s*(?:public\s+|internal\s+|data\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)[^\{]*\{(?P<body>.*?)\n\}"
    )
    field_re = re.compile(
        r"(?ms)(?P<annotations>(?:^\s*@[^\n]+\n)*)^\s*"
        r"(?:private|protected|public|internal)?\s*(?:lateinit\s+)?(?:val\s+|var\s+)?"
        r"(?:(?P<type1>[A-Za-z_$][\w$<>,.? ]*)\s+(?P<name1>[a-zA-Z_$][\w$]*)|"
        r"(?P<name2>[a-zA-Z_$][\w$]*)\s*:\s*(?P<type2>[A-Za-z_$][\w$<>,.? ]*))"
        r"\s*(?:=\s*(?P<default>[^;\n]+))?;?$"
    )
    for root in roots:
        for path in sorted((*root.rglob("*.java"), *root.rglob("*.kt"), *root.rglob("*.kts"))):
            if any(part.casefold() in {".git", "qa", "build", "dist", "target"} for part in path.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="strict")
            except (OSError, UnicodeDecodeError):
                continue
            enum_values = {
                match.group(1): re.findall(r"(?:^|,)\s*([A-Z][A-Z0-9_]*)\b", match.group(2))
                for match in re.finditer(r"(?s)\benum\s+([A-Za-z_$][\w$]*)[^\{]*\{(.*?)\}", text)
            }
            for match in annotated_re.finditer(text):
                annotations = match.group("annotations")
                annotation = re.search(r"@(Entity|Table|Document|RedisHash)\b", annotations)
                annotation_kind = annotation.group(1) if annotation else "Entity"
                storage_engine = (
                    "redis" if annotation_kind == "RedisHash"
                    else "mongodb" if annotation_kind == "Document" and "mongodb" in text.casefold()
                    else "opensearch" if annotation_kind == "Document" and "opensearch" in text.casefold()
                    else "elasticsearch" if annotation_kind == "Document" and "elasticsearch" in text.casefold()
                    else "relational" if annotation_kind in {"Entity", "Table"}
                    else "document"
                )
                configured = re.search(
                    r"@(?:Table|Document|RedisHash)\s*\([^)]*(?:name|collection|indexName|value)\s*=\s*[\"']([^\"']+)",
                    annotations,
                )
                name = configured.group(1) if configured else match.group("name")
                if name.casefold() in known:
                    continue
                fields: list[dict[str, Any]] = []
                primary_key: list[str] = []
                unique_keys: list[list[str]] = []
                references: list[dict[str, str]] = []
                for field_match in field_re.finditer(match.group("body")):
                    field_annotations = field_match.group("annotations") or ""
                    field_name = field_match.group("name1") or field_match.group("name2")
                    field_type = (field_match.group("type1") or field_match.group("type2") or "").strip()
                    column_name = re.search(r"@(?:Column|JoinColumn)\s*\([^)]*name\s*=\s*[\"']([^\"']+)", field_annotations)
                    persisted_name = column_name.group(1) if column_name else field_name
                    required = bool(
                        re.search(r"@(NotNull|NotBlank|NotEmpty)\b", field_annotations)
                        or re.search(r"nullable\s*=\s*false", field_annotations, re.IGNORECASE)
                        or (path.suffix.lower() in {".kt", ".kts"} and not field_type.endswith("?"))
                    )
                    field: dict[str, Any] = {"name": persisted_name, "type": field_type, "required": required}
                    if re.search(r"@GeneratedValue\b", field_annotations):
                        field["generated"] = True
                    default = field_match.group("default")
                    if default and not re.search(r"\b(?:null|None)\b", default):
                        field["default"] = default.strip()
                    enum_name = re.sub(r"[?<>].*", "", field_type).strip()
                    if enum_values.get(enum_name):
                        field["enum"] = enum_values[enum_name]
                    size_min = re.search(r"@Size\s*\([^)]*\bmin\s*=\s*(\d+)", field_annotations)
                    size_max = re.search(r"@Size\s*\([^)]*\bmax\s*=\s*(\d+)", field_annotations)
                    minimum = re.search(r"@(?:Min|DecimalMin)\s*\(\s*(?:value\s*=\s*)?[\"']?(-?\d+)", field_annotations)
                    maximum = re.search(r"@(?:Max|DecimalMax)\s*\(\s*(?:value\s*=\s*)?[\"']?(-?\d+)", field_annotations)
                    pattern = re.search(r"@Pattern\s*\([^)]*\bregexp\s*=\s*[\"']([^\"']+)", field_annotations)
                    if size_min:
                        field["min_length"] = int(size_min.group(1))
                    if size_max:
                        field["max_length"] = int(size_max.group(1))
                    if minimum:
                        field["minimum"] = int(minimum.group(1))
                    elif re.search(r"@Positive\b", field_annotations):
                        field["minimum"] = 1
                    if maximum:
                        field["maximum"] = int(maximum.group(1))
                    if pattern:
                        field["pattern"] = pattern.group(1)
                    fields.append(field)
                    if re.search(r"@(Id|EmbeddedId)\b", field_annotations):
                        primary_key.append(persisted_name)
                    if re.search(r"\bunique\s*=\s*true", field_annotations, re.IGNORECASE):
                        unique_keys.append([persisted_name])
                    if re.search(r"@(ManyToOne|OneToOne|DBRef)\b", field_annotations):
                        target = re.sub(r"[?<>].*", "", field_type).strip()
                        references.append({"field": persisted_name, "entity": target, "target_field": "id"})
                logical_delete = next((
                    field["name"] for field in fields
                    if str(field["name"]).casefold() in {"deleted", "is_deleted", "deleted_at", "delete_flag", "del_flag"}
                ), None)
                entities.append({
                    "name": name,
                    "source_type": "annotated_entity",
                    "class_name": match.group("name"),
                    "storage_kind": annotation_kind,
                    "storage_engine": storage_engine,
                    "fields": fields,
                    "required_fields": [field["name"] for field in fields if field.get("required") and "default" not in field],
                    "primary_key": primary_key,
                    "unique_keys": unique_keys,
                    "references": references,
                    **({"logical_delete_field": logical_delete} if logical_delete else {}),
                    "evidence": [_source_evidence(path, text.count("\n", 0, match.start()) + 1, match.group("name"))],
                })
                known.add(name.casefold())
    for entity in entities:
        entity.setdefault(
            "required_fields",
            [field["name"] for field in entity.get("fields", []) if field.get("required") and "default" not in field],
        )
    return entities


def entity_creation_order(entities: list[dict[str, Any]]) -> list[str]:
    """Return a deterministic parent-before-child order using explicit references only."""

    names = {str(entity.get("name")): entity for entity in entities if entity.get("name")}
    pending = dict(names)
    ordered: list[str] = []
    while pending:
        ready = [
            name for name, entity in pending.items()
            if {
                str(reference.get("entity")) for reference in entity.get("references", [])
                if isinstance(reference, dict) and str(reference.get("entity")) in names
            } <= set(ordered)
        ]
        if not ready:
            ordered.extend(sorted(pending))
            break
        for name in sorted(ready):
            ordered.append(name)
            pending.pop(name)
    return ordered


def _yaml_write(path: Path, value: Any) -> Path:
    try:
        import yaml  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise MockDataError("mock-data discovery requires PyYAML") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
    if not path.is_file() or path.read_text(encoding="utf-8", errors="strict") != rendered:
        path.write_text(rendered, encoding="utf-8")
    return path


def write_discovery(qa_root: Path, roots: list[Path]) -> Path:
    sources = discover_data_sources(roots)
    entities = discover_entities(roots)
    path = qa_root / CONSTRAINTS / INVENTORY_FILE
    previous = load_data(path) if path.is_file() else {}
    generated_at = previous.get("generated_at") if isinstance(previous, dict) else None
    stable = {
        "version": 1,
        "source_roots": [str(root.resolve()) for root in roots],
        "data_sources": sources,
        "entities": entities,
        "creation_order": entity_creation_order(entities),
    }
    previous_stable = {key: value for key, value in previous.items() if key != "generated_at"} if isinstance(previous, dict) else {}
    stable["generated_at"] = generated_at if previous_stable == stable and generated_at else _utc_now()
    _yaml_write(path, stable)
    if (qa_root / EXECUTION / "config.yaml").is_file():
        _merge_environment_defaults(qa_root, sources)
    return path


def _merge_environment_defaults(qa_root: Path, sources: list[dict[str, Any]]) -> None:
    config_path = qa_root / EXECUTION / "config.yaml"
    config = load_execution_config(config_path)
    path = environment_file(config_path, config)
    document = load_bruno_environment_document(path)
    changed = False
    for source in sources:
        environment = source.get("environment", {})
        for reference in environment.values() if isinstance(environment, dict) else []:
            if not isinstance(reference, dict) or not reference.get("env"):
                continue
            name = str(reference["env"])
            if name not in document["vars"]:
                document["vars"][name] = str(reference.get("default", ""))
                changed = True
    if changed:
        path.write_text(render_runtime_environment(document) + render_headers_block(document), encoding="utf-8")


def load_inventory(qa_root: Path) -> dict[str, Any]:
    path = qa_root / CONSTRAINTS / INVENTORY_FILE
    if not path.is_file():
        return {"version": 1, "data_sources": [], "entities": []}
    value = load_data(path)
    if not isinstance(value, dict):
        raise MockDataError(f"mock-data inventory must contain an object: {path}")
    return value


def _identifier(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).casefold())


def _endpoint_parameter(endpoint: dict[str, Any]) -> str | None:
    parameters = [
        str(item.get("name")) for item in endpoint.get("parameters", [])
        if isinstance(item, dict) and item.get("in") == "path" and item.get("name")
    ]
    parameters.extend(re.findall(r"\{([^{}]+)\}", str(endpoint.get("path", ""))))
    unique = list(dict.fromkeys(parameters))
    return unique[0] if len(unique) == 1 else None


def _endpoint_entity(
    endpoint: dict[str, Any],
    entities: list[dict[str, Any]],
) -> dict[str, Any] | None:
    path_tokens = {_identifier(value) for value in str(endpoint.get("path", "")).split("/") if "{" not in value}
    matched = []
    for entity in entities:
        aliases = {
            _identifier(entity.get("name")),
            _identifier(entity.get("class_name")),
        } - {""}
        if aliases & path_tokens:
            matched.append(entity)
    return matched[0] if len(matched) == 1 else None


def _set_nested_identifier(body: Any, name: str, value: str) -> bool:
    if not isinstance(body, dict):
        return False
    matches: list[dict[str, Any]] = []

    def walk(current: Any) -> None:
        if isinstance(current, dict):
            for key, child in current.items():
                if _identifier(key) == _identifier(name):
                    matches.append(current)
                else:
                    walk(child)
        elif isinstance(current, list):
            for child in current:
                walk(child)

    walk(body)
    if len(matches) != 1:
        return False
    key = next(key for key in matches[0] if _identifier(key) == _identifier(name))
    matches[0][key] = value
    return True


def _api_script_prelude() -> str:
    return r"""const namespace = bru.getEnvVar('DLTK_DATA_NAMESPACE');
const id = bru.getEnvVar('__ID_ENV__');
const baseUrl = bru.getEnvVar('baseUrl') || bru.getEnvVar('BASE_URL');
const headers = JSON.parse(bru.getEnvVar('DLTK_MOCK_DATA_HEADERS') || '{}');
const expand = (value) => typeof value === 'string'
  ? value.replace(/\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}/g, (_, name) => bru.getEnvVar(name) || '')
  : Array.isArray(value) ? value.map(expand)
  : value && typeof value === 'object' ? Object.fromEntries(Object.entries(value).map(([key, item]) => [key, expand(item)]))
  : value;
const target = (template) => new URL(template.replace('{__ID__}', encodeURIComponent(id)), baseUrl.replace(/\/$/, '') + '/');
"""


def _api_step(
    module_id: str,
    entity: dict[str, Any],
    cases: list[dict[str, Any]],
    create_endpoint: dict[str, Any],
    read_endpoint: dict[str, Any],
    delete_endpoint: dict[str, Any],
    create_case: dict[str, Any],
    id_field: str,
) -> dict[str, Any] | None:
    body = copy.deepcopy(create_case.get("request", {}).get("body")) if isinstance(create_case.get("request"), dict) else None
    variable = f"DLTK_DATA_{re.sub(r'[^A-Za-z0-9]+', '_', str(entity['name'])).strip('_').upper()}_ID"
    id_field_contract = next(
        (value for value in entity.get("fields", []) if isinstance(value, dict) and str(value.get("name")) == id_field),
        {"name": id_field, "type": "string"},
    )
    if id_field_contract.get("generated") is True:
        return None
    if not _set_nested_identifier(body, id_field, "{{" + variable + "}}"):
        return None
    dependent_case_ids = [str(case["id"]) for case in cases]
    path = str(read_endpoint.get("path", "")).replace("{" + str(_endpoint_parameter(read_endpoint)) + "}", "{__ID__}")
    delete_path = str(delete_endpoint.get("path", "")).replace(
        "{" + str(_endpoint_parameter(delete_endpoint)) + "}", "{__ID__}",
    )
    request = create_case.get("request", {}) if isinstance(create_case.get("request"), dict) else {}
    create_path = str(request.get("path") or create_endpoint.get("path") or "/")
    content_type = str(request.get("content_type") or request.get("body_type") or "application/json")
    prelude = _api_script_prelude().replace("__ID_ENV__", variable)
    precheck = prelude + f"""const response = await fetch(target({json.dumps(path)}), {{headers}});
if (response.status === 404) bru.setVar('{STEP_EXISTS_ENV}', 'false');
else if (response.ok) bru.setVar('{STEP_EXISTS_ENV}', 'true');
else throw new Error(`mock-data precheck returned HTTP ${{response.status}}`);"""
    setup = prelude + f"""const body = expand({json.dumps(body, ensure_ascii=False)});
const response = await fetch(target({json.dumps(create_path)}), {{
  method: 'POST', headers: {{...headers, 'Content-Type': {json.dumps(content_type)}}}, body: JSON.stringify(body)
}});
if (!response.ok) throw new Error(`mock-data API setup returned HTTP ${{response.status}}`);
const responseText = await response.text();
let responseBody;
try {{ responseBody = responseText ? JSON.parse(responseText) : undefined; }} catch {{ responseBody = undefined; }}
const identifiers = [];
const collectIdentifiers = (value) => {{
  if (Array.isArray(value)) value.forEach(collectIdentifiers);
  else if (value && typeof value === 'object') Object.entries(value).forEach(([key, item]) => {{
    if (key.toLowerCase() === {json.dumps(id_field.casefold())} && ['string', 'number'].includes(typeof item)) identifiers.push(String(item));
    else collectIdentifiers(item);
  }});
}};
collectIdentifiers(responseBody);
const location = response.headers.get('location');
if (identifiers.length === 1) bru.setVar({json.dumps(variable)}, identifiers[0]);
else if (location) bru.setVar({json.dumps(variable)}, decodeURIComponent(location.replace(/\\/$/, '').split('/').pop()));"""
    setup_verification = prelude + f"""const response = await fetch(target({json.dumps(path)}), {{headers}});
if (!response.ok) throw new Error(`mock-data setup verification returned HTTP ${{response.status}}`);"""
    cleanup_script = prelude + f"""const response = await fetch(target({json.dumps(delete_path)}), {{method: 'DELETE', headers}});
if (!response.ok && response.status !== 404) throw new Error(`mock-data API cleanup returned HTTP ${{response.status}}`);"""
    cleanup_verification = prelude + f"""const response = await fetch(target({json.dumps(path)}), {{headers}});
if (response.status !== 404) throw new Error(`mock-data cleanup verification returned HTTP ${{response.status}}`);"""
    evidence = [
        *entity.get("evidence", []),
        create_endpoint.get("evidence", {}), read_endpoint.get("evidence", {}), delete_endpoint.get("evidence", {}),
    ]
    return {
        "id": f"source-api-{module_id}-{_safe_name(str(entity['name']))}",
        "phase": "setup",
        "reason": "prerequisite_api",
        "transport": "api",
        "engine": "http",
        "data_source": "public-api",
        "dependent_case_ids": dependent_case_ids,
        "estimated_records": 1,
        "idempotent": True,
        "depends_on": [],
        "evidence": [value for value in evidence if value],
        "ownership": {
            "namespace_env": RUN_NAMESPACE_ENV,
            "resource": str(entity["name"]),
            "selector": f"{id_field} = {variable} derived from {RUN_NAMESPACE_ENV}",
        },
        "runtime_variables": {variable: _runtime_definition(id_field_contract)},
        "precheck": precheck,
        "script": setup,
        "setup_verification": setup_verification,
        "cleanup": cleanup_script,
        "cleanup_verification": cleanup_verification,
    }


def _source_env(source: dict[str, Any], field: str) -> str | None:
    value = source.get("environment", {}).get(field) if isinstance(source.get("environment"), dict) else None
    return str(value.get("env")) if isinstance(value, dict) and value.get("env") else None


def _sql_identifier(value: str, engine: str) -> str:
    pieces = value.split(".")
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", piece) for piece in pieces):
        raise MockDataError(f"source entity uses an unsafe database identifier: {value!r}")
    if engine in {"mysql", "mariadb"}:
        return ".".join(f"`{piece}`" for piece in pieces)
    if engine == "sqlserver":
        return ".".join(f"[{piece}]" for piece in pieces)
    return ".".join(f'"{piece}"' for piece in pieces)


def _field_expression(field: dict[str, Any], variable: str | None, reference_variable: str | None) -> str:
    if variable:
        return f"bru.getEnvVar({json.dumps(variable)})"
    if reference_variable:
        return f"bru.getEnvVar({json.dumps(reference_variable)})"
    enum = field.get("enum")
    if isinstance(enum, list) and enum:
        return json.dumps(enum[0], ensure_ascii=False)
    if field.get("pattern"):
        raise MockDataError(
            f"field {field.get('name')} has a source pattern that cannot be safely synthesized without an example"
        )
    kind = str(field.get("type", "")).casefold()
    if any(token in kind for token in ("int", "number", "decimal", "numeric", "float", "double")):
        minimum = field.get("minimum", 1)
        maximum = field.get("maximum")
        if maximum is not None and minimum > maximum:
            raise MockDataError(f"field {field.get('name')} has contradictory numeric bounds")
        return str(minimum)
    if any(token in kind for token in ("bool", "bit")):
        return "false"
    if "date" in kind or "time" in kind:
        return json.dumps("2026-01-01T00:00:00Z" if "time" in kind else "2026-01-01")
    expression = f"`${{namespace}}-{_safe_name(str(field.get('name', 'value')))}`"
    minimum_length = int(field.get("min_length", 0) or 0)
    maximum_length = int(field.get("max_length", 0) or 0)
    if maximum_length and minimum_length > maximum_length:
        raise MockDataError(f"field {field.get('name')} has contradictory length bounds")
    if minimum_length:
        expression = f"({expression}).padEnd({minimum_length}, 'x')"
    if maximum_length:
        expression = f"({expression}).slice(0, {maximum_length})"
    return expression


def _runtime_definition(field: dict[str, Any]) -> dict[str, Any]:
    if field.get("pattern"):
        raise MockDataError(
            f"field {field.get('name')} has a source pattern that cannot be safely synthesized without an example"
        )
    kind = str(field.get("type", "")).casefold()
    if "objectid" in kind:
        return {"kind": "objectid_namespace"}
    if "uuid" in kind or "uniqueidentifier" in kind:
        return {"kind": "uuid_namespace"}
    if re.search(r"\b(?:tinyint|smallint|int|integer|bigint|number|decimal|numeric|long|short)\b", kind):
        return {"kind": "numeric_namespace"}
    definition: dict[str, Any] = {"kind": "namespace"}
    maximum_length = field.get("max_length")
    if isinstance(maximum_length, int) and maximum_length > 0:
        definition["max_length"] = maximum_length
    return definition


def _sql_connection(source: dict[str, Any], engine: str) -> tuple[str, str, str]:
    url = _source_env(source, "connection_url")
    host, port = _source_env(source, "host"), _source_env(source, "port")
    database, username, password = (
        _source_env(source, "database"), _source_env(source, "username"), _source_env(source, "password")
    )
    if engine in {"mysql", "mariadb"}:
        target = f"bru.getEnvVar({json.dumps(url)})" if url else (
            "{" + ", ".join(
                f"{key}: bru.getEnvVar({json.dumps(name)})"
                for key, name in (("host", host), ("port", port), ("user", username), ("password", password), ("database", database))
                if name
            ) + "}"
        )
        return "const mysql = require('mysql2/promise');", f"const connection = await mysql.createConnection({target});", "await connection.end();"
    if engine == "postgresql":
        target = (
            "{connectionString: bru.getEnvVar(" + json.dumps(url) + ")}" if url else
            "{" + ", ".join(
                f"{key}: bru.getEnvVar({json.dumps(name)})"
                for key, name in (("host", host), ("port", port), ("user", username), ("password", password), ("database", database))
                if name
            ) + "}"
        )
        return "const {Client} = require('pg');", f"const connection = new Client({target}); await connection.connect();", "await connection.end();"
    if engine == "oracle":
        connect_string = url or database
        properties = [
            f"{key}: bru.getEnvVar({json.dumps(name)})"
            for key, name in (("user", username), ("password", password)) if name
        ]
        if connect_string:
            properties.append(
                "connectString: bru.getEnvVar(" + json.dumps(connect_string) + ")"
                ".replace(/^jdbc:oracle:thin:@/, '')"
            )
        target = "{" + ", ".join(properties) + "}"
        return "const oracledb = require('oracledb');", f"const connection = await oracledb.getConnection({target});", "await connection.close();"
    if engine == "sqlserver":
        if url:
            target = f"bru.getEnvVar({json.dumps(url)}).replace(/^jdbc:/, '')"
        else:
            properties = [
                f"{key}: bru.getEnvVar({json.dumps(name)})"
                for key, name in (("server", host), ("user", username), ("password", password), ("database", database)) if name
            ]
            if port:
                properties.append(f"port: Number(bru.getEnvVar({json.dumps(port)}))")
            properties.append("options: {trustServerCertificate: true}")
            target = "{" + ", ".join(properties) + "}"
        return "const sql = require('mssql');", f"const connection = await sql.connect({target});", "await connection.close();"
    raise MockDataError(f"automatic relational fixtures do not support engine {engine!r}")


def _sql_step(
    module_id: str,
    entity: dict[str, Any],
    source: dict[str, Any],
    dependent_case_ids: list[str],
    variable_by_entity: dict[str, str],
) -> dict[str, Any]:
    engine = str(source.get("engine", ""))
    primary_key = [str(value) for value in entity.get("primary_key", [])]
    if len(primary_key) != 1:
        raise MockDataError(f"entity {entity.get('name')} needs exactly one source-backed primary key")
    fields_by_name = {str(value.get("name")): value for value in entity.get("fields", []) if isinstance(value, dict)}
    pk = primary_key[0]
    if pk not in fields_by_name:
        raise MockDataError(f"entity {entity.get('name')} primary key {pk!r} is not declared as a field")
    variable = variable_by_entity[str(entity["name"])]
    references = {
        str(value.get("field")): variable_by_entity.get(str(value.get("entity")))
        for value in entity.get("references", []) if isinstance(value, dict)
    }
    selected = [
        field for field in entity.get("fields", []) if isinstance(field, dict) and (
            str(field.get("name")) == pk
            or str(field.get("name")) in references
            or (field.get("required") is True and "default" not in field)
        )
    ]
    columns = [str(field["name"]) for field in selected]
    expressions = [
        _field_expression(
            field,
            variable if str(field["name"]) == pk else None,
            references.get(str(field["name"])),
        )
        for field in selected
    ]
    table = _sql_identifier(str(entity["name"]), engine)
    quoted = [_sql_identifier(value, engine) for value in columns]
    quoted_pk = _sql_identifier(pk, engine)
    require, connect, close = _sql_connection(source, engine)
    values = f"const values = [{', '.join(expressions)}];"
    namespace = "const namespace = bru.getEnvVar('DLTK_DATA_NAMESPACE');"
    if engine in {"mysql", "mariadb"}:
        select_sql, select_call = f"SELECT 1 FROM {table} WHERE {quoted_pk} = ?", "connection.execute"
        insert_sql = f"INSERT IGNORE INTO {table} ({', '.join(quoted)}) VALUES ({', '.join('?' for _ in quoted)})"
        precheck_body = f"const [rows] = await {select_call}({json.dumps(select_sql)}, [values[0]]); bru.setVar('{STEP_EXISTS_ENV}', rows.length ? 'true' : 'false');"
        setup_body = f"const [result] = await connection.execute({json.dumps(insert_sql)}, values); if (result.affectedRows !== 1) throw new Error('fixture row was not created');"
        verify_body = f"const [rows] = await connection.execute({json.dumps(select_sql)}, [values[0]]); if (rows.length !== 1) throw new Error('fixture row is missing');"
        cleanup_body = f"await connection.execute({json.dumps(f'DELETE FROM {table} WHERE {quoted_pk} = ?')}, [values[0]]);"
    elif engine == "postgresql":
        select_sql = f"SELECT 1 FROM {table} WHERE {quoted_pk} = $1"
        insert_sql = f"INSERT INTO {table} ({', '.join(quoted)}) VALUES ({', '.join(f'${index}' for index in range(1, len(quoted) + 1))}) ON CONFLICT DO NOTHING"
        precheck_body = f"const result = await connection.query({json.dumps(select_sql)}, [values[0]]); bru.setVar('{STEP_EXISTS_ENV}', result.rowCount ? 'true' : 'false');"
        setup_body = f"const result = await connection.query({json.dumps(insert_sql)}, values); if (result.rowCount !== 1) throw new Error('fixture row was not created');"
        verify_body = f"const result = await connection.query({json.dumps(select_sql)}, [values[0]]); if (result.rowCount !== 1) throw new Error('fixture row is missing');"
        cleanup_body = f"await connection.query({json.dumps(f'DELETE FROM {table} WHERE {quoted_pk} = $1')}, [values[0]]);"
    elif engine == "oracle":
        select_sql = f"SELECT 1 FROM {table} WHERE {quoted_pk} = :1"
        insert_sql = (
            f"INSERT INTO {table} ({', '.join(quoted)}) "
            f"SELECT {', '.join(f':{index}' for index in range(1, len(quoted) + 1))} FROM dual "
            f"WHERE NOT EXISTS (SELECT 1 FROM {table} WHERE {quoted_pk} = :{len(quoted) + 1})"
        )
        precheck_body = f"const result = await connection.execute({json.dumps(select_sql)}, [values[0]]); bru.setVar('{STEP_EXISTS_ENV}', result.rows.length ? 'true' : 'false');"
        setup_body = f"const result = await connection.execute({json.dumps(insert_sql)}, [...values, values[0]], {{autoCommit: true}}); if (result.rowsAffected !== 1) throw new Error('fixture row was not created');"
        verify_body = f"const result = await connection.execute({json.dumps(select_sql)}, [values[0]]); if (result.rows.length !== 1) throw new Error('fixture row is missing');"
        cleanup_body = f"await connection.execute({json.dumps(f'DELETE FROM {table} WHERE {quoted_pk} = :1')}, [values[0]], {{autoCommit: true}});"
    elif engine == "sqlserver":
        placeholders = [f"@p{index}" for index in range(1, len(quoted) + 1)]
        select_sql = f"SELECT 1 FROM {table} WHERE {quoted_pk} = @p1"
        insert_sql = (
            f"INSERT INTO {table} ({', '.join(quoted)}) SELECT {', '.join(placeholders)} "
            f"WHERE NOT EXISTS (SELECT 1 FROM {table} WHERE {quoted_pk} = @p{len(quoted) + 1})"
        )
        binder = "const request = (items) => { const value = connection.request(); items.forEach((item, index) => value.input(`p${index + 1}`, item)); return value; };"
        namespace = "\n".join((namespace, binder))
        precheck_body = f"const result = await request([values[0]]).query({json.dumps(select_sql)}); bru.setVar('{STEP_EXISTS_ENV}', result.recordset.length ? 'true' : 'false');"
        setup_body = f"const result = await request([...values, values[0]]).query({json.dumps(insert_sql)}); if (!result.rowsAffected || result.rowsAffected[0] !== 1) throw new Error('fixture row was not created');"
        verify_body = f"const result = await request([values[0]]).query({json.dumps(select_sql)}); if (result.recordset.length !== 1) throw new Error('fixture row is missing');"
        cleanup_body = f"await request([values[0]]).query({json.dumps(f'DELETE FROM {table} WHERE {quoted_pk} = @p1')});"
    else:
        raise MockDataError(f"automatic fixture SQL is not safely defined for engine {engine!r}")

    def script(body: str) -> str:
        return "\n".join((namespace, require, connect, values, "try {", f"  {body}", f"}} finally {{ {close} }}"))

    dependencies = [
        f"source-db-{module_id}-{_safe_name(str(value.get('entity')))}"
        for value in entity.get("references", []) if isinstance(value, dict) and value.get("entity") in variable_by_entity
    ]
    return {
        "id": f"source-db-{module_id}-{_safe_name(str(entity['name']))}",
        "phase": "setup", "reason": "missing_prerequisite_api", "transport": "database",
        "engine": engine, "data_source": str(source["id"]),
        "dependent_case_ids": dependent_case_ids, "estimated_records": 1, "idempotent": True,
        "depends_on": list(dict.fromkeys(dependencies)),
        "evidence": entity.get("evidence", []),
        "ownership": {
            "namespace_env": RUN_NAMESPACE_ENV, "resource": str(entity["name"]),
            "selector": f"{pk} = {variable} derived from {RUN_NAMESPACE_ENV}",
        },
        "runtime_variables": {
            variable: _runtime_definition(fields_by_name[pk]),
        },
        "precheck": script(precheck_body), "script": script(setup_body),
        "setup_verification": script(verify_body), "cleanup": script(cleanup_body),
        "cleanup_verification": script(verify_body.replace("!== 1", "!== 0").replace("is missing", "still exists")),
    }


def _non_relational_step(
    module_id: str,
    entity: dict[str, Any],
    source: dict[str, Any],
    dependent_case_ids: list[str],
    variable_by_entity: dict[str, str],
) -> dict[str, Any]:
    engine = str(source.get("engine", ""))
    primary_key = [str(value) for value in entity.get("primary_key", [])]
    if len(primary_key) != 1:
        raise MockDataError(f"entity {entity.get('name')} needs exactly one source-backed primary key")
    if entity.get("references"):
        raise MockDataError(
            f"entity {entity.get('name')} has document references that cannot be safely materialized from scalar source evidence"
        )
    fields_by_name = {
        str(value.get("name")): value for value in entity.get("fields", []) if isinstance(value, dict)
    }
    pk = primary_key[0]
    if pk not in fields_by_name:
        raise MockDataError(f"entity {entity.get('name')} primary key {pk!r} is not declared as a field")
    variable = variable_by_entity[str(entity["name"])]
    selected = [
        field for field in entity.get("fields", []) if isinstance(field, dict) and (
            str(field.get("name")) == pk or (field.get("required") is True and "default" not in field)
        )
    ]
    if not selected:
        raise MockDataError(f"entity {entity.get('name')} has no source-backed fields for a fixture")
    if engine != "mongodb" and _runtime_definition(fields_by_name[pk])["kind"] == "objectid_namespace":
        raise MockDataError(f"entity {entity.get('name')} uses ObjectId outside a confirmed MongoDB mapping")
    namespace = "const namespace = bru.getEnvVar('DLTK_DATA_NAMESPACE');"
    document = "const document = {" + ", ".join(
        f"{json.dumps(str(field['name']))}: "
        + (
            f"new ObjectId(bru.getEnvVar({json.dumps(variable)}))"
            if str(field["name"]) == pk and _runtime_definition(field)["kind"] == "objectid_namespace"
            else _field_expression(field, variable if str(field["name"]) == pk else None, None)
        )
        for field in selected
    ) + "};"
    resource = str(entity["name"])

    def env_expression(field: str, fallback: str = "undefined") -> str:
        name = _source_env(source, field)
        return f"bru.getEnvVar({json.dumps(name)})" if name else fallback

    url = _source_env(source, "connection_url")
    host = _source_env(source, "host")
    port = _source_env(source, "port")
    username = _source_env(source, "username")
    password = _source_env(source, "password")
    database = _source_env(source, "database")
    index = _source_env(source, "index")
    require: str
    connect: str
    close: str
    precheck_body: str
    setup_body: str
    verify_body: str
    cleanup_body: str

    if engine == "mongodb":
        if not url and not host:
            raise MockDataError(f"MongoDB source {source.get('id')} has no confirmed connection URL or host")
        target = env_expression("connection_url") if url else (
            "(() => { const scheme = " + env_expression("scheme", "'mongodb'") + "; const host = "
            + env_expression("host") + "; return scheme === 'mongodb+srv' ? `${scheme}://${host}` : "
            "`${scheme}://${host}:${" + env_expression("port", "'27017'") + "}`; })()"
        )
        auth = (
            "const options = {}; const username = " + env_expression("username", "''") + "; "
            "if (username) options.auth = {username, password: " + env_expression("password", "''") + "};"
        )
        require = "const {MongoClient, ObjectId} = require('mongodb');"
        connect = (
            f"{auth} const client = new MongoClient({target}, options); await client.connect(); "
            f"const collection = client.db({env_expression('database')}).collection({json.dumps(resource)});"
        )
        close = "await client.close();"
        selector = "{" + json.dumps(pk) + ": document[" + json.dumps(pk) + "]}"
        precheck_body = f"const found = await collection.findOne({selector}, {{projection: {{{json.dumps(pk)}: 1}}}}); bru.setVar('{STEP_EXISTS_ENV}', found ? 'true' : 'false');"
        setup_body = f"const result = await collection.updateOne({selector}, {{$setOnInsert: document}}, {{upsert: true}}); if (result.upsertedCount !== 1) throw new Error('fixture document was not created');"
        verify_body = f"const found = await collection.findOne({selector}, {{projection: {{{json.dumps(pk)}: 1}}}}); if (!found) throw new Error('fixture document is missing');"
        cleanup_body = f"await collection.deleteOne({selector});"
    elif engine in {"elasticsearch", "opensearch"}:
        if not url and not host:
            raise MockDataError(f"{engine} source {source.get('id')} has no confirmed connection URL or host")
        scheme = env_expression("scheme", "'http'")
        target = env_expression("connection_url") if url else (
            "`${" + scheme + "}://${" + env_expression("host") + "}:${" + env_expression("port", "'9200'") + "}`"
        )
        package = "@elastic/elasticsearch" if engine == "elasticsearch" else "@opensearch-project/opensearch"
        require = f"const {{Client}} = require({json.dumps(package)});"
        auth = (
            "const username = " + env_expression("username", "''") + "; const options = {node: " + target + "}; "
            "if (username) options.auth = {username, password: " + env_expression("password", "''") + "};"
        )
        connect = (
            f"{auth} const client = new Client(options); const index = {env_expression('index', json.dumps(resource))}; "
            f"const id = String(document[{json.dumps(pk)}]); const exists = async () => {{ const response = await client.exists({{index, id}}); "
            "return Boolean(response && typeof response === 'object' && 'body' in response ? response.body : response); };"
        )
        close = "await client.close();"
        create_field = "document" if engine == "elasticsearch" else "body"
        precheck_body = f"const found = await exists(); bru.setVar('{STEP_EXISTS_ENV}', found ? 'true' : 'false');"
        setup_body = f"await client.create({{index, id, {create_field}: document}});"
        verify_body = "const found = await exists(); if (!found) throw new Error('fixture document is missing');"
        cleanup_body = "try { await client.delete({index, id}); } catch (error) { if (error?.meta?.statusCode !== 404) throw error; }"
    elif engine == "redis":
        if not url and not host:
            raise MockDataError(f"Redis source {source.get('id')} has no confirmed connection URL or host")
        require = "const {createClient} = require('redis');"
        if url:
            options = "{url: " + env_expression("connection_url") + "}"
        else:
            properties = [
                "url: `${" + env_expression("scheme", "'redis'") + "}://${" + env_expression("host")
                + "}:${" + env_expression("port", "'6379'") + "}`",
            ]
            if username:
                properties.append("username: " + env_expression("username"))
            if password:
                properties.append("password: " + env_expression("password"))
            if database:
                properties.append("database: Number(" + env_expression("database") + ")")
            options = "{" + ", ".join(properties) + "}"
        connect = f"const client = createClient({options}); await client.connect(); const key = {json.dumps(resource + ':')} + String(document[{json.dumps(pk)}]);"
        close = "await client.quit();"
        precheck_body = f"bru.setVar('{STEP_EXISTS_ENV}', await client.exists(key) ? 'true' : 'false');"
        setup_body = "const result = await client.set(key, JSON.stringify(document), {NX: true}); if (result !== 'OK') throw new Error('fixture key was not created');"
        verify_body = "if (await client.exists(key) !== 1) throw new Error('fixture key is missing');"
        cleanup_body = "await client.del(key);"
    else:
        raise MockDataError(f"automatic fixtures do not support engine {engine!r}")

    def script(body: str) -> str:
        return "\n".join((namespace, require, document, connect, "try {", f"  {body}", f"}} finally {{ {close} }}"))

    cleanup_verification = script(
        verify_body.replace("if (!found)", "if (found)")
        .replace("is missing", "still exists")
        .replace("await client.exists(key) !== 1", "await client.exists(key) !== 0")
    )
    return {
        "id": f"source-db-{module_id}-{_safe_name(resource)}",
        "phase": "setup", "reason": "missing_prerequisite_api", "transport": "database",
        "engine": engine, "data_source": str(source["id"]),
        "dependent_case_ids": dependent_case_ids, "estimated_records": 1, "idempotent": True,
        "depends_on": [], "evidence": entity.get("evidence", []),
        "ownership": {
            "namespace_env": RUN_NAMESPACE_ENV, "resource": resource,
            "selector": f"{pk} = {variable} derived from {RUN_NAMESPACE_ENV}",
        },
        "runtime_variables": {
            variable: _runtime_definition(fields_by_name[pk]),
        },
        "precheck": script(precheck_body), "script": script(setup_body),
        "setup_verification": script(verify_body), "cleanup": script(cleanup_body),
        "cleanup_verification": cleanup_verification,
    }


def _database_source_for(entity: dict[str, Any], sources: list[dict[str, Any]]) -> dict[str, Any] | None:
    storage = str(entity.get("storage_engine") or ("relational" if entity.get("source_type") == "ddl" else ""))
    engines = (
        {"mysql", "mariadb", "postgresql", "oracle", "sqlserver"}
        if storage == "relational" else {storage}
    )
    matches = [source for source in sources if str(source.get("engine")) in engines and source.get("configuration_status") != "dependency_only"]
    return matches[0] if len(matches) == 1 else None


def derive_mock_data_contracts(qa_root: Path) -> list[Path]:
    """Write only high-confidence source/OpenAPI fixture plans; ambiguous requirements remain blocked."""

    inventory = load_inventory(qa_root)
    entities = [value for value in inventory.get("entities", []) if isinstance(value, dict)]
    sources = [value for value in inventory.get("data_sources", []) if isinstance(value, dict)]
    variable_by_entity = {
        str(entity.get("name")): f"DLTK_DATA_{re.sub(r'[^A-Za-z0-9]+', '_', str(entity.get('name'))).strip('_').upper()}_ID"
        for entity in entities if entity.get("name")
    }
    entity_by_name = {str(entity.get("name")): entity for entity in entities if entity.get("name")}
    aliases: dict[str, list[dict[str, Any]]] = {}
    for entity in entities:
        for alias in {str(entity.get("name", "")), str(entity.get("class_name", ""))} - {""}:
            aliases.setdefault(alias.casefold(), []).append(entity)
    for alias, matches in aliases.items():
        if len(matches) == 1:
            entity = matches[0]
            entity_by_name.setdefault(alias, entity)
            variable_by_entity.setdefault(alias, variable_by_entity[str(entity["name"])])
    changed: list[Path] = []
    required_packages: set[str] = set()
    for directory in _module_directories(qa_root, None):
        endpoints_path = directory / "endpoints.yaml"
        cases_path = directory / "cases.yaml"
        endpoint_document = load_data(endpoints_path) if endpoints_path.is_file() else {}
        case_document = load_data(cases_path) if cases_path.is_file() else {}
        module_id = str(endpoint_document.get("module", directory.name))
        endpoints = [value for value in endpoint_document.get("endpoints", []) if isinstance(value, dict)]
        cases = [value for value in case_document.get("cases", []) if isinstance(value, dict)]
        for case in cases:
            previous_binding = case.pop("mock_data_path_binding", None)
            if isinstance(previous_binding, dict) and previous_binding.get("name"):
                request = case.get("request") if isinstance(case.get("request"), dict) else {}
                parameters = request.get("path_parameters") if isinstance(request.get("path_parameters"), dict) else {}
                name = str(previous_binding["name"])
                if previous_binding.get("had_value") is True:
                    parameters[name] = copy.deepcopy(previous_binding.get("value"))
                else:
                    parameters.pop(name, None)
                if parameters:
                    request["path_parameters"] = parameters
                else:
                    request.pop("path_parameters", None)
                if request:
                    case["request"] = request
                else:
                    case.pop("request", None)
            for field in ("mock_data_required", "mock_data_step_ids", "mock_data_blocker"):
                case.pop(field, None)
        endpoint_entities = {
            str(endpoint.get("id")): _endpoint_entity(endpoint, entities) for endpoint in endpoints
        }
        steps: list[dict[str, Any]] = []
        database_steps_by_entity: dict[str, dict[str, Any]] = {}
        requirements: list[dict[str, Any]] = []
        by_entity: dict[str, list[dict[str, Any]]] = {}
        for case in cases:
            endpoint = next((item for item in endpoints if item.get("id") == case.get("endpoint_id")), None)
            entity = endpoint_entities.get(str(case.get("endpoint_id")))
            if (
                endpoint is None or entity is None or case.get("scenario") != "success"
                or str(endpoint.get("method")) not in {"GET", "PUT", "PATCH", "DELETE"}
                or _endpoint_parameter(endpoint) is None
            ):
                continue
            by_entity.setdefault(str(entity["name"]), []).append(case)
        endpoint_by_id = {str(value.get("id")): value for value in endpoints}

        def bind_case_identifier(case: dict[str, Any], step: dict[str, Any], entity_name: str) -> None:
            request = case.setdefault("request", {})
            parameters = request.setdefault("path_parameters", {})
            path_name = str(_endpoint_parameter(endpoint_by_id[str(case["endpoint_id"])]))
            case["mock_data_path_binding"] = {
                "name": path_name,
                "had_value": path_name in parameters,
                **({"value": copy.deepcopy(parameters[path_name])} if path_name in parameters else {}),
            }
            parameters[path_name] = "{{" + next(iter(step["runtime_variables"]), variable_by_entity[entity_name]) + "}}"

        def database_fixture(entity: dict[str, Any], case_ids: list[str], visiting: set[str]) -> dict[str, Any]:
            name = str(entity["name"])
            if name in visiting:
                raise MockDataError(f"source entity relationship cycle prevents a safe fixture order: {name}")
            if name in database_steps_by_entity:
                existing = database_steps_by_entity[name]
                existing["dependent_case_ids"] = list(dict.fromkeys([*existing["dependent_case_ids"], *case_ids]))
                return existing
            source = _database_source_for(entity, sources)
            if source is None:
                raise MockDataError(f"entity {name} does not map to exactly one confirmed data source")
            resolved_entity = copy.deepcopy(entity)
            for reference in resolved_entity.get("references", []):
                if not isinstance(reference, dict):
                    continue
                reference_name = str(reference.get("entity"))
                parent = entity_by_name.get(reference_name) or entity_by_name.get(reference_name.casefold())
                if parent is None:
                    raise MockDataError(f"entity {name} references unknown entity {reference.get('entity')!r}")
                reference["entity"] = str(parent["name"])
                database_fixture(parent, case_ids, visiting | {name})
            if str(source.get("engine")) in {"mysql", "mariadb", "postgresql", "oracle", "sqlserver"}:
                generated = _sql_step(module_id, resolved_entity, source, case_ids, variable_by_entity)
            else:
                generated = _non_relational_step(module_id, resolved_entity, source, case_ids, variable_by_entity)
            database_steps_by_entity[name] = generated
            steps.append(generated)
            return generated

        for entity_name, dependent_cases in sorted(by_entity.items()):
            entity = next(value for value in entities if str(value.get("name")) == entity_name)
            related = [value for value in endpoints if endpoint_entities.get(str(value.get("id"))) is entity]
            create = next((value for value in related if value.get("method") == "POST" and _endpoint_parameter(value) is None), None)
            read = next((value for value in related if value.get("method") == "GET" and _endpoint_parameter(value)), None)
            delete = next((value for value in related if value.get("method") == "DELETE" and _endpoint_parameter(value)), None)
            explicit = any(
                any(step.get("phase") == "setup" for step in case.get("database_steps", []) if isinstance(step, dict))
                for case in dependent_cases
            )
            step = None
            primary_key = entity.get("primary_key", [])
            if (
                isinstance(primary_key, list) and len(primary_key) == 1
                and create and read and delete
                and "404" in {str(value) for value in read.get("responses", {})}
            ):
                create_case = next((
                    case for case in cases
                    if case.get("endpoint_id") == create.get("id") and case.get("scenario") == "success"
                ), None)
                if create_case:
                    step = _api_step(
                        module_id, entity, dependent_cases, create, read, delete, create_case,
                        str(primary_key[0]),
                    )
            if step:
                steps.append(step)
                for case in dependent_cases:
                    case["mock_data_required"] = True
                    case["mock_data_step_ids"] = [step["id"]]
                    bind_case_identifier(case, step, entity_name)
                status = "api"
                reason = "source-mapped create/read/delete API"
            elif explicit:
                case_ids = [str(value["id"]) for value in dependent_cases]
                for case in dependent_cases:
                    for authored in case.get("database_steps", []):
                        if isinstance(authored, dict) and authored.get("phase") == "setup":
                            authored["dependent_case_ids"] = list(dict.fromkeys([
                                *authored.get("dependent_case_ids", []), *case_ids,
                            ]))
                    case["mock_data_required"] = True
                    case["mock_data_step_ids"] = [
                        str(item.get("id")) for item in case.get("database_steps", [])
                        if isinstance(item, dict) and item.get("phase") == "setup" and item.get("id")
                    ]
                status = "database"
                reason = "source-backed case database step"
            else:
                try:
                    generated = database_fixture(
                        entity, [str(value["id"]) for value in dependent_cases], set(),
                    )
                except MockDataError as exc:
                    for case in dependent_cases:
                        case["mock_data_required"] = True
                        case["mock_data_step_ids"] = []
                        case["mock_data_blocker"] = str(exc)
                    status = "blocked"
                    reason = str(exc)
                else:
                    for case in dependent_cases:
                        case["mock_data_required"] = True
                        case["mock_data_step_ids"] = [generated["id"]]
                        bind_case_identifier(case, generated, entity_name)
                    status = "database"
                    reason = "source-derived minimal database fixture"
            requirements.append({
                "entity": entity_name,
                "case_ids": [str(value["id"]) for value in dependent_cases],
                "status": status,
                "reason": reason,
                "evidence": entity.get("evidence", []),
            })
        module_plan = directory / MODULE_PLAN_FILE
        _yaml_write(module_plan, {
            "version": 1, "module": module_id, "steps": steps, "requirements": requirements,
        })
        _yaml_write(cases_path, case_document)
        changed.extend([module_plan, cases_path])
        packages = {
            "mysql": "mysql2", "mariadb": "mysql2", "postgresql": "pg",
            "oracle": "oracledb", "sqlserver": "mssql", "mongodb": "mongodb",
            "elasticsearch": "@elastic/elasticsearch",
            "opensearch": "@opensearch-project/opensearch", "redis": "redis",
        }
        required_packages.update(
            packages[str(step.get("engine"))]
            for step in steps if str(step.get("engine")) in packages
        )
    if required_packages:
        package_path = qa_root / BRUNO / "package.json"
        package = load_data(package_path) if package_path.is_file() else {"private": True}
        if not isinstance(package, dict):
            raise MockDataError(f"Bruno package manifest must contain an object: {package_path}")
        dependencies = package.setdefault("dependencies", {})
        if not isinstance(dependencies, dict):
            raise MockDataError(f"Bruno package dependencies must contain an object: {package_path}")
        versions = {
            "mysql2": "3.11.5", "pg": "8.13.1", "oracledb": "6.7.0", "mssql": "11.0.1",
            "mongodb": "6.12.0", "@elastic/elasticsearch": "8.17.0",
            "@opensearch-project/opensearch": "3.4.0", "redis": "4.7.0",
        }
        for name in sorted(required_packages):
            dependencies.setdefault(name, versions[name])
        rendered = json.dumps(package, ensure_ascii=False, indent=2) + "\n"
        if not package_path.is_file() or package_path.read_text(encoding="utf-8") != rendered:
            package_path.write_text(rendered, encoding="utf-8")
            changed.append(package_path)
    return changed


def _module_directories(qa_root: Path, requested: list[str] | None) -> list[Path]:
    modules_root = qa_root / CONTRACTS / "modules"
    directories = sorted(path for path in modules_root.iterdir() if path.is_dir()) if modules_root.is_dir() else []
    if not requested:
        return directories
    module_map_path = qa_root / CONTRACTS / "module-map.yaml"
    module_map = load_data(module_map_path) if module_map_path.is_file() else {}
    metadata = {
        str(item.get("id")): item for item in first_list(module_map, "modules") if item.get("id")
    }
    matches: list[Path] = []
    for value in requested:
        found: list[Path] = []
        for directory in directories:
            endpoint_path = directory / "endpoints.yaml"
            document = load_data(endpoint_path) if endpoint_path.is_file() else {}
            module_id = str(document.get("module", directory.name)) if isinstance(document, dict) else directory.name
            details = metadata.get(module_id, {})
            names = {
                directory.name, module_id, str(document.get("name", "")), str(document.get("swagger_tag", "")),
                str(details.get("name", "")), str(details.get("tag", "")),
                *[str(tag) for tag in details.get("swagger_tags", []) if isinstance(details.get("swagger_tags"), list)],
            }
            if value in names:
                found.append(directory)
        if len(found) != 1:
            raise MockDataError(f"{'unknown' if not found else 'ambiguous'} module: {value}")
        if found[0] not in matches:
            matches.append(found[0])
    return matches


def _plan_step(
    step: dict[str, Any], module_id: str, directory: Path, default_case_id: str,
) -> dict[str, Any]:
    dependent_case_ids = [
        str(value) for value in step.get("dependent_case_ids", []) if str(value).strip()
    ] if isinstance(step.get("dependent_case_ids"), list) else []
    if not dependent_case_ids and default_case_id:
        dependent_case_ids = [default_case_id]
    payload = {
        "id": str(step.get("id") or f"{default_case_id}:setup"),
        "module": module_id,
        "module_directory": directory.name,
        "case_id": dependent_case_ids[0] if dependent_case_ids else default_case_id,
        "dependent_case_ids": dependent_case_ids,
        "engine": str(step.get("engine", "")),
        "transport": str(step.get("transport", "database")),
        "data_source": str(step.get("data_source", step.get("engine", ""))),
        "estimated_records": int(step.get("estimated_records", 1)),
        "depends_on": [str(value) for value in step.get("depends_on", [])] if isinstance(step.get("depends_on"), list) else [],
        "evidence": step.get("evidence", []),
        "ownership": step.get("ownership", {}),
        "runtime_variables": step.get("runtime_variables", {}),
        "precheck": str(step.get("precheck", "")),
        "script": str(step.get("script", "")),
        "setup_verification": str(step.get("setup_verification", "")),
        "cleanup": str(step.get("cleanup", "")),
        "cleanup_verification": str(step.get("cleanup_verification", "")),
    }
    payload["fingerprint"] = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return payload


def build_plan(qa_root: Path, modules: list[str] | None = None) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    selected_modules: list[str] = []
    blocked_case_ids: dict[str, str] = {}
    for directory in _module_directories(qa_root, modules):
        endpoints_path = directory / "endpoints.yaml"
        endpoint_document = load_data(endpoints_path) if endpoints_path.is_file() else {}
        module_id = str(endpoint_document.get("module", directory.name)) if isinstance(endpoint_document, dict) else directory.name
        selected_modules.append(module_id)
        module_plan_path = directory / MODULE_PLAN_FILE
        module_plan = load_data(module_plan_path) if module_plan_path.is_file() else {}
        generated_steps = [
            value for value in module_plan.get("steps", []) if isinstance(value, dict)
        ] if isinstance(module_plan, dict) else []
        for step in generated_steps:
            errors = database_access_errors({"id": str(step.get("id", "source-derived")), "database_steps": [step]})
            if errors:
                raise MockDataError("; ".join(errors))
            steps.append(_plan_step(step, module_id, directory, ""))
        api_case_ids = {
            str(case_id) for step in generated_steps if step.get("transport") == "api"
            for case_id in step.get("dependent_case_ids", [])
        }
        for requirement in module_plan.get("requirements", []) if isinstance(module_plan, dict) else []:
            if not isinstance(requirement, dict) or requirement.get("status") != "blocked":
                continue
            for case_id in requirement.get("case_ids", []):
                blocked_case_ids[str(case_id)] = str(requirement.get("reason", "mock-data requirement is unresolved"))
        cases_path = directory / "cases.yaml"
        for case in first_list(load_data(cases_path), "cases") if cases_path.is_file() else []:
            errors = database_access_errors(case)
            if errors:
                raise MockDataError("; ".join(errors))
            case_id = str(case.get("id", ""))
            for index, step in enumerate(case.get("database_steps", []) if isinstance(case.get("database_steps"), list) else []):
                if not isinstance(step, dict) or step.get("phase") != "setup":
                    continue
                if case_id in api_case_ids:
                    continue
                authored = dict(step)
                authored.setdefault("id", f"{case_id}:setup:{index + 1}")
                steps.append(_plan_step(authored, module_id, directory, case_id))
    ordered: list[dict[str, Any]] = []
    pending = {str(step["id"]): step for step in steps}
    if len(pending) != len(steps):
        raise MockDataError("mock-data setup step ids must be unique in the selected scope")
    while pending:
        ready = [step for step in pending.values() if set(step["depends_on"]) <= {str(item["id"]) for item in ordered}]
        if not ready:
            unresolved = ", ".join(sorted(pending))
            raise MockDataError(f"mock-data dependencies are missing or cyclic: {unresolved}")
        for step in sorted(ready, key=lambda value: (value["module"], value["case_id"], value["id"])):
            ordered.append(step)
            pending.pop(str(step["id"]))
    return {
        "modules": selected_modules,
        "steps": ordered,
        "estimated_records": sum(int(step["estimated_records"]) for step in ordered),
        "data_sources": sorted({
            str(step["data_source"]) for step in ordered if step.get("data_source") != "public-api"
        }),
        "dependent_case_ids": sorted({
            case_id for step in ordered for case_id in step.get("dependent_case_ids", [])
        } | set(blocked_case_ids)),
        "blocked_case_ids": blocked_case_ids,
    }


def is_protected_environment(name: str) -> bool:
    normalized = name.strip().casefold()
    configured = {
        value.strip().casefold() for value in os.environ.get("DLTK_PROTECTED_ENVIRONMENTS", "").split(",") if value.strip()
    }
    return normalized in PROTECTED_ENVIRONMENTS | configured or any(
        re.search(rf"(?:^|[-_.]){token}(?:$|[-_.])", normalized) for token in PROTECTED_ENVIRONMENTS
    )


def is_safe_environment(name: str) -> bool:
    normalized = name.strip().casefold()
    return not is_protected_environment(name) and (
        normalized in SAFE_ENVIRONMENTS
        or any(re.search(rf"(?:^|[-_.]){token}(?:$|[-_.])", normalized) for token in SAFE_ENVIRONMENTS)
    )


def _target_summaries(qa_root: Path, plan: dict[str, Any]) -> list[str]:
    inventory = load_inventory(qa_root)
    sources = {
        str(source.get("id")): source
        for source in inventory.get("data_sources", [])
        if isinstance(source, dict) and source.get("id")
    }
    config_path = qa_root / EXECUTION / "config.yaml"
    config = load_execution_config(config_path)
    environment = load_bruno_environment_document(environment_file(config_path, config))["vars"]
    targets: list[str] = []
    if any(step.get("transport") == "api" or step.get("data_source") == "public-api" for step in plan["steps"]):
        base_url = str(environment.get("baseUrl") or environment.get("BASE_URL") or "")
        parsed = urllib.parse.urlsplit(base_url)
        if not parsed.hostname or parsed.scheme not in {"http", "https"}:
            raise MockDataError("mock-data API target has no confirmed HTTP baseUrl")
        if is_protected_environment(parsed.hostname):
            raise MockDataError("mock-data API target is a protected environment")
        targets.append(
            f"public-api({parsed.scheme}://{parsed.hostname}:{parsed.port or (443 if parsed.scheme == 'https' else 80)})"
        )
    for source_id in plan["data_sources"]:
        source = sources.get(str(source_id))
        if source is None:
            raise MockDataError(f"mock-data target {source_id!r} was not discovered from project configuration")
        if source.get("configuration_status") == "dependency_only":
            raise MockDataError(f"mock-data target {source_id!r} has a dependency but no confirmed connection scope")
        values: dict[str, str] = {}
        configured = source.get("environment", {})
        for field, reference in configured.items() if isinstance(configured, dict) else []:
            if not isinstance(reference, dict) or not reference.get("env"):
                continue
            env_name = str(reference["env"])
            value = os.environ.get(env_name, environment.get(env_name, reference.get("default", "")))
            if value not in {None, ""} and field not in {"password", "username"}:
                values[str(field)] = str(value)
        if values.get("connection_url"):
            values.update(_connection_parts(values.pop("connection_url"), str(source.get("engine", ""))))
        if not values.get("host") or not values.get("port"):
            raise MockDataError(f"mock-data target {source_id!r} has no confirmed host and port")
        scope = values.get("database") or values.get("index") or ",".join(sorted({
            str(step.get("ownership", {}).get("resource"))
            for step in plan["steps"] if step.get("data_source") == source_id
        }))
        if is_protected_environment(values["host"]) or (scope and is_protected_environment(scope)):
            raise MockDataError(f"mock-data target {source_id!r} is a protected environment")
        qualifiers = ",".join(
            f"{name}={values[name]}" for name in ("schema", "scheme", "ssl", "timezone") if values.get(name)
        )
        targets.append(
            f"{source_id}({source.get('engine')}://{values['host']}:{values['port']}/"
            f"{scope or '<unresolved-scope>'}{';' + qualifiers if qualifiers else ''})"
        )
    return targets


def _runtime_variables(qa_root: Path, namespace: str) -> dict[str, str]:
    config_path = qa_root / EXECUTION / "config.yaml"
    config = load_execution_config(config_path)
    document = load_bruno_environment_document(environment_file(config_path, config))
    variables = dict(document["vars"])
    inventory = load_inventory(qa_root)
    for source in inventory.get("data_sources", []) if isinstance(inventory.get("data_sources"), list) else []:
        environment = source.get("environment", {}) if isinstance(source, dict) else {}
        for reference in environment.values() if isinstance(environment, dict) else []:
            if not isinstance(reference, dict) or not reference.get("env"):
                continue
            name = str(reference["env"])
            if name in os.environ:
                variables[name] = os.environ[name]
    variables[RUN_NAMESPACE_ENV] = namespace
    variables["DLTK_MOCK_DATA_HEADERS"] = json.dumps(
        resolved_environment_headers(document), ensure_ascii=False, separators=(",", ":"),
    )
    return variables


def apply_runtime_variables(
    qa_root: Path,
    document: dict[str, dict[str, str]],
    runtime: str | dict[str, Any],
) -> dict[str, dict[str, str]]:
    """Overlay process-provided credentials only in the temporary runtime environment."""

    values = runtime if isinstance(runtime, dict) else _runtime_variables(qa_root, runtime)
    result = {"vars": dict(document["vars"]), "headers": dict(document["headers"])}
    result["vars"].update({str(name): str(value) for name, value in values.items()})
    return result


def _execute_script(qa_root: Path, script: str, variables: dict[str, str], label: str) -> dict[str, str]:
    if not script.strip():
        raise MockDataError(f"{label} has no executable script")
    runner = """const values = JSON.parse(process.env.DLTK_MOCK_DATA_ENV || '{}');
const changed = {};
global.bru = {
  getEnvVar: (name) => Object.prototype.hasOwnProperty.call(values, name) ? values[name] : process.env[name],
  setVar: (name, value) => { values[name] = String(value); changed[name] = String(value); }
};
(async () => {
__SCRIPT__
  process.stdout.write('\\n__RESULT__' + JSON.stringify(changed) + '\\n');
})().catch((error) => {
  console.error(error && error.message ? error.message : String(error));
  process.exit(1);
});
""".replace("__SCRIPT__", script).replace("__RESULT__", SCRIPT_RESULT_MARKER)
    bruno_root = qa_root / BRUNO
    bruno_root.mkdir(parents=True, exist_ok=True)
    path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".cjs", prefix=".dltk-mock-data-", dir=bruno_root, delete=False, encoding="utf-8") as handle:
            handle.write(runner)
            path = Path(handle.name)
        environment = dict(os.environ)
        environment["DLTK_MOCK_DATA_ENV"] = json.dumps(variables, ensure_ascii=False)
        completed = subprocess.run(
            ["node", str(path)], cwd=bruno_root, env=environment, check=False,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError as exc:
        raise MockDataError("Node.js is unavailable; mock-data scripts were not executed") from exc
    finally:
        if path is not None:
            path.unlink(missing_ok=True)
    if completed.returncode:
        detail = str(redact((completed.stderr or completed.stdout).strip()))
        raise MockDataError(f"{label} failed: {detail or f'node exited with {completed.returncode}'}")
    marker = completed.stdout.rfind(SCRIPT_RESULT_MARKER)
    if marker < 0:
        raise MockDataError(f"{label} did not return execution metadata")
    try:
        changed = json.loads(completed.stdout[marker + len(SCRIPT_RESULT_MARKER):].splitlines()[0])
    except (json.JSONDecodeError, IndexError) as exc:
        raise MockDataError(f"{label} returned invalid execution metadata") from exc
    if not isinstance(changed, dict):
        raise MockDataError(f"{label} returned invalid execution metadata")
    safe = {
        str(name): str(value)
        for name, value in changed.items()
        if str(name).startswith(("DLTK_DATA_", "DLTK_STEP_")) and len(str(value)) <= 4096
    }
    variables.update(safe)
    return safe


def _encode_script(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _decode_script(value: Any, label: str) -> str:
    try:
        return base64.b64decode(str(value), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise MockDataError(f"{label} is not a valid frozen cleanup script") from exc


def _frozen_created_step(step: dict[str, Any], state: str) -> dict[str, Any]:
    frozen = {
        key: step[key] for key in (
            "id", "module", "case_id", "dependent_case_ids", "transport", "engine", "data_source",
            "estimated_records", "depends_on", "evidence", "ownership", "fingerprint",
        ) if key in step
    }
    frozen.update({
        "creation_status": state,
        "cleanup_b64": _encode_script(str(step.get("cleanup", ""))),
        "cleanup_verification_b64": _encode_script(str(step.get("cleanup_verification", ""))),
    })
    return frozen


def _ledger_path(qa_root: Path, run_id: str) -> Path:
    if not RUN_ID_RE.fullmatch(run_id) or ".." in run_id:
        raise MockDataError("mock-data run id must be a safe file name")
    return qa_root / RESULT_DIRECTORY / f"{run_id}.json"


def _write_ledger(path: Path, ledger: dict[str, Any]) -> None:
    ledger["updated_at"] = _utc_now()
    write_json(path, ledger)


def _confirmation(
    prompt: str,
    *,
    allowed: bool,
    input_stream: TextIO,
    output: TextIO,
) -> tuple[bool, str]:
    if allowed:
        return True, "explicit_flag"
    if not input_stream.isatty():
        return False, "non_interactive_default_deny"
    console: TextIO | None = None
    target = output
    if input_stream is sys.stdin:
        try:
            console = open("CONOUT$" if os.name == "nt" else "/dev/tty", "w", encoding="utf-8")
            target = console
        except OSError:
            target = output
    try:
        print(prompt, end=" ", file=target, flush=True)
        answer = input_stream.readline().strip().casefold()
    finally:
        if console is not None:
            console.close()
    return answer in {"y", "yes"}, "interactive"


def prepare(
    qa_root: Path,
    *,
    modules: list[str] | None = None,
    allow_write: bool = False,
    run_id: str | None = None,
    input_stream: TextIO = sys.stdin,
    output: TextIO = sys.stderr,
) -> dict[str, Any]:
    qa_root = qa_root.resolve()
    run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    namespace = f"dltk-{run_id}"
    path = _ledger_path(qa_root, run_id)
    if path.exists():
        raise MockDataError(f"mock-data run id already exists: {run_id}")
    try:
        config = load_execution_config(qa_root / EXECUTION / "config.yaml")
        plan = build_plan(qa_root, modules)
    except (MockDataError, OSError, TypeError, ValueError) as exc:
        failed = {
            "version": 1, "run_id": run_id, "namespace": namespace,
            "environment": "unknown", "modules": modules or [], "data_sources": [],
            "estimated_records": 0, "created": [], "status": "planning_failed",
            "events": [{"at": _utc_now(), "action": "plan", "status": "failed", "reason": str(exc)}],
        }
        _write_ledger(path, failed)
        raise
    ledger = {
        "version": 1,
        "run_id": run_id,
        "namespace": namespace,
        "environment": config["active_environment"],
        "modules": plan["modules"],
        "data_sources": plan["data_sources"],
        "estimated_records": plan["estimated_records"],
        "created": [],
        "reused": [],
        "events": [],
        "status": "planned",
        "dependent_case_ids": plan["dependent_case_ids"],
        "blocked_case_ids": plan.get("blocked_case_ids", {}),
    }
    if not plan["steps"]:
        blocked = bool(plan.get("blocked_case_ids"))
        ledger["status"] = "planning_blocked" if blocked else "not_required"
        ledger["events"].append({
            "at": _utc_now(), "action": "write_authorization",
            "decision": "blocked" if blocked else "not_required",
        })
        _write_ledger(path, ledger)
        return {**ledger, "ledger_path": str(path), "authorized": not blocked, "runtime_variables": {}}
    if is_protected_environment(str(config["active_environment"])):
        ledger["status"] = "blocked"
        ledger["events"].append({"at": _utc_now(), "action": "write_authorization", "decision": "protected_environment"})
        _write_ledger(path, ledger)
        raise MockDataError(f"mock-data writes are forbidden in protected environment {config['active_environment']!r}")
    if not is_safe_environment(str(config["active_environment"])):
        ledger["status"] = "blocked"
        ledger["events"].append({"at": _utc_now(), "action": "write_authorization", "decision": "unapproved_environment"})
        _write_ledger(path, ledger)
        raise MockDataError(f"mock-data writes are forbidden in unapproved environment {config['active_environment']!r}")
    try:
        targets = _target_summaries(qa_root, plan)
    except (MockDataError, OSError, TypeError, ValueError) as exc:
        ledger["status"] = "blocked"
        ledger["events"].append({"at": _utc_now(), "action": "target_validation", "status": "failed", "reason": str(exc)})
        _write_ledger(path, ledger)
        raise MockDataError(str(exc)) from exc
    ledger["targets"] = targets
    print(
        f"Mock data write: environment={config['active_environment']} modules={','.join(plan['modules']) or 'all'} "
        f"targets={','.join(targets)} estimated_records={plan['estimated_records']}",
        file=output,
    )
    confirmed, method = _confirmation(
        f"Environment: {config['active_environment']}\nModules: {','.join(plan['modules']) or 'all'}\n"
        f"Targets: {','.join(targets)}\nEstimated records: {plan['estimated_records']}\n"
        "Allow this run to insert mock data? [y/N]",
        allowed=allow_write, input_stream=input_stream, output=output,
    )
    ledger["events"].append({
        "at": _utc_now(), "action": "write_authorization",
        "decision": "allowed" if confirmed else "denied", "method": method,
    })
    if not confirmed:
        ledger["status"] = "write_denied"
        _write_ledger(path, ledger)
        return {**ledger, "ledger_path": str(path), "authorized": False}
    variables = _runtime_variables(qa_root, namespace)
    for step in plan["steps"]:
        definitions = step.get("runtime_variables", {})
        for name, definition in definitions.items() if isinstance(definitions, dict) else []:
            if not str(name).startswith("DLTK_DATA_") or not isinstance(definition, dict):
                raise MockDataError(f"mock-data step {step['id']} has an invalid runtime variable")
            if definition.get("kind") == "namespace":
                value = namespace
                maximum_length = definition.get("max_length")
                if isinstance(maximum_length, int) and maximum_length > 0 and len(value) > maximum_length:
                    digest = hashlib.sha256(f"{namespace}|{step['id']}|{name}".encode("utf-8")).hexdigest()
                    value = digest[:maximum_length]
                variables[str(name)] = value
            elif definition.get("kind") == "numeric_namespace":
                digest = hashlib.sha256(f"{namespace}|{step['id']}".encode("utf-8")).digest()
                variables[str(name)] = str((int.from_bytes(digest[:4], "big") & 0x7FFFFFFF) or 1)
            elif definition.get("kind") == "uuid_namespace":
                digest = hashlib.sha256(f"{namespace}|{step['id']}|{name}".encode("utf-8")).hexdigest()
                variables[str(name)] = f"{digest[:8]}-{digest[8:12]}-4{digest[13:16]}-a{digest[17:20]}-{digest[20:32]}"
            elif definition.get("kind") == "objectid_namespace":
                variables[str(name)] = hashlib.sha256(
                    f"{namespace}|{step['id']}|{name}".encode("utf-8")
                ).hexdigest()[:24]
            else:
                raise MockDataError(f"mock-data step {step['id']} has an unsupported runtime variable kind")
    ledger["runtime_variables"] = {
        name: value for name, value in variables.items() if name.startswith("DLTK_DATA_")
    }
    _write_ledger(path, ledger)

    def persist_owned_runtime_variables() -> None:
        ledger["runtime_variables"] = {
            name: value for name, value in variables.items()
            if name.startswith(("DLTK_DATA_", "DLTK_MOCK_DATA_READY"))
        }
        _write_ledger(path, ledger)

    completed_steps: set[str] = set()
    for step in plan["steps"]:
        variables.pop(STEP_EXISTS_ENV, None)
        try:
            changes = _execute_script(qa_root, str(step.get("precheck", "")), variables, f"mock-data precheck {step['id']}")
            exists = changes.get(STEP_EXISTS_ENV)
            if exists not in {"true", "false"}:
                raise MockDataError(f"mock-data precheck {step['id']} must set {STEP_EXISTS_ENV} to true or false")
            if exists == "true":
                _execute_script(
                    qa_root, str(step.get("setup_verification", "")), variables,
                    f"mock-data setup verification {step['id']}",
                )
                ledger["reused"].append({
                    "id": step["id"], "module": step["module"], "case_id": step["case_id"],
                    "ownership": step["ownership"], "fingerprint": step["fingerprint"],
                })
                ledger["events"].append({
                    "at": _utc_now(), "action": "create", "step_id": step["id"], "status": "reused",
                })
                completed_steps.add(str(step["id"]))
                _write_ledger(path, ledger)
                continue
        except MockDataError as exc:
            ledger["status"] = "generation_failed"
            ledger["events"].append({
                "at": _utc_now(), "action": "precheck", "step_id": step["id"], "status": "failed", "reason": str(exc),
            })
            _write_ledger(path, ledger)
            raise
        possible = _frozen_created_step(step, "possible")
        ledger["created"].append(possible)
        ledger["events"].append({
            "at": _utc_now(), "action": "create", "step_id": step["id"], "status": "started",
        })
        _write_ledger(path, ledger)
        try:
            _execute_script(qa_root, str(step["script"]), variables, f"mock-data setup {step['id']}")
            persist_owned_runtime_variables()
            _execute_script(
                qa_root, str(step.get("setup_verification", "")), variables,
                f"mock-data setup verification {step['id']}",
            )
        except MockDataError as exc:
            ledger["status"] = "generation_failed"
            ledger["events"].append({
                "at": _utc_now(), "action": "create", "step_id": step["id"], "status": "failed", "reason": str(exc),
            })
            _write_ledger(path, ledger)
            raise
        possible["creation_status"] = "created"
        completed_steps.add(str(step["id"]))
        ledger["events"].append({"at": _utc_now(), "action": "create", "step_id": step["id"], "status": "created"})
        ledger["status"] = "created"
        _write_ledger(path, ledger)
    requirements = {
        case_id: {
            str(step["id"]) for step in plan["steps"] if case_id in step.get("dependent_case_ids", [])
        }
        for case_id in plan["dependent_case_ids"]
    }
    ready_case_ids = sorted(
        case_id for case_id, required in requirements.items()
        if case_id not in plan.get("blocked_case_ids", {}) and required <= completed_steps
    )
    for case_id in ready_case_ids:
        variables[mock_data_ready_env(case_id)] = namespace
    if ready_case_ids and len(ready_case_ids) == len(plan["dependent_case_ids"]):
        variables[READY_ENV] = namespace
    ledger["ready_case_ids"] = ready_case_ids
    if ledger["status"] == "planned":
        ledger["status"] = "reused"
    persist_owned_runtime_variables()
    return {**ledger, "ledger_path": str(path), "authorized": True}


def _latest_cleanup_candidate(qa_root: Path, run_id: str | None) -> Path:
    root = qa_root / RESULT_DIRECTORY
    if run_id:
        path = _ledger_path(qa_root, run_id)
        if not path.is_file():
            raise MockDataError(f"mock-data run does not exist: {run_id}")
        return path
    candidates: list[Path] = []
    for path in sorted(root.glob("*.json"), key=lambda item: item.stat().st_mtime_ns, reverse=True) if root.is_dir() else []:
        try:
            value = load_data(path)
        except (OSError, TypeError, ValueError):
            continue
        if isinstance(value, dict) and value.get("created") and value.get("status") not in {"cleaned", "not_required"}:
            candidates.append(path)
    if not candidates:
        raise MockDataError("mock-data cleanup target does not exist")
    return candidates[0]


def cleanup(
    qa_root: Path,
    *,
    modules: list[str] | None = None,
    run_id: str | None = None,
    allow_cleanup: bool = False,
    input_stream: TextIO = sys.stdin,
    output: TextIO = sys.stderr,
) -> dict[str, Any]:
    qa_root = qa_root.resolve()
    config = load_execution_config(qa_root / EXECUTION / "config.yaml")
    path = _latest_cleanup_candidate(qa_root, run_id)
    ledger = load_data(path)
    if not isinstance(ledger, dict):
        raise MockDataError(f"mock-data ledger must contain an object: {path}")
    if is_protected_environment(str(config["active_environment"])):
        ledger.setdefault("events", []).append({
            "at": _utc_now(), "action": "cleanup_authorization", "decision": "protected_environment",
        })
        ledger["status"] = "cleanup_blocked"
        _write_ledger(path, ledger)
        raise MockDataError(f"mock-data cleanup is forbidden in protected environment {config['active_environment']!r}")
    if not is_safe_environment(str(config["active_environment"])):
        ledger.setdefault("events", []).append({
            "at": _utc_now(), "action": "cleanup_authorization", "decision": "unapproved_environment",
        })
        ledger["status"] = "cleanup_blocked"
        _write_ledger(path, ledger)
        raise MockDataError(f"mock-data cleanup is forbidden in unapproved environment {config['active_environment']!r}")
    if str(config["active_environment"]) != str(ledger.get("environment")):
        ledger.setdefault("events", []).append({
            "at": _utc_now(), "action": "cleanup_target_validation", "status": "failed",
            "reason": "active environment differs from the creation environment",
        })
        ledger["status"] = "cleanup_blocked"
        _write_ledger(path, ledger)
        raise MockDataError(
            f"mock-data cleanup environment {config['active_environment']!r} does not match creation environment {ledger.get('environment')!r}"
        )
    selected = set(str(value) for value in modules or [])
    if selected:
        ledger_modules = {str(value) for value in ledger.get("modules", [])}
        direct = {value for value in selected if value in ledger_modules}
        remaining_selectors = selected - direct
        selected_ids = set(direct)
        if remaining_selectors:
            directories = _module_directories(qa_root, list(remaining_selectors))
            for directory in directories:
                endpoint_path = directory / "endpoints.yaml"
                document = load_data(endpoint_path) if endpoint_path.is_file() else {}
                selected_ids.add(str(document.get("module", directory.name)) if isinstance(document, dict) else directory.name)
    else:
        selected_ids = set(str(value) for value in ledger.get("modules", []))
    already_cleaned = {str(value) for value in ledger.get("cleaned_step_ids", [])}
    created = [
        step for step in ledger.get("created", [])
        if isinstance(step, dict) and str(step.get("module")) in selected_ids and str(step.get("id")) not in already_cleaned
    ]
    if not created:
        ledger.setdefault("events", []).append({
            "at": _utc_now(), "action": "cleanup_authorization", "decision": "not_required",
            "modules": sorted(selected_ids),
        })
        remaining = {
            str(step.get("id")) for step in ledger.get("created", []) if isinstance(step, dict)
        } - already_cleaned
        ledger["status"] = "partially_cleaned" if remaining else "cleaned"
        _write_ledger(path, ledger)
        return {**ledger, "ledger_path": str(path), "authorized": True}
    try:
        frozen_plan = {
            "data_sources": sorted({
                str(step.get("data_source")) for step in created if step.get("data_source") != "public-api"
            }),
            "steps": created,
        }
        current_targets = _target_summaries(qa_root, frozen_plan)
        expected_targets = {
            str(value).split("(", 1)[0]: str(value) for value in ledger.get("targets", [])
        }
        if any(expected_targets.get(value.split("(", 1)[0]) != value for value in current_targets):
            raise MockDataError("mock-data cleanup target configuration changed since creation")
    except (MockDataError, OSError, TypeError, ValueError) as exc:
        ledger.setdefault("events", []).append({
            "at": _utc_now(), "action": "cleanup_target_validation", "status": "failed", "reason": str(exc),
        })
        ledger["status"] = "cleanup_blocked"
        _write_ledger(path, ledger)
        raise MockDataError(str(exc)) from exc
    print(
        f"Mock data cleanup: environment={config['active_environment']} run_id={ledger.get('run_id')} "
        f"modules={','.join(sorted(selected_ids)) or 'all'} records={sum(int(step.get('estimated_records', 1)) for step in created)}",
        file=output,
    )
    confirmed, method = _confirmation(
        f"Environment: {config['active_environment']}\nRun: {ledger.get('run_id')}\n"
        f"Modules: {','.join(sorted(selected_ids)) or 'all'}\n"
        f"Records: {sum(int(step.get('estimated_records', 1)) for step in created)}\n"
        "Delete only the mock data created by this run? [y/N]",
        allowed=allow_cleanup, input_stream=input_stream, output=output,
    )
    ledger.setdefault("events", []).append({
        "at": _utc_now(), "action": "cleanup_authorization",
        "decision": "allowed" if confirmed else "denied", "method": method,
        "modules": sorted(selected_ids),
    })
    if not confirmed:
        ledger["status"] = "retained"
        _write_ledger(path, ledger)
        return {**ledger, "ledger_path": str(path), "authorized": False}
    variables = _runtime_variables(qa_root, str(ledger.get("namespace", "")))
    stored_runtime = ledger.get("runtime_variables", {})
    variables.update({
        str(name): str(value)
        for name, value in stored_runtime.items()
        if str(name).startswith("DLTK_DATA_")
    } if isinstance(stored_runtime, dict) else {})
    failures: list[str] = []
    cleaned: list[str] = []
    blocked_dependencies: set[str] = set()
    for step in reversed(created):
        step_id = str(step.get("id", "<unknown>"))
        if step_id in blocked_dependencies:
            reason = f"mock-data cleanup {step_id} was retained because a dependent cleanup failed"
            failures.append(reason)
            blocked_dependencies.update(str(value) for value in step.get("depends_on", []))
            ledger["events"].append({
                "at": _utc_now(), "action": "cleanup", "step_id": step_id,
                "status": "blocked", "reason": reason,
            })
            continue
        try:
            cleanup_script = _decode_script(step.get("cleanup_b64"), f"mock-data cleanup {step_id}")
            verification_script = _decode_script(
                step.get("cleanup_verification_b64"), f"mock-data cleanup verification {step_id}",
            )
            _execute_script(qa_root, cleanup_script, variables, f"mock-data cleanup {step_id}")
            _execute_script(
                qa_root, verification_script, variables,
                f"mock-data cleanup verification {step_id}",
            )
            cleaned.append(step_id)
            ledger["events"].append({"at": _utc_now(), "action": "cleanup", "step_id": step_id, "status": "cleaned"})
        except MockDataError as exc:
            failures.append(str(exc))
            blocked_dependencies.update(str(value) for value in step.get("depends_on", []))
            ledger["events"].append({
                "at": _utc_now(), "action": "cleanup", "step_id": step_id, "status": "failed", "reason": str(exc),
            })
    all_cleaned = set(ledger.get("cleaned_step_ids", [])) | set(cleaned)
    ledger["cleaned_step_ids"] = sorted(all_cleaned)
    remaining = {
        str(step.get("id")) for step in ledger.get("created", []) if isinstance(step, dict)
    } - all_cleaned
    ledger["status"] = "cleanup_failed" if failures else "partially_cleaned" if remaining else "cleaned"
    _write_ledger(path, ledger)
    if failures:
        raise MockDataError("; ".join(failures))
    return {**ledger, "ledger_path": str(path), "authorized": True}


def command(
    qa_root: Path,
    *,
    action: str,
    modules: list[str] | None,
    run_id: str | None,
    allowed: bool,
) -> int:
    try:
        result = (
            prepare(qa_root, modules=modules, allow_write=allowed, run_id=run_id)
            if action == "generate"
            else cleanup(qa_root, modules=modules, run_id=run_id, allow_cleanup=allowed)
        )
    except (MockDataError, OSError, TypeError, ValueError) as exc:
        print(f"ERROR: {redact(str(exc))}", file=sys.stderr)
        message = str(exc)
        if "forbidden" in message or "authorization" in message:
            return 7
        if "does not exist" in message or "no mock-data run" in message:
            return 4
        return 10
    print(
        f"mock_data action={action} status={result['status']} run_id={result['run_id']} "
        f"created={len(result.get('created', []))} ledger={result['ledger_path']}"
    )
    return 0
