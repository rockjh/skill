"""Source-backed business context helpers for E2E planning."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


RULE_STATUSES = ("confirmed", "manual_confirmation", "conflict", "not_applicable", "missing_evidence")

_SENTENCE_RE = re.compile(r"(?s)([^.!?。！？\n]+[.!?。！？]?)")
_PARTICIPANT_RE = re.compile(
    r"(?i)\b(?:participants?|actors?|services?|systems?|components?)\b\s*"
    r"(?:are|include|involve|:)?\s*([^.!?;。！？\n]+)"
)
_ACTOR_RE = re.compile(r"(?i)\b([A-Za-z][A-Za-z0-9_-]{1,60})\s+(?:service|system|module|component)\b")
_ENTITY_RE = re.compile(
    r"(?i)\b(?:entity|entities|record|resource|object|document)\b\s*"
    r"(?:is|are|:)?\s*([A-Za-z][A-Za-z0-9 _-]{1,60})"
)
_STATE_RE = re.compile(
    r"(?i)(?:becomes?|moves?\s+to|state|status|final\s+status|result)\s*"
    r"(?:is|are|to|:|=)?\s*([A-Za-z][A-Za-z0-9_.:-]{1,60})"
)
_CHINESE_PARTICIPANT_RE = re.compile(r"(?:参与方|服务|系统|模块)\s*[:：]?\s*([^，。；;\n]+)")
_CHINESE_ENTITY_RE = re.compile(r"(?:实体|记录|资源|对象)\s*[:：]?\s*([^，。；;\n]+)")
_CHINESE_STATE_RE = re.compile(r"(?:状态|结果|最终状态)\s*(?:为|是|：|:|=)\s*([^，。；;\n]+)")


def _unique(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    elif not isinstance(values, (list, tuple, set)):
        try:
            values = list(values)
        except TypeError:
            return []
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _marker_values(text: str, names: tuple[str, ...]) -> list[str]:
    pattern = re.compile(rf"(?im)^\s*(?:[-*]\s*)?(?:{'|'.join(names)})\s*:\s*(.+?)\s*$")
    return _unique(part.strip() for value in pattern.findall(text) for part in re.split(r"[,;]", value))


def _natural_language_values(body: str) -> dict[str, list[str]]:
    """Extract conservative facts from prose without inventing business rules."""

    sentences = [part.strip(" \t\r\n-*") for part in _SENTENCE_RE.findall(body) if part.strip()]
    participants: list[str] = []
    entities: list[str] = []
    states: list[str] = []
    observations: list[str] = []
    async_behaviors: list[str] = []
    retries: list[str] = []
    idempotency: list[str] = []
    recovery: list[str] = []
    for sentence in sentences:
        for match in _PARTICIPANT_RE.finditer(sentence):
            participants.extend(re.split(r"[,;]|\band\b|\bor\b", match.group(1)))
        for match in _CHINESE_PARTICIPANT_RE.finditer(sentence):
            participants.extend(re.split(r"[、，,和及]", match.group(1)))
        for match in _ACTOR_RE.finditer(sentence):
            participants.append(match.group(1))
        for match in _ENTITY_RE.finditer(sentence):
            entities.extend(re.split(r"[,;]|\band\b|\bor\b", match.group(1)))
        for match in _CHINESE_ENTITY_RE.finditer(sentence):
            entities.extend(re.split(r"[、，,和及]", match.group(1)))
        for match in _STATE_RE.finditer(sentence):
            states.extend(re.split(r"[,;]|\band\b|\bor\b", match.group(1)))
        for match in _CHINESE_STATE_RE.finditer(sentence):
            states.extend(re.split(r"[、，,和及]", match.group(1)))
        if re.search(r"(?i)\b(?:observe|query|inspect|verify|poll|read|watch|check)\b|查询|观察|轮询|验证|检查|读取", sentence):
            observations.append(sentence)
        if re.search(r"(?i)\b(?:async(?:hronous)?|message|event|callback|queue|eventually)\b|寮傛|娑堟伅|浜嬩欢", sentence):
            async_behaviors.append(sentence)
        if re.search(r"异步|消息|事件|回调|队列|最终一致性", sentence):
            async_behaviors.append(sentence)
        if re.search(r"(?i)\b(?:retry|retries|backoff)\b|閲嶈瘯", sentence):
            retries.append(sentence)
        if re.search(r"重试|退避", sentence):
            retries.append(sentence)
        if re.search(r"(?i)\bidempot(?:ent|ency)\b|骞傜瓑", sentence):
            idempotency.append(sentence)
        if re.search(r"幂等|重复提交", sentence):
            idempotency.append(sentence)
        if re.search(r"(?i)\b(?:recover|recovery|compensat|restore)\b|鎭㈠|琛ュ伩", sentence):
            recovery.append(sentence)
        if re.search(r"恢复|补偿|回滚", sentence):
            recovery.append(sentence)
    def prose_only(values: list[str], labels: str) -> list[str]:
        return _unique(
            value for value in values
            if not re.match(rf"^\s*(?:{labels})\s*[:：]", value, re.IGNORECASE)
        )

    # A prose sentence with multiple service-like subjects is useful context,
    # but it is intentionally not promoted to a confirmed business rule.
    return {
        "participants": _unique(value.strip(" .:") for value in participants),
        "entities": _unique(value.strip(" .:") for value in entities),
        "states": _unique(value.strip(" .:") for value in states),
        "observations": _unique(observations),
        "async_behaviors": prose_only(async_behaviors, r"async(?:hronous)?[_ ]?behavior|异步行为"),
        "retries": prose_only(retries, r"retry|retries|重试(?:规则)?"),
        "idempotency": prose_only(idempotency, r"idempotency|幂等(?:规则)?|重复提交规则"),
        "recovery": prose_only(recovery, r"recovery|恢复|补偿"),
    }


def build_context(rule: Mapping[str, Any], *, body: str = "") -> dict[str, Any]:
    """Normalize facts from a design rule without inventing domain vocabulary."""

    explicit = rule.get("context") if isinstance(rule.get("context"), Mapping) else {}
    calls = rule.get("calls") if isinstance(rule.get("calls"), list) else []
    operations = _unique(
        item.get("path") or item.get("event") or item.get("task")
        for item in calls if isinstance(item, Mapping)
    )
    prose = _natural_language_values(body)
    context = {
        "participants": _unique(explicit.get("participants") or rule.get("participants") or prose["participants"]),
        "services": _unique(explicit.get("services") or rule.get("participants") or prose["participants"]),
        "modules": _unique(explicit.get("modules") or rule.get("modules") or _marker_values(body, ("module", "component"))),
        "entities": _unique(explicit.get("entities") or _marker_values(body, ("entity",)) or prose["entities"]),
        "entry_operations": _unique(explicit.get("entry_operations") or operations),
        "states": _unique(explicit.get("states") or rule.get("states") or prose["states"]),
        "transitions": _unique(explicit.get("transitions") or rule.get("transitions")),
        "branches": _unique(explicit.get("branches") or rule.get("branches")),
        "exceptions": _unique(explicit.get("exceptions") or rule.get("exceptions")),
        "async_behaviors": _unique(explicit.get("async_behaviors") or rule.get("async_behavior") or prose["async_behaviors"]),
        "retries": _unique(explicit.get("retries") or rule.get("retries") or prose["retries"]),
        "idempotency": _unique(explicit.get("idempotency") or rule.get("idempotency") or prose["idempotency"]),
        "external_dependencies": _unique(explicit.get("external_dependencies") or _marker_values(body, ("external dependency", "dependency"))),
        "correlation_keys": _unique(explicit.get("correlation_keys") or _marker_values(body, ("correlation key", "correlation"))),
        "side_effects": _unique(explicit.get("side_effects") or rule.get("side_effects")),
        "observations": _unique(explicit.get("observations") or _marker_values(body, ("observation", "observe", "query")) or prose["observations"]),
        "cleanup": _unique(explicit.get("cleanup") or rule.get("cleanup")),
        "recovery": _unique(explicit.get("recovery") or rule.get("recovery") or prose["recovery"]),
    }
    if not context["services"]:
        context["services"] = list(context["participants"])
    context["evidence"] = dict(rule.get("source", {})) if isinstance(rule.get("source"), Mapping) else {}
    return context


def classify_rule(rule: Mapping[str, Any], *, conflict: bool = False) -> str:
    """Classify only the current rule; unrelated rules remain generatable."""

    if conflict:
        return "conflict"
    if str(rule.get("not_applicable", "")).casefold() in {"true", "yes", "1"}:
        return "not_applicable"
    if rule.get("manual_confirmation") is True:
        return "manual_confirmation"
    context = rule.get("context") if isinstance(rule.get("context"), Mapping) else {}
    if not any(
        _unique(rule.get(field)) or _unique(context.get(field))
        for field in ("participants", "assertions", "states", "final_statuses", "side_effects")
    ):
        return "missing_evidence"
    return "confirmed"


def context_status(rule: Mapping[str, Any]) -> dict[str, Any]:
    """Return status and precise missing facts for minimal user questions."""

    context = rule.get("context") if isinstance(rule.get("context"), Mapping) else {}
    missing = [name for name in ("participants", "entry_operations", "states", "observations") if not _unique(context.get(name))]
    status = str(rule.get("status") or classify_rule(rule))
    normalized = status if status in RULE_STATUSES else "missing_evidence"
    return {
        "status": normalized,
        "missing": missing,
        "impact": "scenario_only" if normalized != "confirmed" else "none",
        "questions": confirmation_questions(rule, missing=missing) if normalized != "confirmed" else [],
    }


def confirmation_questions(rule: Mapping[str, Any], *, missing: list[str] | None = None) -> list[dict[str, Any]]:
    """Build the smallest actionable confirmation request for one rule."""

    context = rule.get("context") if isinstance(rule.get("context"), Mapping) else {}
    missing = list(missing if missing is not None else context_status(rule)["missing"])
    if not missing:
        return []
    known = {
        field: _unique(context.get(field))
        for field in ("participants", "entry_operations", "states", "observations")
        if _unique(context.get(field))
    }
    return [{
        "rule_id": str(rule.get("id", "")),
        "known": known,
        "uncertain": missing,
        "impact": "only this scenario and its dependent steps",
        "choices": ["confirm extracted facts", "provide missing facts", "exclude this rule"],
        "default": "keep blocked until confirmed",
        "without_confirmation": "leave the scenario blocked and generate no business execution claim",
    }]
