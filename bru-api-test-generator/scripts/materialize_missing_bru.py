#!/usr/bin/env python3
"""Materialize missing draft Bruno files inside their owning Tag module."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import textwrap
from pathlib import Path
from typing import Any

from manifest_io import first_list, load_data
from parse_openapi import render_manifest, update_module_document


DEFAULT_AUTH_CONFIG = {
    "mode": "seres-sign",
    "seres-sign": True,
    "base_url_env": "BASE_URL",
    "modes": {
        "seres-sign": {
            "enabled": True,
            "algorithm": "SHA256",
            "secret_key_env": "SECRET_KEY",
            "access_key_env": "ACCESS_KEY",
            "signature": {
                "parameters": {
                    "url": "request.path",
                    "body": "request.body",
                    "query": "request.query",
                    "timestamp": "timestamp",
                    "secret_key_env": "SECRET_KEY",
                    "access_key_env": "ACCESS_KEY",
                },
                "append_secret": True,
            },
            "headers": {
                "sign": "sign",
                "timestamp": "timestamp",
                "accesskey": "accesskey",
            },
        }
    },
}


MODE_ALIASES = {
    "seres.sign": "seres-sign",
    "seres_sign": "seres-sign",
    "seres-sign": "seres-sign",
    "oauth2": "oauth2",
    "oauth2-client-credentials": "oauth2",
    "oauth2-authorization-code": "oauth2",
    "session": "cookie",
    "cookie-session": "cookie",
}
AUTH_MARKER = "bru-api-test-generator: auth-start"
AUTH_END_MARKER = "bru-api-test-generator: auth-end"


def canonical_mode(value: Any) -> str:
    name = str(value or "").strip().lower()
    return MODE_ALIASES.get(name, name)


def mode_settings(config: dict[str, Any], mode: str) -> dict[str, Any]:
    modes = config.get("modes") if isinstance(config.get("modes"), dict) else {}
    for name, value in modes.items():
        if canonical_mode(name) == mode and isinstance(value, dict):
            return value
    return {}


def configured_env_name(value: Any, default: str, label: str) -> str:
    """Return a usable Bruno environment variable name or reject config."""

    candidate = default if value is None else str(value).strip()
    if not candidate or candidate.lower() in {"none", "null"}:
        raise ValueError(f"{label} must name an environment variable")
    return candidate


def auth_mode(config: dict[str, Any]) -> str:
    explicit = config.get("mode")
    if isinstance(explicit, str) and explicit.strip():
        selected = canonical_mode(explicit)
        if config.get(explicit) is False or config.get(selected) is False:
            return "none"
        modes = config.get("modes") if isinstance(config.get("modes"), dict) else {}
        settings = mode_settings(config, selected)
        if isinstance(settings, dict) and settings.get("enabled") is False:
            return "none"
        enabled = {
            canonical_mode(name)
            for name, value in modes.items()
            if isinstance(value, dict) and value.get("enabled") is True
        }
        enabled.update(
            canonical_mode(name)
            for name, value in config.items()
            if name not in {"mode", "modes", "version", "base_url_env"} and value is True
        )
        if enabled - {selected}:
            raise ValueError("request-auth.yaml enables more than one authentication mode: " + ", ".join(sorted(enabled)))
        return selected
    modes = config.get("modes") if isinstance(config.get("modes"), dict) else {}
    enabled = [
        canonical_mode(name)
        for name, value in modes.items()
        if isinstance(value, dict) and value.get("enabled") is True
    ]
    enabled.extend(
        canonical_mode(name)
        for name, value in config.items()
        if name not in {"modes", "version", "base_url_env"} and value is True
    )
    enabled = list(dict.fromkeys(enabled))
    if len(enabled) > 1:
        raise ValueError("request-auth.yaml enables more than one authentication mode: " + ", ".join(enabled))
    selected = enabled[0] if enabled else "none"
    if mode_settings(config, selected).get("enabled") is False:
        return "none"
    return selected


def auth_script(config: dict[str, Any]) -> str:
    mode = auth_mode(config)
    base_env = configured_env_name(config.get("base_url_env"), "BASE_URL", "base_url_env")
    settings = mode_settings(config, mode)
    if mode in {"none", "disabled"}:
        return ""
    if mode in {"bearer", "bearer-token", "token", "oauth2"}:
        token_env = configured_env_name(settings.get("token_env"), "ACCESS_TOKEN", "token_env")
        header = str(settings.get("header", "Authorization"))
        prefix = str(settings.get("prefix", "Bearer"))
        value = f'`${{prefix}} ${{token}}`' if prefix else "token"
        return f'''script:pre-request {{
  // {AUTH_MARKER} {mode}
      // 从环境中读取访问令牌，避免在用例中写入敏感值。
  const token = bru.getEnvVar("{token_env}");
  if (!token) throw new Error("{token_env} must be configured in the Bruno environment");
  const prefix = {json.dumps(prefix, ensure_ascii=False)};
  req.setHeader("{header}", {value});
  // {AUTH_END_MARKER}
}}'''
    if mode == "cookie":
        cookie_env = configured_env_name(
            settings.get("cookie_env", settings.get("token_env")),
            "SESSION_COOKIE",
            "cookie_env",
        )
        header = str(settings.get("header", "Cookie"))
        return f'''script:pre-request {{
  // {AUTH_MARKER} cookie
  // 从环境中读取会话 Cookie，避免在用例中写入敏感值。
  const cookie = bru.getEnvVar("{cookie_env}");
  if (!cookie) throw new Error("{cookie_env} must be configured in the Bruno environment");
  req.setHeader("{header}", cookie);
  // {AUTH_END_MARKER}
}}'''
    if mode in {"api-key", "apikey"}:
        token_env = configured_env_name(settings.get("token_env"), "API_KEY", "token_env")
        header = str(settings.get("header", "X-API-Key"))
        return f'''script:pre-request {{
  // {AUTH_MARKER} api-key
  // 从环境中读取 API Key，避免在用例中写入敏感值。
  const token = bru.getEnvVar("{token_env}");
  if (!token) throw new Error("{token_env} must be configured in the Bruno environment");
  req.setHeader("{header}", token);
  // {AUTH_END_MARKER}
}}'''
    if mode in {"headers", "custom", "custom-headers"}:
        headers = settings.get("headers") if isinstance(settings.get("headers"), dict) else {}
        if not headers:
            raise ValueError("custom request authentication mode requires modes.custom.headers")
        lines = ["script:pre-request {", f"  // {AUTH_MARKER} custom"]
        lines.append("  // 从隔离环境读取并设置本用例所需的自定义请求头。")
        for index, (header, header_config) in enumerate(headers.items()):
            if isinstance(header_config, dict):
                env_name = configured_env_name(header_config.get("env"), "", f"custom header {header} env")
                prefix = str(header_config.get("prefix", ""))
            else:
                env_name = configured_env_name(header_config, "", f"custom header {header} env")
                prefix = ""
            variable = f"token_{index}"
            value = f'`{prefix} ${{{variable}}}`' if prefix else variable
            lines.extend([
                f'  const {variable} = bru.getEnvVar("{env_name}");',
                f'  if (!{variable}) throw new Error("{env_name} must be configured in the Bruno environment");',
                f'  req.setHeader("{header}", {value});',
            ])
        lines.extend([f"  // {AUTH_END_MARKER}", "}"])
        return "\n".join(lines)
    if mode != "seres-sign":
        raise ValueError(f"unsupported request authentication mode: {mode}")
    algorithm = str(settings.get("algorithm", "SHA256")).upper()
    if algorithm != "SHA256":
        raise ValueError("seres-sign currently supports only algorithm: SHA256")
    signature = settings.get("signature") if isinstance(settings.get("signature"), dict) else {}
    parameter_config = signature.get("parameters") if isinstance(signature.get("parameters"), dict) else {}
    signature_headers = settings.get("headers") if isinstance(settings.get("headers"), dict) else {}
    secret_env = configured_env_name(
        parameter_config.get("secret_key_env", signature.get("secret_key_env", settings.get("secret_key_env"))),
        "SECRET_KEY",
        "secret_key_env",
    )
    access_env = configured_env_name(
        parameter_config.get("access_key_env", signature.get("access_key_env", settings.get("access_key_env"))),
        "ACCESS_KEY",
        "access_key_env",
    )
    url_value = parameter_config.get("url", signature.get("url", "request.path"))
    body_value = parameter_config.get("body", signature.get("body", "request.body"))
    query_value = parameter_config.get("query", signature.get("query", "request.query"))
    url_source = str(url_value)
    body_source = str(body_value)
    query_source = str(query_value)
    timestamp_parameter = str(parameter_config.get("timestamp", signature.get("timestamp_parameter", "timestamp")))
    append_secret_value = signature.get("append_secret", True)
    append_secret = append_secret_value is not False and str(append_secret_value).lower() not in {"", "none", "false"}
    sign_header = str(signature_headers.get("sign", "sign"))
    timestamp_header = str(signature_headers.get("timestamp", "timestamp"))
    access_header = str(signature_headers.get("accesskey", "accesskey"))
    extra_headers = settings.get("extra_headers") if isinstance(settings.get("extra_headers"), dict) else {}
    extra_lines: list[str] = []
    for index, (header, header_config) in enumerate(extra_headers.items()):
        if isinstance(header_config, dict):
            env_name = configured_env_name(header_config.get("env"), "", f"extra header {header} env")
            prefix = str(header_config.get("prefix", ""))
        else:
            env_name = configured_env_name(header_config, "", f"extra header {header} env")
            prefix = ""
        variable = f"extra_header_{index}"
        value = f'`{prefix} ${{{variable}}}`' if prefix else variable
        extra_lines.extend([
            f'  const {variable} = bru.getEnvVar("{env_name}");',
            f'  if (!{variable}) throw new Error("{env_name} must be configured in the Bruno environment");',
            f'  req.setHeader("{header}", {value});',
        ])
    query_enabled = query_value is not False and query_source.lower() not in {"", "none", "false"}
    body_enabled = body_value is not False and body_source.lower() not in {"", "none", "false"}
    url_expression = "requestPath" if url_source in {"request.path", "path"} else "resolvedUrl"
    query_block = """  queryString.split("&").filter(Boolean).forEach(pair => {
    const separator = pair.indexOf("=");
    const rawKey = separator >= 0 ? pair.slice(0, separator) : pair;
    const rawValue = separator >= 0 ? pair.slice(separator + 1) : "";
    const key = decodeURIComponent(rawKey.replace(/\\+/g, " "));
    const value = decodeURIComponent(rawValue.replace(/\\+/g, " "));
    if (Object.prototype.hasOwnProperty.call(params, key)) {
      params[key] = Array.isArray(params[key]) ? params[key].concat(value) : [params[key], value];
    } else {
      params[key] = value;
    }
  });""" if query_enabled else ""
    body_line = (
        'const body = typeof rawBody === "string" ? rawBody : (rawBody ? JSON.stringify(rawBody) : "");'
        if body_enabled
        else 'const body = "";'
    )
    secret_suffix = "&${secretKey}" if append_secret else ""
    return f'''script:pre-request {{
  // {AUTH_MARKER} seres-sign
  // 从隔离环境读取地址和签名凭据。
  const CryptoJS = require("crypto-js");
  const secretKey = bru.getEnvVar("{secret_env}");
  const accessKey = bru.getEnvVar("{access_env}");
  const baseUrl = bru.getEnvVar("{base_env}");
  if (!secretKey || !accessKey || !baseUrl) {{
    throw new Error("{base_env}, {secret_env}, and {access_env} must be configured in the Bruno environment");
  }}

  const resolvedUrl = req.getUrl().replace(/^\\{{\\{{(?:{base_env}|{base_env.lower()})\\}}\\}}/, baseUrl);
  const queryIndex = resolvedUrl.indexOf("?");
  const requestUrl = queryIndex >= 0 ? resolvedUrl.slice(0, queryIndex) : resolvedUrl;
  const queryString = queryIndex >= 0 ? resolvedUrl.slice(queryIndex + 1) : "";
  const schemeIndex = requestUrl.indexOf("://");
  const pathIndex = schemeIndex >= 0 ? requestUrl.indexOf("/", schemeIndex + 3) : requestUrl.indexOf("/");
  const requestPath = pathIndex >= 0 ? requestUrl.slice(pathIndex) : "/";
  const url = {url_expression};
  const params = {{}};
{query_block}
  params["{timestamp_parameter}"] = Date.now().toString();
  Object.entries(params).forEach(([key, value]) => {{
    if (value === null || value === undefined || value === "") delete params[key];
    else if (Array.isArray(value)) params[key] = `[${{value.sort().join(",")}}]`;
  }});
  const signParams = Object.keys(params).sort().map(key => `${{key}}=${{params[key]}}`).join("&");
  const rawBody = req.getBody();
  {body_line}
  // 按参数名排序后生成 SHA-256 签名。
  const signStr = body ? `${{url}}&${{body}}&${{signParams}}{secret_suffix}` : `${{url}}&${{signParams}}{secret_suffix}`;
  const sign = CryptoJS.SHA256(signStr).toString();
  // 将签名、时间戳、访问密钥和扩展 Header 写入当前请求。
  req.setHeader("{sign_header}", sign);
  req.setHeader("{timestamp_header}", params["{timestamp_parameter}"]);
  req.setHeader("{access_header}", accessKey);
{chr(10).join(extra_lines)}
  // {AUTH_END_MARKER}
}}'''


def json_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ": "))


SCENARIO_LABELS = {
    "success": "成功",
    "authentication": "认证",
    "authorization": "权限",
    "validation": "参数校验",
    "business_error": "业务异常",
    "query": "查询",
    "safety": "安全",
    "file": "文件",
}


def safe_display_stem(value: str) -> str:
    stem = re.sub(r"[\\/\x00-\x1f<>:\"|?*]+", "-", str(value).strip())
    stem = re.sub(r"\s+", "-", stem).strip(" .-")
    return stem


def display_case_file(case: dict[str, Any], endpoint: dict[str, Any], module_name: str) -> Path:
    """Use Chinese business labels when present, while keeping names unique."""

    case_id = str(case.get("id", "case"))
    candidates = (
        case.get("display_name"),
        case.get("name"),
        case.get("title"),
        endpoint.get("summary"),
        endpoint.get("swagger_tag"),
        module_name,
    )
    label = next(
        (str(value).strip() for value in candidates if isinstance(value, str) and any(ord(char) > 127 for char in value)),
        "",
    )
    if not label:
        return Path(f"{case_id}.bru")
    scenario = str(case.get("scenario") or case.get("category") or "").strip().lower()
    suffix = SCENARIO_LABELS.get(scenario)
    if suffix and suffix not in label:
        label = f"{label}-{suffix}"
    stem = safe_display_stem(label) or "case"
    digest = hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:8]
    return Path(f"{stem}-{digest}.bru")


def request_url(endpoint: dict[str, Any], request: dict[str, Any], base_env: str = "BASE_URL") -> str:
    path = str(request.get("path") or endpoint.get("path") or "/")
    path = re.sub(r"(?<!\{)\{([^{}]+)\}(?!\})", lambda match: "{{" + match.group(1) + "}}", path)
    query = request.get("query")
    if isinstance(query, dict) and query:
        pairs = [f"{key}={{{{{key}}}}}" for key in query]
        path += "?" + "&".join(pairs)
    return "{{" + base_env + "}}" + path


def body_kind(request: dict[str, Any], body: Any) -> str:
    value = request.get("body_type") or request.get("content_type") or ""
    value = str(value).strip().lower()
    if body is None:
        return "none"
    if value in {"json", "application/json", "application/*+json"} or value.endswith("+json") or not value:
        return "json"
    if "multipart/form-data" in value or value in {"multipart", "multipart-form"}:
        return "multipart-form"
    if "x-www-form-urlencoded" in value or value in {"form", "form-urlencoded"}:
        return "form-urlencoded"
    if "graphql" in value:
        return "graphql"
    if "xml" in value or value.endswith("+xml"):
        return "xml"
    if "text/" in value or value in {"text", "raw"}:
        return "text"
    if "octet-stream" in value or value in {"binary", "file"}:
        return "file"
    return "text"


def endpoint_content_type(endpoint: dict[str, Any]) -> str | None:
    request_body = endpoint.get("request_body")
    if not isinstance(request_body, dict):
        return None
    content = request_body.get("content")
    if isinstance(content, dict):
        for value in content:
            if isinstance(value, str) and value.strip():
                return value.strip()
    content_type = request_body.get("content_type")
    return str(content_type).strip() if content_type else None


def render_body(body: Any, kind: str) -> str:
    if kind == "json":
        if isinstance(body, str):
            # Preserve raw JSON supplied by a caller; json.dumps would turn it
            # into a JSON string and change the request contract.
            return body
        return json.dumps(body, ensure_ascii=False, indent=2)
    if kind in {"text", "xml"}:
        return str(body)
    if kind == "file":
        if isinstance(body, dict):
            lines: list[str] = []
            for key, value in body.items():
                value = value.get("file") or value.get("path") if isinstance(value, dict) else value
                rendered = str(value or "")
                if not rendered.startswith("@file("):
                    rendered = f"@file({rendered})"
                lines.append(f"  {key}: {rendered}")
            return "\n".join(lines)
        rendered = str(body)
        return f"  file: {rendered if rendered.startswith('@file(') else f'@file({rendered})'}"
    if kind in {"form-urlencoded", "multipart-form"}:
        if not isinstance(body, dict):
            return str(body)
        lines: list[str] = []
        for key, value in body.items():
            rendered = str(value)
            if isinstance(value, dict) and value.get("file"):
                rendered = "@" + str(value["file"])
            lines.append(f"  {key}: {rendered}")
        return "\n".join(lines)
    if kind == "graphql":
        return str(body.get("query", "") if isinstance(body, dict) else body)
    return str(body)


def assertion_expression(assertion: dict[str, Any]) -> str | None:
    target = str(assertion.get("target") or assertion.get("kind") or "").strip().lower()
    path = str(assertion.get("path") or "")
    if target in {"header", "response.header", "headers", "response.headers"}:
        name = path.removeprefix("$response.headers.").removeprefix("$.headers.")
        if not name:
            return None
        return f"res.headers.{name}" if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else f"res.headers['{name}']"
    if target in {"cookie", "response.cookie", "cookies", "response.cookies"}:
        name = path.removeprefix("$response.cookies.").removeprefix("$.cookies.")
        if not name:
            return None
        return f"res.cookies.{name}" if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else f"res.cookies['{name}']"
    if target in {"text", "body_text", "response.body", "raw", "xml", "binary", "file"}:
        return "res.body"
    if path == "$":
        return "res.body"
    if path.startswith("$."):
        return "res.body" + path[1:]
    if path.startswith("$response.headers."):
        return f"res.headers.{path[len('$response.headers.'):]}"
    return None


def append_assertion(lines: list[str], assertion: dict[str, Any]) -> None:
    expression = assertion_expression(assertion)
    if not expression:
        return
    if "equals" in assertion:
        lines.append(f"  {expression}: eq {json_value(assertion['equals'])}")
    elif "eq" in assertion:
        lines.append(f"  {expression}: eq {json_value(assertion['eq'])}")
    elif assertion.get("exists") is True:
        lines.append(f"  {expression}: exists")
    elif "contains" in assertion:
        lines.append(f"  {expression}: contains {json_value(assertion['contains'])}")
    elif "matches" in assertion:
        lines.append(f"  {expression}: matches {json_value(assertion['matches'])}")


def render_case(case: dict[str, Any], endpoint: dict[str, Any], config: dict[str, Any]) -> str:
    request = case.get("request") if isinstance(case.get("request"), dict) else {}
    method = str(endpoint.get("method", "GET")).lower()
    base_env = str(config.get("base_url_env", "BASE_URL"))
    title = str(case.get("title") or case.get("display_name") or case.get("name") or endpoint.get("summary") or case.get("id"))
    description = str(
        case.get("description")
        or case.get("summary")
        or f"验证“{title}”场景的请求、响应和业务断言。"
    )
    body = request.get("body")
    if "content_type" not in request:
        inferred_content_type = endpoint_content_type(endpoint)
        if inferred_content_type:
            request = dict(request)
            request["content_type"] = inferred_content_type
    kind = body_kind(request, body)
    sequence = case.get("sequence", case.get("seq", 1))
    lines = [
        "meta {",
        f"  name: {case.get('id')}",
        "  type: http",
        f"  seq: {sequence}",
        "}",
        "",
        f"{method} {{",
        f"  url: {request_url(endpoint, request, base_env)}",
        f"  body: {kind}",
        "  auth: none",
        "}",
    ]
    script = auth_script(config)
    if script:
        lines.extend(["", script])
    headers = request.get("headers")
    headers = dict(headers) if isinstance(headers, dict) else {}
    content_type = request.get("content_type")
    if content_type and "Content-Type" not in headers and kind != "none":
        headers["Content-Type"] = str(content_type)
    if headers:
        lines.extend(["", "headers {"])
        lines.extend(f"  {key}: {value}" for key, value in headers.items())
        lines.append("}")
    if kind != "none":
        lines.extend(["", f"body:{kind} {{", render_body(body, kind), "}"])
        if kind == "graphql" and isinstance(body, dict) and body.get("variables") is not None:
            lines.extend(["", "body:graphql:vars {", json.dumps(body["variables"], ensure_ascii=False, indent=2), "}"])
    lines.extend(["", "assert {"])
    expected = case.get("expected") if isinstance(case.get("expected"), dict) else {}
    if expected.get("http_status") is not None:
        lines.append(f"  res.status: eq {expected['http_status']}")
    if expected.get("business_code") is not None:
        code_path = str(expected.get("business_code_path", "$.code"))
        code_assertion = {"path": code_path, "equals": expected["business_code"]}
        append_assertion(lines, code_assertion)
    assertions = case.get("assertions") if isinstance(case.get("assertions"), list) else []
    for assertion in assertions:
        if not isinstance(assertion, dict):
            continue
        append_assertion(lines, assertion)
    lines.append("}")
    lines.extend([
        "",
        "docs {",
        f"  ## 用例标题：{title}",
        "",
        f"  简短描述：{description}",
        "",
        f"  - 服务地址从 Bruno 环境变量 `{base_env}` 读取。",
        "  - 请求发送前执行配置的认证或签名逻辑。" if script else "  - 当前用例不启用认证前置逻辑。",
        "  - 响应校验覆盖状态码、业务结果和具体字段。",
        "}",
    ])
    return "\n".join(lines) + "\n"


def ensure_auth_script(content: str, config: dict[str, Any]) -> str:
    script = auth_script(config)
    if not script:
        if AUTH_MARKER not in content:
            return content
        nested_pattern = re.compile(
            rf"(?ms)^[ \t]*\{{[ \t]*\n\s*// {re.escape(AUTH_MARKER)}[^\n]*\n.*?"
            rf"^[ \t]*// {re.escape(AUTH_END_MARKER)}[ \t]*\n[ \t]*\}}[ \t]*(?:\n|$)"
        )
        updated = nested_pattern.sub("", content, count=1)
        standalone_pattern = re.compile(
            rf"(?ms)^[ \t]*script:pre-request[ \t]*\{{[ \t]*\n\s*// {re.escape(AUTH_MARKER)}[^\n]*\n.*?"
            rf"^[ \t]*// {re.escape(AUTH_END_MARKER)}[ \t]*\n[ \t]*\}}[ \t]*(?:\n|$)"
        )
        return standalone_pattern.sub("", updated, count=1)
    generated_opening = script.find("{")
    generated_closing = script.rfind("}")
    if generated_opening < 0 or generated_closing <= generated_opening:
        raise ValueError("generated authentication script is malformed")
    body = textwrap.dedent(script[generated_opening + 1:generated_closing]).strip("\n")
    if AUTH_MARKER in content:
        pattern = re.compile(
            rf"(?ms)^(?P<indent>[ \t]*)// {re.escape(AUTH_MARKER)}[^\n]*\n.*?"
            rf"^(?P=indent)// {re.escape(AUTH_END_MARKER)}[ \t]*$"
        )
        match = pattern.search(content)
        if match is None:
            raise ValueError("existing generated authentication block has no auth-end marker")
        replacement = textwrap.indent(body, match.group("indent"))
        return pattern.sub(lambda _: replacement, content, count=1)
    existing_position = content.find("script:pre-request")
    if existing_position >= 0:
        opening = content.find("{", existing_position)
        if opening < 0:
            raise ValueError("cannot merge authentication into the existing Bruno pre-request script")
        indented = "\n".join(f"  {line}" if line else "" for line in body.splitlines())
        insertion = f"\n  {{\n{indented}\n  }}"
        return content[:opening + 1] + insertion + content[opening + 1:]
    lines = content.splitlines()
    insert_at = next(
        (index for index, line in enumerate(lines) if line.startswith(("headers ", "body:", "assert "))),
        len(lines),
    )
    lines[insert_at:insert_at] = [script, ""]
    return "\n".join(lines).rstrip() + "\n"


def sync_index_counts(contracts_root: Path) -> None:
    """Refresh generated case counts after materializing a changed manifest."""

    index_path = contracts_root / "index.yaml"
    if not index_path.is_file():
        return
    index = load_data(index_path)
    if not isinstance(index, dict):
        return
    total_cases = 0
    for entry in index.get("modules", []):
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        directory = str(entry.get("directory", entry["id"]))
        module_dir = contracts_root / "modules" / directory
        endpoints = first_list(load_data(module_dir / "endpoints.yaml"), "endpoints") if (module_dir / "endpoints.yaml").is_file() else []
        cases = first_list(load_data(module_dir / "cases.yaml"), "cases") if (module_dir / "cases.yaml").is_file() else []
        entry["endpoint_count"] = len(endpoints)
        entry["case_count"] = len(cases)
        total_cases += len(cases)
    index["generated_cases"] = total_cases
    index_path.write_text(render_manifest(index, index_path), encoding="utf-8")


def materialize(
    contracts_root: Path,
    bruno_root: Path,
    dry_run: bool = False,
    auth_config_path: Path | None = None,
) -> list[Path]:
    modules_root = contracts_root / "modules"
    if not modules_root.is_dir():
        raise SystemExit(f"modules directory does not exist: {modules_root}")
    config_path = auth_config_path or (contracts_root / "request-auth.yaml")
    config = load_data(config_path) if config_path.is_file() else DEFAULT_AUTH_CONFIG
    if not isinstance(config, dict):
        raise SystemExit(f"request authentication config must be an object: {config_path}")
    module_map_path = contracts_root / "module-map.yaml"
    module_map = load_data(module_map_path) if module_map_path.is_file() else {}
    module_metadata = {
        str(item.get("id")): item
        for item in module_map.get("modules", [])
        if isinstance(item, dict) and item.get("id")
    } if isinstance(module_map, dict) and isinstance(module_map.get("modules"), list) else {}
    created: list[Path] = []
    for module_dir in sorted(path for path in modules_root.iterdir() if path.is_dir()):
        endpoints_doc = load_data(module_dir / "endpoints.yaml")
        endpoints = first_list(endpoints_doc, "endpoints")
        endpoint_by_id = {str(item.get("id")): item for item in endpoints}
        cases_path = module_dir / "cases.yaml"
        if not cases_path.is_file():
            continue
        cases = first_list(load_data(cases_path), "cases")
        for case in cases:
            case_id = str(case.get("id", ""))
            endpoint = endpoint_by_id.get(str(case.get("endpoint_id")))
            if not case_id or endpoint is None:
                continue
            configured = case.get("bru") or case.get("bru_file") or case.get("file_name")
            relative = (
                Path(configured)
                if isinstance(configured, str) and configured
                else display_case_file(case, endpoint, module_dir.name)
            )
            module_bru = bruno_root / module_dir.name
            target = (module_bru / relative).resolve()
            try:
                target.relative_to(module_bru.resolve())
            except ValueError as exc:
                raise SystemExit(f"case {case_id} points outside Tag module {module_dir.name}: {relative}") from exc
            if target.exists():
                try:
                    existing = target.read_text(encoding="utf-8", errors="strict")
                except (OSError, UnicodeDecodeError) as exc:
                    raise SystemExit(f"cannot read Bruno file {target}: {exc}") from exc
                updated = ensure_auth_script(existing, config)
                if updated == existing:
                    continue
                created.append(target)
                if not dry_run:
                    target.write_text(updated, encoding="utf-8")
                continue
            created.append(target)
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(render_case(case, endpoint, config), encoding="utf-8")
        module_bru = bruno_root / module_dir.name
        if module_bru.is_dir():
            for existing_path in sorted(module_bru.rglob("*.bru")):
                try:
                    existing = existing_path.read_text(encoding="utf-8", errors="strict")
                except (OSError, UnicodeDecodeError) as exc:
                    raise SystemExit(f"cannot read Bruno file {existing_path}: {exc}") from exc
                updated = ensure_auth_script(existing, config)
                if updated != existing:
                    created.append(existing_path)
                    if not dry_run:
                        existing_path.write_text(updated, encoding="utf-8")
        documentation_path = module_dir / "CASES.md"
        existing_documentation = (
            documentation_path.read_text(encoding="utf-8", errors="strict")
            if documentation_path.is_file()
            else ""
        )
        tag = str(endpoints_doc.get("swagger_tag", "")) if isinstance(endpoints_doc, dict) else ""
        module_id = str(endpoints_doc.get("module", module_dir.name)) if isinstance(endpoints_doc, dict) else module_dir.name
        module = dict(module_metadata.get(module_id, {}))
        module.setdefault("id", module_id)
        module.setdefault("name", str(endpoints_doc.get("name", tag or module_dir.name)) if isinstance(endpoints_doc, dict) else module_dir.name)
        if tag:
            module["swagger_tags"] = [tag]
        updated_documentation = update_module_document(existing_documentation, module, endpoints, cases)
        if updated_documentation != existing_documentation:
            created.append(documentation_path)
            if not dry_run:
                documentation_path.write_text(updated_documentation, encoding="utf-8")
    # Apply the selected pre-request mode to collection files that are not
    # owned by a generated module, such as coordinator or cross-module cases.
    if bruno_root.is_dir():
        for existing_path in sorted(bruno_root.rglob("*.bru")):
            try:
                existing = existing_path.read_text(encoding="utf-8", errors="strict")
            except (OSError, UnicodeDecodeError) as exc:
                raise SystemExit(f"cannot read Bruno file {existing_path}: {exc}") from exc
            updated = ensure_auth_script(existing, config)
            if updated != existing:
                created.append(existing_path)
                if not dry_run:
                    existing_path.write_text(updated, encoding="utf-8")
    if not dry_run:
        sync_index_counts(contracts_root)
    return list(dict.fromkeys(created))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("contracts_root", type=Path)
    parser.add_argument("bruno_root", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--auth-config", type=Path, help="request-auth.yaml selecting the active pre-request mode")
    args = parser.parse_args()
    created = materialize(args.contracts_root, args.bruno_root, args.dry_run, args.auth_config)
    action = "would materialize" if args.dry_run else "materialized"
    print(f"{action} {len(created)} Bruno/documentation artifact(s)")
    for path in created:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
