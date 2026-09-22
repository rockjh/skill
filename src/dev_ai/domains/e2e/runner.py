"""Fixed-order E2E gate and pytest orchestration."""

from __future__ import annotations

import ast
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from ...core.redaction import redact
from ...core.schema import (
    E2E_ORDERED_GATES,
    E2E_ORDERED_GATE_SEQUENCE,
    E2E_RUN_STAGES,
    REPORT_SCHEMA,
    WORKSPACE_DOCUMENT_SCHEMA,
    validate_schema,
)
from .contracts import CONTROL_NAMES, _expectation_field, _lookup_value, contract_errors
from .discovery import (
    _contains_usable_credential_text,
    _error,
    _load_json,
    _runtime_probe_live_errors,
    _strings,
    _walk_files,
    discovery_errors,
    read_only_environment_probe,
)
from .source_versions import source_version_results
from .static_checks import (
    _call_argument,
    _import_aliases,
    _resolved_call_name,
    static_errors,
)


ORDERED_GATES = E2E_ORDERED_GATES
ORDERED_GATE_SEQUENCE = E2E_ORDERED_GATE_SEQUENCE


def _run(command: list[str], project_root: Path, environment: Mapping[str, str] | None = None) -> int:
    """不经过 Shell 执行固定阶段命令并返回退出状态。"""

    completed = subprocess.run(
        command,
        cwd=project_root,
        env=dict(environment) if environment else None,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.stdout:
        print(redact(completed.stdout.rstrip()), file=sys.stderr)
    if completed.stderr:
        print(redact(completed.stderr.rstrip()), file=sys.stderr)
    return completed.returncode


def _pytest_junit_passed(path: Path) -> bool:
    """Require at least one executed test and reject skipped, xfailed, failed, or errored nodes."""

    try:
        root = ET.fromstring(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ET.ParseError):
        return False
    suites = [root] if root.tag.rsplit("}", 1)[-1] == "testsuite" else [
        item for item in root.iter() if item.tag.rsplit("}", 1)[-1] == "testsuite"
    ]
    if not suites:
        return False
    totals = {
        name: sum(int(suite.get(name, "0")) for suite in suites)
        for name in ("tests", "failures", "errors", "skipped")
    }
    return totals["tests"] > 0 and all(totals[name] == 0 for name in ("failures", "errors", "skipped"))


def _events(
    evidence_root: Path,
    run_id: str,
    paths: Iterable[Path] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """严格读取只属于本次运行且结构完整的 JSON 证据事件。"""

    events: list[dict[str, Any]] = []
    errors: list[str] = []
    if not evidence_root.is_dir():
        return events, errors
    schemas = {
        "endpoint": {"phase", "method", "target_ref", "status", "summary", "verified", "step_id", "protocol_ref", "protocol_path"},
        "business_entered": set(),
        "control": {"control_kind", "action", "correlation_ref", "side_effect"},
        "step": {"step_id", "status", "reason", "evidence"},
        "restoration": {"status", "resources"},
    }
    project_root = evidence_root.parents[2]
    scenario_root = project_root / "scenarios"
    scenario_names = {path.name for path in scenario_root.iterdir() if path.is_dir()} if scenario_root.is_dir() else set()
    event_paths = sorted(paths) if paths is not None else sorted(evidence_root.glob("*.json"))
    for path in event_paths:
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors.append(_error(path, "evidence-parse", f"运行证据无法解析: {exc}"))
            continue
        if not isinstance(event, dict) or set(event) != {"version", "run_id", "kind", "scenario", "details"}:
            errors.append(_error(path, "evidence-schema", "运行证据顶层结构无效"))
            continue
        kind = event.get("kind")
        details = event.get("details")
        if event.get("version") != 1 or event.get("run_id") != run_id or kind not in schemas:
            errors.append(_error(path, "evidence-identity", "运行证据版本、run_id 或 kind 无效"))
            continue
        if not isinstance(event.get("scenario"), str) or not event["scenario"].strip() or not isinstance(details, dict):
            errors.append(_error(path, "evidence-content", "运行证据场景和 details 无效"))
            continue
        if event["scenario"] not in scenario_names:
            errors.append(_error(path, "evidence-scenario", f"运行证据引用未知场景: {event['scenario']}"))
            continue
        expected = schemas[kind]
        actual = set(details)
        if kind == "restoration":
            if (
                actual not in (expected, expected | {"error"})
                or details.get("status") not in {"passed", "failed"}
                or not _strings(details.get("resources"), nonempty=False)
                or (details.get("status") == "failed") != ("error" in details)
            ):
                errors.append(_error(path, "evidence-restoration", "恢复证据结构或状态无效"))
                continue
        elif kind == "control" and actual not in (expected, expected | {"step_id", "protocol_ref", "protocol_path"}):
            errors.append(_error(path, "evidence-details", f"{kind} 证据字段无效"))
            continue
        elif kind != "control" and actual != expected:
            errors.append(_error(path, "evidence-details", f"{kind} 证据字段无效"))
            continue
        elif kind == "endpoint" and (
            details.get("phase") not in {"smoke", "business"}
            or not isinstance(details.get("method"), str)
            or not details["method"].strip()
            or details.get("phase") == "smoke" and details["method"].upper() not in {"GET", "HEAD", "READ"}
            or details.get("phase") == "business" and (
                not isinstance(details.get("step_id"), str) or not details["step_id"].strip()
                or not isinstance(details.get("protocol_ref"), str) or not details["protocol_ref"].strip()
            )
            or details.get("phase") == "smoke" and (
                details.get("step_id") is not None or details.get("protocol_ref") is not None or details.get("protocol_path") is not None
            )
            or not isinstance(details.get("target_ref"), str)
            or not details["target_ref"].strip()
            or details.get("verified") is not True
            or str(details.get("method", "")).upper() in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}
            and (
                not isinstance(details.get("status"), int)
                or isinstance(details.get("status"), bool)
                or not 100 <= details["status"] < 500
            )
        ):
            errors.append(_error(path, "evidence-endpoint", "接口证据阶段、方法或目标引用无效"))
            continue
        elif kind == "control" and (
            details.get("control_kind") not in CONTROL_NAMES
            or not isinstance(details.get("action"), str)
            or not details["action"].strip()
            or not isinstance(details.get("correlation_ref"), str)
            or not details["correlation_ref"].strip()
            or details.get("side_effect") not in {"read", "write"}
            or ("step_id" in details and (
                not isinstance(details.get("step_id"), str) or not details["step_id"].strip()
                or not isinstance(details.get("protocol_ref"), str) or not details["protocol_ref"].strip()
                or details.get("protocol_path") is not None
                and (not isinstance(details.get("protocol_path"), str) or not details["protocol_path"].startswith("/"))
            ))
        ):
            errors.append(_error(path, "evidence-control", "控制证据类别、动作或关联引用无效"))
            continue
        elif kind == "step" and (
            not isinstance(details.get("step_id"), str)
            or not details["step_id"].strip()
            or details.get("status") not in {
                "executable", "environment_missing", "authorization_missing", "control_gap", "product_gap", "runtime_failure",
            }
            or not isinstance(details.get("reason"), str)
            or not details["reason"].strip()
            or not _strings(details.get("evidence"), nonempty=False)
        ):
            errors.append(_error(path, "evidence-step", "步骤证据状态、原因或引用无效"))
            continue
        events.append(event)
    protocol_path = project_root / "discovery" / "protocol-rules.yaml"
    try:
        protocol = yaml.safe_load(protocol_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        protocol = None
    operations = {
        str(item.get("id")): item
        for item in (protocol.get("operations", []) if isinstance(protocol, dict) else [])
        if isinstance(item, dict) and item.get("id")
    }
    scenario_steps: dict[str, dict[str, Mapping[str, Any]]] = {}
    for scenario_name in scenario_names:
        definition_path = scenario_root / scenario_name / "场景定义.yaml"
        try:
            definition = yaml.safe_load(definition_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            continue
        scenario_steps[scenario_name] = {
            str(step.get("id")): step
            for step in (definition.get("steps", []) if isinstance(definition, dict) else [])
            if isinstance(step, Mapping) and step.get("id")
        }
    for event in events:
        details = event.get("details", {})
        if event.get("kind") not in {"endpoint", "control"} or details.get("phase") == "smoke":
            continue
        if event.get("kind") == "control" and "step_id" not in details:
            continue
        step = scenario_steps.get(str(event.get("scenario")), {}).get(str(details.get("step_id")))
        operation = operations.get(str(details.get("protocol_ref")))
        if not isinstance(step, Mapping) or step.get("protocol_ref") != details.get("protocol_ref"):
            errors.append(_error(protocol_path, "evidence-protocol-step", f"接口证据未绑定场景步骤的 protocol_ref: {details.get('step_id')}"))
            continue
        if not isinstance(operation, Mapping):
            errors.append(_error(protocol_path, "evidence-protocol-ref", f"接口证据引用未知正式协议操作: {details.get('protocol_ref')}"))
            continue
        if event.get("kind") == "control":
            if operation.get("kind") == "http":
                if details.get("protocol_path") != operation.get("path"):
                    errors.append(_error(protocol_path, "evidence-protocol-path", f"控制证据路径与正式协议不一致: {details.get('protocol_ref')}"))
            elif details.get("protocol_path") is not None:
                errors.append(_error(protocol_path, "evidence-protocol-path", "非 HTTP 控制证据不得声明 HTTP path"))
            continue
        if operation.get("kind") == "http":
            if str(operation.get("method", "")).upper() != str(details.get("method", "")).upper():
                errors.append(_error(protocol_path, "evidence-protocol-method", f"实际方法与正式协议不一致: {details.get('protocol_ref')}"))
            if operation.get("path") != details.get("protocol_path"):
                errors.append(_error(protocol_path, "evidence-protocol-path", f"实际路径与正式协议不一致: {details.get('protocol_ref')}"))
            statuses = {str(value).upper() for value in operation.get("status_codes", [])}
            actual = str(details.get("status"))
            if statuses and actual not in statuses and (f"{actual[:1]}XX" if actual else "") not in statuses and "DEFAULT" not in statuses:
                errors.append(_error(protocol_path, "evidence-protocol-status", f"实际状态码不在正式协议中: {details.get('protocol_ref')}={actual}"))
        response_fields = {
            str(field.get("path") or field.get("name"))
            for field in operation.get("response_fields", [])
            if isinstance(field, Mapping) and (field.get("path") or field.get("name"))
        }
        if response_fields:
            summary = details.get("summary")
            for expectation in step.get("expect", []) if isinstance(step.get("expect"), list) else []:
                field_name, _ = _expectation_field(expectation)
                if not field_name or not any(path == field_name or path.endswith("." + field_name) for path in response_fields):
                    continue
                found, _ = _lookup_value(summary, field_name, "header" if field_name.startswith("header.") else "response")
                if not found:
                    errors.append(_error(protocol_path, "evidence-protocol-response", f"运行响应缺少正式协议断言字段: {field_name}"))
    return events, errors


def _report_scenarios(
    scenarios: Sequence[tuple[Path, dict[str, Any]]],
    events: Sequence[dict[str, Any]],
    executions: Mapping[str, dict[str, Any]],
    smoke_status: str,
) -> list[dict[str, Any]]:
    """把静态契约和真实运行事件聚合为逐场景验收结果。"""

    reports: list[dict[str, Any]] = []
    for directory, definition in scenarios:
        scenario_events = [event for event in events if event.get("scenario") == directory.name]
        controls = definition.get("controls", {})
        step_events = {
            str(event.get("details", {}).get("step_id")): event.get("details", {})
            for event in scenario_events if event.get("kind") == "step"
        }
        endpoint_events = {
            str(event.get("details", {}).get("step_id")): event.get("details", {})
            for event in scenario_events
            if event.get("kind") == "endpoint" and event.get("details", {}).get("phase") == "business"
        }
        step_results = []
        for step in definition.get("steps", []):
            if not isinstance(step, Mapping):
                continue
            step_id = str(step.get("id"))
            detail = step_events.get(step_id)
            if detail is None:
                status = str(step.get("status", "executable"))
                reason = str(step.get("status_reason", "未产生运行证据"))
                evidence = [str(item) for item in step.get("evidence", [])]
            else:
                status = str(detail.get("status"))
                reason = str(detail.get("reason"))
                evidence = [str(item) for item in detail.get("evidence", [])]
            endpoint_detail = endpoint_events.get(step_id, {})
            step_results.append({
                "id": step_id,
                "status": status,
                "reason": reason,
                "evidence": evidence,
                "design_rule_id": step.get("design_rule_id"),
                "protocol_ref": step.get("protocol_ref"),
                "phase": step.get("phase"),
                "expected": [str(value) for value in step.get("expect", [])] if isinstance(step.get("expect"), list) else [],
                "actual": (
                    detail.get("actual") if detail is not None and detail.get("actual") is not None
                    else endpoint_detail.get("summary", endpoint_detail.get("status"))
                    if endpoint_detail else None
                ),
            })
        executed = sum(
            item.get("status") in {"executable", "runtime_failure", "product_gap"}
            for step_id, item in step_events.items() if step_id in {str(step.get("id")) for step in definition.get("steps", []) if isinstance(step, Mapping)}
        )
        covered = sum(item["id"] in step_events for item in step_results)
        has_runtime_failure = any(item["status"] == "runtime_failure" for item in step_results)
        has_product_failure = any(item["status"] == "product_gap" for item in step_results)
        blocked = any(item["status"] in {"environment_missing", "authorization_missing", "control_gap"} for item in step_results)
        all_blocked = bool(step_results) and all(
            item["status"] in {"environment_missing", "authorization_missing", "control_gap"}
            for item in step_results
        )
        business_info = executions.get(directory.name, {"status": "N/A", "exit_code": None, "reason": "未执行"})
        business_status = business_info.get("status")
        business_failed = has_runtime_failure or has_product_failure or (business_status == "failed" and not blocked)
        classification = (
            "business_failure" if business_failed else
            "blocked" if all_blocked and business_status == "N/A" else
            "partially_covered" if blocked or (step_results and covered < len(step_results) and business_status != "N/A") else
            "executed" if business_status == "passed" else
            "blocked" if definition.get("meta", {}).get("status") != "ready" else
            "static_complete"
        )
        reports.append({
            "name": directory.name,
            "owner": definition.get("generation", {}).get("owner"),
            "generation_mode": definition.get("generation", {}).get("mode"),
            "degradation_reason": definition.get("generation", {}).get("degradation_reason"),
            "status": definition.get("meta", {}).get("status"),
            "participants": [str(item) for item in definition.get("meta", {}).get("participants", [])] if isinstance(definition.get("meta", {}).get("participants", []), list) else [],
            "integrations": definition.get("integrations", {"services": [], "components": []}) if isinstance(definition.get("integrations"), Mapping) else {"services": [], "components": []},
            "design_rule_ids": sorted({
                str(step.get("design_rule_id"))
                for step in definition.get("steps", [])
                if isinstance(step, Mapping) and step.get("design_rule_id")
            }),
            "protocol_refs": sorted({
                str(step.get("protocol_ref"))
                for step in definition.get("steps", [])
                if isinstance(step, Mapping) and step.get("protocol_ref")
            }),
            "planned_controls": [
                name for name in CONTROL_NAMES
                if isinstance(controls.get(name), dict) and controls[name].get("planned_use")
            ],
            "used_controls": [event.get("details") for event in scenario_events if event.get("kind") == "control"],
            "endpoint_calls": [event.get("details") for event in scenario_events if event.get("kind") == "endpoint"],
            "business_entered": any(event.get("kind") == "business_entered" for event in scenario_events),
            "smoke": (
                "N/A" if smoke_status == "N/A" else
                "passed" if smoke_status == "passed" and any(
                    event.get("kind") == "endpoint" and event.get("details", {}).get("phase") == "smoke"
                    for event in scenario_events
                ) else "failed"
            ),
            "business": business_info,
            "restoration": [event.get("details") for event in scenario_events if event.get("kind") == "restoration"],
            "step_results": step_results,
            "execution_rate": (executed / len(step_results)) if step_results else 0,
            "coverage_rate": (covered / len(step_results)) if step_results else 0,
            "business_correctness": "failed" if business_failed else "passed" if business_status == "passed" and not blocked else "unknown",
            "classification": classification,
        })
    return reports


def _unexpected_control_correlations(
    definition: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> set[str]:
    """返回未绑定到场景隔离契约的运行控制关联值。"""

    isolation = definition.get("isolation", {})
    if not isinstance(isolation, Mapping):
        return {
            str(event.get("details", {}).get("correlation_ref"))
            for event in events if event.get("kind") == "control"
        }
    allowed = set(str(item) for item in isolation.get("correlation_keys", []))
    allowed.update(str(item) for item in isolation.get("mutable_controls", []))
    allowed.update(
        str(item.get("identity")) for item in isolation.get("owned_resources", []) if isinstance(item, Mapping)
    )
    observed = {
        str(event.get("details", {}).get("correlation_ref"))
        for event in events if event.get("kind") == "control"
    }
    return observed - allowed


def _write_report(project_root: Path, report: dict[str, Any]) -> Path:
    """原子写入机器可读运行报告。"""

    schema_errors = validate_schema(REPORT_SCHEMA["document"], report)
    if schema_errors:
        raise ValueError("invalid E2E report: " + "; ".join(schema_errors[:10]))
    path = project_root / "artifacts" / "e2e-run.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(_redact_report(report), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _redact_report(value: Any, key: str = "") -> Any:
    """递归脱敏报告中的凭据键、认证文本和带密钥查询参数。"""

    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    fragments = {
        "password", "secret", "token", "authorization", "apikey", "accesskey", "credential",
        "cookie", "connection", "dsn", "privatekey", "sshkey", "passphrase", "clientsecret", "auth",
    }
    sensitive_key = any(fragment in normalized for fragment in fragments)
    if sensitive_key and normalized != "connectionsource":
        return "<redacted>"
    if isinstance(value, dict):
        return {str(child_key): _redact_report(child, str(child_key)) for child_key, child in value.items()}
    if isinstance(value, list):
        return [_redact_report(item) for item in value]
    if sensitive_key:
        return "<redacted>"
    if isinstance(value, str) and _contains_usable_credential_text(value):
        return "<redacted>"
    return value


def _pytest_arg_errors(arguments: Sequence[str]) -> list[str]:
    """只允许不会改变测试选择或成功语义的展示型 pytest 参数。"""

    exact = {"-q", "-v", "-vv", "-vvv", "-s", "--showlocals", "--disable-warnings"}
    prefixes = ("--tb=", "--durations=", "--color=", "--capture=", "--log-cli-level=")
    return [argument for argument in arguments if argument not in exact and not argument.startswith(prefixes)]


def _smoke_paths(project_root: Path, scenario_names: set[str]) -> list[Path]:
    """按 record_endpoint 的场景字面量选择只读冒烟模块。"""

    selected: list[Path] = []
    root = project_root / "tests" / "runtime"
    if not root.is_dir():
        return selected
    for path in _walk_files(root):
        if path.suffix != ".py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeError, SyntaxError):
            continue
        aliases = _import_aliases(tree)
        declared = {
            str(argument.value)
            for call in ast.walk(tree)
            if isinstance(call, ast.Call) and _resolved_call_name(call, aliases).rsplit(".", 1)[-1] == "record_endpoint"
            if (argument := _call_argument(call, "scenario", 0)) is not None
            and isinstance(argument, ast.Constant) and isinstance(argument.value, str)
        }
        if declared.intersection(scenario_names):
            selected.append(path)
    return sorted(selected)


def _gate_digest(paths: Iterable[Path]) -> str:
    """计算一组门禁输入文件的稳定内容摘要。"""

    digest = hashlib.sha256()
    for path in sorted(set(item.resolve() for item in paths)):
        digest.update(str(path).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes() if path.is_file() else b"<missing>")
        digest.update(b"\0")
    return digest.hexdigest()


def _seal_path(project_root: Path, gate: str) -> Path:
    """返回顺序门禁摘要文件路径。"""

    return project_root / ".e2e-state" / f"{gate}.json"


def _gate_session_path(project_root: Path) -> Path:
    """Return the active ordered-gate session file."""

    return project_root / ".e2e-state" / "session.json"


def _start_gate_session(project_root: Path) -> None:
    """Invalidate all historical ordering seals by starting a new run."""

    path = _gate_session_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "version": 1,
        "run_id": uuid.uuid4().hex,
        "next_gate": ORDERED_GATE_SEQUENCE[0],
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def _gate_session_errors(project_root: Path, stage: str) -> list[str]:
    """Require the next stage of a fresh, single ordered gate run."""

    path = _gate_session_path(project_root)
    errors: list[str] = []
    session = _load_json(path, errors)
    try:
        started = dt.datetime.fromisoformat(str(session.get("started_at"))) if isinstance(session, dict) else None
        fresh = started is not None and dt.datetime.now(dt.timezone.utc) - started <= dt.timedelta(hours=1)
    except ValueError:
        fresh = False
    if (
        not isinstance(session, dict)
        or session.get("version") != 1
        or session.get("next_gate") != stage
        or not fresh
    ):
        errors.append(_error(path, "gate-order", f"当前门禁会话不允许执行 {stage}；必须从 workspace_inventory 开始连续执行"))
    return errors


def _advance_gate_session(project_root: Path, stage: str) -> None:
    """Advance the active gate session after one successful stage."""

    path = _gate_session_path(project_root)
    session = json.loads(path.read_text(encoding="utf-8"))
    index = ORDERED_GATE_SEQUENCE.index(stage)
    session["next_gate"] = ORDERED_GATE_SEQUENCE[index + 1] if index + 1 < len(ORDERED_GATE_SEQUENCE) else "complete"
    path.write_text(json.dumps(session, indent=2) + "\n", encoding="utf-8")


def _write_seal(project_root: Path, gate: str, paths: Iterable[Path]) -> None:
    """记录已通过门禁的输入摘要供下一阶段核验。"""

    path = _seal_path(project_root, gate)
    path.parent.mkdir(parents=True, exist_ok=True)
    session_errors: list[str] = []
    session = _load_json(_gate_session_path(project_root), session_errors)
    run_id = session.get("run_id") if isinstance(session, dict) else None
    path.write_text(json.dumps({"version": 1, "run_id": run_id, "digest": _gate_digest(paths)}, indent=2) + "\n", encoding="utf-8")


def _seal_errors(project_root: Path, gate: str, paths: Iterable[Path]) -> list[str]:
    """拒绝缺失、损坏或输入已变化的前置门禁摘要。"""

    path = _seal_path(project_root, gate)
    errors: list[str] = []
    seal = _load_json(path, errors)
    session = _load_json(_gate_session_path(project_root), errors)
    expected = _gate_digest(paths)
    if (
        not isinstance(seal, dict)
        or seal.get("version") != 1
        or seal.get("digest") != expected
        or not isinstance(session, dict)
        or seal.get("run_id") != session.get("run_id")
    ):
        errors.append(_error(path, "gate-order", f"{gate} 门禁未执行或输入已变化，必须按顺序重新执行"))
    return errors


def _discovery_inputs(project_root: Path) -> list[Path]:
    """列出发现门禁摘要覆盖的文件。"""

    return [project_root / "discovery" / "workspace.yaml"]


def _contract_inputs(project_root: Path, selected: str | None) -> list[Path]:
    """列出契约门禁摘要覆盖的发现和场景定义文件。"""

    paths = _discovery_inputs(project_root)
    paths.extend(
        project_root / "discovery" / name
        for name in ("design-rules.yaml", "protocol-rules.yaml", "logic.yaml", "scenario-plan.yaml", "version-lock.yaml", "exclusions.yaml")
    )
    paths.append(project_root / "config" / "value-resolution.yaml")
    root = project_root / "scenarios"
    if root.is_dir():
        paths.extend(path / "场景定义.yaml" for path in root.iterdir() if path.is_dir())
    return paths


def _diagnostic_rule(error: str) -> str:
    """从带路径和行号的诊断中提取稳定规则名。"""

    match = re.search(r":\d+: ([a-z][a-z0-9-]+):", error)
    return match.group(1) if match else "unclassified"


def _scoped_errors(errors: list[str], stage: str) -> list[str]:
    """把聚合校验诊断稳定归入不可合并的执行阶段。"""

    discovery_prefixes = {
        "workspace_inventory": ("discovery-", "inventory-", "workspace-", "repository-", "build-", "module-", "existing-"),
        "dependency_topology": ("topology-",),
        "initial_configuration": ("configuration-", "secret-free"),
        "runtime_probe": ("runtime-",),
    }
    contract_prefixes = {
        "control_matrix": (
            "scenario-schema", "scenario-meta", "scenario-id", "scenario-status", "readiness-", "preconditions",
            "steps-", "step-", "control-", "ready-", "database-", "integration-", "pending-", "contract-",
            "design-", "protocol-", "logic-", "scenario-plan-", "value-resolution-", "version-lock-", "generation-",
        ),
        "scenario_split": ("generation-schema", "generation-mode", "generation-degradation", "multi-scenario-mode"),
        "scenario_ownership": ("generation-owner", "generation-scope", "multi-scenario-owner"),
    }
    if stage in discovery_prefixes:
        assigned = discovery_prefixes
    elif stage in contract_prefixes:
        assigned = contract_prefixes
    elif stage == "shared_integration":
        known = tuple(prefix for prefixes in contract_prefixes.values() for prefix in prefixes)
        return [error for error in errors if not _diagnostic_rule(error).startswith(known)]
    else:
        return errors
    prefixes = assigned[stage]
    selected = [error for error in errors if _diagnostic_rule(error).startswith(prefixes)]
    if stage == "workspace_inventory":
        known = tuple(prefix for values in discovery_prefixes.values() for prefix in values)
        selected.extend(error for error in errors if not _diagnostic_rule(error).startswith(known))
    return list(dict.fromkeys(selected))


def _ordered_stage_errors(project_root: Path, stage: str, selected: str | None) -> list[str]:
    """执行单个发现或契约阶段，并校验前一阶段内容摘要。"""

    discovery_order = ("workspace_inventory", "dependency_topology", "initial_configuration", "runtime_probe")
    contract_order = ("control_matrix", "scenario_split", "scenario_ownership", "shared_integration")
    if stage == "workspace_inventory":
        _start_gate_session(project_root)
    session_errors = _gate_session_errors(project_root, stage)
    if session_errors:
        return session_errors
    if stage in discovery_order:
        index = discovery_order.index(stage)
        errors: list[str] = []
        if index:
            errors.extend(_seal_errors(project_root, discovery_order[index - 1], _discovery_inputs(project_root)))
        aggregate, discovery = discovery_errors(project_root)
        errors.extend(_scoped_errors(aggregate, stage))
        if stage == "runtime_probe" and not errors:
            errors.extend(_runtime_probe_live_errors(
                project_root / "discovery" / "workspace.yaml",
                discovery.get("runtime_probe"),
            ))
        if not errors:
            _write_seal(project_root, stage, _discovery_inputs(project_root))
            if stage == "runtime_probe":
                _write_seal(project_root, "discovery", _discovery_inputs(project_root))
            _advance_gate_session(project_root, stage)
        return errors
    if stage in contract_order:
        index = contract_order.index(stage)
        previous_gate = "discovery" if index == 0 else contract_order[index - 1]
        previous_inputs = _discovery_inputs(project_root) if index == 0 else _contract_inputs(project_root, selected)
        errors = _seal_errors(project_root, previous_gate, previous_inputs)
        aggregate, _, _ = contract_errors(project_root, selected)
        errors.extend(_scoped_errors(aggregate, stage))
        if not errors:
            _write_seal(project_root, stage, _contract_inputs(project_root, selected))
            if stage == "shared_integration":
                _write_seal(project_root, "contracts", _contract_inputs(project_root, selected))
            _advance_gate_session(project_root, stage)
        return errors
    raise ValueError(f"未知有序阶段: {stage}")


def run_ordered(project_root: Path, scenario: str | None, pytest_args: list[str], *, static_only: bool) -> int:
    """按不可跳过的固定顺序运行门禁、冒烟、业务和恢复汇总。"""

    project_root = project_root.resolve()
    run_id = uuid.uuid4().hex
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    evidence_root = project_root / "artifacts" / "evidence" / run_id
    environment = os.environ.copy()
    environment.pop("PYTEST_ADDOPTS", None)
    environment.pop("PYTEST_PLUGINS", None)
    environment.pop("E2E_EVIDENCE_DIR", None)
    environment.pop("E2E_RUN_ID", None)
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    evidence_environment = environment.copy()
    evidence_environment["E2E_EVIDENCE_DIR"] = str(evidence_root)
    evidence_environment["E2E_RUN_ID"] = run_id
    stages = E2E_RUN_STAGES
    outcomes = {name: {"status": "N/A", "exit_code": None} for name in stages}
    report: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "started_at": started,
        "finished_at": None,
        "selected_scenario": scenario,
        "static_only": static_only,
        "stages": outcomes,
        "discovery": {},
        "design": {},
        "protocol": {},
        "coverage": {},
        "exclusions": [],
        "support_only": {},
        "differences": [],
        "source_versions": [],
        "scenarios": [],
        "evidence_diagnostics": [],
    }
    executions: dict[str, dict[str, Any]] = {}
    run_event_paths: set[Path] = set()
    try:
        active_probe = read_only_environment_probe(project_root)
    except Exception as exc:
        active_probe = {
            "requested": True,
            "outcome": "blocked",
            "blockers": [f"probe:{type(exc).__name__}"],
            "listeners": [],
            "processes": [],
            "associations": [],
            "read_only_smoke": [],
            "configuration_checks": [],
        }

    def finish(exit_code: int) -> int:
        """聚合当前证据、写报告并保留原始失败状态。"""

        events, evidence_errors = _events(evidence_root, run_id, run_event_paths)
        if evidence_errors:
            report["evidence_diagnostics"] = evidence_errors
            if exit_code == 0:
                exit_code = 1
        contract_diagnostics, selected_scenarios, discovery = contract_errors(project_root, scenario)
        inventory = discovery.get("inventory", {}) if isinstance(discovery, dict) else {}
        repositories = inventory.get("repositories", [])
        repository_schema = WORKSPACE_DOCUMENT_SCHEMA["properties"]["inventory"]["properties"]["repositories"]
        if validate_schema(repository_schema, repositories):
            repositories = []
        topology = discovery.get("topology", {})
        if validate_schema(WORKSPACE_DOCUMENT_SCHEMA["properties"]["topology"], topology):
            topology = {}
        configuration = discovery.get("configuration", {})
        if validate_schema(WORKSPACE_DOCUMENT_SCHEMA["properties"]["configuration"], configuration):
            configuration = {}
        runtime_probe = discovery.get("runtime_probe", {})
        if validate_schema(WORKSPACE_DOCUMENT_SCHEMA["properties"]["runtime_probe"], runtime_probe):
            runtime_probe = {}
        report["discovery"] = {
            "repositories": repositories,
            "existing_e2e": inventory.get("existing_e2e", []),
            "topology": topology,
            "configuration": configuration,
            "runtime_probe": runtime_probe,
            "active_runtime_probe": active_probe,
            "diagnostics": contract_diagnostics if exit_code else [],
        }
        if active_probe.get("outcome") == "blocked":
            report["discovery"]["diagnostics"].append(
                f"active read-only probe blocked: {active_probe.get('blockers', ['unknown'])}"
            )
        def artifact(name: str) -> dict[str, Any]:
            artifact_path = project_root / "discovery" / name
            try:
                value = yaml.safe_load(artifact_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, yaml.YAMLError):
                return {}
            return value if isinstance(value, dict) else {}

        design_artifact = artifact("design-rules.yaml")
        protocol_artifact = artifact("protocol-rules.yaml")
        report["design"] = {
            "documents": design_artifact.get("documents", []),
            "rules": [
                {"id": item.get("id"), "section": item.get("section"), "source": item.get("source")}
                for item in design_artifact.get("rules", []) if isinstance(item, dict)
            ],
            "manual_confirmation": [
                item.get("id") for item in design_artifact.get("rules", [])
                if isinstance(item, dict) and item.get("manual_confirmation")
            ],
        }
        report["protocol"] = {
            "documents": protocol_artifact.get("documents", []),
            "operations": [
                {"id": item.get("id"), "kind": item.get("kind"), "method": item.get("method"), "path": item.get("path"), "source": item.get("source")}
                for item in protocol_artifact.get("operations", []) if isinstance(item, dict)
            ],
        }
        value_path = project_root / "config" / "value-resolution.yaml"
        try:
            value_resolution = yaml.safe_load(value_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            value_resolution = {}
        report["support_only"] = {
            "repository_versions": [
                {"repo": item.get("id"), "commit": item.get("commit")}
                for item in repositories if isinstance(item, dict)
            ],
            "configuration": configuration,
            "value_resolution": value_resolution if isinstance(value_resolution, dict) else {},
        }
        report["coverage"] = {
            "design_to_scenario": {
                "rule_count": len(report["design"]["rules"]),
                "rule_ids": [str(item.get("id")) for item in report["design"]["rules"] if item.get("id")],
                "scenario_references": sum(
                    1 for _, definition in selected_scenarios
                    for step in definition.get("steps", [])
                    if isinstance(step, dict) and step.get("design_rule_id")
                ),
            },
            "protocol_to_call": {
                "operation_count": len(report["protocol"]["operations"]),
                "operation_ids": [str(item.get("id")) for item in report["protocol"]["operations"] if item.get("id")],
                "calls": sum(len(item.get("endpoint_calls", [])) for item in report.get("scenarios", [])),
            },
        }
        referenced_rules = {
            str(step.get("design_rule_id"))
            for _, definition in selected_scenarios
            for step in definition.get("steps", [])
            if isinstance(step, dict) and step.get("design_rule_id")
        }
        referenced_protocols = {
            str(step.get("protocol_ref"))
            for _, definition in selected_scenarios
            for step in definition.get("steps", [])
            if isinstance(step, dict) and step.get("protocol_ref")
        }
        report["coverage"]["design_to_scenario"]["unreferenced_rules"] = sorted(
            set(report["coverage"]["design_to_scenario"]["rule_ids"]) - referenced_rules
        )
        report["coverage"]["protocol_to_call"]["unused_operations"] = sorted(
            set(report["coverage"]["protocol_to_call"]["operation_ids"]) - referenced_protocols
        )
        exclusions_path = project_root / "discovery" / "exclusions.yaml"
        try:
            exclusions_document = yaml.safe_load(exclusions_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            exclusions_document = {}
        if isinstance(exclusions_document, dict) and isinstance(exclusions_document.get("exclusions"), list):
            report["exclusions"] = exclusions_document["exclusions"]
        report["scenarios"] = _report_scenarios(
            selected_scenarios,
            events,
            executions,
            outcomes["read_only_smoke"]["status"],
        )
        report["coverage"]["protocol_to_call"]["calls"] = sum(
            len(item.get("endpoint_calls", [])) for item in report["scenarios"]
        )
        report["differences"] = [
            {
                "scenario": item["name"],
                "step": step["id"],
                "design_rule_id": step.get("design_rule_id"),
                "protocol_ref": step.get("protocol_ref"),
                "expected": step.get("expected", []),
                "actual": step.get("actual"),
                "status": step["status"],
                "reason": step["reason"],
            }
            for item in report["scenarios"]
            for step in item.get("step_results", [])
            if step.get("status") in {"product_gap", "runtime_failure"}
        ]
        report["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        outcomes["summary"] = {"status": "passed" if exit_code == 0 else "failed", "exit_code": exit_code}
        path = _write_report(project_root, report)
        print(f"E2E 报告: {path}")
        return exit_code

    invalid_pytest_args = _pytest_arg_errors(pytest_args)
    if invalid_pytest_args:
        print(f"不允许改变业务测试选择或成功语义的 pytest 参数: {invalid_pytest_args}", file=sys.stderr)
        outcomes["business"] = {"status": "failed", "exit_code": 2}
        return finish(2)

    selector = ["--scenario", scenario] if scenario else []
    checker = [sys.executable, "-m", "dev_ai", "e2e", "check", "--project", str(project_root)]
    for stage in ("workspace_inventory", "dependency_topology", "initial_configuration", "runtime_probe"):
        code = _run([*checker, "--gate", stage], project_root, environment)
        outcomes[stage] = {"status": "passed" if code == 0 else "failed", "exit_code": code}
        if code:
            return finish(code)

    for stage in ("control_matrix", "scenario_split", "scenario_ownership", "shared_integration"):
        code = _run([*checker, "--gate", stage, *selector], project_root, environment)
        outcomes[stage] = {"status": "passed" if code == 0 else "failed", "exit_code": code}
        if code:
            return finish(code)

    static_code = _run([*checker, "--gate", "static", *selector], project_root, environment)
    outcomes["static"] = {"status": "passed" if static_code == 0 else "failed", "exit_code": static_code}
    if static_code:
        return finish(static_code)

    tests_code = _run([
        sys.executable, "-m", "pytest", "tests", "-m", "not read_only_smoke and not business_e2e", "--maxfail=1",
    ], project_root, environment)
    outcomes["environment_tests"] = {"status": "passed" if tests_code == 0 else "failed", "exit_code": tests_code}
    if tests_code:
        return finish(tests_code)

    source_code = _run([
        sys.executable, "-m", "dev_ai", "e2e", "source-status", "--project", str(project_root), *selector,
    ], project_root, environment)
    source_errors_found, source_results = source_version_results(project_root, scenario)
    report["source_versions"] = source_results
    outcomes["source_versions"] = {"status": "passed" if source_code == 0 else "failed", "exit_code": source_code}
    if source_code or source_errors_found:
        return finish(source_code or 1)

    collect_target = str(project_root / "scenarios" / scenario) if scenario else str(project_root / "scenarios")
    collect_code = _run([sys.executable, "-m", "pytest", "--collect-only", collect_target], project_root, environment)
    outcomes["collect"] = {"status": "passed" if collect_code == 0 else "failed", "exit_code": collect_code}
    if collect_code:
        return finish(collect_code)
    if static_only:
        return finish(0)

    _, selected_scenarios, discovery = contract_errors(project_root, scenario)
    probe = discovery.get("runtime_probe", {}) if isinstance(discovery, dict) else {}
    if any("constructability" in definition for _, definition in selected_scenarios) and (
        probe.get("requested") is not True or probe.get("outcome") != "completed"
    ):
        for directory, _ in selected_scenarios:
            executions[directory.name] = {
                "status": "N/A",
                "exit_code": None,
                "reason": "执行型任务必须先完成本地只读运行探测",
            }
        print("执行型任务缺少 completed 的本地只读运行探测", file=sys.stderr)
        outcomes["read_only_smoke"] = {"status": "failed", "exit_code": 8}
        return finish(8)
    if probe.get("requested") is True and probe.get("read_only_smoke"):
        for directory, _ in selected_scenarios:
            smoke_targets = _smoke_paths(project_root, {directory.name})
            if not smoke_targets:
                print(f"场景缺少可静态绑定的只读冒烟模块: {directory.name}", file=sys.stderr)
                outcomes["read_only_smoke"] = {"status": "failed", "exit_code": 1}
                return finish(1)
            before = set(evidence_root.glob("*.json")) if evidence_root.is_dir() else set()
            smoke_junit = evidence_root / f"smoke-{directory.name}.xml"
            smoke_junit.parent.mkdir(parents=True, exist_ok=True)
            smoke_code = _run([
                sys.executable, "-m", "pytest", *(str(path) for path in smoke_targets),
                "-m", "read_only_smoke", "--maxfail=1",
                f"--confcutdir={project_root / 'tests' / 'runtime'}", f"--junitxml={smoke_junit}",
            ], project_root, evidence_environment)
            created = set(evidence_root.glob("*.json")) - before if evidence_root.is_dir() else set()
            run_event_paths.update(created)
            smoke_events, smoke_evidence_errors = _events(evidence_root, run_id, created)
            smoke_scenarios = {
                str(event.get("scenario")) for event in smoke_events
                if event.get("kind") == "endpoint" and event.get("details", {}).get("phase") == "smoke"
            }
            if smoke_code or not _pytest_junit_passed(smoke_junit) or smoke_evidence_errors or smoke_scenarios != {directory.name}:
                print(
                    f"只读冒烟必须由当前场景新增合法 endpoint 证据: expected={directory.name}, actual={sorted(smoke_scenarios)}",
                    file=sys.stderr,
                )
                outcomes["read_only_smoke"] = {"status": "failed", "exit_code": smoke_code or 1}
                return finish(smoke_code or 1)
        outcomes["read_only_smoke"] = {"status": "passed", "exit_code": 0}

    from . import runtime as runtime_module
    business_code = 0
    for directory, definition in selected_scenarios:
        contract_steps = [step for step in definition.get("steps", []) if isinstance(step, dict)]
        executable_steps = [step for step in contract_steps if step.get("status", "executable") == "executable"]
        if not executable_steps and ("constructability" in definition or definition.get("meta", {}).get("status") != "ready"):
            executions[directory.name] = {
                "status": "N/A",
                "exit_code": None,
                "reason": "没有具备安全执行条件的步骤；逐步骤阻塞原因见 step_results",
            }
            business_code = business_code or 8
            continue
        try:
            runtime_module.preflight(project_root, directory.name, environ=environment)
        except Exception as exc:
            print(f"运行预检失败 [{directory.name}]: {exc}", file=sys.stderr)
            executions[directory.name] = {"status": "failed", "exit_code": 1, "reason": f"运行预检失败: {exc}"}
            business_code = 1
            continue
        target = str(directory / f"test_{directory.name}.py")
        before = set(evidence_root.glob("*.json")) if evidence_root.is_dir() else set()
        business_junit = evidence_root / f"business-{directory.name}.xml"
        business_junit.parent.mkdir(parents=True, exist_ok=True)
        code = _run([
            sys.executable, "-m", "pytest", target, "-m", "business_e2e",
            f"--confcutdir={directory}", f"--junitxml={business_junit}", *pytest_args,
        ], project_root, evidence_environment)
        created = set(evidence_root.glob("*.json")) - before if evidence_root.is_dir() else set()
        run_event_paths.update(created)
        current_events, current_evidence_errors = _events(evidence_root, run_id, created)
        scenario_events = [event for event in current_events if event.get("scenario") == directory.name]
        foreign_events = [event for event in current_events if event.get("scenario") != directory.name]
        entered = any(event.get("kind") == "business_entered" for event in scenario_events)
        endpoints = [event for event in scenario_events if event.get("kind") == "endpoint" and event.get("details", {}).get("phase") == "business"]
        observed_controls = {
            (
                str(event.get("details", {}).get("control_kind")),
                str(event.get("details", {}).get("action")),
                str(event.get("details", {}).get("side_effect")),
            )
            for event in scenario_events if event.get("kind") == "control"
        }
        controls = definition.get("controls", {})
        planned = {
            (str(step.get("control")), str(step.get("action")), str(step.get("side_effect")))
            for step in executable_steps
        }
        missing_controls = planned - observed_controls
        unexpected_correlations = _unexpected_control_correlations(definition, scenario_events)
        planned_api = any(name in {"public_api", "test_or_admin_api"} for name, _, _ in planned)
        step_events = [event.get("details", {}) for event in scenario_events if event.get("kind") == "step"]
        expected_step_ids = [str(step.get("id")) for step in contract_steps]
        actual_step_ids = [str(item.get("step_id")) for item in step_events]
        step_evidence_required = "constructability" in definition
        invalid_step_coverage = step_evidence_required and (
            sorted(actual_step_ids) != sorted(expected_step_ids) or len(actual_step_ids) != len(set(actual_step_ids))
        )
        planned_status = {str(step.get("id")): str(step.get("status", "executable")) for step in contract_steps}
        invalid_reclassification = any(
            planned_status.get(str(item.get("step_id"))) == "executable"
            and item.get("status") in {"environment_missing", "authorization_missing", "control_gap"}
            for item in step_events
        )
        reported_failure = any(item.get("status") in {"product_gap", "runtime_failure"} for item in step_events)
        reported_blocker = any(
            item.get("status") in {"environment_missing", "authorization_missing", "control_gap"}
            for item in step_events
        )
        missing_failure_evidence = step_evidence_required and code != 0 and not reported_failure and not reported_blocker
        evidence_failure = bool(
            current_evidence_errors or foreign_events or not entered or missing_controls or unexpected_correlations
            or invalid_step_coverage or invalid_reclassification or missing_failure_evidence
        )
        if planned_api and not endpoints:
            evidence_failure = True
        if evidence_failure:
            print(
                f"业务证据不完整 [{directory.name}]: entered={entered}, missing_controls={sorted(missing_controls)}, "
                f"unexpected_correlations={sorted(unexpected_correlations)}, foreign_events={len(foreign_events)}, "
                f"business_endpoints={len(endpoints)}, step_ids={actual_step_ids}",
                file=sys.stderr,
            )
        protocol_controls = [
            (str(step.get("id")), str(step.get("protocol_ref")))
            for step in executable_steps
            if step.get("protocol_ref") and step.get("control") in {"messages", "scheduled_jobs", "test_or_admin_api"}
        ]
        observed_protocol_controls = {
            (str(event.get("details", {}).get("step_id")), str(event.get("details", {}).get("protocol_ref")))
            for event in scenario_events
            if event.get("kind") == "control" and event.get("details", {}).get("step_id")
        }
        if set(protocol_controls) - observed_protocol_controls:
            evidence_failure = True
            print(f"协议控制缺少逐步骤运行证据 [{directory.name}]: {sorted(set(protocol_controls) - observed_protocol_controls)}", file=sys.stderr)
        partial_or_failed = reported_failure or reported_blocker
        scenario_code = code or (1 if evidence_failure or partial_or_failed or not _pytest_junit_passed(business_junit) else 0)
        executions[directory.name] = {
            "status": "passed" if scenario_code == 0 else "failed",
            "exit_code": scenario_code,
            "reason": (
                "pytest 与逐步骤运行证据均通过" if scenario_code == 0 else
                "场景仅部分执行" if reported_blocker and not reported_failure else
                "业务结果失败" if reported_failure else
                "pytest 失败或运行证据不完整"
            ),
        }
        if scenario_code:
            business_code = business_code or scenario_code
    outcomes["business"] = {"status": "passed" if business_code == 0 else "failed", "exit_code": business_code}

    events, evidence_errors = _events(evidence_root, run_id, run_event_paths)
    write_definitions = {
        directory.name: definition for directory, definition in selected_scenarios
        if executions.get(directory.name, {}).get("status") in {"passed", "failed"}
        and any(
            event.get("kind") == "business_entered" and event.get("scenario") == directory.name
            for event in events
        )
        and any(isinstance(step, dict) and step.get("side_effect") == "write" for step in definition.get("steps", []))
    }
    restoration_events = [
        event for event in events
        if event.get("kind") == "restoration" and event.get("scenario") in write_definitions
    ]
    failed_restore = any(event.get("details", {}).get("status") != "passed" for event in restoration_events)
    restored_resources = {
        scenario_name: {
            str(resource)
            for event in restoration_events
            if event.get("scenario") == scenario_name and event.get("details", {}).get("status") == "passed"
            for resource in event.get("details", {}).get("resources", [])
        }
        for scenario_name in write_definitions
    }
    expected_resources = {
        scenario_name: {
            str(resource.get("identity"))
            for resource in definition.get("isolation", {}).get("owned_resources", [])
            if isinstance(resource, dict)
        }
        for scenario_name, definition in write_definitions.items()
    }
    resource_mismatch = {
        name: {"expected": sorted(expected_resources[name]), "actual": sorted(restored_resources[name])}
        for name in write_definitions if restored_resources[name] != expected_resources[name]
    }
    if not write_definitions:
        restoration_code = 0
    else:
        restoration_code = 1 if evidence_errors or failed_restore or resource_mismatch else 0
        if resource_mismatch:
            print(f"恢复资源与场景拥有资源不一致: {resource_mismatch}", file=sys.stderr)
        outcomes["restoration"] = {
            "status": "passed" if restoration_code == 0 else "failed",
            "exit_code": restoration_code,
        }
    return finish(business_code or restoration_code)


def check_gate(project_root: Path, gate: str, scenario: str | None = None) -> list[str]:
    if gate in ORDERED_GATES:
        return _ordered_stage_errors(project_root, gate, scenario)
    if gate == "discovery":
        return discovery_errors(project_root)[0]
    if gate == "contracts":
        return contract_errors(project_root, scenario)[0]
    if gate == "static":
        errors = _gate_session_errors(project_root, "static")
        errors.extend(_seal_errors(project_root, "discovery", _discovery_inputs(project_root)))
        errors.extend(_seal_errors(project_root, "contracts", _contract_inputs(project_root, scenario)))
        errors.extend(static_errors(project_root, scenario))
        if not errors:
            _write_seal(project_root, "static", _contract_inputs(project_root, scenario))
            _advance_gate_session(project_root, "static")
        return errors
    return static_errors(project_root, scenario)


pytest_arg_errors = _pytest_arg_errors

__all__ = ["ORDERED_GATES", "check_gate", "pytest_arg_errors", "run_ordered"]
