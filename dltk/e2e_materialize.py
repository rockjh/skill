"""Materialize isolated, source-backed E2E scenario assets."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from .e2e_context import build_context
from .e2e_data import generate_scenario_data


CONTROL_BY_KIND = {
    "public_api": "public_api",
    "test_or_admin_api": "test_or_admin_api",
    "database_control": "database_control",
    "messages": "messages",
    "scheduled_jobs": "scheduled_jobs",
    "mocks_and_faults": "mocks_and_faults",
    "dynamic_configuration": "dynamic_configuration",
    "existing_test_data": "public_api",
    "database_read": "database_read",
    "observability": "observability",
}
CANDIDATE_KINDS = tuple(CONTROL_BY_KIND)
CONTROL_NAMES = (
    "public_api", "test_or_admin_api", "mocks_and_faults", "dynamic_configuration",
    "scheduled_jobs", "messages", "database_read", "database_control", "observability",
)


def _safe_name(value: Any, fallback: str) -> str:
    """Keep a user-facing scenario name inside one directory component."""

    text = str(value or fallback).strip()
    text = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:80] or fallback


def _step_id(value: Any, index: int) -> str:
    """Create a stable identifier without embedding a project-specific term."""

    text = re.sub(r"[^A-Za-z0-9_]+", "_", str(value or f"STEP_{index + 1}")).strip("_").upper()
    return text[:60] or f"STEP_{index + 1}"


def _source_entries(project_root: Path) -> list[dict[str, Any]]:
    """Read only repository and source anchors already present in workspace discovery."""

    path = project_root / "discovery" / "workspace.yaml"
    try:
        workspace = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return []
    if not isinstance(workspace, Mapping):
        return []
    repositories = workspace.get("inventory", {}).get("repositories", [])
    configuration = workspace.get("configuration", {})
    anchors_by_repo: dict[str, list[str]] = {}
    for item in configuration.get("sources", []) if isinstance(configuration, Mapping) else []:
        if not isinstance(item, Mapping):
            continue
        for evidence in item.get("evidence", []) if isinstance(item.get("evidence"), list) else []:
            text = str(evidence)
            if "#" in text:
                repo, anchor = text.split("#", 1)
                anchors_by_repo.setdefault(repo, []).append(anchor)
    result: list[dict[str, Any]] = []
    for repository in repositories if isinstance(repositories, list) else []:
        if not isinstance(repository, Mapping):
            continue
        repo = str(repository.get("id") or "")
        commit = str(repository.get("commit") or "")
        if not repo or not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
            continue
        anchors = list(dict.fromkeys(anchors_by_repo.get(repo, [])))
        if not anchors:
            builds = repository.get("build_files", [])
            anchors = [str(builds[0])] if isinstance(builds, list) and builds and builds[0] else []
        if anchors:
            result.append({"repo": repo, "commit": commit, "anchors": anchors})
    return result


def _workspace_configuration(project_root: Path) -> tuple[str, Mapping[str, Any], Mapping[str, Any]]:
    """Return active environment and its resolved-shape configuration when available."""

    try:
        config = yaml.safe_load((project_root / "config" / "config.yaml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        config = {}
    active = str(config.get("active_environment") or "test") if isinstance(config, Mapping) else "test"
    try:
        environment = yaml.safe_load((project_root / "config" / "environments" / f"{active}.yaml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        environment = {}
    return active, config if isinstance(config, Mapping) else {}, environment if isinstance(environment, Mapping) else {}


def _workspace_capabilities(project_root: Path) -> set[str]:
    """Map discovered topology/configuration evidence to generic control paths."""

    try:
        workspace = yaml.safe_load((project_root / "discovery" / "workspace.yaml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return set()
    if not isinstance(workspace, Mapping):
        return set()
    capabilities: set[str] = set()
    configuration = workspace.get("configuration", {})
    services = configuration.get("services", []) if isinstance(configuration, Mapping) else []
    if isinstance(services, list) and services:
        capabilities.add("public_api")
    for collection, names in (
        ("controls", {"test_or_admin_api", "mocks_and_faults", "dynamic_configuration", "scheduled_jobs", "messages", "database_control"}),
        ("data_sources", {"database_read", "database_control"}),
        ("middleware", {"messages", "observability"}),
    ):
        values = configuration.get(collection, []) if isinstance(configuration, Mapping) else []
        for item in values if isinstance(values, list) else []:
            text = " ".join(str(item.get(key, "")) for key in ("id", "type", "kind", "name", "capability")) if isinstance(item, Mapping) else str(item)
            lowered = text.casefold()
            capabilities.update(name for name, fragments in {
                "test_or_admin_api": ("admin", "test api", "control api"),
                "mocks_and_faults": ("mock", "fault", "stub"),
                "dynamic_configuration": ("config", "feature", "flag"),
                "scheduled_jobs": ("job", "schedule", "cron", "task"),
                "messages": ("message", "kafka", "topic", "broker"),
                "database_control": ("write", "mutat", "database control"),
                "database_read": ("database", "query", "read"),
                "observability": ("observe", "status", "log", "trace"),
            }.items() if any(fragment in lowered for fragment in fragments))
    searches = workspace.get("topology", {}).get("searches", {}) if isinstance(workspace.get("topology"), Mapping) else {}
    if isinstance(searches, Mapping):
        if any(isinstance(value, Mapping) and value.get("evidence") for value in searches.values()):
            mapping = {"http_rpc": "public_api", "messages": "messages", "database": "database_read", "cache": "observability", "jobs": "scheduled_jobs", "configuration": "dynamic_configuration"}
            capabilities.update(mapping[key] for key, value in searches.items() if key in mapping and isinstance(value, Mapping) and value.get("evidence"))
    runtime = workspace.get("runtime_probe", {})
    if isinstance(runtime, Mapping) and runtime.get("read_only_smoke"):
        capabilities.add("observability")
    return capabilities


def _resolved(value: Any) -> bool:
    """Check environment-owned mappings without exposing their values."""

    if isinstance(value, str):
        return not re.fullmatch(r"\$\{[A-Z][A-Z0-9_]*\}", value)
    if isinstance(value, Mapping):
        return all(_resolved(child) for child in value.values())
    if isinstance(value, list):
        return all(_resolved(child) for child in value)
    return True


def _operation_map(protocols: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(item.get("id")): item
        for item in protocols.get("operations", []) if isinstance(item, Mapping) and item.get("id")
    }


def _protocol_operations(rule: Mapping[str, Any], protocols: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    operations = _operation_map(protocols)
    return [operations[ref] for ref in rule.get("protocol_refs", []) if str(ref) in operations]


def _cleanup_operation(operations: list[Mapping[str, Any]], trigger: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Find a formally declared cleanup call for a mutating HTTP operation."""

    if str(trigger.get("kind", "")).casefold() != "http":
        return None
    if str(trigger.get("method", "GET")).upper() in {"GET", "HEAD", "OPTIONS"}:
        return None
    trigger_path = str(trigger.get("path", "")).rstrip("/")
    for candidate in operations:
        if candidate is trigger or str(candidate.get("kind", "")).casefold() != "http":
            continue
        if str(candidate.get("method", "")).upper() != "DELETE":
            continue
        candidate_path = str(candidate.get("path", "")).rstrip("/")
        # A resource collection and its item endpoint are the only generic
        # relationship we can infer without business vocabulary.
        if candidate_path == trigger_path or candidate_path.startswith(trigger_path + "/"):
            return candidate
    return None


def _expectations(rule: Mapping[str, Any], operation: Mapping[str, Any]) -> list[str]:
    values = rule.get("assertions") or rule.get("final_result") or rule.get("final_statuses")
    if isinstance(values, list) and values:
        return [str(value) for value in values if str(value).strip()]
    states = rule.get("states")
    if isinstance(states, list) and states:
        return [f"status={value}" for value in states if str(value).strip()]
    return ["business_result_unconfirmed"]


def _candidate_matrix(
    *,
    source_ref: str,
    component: str,
    control: str,
    usable: bool,
    side_effect: str,
    correlation: str,
    cleanup: str,
    action: str,
    available_controls: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Build the complete control matrix for one constructability item."""

    result: list[dict[str, Any]] = []
    for kind in CANDIDATE_KINDS:
        kind_control = CONTROL_BY_KIND[kind]
        active = usable and kind_control == control and (available_controls is None or kind_control in available_controls)
        result.append({
            "kind": kind,
            "status": "usable" if active else "not_found",
            "component": component or "unresolved-component",
            "consumer_source": source_ref,
            "control": kind_control,
            "side_effect": side_effect if active else "none",
            "trigger": action,
            "observation": f"observe_{action}",
            "isolation": correlation,
            "cleanup": cleanup,
            "impact": "scenario-local",
            "recovery": f"verify_{correlation}",
            "evidence": [source_ref],
        })
    return result


def _control_matrix(
    *,
    source_ref: str,
    action: str,
    correlation: str,
    expectations: list[str],
    usable_public: bool,
    blocked: bool,
    cleanup: list[str],
    component: str,
    available_controls: set[str] | None = None,
) -> dict[str, Any]:
    controls: dict[str, Any] = {}
    for name in CONTROL_NAMES:
        usable = usable_public and not blocked and (available_controls is None or name in available_controls)
        controls[name] = {
            "status": "usable" if usable else "not_found",
            "assessment": "formal protocol control discovered" if usable else "no source-confirmed safe control discovered",
            "evidence": [source_ref],
            "planned_use": [action] if usable else [],
            "component": component or "unresolved-component",
            "trigger": action,
            "impact": "scenario-local" if usable else "none",
            "observation": expectations[0] if expectations else f"observe_{action}",
            "isolation": correlation,
            "cleanup": list(cleanup),
            "recovery": [f"verify_{correlation}"],
        }
        if name == "database_control":
            controls[name]["safety"] = None
        if name == "observability":
            controls[name].update({"correlation_keys": [correlation], "business_evidence": list(expectations), "recovery": [f"verify_{correlation}"]})
    controls["decision"] = {"safe_control_path": bool(usable_public and not blocked and available_controls), "blockers": [] if usable_public and not blocked and available_controls else ["control:public_api", f"contract:{source_ref}"]}
    return controls


def _definition(
    project_root: Path,
    rule: Mapping[str, Any],
    protocols: Mapping[str, Any],
    *,
    owner: str,
) -> tuple[str, dict[str, Any], dict[str, Any], str]:
    """Build one scenario definition, data document, and diagram."""

    context = build_context(rule)
    operations = _protocol_operations(rule, protocols)
    operation = operations[0] if operations else {}
    operation_id = str(operation.get("id") or (rule.get("protocol_refs") or [""])[0])
    title = _safe_name(rule.get("title") or rule.get("id"), str(rule.get("id") or "scenario"))
    scenario_id = re.sub(r"[^A-Za-z0-9_]+", "_", str(rule.get("id") or "SCENARIO")).upper().strip("_") or "SCENARIO"
    scenario_id = (scenario_id if scenario_id[0].isalpha() else "S_" + scenario_id)[:60]
    step_id = _step_id(operation_id or rule.get("id"), 0)
    design_rule_id = str(rule.get("id") or scenario_id)
    action = str(operation.get("id") or context.get("entry_operations") or "business_entry")
    correlation = f"correlation_{scenario_id.lower()}"
    cleanup_action = f"cleanup_{scenario_id.lower()}"
    source_entries = _source_entries(project_root)
    source_ref = f"{source_entries[0]['repo']}#{source_entries[0]['anchors'][0]}" if source_entries else ""
    kind = str(operation.get("kind") or "")
    read_only = (
        (kind == "http" and str(operation.get("method", "GET")).upper() in {"GET", "HEAD", "OPTIONS"})
        or (kind == "graphql" and str(operation.get("operation_type", "query")).casefold() == "query")
    )
    cleanup_operation = _cleanup_operation(operations, operation)
    cleanup_ref = str(cleanup_operation.get("id")) if cleanup_operation else ""
    if cleanup_ref:
        cleanup_action = f"protocol:{cleanup_ref}"
    available_controls = _workspace_capabilities(project_root)
    available_controls.add({"http": "public_api", "message": "messages", "task": "scheduled_jobs"}.get(kind, "observability"))
    # The formal response itself is a safe observation source for HTTP
    # operations, including a write whose cleanup is separately declared.
    if kind in {"http", "graphql"}:
        available_controls.add("observability")
    expectations = _expectations(rule, operation)
    has_design_expectation = expectations != ["business_result_unconfirmed"]
    confirmed = str(rule.get("status")) == "confirmed" and has_design_expectation and bool(operations) and bool(source_entries)
    # Mutating calls are executable only when a formal cleanup operation is
    # available. Otherwise keep the scenario and classify the step precisely.
    safe_mutation = read_only or bool(cleanup_ref)
    async_rule = bool(rule.get("async"))
    blocked = not confirmed or not safe_mutation or (async_rule and len(operations) < 2)
    status = "contract_blocked" if blocked else "pending_environment"
    active, config, environment = _workspace_configuration(project_root)
    services = context.get("services") or context.get("participants") or []
    known_services = environment.get("services", {}) if isinstance(environment, Mapping) else {}
    runtime_confirmed = bool(services) and isinstance(known_services, Mapping) and all(
        str(service) in known_services and _resolved(known_services[str(service)]) for service in services
    )
    blockers = [] if blocked else ([] if runtime_confirmed else [f"config:{str(services[0]) if services else 'service'}"])
    data = generate_scenario_data(
        list(operations), namespace=scenario_id.lower(), environments=(active,), correlation_keys=[correlation]
    )
    side_effect = "read" if read_only else "write"
    step_status = "control_gap" if blocked else ("executable" if runtime_confirmed else "environment_missing")
    step_reason = (
        "formal protocol and source evidence mapped"
        if not blocked
        else "formal protocol and source evidence are available but no safe cleanup adapter is declared"
        if confirmed and not safe_mutation
        else "required control or source evidence is unavailable"
    )
    candidate_control = "public_api" if kind == "http" else "messages" if kind == "message" else "scheduled_jobs" if kind == "task" else "public_api"
    component = str(operation.get("service") or (context.get("services") or [operation_id or design_rule_id])[0])
    candidates = _candidate_matrix(
        source_ref=source_ref or "protocol:unconfirmed",
        component=component,
        control=candidate_control,
        usable=not blocked,
        side_effect=side_effect,
        correlation=correlation,
        cleanup=cleanup_action,
        action=action,
        available_controls=available_controls,
    )
    steps: list[dict[str, Any]] = [{
        "id": step_id,
        "action": action,
        "control": candidate_control,
        "side_effect": side_effect,
        "expect": expectations,
        "status": step_status,
        "status_reason": step_reason,
        "evidence": [source_ref] if source_ref else [],
        "design_rule_id": design_rule_id,
        "phase": "request",
    }]
    has_required_input = any(
        isinstance(operation.get(name), list) and any(isinstance(field, Mapping) and field.get("required") is True for field in operation.get(name, []))
        for name in ("request_fields", "parameters", "arguments", "message_fields", "header_fields")
    )
    if has_required_input:
        steps[0]["data_ref"] = f"业务数据.json#/{re.sub(r'[^A-Za-z0-9_]+', '_', operation_id or 'step').lower()}"
    if operation_id:
        steps[0]["protocol_ref"] = operation_id
    if async_rule:
        acceptance_status = str((rule.get("acceptance_statuses") or [f"accepted_for_{design_rule_id}"])[-1])
        final_status = str((rule.get("final_statuses") or [f"terminal_for_{design_rule_id}"])[-1])
        async_contract = {
            "trigger": action,
            "correlation_key": correlation,
            "expected_status": final_status,
            "acceptance_status": acceptance_status,
            "final_status": final_status,
            "timeout_seconds": 60,
            "interval_seconds": 1,
            "retries": max(0, len(rule.get("retries", [])) if isinstance(rule.get("retries"), list) else 0),
            "repeat_detection": "same correlation key and terminal status",
            "final_failure": "timeout_or_terminal_failure",
        }
        observer_operations = operations[1:]
        steps[0]["async"] = async_contract
        steps[0]["expect"] = [f"status={acceptance_status}"]
        if not observer_operations:
            steps[0]["status"] = "control_gap"
            steps[0]["status_reason"] = "async flow has no formal processing or final observation operation"
        for index, observer in enumerate(observer_operations):
            phase = "final_business" if index == len(observer_operations) - 1 else "processing"
            name = "observe_final" if phase == "final_business" else f"observe_processing_{index + 1}"
            observer_ref = str(observer.get("id", ""))
            observer_kind = str(observer.get("kind", ""))
            observer_method = str(observer.get("method", "GET")).upper()
            observer_has_adapter = bool(observer_ref and observer_kind == "http" and observer_method in {"GET", "HEAD"})
            observer_status = (
                "control_gap" if blocked or not observer_has_adapter
                else "executable" if runtime_confirmed
                else "environment_missing"
            )
            expected_observer_status = async_contract["final_status"] if phase == "final_business" else async_contract["acceptance_status"]
            observer_async_contract = dict(async_contract)
            observer_async_contract["expected_status"] = expected_observer_status
            observer_step = {
                "id": f"{step_id}_{phase.upper()}", "action": name, "control": "observability",
                "side_effect": "none", "expect": [f"status={expected_observer_status}"],
                "status": observer_status, "status_reason": "formal observer protocol and bounded polling are available" if observer_status == "executable" else "async observer adapter requires a source-confirmed read protocol" if observer_status == "control_gap" else "active environment configuration is required for the async observer",
                "evidence": [source_ref] if source_ref else [], "design_rule_id": design_rule_id,
                "phase": phase, "async": observer_async_contract,
            }
            if observer_ref:
                observer_step["protocol_ref"] = observer_ref
            if any(
                isinstance(observer.get(name), list)
                and any(isinstance(field, Mapping) and field.get("required") is True for field in observer.get(name, []))
                for name in ("request_fields", "parameters", "arguments", "message_fields", "header_fields")
            ):
                observer_step["data_ref"] = f"业务数据.json#/{re.sub(r'[^A-Za-z0-9_]+', '_', observer_ref or 'observer').lower()}"
            steps.append(observer_step)
    preconditions = [str(item) for item in (rule.get("preconditions") or ["scenario_data_ready"]) if str(item).strip()]
    constructability = {
        "preconditions": [{"id": item, "data_ownership": "test_owned", "constructible": True, "candidates": candidates} for item in preconditions],
        "steps": [{"step_id": step["id"], "candidates": candidates if step["control"] == candidate_control else _candidate_matrix(
            source_ref=source_ref or "protocol:unconfirmed", component=operation_id, control="observability", usable=False,
            side_effect="none", correlation=correlation, cleanup=cleanup_action, action=str(step["action"]), available_controls=available_controls)} for step in steps],
    }
    owned_resources = []
    if not read_only and cleanup_ref:
        owned_resources.append({
            "kind": "protocol-resource",
            "identity": correlation,
            "cleanup": cleanup_action,
            "restore": cleanup_action,
            "verify": f"verify_{correlation}",
        })
    cleanup = {
        "strategy": "formal protocol cleanup" if cleanup_ref else "read-only resource ownership" if read_only else "blocked until an approved cleanup adapter exists",
        "actions": [cleanup_action],
        "verifies": [f"verify_{correlation}"],
    }
    definition = {
        "meta": {"id": scenario_id, "name": title, "status": status, "actor": str((context.get("participants") or ["scenario_actor"])[0]), "participants": context.get("participants", []), "context": context},
        "generation": {"mode": "main_agent", "owner": owner, "write_scope": f"scenarios/{title}", "degradation_reason": None},
        "readiness": {"source_contract": "confirmed" if confirmed else "blocked", "safe_control": "confirmed" if not blocked else "blocked", "runtime_configuration": "confirmed" if runtime_confirmed else "missing", "test_data": "confirmed", "blockers": blockers or ([f"contract:{source_ref}"] if source_ref and blocked else ["contract:design-evidence"])},
        "preconditions": preconditions,
        "constructability": constructability,
        "integrations": {"services": [str(item) for item in services if str(item).strip()], "components": []},
        "controls": _control_matrix(source_ref=source_ref or "protocol:unconfirmed", action=action, correlation=correlation, expectations=expectations, usable_public=not blocked, blocked=blocked, cleanup=cleanup["actions"], component=component, available_controls=available_controls),
        "isolation": {"namespace": scenario_id.lower(), "correlation_keys": [correlation], "owned_resources": owned_resources, "mutable_controls": [], "serial_lock": None},
        "steps": steps,
        "cleanup": cleanup,
        "source": source_entries,
    }
    diagram_participants = context.get("participants") or [str((context.get("services") or [design_rule_id])[0]), component]
    diagram_lines = [f"# {title}", "", "本图来自当前设计规则和正式协议映射，未补充未确认的业务规则。", "", "关键步骤："]
    for index, step in enumerate(steps, 1):
        diagram_lines.append(f"{index}. {step['action']}")
    diagram_lines.extend(["", "```mermaid", "sequenceDiagram"])
    for index, participant in enumerate(diagram_participants, 1):
        diagram_lines.append(f"    participant P{index} as {participant}")
    if len(diagram_participants) >= 2:
        diagram_lines.append(f"    P1->>P2: {action}")
    diagram_lines.extend(["```", ""])
    return title, definition, data, "\n".join(diagram_lines)


def _test_source(name: str, definition: Mapping[str, Any]) -> str:
    """Render a runtime-only pytest orchestration module."""

    scenario_id = str(definition["meta"]["id"])
    steps = [step for step in definition.get("steps", []) if isinstance(step, Mapping)]
    lines = [
        '"""编排当前场景的黑盒业务链路，并保留真实运行证据。"""',
        "from pathlib import Path",
        "import pytest",
        "from dltk.e2e_runtime import assert_business_expectations, cleanup_scenario, invoke_protocol, poll_until, preflight, record_business_entry, report_control_gap, step_guard",
        "",
        "",
        "def _project_root():",
        '    """定位生成工程根目录。"""',
        "    return Path(__file__).resolve().parents[2]",
        "",
        "@pytest.mark.business_e2e",
        f'@pytest.mark.scenario_id("{scenario_id}")',
        f"def test_{re.sub(r'[^A-Za-z0-9_]+', '_', str(definition['meta']['name'])).strip('_') or 'scenario'}():",
        '    """执行正式协议调用、业务断言和保证执行的清理。"""',
        f'    scenario_context = preflight(_project_root(), {str(definition["meta"]["name"])!r})',
        f'    scenario_context["scenario"] = {str(definition["meta"]["name"])!r}',
        "    project_root = _project_root()",
        "    body_error = None",
        "    try:",
    ]
    lines.extend([
        f'        record_business_entry({str(definition["meta"]["name"])!r})',
        "        blocked_steps = False",
    ])
    for step in steps:
        step_id = str(step.get("id"))
        evidence = list(step.get("evidence", [])) if isinstance(step.get("evidence"), list) else []
        protocol_ref = str(step.get("protocol_ref", ""))
        status = str(step.get("status", "executable"))
        if not protocol_ref or status != "executable":
            lines.extend([
                f'        gap = report_control_gap(scenario_context["scenario"], step_id={step_id!r}, reason={str(step.get("status_reason") or "step is not executable")!r}, evidence={evidence!r})',
                '        assert gap["status"] == "control_gap"',
                "        blocked_steps = True",
            ])
            continue
        data_key = str(step.get("data_ref", "")).split("#/")[-1]
        payload_expression = (
            f'scenario_context["business_data"][scenario_context["active_environment"]].get({data_key!r}, {{}})'
            if data_key else "{}"
        )
        expectations = list(step.get("expect", [])) if isinstance(step.get("expect"), list) else []
        lines.extend([
            f"        with step_guard(scenario_context[\"scenario\"], step_id={step_id!r}, evidence={evidence!r}):",
            f"            payload = {payload_expression}",
        ])
        async_spec = step.get("async") if isinstance(step.get("async"), Mapping) else None
        if async_spec and str(step.get("phase")) in {"processing", "final_business"}:
            lines.extend([
                "            def observe_result():",
                f'                return invoke_protocol(project_root, scenario_context, {protocol_ref!r}, payload, step_id={step_id!r})',
                "            def accepted_result(candidate):",
                "                try:",
                f"                    return assert_business_expectations(candidate, {expectations!r})",
                "                except AssertionError:",
                "                    return False",
                "            result = poll_until(",
                "                observe_result, accepted_result,",
                f'                timeout_seconds={float(async_spec.get("timeout_seconds", 60))!r}, interval_seconds={float(async_spec.get("interval_seconds", 1))!r},',
                "            )",
                f"            assert assert_business_expectations(result, {expectations!r})",
            ])
        else:
            lines.extend([
                f'            result = invoke_protocol(project_root, scenario_context, {protocol_ref!r}, payload, step_id={step_id!r})',
                f'            assert assert_business_expectations(result, {expectations!r})',
            ])
    lines.extend([
        "        if blocked_steps:",
        "            raise AssertionError('one or more E2E steps are not executable')",
        "    except BaseException as exc:",
        "        body_error = exc",
        "        raise",
        "    finally:",
        "        try:",
        f'            cleanup_scenario(project_root, scenario_context, {str(definition["meta"]["name"])!r})',
        "        except BaseException as cleanup_error:",
        "            if body_error is None:",
        "                raise",
        "            if hasattr(body_error, \"add_note\"):",
        "                body_error.add_note(f\"cleanup failed: {cleanup_error}\")",
        "",
    ])
    return "\n".join(lines)


def materialize_scenarios(
    project_root: Path,
    design: Mapping[str, Any],
    protocols: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    owner: str = "main-agent",
) -> tuple[list[Path], list[str]]:
    """Create only missing scenario directories and never overwrite user assets."""

    project_root = project_root.resolve()
    scenarios_root = project_root / "scenarios"
    scenarios_root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    blockers: list[str] = []
    rules = {str(rule.get("id")): rule for rule in design.get("rules", []) if isinstance(rule, Mapping) and rule.get("id")}
    for item in plan.get("scenarios", []) if isinstance(plan, Mapping) else []:
        if not isinstance(item, Mapping):
            continue
        rule_id = str(item.get("design_rule_ids", [""])[0])
        rule = rules.get(rule_id)
        if not rule:
            continue
        if not _source_entries(project_root):
            blockers.append(f"scenario:{rule_id}:missing-source-evidence")
            continue
        title, definition, data, diagram = _definition(project_root, rule, protocols, owner=owner)
        directory = scenarios_root / title
        if directory.exists() and any(directory.iterdir()):
            try:
                existing = yaml.safe_load((directory / "场景定义.yaml").read_text(encoding="utf-8"))
            except (OSError, UnicodeError, yaml.YAMLError):
                existing = {}
            if not isinstance(existing, Mapping) or existing.get("meta", {}).get("id") != definition.get("meta", {}).get("id"):
                suffix = re.sub(r"[^A-Za-z0-9_]+", "_", str(definition.get("meta", {}).get("id") or "scenario")).strip("_")
                title = f"{title}_{suffix}"[:100]
                definition["meta"]["name"] = title
                definition["generation"]["write_scope"] = f"scenarios/{title}"
                directory = scenarios_root / title
        if directory.exists() and any(directory.iterdir()):
            continue
        directory.mkdir(parents=True, exist_ok=True)
        test_file_name = re.sub(r"\s+", "_", title)
        files = {
            directory / "场景定义.yaml": yaml.safe_dump(definition, allow_unicode=True, sort_keys=False),
            directory / "业务数据.json": json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            directory / "业务流程图.md": diagram,
            directory / f"test_{test_file_name}.py": _test_source(title, definition),
        }
        for path, content in files.items():
            path.write_text(content, encoding="utf-8")
            written.append(path)
        if not definition["source"]:
            blockers.append(f"scenario:{title}:missing-source-evidence")
    return written, blockers
