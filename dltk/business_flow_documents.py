"""Render business-flow Markdown, indexes, and coverage reports."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .artifacts import write_json
from .redaction import redact
from .schema import (
    BUSINESS_FLOW_COMPARISON_SCHEMA,
    BUSINESS_FLOW_DEPENDENCY_GRAPH_SCHEMA,
    BUSINESS_FLOW_DISCOVERY_SCHEMA,
    BUSINESS_FLOW_EVIDENCE_CACHE_SCHEMA,
    BUSINESS_FLOW_INDEX_SCHEMA,
    BUSINESS_FLOW_MIGRATIONS_SCHEMA,
    BUSINESS_FLOW_MODULE_MAP_SCHEMA,
    BUSINESS_FLOW_OWNERSHIP_SCHEMA,
    BUSINESS_FLOW_REPORT_SCHEMA,
    BUSINESS_FLOW_SCHEMA_VERSION,
    validate_schema,
)
from .business_flow_models import BehaviorEvidence, EntryPoint, EntryReview, ErrorEvidence, FlowStep, GitInfo, ScanResult


PLACEHOLDER_RE = re.compile(
    r"(?:代码中未确认|处理链中的业务步骤|更具体的业务目的|错误后果按可传播错误记录|"
    r"执行[^；。]*未确认|结果代码中未确认)",
)


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


def _error_dict(error: ErrorEvidence) -> dict[str, Any]:
    return {
        "code": error.code,
        "condition": str(redact(error.condition)),
        "source": f"{error.file}:{error.line}",
        "capture_boundary": error.capture_boundary,
        "propagation": error.propagation,
        "consequence": error.consequence,
        "phase": error.phase,
        "recovery": error.recovery,
    }


def _error_from_dict(value: object, fallback: str) -> ErrorEvidence:
    item = value if isinstance(value, dict) else {}
    file, separator, number = str(item.get("source", fallback)).rpartition(":")
    line = int(number) if separator and number.isdigit() else int(fallback.rpartition(":")[-1] or 1)
    if not file:
        file = fallback.rpartition(":")[0]
    return ErrorEvidence(
        str(item.get("code", "代码中未确认")),
        str(item.get("condition", "代码中未确认")),
        file,
        line,
        str(item.get("capture_boundary", "代码中未确认")),
        str(item.get("propagation", "代码中未确认")),
        str(item.get("consequence", "代码中未确认")),
        str(item.get("phase", "sync")),
        str(item.get("recovery", "代码中未确认")),
    )


def _cache_entry(entry: EntryPoint) -> dict[str, Any]:
    """Serialize all parsed evidence needed to resume without rescanning."""
    return {
        "id": entry.entry_id,
        "type": entry.kind,
        "identifier": entry.identifier,
        "handler": entry.handler,
        "source": f"{entry.file}:{entry.line}",
        "module": entry.module,
        "module_rationale": entry.module_rationale,
        "caller": str(redact(entry.caller)),
        "input_summary": str(redact(entry.input_summary)),
        "functions": list(entry.functions),
        "errors": [_error_dict(error) for error in entry.errors],
        "behaviors": [
            {
                "kind": behavior.kind,
                "statement": str(redact(behavior.statement)),
                "source": f"{behavior.file}:{behavior.line}",
            }
            for behavior in entry.behaviors
        ],
        "has_loop": entry.has_loop,
        "has_external_call": entry.has_external_call,
        "has_persistence": entry.has_persistence,
        "has_async": entry.has_async,
        "binding_confirmed": entry.binding_confirmed,
        "handler_confirmed": entry.handler_confirmed,
    }


def _cache_payload(scan: ScanResult) -> dict[str, Any]:
    return {
        "schema_version": int(BUSINESS_FLOW_SCHEMA_VERSION),
        "source_fingerprint": scan.source_fingerprint,
        "root": str(scan.root),
        "git": {
            "branch": scan.git.branch,
            "head": scan.git.head,
            "target": scan.git.target,
            "dirty": scan.git.dirty,
            "includes_uncommitted": scan.git.includes_uncommitted,
            "comparison": scan.git.comparison,
        },
        "languages": list(scan.languages),
        "frameworks": list(scan.frameworks),
        "files": list(scan.files),
        "source_lines": dict(scan.source_lines),
        "unresolved": list(scan.unresolved),
        "exclusions": list(scan.exclusions),
        "candidate_entry_count": scan.candidate_entry_count,
        "confirmed_binding_count": scan.confirmed_binding_count,
        "confirmed_handler_count": scan.confirmed_handler_count,
        "entries": {entry.entry_id: _cache_entry(entry) for entry in scan.entries},
    }


def _review_placeholders(review: EntryReview) -> list[str]:
    fields = {
        "trigger": review.trigger,
        "purpose": review.purpose,
        "input": review.input,
        "outcome": review.outcome,
        "failure": review.failure,
    }
    problems = [name for name, value in fields.items() if not str(value).strip() or PLACEHOLDER_RE.search(str(value))]
    if not review.steps:
        problems.append("steps")
    if any(not step.text.strip() or PLACEHOLDER_RE.search(step.text) for step in review.steps):
        problems.append("step_text")
    if not any(step.kind == "action" for step in review.steps):
        problems.append("action_step")
    return problems


def _review(entry: EntryPoint) -> EntryReview:
    if entry.review is not None:
        return entry.review
    steps: list[FlowStep] = []
    explicit_loops = 0
    for behavior in entry.behaviors:
        if behavior.kind == "循环":
            explicit_loops += 1
            steps.append(FlowStep(
                "loop",
                f"按源码循环条件逐项处理：{behavior.statement}",
                f"{behavior.file}:{behavior.line}",
            ))
            continue
        participant = "当前系统"
        if behavior.kind == "持久化":
            participant = "数据库"
        elif behavior.kind == "外部调用":
            participant = "可见外部接口"
        elif behavior.kind in {"消息", "异步"}:
            participant = "消息/异步系统"
        source = f"{behavior.file}:{behavior.line}"
        if behavior.kind == "校验" and re.search(r"\bif\b|\bunless\b|\bwhen\b", behavior.statement, re.I):
            steps.append(FlowStep("alt", f"满足前置条件：{behavior.statement}", source, "当前系统"))
            steps.append(FlowStep("action", _business_step(behavior), source, participant))
            steps.append(FlowStep("end", "结束前置条件分支", source, "当前系统"))
        else:
            steps.append(FlowStep("action", _business_step(behavior), source, participant))
    if not steps:
        steps.append(FlowStep("action", "处理链中的业务步骤代码中未确认。", f"{entry.file}:{entry.line}"))
    if explicit_loops:
        end_source = (
            f"{entry.file}:{max((behavior.line for behavior in entry.behaviors), default=entry.line)}"
        )
        steps.extend(
            FlowStep("end", "结束源码循环", end_source)
            for _ in range(explicit_loops)
        )
    elif entry.has_loop:
        loop_source = f"{entry.file}:{min((behavior.line for behavior in entry.behaviors), default=entry.line)}"
        end_source = f"{entry.file}:{max((behavior.line for behavior in entry.behaviors), default=entry.line)}"
        steps = [FlowStep("loop", "源码中的逐项循环处理", loop_source), *steps, FlowStep("end", "结束循环", end_source)]
    return EntryReview(
        review_id=entry.entry_id,
        trigger=entry.caller,
        purpose=f"处理 {entry.identifier} 对应的业务动作；更具体的业务目的代码中未确认。",
        input=entry.input_summary,
        outcome="结果代码中未确认；受理成功不等同远端或后台完成。",
        failure="错误后果按可传播错误记录；未被源码确认的捕获、回滚、重试和补偿不作推断。",
        steps=steps,
        status="draft",
    )


def _business_step(behavior: BehaviorEvidence) -> str:
    statements = {
        "校验": "校验输入或前置条件，未通过时停止后续动作（具体错误结果见异常表）。",
        "循环": "按源码循环条件逐项处理；单项失败是否继续以异常捕获证据为准。",
        "分支": "进入源码明确的条件分支。",
        "事务": "在源码标记的事务边界内执行后续本地动作。",
        "锁与幂等": "执行源码可确认的并发或幂等控制。",
        "持久化": "写入或读取本地持久化对象；具体对象和提交点以证据为准。",
        "外部调用": "调用源码可见的外部接口；远端内部实现不在本地证据范围内。",
        "消息": "发送或发布消息，后续消费结果不等同于本入口同步完成。",
        "异步": "提交后台或异步处理；受理成功不等同于后台终态成功。",
        "缓存或文件": "读写缓存或文件资源。",
        "状态变化": "改变业务状态，前后状态值以源码证据为准。",
        "结果": "形成源码中的返回或产出结果。",
    }
    return statements.get(behavior.kind, f"执行 {behavior.kind} 处理；具体规则代码中未确认。")


def _non_business_candidate(entry: EntryPoint) -> bool:
    return bool(
        re.search(
            r"(?:^|[/ ])(?:health|actuator|metrics|static|swagger|openapi)(?:[/ ]|$)",
            entry.identifier,
            re.IGNORECASE,
        )
    )


def _source_line(value: str) -> int:
    number = value.rsplit(":", 1)[-1]
    return int(number) if number.isdigit() else 0


def _append_error(lines: list[str], error: ErrorEvidence) -> None:
    condition = str(redact(error.condition)).replace("\n", " ")[:100]
    consequence = str(redact(error.consequence)).replace("\n", " ")[:100]
    lines.append(f"alt {error.code}：{condition}")
    lines.append(f"System-->>Caller: {error.phase}错误；{consequence}")
    lines.append("end")


def _diagram(entry: EntryPoint) -> str:
    review = _review(entry)
    lines = [
        "```mermaid",
        "sequenceDiagram",
        "autonumber",
        "participant Caller as 调用方",
        "participant System as 当前系统",
    ]
    participants = {step.participant for step in review.steps if step.participant not in {"当前系统", "调用方"}}
    aliases = {"数据库": "DB", "缓存/文件": "Storage", "消息/异步系统": "Middleware", "可见外部接口": "External"}
    for participant in sorted(participants):
        alias = aliases.get(participant, "P" + str(len(aliases) + 1))
        aliases.setdefault(participant, alias)
        lines.append(f"participant {alias} as {participant}")
    lines.append(f"Caller->>System: {entry.identifier}")
    lines.append(f"System->>System: 进入 {entry.handler}")
    errors = sorted(entry.errors, key=lambda item: item.line)
    emitted_errors: set[int] = set()
    for step in review.steps:
        step_line = _source_line(step.source)
        for index, error in enumerate(errors):
            if index not in emitted_errors and error.line <= step_line:
                _append_error(lines, error)
                emitted_errors.add(index)
        text = str(redact(step.text)).replace("\n", " ")[:120]
        participant = aliases.get(step.participant, "System")
        if step.kind == "alt":
            lines.append(f"alt {text}")
        elif step.kind == "else":
            lines.append(f"else {text}")
        elif step.kind == "opt":
            lines.append(f"opt {text}")
        elif step.kind == "loop":
            lines.append(f"loop {text}")
        elif step.kind == "end":
            lines.append("end")
        else:
            lines.append(f"System->>{participant}: {text}")
    for index, error in enumerate(errors):
        if index not in emitted_errors:
            _append_error(lines, error)
    lines.append(f"Note over Caller,System: {str(redact(review.outcome))[:140]}")
    lines.append("```")
    return "\n".join(lines)


def _validate_mermaid(diagram: str) -> list[str]:
    """Validate the sequence subset emitted by this skill before claiming it renders."""
    lines = [line.strip() for line in diagram.splitlines() if line.strip()]
    if not lines or lines[0] != "sequenceDiagram":
        return ["missing sequenceDiagram header"]
    aliases = {match.group(1) for line in lines if (match := re.match(r"participant\s+(\w+)\s+as\s+", line))}
    blocks: list[str] = []
    errors: list[str] = []
    for line in lines[1:]:
        if line.startswith(("alt ", "opt ", "loop ")):
            blocks.append(line.split(" ", 1)[0])
        elif line.startswith("else "):
            if not blocks or blocks[-1] != "alt":
                errors.append("else outside alt")
        elif line == "end":
            if not blocks:
                errors.append("unmatched end")
            else:
                blocks.pop()
        elif match := re.match(r"(\w+)(?:-->>|->>)(\w+):\s*", line):
            if match.group(1) not in aliases or match.group(2) not in aliases:
                errors.append(f"unknown participant in message: {line}")
        elif line.startswith("Note over "):
            note_match = re.match(r"Note over\s+(\w+)(?:,(\w+))?:", line)
            if not note_match:
                errors.append(f"invalid note statement: {line}")
            else:
                for participant in note_match.groups():
                    if participant and participant not in aliases:
                        errors.append(f"unknown participant in note: {line}")
        elif line == "autonumber" or line.startswith("participant "):
            continue
        else:
            errors.append(f"unsupported sequence statement: {line}")
    if blocks:
        errors.append("unclosed " + ", ".join(blocks))
    if errors or not shutil.which("mmdc"):
        return errors
    # Use Mermaid's own parser when the optional CLI is installed. The custom
    # validator above remains the deterministic fallback for normal installs.
    try:
        with tempfile.TemporaryDirectory(prefix="business-flow-mermaid-") as temporary:
            source = Path(temporary) / "diagram.mmd"
            output = Path(temporary) / "diagram.svg"
            source.write_text(diagram, encoding="utf-8")
            completed = subprocess.run(
                ["mmdc", "-i", str(source), "-o", str(output), "-q"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if completed.returncode:
                detail = (completed.stderr or completed.stdout or "Mermaid renderer failed").strip()
                return [f"Mermaid renderer rejected diagram: {detail[:240]}"]
    except (OSError, subprocess.SubprocessError) as exc:
        return [f"Mermaid renderer unavailable: {exc}"]
    return errors


def _entry_text(entry: EntryPoint) -> str:
    review = _review(entry)
    error_lines = [
        f"- `{error.code}`：{redact(error.condition)} （阶段：{error.phase}；捕获：{error.capture_boundary}；传播：{error.propagation}；后果：{error.consequence}；恢复：{error.recovery}；证据：`{error.file}:{error.line}`）"
        for error in entry.errors
    ] or ["- 无"]
    step_lines = [
        f"{index}. {step.text}（参与方：{step.participant}；证据：`{step.source}`）"
        for index, step in enumerate(review.steps, 1)
    ]
    evidence_lines = [
        f"- 行为 **{behavior.kind}**：`{redact(behavior.statement)}`（证据：`{behavior.file}:{behavior.line}`）"
        for behavior in entry.behaviors
    ] or ["- 代码中未确认可展开的行为证据。"]
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
        f"- 业务目的：{review.purpose}",
        f"- 影响行为的输入：{review.input}",
        f"- 结果含义：{review.outcome}",
        f"- 失败、部分成功和结果未知：{review.failure}",
        f"- 事实评审：`{review.status}`；确认人：{review.confirmed_by or '未确认'}。",
        "",
        "### 业务步骤",
        "",
        *step_lines,
        "",
        "### 业务流程",
        "",
        f"主动抛出的带错误码业务异常：`{_codes(entry)}`。",
        "",
        *error_lines,
        "",
        _diagram(entry),
        "",
        "### 源码证据与边界",
        "",
        f"- 调用链中可静态确认的函数：{', '.join(f'`{name}`' for name in entry.functions) or '代码中未确认'}。",
        "- 行为证据：",
        *evidence_lines,
        "- 未确认项：未提供远端实现或无法静态关联的调用，不在本地文档中推断其内部顺序、事务或终态。",
        "",
    ])


def _module_files(
    docs_root: Path,
    modules: list[str],
    previous: dict[str, Any],
    preferred: dict[str, str] | None = None,
) -> dict[str, str]:
    def safe_filename(value: object) -> bool:
        raw = str(value or "").replace("\\", "/")
        path = Path(raw)
        return bool(raw) and path.suffix.lower() == ".md" and not path.is_absolute() and ".." not in path.parts

    old = {
        str(item.get("name")): str(item.get("file"))
        for item in previous.get("modules", [])
        if isinstance(item, dict) and safe_filename(item.get("file"))
    }
    used = set(old.values())
    result: dict[str, str] = {}
    for index, module in enumerate(sorted(modules)):
        if preferred and safe_filename(preferred.get(module)):
            result[module] = preferred[module]
            used.add(preferred[module])
            continue
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


def _markdown_entry_sections(docs_root: Path, index: dict[str, Any]) -> dict[str, str]:
    """Read prior generated entry sections without treating prose as code facts."""
    sections: dict[str, str] = {}
    for module in index.get("modules", []):
        if not isinstance(module, dict):
            continue
        filename = str(module.get("file", ""))
        if not filename or Path(filename).is_absolute() or ".." in Path(filename).parts:
            continue
        path = docs_root / filename
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        matches = list(re.finditer(r"<!-- business-flow-entry:\s*([^>]+?)\s*-->", text))
        for index, match in enumerate(matches):
            entry_id = match.group(1).strip()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            sections[entry_id] = text[match.start():end]
    return sections


def _fact_tokens(section: str) -> dict[str, set[str]]:
    """Extract comparable facts from a generated entry section.

    This intentionally reports category-level differences, not a fabricated
    semantic score.  A human can follow each category back to the old/new
    entry section and its source evidence.
    """
    diagram = "\n".join(
        block for block in re.findall(r"```mermaid\s*(.*?)```", section, flags=re.S)
    )
    participants = set(re.findall(r"^participant\s+\w+\s+as\s+(.+)$", diagram, flags=re.M))
    controls = set(re.findall(r"^(alt|opt|loop)\s+(.+)$", diagram, flags=re.M))
    errors = set(re.findall(r"^[-*]\s+`([^`]+)`：", section, flags=re.M))
    evidence = set(re.findall(r"`([^`\n]+:\d+)`", section))
    async_terms = {
        token for token in ("异步", "后台", "消息", "受理", "远端完成", "结果未知")
        if token in section
    }
    # Keep the business prose separate from source/version metadata.  These
    # lines are useful for detecting a changed rule without comparing line
    # numbers that naturally move between snapshots.
    prose = {
        line.strip()
        for line in section.splitlines()
        if line.strip().startswith("- ")
        and not any(marker in line for marker in ("证据：`", "源码", "入口类型", "主归属业务模块"))
    }
    return {
        "prose": prose,
        "controls": {f"{kind}:{text}" for kind, text in controls},
        "errors": errors,
        "participants": participants,
        "async": async_terms,
        "evidence": evidence,
    }


def compare_markdown_facts(
    docs_root: Path,
    previous_index: dict[str, Any],
    entries: list[EntryPoint],
) -> list[dict[str, Any]]:
    """Align prior Markdown by stable entry marker and report factual drift."""
    if not previous_index:
        return []
    old_sections = _markdown_entry_sections(docs_root, previous_index)
    diffs: list[dict[str, Any]] = []
    categories = ("prose", "controls", "errors", "participants", "async", "evidence")
    for entry in entries:
        old_section = old_sections.get(entry.entry_id)
        if old_section is None:
            diffs.append({
                "id": entry.entry_id, "category": "entry", "status": "added",
                "old": [], "new": [entry.identifier],
                "reason": "旧 Markdown 没有同一稳定入口标记，无法把旧正文对齐到当前入口。",
            })
            continue
        if "### 入口说明" not in old_section or "### 业务流程" not in old_section:
            diffs.append({
                "id": entry.entry_id,
                "category": "entry",
                "status": "unknown",
                "old": [],
                "new": [entry.identifier],
                "reason": "旧正文有入口标记但缺少标准章节，无法可靠比较业务事实。",
            })
        old_facts = _fact_tokens(old_section)
        new_facts = _fact_tokens(_entry_text(entry))
        for category in categories:
            old_values = old_facts[category]
            new_values = new_facts[category]
            if old_values == new_values:
                continue
            if not old_values:
                status = "added"
                reason = "当前入口新增该类可观察事实。"
            elif not new_values:
                status = "missing"
                reason = "旧正文存在该类事实，但当前正文没有对应表达；需核对是否删除或漏写。"
            elif old_values & new_values:
                status = "contradictory"
                reason = "旧新正文仅部分重合，存在规则、分支、参与方、错误、异步或证据差异。"
            else:
                status = "contradictory"
                reason = "旧新正文在该事实类别完全不一致，不能用版本号变化解释。"
            diffs.append({
                "id": entry.entry_id,
                "category": category,
                "status": status,
                "old": sorted(old_values),
                "new": sorted(new_values),
                "reason": reason,
            })
    current_ids = {entry.entry_id for entry in entries}
    for entry_id in sorted(set(old_sections) - current_ids):
        diffs.append({
            "id": entry_id, "category": "entry", "status": "missing", "old": [entry_id], "new": [],
            "reason": "旧 Markdown 中存在该入口标记，但当前源码入口集合没有对应入口。",
        })
    return diffs


def _require_contract(schema: dict[str, Any], value: dict[str, Any], name: str) -> None:
    errors = validate_schema(schema, value)
    if errors:
        raise ValueError(f"invalid generated {name}: {'; '.join(errors)}")


def _review_dict(review: EntryReview) -> dict[str, Any]:
    return {
        "id": review.review_id,
        "trigger": review.trigger,
        "purpose": review.purpose,
        "input": review.input,
        "outcome": review.outcome,
        "failure": review.failure,
        "status": review.status,
        "confirmed_by": review.confirmed_by,
        "steps": [
            {"kind": step.kind, "text": step.text, "source": step.source, "participant": step.participant}
            for step in review.steps
        ],
    }


def _review_from_dict(value: object, fallback: EntryPoint) -> EntryReview | None:
    if not isinstance(value, dict):
        return None
    steps = [
        FlowStep(
            str(step.get("kind", "action")), str(step.get("text", "代码中未确认")),
            str(step.get("source", f"{fallback.file}:{fallback.line}")), str(step.get("participant", "当前系统")),
        )
        for step in value.get("steps", []) if isinstance(step, dict)
    ]
    return EntryReview(
        str(value.get("id", fallback.entry_id)), str(value.get("trigger", fallback.caller)),
        str(value.get("purpose", "代码中未确认")), str(value.get("input", fallback.input_summary)),
        str(value.get("outcome", "代码中未确认")), str(value.get("failure", "代码中未确认")), steps,
        str(value.get("status", "draft")), str(value.get("confirmed_by", "")),
    )


def write_discovery(scan: ScanResult, docs_root: Path) -> tuple[Path, Path]:
    docs_root.mkdir(parents=True, exist_ok=True)
    discovery = {
        "schema_version": int(BUSINESS_FLOW_SCHEMA_VERSION),
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
                "errors": [_error_dict(error) for error in entry.errors],
            }
            for entry in scan.entries
        ],
        "candidate_entry_count": scan.candidate_entry_count,
        "confirmed_binding_count": scan.confirmed_binding_count,
        "confirmed_handler_count": scan.confirmed_handler_count,
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
    previous_modules = {
        str(module.get("name")): module
        for module in previous.get("modules", [])
        if isinstance(module, dict) and module.get("name")
    }
    entry_lookup = {entry.entry_id: entry for entry in scan.entries}

    def draft_module_facts(name: str, entry_ids: list[str]) -> tuple[str, str, list[str], list[str]]:
        group = [entry_lookup[item] for item in entry_ids if item in entry_lookup]
        objects = sorted({
            token
            for entry in group
            for behavior in entry.behaviors
            if behavior.kind in {"持久化", "状态变化"}
            for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", behavior.statement)
            if token.lower() not in {"repository", "service", "status", "state"}
        })[:12]
        partners = sorted({
            "可见外部接口" if entry.has_external_call else ""
            for entry in group
        } | ({"消息/异步系统"} if any(entry.has_async for entry in group) else set()))
        partners = [item for item in partners if item]
        responsibility = f"围绕 {name} 模块入口处理已确认的业务动作"
        rationale = f"按入口业务目标、处理对象和状态/副作用边界归组；当前入口：{', '.join(entry_ids)}"
        return rationale, responsibility, objects, partners
    previous_reviews = {
        str(review.get("id")): review
        for review in previous.get("entry_reviews", [])
        if isinstance(review, dict) and review.get("id")
    }

    def review_for(entry: EntryPoint) -> dict[str, Any]:
        previous_review = previous_reviews.get(entry.entry_id)
        if previous_review is None:
            return _review_dict(_review(entry))
        if previous.get("source_fingerprint") == scan.source_fingerprint:
            return previous_review
        refreshed = dict(previous_review)
        refreshed["status"] = "draft"
        refreshed["confirmed_by"] = ""
        return refreshed

    module_map = {
        "schema_version": int(BUSINESS_FLOW_SCHEMA_VERSION),
        "source_fingerprint": scan.source_fingerprint,
        "effective_git": scan.git.target,
        "confirmed": bool(previous.get("confirmed")) and unchanged,
        "entry_reviews": [
            review_for(entry)
            for entry in scan.entries
            if entry.entry_id not in excluded_ids
        ],
        "migrations": previous.get("migrations", []),
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
                ), draft_module_facts(name, entry_ids)[0]),
                "file": str(previous_modules.get(name, {}).get("file") or f"{index:02d}-{_slug(name)}.md"),
                "responsibility": str(previous_modules.get(name, {}).get("responsibility") or draft_module_facts(name, entry_ids)[1]),
                "objects": [str(value) for value in previous_modules.get(name, {}).get("objects", [])] or draft_module_facts(name, entry_ids)[2],
                "partners": [str(value) for value in previous_modules.get(name, {}).get("partners", [])] or draft_module_facts(name, entry_ids)[3],
                "questions": [str(value) for value in previous_modules.get(name, {}).get("questions", ["确认模块边界及跨模块入口"])],
                "entry_ids": sorted(entry_ids),
            }
            for index, (name, entry_ids) in enumerate(sorted(groups.items()))
        ],
    }
    _require_contract(BUSINESS_FLOW_MODULE_MAP_SCHEMA, module_map, "business-flow module map")
    draft_path = docs_root / "business-flow-modules-draft.json"
    draft_stale = True
    if draft_path.is_file():
        try:
            draft_stale = json.loads(draft_path.read_text(encoding="utf-8")).get("source_fingerprint") != scan.source_fingerprint
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
            draft_stale = True
    if draft_stale:
        write_json(draft_path, module_map)
    write_json(module_map_path, module_map)
    # Keep a complete parsed-evidence snapshot so a same-fingerprint resume can
    # rebuild the scan result without parsing the source tree again.
    write_json(docs_root / "business-flow-evidence-cache.json", _cache_payload(scan))
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
    critical_resolution_errors: list[str] = []
    critical_markers = (
        "unknown receiver type",
        "handler for ",
        "multiple possible definitions",
        "registered business entry could not be mapped",
        "business entry identifier is not statically resolvable",
    )
    for finding, item in resolutions.items():
        if not any(marker in finding for marker in critical_markers):
            continue
        missing = [field for field in ("path", "controls", "unknowns") if field not in item]
        if missing:
            critical_resolution_errors.append(
                f"resolution for critical finding requires structured fields: {finding} ({', '.join(missing)})"
            )
            continue
        path_values = item.get("path", [])
        invalid_path = [str(value) for value in path_values if not valid_source(value)]
        if not path_values or invalid_path:
            critical_resolution_errors.append(
                f"resolution for critical finding has invalid path evidence: {finding}"
            )
    if critical_resolution_errors:
        return critical_resolution_errors
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
            entry.errors = [_error_from_dict(error, f"{entry.file}:{entry.line}") for error in override.get("errors", [])]
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
        entry_errors = [_error_from_dict(error, source) for error in item.get("errors", [])]
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
    entry_by_id = {entry.entry_id: entry for entry in scan.entries}
    review_ids = [str(item.get("id")) for item in document.get("entry_reviews", []) if isinstance(item, dict)]
    if len(review_ids) != len(set(review_ids)):
        return ["module map contains duplicate entry reviews"]
    unknown_reviews = set(review_ids) - set(entry_by_id)
    if unknown_reviews:
        return ["module map reviews unknown entries: " + ", ".join(sorted(unknown_reviews))]
    excluded_review_ids = {
        str(item.get("candidate"))
        for item in document.get("exclusions", [])
        if isinstance(item, dict) and item.get("candidate")
    }
    expected_review_ids = set(entry_by_id) - excluded_review_ids
    missing_reviews = expected_review_ids - set(review_ids)
    stale_reviews = set(review_ids) - expected_review_ids
    if missing_reviews:
        return [
            "module map must contain exactly one confirmed entry review for every non-excluded entry; missing: "
            + ", ".join(sorted(missing_reviews))
        ]
    if stale_reviews:
        return ["module map contains reviews for excluded entries: " + ", ".join(sorted(stale_reviews))]
    for item in document.get("entry_reviews", []):
        if not isinstance(item, dict):
            continue
        entry = entry_by_id[str(item["id"])]
        review = _review_from_dict(item, entry)
        if review is None or not review.steps:
            return [f"entry review {entry.entry_id} must contain at least one step"]
        if review.status not in {"draft", "confirmed"}:
            return [f"entry review {entry.entry_id} has invalid status: {review.status}"]
        if review.status != "confirmed" or not review.confirmed_by.strip():
            return [f"entry review {entry.entry_id} is not explicitly human-confirmed"]
        placeholder_fields = _review_placeholders(review)
        if placeholder_fields:
            return [
                f"entry review {entry.entry_id} still contains generated placeholder facts: "
                + ", ".join(placeholder_fields)
            ]
        invalid_steps = [step.source for step in review.steps if not valid_source(step.source)]
        if invalid_steps:
            return [f"entry review {entry.entry_id} has evidence outside scanned source: {', '.join(invalid_steps)}"]
        controls: list[str] = []
        for step in review.steps:
            if step.kind in {"alt", "opt", "loop"}:
                controls.append(step.kind)
            elif step.kind == "else" and (not controls or controls[-1] != "alt"):
                return [f"entry review {entry.entry_id} has an else step outside alt"]
            elif step.kind == "end":
                if not controls:
                    return [f"entry review {entry.entry_id} has an unmatched end step"]
                controls.pop()
        if controls:
            return [f"entry review {entry.entry_id} has unclosed control blocks: {', '.join(controls)}"]
        last_line_by_file: dict[str, int] = {}
        out_of_order = False
        for step in review.steps:
            source_file = str(step.source).rsplit(":", 1)[0]
            source_line = _source_line(step.source)
            if source_line < last_line_by_file.get(source_file, 0):
                out_of_order = True
                break
            last_line_by_file[source_file] = source_line
        if out_of_order:
            return [f"entry review {entry.entry_id} steps are not ordered by source location"]
        if review.review_id != entry.entry_id:
            return [f"entry review id does not match entry id: {review.review_id} != {entry.entry_id}"]
        entry.review = review
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
    module_files: dict[str, str] = {}
    for module in document.get("modules", []):
        if not isinstance(module, dict) or not str(module.get("name", "")).strip():
            errors.append("module map contains a module without a name")
            continue
        name = str(module["name"])
        if not module.get("entry_ids"):
            errors.append(f"module {name} has no entries")
        rationales[name] = str(module.get("rationale", ""))
        filename = str(module.get("file", "")).strip()
        if not filename:
            errors.append(f"module {name} has no stable file name")
        elif not filename.lower().endswith(".md"):
            errors.append(f"module {name} file must be Markdown: {filename}")
        elif (path.parent / filename).resolve().parent != path.parent.resolve():
            errors.append(f"module {name} file escapes docs root: {filename}")
        elif filename in module_files.values():
            errors.append(f"multiple modules use the same document file: {filename}")
        else:
            module_files[name] = filename
        rationale = str(module.get("rationale", "")).strip()
        if not rationale or rationale in {"代码中未确认", "待依据业务职责、对象、数据归属和调用关系确认"}:
            errors.append(f"module {name} has no boundary rationale")
        responsibility = str(module.get("responsibility", "")).strip()
        if not responsibility or responsibility in {"代码中未确认", "待人工确认"}:
            errors.append(f"module {name} has no confirmed responsibility")
        elif re.fullmatch(r"围绕 .+ 模块入口处理已确认的业务动作", responsibility):
            errors.append(f"module {name} still uses generated responsibility placeholder")
        for entry_id in module.get("entry_ids", []):
            entry_id = str(entry_id)
            if entry_id in excluded_ids:
                continue
            if entry_id in owners:
                errors.append(f"entry {entry_id} belongs to multiple modules")
            owners[entry_id] = name
    active_entry_ids = {entry.entry_id for entry in scan.entries}
    for module in document.get("modules", []):
        if not isinstance(module, dict):
            continue
        name = str(module.get("name", ""))
        active_ids = set(str(value) for value in module.get("entry_ids", [])) & active_entry_ids
        if not active_ids:
            errors.append(f"module {name} has no active entries after exclusions")
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


def build_index(
    scan: ScanResult,
    docs_root: Path,
    files: dict[str, str],
    *,
    comparison: str,
    old_commit: str | None = None,
    migrations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
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
        "schema_version": int(BUSINESS_FLOW_SCHEMA_VERSION),
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
        "migrations": migrations or [],
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
                "errors": [_error_dict(error) for error in entry.errors],
                "behaviors": [
                    {"kind": behavior.kind, "statement": str(redact(behavior.statement)), "source": f"{behavior.file}:{behavior.line}"}
                    for behavior in entry.behaviors
                ],
                "review": _review_dict(_review(entry)),
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


def render_module(
    module: str,
    entries: list[EntryPoint],
    git: GitInfo,
    scan: ScanResult,
    *,
    comparison: str,
    module_meta: dict[str, Any] | None = None,
) -> str:
    dirty = "；包含未提交变更" if git.includes_uncommitted else ""
    lines = [
        f"# {_display(module)}流程设计",
        "",
        f"> 生效 Git 版本：`{git.target}`{dirty}",
        ">",
        f"> 源码指纹：`{scan.source_fingerprint}`；来源类型：已实现源码与配置（不是未来方案）。",
        ">",
        f"> 覆盖说明：本文依据指定版本代码整理，覆盖本模块 {len(entries)} 个可确认业务入口；无法从代码确认的内容保留为“代码中未确认”。",
        f">",
        f"> 模块边界：{(entries[0].module_rationale if entries else '代码中未确认').rstrip('。')}。",
        f"> 模块职责：{str((module_meta or {}).get('responsibility', '代码中未确认')).rstrip('。')}。",
        f"> 核心对象：{', '.join(str(value) for value in (module_meta or {}).get('objects', [])) or '代码中未确认'}；关键协作方：{', '.join(str(value) for value in (module_meta or {}).get('partners', [])) or '代码中未确认'}。",
        f"> 待确认问题：{'；'.join(str(value) for value in (module_meta or {}).get('questions', [])) or '无'}。",
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
            "markdown_fact_mismatches": [],
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
    fact_mismatches: list[str] = []
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
            review = entry.get("review", {})
            if isinstance(review, dict):
                for field in ("purpose", "input", "outcome", "failure"):
                    value = str(review.get(field, "")).strip()
                    if value and value not in section:
                        fact_mismatches.append(f"{entry_id}:{field}")
                steps = review.get("steps", [])
                if isinstance(steps, list) and section and len(re.findall(r"^\d+\. ", section, re.MULTILINE)) < len(steps):
                    fact_mismatches.append(f"{entry_id}:steps")
                diagram_match = re.search(r"```mermaid\s*(.*?)```", section, re.DOTALL)
                diagram = diagram_match.group(1) if diagram_match else ""
                if diagram:
                    diagram_mismatches.extend(
                        f"{entry_id}:mermaid:{problem}" for problem in _validate_mermaid(diagram)
                    )
                    error_prefixes = {
                        f"alt {str(error.get('code', ''))}："
                        for error in entry.get("errors", [])
                        if isinstance(error, dict)
                    }
                    diagram_controls = [
                        line.split(" ", 1)[0]
                        for line in diagram.splitlines()
                        if line.startswith(("alt ", "opt ", "loop "))
                        and not any(line.startswith(prefix) for prefix in error_prefixes)
                    ]
                    review_controls = [
                        str(step.get("kind"))
                        for step in steps
                        if isinstance(step, dict) and step.get("kind") in {"alt", "opt", "loop"}
                    ]
                    if diagram_controls != review_controls:
                        diagram_mismatches.append(f"{entry_id}:mermaid:control-structure-mismatch")
                    for error in entry.get("errors", []):
                        if not isinstance(error, dict):
                            continue
                        code = str(error.get("code", ""))
                        consequence = str(redact(error.get("consequence", ""))).replace("\n", " ")[:100]
                        if code and code != "代码中未确认" and code not in diagram:
                            diagram_mismatches.append(f"{entry_id}:mermaid:error-code:{code}")
                        if consequence and consequence not in diagram:
                            diagram_mismatches.append(f"{entry_id}:mermaid:error-consequence:{code}")
                else:
                    diagram_mismatches.append(f"{entry_id}:missing-mermaid")
                for step in steps:
                    if not isinstance(step, dict):
                        continue
                    expected_text = str(redact(step.get("text", ""))).replace("\n", " ")[:120]
                    if expected_text and expected_text not in diagram:
                        diagram_mismatches.append(f"{entry_id}:step:{expected_text}")
            for code in entry.get("error_codes", []):
                if str(code) != "代码中未确认" and str(code) not in section:
                    missing_error_codes.append(f"{entry_id}:{code}")
            expected_errors: set[tuple[str, str, str, str, str, str, str, str]] = set()
            for error in entry.get("errors", []):
                if isinstance(error, dict):
                    code = str(error.get("code", ""))
                    evidence = str(error.get("source", ""))
                    condition = str(redact(error.get("condition", "")))
                    expected_errors.add((
                        code,
                        condition,
                        evidence,
                        str(redact(error.get("phase", ""))),
                        str(redact(error.get("capture_boundary", ""))),
                        str(redact(error.get("propagation", ""))),
                        str(redact(error.get("consequence", ""))),
                        str(redact(error.get("recovery", ""))),
                    ))
                    if (evidence and evidence not in section) or (condition and condition not in section):
                        missing_error_evidence.append(f"{entry_id}:{evidence}:{condition}")
            documented_errors = {
                (
                    match.group("code"), match.group("condition").strip(), match.group("source"),
                    match.group("phase").strip(), match.group("capture").strip(),
                    match.group("propagation").strip(), match.group("consequence").strip(),
                    match.group("recovery").strip(),
                )
                for match in re.finditer(
                    r"^- `(?P<code>[^`]+)`：(?P<condition>.*?)（阶段：(?P<phase>.*?)；捕获：(?P<capture>.*?)；传播：(?P<propagation>.*?)；后果：(?P<consequence>.*?)；恢复：(?P<recovery>.*?)；证据：`(?P<source>[^`]+)`）$",
                    section,
                    re.MULTILINE,
                )
            }
            stale_error_evidence.extend(
                f"{entry_id}:{code}:{source}:{condition}"
                for code, condition, source, _phase, _capture, _propagation, _consequence, _recovery
                in sorted(documented_errors - expected_errors)
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
        "markdown_fact_mismatches": sorted(dict.fromkeys(fact_mismatches)),
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
        (
            entry.entry_id, error.code, str(redact(error.condition)), f"{error.file}:{error.line}",
            str(redact(error.phase)), str(redact(error.capture_boundary)),
            str(redact(error.propagation)), str(redact(error.consequence)), str(redact(error.recovery)),
        )
        for entry in code_entries
        for error in entry.errors
        if error.code != "代码中未确认"
    }
    documented_errors = {
        (
            str(item.get("id")), str(error.get("code")), str(error.get("condition")), str(error.get("source")),
            str(error.get("phase")), str(error.get("capture_boundary")), str(error.get("propagation")),
            str(error.get("consequence")), str(error.get("recovery")),
        )
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
    preferred: dict[str, str] = {}
    module_metadata: dict[str, Any] = {}
    mapping: dict[str, Any] = {}
    module_map_path = docs_root / "business-flow-modules.json"
    if module_map_path.is_file():
        try:
            mapping = json.loads(module_map_path.read_text(encoding="utf-8"))
            for item in mapping.get("modules", []):
                if isinstance(item, dict) and item.get("name"):
                    name = str(item["name"])
                    preferred[name] = str(item.get("file", ""))
                    module_metadata[name] = item
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
    files = _module_files(docs_root, modules, previous, preferred)
    # Capture old Markdown before any module file is rewritten so the
    # comparison artifact describes the actual migration, not the new output.
    semantic_diffs = compare_markdown_facts(
        docs_root,
        previous,
        [entry for entry in scan.entries if not module_filter or entry.module == module_filter],
    )
    for module, filename in files.items():
        if module_filter and module != module_filter:
            continue
        entries = [entry for entry in scan.entries if entry.module == module]
        path = docs_root / filename
        if comparison == "version_only" and path.is_file():
            _update_version_only(path, scan.git)
        else:
            path.write_text(
                redact(render_module(module, entries, scan.git, scan, comparison=comparison, module_meta=module_metadata.get(module))),
                encoding="utf-8",
            )
    stale_files = {
        str(item.get("file")).replace("\\", "/")
        for item in previous.get("modules", [])
        if isinstance(item, dict)
        and str(item.get("file", "")).replace("\\", "/").endswith(".md")
        and ".." not in Path(str(item.get("file", ""))).parts
        and not Path(str(item.get("file", ""))).is_absolute()
    } - set(files.values())
    for filename in (stale_files if not module_filter else ()):
        path = docs_root / filename
        if path.is_file():
            path.unlink()
    mapping_migrations = mapping.get("migrations", [])
    index = build_index(
        scan, docs_root, files, comparison=comparison, old_commit=old_commit, migrations=mapping_migrations,
    )
    _require_contract(BUSINESS_FLOW_INDEX_SCHEMA, index, "business-flow index")
    index_path = write_json(docs_root / "business-flow-index.json", index)
    ownership = {
            "schema_version": int(BUSINESS_FLOW_SCHEMA_VERSION),
            "source_fingerprint": scan.source_fingerprint,
            "confirmed": True,
            "entries": [
                {"id": entry.entry_id, "module": entry.module, "file": files[entry.module]}
                for entry in scan.entries
            ],
        }
    _require_contract(BUSINESS_FLOW_OWNERSHIP_SCHEMA, ownership, "business-flow ownership")
    write_json(docs_root / "business-flow-ownership.json", ownership)
    migrations = {
            "schema_version": int(BUSINESS_FLOW_SCHEMA_VERSION),
            "source_fingerprint": scan.source_fingerprint,
            "migrations": mapping_migrations,
        }
    _require_contract(BUSINESS_FLOW_MIGRATIONS_SCHEMA, migrations, "business-flow migrations")
    write_json(docs_root / "business-flow-migrations.json", migrations)
    comparison_artifact = {
            "schema_version": int(BUSINESS_FLOW_SCHEMA_VERSION),
            "source_fingerprint": scan.source_fingerprint,
            "comparison": changed.get("comparison", comparison),
            "entry_alignment": {
                "added": changed.get("added_entries", []),
                "updated": changed.get("updated_entries", []),
                "deleted": changed.get("deleted_entries", []),
            },
            "version_only_documents": changed.get("version_only_documents", []),
            "business_changed_documents": changed.get("business_changed_documents", modules),
            "changed_paths": changed.get("business_changed_paths", changed.get("changed_paths", [])),
            "comparison_error": changed.get("comparison_error"),
            "semantic_diffs": semantic_diffs,
            "fact_diffs": [
                {
                    "id": entry.entry_id,
                    "changes": [field for field, old_value, new_value in (
                        ("type", old.get("type"), entry.kind),
                        ("identifier", old.get("identifier"), entry.identifier),
                        ("handler", old.get("handler"), entry.handler),
                        ("module", old.get("module"), entry.module),
                        ("errors", old.get("errors", []), [_error_dict(error) for error in entry.errors]),
                        ("behaviors", old.get("behaviors", []), [
                            {"kind": item.kind, "statement": str(redact(item.statement)), "source": f"{item.file}:{item.line}"}
                            for item in entry.behaviors
                        ]),
                    ) if old_value != new_value]
                }
                for entry in scan.entries
                for old in previous.get("entries", [])
                if isinstance(old, dict) and old.get("id") == entry.entry_id
            ],
        }
    _require_contract(BUSINESS_FLOW_COMPARISON_SCHEMA, comparison_artifact, "business-flow comparison")
    write_json(docs_root / "business-flow-comparison.json", comparison_artifact)
    evidence_cache = _cache_payload(scan)
    _require_contract(BUSINESS_FLOW_EVIDENCE_CACHE_SCHEMA, evidence_cache, "business-flow evidence cache")
    write_json(docs_root / "business-flow-evidence-cache.json", evidence_cache)
    dependency_graph = {
            "schema_version": int(BUSINESS_FLOW_SCHEMA_VERSION),
            "source_fingerprint": scan.source_fingerprint,
            "nodes": [
                {"entry": entry.entry_id, "functions": entry.functions}
                for entry in scan.entries
            ],
            "shared_sources": sorted({
                function
                for entry in scan.entries
                for function in entry.functions
                if ":" in function
            }),
        }
    _require_contract(BUSINESS_FLOW_DEPENDENCY_GRAPH_SCHEMA, dependency_graph, "business-flow dependency graph")
    write_json(docs_root / "business-flow-dependency-graph.json", dependency_graph)
    reported_entries = [entry for entry in scan.entries if not module_filter or entry.module == module_filter]
    reported_modules = sorted({entry.module for entry in reported_entries})
    reported_ids = {entry.entry_id for entry in reported_entries}
    report_coverage = coverage(scan, index, docs_root, module_filter)
    confirmed_review_ids = {
        str(item.get("id"))
        for item in index.get("entries", [])
        if isinstance(item, dict)
        and (not module_filter or item.get("module") == module_filter)
        and isinstance(item.get("review"), dict)
        and item["review"].get("status") == "confirmed"
        and item["review"].get("confirmed_by")
    }
    completed_entry_count = len(confirmed_review_ids) - len(
        set(report_coverage["markdown_missing_entries"])
    )
    pending_review_count = len([
        item for item in index.get("entries", [])
        if isinstance(item, dict)
        and (not module_filter or item.get("module") == module_filter)
        and (not isinstance(item.get("review"), dict)
             or item["review"].get("status") != "confirmed"
             or not item["review"].get("confirmed_by"))
    ])
    report = {
        "schema_version": int(BUSINESS_FLOW_SCHEMA_VERSION),
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
        "candidate_entry_count": scan.candidate_entry_count,
        "confirmed_binding_count": scan.confirmed_binding_count,
        "confirmed_handler_count": scan.confirmed_handler_count,
        "completed_entry_count": max(0, completed_entry_count),
        "excluded_entry_count": len(scan.exclusions),
        "pending_review_count": pending_review_count,
        "added_entries": [value for value in changed.get("added_entries", []) if value in reported_ids],
        "updated_entries": [value for value in changed.get("updated_entries", []) if value in reported_ids],
        "deleted_entries": changed.get("deleted_entries", []) if not module_filter else [],
        "version_only_documents": [value for value in changed.get("version_only_documents", []) if value in reported_modules],
        "business_changed_documents": [value for value in changed.get("business_changed_documents", modules) if value in reported_modules],
        "comparison": changed.get("comparison", comparison),
        "comparison_error": changed.get("comparison_error"),
        "exclusions": scan.exclusions,
        "unresolved": scan.unresolved,
        "coverage": report_coverage,
        "index_path": str(index_path),
    }
    _require_contract(BUSINESS_FLOW_REPORT_SCHEMA, report, "business-flow report")
    report_path = write_json(docs_root / "business-flow-report.json", report)
    return index_path, report_path, report
