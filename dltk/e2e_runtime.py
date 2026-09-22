"""提供 E2E 配置预检、运行证据和可恢复控制能力。"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

import yaml


PLACEHOLDER_RE = re.compile(r"^\$\{([A-Z][A-Z0-9_]*)\}$")
SENSITIVE_FRAGMENTS = {
    "authorization", "cookie", "password", "secret", "token", "accesskey", "apikey",
    "credential", "connection", "dsn", "privatekey", "sshkey", "passphrase", "clientsecret", "auth",
}
SENSITIVE_TEXT_RE = re.compile(
    r"(?:\b(?:password|secret|token|authorization|auth|api[_-]?key|access[_-]?(?:key|token)|client[_-]?secret|private[_-]?key|ssh[_-]?key|passphrase|cookie|dsn)\s*[:=]\s*[^\s,;&]+"
    r"|--(?:password|secret|token|authorization|auth|api-key|access-key|access-token|client-secret|private-key|ssh-key|passphrase|cookie|dsn)(?:=|\s+)\S+)",
    re.IGNORECASE,
)
T = TypeVar("T")


class PreflightError(RuntimeError):
    """表示运行环境尚未满足安全执行条件。"""


def read_only_rpc(method: str, *, source_ref: str) -> Callable[[T], T]:
    """标记经业务源码锚点确认的只读 RPC 适配器，供静态门禁解析。"""

    if method not in {"READ", "RPC"} or not source_ref.strip():
        raise ValueError("只读 RPC 标记参数无效")

    def decorate(function: T) -> T:
        """保留函数本身，运行安全由静态门禁和超时共同约束。"""

        return function

    return decorate


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """递归合并映射，并只替换对应叶子值。"""

    result = dict(base)
    for key, value in override.items():
        current = result.get(key)
        result[key] = deep_merge(current, value) if isinstance(current, Mapping) and isinstance(value, Mapping) else value
    return result


def resolve_placeholders(
    value: Any,
    *,
    environ: Mapping[str, str] | None = None,
    path: str = "$",
    allow_missing: bool = False,
) -> Any:
    """仅解析完整环境变量占位符，并报告缺失路径而不泄露值。"""

    environment = os.environ if environ is None else environ
    if isinstance(value, str):
        match = PLACEHOLDER_RE.fullmatch(value)
        if not match:
            return value
        name = match.group(1)
        resolved = environment.get(name, "")
        if not resolved.strip():
            if allow_missing:
                return value
            raise PreflightError(f"缺少运行值: {path} ({name})")
        return resolved
    if isinstance(value, list):
        return [
            resolve_placeholders(item, environ=environment, path=f"{path}[{index}]", allow_missing=allow_missing)
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        return {
            key: resolve_placeholders(item, environ=environment, path=f"{path}.{key}", allow_missing=allow_missing)
            for key, item in value.items()
        }
    return value


def _load_yaml(path: Path) -> dict[str, Any]:
    """读取必须为映射的 YAML 文档。"""

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise PreflightError(f"配置必须是映射: {path}")
    return document


def _has_write_step(definition: Mapping[str, Any]) -> bool:
    """判断场景是否包含会产生副作用的步骤。"""

    return any(
        isinstance(step, Mapping)
        and step.get("side_effect") == "write"
        and step.get("status", "executable") == "executable"
        for step in definition.get("steps", [])
    )


def _required_control(definition: Mapping[str, Any], name: str) -> bool:
    """判断场景是否计划使用指定危险控制。"""

    controls = definition.get("controls", {})
    control = controls.get(name, {}) if isinstance(controls, Mapping) else {}
    active_actions = {
        str(step.get("action"))
        for step in definition.get("steps", [])
        if isinstance(step, Mapping)
        and step.get("control") == name
        and step.get("status", "executable") == "executable"
    }
    legacy_active = any(
        isinstance(step, Mapping)
        and step.get("control") == name
        and "status" not in step
        and not isinstance(step.get("action"), str)
        for step in definition.get("steps", [])
    )
    planned = {str(item) for item in control.get("planned_use", [])} if isinstance(control, Mapping) else set()
    return (
        isinstance(control, Mapping)
        and control.get("status") == "usable"
        and ((legacy_active and bool(planned)) or bool(active_actions & planned))
    )


def _write_control(definition: Mapping[str, Any], name: str) -> bool:
    """判断指定控制是否承担场景写步骤。"""

    return _required_control(definition, name) and any(
        isinstance(step, Mapping) and step.get("control") == name and step.get("side_effect") == "write"
        for step in definition.get("steps", [])
    )


def _protected_environment_name(value: Any) -> bool:
    """Reject conventional production identifiers independently of YAML flags."""

    return isinstance(value, str) and re.search(
        r"(?:^|[-_])(?:prod|production|prd|live)(?:$|[-_])",
        value,
        re.IGNORECASE,
    ) is not None


def preflight(project_root: Path, scenario_name: str, *, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """在任何副作用前解析环境、业务数据、隔离边界和控制授权。"""

    environment = os.environ if environ is None else environ
    project_root = project_root.resolve()
    if Path(scenario_name).name != scenario_name or scenario_name in {".", ".."}:
        raise PreflightError("场景名不能是路径")
    scenario_root = project_root / "scenarios" / scenario_name
    definition = _load_yaml(scenario_root / "场景定义.yaml")
    status = definition.get("meta", {}).get("status")
    partial = status == "pending_environment" and any(
        isinstance(step, Mapping) and step.get("status") == "executable"
        for step in definition.get("steps", [])
    )
    if status != "ready" and not partial:
        raise PreflightError(f"场景状态不是 ready: {status}")

    runtime = _load_yaml(project_root / "config" / "config.yaml")
    active = runtime.get("active_environment")
    if not isinstance(active, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", active):
        raise PreflightError("active_environment 无效")
    if _protected_environment_name(active):
        raise PreflightError("生产或在线环境标识禁止执行 E2E")
    environment_path = project_root / "config" / "environments" / f"{active}.yaml"
    if not environment_path.is_file():
        raise PreflightError(f"缺少激活环境配置: {environment_path}")
    selected = _load_yaml(environment_path)
    merged = deep_merge(runtime.get("defaults", {}), selected)
    committed_safety = runtime.get("defaults", {}).get("safety", {})
    if not isinstance(committed_safety, Mapping) or any(value is not False for value in committed_safety.values()):
        raise PreflightError("提交配置中的危险能力必须全部默认关闭")

    business_path = scenario_root / "业务数据.json"
    try:
        business = json.loads(business_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PreflightError(f"业务数据无法解析: {business_path}") from exc
    if not isinstance(business, dict) or not isinstance(business.get(active), dict):
        raise PreflightError(f"业务数据缺少激活环境根键: {active}")

    safety = selected.get("safety", {})
    if not isinstance(safety, Mapping):
        raise PreflightError("环境 safety 必须是映射")
    for field in ("test_environment", "side_effects_allowed", "protected"):
        if not isinstance(safety.get(field), bool):
            raise PreflightError(f"环境 safety.{field} 必须是布尔值")
    if safety.get("test_environment") is True and safety.get("protected") is True:
        raise PreflightError("测试环境与受保护环境标记不得同时为 true")
    potentially_writing = _has_write_step(definition) or any(
        _required_control(definition, name)
        for name in (
            "public_api", "test_or_admin_api", "mocks_and_faults", "dynamic_configuration",
            "scheduled_jobs", "messages", "database_control",
        )
    )
    if potentially_writing:
        if (
            safety.get("test_environment") is not True
            or safety.get("side_effects_allowed") is not True
            or safety.get("protected") is True
        ):
            raise PreflightError("目标环境未明确允许测试副作用")
        namespace = definition.get("isolation", {}).get("namespace")
        if not isinstance(namespace, str) or not namespace.strip():
            raise PreflightError("写场景缺少隔离命名空间")

    if _required_control(definition, "database_control"):
        if safety.get("protected") is True or safety.get("test_environment") is not True:
            raise PreflightError("数据库控制只允许明确的非受保护测试环境")
        if environment.get("E2E_ENABLE_DATABASE_CONTROL", "").lower() != "true":
            raise PreflightError("数据库控制未获得本次运行启用")
        authorization = environment.get("E2E_CONTROL_AUTHORIZATION_REF", "").strip()
        authorized_environment = environment.get("E2E_CONTROL_ENVIRONMENT", "").strip()
        if not authorization or authorized_environment != active:
            raise PreflightError("数据库控制缺少当前测试环境的显式授权引用")

    authorization_controls = {
        "dynamic_configuration": ("E2E_ENABLE_MUTABLE_CONFIGURATION", "E2E_MUTABLE_CONFIGURATION_AUTHORIZATION_REF"),
        "messages": ("E2E_ENABLE_MESSAGE_PUBLISH", "E2E_MESSAGE_PUBLISH_AUTHORIZATION_REF"),
        "test_or_admin_api": ("E2E_ENABLE_DANGEROUS_CONTROL", "E2E_DANGEROUS_CONTROL_AUTHORIZATION_REF"),
        "mocks_and_faults": ("E2E_ENABLE_DANGEROUS_CONTROL", "E2E_DANGEROUS_CONTROL_AUTHORIZATION_REF"),
        "scheduled_jobs": ("E2E_ENABLE_DANGEROUS_CONTROL", "E2E_DANGEROUS_CONTROL_AUTHORIZATION_REF"),
    }
    for control, (enabled_name, authorization_name) in authorization_controls.items():
        if not _required_control(definition, control):
            continue
        enabled = environment.get(enabled_name, "").lower() == "true"
        authorization = environment.get(authorization_name, "").strip()
        authorized_environment = environment.get("E2E_CONTROL_ENVIRONMENT", "").strip()
        if not enabled or not authorization or authorized_environment != active:
            raise PreflightError(f"危险控制 {control} 缺少当前测试环境的本次运行授权")

    integrations = definition.get("integrations", {})
    selected_configuration = {
        key: value for key, value in merged.items() if key not in {"services", "components"}
    }
    for collection in ("services", "components"):
        available = merged.get(collection, {})
        if not isinstance(available, Mapping):
            raise PreflightError(f"环境配置 {collection} 必须是映射")
        identifiers = integrations.get(collection, [])
        if collection == "components":
            identifiers = [item.get("id") for item in identifiers if isinstance(item, Mapping) and item.get("required") is True]
        missing = [identifier for identifier in identifiers if identifier not in available]
        if missing and not partial:
            raise PreflightError(f"环境配置缺少场景依赖: {collection}.{','.join(str(item) for item in missing)}")
        selected_configuration[collection] = {identifier: available[identifier] for identifier in identifiers if identifier in available}

    return {
        "active_environment": active,
        "configuration": resolve_placeholders(
            selected_configuration, environ=environment, path="$.configuration", allow_missing=partial
        ),
        "business_data": resolve_placeholders(
            business[active], environ=environment, path="$.business_data", allow_missing=partial
        ),
        "definition": definition,
        "partial": partial,
        "executable_steps": [
            str(step.get("id")) for step in definition.get("steps", [])
            if isinstance(step, Mapping) and step.get("status", "executable") == "executable"
        ],
    }


def _redact(value: Any, key: str = "") -> Any:
    """递归移除运行证据中的常见敏感字段。"""

    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    sensitive_key = any(fragment in normalized for fragment in SENSITIVE_FRAGMENTS)
    if sensitive_key and normalized != "connectionsource":
        return "<redacted>"
    if isinstance(value, dict):
        return {str(child_key): _redact(child, str(child_key)) for child_key, child in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if sensitive_key:
        return "<redacted>"
    if isinstance(value, str) and (
        re.search(r"://[^/@:\s]+:[^/@\s]+@", value)
        or re.search(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", value, re.IGNORECASE)
        or re.search(
            r"[?&](?:password|secret|token|auth|passphrase|private[_-]?key|ssh[_-]?key|client[_-]?secret|access[_-]?(?:token|key)|api[_-]?key)=[^&\s]+",
            value,
            re.IGNORECASE,
        )
        or SENSITIVE_TEXT_RE.search(value)
    ):
        return "<redacted>"
    return value


def _emit_event(kind: str, scenario: str, **details: Any) -> Path | None:
    """以单事件文件写入并发安全、可聚合的运行证据。"""

    root_text = os.environ.get("E2E_EVIDENCE_DIR", "").strip()
    if not root_text:
        return None
    run_id = os.environ.get("E2E_RUN_ID", "").strip()
    if not run_id:
        raise PreflightError("证据目录已启用但缺少 E2E_RUN_ID")
    root = Path(root_text)
    root.mkdir(parents=True, exist_ok=True)
    document = {
        "version": 1,
        "run_id": run_id,
        "kind": kind,
        "scenario": scenario,
        "details": _redact(details),
    }
    path = root / f"{uuid.uuid4().hex}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
    return path


def record_endpoint(
    scenario: str,
    *,
    phase: str,
    method: str,
    target_ref: str,
    status: int | str,
    summary: Any,
    verified: bool,
    step_id: str | None = None,
    protocol_ref: str | None = None,
    protocol_path: str | None = None,
) -> None:
    """记录一次真实接口调用的脱敏结果。"""

    normalized_method = method.upper()
    if phase not in {"smoke", "business"}:
        raise PreflightError(f"接口证据阶段无效: {phase}")
    if phase == "smoke" and normalized_method not in {"GET", "HEAD", "READ"}:
        raise PreflightError(f"只读冒烟禁止方法: {normalized_method}")
    if phase == "business" and (not str(step_id or "").strip() or not str(protocol_ref or "").strip()):
        raise PreflightError("业务接口证据必须绑定 step_id 和 protocol_ref")
    if not target_ref.strip():
        raise PreflightError("接口证据缺少非敏感目标引用")
    if verified is not True:
        raise PreflightError("接口响应尚未通过当前调用的显式校验")
    if normalized_method in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
        if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
            raise PreflightError("HTTP 接口状态必须是有效整数状态码")
        if status >= 500:
            raise PreflightError(f"接口返回服务端失败状态: {status}")
    _emit_event(
        "endpoint",
        scenario,
        phase=phase,
        method=normalized_method,
        target_ref=target_ref,
        status=status,
        summary=summary,
        verified=verified,
        step_id=step_id,
        protocol_ref=protocol_ref,
        protocol_path=protocol_path,
    )


def record_business_entry(scenario: str) -> None:
    """记录场景已经进入真实业务步骤。"""

    _emit_event("business_entered", scenario)


def _protocol_operation(project_root: Path, protocol_ref: str) -> dict[str, Any]:
    """Load one formally discovered operation without reading application code."""

    path = project_root.resolve() / "discovery" / "protocol-rules.yaml"
    try:
        document = _load_yaml(path)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise PreflightError(f"formal protocol artifact unavailable: {path}") from exc
    operations = document.get("operations", [])
    for operation in operations if isinstance(operations, list) else []:
        if isinstance(operation, Mapping) and str(operation.get("id")) == str(protocol_ref):
            return dict(operation)
    raise PreflightError(f"formal protocol operation not found: {protocol_ref}")


def _nested_value(value: Any, path: str) -> Any:
    """Resolve a dotted or bracketed value from scenario-owned data."""

    current = value
    for token in re.findall(r"[^.\[\]]+|\[\d+\]", str(path)):
        if token.startswith("["):
            current = current[int(token[1:-1])]
        elif isinstance(current, Mapping):
            current = current[token]
        else:
            raise KeyError(path)
    return current


def _service_base_url(configuration: Mapping[str, Any], operation: Mapping[str, Any]) -> str:
    """Resolve a service URL from preflight configuration without inventing one."""

    services = configuration.get("services", {})
    candidates: list[Any] = []
    service_name = operation.get("service")
    if isinstance(services, Mapping):
        if service_name and service_name in services:
            candidates.append(services[service_name])
        candidates.extend(value for key, value in services.items() if key != service_name)
    elif isinstance(services, list):
        candidates.extend(services)
    if not service_name and len(candidates) > 1:
        raise PreflightError("formal HTTP operation is not bound to a unique configured service")
    for item in candidates:
        if isinstance(item, str) and item.startswith(("http://", "https://")):
            parsed = urllib.parse.urlsplit(item)
            if parsed.username or parsed.password:
                raise PreflightError("service base URL must not contain embedded credentials")
            return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
        if isinstance(item, Mapping):
            for key in ("base_url", "url", "endpoint"):
                value = item.get(key)
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    parsed = urllib.parse.urlsplit(value)
                    if parsed.username or parsed.password:
                        raise PreflightError("service base URL must not contain embedded credentials")
                    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
    raise PreflightError("active environment has no resolved HTTP service base URL")


def _graphql_protocol_request(
    project_root: Path,
    scenario_context: Mapping[str, Any],
    operation: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    protocol_ref: str,
    step_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Execute a GraphQL operation through its configured HTTP endpoint."""

    configuration = scenario_context.get("configuration", {})
    configuration = configuration if isinstance(configuration, Mapping) else {}
    base_url = _service_base_url(configuration, operation)
    route = str(operation.get("path") or "")
    target = base_url + route
    arguments = operation.get("arguments", []) if isinstance(operation.get("arguments"), list) else []
    variables: dict[str, Any] = {}
    definitions: list[str] = []
    calls: list[str] = []
    for argument in arguments:
        if not isinstance(argument, Mapping):
            continue
        name = str(argument.get("name") or argument.get("path") or "")
        if not name:
            continue
        variables[name] = payload.get(name)
        graph_type = str(argument.get("type") or "String")
        definitions.append(f"${name}: {graph_type}")
        calls.append(f"{name}: ${name}")
    fields = [
        str(field.get("path") or field.get("name"))
        for field in operation.get("response_fields", [])
        if isinstance(field, Mapping) and (field.get("path") or field.get("name"))
    ]
    selection = " { " + " ".join(dict.fromkeys(fields)) + " }" if fields else ""
    operation_type = str(operation.get("operation_type") or "query").casefold()
    query = f"{operation_type}"
    if definitions:
        query += "(" + ", ".join(definitions) + ")"
    query += " { " + str(operation.get("method") or operation.get("id"))
    if calls:
        query += "(" + ", ".join(calls) + ")"
    query += selection + " }"
    request_body = json.dumps({"query": query, "variables": variables}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        target,
        data=request_body,
        method="POST",
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status = int(response.status)
            raw_body = response.read(2_000_000)
            response_headers = {str(key): str(value) for key, value in response.headers.items()}
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        raw_body = exc.read(2_000_000)
        response_headers = {str(key): str(value) for key, value in exc.headers.items()} if exc.headers else {}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PreflightError(f"protocol request failed: {type(exc).__name__}") from exc
    text_body = raw_body.decode("utf-8", errors="replace")
    try:
        parsed_body: Any = json.loads(text_body) if text_body else None
    except json.JSONDecodeError:
        parsed_body = text_body
    if not 200 <= status < 300:
        raise AssertionError(f"GraphQL protocol status {status} is not successful for {protocol_ref}")
    if isinstance(parsed_body, Mapping) and parsed_body.get("errors"):
        raise AssertionError(f"GraphQL protocol returned errors for {protocol_ref}")
    record_endpoint(
        str(scenario_context.get("scenario", scenario_context.get("name", "scenario"))),
        phase="business",
        method="POST",
        target_ref=urllib.parse.urlunsplit(urllib.parse.urlsplit(target)._replace(query="")),
        status=status,
        summary={"response_keys": sorted(parsed_body) if isinstance(parsed_body, Mapping) else type(parsed_body).__name__},
        verified=True,
        step_id=step_id,
        protocol_ref=protocol_ref,
        protocol_path=route or "/",
    )
    return {"status": status, "headers": response_headers, "body": parsed_body, "operation": operation, "target_ref": target}


def invoke_protocol(
    project_root: Path,
    scenario_context: Mapping[str, Any],
    protocol_ref: str,
    payload: Mapping[str, Any] | None = None,
    *,
    step_id: str,
    phase: str = "business",
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Execute one confirmed HTTP operation and return redaction-safe evidence."""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    operation = _protocol_operation(project_root, protocol_ref)
    operation_kind = str(operation.get("kind", "http")).casefold()
    if operation_kind == "graphql":
        return _graphql_protocol_request(
            project_root,
            scenario_context,
            operation,
            payload if isinstance(payload, Mapping) else {},
            protocol_ref=protocol_ref,
            step_id=step_id,
            timeout_seconds=timeout_seconds,
        )
    if operation_kind != "http":
        raise PreflightError(f"protocol adapter unavailable for operation kind: {operation.get('kind')}")
    method = str(operation.get("method", "GET")).upper()
    route = str(operation.get("path", ""))
    if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"} or not route.startswith("/"):
        raise PreflightError(f"unsupported HTTP operation: {method} {route}")
    context_configuration = scenario_context.get("configuration", {})
    configuration = context_configuration if isinstance(context_configuration, Mapping) else {}
    base_url = _service_base_url(configuration, operation)
    data: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
    headers: dict[str, str] = {"Accept": "application/json"}
    raw_headers = configuration.get("headers")
    if isinstance(raw_headers, Mapping):
        headers.update({str(key): str(value) for key, value in raw_headers.items() if isinstance(value, (str, int, float))})
    query: dict[str, str] = {}
    for parameter in operation.get("parameters", []) if isinstance(operation.get("parameters"), list) else []:
        if not isinstance(parameter, Mapping):
            continue
        name = str(parameter.get("name", ""))
        if not name:
            continue
        try:
            value = _nested_value(data, str(parameter.get("path") or name))
        except (KeyError, IndexError, TypeError, ValueError):
            if parameter.get("required") is True:
                raise PreflightError(f"missing required protocol parameter: {name}")
            continue
        location = str(parameter.get("in") or "query")
        if location == "query":
            query[name] = str(value)
        elif location == "header":
            headers[name] = str(value)
        elif location == "path":
            route = route.replace("{" + name + "}", urllib.parse.quote(str(value), safe=""))
    body_value: Any = data.get("body", data)
    if method in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        body_bytes = None
    else:
        body_bytes = json.dumps(body_value, ensure_ascii=False).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    target = base_url + route
    if query:
        target += "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(target, data=body_bytes, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status = int(response.status)
            raw_body = response.read(2_000_000)
            response_headers = {str(key): str(value) for key, value in response.headers.items()}
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        raw_body = exc.read(2_000_000)
        response_headers = {str(key): str(value) for key, value in exc.headers.items()} if exc.headers else {}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PreflightError(f"protocol request failed: {type(exc).__name__}") from exc
    text_body = raw_body.decode("utf-8", errors="replace")
    try:
        parsed_body: Any = json.loads(text_body) if text_body else None
    except json.JSONDecodeError:
        parsed_body = text_body
    declared_codes = [str(value).upper() for value in operation.get("status_codes", [])]
    verified = (
        any(
            code == str(status)
            or code in {"DEFAULT", "XX"}
            or len(code) == 3 and code[0].isdigit() and code[1:] == "XX" and str(status).startswith(code[0])
            for code in declared_codes
        )
        if declared_codes else 200 <= status < 300
    )
    if not verified:
        raise AssertionError(f"protocol status {status} is not declared for {protocol_ref}")
    record_endpoint(
        str(scenario_context.get("scenario", scenario_context.get("name", "scenario"))),
        phase=phase,
        method=method,
        target_ref=urllib.parse.urlunsplit(urllib.parse.urlsplit(target)._replace(query="")),
        status=status,
        summary={"response_keys": sorted(parsed_body) if isinstance(parsed_body, Mapping) else type(parsed_body).__name__},
        verified=True,
        step_id=step_id,
        protocol_ref=protocol_ref,
        protocol_path=route,
    )
    return {"status": status, "headers": response_headers, "body": parsed_body, "operation": operation, "target_ref": target}


def assert_business_expectations(result: Mapping[str, Any], expectations: Sequence[str]) -> bool:
    """Evaluate simple design expressions against a real protocol result."""

    body = result.get("body") if isinstance(result, Mapping) else None
    for raw in expectations:
        expression = str(raw).strip()
        match = re.fullmatch(r"(?:response\.)?([A-Za-z_][\w.]*)\s*(?:==|=)\s*['\"]?([^'\"]+)['\"]?", expression)
        if not match:
            raise AssertionError(f"business expectation requires an adapter: {expression}")
        field, expected = match.groups()
        actual: Any
        if field in {"status", "http_status"} and expected.isdigit():
            actual = result.get("status")
        else:
            try:
                actual = _nested_value(body, field)
            except (KeyError, IndexError, TypeError, ValueError):
                try:
                    actual = _nested_value(body.get("data"), field) if isinstance(body, Mapping) else None
                except (KeyError, IndexError, TypeError, ValueError):
                    raise AssertionError(f"business expectation field missing: {field}")
        if str(actual) != expected:
            raise AssertionError(f"business expectation failed: {expression}; actual={_redact(actual)!r}")
    return True


def cleanup_scenario(project_root: Path, scenario_context: Mapping[str, Any], scenario: str) -> dict[str, Any]:
    """Run declared protocol cleanup and emit restoration evidence."""

    definition = scenario_context.get("definition", {})
    isolation = definition.get("isolation", {}) if isinstance(definition, Mapping) else {}
    resources = isolation.get("owned_resources", []) if isinstance(isolation, Mapping) else []
    if not resources:
        _emit_event("restoration", scenario, status="passed", resources=[])
        return {"status": "passed", "resources": []}
    if isinstance(scenario_context, dict) and scenario_context.get("_cleanup_done") is True:
        _emit_event("restoration", scenario, status="passed", resources=[], repeated=True)
        return {"status": "passed", "resources": [], "repeated": True}

    restored: list[str] = []
    try:
        from .e2e_data import generate_request_data

        namespace = str(isolation.get("namespace") or scenario)
        for resource in resources:
            if not isinstance(resource, Mapping):
                raise PreflightError("scenario-owned resource declaration is invalid")
            identity = str(resource.get("identity", "")).strip()
            action = str(resource.get("cleanup", "")).strip()
            if not identity or not action.startswith("protocol:"):
                raise PreflightError("scenario-owned mutable resources require a formal protocol cleanup adapter")
            protocol_ref = action.split(":", 1)[1].strip()
            operation = _protocol_operation(project_root, protocol_ref)
            payload = generate_request_data(operation, namespace=namespace)
            result = invoke_protocol(
                project_root,
                scenario_context,
                protocol_ref,
                payload,
                step_id=f"cleanup_{identity}",
                phase="business",
            )
            restored.append(identity)
            _emit_event(
                "restoration_verification",
                scenario,
                resource=identity,
                protocol_ref=protocol_ref,
                status=result.get("status"),
            )
    except BaseException as exc:
        _emit_event("restoration", scenario, status="failed", resources=restored, error=str(exc))
        raise
    if isinstance(scenario_context, dict):
        scenario_context["_cleanup_done"] = True
    _emit_event("restoration", scenario, status="passed", resources=restored)
    return {"status": "passed", "resources": restored}


def record_step(
    scenario: str,
    *,
    step_id: str,
    status: str,
    reason: str,
    evidence: Sequence[str] = (),
) -> None:
    """记录单个业务步骤的执行或阻塞分类。"""

    allowed = {
        "executable", "environment_missing", "authorization_missing", "control_gap", "product_gap", "runtime_failure",
    }
    if not isinstance(step_id, str) or not step_id.strip() or status not in allowed:
        raise PreflightError("步骤证据 ID 或状态无效")
    if not isinstance(reason, str) or not reason.strip():
        raise PreflightError("步骤证据必须包含原因")
    if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes)) or not all(
        isinstance(item, str) and item.strip() for item in evidence
    ):
        raise PreflightError("步骤证据引用必须是字符串序列")
    _emit_event("step", scenario, step_id=step_id, status=status, reason=reason, evidence=list(evidence))


def report_control_gap(scenario: str, *, step_id: str, reason: str, evidence: Sequence[str] = ()) -> dict[str, Any]:
    """Record a precise non-executable step without claiming business success."""

    record_step(scenario, step_id=step_id, status="control_gap", reason=reason, evidence=evidence)
    return {"status": "control_gap", "step_id": step_id}


@contextmanager
def step_guard(
    scenario: str,
    *,
    step_id: str,
    evidence: Sequence[str],
) -> Iterator[None]:
    """将真实步骤成功或异常稳定归类为逐步骤运行证据。"""

    try:
        yield
    except BaseException as exc:
        record_step(
            scenario,
            step_id=step_id,
            status="runtime_failure",
            reason=f"{type(exc).__name__}: {exc}",
            evidence=evidence,
        )
        raise
    else:
        record_step(
            scenario,
            step_id=step_id,
            status="executable",
            reason="步骤已真实执行并通过断言",
            evidence=evidence,
        )


def poll_until(
    observe: Callable[[], T],
    accept: Callable[[T], bool],
    *,
    timeout_seconds: float,
    interval_seconds: float,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> T:
    """使用单调截止时间有限轮询，并在超时时保留最后观察值。"""

    if timeout_seconds <= 0 or interval_seconds <= 0:
        raise ValueError("轮询超时和间隔必须为正数")
    deadline = clock() + timeout_seconds
    last: T
    while True:
        last = observe()
        if accept(last):
            return last
        remaining = deadline - clock()
        if remaining <= 0:
            raise AssertionError(f"有限轮询超时，最后观察值: {_redact(last)!r}")
        sleeper(min(interval_seconds, remaining))


def record_control(
    scenario: str,
    *,
    kind: str,
    action: str,
    correlation_ref: str,
    side_effect: str,
    step_id: str | None = None,
    protocol_ref: str | None = None,
    protocol_path: str | None = None,
) -> None:
    """记录本次场景实际使用的受控能力。"""

    if not all(isinstance(value, str) and value.strip() for value in (kind, action, correlation_ref)):
        raise PreflightError("控制证据字段不得为空")
    if side_effect not in {"read", "write"}:
        raise PreflightError("控制证据 side_effect 必须是 read 或 write")
    details: dict[str, Any] = {
        "control_kind": kind,
        "action": action,
        "correlation_ref": correlation_ref,
        "side_effect": side_effect,
    }
    if any(value is not None for value in (step_id, protocol_ref, protocol_path)):
        if not all(isinstance(value, str) and value.strip() for value in (step_id, protocol_ref)):
            raise PreflightError("协议控制证据必须绑定 step_id 和 protocol_ref")
        if protocol_path is not None and (not isinstance(protocol_path, str) or not protocol_path.startswith("/")):
            raise PreflightError("协议控制证据的 HTTP path 必须以 / 开头")
        details.update(step_id=step_id, protocol_ref=protocol_ref, protocol_path=protocol_path)
    _emit_event("control", scenario, **details)


def _record_restoration(
    scenario: str,
    *,
    status: str,
    resources: list[str],
    body_error: BaseException | None,
    error: BaseException | None = None,
) -> None:
    """写入恢复证据，且证据 I/O 失败时仍保留已有业务异常。"""

    details: dict[str, Any] = {"status": status, "resources": resources}
    if error is not None:
        details["error"] = str(error)
    try:
        _emit_event("restoration", scenario, **details)
    except BaseException as evidence_error:
        if body_error is None:
            raise
        if hasattr(body_error, "add_note"):
            body_error.add_note(f"恢复证据写入失败: {evidence_error}")


@contextmanager
def restoration_guard(
    scenario: str,
    *,
    scenario_context: Mapping[str, Any],
    restore: Callable[[str], None],
    verify: Callable[[str], bool],
) -> Iterator[None]:
    """按场景契约派生资源并保证恢复执行，同时保留原始异常。"""

    definition = scenario_context.get("definition", {})
    isolation = definition.get("isolation", {}) if isinstance(definition, Mapping) else {}
    declared = isolation.get("owned_resources", []) if isinstance(isolation, Mapping) else []
    resources = [
        str(item.get("identity")) for item in declared
        if isinstance(item, Mapping) and isinstance(item.get("identity"), str) and item["identity"].strip()
    ]
    mutable_controls = isolation.get("mutable_controls", []) if isinstance(isolation, Mapping) else []
    if not resources or not all(isinstance(item, str) and item.strip() for item in resources):
        raise PreflightError("恢复资源必须由场景契约声明的精确拥有资源派生")
    if not isinstance(mutable_controls, list) or not set(str(item) for item in mutable_controls) <= set(resources):
        raise PreflightError("每个可变控制都必须由 owned_resources 纳入恢复")
    body_error: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        body_error = exc
        raise
    finally:
        try:
            failures: list[str] = []
            for resource in resources:
                try:
                    restore(resource)
                    if verify(resource) is not True:
                        raise AssertionError(f"恢复校验未通过: {resource}")
                except BaseException as resource_error:
                    failures.append(f"{resource}: {resource_error}")
            if failures:
                raise RuntimeError("; ".join(failures))
            _record_restoration(scenario, status="passed", resources=resources, body_error=body_error)
        except BaseException as cleanup_error:
            _record_restoration(
                scenario,
                status="failed",
                resources=resources,
                body_error=body_error or cleanup_error,
                error=cleanup_error,
            )
            if body_error is None:
                raise
            if hasattr(body_error, "add_note"):
                body_error.add_note(f"恢复失败: {cleanup_error}")


@contextmanager
def controlled_database_state(
    scenario: str,
    *,
    scenario_context: Mapping[str, Any],
    snapshot: Callable[[str], T],
    mutate: Callable[[str], int],
    restore: Callable[[T, str], int],
    verify_restored: Callable[[T, str], bool],
) -> Iterator[T]:
    """执行单个精确行数控制；旧场景通过多操作实现共享同一恢复语义。"""

    definition = scenario_context.get("definition", {})
    controls = definition.get("controls", {}) if isinstance(definition, Mapping) else {}
    safety = controls.get("database_control", {}).get("safety", {}) if isinstance(controls, Mapping) else {}
    operation = {
        "id": "bounded_mutation",
        "selector_ref": safety.get("exact_selector") if isinstance(safety, Mapping) else None,
        "snapshot": snapshot,
        "mutate": mutate,
        "restore": restore,
        "verify_restored": verify_restored,
    }
    with controlled_database_operations(
        scenario,
        scenario_context=scenario_context,
        operations=[operation],
    ) as snapshots:
        yield snapshots["bounded_mutation"]


@contextmanager
def controlled_database_operations(
    scenario: str,
    *,
    scenario_context: Mapping[str, Any],
    operations: Sequence[Mapping[str, Any]],
) -> Iterator[dict[str, Any]]:
    """按依赖顺序执行精确单行数据库操作，并按逆序恢复。"""

    active_environment = scenario_context.get("active_environment")
    definition = scenario_context.get("definition", {})
    controls = definition.get("controls", {}) if isinstance(definition, Mapping) else {}
    database_control = controls.get("database_control", {}) if isinstance(controls, Mapping) else {}
    safety = database_control.get("safety", {}) if isinstance(database_control, Mapping) else {}
    if not isinstance(safety, Mapping) or database_control.get("status") != "usable":
        raise PreflightError("场景契约未声明可用数据库控制")
    expected_environment = safety.get("target_environment")
    contract_operations = safety.get("operations") if isinstance(safety, Mapping) else None
    if not isinstance(contract_operations, list) or not contract_operations:
        contract_operations = [{
            "id": "bounded_mutation",
            "exact_selector": safety.get("exact_selector") if isinstance(safety, Mapping) else None,
            "expected_rows": safety.get("expected_rows") if isinstance(safety, Mapping) else None,
        }]
    authorization = os.environ.get("E2E_CONTROL_AUTHORIZATION_REF", "").strip()
    enabled = os.environ.get("E2E_ENABLE_DATABASE_CONTROL", "").lower() == "true"
    authorized_environment = os.environ.get("E2E_CONTROL_ENVIRONMENT", "").strip()
    if not enabled or not authorization or active_environment != expected_environment or authorized_environment != active_environment:
        raise PreflightError("数据库控制未获得当前测试环境的显式授权")
    if _protected_environment_name(active_environment) or _protected_environment_name(expected_environment):
        raise PreflightError("生产或在线环境禁止数据库控制")

    callbacks = {str(item.get("id")): item for item in operations if isinstance(item, Mapping)}
    ordered: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    completed_ids: set[str] = set()
    for contract in contract_operations:
        if not isinstance(contract, Mapping):
            raise PreflightError("数据库控制操作契约无效")
        operation_id = str(contract.get("id", ""))
        callback = callbacks.get(operation_id)
        if callback is None:
            raise PreflightError(f"缺少数据库控制回调: {operation_id}")
        depends_on = contract.get("depends_on", [])
        if not isinstance(depends_on, list) or not set(str(item) for item in depends_on).issubset(completed_ids):
            raise PreflightError(f"数据库控制操作未按依赖顺序排列: {operation_id}")
        selector_ref = contract.get("exact_selector")
        if not isinstance(selector_ref, str) or not selector_ref.strip():
            selector_ref = callback.get("selector_ref")
        if not isinstance(selector_ref, str) or not selector_ref.strip():
            raise PreflightError(f"数据库控制操作缺少精确选择器: {operation_id}")
        if contract.get("expected_rows", 1) != 1 or isinstance(contract.get("expected_rows", 1), bool):
            raise PreflightError(f"数据库控制操作 expected_rows 必须精确为 1: {operation_id}")
        required_callbacks = ("snapshot", "mutate", "restore", "verify_restored")
        if isinstance(safety.get("operations"), list):
            required_callbacks += ("verify",)
        for name in required_callbacks:
            if not callable(callback.get(name)):
                raise PreflightError(f"数据库控制回调无效: {operation_id}.{name}")
        ordered.append((contract, callback))
        completed_ids.add(operation_id)

    snapshots: dict[str, Any] = {}
    successful: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    body_error: BaseException | None = None
    try:
        for contract, callback in ordered:
            operation_id = str(contract.get("id"))
            selector_ref = str(contract.get("exact_selector") or callback.get("selector_ref"))
            original = callback["snapshot"](selector_ref)
            snapshots[operation_id] = original
            successful.append((contract, callback))
            actual = callback["mutate"](selector_ref)
            if not isinstance(actual, int) or isinstance(actual, bool) or actual != 1:
                raise AssertionError(f"数据库控制影响行数不符: operation={operation_id}, expected=1, actual={actual}")
            if callback.get("verify") is not None and callback["verify"](selector_ref) is not True:
                raise AssertionError(f"数据库控制结果校验失败: {operation_id}")
            record_control(
                scenario,
                kind="database_control",
                action=operation_id,
                correlation_ref=selector_ref,
                side_effect="write",
            )
        yield snapshots
    except BaseException as exc:
        body_error = exc
        raise
    finally:
        try:
            restoration_errors: list[str] = []
            restored_resources: list[str] = []
            for contract, callback in reversed(successful):
                operation_id = str(contract.get("id"))
                selector_ref = str(contract.get("exact_selector") or callback.get("selector_ref"))
                try:
                    restored_rows = callback["restore"](snapshots[operation_id], selector_ref)
                    if (
                        not isinstance(restored_rows, int)
                        or isinstance(restored_rows, bool)
                        or restored_rows != 1
                        or callback["verify_restored"](snapshots[operation_id], selector_ref) is not True
                    ):
                        raise AssertionError(f"数据库恢复校验失败: expected=1, actual={restored_rows}")
                    restored_resources.append(selector_ref)
                except BaseException as cleanup_error:
                    restoration_errors.append(f"{operation_id}: {cleanup_error}")
            if restoration_errors:
                raise RuntimeError("; ".join(restoration_errors))
            _record_restoration(scenario, status="passed", resources=restored_resources, body_error=body_error)
        except BaseException as cleanup_error:
            _record_restoration(
                scenario,
                status="failed",
                resources=[str(contract.get("exact_selector") or callback.get("selector_ref")) for contract, callback in successful],
                body_error=body_error or cleanup_error,
                error=cleanup_error,
            )
            if body_error is None:
                raise
            if hasattr(body_error, "add_note"):
                body_error.add_note(f"数据库恢复失败: {cleanup_error}")
