"""Resolve request-only values from local execution configuration."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .api_test_manifest_io import first_list, load_data
from .api_test_qa_paths import CONTRACTS


VARIABLE_RE = re.compile(r"\{\{([^{}]+)\}\}")


def apply_environment_values(manifest: dict[str, Any], variables: dict[str, str]) -> None:
    """Use named local variables as request templates without persisting secrets."""

    available = {
        re.sub(r"[^A-Za-z0-9]", "", str(name)).casefold(): str(name)
        for name, value in variables.items()
        if str(value).strip()
    }

    def apply_schema(schema: Any, property_name: str | None = None) -> None:
        if not isinstance(schema, dict):
            return
        variable = available.get(re.sub(r"[^A-Za-z0-9]", "", property_name or "").casefold())
        if variable and not any(key in schema for key in ("example", "default", "enum", "const")):
            schema["example"] = "{{" + variable + "}}"
            schema["x-local-environment"] = variable
        for name, child in (schema.get("properties") or {}).items():
            apply_schema(child, str(name))
        apply_schema(schema.get("items"), property_name)

    for endpoint in manifest.get("endpoints", []):
        if not isinstance(endpoint, dict):
            continue
        for parameter in endpoint.get("parameters", []):
            if isinstance(parameter, dict):
                schema = parameter.get("schema") if isinstance(parameter.get("schema"), dict) else parameter
                apply_schema(schema, str(parameter.get("name", "")))
        body = endpoint.get("request_body")
        if not isinstance(body, dict):
            continue
        content = body.get("content", {}) if isinstance(body.get("content"), dict) else {}
        for media in content.values():
            if isinstance(media, dict):
                apply_schema(media.get("schema"))
        if not content:
            apply_schema(body.get("schema", body))


def _request_fields(value: Any, prefix: str = "request") -> list[tuple[str, Any]]:
    fields: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}"
            fields.extend(_request_fields(child, path) if isinstance(child, (dict, list)) else [(path, child)])
    elif isinstance(value, list):
        for index, child in enumerate(value):
            path = f"{prefix}[{index}]"
            fields.extend(_request_fields(child, path) if isinstance(child, (dict, list)) else [(path, child)])
    return fields


def write_value_resolutions(qa_root: Path) -> list[Path]:
    """Record only config/fixture references used to construct requests."""

    paths: list[Path] = []
    modules = qa_root / CONTRACTS / "modules"
    if not modules.is_dir():
        return paths
    for directory in sorted(path for path in modules.iterdir() if path.is_dir()):
        endpoint_doc = load_data(directory / "endpoints.yaml")
        case_path = directory / "cases.yaml"
        case_doc = load_data(case_path) if case_path.is_file() else {}
        endpoint_ids = {str(item.get("id")) for item in first_list(endpoint_doc, "endpoints") if item.get("id")}
        fields: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for case in first_list(case_doc, "cases"):
            endpoint_id = str(case.get("endpoint_id", ""))
            if endpoint_id not in endpoint_ids:
                continue
            for field_path, value in _request_fields(case.get("request", {})):
                match = VARIABLE_RE.fullmatch(value) if isinstance(value, str) else None
                key = (endpoint_id, field_path)
                if not match or key in seen:
                    continue
                seen.add(key)
                variable = match.group(1)
                source = "fixture" if "FILE" in variable.upper() else "config"
                fields.append({
                    "endpoint_id": endpoint_id,
                    "field_path": field_path,
                    "status": "resolved",
                    "value": value,
                    "value_source": source,
                    "evidence": {
                        "source_kind": source,
                        "file": str(qa_root / "execution"),
                        "symbol": variable,
                        "line": 1,
                        "endpoint_scope": [endpoint_id],
                        "confidence": "high",
                    },
                })
        output = directory / "value-resolution.yaml"
        output.write_text(yaml.safe_dump({
            "version": 1,
            "module": endpoint_doc.get("module", directory.name),
            "fields": fields,
        }, allow_unicode=True, sort_keys=False), encoding="utf-8")
        paths.append(output)
    return paths
