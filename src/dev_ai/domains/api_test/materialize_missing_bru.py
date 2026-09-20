#!/usr/bin/env python3
"""Materialize missing draft Bruno files inside their owning Tag module."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import html
import io
import json
import re
import sys
import textwrap
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

from .manifest_io import first_list, load_data
from .execution_config import (
    COLLECTION_MARKER,
    COLLECTION_TEMPLATE,
    HEADER_NAME_RE,
    environment_file,
    load_bruno_environment_document,
    load_execution_config,
    render_headers_block,
    render_runtime_environment,
)
from .parse_openapi import display_directory, render_manifest, update_module_document
from .constraints import (
    database_access_errors,
    database_steps,
    mock_data_ready_env,
    qa_root_for_contracts,
    validate_stage,
    write_module_lock,
)
from .qa_lock import write as write_qa_lock


LEGACY_AUTH_MARKER = "dev-ai: auth-start"
LEGACY_AUTH_END_MARKER = "dev-ai: auth-end"
OMIT_MARKER = "dev-ai: omit-common-headers"
OMIT_END_MARKER = "dev-ai: omit-common-headers-end"


def json_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ": "))


CHINESE_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
BUSINESS_FILE_RE = re.compile(r"^\d{2,}-.+\.bru$", re.IGNORECASE)


def safe_display_stem(value: str) -> str:
    return re.sub(r"[\\/\x00-\x1f<>:\"|?*]", "", str(value)).strip(" .")


def normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9\u3400-\u4dbf\u4e00-\u9fff]+", "", value.casefold())


def valid_business_file(
    path: Path,
    case_id: str = "",
    title: str | None = None,
    sequence: int | None = None,
) -> bool:
    if not BUSINESS_FILE_RE.fullmatch(path.name):
        return False
    number, description = path.stem.split("-", 1)
    return bool(
        description.strip()
        and CHINESE_RE.search(description)
        and (not case_id or normalized_name(description) != normalized_name(case_id))
        and (title is None or description == safe_display_stem(title))
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


def is_http_request_content(content: str) -> bool:
    meta = re.search(r"(?ms)^\s*meta\s*\{(.*?)^\s*\}", content)
    return bool(
        meta
        and re.search(r"(?m)^\s*type:\s*http\s*$", meta.group(1), re.IGNORECASE)
        and re.search(r"(?mi)^\s*(?:get|post|put|patch|delete|head|options|trace)\s*\{", content)
    )


def case_sequence(case: dict[str, Any], position: int) -> int:
    value = case.get("sequence", case.get("seq", position))
    try:
        sequence = int(value)
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"case {case.get('id')} has invalid sequence: {value}") from exc
    if sequence < 1:
        raise SystemExit(f"case {case.get('id')} sequence must be positive: {value}")
    return sequence


def display_case_file(
    case: dict[str, Any],
    position: int = 1,
) -> Path:
    """Build a deterministic filename from the case's Chinese title."""

    label = str(case.get("title", "")).strip()
    if not label or not CHINESE_RE.search(label):
        raise SystemExit(f"case {case.get('id')} must declare a Chinese case.title")
    stem = safe_display_stem(label)
    if not stem:
        raise SystemExit(f"case {case.get('id')} has no usable business filename")
    return Path(f"{case_sequence(case, position):02d}-{stem}.bru")


def request_url(endpoint: dict[str, Any], request: dict[str, Any], base_env: str = "baseUrl") -> str:
    path = str(request.get("path") or endpoint.get("path") or "/")
    path_parameters = request.get("path_parameters", {})
    if isinstance(path_parameters, dict):
        for name, value in path_parameters.items():
            rendered = str(value)
            if not re.fullmatch(r"\{\{[^}]+\}\}", rendered):
                rendered = urllib.parse.quote(rendered, safe="")
            path = path.replace("{" + str(name) + "}", rendered)
    path = re.sub(r"(?<!\{)\{([^{}]+)\}(?!\})", lambda match: "{{" + match.group(1) + "}}", path)
    query = request.get("query")
    if isinstance(query, dict) and query:
        pairs: list[tuple[str, Any]] = []
        parameters = {
            str(item.get("name")): item
            for item in endpoint.get("parameters", [])
            if isinstance(item, dict) and item.get("in") == "query" and item.get("name")
        }
        for key, value in query.items():
            parameter = parameters.get(str(key), {})
            style = str(parameter.get("style") or "form")
            explode = parameter.get("explode")
            explode = style == "form" if explode is None else bool(explode)
            if isinstance(value, list):
                if style == "form" and explode:
                    pairs.extend((str(key), item) for item in value)
                else:
                    delimiter = {"spaceDelimited": " ", "pipeDelimited": "|"}.get(style, ",")
                    pairs.append((str(key), delimiter.join(str(item) for item in value)))
                continue
            if isinstance(value, dict):
                if style == "deepObject":
                    pairs.extend((f"{key}[{name}]", item) for name, item in value.items())
                elif style == "form" and explode:
                    pairs.extend((str(name), item) for name, item in value.items())
                elif style == "form":
                    flattened = ",".join(str(item) for pair in value.items() for item in pair)
                    pairs.append((str(key), flattened))
                else:
                    raise ValueError(f"query field {key} uses unsupported object serialization style {style}")
                continue
            pairs.append((str(key), value))
        rendered_pairs = []
        for key, value in pairs:
            if isinstance(value, bool):
                rendered = str(value).lower()
            elif value is None:
                rendered = ""
            else:
                rendered = str(value)
            if not re.fullmatch(r"\{\{[^}]+\}\}", rendered):
                rendered = urllib.parse.quote(rendered, safe="")
            rendered_pairs.append(f"{urllib.parse.quote(str(key), safe='')}={rendered}")
        path += "?" + "&".join(rendered_pairs)
    return "{{" + base_env + "}}" + path


def body_kind(request: dict[str, Any], body: Any) -> str:
    value = request.get("body_type") or request.get("content_type") or ""
    value = str(value).strip().lower()
    if body is None:
        return "none"
    if value in {"json", "application/json", "application/*+json"} or value.endswith("+json") or not value:
        return "json"
    if "multipart/form-data" in value or value in {"multipart", "multipart-form"}:
        if body == {}:
            return "none"
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
    if "eq" in assertion:
        lines.append(f"  {expression}: eq {json_value(assertion['eq'])}")
    if assertion.get("exists") is True:
        lines.append(f"  {expression}: exists")
    if "contains" in assertion:
        lines.append(f"  {expression}: contains {json_value(assertion['contains'])}")
    if "matches" in assertion:
        lines.append(f"  {expression}: matches {json_value(assertion['matches'])}")
    if "type" in assertion:
        operator = {
            "string": "isString",
            "number": "isNumber",
            "integer": "isNumber",
            "boolean": "isBoolean",
            "array": "isArray",
            "object": "isObject",
        }.get(str(assertion["type"]).lower())
        if operator:
            lines.append(f"  {expression}: {operator}")
    if "length" in assertion:
        lines.append(f"  {expression}: length {assertion['length']}")
    if "minimum" in assertion:
        lines.append(f"  {expression}: gte {json_value(assertion['minimum'])}")
    if "maximum" in assertion:
        lines.append(f"  {expression}: lte {json_value(assertion['maximum'])}")
    if assertion.get("nullable") is False:
        lines.append(f"  {expression}: isNotNull")
    if assertion.get("is_null") is True:
        lines.append(f"  {expression}: isNull")
    if "equals_variable" in assertion:
        lines.append(f"  {expression}: eq {{{{{assertion['equals_variable']}}}}}")


def post_response_script(case: dict[str, Any], assertions: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    captures = case.get("captures", [])
    if isinstance(captures, dict):
        captures = [{"name": name, "path": path} for name, path in captures.items()]
    for capture in captures if isinstance(captures, list) else []:
        if not isinstance(capture, dict) or not capture.get("name"):
            continue
        expression = assertion_expression({"path": capture.get("path", "$")})
        if expression:
            lines.append(
                f"  if ({expression} === undefined || {expression} === null || {expression} === '') "
                f"throw new Error({json.dumps('missing flow capture ' + str(capture['name']))});"
            )
            lines.append(f"  bru.setVar({json.dumps(str(capture['name']))}, {expression});")
            lines.extend([
                f"  test({json.dumps('dev-ai:flow:capture:' + str(capture['name']))}, function () {{",
                "    expect(true).to.equal(true);",
                "  });",
            ])
    for assertion in assertions:
        expression = assertion_expression(assertion)
        capture_name = assertion.get("capture_as") or assertion.get("capture")
        if expression and capture_name:
            lines.append(f"  bru.setVar({json.dumps(str(capture_name))}, {expression});")
        items = assertion.get("items")
        item_type = items.get("type") if isinstance(items, dict) else assertion.get("item_type")
        js_type = {
            "string": "string",
            "number": "number",
            "integer": "number",
            "boolean": "boolean",
            "object": "object",
        }.get(str(item_type).lower())
        if expression and js_type:
            label = json.dumps(f"{assertion.get('path', '$')} array item type")
            lines.extend([
                f"  test({label}, function () {{",
                f"    expect({expression}).to.be.an('array');",
                f"    {expression}.forEach(item => expect(typeof item).to.equal('{js_type}'));",
                "  });",
            ])
    for name in case.get("flow_uses", []) if isinstance(case.get("flow_uses"), list) else []:
        if not str(name).strip():
            continue
        lines.extend([
            f"  test({json.dumps('dev-ai:flow:use:' + str(name))}, function () {{",
            f"    expect(bru.getVar({json.dumps(str(name))})).to.not.equal(undefined);",
            "  });",
        ])
    absent_path = str(case.get("flow_assert_absent", "")).strip()
    if absent_path:
        expression = assertion_expression({"path": absent_path})
        if expression:
            lines.extend([
                f"  test({json.dumps('dev-ai:flow:absence:' + absent_path)}, function () {{",
                f"    expect({expression}).to.equal(null);",
                "  });",
            ])
        if expression and str(assertion.get("type", "")).lower() == "integer":
            label = json.dumps(f"{assertion.get('path', '$')} integer type")
            lines.extend([
                f"  test({label}, function () {{",
                f"    expect(Number.isInteger({expression})).to.equal(true);",
                "  });",
            ])
    for step in database_steps(case):
        if step.get("phase") != "assertion":
            continue
        engine = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(step["engine"]).strip())
        lines.append(f"  // dev-ai: database-assertion {engine}")
        lines.extend(textwrap.indent(str(step["script"]).strip(), "  ").splitlines())
    return "\n".join(["script:post-response {", *lines, "}"]) if lines else ""


def pre_request_script(case: dict[str, Any]) -> str:
    lines: list[str] = []
    omitted_headers = request_omit_headers(case)
    if omitted_headers:
        lines.extend([
            f"  // {OMIT_MARKER}",
            f"  req.deleteHeaders({json.dumps(omitted_headers, ensure_ascii=False)});",
            f"  // {OMIT_END_MARKER}",
        ])
    setup_steps = [step for step in database_steps(case) if step.get("phase") == "setup"]
    if setup_steps or case.get("mock_data_required") is True:
        ready_env = mock_data_ready_env(str(case.get("id", "")))
        lines.extend([
            "  // dev-ai: mock-data-prerequisite",
            f'  if (!bru.getEnvVar("{ready_env}")) {{',
            '    throw new Error("mock data prerequisite was not authorized or could not be prepared");',
            "  }",
        ])
    for name in case.get("flow_uses", []) if isinstance(case.get("flow_uses"), list) else []:
        if not str(name).strip():
            continue
        lines.extend([
            f"  if (bru.getVar({json.dumps(str(name))}) === undefined) "
            f"throw new Error({json.dumps('missing flow use ' + str(name))});",
        ])
    return "\n".join(["script:pre-request {", *lines, "}"]) if lines else ""


def render_case(
    case: dict[str, Any],
    endpoint: dict[str, Any],
    sequence: int | None = None,
) -> str:
    database_errors = database_access_errors(case)
    if database_errors:
        raise ValueError("; ".join(database_errors))
    request = case.get("request") if isinstance(case.get("request"), dict) else {}
    method = str(endpoint.get("method", "GET")).lower()
    base_env = "baseUrl"
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
    sequence = sequence if sequence is not None else case.get("sequence", case.get("seq", 1))
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
    request_script = pre_request_script(case)
    if request_script:
        lines.extend(["", request_script])
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
        rendered_body = textwrap.indent(render_body(body, kind), "  ")
        lines.extend(["", f"body:{kind} {{", rendered_body, "}"])
        if kind == "graphql" and isinstance(body, dict) and body.get("variables") is not None:
            variables = textwrap.indent(json.dumps(body["variables"], ensure_ascii=False, indent=2), "  ")
            lines.extend(["", "body:graphql:vars {", variables, "}"])
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
    post_response = post_response_script(case, [item for item in assertions if isinstance(item, dict)])
    if post_response:
        lines.extend(["", post_response])
    lines.extend([
        "",
        "docs {",
        f"  ## 用例标题：{title}",
        "",
        f"  简短描述：{description}",
        "",
        f"  - 服务地址从 Bruno 环境变量 `{base_env}` 读取。",
        "  - 认证和公共 Header 由集合级配置在运行时统一处理。",
        "  - 响应校验覆盖状态码、业务结果和具体字段。",
        "}",
    ])
    return "\n".join(lines) + "\n"


def request_omit_headers(case: dict[str, Any]) -> list[str]:
    request = case.get("request") if isinstance(case.get("request"), dict) else {}
    configured = request.get("omit_common_headers", [])
    if configured is None:
        return []
    if not isinstance(configured, list) or any(
        not isinstance(name, str) or not HEADER_NAME_RE.fullmatch(name.strip())
        for name in configured
    ):
        raise ValueError(f"case {case.get('id')} request.omit_common_headers must be a list of Header names")
    return list(dict.fromkeys(name.strip() for name in configured))


def request_omit_script(case: dict[str, Any]) -> str:
    headers = request_omit_headers(case)
    if not headers:
        return ""
    return "\n".join([
        "script:pre-request {",
        f"  // {OMIT_MARKER}",
        f"  req.deleteHeaders({json.dumps(headers, ensure_ascii=False)});",
        f"  // {OMIT_END_MARKER}",
        "}",
    ])


def _remove_managed_block(content: str, start_marker: str, end_marker: str) -> str:
    if start_marker not in content:
        return content
    nested_pattern = re.compile(
        rf"(?ms)^[ \t]*\{{[ \t]*\n\s*// {re.escape(start_marker)}[^\n]*\n.*?"
        rf"^[ \t]*// {re.escape(end_marker)}[ \t]*\n[ \t]*\}}[ \t]*(?:\n|$)"
    )
    updated = nested_pattern.sub("", content, count=1)
    block_pattern = re.compile(
        rf"(?ms)^(?P<indent>[ \t]*)// {re.escape(start_marker)}[^\n]*\n.*?"
        rf"^(?P=indent)// {re.escape(end_marker)}[ \t]*$(?:\n)?"
    )
    updated, count = block_pattern.subn("", updated, count=1)
    if count == 0 and start_marker in updated:
        raise ValueError(f"existing generated block has no {end_marker} marker")
    empty_script = re.compile(r"(?ms)^[ \t]*script:pre-request[ \t]*\{[ \t\r\n]*\}[ \t]*(?:\n|$)")
    return empty_script.sub("", updated, count=1)


def strip_legacy_auth(content: str) -> str:
    return _remove_managed_block(content, LEGACY_AUTH_MARKER, LEGACY_AUTH_END_MARKER)


def ensure_request_script(content: str, case: dict[str, Any]) -> str:
    updated = strip_legacy_auth(content)
    updated = _remove_managed_block(updated, OMIT_MARKER, OMIT_END_MARKER)
    script = request_omit_script(case)
    if not script:
        return updated
    body = "\n".join(script.splitlines()[1:-1])
    existing_position = updated.find("script:pre-request")
    if existing_position >= 0:
        opening = updated.find("{", existing_position)
        if opening < 0:
            raise ValueError("cannot merge common Header exclusion into the existing Bruno pre-request script")
        return updated[:opening + 1] + "\n" + body + updated[opening + 1:]
    lines = updated.splitlines()
    insert_at = next(
        (index for index, line in enumerate(lines) if line.startswith(("headers ", "body:", "assert "))),
        len(lines),
    )
    lines[insert_at:insert_at] = [script, ""]
    return "\n".join(lines).rstrip() + "\n"


def strip_meta_tags(content: str) -> str:
    """Remove legacy generated tags; execution scope is directory-based."""

    meta = re.search(r"(?ms)^\s*meta\s*\{(.*?)^\s*\}", content)
    if meta is None:
        raise ValueError("business Bruno request has no meta block")
    block = meta.group(0)
    updated = re.sub(r"(?m)^\s*tags:\s*.*\r?\n?", "", block)
    return content[:meta.start()] + updated + content[meta.end():]


def contract_signature(content: str) -> str:
    blocks = re.findall(
        r"(?mis)^\s*(?:meta|get|post|put|patch|delete|head|options|trace|headers|body:[^\s{]+|assert)\s*\{.*?^\s*\}",
        content,
    )
    return "\n".join(re.sub(r"\s+", " ", block).strip() for block in blocks)


def _case_fingerprint(case: dict[str, Any]) -> str:
    payload = {
        key: value
        for key, value in case.items()
        if key not in {"bru", "bru_file", "file_name", "manual_review"}
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _conflict(case_id: str, actual: str, expected: str) -> SystemExit:
    difference = list(difflib.unified_diff(
        actual.splitlines(), expected.splitlines(), fromfile="current.bru", tofile="cases.yaml rendered", lineterm=""
    ))[:80]
    return SystemExit(json.dumps({
        "error": "materialize_conflict",
        "case_id": case_id,
        "message": ".bru and cases.yaml both changed since the last materialization",
        "diff": difference,
    }, ensure_ascii=False, indent=2))


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


def _binary_schema(endpoint: dict[str, Any]) -> dict[str, Any] | None:
    body = endpoint.get("request_body", {}) if isinstance(endpoint.get("request_body"), dict) else {}
    pending: list[Any] = [body]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            if value.get("format") == "binary":
                return value
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return None


def _fixture_variable(endpoint_id: str, fixture_type: str) -> str:
    prefix = re.sub(r"[^A-Za-z0-9]+", "_", endpoint_id).strip("_").upper() or "ENDPOINT"
    return f"{prefix}_FILE_{fixture_type.replace('-', '_').upper()}"


def _fixture_type(case: dict[str, Any]) -> str:
    explicit = str(case.get("fixture_type", ""))
    if explicit:
        return explicit
    case_id = str(case.get("id", "")).upper()
    for token, fixture_type in (
        ("HEADER_ONLY", "header-only"),
        ("MISSING_COLUMN", "missing-column"),
        ("INVALID_CONTENT", "invalid-content"),
        ("INVALID_EXTENSION", "invalid-extension"),
        ("INVALID_MIME", "invalid-mime"),
        ("OVERSIZED", "oversized"),
        ("EMPTY", "empty"),
    ):
        if token in case_id:
            return fixture_type
    return "legal" if str(case.get("scenario")) == "success" else ""


def _replace_fixture_variable(value: Any, variable: str) -> Any:
    if isinstance(value, str) and re.fullmatch(
        r"\{\{(?:UPLOAD_FILE|EMPTY_UPLOAD_FILE|INVALID_EXTENSION_FILE|INVALID_MIME_FILE|OVERSIZED_UPLOAD_FILE|[A-Z0-9_]+_FILE_[A-Z0-9_]+)\}\}",
        value,
    ):
        return "{{" + variable + "}}"
    if isinstance(value, dict):
        return {key: _replace_fixture_variable(child, variable) for key, child in value.items()}
    if isinstance(value, list):
        return [_replace_fixture_variable(child, variable) for child in value]
    return value


def _xlsx_bytes(columns: list[str], rows: list[list[str]]) -> bytes:
    strings = [*columns, *(cell for row in rows for cell in row)]
    unique = list(dict.fromkeys(strings))
    indexes = {value: index for index, value in enumerate(unique)}
    cells: list[str] = []
    for row_index, row in enumerate([columns, *rows], 1):
        rendered = "".join(
            f'<c r="{chr(65 + column_index)}{row_index}" t="s"><v>{indexes[cell]}</v></c>'
            for column_index, cell in enumerate(row)
        )
        cells.append(f'<row r="{row_index}">{rendered}</row>')
    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
            '</Types>'
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>'
        ),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
            '</Relationships>'
        ),
        "xl/worksheets/sheet1.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(cells)}</sheetData></worksheet>'
        ),
        "xl/sharedStrings.xml": (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(strings)}" uniqueCount="{len(unique)}">'
            + "".join(f"<si><t>{html.escape(value)}</t></si>" for value in unique)
            + "</sst>"
        ),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content.encode("utf-8"))
    return output.getvalue()


def _fixture_bytes(fixture_type: str, excel: bool, columns: list[str]) -> bytes:
    if fixture_type == "empty":
        return b""
    if not excel:
        return b"invalid\n" if fixture_type in {"invalid-content", "invalid-mime"} else b"valid\n"
    if fixture_type == "header-only":
        return _xlsx_bytes(columns, [])
    if fixture_type == "missing-column":
        return _xlsx_bytes(columns[:-1] or ["missing"], [["1"] * max(1, len(columns) - 1)])
    if fixture_type == "invalid-content":
        return _xlsx_bytes(columns, [["__INVALID__"] * len(columns)])
    return _xlsx_bytes(columns, [[str(index + 1) for index in range(len(columns))]])


def generate_fixtures(
    contracts_root: Path,
    bruno_root: Path,
    config_path: Path,
) -> list[Path]:
    """Create endpoint-owned fixtures, variables, and a checksum manifest."""

    qa_root = qa_root_for_contracts(contracts_root)
    generated_root = contracts_root / "fixtures" / "generated"
    generated_root.mkdir(parents=True, exist_ok=True)
    manifest_items: list[dict[str, Any]] = []
    changed: list[Path] = []
    config = load_execution_config(config_path)
    env_path = environment_file(config_path, config)
    environment = load_bruno_environment_document(env_path)
    modules_root = contracts_root / "modules"
    for module_dir in sorted(path for path in modules_root.iterdir() if path.is_dir()):
        endpoint_doc = load_data(module_dir / "endpoints.yaml")
        cases_path = module_dir / "cases.yaml"
        case_doc = load_data(cases_path) if cases_path.is_file() else {}
        endpoints = {str(item.get("id")): item for item in first_list(endpoint_doc, "endpoints") if item.get("id")}
        cases = first_list(case_doc, "cases")
        cases_changed = False
        for endpoint_id, endpoint in endpoints.items():
            schema = _binary_schema(endpoint)
            if schema is None:
                continue
            extensions = [str(value).casefold().lstrip(".") for value in schema.get("x-allowed-extensions", [])]
            excel = bool(set(extensions) & {"xls", "xlsx"}) or schema.get("x-excel-template") is not None
            extension = "xlsx" if excel else (extensions[0] if extensions else "bin")
            columns = [str(value) for value in schema.get("x-template-columns", ["id", "name"])] or ["id"]
            endpoint_cases = [case for case in cases if str(case.get("endpoint_id")) == endpoint_id]
            types = {_fixture_type(case) for case in endpoint_cases} - {""}
            if excel:
                types.update({"legal", "empty", "header-only", "missing-column", "invalid-content", "oversized"})
            for fixture_type in sorted(types):
                variable = _fixture_variable(endpoint_id, fixture_type)
                for case in endpoint_cases:
                    if _fixture_type(case) == fixture_type:
                        replaced = _replace_fixture_variable(case.get("request", {}), variable)
                        if replaced != case.get("request", {}):
                            case["request"] = replaced
                            case["fixture_type"] = fixture_type
                            cases_changed = True
                file_extension = "txt" if fixture_type == "invalid-extension" else extension
                safe_endpoint = re.sub(r"[^A-Za-z0-9._-]+", "-", endpoint_id).strip("-") or "endpoint"
                fixture_path = generated_root / safe_endpoint / f"{fixture_type}.{file_extension}"
                fixture_path.parent.mkdir(parents=True, exist_ok=True)
                payload = _fixture_bytes(fixture_type, excel, columns)
                if not fixture_path.is_file() or fixture_path.read_bytes() != payload:
                    fixture_path.write_bytes(payload)
                    changed.append(fixture_path)
                if fixture_type == "oversized":
                    maximum = int(schema.get("x-max-size", schema.get("maxLength", 4096)))
                    if fixture_path.stat().st_size <= maximum:
                        with fixture_path.open("r+b") as handle:
                            handle.seek(maximum)
                            handle.write(b"\0")
                        changed.append(fixture_path)
                relative = fixture_path.relative_to(qa_root).as_posix()
                environment["vars"][variable] = (Path("..") / Path(relative)).as_posix()
                manifest_items.append({
                    "id": f"{endpoint_id}:{fixture_type}",
                    "type": fixture_type,
                    "source": "generated",
                    "endpoint_scope": [endpoint_id],
                    "variable": variable,
                    "path": relative,
                    "sha256": hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
                    "validation": {"exists": True, "size": fixture_path.stat().st_size, "extension": file_extension},
                    "evidence": {
                        "source_kind": "openapi",
                        "file": str(module_dir / "endpoints.yaml"),
                        "symbol": endpoint_id,
                        "line": 1,
                        "endpoint_scope": [endpoint_id],
                        "confidence": "high",
                    },
                })
        if cases_changed:
            updated = dict(case_doc) if isinstance(case_doc, dict) else {}
            updated["cases"] = cases
            cases_path.write_text(render_manifest(updated, cases_path), encoding="utf-8")
            changed.append(cases_path)
    manifest_path = generated_root / "manifest.yaml"
    rendered_manifest = render_manifest({"version": 1, "fixtures": manifest_items}, manifest_path)
    if not manifest_path.is_file() or manifest_path.read_text(encoding="utf-8", errors="strict") != rendered_manifest:
        manifest_path.write_text(rendered_manifest, encoding="utf-8")
        changed.append(manifest_path)
    if manifest_items:
        rendered_environment = render_runtime_environment(environment) + render_headers_block(environment)
        if env_path.read_text(encoding="utf-8", errors="strict") != rendered_environment:
            env_path.write_text(rendered_environment, encoding="utf-8")
            changed.append(env_path)
    return changed


def materialize(
    contracts_root: Path,
    bruno_root: Path,
    dry_run: bool = False,
    execution_config_path: Path | None = None,
    module_filter: str | None = None,
    sync_index: bool = True,
    check: bool = False,
    verify: bool = True,
) -> list[Path]:
    modules_root = contracts_root / "modules"
    if not modules_root.is_dir():
        raise SystemExit(f"modules directory does not exist: {modules_root}")
    qa_root = qa_root_for_contracts(contracts_root)
    config_path = execution_config_path or (qa_root / "execution" / "config.yaml")
    try:
        load_execution_config(config_path)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    collection_path = bruno_root / "collection.bru"
    if not collection_path.exists():
        if module_filter:
            raise SystemExit("module materialization requires coordinator-initialized collection.bru")
        if not dry_run:
            collection_path.parent.mkdir(parents=True, exist_ok=True)
            collection_path.write_text(COLLECTION_TEMPLATE, encoding="utf-8")
        created = [collection_path]
    else:
        created = []
        if COLLECTION_MARKER not in collection_path.read_text(encoding="utf-8", errors="strict"):
            raise SystemExit(f"collection runtime script is missing from {collection_path}")
    if not dry_run and not module_filter:
        created.extend(generate_fixtures(contracts_root, bruno_root, config_path))
    module_map_path = contracts_root / "module-map.yaml"
    module_map = load_data(module_map_path) if module_map_path.is_file() else {}
    module_metadata = {
        str(item.get("id")): item
        for item in module_map.get("modules", [])
        if isinstance(item, dict) and item.get("id")
    } if isinstance(module_map, dict) and isinstance(module_map.get("modules"), list) else {}
    state_path = contracts_root / "generation-state.yaml"
    generation_state = load_data(state_path) if state_path.is_file() else {}
    state_cases = generation_state.get("cases", {}) if isinstance(generation_state, dict) and isinstance(generation_state.get("cases"), dict) else {}
    materialized_state: dict[str, dict[str, Any]] = {}
    for module_dir in sorted(path for path in modules_root.iterdir() if path.is_dir()):
        endpoints_doc = load_data(module_dir / "endpoints.yaml")
        endpoints = first_list(endpoints_doc, "endpoints")
        endpoint_by_id = {str(item.get("id")): item for item in endpoints}
        module_id = str(endpoints_doc.get("module", module_dir.name)) if isinstance(endpoints_doc, dict) else module_dir.name
        module_tag = str(endpoints_doc.get("swagger_tag", "")).strip() if isinstance(endpoints_doc, dict) else ""
        if module_tag and module_dir.name != display_directory(module_tag, module_id):
            raise SystemExit(
                f"module {module_id} directory {module_dir.name!r} does not match OpenAPI Tag {module_tag!r}"
            )
        if module_filter and module_filter not in {module_id, module_dir.name}:
            continue
        module_state_cases = state_cases
        local_state_path = module_dir / "materialization-state.yaml"
        if module_filter and local_state_path.is_file():
            local_state = load_data(local_state_path)
            if isinstance(local_state, dict) and isinstance(local_state.get("cases"), dict):
                module_state_cases = local_state["cases"]
        cases_path = module_dir / "cases.yaml"
        if not cases_path.is_file():
            continue
        cases_document = load_data(cases_path)
        cases = first_list(cases_document, "cases")
        module_bru = bruno_root / module_dir.name
        existing_by_id: dict[str, Path] = {}
        if module_bru.is_dir():
            for path in sorted(module_bru.rglob("*.bru")):
                try:
                    content = path.read_text(encoding="utf-8", errors="strict")
                except (OSError, UnicodeDecodeError) as exc:
                    raise SystemExit(f"cannot read Bruno file {path}: {exc}") from exc
                if not is_http_request_content(content):
                    continue
                existing_id = bru_meta_name(content)
                if not existing_id:
                    raise SystemExit(f"business Bruno request has no meta.name: {path}")
                if not valid_business_file(path, existing_id):
                    raise SystemExit(
                        f"case {existing_id} has invalid business Bruno filename {path.name!r}; "
                        "expected NN-Chinese-case-title.bru and not the stable case ID"
                    )
                if existing_id in existing_by_id:
                    raise SystemExit(
                        f"case {existing_id} is mapped by multiple Bruno files: "
                        f"{existing_by_id[existing_id]} and {path}"
                    )
                existing_by_id[existing_id] = path

        planned: list[tuple[dict[str, Any], dict[str, Any], Path]] = []
        targets: dict[Path, str] = {}
        mappings_changed = False
        for position, case in enumerate(cases, 1):
            case_id = str(case.get("id", ""))
            endpoint = endpoint_by_id.get(str(case.get("endpoint_id")))
            if not case_id or endpoint is None:
                continue
            configured = case.get("bru") or case.get("bru_file") or case.get("file_name")
            if isinstance(configured, str) and configured:
                relative = Path(configured)
                explicit_sequence = case.get("sequence", case.get("seq"))
                if relative.parent != Path(".") or not valid_business_file(
                    relative,
                    case_id,
                    str(case.get("title", "")),
                    case_sequence(case, position) if explicit_sequence is not None else None,
                ):
                    raise SystemExit(
                        f"case {case_id} has invalid business Bruno filename {configured!r}; "
                        "expected its stable sequence and sanitized Chinese case.title"
                    )
                if "environments" in {part.lower() for part in relative.parts[:-1]}:
                    raise SystemExit(f"case {case_id} cannot use an environments path: {configured}")
            elif case_id in existing_by_id:
                relative = existing_by_id[case_id].relative_to(module_bru)
                if relative.parent != Path(".") or not valid_business_file(relative, case_id, str(case.get("title", ""))):
                    raise SystemExit(
                        f"case {case_id} existing Bruno filename {relative.name!r} does not match "
                        "its sanitized Chinese case.title"
                    )
            else:
                relative = display_case_file(case, position)
            target = (module_bru / relative).resolve()
            try:
                target.relative_to(module_bru.resolve())
            except ValueError as exc:
                raise SystemExit(f"case {case_id} points outside Tag module {module_dir.name}: {relative}") from exc
            if target in targets:
                raise SystemExit(
                    f"Bruno filename collision: cases {targets[target]} and {case_id} both map to {relative}"
                )
            targets[target] = case_id
            planned.append((case, endpoint, target))
            desired_mapping = relative.as_posix()
            if case.get("bru") != desired_mapping or "bru_file" in case or "file_name" in case:
                if "bru_file" in case or "file_name" in case:
                    print(f"WARNING: case {case_id} uses legacy bru_file/file_name; migrated to bru", file=sys.stderr)
                case["bru"] = desired_mapping
                case.pop("bru_file", None)
                case.pop("file_name", None)
                mappings_changed = True

        if mappings_changed:
            created.append(cases_path)
            if not dry_run:
                updated_cases_document = dict(cases_document) if isinstance(cases_document, dict) else {}
                updated_cases_document["cases"] = cases
                cases_path.write_text(render_manifest(updated_cases_document, cases_path), encoding="utf-8")

        for case, endpoint, target in planned:
            case_id = str(case["id"])
            rendered_case = dict(case)
            expected_sequence = int(target.stem.split("-", 1)[0])
            expected = render_case(rendered_case, endpoint, expected_sequence)
            current_case_fingerprint = _case_fingerprint(case)
            baseline = module_state_cases.get(case_id, {}) if isinstance(module_state_cases.get(case_id), dict) else {}
            if target.exists():
                try:
                    existing = target.read_text(encoding="utf-8", errors="strict")
                except (OSError, UnicodeDecodeError) as exc:
                    raise SystemExit(f"cannot read Bruno file {target}: {exc}") from exc
                actual_id = bru_meta_name(existing)
                if actual_id != case_id:
                    raise SystemExit(
                        f"Bruno filename collision: case {case_id} maps to {target}, "
                        f"whose meta.name is {actual_id or '<missing>'}"
                    )
                actual_hash = hashlib.sha256(existing.encode("utf-8")).hexdigest()
                case_changed = bool(
                    baseline.get("materialized_case_fingerprint")
                    and baseline.get("materialized_case_fingerprint") != current_case_fingerprint
                )
                bru_changed = bool(
                    baseline.get("bru_sha256")
                    and baseline.get("bru_sha256") != actual_hash
                )
                if case_changed and bru_changed:
                    raise _conflict(case_id, existing, expected)
                if check:
                    if existing != expected:
                        created.append(target)
                    materialized_state[case_id] = {
                        "materialized_case_fingerprint": current_case_fingerprint,
                        "bru_sha256": actual_hash,
                    }
                    continue
                if bru_changed and not case_changed and existing != expected:
                    raise SystemExit(json.dumps({
                        "error": "manual_bru_drift",
                        "case_id": case_id,
                        "message": ".bru was modified outside materialization; reconcile it with cases.yaml",
                        "diff": list(difflib.unified_diff(
                            existing.splitlines(), expected.splitlines(),
                            fromfile="current.bru", tofile="cases.yaml rendered", lineterm="",
                        ))[:80],
                    }, ensure_ascii=False, indent=2))
                if expected == existing:
                    materialized_state[case_id] = {
                        "materialized_case_fingerprint": current_case_fingerprint,
                        "bru_sha256": actual_hash,
                    }
                    continue
                created.append(target)
                if not dry_run:
                    target.write_text(expected, encoding="utf-8")
                materialized_state[case_id] = {
                    "materialized_case_fingerprint": current_case_fingerprint,
                    "bru_sha256": hashlib.sha256(expected.encode("utf-8")).hexdigest(),
                }
                continue
            created.append(target)
            if not dry_run:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(expected, encoding="utf-8")
            materialized_state[case_id] = {
                "materialized_case_fingerprint": current_case_fingerprint,
                "bru_sha256": hashlib.sha256(expected.encode("utf-8")).hexdigest(),
            }
        if module_bru.is_dir():
            for existing_path in sorted(module_bru.rglob("*.bru")):
                try:
                    existing = existing_path.read_text(encoding="utf-8", errors="strict")
                except (OSError, UnicodeDecodeError) as exc:
                    raise SystemExit(f"cannot read Bruno file {existing_path}: {exc}") from exc
                if not is_http_request_content(existing):
                    continue
                updated = strip_meta_tags(strip_legacy_auth(existing))
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
    # Remove legacy generated authentication from coordinator/cross-module requests.
    if bruno_root.is_dir() and not module_filter:
        for existing_path in sorted(bruno_root.rglob("*.bru")):
            try:
                existing = existing_path.read_text(encoding="utf-8", errors="strict")
            except (OSError, UnicodeDecodeError) as exc:
                raise SystemExit(f"cannot read Bruno file {existing_path}: {exc}") from exc
            if not is_http_request_content(existing):
                continue
            updated = strip_meta_tags(strip_legacy_auth(existing))
            if updated != existing:
                created.append(existing_path)
                if not dry_run:
                    existing_path.write_text(updated, encoding="utf-8")
    if not dry_run and sync_index and not module_filter:
        sync_index_counts(contracts_root)
    if not dry_run and module_filter and materialized_state:
        selected_module = next(
            (
                directory for directory in modules_root.iterdir()
                if directory.is_dir()
                and module_filter in {
                    directory.name,
                    str((load_data(directory / "endpoints.yaml") or {}).get("module", directory.name)),
                }
            ),
            None,
        )
        if selected_module is not None:
            local_state_path = selected_module / "materialization-state.yaml"
            local_state_path.write_text(
                render_manifest({"version": 1, "cases": materialized_state}, local_state_path),
                encoding="utf-8",
            )
    if not dry_run and not module_filter and isinstance(generation_state, dict) and materialized_state:
        updated_state = dict(generation_state)
        updated_cases = dict(state_cases)
        for case_id, values in materialized_state.items():
            current = dict(updated_cases.get(case_id, {})) if isinstance(updated_cases.get(case_id), dict) else {}
            current.update(values)
            current["fingerprint"] = values["materialized_case_fingerprint"]
            updated_cases[case_id] = current
        updated_state["cases"] = updated_cases
        state_path.write_text(render_manifest(updated_state, state_path), encoding="utf-8")
        write_qa_lock(contracts_root)
    if not dry_run and verify:
        drift = materialize(
            contracts_root,
            bruno_root,
            dry_run=True,
            execution_config_path=execution_config_path,
            module_filter=module_filter,
            sync_index=False,
            check=True,
            verify=False,
        )
        if drift:
            raise SystemExit("materialize consistency check failed: " + ", ".join(str(path) for path in drift))
    if not dry_run and module_filter:
        write_module_lock(qa_root, module_filter)
    return list(dict.fromkeys(created))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("contracts_root", type=Path)
    parser.add_argument("bruno_root", type=Path, nargs="?")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--execution-config", type=Path, help="qa/execution/config.yaml (the only shared runtime config)")
    parser.add_argument("--module", help="materialize only one module id or directory; never updates index.yaml")
    parser.add_argument("--sync-index", action="store_true", help="refresh index counts without materializing requests")
    parser.add_argument("--check", action="store_true", help="report request/assertion drift without overwriting existing files")
    args = parser.parse_args()
    if args.sync_index:
        if args.module:
            parser.error("--sync-index cannot be combined with --module")
        sync_index_counts(args.contracts_root)
        print("synchronized index counts")
        return 0
    if args.bruno_root is None:
        parser.error("bruno_root is required unless --sync-index is used")
    qa_root = qa_root_for_contracts(args.contracts_root)
    generation_errors = validate_stage(qa_root, "generation", module=args.module)
    if generation_errors:
        for error in generation_errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    created = materialize(
        args.contracts_root,
        args.bruno_root,
        args.dry_run or args.check,
        args.execution_config,
        args.module,
        not args.module,
        args.check,
    )
    action = "would materialize" if args.dry_run else "materialized"
    print(f"{action} {len(created)} Bruno/documentation artifact(s)")
    for path in created:
        print(path)
    if args.check and created:
        return 1
    errors = validate_stage(qa_root, "materialization", module=args.module)
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
