"""提供 E2E 配置预检、运行证据和可恢复控制能力。"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
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


def resolve_placeholders(value: Any, *, environ: Mapping[str, str] | None = None, path: str = "$") -> Any:
    """仅解析完整环境变量占位符，并报告缺失路径而不泄露值。"""

    environment = os.environ if environ is None else environ
    if isinstance(value, str):
        match = PLACEHOLDER_RE.fullmatch(value)
        if not match:
            return value
        name = match.group(1)
        resolved = environment.get(name, "")
        if not resolved.strip():
            raise PreflightError(f"缺少运行值: {path} ({name})")
        return resolved
    if isinstance(value, list):
        return [resolve_placeholders(item, environ=environment, path=f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, dict):
        return {
            key: resolve_placeholders(item, environ=environment, path=f"{path}.{key}")
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

    return any(isinstance(step, Mapping) and step.get("side_effect") == "write" for step in definition.get("steps", []))


def _required_control(definition: Mapping[str, Any], name: str) -> bool:
    """判断场景是否计划使用指定危险控制。"""

    controls = definition.get("controls", {})
    control = controls.get(name, {}) if isinstance(controls, Mapping) else {}
    return isinstance(control, Mapping) and control.get("status") == "usable" and bool(control.get("planned_use"))


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
    if status != "ready":
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
        if missing:
            raise PreflightError(f"环境配置缺少场景依赖: {collection}.{','.join(str(item) for item in missing)}")
        selected_configuration[collection] = {identifier: available[identifier] for identifier in identifiers}

    return {
        "active_environment": active,
        "configuration": resolve_placeholders(selected_configuration, environ=environment, path="$.configuration"),
        "business_data": resolve_placeholders(business[active], environ=environment, path="$.business_data"),
        "definition": definition,
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
) -> None:
    """记录一次真实接口调用的脱敏结果。"""

    normalized_method = method.upper()
    if phase not in {"smoke", "business"}:
        raise PreflightError(f"接口证据阶段无效: {phase}")
    if phase == "smoke" and normalized_method not in {"GET", "HEAD", "READ"}:
        raise PreflightError(f"只读冒烟禁止方法: {normalized_method}")
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
    )


def record_business_entry(scenario: str) -> None:
    """记录场景已经进入真实业务步骤。"""

    _emit_event("business_entered", scenario)


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


def record_control(scenario: str, *, kind: str, action: str, correlation_ref: str, side_effect: str) -> None:
    """记录本次场景实际使用的受控能力。"""

    if not all(isinstance(value, str) and value.strip() for value in (kind, action, correlation_ref)):
        raise PreflightError("控制证据字段不得为空")
    if side_effect not in {"read", "write"}:
        raise PreflightError("控制证据 side_effect 必须是 read 或 write")
    _emit_event(
        "control",
        scenario,
        control_kind=kind,
        action=action,
        correlation_ref=correlation_ref,
        side_effect=side_effect,
    )


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
    """执行精确行数控制并在所有退出路径恢复数据库原值。"""

    active_environment = scenario_context.get("active_environment")
    definition = scenario_context.get("definition", {})
    controls = definition.get("controls", {}) if isinstance(definition, Mapping) else {}
    database_control = controls.get("database_control", {}) if isinstance(controls, Mapping) else {}
    safety = database_control.get("safety", {}) if isinstance(database_control, Mapping) else {}
    if not isinstance(safety, Mapping) or database_control.get("status") != "usable":
        raise PreflightError("场景契约未声明可用数据库控制")
    expected_environment = safety.get("target_environment")
    expected_rows = safety.get("expected_rows")
    selector_ref = safety.get("exact_selector")
    if not isinstance(selector_ref, str) or not selector_ref.strip():
        raise PreflightError("数据库控制缺少场景契约派生的精确选择器")
    authorization = os.environ.get("E2E_CONTROL_AUTHORIZATION_REF", "").strip()
    enabled = os.environ.get("E2E_ENABLE_DATABASE_CONTROL", "").lower() == "true"
    authorized_environment = os.environ.get("E2E_CONTROL_ENVIRONMENT", "").strip()
    if not enabled or not authorization or active_environment != expected_environment or authorized_environment != active_environment:
        raise PreflightError("数据库控制未获得当前测试环境的显式授权")
    if not isinstance(expected_rows, int) or isinstance(expected_rows, bool) or expected_rows != 1:
        raise PreflightError("expected_rows 必须精确为 1")
    if _protected_environment_name(active_environment) or _protected_environment_name(expected_environment):
        raise PreflightError("生产或在线环境禁止数据库控制")

    original = snapshot(selector_ref)
    body_error: BaseException | None = None
    try:
        actual = mutate(selector_ref)
        if not isinstance(actual, int) or isinstance(actual, bool) or actual != expected_rows:
            raise AssertionError(f"数据库控制影响行数不符: expected={expected_rows}, actual={actual}")
        record_control(
            scenario,
            kind="database_control",
            action="bounded_mutation",
            correlation_ref=selector_ref,
            side_effect="write",
        )
        yield original
    except BaseException as exc:
        body_error = exc
        raise
    finally:
        try:
            restored_rows = restore(original, selector_ref)
            if (
                not isinstance(restored_rows, int)
                or isinstance(restored_rows, bool)
                or restored_rows != expected_rows
                or verify_restored(original, selector_ref) is not True
            ):
                raise AssertionError(
                    f"数据库恢复校验失败: expected={expected_rows}, actual={restored_rows}"
                )
            _record_restoration(scenario, status="passed", resources=[selector_ref], body_error=body_error)
        except BaseException as cleanup_error:
            _record_restoration(
                scenario,
                status="failed",
                resources=[selector_ref],
                body_error=body_error or cleanup_error,
                error=cleanup_error,
            )
            if body_error is None:
                raise
            if hasattr(body_error, "add_note"):
                body_error.add_note(f"数据库恢复失败: {cleanup_error}")
