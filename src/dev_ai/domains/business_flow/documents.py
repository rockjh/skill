"""Render business-flow Markdown, indexes, and coverage reports."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ...core.artifacts import write_json
from ...core.redaction import redact
from ...core.schema import (
    BUSINESS_FLOW_DISCOVERY_SCHEMA,
    BUSINESS_FLOW_INDEX_SCHEMA,
    BUSINESS_FLOW_MODULE_MAP_SCHEMA,
    BUSINESS_FLOW_REPORT_SCHEMA,
    validate_schema,
)
from .models import BehaviorEvidence, EntryPoint, ErrorEvidence, GitInfo, ScanResult


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-") or "common"


def _display(value: str) -> str:
    if value == "公共能力":
        return value
    return value.replace("-", " ").replace("_", " ").title()


def _entry_title(entry: EntryPoint) -> str:
    names = {
        "url": "URL",
        "webhook": "Webhook",
        "websocket": "WebSocket",
        "sse": "SSE",
        "rpc": "RPC",
        "message": "消息",
        "scheduled": "定时任务",
        "event": "事件监听",
        "cli": "命令行任务",
        "file": "文件入口",
        "batch": "批处理任务",
    }
    return f"{names.get(entry.kind, entry.kind)} {entry.identifier} {entry.handler}".strip()


def _codes(entry: EntryPoint) -> str:
    return "；".join(entry.error_codes()) if entry.error_codes() else "无"


def _non_business_candidate(entry: EntryPoint) -> bool:
    return bool(
        re.search(
            r"(?:^|[/ ])(?:health|actuator|metrics|static|swagger|openapi)(?:[/ ]|$)",
            entry.identifier,
            re.IGNORECASE,
        )
    )


def _diagram(entry: EntryPoint) -> str:
    behavior_kinds = {behavior.kind for behavior in entry.behaviors}
    has_persistence = entry.has_persistence or "持久化" in behavior_kinds
    has_external = entry.has_external_call or "外部调用" in behavior_kinds
    has_async = entry.has_async or bool({"消息", "异步"} & behavior_kinds)
    has_storage = "缓存或文件" in behavior_kinds
    has_explicit_result = "结果" in behavior_kinds
    lines = [
        "```mermaid",
        "sequenceDiagram",
        "autonumber",
        "participant Caller as 调用方",
        "participant System as 当前系统",
    ]
    if has_persistence:
        lines.append("participant DB as 数据库")
    if has_storage:
        lines.append("participant Storage as 缓存/文件")
    if has_external:
        lines.append("participant External as 外部系统")
    if has_async:
        lines.append("participant Middleware as 消息/异步系统")
    lines.append(f"Caller->>System: {entry.identifier}")
    lines.append(f"System->>System: 进入 {entry.handler}")
    for behavior in entry.behaviors:
        statement = str(redact(behavior.statement)).replace("\n", " ")[:120]
        if behavior.kind == "持久化":
            lines.append(f"System->>DB: {statement}")
        elif behavior.kind == "缓存或文件":
            lines.append(f"System->>Storage: {statement}")
        elif behavior.kind == "外部调用":
            lines.extend(("opt 外部调用", f"    System->>External: {statement}", "end"))
        elif behavior.kind in {"消息", "异步"}:
            lines.extend((f"opt {behavior.kind}", f"    System->>Middleware: {statement}", "end"))
        else:
            lines.append(f"System->>System: {behavior.kind}：{statement}")
    if entry.has_loop or "循环" in behavior_kinds:
        lines.append("loop 代码中的循环处理")
        lines.append("    System->>System: 逐项处理")
        lines.append("end")
    if entry.errors:
        first, *remaining = entry.errors
        condition = str(redact(first.condition)).replace("\n", " ")[:100]
        lines.append(f"alt {first.code}：{condition}")
        lines.append("    System-->>Caller: 返回代码中映射的错误结果")
        for error in remaining:
            condition = str(redact(error.condition)).replace("\n", " ")[:100]
            lines.append(f"else {error.code}：{condition}")
            lines.append("    System-->>Caller: 返回代码中映射的错误结果")
        if has_explicit_result:
            lines.append("else 成功（存在源码返回证据）")
            lines.append("    System-->>Caller: 返回源码中的成功结果")
        lines.append("end")
        if not has_explicit_result:
            lines.append("Note over Caller,System: 非错误结果代码中未确认")
    else:
        if has_explicit_result:
            lines.append("System-->>Caller: 返回源码中的结果；代码未发现主动错误码")
        else:
            lines.append("Note over Caller,System: 结果代码中未确认；代码未发现主动错误码")
    lines.append("```")
    return "\n".join(lines)


def _entry_text(entry: EntryPoint) -> str:
    error_lines = [
        f"- `{error.code}`：{redact(error.condition)} （证据：`{error.file}:{error.line}`）"
        for error in entry.errors
    ] or ["- 无"]
    behavior_lines = [
        f"{index}. **{behavior.kind}**：`{redact(behavior.statement)}`（证据：`{behavior.file}:{behavior.line}`）"
        for index, behavior in enumerate(entry.behaviors, 1)
    ] or ["1. 代码中未确认可展开的业务步骤。"]
    return "\n".join([
        f"<!-- business-flow-entry: {entry.entry_id} -->",
        "",
        f"## {_entry_title(entry)}",
        "",
        "### 入口说明",
        "",
        f"- 入口类型：`{entry.kind}`；入口处理器：`{entry.handler}`；证据：`{entry.file}:{entry.line}`。",
        f"- 主归属业务模块：`{_display(entry.module)}`；划分依据：{entry.module_rationale.rstrip('。')}。",
        f"- 调用方：{entry.caller}。核心输入：{entry.input_summary}。",
        f"- 调用链中可静态确认的函数：{', '.join(f'`{name}`' for name in entry.functions) or '代码中未确认'}。",
        "- 以下步骤严格按可达源码证据排序；没有证据的事务、锁、幂等、状态变化或外部交互不作实现推断。",
        "",
        *behavior_lines,
        "",
        "### 业务流程",
        "",
        f"主动抛出的带错误码业务异常：`{_codes(entry)}`。",
        "",
        *error_lines,
        "",
        _diagram(entry),
        "",
    ])


def _module_files(docs_root: Path, modules: list[str], previous: dict[str, Any]) -> dict[str, str]:
    old = {str(item.get("name")): str(item.get("file")) for item in previous.get("modules", []) if isinstance(item, dict)}
    used = set(old.values())
    result: dict[str, str] = {}
    for index, module in enumerate(sorted(modules)):
        if module in old:
            result[module] = old[module]
            continue
        candidate = f"{index:02d}-{_slug(module)}.md"
        while candidate in used:
            index += 1
            candidate = f"{index:02d}-{_slug(module)}.md"
        used.add(candidate)
        result[module] = candidate
    return result


def _read_index(docs_root: Path) -> dict[str, Any]:
    path = docs_root / "business-flow-index.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _require_contract(schema: dict[str, Any], value: dict[str, Any], name: str) -> None:
    errors = validate_schema(schema, value)
    if errors:
        raise ValueError(f"invalid generated {name}: {'; '.join(errors)}")


def write_discovery(scan: ScanResult, docs_root: Path) -> tuple[Path, Path]:
    docs_root.mkdir(parents=True, exist_ok=True)
    discovery = {
        "schema_version": 2,
        "source_fingerprint": scan.source_fingerprint,
        "effective_git": {
            "commit": scan.git.target,
            "branch": scan.git.branch,
            "workspace_dirty": scan.git.dirty,
            "includes_uncommitted_changes": scan.git.includes_uncommitted,
        },
        "languages": scan.languages,
        "frameworks": scan.frameworks,
        "source_files": scan.files,
        "entries": [
            {
                "id": entry.entry_id,
                "type": entry.kind,
                "identifier": entry.identifier,
                "handler": entry.handler,
                "source": f"{entry.file}:{entry.line}",
                "suggested_module": entry.module,
                "non_business_candidate": _non_business_candidate(entry),
                "core_capabilities": entry.functions,
                "errors": [
                    {"code": error.code, "condition": str(redact(error.condition)), "source": f"{error.file}:{error.line}"}
                    for error in entry.errors
                ],
            }
            for entry in scan.entries
        ],
        "unresolved": scan.unresolved,
    }
    _require_contract(BUSINESS_FLOW_DISCOVERY_SCHEMA, discovery, "business-flow discovery")
    discovery_path = write_json(docs_root / "business-flow-discovery.json", discovery)
    module_map_path = docs_root / "business-flow-modules.json"
    previous: dict[str, Any] = {}
    if module_map_path.is_file():
        try:
            loaded = json.loads(module_map_path.read_text(encoding="utf-8"))
            previous = loaded if isinstance(loaded, dict) else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            previous = {}
    old_owners = {
        str(entry_id): str(module.get("name"))
        for module in previous.get("modules", [])
        if isinstance(module, dict)
        for entry_id in module.get("entry_ids", [])
    }
    known_candidates = {entry.entry_id for entry in scan.entries}
    previous_exclusions = {
        str(item.get("candidate")): item
        for item in previous.get("exclusions", [])
        if isinstance(item, dict) and str(item.get("candidate", "")) in known_candidates
    }
    for entry in scan.entries:
        if _non_business_candidate(entry) and entry.entry_id not in previous_exclusions:
            previous_exclusions[entry.entry_id] = {
                "candidate": entry.entry_id,
                "reason": "健康检查、框架管理或静态资源入口，不承载业务处理，默认排除。",
                "evidence": [f"{entry.file}:{entry.line}"],
            }
    excluded_ids = set(previous_exclusions)
    groups: dict[str, list[str]] = {}
    for entry in scan.entries:
        if entry.entry_id in excluded_ids:
            continue
        groups.setdefault(old_owners.get(entry.entry_id, entry.module), []).append(entry.entry_id)
    additional_ids = {
        str(item.get("id"))
        for item in previous.get("additional_entries", [])
        if isinstance(item, dict) and item.get("id")
    }
    for entry_id in additional_ids:
        if entry_id in excluded_ids:
            continue
        groups.setdefault(old_owners.get(entry_id, "公共能力"), []).append(entry_id)
    current_ids = ({entry.entry_id for entry in scan.entries} | additional_ids) - excluded_ids
    unchanged = (
        bool(previous)
        and previous.get("source_fingerprint") == scan.source_fingerprint
        and set(old_owners) == current_ids
    )
    module_map = {
        "schema_version": 2,
        "source_fingerprint": scan.source_fingerprint,
        "effective_git": scan.git.target,
        "confirmed": bool(previous.get("confirmed")) and unchanged,
        "resolutions": previous.get("resolutions", []),
        "entry_overrides": previous.get("entry_overrides", []),
        "additional_entries": previous.get("additional_entries", []),
        "exclusions": list(previous_exclusions.values()),
        "modules": [
            {
                "name": name,
                "display_name": _display(name),
                "rationale": next((
                    str(module.get("rationale"))
                    for module in previous.get("modules", [])
                    if isinstance(module, dict) and module.get("name") == name
                ), "待依据业务职责、对象、数据归属和调用关系确认"),
                "entry_ids": sorted(entry_ids),
            }
            for name, entry_ids in sorted(groups.items())
        ],
    }
    _require_contract(BUSINESS_FLOW_MODULE_MAP_SCHEMA, module_map, "business-flow module map")
    write_json(module_map_path, module_map)
    return discovery_path, module_map_path


def apply_module_map(scan: ScanResult, path: Path) -> list[str]:
    if not path.is_file():
        return [f"module map does not exist: {path}; run business-flow discover"]
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"cannot read module map {path}: {exc}"]
    schema_errors = validate_schema(BUSINESS_FLOW_MODULE_MAP_SCHEMA, document)
    if schema_errors:
        return [f"invalid module map: {error}" for error in schema_errors]
    if not isinstance(document, dict) or document.get("confirmed") is not True:
        return [f"module map is not confirmed: {path}"]
    if document.get("source_fingerprint") != scan.source_fingerprint:
        return [
            "module map source fingerprint is stale; run business-flow discover and review/confirm the new map"
        ]

    def source_parts(value: object, fallback: str = "") -> tuple[str, int] | None:
        file, separator, number = str(value or fallback).rpartition(":")
        if not separator or not number.isdigit() or not file:
            return None
        return file, int(number)

    def valid_source(value: object) -> bool:
        location = source_parts(value)
        return bool(
            location
            and location[0] in scan.files
            and 1 <= location[1] <= scan.source_lines.get(location[0], 0)
        )

    resolutions = {
        str(item.get("finding")): item
        for item in document.get("resolutions", [])
        if isinstance(item, dict)
        and str(item.get("finding", "")).strip()
        and str(item.get("resolution", "")).strip()
        and item.get("evidence")
    }
    resolution_findings = [
        str(item.get("finding")) for item in document.get("resolutions", []) if isinstance(item, dict)
    ]
    if len(resolution_findings) != len(set(resolution_findings)):
        return ["module map contains duplicate unresolved finding resolutions"]
    unknown_resolutions = set(resolutions) - set(scan.unresolved)
    if unknown_resolutions:
        return ["module map resolves findings not present in discovery: " + ", ".join(sorted(unknown_resolutions))]
    invalid_resolution_evidence = [
        str(evidence)
        for item in resolutions.values()
        for evidence in item.get("evidence", [])
        if not valid_source(evidence)
    ]
    if invalid_resolution_evidence:
        return ["resolution evidence is not a scanned source location: " + ", ".join(invalid_resolution_evidence)]
    scan.unresolved = [finding for finding in scan.unresolved if finding not in resolutions]

    entry_by_id = {entry.entry_id: entry for entry in scan.entries}
    override_ids = [
        str(item.get("id")) for item in document.get("entry_overrides", []) if isinstance(item, dict)
    ]
    if len(override_ids) != len(set(override_ids)):
        return ["module map contains duplicate entry overrides"]
    unknown_overrides = {
        str(item.get("id"))
        for item in document.get("entry_overrides", [])
        if isinstance(item, dict) and str(item.get("id", "")) not in entry_by_id
    }
    if unknown_overrides:
        return ["module map overrides unknown entries: " + ", ".join(sorted(unknown_overrides))]
    invalid_override_evidence = [
        str(evidence.get("source", ""))
        for override in document.get("entry_overrides", [])
        if isinstance(override, dict)
        for field in ("errors", "behaviors")
        for evidence in override.get(field, [])
        if isinstance(evidence, dict) and not valid_source(evidence.get("source"))
    ]
    if invalid_override_evidence:
        return ["entry override evidence is not a scanned source location: " + ", ".join(invalid_override_evidence)]
    for override in document.get("entry_overrides", []):
        if not isinstance(override, dict) or str(override.get("id")) not in entry_by_id:
            continue
        entry = entry_by_id[str(override["id"])]
        for field in ("kind", "identifier", "handler", "caller", "input_summary"):
            document_field = "type" if field == "kind" else field
            if str(override.get(document_field, "")).strip():
                setattr(entry, field, str(override[document_field]))
        if override.get("core_capabilities"):
            entry.functions = [str(value) for value in override["core_capabilities"]]
        if override.get("errors") is not None:
            entry.errors = [
                ErrorEvidence(str(error.get("code", "代码中未确认")), str(error.get("condition", "代码中未确认")), *(source_parts(error.get("source"), f"{entry.file}:{entry.line}") or (entry.file, entry.line)))
                for error in override.get("errors", []) if isinstance(error, dict)
            ]
        if override.get("behaviors") is not None:
            entry.behaviors = [
                BehaviorEvidence(str(behavior.get("kind", "业务处理")), str(behavior.get("statement", "代码中未确认")), *(source_parts(behavior.get("source"), f"{entry.file}:{entry.line}") or (entry.file, entry.line)))
                for behavior in override.get("behaviors", []) if isinstance(behavior, dict)
            ]

    errors: list[str] = []
    for item in document.get("additional_entries", []):
        if not isinstance(item, dict):
            continue
        source = str(item.get("source", ""))
        location = source_parts(source, "")
        if not location:
            errors.append(f"additional entry {item.get('id', '')} has invalid source location")
            continue
        file, line = location
        if not valid_source(source):
            errors.append(f"additional entry {item.get('id', '')} source is not in the scanned source set: {source}")
            continue
        nested_sources = [
            str(evidence.get("source", ""))
            for field in ("errors", "behaviors")
            for evidence in item.get(field, [])
            if isinstance(evidence, dict) and not valid_source(evidence.get("source"))
        ]
        if nested_sources:
            errors.append(
                f"additional entry {item.get('id', '')} has invalid evidence: {', '.join(nested_sources)}"
            )
            continue
        entry_errors = [
            ErrorEvidence(str(error.get("code", "代码中未确认")), str(error.get("condition", "代码中未确认")), *(source_parts(error.get("source"), source) or (file, line)))
            for error in item.get("errors", [])
            if isinstance(error, dict)
        ]
        behaviors = [
            BehaviorEvidence(str(behavior.get("kind", "业务处理")), str(behavior.get("statement", "代码中未确认")), *(source_parts(behavior.get("source"), source) or (file, line)))
            for behavior in item.get("behaviors", [])
            if isinstance(behavior, dict)
        ]
        scan.entries.append(EntryPoint(
            entry_id=str(item.get("id", "")), kind=str(item.get("type", "other")),
            identifier=str(item.get("identifier", "代码中未确认")), handler=str(item.get("handler", "代码中未确认")),
            file=file, line=line, module="", source=file,
            caller=str(item.get("caller", "代码中未确认")), input_summary=str(item.get("input_summary", "代码中未确认")),
            functions=[str(value) for value in item.get("core_capabilities", [])], errors=entry_errors, behaviors=behaviors,
        ))
    excluded_ids: set[str] = set()
    known_candidates = {entry.entry_id for entry in scan.entries}
    exclusion_candidates = [
        str(item.get("candidate")) for item in document.get("exclusions", []) if isinstance(item, dict)
    ]
    if len(exclusion_candidates) != len(set(exclusion_candidates)):
        errors.append("module map contains duplicate exclusions")
    for exclusion in document.get("exclusions", []):
        if not isinstance(exclusion, dict) or not all(exclusion.get(name) for name in ("candidate", "reason", "evidence")):
            errors.append("each exclusion requires candidate, reason, and evidence")
        elif str(exclusion["candidate"]) not in known_candidates:
            errors.append(f"exclusion references an unknown candidate: {exclusion['candidate']}")
        elif any(not valid_source(value) for value in exclusion["evidence"]):
            errors.append(f"exclusion {exclusion['candidate']} has evidence outside scanned source")
        else:
            excluded_ids.add(str(exclusion["candidate"]))
    if excluded_ids:
        scan.entries = [entry for entry in scan.entries if entry.entry_id not in excluded_ids]
    scan.exclusions = [
        f"{item['candidate']}：{item['reason']}（证据：{', '.join(str(value) for value in item['evidence'])}）"
        for item in document.get("exclusions", [])
        if isinstance(item, dict) and all(item.get(name) for name in ("candidate", "reason", "evidence"))
    ]
    owners: dict[str, str] = {}
    rationales: dict[str, str] = {}
    module_names = [
        str(item.get("name")) for item in document.get("modules", []) if isinstance(item, dict)
    ]
    if len(module_names) != len(set(module_names)):
        errors.append("module map contains duplicate module names")
    for module in document.get("modules", []):
        if not isinstance(module, dict) or not str(module.get("name", "")).strip():
            errors.append("module map contains a module without a name")
            continue
        name = str(module["name"])
        if not module.get("entry_ids"):
            errors.append(f"module {name} has no entries")
        rationales[name] = str(module.get("rationale", ""))
        rationale = str(module.get("rationale", "")).strip()
        if not rationale or rationale in {"代码中未确认", "待依据业务职责、对象、数据归属和调用关系确认"}:
            errors.append(f"module {name} has no boundary rationale")
        for entry_id in module.get("entry_ids", []):
            entry_id = str(entry_id)
            if entry_id in owners:
                errors.append(f"entry {entry_id} belongs to multiple modules")
            owners[entry_id] = name
    expected = {entry.entry_id for entry in scan.entries}
    if "" in expected:
        errors.append("additional entry is missing an id")
    duplicate_ids = {entry.entry_id for entry in scan.entries if sum(item.entry_id == entry.entry_id for item in scan.entries) > 1}
    if duplicate_ids:
        errors.append("duplicate entry ids: " + ", ".join(sorted(duplicate_ids)))
    missing = expected - set(owners)
    stale = set(owners) - expected
    if missing:
        errors.append("module map is missing entries: " + ", ".join(sorted(missing)))
    if stale:
        errors.append("module map contains stale entries: " + ", ".join(sorted(stale)))
    if errors:
        return errors
    for entry in scan.entries:
        entry.module = owners[entry.entry_id]
        entry.module_rationale = rationales[entry.module]
    return []


def build_index(scan: ScanResult, docs_root: Path, files: dict[str, str], *, comparison: str, old_commit: str | None = None) -> dict[str, Any]:
    modules = []
    for module in sorted(files):
        entries = [entry for entry in scan.entries if entry.module == module]
        modules.append({
            "name": module,
            "file": files[module],
            "rationale": entries[0].module_rationale if entries else "代码中未确认",
            "entry_ids": [entry.entry_id for entry in entries],
        })
    return {
        "schema_version": 2,
        "source_fingerprint": scan.source_fingerprint,
        "effective_git": {
            "commit": scan.git.target,
            "branch": scan.git.branch,
            "workspace_dirty": scan.git.dirty,
            "includes_uncommitted_changes": scan.git.includes_uncommitted,
        },
        "comparison": comparison,
        "old_commit": old_commit,
        "languages": scan.languages,
        "frameworks": scan.frameworks,
        "modules": modules,
        "entries": [
            {
                "id": entry.entry_id,
                "type": entry.kind,
                "identifier": entry.identifier,
                "handler": entry.handler,
                "module": entry.module,
                "source": f"{entry.file}:{entry.line}",
                "caller": entry.caller,
                "input_summary": entry.input_summary,
                "core_capabilities": entry.functions,
                "error_codes": entry.error_codes(),
                "errors": [
                    {"code": error.code, "condition": str(redact(error.condition)), "source": f"{error.file}:{error.line}"}
                    for error in entry.errors
                ],
                "behaviors": [
                    {"kind": behavior.kind, "statement": str(redact(behavior.statement)), "source": f"{behavior.file}:{behavior.line}"}
                    for behavior in entry.behaviors
                ],
            }
            for entry in scan.entries
        ],
        "counts": {
            "entries": len(scan.entries),
            "modules": len(modules),
            "error_codes": len({code for entry in scan.entries for code in entry.error_codes() if code != "代码中未确认"}),
            "by_type": {kind: sum(entry.kind == kind for entry in scan.entries) for kind in sorted({entry.kind for entry in scan.entries})},
        },
        "unresolved": scan.unresolved,
    }


def render_module(module: str, entries: list[EntryPoint], git: GitInfo, scan: ScanResult, *, comparison: str) -> str:
    dirty = "；包含未提交变更" if git.includes_uncommitted else ""
    lines = [
        f"# {_display(module)}流程设计",
        "",
        f"> 生效 Git 版本：`{git.target}`{dirty}",
        ">",
        f"> 覆盖说明：本文依据指定版本代码整理，覆盖本模块 {len(entries)} 个可确认业务入口；无法从代码确认的内容保留为“代码中未确认”。",
        f">",
        f"> 模块边界：{(entries[0].module_rationale if entries else '代码中未确认').rstrip('。')}。",
        f">",
        f"> 代码识别：语言 {', '.join(scan.languages) or '代码中未确认'}；框架 {', '.join(scan.frameworks) or '代码中未确认'}；更新模式 `{comparison}`。",
        "",
    ]
    lines.extend(_entry_text(entry) for entry in entries)
    return "\n".join(lines).rstrip() + "\n"


def _update_version_only(path: Path, git: GitInfo) -> None:
    text = path.read_text(encoding="utf-8")
    marker = "；包含未提交变更" if git.includes_uncommitted else ""
    replacement = f"> 生效 Git 版本：`{git.target}`{marker}"
    updated = re.sub(r"^> 生效 Git 版本：.*$", replacement, text, count=1, flags=re.MULTILINE)
    if updated != text:
        path.write_text(redact(updated), encoding="utf-8")


def _markdown_coverage(
    scan: ScanResult,
    index: dict[str, Any],
    docs_root: Path | None,
    module_filter: str | None = None,
) -> dict[str, Any]:
    if docs_root is None:
        return {
            "markdown_document_count": 0,
            "markdown_entry_count": 0,
            "markdown_missing_documents": [],
            "markdown_missing_entries": [],
            "markdown_stale_entries": [],
            "markdown_missing_error_codes": [],
            "markdown_missing_error_evidence": [],
            "markdown_stale_error_evidence": [],
            "markdown_diagram_mismatches": [],
            "markdown_version_mismatches": [],
        }
    entries = [
        item for item in index.get("entries", [])
        if isinstance(item, dict) and (not module_filter or item.get("module") == module_filter)
    ]
    modules = [
        item for item in index.get("modules", [])
        if isinstance(item, dict) and (not module_filter or item.get("name") == module_filter)
    ]
    text_by_module: dict[str, str] = {}
    missing_documents: list[str] = []
    for module in modules:
        filename = str(module.get("file", ""))
        path = docs_root / filename
        if not filename or not path.is_file():
            missing_documents.append(filename or str(module.get("name", "unknown")))
            continue
        try:
            text_by_module[str(module.get("name", ""))] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            missing_documents.append(filename)
    missing_entries: list[str] = []
    stale_entries: list[str] = []
    missing_error_codes: list[str] = []
    missing_error_evidence: list[str] = []
    stale_error_evidence: list[str] = []
    diagram_mismatches: list[str] = []
    version_mismatches: list[str] = []
    marker = re.compile(r"^<!-- business-flow-entry: (.+) -->\s*$", re.MULTILINE)
    expected_commit = str(index.get("effective_git", {}).get("commit", ""))
    for module in modules:
        name = str(module.get("name", ""))
        text = text_by_module.get(name, "")
        module_entries = [entry for entry in entries if str(entry.get("module")) == name]
        matches = list(marker.finditer(text))
        sections = {
            match.group(1): text[match.start():matches[position + 1].start() if position + 1 < len(matches) else len(text)]
            for position, match in enumerate(matches)
        }
        expected_ids = {str(entry.get("id", "")) for entry in module_entries}
        stale_entries.extend(sorted(set(sections) - expected_ids))
        if text and f"> 生效 Git 版本：`{expected_commit}`" not in text:
            version_mismatches.append(name)
        if sum(section.count("sequenceDiagram") for section in sections.values()) != len(module_entries):
            diagram_mismatches.append(name)
        for entry in module_entries:
            entry_id = str(entry.get("id", ""))
            source = str(entry.get("source", ""))
            identifier = str(entry.get("identifier", ""))
            section = sections.get(entry_id, "")
            if not section or source not in section or identifier not in section:
                missing_entries.append(entry_id)
            for code in entry.get("error_codes", []):
                if str(code) != "代码中未确认" and str(code) not in section:
                    missing_error_codes.append(f"{entry_id}:{code}")
            expected_errors: set[tuple[str, str, str]] = set()
            for error in entry.get("errors", []):
                if isinstance(error, dict):
                    code = str(error.get("code", ""))
                    evidence = str(error.get("source", ""))
                    condition = str(redact(error.get("condition", "")))
                    expected_errors.add((code, condition, evidence))
                    if (evidence and evidence not in section) or (condition and condition not in section):
                        missing_error_evidence.append(f"{entry_id}:{evidence}:{condition}")
            documented_errors = {
                (match.group("code"), match.group("condition").strip(), match.group("source"))
                for match in re.finditer(
                    r"^- `(?P<code>[^`]+)`：(?P<condition>.*?)（证据：`(?P<source>[^`]+)`）$",
                    section,
                    re.MULTILINE,
                )
            }
            stale_error_evidence.extend(
                f"{entry_id}:{code}:{source}:{condition}"
                for code, condition, source in sorted(documented_errors - expected_errors)
            )
    return {
        "markdown_document_count": len(text_by_module),
        "markdown_entry_count": len(entries) - len(missing_entries),
        "markdown_missing_documents": sorted(dict.fromkeys(missing_documents)),
        "markdown_missing_entries": sorted(dict.fromkeys(missing_entries)),
        "markdown_stale_entries": sorted(dict.fromkeys(stale_entries)),
        "markdown_missing_error_codes": sorted(dict.fromkeys(missing_error_codes)),
        "markdown_missing_error_evidence": sorted(dict.fromkeys(missing_error_evidence)),
        "markdown_stale_error_evidence": sorted(dict.fromkeys(stale_error_evidence)),
        "markdown_diagram_mismatches": sorted(dict.fromkeys(diagram_mismatches)),
        "markdown_version_mismatches": sorted(dict.fromkeys(version_mismatches)),
    }


def coverage(
    scan: ScanResult,
    index: dict[str, Any],
    docs_root: Path | None = None,
    module_filter: str | None = None,
) -> dict[str, Any]:
    code_entries = [entry for entry in scan.entries if not module_filter or entry.module == module_filter]
    index_entries = [
        item for item in index.get("entries", [])
        if isinstance(item, dict) and (not module_filter or item.get("module") == module_filter)
    ]
    code_ids = {entry.entry_id for entry in code_entries}
    documented_ids = {str(item.get("id")) for item in index_entries}
    code_errors = {
        (entry.entry_id, error.code, str(redact(error.condition)), f"{error.file}:{error.line}")
        for entry in code_entries
        for error in entry.errors
        if error.code != "代码中未确认"
    }
    documented_errors = {
        (str(item.get("id")), str(error.get("code")), str(error.get("condition")), str(error.get("source")))
        for item in index_entries
        for error in item.get("errors", [])
        if isinstance(error, dict) and str(error.get("code")) != "代码中未确认"
    }
    code_codes = {item[1] for item in code_errors}
    documented_codes = {item[1] for item in documented_errors}
    missing_error_evidence = sorted(":".join(item) for item in code_errors - documented_errors)
    stale_error_evidence = sorted(":".join(item) for item in documented_errors - code_errors)
    result = {
        "code_entry_count": len(code_ids),
        "documented_entry_count": len(documented_ids),
        "missing_entries": sorted(code_ids - documented_ids),
        "stale_entries": sorted(documented_ids - code_ids),
        "code_error_code_count": len(code_codes),
        "documented_error_code_count": len(documented_codes),
        "missing_error_codes": sorted(code_codes - documented_codes),
        "stale_error_codes": sorted(documented_codes - code_codes),
        "missing_error_evidence": missing_error_evidence,
        "stale_error_evidence": stale_error_evidence,
        "unique_module_count": len({entry.module for entry in code_entries}),
    }
    result.update(_markdown_coverage(scan, index, docs_root, module_filter))
    return result


def write_artifacts(
    scan: ScanResult,
    docs_root: Path,
    *,
    comparison: str,
    old_commit: str | None,
    changed: dict[str, Any],
    module_filter: str | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    docs_root.mkdir(parents=True, exist_ok=True)
    previous = _read_index(docs_root)
    modules = sorted({entry.module for entry in scan.entries})
    files = _module_files(docs_root, modules, previous)
    for module, filename in files.items():
        if module_filter and module != module_filter:
            continue
        entries = [entry for entry in scan.entries if entry.module == module]
        path = docs_root / filename
        if comparison == "version_only" and path.is_file():
            _update_version_only(path, scan.git)
        else:
            path.write_text(redact(render_module(module, entries, scan.git, scan, comparison=comparison)), encoding="utf-8")
    stale_files = {str(item.get("file")) for item in previous.get("modules", []) if isinstance(item, dict)} - set(files.values())
    for filename in (stale_files if not module_filter else ()):
        path = docs_root / filename
        if path.is_file():
            path.unlink()
    index = build_index(scan, docs_root, files, comparison=comparison, old_commit=old_commit)
    _require_contract(BUSINESS_FLOW_INDEX_SCHEMA, index, "business-flow index")
    index_path = write_json(docs_root / "business-flow-index.json", index)
    reported_entries = [entry for entry in scan.entries if not module_filter or entry.module == module_filter]
    reported_modules = sorted({entry.module for entry in reported_entries})
    reported_ids = {entry.entry_id for entry in reported_entries}
    report = {
        "schema_version": 2,
        "source_fingerprint": scan.source_fingerprint,
        "effective_git": index["effective_git"],
        "project": str(scan.root),
        "scope": module_filter,
        "languages": scan.languages,
        "frameworks": scan.frameworks,
        "module_count": len(reported_modules),
        "document_count": len(reported_modules),
        "url_entry_count": sum(entry.kind in {"url", "webhook", "websocket", "sse"} for entry in reported_entries),
        "scheduled_task_count": sum(entry.kind == "scheduled" for entry in reported_entries),
        "message_consumer_count": sum(entry.kind == "message" for entry in reported_entries),
        "other_entry_count": sum(entry.kind not in {"url", "webhook", "websocket", "sse", "scheduled", "message"} for entry in reported_entries),
        "active_error_code_count": len({code for entry in reported_entries for code in entry.error_codes() if code != "代码中未确认"}),
        "entry_count": len(reported_entries),
        "added_entries": [value for value in changed.get("added_entries", []) if value in reported_ids],
        "updated_entries": [value for value in changed.get("updated_entries", []) if value in reported_ids],
        "deleted_entries": changed.get("deleted_entries", []) if not module_filter else [],
        "version_only_documents": [value for value in changed.get("version_only_documents", []) if value in reported_modules],
        "business_changed_documents": [value for value in changed.get("business_changed_documents", modules) if value in reported_modules],
        "comparison": changed.get("comparison", comparison),
        "comparison_error": changed.get("comparison_error"),
        "exclusions": scan.exclusions,
        "unresolved": scan.unresolved,
        "coverage": coverage(scan, index, docs_root, module_filter),
        "index_path": str(index_path),
    }
    _require_contract(BUSINESS_FLOW_REPORT_SCHEMA, report, "business-flow report")
    report_path = write_json(docs_root / "business-flow-report.json", report)
    return index_path, report_path, report
