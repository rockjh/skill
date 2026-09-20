#!/usr/bin/env python3
"""Extract a small, deterministic endpoint inventory from a local OpenAPI file."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

from .execution_config import initialize_execution_layout
from .qa_paths import EXECUTION
from .tool_version import GENERATOR_VERSION

HTTP_METHODS = {
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "head",
    "options",
    "trace",
}


CASE_DOCS_START = "<!-- AUTO_CASES_START -->"
CASE_DOCS_END = "<!-- AUTO_CASES_END -->"
MODULE_DOCS_START = "<!-- AUTO_MODULE_START -->"
MODULE_DOCS_END = "<!-- AUTO_MODULE_END -->"
REVIEW_RE = re.compile(r"review-[A-Za-z0-9_.-]+", re.IGNORECASE)


def slugify_tag(tag: str) -> str:
    """Return a stable ASCII directory/module id for an OpenAPI tag."""

    normalized = unicodedata.normalize("NFKD", tag).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", normalized).strip("-").lower()
    if not slug:
        slug = f"tag-{hashlib.sha256(tag.encode('utf-8')).hexdigest()[:10]}"
    return slug


def display_directory(value: str, fallback: str) -> str:
    """Keep business-language names while making one safe path segment."""

    name = unicodedata.normalize("NFC", str(value)).strip()
    name = re.sub(r"[\\/\x00-\x1f<>:\"|?*]+", "-", name)
    name = re.sub(r"\s+", "-", name).strip(" .-")
    return name or fallback


def module_directory(module: dict[str, Any], module_id: str) -> str:
    """Return the optional display directory, falling back to the stable id."""

    value = module.get("directory", module_id)
    directory = display_directory(str(value), module_id)
    if directory != str(value).strip() or directory in {".", ".."}:
        raise ValueError(f"module {module_id} directory must be one safe path segment")
    return directory


def operation_tags(operation: dict[str, Any], method: str, route: str) -> list[str]:
    tags = operation.get("tags")
    if tags is None:
        return []
    if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag.strip() for tag in tags):
        raise ValueError(f"operation {method.upper()} {route} has invalid Swagger tags")
    result = list(dict.fromkeys(tag.strip() for tag in tags))
    return result


def primary_tag_override(endpoint: dict[str, Any], module_map: dict[str, Any]) -> str | None:
    overrides = module_map.get("primary_tags", {}) if isinstance(module_map, dict) else {}
    if not isinstance(overrides, dict):
        return None
    keys = (
        endpoint.get("id"),
        endpoint.get("operation_id"),
        f"{endpoint.get('method')} {endpoint.get('path')}",
    )
    for key in keys:
        if key in overrides and isinstance(overrides[key], str) and overrides[key].strip():
            return overrides[key].strip()
    return None


def load_document(path: Path) -> dict[str, Any]:
    try:
        if path.suffix.lower() == ".json":
            return json.loads(path.read_text(encoding="utf-8"))
        import yaml  # type: ignore[import-not-found]

        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("the OpenAPI document must be an object")
        return loaded
    except ModuleNotFoundError as exc:
        raise SystemExit("YAML input requires an existing PyYAML installation") from exc
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(f"cannot parse {path}: {exc}") from exc


def stable_id(method: str, path: str, operation: dict[str, Any]) -> str:
    operation_id = operation.get("operationId")
    if isinstance(operation_id, str) and operation_id.strip():
        raw = f"{operation_id}_{method}_{path}"
    else:
        raw = f"{method}_{path}"
    value = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_")
    return value.upper() or "ENDPOINT"


def resolve_value(document: dict[str, Any], value: Any, seen: set[str] | None = None) -> Any:
    """Resolve local JSON pointers while preserving the original $ref marker."""

    seen = set() if seen is None else seen
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/") and ref not in seen:
            target: Any = document
            try:
                for token in ref[2:].split("/"):
                    target = target[token.replace("~1", "/").replace("~0", "~")]
            except (KeyError, TypeError):
                target = None
            if isinstance(target, dict):
                merged = copy.deepcopy(target)
                merged.update({key: item for key, item in value.items() if key != "$ref"})
                resolved = {
                    key: resolve_value(document, item, seen | {ref})
                    for key, item in merged.items()
                }
                resolved["$ref"] = ref
                return resolved
        return {key: resolve_value(document, item, seen) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_value(document, item, seen) for item in value]
    return value


def parameter_summary(parameter: Any) -> dict[str, Any]:
    if not isinstance(parameter, dict):
        return {"raw": parameter}
    result = {
        "name": parameter.get("name"),
        "in": parameter.get("in"),
        "required": bool(parameter.get("required", False)),
    }
    if isinstance(parameter.get("$ref"), str):
        result["$ref"] = parameter["$ref"]
    if "schema" in parameter:
        result["schema"] = parameter["schema"]
    elif "type" in parameter:
        result["type"] = parameter["type"]
    for key in (
        "enum", "minimum", "maximum", "minLength", "maxLength", "pattern", "format",
        "example", "default", "items", "style", "explode", "allowReserved",
    ):
        if key in parameter:
            result[key] = parameter[key]
    return result


def extract(path: Path, document: dict[str, Any]) -> dict[str, Any]:
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise SystemExit("OpenAPI document has no object-valued paths field")

    version = document.get("openapi") or document.get("swagger") or "unknown"
    tag_descriptions = {
        str(item.get("name")): str(item.get("description", "")).strip()
        for item in document.get("tags", [])
        if isinstance(item, dict) and item.get("name") and str(item.get("description", "")).strip()
    }
    security_schemes = {}
    components = document.get("components")
    if isinstance(components, dict):
        security_schemes = resolve_value(document, components.get("securitySchemes", {})) or {}
    elif isinstance(document.get("securityDefinitions"), dict):
        security_schemes = resolve_value(document, document.get("securityDefinitions", {})) or {}

    def security_headers(security: Any) -> list[str]:
        names: set[str] = set()
        requirements = security if isinstance(security, list) else []
        for requirement in requirements:
            if not isinstance(requirement, dict):
                continue
            for scheme_name in requirement:
                scheme = security_schemes.get(scheme_name) if isinstance(security_schemes, dict) else None
                if not isinstance(scheme, dict):
                    continue
                if str(scheme.get("type", "")).casefold() == "apikey":
                    location = str(scheme.get("in", "")).casefold()
                    header = str(scheme.get("name", "")).strip()
                    if location == "header" and header:
                        names.add(header)
                    elif location == "cookie":
                        names.add("Cookie")
                elif str(scheme.get("type", "")).casefold() in {"http", "oauth2", "openidconnect"}:
                    names.add("Authorization")
        return sorted(names, key=str.casefold)

    endpoints: list[dict[str, Any]] = []
    for route, path_item in paths.items():
        if not isinstance(route, str) or not isinstance(path_item, dict):
            continue
        inherited_parameters = path_item.get("parameters", [])
        for method, operation in path_item.items():
            if method.lower() not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            parameters = []
            for item in [*inherited_parameters, *operation.get("parameters", [])]:
                parameters.append(parameter_summary(resolve_value(document, item)))
            body = resolve_value(document, operation.get("requestBody"))
            if body is None:
                body = next(
                    (resolve_value(document, item) for item in operation.get("parameters", [])
                     if isinstance(item, dict) and item.get("in") == "body"),
                    None,
                )
            endpoint_tags = operation_tags(operation, method, route)
            endpoint_id = stable_id(method.upper(), route, operation)
            endpoint = {
                "id": endpoint_id,
                "method": method.upper(),
                "path": route,
                "operation_id": operation.get("operationId"),
                "tags": endpoint_tags,
                "summary": operation.get("summary"),
                "parameters": parameters,
                "request_body": body,
                "responses": resolve_value(document, operation.get("responses", {})),
                "security": operation.get("security", document.get("security")),
                "evidence": {
                    "source_kind": "openapi",
                    "file": str(path),
                    "symbol": str(operation.get("operationId") or f"{method.upper()} {route}"),
                    "line": 1,
                    "endpoint_scope": [endpoint_id],
                    "confidence": "high",
                },
            }
            declared_security_headers = security_headers(endpoint["security"])
            if declared_security_headers:
                endpoint["security_headers"] = declared_security_headers
            for extension in (
                "x-permissions", "x-permission", "x-roles", "x-role",
                "x-idempotent", "x-safety",
            ):
                if extension in operation:
                    endpoint[extension] = resolve_value(document, operation[extension])
            explicit_primary = operation.get("x-primary-tag") or operation.get("primary_tag")
            if isinstance(explicit_primary, str) and explicit_primary.strip():
                endpoint["primary_tag"] = explicit_primary.strip()
            endpoint["obligations"] = constraint_obligations(endpoint)
            endpoints.append(endpoint)

    endpoints.sort(key=lambda item: (item["path"], item["method"]))
    source_sha = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    return {
        "version": 1,
        "source": {
            "file": str(path),
            "spec_version": str(version),
            "sha256": source_sha,
        },
        "components": {
            "schemas": resolve_value(document, document.get("components", {}).get("schemas", {}))
            if isinstance(document.get("components"), dict)
            else resolve_value(document, document.get("definitions", {})),
            "parameters": resolve_value(document, document.get("components", {}).get("parameters", {}))
            if isinstance(document.get("components"), dict)
            else resolve_value(document, document.get("parameters", {})),
            "responses": resolve_value(document, document.get("components", {}).get("responses", {}))
            if isinstance(document.get("components"), dict)
            else resolve_value(document, document.get("responses", {})),
        },
        "tag_descriptions": tag_descriptions,
        "endpoints": endpoints,
    }


def module_tag(module: dict[str, Any]) -> str:
    tags = module.get("swagger_tags")
    if isinstance(tags, list) and len(tags) == 1 and isinstance(tags[0], str) and tags[0].strip():
        return tags[0].strip()
    # Untagged OpenAPI operations can be owned by an explicitly configured
    # module. Keep a stable display value in generated manifests without
    # pretending it came from the specification.
    return str(module.get("tag") or module.get("name") or module.get("id") or "untagged").strip()


def validate_module_map(manifest: dict[str, Any], module_map: dict[str, Any]) -> dict[str, str]:
    modules = module_map.get("modules") if isinstance(module_map, dict) else None
    if not isinstance(modules, list) or not modules:
        raise ValueError("module map must contain a non-empty modules list")
    endpoint_tags = {
        tag
        for endpoint in manifest["endpoints"]
        for tag in endpoint.get("tags", [])
    }
    tag_to_module: dict[str, str] = {}
    ids: set[str] = set()
    directories: set[str] = set()
    for module in modules:
        if not isinstance(module, dict) or not module.get("id"):
            raise ValueError("every module map entry must have an id")
        module_id = str(module["id"])
        if module_id in ids:
            raise ValueError(f"module map repeats id {module_id}")
        ids.add(module_id)
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", module_id):
            raise ValueError(f"module id {module_id} is not a stable ASCII slug")
        tags = module.get("swagger_tags")
        if tags is not None and (
            not isinstance(tags, list)
            or len(tags) > 1
            or any(not isinstance(tag, str) or not tag.strip() for tag in tags)
        ):
            raise ValueError(f"module {module.get('id')} swagger_tags must be a list of zero or one non-empty values")
        if isinstance(tags, list) and len(tags) == 1:
            tag = tags[0].strip()
            if tag in tag_to_module:
                raise ValueError(f"Swagger tag {tag!r} is assigned to multiple modules")
            tag_to_module[tag] = module_id
            if "name" in module and module.get("name") != tag:
                raise ValueError(f"module {module_id} name must preserve the original Swagger tag text {tag!r}")
        directory = module_directory(module, module_id)
        if isinstance(tags, list) and len(tags) == 1:
            expected_directory = display_directory(tags[0].strip(), module_id)
            if directory != expected_directory:
                raise ValueError(
                    f"module {module_id} directory must come from Swagger tag {tags[0].strip()!r}: "
                    f"expected {expected_directory!r}"
                )
        if directory in directories:
            raise ValueError(f"module map repeats directory {directory}")
        directories.add(directory)
    missing = sorted(endpoint_tags - set(tag_to_module))
    extra = sorted(set(tag_to_module) - endpoint_tags)
    if missing:
        raise ValueError("Swagger tags are not assigned to modules: " + ", ".join(missing))
    if extra:
        raise ValueError("module map contains tags not present in OpenAPI: " + ", ".join(extra))
    return tag_to_module


def partition_manifest(manifest: dict[str, Any], module_map: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    modules = module_map.get("modules") if isinstance(module_map, dict) else None
    tag_to_module = validate_module_map(manifest, module_map)
    tag_descriptions = manifest.get("tag_descriptions", {})
    grouped = {str(module.get("id")): [] for module in modules if isinstance(module, dict) and module.get("id")}

    for endpoint in manifest["endpoints"]:
        tags = endpoint.get("tags", [])
        primary = endpoint.get("primary_tag") if isinstance(endpoint.get("primary_tag"), str) else None
        if len(tags) == 1:
            primary = primary or tags[0]
        elif len(tags) > 1:
            primary = primary or primary_tag_override(endpoint, module_map)
            if not primary:
                raise ValueError(
                    f"operation {endpoint['method']} {endpoint['path']} has multiple Swagger tags; "
                    "declare primary_tags in module-map.yaml"
                )
            if primary not in tags:
                raise ValueError(
                    f"operation {endpoint['method']} {endpoint['path']} primary tag {primary!r} "
                    f"is not one of {tags!r}"
                )
        else:
            module_id = fallback_module_for_endpoint(endpoint, module_map)
            if not module_id:
                raise ValueError(
                    f"operation {endpoint['method']} {endpoint['path']} has no Swagger tag; "
                    "map it with operation_ids/path_prefixes or declare one default module"
                )
            primary = module_tag(next(module for module in module_map["modules"] if str(module.get("id")) == module_id))
        module_id = module_id if not tags else tag_to_module.get(primary)
        if not module_id:
            raise ValueError(f"operation {endpoint['method']} {endpoint['path']} tag {primary!r} is not assigned")
        endpoint["primary_tag"] = primary
        endpoint["swagger_tag"] = primary
        if isinstance(tag_descriptions, dict) and str(tag_descriptions.get(primary, "")).strip():
            endpoint["tag_description"] = str(tag_descriptions[primary]).strip()
        endpoint["module"] = module_id
        grouped[module_id].append(endpoint)
    return grouped


def fallback_module_for_endpoint(endpoint: dict[str, Any], module_map: dict[str, Any]) -> str | None:
    """Resolve an untagged operation using explicit, reviewable ownership rules."""

    modules = module_map.get("modules", []) if isinstance(module_map, dict) else []
    matches: list[str] = []
    operation_id = str(endpoint.get("operation_id") or "")
    path = str(endpoint.get("path") or "")
    for module in modules:
        if not isinstance(module, dict) or not module.get("id"):
            continue
        ids = module.get("operation_ids", [])
        prefixes = module.get("path_prefixes", [])
        if isinstance(ids, str):
            ids = [ids]
        if isinstance(prefixes, str):
            prefixes = [prefixes]
        if operation_id and operation_id in {str(item) for item in ids if item is not None}:
            matches.append(str(module["id"]))
            continue
        if any(path == str(prefix) or path.startswith(str(prefix).rstrip("/") + "/") for prefix in prefixes):
            matches.append(str(module["id"]))
    if len(matches) == 1:
        return matches[0]
    defaults = [
        str(module["id"])
        for module in modules
        if isinstance(module, dict) and module.get("id") and module.get("default") is True
    ]
    if len(defaults) == 1:
        return defaults[0]
    if len(modules) == 1 and isinstance(modules[0], dict) and modules[0].get("id"):
        return str(modules[0]["id"])
    return None


def reference_names(value: Any) -> set[tuple[str, str]]:
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
            found.update(reference_names(child))
    elif isinstance(value, list):
        for child in value:
            found.update(reference_names(child))
    return found


def local_components(manifest: dict[str, Any], endpoints: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    components = manifest.get("components") if isinstance(manifest.get("components"), dict) else {}
    result: dict[str, dict[str, Any]] = {"schemas": {}, "parameters": {}, "responses": {}}
    needed = reference_names(endpoints)
    pending = list(needed)
    seen: set[tuple[str, str]] = set()
    while pending:
        kind, name = pending.pop()
        if (kind, name) in seen:
            continue
        seen.add((kind, name))
        source = components.get(kind, {})
        if not isinstance(source, dict) or name not in source:
            continue
        value = source[name]
        result[kind][name] = value
        pending.extend(reference_names(value) - seen)
    return result


def generate_module_map(manifest: dict[str, Any]) -> dict[str, Any]:
    """Create a reviewable one-module-per-Tag map from an offline inventory."""

    tags = sorted({tag for endpoint in manifest["endpoints"] for tag in endpoint.get("tags", [])})
    used: set[str] = set()
    used_directories: set[str] = set()
    modules: list[dict[str, Any]] = []
    for tag in tags:
        base = slugify_tag(tag)
        module_id = base
        if module_id in used:
            module_id = f"{base}-{hashlib.sha256(tag.encode('utf-8')).hexdigest()[:8]}"
        used.add(module_id)
        descriptions = manifest.get("tag_descriptions", {})
        description = str(descriptions.get(tag, "")).strip() if isinstance(descriptions, dict) else ""
        directory = display_directory(tag, module_id)
        if directory in used_directories:
            raise ValueError(
                f"OpenAPI Tags produce the same safe module directory {directory!r}; "
                "rename the conflicting Tag names"
            )
        used_directories.add(directory)
        item = {"id": module_id, "name": tag, "directory": directory, "swagger_tags": [tag]}
        if description:
            item["business_scope"] = description
        modules.append(item)
    if any(not endpoint.get("tags") for endpoint in manifest["endpoints"]):
        modules.append(
            {
                "id": "untagged",
                "name": "未标记接口",
                "directory": "未标记接口",
                "default": True,
                "path_prefixes": [],
                "swagger_tags": [],
                "business_scope": "OpenAPI 未声明 Tag 的接口；请在 module-map.yaml 中补充路径或 operationId 归属。",
            }
        )
    return {"version": 1, "modules": modules}


def markdown_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).replace("|", "\\|")


def canonical_fingerprint(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write_if_changed(path: Path, content: str) -> bool:
    if path.is_file() and path.read_text(encoding="utf-8", errors="strict") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def chinese_text(value: str) -> bool:
    return bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", value))


def endpoint_subject(endpoint: dict[str, Any]) -> str:
    tags = endpoint.get("tags") if isinstance(endpoint.get("tags"), list) else []
    raw = str(endpoint.get("primary_tag") or (tags[0] if tags else "")).strip()
    raw = re.sub(r"(?i)(?:[-_ ]?(?:controller|api))$", "", raw).strip("-_ ")
    if raw:
        return raw.upper() if re.fullmatch(r"[A-Za-z]{1,5}", raw) else raw
    operation = str(endpoint.get("operation_id") or "")
    acronyms = re.findall(r"(?<![A-Za-z])([A-Z]{2,6})(?![a-z])", operation)
    if acronyms:
        return acronyms[-1]
    parts = [part for part in str(endpoint.get("path", "")).split("/") if part and not part.startswith("{")]
    return (parts[-1].replace("-", " ") if parts else "接口")


def endpoint_business_action(endpoint: dict[str, Any]) -> str:
    summary = markdown_text(endpoint.get("summary"))
    if summary and summary not in {"请求成功", "操作成功", "成功"}:
        return re.sub(r"(?:成功受理|成功|失败)$", "", summary).strip()
    operation = str(endpoint.get("operation_id") or "").lower()
    route = str(endpoint.get("path") or "").lower()
    method = str(endpoint.get("method") or "GET").upper()
    subject = endpoint_subject(endpoint)
    key = operation + " " + route
    if "import" in key or "upload" in key:
        return f"导入 {subject} 文件"
    if "export" in key or "download" in key:
        return f"导出 {subject} 文件"
    if ("batch" in key or "bulk" in key) and "delete" in key:
        return f"批量删除 {subject} 信息"
    if ("batch" in key or "bulk" in key) and any(word in key for word in ("group", "change", "update")):
        return f"批量变更 {subject} 分组" if "group" in key else f"批量更新 {subject} 信息"
    if any(word in key for word in ("listbypage", "page", "pagination")):
        return f"分页查询 {subject} 信息"
    verbs = {
        "GET": "查询",
        "POST": "新增",
        "PUT": "更新",
        "PATCH": "变更",
        "DELETE": "删除",
    }
    return f"{verbs.get(method, '调用')} {subject} 信息"


def business_case_title(
    endpoint: dict[str, Any],
    scenario: str,
    detail: str = "",
    status: int | None = None,
) -> str:
    action = endpoint_business_action(endpoint)
    if scenario == "success":
        return f"{action}{'成功受理' if status == 202 else '成功'}"
    if scenario == "validation":
        return f"{action}失败：{detail}"
    if scenario == "query":
        return f"{action}：{detail}"
    if scenario == "file":
        return f"{action}失败：{detail}"
    return f"{action}：{detail or scenario}"


def request_body_schema(endpoint: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    request_body = endpoint.get("request_body", {}) if isinstance(endpoint.get("request_body"), dict) else {}
    content = request_body.get("content", {}) if isinstance(request_body.get("content"), dict) else {}
    media_type = next(iter(content), None)
    media = content.get(media_type, {}) if media_type else {}
    schema = media.get("schema", {}) if isinstance(media, dict) and isinstance(media.get("schema"), dict) else {}
    if not schema and isinstance(request_body.get("schema"), dict):
        schema = request_body["schema"]
    return media_type, schema


CONSTRAINT_KEYS = ("enum", "pattern", "minimum", "maximum", "minLength", "maxLength", "format")


def constraint_obligations(endpoint: dict[str, Any]) -> list[dict[str, Any]]:
    """Return stable, independently checkable OpenAPI constraint obligations."""

    endpoint_key = f"{str(endpoint.get('method', '')).upper()} {endpoint.get('path', '')}"
    obligations: list[dict[str, Any]] = []

    def add(kind: str, target: str, constraint: str, evidence: Any = True) -> None:
        obligations.append({
            "id": f"{kind}:{endpoint_key}:{target}:{constraint}",
            "kind": kind.lower(),
            "target": target,
            "constraint": constraint,
            "evidence": copy.deepcopy(evidence),
        })

    for parameter in (item for item in endpoint.get("parameters", []) if isinstance(item, dict)):
        location = str(parameter.get("in", "query"))
        name = str(parameter.get("name", "parameter"))
        schema = parameter.get("schema", {}) if isinstance(parameter.get("schema"), dict) else parameter
        kind = "QUERY" if location == "query" else "VALIDATION"
        if parameter.get("required") is True and location != "path":
            add(kind, f"{location}.{name}", "required")
        for key in CONSTRAINT_KEYS:
            if key in schema:
                add(kind, f"{location}.{name}", key, schema[key])

    media_type, body_schema = request_body_schema(endpoint)

    def walk_schema(schema: dict[str, Any], prefix: str) -> None:
        properties = schema.get("properties", {}) if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required", []) if isinstance(schema.get("required"), list) else []
        for name, child in properties.items():
            if not isinstance(child, dict):
                continue
            target = f"{prefix}.{name}"
            is_file = child.get("format") == "binary"
            if name in required:
                add("FILE" if is_file else "VALIDATION", target, "missing")
            for key in CONSTRAINT_KEYS:
                if key in child and not (is_file and key in {"format", "minLength", "maxLength"}):
                    add("VALIDATION", target, key, child[key])
            if is_file:
                for constraint, keys in (
                    ("empty", ("minLength", "x-min-size")),
                    ("extension", ("x-allowed-extensions",)),
                    ("mime", ("contentMediaType", "x-allowed-mime-types")),
                    ("size", ("maxLength", "x-max-size")),
                ):
                    evidence = next((child[key] for key in keys if key in child), None)
                    if evidence is not None:
                        add("FILE", target, constraint, evidence)
            walk_schema(child, target)
            items = child.get("items")
            if isinstance(items, dict):
                walk_schema(items, target + "[]")

    walk_schema(body_schema, "body")
    if media_type and str(media_type) != "multipart/form-data":
        add("VALIDATION", "Content-Type", "content-type", media_type)
    return obligations


def _response_evidence(endpoint: dict[str, Any], status: int) -> tuple[dict[str, Any], Any]:
    responses = endpoint.get("responses", {}) if isinstance(endpoint.get("responses"), dict) else {}
    response = responses.get(str(status), responses.get(status, {}))
    response = response if isinstance(response, dict) else {}
    content = response.get("content", {}) if isinstance(response.get("content"), dict) else {}
    media = next((item for item in content.values() if isinstance(item, dict)), {})
    schema = media.get("schema", {}) if isinstance(media.get("schema"), dict) else {}
    if not schema and isinstance(response.get("schema"), dict):
        schema = response["schema"]
    example = media.get("example", response.get("example"))
    examples = media.get("examples")
    if example is None and isinstance(examples, dict):
        first = next((item for item in examples.values() if isinstance(item, dict)), None)
        example = first.get("value") if first else None
    return schema, example


def _fixed_schema_value(schema: Any) -> Any:
    if not isinstance(schema, dict):
        return None
    for key in ("const", "example", "default"):
        if key in schema:
            return copy.deepcopy(schema[key])
    enum = schema.get("enum")
    return copy.deepcopy(enum[0]) if isinstance(enum, list) and len(enum) == 1 else None


def header_environment_name(name: str) -> str:
    """Return a stable environment variable name for any required Header."""

    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", snake).strip("_").upper()
    return normalized or "HEADER_VALUE"


def _example_result_assertions(value: Any, path: str) -> list[dict[str, Any]]:
    if isinstance(value, list):
        assertion: dict[str, Any] = {"path": path, "type": "array", "length": len(value)}
        if value:
            item = value[0]
            item_type = (
                "object" if isinstance(item, dict)
                else "boolean" if isinstance(item, bool)
                else "integer" if isinstance(item, int)
                else "number" if isinstance(item, float)
                else "array" if isinstance(item, list)
                else "string"
            )
            assertion["items"] = {"type": item_type}
        return [assertion]
    if not isinstance(value, dict):
        return [{"path": path, "equals": value}]
    assertions: list[dict[str, Any]] = []
    priority = (
        "id", "resourceId", "page", "pageNum", "pageNumber", "pageSize", "size",
        "total", "totalCount", "totalElements", "totalPages", "records", "content", "items", "list",
    )
    ordered = [key for key in priority if key in value]
    ordered.extend(key for key in value if key not in ordered)
    for key in ordered:
        child = value[key]
        child_path = f"{path}.{key}"
        if isinstance(child, (dict, list)):
            nested = _example_result_assertions(child, child_path)
            if nested:
                assertions.extend(nested)
        else:
            assertions.append({"path": child_path, "equals": child})
        if key not in priority and assertions:
            break
    return assertions


def _schema_result_assertions(schema: Any, path: str) -> list[dict[str, Any]]:
    if not isinstance(schema, dict):
        return []
    fixed = _fixed_schema_value(schema)
    if fixed is not None:
        return _example_result_assertions(fixed, path)
    if schema.get("type") == "array" and isinstance(schema.get("items"), dict):
        item_schema = schema["items"]
        item_type = item_schema.get("type")
        if not item_type and isinstance(item_schema.get("properties"), dict):
            item_type = "object"
        assertion: dict[str, Any] = {"path": path, "type": "array"}
        if item_type:
            assertion["items"] = {"type": item_type}
        return [assertion] if "items" in assertion else []
    properties = schema.get("properties", {}) if isinstance(schema.get("properties"), dict) else {}
    priority = (
        "id", "resourceId", "page", "pageNum", "pageNumber", "pageSize", "size",
        "total", "totalCount", "totalElements", "totalPages", "records", "content", "items", "list",
    )
    ordered = [key for key in priority if key in properties]
    ordered.extend(key for key in properties if key not in ordered)
    assertions: list[dict[str, Any]] = []
    for key in ordered:
        assertions.extend(_schema_result_assertions(properties[key], f"{path}.{key}"))
        if assertions and key not in priority:
            break
    return assertions


def exact_response_assertions(endpoint: dict[str, Any], status: int) -> list[dict[str, Any]]:
    """Derive exact response assertions only from explicit contract values."""

    schema, example = _response_evidence(endpoint, status)
    properties = schema.get("properties", {}) if isinstance(schema.get("properties"), dict) else {}
    fixed: dict[str, Any] = {}
    if isinstance(example, dict):
        fixed.update({str(key): value for key, value in example.items() if not isinstance(value, (dict, list))})
    for key, value_schema in properties.items():
        value = _fixed_schema_value(value_schema)
        if value is not None:
            fixed.setdefault(str(key), value)
    business_key = next((key for key in ("errorCode", "code", "status") if key in fixed), None)
    if business_key is None:
        return []
    success_values = {0, "0", 200, "200", True, "true", "ok", "success", "SUCCESS", "OK"}
    if fixed[business_key] not in success_values:
        return []
    envelope_keys = {"status", "errorCode", "code", "errorMsg", "msg", "message"}
    details: list[dict[str, Any]] = []
    if isinstance(example, dict):
        for key, value in example.items():
            if key not in envelope_keys:
                details.extend(_example_result_assertions(value, f"$.{key}"))
    if not details:
        detail = next(
            ((f"$.{key}", value) for key, value in fixed.items() if key not in envelope_keys),
            None,
        )
        if detail:
            details.append({"path": detail[0], "equals": detail[1]})
    if not details:
        for key, value_schema in properties.items():
            if key not in envelope_keys:
                details.extend(_schema_result_assertions(value_schema, f"$.{key}"))
    if not details:
        return []
    return [{"path": f"$.{business_key}", "equals": fixed[business_key]}, *details]


def exact_error_assertions(endpoint: dict[str, Any], status: int) -> list[dict[str, Any]]:
    schema, example = _response_evidence(endpoint, status)
    properties = schema.get("properties", {}) if isinstance(schema.get("properties"), dict) else {}
    fixed: dict[str, Any] = {}
    if isinstance(example, dict):
        fixed.update({str(key): value for key, value in example.items() if not isinstance(value, (dict, list))})
    for key, value_schema in properties.items():
        value = _fixed_schema_value(value_schema)
        if value is not None:
            fixed.setdefault(str(key), value)
    keys = [key for key in ("status", "errorCode", "code", "errorMsg", "msg", "message") if key in fixed]
    return [{"path": f"$.{key}", "equals": fixed[key]} for key in keys[:2]]


def response_business_code_path(endpoint: dict[str, Any], status: int) -> str | None:
    schema, example = _response_evidence(endpoint, status)
    properties = schema.get("properties", {}) if isinstance(schema.get("properties"), dict) else {}
    example = example if isinstance(example, dict) else {}
    return next((f"$.{key}" for key in ("errorCode", "code", "status") if key in properties or key in example), None)


def response_array_path(endpoint: dict[str, Any], status: int) -> str | None:
    schema, _ = _response_evidence(endpoint, status)
    if schema.get("type") == "array":
        return "$"
    properties = schema.get("properties", {}) if isinstance(schema.get("properties"), dict) else {}
    for first in ("data", "result", "items", "list", "records"):
        value = properties.get(first)
        if isinstance(value, dict) and value.get("type") == "array":
            return f"$.{first}"
        nested = value.get("properties", {}) if isinstance(value, dict) and isinstance(value.get("properties"), dict) else {}
        for second in ("items", "list", "records", "content"):
            if isinstance(nested.get(second), dict) and nested[second].get("type") == "array":
                return f"$.{first}.{second}"
    return None


def inferred_scenario_matrix(endpoint: dict[str, Any]) -> dict[str, dict[str, Any]]:
    parameters = [item for item in endpoint.get("parameters", []) if isinstance(item, dict)]
    media_type, body_schema = request_body_schema(endpoint)
    query = [item for item in parameters if item.get("in") == "query"]
    body_properties = body_schema.get("properties", {}) if isinstance(body_schema.get("properties"), dict) else {}
    body_required = body_schema.get("required", []) if isinstance(body_schema.get("required"), list) else []
    non_file_required = any(
        name not in body_properties
        or not isinstance(body_properties.get(name), dict)
        or body_properties[name].get("format") != "binary"
        for name in body_required
    )
    validation_applicable = any(
        parameter.get("required") is True
        or any(
            key in (parameter.get("schema") if isinstance(parameter.get("schema"), dict) else parameter)
            for key in ("enum", "pattern", "minimum", "maximum", "minLength", "maxLength", "format")
        )
        for parameter in parameters
    ) or bool(
        non_file_required
        or any(
            isinstance(schema, dict)
            and schema.get("format") != "binary"
            and any(key in schema for key in ("enum", "pattern", "minimum", "maximum", "minLength", "maxLength", "format"))
            for schema in (
                body_properties.values()
            )
        )
    ) or bool(media_type and "415" in (endpoint.get("responses") or {}))
    properties = body_properties
    file_applicable = media_type == "multipart/form-data" or any(
        isinstance(value, dict) and value.get("format") == "binary"
        for value in properties.values()
    )
    security = endpoint.get("security")
    secured = bool(security)
    permission = (
        endpoint.get("x-permissions") or endpoint.get("x-permission")
        or endpoint.get("x-roles") or endpoint.get("x-role")
    )
    def decision(applicable: bool, reason: str) -> dict[str, Any]:
        return {"applicable": applicable, "status": "inferred" if applicable else "confirmed", "reason": reason}

    return {
        "success": decision(True, "所有可达接口默认覆盖成功路径"),
        "authentication": decision(
            secured,
            "OpenAPI security 声明了认证要求" if secured else "OpenAPI 未声明认证；管理端路径或审计 Header 不作为认证证据",
        ),
        "authorization": decision(
            bool(permission),
            "OpenAPI 权限或角色扩展声明了授权要求" if permission else "OpenAPI 未声明权限模型；等待设计文档确认",
        ),
        "validation": decision(validation_applicable, "契约声明了输入校验约束" if validation_applicable else "契约未声明可验证的输入约束"),
        "business_error": decision(False, "reviewed design rules have not declared a business error"),
        "query": decision(bool(query), "根据分页、筛选和排序查询参数推导" if query else "接口无查询参数"),
        "safety": decision(False, "reviewed design rules have not declared idempotency or concurrency behavior"),
        "file": decision(file_applicable, "multipart/form-data 或 binary schema" if file_applicable else "接口不是文件上传或下载"),
    }


def module_business_scope(
    module: dict[str, Any],
    endpoints: list[dict[str, Any]],
    manifest: dict[str, Any] | None = None,
) -> str:
    """Build a readable business summary from reviewed metadata or endpoint summaries."""

    for key in ("business_scope", "description", "business", "summary"):
        if str(module.get(key, "")).strip():
            return markdown_text(module[key])
    tag = module_tag(module) if module.get("swagger_tags") else str(module.get("name", module.get("id", "")))
    descriptions = manifest.get("tag_descriptions", {}) if isinstance(manifest, dict) else {}
    if isinstance(descriptions, dict) and str(descriptions.get(tag, "")).strip():
        return markdown_text(descriptions[tag])
    summaries = list(dict.fromkeys(
        markdown_text(endpoint.get("summary"))
        for endpoint in endpoints
        if markdown_text(endpoint.get("summary"))
    ))
    if summaries:
        return "、".join(summaries)
    return f"{markdown_text(module.get('name', tag))}相关接口及自动化测试业务"


def case_title(case: dict[str, Any], endpoint: dict[str, Any]) -> str:
    for key in ("title", "display_name", "name"):
        value = markdown_text(case.get(key))
        if value:
            return value
    scenario = markdown_text(case.get("scenario") or case.get("category"))
    return business_case_title(endpoint, scenario or "success")


def case_description(case: dict[str, Any], endpoint: dict[str, Any], title: str) -> str:
    for key in ("description", "summary"):
        value = markdown_text(case.get(key))
        if value:
            return value
    method = markdown_text(endpoint.get("method", "HTTP"))
    path = markdown_text(endpoint.get("path", "/"))
    return f"验证“{title}”场景调用 {method} {path} 后的状态码、业务结果和响应字段符合约定。"


def render_case_documentation(cases: list[dict[str, Any]], endpoints: list[dict[str, Any]]) -> str:
    endpoint_by_id = {str(item.get("id")): item for item in endpoints if item.get("id")}
    lines = [
        "## 自动化用例",
        "",
        "| 用例 ID | 场景 | 接口 | 预期状态 |",
        "| --- | --- | --- | --- |",
    ]
    for case in cases:
        endpoint = endpoint_by_id.get(str(case.get("endpoint_id")), {})
        if case.get("id") and endpoint:
            expected = case.get("expected") if isinstance(case.get("expected"), dict) else {}
            lines.append(
                f"| `{case['id']}` | {case_title(case, endpoint)} | "
                f"`{markdown_text(endpoint.get('method', 'HTTP'))} {markdown_text(endpoint.get('path', '/'))}` | "
                f"{markdown_text(expected.get('http_status', '待确认'))} |"
            )
    lines.extend([
        "",
        "```mermaid",
        "sequenceDiagram",
        "    participant C as 自动化用例",
        "    participant B as Bruno",
        "    participant S as 业务服务",
        "    C->>B: 准备请求",
        "    B->>S: 调用模块接口",
        "    S-->>B: 返回响应",
        "    B-->>C: 校验契约与业务结果",
        "```",
        "",
        CASE_DOCS_START,
    ])
    if not cases:
        lines.extend(["", "当前尚未登记自动化用例。"])
    for case in cases:
        case_id = str(case.get("id", "")).strip()
        endpoint = endpoint_by_id.get(str(case.get("endpoint_id")), {})
        if not case_id or not endpoint:
            continue
        title = case_title(case, endpoint)
        description = case_description(case, endpoint, title)
        method = markdown_text(endpoint.get("method", "HTTP"))
        route = markdown_text(endpoint.get("path", "/"))
        lines.extend([
            "",
            f"<!-- CASE_START: {case_id} -->",
            f"### {title}",
            "",
            f"- 用例 ID：`{case_id}`",
            f"- 接口：`{method} {route}`",
            "",
            f"简短描述：{description}",
        ])
        if case.get("flow_required") is True or endpoint.get("flow_required") is True:
            lines.extend([
                "",
                "```mermaid",
                "sequenceDiagram",
                "    participant C as 自动化用例",
                "    participant B as Bruno",
                "    participant S as 业务服务",
                f"    C->>B: 准备“{title}”流程步骤",
                f"    B->>S: {method} {route}",
                "    S-->>B: 返回流程响应",
                "    B-->>C: 捕获并校验流程变量",
                "```",
            ])
        lines.append(f"<!-- CASE_END: {case_id} -->")
    lines.extend([CASE_DOCS_END, ""])
    return "\n".join(lines)


def render_module_overview(
    module: dict[str, Any],
    endpoints: list[dict[str, Any]],
    manifest: dict[str, Any] | None = None,
) -> str:
    module_id = str(module.get("id", ""))
    tag = module_tag(module) if module.get("swagger_tags") else str(module.get("name", module_id))
    name = markdown_text(module.get("name", tag))
    scope = module_business_scope(module, endpoints, manifest)
    lines = [
        MODULE_DOCS_START,
        f"# {name}模块",
        "",
        f"- 模块 ID：`{module_id}`",
        f"- Swagger Tag：`{tag}`",
        f"- 业务范围：{scope}",
        "",
        "## 模块内容",
        "",
        "- `endpoints.yaml`：模块接口清单和场景决策。",
        "- `parameters.yaml`：模块独立的请求参数。",
        "- `definitions.yaml`：模块独立的数据定义。",
        "- `responses.yaml`：模块独立的响应定义。",
        "- `logic.yaml`：设计文档确认的业务路径和预期。",
        "- `cases.yaml`：自动化用例、预期结果和断言。",
        "- `flows.yaml`：有序业务操作流程。",
        "- `exclusions.yaml`：经审批的排除项及原因。",
        "",
        "## 接口清单",
        "",
        "| 方法 | 路径 | 业务说明 |",
        "| --- | --- | --- |",
    ]
    lines.extend(
        f"| {markdown_text(item.get('method'))} | `{markdown_text(item.get('path'))}` | "
        f"{markdown_text(item.get('summary')) or '待补充'} |"
        for item in endpoints
    )
    lines.extend([MODULE_DOCS_END, ""])
    return "\n".join(lines)


def render_module_document(
    module: dict[str, Any],
    endpoints: list[dict[str, Any]],
    cases: list[dict[str, Any]] | None = None,
    manifest: dict[str, Any] | None = None,
) -> str:
    return (
        render_module_overview(module, endpoints, manifest).rstrip()
        + "\n\n"
        + render_case_documentation(cases or [], endpoints).rstrip()
        + "\n"
    )


def update_case_documentation(
    existing: str,
    cases: list[dict[str, Any]],
    endpoints: list[dict[str, Any]],
) -> str:
    generated = render_case_documentation(cases, endpoints).rstrip()
    pattern = re.compile(
        rf"(?ms)^## 自动化用例[ \t]*\n.*?{re.escape(CASE_DOCS_END)}[ \t]*(?:\n|$)"
    )
    if pattern.search(existing):
        return pattern.sub(generated + "\n", existing, count=1).rstrip() + "\n"
    return existing.rstrip() + "\n\n" + generated + "\n"


def update_module_document(
    existing: str,
    module: dict[str, Any],
    endpoints: list[dict[str, Any]],
    cases: list[dict[str, Any]],
    manifest: dict[str, Any] | None = None,
) -> str:
    overview = render_module_overview(module, endpoints, manifest).rstrip()
    overview_pattern = re.compile(
        rf"(?ms){re.escape(MODULE_DOCS_START)}.*?{re.escape(MODULE_DOCS_END)}[ \t]*(?:\n|$)"
    )
    if overview_pattern.search(existing):
        updated = overview_pattern.sub(overview + "\n", existing, count=1)
        return update_case_documentation(updated, cases, endpoints)
    generated = render_module_document(module, endpoints, cases, manifest).rstrip()
    legacy = re.fullmatch(r"(?s)\s*# .+?\s+Swagger tag:\s*`[^`]+`\s*", existing)
    if legacy or not existing.strip():
        return generated + "\n"
    return generated + "\n\n## 原有补充说明\n\n" + existing.strip() + "\n"


def render_contracts_readme(index: dict[str, Any], modules: dict[str, tuple[dict[str, Any], list[dict[str, Any]], str]]) -> str:
    lines = [
        "# API 自动化测试契约",
        "",
        "本目录按 Swagger Tag 隔离接口契约、业务逻辑、自动化用例和执行流程。",
        "",
        "## 模块总览",
    ]
    for entry in index.get("modules", []):
        module_id = str(entry.get("id", ""))
        module, endpoints, scope = modules[module_id]
        name = markdown_text(module.get("name", module_id))
        directory = str(entry.get("directory", module_id)).replace(" ", "%20")
        lines.extend([
            "",
            f"<!-- MODULE_START: {module_id} -->",
            f"### [{name}](modules/{directory}/CASES.md)",
            "",
            f"- 业务范围：{scope}",
            f"- 包含内容：接口 {len(endpoints)} 个，以及本模块独立的参数、定义、响应、逻辑、用例、流程和排除项。",
            f"- Swagger Tag：`{entry.get('swagger_tag', '')}`",
            "",
            "| 方法 | 路径 | 业务说明 |",
            "| --- | --- | --- |",
        ])
        lines.extend(
            f"| {markdown_text(item.get('method'))} | `{markdown_text(item.get('path'))}` | "
            f"{markdown_text(item.get('summary')) or '待补充'} |"
            for item in endpoints
        )
        lines.append(f"<!-- MODULE_END: {module_id} -->")
    return "\n".join(lines).rstrip() + "\n"


def seed_contract_cases(
    endpoint: dict[str, Any],
    coverage_profile: str = "contract-draft",
    security_profile: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Create business-readable draft cases that are provable from OpenAPI."""

    endpoint_id = str(endpoint.get("id", "ENDPOINT"))
    responses = endpoint.get("responses", {}) if isinstance(endpoint.get("responses"), dict) else {}
    success_status = next((int(code) for code in responses if str(code).isdigit() and 200 <= int(code) < 300), None)
    error_status = next((int(code) for code in responses if str(code) in {"400", "422"}), None)
    obligations = {
        str(item.get("id")): item
        for item in endpoint.get("obligations", constraint_obligations(endpoint))
        if isinstance(item, dict) and item.get("id")
    }

    fixture_prefix = re.sub(r"[^A-Za-z0-9]+", "_", endpoint_id).strip("_").upper() or "ENDPOINT"

    def fixture_variable(fixture_type: str) -> str:
        return f"{fixture_prefix}_FILE_{fixture_type.replace('-', '_').upper()}"

    def obligation_ids(target: str, *constraints: str) -> list[str]:
        wanted = set(constraints)
        return [
            obligation_id
            for obligation_id, item in obligations.items()
            if item.get("target") == target and item.get("constraint") in wanted
        ]

    def schema_value(schema: dict[str, Any], name: str = "value") -> Any:
        for key in ("example", "default"):
            if key in schema:
                return copy.deepcopy(schema[key])
        enum = schema.get("enum")
        if isinstance(enum, list) and enum:
            return copy.deepcopy(enum[0])
        if schema.get("format") == "binary":
            return {"file": "{{" + fixture_variable("legal") + "}}"}
        kind = str(schema.get("type", "string")).lower()
        if kind == "integer":
            return max(int(schema.get("minimum", 1)), 1)
        if kind == "number":
            return max(float(schema.get("minimum", 1)), 1.0)
        if kind == "boolean":
            return True
        if kind == "array":
            items = schema.get("items", {}) if isinstance(schema.get("items"), dict) else {}
            return [schema_value(items, name)]
        if kind == "object" or isinstance(schema.get("properties"), dict):
            properties = schema.get("properties", {}) if isinstance(schema.get("properties"), dict) else {}
            required = schema.get("required", []) if isinstance(schema.get("required"), list) else []
            names = required or list(properties)[:1]
            return {
                str(field): schema_value(properties.get(field, {}) if isinstance(properties.get(field), dict) else {}, str(field))
                for field in names
            }
        if kind == "string" and not schema.get("pattern"):
            value = {
                "date": "2026-01-01",
                "date-time": "2026-01-01T00:00:00Z",
                "email": "qa@example.com",
                "uuid": "00000000-0000-4000-8000-000000000001",
            }.get(str(schema.get("format", "")).lower(), f"test-{name}")
            minimum = max(int(schema.get("minLength", 0)), 1)
            maximum = int(schema.get("maxLength", max(len(value), minimum)))
            return value.ljust(minimum, "x")[:maximum]
        return f"review-{name}"

    def set_parameter(request: dict[str, Any], parameter: dict[str, Any], value: Any) -> None:
        location = str(parameter.get("in", "query"))
        name = str(parameter.get("name", "parameter"))
        if location == "query":
            request.setdefault("query", {})[name] = value
        elif location == "header":
            request.setdefault("headers", {})[name] = value
        elif location == "path":
            request.setdefault("path_parameters", {})[name] = value

    parameters = [item for item in endpoint.get("parameters", []) if isinstance(item, dict)]
    success_request: dict[str, Any] = {}
    for parameter in parameters:
        if parameter.get("required") is True or parameter.get("example") is not None or parameter.get("examples"):
            schema = parameter.get("schema", {}) if isinstance(parameter.get("schema"), dict) else parameter
            name = str(parameter.get("name", "parameter"))
            examples = parameter.get("examples") if isinstance(parameter.get("examples"), dict) else {}
            example = next(
                (
                    item.get("value") if isinstance(item, dict) and "value" in item else item
                    for item in examples.values()
                ),
                None,
            )
            if parameter.get("in") == "header" and parameter.get("required") is True:
                variable = {
                    "authorization": "AUTH_TOKEN",
                    "cookie": "SESSION_COOKIE",
                }.get(name.casefold(), header_environment_name(name))
                value = "{{" + variable + "}}"
            elif parameter.get("example") is not None:
                value = copy.deepcopy(parameter["example"])
            elif example is not None:
                value = copy.deepcopy(example)
            elif parameter.get("in") == "path" and not any(key in schema for key in ("example", "default", "enum")):
                value = f"review-{name}"
            else:
                value = schema_value(schema, name)
            set_parameter(success_request, parameter, value)

    media_type, body_schema = request_body_schema(endpoint)
    if body_schema:
        success_request["body_type"] = media_type or "application/json"
        success_request["body"] = schema_value(body_schema, "body")

    success_assertions = exact_response_assertions(endpoint, success_status) if success_status is not None else []
    error_assertions = exact_error_assertions(endpoint, error_status) if error_status is not None else []

    def build(
        suffix: str,
        scenario: str,
        detail: str,
        status: int,
        request: dict[str, Any],
        coverage_ids: list[str] | None = None,
        assertions: list[dict[str, Any]] | None = None,
        fixture_type: str | None = None,
    ) -> dict[str, Any]:
        failure_scenarios = {"validation", "authentication", "authorization", "business_error", "file"}
        case_assertions = copy.deepcopy(
            assertions if assertions is not None else (
                error_assertions if scenario in failure_scenarios else success_assertions
            )
        )
        exact = bool(case_assertions)
        if scenario == "query" and 200 <= status < 300:
            result_roots = ("$.data", "$.result", "$.items", "$.list", "$.records", "$.content", "$.page", "$.total")
            exact = exact and any(
                str(item.get("path", "")) == "$"
                or any(str(item.get("path", "")).startswith(root) for root in result_roots)
                for item in case_assertions
                if isinstance(item, dict)
            )
        review_values = sorted(set(REVIEW_RE.findall(json.dumps(request, ensure_ascii=False))))
        review_required = not exact or bool(review_values)
        built = {
            "id": f"{endpoint_id}_{suffix}",
            "title": business_case_title(endpoint, scenario, detail, status),
            "description": f"验证{endpoint_business_action(endpoint)}的{detail or '正常'}场景。",
            "endpoint_id": endpoint_id,
            "scenario": scenario,
            "status": "draft" if review_required else "runnable",
            "source": "openapi",
            "review_required": review_required,
            "request": copy.deepcopy(request),
            "expected": {"http_status": status},
            "coverage_ids": list(dict.fromkeys(coverage_ids or [])),
            "assertions": case_assertions,
        }
        if fixture_type:
            built["fixture_type"] = fixture_type
        if review_values:
            built["review_reasons"] = {
                value: "OpenAPI、执行配置和测试 Fixture 均未提供可用请求值"
                for value in review_values
            }
        elif not exact:
            built["review_reason"] = "OpenAPI 不足以生成精确协议断言"
        if review_required:
            evidence = copy.deepcopy(endpoint.get("evidence")) if isinstance(endpoint.get("evidence"), dict) else {
                "source_kind": "openapi",
                "file": "openapi",
                "symbol": endpoint_id,
                "line": 1,
                "endpoint_scope": [endpoint_id],
                "confidence": "medium",
            }
            built["manual_confirmation"] = {
                "automation_blocker": (
                    "No exact config, fixture, or support-source request value was found"
                    if review_values else "No exact protocol assertion could be derived from OpenAPI"
                ),
                "search_records": [evidence],
            }
        return built

    seeded = [build("SUCCESS", "success", "", success_status, success_request)] if success_status is not None else []
    for parameter in parameters:
        name = str(parameter.get("name", "参数"))
        schema = parameter.get("schema", {}) if isinstance(parameter.get("schema"), dict) else parameter
        suffix = slugify_tag(name).replace("-", "_").upper()
        location = str(parameter.get("in", "query"))
        if error_status is not None and parameter.get("required") is True and location in {"query", "header"}:
            request = copy.deepcopy(success_request)
            request.get("query" if location == "query" else "headers", {}).pop(name, None)
            if location == "header":
                request["omit_common_headers"] = list(dict.fromkeys([
                    *request.get("omit_common_headers", []),
                    name,
                ]))
            detail = f"缺少必填 Header {name}" if location == "header" else f"缺少必填参数 {name}"
            seeded.append(build(
                f"MISSING_{suffix}", "validation", detail, error_status, request,
                obligation_ids(f"{location}.{name}", "required"),
            ))
        if error_status is not None and isinstance(schema.get("enum"), list):
            request = copy.deepcopy(success_request)
            set_parameter(request, parameter, "__INVALID_ENUM__")
            seeded.append(build(
                f"INVALID_{suffix}", "validation", f"参数 {name} 使用非法枚举值", error_status, request,
                obligation_ids(f"{location}.{name}", "enum"),
            ))
        if error_status is not None and schema.get("pattern"):
            request = copy.deepcopy(success_request)
            set_parameter(request, parameter, "__INVALID_PATTERN__")
            seeded.append(build(
                f"INVALID_PATTERN_{suffix}", "validation", f"参数 {name} 不符合格式", error_status, request,
                obligation_ids(f"{location}.{name}", "pattern"),
            ))
        for keyword, delta, label in (
            ("minimum", -1, "小于最小值"),
            ("maximum", 1, "大于最大值"),
            ("minLength", -1, "短于最小长度"),
            ("maxLength", 1, "超过最大长度"),
        ):
            boundary = schema.get(keyword)
            if error_status is None or not isinstance(boundary, (int, float)):
                continue
            value: Any = (
                "x" * max(0, int(boundary) + delta)
                if keyword.endswith("Length")
                else boundary + delta
            )
            request = copy.deepcopy(success_request)
            set_parameter(request, parameter, value)
            seeded.append(build(
                f"INVALID_{keyword.upper()}_{suffix}", "validation",
                f"参数 {name} {label}", error_status, request,
                obligation_ids(f"{location}.{name}", keyword),
            ))
        if error_status is not None and schema.get("format"):
            request = copy.deepcopy(success_request)
            set_parameter(request, parameter, "__INVALID_FORMAT__")
            seeded.append(build(
                f"INVALID_FORMAT_{suffix}", "validation",
                f"参数 {name} 格式非法", error_status, request,
                obligation_ids(f"{location}.{name}", "format"),
            ))
        if name.lower() in {"page", "pagenum", "pagesize", "pageindex", "limit", "offset"}:
            status = error_status if error_status is not None else success_status
            if status is not None:
                request = copy.deepcopy(success_request)
                minimum = schema.get("minimum")
                value = minimum - 1 if isinstance(minimum, (int, float)) else 0
                set_parameter(request, parameter, value)
                seeded.append(build(f"BOUNDARY_{suffix}", "query", f"分页参数 {name} 取边界值 {value}", status, request))

    def body_members(
        schema: dict[str, Any],
        target_prefix: str = "body",
        access_prefix: tuple[str | int, ...] = (),
    ) -> list[tuple[str, tuple[str | int, ...], dict[str, Any], bool]]:
        result: list[tuple[str, tuple[str | int, ...], dict[str, Any], bool]] = []
        members = schema.get("properties", {}) if isinstance(schema.get("properties"), dict) else {}
        required_names = set(schema.get("required", [])) if isinstance(schema.get("required"), list) else set()
        for name, child in members.items():
            if not isinstance(child, dict):
                continue
            target = f"{target_prefix}.{name}"
            access = (*access_prefix, str(name))
            result.append((target, access, child, name in required_names))
            result.extend(body_members(child, target, access))
            items = child.get("items")
            if isinstance(items, dict):
                result.extend(body_members(items, target + "[]", (*access, 0)))
        return result

    def mutate_body(request: dict[str, Any], access: tuple[str | int, ...], value: Any = None, omit: bool = False) -> None:
        current: Any = request.get("body")
        if not isinstance(current, (dict, list)):
            return
        for index, part in enumerate(access[:-1]):
            following = access[index + 1]
            if isinstance(part, int):
                if not isinstance(current, list):
                    return
                while len(current) <= part:
                    current.append({} if isinstance(following, str) else [])
                current = current[part]
            else:
                if not isinstance(current, dict):
                    return
                expected = [] if isinstance(following, int) else {}
                if not isinstance(current.get(part), type(expected)):
                    current[part] = expected
                current = current[part]
        leaf = access[-1]
        if isinstance(leaf, int):
            if not isinstance(current, list):
                return
            while len(current) <= leaf:
                current.append(None)
            if omit:
                current.pop(leaf)
            else:
                current[leaf] = value
        elif isinstance(current, dict):
            if omit:
                current.pop(leaf, None)
            else:
                current[leaf] = value

    members = body_members(body_schema)
    for target, access, field_schema, is_required in members:
        if not is_required:
            continue
        if error_status is None:
            break
        if media_type == "multipart/form-data" and field_schema.get("format") == "binary":
            continue
        request = copy.deepcopy(success_request)
        mutate_body(request, access, omit=True)
        suffix = slugify_tag(target.removeprefix("body.").replace("[]", " item ")).replace("-", "_").upper()
        seeded.append(build(
            f"MISSING_BODY_{suffix}", "validation", f"缺少必填字段 {target}", error_status, request,
            obligation_ids(target, "missing"),
        ))
    for target, access, field_schema, _ in members:
        if error_status is None or not isinstance(field_schema, dict):
            continue
        variants: list[tuple[str, Any, str]] = []
        if isinstance(field_schema.get("enum"), list):
            variants.append(("enum", "__INVALID_ENUM__", f"字段 {target} 使用非法枚举值"))
        if field_schema.get("pattern"):
            variants.append(("pattern", "__INVALID_PATTERN__", f"字段 {target} 不符合格式"))
        if isinstance(field_schema.get("minimum"), (int, float)):
            variants.append(("minimum", field_schema["minimum"] - 1, f"字段 {target} 小于最小值"))
        if isinstance(field_schema.get("maximum"), (int, float)):
            variants.append(("maximum", field_schema["maximum"] + 1, f"字段 {target} 大于最大值"))
        if isinstance(field_schema.get("minLength"), int):
            variants.append(("minLength", "x" * max(0, field_schema["minLength"] - 1), f"字段 {target} 短于最小长度"))
        if isinstance(field_schema.get("maxLength"), int):
            variants.append(("maxLength", "x" * (field_schema["maxLength"] + 1), f"字段 {target} 超过最大长度"))
        if field_schema.get("format") and field_schema.get("format") != "binary":
            variants.append(("format", "__INVALID_FORMAT__", f"字段 {target} 格式非法"))
        for constraint, invalid, detail in variants:
            request = copy.deepcopy(success_request)
            mutate_body(request, access, invalid)
            suffix = slugify_tag(target.removeprefix("body.").replace("[]", " item ")).replace("-", "_").upper()
            seeded.append(build(
                f"INVALID_BODY_{constraint.upper()}_{suffix}", "validation", detail, error_status, request,
                obligation_ids(target, constraint),
            ))
    binary_fields = [item for item in members if item[2].get("format") == "binary"]
    if binary_fields:
        for case in seeded:
            if case.get("scenario") == "success":
                case["fixture_type"] = "legal"
    if media_type == "multipart/form-data" and binary_fields and error_status is not None:
        request = copy.deepcopy(success_request)
        for _, access, _, _ in binary_fields:
            mutate_body(request, access, omit=True)
        seeded.append(build(
            "MISSING_UPLOAD_FILE", "file", "缺少上传文件", error_status, request,
            [item for target, _, _, _ in binary_fields for item in obligation_ids(target, "missing")],
        ))
    if coverage_profile == "full-matrix":
        query_parameters = [item for item in parameters if item.get("in") == "query"]
        paging_names = {"page", "pagenum", "pageindex", "pagesize", "limit", "offset"}
        ordering_names = {"sort", "orderby", "order"}
        for parameter in query_parameters:
            name = str(parameter.get("name", "query"))
            lowered = name.lower()
            if lowered in paging_names:
                continue
            request = copy.deepcopy(success_request)
            value = (
                "__VALID_SORT__"
                if lowered in ordering_names
                else schema_value(parameter.get("schema", {}), name)
            )
            set_parameter(request, parameter, value)
            suffix = slugify_tag(name).replace("-", "_").upper()
            seeded.append(build(
                f"QUERY_{suffix}", "query", f"查询参数 {name}",
                success_status or 200, request,
            ))
        if len(query_parameters) > 1:
            request = copy.deepcopy(success_request)
            for parameter in query_parameters:
                set_parameter(
                    request,
                    parameter,
                    schema_value(parameter.get("schema", {}), str(parameter.get("name", "query"))),
                )
            seeded.append(build("QUERY_COMBINED", "query", "组合查询", success_status or 200, request))
        filter_parameter = next(
            (
                item for item in query_parameters
                if str(item.get("name", "")).lower() not in paging_names | ordering_names
                and not (
                    isinstance(item.get("schema"), dict)
                    and item["schema"].get("enum")
                )
            ),
            None,
        )
        if filter_parameter is not None:
            request = copy.deepcopy(success_request)
            set_parameter(request, filter_parameter, "__NO_MATCH__")
            seeded.append(build("QUERY_EMPTY_RESULT", "query", "查询空结果", success_status or 200, request))

        if media_type and media_type != "multipart/form-data" and "415" in responses:
            request = copy.deepcopy(success_request)
            request["body_type"] = "text/plain" if media_type != "text/plain" else "application/json"
            seeded.append(build(
                "INVALID_CONTENT_TYPE", "validation", "Content-Type 错误", 415, request,
                obligation_ids("Content-Type", "content-type"), exact_error_assertions(endpoint, 415),
            ))

        profile = security_profile or {}
        auth_profile = profile.get("auth-token", {}) if isinstance(profile, dict) else {}
        probe = auth_profile.get("probe_result", {}) if isinstance(auth_profile, dict) else {}
        if endpoint.get("security") and isinstance(probe, dict):
            auth_cases = (
                ("NO_TOKEN", "no_token_status", "缺少认证 Token", None),
                ("INVALID_TOKEN", "invalid_token_status", "认证 Token 非法", "Bearer __INVALID_TOKEN__"),
            )
            for suffix, status_key, detail, token in auth_cases:
                status = probe.get(status_key)
                if not isinstance(status, int):
                    continue
                request = copy.deepcopy(success_request)
                if token:
                    request.setdefault("headers", {})["Authorization"] = token
                else:
                    request["omit_common_headers"] = ["Authorization"]
                seeded.append(build(suffix, "authentication", detail, status, request))

        permission = (
            endpoint.get("x-permissions") or endpoint.get("x-permission")
            or endpoint.get("x-roles") or endpoint.get("x-role")
        )
        if permission and "403" in responses:
            request = copy.deepcopy(success_request)
            request.setdefault("headers", {})["Authorization"] = "Bearer {{UNAUTHORIZED_TOKEN}}"
            seeded.append(build("FORBIDDEN", "authorization", "权限不足", 403, request))

        if binary_fields and error_status is not None:
            excel_upload = any(
                any(str(value).casefold().lstrip(".") in {"xls", "xlsx"} for value in constraints.get("x-allowed-extensions", []))
                or "spreadsheet" in str(constraints.get("contentMediaType", "")).casefold()
                or constraints.get("x-excel-template") is not None
                for _, _, constraints, _ in binary_fields
            )
            file_cases = (
                ("EMPTY_UPLOAD_FILE", "上传空文件", "empty", ("minLength", "x-min-size")),
                ("INVALID_FILE_EXTENSION", "文件扩展名非法", "invalid-extension", ("x-allowed-extensions",)),
                ("INVALID_FILE_MIME", "文件 MIME 类型非法", "invalid-mime", ("contentMediaType", "x-allowed-mime-types")),
                ("OVERSIZED_UPLOAD_FILE", "文件超过声明大小", "oversized", ("maxLength", "x-max-size")),
            )
            for target, access, constraints, _ in binary_fields:
                field_suffix = "" if len(binary_fields) == 1 else "_" + slugify_tag(target).replace("-", "_").upper()
                for suffix, detail, fixture_type, evidence_keys in file_cases:
                    excel_required = excel_upload and fixture_type in {"empty", "oversized"}
                    if not excel_required and not any(key in constraints for key in evidence_keys):
                        continue
                    request = copy.deepcopy(success_request)
                    mutate_body(request, access, {"file": "{{" + fixture_variable(fixture_type) + "}}"})
                    constraint = {
                        "EMPTY_UPLOAD_FILE": "empty",
                        "INVALID_FILE_EXTENSION": "extension",
                        "INVALID_FILE_MIME": "mime",
                        "OVERSIZED_UPLOAD_FILE": "size",
                    }[suffix]
                    seeded.append(build(
                        suffix + field_suffix, "file", detail, error_status, request,
                        obligation_ids(target, constraint),
                        fixture_type=fixture_type,
                    ))
                if excel_upload:
                    for suffix, detail, fixture_type in (
                        ("HEADER_ONLY_UPLOAD_FILE", "Excel 表头下无数据", "header-only"),
                        ("MISSING_COLUMN_UPLOAD_FILE", "Excel 缺少必需列", "missing-column"),
                        ("INVALID_CONTENT_UPLOAD_FILE", "Excel 内容非法", "invalid-content"),
                    ):
                        request = copy.deepcopy(success_request)
                        mutate_body(request, access, {"file": "{{" + fixture_variable(fixture_type) + "}}"})
                        seeded.append(build(
                            suffix + field_suffix, "file", detail, error_status, request,
                            fixture_type=fixture_type,
                        ))

    empty_path = response_array_path(endpoint, success_status) if success_status is not None else None
    for case in seeded:
        if case["id"].endswith("QUERY_EMPTY_RESULT") and empty_path:
            case["assertions"] = [*success_assertions, {"path": empty_path, "length": 0}]
            case["review_required"] = False
            case["status"] = "runnable"
            case.pop("review_reason", None)

    deduplicated: dict[str, dict[str, Any]] = {}
    for case in seeded:
        key = canonical_fingerprint({
            "endpoint_id": case.get("endpoint_id"),
            "scenario": case.get("scenario"),
            "request": case.get("request"),
            "assertions": case.get("assertions"),
        })
        if key in deduplicated:
            deduplicated[key]["coverage_ids"] = list(dict.fromkeys([
                *deduplicated[key].get("coverage_ids", []),
                *case.get("coverage_ids", []),
            ]))
        else:
            deduplicated[key] = case
    return list(deduplicated.values())


def write_partitioned(
    manifest: dict[str, Any],
    module_map_path: Path,
    output_dir: Path,
    seed_cases: bool = False,
    incremental: bool = False,
    coverage_profile: str = "full-matrix",
    design_sha256: str | None = None,
) -> dict[str, Any]:
    contracts_root = output_dir.parent
    qa_root = contracts_root.parent.parent if contracts_root.parent.name == "data" else contracts_root.parent
    if coverage_profile not in {"contract-draft", "full-matrix"}:
        raise ValueError(f"unsupported coverage profile: {coverage_profile}")
    try:
        initialize_execution_layout(qa_root)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    module_map_path = module_map_path.resolve()
    output_dir = output_dir.resolve()
    module_map = load_document(module_map_path)
    grouped = partition_manifest(manifest, module_map)
    state_path = output_dir.parent / "generation-state.yaml"
    previous_state = load_document(state_path) if state_path.is_file() else {}
    previous_modules = previous_state.get("modules", {}) if isinstance(previous_state, dict) else {}
    previous_cases = previous_state.get("cases", {}) if isinstance(previous_state, dict) else {}
    current_endpoint_fingerprints = {
        str(endpoint["id"]): canonical_fingerprint({
            key: value for key, value in endpoint.items()
            if key not in {"case_ids", "cases", "scenario_matrix", "scenarios"}
        })
        for endpoint in manifest["endpoints"]
        if endpoint.get("id")
    }
    current_module_fingerprints = {
        module_id: canonical_fingerprint({
            str(endpoint.get("id")): current_endpoint_fingerprints.get(str(endpoint.get("id")))
            for endpoint in endpoints
        })
        for module_id, endpoints in grouped.items()
    }
    deleted_endpoint_ids = sorted(
        set(previous_state.get("endpoints", {})) - set(current_endpoint_fingerprints)
    ) if isinstance(previous_state, dict) and isinstance(previous_state.get("endpoints"), dict) else []
    summary: dict[str, Any] = {
        "changed_modules": [],
        "skipped_modules": [],
        "new_endpoint_ids": sorted(set(current_endpoint_fingerprints) - set(previous_state.get("endpoints", {})))
        if isinstance(previous_state, dict) and isinstance(previous_state.get("endpoints"), dict) else sorted(current_endpoint_fingerprints),
        "deleted_endpoint_ids": deleted_endpoint_ids,
        "manual_review_cases": [],
    }
    module_metadata = {
        str(item["id"]): item
        for item in module_map.get("modules", [])
        if isinstance(item, dict) and item.get("id")
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    index = {
        "version": 1,
        "source": manifest["source"],
        "module_map": str(module_map_path),
        "execution_config_file": str((qa_root / EXECUTION / "config.yaml")),
        "generation_status": "draft",
        "coverage_profile": coverage_profile,
        "inventory_endpoints": len(manifest["endpoints"]),
        "generated_cases": 0,
        "failed_modules": 0,
        "modules": [],
    }
    readme_modules: dict[str, tuple[dict[str, Any], list[dict[str, Any]], str]] = {}
    state_cases: dict[str, dict[str, Any]] = {}
    for module_id, endpoints in sorted(grouped.items()):
        module = module_metadata[module_id]
        module_dir = output_dir / module_directory(module, module_id)
        module_dir.mkdir(parents=True, exist_ok=True)
        tag = module_tag(module)
        endpoints_path = module_dir / "endpoints.yaml"
        previous_module = previous_modules.get(module_id, {}) if isinstance(previous_modules, dict) else {}
        module_unchanged = bool(
            incremental
            and previous_state.get("generator_version") == GENERATOR_VERSION
            and previous_state.get("coverage_profile") == coverage_profile
            and previous_state.get("design_sha256") == design_sha256
            and isinstance(previous_module, dict)
            and previous_module.get("contract_fingerprint") == current_module_fingerprints[module_id]
            and endpoints_path.is_file()
        )
        summary["skipped_modules" if module_unchanged else "changed_modules"].append(module_id)
        existing_endpoints_document = load_document(endpoints_path) if endpoints_path.is_file() else {}
        existing_endpoints = existing_endpoints_document.get("endpoints", []) if isinstance(existing_endpoints_document, dict) else []
        existing_by_id = {
            str(item.get("id")): item
            for item in existing_endpoints
            if isinstance(item, dict) and item.get("id")
        }
        for endpoint in endpoints:
            previous = existing_by_id.get(str(endpoint.get("id")), {})
            for key in ("case_ids", "cases", "scenario_matrix", "scenarios"):
                if key in previous:
                    endpoint[key] = previous[key]
            if "scenario_matrix" not in endpoint and "scenarios" not in endpoint:
                endpoint["scenario_matrix"] = inferred_scenario_matrix(endpoint)
        module_manifest = {
            "version": 1,
            "module": module_id,
            "name": str(module.get("name", tag)),
            "swagger_tag": tag,
            "tag_description": str(module.get("description") or module.get("business_scope") or ""),
            "source": manifest["source"],
            "endpoints": endpoints,
        }
        if not module_unchanged:
            write_if_changed(endpoints_path, render_manifest(module_manifest, endpoints_path))
        generated_artifacts = {
            "cases.yaml": {"version": 1, "module": module_id, "swagger_tag": tag, "cases": []},
            "logic.yaml": {"version": 1, "module": module_id, "swagger_tag": tag, "logic": []},
            "flows.yaml": {"version": 1, "module": module_id, "swagger_tag": tag, "flows": []},
            "exclusions.yaml": {"version": 1, "module": module_id, "swagger_tag": tag, "exclusions": []},
            "parameters.yaml": {
                "version": 1,
                "module": module_id,
                "swagger_tag": tag,
                "definitions": local_components(manifest, endpoints).get("parameters", {}),
                "parameters": [
                    {"endpoint_id": endpoint["id"], "items": endpoint.get("parameters", [])}
                    for endpoint in endpoints
                    if endpoint.get("parameters")
                ],
            },
            "definitions.yaml": {
                "version": 1,
                "module": module_id,
                "swagger_tag": tag,
                "definitions": local_components(manifest, endpoints).get("schemas", {}),
            },
            "responses.yaml": {
                "version": 1,
                "module": module_id,
                "swagger_tag": tag,
                "definitions": local_components(manifest, endpoints).get("responses", {}),
                "responses": [
                    {"endpoint_id": endpoint["id"], "items": endpoint.get("responses", {})}
                    for endpoint in endpoints
                    if endpoint.get("responses")
                ],
            },
        }
        for filename, payload in generated_artifacts.items():
            target = module_dir / filename
            if not module_unchanged and (filename in {"parameters.yaml", "definitions.yaml", "responses.yaml"} or not target.exists()):
                write_if_changed(target, render_manifest(payload, target))
        cases_path = module_dir / "cases.yaml"
        cases_document = load_document(cases_path) if cases_path.is_file() else {}
        cases = cases_document.get("cases", []) if isinstance(cases_document, dict) else []
        cases = [item for item in cases if isinstance(item, dict)] if isinstance(cases, list) else []
        for case in cases:
            case_id = str(case.get("id", ""))
            previous_case = previous_cases.get(case_id, {}) if isinstance(previous_cases, dict) else {}
            current_case_fingerprint = canonical_fingerprint({
                key: value for key, value in case.items()
                if key not in {"bru", "bru_file", "file_name", "manual_review"}
            })
            if (
                isinstance(previous_case, dict)
                and previous_case.get("fingerprint")
                and previous_case.get("fingerprint") != current_case_fingerprint
            ):
                case["manual_review"] = True
                summary["manual_review_cases"].append(case_id)
        if seed_cases:
            existing_by_case_id = {str(case.get("id")): case for case in cases if case.get("id")}
            for endpoint in endpoints:
                security_path = output_dir.parent / "security-profile.yaml"
                security_profile = load_document(security_path) if security_path.is_file() else {}
                for seeded in seed_contract_cases(endpoint, coverage_profile, security_profile):
                    existing = existing_by_case_id.get(str(seeded["id"]))
                    if existing is None:
                        cases.append(seeded)
                        existing_by_case_id[str(seeded["id"])] = seeded
                    elif existing.get("source") == "openapi" and existing.get("manual_review") is not True:
                        preserved = {
                            key: existing[key]
                            for key in ("bru", "bru_file", "file_name", "sequence", "seq")
                            if key in existing
                        }
                        existing.clear()
                        existing.update(seeded)
                        existing.update(preserved)
                endpoint["case_ids"] = list(dict.fromkeys([
                    *endpoint.get("case_ids", []),
                    *(case["id"] for case in cases if case.get("endpoint_id") == endpoint.get("id")),
                ]))
            cases_document = dict(cases_document) if isinstance(cases_document, dict) else {}
            cases_document.update({"version": 1, "module": module_id, "swagger_tag": tag, "cases": cases})
            write_if_changed(cases_path, render_manifest(cases_document, cases_path))
            module_manifest["endpoints"] = endpoints
            write_if_changed(endpoints_path, render_manifest(module_manifest, endpoints_path))
        for case in cases:
            case_id = str(case.get("id", ""))
            if case_id:
                state_cases[case_id] = {
                    "module": module_id,
                    "endpoint_id": str(case.get("endpoint_id", "")),
                    "fingerprint": canonical_fingerprint({
                        key: value for key, value in case.items()
                        if key not in {"bru", "bru_file", "file_name", "manual_review"}
                    }),
                    "manual_review": case.get("manual_review") is True,
                }
        # Business logic is populated only from reviewed design rules after
        # OpenAPI partitioning.  OpenAPI can seed request/transport cases but
        # must never become a business expectation source.
        index["generated_cases"] += len(cases)
        cases_md = module_dir / "CASES.md"
        existing_cases_md = cases_md.read_text(encoding="utf-8", errors="strict") if cases_md.is_file() else ""
        updated_cases_md = update_module_document(existing_cases_md, module, endpoints, cases, manifest)
        if not module_unchanged and updated_cases_md != existing_cases_md:
            cases_md.write_text(updated_cases_md, encoding="utf-8")
        scope = module_business_scope(module, endpoints, manifest)
        readme_modules[module_id] = (module, endpoints, scope)
        index["modules"].append(
            {
                "id": module_id,
                "name": str(module.get("name", tag)),
                "directory": module_dir.name,
                "swagger_tag": tag,
                "endpoints_file": str((module_dir / "endpoints.yaml").relative_to(output_dir.parent)),
                "logic_file": str((module_dir / "logic.yaml").relative_to(output_dir.parent)),
                "cases_file": str((module_dir / "cases.yaml").relative_to(output_dir.parent)),
                "flows_file": str((module_dir / "flows.yaml").relative_to(output_dir.parent)),
                "exclusions_file": str((module_dir / "exclusions.yaml").relative_to(output_dir.parent)),
                "parameters_file": str((module_dir / "parameters.yaml").relative_to(output_dir.parent)),
                "definitions_file": str((module_dir / "definitions.yaml").relative_to(output_dir.parent)),
                "responses_file": str((module_dir / "responses.yaml").relative_to(output_dir.parent)),
                "documentation_file": str((module_dir / "CASES.md").relative_to(output_dir.parent)),
                "endpoint_count": len(endpoints),
                "case_count": len(cases),
            }
        )
    required_header_profiles: dict[str, dict[str, Any]] = {}
    for endpoint in manifest["endpoints"]:
        for parameter in endpoint.get("parameters", []):
            if not isinstance(parameter, dict) or parameter.get("in") != "header" or parameter.get("required") is not True:
                continue
            header = str(parameter.get("name", "")).strip()
            if not header or header.casefold() in {"authorization", "cookie"}:
                continue
            required_header_profiles[f"required-header-{slugify_tag(header)}"] = {
                "type": "required-header",
                "header": header,
                "status": "probe-required",
            }
    security_profile = {
        "version": 1,
        "source": manifest["source"],
        "auth-token": {
            "type": "authentication",
            "header": "Authorization",
            "status": "probe-required",
            "security": sorted(
                {
                    canonical
                    for endpoint in manifest["endpoints"]
                    for canonical in (
                        [json.dumps(endpoint.get("security"), ensure_ascii=True, sort_keys=True)]
                        if endpoint.get("security") is not None
                        else []
                    )
                }
            ),
        },
        **required_header_profiles,
    }
    security_path = output_dir.parent / "security-profile.yaml"
    if not security_path.exists():
        security_path.write_text(render_manifest(security_profile, security_path), encoding="utf-8")
    index_path = output_dir.parent / "index.yaml"
    readme_path = output_dir.parent / "README.md"
    write_if_changed(index_path, render_manifest(index, index_path))
    write_if_changed(readme_path, render_contracts_readme(index, readme_modules))

    changed = bool(
        summary["changed_modules"]
        or summary["new_endpoint_ids"]
        or summary["deleted_endpoint_ids"]
        or summary["manual_review_cases"]
        or previous_state.get("generator_version") != GENERATOR_VERSION
        or previous_state.get("coverage_profile") != coverage_profile
        or previous_state.get("design_sha256") != design_sha256
    )
    generated_at = (
        datetime.now(timezone.utc).isoformat()
        if changed or not previous_state.get("last_generated_at")
        else previous_state["last_generated_at"]
    )
    generation_state = {
        "version": 1,
        "generator_version": GENERATOR_VERSION,
        "coverage_profile": coverage_profile,
        "openapi_sha256": manifest.get("source", {}).get("sha256"),
        "design_sha256": design_sha256,
        "last_generated_at": generated_at,
        "endpoints": {
            endpoint_id: {"fingerprint": fingerprint}
            for endpoint_id, fingerprint in sorted(current_endpoint_fingerprints.items())
        },
        "modules": {
            module_id: {
                "contract_fingerprint": current_module_fingerprints[module_id],
                "endpoint_ids": [str(endpoint.get("id")) for endpoint in grouped[module_id]],
            }
            for module_id in sorted(grouped)
        },
        "cases": dict(sorted(state_cases.items())),
        "deleted_endpoint_ids": deleted_endpoint_ids,
        "manual_review_cases": sorted(set(summary["manual_review_cases"])),
    }
    write_if_changed(state_path, render_manifest(generation_state, state_path))
    qa_lock_path = output_dir.parent / "qa-lock.yaml"
    qa_lock = {
        "version": 1,
        "openapi_sha256": generation_state["openapi_sha256"],
        "module_fingerprints": {
            key: value["contract_fingerprint"] for key, value in generation_state["modules"].items()
        },
        "case_fingerprints": {
            key: value["fingerprint"] for key, value in generation_state["cases"].items()
        },
        "generation_state_fingerprint": canonical_fingerprint(generation_state),
    }
    write_if_changed(qa_lock_path, render_manifest(qa_lock, qa_lock_path))
    return summary


def render_manifest(manifest: dict[str, Any], output: Path | None) -> str:
    if output and output.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore[import-not-found]
        except ModuleNotFoundError as exc:
            raise SystemExit("YAML output requires an existing PyYAML installation") from exc
        return yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False)
    return json.dumps(manifest, ensure_ascii=True, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path, help="local JSON or YAML OpenAPI/Swagger file")
    parser.add_argument("-o", "--output", type=Path, help="write JSON manifest to this file")
    parser.add_argument("--module-map", type=Path, help="module mapping YAML/JSON")
    parser.add_argument("--write-module-map", type=Path, help="write a one-module-per-Tag map for review")
    parser.add_argument("--output-dir", type=Path, help="write one endpoint manifest per module")
    parser.add_argument("--seed-cases", action="store_true", help="seed review-required cases implied directly by OpenAPI")
    parser.add_argument("--incremental", action="store_true", help="skip unchanged modules and preserve manually changed cases")
    parser.add_argument("--coverage-profile", choices=("contract-draft", "full-matrix"), default="full-matrix")
    args = parser.parse_args()

    if not args.spec.is_file():
        parser.error(f"offline specification does not exist: {args.spec}")
    if bool(args.module_map) != bool(args.output_dir):
        parser.error("--module-map and --output-dir must be provided together")
    if args.write_module_map and args.module_map:
        parser.error("--write-module-map cannot be combined with --module-map")
    if args.output and args.output_dir:
        parser.error("--output and --output-dir cannot be combined")
    manifest = extract(args.spec, load_document(args.spec))
    if args.write_module_map:
        args.write_module_map.parent.mkdir(parents=True, exist_ok=True)
        args.write_module_map.write_text(
            render_manifest(generate_module_map(manifest), args.write_module_map),
            encoding="utf-8",
        )
        print(f"wrote {len(generate_module_map(manifest)['modules'])} Tag modules to {args.write_module_map}")
        return 0
    if args.module_map:
        if not args.module_map.is_file():
            parser.error(f"module map does not exist: {args.module_map}")
        summary = write_partitioned(
            manifest,
            args.module_map,
            args.output_dir,
            args.seed_cases,
            args.incremental,
            args.coverage_profile,
        )
        module_count = len(load_document(args.module_map).get("modules", []))
        print(
            f"processed {len(manifest['endpoints'])} endpoints across {module_count} modules; "
            f"changed={len(summary['changed_modules'])} skipped={len(summary['skipped_modules'])}"
        )
        for endpoint_id in summary["deleted_endpoint_ids"]:
            print(f"REVIEW: endpoint {endpoint_id} was deleted; remove its registered .bru file after review", file=sys.stderr)
        for case_id in summary["manual_review_cases"]:
            print(f"REVIEW: case {case_id} was modified manually and was not overwritten", file=sys.stderr)
        return 0
    rendered = render_manifest(manifest, args.output)
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
