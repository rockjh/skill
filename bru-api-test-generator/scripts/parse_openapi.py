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
from pathlib import Path
from typing import Any


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


DEFAULT_REQUEST_AUTH_TEMPLATE = """# Bruno 请求认证配置模板。
# 以 _env 结尾的值是 Bruno 环境变量名，不能填写真实密钥。
# 只能启用一种模式；`seres.sign` 是 `seres-sign` 的兼容别名。
version: 1
mode: seres-sign
seres-sign: true
# seres.sign: true
base_url_env: BASE_URL
modes:
  seres-sign:
    enabled: true
    algorithm: SHA256
    secret_key_env: SECRET_KEY
    access_key_env: ACCESS_KEY
    signature:
      parameters:
        # request.path 仅签名路径；request.url 签名解析后的完整 URL。
        url: request.path
        body: request.body
        query: request.query
        timestamp: timestamp
        secret_key_env: SECRET_KEY
        access_key_env: ACCESS_KEY
      append_secret: true
    headers:
      sign: sign
      timestamp: timestamp
      accesskey: accesskey
    extra_headers:
      # 自定义 Header 的值也从环境变量读取。
      # X-Tenant-Id: TENANT_ID
      # X-Service-Token:
      #   env: SERVICE_TOKEN
      #   prefix: Bearer
  bearer:
    enabled: false
    token_env: ACCESS_TOKEN
    header: Authorization
    prefix: Bearer
  oauth2:
    enabled: false
    token_env: ACCESS_TOKEN
    header: Authorization
    prefix: Bearer
    # token_url 由项目 bootstrap 负责，Bruno 只读取已刷新令牌。
    # token_url: https://example.invalid/oauth/token
  cookie:
    enabled: false
    cookie_env: SESSION_COOKIE
    header: Cookie
  api-key:
    enabled: false
    token_env: API_KEY
    header: X-API-Key
  custom:
    enabled: false
    headers:
      # X-Service-Token: SERVICE_TOKEN
      # X-Tenant-Token:
      #   env: TENANT_TOKEN
      #   prefix: Bearer
"""


CASE_DOCS_START = "<!-- AUTO_CASES_START -->"
CASE_DOCS_END = "<!-- AUTO_CASES_END -->"
MODULE_DOCS_START = "<!-- AUTO_MODULE_START -->"
MODULE_DOCS_END = "<!-- AUTO_MODULE_END -->"


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
            endpoint = {
                "id": stable_id(method.upper(), route, operation),
                "method": method.upper(),
                "path": route,
                "operation_id": operation.get("operationId"),
                "tags": endpoint_tags,
                "summary": operation.get("summary"),
                "parameters": parameters,
                "request_body": body,
                "responses": resolve_value(document, operation.get("responses", {})),
                "security": operation.get("security", document.get("security")),
            }
            explicit_primary = operation.get("x-primary-tag") or operation.get("primary_tag")
            if isinstance(explicit_primary, str) and explicit_primary.strip():
                endpoint["primary_tag"] = explicit_primary.strip()
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
        directory = display_directory(tag, module_id)
        if directory in used_directories:
            directory = f"{directory}-{hashlib.sha256(tag.encode('utf-8')).hexdigest()[:8]}"
        used_directories.add(directory)
        item = {"id": module_id, "name": tag, "directory": directory, "swagger_tags": [tag]}
        descriptions = manifest.get("tag_descriptions", {})
        if isinstance(descriptions, dict) and descriptions.get(tag):
            item["business_scope"] = str(descriptions[tag]).strip()
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
    summary = markdown_text(endpoint.get("summary"))
    scenario = markdown_text(case.get("scenario") or case.get("category"))
    if summary and scenario:
        return f"{summary}-{scenario}"
    if summary:
        return summary
    return f"自动化用例 {markdown_text(case.get('id', '未命名'))}"


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
    lines = ["## 自动化用例", "", CASE_DOCS_START]
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
        expected = case.get("expected") if isinstance(case.get("expected"), dict) else {}
        status = markdown_text(expected.get("http_status", "预期状态"))
        lines.extend([
            "",
            f"<!-- CASE_START: {case_id} -->",
            f"### {title}",
            "",
            f"- 用例 ID：`{case_id}`",
            f"- 接口：`{method} {route}`",
            "",
            f"简短描述：{description}",
            "",
            "```mermaid",
            "sequenceDiagram",
            "    participant C as 自动化用例",
            "    participant B as Bruno",
            "    participant S as 业务服务",
            f"    C->>B: 准备“{title}”请求",
            f"    B->>S: {method} {route}",
            f"    S-->>B: 返回 HTTP {status}",
            "    B-->>C: 校验状态码、业务结果和响应字段",
            "```",
            f"<!-- CASE_END: {case_id} -->",
        ])
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
        "- `logic.yaml`：源码正常路径和异常路径。",
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


def write_partitioned(manifest: dict[str, Any], module_map_path: Path, output_dir: Path) -> None:
    module_map = load_document(module_map_path)
    grouped = partition_manifest(manifest, module_map)
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
        "request_auth_file": str((output_dir.parent / "request-auth.yaml")),
        "generation_status": "draft",
        "inventory_endpoints": len(manifest["endpoints"]),
        "generated_cases": 0,
        "blocked_modules": 0,
        "modules": [],
    }
    readme_modules: dict[str, tuple[dict[str, Any], list[dict[str, Any]], str]] = {}
    for module_id, endpoints in sorted(grouped.items()):
        module = module_metadata[module_id]
        module_dir = output_dir / module_directory(module, module_id)
        module_dir.mkdir(parents=True, exist_ok=True)
        tag = module_tag(module)
        endpoints_path = module_dir / "endpoints.yaml"
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
        module_manifest = {
            "version": 1,
            "module": module_id,
            "name": str(module.get("name", tag)),
            "swagger_tag": tag,
            "source": manifest["source"],
            "endpoints": endpoints,
        }
        endpoints_path.write_text(
            render_manifest(module_manifest, endpoints_path),
            encoding="utf-8",
        )
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
            if filename in {"parameters.yaml", "definitions.yaml", "responses.yaml"} or not target.exists():
                target.write_text(render_manifest(payload, target), encoding="utf-8")
        cases_path = module_dir / "cases.yaml"
        cases_document = load_document(cases_path) if cases_path.is_file() else {}
        cases = cases_document.get("cases", []) if isinstance(cases_document, dict) else []
        cases = [item for item in cases if isinstance(item, dict)] if isinstance(cases, list) else []
        index["generated_cases"] += len(cases)
        cases_md = module_dir / "CASES.md"
        existing_cases_md = cases_md.read_text(encoding="utf-8", errors="strict") if cases_md.is_file() else ""
        updated_cases_md = update_module_document(existing_cases_md, module, endpoints, cases, manifest)
        if updated_cases_md != existing_cases_md:
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
    security_profile = {
        "version": 1,
        "status": "inferred",
        "source": manifest["source"],
        "profiles": [
            {
                "id": "openapi-default",
                "source": "openapi",
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
                "requires_runtime_confirmation": True,
            }
        ],
    }
    security_path = output_dir.parent / "security-profile.yaml"
    if not security_path.exists():
        security_path.write_text(render_manifest(security_profile, security_path), encoding="utf-8")
    auth_path = output_dir.parent / "request-auth.yaml"
    if not auth_path.exists():
        auth_path.write_text(DEFAULT_REQUEST_AUTH_TEMPLATE, encoding="utf-8")
    (output_dir.parent / "index.yaml").write_text(
        render_manifest(index, output_dir.parent / "index.yaml"),
        encoding="utf-8",
    )
    (output_dir.parent / "README.md").write_text(
        render_contracts_readme(index, readme_modules),
        encoding="utf-8",
    )


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
        write_partitioned(manifest, args.module_map, args.output_dir)
        module_count = len(load_document(args.module_map).get("modules", []))
        print(f"wrote {len(manifest['endpoints'])} endpoints across {module_count} modules")
        return 0
    rendered = render_manifest(manifest, args.output)
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
