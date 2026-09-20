"""Design-document discovery, parsing, and OpenAPI coverage mapping.

Design documents are deliberately treated as opaque reviewed text.  The
parser extracts only auditable section boundaries and a few explicit scalar
markers; it never attempts to infer business behaviour from source code or
runtime responses.
"""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from .manifest_io import load_data

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE")
METHOD_PATH_RE = re.compile(
    r"(?im)^\s*(?:#{1,6}\s*)?(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE)\s+(`?/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%{}-]*`?)\s*$"
)
MARKER_RE = {
    "rule_id": re.compile(r"(?im)^\s*(?:rule[_ ]?id|规则\s*id)\s*[:：]\s*([A-Za-z0-9_.:-]+)"),
    "business_code": re.compile(r"(?im)^\s*(?:business[_ ]?code|业务(?:错误)?码|错误码)\s*[:：=]\s*([^\s,，;；]+)"),
    "http_status": re.compile(r"(?im)^\s*(?:http[_ ]?status|HTTP\s*状态(?:码)?|status)\s*[:：=]\s*(\d{3})"),
    "state": re.compile(r"(?im)^\s*(?:state|状态|最终状态)\s*[:：=]\s*([^\s,，;；]+)"),
    "acceptance_status": re.compile(r"(?im)^\s*(?:acceptance[_ ]?status|接入状态)\s*[:：=]\s*([^\s,，;；]+)"),
    "final_status": re.compile(r"(?im)^\s*(?:final[_ ]?status|最终结果状态)\s*[:：=]\s*([^\s,，;；]+)"),
    "scenario": re.compile(r"(?im)^\s*(?:scenario|case|场景|用例)\s*[:：=]\s*([A-Za-z0-9_.-]+)"),
    "condition": re.compile(r"(?im)^\s*(?:condition|precondition|条件|前置条件)\s*[:：=]\s*(.+?)\s*$"),
    "request": re.compile(r"(?im)^\s*(?:request|请求)\s*[:：]\s*(.+?)\s*$"),
    "async": re.compile(r"(?im)^\s*(?:async|asynchronous|异步)\s*[:：=]\s*(\S+)\s*$"),
    "transition": re.compile(r"(?im)^\s*(?:transition|state[_ ]?transition|状态流转)\s*[:：=]\s*(.+?)\s*$"),
    "side_effect": re.compile(r"(?im)^\s*(?:side[_ ]?effect|副作用)\s*[:：=]\s*(.+?)\s*$"),
    "idempotency": re.compile(r"(?im)^\s*(?:idempotency|幂等)\s*[:：=]\s*(.+?)\s*$"),
    "retry": re.compile(r"(?im)^\s*(?:retry|重试)\s*[:：=]\s*(.+?)\s*$"),
    "concurrency": re.compile(r"(?im)^\s*(?:concurrency|并发|重复提交)\s*[:：=]\s*(.+?)\s*$"),
    "external_failure": re.compile(r"(?im)^\s*(?:external[_ ]?failure|外部系统失败)\s*[:：=]\s*(.+?)\s*$"),
    "flow": re.compile(r"(?im)^\s*(?:test[_ ]?flow|flow|测试流程|自动化流程)\s*[:：=]\s*(.+?)\s*$"),
}
ASSERTION_RE = re.compile(r"(?im)^\s*(?:assert|assertion|断言)\s*[:：]\s*(\$[^=:\s]+)\s*(?:=|equals|等于)\s*(.+?)\s*$")
EXCLUSION_RE = re.compile(r"(?im)^\s*(?:exclude|exclusion|排除)\s*[:：]\s*((?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE)\s+/\S+)")


@dataclass(frozen=True)
class DesignDiscovery:
    files: tuple[Path, ...]
    candidates: tuple[Path, ...]
    hints: tuple[Path, ...]


def _markdown_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        path for pattern in ("*.md", "*.markdown")
        for path in root.rglob(pattern)
        if path.is_file() and ".git" not in path.parts
    )


def _approved_exclusion(item: dict[str, Any]) -> bool:
    """An exclusion is never approved by omission."""

    if "approved" in item:
        return item.get("approved") is True
    return str(item.get("status", "")).strip().casefold() == "approved"


def _typed_value(value: str) -> Any:
    """Preserve reviewed YAML scalar/container types in generated assertions."""

    try:
        return yaml.safe_load(value)
    except yaml.YAMLError as exc:
        raise ValueError(str(exc)) from exc


def _flow_values(content: str, marker_errors: list[str]) -> list[dict[str, Any]]:
    """Read explicit reviewed flow declarations without inferring a workflow.

    A flow is intentionally a small YAML value embedded in the design section:
    ``Test Flow: {id: JOB_FLOW, steps: [{rule_id: JOB_ACCEPTED, capture: job_id,
    capture_path: $.jobId}, ...]}``. No endpoint or case is guessed when a
    step is incomplete.
    """

    declarations: list[dict[str, Any]] = []
    for match in MARKER_RE["flow"].finditer(content):
        try:
            value = _typed_value(match.group(1).strip())
        except ValueError as exc:
            marker_errors.append(f"invalid Flow mapping: {exc}")
            continue
        if isinstance(value, dict):
            declarations.append(value)
        elif isinstance(value, list):
            declarations.append({"steps": value})
        elif isinstance(value, str) and value.strip():
            declarations.append({"id": value.strip()})
        else:
            marker_errors.append("Flow must be a YAML object or step list")
    return declarations


def _instruction_hints(project_root: Path) -> list[Path]:
    hints: list[Path] = []
    for name in ("AGENTS.md", "README.md", "README", "README.txt"):
        path = project_root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except OSError:
            continue
        for raw in re.findall(r"(?im)(?:^|[`\s])((?:docs|doc|design)[/\\][^\s`),;]+)", text):
            candidate = (project_root / raw.replace("\\", "/")).resolve()
            if candidate.is_file() and candidate.suffix.lower() in {".md", ".markdown"}:
                hints.append(candidate)
            elif candidate.is_dir() and _markdown_files(candidate):
                hints.append(candidate)
    return list(dict.fromkeys(hints))


def discover(
    project_root: Path,
    *,
    design_roots: Iterable[Path] = (),
    design_files: Iterable[Path] = (),
) -> DesignDiscovery:
    """Find reviewed Markdown design sources in the prescribed order."""

    project_root = project_root.resolve()
    explicit_files = [path.resolve() for path in design_files]
    explicit_roots = [path.resolve() for path in design_roots]
    if explicit_files or explicit_roots:
        files = [path for path in explicit_files if path.is_file()]
        for root in explicit_roots:
            files.extend(_markdown_files(root))
        return DesignDiscovery(tuple(dict.fromkeys(files)), tuple(explicit_roots), tuple())

    hints = _instruction_hints(project_root)
    hinted_files = [path for path in hints if path.is_file()]
    hinted_roots = [path for path in hints if path.is_dir()]
    if hinted_files or hinted_roots:
        files = hinted_files[:]
        for root in hinted_roots:
            files.extend(_markdown_files(root))
        candidates = list(dict.fromkeys([*hinted_roots, *(path.parent for path in hinted_files)]))
        return DesignDiscovery(tuple(dict.fromkeys(files)), tuple(candidates), tuple(hints))

    roots = [
        project_root / "docs" / "design",
        project_root / "docs" / "详细设计",
        project_root / "design",
        project_root / "doc" / "design",
    ]
    existing = [root for root in roots if root.is_dir() and _markdown_files(root)]
    files = [file for root in existing for file in _markdown_files(root)]
    return DesignDiscovery(tuple(dict.fromkeys(files)), tuple(existing), tuple())


def _section_records(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8", errors="strict")
    matches = list(METHOD_PATH_RE.finditer(text))
    records: list[dict[str, Any]] = []
    for section_index, match in enumerate(matches):
        method = match.group(1).upper()
        route = match.group(2).strip("`")
        body_start = match.end()
        body_end = matches[section_index + 1].start() if section_index + 1 < len(matches) else len(text)
        body = text[body_start:body_end]
        rule_matches = list(MARKER_RE["rule_id"].finditer(body))
        chunks: list[tuple[str, str | None, int]] = []
        if len(rule_matches) <= 1:
            chunks.append((body.strip(), rule_matches[0].group(1) if rule_matches else None, body_start))
        else:
            preamble = body[:rule_matches[0].start()].strip()
            for rule_index, rule_match in enumerate(rule_matches):
                end = rule_matches[rule_index + 1].start() if rule_index + 1 < len(rule_matches) else len(body)
                content = body[rule_match.start():end].strip()
                if preamble:
                    content = preamble + "\n" + content
                chunks.append((content, rule_match.group(1), body_start + rule_match.start()))
        section_title = f"{method} {route}"
        section_line = text[:match.start()].count("\n") + 1
        section_digest = hashlib.sha256(body.strip().encode("utf-8")).hexdigest()
        for rule_index, (content, rule_id, offset) in enumerate(chunks):
            marker_errors: list[str] = []
            codes = []
            for value in MARKER_RE["business_code"].finditer(content):
                try:
                    codes.append(_typed_value(value.group(1).strip()))
                except ValueError as exc:
                    marker_errors.append(f"invalid Business code: {exc}")
            statuses = [int(value.group(1)) for value in MARKER_RE["http_status"].finditer(content)]
            states = [value.group(1).strip() for value in MARKER_RE["state"].finditer(content)]
            acceptance = [value.group(1).strip() for value in MARKER_RE["acceptance_status"].finditer(content)]
            final = [value.group(1).strip() for value in MARKER_RE["final_status"].finditer(content)]
            scenario_match = MARKER_RE["scenario"].search(content)
            scenario = scenario_match.group(1).strip().casefold() if scenario_match else ""
            condition_match = MARKER_RE["condition"].search(content)
            async_match = MARKER_RE["async"].search(content)
            is_async = False
            if async_match:
                try:
                    async_value = _typed_value(async_match.group(1).strip())
                except ValueError as exc:
                    marker_errors.append(f"invalid Async value: {exc}")
                else:
                    if isinstance(async_value, bool):
                        is_async = async_value
                    else:
                        marker_errors.append("Async must be true or false")
            if not scenario:
                scenario = "success" if rule_index == 0 else (
                    "business_error"
                    if codes and str(codes[0]).casefold() not in {"0", "ok", "success"}
                    else "success"
                )
            assertions = []
            for assertion in ASSERTION_RE.finditer(content):
                try:
                    expected = _typed_value(assertion.group(2).strip())
                except ValueError as exc:
                    marker_errors.append(f"invalid Assert value for {assertion.group(1).strip()}: {exc}")
                    continue
                assertions.append({"path": assertion.group(1).strip(), "equals": expected})
            request_match = MARKER_RE["request"].search(content)
            request: dict[str, Any] | None = None
            if request_match:
                try:
                    parsed_request = _typed_value(request_match.group(1).strip())
                except ValueError as exc:
                    marker_errors.append(f"invalid Request mapping: {exc}")
                else:
                    if isinstance(parsed_request, dict):
                        request = parsed_request
                    else:
                        marker_errors.append("Request must be a YAML/JSON object")
            flows = _flow_values(content, marker_errors)
            title = rule_id or section_title
            records.append({
                "id": rule_id,
                "method": method,
                "path": route,
                "title": title,
                "content": content,
                "scenario": scenario,
                "condition": condition_match.group(1).strip() if condition_match else "",
                "business_codes": codes,
                "http_statuses": statuses,
                "states": states,
                "transitions": [value.group(1).strip() for value in MARKER_RE["transition"].finditer(content)],
                "side_effects": [value.group(1).strip() for value in MARKER_RE["side_effect"].finditer(content)],
                "idempotency": [value.group(1).strip() for value in MARKER_RE["idempotency"].finditer(content)],
                "retries": [value.group(1).strip() for value in MARKER_RE["retry"].finditer(content)],
                "concurrency": [value.group(1).strip() for value in MARKER_RE["concurrency"].finditer(content)],
                "external_failures": [value.group(1).strip() for value in MARKER_RE["external_failure"].finditer(content)],
                "async": is_async,
                "acceptance_statuses": acceptance,
                "final_statuses": final,
                "assertions": assertions,
                "request": request,
                "request_declared": request_match is not None,
                "_flows": flows,
                "marker_errors": marker_errors,
                "section_line": section_line,
                "section_sha256": section_digest,
                "evidence": {
                    "source_kind": "design",
                    "file": str(path),
                    "symbol": title,
                    "line": text[:offset].count("\n") + 1,
                    "endpoint_scope": [f"{method} {route}"],
                    "confidence": "high",
                },
            })
    return records


def _explicit_exclusions(
    project_root: Path,
    files: Iterable[Path],
    qa_root: Path | None = None,
) -> list[dict[str, Any]]:
    qa_root = (qa_root or project_root / "qa").resolve()
    candidates = [
        project_root / "exclusions.yaml",
        qa_root / "constraints" / "exclusions.yaml",
        qa_root / "contracts" / "exclusions.yaml",
    ]
    modules = qa_root / "contracts" / "modules"
    if modules.is_dir():
        candidates.extend(modules.glob("*/exclusions.yaml"))
    candidates.extend(path.parent / "exclusions.yaml" for path in files)
    exclusions: list[dict[str, Any]] = []
    for path in dict.fromkeys(candidates):
        if not path.is_file():
            continue
        document = load_data(path)
        values = document.get("exclusions", []) if isinstance(document, dict) else []
        if isinstance(values, list):
            exclusions.extend(item for item in values if isinstance(item, dict))
    return exclusions


def _request_shape_errors(rule: dict[str, Any], endpoint: dict[str, Any]) -> list[str]:
    request = rule.get("request")
    if not isinstance(request, dict):
        return []
    errors: list[str] = []
    allowed = {
        "path", "path_parameters", "query", "headers", "body", "body_type",
        "content_type", "omit_common_headers",
    }
    unknown = sorted(str(key) for key in request if key not in allowed)
    if unknown:
        errors.append("unsupported request field(s): " + ", ".join(unknown))
    endpoint_path = str(endpoint.get("path", ""))
    if request.get("path") not in {None, "", endpoint_path}:
        errors.append(f"request.path must remain {endpoint_path}")
    parameters = [item for item in endpoint.get("parameters", []) if isinstance(item, dict)]
    for request_field, location in (("path_parameters", "path"), ("query", "query")):
        values = request.get(request_field)
        if not isinstance(values, dict):
            continue
        declared = {str(item.get("name")) for item in parameters if item.get("in") == location}
        undeclared = sorted(str(name) for name in values if str(name) not in declared)
        if undeclared:
            errors.append(f"request.{request_field} contains field(s) absent from OpenAPI: {', '.join(undeclared)}")
    headers = request.get("headers")
    if isinstance(headers, dict):
        declared_headers = {
            str(item.get("name", "")).casefold()
            for item in parameters if item.get("in") == "header" and item.get("name")
        }
        declared_headers.update(
            str(name).casefold()
            for name in endpoint.get("security_headers", [])
            if str(name).strip()
        )
        if endpoint.get("security"):
            declared_headers.update({"authorization", "cookie"})
        undeclared_headers = sorted(
            str(name) for name in headers if str(name).casefold() not in declared_headers
        )
        if undeclared_headers:
            errors.append(
                "request.headers contains field(s) absent from OpenAPI: "
                + ", ".join(undeclared_headers)
            )
    if "body" in request:
        body = endpoint.get("request_body")
        if not isinstance(body, dict) or not body:
            errors.append("request.body is not declared by OpenAPI")
    elif isinstance(request.get("body_type"), str) and request.get("body_type"):
        errors.append("request.body_type is declared without request.body")
    requested_media = str(request.get("content_type") or request.get("body_type") or "").strip()
    body = endpoint.get("request_body") if isinstance(endpoint.get("request_body"), dict) else {}
    content = body.get("content", {}) if isinstance(body.get("content"), dict) else {}
    if requested_media and content:
        normalized = requested_media.casefold()
        media_matches = {
            str(media).casefold() for media in content
            if str(media).casefold() == normalized
            or (normalized in {"json", "application/json"} and "json" in str(media).casefold())
            or (normalized in {"multipart", "multipart-form", "multipart/form-data"} and "multipart/form-data" in str(media).casefold())
            or (normalized in {"form", "form-urlencoded", "application/x-www-form-urlencoded"} and "x-www-form-urlencoded" in str(media).casefold())
            or (normalized in {"text", "text/plain"} and str(media).casefold().startswith("text/"))
            or (normalized in {"xml", "application/xml"} and "xml" in str(media).casefold())
            or (normalized == "file" and any(
                isinstance(value, dict)
                and isinstance(value.get("schema"), dict)
                and value["schema"].get("format") == "binary"
                for value in content.values()
            ))
        }
        if not media_matches:
            errors.append(f"request media type {requested_media} is absent from OpenAPI")
    if isinstance(body, dict) and "body" in request and request.get("body") is not None:
        media_name = next(iter(content), None)
        schema = content.get(media_name, {}).get("schema") if isinstance(media_name, str) else None
        if not isinstance(schema, dict):
            schema = body.get("schema") if isinstance(body.get("schema"), dict) else None
        if isinstance(schema, dict):
            errors.extend(_schema_request_errors(request["body"], schema, "request.body"))
    if isinstance(request, dict) and body.get("required") is True and "body" not in request:
        errors.append("request.body is required by OpenAPI")
    return errors


def _schema_request_errors(value: Any, schema: dict[str, Any], path: str) -> list[str]:
    """Reject reviewed request examples that contradict OpenAPI's shape."""

    errors: list[str] = []
    if value is None:
        if schema.get("nullable") is True or schema.get("type") == "null":
            return errors
        return [f"{path} is null but OpenAPI does not allow null"]
    expected = str(schema.get("type", "")).casefold()
    type_ok = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
    }
    if expected in type_ok and not type_ok[expected]:
        errors.append(f"{path} violates OpenAPI type {expected}")
        return errors
    if isinstance(schema.get("enum"), list) and value not in schema["enum"]:
        errors.append(f"{path} is outside the OpenAPI enum")
    if isinstance(value, str):
        if schema.get("minLength") is not None and len(value) < int(schema["minLength"]):
            errors.append(f"{path} is shorter than OpenAPI minLength {schema['minLength']}")
        if schema.get("maxLength") is not None and len(value) > int(schema["maxLength"]):
            errors.append(f"{path} is longer than OpenAPI maxLength {schema['maxLength']}")
        if schema.get("pattern"):
            try:
                if re.fullmatch(str(schema["pattern"]), value) is None:
                    errors.append(f"{path} violates the OpenAPI pattern")
            except re.error as exc:
                errors.append(f"OpenAPI pattern for {path} is invalid: {exc}")
        format_name = str(schema.get("format", "")).casefold()
        format_patterns = {
            "email": r"[^@\s]+@[^@\s]+\.[^@\s]+",
            "uuid": r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}",
            "date": r"\d{4}-\d{2}-\d{2}",
            "date-time": r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})",
        }
        if format_name in format_patterns and re.fullmatch(format_patterns[format_name], value) is None:
            errors.append(f"{path} violates the OpenAPI {format_name} format")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if schema.get("minimum") is not None and value < schema["minimum"]:
            errors.append(f"{path} is below OpenAPI minimum {schema['minimum']}")
        if schema.get("maximum") is not None and value > schema["maximum"]:
            errors.append(f"{path} is above OpenAPI maximum {schema['maximum']}")
    if isinstance(value, dict):
        properties = schema.get("properties", {}) if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required", []) if isinstance(schema.get("required"), list) else []
        for name in required:
            if name not in value:
                errors.append(f"{path} is missing OpenAPI-required field {name}")
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in properties:
                    errors.append(f"{path}.{name} is absent from OpenAPI")
        for name, child in properties.items():
            if name in value and isinstance(child, dict):
                errors.extend(_schema_request_errors(value[name], child, f"{path}.{name}"))
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            errors.extend(_schema_request_errors(item, schema["items"], f"{path}[{index}]"))
    return errors


def _normalize_flows(sections: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate explicit design flow declarations and bind them to rule IDs."""

    rules = {str(section.get("id")): section for section in sections if section.get("id")}
    declarations = [
        (str(section.get("id", "")), value)
        for section in sections
        for value in section.pop("_flows", [])
        if isinstance(value, dict)
    ]
    flows: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: dict[str, str] = {}
    rule_flow: dict[str, str] = {}
    allowed_flow = {"id", "mode", "steps", "cleanup"}
    allowed_step = {"rule_id", "operation", "capture", "capture_path", "uses", "assert_absent"}
    for declaring_rule, declaration in declarations:
        unknown = sorted(str(key) for key in declaration if key not in allowed_flow)
        flow_id = str(declaration.get("id", "")).strip()
        label = flow_id or "<missing>"
        if unknown:
            errors.append(f"design flow {label} has unsupported field(s): {', '.join(unknown)}")
        if not flow_id or re.fullmatch(r"[A-Za-z0-9_.:-]+", flow_id) is None:
            errors.append("design Flow.id must be a non-empty stable identifier")
            continue
        mode = str(declaration.get("mode", "sequential")).strip().casefold()
        if mode != "sequential":
            errors.append(f"design flow {flow_id} mode {mode or '<empty>'} is not executable by the Bruno runner")
        raw_steps = declaration.get("steps")
        if not isinstance(raw_steps, list) or len(raw_steps) < 2:
            errors.append(f"design flow {flow_id} must declare at least two ordered steps")
            continue
        if declaring_rule not in [str(item.get("rule_id")) for item in raw_steps if isinstance(item, dict)]:
            errors.append(f"design flow {flow_id} must include its declaring rule {declaring_rule}")
        steps: list[dict[str, Any]] = []
        captured: set[str] = set()
        referenced: set[str] = set()
        for index, raw_step in enumerate(raw_steps, start=1):
            if not isinstance(raw_step, dict):
                errors.append(f"design flow {flow_id} step {index} must be an object")
                continue
            unknown_step = sorted(str(key) for key in raw_step if key not in allowed_step)
            if unknown_step:
                errors.append(
                    f"design flow {flow_id} step {index} has unsupported field(s): {', '.join(unknown_step)}"
                )
            rule_id = str(raw_step.get("rule_id", "")).strip()
            operation = str(raw_step.get("operation", "")).strip().casefold()
            if not rule_id or rule_id not in rules:
                errors.append(f"design flow {flow_id} step {index} references unknown rule {rule_id or '<missing>'}")
            elif rule_id in referenced:
                errors.append(f"design flow {flow_id} repeats rule {rule_id}; use distinct reviewed rule IDs")
            elif rule_id in rule_flow and rule_flow[rule_id] != flow_id:
                errors.append(
                    f"design rule {rule_id} belongs to multiple flows: {rule_flow[rule_id]} and {flow_id}"
                )
            referenced.add(rule_id)
            rule_flow[rule_id] = flow_id
            if not operation:
                errors.append(f"design flow {flow_id} step {index} has no operation")
            raw_capture = raw_step.get("capture")
            capture_paths: dict[str, str] = {}
            if isinstance(raw_capture, dict):
                capture_paths = {str(name): str(path) for name, path in raw_capture.items()}
            elif isinstance(raw_capture, str) and raw_capture.strip():
                capture_paths = {raw_capture.strip(): str(raw_step.get("capture_path", ""))}
            elif raw_capture not in (None, [], {}):
                errors.append(f"design flow {flow_id} step {index} capture must map variable names to JSON paths")
            for name, path in capture_paths.items():
                if not name or not path.startswith("$"):
                    errors.append(
                        f"design flow {flow_id} step {index} capture {name or '<missing>'} needs a JSON path"
                    )
            raw_uses = raw_step.get("uses", [])
            uses = [raw_uses] if isinstance(raw_uses, str) else raw_uses if isinstance(raw_uses, list) else []
            uses = [str(value).strip() for value in uses if str(value).strip()]
            if raw_uses not in (None, [], "") and not isinstance(raw_uses, (str, list)):
                errors.append(f"design flow {flow_id} step {index} uses must be a string or list")
            missing = sorted(set(uses) - captured)
            if missing:
                errors.append(
                    f"design flow {flow_id} step {index} uses values before capture: {', '.join(missing)}"
                )
            rule = rules.get(rule_id, {})
            if uses and not set(uses).issubset(_request_variables(rule.get("request"))):
                missing_request = sorted(set(uses) - _request_variables(rule.get("request")))
                errors.append(
                    f"design flow {flow_id} step {index} request does not use captured value(s): "
                    + ", ".join(missing_request)
                )
            step: dict[str, Any] = {"rule_id": rule_id, "operation": operation}
            if capture_paths:
                step["capture"] = list(capture_paths)
                step["capture_paths"] = capture_paths
            if uses:
                step["uses"] = uses
            if raw_step.get("assert_absent"):
                if not isinstance(raw_step["assert_absent"], str) or not raw_step["assert_absent"].startswith("$"):
                    errors.append(f"design flow {flow_id} step {index} assert_absent needs a JSON path")
                elif not any(
                    item.get("path") == raw_step["assert_absent"] and item.get("equals", object()) is None
                    for item in rule.get("assertions", []) if isinstance(item, dict)
                ):
                    errors.append(
                        f"design flow {flow_id} step {index} has no design assertion for absent {raw_step['assert_absent']}"
                    )
                step["assert_absent"] = raw_step["assert_absent"]
            steps.append(step)
            captured.update(capture_paths)
        normalized: dict[str, Any] = {
            "id": flow_id,
            "mode": mode,
            "source": "design",
            "design_rule_ids": [str(step.get("rule_id")) for step in steps],
            "steps": steps,
        }
        if str(declaration.get("cleanup", "")).strip():
            normalized["cleanup"] = str(declaration["cleanup"]).strip()
        fingerprint = repr(normalized)
        if flow_id in seen and seen[flow_id] != fingerprint:
            errors.append(f"conflicting design flow declarations for {flow_id}")
        elif flow_id not in seen:
            seen[flow_id] = fingerprint
            flows.append(normalized)
    return flows, errors


def _request_variables(value: Any) -> set[str]:
    if isinstance(value, str):
        return {item.strip() for item in re.findall(r"\{\{([^}]+)\}\}", value) if item.strip()}
    if isinstance(value, dict):
        return set().union(*(_request_variables(item) for item in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_request_variables(item) for item in value), set())
    return set()


def build_rules(
    project_root: Path,
    files: Iterable[Path],
    manifest: dict[str, Any],
    *,
    qa_root: Path | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Create auditable design rules and return blocking mapping errors."""

    project_root = project_root.resolve()
    files = tuple(path.resolve() for path in files)
    sections: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []
    for path in files:
        try:
            content = path.read_bytes()
            parsed = _section_records(path)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            return {}, [f"cannot read design document {path}: {exc}"]
        documents.append({
            "path": str(path),
            "sha256": hashlib.sha256(content).hexdigest(),
            "sections": len({(item.get("method"), item.get("path"), item.get("section_line")) for item in parsed}),
        })
        for item in parsed:
            item["id"] = item["id"] or "DESIGN-" + hashlib.sha256(
                f"{path}:{item['method']} {item['path']}:{item.get('section_line')}:{item.get('section_sha256')}".encode("utf-8")
            ).hexdigest()[:12]
            sections.append(item)
    flows, flow_errors = _normalize_flows(sections)
    endpoints = [item for item in manifest.get("endpoints", []) if isinstance(item, dict)]
    endpoint_keys = {
        f"{str(item.get('method', '')).upper()} {item.get('path')}": str(item.get("id", ""))
        for item in endpoints
    }
    exclusions = _explicit_exclusions(project_root, files, qa_root)
    errors: list[str] = list(flow_errors)
    excluded_keys = {
        f"{str(item.get('method', '')).upper()} {item.get('path')}"
        for item in exclusions
        if _approved_exclusion(item) and item.get("reason") and item.get("method") and item.get("path")
    }
    for item in exclusions:
        if not _approved_exclusion(item):
            continue
        if not str(item.get("reason", "")).strip():
            errors.append("approved exclusion must include a reason")
            continue
        prefix = str(item.get("path_prefix", "")).strip()
        if prefix:
            method = str(item.get("method", "")).upper()
            for key in endpoint_keys:
                endpoint_method, endpoint_path = key.split(" ", 1)
                if endpoint_path.startswith(prefix) and (not method or method == endpoint_method):
                    excluded_keys.add(key)
    for item in exclusions:
        keys = []
        exact = f"{str(item.get('method', '')).upper()} {item.get('path')}"
        if exact in endpoint_keys:
            keys.append(exact)
        prefix = str(item.get("path_prefix", "")).strip()
        if prefix:
            method = str(item.get("method", "")).upper()
            keys.extend(
                key for key in endpoint_keys
                if key.split(" ", 1)[1].startswith(prefix) and (not method or key.startswith(method + " "))
            )
        endpoint_ids = list(dict.fromkeys(endpoint_keys[key] for key in keys))
        if endpoint_ids:
            item["endpoint_ids"] = endpoint_ids
            if len(endpoint_ids) == 1:
                item.setdefault("endpoint_id", endpoint_ids[0])
        elif _approved_exclusion(item) and str(item.get("reason", "")).strip():
            errors.append("approved exclusion does not match an OpenAPI endpoint")
    by_key: dict[str, list[dict[str, Any]]] = {}
    for section in sections:
        key = f"{section['method']} {section['path']}"
        by_key.setdefault(key, []).append(section)
        if key not in endpoint_keys:
            errors.append(f"design contract drift: {key} is not present in OpenAPI")
    for key in endpoint_keys:
        if key not in by_key and key not in excluded_keys:
            errors.append(f"missing design documentation for OpenAPI endpoint {key}")
    for key, values in by_key.items():
        fingerprints = {str(item.get("section_sha256", "")) for item in values}
        files_for_key = {str(item.get("evidence", {}).get("file", "")) for item in values}
        if len(fingerprints) > 1 and len(files_for_key) > 1:
            errors.append(f"conflicting design documents for {key}")
    rule_ids: set[str] = set()
    rules_by_id = {str(section["id"]): section for section in sections}
    rules = []
    manual_confirmations: list[dict[str, Any]] = []
    flows_by_rule: dict[str, list[dict[str, Any]]] = {}
    for flow in flows:
        for step in flow.get("steps", []):
            flows_by_rule.setdefault(str(step.get("rule_id", "")), []).append(flow)
    for section in sections:
        section = dict(section)
        key = f"{section['method']} {section['path']}"
        section["endpoint_id"] = endpoint_keys.get(key)
        confirmation_reasons: list[str] = []

        def require_confirmation(reason: str) -> None:
            confirmation_reasons.append(reason)
            errors.append(f"design rule {section['id']} {reason}")

        if section["id"] in rule_ids:
            errors.append(f"duplicate design rule ID: {section['id']}")
        rule_ids.add(str(section["id"]))
        if section.get("scenario") not in {
            "success", "authentication", "authorization", "query", "business_error", "safety",
        }:
            require_confirmation("must declare a supported Scenario")
        endpoint = next((item for item in endpoints if str(item.get("id")) == section.get("endpoint_id")), None)
        for marker_error in section.get("marker_errors", []):
            require_confirmation(marker_error)
        siblings = by_key.get(key, [])
        if (section.get("scenario") != "success" or len(siblings) > 1) and not isinstance(section.get("request"), dict):
            require_confirmation("must declare an explicit Request mapping for its business branch")
        if not section.get("assertions"):
            require_confirmation("must declare at least one explicit business-result Assert")
        if isinstance(endpoint, dict):
            for request_error in _request_shape_errors(section, endpoint):
                errors.append(
                    f"design request conflicts with OpenAPI for rule {section['id']}: {request_error}; "
                    "revise the design document"
                )
        declared_statuses = {
            int(status)
            for status in (endpoint.get("responses", {}) if isinstance(endpoint, dict) else {})
            if str(status).isdigit()
        }
        for status in section.get("http_statuses", []):
            if status not in declared_statuses:
                errors.append(f"design rule {section['id']} HTTP status {status} is not declared by OpenAPI {key}")
        if section.get("async") and (
            not section.get("acceptance_statuses") or not section.get("final_statuses")
        ):
            require_confirmation("must declare acceptance status and final status for its async behavior")
        executable_flows = [flow for flow in flows_by_rule.get(str(section["id"]), []) if len(flow.get("steps", [])) >= 2]
        if section.get("async") and not executable_flows:
            require_confirmation(
                "requires an executable acceptance/final flow; single-request async metadata is not executable"
            )
        if section.get("async"):
            require_confirmation("requires bounded final-state polling; a sequential query cannot prove async completion")
        if section.get("scenario") == "safety" and not executable_flows:
            require_confirmation(
                "requires an executable repeated/concurrent request flow; single-request safety metadata is not executable"
            )
        repeated_endpoint_flow = any(
            sum(
                1
                for step in flow.get("steps", [])
                if (
                    f"{rules_by_id[str(step.get('rule_id'))]['method']} "
                    f"{rules_by_id[str(step.get('rule_id'))]['path']}"
                ) == key
            ) >= 2
            for flow in executable_flows
            if all(str(step.get("rule_id")) in rules_by_id for step in flow.get("steps", []))
        )
        if section.get("idempotency") and not repeated_endpoint_flow:
            require_confirmation(
                "requires an executable repeated request flow for the same endpoint"
            )
        if section.get("retries") and not any(
            str(step.get("operation", "")) in {"retry", "retry-request"}
            for flow in executable_flows for step in flow.get("steps", [])
        ):
            require_confirmation(
                "requires an executable retry step; metadata alone is not executable"
            )
        if section.get("concurrency"):
            require_confirmation(
                "requires a real concurrent execution mechanism; sequential flow metadata is not concurrency evidence"
            )
        if section.get("external_failures"):
            require_confirmation(
                "requires authorized fault injection and verified restoration; a flow step label is not execution evidence"
            )
        if confirmation_reasons:
            section["manual_confirmation"] = {
                "required": True,
                "reasons": list(dict.fromkeys(confirmation_reasons)),
            }
            manual_confirmations.append({
                "rule_id": section["id"],
                "reasons": list(dict.fromkeys(confirmation_reasons)),
                "evidence": section["evidence"],
            })
        rules.append(section)
    return {
        "version": 1,
        "source": "design",
        "documents": documents,
        "rules": rules,
        "flows": flows,
        "exclusions": exclusions,
        "manual_confirmations": manual_confirmations,
        "coverage": {
            "openapi_endpoints": len(endpoints),
            "documented_endpoints": len({key for key in by_key if key in endpoint_keys}),
            "excluded_endpoints": len(excluded_keys),
        },
    }, sorted(dict.fromkeys(errors))


def summary(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "sha256": hashlib.sha256(
            str([(item.get("path"), item.get("sha256")) for item in document.get("documents", [])]).encode("utf-8")
        ).hexdigest(),
        "documents": [
            {"path": item.get("path"), "sha256": item.get("sha256")}
            for item in document.get("documents", []) if isinstance(item, dict)
        ],
        "rule_count": len(document.get("rules", [])) if isinstance(document.get("rules"), list) else 0,
    }


def apply_to_contracts(contracts_root: Path, document: dict[str, Any]) -> list[Path]:
    """Attach design provenance to cases/logic without consulting source code."""

    changed: list[Path] = []
    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise ValueError("design rules require PyYAML") from exc
    rules = [item for item in document.get("rules", []) if isinstance(item, dict)]
    flows = [item for item in document.get("flows", []) if isinstance(item, dict)]
    flow_steps_by_rule = {
        str(step.get("rule_id")): step
        for flow in flows
        for step in flow.get("steps", [])
        if isinstance(step, dict) and step.get("rule_id")
    }
    flow_rank = {
        str(step.get("rule_id")): (flow_index, step_index)
        for flow_index, flow in enumerate(flows)
        for step_index, step in enumerate(flow.get("steps", []))
        if isinstance(step, dict) and step.get("rule_id")
    }
    # OpenAPI can establish protocol/shape checks on its own.  Query semantics,
    # authentication, and authorization behavior are business expectations and
    # remain design-backed even when OpenAPI exposes related parameters/security.
    openapi_scenarios = {"validation", "file"}
    by_endpoint: dict[str, list[dict[str, Any]]] = {}
    for rule in rules:
        endpoint_id = str(rule.get("endpoint_id", ""))
        if endpoint_id:
            by_endpoint.setdefault(endpoint_id, []).append(rule)
    approved_exclusions: dict[str, dict[str, Any]] = {}
    for exclusion in document.get("exclusions", []):
        if not isinstance(exclusion, dict) or not _approved_exclusion(exclusion):
            continue
        endpoint_ids = exclusion.get("endpoint_ids", [])
        if not isinstance(endpoint_ids, list):
            endpoint_ids = [exclusion.get("endpoint_id")]
        for endpoint_id in endpoint_ids:
            if endpoint_id:
                approved_exclusions[str(endpoint_id)] = exclusion
    modules = contracts_root / "modules"
    module_directories = {
        str((load_data(directory / "endpoints.yaml") or {}).get("module", directory.name)): directory
        for directory in sorted(path for path in modules.iterdir() if path.is_dir())
        if (directory / "endpoints.yaml").is_file()
    } if modules.is_dir() else {}
    endpoint_modules = {
        str(endpoint.get("id")): module_id
        for module_id, directory in module_directories.items()
        for endpoint in (load_data(directory / "endpoints.yaml") or {}).get("endpoints", [])
        if isinstance(endpoint, dict) and endpoint.get("id")
    }
    rule_modules = {
        str(rule.get("id")): endpoint_modules.get(str(rule.get("endpoint_id")), "")
        for rule in rules
    }
    for flow in flows:
        owners = {
            rule_modules.get(str(step.get("rule_id")), "")
            for step in flow.get("steps", []) if isinstance(step, dict)
        } - {""}
        if len(owners) != 1:
            raise ValueError(
                f"design flow {flow.get('id')} must stay within one Tag module; use the E2E domain for cross-module flows"
            )
    case_locations: dict[str, tuple[str, str]] = {}
    flow_endpoint_ids = {
        str(rule.get("endpoint_id"))
        for rule in rules if str(rule.get("id")) in flow_steps_by_rule
    }
    for directory in sorted(path for path in modules.iterdir() if path.is_dir()) if modules.is_dir() else []:
        endpoint_doc = load_data(directory / "endpoints.yaml") if (directory / "endpoints.yaml").is_file() else {}
        module_endpoint_ids = {
            str(endpoint.get("id"))
            for endpoint in endpoint_doc.get("endpoints", [])
            if isinstance(endpoint, dict) and endpoint.get("id")
        } if isinstance(endpoint_doc, dict) else set()
        excluded_endpoint_ids = module_endpoint_ids & set(approved_exclusions)
        cases_path = directory / "cases.yaml"
        cases_doc = load_data(cases_path) if cases_path.is_file() else {}
        cases = cases_doc.get("cases", []) if isinstance(cases_doc, dict) else []
        all_cases = [
            case for case in cases
            if isinstance(case, dict) and str(case.get("endpoint_id")) not in excluded_endpoint_ids
        ] if isinstance(cases, list) else []
        if excluded_endpoint_ids and isinstance(endpoint_doc, dict):
            updated_endpoints = dict(endpoint_doc)
            updated_endpoints["endpoints"] = [
                {**endpoint, "case_ids": []}
                if isinstance(endpoint, dict) and str(endpoint.get("id")) in excluded_endpoint_ids
                else endpoint
                for endpoint in endpoint_doc.get("endpoints", [])
            ]
            endpoints_path = directory / "endpoints.yaml"
            rendered = yaml.safe_dump(updated_endpoints, allow_unicode=True, sort_keys=False)
            if endpoints_path.read_text(encoding="utf-8") != rendered:
                endpoints_path.write_text(rendered, encoding="utf-8")
                changed.append(endpoints_path)
            exclusions_path = directory / "exclusions.yaml"
            exclusions_doc = load_data(exclusions_path) if exclusions_path.is_file() else {}
            existing = [
                item for item in exclusions_doc.get("exclusions", [])
                if isinstance(item, dict) and item.get("design_coverage") is not True
            ] if isinstance(exclusions_doc, dict) else []
            for endpoint_id in sorted(excluded_endpoint_ids):
                source = approved_exclusions[endpoint_id]
                existing.append({
                    "endpoint_id": endpoint_id,
                    "method": source.get("method"),
                    "path": source.get("path"),
                    "status": "approved",
                    "reason": source.get("reason"),
                    "design_coverage": True,
                })
            updated_exclusions = dict(exclusions_doc) if isinstance(exclusions_doc, dict) else {}
            updated_exclusions.update({
                "version": 1,
                "module": endpoint_doc.get("module", directory.name),
                "exclusions": existing,
            })
            rendered = yaml.safe_dump(updated_exclusions, allow_unicode=True, sort_keys=False)
            if not exclusions_path.is_file() or exclusions_path.read_text(encoding="utf-8") != rendered:
                exclusions_path.write_text(rendered, encoding="utf-8")
                changed.append(exclusions_path)
        templates = {
            str(case.get("endpoint_id")): case
            for case in all_cases if str(case.get("scenario")) == "success"
        }
        existing_design_cases = {
            str(case["design_rule_ids"][0]): case
            for case in all_cases
            if case.get("source") == "design"
            and isinstance(case.get("design_rule_ids"), list)
            and case.get("design_rule_ids")
        }
        cases = []
        logic_by_id: dict[str, dict[str, Any]] = {}
        for case in all_cases:
            endpoint_id = str(case.get("endpoint_id", ""))
            scenario = str(case.get("scenario", "success"))
            if scenario not in openapi_scenarios or endpoint_id not in by_endpoint:
                continue
            case["source"] = "openapi"
            case["openapi_obligation_ids"] = list(case.get("coverage_ids", []))
            case["openapi_trace"] = f"{endpoint_id}:{scenario}"
            case["assertions"] = [
                assertion for assertion in case.get("assertions", [])
                if isinstance(assertion, dict) and not ({"equals", "eq", "contains"} & set(assertion))
            ]
            expected_status = case.get("expected", {}).get("http_status") if isinstance(case.get("expected"), dict) else None
            case["expected"] = {"http_status": expected_status}
            case["review_required"] = False
            case["status"] = "runnable"
            case.pop("review_reason", None)
            case.pop("review_reasons", None)
            case.pop("manual_confirmation", None)
            cases.append(case)
        for endpoint_id, candidates in by_endpoint.items():
            if endpoint_id not in module_endpoint_ids or endpoint_id in excluded_endpoint_ids:
                continue
            template = templates.get(endpoint_id, {
                "id": f"{endpoint_id}_SUCCESS",
                "title": f"设计规则 {endpoint_id}",
                "description": f"验证设计规则 {endpoint_id}",
                "endpoint_id": endpoint_id,
                "request": {},
                "expected": {},
                "coverage_ids": [],
            })
            for selected in candidates:
                rule_id = str(selected["id"])
                scenario = str(selected.get("scenario", "success"))
                previous = existing_design_cases.get(rule_id)
                case = copy.deepcopy(previous or template)
                stable_rule = re.sub(r"[^A-Za-z0-9]+", "_", rule_id).strip("_").upper() or "DESIGN"
                if len(candidates) == 1 and scenario == "success":
                    case.setdefault("id", f"{endpoint_id}_SUCCESS")
                else:
                    case["id"] = f"{endpoint_id}_{stable_rule}"
                    case["title"] = f"{template.get('title', endpoint_id)} - 设计规则 {rule_id}"
                    case.pop("bru", None)
                    case.pop("bru_file", None)
                    case.pop("file_name", None)
                case["endpoint_id"] = endpoint_id
                case["scenario"] = scenario
                case["source"] = "design"
                case["design_rule_ids"] = [str(selected["id"])]
                case["design_evidence"] = [selected["evidence"]]
                flow_step = flow_steps_by_rule.get(rule_id)
                if flow_step and isinstance(flow_step.get("capture_paths"), dict):
                    case["captures"] = copy.deepcopy(flow_step["capture_paths"])
                    case["design_flow_capture"] = True
                elif case.pop("design_flow_capture", None):
                    case.pop("captures", None)
                if flow_step:
                    case["flow_operation"] = str(flow_step.get("operation", ""))
                    if flow_step.get("uses"):
                        case["flow_uses"] = list(flow_step["uses"])
                    else:
                        case.pop("flow_uses", None)
                    if flow_step.get("assert_absent"):
                        case["flow_assert_absent"] = str(flow_step["assert_absent"])
                    else:
                        case.pop("flow_assert_absent", None)
                else:
                    for field in ("flow_operation", "flow_uses", "flow_assert_absent"):
                        case.pop(field, None)
                if isinstance(selected.get("request"), dict):
                    case["request"] = copy.deepcopy(selected["request"])
                elif scenario != "success":
                    case["request"] = {}
                expected = {"http_status": (template.get("expected") or {}).get("http_status")}
                if selected.get("http_statuses"):
                    expected["http_status"] = selected["http_statuses"][0]
                if selected.get("states"):
                    expected["state"] = selected["states"][-1]
                if selected.get("async"):
                    expected["async"] = True
                    if selected.get("acceptance_statuses"):
                        expected["acceptance_status"] = selected["acceptance_statuses"][0]
                    if selected.get("final_statuses"):
                        expected["final_status"] = selected["final_statuses"][0]
                if selected.get("business_codes"):
                    expected["business_code"] = selected["business_codes"][0]
                    path = next((str(item.get("path")) for item in selected.get("assertions", []) if isinstance(item, dict) and str(item.get("path", "")).casefold() in {"$.code", "$.businesscode", "$.errorcode"}), "$.code")
                    expected["business_code_path"] = path
                case["expected"] = expected
                request_is_executable = scenario == "success" or isinstance(selected.get("request"), dict)
                if selected.get("assertions") and request_is_executable:
                    case["assertions"] = list(selected["assertions"])
                    case["review_required"] = False
                    case["status"] = "runnable"
                    case.pop("review_reason", None)
                    case.pop("review_reasons", None)
                    case.pop("manual_confirmation", None)
                else:
                    case["assertions"] = list(selected.get("assertions", []))
                    case["review_required"] = True
                    case["status"] = "draft"
                    case["review_reason"] = (
                        "design rule does not contain an explicit business-result assertion"
                        if not selected.get("assertions") else
                        "design rule needs an explicit Request mapping that triggers this outcome"
                    )
                    case["manual_confirmation"] = {
                        "automation_blocker": case["review_reason"],
                        "search_records": [selected["evidence"]],
                    }
                item = logic_by_id.setdefault(rule_id, {
                    "id": rule_id,
                    "status": "confirmed",
                    "source": "design",
                    "source_symbol": f"{selected['evidence']['file']}:{selected['evidence']['line']}",
                    "condition": selected.get("condition") or selected["title"],
                    "expected_http_status": (selected.get("http_statuses") or [None])[0],
                    "expected_business_code": (selected.get("business_codes") or [None])[0],
                    "expected_state": (selected.get("states") or [None])[-1],
                    "transitions": list(selected.get("transitions", [])),
                    "side_effects": list(selected.get("side_effects", [])),
                    "idempotency": list(selected.get("idempotency", [])),
                    "retries": list(selected.get("retries", [])),
                    "concurrency": list(selected.get("concurrency", [])),
                    "external_failures": list(selected.get("external_failures", [])),
                    "async": selected.get("async") is True,
                    "acceptance_status": (selected.get("acceptance_statuses") or [None])[0],
                    "final_status": (selected.get("final_statuses") or [None])[0],
                    "design_rule_id": rule_id,
                    "evidence": [selected["evidence"]],
                    "case_ids": [],
                })
                item["case_ids"].append(str(case.get("id")))
                case_locations[rule_id] = (
                    str(endpoint_doc.get("module", directory.name)),
                    str(case.get("id")),
                )
                cases.append(case)
        cases.sort(key=lambda case: flow_rank.get(
            str((case.get("design_rule_ids") or [""])[0]),
            (len(flows), len(cases)),
        ))
        if isinstance(cases_doc, dict) and cases_path.is_file():
            updated = dict(cases_doc)
            updated["cases"] = cases
            rendered = yaml.safe_dump(updated, allow_unicode=True, sort_keys=False)
            if cases_path.read_text(encoding="utf-8") != rendered:
                cases_path.write_text(rendered, encoding="utf-8")
                changed.append(cases_path)
        if isinstance(endpoint_doc, dict) and (cases_path.is_file() or excluded_endpoint_ids):
            endpoint_payload = dict(endpoint_doc)
            by_case_endpoint = {}
            for case in cases:
                by_case_endpoint.setdefault(str(case.get("endpoint_id")), []).append(str(case.get("id")))
            updated_endpoints = []
            for endpoint in endpoint_doc.get("endpoints", []):
                if not isinstance(endpoint, dict):
                    updated_endpoints.append(endpoint)
                    continue
                endpoint = dict(endpoint)
                endpoint_id = str(endpoint.get("id"))
                endpoint["case_ids"] = by_case_endpoint.get(endpoint_id, [])
                if endpoint_id in flow_endpoint_ids:
                    endpoint["flow_required"] = True
                    endpoint["flow_kind"] = "design"
                elif endpoint.get("flow_kind") == "design":
                    endpoint.pop("flow_required", None)
                    endpoint.pop("flow_kind", None)
                matrix = dict(endpoint.get("scenario_matrix", {})) if isinstance(endpoint.get("scenario_matrix"), dict) else {}
                endpoint_cases = [case for case in cases if str(case.get("endpoint_id")) == endpoint_id]
                for scenario in ("success", "authentication", "authorization", "query", "business_error", "safety"):
                    applicable = any(str(case.get("scenario")) == scenario for case in endpoint_cases)
                    matrix[scenario] = {
                        "applicable": applicable,
                        "status": "confirmed",
                        "reason": (
                            "reviewed design rule declares this scenario"
                            if applicable else "reviewed design rules do not declare this scenario"
                        ),
                    }
                endpoint["scenario_matrix"] = matrix
                updated_endpoints.append(endpoint)
            endpoint_payload["endpoints"] = updated_endpoints
            endpoints_path = directory / "endpoints.yaml"
            rendered = yaml.safe_dump(endpoint_payload, allow_unicode=True, sort_keys=False)
            if endpoints_path.read_text(encoding="utf-8") != rendered:
                endpoints_path.write_text(rendered, encoding="utf-8")
                changed.append(endpoints_path)
        logic_path = directory / "logic.yaml"
        existing = load_data(logic_path) if logic_path.is_file() else {}
        payload = dict(existing) if isinstance(existing, dict) else {}
        payload.update({"version": 1, "module": endpoint_doc.get("module", directory.name), "logic": list(logic_by_id.values())})
        rendered = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        if not logic_path.is_file() or logic_path.read_text(encoding="utf-8") != rendered:
            logic_path.write_text(rendered, encoding="utf-8")
            changed.append(logic_path)
    flows_by_module: dict[str, list[dict[str, Any]]] = {module_id: [] for module_id in module_directories}
    for flow in flows:
        steps = []
        owner = ""
        for step in flow.get("steps", []):
            rule_id = str(step.get("rule_id", ""))
            if rule_id not in case_locations:
                raise ValueError(f"design flow {flow.get('id')} has no generated case for rule {rule_id}")
            step_owner, case_id = case_locations[rule_id]
            owner = owner or step_owner
            rendered_step = {"operation": step.get("operation"), "case_id": case_id}
            for field in ("capture", "uses", "assert_absent"):
                if field in step:
                    rendered_step[field] = copy.deepcopy(step[field])
            steps.append(rendered_step)
        rendered_flow: dict[str, Any] = {
            "id": flow.get("id"),
            "source": "design",
            "design_rule_ids": [str(step.get("rule_id")) for step in flow.get("steps", [])],
            "steps": steps,
        }
        if flow.get("cleanup"):
            rendered_flow["cleanup"] = flow["cleanup"]
        flows_by_module.setdefault(owner, []).append(rendered_flow)
    for module_id, directory in module_directories.items():
        path = directory / "flows.yaml"
        existing = load_data(path) if path.is_file() else {}
        payload = dict(existing) if isinstance(existing, dict) else {}
        payload.update({"version": 1, "module": module_id, "flows": flows_by_module.get(module_id, [])})
        rendered = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        if not path.is_file() or path.read_text(encoding="utf-8") != rendered:
            path.write_text(rendered, encoding="utf-8")
            changed.append(path)
    return changed
