"""Scenario control, ownership, readiness, and isolation contracts."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml

from ...core.schema import (
    E2E_CANDIDATE_KINDS,
    E2E_CONTROL_NAMES,
    E2E_CONTROL_STATUSES,
    E2E_GENERATION_MODES,
    E2E_SCENARIO_STATUSES,
    E2E_STEP_STATUSES,
    SCENARIO_REQUIRED,
    get_schema,
    validate_schema,
)
from .discovery import (
    SHA_RE,
    SKIP_DIRS,
    _error,
    _exact_keys,
    _git,
    _has_constructed_business_value,
    _load_json,
    _load_yaml,
    _missing_placeholder_blockers,
    _missing_placeholders,
    _resolve,
    _secret_errors,
    _semantic_evidence,
    _strings,
    _walk_files,
    discover_documents,
    discover_protocols,
    discovery_errors,
)
from .source_versions import build_generation_lock, generation_lock_errors, input_summary


HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"}
HEADING_RE = re.compile(r"(?m)^\s{0,3}(#{1,6})\s+(.+?)\s*$")
METHOD_PATH_RE = re.compile(
    r"(?im)^\s*(?:#{1,6}\s*)?(?:[-*]\s*)?(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE)\s+(`?/[^\s`]*`?)\s*$"
)
DESIGN_MARKERS = {
    "rule_id": r"rule[_ ]?id|规则\s*id|规则编号",
    "participant": r"participant|参与方和服务边界|参与方|服务|调用方|下游|service",
    "state": r"state|状态|最终状态",
    "acceptance": r"acceptance[_ ]?status|接入状态",
    "final": r"final[_ ]?status|最终结果状态|最终状态",
    "business_code": r"business[_ ]?code|业务(?:错误)?码|错误码",
    "branch": r"branch|分支|条件分支|异常分支",
    "transition": r"transition|状态流转",
    "precondition": r"precondition|前置条件",
    "side_effect": r"side[_ ]?effect|副作用",
    "assertion": r"assert|assertion|断言|最终业务结果",
    "event": r"event|事件|topic|主题",
    "task": r"task|任务",
    "idempotency": r"idempotency|幂等(?:规则)?|重复提交规则",
    "retry": r"retry|retries|重试(?:规则)?",
    "concurrency": r"concurrency|并发(?:规则)?",
    "async_behavior": r"async(?:hronous)?[_ ]?behavior|异步行为",
    "cleanup": r"cleanup|清理",
    "recovery": r"recovery|恢复|补偿",
}
SEMANTIC_RE = re.compile(
    r"业务流程|参与方|服务边界|状态流转|前置条件|异常|业务错误码|异步|消息|事件|最终业务结果|副作用|幂等|重试|并发|补偿|清理|恢复|business flow|business process|participants?|service boundary|state transition|precondition|exception|business error code|asynchronous?|async|message|event|final business result|side effect|idempotency|retry|concurrency|compensation|cleanup|recovery",
    re.I,
)


def _marker(pattern: str, *, token: bool = False) -> re.Pattern[str]:
    tail = r"([^\s,，]+)" if token else r"(.+?)\s*$"
    return re.compile(rf"(?im)^\s*(?:[-*]\s*)?(?:{pattern})\s*[:：]\s*{tail}")


MARKER_RES = {name: _marker(pattern, token=name in {"rule_id", "state", "acceptance", "final", "business_code", "event", "task"}) for name, pattern in DESIGN_MARKERS.items()}


def _section_records(path: Path) -> list[tuple[str, int, str]]:
    """Split a reviewed Markdown/text document into auditable sections."""

    text = path.read_text(encoding="utf-8-sig")
    headings = list(HEADING_RE.finditer(text))
    if headings:
        return [
            (
                match.group(2).strip(),
                text[:match.start()].count("\n") + 1,
                text[match.start():(headings[index + 1].start() if index + 1 < len(headings) else len(text))].strip(),
            )
            for index, match in enumerate(headings)
        ]
    return [(path.stem, 1, text.strip())] if text.strip() else []


def _values(name: str, body: str) -> list[str]:
    return [match.group(1).strip() for match in MARKER_RES[name].finditer(body)]


def parse_design_documents(files: Iterable[Path]) -> dict[str, Any]:
    """Extract only explicit reviewed business rules from design documents."""

    documents: list[dict[str, Any]] = []
    rules: list[dict[str, Any]] = []
    errors: list[str] = []
    for path in files:
        path = path.resolve()
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8-sig")
            sections = _section_records(path)
        except (OSError, UnicodeError) as exc:
            errors.append(f"cannot read design document {path}: {exc}")
            continue
        version = re.search(r"(?im)^\s*(?:version|版本)\s*[:：]\s*([^\s]+)", text)
        documents.append({
            "path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
            "version": version.group(1) if version else None, "sections": len(sections),
        })
        for title, line, body in sections:
            method_matches = list(METHOD_PATH_RE.finditer(body))
            method_match = method_matches[0] if method_matches else None
            events = _values("event", body)
            tasks = _values("task", body)
            if not SEMANTIC_RE.search(body) and not method_match and not events and not tasks:
                continue
            participants = [
                value.strip()
                for item in _values("participant", body)
                for value in re.split(r"[,，、;；]", item)
                if value.strip()
            ]
            rule_ids = _values("rule_id", body)
            rule_id = rule_ids[0] if rule_ids else "DESIGN-" + hashlib.sha256(f"{path}:{line}:{title}".encode()).hexdigest()[:12]
            method = method_match.group(1).upper() if method_match else None
            route = method_match.group(2).strip("`") if method_match else None
            calls = [
                {"kind": "http", "method": match.group(1).upper(), "path": match.group(2).strip("`")}
                for match in method_matches
            ]
            calls.extend({"kind": "message", "event": event} for event in events)
            calls.extend({"kind": "task", "task": task} for task in tasks)
            cross_service = len(set(participants)) >= 2 or bool(re.search(r"跨服务|下游|消息|事件|异步|cross[- ]service", body, re.I))
            assertions = _values("assertion", body)
            states = _values("state", body)
            codes = _values("business_code", body)
            final_statuses = _values("final", body)
            side_effects = _values("side_effect", body)
            has_business_contract = (
                len(set(participants)) >= 2
                and bool(
                    SEMANTIC_RE.search(body)
                    or calls
                    or assertions
                    or states
                    or final_statuses
                    or side_effects
                    or codes
                    or _values("branch", body)
                )
            )
            rules.append({
                "id": rule_id,
                "title": title,
                "type": "cross_service" if cross_service else "business",
                "manual_confirmation": not has_business_contract,
                "method": method,
                "path": route,
                "event": (events or [None])[0],
                "task": (tasks or [None])[0],
                "calls": calls,
                "participants": list(dict.fromkeys(participants)),
                "states": states,
                "transitions": _values("transition", body),
                "acceptance_statuses": _values("acceptance", body),
                "final_statuses": final_statuses,
                "preconditions": _values("precondition", body),
                "business_codes": codes,
                "exceptions": list(dict.fromkeys([*codes, *_values("branch", body)])),
                "branches": _values("branch", body),
                "assertions": assertions,
                "final_result": list(dict.fromkeys([*assertions, *states])),
                "side_effects": side_effects,
                "idempotency": _values("idempotency", body),
                "retries": _values("retry", body),
                "concurrency": _values("concurrency", body),
                "async_behavior": _values("async_behavior", body),
                "cleanup": _values("cleanup", body),
                "recovery": _values("recovery", body),
                "async": bool(re.search(r"异步|asynchronous|async", body, re.I)),
                "section": title,
                "line": line,
                "section_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "protocol_refs": [],
                "source": {"source_kind": "design", "file": str(path), "section": title, "line": line},
            })
    by_id: dict[str, str] = {}
    by_entry: dict[tuple[str, str, str], tuple[str, str]] = {}
    for rule in rules:
        rule_id = str(rule["id"])
        digest = str(rule["section_sha256"])
        if rule_id in by_id and by_id[rule_id] != digest:
            errors.append(f"conflicting design documents for rule ID {rule_id}")
        by_id[rule_id] = digest
        entry = (
            str(rule.get("method") or "").upper(),
            str(rule.get("path") or rule.get("event") or rule.get("task") or ""),
            "http" if rule.get("path") else "event" if rule.get("event") else "task" if rule.get("task") else "",
        )
        source_file = str(rule.get("source", {}).get("file", "")) if isinstance(rule.get("source"), dict) else ""
        if entry[1] and entry in by_entry and by_entry[entry][1] != source_file and by_entry[entry][0] != digest:
            errors.append(f"conflicting design documents for {entry[2]} {entry[0]} {entry[1]}")
        by_entry[entry] = (digest, source_file)
    if not rules:
        errors.append("design documents contain no recognized business-flow or cross-service sections")
    return {"version": 1, "source": "design", "documents": documents, "rules": rules, "errors": sorted(set(errors))}


def _load_protocol(path: Path) -> Any:
    if path.suffix.casefold() == ".json":
        return json.loads(path.read_text(encoding="utf-8-sig"))
    if path.suffix.casefold() in {".yaml", ".yml"}:
        return yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    return path.read_text(encoding="utf-8-sig")


def _resolve_ref(root: Mapping[str, Any], value: Any, seen: frozenset[str] = frozenset()) -> Any:
    """Resolve local JSON references while retaining recursive boundaries."""

    if not isinstance(value, Mapping) or not isinstance(value.get("$ref"), str):
        return value
    reference = str(value["$ref"])
    if not reference.startswith("#/") or reference in seen:
        return value
    current: Any = root
    for token in reference[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, Mapping) or token not in current:
            return value
        current = current[token]
    return _resolve_ref(root, current, seen | {reference})


def _schema_fields(schema: Any, root: Mapping[str, Any], prefix: str = "", seen: frozenset[int] = frozenset()) -> list[dict[str, Any]]:
    schema = _resolve_ref(root, schema)
    if not isinstance(schema, Mapping) or id(schema) in seen:
        return []
    # OpenAPI schemas commonly compose object fragments with allOf/oneOf. Keep
    # the contract fields visible without treating composition as business data.
    composed: list[dict[str, Any]] = []
    for key in ("allOf", "oneOf", "anyOf"):
        variants = schema.get(key)
        if isinstance(variants, list):
            for variant in variants:
                composed.extend(_schema_fields(variant, root, prefix, seen | {id(schema)}))
    required = set(schema.get("required", [])) if isinstance(schema.get("required"), list) else set()
    properties = schema.get("properties", {})
    fields: list[dict[str, Any]] = []
    for name, raw in properties.items() if isinstance(properties, Mapping) else ():
        child = _resolve_ref(root, raw)
        child = child if isinstance(child, Mapping) else {}
        field_path = f"{prefix}.{name}" if prefix else str(name)
        item = {"path": field_path, "required": name in required}
        for key in ("type", "format", "enum", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength", "minItems", "maxItems", "pattern", "nullable", "default"):
            if key in child:
                item[key] = child[key]
        fields.append(item)
        fields.extend(_schema_fields(child.get("items") if child.get("type") == "array" else child, root, field_path, seen | {id(schema)}))
    merged: list[dict[str, Any]] = []
    for field in [*composed, *fields]:
        path = field.get("path")
        existing = next((item for item in merged if item.get("path") == path), None)
        if existing is None:
            merged.append(field)
        else:
            existing.update({key: value for key, value in field.items() if key not in existing or existing[key] in (None, False)})
            existing["required"] = bool(existing.get("required") or field.get("required"))
    return merged


def _openapi_operations(document: Mapping[str, Any], path: Path) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    paths = document.get("paths", {})
    for route, path_item in paths.items() if isinstance(paths, Mapping) else ():
        path_item = _resolve_ref(document, path_item)
        if not isinstance(path_item, Mapping):
            continue
        shared_parameters = path_item.get("parameters", []) if isinstance(path_item.get("parameters"), list) else []
        for method, operation in path_item.items():
            upper = str(method).upper()
            if upper not in HTTP_METHODS or not isinstance(operation, Mapping):
                continue
            parameters = [*shared_parameters, *(operation.get("parameters", []) if isinstance(operation.get("parameters"), list) else [])]
            parameter_contracts: list[dict[str, Any]] = []
            body_schemas: list[tuple[Any, bool]] = []
            for raw_parameter in parameters:
                parameter = _resolve_ref(document, raw_parameter)
                if not isinstance(parameter, Mapping) or not parameter.get("name"):
                    continue
                schema = _resolve_ref(document, parameter.get("schema", {}))
                item = {
                    "name": str(parameter["name"]), "in": str(parameter.get("in", "")),
                    "required": bool(parameter.get("required")), "schema": schema,
                }
                parameter_contracts.append(item)
                if item["in"] == "body":
                    body_schemas.append((schema, bool(parameter.get("required"))))
            request_body = _resolve_ref(document, operation.get("requestBody", {}))
            request_content = request_body.get("content", {}) if isinstance(request_body, Mapping) else {}
            request_fields = [
                field
                for media in request_content.values() if isinstance(request_content, Mapping) and isinstance(media, Mapping)
                for field in _schema_fields(media.get("schema", {}), document)
            ]
            if not request_fields and body_schemas:
                request_fields = [
                    field for schema, _ in body_schemas for field in _schema_fields(schema, document)
                ]
            request_body_required = bool(request_body.get("required")) if isinstance(request_body, Mapping) else False
            request_body_required = request_body_required or any(required for _, required in body_schemas)
            responses: dict[str, Any] = {}
            response_fields: list[dict[str, Any]] = []
            raw_responses = operation.get("responses", {})
            for status, raw_response in raw_responses.items() if isinstance(raw_responses, Mapping) else ():
                response = _resolve_ref(document, raw_response)
                content = response.get("content", {}) if isinstance(response, Mapping) else {}
                fields = [
                    field
                    for media in content.values() if isinstance(content, Mapping) and isinstance(media, Mapping)
                    for field in _schema_fields(media.get("schema", {}), document)
                ]
                if not fields and isinstance(response, Mapping):
                    fields = _schema_fields(response.get("schema", {}), document)
                response_headers = []
                raw_headers = response.get("headers", {}) if isinstance(response, Mapping) else {}
                for header_name, raw_header in raw_headers.items() if isinstance(raw_headers, Mapping) else ():
                    header = _resolve_ref(document, raw_header)
                    if isinstance(header, Mapping):
                        schema = _resolve_ref(document, header.get("schema", {}))
                        response_headers.append({"name": str(header_name), "required": bool(header.get("required")), "schema": schema})
                responses[str(status)] = {
                    "description": response.get("description") if isinstance(response, Mapping) else None,
                    "fields": fields,
                    "headers": response_headers,
                }
                response_fields.extend({"status": str(status), **field} for field in fields)
                response_fields.extend(
                    {
                        "status": str(status),
                        "path": f"header.{header.get('name')}",
                        "required": bool(header.get("required")),
                        **({key: value for key, value in header.get("schema", {}).items() if key in {"type", "format", "enum", "pattern"}} if isinstance(header.get("schema"), Mapping) else {}),
                    }
                    for header in response_headers
                )
            operations.append({
                "id": str(operation.get("operationId") or f"{upper} {route}"),
                "kind": "http", "method": upper, "path": str(route),
                "parameters": parameter_contracts,
                "request_body": {"required": request_body_required, "content": request_content},
                "request_fields": request_fields,
                "response_fields": response_fields,
                "status_codes": sorted(responses), "responses": responses,
                "source": {"source_kind": "protocol", "file": str(path), "operation": f"{upper} {route}"},
            })
    return operations


def _asyncapi_operations(document: Mapping[str, Any], path: Path) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    root_operations = document.get("operations", {})
    for operation_id, raw_operation in root_operations.items() if isinstance(root_operations, Mapping) else ():
        operation = _resolve_ref(document, raw_operation)
        if not isinstance(operation, Mapping):
            continue
        channel = _resolve_ref(document, operation.get("channel", {}))
        channel_name = str(channel.get("address") or operation_id) if isinstance(channel, Mapping) else str(operation_id)
        raw_messages = operation.get("messages", [])
        raw_message = raw_messages[0] if isinstance(raw_messages, list) and raw_messages else {}
        message = _resolve_ref(document, raw_message)
        payload = _resolve_ref(document, message.get("payload", {})) if isinstance(message, Mapping) else {}
        headers = _resolve_ref(document, message.get("headers", {})) if isinstance(message, Mapping) else {}
        event = str(message.get("name") or message.get("title") or operation_id) if isinstance(message, Mapping) else str(operation_id)
        operations.append({
            "id": str(operation_id), "kind": "message", "channel": channel_name,
            "direction": "publish" if operation.get("action") == "send" else "subscribe",
            "event": event, "message_version": message.get("messageId") or message.get("x-version") or message.get("version") if isinstance(message, Mapping) else None,
            "message_fields": _schema_fields(payload, document), "header_fields": _schema_fields(headers, document),
            "message_schema": payload,
            "source": {"source_kind": "protocol", "file": str(path), "operation": str(operation_id)},
        })
    if operations:
        return operations
    channels = document.get("channels", {})
    for channel, item in channels.items() if isinstance(channels, Mapping) else ():
        if not isinstance(item, Mapping):
            continue
        for direction in ("publish", "subscribe"):
            operation = item.get(direction)
            if not isinstance(operation, Mapping):
                continue
            message = _resolve_ref(document, operation.get("message", {}))
            event = str(message.get("name") or operation.get("operationId") or channel) if isinstance(message, Mapping) else str(channel)
            payload = _resolve_ref(document, message.get("payload", {})) if isinstance(message, Mapping) else {}
            headers = _resolve_ref(document, message.get("headers", {})) if isinstance(message, Mapping) else {}
            operations.append({
                "id": str(operation.get("operationId") or f"{direction}:{channel}"),
                "kind": "message", "channel": str(channel), "direction": direction, "event": event,
                "message_version": message.get("messageId") or message.get("x-version") or message.get("version") if isinstance(message, Mapping) else None,
                "message_fields": _schema_fields(payload, document), "header_fields": _schema_fields(headers, document),
                "message_schema": payload,
                "source": {"source_kind": "protocol", "file": str(path), "operation": f"{direction}:{channel}"},
            })
    return operations


def _proto_operations(text: str, path: Path) -> list[dict[str, Any]]:
    messages: dict[str, list[dict[str, Any]]] = {}
    for match in re.finditer(r"(?s)\bmessage\s+(\w+)\s*\{(.*?)\}", text):
        messages[match.group(1)] = [
            {"name": field.group(3), "type": field.group(2), "repeated": field.group(1) == "repeated", "required": field.group(1) == "required", "number": int(field.group(4))}
            for field in re.finditer(r"(?m)^\s*(?:(repeated|required|optional)\s+)?([\w.<>]+)\s+(\w+)\s*=\s*(\d+)\s*;", match.group(2))
        ]
    operations: list[dict[str, Any]] = []
    for service in re.finditer(r"(?s)\bservice\s+(\w+)\s*\{(.*?)\}", text):
        for rpc in re.finditer(r"\brpc\s+(\w+)\s*\(\s*(?:stream\s+)?([\w.]+)\s*\)\s*returns\s*\(\s*(?:stream\s+)?([\w.]+)\s*\)", service.group(2)):
            operations.append({
                "id": f"{service.group(1)}.{rpc.group(1)}", "kind": "rpc", "service": service.group(1),
                "method": rpc.group(1), "request_type": rpc.group(2), "response_type": rpc.group(3),
                "request_fields": messages.get(rpc.group(2).split(".")[-1], []),
                "response_fields": messages.get(rpc.group(3).split(".")[-1], []),
                "source": {"source_kind": "protocol", "file": str(path), "operation": rpc.group(1)},
            })
    return operations


def _graphql_operations(text: str, path: Path) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    type_fields = {
        block.group(1): [
            {"path": field.group(1), "name": field.group(1), "type": field.group(2), "required": field.group(2).endswith("!")}
            for field in re.finditer(r"(?m)^\s*(\w+)\s*(?:\([^)]*\))?\s*:\s*([\[\]!\w]+)", block.group(2))
        ]
        for block in re.finditer(r"(?s)\b(?:type|input)\s+(\w+)\s*\{(.*?)\}", text)
    }
    for block in re.finditer(r"(?s)\btype\s+(Query|Mutation|Subscription)\s*\{(.*?)\}", text):
        for field in re.finditer(r"(?m)^\s*(\w+)\s*(?:\(([^)]*)\))?\s*:\s*([\[\]!\w]+)", block.group(2)):
            arguments = [
                {"path": arg.group(1), "name": arg.group(1), "type": arg.group(2), "required": arg.group(2).endswith("!")}
                for arg in re.finditer(r"(\w+)\s*:\s*([\[\]!\w]+)", field.group(2) or "")
            ]
            operations.append({
                "id": field.group(1), "kind": "graphql", "operation_type": block.group(1).casefold(),
                "method": field.group(1), "arguments": arguments, "response_type": field.group(3),
                "request_fields": arguments,
                "response_fields": type_fields.get(re.sub(r"[\[\]!]", "", field.group(3)), []),
                "source": {"source_kind": "protocol", "file": str(path), "operation": field.group(1)},
            })
    return operations


def parse_protocol_documents(files: Iterable[Path]) -> dict[str, Any]:
    """Extract formal transport contracts without assigning business outcomes."""

    documents: list[dict[str, Any]] = []
    operations: list[dict[str, Any]] = []
    errors: list[str] = []
    for path in files:
        path = path.resolve()
        try:
            raw = path.read_bytes()
            document = _load_protocol(path)
        except (OSError, UnicodeError, ValueError, yaml.YAMLError, json.JSONDecodeError) as exc:
            errors.append(f"cannot read protocol {path}: {exc}")
            continue
        version = None
        if isinstance(document, Mapping):
            version = document.get("openapi") or document.get("asyncapi") or document.get("swagger")
        documents.append({"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "version": str(version) if version else None})
        if isinstance(document, Mapping) and isinstance(document.get("paths"), Mapping):
            operations.extend(_openapi_operations(document, path))
        elif isinstance(document, Mapping) and isinstance(document.get("channels"), Mapping):
            operations.extend(_asyncapi_operations(document, path))
        elif isinstance(document, str) and path.suffix.casefold() == ".proto":
            operations.extend(_proto_operations(document, path))
        elif isinstance(document, str) and path.suffix.casefold() in {".graphql", ".graphqls"}:
            operations.extend(_graphql_operations(document, path))
        else:
            errors.append(f"formal protocol document has unsupported shape: {path}")
    ids: dict[str, str] = {}
    for operation in operations:
        identity = json.dumps({key: operation.get(key) for key in ("kind", "method", "path", "event", "channel", "service")}, sort_keys=True)
        if operation["id"] in ids and ids[operation["id"]] != identity:
            errors.append(f"conflicting formal protocol operation ID: {operation['id']}")
        ids[str(operation["id"])] = identity
    if not operations:
        errors.append("formal protocol documents contain no HTTP, RPC, message, task, or GraphQL operations")
    return {"version": 1, "source": "protocol", "documents": documents, "operations": operations, "errors": sorted(set(errors))}


_EXPECTATION_FIELD_RE = re.compile(
    r"^\s*(?:response\.)?([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*|\[\d+\])*)\s*(?:==|=|:|\bis\b)\s*(.*?)\s*$",
    re.I,
)
_PLACEHOLDER_VALUE_RE = re.compile(r"^\$\{[^{}]+\}$")


def _required_protocol_inputs(operation: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Return required input paths and their protocol scope."""

    required: list[tuple[str, str]] = []
    for field in operation.get("request_fields", []) if isinstance(operation.get("request_fields"), list) else []:
        if not isinstance(field, Mapping) or field.get("required") is not True:
            continue
        name = field.get("path") or field.get("name")
        if name:
            required.append((str(name), "request"))
    for field in operation.get("parameters", []) if isinstance(operation.get("parameters"), list) else []:
        if isinstance(field, Mapping) and field.get("required") is True and field.get("name"):
            required.append((str(field["name"]), str(field.get("in") or "parameter")))
    for field in operation.get("arguments", []) if isinstance(operation.get("arguments"), list) else []:
        if isinstance(field, Mapping) and field.get("required") is True and (field.get("path") or field.get("name")):
            required.append((str(field.get("path") or field.get("name")), "argument"))
    for field in operation.get("message_fields", []) if isinstance(operation.get("message_fields"), list) else []:
        if isinstance(field, Mapping) and field.get("required") is True and (field.get("path") or field.get("name")):
            required.append((str(field.get("path") or field.get("name")), "message"))
    for field in operation.get("header_fields", []) if isinstance(operation.get("header_fields"), list) else []:
        if isinstance(field, Mapping) and field.get("required") is True and (field.get("path") or field.get("name")):
            required.append((str(field.get("path") or field.get("name")), "header"))
    if isinstance(operation.get("request_body"), Mapping) and operation["request_body"].get("required") and not required:
        required.append(("$body", "request"))
    return list(dict.fromkeys(required))


def _lookup_value(value: Any, path: str, scope: str = "request") -> tuple[bool, Any]:
    """Resolve a dotted protocol field from a scenario data subtree."""

    if path == "$body":
        return isinstance(value, (Mapping, list)), value
    if scope == "header" and path.startswith("header."):
        path = path.removeprefix("header.")
    elif scope == "message" and path.startswith("payload."):
        path = path.removeprefix("payload.")
    candidates = [value]
    if isinstance(value, Mapping):
        if scope == "message" and isinstance(value.get("payload"), (Mapping, list)):
            candidates.insert(0, value["payload"])
        if scope == "header" and isinstance(value.get("headers"), (Mapping, list)):
            candidates.insert(0, value["headers"])
        for key in ("payload", "body", "request", "headers", "query", "path", "parameters"):
            if isinstance(value.get(key), (Mapping, list)) and value[key] not in candidates:
                candidates.append(value[key])
    tokens = re.findall(r"[^.\[\]]+|\[\d+\]", path)
    for candidate in candidates:
        current = candidate
        try:
            for token in tokens:
                if token.startswith("[") and token.endswith("]"):
                    current = current[int(token[1:-1])]
                elif isinstance(current, Mapping):
                    current = current[token]
                else:
                    raise (KeyError(token))
            return True, current
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return False, None


def _expectation_field(value: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, str):
        return None, None
    match = _EXPECTATION_FIELD_RE.match(value)
    if not match:
        return None, None
    field, expected = match.groups()
    return field, expected.strip().strip("'\"")


def _protocol_field_errors(path: Path, step: Mapping[str, Any], operation: Mapping[str, Any], data: Any) -> list[str]:
    """Validate scenario input literals and response assertions against a formal contract."""

    errors: list[str] = []
    required = _required_protocol_inputs(operation)
    if required and "data_ref" not in step:
        errors.append(_error(path, "protocol-data-ref-required", f"协议步骤 {step.get('id')} 缺少用于构造必填输入的 data_ref"))
        return errors
    if required and not isinstance(data, (Mapping, list)):
        errors.append(_error(path, "protocol-data-shape", f"协议步骤 {step.get('id')} 的 data_ref 必须解析为对象或数组"))
        return errors
    request_fields = [
        field for field in operation.get("request_fields", [])
        if isinstance(field, Mapping)
    ] if isinstance(operation.get("request_fields"), list) else []
    field_by_name = {
        str(field.get("path") or field.get("name")): field
        for field in request_fields if field.get("path") or field.get("name")
    }
    for field in operation.get("parameters", []) if isinstance(operation.get("parameters"), list) else []:
        if not isinstance(field, Mapping) or not field.get("name"):
            continue
        schema = field.get("schema")
        field_by_name.setdefault(str(field["name"]), schema if isinstance(schema, Mapping) else {})
    for name, scope in required:
        if name == "$body":
            continue
        found, current = _lookup_value(data, name, scope)
        if not found:
            errors.append(_error(path, "protocol-required-field", f"协议步骤 {step.get('id')} 缺少正式协议必填字段: {name}"))
            continue
        field = field_by_name.get(name)
        if not isinstance(field, Mapping) or (isinstance(current, str) and _PLACEHOLDER_VALUE_RE.fullmatch(current)):
            continue
        if "enum" in field and isinstance(field.get("enum"), list) and current not in field["enum"]:
            errors.append(_error(path, "protocol-field-constraint", f"协议字段 {name} 不满足 enum 约束: {current!r}"))
        if isinstance(current, (int, float)) and not isinstance(current, bool):
            for key, comparator in (("minimum", lambda a, b: a < b), ("maximum", lambda a, b: a > b), ("exclusiveMinimum", lambda a, b: a <= b), ("exclusiveMaximum", lambda a, b: a >= b)):
                if key in field and comparator(current, field[key]):
                    errors.append(_error(path, "protocol-field-constraint", f"协议字段 {name} 不满足 {key} 约束"))
        if isinstance(current, (str, list)):
            for key, comparator in (("minLength", lambda a, b: a < b), ("maxLength", lambda a, b: a > b), ("minItems", lambda a, b: a < b), ("maxItems", lambda a, b: a > b)):
                if key in field and comparator(len(current), field[key]):
                    errors.append(_error(path, "protocol-field-constraint", f"协议字段 {name} 不满足 {key} 约束"))
        if "pattern" in field and isinstance(current, str):
            try:
                matched = re.search(str(field["pattern"]), current) is not None
            except re.error:
                matched = False
            if not matched:
                errors.append(_error(path, "protocol-field-constraint", f"协议字段 {name} 不满足 pattern 约束"))

    response_fields = [
        field for field in operation.get("response_fields", [])
        if isinstance(field, Mapping) and (field.get("path") or field.get("name"))
    ] if isinstance(operation.get("response_fields"), list) else []
    response_paths = {str(field.get("path") or field.get("name")) for field in response_fields}
    for expectation in step.get("expect", []) if isinstance(step.get("expect"), list) else []:
        field_name, expected = _expectation_field(expectation)
        if not field_name or not response_paths:
            continue
        if field_name not in response_paths and not any(path.endswith("." + field_name) for path in response_paths):
            errors.append(_error(path, "protocol-response-field", f"步骤 {step.get('id')} 的响应断言字段不在正式协议中: {field_name}"))
            continue
        matched_field = next((field for field in response_fields if str(field.get("path") or field.get("name")) in {field_name, *[candidate for candidate in response_paths if candidate.endswith("." + field_name)]}), None)
        if isinstance(matched_field, Mapping) and isinstance(matched_field.get("enum"), list) and expected and expected not in {str(item) for item in matched_field["enum"]}:
            errors.append(_error(path, "protocol-response-constraint", f"响应断言 {field_name} 不满足正式协议 enum 约束: {expected!r}"))
    return errors


def _approved_exclusion(item: Mapping[str, Any]) -> bool:
    return item.get("approved") is True or str(item.get("status", "")).casefold() == "approved"


def _excluded(rule: Mapping[str, Any], exclusions: Iterable[dict[str, Any]]) -> bool:
    for item in exclusions:
        if not _approved_exclusion(item):
            continue
        if str(item.get("rule_id") or item.get("design_rule_id") or "") == str(rule.get("id")):
            return True
        if rule.get("path") and str(item.get("method", "")).upper() == str(rule.get("method", "")).upper() and item.get("path") == rule.get("path"):
            return True
        if rule.get("event") and item.get("event") == rule.get("event"):
            return True
        if rule.get("task") and item.get("task") == rule.get("task"):
            return True
    return False


def map_design_to_protocol(design: dict[str, Any], protocols: dict[str, Any], exclusions: Iterable[dict[str, Any]] = ()) -> list[str]:
    """Map each design-declared call to one formal operation; unused APIs are allowed."""

    errors = [str(value) for value in design.get("errors", [])] + [str(value) for value in protocols.get("errors", [])]
    operations = [item for item in protocols.get("operations", []) if isinstance(item, dict)]
    for rule in (item for item in design.get("rules", []) if isinstance(item, dict)):
        if rule.get("async") and (not rule.get("acceptance_statuses") or not rule.get("final_statuses")):
            errors.append(f"async design rule {rule.get('id')} must declare acceptance and final status")
        calls = rule.get("calls", []) if isinstance(rule.get("calls"), list) else []
        if not calls and rule.get("path"):
            calls = [{"kind": "http", "method": rule.get("method"), "path": rule.get("path")}]
        elif not calls and rule.get("event"):
            calls = [{"kind": "message", "event": rule.get("event")}]
        elif not calls and rule.get("task"):
            calls = [{"kind": "task", "task": rule.get("task")}]
        candidates: list[dict[str, Any]] = []
        for call in calls:
            if not isinstance(call, dict):
                continue
            if call.get("kind") == "http":
                matches = [
                    operation for operation in operations
                    if operation.get("kind") == "http"
                    and str(operation.get("method", "")).upper() == str(call.get("method", "")).upper()
                    and operation.get("path") == call.get("path")
                ]
            elif call.get("kind") == "message":
                matches = [
                    operation for operation in operations
                    if operation.get("kind") == "message"
                    and str(call.get("event")) in {str(operation.get("event")), str(operation.get("channel")), str(operation.get("id"))}
                ]
            else:
                matches = [
                    operation for operation in operations
                    if str(call.get("task")) in {str(operation.get("id")), str(operation.get("method"))}
                ]
            if not matches and not _excluded({**rule, **call}, exclusions):
                label = f"{call.get('method') or call.get('kind') or ''} {call.get('path') or call.get('event') or call.get('task')}".strip()
                errors.append(f"contract drift: design rule {rule.get('id')} call {label} is absent from formal protocol")
            if len(matches) > 1:
                errors.append(f"ambiguous formal protocol mapping for design rule {rule.get('id')} call {call}: {[item.get('id') for item in matches]}")
            candidates.extend(matches)
        if not calls:
            normalized_rule = re.sub(r"[^a-z0-9]", "", str(rule.get("id", "")).casefold())
            candidates = [
                operation for operation in operations
                if re.sub(r"[^a-z0-9]", "", str(operation.get("id", "")).casefold()) == normalized_rule
            ]
        rule["protocol_refs"] = list(dict.fromkeys(str(operation["id"]) for operation in candidates))
        declares_call = rule.get("type") == "cross_service" or bool(calls)
        if declares_call and not calls and not candidates and not _excluded(rule, exclusions):
            label = f"{rule.get('method') or ''} {rule.get('path') or rule.get('event') or rule.get('task')}".strip()
            errors.append(f"contract drift: design rule {rule.get('id')} call {label} is absent from formal protocol")
    return sorted(set(errors))


def _read_exclusions(project_root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    result: list[dict[str, Any]] = []
    errors: list[str] = []
    for path in (project_root / "exclusions.yaml", project_root / "discovery" / "exclusions.yaml"):
        if not path.is_file():
            continue
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            errors.append(f"cannot parse exclusions {path}: {exc}")
            continue
        values = document.get("exclusions") if isinstance(document, dict) else None
        if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
            errors.append(f"exclusions must be a list of mappings: {path}")
            continue
        for item in values:
            if not _approved_exclusion(item):
                errors.append(f"exclusion is not user-approved: {item}")
                continue
            if not any(item.get(key) for key in ("rule_id", "design_rule_id", "path", "event", "task", "operation_id")):
                errors.append(f"approved exclusion has no exact scope: {item}")
            for key in ("reason", "manual_followup"):
                if not str(item.get(key, "")).strip():
                    errors.append(f"approved exclusion requires {key}: {item}")
            if not str(item.get("impact") or item.get("affected_design") or "").strip():
                errors.append(f"approved exclusion requires affected design/protocol scope: {item}")
            result.append(item)
    return result, errors


def _workspace_participant_errors(project_root: Path, design: Mapping[str, Any]) -> list[str]:
    if not any(isinstance(rule, dict) for rule in design.get("rules", [])):
        return []
    workspace_path = project_root / "discovery" / "workspace.yaml"
    try:
        workspace = yaml.safe_load(workspace_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return ["discovery/workspace.yaml is required to map design participants to service topology"]
    if not isinstance(workspace, dict):
        return ["discovery/workspace.yaml is invalid"]
    configuration = workspace.get("configuration", {})
    services = configuration.get("services", []) if isinstance(configuration, dict) else []
    components = [
        *(configuration.get("data_sources", []) if isinstance(configuration, dict) and isinstance(configuration.get("data_sources"), list) else []),
        *(configuration.get("middleware", []) if isinstance(configuration, dict) and isinstance(configuration.get("middleware"), list) else []),
        *(configuration.get("controls", []) if isinstance(configuration, dict) and isinstance(configuration.get("controls"), list) else []),
    ]
    nodes = workspace.get("topology", {}).get("nodes", [])
    known = {
        str(item.get("id")).casefold()
        for item in [*(services if isinstance(services, list) else []), *components, *(nodes if isinstance(nodes, list) else [])]
        if isinstance(item, dict) and item.get("id")
    }
    known.update(value.rsplit(":", 1)[-1] for value in list(known))
    errors: list[str] = []
    for rule in design.get("rules", []):
        if not isinstance(rule, dict):
            continue
        missing = [participant for participant in rule.get("participants", []) if str(participant).casefold() not in known]
        if missing:
            errors.append(f"design participants are absent from discovered topology for {rule.get('id')}: {missing}")
    return errors


def _logic(design: Mapping[str, Any]) -> dict[str, Any]:
    logic = []
    for rule in design.get("rules", []):
        if not isinstance(rule, dict):
            continue
        logic.append({
            "id": f"LOGIC_{rule.get('id')}", "source": "design", "design_rule_id": rule.get("id"),
            "title": rule.get("title"), "participants": rule.get("participants", []),
            "protocol_refs": rule.get("protocol_refs", []), "preconditions": rule.get("preconditions", []),
            "states": rule.get("states", []), "transitions": rule.get("transitions", []),
            "branches": rule.get("branches", []), "exceptions": rule.get("exceptions", []),
            "assertions": rule.get("assertions", []), "final_result": rule.get("final_result", []),
            "side_effects": rule.get("side_effects", []), "idempotency": rule.get("idempotency", []),
            "retries": rule.get("retries", []), "concurrency": rule.get("concurrency", []),
            "async_behavior": rule.get("async_behavior", []), "async": rule.get("async") is True,
            "acceptance_status": (rule.get("acceptance_statuses") or [None])[0],
            "final_status": (rule.get("final_statuses") or [None])[0],
            "evidence": [rule.get("source")],
        })
    return {"version": 1, "source": "design", "logic": logic}


def _scenario_plan(design: Mapping[str, Any], exclusions: Iterable[dict[str, Any]]) -> dict[str, Any]:
    excluded_ids = {
        str(item.get("rule_id") or item.get("design_rule_id"))
        for item in exclusions if item.get("rule_id") or item.get("design_rule_id")
    }
    return {
        "version": 1,
        "source": "design",
        "scenarios": [
            {
                "id": str(rule.get("id")), "title": rule.get("title"), "participants": rule.get("participants", []),
                "design_rule_ids": [str(rule.get("id"))], "protocol_refs": rule.get("protocol_refs", []),
                "required_coverage": {
                    key: rule.get(key, []) for key in ("preconditions", "transitions", "branches", "exceptions", "final_result", "side_effects")
                },
            }
            for rule in design.get("rules", []) if isinstance(rule, dict) and str(rule.get("id")) not in excluded_ids
        ],
    }


def _render_yaml(purpose: str, document: dict[str, Any]) -> str:
    rendered = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    blocks = set(document)
    lines: list[str] = []
    for line in rendered.splitlines():
        key = line.split(":", 1)[0] if line and not line.startswith((" ", "-")) and ":" in line else ""
        if key in blocks:
            lines.append(f"# 区块：{key}；内容只能来自人工审查或执行准备来源。")
        lines.append(line)
    return f"# 用途：{purpose}；不得保存凭据、令牌或运行时敏感值。\n" + "\n".join(lines) + "\n"


def _write_artifacts(documents: Mapping[Path, str]) -> None:
    temporary: list[tuple[Path, Path]] = []
    try:
        for path, content in documents.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
                stream.write(content)
                temporary.append((Path(stream.name), path))
        for source, target in temporary:
            source.replace(target)
    finally:
        for source, _ in temporary:
            source.unlink(missing_ok=True)


def generate_artifacts(
    project_root: Path,
    *,
    design_roots: Iterable[Path] = (),
    design_files: Iterable[Path] = (),
    openapi_roots: Iterable[Path] = (),
    openapi_files: Iterable[Path] = (),
) -> tuple[dict[str, Any], list[str]]:
    """Validate every authority input, then atomically write generation artifacts."""

    project_root = project_root.resolve()
    design_discovery = discover_documents(project_root, roots=design_roots, files=design_files)
    protocol_discovery = discover_protocols(project_root, roots=openapi_roots, files=openapi_files)
    errors: list[str] = []
    if not design_discovery.files:
        candidates = ", ".join(map(str, design_discovery.candidates)) or "none"
        errors.append(f"design documents are required; provide --design-root/--design-file (candidates: {candidates})")
    if not protocol_discovery.files:
        candidates = ", ".join(map(str, protocol_discovery.candidates)) or "none"
        errors.append(f"formal protocol documents are required; provide --openapi-root/--openapi-file (candidates: {candidates})")
    if not (design_roots or design_files) and len(design_discovery.candidates) > 1:
        errors.append("multiple design roots found; choose one with --design-root/--design-file: " + ", ".join(map(str, design_discovery.candidates)))
    if not (openapi_roots or openapi_files) and len(protocol_discovery.candidates) > 1:
        errors.append("multiple protocol roots found; choose one with --openapi-root/--openapi-file: " + ", ".join(map(str, protocol_discovery.candidates)))
    design = parse_design_documents(design_discovery.files) if design_discovery.files else {"version": 1, "source": "design", "documents": [], "rules": [], "errors": []}
    protocols = parse_protocol_documents(protocol_discovery.files) if protocol_discovery.files else {"version": 1, "source": "protocol", "documents": [], "operations": [], "errors": []}
    exclusions, exclusion_errors = _read_exclusions(project_root)
    errors.extend(exclusion_errors)
    errors.extend(map_design_to_protocol(design, protocols, exclusions))
    errors.extend(_workspace_participant_errors(project_root, design))
    manual = [str(rule.get("id")) for rule in design.get("rules", []) if isinstance(rule, dict) and rule.get("manual_confirmation")]
    if manual:
        errors.append("design rules require manual_confirmation: " + ", ".join(manual))

    value_path = project_root / "config" / "value-resolution.yaml"
    if value_path.is_file():
        try:
            value_resolution = yaml.safe_load(value_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            value_resolution = None
            errors.append(f"cannot parse value-resolution.yaml: {exc}")
        if not isinstance(value_resolution, dict) or value_resolution.get("source") != "support-only" or not isinstance(value_resolution.get("values"), list):
            errors.append("value-resolution.yaml must be a support-only mapping with a values list")
        elif value_resolution.get("business_expectations"):
            errors.append("value-resolution.yaml must not define business expectations")
    else:
        value_resolution = {"version": 1, "source": "support-only", "values": [], "business_expectations": []}

    discovery_dir = project_root / "discovery"
    lock_path = discovery_dir / "version-lock.yaml"
    try:
        previous = yaml.safe_load(lock_path.read_text(encoding="utf-8")) if lock_path.is_file() else None
    except (OSError, UnicodeError, yaml.YAMLError):
        previous = None
    logic = _logic(design)
    plan = _scenario_plan(design, exclusions)
    result = {
        "design": {"files": [str(path) for path in design_discovery.files], "candidates": [str(path) for path in design_discovery.candidates], "summary": input_summary(design)},
        "protocol": {"files": [str(path) for path in protocol_discovery.files], "candidates": [str(path) for path in protocol_discovery.candidates], "summary": input_summary(protocols)},
        "coverage": {"design_rules": len(design.get("rules", [])), "protocol_operations": len(protocols.get("operations", [])), "planned_scenarios": len(plan["scenarios"]), "manual_confirmation": manual, "errors": sorted(set(errors))},
        "artifacts": [],
    }
    if errors:
        return result, sorted(set(errors))

    lock = build_generation_lock(project_root, design, protocols, value_resolution=value_resolution, previous=previous)
    documents = {
        discovery_dir / "design-rules.yaml": _render_yaml("保存人工审查设计文档提取的业务规则；规则来源只能是 design", design),
        discovery_dir / "protocol-rules.yaml": _render_yaml("保存正式协议提取的调用结构；协议不定义业务预期", protocols),
        discovery_dir / "logic.yaml": _render_yaml("保存由 design rule 生成的业务逻辑；不得引用 source 或 observed", logic),
        discovery_dir / "scenario-plan.yaml": _render_yaml("保存设计规则到待生成场景和正式协议调用的双向覆盖计划", plan),
        value_path: _render_yaml("保存执行准备阶段的配置、Fixture 和 support-only 来源", value_resolution),
        lock_path: _render_yaml("锁定设计、正式协议、源码支持版本和场景数据/清理摘要", lock),
    }
    if exclusions:
        documents[discovery_dir / "exclusions.yaml"] = _render_yaml("保存经用户确认的排除入口、原因、影响范围和人工补测要求", {"exclusions": exclusions})
    _write_artifacts(documents)
    for legacy in (project_root / "source-rules.yaml", discovery_dir / "source-rules.yaml"):
        legacy.unlink(missing_ok=True)
    result["artifacts"] = [str(path) for path in documents]
    return result, []


def _generation_artifact_errors(project_root: Path, definition: Mapping[str, Any]) -> list[str]:
    """Validate design/protocol provenance without reading application source."""

    errors: list[str] = []
    discovery_root = project_root / "discovery"
    design_path = discovery_root / "design-rules.yaml"
    protocol_path = discovery_root / "protocol-rules.yaml"
    logic_paths = [discovery_root / "logic.yaml"]
    scenarios_root = project_root / "scenarios"
    if scenarios_root.is_dir():
        logic_paths.extend(path / "logic.yaml" for path in scenarios_root.iterdir() if path.is_dir())
    if (project_root / "source-rules.yaml").is_file() or (discovery_root / "source-rules.yaml").is_file():
        errors.append(_error(discovery_root / "source-rules.yaml", "source-rules-retired", "source-rules.yaml 已停用，业务规则必须来自设计文档"))
    required_artifacts = (
        design_path,
        protocol_path,
        discovery_root / "logic.yaml",
        discovery_root / "scenario-plan.yaml",
        discovery_root / "version-lock.yaml",
        project_root / "config" / "value-resolution.yaml",
    )
    for path in required_artifacts:
        if not path.is_file():
            errors.append(_error(path, "generation-artifact-required", "缺少设计权威生成产物；必须先运行 dev-ai e2e generate"))

    def load(path: Path) -> Any:
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            errors.append(_error(path, "generation-artifact-parse", f"生成依据产物无法解析: {exc}"))
            return None

    design = load(design_path) if design_path.is_file() else {}
    protocol = load(protocol_path) if protocol_path.is_file() else {}
    schema_artifacts = {
        "e2e.design-rules": design_path,
        "e2e.protocol-rules": protocol_path,
        "e2e.logic": discovery_root / "logic.yaml",
        "e2e.scenario-plan": discovery_root / "scenario-plan.yaml",
        "e2e.value-resolution": project_root / "config" / "value-resolution.yaml",
        "e2e.version-lock": discovery_root / "version-lock.yaml",
    }
    for scope, artifact_path in schema_artifacts.items():
        if not artifact_path.is_file():
            continue
        document = load(artifact_path)
        if document is None:
            continue
        for message in validate_schema(get_schema(scope)["document"], document):
            errors.append(_error(artifact_path, "artifact-schema", f"{scope} schema violation: {message}"))
    design_rules = design.get("rules", []) if isinstance(design, dict) else []
    rule_ids = {str(item.get("id")) for item in design_rules if isinstance(item, dict) and item.get("id")}
    for source_name, artifact_path, document in (
        ("design", design_path, design),
        ("protocol", protocol_path, protocol),
    ):
        if isinstance(document, dict) and isinstance(document.get("errors"), list):
            for message in document["errors"]:
                errors.append(_error(artifact_path, f"{source_name}-discovery", str(message)))
    if design_path.is_file() and not design_rules:
        errors.append(_error(design_path, "design-rules-required", "设计产物必须包含至少一条业务规则"))
    manual_rules = [
        str(item.get("id"))
        for item in design_rules
        if isinstance(item, dict) and item.get("manual_confirmation")
    ]
    if manual_rules:
        errors.append(_error(design_path, "design-manual-confirmation", f"设计规则需要人工确认后才能生成场景: {manual_rules}"))
    if design_path.is_file() and (not isinstance(design, dict) or design.get("source") != "design"):
        errors.append(_error(design_path, "design-source", "design-rules.yaml 的 source 必须是 design"))
    if protocol_path.is_file() and (not isinstance(protocol, dict) or protocol.get("source") != "protocol"):
        errors.append(_error(protocol_path, "protocol-source", "protocol-rules.yaml 的 source 必须是 protocol"))
    protocol_operations = protocol.get("operations", []) if isinstance(protocol, dict) else []
    operation_ids = {str(item.get("id")) for item in protocol_operations if isinstance(item, dict) and item.get("id")}
    for operation in protocol_operations if isinstance(protocol_operations, list) else []:
        if not isinstance(operation, Mapping):
            continue
        kind = str(operation.get("kind"))
        required_by_kind = {
            "http": ("method", "path", "status_codes", "responses"),
            "message": ("channel", "direction", "event", "message_fields", "header_fields"),
            "rpc": ("service", "method", "request_type", "response_type", "request_fields", "response_fields"),
            "graphql": ("operation_type", "method", "arguments", "response_fields"),
            "task": ("method",),
        }.get(kind, ())
        missing = [field for field in required_by_kind if field not in operation]
        if missing:
            errors.append(_error(protocol_path, "protocol-operation-contract", f"formal protocol operation {operation.get('id')} is missing {kind} fields: {missing}"))
    if protocol_path.is_file() and not protocol_operations:
        errors.append(_error(protocol_path, "protocol-operations-required", "正式协议产物必须包含至少一个可调用操作"))
    plan_path = discovery_root / "scenario-plan.yaml"
    if plan_path.is_file():
        exclusions, exclusion_errors = _read_exclusions(project_root)
        errors.extend(_error(plan_path, "exclusions-invalid", message) for message in exclusion_errors)
        excluded_rule_ids = {
            str(item.get("rule_id") or item.get("design_rule_id"))
            for item in exclusions if item.get("rule_id") or item.get("design_rule_id")
        }
        plan = load(plan_path)
        planned = plan.get("scenarios", []) if isinstance(plan, dict) else []
        if not isinstance(plan, dict) or plan.get("source") != "design" or not isinstance(planned, list) or not planned:
            errors.append(_error(plan_path, "scenario-plan-schema", "scenario-plan.yaml 必须包含 design 来源的非空场景计划"))
        else:
            planned_rule_ids = {
                str(rule_id)
                for item in planned if isinstance(item, dict)
                for rule_id in (item.get("design_rule_ids") if isinstance(item.get("design_rule_ids"), list) else [])
            }
            missing_plan = sorted(rule_ids - planned_rule_ids - excluded_rule_ids)
            if missing_plan:
                errors.append(_error(plan_path, "scenario-plan-coverage", f"设计规则未进入场景计划: {missing_plan}"))
            for item in planned:
                if not isinstance(item, dict):
                    continue
                unknown_operations = set(map(str, item.get("protocol_refs", []))) - operation_ids
                if unknown_operations:
                    errors.append(_error(plan_path, "scenario-plan-protocol", f"场景计划引用未知协议操作: {sorted(unknown_operations)}"))
    for logic_path in logic_paths:
        if not logic_path.is_file():
            continue
        document = load(logic_path)
        if not isinstance(document, dict) or document.get("source") != "design":
            errors.append(_error(logic_path, "logic-source", "logic.yaml 的业务逻辑来源只能是 design"))
            continue
        values = document.get("logic")
        if not isinstance(values, list) or not values:
            errors.append(_error(logic_path, "logic-required", "logic.yaml 必须包含非空 logic 列表"))
            continue
        for item in values:
            if not isinstance(item, dict) or item.get("source") != "design" or not item.get("design_rule_id"):
                errors.append(_error(logic_path, "logic-design-trace", "每条业务逻辑必须引用 design_rule_id"))
                continue
            if str(item.get("design_rule_id")) not in rule_ids:
                errors.append(_error(logic_path, "logic-rule-reference", f"logic 引用了未知设计规则: {item.get('design_rule_id')}"))
            if item.get("source_candidate_id") or item.get("observed_rule_id"):
                errors.append(_error(logic_path, "logic-non-design-source", "logic 不得引用 source 或 observed 规则"))
            evidence = item.get("evidence", [])
            if any(isinstance(value, dict) and value.get("source_kind") != "design" for value in evidence if isinstance(evidence, list)):
                errors.append(_error(logic_path, "logic-evidence-source", "logic 证据必须来自 design"))
            if item.get("async") is True and (not str(item.get("acceptance_status", "")).strip() or not str(item.get("final_status", "")).strip()):
                errors.append(_error(logic_path, "logic-async-states", "异步逻辑必须区分接入状态和最终业务状态"))
    value_path = project_root / "config" / "value-resolution.yaml"
    if value_path.is_file():
        values = load(value_path)
        if not isinstance(values, dict) or values.get("source") != "support-only" or not isinstance(values.get("values"), list):
            errors.append(_error(value_path, "value-resolution-source", "value-resolution.yaml 必须是 support-only 且包含 values 列表"))
        if isinstance(values, dict) and values.get("business_expectations"):
            errors.append(_error(value_path, "value-resolution-business", "value-resolution.yaml 不得定义业务预期"))
    lock_path = discovery_root / "version-lock.yaml"
    if lock_path.is_file():
        lock = load(lock_path)
        errors.extend(
            _error(lock_path, "version-lock-stale", message)
            for message in generation_lock_errors(project_root, lock)
        )
    if design_path.is_file() and isinstance(definition, Mapping):
        scenario_steps = definition.get("steps", []) if isinstance(definition.get("steps"), list) else []
        meta = definition.get("meta")
        referenced_rule_ids = {
            str(step.get("design_rule_id"))
            for step in scenario_steps
            if isinstance(step, Mapping) and step.get("design_rule_id")
        }
        required_participants = {
            str(participant).strip()
            for rule in design_rules
            if isinstance(rule, Mapping) and str(rule.get("id")) in referenced_rule_ids
            for participant in (rule.get("participants") or [])
            if str(participant).strip()
        }
        declared_participants = {
            str(participant).strip()
            for participant in (meta.get("participants") if isinstance(meta, Mapping) and isinstance(meta.get("participants"), list) else [])
            if str(participant).strip()
        }
        if required_participants and not declared_participants.issuperset(required_participants):
            errors.append(_error(
                project_root / "scenarios" / str(meta.get("name", "scenario") if isinstance(meta, Mapping) else "scenario") / "场景定义.yaml",
                "scenario-participant-coverage",
                f"场景参与方必须覆盖设计规则声明的参与方: {sorted(required_participants - declared_participants)}",
                "participants:",
            ))
        generic = {"http 200", "http 201", "message published", "消息已发布", "task accepted", "任务已接收", "database row exists", "数据库存在一条记录"}
        for step in scenario_steps:
            if not isinstance(step, Mapping):
                continue
            if not step.get("design_rule_id"):
                scenario_name = meta.get("name", "scenario") if isinstance(meta, Mapping) else "scenario"
                errors.append(_error(project_root / "scenarios" / str(scenario_name) / "场景定义.yaml", "step-design-trace", f"步骤缺少 design_rule_id: {step.get('id')}"))
                continue
            elif str(step.get("design_rule_id")) not in rule_ids:
                errors.append(_error(design_path, "step-design-rule", f"步骤引用未知设计规则: {step.get('design_rule_id')}"))
            requires_protocol = str(step.get("control", "")) in {"public_api", "messages", "scheduled_jobs"}
            if requires_protocol and not step.get("protocol_ref"):
                errors.append(_error(protocol_path, "step-protocol-required", f"协议调用步骤必须引用 protocol_ref: {step.get('id')}"))
            elif step.get("protocol_ref") and str(step.get("protocol_ref")) not in operation_ids:
                errors.append(_error(protocol_path, "step-protocol-ref", f"步骤引用未知正式协议操作: {step.get('protocol_ref')}"))
            expectations = [str(value).casefold() for value in step.get("expect", [])] if isinstance(step.get("expect"), list) else []
            only_transport = all(
                value in generic or re.fullmatch(r"http\s+[1-5][0-9]{2}(?:\s+\w+)?", value)
                for value in expectations
            )
            if expectations and only_transport:
                errors.append(_error(design_path, "business-result-assertion", f"业务成功步骤不能只验证传输或接入结果: {step.get('id')}"))
            rule = next((item for item in design_rules if isinstance(item, dict) and item.get("id") == step.get("design_rule_id")), None)
            if isinstance(rule, dict):
                if step.get("protocol_ref") and str(step.get("protocol_ref")) not in {
                    str(value) for value in rule.get("protocol_refs", [])
                }:
                    errors.append(_error(protocol_path, "step-protocol-design-map", f"protocol_ref 未映射到设计规则 {step.get('design_rule_id')}: {step.get('protocol_ref')}"))
                design_terms = [
                    str(value).casefold()
                    for field in ("assertions", "states", "final_result", "side_effects", "business_codes", "exceptions")
                    for value in (rule.get(field) if isinstance(rule.get(field), list) else [rule.get(field)])
                    if value is not None
                ]
                concrete = [
                    value for value in expectations
                    if value not in generic and not re.fullmatch(r"http\s+[1-5][0-9]{2}(?:\s+\w+)?", value)
                ]
                for expectation in concrete:
                    if not any(expectation in term or term in expectation for term in design_terms if term):
                        errors.append(_error(design_path, "expectation-design-trace", f"业务预期必须可追溯到设计规则 {step.get('design_rule_id')}: {expectation}"))
            if isinstance(rule, dict) and not (
                rule.get("assertions") or rule.get("states") or rule.get("final_result")
            ):
                errors.append(_error(design_path, "design-result-required", f"design rule has no concrete result or state: {step.get('design_rule_id')}"))
        concrete_expectations = [
            str(value).casefold()
            for step in scenario_steps if isinstance(step, Mapping)
            for value in (step.get("expect") if isinstance(step.get("expect"), list) else [])
            if str(value).casefold() not in generic and not re.fullmatch(r"http\s+[1-5][0-9]{2}(?:\s+\w+)?", str(value).casefold())
        ]
        if scenario_steps and not concrete_expectations:
            errors.append(_error(design_path, "scenario-business-result", "每个场景至少需要一个设计定义的具体业务结果或状态变化断言"))
        for rule_id in sorted(referenced_rule_ids):
            rule = next((item for item in design_rules if isinstance(item, dict) and str(item.get("id")) == rule_id), None)
            if not isinstance(rule, dict):
                continue
            actual_protocol_refs = {
                str(step.get("protocol_ref"))
                for step in scenario_steps
                if isinstance(step, Mapping) and str(step.get("design_rule_id")) == rule_id and step.get("protocol_ref")
            }
            missing_protocol_refs = set(map(str, rule.get("protocol_refs", []))) - actual_protocol_refs
            if missing_protocol_refs:
                errors.append(_error(protocol_path, "design-call-coverage", f"设计规则 {rule_id} 的正式调用未映射到场景步骤: {sorted(missing_protocol_refs)}"))
            scenario_text = "\n".join(
                str(value).casefold()
                for step in scenario_steps if isinstance(step, Mapping) and str(step.get("design_rule_id")) == rule_id
                for value in [step.get("action"), step.get("status_reason"), *(step.get("expect") if isinstance(step.get("expect"), list) else [])]
                if value is not None
            )
            cleanup_values = definition.get("cleanup") if isinstance(definition.get("cleanup"), Mapping) else {}
            scenario_text = "\n".join([
                scenario_text,
                *(str(value).casefold() for value in definition.get("preconditions", []) if value is not None),
                *(str(value).casefold() for value in cleanup_values.get("actions", []) if value is not None),
                *(str(value).casefold() for value in cleanup_values.get("verifies", []) if value is not None),
            ])
            rule_steps = [
                step for step in scenario_steps
                if isinstance(step, Mapping) and str(step.get("design_rule_id")) == rule_id
            ]
            for field in (
                "preconditions", "transitions", "branches", "exceptions", "assertions", "final_result",
                "side_effects", "idempotency", "retries", "concurrency", "async_behavior", "cleanup", "recovery",
            ):
                for expected in rule.get(field, []) if isinstance(rule.get(field), list) else []:
                    if str(expected).casefold() not in scenario_text:
                        errors.append(_error(design_path, "design-element-coverage", f"设计规则 {rule_id} 的 {field} 未映射到场景步骤: {expected}"))
            for field, phase in (("acceptance_statuses", {"request", "message_acceptance"}), ("final_statuses", {"final_business"})):
                expected_values = rule.get(field, []) if isinstance(rule.get(field), list) else []
                phase_text = "\n".join(
                    str(value).casefold()
                    for step in rule_steps if step.get("phase") in phase
                    for value in [step.get("action"), step.get("status_reason"), *(step.get("expect") if isinstance(step.get("expect"), list) else [])]
                    if value is not None
                )
                for expected in expected_values:
                    if str(expected).casefold() not in phase_text:
                        errors.append(_error(design_path, "design-status-coverage", f"design rule {rule_id} {field} is not mapped to its required phase: {expected}"))
        async_rules = {str(item.get("id")) for item in design_rules if isinstance(item, dict) and item.get("async") is True}
        for rule_id in sorted(async_rules):
            phases = {str(step.get("phase")) for step in scenario_steps if isinstance(step, Mapping) and step.get("design_rule_id") == rule_id}
            required_phases = {"processing", "final_business"}
            if not phases.intersection({"request", "message_acceptance"}):
                errors.append(_error(design_path, "async-step-phases", f"async design rule {rule_id} must cover request or message acceptance"))
            if not required_phases.issubset(phases):
                errors.append(_error(design_path, "async-step-phases", f"async design rule {rule_id} must cover processing and final business phases"))
            rule = next((item for item in design_rules if isinstance(item, dict) and str(item.get("id")) == rule_id), None)
            if isinstance(rule, dict) and rule.get("side_effects") and "side_effect" not in phases:
                errors.append(_error(design_path, "async-side-effect-phase", f"async design rule {rule_id} must cover documented side effects"))
    return errors


SCENARIO_KEYS = set(SCENARIO_REQUIRED)


SCENARIO_STATUSES = set(E2E_SCENARIO_STATUSES)


GENERATION_MODES = set(E2E_GENERATION_MODES)


CONTROL_NAMES = E2E_CONTROL_NAMES


CONTROL_STATUSES = set(E2E_CONTROL_STATUSES)


STEP_STATUSES = set(E2E_STEP_STATUSES)


CANDIDATE_KINDS = set(E2E_CANDIDATE_KINDS)


CANDIDATE_STATUSES = {"usable", "unusable", "not_found"}


WRITE_CONTROLS = {
    "public_api", "test_or_admin_api", "mocks_and_faults", "dynamic_configuration",
    "scheduled_jobs", "messages", "database_control",
}


INHERENT_WRITE_CONTROLS = {
    "test_or_admin_api", "mocks_and_faults", "dynamic_configuration",
    "scheduled_jobs", "messages", "database_control",
}


ID_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")


ENV_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


PENDING_BLOCKER_RE = re.compile(r"^(config|credential|connection|business_data):[A-Za-z0-9_./#${}-]+$")


SQL_RE = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|MERGE|ALTER|DROP|TRUNCATE|REPLACE)\b", re.IGNORECASE)


URL_RE = re.compile(r"https?://", re.IGNORECASE)


def _scenario_directories(project_root: Path, selected: str | None, errors: list[str]) -> list[Path]:
    """发现直接场景目录并安全解析可选过滤器。"""

    root = project_root / "scenarios"
    if not root.is_dir():
        errors.append(_error(root, "scenarios-required", "scenarios 目录不存在"))
        return []
    directories = sorted(path for path in root.iterdir() if path.is_dir() and path.name not in SKIP_DIRS)
    if not directories:
        errors.append(_error(root, "scenario-required", "至少需要一个场景目录"))
        return []
    if selected is None:
        return directories
    if Path(selected).name != selected or selected in {".", ".."}:
        errors.append(_error(root, "scenario-selector", f"场景名不能是路径: {selected}"))
        return []
    matches = [path for path in directories if path.name == selected]
    if len(matches) != 1:
        errors.append(_error(root, "scenario-selector", f"场景不存在或不唯一: {selected}"))
    return matches


def _derived_status(readiness: Mapping[str, Any], decision: Mapping[str, Any]) -> str | None:
    """根据契约、控制、配置和数据状态推导唯一场景状态。"""

    source_contract = readiness.get("source_contract")
    safe_control = readiness.get("safe_control")
    runtime = readiness.get("runtime_configuration")
    test_data = readiness.get("test_data")
    blockers = readiness.get("blockers")
    if (source_contract == "blocked" or safe_control == "blocked") and decision.get("safe_control_path") is False:
        return "contract_blocked" if isinstance(blockers, list) and blockers and decision.get("blockers") else None
    if source_contract != "confirmed" or safe_control != "confirmed" or decision.get("safe_control_path") is not True:
        return None
    if runtime == "confirmed" and test_data == "confirmed" and blockers == []:
        return "ready"
    if runtime in {"confirmed", "missing"} and test_data in {"confirmed", "missing"} and "missing" in {runtime, test_data}:
        valid = isinstance(blockers, list) and blockers and all(
            isinstance(blocker, str) and PENDING_BLOCKER_RE.fullmatch(blocker) for blocker in blockers
        )
        return "pending_environment" if valid else None
    return None


def _control_errors(
    path: Path,
    controls: Any,
    steps: list[Any],
    source_symbols: set[str],
) -> list[str]:
    """校验完整控制矩阵及其步骤映射。"""

    errors: list[str] = []
    expected = set(CONTROL_NAMES) | {"decision"}
    if not _exact_keys(path, controls, expected, "control-matrix-schema", errors):
        return errors
    for name in CONTROL_NAMES:
        item = controls.get(name)
        required = {"status", "assessment", "evidence", "planned_use"}
        if name == "observability":
            required |= {"correlation_keys", "business_evidence", "recovery"}
        if name == "database_control":
            required |= {"safety"}
        if not isinstance(item, dict) or set(item) != required:
            errors.append(_error(path, "control-entry-schema", f"控制类别结构无效: {name}", f"{name}:"))
            continue
        status = item.get("status")
        if status not in CONTROL_STATUSES:
            errors.append(_error(path, "control-status", f"控制状态无效: {name}={status}", "status:"))
        if not isinstance(item.get("assessment"), str) or not item["assessment"].strip():
            errors.append(_error(path, "control-assessment", f"控制类别缺少评估: {name}", "assessment:"))
        evidence = item.get("evidence")
        planned = item.get("planned_use")
        if not _strings(evidence, nonempty=status != "not_applicable"):
            errors.append(_error(path, "control-evidence", f"控制证据无效: {name}", "evidence:"))
        elif any(reference not in source_symbols for reference in evidence):
            errors.append(_error(path, "control-evidence-source", f"控制证据未解析到场景源码锚点: {name}", "evidence:"))
        if not _strings(planned, nonempty=False):
            errors.append(_error(path, "control-plan", f"控制用途必须是去重字符串列表: {name}", "planned_use:"))
            planned = []
        if status != "usable" and planned:
            errors.append(_error(path, "control-plan-status", f"非 usable 控制不得计划使用: {name}", "planned_use:"))
        controlled_steps = [
            step for step in steps
            if isinstance(step, dict)
            and step.get("status", "executable") not in {"control_gap", "product_gap"}
            and (step.get("control") == name or name == "observability")
        ]
        contract_symbols = {
            str(step.get("action")) for step in controlled_steps if isinstance(step.get("action"), str)
        }
        contract_symbols.update(
            str(expectation)
            for step in controlled_steps
            for expectation in step.get("expect", [])
        )
        unmapped = set(planned) - contract_symbols
        if unmapped:
            errors.append(_error(path, "control-plan-mapping", f"控制用途未映射到步骤: {name}={sorted(unmapped)}", "planned_use:"))
        step_actions = {
            str(step.get("action")) for step in steps
            if isinstance(step, dict)
            and step.get("control") == name
            and step.get("status", "executable") not in {"control_gap", "product_gap"}
        }
        missing_plans = step_actions - set(planned)
        if missing_plans:
            errors.append(_error(path, "control-step-plan", f"每个步骤动作必须进入对应控制 planned_use: {name}={sorted(missing_plans)}", "planned_use:"))
        if name == "observability" and status == "usable":
            for field in ("correlation_keys", "business_evidence", "recovery"):
                if not _strings(item.get(field)):
                    errors.append(_error(path, "observability-proof", f"可执行场景缺少 {field}", field))
        if name == "database_control":
            safety = item.get("safety")
            safety_keys = {
                "authorization_required",
                "target_environment",
                "purpose",
                "consumer_source",
                "exact_selector",
                "expected_rows",
                "snapshot",
                "mutation",
                "trigger",
                "verification",
                "restoration",
                "restoration_verification",
            }
            if status == "usable":
                multi_operation = isinstance(safety, dict) and "operations" in safety
                required_safety_keys = {"authorization_required", "target_environment", "purpose", "trigger", "verification", "operations"} if multi_operation else safety_keys
                if not isinstance(safety, dict) or not required_safety_keys.issubset(set(safety)) or (set(safety) - safety_keys - {"operations"}):
                    errors.append(_error(path, "database-control-safety", "database_control.safety 键集合无效", "safety:"))
                elif (
                    safety.get("authorization_required") is not True
                ):
                    errors.append(_error(path, "database-control-safety", "数据库控制必须要求逐次授权", "authorization_required"))
                elif not all(isinstance(safety.get(field), str) and safety[field].strip() for field in (required_safety_keys - {"authorization_required", "expected_rows", "operations"})):
                    errors.append(_error(path, "database-control-safety", "数据库控制 safety 字符串字段不得为空", "safety:"))
                elif not multi_operation and (safety.get("expected_rows") != 1 or isinstance(safety.get("expected_rows"), bool)):
                    errors.append(_error(path, "database-control-safety", "数据库控制 expected_rows 必须精确为 1", "expected_rows:"))
                elif safety.get("purpose") not in {"preparation", "time_advance", "expiry_simulation", "state_trigger"}:
                    errors.append(_error(path, "database-control-purpose", "控制 SQL purpose 只能用于准备、时间推进、过期模拟或状态触发", "purpose:"))
                else:
                    operations = safety.get("operations")
                    operation_ids: list[str] = []
                    operation_keys = {
                        "id", "depends_on", "consumer_source", "exact_selector", "expected_rows",
                        "snapshot", "mutation", "verification", "restoration", "restoration_verification",
                    }
                    if not isinstance(operations, list) or not operations:
                        errors.append(_error(path, "database-operations", "数据库控制必须声明非空有序 operations", "operations:"))
                    else:
                        for operation in operations:
                            if not isinstance(operation, dict) or set(operation) != operation_keys:
                                errors.append(_error(path, "database-operation-schema", "数据库控制操作结构无效", "operations:"))
                                continue
                            operation_id = operation.get("id")
                            if not isinstance(operation_id, str) or not operation_id.strip() or operation_id in operation_ids:
                                errors.append(_error(path, "database-operation-id", f"数据库操作 ID 无效或重复: {operation_id}", "id:"))
                                continue
                            dependencies = operation.get("depends_on")
                            if not _strings(dependencies, nonempty=False) or not set(dependencies).issubset(operation_ids):
                                errors.append(_error(path, "database-operation-order", f"数据库操作依赖必须引用此前操作: {operation_id}", "depends_on:"))
                            if operation.get("expected_rows") != 1 or isinstance(operation.get("expected_rows"), bool):
                                errors.append(_error(path, "database-operation-rows", f"每个数据库操作 expected_rows 必须精确为 1: {operation_id}", "expected_rows:"))
                            string_fields = operation_keys - {"depends_on", "expected_rows"}
                            if not all(isinstance(operation.get(field), str) and operation[field].strip() for field in string_fields):
                                errors.append(_error(path, "database-operation-value", f"数据库操作字段不得为空: {operation_id}", "operations:"))
                            operation_ids.append(operation_id)
            elif safety is not None:
                errors.append(_error(path, "database-control-safety", "未使用数据库控制时 safety 必须为 null", "safety:"))
    decision = controls.get("decision")
    if not isinstance(decision, dict) or set(decision) != {"safe_control_path", "blockers"} or not isinstance(decision.get("safe_control_path"), bool) or not _strings(decision.get("blockers"), nonempty=False):
        errors.append(_error(path, "control-decision", "控制决策结构无效", "decision:"))
    return errors


def _constructability_errors(
    path: Path,
    value: Any,
    preconditions: list[str],
    steps: list[Any],
    source_symbols: set[str],
    runtime_evidence: set[str],
    readiness: Mapping[str, Any],
    scenario_status: Any,
    isolation: Any,
    cleanup: Any,
) -> list[str]:
    """校验每个前置和步骤都已穷尽八类构造路径。"""

    errors: list[str] = []
    if not _exact_keys(path, value, {"preconditions", "steps"}, "constructability-schema", errors):
        return errors
    candidate_keys = {
        "kind", "status", "component", "consumer_source", "control", "side_effect", "trigger",
        "observation", "isolation", "cleanup", "evidence",
    }
    isolation_refs = set(str(item) for item in isolation.get("correlation_keys", [])) if isinstance(isolation, Mapping) else set()
    if isinstance(isolation, Mapping):
        isolation_refs.update(
            str(item.get("identity")) for item in isolation.get("owned_resources", []) if isinstance(item, Mapping)
        )
        isolation_refs.update(str(item) for item in isolation.get("mutable_controls", []))
    cleanup_refs = set()
    if isinstance(cleanup, Mapping):
        cleanup_refs.update(str(item) for item in cleanup.get("actions", []))
        cleanup_refs.update(str(item) for item in cleanup.get("verifies", []))

    def check_candidates(candidates: Any, owner: str) -> list[dict[str, Any]]:
        """校验单个前置或步骤的候选矩阵。"""

        if not isinstance(candidates, list):
            errors.append(_error(path, "constructability-candidates", f"候选路径必须是列表: {owner}", "candidates:"))
            return []
        valid = [item for item in candidates if isinstance(item, dict)]
        kinds = [str(item.get("kind")) for item in valid]
        if len(valid) != len(CANDIDATE_KINDS) or set(kinds) != CANDIDATE_KINDS or len(kinds) != len(set(kinds)):
            errors.append(_error(path, "constructability-complete", f"必须逐项评估全部候选控制路径: {owner}", "candidates:"))
        for candidate in valid:
            kind = candidate.get("kind")
            if set(candidate) != candidate_keys:
                errors.append(_error(path, "constructability-candidate-schema", f"候选路径结构无效: {owner}/{kind}", "candidates:"))
                continue
            if candidate.get("status") not in CANDIDATE_STATUSES:
                errors.append(_error(path, "constructability-candidate-status", f"候选路径状态无效: {owner}/{kind}", "status:"))
            if candidate.get("side_effect") not in {"none", "read", "write"}:
                errors.append(_error(path, "constructability-side-effect", f"候选路径副作用无效: {owner}/{kind}", "side_effect:"))
            for field in candidate_keys - {"kind", "status", "side_effect", "evidence"}:
                if not isinstance(candidate.get(field), str) or not candidate[field].strip():
                    errors.append(_error(path, "constructability-candidate-value", f"候选路径字段不得为空: {owner}/{kind}.{field}", f"{field}:"))
            evidence = candidate.get("evidence")
            if not _strings(evidence) or any(item not in source_symbols | runtime_evidence for item in evidence):
                errors.append(_error(path, "constructability-evidence", f"候选路径必须绑定源码或只读运行证据: {owner}/{kind}", "evidence:"))
            if candidate.get("status") == "usable" and candidate.get("side_effect") == "write":
                if candidate.get("isolation") not in isolation_refs or candidate.get("cleanup") not in cleanup_refs:
                    errors.append(_error(path, "constructability-write-probe", f"写入候选必须纳入精确隔离、清理和恢复验证: {owner}/{kind}", "side_effect:"))
        return valid

    precondition_entries = value.get("preconditions") if isinstance(value, dict) else None
    if not isinstance(precondition_entries, list):
        errors.append(_error(path, "constructability-preconditions", "constructability.preconditions 必须是列表", "preconditions:"))
        precondition_entries = []
    precondition_ids = [str(item.get("id")) for item in precondition_entries if isinstance(item, dict)]
    if precondition_ids != preconditions:
        errors.append(_error(path, "constructability-precondition-coverage", "可构造性前置项必须按原顺序完整映射 preconditions", "constructability:"))
    for entry in precondition_entries:
        if not isinstance(entry, dict) or set(entry) != {"id", "data_ownership", "constructible", "candidates"}:
            errors.append(_error(path, "constructability-precondition-schema", "前置可构造性结构无效", "constructability:"))
            continue
        ownership = entry.get("data_ownership")
        constructible = entry.get("constructible")
        if ownership not in {"test_owned", "environment_owned", "not_data"} or not isinstance(constructible, bool):
            errors.append(_error(path, "constructability-ownership", f"前置数据归属无效: {entry.get('id')}", "data_ownership:"))
        candidates = check_candidates(entry.get("candidates"), f"precondition:{entry.get('id')}")
        usable = {str(item.get("kind")) for item in candidates if item.get("status") == "usable"}
        if ownership == "test_owned" and constructible is True:
            if not usable.intersection(CANDIDATE_KINDS - {"existing_test_data"}):
                errors.append(_error(path, "constructability-test-owned", f"可构造的测试自有数据必须选择受控构造路径: {entry.get('id')}", "constructible:"))
            if readiness.get("test_data") == "missing" or any(str(item).startswith("business_data:") for item in readiness.get("blockers", [])):
                errors.append(_error(path, "constructability-not-environment", f"可构造测试数据不得声明为环境预置缺失: {entry.get('id')}", "test_data:"))

    step_entries = value.get("steps") if isinstance(value, dict) else None
    if not isinstance(step_entries, list):
        errors.append(_error(path, "constructability-steps", "constructability.steps 必须是列表", "steps:"))
        step_entries = []
    expected_step_ids = [str(item.get("id")) for item in steps if isinstance(item, dict)]
    actual_step_ids = [str(item.get("step_id")) for item in step_entries if isinstance(item, dict)]
    if actual_step_ids != expected_step_ids:
        errors.append(_error(path, "constructability-step-coverage", "可构造性步骤必须按原顺序完整映射 steps", "constructability:"))
    candidates_by_step: dict[str, list[dict[str, Any]]] = {}
    for entry in step_entries:
        if not isinstance(entry, dict) or set(entry) != {"step_id", "candidates"}:
            errors.append(_error(path, "constructability-step-schema", "步骤可构造性结构无效", "constructability:"))
            continue
        candidates_by_step[str(entry.get("step_id"))] = check_candidates(entry.get("candidates"), f"step:{entry.get('step_id')}")

    for step in steps:
        if not isinstance(step, dict):
            continue
        status = step.get("status")
        candidates = candidates_by_step.get(str(step.get("id")), [])
        usable = any(item.get("status") == "usable" for item in candidates)
        closed = bool(candidates) and all(item.get("status") in {"unusable", "not_found"} for item in candidates)
        if not isinstance(step.get("status_reason"), str) or not step["status_reason"].strip():
            errors.append(_error(path, "step-status-reason", f"步骤必须记录状态原因: {step.get('id')}", "status_reason:"))
        if not _strings(step.get("evidence")) or any(
            item not in source_symbols | runtime_evidence for item in step.get("evidence", [])
        ):
            errors.append(_error(path, "step-status-evidence", f"步骤状态必须绑定源码或只读运行证据: {step.get('id')}", "evidence:"))
        if status not in STEP_STATUSES or status == "runtime_failure":
            errors.append(_error(path, "step-static-status", f"运行前步骤状态无效: {step.get('id')}={status}", "status:"))
        elif status in {"executable", "environment_missing", "authorization_missing"} and not usable:
            errors.append(_error(path, "step-executable-path", f"步骤状态要求至少一个源码确认可用的控制路径: {step.get('id')}", "status:"))
        elif status in {"control_gap", "product_gap"} and not closed:
            errors.append(_error(path, "step-blocked-path", f"步骤阻塞前必须以证据关闭全部候选路径: {step.get('id')}", "status:"))
    if scenario_status == "contract_blocked":
        all_entries = [*precondition_entries, *step_entries]
        if not any(
            isinstance(entry, dict)
            and isinstance(entry.get("candidates"), list)
            and len(entry["candidates"]) == len(CANDIDATE_KINDS)
            and all(
                isinstance(candidate, dict) and candidate.get("status") in {"unusable", "not_found"}
                for candidate in entry["candidates"]
            )
            for entry in all_entries
        ):
            errors.append(_error(path, "contract-constructability-complete", "contract_blocked 必须包含至少一个以证据关闭全部候选路径的阻塞项", "constructability:"))
    return errors


def _isolation_errors(path: Path, isolation: Any, steps: list[Any]) -> list[str]:
    """校验场景拥有资源、关联键和串行锁声明。"""

    errors: list[str] = []
    expected = {"namespace", "correlation_keys", "owned_resources", "mutable_controls", "serial_lock"}
    if not _exact_keys(path, isolation, expected, "isolation-schema", errors):
        return errors
    if not isinstance(isolation.get("namespace"), str) or not isolation["namespace"].strip():
        errors.append(_error(path, "isolation-namespace", "namespace 不得为空", "namespace:"))
    for field in ("correlation_keys", "mutable_controls"):
        if not _strings(isolation.get(field), nonempty=field == "correlation_keys"):
            errors.append(_error(path, "isolation-list", f"{field} 必须是去重字符串列表", field))
    resources = isolation.get("owned_resources")
    identities: set[str] = set()
    if not isinstance(resources, list):
        errors.append(_error(path, "isolation-resources", "owned_resources 必须是列表", "owned_resources"))
    else:
        for item in resources:
            if not isinstance(item, dict) or set(item) != {"kind", "identity", "cleanup", "restore", "verify"}:
                errors.append(_error(path, "isolation-resource-schema", "拥有资源条目无效", "owned_resources"))
                continue
            if not all(isinstance(item.get(field), str) and item[field].strip() for field in item):
                errors.append(_error(path, "isolation-resource-value", "拥有资源字段不得为空", "owned_resources"))
            if item.get("identity") in identities:
                errors.append(_error(path, "isolation-resource-duplicate", f"拥有资源重复: {item.get('identity')}", "identity:"))
            identities.add(str(item.get("identity")))
    mutable_controls = isolation.get("mutable_controls", [])
    if isinstance(mutable_controls, list):
        undeclared = set(str(item) for item in mutable_controls) - identities
        if undeclared:
            errors.append(_error(
                path,
                "isolation-mutable-resource",
                f"每个可变控制都必须作为 owned_resources 精确声明并恢复: {sorted(undeclared)}",
                "mutable_controls:",
            ))
    serial_lock = isolation.get("serial_lock")
    if serial_lock is not None:
        errors.append(_error(path, "isolation-serial-lock", "通用门禁不接受未验证的共享串行锁；资源必须真正隔离", "serial_lock:"))
    if any(isinstance(step, dict) and step.get("side_effect") == "write" for step in steps) and not resources:
        errors.append(_error(path, "isolation-write-resource", "写场景必须声明至少一个精确拥有资源及其清理、恢复和验证动作", "owned_resources:"))
    return errors


def _scenario_errors(
    project_root: Path,
    directory: Path,
    definition: dict[str, Any],
    discovery: dict[str, Any],
) -> list[str]:
    """校验一个场景的完整契约、数据、来源和恢复。"""

    path = directory / "场景定义.yaml"
    errors: list[str] = []
    _exact_keys(path, definition, SCENARIO_KEYS, "scenario-schema", errors)

    meta = definition.get("meta")
    expected_meta_keys = {"id", "name", "status", "actor"}
    if isinstance(meta, dict) and "participants" in meta:
        expected_meta_keys.add("participants")
    if _exact_keys(path, meta, expected_meta_keys, "scenario-meta", errors):
        if not isinstance(meta.get("id"), str) or not ID_RE.fullmatch(meta["id"]):
            errors.append(_error(path, "scenario-id", "场景 ID 格式无效", "id:"))
        if meta.get("status") not in SCENARIO_STATUSES:
            errors.append(_error(path, "scenario-status", "场景状态无效", "status:"))
        for field in ("name", "actor"):
            if not isinstance(meta.get(field), str) or not meta[field].strip():
                errors.append(_error(path, "scenario-meta-value", f"{field} 不得为空", f"{field}:"))
        if "participants" in meta and not _strings(meta.get("participants")):
            errors.append(_error(path, "scenario-meta-participants", "participants 必须是非空且不重复的字符串列表", "participants:"))

    generation = definition.get("generation")
    if _exact_keys(path, generation, {"mode", "owner", "write_scope", "degradation_reason"}, "generation-schema", errors):
        mode = generation.get("mode")
        if mode not in GENERATION_MODES:
            errors.append(_error(path, "generation-mode", f"生成模式无效: {mode}", "mode:"))
        if not isinstance(generation.get("owner"), str) or not generation["owner"].strip():
            errors.append(_error(path, "generation-owner", "owner 不得为空", "owner:"))
        expected_scope = f"scenarios/{directory.name}"
        if generation.get("write_scope") != expected_scope:
            errors.append(_error(path, "generation-scope", f"write_scope 必须为 {expected_scope}", "write_scope:"))
        reason = generation.get("degradation_reason")
        if mode == "sequential_degraded" and (not isinstance(reason, str) or not reason.strip()):
            errors.append(_error(path, "generation-degradation", "降级模式必须说明原因", "degradation_reason:"))
        if mode != "sequential_degraded" and reason is not None:
            errors.append(_error(path, "generation-degradation", "非降级模式的 degradation_reason 必须为 null", "degradation_reason:"))

    readiness = definition.get("readiness")
    readiness_keys = {"source_contract", "safe_control", "runtime_configuration", "test_data", "blockers"}
    if _exact_keys(path, readiness, readiness_keys, "readiness-schema", errors):
        if readiness.get("source_contract") not in {"confirmed", "blocked"} or readiness.get("safe_control") not in {"confirmed", "blocked"}:
            errors.append(_error(path, "readiness-contract", "源码契约和安全控制状态无效", "readiness:"))
        if readiness.get("runtime_configuration") not in {"confirmed", "missing"} or readiness.get("test_data") not in {"confirmed", "missing"}:
            errors.append(_error(path, "readiness-environment", "运行配置或测试数据状态无效", "readiness:"))
        if not _strings(readiness.get("blockers"), nonempty=False):
            errors.append(_error(path, "readiness-blockers", "blockers 必须是去重字符串列表", "blockers:"))

    if not _strings(definition.get("preconditions")):
        errors.append(_error(path, "preconditions", "preconditions 必须是非空去重字符串列表", "preconditions:"))

    steps = definition.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append(_error(path, "steps-required", "steps 必须是非空列表", "steps:"))
        steps = []
    step_ids: set[str] = set()
    for step in steps:
        allowed_step_keys = {
            "id", "action", "control", "side_effect", "expect", "data_ref", "status", "status_reason", "evidence",
            "design_rule_id", "protocol_ref", "phase",
        }
        required_step_keys = {"id", "action", "control", "side_effect", "expect"}
        if not isinstance(step, dict) or not required_step_keys.issubset(set(step)) or set(step) - allowed_step_keys:
            errors.append(_error(path, "step-schema", "步骤键集合无效", "steps:"))
            continue
        if not isinstance(step.get("id"), str) or not step["id"].strip() or step["id"] in step_ids:
            errors.append(_error(path, "step-id", f"步骤 ID 无效或重复: {step.get('id')}", "id:"))
        step_ids.add(str(step.get("id")))
        if step.get("control") not in CONTROL_NAMES or step.get("side_effect") not in {"none", "read", "write"}:
            errors.append(_error(path, "step-control", f"步骤控制或副作用无效: {step.get('id')}", "control:"))
        if step.get("side_effect") == "write" and step.get("control") not in WRITE_CONTROLS:
            errors.append(_error(path, "step-side-effect-control", f"写步骤不得声明为只读或可观测控制: {step.get('id')}", "side_effect:"))
        if step.get("control") in INHERENT_WRITE_CONTROLS and step.get("side_effect") != "write":
            errors.append(_error(path, "step-side-effect-control", f"危险控制步骤必须声明 side_effect=write: {step.get('id')}", "side_effect:"))
        if step.get("control") in {"database_read", "observability"} and step.get("side_effect") == "write":
            errors.append(_error(path, "step-readonly-control", f"只读控制不得承担写步骤: {step.get('id')}", "control:"))
        if not isinstance(step.get("action"), str) or not step["action"].strip() or not _strings(step.get("expect")):
            errors.append(_error(path, "step-content", f"步骤动作或期望无效: {step.get('id')}", "action:"))
        if "status" in step and step.get("status") not in STEP_STATUSES:
            errors.append(_error(path, "step-status", f"步骤状态无效: {step.get('id')}", "status:"))
        if "status_reason" in step and (not isinstance(step.get("status_reason"), str) or not step["status_reason"].strip()):
            errors.append(_error(path, "step-status-reason", f"步骤状态原因不得为空: {step.get('id')}", "status_reason:"))
        if "evidence" in step and not _strings(step.get("evidence"), nonempty=False):
            errors.append(_error(path, "step-evidence", f"步骤证据必须是字符串列表: {step.get('id')}", "evidence:"))
        if "data_ref" in step and (not isinstance(step["data_ref"], str) or not step["data_ref"].startswith("业务数据.json#/")):
            errors.append(_error(path, "data-ref", f"data_ref 无效: {step.get('data_ref')}", "data_ref:"))

        if "phase" in step and step.get("phase") not in {"request", "message_acceptance", "processing", "final_business", "side_effect"}:
            errors.append(_error(path, "step-phase", f"invalid step phase: {step.get('id')}", "phase:"))
        for field in ("design_rule_id", "protocol_ref"):
            if field in step and (not isinstance(step.get(field), str) or not step[field].strip()):
                errors.append(_error(path, "step-provenance", f"empty {field}: {step.get('id')}", field + ":"))

    source_symbols = {
        f"{item.get('repo')}#{anchor}"
        for item in definition.get("source", []) if isinstance(item, dict)
        for anchor in item.get("anchors", [])
    }
    constructability = definition.get("constructability")
    if constructability is not None:
        probe = discovery.get("runtime_probe", {}) if isinstance(discovery, dict) else {}
        runtime_evidence = set()
        if isinstance(probe, dict):
            runtime_evidence.update(
                f"runtime_probe:process/{item.get('id')}" for item in probe.get("processes", []) if isinstance(item, dict)
            )
            runtime_evidence.update(
                f"runtime_probe:listener/{item.get('id')}" for item in probe.get("listeners", []) if isinstance(item, dict)
            )
            runtime_evidence.update(
                f"runtime_probe:smoke/{item.get('node')}" for item in probe.get("read_only_smoke", []) if isinstance(item, dict)
            )
        errors.extend(_constructability_errors(
            path,
            constructability,
            [str(item) for item in definition.get("preconditions", [])],
            steps,
            source_symbols,
            runtime_evidence,
            readiness if isinstance(readiness, dict) else {},
            meta.get("status") if isinstance(meta, dict) else None,
            definition.get("isolation"),
            definition.get("cleanup"),
        ))
    elif isinstance(meta, dict) and meta.get("status") == "contract_blocked":
        errors.append(_error(path, "contract-constructability-required", "contract_blocked 必须记录全部候选控制路径分析", "constructability:"))
    errors.extend(_control_errors(path, definition.get("controls"), steps, source_symbols))
    errors.extend(_isolation_errors(path, definition.get("isolation"), steps))
    controls = definition.get("controls", {})
    if isinstance(controls, dict):
        control_semantics = {
            "public_api": {"http_rpc"},
            "test_or_admin_api": {"http_rpc"},
            "mocks_and_faults": {"http_rpc", "configuration"},
            "dynamic_configuration": {"configuration"},
            "scheduled_jobs": {"jobs"},
            "messages": {"messages"},
            "database_read": {"database"},
            "database_control": {"database", "jobs"},
            "observability": {"http_rpc", "messages", "database", "cache", "jobs"},
        }
        for name, categories in control_semantics.items():
            item = controls.get(name, {})
            if not isinstance(item, dict) or item.get("status") in {"not_found", "not_applicable"}:
                continue
            for reference in item.get("evidence", []):
                if not _source_reference_semantic(project_root, discovery, str(reference), categories):
                    errors.append(_error(path, "control-evidence-semantic", f"控制证据与能力类别不匹配: {name}={reference}", str(reference)))
        for step in steps:
            if not isinstance(step, dict):
                continue
            control = controls.get(str(step.get("control")), {})
            if step.get("status", "executable") not in {"control_gap", "product_gap"} and (
                not isinstance(control, dict) or control.get("status") != "usable"
            ):
                errors.append(_error(path, "step-control-usable", f"步骤引用的控制能力不可用: {step.get('control')}", "control:"))
    decision = controls.get("decision", {}) if isinstance(controls, dict) else {}
    derived = _derived_status(readiness if isinstance(readiness, dict) else {}, decision if isinstance(decision, dict) else {})
    observability = controls.get("observability", {}) if isinstance(controls, dict) else {}
    if derived == "ready" and (not isinstance(observability, dict) or observability.get("status") != "usable"):
        errors.append(_error(path, "ready-observability", "ready 场景必须有 usable 的可观测证据与恢复能力", "observability:"))
        derived = None
    database_control = controls.get("database_control", {}) if isinstance(controls, dict) else {}
    if isinstance(database_control, dict) and database_control.get("status") == "usable":
        safety = database_control.get("safety", {})
        isolation = definition.get("isolation", {})
        if isinstance(safety, dict) and isinstance(isolation, dict):
            multi_operation = isinstance(safety.get("operations"), list)
            owned_selectors = set(str(item) for item in isolation.get("correlation_keys", []))
            owned_selectors.update(
                str(item.get("identity")) for item in isolation.get("owned_resources", []) if isinstance(item, dict)
            )
            selectors = [
                str(item.get("exact_selector")) for item in safety.get("operations", [])
                if isinstance(item, dict)
            ] if multi_operation else [str(safety.get("exact_selector"))]
            if any(selector not in owned_selectors for selector in selectors):
                errors.append(_error(path, "database-selector-owned", "控制 SQL exact_selector 必须属于当前场景隔离契约", "exact_selector:"))
            consumers = [
                str(item.get("consumer_source")) for item in safety.get("operations", [])
                if isinstance(item, dict)
            ] if multi_operation else [str(safety.get("consumer_source"))]
            if any(consumer not in source_symbols for consumer in consumers):
                errors.append(_error(path, "database-consumer-source", "consumer_source 必须解析到场景源码锚点", "consumer_source:"))
            elif any(not _source_reference_semantic(project_root, discovery, consumer, {"database", "jobs"}) for consumer in consumers):
                errors.append(_error(path, "database-consumer-semantic", "consumer_source 必须定位后续数据库消费或任务触发逻辑", "consumer_source:"))
            cleanup = definition.get("cleanup", {})
            trace_symbols = set(str(item) for item in definition.get("preconditions", []))
            trace_symbols.update(str(step.get("action")) for step in steps if isinstance(step, dict))
            trace_symbols.update(
                str(expectation)
                for step in steps if isinstance(step, dict)
                for expectation in step.get("expect", [])
            )
            if isinstance(cleanup, dict):
                trace_symbols.update(str(item) for item in cleanup.get("actions", []))
                trace_symbols.update(str(item) for item in cleanup.get("verifies", []))
            if not multi_operation:
                for field in ("snapshot", "mutation", "trigger", "verification", "restoration", "restoration_verification"):
                    if safety.get(field) not in trace_symbols:
                        errors.append(_error(path, "database-control-trace", f"数据库控制符号未映射到场景链路: {field}", f"{field}:"))
            business_trigger_steps = [
                step for step in steps
                if isinstance(step, dict)
                and step.get("control") != "database_control"
                and step.get("action") == safety.get("trigger")
            ]
            if not business_trigger_steps or not any(
                safety.get("verification") in step.get("expect", []) for step in business_trigger_steps
            ):
                errors.append(_error(
                    path,
                    "database-control-business-path",
                    "控制 SQL 只能准备或推进测试状态，必须由独立正常业务步骤触发并验证最终结果",
                    "trigger:",
                ))
            for operation in safety.get("operations", []) if isinstance(safety.get("operations"), list) else []:
                if not isinstance(operation, dict):
                    continue
                selector = operation.get("exact_selector")
                if selector not in owned_selectors:
                    errors.append(_error(path, "database-operation-selector-owned", f"数据库操作 selector 必须属于当前场景隔离契约: {selector}", "exact_selector:"))
                if operation.get("consumer_source") not in source_symbols:
                    errors.append(_error(path, "database-operation-consumer-source", f"数据库操作 consumer_source 未解析到场景源码锚点: {operation.get('id')}", "consumer_source:"))
                for field in ("snapshot", "mutation", "verification", "restoration", "restoration_verification"):
                    if operation.get(field) not in trace_symbols:
                        errors.append(_error(path, "database-operation-trace", f"数据库操作符号未映射到场景链路: {operation.get('id')}.{field}", field + ":"))
                final_expectations = {
                    str(expectation)
                    for step in business_trigger_steps
                    for expectation in step.get("expect", [])
                }
                if operation.get("mutation") in final_expectations or operation.get("verification") in final_expectations:
                    errors.append(_error(
                        path,
                        "database-control-final-result",
                        f"数据库写入不得直接制造或充当最终业务结果: {operation.get('id')}",
                        "operations:",
                    ))
            target = safety.get("target_environment")
            if not isinstance(target, str) or not ENV_RE.fullmatch(target):
                errors.append(_error(path, "database-target-environment", "target_environment 必须是精确测试环境标识", "target_environment:"))
    isolation = definition.get("isolation", {})
    cleanup = definition.get("cleanup", {})
    if isinstance(observability, dict) and observability.get("status") == "usable" and isinstance(isolation, dict):
        observed_keys = set(str(item) for item in observability.get("correlation_keys", []))
        isolated_keys = set(str(item) for item in isolation.get("correlation_keys", []))
        if observed_keys != isolated_keys:
            errors.append(_error(path, "observability-correlation-mapping", "observability.correlation_keys 必须与场景隔离关联键完全一致", "correlation_keys:"))
        expected_evidence = {
            str(expectation)
            for step in steps if isinstance(step, dict)
            for expectation in step.get("expect", [])
        }
        if set(str(item) for item in observability.get("business_evidence", [])) != expected_evidence:
            errors.append(_error(path, "observability-evidence-mapping", "observability.business_evidence 必须完整映射步骤期望", "business_evidence:"))
        cleanup_symbols = set()
        if isinstance(cleanup, dict):
            cleanup_symbols.update(str(item) for item in cleanup.get("actions", []))
            cleanup_symbols.update(str(item) for item in cleanup.get("verifies", []))
        if set(str(item) for item in observability.get("recovery", [])) != cleanup_symbols:
            errors.append(_error(path, "observability-recovery-mapping", "observability.recovery 必须完整映射 cleanup 动作和验证", "recovery:"))
    if isinstance(meta, dict) and meta.get("status") != derived:
        errors.append(_error(path, "scenario-status-derived", f"声明状态与推导状态不符: declared={meta.get('status')}, derived={derived}", "status:"))

    if isinstance(meta, dict) and meta.get("status") == "contract_blocked" and isinstance(controls, dict):
        readiness_blockers = readiness.get("blockers", []) if isinstance(readiness, dict) else []
        decision_blockers = decision.get("blockers", []) if isinstance(decision, dict) else []
        if readiness_blockers != decision_blockers:
            errors.append(_error(path, "contract-blocker-consistency", "contract_blocked 的 readiness 与 control decision 必须记录同一组阻塞", "blockers:"))
        control_candidates = set(CONTROL_NAMES) - {"database_read", "observability"}
        for blocker in decision_blockers if isinstance(decision_blockers, list) else []:
            if str(blocker).startswith("control:"):
                name = str(blocker).split(":", 1)[1]
                item = controls.get(name, {})
                if name not in control_candidates or not isinstance(item, dict) or item.get("status") not in {"unusable", "not_found"} or not item.get("evidence"):
                    errors.append(_error(path, "contract-control-blocker", f"控制阻塞未绑定不可用能力及源码证据: {blocker}", str(blocker)))
            elif str(blocker).startswith("contract:"):
                reference = str(blocker).split(":", 1)[1]
                if reference not in source_symbols:
                    errors.append(_error(path, "contract-source-blocker", f"契约阻塞未绑定场景源码锚点: {blocker}", str(blocker)))
            else:
                errors.append(_error(path, "contract-blocker-format", f"契约阻塞必须使用 control:<name> 或 contract:<repo>#<anchor>: {blocker}", str(blocker)))
        if isinstance(readiness, dict) and readiness.get("safe_control") == "blocked":
            incomplete = [
                name for name in control_candidates
                if not isinstance(controls.get(name), dict)
                or controls[name].get("status") not in {"unusable", "not_found"}
                or not controls[name].get("evidence")
            ]
            if incomplete:
                errors.append(_error(path, "contract-safe-control", f"contract_blocked 前必须以源码证据确认全部候选控制不可用: {sorted(incomplete)}", "safe_control:"))

    errors.extend(_integration_errors(path, definition.get("integrations"), discovery))
    configuration = discovery.get("configuration", {}) if isinstance(discovery, dict) else {}
    owner_by_integration = {
        str(item.get("id")): str(item.get("owner"))
        for collection in ("services", "data_sources", "middleware", "controls")
        for item in configuration.get(collection, []) if isinstance(item, dict)
    }
    integrations = definition.get("integrations", {})
    used_ids = set(integrations.get("services", [])) if isinstance(integrations, dict) and isinstance(integrations.get("services"), list) else set()
    if isinstance(integrations, dict) and isinstance(integrations.get("components"), list):
        used_ids.update(str(item.get("id")) for item in integrations["components"] if isinstance(item, dict))
    if constructability is not None and isinstance(readiness, dict) and readiness.get("runtime_configuration") == "confirmed":
        probe = discovery.get("runtime_probe", {}) if isinstance(discovery, dict) else {}
        confirmed_configuration = {
            str(item.get("id")) for item in probe.get("configuration_checks", [])
            if isinstance(item, dict) and item.get("effective") == "confirmed"
        } if isinstance(probe, dict) else set()
        missing_confirmation = used_ids - confirmed_configuration
        if missing_confirmation:
            errors.append(_error(
                path,
                "runtime-configuration-unconfirmed",
                f"源码默认值不能替代运行时有效配置确认: {sorted(missing_confirmation)}",
                "runtime_configuration:",
            ))
    required_source_repos = {
        owner_by_integration[item].split(":", 1)[0]
        for item in used_ids if item in owner_by_integration
    }
    declared_source_repos = {
        str(item.get("repo")) for item in definition.get("source", []) if isinstance(item, dict)
    }
    if required_source_repos - declared_source_repos:
        errors.append(_error(
            path,
            "source-owner-coverage",
            f"场景源码基线遗漏集成所有者仓库: {sorted(required_source_repos - declared_source_repos)}",
            "source:",
        ))
    errors.extend(_cleanup_errors(path, definition.get("cleanup"), steps, definition.get("isolation")))
    errors.extend(_source_errors(path, definition.get("source"), discovery, project_root))
    errors.extend(_scenario_artifact_errors(project_root, directory, definition))
    return errors


def _integration_errors(path: Path, integrations: Any, discovery: dict[str, Any]) -> list[str]:
    """校验场景依赖只引用发现契约中的服务和组件。"""

    errors: list[str] = []
    if not _exact_keys(path, integrations, {"services", "components"}, "integration-schema", errors):
        return errors
    configuration = discovery.get("configuration", {}) if isinstance(discovery, dict) else {}
    service_ids = {str(item.get("id")) for item in configuration.get("services", []) if isinstance(item, dict)}
    component_types = {
        str(item.get("id")): str(item.get("type"))
        for name in ("data_sources", "middleware", "controls")
        for item in configuration.get(name, [])
        if isinstance(item, dict)
    }
    services = integrations.get("services")
    if not _strings(services, nonempty=False) or not set(services).issubset(service_ids):
        errors.append(_error(path, "integration-service", "services 包含未发现或重复的服务", "services:"))
    components = integrations.get("components")
    if not isinstance(components, list):
        errors.append(_error(path, "integration-components", "components 必须是列表", "components:"))
        return errors
    seen: set[str] = set()
    for component in components:
        if not isinstance(component, dict) or set(component) != {"id", "type", "required"}:
            errors.append(_error(path, "integration-component-schema", "组件依赖条目无效", "components:"))
            continue
        component_id = component.get("id")
        if component_id not in component_types or component_id in seen or not isinstance(component.get("required"), bool):
            errors.append(_error(path, "integration-component", f"组件依赖无效或重复: {component_id}", "id:"))
        elif str(component.get("type")) != component_types[component_id]:
            errors.append(_error(path, "integration-component-type", f"组件类型与发现结果不符: {component_id}", "type:"))
        seen.add(str(component_id))
    return errors


def _cleanup_errors(path: Path, cleanup: Any, steps: list[Any], isolation: Any) -> list[str]:
    """校验写步骤具有明确且可验证的清理契约。"""

    errors: list[str] = []
    if not _exact_keys(path, cleanup, {"strategy", "actions", "verifies"}, "cleanup-schema", errors):
        return errors
    if not isinstance(cleanup.get("strategy"), str) or not cleanup["strategy"].strip() or not _strings(cleanup.get("actions")) or not _strings(cleanup.get("verifies")):
        errors.append(_error(path, "cleanup-content", "清理策略、动作和验证均不得为空", "cleanup:"))
    if any(isinstance(step, dict) and step.get("side_effect") == "write" for step in steps) and not cleanup.get("actions"):
        errors.append(_error(path, "cleanup-write-required", "写步骤必须声明清理动作", "cleanup:"))
    if isinstance(isolation, dict) and isinstance(cleanup, dict):
        actions = set(str(item) for item in cleanup.get("actions", []))
        verifies = set(str(item) for item in cleanup.get("verifies", []))
        for resource in isolation.get("owned_resources", []):
            if not isinstance(resource, dict):
                continue
            missing_actions = {
                str(resource.get(field)) for field in ("cleanup", "restore")
                if str(resource.get(field)) not in actions
            }
            if missing_actions:
                errors.append(_error(path, "cleanup-resource-action", f"拥有资源的清理/恢复动作未进入 finally 契约: {sorted(missing_actions)}", "actions:"))
            if str(resource.get("verify")) not in verifies:
                errors.append(_error(path, "cleanup-resource-verify", f"拥有资源的恢复验证未进入契约: {resource.get('verify')}", "verifies:"))
    return errors


def _source_errors(path: Path, source: Any, discovery: dict[str, Any], project_root: Path) -> list[str]:
    """校验场景源码基线可解析且锚点非空。"""

    errors: list[str] = []
    repositories = {
        str(item.get("id")): item
        for item in discovery.get("inventory", {}).get("repositories", [])
        if isinstance(item, dict)
    }
    if not isinstance(source, list) or not source:
        return [_error(path, "source-required", "source 必须是非空列表", "source:")]
    seen: set[str] = set()
    for item in source:
        if not isinstance(item, dict) or set(item) != {"repo", "commit", "anchors"}:
            errors.append(_error(path, "source-schema", "源码条目无效", "source:"))
            continue
        repo_id = item.get("repo")
        if repo_id not in repositories or repo_id in seen:
            errors.append(_error(path, "source-repository", f"源码仓库未知或重复: {repo_id}", "repo:"))
            continue
        seen.add(str(repo_id))
        commit = item.get("commit")
        if not isinstance(commit, str) or not SHA_RE.fullmatch(commit) or not _strings(item.get("anchors")):
            errors.append(_error(path, "source-contract", f"源码提交或锚点无效: {repo_id}", "commit:"))
            continue
        repo_root = _resolve(project_root, str(repositories[repo_id].get("root", "")))
        if commit != repositories[repo_id].get("commit"):
            errors.append(_error(path, "source-commit-coherence", f"场景源码提交必须等于工作区发现快照: {repo_id}", "commit:"))
        if _git(repo_root, "cat-file", "-e", f"{commit}^{{commit}}").returncode != 0:
            errors.append(_error(path, "source-commit", f"源码提交不存在: {repo_id}#{commit}", commit))
        for anchor in item["anchors"]:
            if _git(repo_root, "grep", "-q", "--fixed-strings", str(anchor), commit, "--", ".").returncode != 0:
                errors.append(_error(path, "source-anchor", f"源码锚点无法解析: {repo_id}#{anchor}", str(anchor)))
    relevant_repositories = {
        str(node.get("id", "")).split(":", 1)[0]
        for node in discovery.get("topology", {}).get("nodes", [])
        if isinstance(node, dict) and node.get("relevant") is True and ":" in str(node.get("id", ""))
    }
    missing_repositories = relevant_repositories - seen
    if missing_repositories:
        errors.append(_error(path, "source-relevant-coverage", f"场景源码基线遗漏相关依赖仓库: {sorted(missing_repositories)}", "source:"))
    return errors


def _scenario_artifact_errors(project_root: Path, directory: Path, definition: dict[str, Any]) -> list[str]:
    """校验场景必需文件、环境数据路径和稳定标识。"""

    errors: list[str] = []
    definition_path = directory / "场景定义.yaml"
    required = [directory / "业务数据.json", directory / "业务流程图.md", directory / f"test_{directory.name}.py"]
    for path in required:
        if not path.is_file():
            errors.append(_error(path, "scenario-artifact", "场景必需文件不存在"))
    runtime_path = project_root / "config" / "config.yaml"
    runtime = _load_yaml(runtime_path, errors)
    active = runtime.get("active_environment")
    if not isinstance(active, str) or not ENV_RE.fullmatch(active):
        errors.append(_error(runtime_path, "active-environment", "active_environment 格式无效", "active_environment"))
    environment_path = project_root / "config" / "environments" / f"{active}.yaml" if isinstance(active, str) else None
    if isinstance(active, str) and environment_path is not None and not environment_path.is_file():
        errors.append(_error(runtime_path, "active-environment-file", f"缺少环境文件: {active}.yaml", active))
    environment: dict[str, Any] = {}
    if environment_path is not None and environment_path.is_file():
        environment = _load_yaml(environment_path, errors)
    integrations = definition.get("integrations", {})
    missing_runtime: list[str] = []
    runtime_blockers: set[str] = set()
    if definition.get("meta", {}).get("status") == "ready" and isinstance(integrations, dict):
        available_services = environment.get("services", {}) if isinstance(environment.get("services"), dict) else {}
        available_components = environment.get("components", {}) if isinstance(environment.get("components"), dict) else {}
        missing_services = [item for item in integrations.get("services", []) if item not in available_services]
        missing_components = [
            item.get("id") for item in integrations.get("components", [])
            if isinstance(item, dict) and item.get("required") is True and item.get("id") not in available_components
        ]
        if missing_services or missing_components:
            errors.append(_error(directory / "场景定义.yaml", "ready-runtime-mapping", f"ready 场景缺少激活环境映射: services={missing_services}, components={missing_components}", "integrations:"))
        missing_runtime.extend(f"services.{item}" for item in missing_services)
        missing_runtime.extend(f"components.{item}" for item in missing_components)
        runtime_blockers.update(f"config:services/{item}" for item in missing_services)
        runtime_blockers.update(f"config:components/{item}" for item in missing_components)
    if isinstance(integrations, dict):
        available_services = environment.get("services", {}) if isinstance(environment.get("services"), dict) else {}
        available_components = environment.get("components", {}) if isinstance(environment.get("components"), dict) else {}
        selected_runtime = {
            "services": {item: available_services[item] for item in integrations.get("services", []) if item in available_services},
            "components": {
                item.get("id"): available_components[item.get("id")]
                for item in integrations.get("components", []) if isinstance(item, dict)
                and item.get("required") is True and item.get("id") in available_components
            },
        }
        missing_runtime.extend(_missing_placeholders(selected_runtime, "$.configuration"))
        runtime_blockers.update(_missing_placeholder_blockers(selected_runtime, "config"))
    database_control = definition.get("controls", {}).get("database_control", {})
    if isinstance(database_control, dict) and database_control.get("status") == "usable":
        safety = database_control.get("safety", {})
        if isinstance(safety, dict) and definition.get("meta", {}).get("status") == "ready" and safety.get("target_environment") != active:
            errors.append(_error(directory / "场景定义.yaml", "database-target-active", "ready 场景的数据库控制环境必须等于 active_environment", "target_environment:"))
    data = _load_json(directory / "业务数据.json", errors)
    protocol_artifact_path = project_root / "discovery" / "protocol-rules.yaml"
    try:
        protocol_document = yaml.safe_load(protocol_artifact_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        protocol_document = None
    protocol_operations = {
        str(item.get("id")): item
        for item in (protocol_document.get("operations", []) if isinstance(protocol_document, dict) else [])
        if isinstance(item, dict) and item.get("id")
    }

    def data_keys(value: Any, prefix: str = "") -> set[str]:
        """Collect request payload paths for protocol required-field checks."""

        if isinstance(value, dict):
            result: set[str] = set()
            for key, child in value.items():
                path_key = f"{prefix}.{key}" if prefix else str(key)
                result.add(path_key)
                result.update(data_keys(child, path_key))
            return result
        if isinstance(value, list):
            return {path for index, child in enumerate(value) for path in data_keys(child, f"{prefix}.{index}")}
        return {prefix} if prefix else set()

    if isinstance(data, dict):
        errors.extend(_secret_errors(directory / "业务数据.json", data))
        logical_paths: dict[str, set[str]] = {}
        for environment, values in data.items():
            if not isinstance(values, dict):
                errors.append(_error(directory / "业务数据.json", "business-data-environment", f"环境数据必须是映射: {environment}"))
                continue
            paths: set[str] = set()

            def collect(value: Any, prefix: str = "") -> None:
                """收集环境对象的逻辑叶子路径。"""

                if isinstance(value, dict):
                    for key, child in value.items():
                        collect(child, f"{prefix}/{key}")
                elif isinstance(value, list):
                    for index, child in enumerate(value):
                        collect(child, f"{prefix}/{index}")
                else:
                    paths.add(prefix)
                    if isinstance(value, str) and (URL_RE.search(value) or SQL_RE.search(value)):
                        errors.append(_error(directory / "业务数据.json", "business-data-content", f"业务数据不得保存端点或 SQL: {environment}{prefix}"))

            collect(values)
            logical_paths[str(environment)] = paths
        if logical_paths and len({frozenset(paths) for paths in logical_paths.values()}) > 1:
            errors.append(_error(directory / "业务数据.json", "business-data-shape", "所有环境必须暴露相同逻辑数据路径"))
        status = definition.get("meta", {}).get("status")
        if status == "ready" and not isinstance(data.get(active), dict):
            errors.append(_error(directory / "业务数据.json", "active-business-data", f"ready 场景缺少环境数据: {active}"))
        missing_test_data = [] if isinstance(data.get(active), dict) else [f"business_data:{active}"]
        data_blockers = set() if isinstance(data.get(active), dict) else {f"business_data:{active}"}
        if isinstance(data.get(active), dict):
            missing_test_data.extend(_missing_placeholders(data[active], "$.business_data"))
            data_blockers.update(_missing_placeholder_blockers(data[active], "business_data"))
        if status == "ready" and (missing_runtime or missing_test_data):
            errors.append(_error(directory / "场景定义.yaml", "ready-runtime-values", f"ready 场景仍缺少运行值: runtime={missing_runtime}, test_data={missing_test_data}", "status: ready"))
        readiness = definition.get("readiness", {})
        if status == "pending_environment" and isinstance(readiness, dict):
            blockers = set(str(item) for item in readiness.get("blockers", []))
            expected_blockers = runtime_blockers | data_blockers
            if blockers != expected_blockers:
                errors.append(_error(
                    directory / "场景定义.yaml",
                    "pending-blocker-binding",
                    f"pending_environment 阻塞必须与当前实际缺失项精确一致: expected={sorted(expected_blockers)}, actual={sorted(blockers)}",
                    "blockers:",
                ))
            if readiness.get("runtime_configuration") != ("missing" if runtime_blockers else "confirmed"):
                errors.append(_error(directory / "场景定义.yaml", "pending-runtime-state", "runtime_configuration 与实际缺失配置不一致", "runtime_configuration:"))
            if readiness.get("test_data") != ("missing" if data_blockers else "confirmed"):
                errors.append(_error(directory / "场景定义.yaml", "pending-data-state", "test_data 与实际缺失数据不一致", "test_data:"))
        for step in definition.get("steps", []):
            if not isinstance(step, dict):
                continue
            protocol_ref = step.get("protocol_ref")
            operation = protocol_operations.get(str(protocol_ref)) if protocol_ref else None
            if "data_ref" not in step:
                if isinstance(operation, dict):
                    errors.extend(_protocol_field_errors(directory / "业务数据.json", step, operation, None))
                continue
            pointer = str(step["data_ref"]).split("#/", 1)[-1]
            for environment, values in data.items():
                current = values
                try:
                    for part in pointer.split("/"):
                        key = part.replace("~1", "/").replace("~0", "~")
                        current = current[int(key)] if isinstance(current, list) else current[key]
                except (KeyError, IndexError, TypeError, ValueError):
                    errors.append(_error(directory / "业务数据.json", "data-ref-resolve", f"data_ref 在环境 {environment} 中无法解析: {step['data_ref']}"))
                    continue
                if not _has_constructed_business_value(current):
                    errors.append(_error(
                        directory / "业务数据.json",
                        "business-data-overinjected",
                        f"data_ref 在环境 {environment} 中全部依赖变量注入；应按正式协议保留可构造字面值或在运行期生成随机值: {step['data_ref']}",
                    ))
                if isinstance(operation, dict):
                    errors.extend(_protocol_field_errors(directory / "业务数据.json", step, operation, current))
    test_path = directory / f"test_{directory.name}.py"
    stable_id = definition.get("meta", {}).get("id")
    if test_path.is_file() and isinstance(stable_id, str):
        content = test_path.read_text(encoding="utf-8")
        marker = f'scenario_id("{stable_id}")'
        if content.count(marker) != 1:
            errors.append(_error(test_path, "scenario-marker", "稳定 ID 必须只出现在一个 scenario_id marker 中", "scenario_id"))
        if content.count("business_e2e") != 1:
            errors.append(_error(test_path, "business-marker", "场景测试必须有且只有一个 business_e2e marker", "business_e2e"))
        if content.count(stable_id) != 1:
            errors.append(_error(test_path, "scenario-id-unique", "稳定 ID 在场景测试中只能出现于唯一 marker", stable_id))
        generated_roots = [project_root / name for name in ("common", "config", "scripts", "tests", "scenarios")]
        for root in generated_roots:
            if not root.is_dir():
                continue
            for other in _walk_files(root):
                if other in {definition_path, test_path} or other.suffix.lower() not in {".py", ".yaml", ".yml", ".json", ".md", ".toml"}:
                    continue
                try:
                    if stable_id in other.read_text(encoding="utf-8"):
                        errors.append(_error(other, "scenario-id-leak", "稳定 ID 不得复制到 marker 之外"))
                except (OSError, UnicodeError):
                    continue
    return errors


def _cross_scenario_errors(scenarios: list[tuple[Path, dict[str, Any]]]) -> list[str]:
    """校验多场景委派和资源隔离不发生冲突。"""

    errors: list[str] = []
    if len(scenarios) > 1:
        modes = {str(definition.get("generation", {}).get("mode")) for _, definition in scenarios}
        if modes not in ({"delegated"}, {"sequential_degraded"}):
            errors.append(_error(scenarios[0][0] / "场景定义.yaml", "multi-scenario-mode", "多场景必须全部 delegated，或全部明确 sequential_degraded"))
        owners = [str(definition.get("generation", {}).get("owner")) for _, definition in scenarios]
        if "delegated" in modes and len(owners) != len(set(owners)):
            errors.append(_error(scenarios[0][0] / "场景定义.yaml", "multi-scenario-owner", "delegated 场景 owner 必须唯一"))

    namespaces: dict[str, Path] = {}
    correlations: dict[str, tuple[Path, str | None]] = {}
    resources: dict[str, tuple[Path, str | None]] = {}
    mutable: dict[str, tuple[Path, str | None]] = {}
    for directory, definition in scenarios:
        path = directory / "场景定义.yaml"
        isolation = definition.get("isolation", {})
        if not isinstance(isolation, dict):
            continue
        namespace = isolation.get("namespace")
        if isinstance(namespace, str) and namespace:
            if namespace in namespaces:
                errors.append(_error(path, "isolation-namespace-collision", f"命名空间与 {namespaces[namespace]} 冲突: {namespace}", "namespace:"))
            namespaces[namespace] = path
        lock = isolation.get("serial_lock") if isinstance(isolation.get("serial_lock"), str) else None
        for correlation in isolation.get("correlation_keys", []):
            correlation = str(correlation)
            if correlation in correlations:
                errors.append(_error(path, "isolation-correlation-collision", f"关联键与 {correlations[correlation][0]} 冲突: {correlation}", "correlation_keys:"))
            correlations[correlation] = (path, lock)
        for resource in isolation.get("owned_resources", []):
            if not isinstance(resource, dict):
                continue
            identity = str(resource.get("identity", ""))
            if identity in resources:
                errors.append(_error(path, "isolation-resource-collision", f"拥有资源与 {resources[identity][0]} 冲突: {identity}", "identity:"))
            resources[identity] = (path, lock)
        for control in isolation.get("mutable_controls", []):
            control = str(control)
            if control in mutable:
                errors.append(_error(path, "isolation-control-collision", f"可变控制与 {mutable[control][0]} 冲突: {control}", "mutable_controls:"))
            mutable[control] = (path, lock)
    return errors


def _source_reference_exists(project_root: Path, reference: str) -> bool:
    """核对 repository#anchor 是否存在于发现契约固定的提交。"""

    if "#" not in reference:
        return False
    repo_id, anchor = reference.split("#", 1)
    try:
        document = yaml.safe_load((project_root / "discovery" / "workspace.yaml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return False
    repositories = document.get("inventory", {}).get("repositories", []) if isinstance(document, dict) else []
    for repository in repositories:
        if not isinstance(repository, dict) or repository.get("id") != repo_id:
            continue
        root = _resolve(project_root, str(repository.get("root", "")))
        commit = str(repository.get("commit", ""))
        return bool(anchor) and _git(root, "grep", "-q", "--fixed-strings", anchor, commit, "--", ".").returncode == 0
    return False


def _source_reference_semantic(
    project_root: Path,
    discovery: Mapping[str, Any],
    reference: str,
    categories: set[str],
) -> bool:
    """Verify a scenario source reference has one of the required source semantics."""

    if "#" not in reference:
        return False
    repo_id, anchor = reference.split("#", 1)
    repositories = discovery.get("inventory", {}).get("repositories", [])
    for repository in repositories if isinstance(repositories, list) else []:
        if not isinstance(repository, dict) or repository.get("id") != repo_id:
            continue
        root = _resolve(project_root, str(repository.get("root", "")))
        commit = str(repository.get("commit", ""))
        return bool(anchor) and any(_semantic_evidence(root, commit, anchor, category) for category in categories)
    return False


def contract_errors(project_root: Path, selected: str | None = None) -> tuple[list[str], list[tuple[Path, dict[str, Any]]], dict[str, Any]]:
    """校验全部或指定场景契约，并执行跨场景所有权检查。"""

    errors, discovery = discovery_errors(project_root)
    directories = _scenario_directories(project_root, None, errors)
    all_scenarios: list[tuple[Path, dict[str, Any]]] = []
    for directory in directories:
        definition_path = directory / "场景定义.yaml"
        definition = _load_yaml(definition_path, errors)
        if definition:
            all_scenarios.append((directory, definition))
            errors.extend(_generation_artifact_errors(project_root, definition))
            errors.extend(_scenario_errors(project_root, directory, definition, discovery))
    if not all_scenarios:
        errors.extend(_generation_artifact_errors(project_root, {}))
    errors.extend(_cross_scenario_errors(all_scenarios))
    design_path = project_root / "discovery" / "design-rules.yaml"
    if design_path.is_file():
        try:
            design = yaml.safe_load(design_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            design = None
        if isinstance(design, dict) and isinstance(design.get("rules"), list):
            rule_ids = {
                str(rule.get("id"))
                for rule in design["rules"]
                if isinstance(rule, dict) and rule.get("id")
            }
            exclusions: Any = []
            for exclusion_path in (project_root / "discovery" / "exclusions.yaml", project_root / "exclusions.yaml"):
                try:
                    exclusion_document = yaml.safe_load(exclusion_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, yaml.YAMLError):
                    exclusion_document = None
                if isinstance(exclusion_document, dict) and isinstance(exclusion_document.get("exclusions"), list):
                    exclusions.extend(exclusion_document["exclusions"])
            excluded_rule_ids = {
                str(item.get("rule_id") or item.get("design_rule_id"))
                for item in exclusions
                if isinstance(item, dict)
                and (item.get("approved") is True or str(item.get("status", "")).casefold() == "approved")
                and (item.get("rule_id") or item.get("design_rule_id"))
            }
            referenced = {
                str(step.get("design_rule_id"))
                for _, definition in all_scenarios
                for step in definition.get("steps", [])
                if isinstance(step, dict) and step.get("design_rule_id")
            }
            missing = sorted(rule_ids - referenced - excluded_rule_ids)
            if missing:
                errors.append(_error(
                    design_path,
                    "design-scenario-coverage",
                    f"每条设计业务规则必须映射到场景步骤: {missing}",
                    "rules:",
                ))
    errors = list(dict.fromkeys(errors))
    if selected is None:
        return errors, all_scenarios, discovery
    selected_directories = _scenario_directories(project_root, selected, errors)
    selected_paths = set(selected_directories)
    return errors, [item for item in all_scenarios if item[0] in selected_paths], discovery


__all__ = [
    "CONTROL_NAMES", "CONTROL_STATUSES", "contract_errors", "generate_artifacts",
    "map_design_to_protocol", "parse_design_documents", "parse_protocol_documents",
]
