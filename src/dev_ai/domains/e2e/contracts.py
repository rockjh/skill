"""Scenario control, ownership, readiness, and isolation contracts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from ...core.schema import (
    E2E_CONTROL_NAMES,
    E2E_CONTROL_STATUSES,
    E2E_GENERATION_MODES,
    E2E_SCENARIO_STATUSES,
    SCENARIO_REQUIRED,
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
    discovery_errors,
)


SCENARIO_KEYS = set(SCENARIO_REQUIRED)


SCENARIO_STATUSES = set(E2E_SCENARIO_STATUSES)


GENERATION_MODES = set(E2E_GENERATION_MODES)


CONTROL_NAMES = E2E_CONTROL_NAMES


CONTROL_STATUSES = set(E2E_CONTROL_STATUSES)


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
            if isinstance(step, dict) and (step.get("control") == name or name == "observability")
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
            if isinstance(step, dict) and step.get("control") == name
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
                if not isinstance(safety, dict) or set(safety) != safety_keys:
                    errors.append(_error(path, "database-control-safety", "database_control.safety 键集合无效", "safety:"))
                elif (
                    safety.get("authorization_required") is not True
                    or not isinstance(safety.get("expected_rows"), int)
                    or isinstance(safety.get("expected_rows"), bool)
                    or safety["expected_rows"] != 1
                ):
                    errors.append(_error(path, "database-control-safety", "数据库控制必须要求授权且 expected_rows 必须精确为 1", "authorization_required"))
                elif not all(isinstance(safety.get(field), str) and safety[field].strip() for field in safety_keys - {"authorization_required", "expected_rows"}):
                    errors.append(_error(path, "database-control-safety", "数据库控制 safety 字符串字段不得为空", "safety:"))
                elif safety.get("purpose") not in {"preparation", "time_advance", "expiry_simulation", "state_trigger"}:
                    errors.append(_error(path, "database-control-purpose", "控制 SQL purpose 只能用于准备、时间推进、过期模拟或状态触发", "purpose:"))
            elif safety is not None:
                errors.append(_error(path, "database-control-safety", "未使用数据库控制时 safety 必须为 null", "safety:"))
    decision = controls.get("decision")
    if not isinstance(decision, dict) or set(decision) != {"safe_control_path", "blockers"} or not isinstance(decision.get("safe_control_path"), bool) or not _strings(decision.get("blockers"), nonempty=False):
        errors.append(_error(path, "control-decision", "控制决策结构无效", "decision:"))
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
    if _exact_keys(path, meta, {"id", "name", "status", "actor"}, "scenario-meta", errors):
        if not isinstance(meta.get("id"), str) or not ID_RE.fullmatch(meta["id"]):
            errors.append(_error(path, "scenario-id", "场景 ID 格式无效", "id:"))
        if meta.get("status") not in SCENARIO_STATUSES:
            errors.append(_error(path, "scenario-status", "场景状态无效", "status:"))
        for field in ("name", "actor"):
            if not isinstance(meta.get(field), str) or not meta[field].strip():
                errors.append(_error(path, "scenario-meta-value", f"{field} 不得为空", f"{field}:"))

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
        if not isinstance(step, dict) or set(step) not in ({"id", "action", "control", "side_effect", "expect"}, {"id", "action", "control", "side_effect", "data_ref", "expect"}):
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
        if "data_ref" in step and (not isinstance(step["data_ref"], str) or not step["data_ref"].startswith("业务数据.json#/")):
            errors.append(_error(path, "data-ref", f"data_ref 无效: {step.get('data_ref')}", "data_ref:"))

    source_symbols = {
        f"{item.get('repo')}#{anchor}"
        for item in definition.get("source", []) if isinstance(item, dict)
        for anchor in item.get("anchors", [])
    }
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
            if not isinstance(control, dict) or control.get("status") != "usable":
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
            owned_selectors = set(str(item) for item in isolation.get("correlation_keys", []))
            owned_selectors.update(
                str(item.get("identity")) for item in isolation.get("owned_resources", []) if isinstance(item, dict)
            )
            if safety.get("exact_selector") not in owned_selectors:
                errors.append(_error(path, "database-selector-owned", "控制 SQL exact_selector 必须属于当前场景隔离契约", "exact_selector:"))
            if safety.get("consumer_source") not in source_symbols:
                errors.append(_error(path, "database-consumer-source", "consumer_source 必须解析到场景源码锚点", "consumer_source:"))
            elif not _source_reference_semantic(
                project_root,
                discovery,
                str(safety.get("consumer_source")),
                {"database", "jobs"},
            ):
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
            if not isinstance(step, dict) or "data_ref" not in step:
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
                        f"data_ref 在环境 {environment} 中全部依赖变量注入；应按源码契约保留可构造字面值或在运行期生成随机值: {step['data_ref']}",
                    ))
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
            errors.extend(_scenario_errors(project_root, directory, definition, discovery))
    errors.extend(_cross_scenario_errors(all_scenarios))
    if selected is None:
        return errors, all_scenarios, discovery
    selected_directories = _scenario_directories(project_root, selected, errors)
    selected_paths = set(selected_directories)
    return errors, [item for item in all_scenarios if item[0] in selected_paths], discovery


__all__ = ["CONTROL_NAMES", "CONTROL_STATUSES", "contract_errors"]
