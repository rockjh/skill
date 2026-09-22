"""Design-document discovery, parsing, and OpenAPI coverage mapping.

Design documents remain the only business authority.  The parser keeps explicit
markers when present, and otherwise extracts conservative semantic candidates
from reviewed prose/code while retaining quotes, evidence levels, and unknowns;
it never infers business behaviour from source code or runtime responses.
"""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from .api_test_manifest_io import load_data

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE")
METHOD_PATH_RE = re.compile(
    r"(?im)^\s*(?:#{1,6}\s*)?(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE)\s+(`?/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%{}-]*`?)\s*$"
)
# Natural-language designs commonly put the operation in a sentence (or in a
# curl example) instead of using a heading.  The fallback is deliberately
# conservative: it only creates a candidate when an HTTP verb is followed by a
# path, and never invents a request body or a business result.
INLINE_METHOD_PATH_RE = re.compile(
    r"(?i)\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE)\s+"
    r"(?:\|\s*)?(?:(?:https?://[^/\s|]+))?(`?/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%{}-]+`?)"
)
TABLE_METHOD_PATH_RE = re.compile(
    r"(?i)\|\s*(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE)\s*\|\s*"
    r"(`?/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%{}-]+`?)"
)
PATH_PARAMETER_RE = re.compile(r"\{([^{}]+)\}")
DESIGN_VERSION_RE = re.compile(
    r"(?im)^\s*(?:api\s+version|design\s+version|接口版本|设计版本)\s*[:：=]\s*([A-Za-z0-9_.-]+)\s*$"
)
STATUS_ASSERTION_RE = re.compile(
    r"(?i)(?:status|state|状态)\s*(?:is|becomes?|changes?\s+to|=|:|为|变为|变成|更新为)?\s*"
    r"([A-Za-z][A-Za-z0-9_-]*|[\u3400-\u4dbf\u4e00-\u9fff]{1,24})"
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
ABSENT_ASSERTION_RE = re.compile(r"(?im)^\s*(?:assert\s+absent|absence|不存在)\s*[:：]\s*(\$[^\s]+)\s*$")
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


def _clean_route(route: str) -> str:
    cleaned = route.strip("`'\"").rstrip(".,;:)\"'")
    return re.split(r"[?#]", cleaned, 1)[0] or "/"


def _natural_sentences(content: str) -> list[str]:
    values = re.split(r"(?:\r?\n|(?<=[.!?。！？；;]))", content)
    return [re.sub(r"\s+", " ", value).strip(" -*\t") for value in values if value.strip()]


def _semantic_fields(content: str) -> dict[str, Any]:
    """Extract only business facts stated in prose.

    This is intentionally a lossy candidate extractor.  It records the source
    sentence and leaves unsupported details unknown; OpenAPI still owns all
    transport shape and the generation gate decides whether a candidate is
    executable.
    """

    sentences = _natural_sentences(content)
    status_values = []
    for match in STATUS_ASSERTION_RE.finditer(content):
        value = match.group(1).strip()
        if value.casefold() not in {
            "is", "to", "and", "or", "from", "change", "changes", "changed",
            "become", "becomes", "became", "updated", "update",
        }:
            status_values.append(value)
    status_values = list(dict.fromkeys(status_values))
    http_statuses = []
    for match in re.finditer(
        r"(?i)(?:\bHTTP(?:\s+status|\s+code)?\s*[:=]?\s*|\breturns?\s+|\u8fd4\u56de\s*)([1-5]\d{2})\b",
        content,
    ):
        http_statuses.append(int(match.group(1)))
    http_statuses = list(dict.fromkeys(http_statuses))
    transitions = [
        f"{match.group(1)} -> {match.group(2)}"
        for match in re.finditer(
            r"(?i)(?:from|由|从)\s*([A-Za-z][A-Za-z0-9_-]*|[\u3400-\u4dbf\u4e00-\u9fff]{1,24})\s*"
            r"(?:to|到|->|变为|变成)\s*([A-Za-z][A-Za-z0-9_-]*|[\u3400-\u4dbf\u4e00-\u9fff]{1,24})",
            content,
        )
    ]
    codes: list[Any] = []
    for match in re.finditer(
        r"(?i)(?:business\s+(?:error\s+)?code|error\s+code|业务(?:错误)?码)\s*(?:is|=|:|为)?\s*([A-Za-z0-9_.-]+)",
        content,
    ):
        raw = match.group(1).rstrip(".,;:)]}")
        try:
            codes.append(_typed_value(raw))
        except ValueError:
            codes.append(raw)
    def matching(*terms: str) -> list[str]:
        def searchable(sentence: str) -> str:
            # Endpoint names such as `/records/retry` must not turn an
            # otherwise ordinary operation into a retry/idempotency rule.
            return re.sub(r"(?:https?://|/)[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%{}-]*", " ", sentence).casefold()

        return [sentence for sentence in sentences if any(term in searchable(sentence) for term in terms)]

    preconditions = matching("precondition", "before", "when", "only if", "前置", "前提", "当")
    errors = matching("error", "fail", "failed", "reject", "invalid", "错误", "失败", "拒绝")
    request_meaning = matching(
        "request field", "request parameter", "payload", "business id", "业务标识", "请求字段", "请求参数"
    )
    side_effects = matching("side effect", "write", "persist", "database", "external call", "落库", "写入", "外部调用")
    negative_constraints = [
        sentence for sentence in side_effects
        if re.search(r"(?i)(?:\b(?:no|without|never|not|does not)\b|不会|不落库|不写入|不重复写入|不产生重复|无任何|不得)", sentence)
    ]
    idempotency = matching("idempotent", "duplicate", "repeat", "same key", "幂等", "重复")
    retries = matching("retry", "retries", "again", "重试")
    concurrency = matching("concurrent", "parallel", "simultaneous", "并发", "同时")
    asynchronous = matching("async", "asynchronous", "poll", "eventually", "异步", "轮询", "最终")
    consistency = matching("consistent", "consistency", "atomic", "same transaction", "一致", "原子", "同一事务")
    success = matching("success", "successful", "completed", "created", "accepted", "成功", "完成", "创建")
    signals = [
        *preconditions, *request_meaning, *errors, *side_effects, *idempotency, *retries,
        *concurrency, *asynchronous, *consistency, *success, *status_values, *http_statuses, *codes,
    ]
    scenario = "business_error" if errors and not success else "success"
    if idempotency or retries or concurrency:
        scenario = "safety"
    assertions = [{"path": "$.status", "equals": value} for value in status_values]
    if codes:
        assertions.append({"path": "$.code", "equals": codes[0]})
    candidate_assertions: list[dict[str, Any]] = [
        {"kind": "response", "assertion": dict(assertion), "executable": True}
        for assertion in assertions
    ]
    for match in re.finditer(r"(?i)(?:does not return|without response field|不存在字段)\s*(\$\.[A-Za-z0-9_.-]+)", content):
        assertion = {"path": match.group(1).rstrip(".,;:)") , "exists": False}
        assertions.append(assertion)
        candidate_assertions.append({"kind": "response", "assertion": assertion, "executable": True})
    candidate_assertions.extend(
        {
            "kind": "negative_side_effect",
            "expectation": sentence,
            "observable_boundary": None,
            "executable": False,
        }
        for sentence in negative_constraints
    )
    candidate_assertions.extend(
        {
            "kind": "idempotency",
            "expectation": sentence,
            "request_count": 2,
            "executable": False,
        }
        for sentence in idempotency
    )
    candidate_assertions.extend(
        _retry_candidate_assertion(sentence)
        for sentence in retries
    )
    return {
        "preconditions": preconditions,
        "request_meaning": request_meaning,
        "success_results": success,
        "business_errors": errors,
        "side_effects": side_effects,
        "negative_constraints": negative_constraints,
        "idempotency": idempotency,
        "retries": retries,
        "concurrency": concurrency,
        "async_notes": asynchronous,
        "consistency": consistency,
        "states": status_values,
        "transitions": list(dict.fromkeys(transitions)),
        "assertions": assertions,
        "candidate_assertions": candidate_assertions,
        "business_codes": codes,
        "http_statuses": http_statuses,
        "scenario": scenario,
        "signals": signals,
    }


def _retry_candidate_assertion(sentence: str) -> dict[str, Any]:
    """Normalize only retry facts that are stated without ambiguity."""

    lowered = sentence.casefold()
    assertion: dict[str, Any] = {
        "kind": "retry",
        "expectation": sentence,
        "executable": False,
    }
    state_matches = re.findall(
        r"(?i)(?:reset(?:s|ted)?(?:\s+the\s+step)?\s+to|set\s+to|重置为|置为|更新为|变为)\s*"
        r"([A-Za-z][A-Za-z0-9_-]*|[\u3400-\u4dbf\u4e00-\u9fff]{1,24})",
        sentence,
    )
    if state_matches:
        assertion["state_changes"] = [{
            "subject": (
                "first_failed_step"
                if "first failed" in lowered or "第一个失败" in sentence
                else "documented_retry_target"
            ),
            "to": state,
        } for state in dict.fromkeys(state_matches)]
    successful_steps = any(
        value in lowered for value in ("previous successful", "already successful", "successful steps")
    ) or any(value in sentence for value in ("前面成功", "之前成功", "已成功", "成功步骤"))
    not_reexecuted = any(
        value in lowered for value in ("not rerun", "not re-run", "do not execute again", "not execute again")
    ) or any(value in sentence for value in ("不重复执行", "不再执行"))
    if successful_steps and not_reexecuted:
        assertion["successful_steps_unchanged"] = True
        assertion["successful_steps_not_reexecuted"] = True
    return assertion


def _rule_id(path: Path, method: str, route: str, line: int, content: str) -> str:
    digest = hashlib.sha256(f"{path}:{method} {route}:{line}:{content}".encode("utf-8")).hexdigest()[:12]
    return "DESIGN-" + digest


def _unknown_design_id(item: dict[str, Any]) -> str:
    category = str(item.get("category") or "design_without_openapi")
    return "UNKNOWN-DESIGN-" + hashlib.sha256(
        (str(item.get("path", "")) + category).encode("utf-8")
    ).hexdigest()[:12]


def _inline_body_start(text: str, start: int) -> int:
    """Keep inline endpoint evidence to its containing sentence."""

    boundaries = [text.rfind(value, 0, start) for value in ("\n", ".", "。", "!", "！", "?", "？", ";", "；")]
    return max(boundaries, default=-1) + 1


def _ordered_endpoint_context(text: str, start: int) -> bool:
    """Return whether the endpoint is part of an explicitly ordered sequence."""

    line_start = text.rfind("\n", 0, start) + 1
    prefix = text[line_start:start]
    if re.search(
        r"(?i)^\s*(?:\d+[.)]|\|\s*\d+\s*\||[-*]\s*(?:\[[ xX]\]\s*)?(?:step\s*)?\d*|"
        r"step\s+\d+|curl\b|步骤\s*\d+|[^\r\n]*?(?:-->>|->>|--?>|=>))",
        prefix,
    ):
        return True
    return bool(re.search(
        r"(?i)(?:\b(?:first|second|then|next|after|finally|subsequently)\b|第一步|第二步|然后|随后|接着|最后)",
        prefix,
    ))


def _section_records(path: Path, *, include_inline: bool = True) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8", errors="strict")
    heading_matches = list(METHOD_PATH_RE.finditer(text))
    match_records: list[tuple[re.Match[str], bool]] = [(match, False) for match in heading_matches]
    if include_inline:
        inline_matches = [*INLINE_METHOD_PATH_RE.finditer(text), *TABLE_METHOD_PATH_RE.finditer(text)]
        seen_spans: set[tuple[int, int]] = set()
        seen_operations: list[tuple[int, int, str, str]] = []
        for match in sorted(inline_matches, key=lambda value: value.start()):
            if (match.start(), match.end()) in seen_spans:
                continue
            seen_spans.add((match.start(), match.end()))
            operation = (match.group(1).upper(), _clean_route(match.group(2)))
            if any(
                existing_start <= match.start() < existing_end
                and existing_method == operation[0]
                and existing_path == operation[1]
                for existing_start, existing_end, existing_method, existing_path in seen_operations
            ):
                continue
            seen_operations.append((match.start(), match.end(), operation[0], operation[1]))
            if any(
                match.start() >= heading.start() and match.end() <= heading.end()
                for heading in heading_matches
            ):
                continue
            match_records.append((match, True))
    match_records.sort(key=lambda item: item[0].start())
    records: list[dict[str, Any]] = []
    for section_index, (match, inline) in enumerate(match_records):
        method = match.group(1).upper()
        route = _clean_route(match.group(2))
        body_start = match.end()
        if inline:
            # Keep the sentence containing the endpoint as evidence.
            body_start = _inline_body_start(text, match.start())
        body_end = match_records[section_index + 1][0].start() if section_index + 1 < len(match_records) else len(text)
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
                    codes.append(_typed_value(value.group(1).strip().rstrip(".,;:)]}")))
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
            for assertion in ABSENT_ASSERTION_RE.finditer(content):
                assertions.append({"path": assertion.group(1).strip(), "exists": False})
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
            semantic = _semantic_fields(content)
            explicit = bool(
                scenario_match or request_match or assertions or statuses or codes
                or any(MARKER_RE[name].search(content) for name in (
                    "condition", "async", "transition", "side_effect", "idempotency",
                    "retry", "concurrency", "external_failure", "flow",
                ))
            )
            if not explicit:
                scenario = semantic["scenario"]
                states = semantic["states"]
                statuses = semantic["http_statuses"]
                assertions = semantic["assertions"]
                codes = semantic["business_codes"]
                is_async = bool(semantic["async_notes"])
                if is_async and len(states) >= 2:
                    acceptance = [states[0]]
                    final = [states[-1]]
            semantic_side_effects = semantic["side_effects"]
            semantic_idempotency = semantic["idempotency"]
            semantic_retries = semantic["retries"]
            semantic_concurrency = semantic["concurrency"]
            marker_side_effects = [value.group(1).strip() for value in MARKER_RE["side_effect"].finditer(content)]
            marker_idempotency = [value.group(1).strip() for value in MARKER_RE["idempotency"].finditer(content)]
            marker_retries = [value.group(1).strip() for value in MARKER_RE["retry"].finditer(content)]
            marker_concurrency = [value.group(1).strip() for value in MARKER_RE["concurrency"].finditer(content)]
            evidence_level = "explicit" if explicit else ("derived" if semantic["signals"] else "unknown")
            candidate_assertions = list(semantic["candidate_assertions"])
            for assertion in assertions:
                candidate = {"kind": "response", "assertion": dict(assertion), "executable": True}
                if candidate not in candidate_assertions:
                    candidate_assertions.append(candidate)
            title = rule_id or next(iter(_natural_sentences(content)), section_title)[:120]
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
                "transitions": [value.group(1).strip() for value in MARKER_RE["transition"].finditer(content)] or semantic["transitions"],
                "side_effects": marker_side_effects or semantic_side_effects,
                "negative_constraints": semantic["negative_constraints"],
                "idempotency": marker_idempotency or semantic_idempotency,
                "retries": marker_retries or semantic_retries,
                "concurrency": marker_concurrency or semantic_concurrency,
                "external_failures": [value.group(1).strip() for value in MARKER_RE["external_failure"].finditer(content)],
                "async": is_async,
                "acceptance_statuses": acceptance,
                "final_statuses": final,
                "assertions": assertions,
                "candidate_assertions": candidate_assertions,
                "request": request,
                "request_declared": request_match is not None,
                "evidence_level": evidence_level,
                "derivation": (
                    "Converted stated business sentences into candidate fields; no transport or business value was invented."
                    if not explicit and semantic["signals"] else ""
                ),
                "understanding": {
                    "preconditions": semantic["preconditions"],
                    "request_meaning": (
                        [{"request": request}] if isinstance(request, dict) else semantic["request_meaning"]
                    ),
                    "success_results": semantic["success_results"],
                    "business_errors": semantic["business_errors"],
                    "side_effects": semantic["side_effects"],
                    "idempotency": semantic["idempotency"],
                    "retries": semantic["retries"],
                    "concurrency": semantic["concurrency"],
                    "async": semantic["async_notes"],
                    "consistency": semantic["consistency"],
                    "negative_constraints": semantic["negative_constraints"],
                },
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
                    "quote": content[:2000],
                    "evidence_level": evidence_level,
                },
                "_ordered": _ordered_endpoint_context(text, match.start()),
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
    allowed_flow = {"id", "mode", "steps", "cleanup", "final_status", "data_transfer"}
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
            step: dict[str, Any] = {
                "rule_id": rule_id,
                "operation": operation,
                "business_assertions": list(rule.get("candidate_assertions", rule.get("assertions", []))),
            }
            if capture_paths:
                step["capture"] = list(capture_paths)
                step["capture_paths"] = capture_paths
            if uses:
                step["uses"] = uses
            if raw_step.get("assert_absent"):
                if not isinstance(raw_step["assert_absent"], str) or not raw_step["assert_absent"].startswith("$"):
                    errors.append(f"design flow {flow_id} step {index} assert_absent needs a JSON path")
                elif not any(
                    item.get("path") == raw_step["assert_absent"]
                    and (item.get("equals", object()) is None or item.get("exists") is False)
                    for item in rule.get("assertions", []) if isinstance(item, dict)
                ):
                    errors.append(
                        f"design flow {flow_id} step {index} has no design assertion for absent {raw_step['assert_absent']}"
                    )
                step["assert_absent"] = raw_step["assert_absent"]
            steps.append(step)
            captured.update(capture_paths)
        declared_transfer = declaration.get("data_transfer")
        if declared_transfer is not None and not isinstance(declared_transfer, list):
            errors.append(f"design flow {flow_id} data_transfer must be a list")
            declared_transfer = None
        normalized: dict[str, Any] = {
            "id": flow_id,
            "mode": mode,
            "source": "design",
            "design_rule_ids": [str(step.get("rule_id")) for step in steps],
            "steps": steps,
            "business_assertions": [
                assertion
                for step in steps
                for assertion in step.get("business_assertions", [])
                if isinstance(assertion, dict)
            ],
            "data_transfer": copy.deepcopy(declared_transfer) if declared_transfer is not None else [
                {"captures": step.get("capture_paths", {}), "uses": step.get("uses", [])}
                for step in steps
                if step.get("capture_paths") or step.get("uses")
            ],
        }
        last_rule = rules.get(str(steps[-1].get("rule_id")), {}) if steps else {}
        final_status = str(declaration.get("final_status", "")).strip()
        if not final_status:
            final_status = str((last_rule.get("final_statuses") or last_rule.get("states") or [""])[-1]).strip()
        if not final_status:
            final_status = next(
                (
                    str(item.get("assertion", {}).get("equals"))
                    for item in steps[-1].get("business_assertions", [])
                    if isinstance(item, dict)
                    and item.get("kind") == "response"
                    and str(item.get("assertion", {}).get("path", "")).casefold() == "$.status"
                    and "equals" in item.get("assertion", {})
                ),
                "",
            )
        if final_status:
            normalized["final_status"] = final_status
        cleanup = str(declaration.get("cleanup", "")).strip()
        methods = [str(rules.get(str(step.get("rule_id")), {}).get("method", "")).upper() for step in steps]
        if not cleanup and methods and all(method in {"GET", "HEAD", "OPTIONS"} for method in methods):
            cleanup = "not required: all flow operations are read-only"
        if not cleanup and any(method in {"POST", "PUT", "PATCH", "DELETE"} for method in methods):
            errors.append(f"design flow {flow_id} must declare a cleanup action for mutating operations")
        normalized["cleanup"] = cleanup
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


def _natural_flow_candidates(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn unlabelled ordered calls into auditable flow candidates.

    A candidate is executable when order, assertions, and data dependencies are
    explicit enough to run without inventing captures.  Otherwise it remains a
    concrete pending candidate.
    """

    grouped: dict[str, list[dict[str, Any]]] = {}
    for section in sections:
        grouped.setdefault(str(section.get("evidence", {}).get("file", "")), []).append(section)
    candidates: list[dict[str, Any]] = []
    for source, values in grouped.items():
        operations = []
        seen: set[str] = set()
        ordered_values = [item for item in values if item.get("_ordered") is True]
        if len(ordered_values) < 2:
            continue
        for item in sorted(ordered_values, key=lambda value: int(value.get("section_line", 0))):
            operation = f"{item.get('method')} {item.get('path')}"
            if operation in seen:
                continue
            seen.add(operation)
            operations.append(item)
        if len(operations) < 2:
            continue
        candidate_id = "FLOW-CANDIDATE-" + hashlib.sha256(
            f"{source}:{','.join(str(item.get('id')) for item in operations)}".encode("utf-8")
        ).hexdigest()[:12]
        unknown: list[str] = []
        for item in operations[1:]:
            if PATH_PARAMETER_RE.findall(str(item.get("path", ""))):
                unknown.append("step-to-step data transfer")
        if any(not item.get("assertions") for item in operations):
            unknown.append("business assertions")
        final_status = str((operations[-1].get("final_statuses") or operations[-1].get("states") or [""])[-1]).strip()
        if not final_status:
            unknown.append("final status")
        mutating = any(str(item.get("method", "")).upper() in {"POST", "PUT", "PATCH", "DELETE"} for item in operations)
        cleanup = ""
        if mutating:
            unknown.append("owned test-data isolation and cleanup action")
        else:
            cleanup = "not required: all ordered operations are read-only"
        can_generate = not unknown
        candidates.append({
            "id": candidate_id,
            "name": f"ordered design calls in {Path(source).name or 'design'}",
            "source": source,
            "evidence_level": "derived",
            "steps": [
                {
                    "order": index,
                    "rule_id": item.get("id"),
                    "operation": f"{item.get('method')} {item.get('path')}",
                    "business_assertions": list(item.get("assertions", [])),
                }
                for index, item in enumerate(operations, start=1)
            ],
            "data_transfer": [],
            "final_status": final_status,
            "cleanup": cleanup,
            "unknown": list(dict.fromkeys(unknown)),
            "can_generate": can_generate,
        })
    return candidates


def _path_shape(path: str) -> str:
    return PATH_PARAMETER_RE.sub("{}", str(path).rstrip("/")) or "/"


def _semantic_tokens(value: Any) -> set[str]:
    """Return conservative ASCII and CJK business-name tokens."""

    text = str(value or "").casefold()
    tokens = set(re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", text))
    for chunk in re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]{2,}", text):
        tokens.add(chunk)
        tokens.update(chunk[index:index + 2] for index in range(len(chunk) - 1))
    return {token for token in tokens if token}


def _endpoint_semantic_tokens(endpoint: dict[str, Any]) -> set[str]:
    value = " ".join(
        str(endpoint.get(key) or "")
        for key in ("operation_id", "summary", "description", "path")
    )
    return _semantic_tokens(value)


def _semantic_operation_records(
    path: Path,
    text: str,
    endpoints: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Create semantic candidates when prose names an operation but no route.

    A candidate is accepted only for a unique token match. Ties and weak
    matches are returned as auditable unresolved mapping records instead of
    silently selecting an OpenAPI operation.
    """

    design_tokens = _semantic_tokens(text)
    scored = []
    for endpoint in endpoints:
        overlap = design_tokens & _endpoint_semantic_tokens(endpoint)
        if overlap:
            scored.append((len(overlap), endpoint, overlap))
    if not scored:
        return [], [{"category": "design_without_openapi", "path": str(path), "quote": text[:2000], "candidates": []}]
    best = max(score for score, _, _ in scored)
    winners = [(endpoint, overlap) for score, endpoint, overlap in scored if score == best]
    if best < 2:
        return [], [{"category": "design_without_openapi", "path": str(path), "quote": text[:2000], "candidates": []}]
    if len(winners) != 1:
        return [], [{
            "category": "multiple_candidates",
            "path": str(path),
            "quote": text[:2000],
            "candidates": [
                {"endpoint_id": str(endpoint.get("id", "")), "operation": f"{endpoint.get('method')} {endpoint.get('path')}"}
                for endpoint, _ in winners
            ],
        }]
    endpoint, overlap = winners[0]
    semantic = _semantic_fields(text)
    line = 1
    record = {
        "id": _rule_id(path, str(endpoint.get("method")), str(endpoint.get("path")), line, text),
        "method": str(endpoint.get("method", "")).upper(),
        "path": str(endpoint.get("path", "")),
        "title": next(iter(_natural_sentences(text)), str(endpoint.get("summary") or endpoint.get("operation_id") or "semantic candidate"))[:120],
        "content": text.strip(),
        "scenario": semantic["scenario"],
        "condition": " ".join(semantic["preconditions"][:1]),
        "business_codes": semantic["business_codes"],
        "http_statuses": semantic["http_statuses"],
        "states": semantic["states"],
        "transitions": semantic["transitions"],
        "side_effects": semantic["side_effects"],
        "negative_constraints": semantic["negative_constraints"],
        "idempotency": semantic["idempotency"],
        "retries": semantic["retries"],
        "concurrency": semantic["concurrency"],
        "external_failures": [],
        "async": bool(semantic["async_notes"]),
        "acceptance_statuses": [],
        "final_statuses": [],
        "assertions": semantic["assertions"],
        "candidate_assertions": semantic["candidate_assertions"],
        "request": None,
        "request_declared": False,
        "evidence_level": "derived" if semantic["signals"] else "unknown",
        "_semantic_candidate": True,
        "derivation": f"Unique semantic candidate matched OpenAPI tokens: {', '.join(sorted(overlap))}.",
        "understanding": {
            "preconditions": semantic["preconditions"],
            "request_meaning": semantic["request_meaning"],
            "success_results": semantic["success_results"],
            "business_errors": semantic["business_errors"],
            "side_effects": semantic["side_effects"],
            "idempotency": semantic["idempotency"],
            "retries": semantic["retries"],
            "concurrency": semantic["concurrency"],
            "async": semantic["async_notes"],
            "consistency": semantic["consistency"],
            "negative_constraints": semantic["negative_constraints"],
        },
        "_flows": [],
        "marker_errors": [],
        "section_line": line,
        "section_sha256": hashlib.sha256(text.strip().encode("utf-8")).hexdigest(),
        "evidence": {
            "source_kind": "design",
            "file": str(path),
            "symbol": "semantic candidate",
            "line": line,
            "endpoint_scope": [f"{endpoint.get('method')} {endpoint.get('path')}"],
            "confidence": "medium",
            "quote": text[:2000],
            "evidence_level": "derived" if semantic["signals"] else "unknown",
        },
    }
    return [record], []


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
    unresolved_documents: list[dict[str, Any]] = []
    endpoint_preview = [item for item in manifest.get("endpoints", []) if isinstance(item, dict)]
    for path in files:
        try:
            content = path.read_bytes()
            marker_parsed = _section_records(path, include_inline=False)
            parsed = _section_records(path)
            text = content.decode("utf-8")
            if not parsed:
                parsed, semantic_unresolved = _semantic_operation_records(path, text, endpoint_preview)
            else:
                semantic_unresolved = []
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            return {}, [f"cannot read design document {path}: {exc}"]
        marker_count = len({(item.get("method"), item.get("path"), item.get("section_line")) for item in marker_parsed})
        semantic_record_count = len(parsed) + len(semantic_unresolved)
        marker_operations = sorted({f"{item.get('method')} {item.get('path')}" for item in marker_parsed})
        semantic_operations = sorted({f"{item.get('method')} {item.get('path')}" for item in parsed})
        documents.append({
            "path": str(path),
            "sha256": hashlib.sha256(content).hexdigest(),
            "design_version": next((match.group(1) for match in DESIGN_VERSION_RE.finditer(text)), None),
            "sections": marker_count,
            "semantic_rules": semantic_record_count,
            "parser_gap": semantic_record_count > marker_count,
            "marker_operations": marker_operations,
            "semantic_operations": semantic_operations,
            "semantic_only_operations": sorted(set(semantic_operations) - set(marker_operations))
            or (["unmapped design prose"] if semantic_unresolved else []),
            "understanding": "unknown" if not parsed else (
                "semantic" if semantic_unresolved or any(item.get("evidence_level") != "explicit" for item in parsed) else "marker"
            ),
        })
        if not parsed:
            unresolved_documents.extend(
                semantic_unresolved
                or [{"path": str(path), "quote": text[:2000], "category": "design_without_openapi", "candidates": []}]
            )
        for item in parsed:
            item["id"] = item["id"] or "DESIGN-" + hashlib.sha256(
                f"{path}:{item['method']} {item['path']}:{item.get('section_line')}:{item.get('section_sha256')}".encode("utf-8")
            ).hexdigest()[:12]
            sections.append(item)
    flows, flow_errors = _normalize_flows(sections)
    flow_candidates = _natural_flow_candidates(sections) if not flows else []
    for candidate in flow_candidates:
        if not candidate.get("can_generate"):
            continue
        flow_steps = [
            {
                "rule_id": step.get("rule_id"),
                "operation": str(step.get("operation", "")).casefold(),
                "business_assertions": [
                    {"kind": "response", "assertion": dict(item), "executable": True}
                    for item in step.get("business_assertions", [])
                    if isinstance(item, dict)
                ],
            }
            for step in candidate.get("steps", [])
        ]
        generated_flow = {
            "id": candidate.get("id"),
            "mode": "sequential",
            "source": "design",
            "design_rule_ids": [str(step.get("rule_id")) for step in flow_steps],
            "steps": flow_steps,
            "business_assertions": [
                assertion for step in flow_steps for assertion in step.get("business_assertions", [])
            ],
            "data_transfer": list(candidate.get("data_transfer", [])),
            "final_status": candidate.get("final_status", ""),
        }
        if candidate.get("cleanup"):
            generated_flow["cleanup"] = str(candidate.get("cleanup"))
        flows.append(generated_flow)
    endpoints = [item for item in manifest.get("endpoints", []) if isinstance(item, dict)]
    endpoint_keys = {
        f"{str(item.get('method', '')).upper()} {item.get('path')}": str(item.get("id", ""))
        for item in endpoints
    }
    endpoint_by_id = {str(item.get("id")): item for item in endpoints if item.get("id")}
    endpoint_shapes: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for endpoint in endpoints:
        endpoint_shapes.setdefault(
            (str(endpoint.get("method", "")).upper(), _path_shape(str(endpoint.get("path", "")))),
            [],
        ).append(endpoint)
    exclusions = _explicit_exclusions(project_root, files, qa_root)
    errors: list[str] = list(flow_errors)
    design_versions = {
        str(item.get("design_version"))
        for item in documents
        if str(item.get("design_version", "")).strip()
    }
    if len(design_versions) > 1:
        errors.append("design documents declare conflicting versions: " + ", ".join(sorted(design_versions)))
    openapi_version = str(manifest.get("source", {}).get("api_version", "")).strip()
    if openapi_version and design_versions and openapi_version not in design_versions:
        errors.append(
            f"design/OpenAPI version conflict: design={','.join(sorted(design_versions))} OpenAPI={openapi_version}"
        )
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
    mapping: list[dict[str, Any]] = []
    mapped_endpoint_ids: set[str] = set()
    conflicting_operations: list[tuple[str, list[dict[str, Any]]]] = []
    conflicting_keys: set[str] = set()
    for section in sections:
        key = f"{section['method']} {section['path']}"
        by_key.setdefault(key, []).append(section)
        exact = endpoint_keys.get(key)
        aliases: list[dict[str, Any]] = []
        if not exact:
            aliases = endpoint_shapes.get((section["method"], _path_shape(section["path"])), [])
        if exact:
            category = "semantic_candidate" if section.get("_semantic_candidate") else "exact"
            endpoint_id = exact
            alias_map: dict[str, str] = {}
        elif len(aliases) == 1:
            category = "parameter_alias"
            endpoint_id = str(aliases[0].get("id"))
            design_names = PATH_PARAMETER_RE.findall(section["path"])
            openapi_names = PATH_PARAMETER_RE.findall(str(aliases[0].get("path")))
            alias_map = dict(zip(design_names, openapi_names))
        elif len(aliases) > 1:
            category = "multiple_candidates"
            endpoint_id = None
            alias_map = {}
        else:
            category = "design_without_openapi"
            endpoint_id = None
            alias_map = {}
        if endpoint_id:
            mapped_endpoint_ids.add(endpoint_id)
        section["_mapping"] = {
            "category": category,
            "design_operation": key,
            "openapi_operation": (
                f"{endpoint_by_id[endpoint_id].get('method')} {endpoint_by_id[endpoint_id].get('path')}"
                if endpoint_id in endpoint_by_id else None
            ),
            "endpoint_id": endpoint_id,
            "parameter_aliases": alias_map,
        }
        if category == "design_without_openapi":
            same_path_methods = [
                f"{item.get('method')} {item.get('path')}"
                for item in endpoints
                if str(item.get("path")) == section["path"]
                and str(item.get("method", "")).upper() != section["method"]
            ]
            if same_path_methods:
                section["_mapping"]["note"] = (
                    "HTTP method differs from OpenAPI candidates: " + ", ".join(same_path_methods)
                )
            elif any(
                _path_shape(str(item.get("path", ""))) == _path_shape(section["path"])
                for item in endpoints
            ):
                section["_mapping"]["note"] = "path structure is present only under a different OpenAPI operation"
        mapping.append({
            "rule_id": section.get("id"),
            **section["_mapping"],
        })
        if category == "design_without_openapi":
            errors.append(f"design contract drift: {key} is not present in OpenAPI")
        elif category == "multiple_candidates":
            errors.append(f"design operation {key} has multiple OpenAPI candidates")
    for key in endpoint_keys:
        endpoint_id = endpoint_keys[key]
        if endpoint_id not in mapped_endpoint_ids and key not in excluded_keys:
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
    for key, values in by_key.items():
        fingerprints = {str(item.get("section_sha256", "")) for item in values}
        files_for_key = {str(item.get("evidence", {}).get("file", "")) for item in values}
        if len(fingerprints) > 1 and len(files_for_key) > 1:
            conflicting_operations.append((key, values))
            conflicting_keys.add(key)
    for key, values in conflicting_operations:
        first = values[0] if values else {}
        evidence = first.get("evidence") if isinstance(first.get("evidence"), dict) else {
            "source_kind": "design", "file": "design", "symbol": key, "line": 1,
            "endpoint_scope": [key], "confidence": "low",
        }
        conflict_id = "DESIGN-CONFLICT-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
        manual_confirmations.append({
            "rule_id": conflict_id,
            "reasons": ["multiple selected design documents describe the same operation differently"],
            "evidence": evidence,
            "related_interface": key,
            "business_rule": conflict_id,
            "design_quote": "\n---\n".join(
                str(item.get("evidence", {}).get("quote", item.get("content", ""))[:1000])
                for item in values
            ),
            "current_derivation": "The operation is mapped, but the selected design documents disagree; no version was chosen.",
            "ambiguity": "The conflicting design descriptions may represent different contract versions or incompatible business expectations.",
            "impact": "The generator cannot choose a single business assertion, state, or side-effect expectation safely.",
            "options": [
                "select the authoritative design document/version",
                "merge the documents after resolving the conflict",
                "remove or explicitly exclude the obsolete document",
            ],
        })
    if len(design_versions) > 1 or (openapi_version and design_versions and openapi_version not in design_versions):
        first_document = documents[0] if documents else {"path": "design", "sha256": "0" * 64}
        version_id = "DESIGN-VERSION-CONFLICT"
        manual_confirmations.append({
            "rule_id": version_id,
            "reasons": ["design and contract versions cannot be matched uniquely"],
            "evidence": {
                "source_kind": "design", "file": str(first_document.get("path", "design")),
                "symbol": "version declaration", "line": 1, "endpoint_scope": ["design documents"],
                "confidence": "high", "quote": ", ".join(sorted(design_versions)),
                "evidence_level": "unknown",
            },
            "related_interface": "all versioned operations",
            "business_rule": version_id,
            "design_quote": ", ".join(sorted(design_versions)),
            "current_derivation": f"OpenAPI API version is {openapi_version or 'unspecified'}.",
            "ambiguity": "The documents and OpenAPI may describe different API versions.",
            "impact": "Endpoint mappings and business assertions may target the wrong contract version.",
            "options": ["select design documents for the OpenAPI version", "provide the matching OpenAPI contract"],
        })
    flows_by_rule: dict[str, list[dict[str, Any]]] = {}
    for flow in flows:
        for step in flow.get("steps", []):
            flows_by_rule.setdefault(str(step.get("rule_id", "")), []).append(flow)
    for section in sections:
        section = dict(section)
        key = f"{section['method']} {section['path']}"
        section["endpoint_id"] = section.get("_mapping", {}).get("endpoint_id")
        section["mapping_category"] = section.get("_mapping", {}).get("category", "design_without_openapi")
        section["matched_operation"] = section.get("_mapping", {}).get("openapi_operation")
        section["parameter_aliases"] = section.get("_mapping", {}).get("parameter_aliases", {})
        confirmation_reasons: list[str] = []

        def require_confirmation(reason: str) -> None:
            confirmation_reasons.append(reason)
            errors.append(f"design rule {section['id']} {reason}")

        if section["mapping_category"] in {"multiple_candidates", "design_without_openapi"}:
            require_confirmation(
                str(section.get("_mapping", {}).get("note") or "cannot uniquely match the design operation to an OpenAPI operation")
            )
        if key in conflicting_keys:
            require_confirmation("multiple selected design documents describe this operation differently")
        if section.get("evidence_level") == "unknown":
            require_confirmation("design prose does not state enough business facts to form a safe expectation")

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
        if section.get("scenario") == "business_error" and not section.get("business_codes"):
            require_confirmation("business error code is not defined by the design")
        if section.get("scenario") == "business_error" and not section.get("http_statuses"):
            require_confirmation("business error HTTP status is not defined by the design")
        if section.get("negative_constraints") and not any(
            isinstance(item, dict)
            and item.get("kind") == "response"
            and item.get("assertion", {}).get("exists") is False
            for item in section.get("candidate_assertions", [])
        ) and not any(
            isinstance(step, dict) and step.get("assert_absent")
            for flow in flows
            for step in flow.get("steps", [])
            if str(step.get("rule_id")) == str(section.get("id"))
        ):
            require_confirmation("negative side effect has no reviewed observable absence boundary")
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
            pending = {
                "related_interface": section.get("matched_operation") or f"{section['method']} {section['path']}",
                "business_rule": section["id"],
                "design_quote": section.get("evidence", {}).get("quote", section.get("content", "")[:1000]),
                "current_derivation": section.get("derivation") or section.get("condition") or section.get("title"),
                "ambiguity": "; ".join(dict.fromkeys(confirmation_reasons)),
                "impact": "business assertion, flow executability, or endpoint mapping cannot be determined safely",
                "options": ["confirm the documented expectation", "revise the design document with the missing fact"],
            }
            section["manual_confirmation"] = {
                "required": True,
                "reasons": list(dict.fromkeys(confirmation_reasons)),
                **pending,
            }
            manual_confirmations.append({
                "rule_id": section["id"],
                "reasons": list(dict.fromkeys(confirmation_reasons)),
                "evidence": section["evidence"],
                **pending,
            })
        section.pop("_mapping", None)
        section.pop("_semantic_candidate", None)
        section.pop("_ordered", None)
        rules.append(section)
    # Retain the reverse side of the mapping even when a contract operation has
    # no design text.  This lets reports distinguish a real omission from a
    # parser format difference.
    for key, endpoint_id in endpoint_keys.items():
        if endpoint_id not in mapped_endpoint_ids and key not in excluded_keys:
            mapping.append({
                "rule_id": None,
                "category": "openapi_without_design",
                "design_operation": None,
                "openapi_operation": key,
                "endpoint_id": endpoint_id,
                "parameter_aliases": {},
            })
            manual_confirmations.append({
                "rule_id": f"OPENAPI-ONLY-{endpoint_id}",
                "reasons": ["OpenAPI operation has no design rule or approved exclusion"],
                "evidence": next(
                    (
                        endpoint.get("evidence")
                        for endpoint in endpoints
                        if str(endpoint.get("id")) == endpoint_id and isinstance(endpoint.get("evidence"), dict)
                    ),
                    {
                        "source_kind": "openapi", "file": "openapi", "symbol": key, "line": 1,
                        "endpoint_scope": [key], "confidence": "high",
                    },
                ),
                "related_interface": key,
                "business_rule": f"OPENAPI-ONLY-{endpoint_id}",
                "design_quote": "No design text was found for this OpenAPI operation.",
                "current_derivation": "No design rule was found for this OpenAPI operation.",
                "ambiguity": "The operation may be undocumented, excluded, or from another contract version.",
                "impact": "Protocol-only coverage must not be counted as business coverage.",
                "options": ["add the operation to the design", "add an approved exclusion", "confirm the contract version"],
            })
    for candidate in flow_candidates:
        first_rule = next((item for item in sections if str(item.get("id")) == str(candidate.get("steps", [{}])[0].get("rule_id"))), {})
        evidence = first_rule.get("evidence", {
            "source_kind": "design", "file": str(candidate.get("source", "design")), "symbol": str(candidate.get("id")),
            "line": 1, "endpoint_scope": ["design flow"], "confidence": "medium",
        })
        if candidate.get("can_generate"):
            continue
        reasons = ["ordered design calls need explicit captures, final convergence, and cleanup before execution"]
        errors.append(f"design flow candidate {candidate.get('id')} requires confirmation")
        manual_confirmations.append({
            "rule_id": str(candidate.get("id")),
            "reasons": reasons,
            "evidence": evidence,
            "related_interface": ", ".join(str(step.get("operation")) for step in candidate.get("steps", [])),
            "business_rule": str(candidate.get("id")),
            "design_quote": str(evidence.get("quote", "")),
            "current_derivation": "Ordered calls were recognized, but no executable capture/convergence contract was stated.",
            "ambiguity": "; ".join(candidate.get("unknown", [])),
            "impact": "independent requests would not prove the documented business flow",
            "options": ["add a reviewed Test Flow with captures and cleanup", "confirm that the calls are independent"],
        })
    actionable_unresolved: list[dict[str, Any]] = []
    for item in unresolved_documents:
        if endpoints and all(
            f"{str(endpoint.get('method', '')).upper()} {endpoint.get('path')}" in excluded_keys
            for endpoint in endpoints
        ):
            continue
        if not endpoints and re.search(r"(?i)(?:no\s+api\s+operations?|no\s+endpoints?|无接口|无业务规则)", item["quote"]):
            continue
        actionable_unresolved.append(item)
    # Preserve unresolved design-side mappings as first-class audit records.
    # A parser gap must not disappear merely because no executable rule exists.
    for item in actionable_unresolved:
        unknown_id = _unknown_design_id(item)
        category = str(item.get("category") or "design_without_openapi")
        candidates = item.get("candidates", []) if isinstance(item.get("candidates"), list) else []
        mapping.append({
            "rule_id": unknown_id,
            "category": category,
            "design_operation": None,
            "openapi_operation": None,
            "endpoint_id": None,
            "parameter_aliases": {},
            "candidates": candidates,
        })
    mapping_counts = {
        category: sum(1 for item in mapping if item.get("category") == category)
        for category in (
            "exact", "parameter_alias", "semantic_candidate", "design_without_openapi",
            "openapi_without_design", "multiple_candidates",
        )
    }
    for item in actionable_unresolved:
        unknown_id = _unknown_design_id(item)
        category = str(item.get("category") or "design_without_openapi")
        candidates = item.get("candidates", []) if isinstance(item.get("candidates"), list) else []
        candidate_operations = [
            str(candidate.get("operation"))
            for candidate in candidates
            if isinstance(candidate, dict) and candidate.get("operation")
        ]
        evidence = {
            "source_kind": "design", "file": item["path"], "symbol": "unmapped design document",
            "line": 1, "endpoint_scope": ["design document"], "confidence": "low",
            "quote": item["quote"], "evidence_level": "unknown",
        }
        manual_confirmations.append({
            "rule_id": unknown_id,
            "reasons": [
                "multiple semantic OpenAPI candidates cannot be selected uniquely"
                if category == "multiple_candidates"
                else "no HTTP operation or unique semantic OpenAPI candidate was identified"
            ],
            "evidence": evidence,
            "related_interface": ", ".join(candidate_operations) or "unknown",
            "business_rule": unknown_id,
            "design_quote": item["quote"],
            "current_derivation": (
                "Several OpenAPI operations matched the design wording; no operation was selected."
                if category == "multiple_candidates"
                else "No safe operation mapping was found; no business expectation was invented."
            ),
            "ambiguity": (
                "multiple OpenAPI operations are equally plausible: " + ", ".join(candidate_operations)
                if candidate_operations
                else "the document may describe an operation without naming a route or OpenAPI operation"
            ),
            "impact": "business rules cannot be attached to executable API cases",
            "options": (
                ["select one of the candidate OpenAPI operations", "revise the design with the exact operation"]
                if category == "multiple_candidates"
                else ["identify the interface in the design", "confirm the matching OpenAPI operation"]
            ),
        })
    understanding = [
        {
            "rule_id": rule.get("id"),
            "business_name": rule.get("title"),
            "design_source": rule.get("evidence"),
            "design_summary": rule.get("content", "")[:2000],
            "candidate_http_method": rule.get("method"),
            "candidate_url_path": rule.get("path"),
            "matched_openapi_operation": rule.get("matched_operation"),
            "preconditions": rule.get("understanding", {}).get("preconditions", []),
            "request_meaning": rule.get("understanding", {}).get("request_meaning", []),
            "success_result": rule.get("understanding", {}).get("success_results", []),
            "state_changes": rule.get("states", []) + rule.get("transitions", []),
            "business_errors": rule.get("understanding", {}).get("business_errors", []),
            "side_effects": rule.get("side_effects", []),
            "idempotency": rule.get("idempotency", []),
            "retries": rule.get("retries", []),
            "concurrency": rule.get("concurrency", []),
            "async": rule.get("understanding", {}).get("async", []),
            "consistency": rule.get("understanding", {}).get("consistency", []),
            "negative_constraints": rule.get("understanding", {}).get("negative_constraints", []),
            "candidate_assertions": rule.get("candidate_assertions", []),
            "evidence_level": rule.get("evidence_level", "unknown"),
            "derivation": rule.get("derivation", ""),
            "unknown": list(dict.fromkeys(
                [item for item in [
                    "business assertion" if not rule.get("assertions") else "",
                    "request business meaning"
                    if rule.get("scenario") != "success" and not rule.get("request_declared") else "",
                ] if item]
                + ([
                    str(reason)
                    for reason in rule.get("manual_confirmation", {}).get("reasons", [])
                    if str(reason).strip()
                ] if isinstance(rule.get("manual_confirmation"), dict) else [])
                + (["unique OpenAPI mapping"] if rule.get("mapping_category") in {"multiple_candidates", "design_without_openapi"} else [])
            )),
            "can_generate": not bool(rule.get("manual_confirmation")),
        }
        for rule in rules
    ]
    understanding.extend({
        "rule_id": _unknown_design_id(item),
        "business_name": Path(item["path"]).stem,
        "design_source": {"source_kind": "design", "file": item["path"], "symbol": "unmapped design document", "line": 1, "endpoint_scope": ["design document"], "confidence": "low", "quote": item["quote"], "evidence_level": "unknown"},
        "design_summary": item["quote"],
        "candidate_http_method": None,
        "candidate_url_path": None,
        "matched_openapi_operation": None,
        "preconditions": [], "request_meaning": [], "success_result": [], "state_changes": [],
        "business_errors": [], "side_effects": [], "idempotency": [], "retries": [], "concurrency": [],
        "async": [], "consistency": [], "negative_constraints": [], "evidence_level": "unknown",
        "candidate_assertions": [],
        "derivation": "No safe operation mapping was found; no business expectation was invented.",
        "unknown": [
            "OpenAPI operation selection" if item.get("category") == "multiple_candidates" else "HTTP operation",
            "OpenAPI mapping", "business assertions",
        ], "can_generate": False,
    } for item in actionable_unresolved)
    return {
        "version": 1,
        "source": "design",
        "documents": documents,
        "parser_diagnostics": [
            {
                "path": item.get("path"),
                "marker_sections": item.get("sections", 0),
                "semantic_rules": item.get("semantic_rules", 0),
                "parser_gap": item.get("parser_gap") is True,
                "marker_operations": item.get("marker_operations", []),
                "semantic_operations": item.get("semantic_operations", []),
                "semantic_only_operations": item.get("semantic_only_operations", []),
                "explanation": (
                    "semantic extraction found business content outside fixed markers"
                    if item.get("parser_gap") else "marker parser and semantic pass agree"
                ),
            }
            for item in documents
        ],
        "rules": rules,
        "understanding": understanding,
        "mapping": {"items": mapping, "counts": mapping_counts},
        "flows": flows,
        "flow_candidates": flow_candidates,
        "exclusions": exclusions,
        "manual_confirmations": manual_confirmations,
        "understanding_status": "blocked" if errors or manual_confirmations else "complete",
        "design_fingerprint": hashlib.sha256(
            "".join(str(item.get("sha256", "")) for item in documents).encode("utf-8")
        ).hexdigest(),
        "openapi_fingerprint": str(manifest.get("source", {}).get("sha256", "")),
        "coverage": {
            "openapi_endpoints": len(endpoints),
            "documented_endpoints": len(mapped_endpoint_ids),
            "excluded_endpoints": len(excluded_keys),
            "mapped_endpoints": len(mapped_endpoint_ids),
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
        "mapping_counts": dict(document.get("mapping", {}).get("counts", {}))
        if isinstance(document.get("mapping"), dict) else {},
        "unknown_count": sum(
            1 for item in document.get("understanding", [])
            if isinstance(item, dict) and item.get("unknown")
        ) if isinstance(document.get("understanding"), list) else 0,
        "understanding_status": document.get("understanding_status", "incomplete"),
        "design_fingerprint": document.get("design_fingerprint", ""),
        "openapi_fingerprint": document.get("openapi_fingerprint", ""),
    }


UNDERSTANDING_FIELDS = {
    "rule_id", "business_name", "design_source", "design_summary", "candidate_http_method",
    "candidate_url_path", "matched_openapi_operation", "preconditions", "request_meaning",
    "success_result", "state_changes", "business_errors", "side_effects", "idempotency",
    "retries", "concurrency", "async", "consistency", "negative_constraints",
    "candidate_assertions", "evidence_level", "derivation", "unknown", "can_generate",
}
MAPPING_CATEGORIES = {
    "exact", "parameter_alias", "semantic_candidate", "design_without_openapi",
    "openapi_without_design", "multiple_candidates",
}


def understanding_errors(document: dict[str, Any]) -> list[str]:
    """Validate the persisted design-understanding phase before generation."""

    errors: list[str] = []
    if not isinstance(document, dict):
        return ["design understanding artifact must be an object"]
    if document.get("source") != "design":
        errors.append("design understanding artifact must declare source: design")
    if document.get("understanding_status") != "complete":
        errors.append("design understanding phase is not complete")
    rules = [item for item in document.get("rules", []) if isinstance(item, dict)]
    matrix = [item for item in document.get("understanding", []) if isinstance(item, dict)]
    rules_by_id = {str(item.get("id")): item for item in rules if item.get("id")}
    matrix_by_id = {str(item.get("rule_id")): item for item in matrix if item.get("rule_id")}
    for rule_id in sorted(set(rules_by_id) - set(matrix_by_id)):
        errors.append(f"design rule {rule_id} has no understanding matrix row")
    for rule_id in sorted(set(matrix_by_id) - set(rules_by_id)):
        if not rule_id.startswith("UNKNOWN-DESIGN-"):
            errors.append(f"understanding matrix row {rule_id} has no design rule")
    for rule_id, item in matrix_by_id.items():
        missing = sorted(field for field in UNDERSTANDING_FIELDS if field not in item)
        if missing:
            errors.append(f"understanding matrix row {rule_id} is missing: {', '.join(missing)}")
        if item.get("evidence_level") not in {"explicit", "derived", "unknown"}:
            errors.append(f"understanding matrix row {rule_id} has invalid evidence level")
        if item.get("can_generate") is False and not item.get("unknown"):
            errors.append(f"non-executable understanding row {rule_id} has no unknowns")
    mapping = document.get("mapping") if isinstance(document.get("mapping"), dict) else {}
    items = [item for item in mapping.get("items", []) if isinstance(item, dict)]
    counts = mapping.get("counts") if isinstance(mapping.get("counts"), dict) else {}
    actual_counts = {category: sum(1 for item in items if item.get("category") == category) for category in MAPPING_CATEGORIES}
    for category in MAPPING_CATEGORIES:
        try:
            declared_count = int(counts.get(category, -1))
        except (TypeError, ValueError):
            declared_count = -1
        if declared_count != actual_counts[category]:
            errors.append(f"mapping count for {category} is inconsistent with mapping items")
    for item in items:
        if item.get("category") not in MAPPING_CATEGORIES:
            errors.append(f"mapping item has invalid category: {item.get('category')}")
    confirmations = [item for item in document.get("manual_confirmations", []) if isinstance(item, dict)]
    confirmation_ids = {str(item.get("rule_id")) for item in confirmations}
    for item in confirmations:
        for field in ("rule_id", "reasons", "evidence", "related_interface", "business_rule", "design_quote", "current_derivation", "ambiguity", "impact", "options"):
            if not item.get(field):
                errors.append(f"manual confirmation {item.get('rule_id', '<unknown>')} is missing {field}")
    for candidate in document.get("flow_candidates", []) if isinstance(document.get("flow_candidates"), list) else []:
        if isinstance(candidate, dict) and candidate.get("can_generate") is False and str(candidate.get("id")) not in confirmation_ids:
            errors.append(f"flow candidate {candidate.get('id')} has no manual confirmation")
    for rule_id, rule in rules_by_id.items():
        if rule.get("manual_confirmation") and rule_id not in confirmation_ids:
            errors.append(f"design rule {rule_id} has no top-level manual confirmation")
    for rule_id, item in matrix_by_id.items():
        if item.get("can_generate") is False and rule_id not in confirmation_ids:
            errors.append(f"non-executable understanding row {rule_id} has no manual confirmation")
    for flow in document.get("flows", []) if isinstance(document.get("flows"), list) else []:
        if not isinstance(flow, dict):
            continue
        for field in ("business_assertions", "data_transfer", "final_status"):
            if field not in flow:
                errors.append(f"design flow {flow.get('id', '<unknown>')} is missing {field}")
        if not str(flow.get("final_status", "")).strip():
            errors.append(f"design flow {flow.get('id', '<unknown>')} has no final status")
        if not str(flow.get("cleanup", "")).strip():
            errors.append(f"design flow {flow.get('id', '<unknown>')} has no cleanup action")
    return list(dict.fromkeys(errors))


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
                endpoint = next(
                    (
                        item for item in endpoint_doc.get("endpoints", [])
                        if isinstance(item, dict) and str(item.get("id")) == endpoint_id
                    ),
                    {},
                )
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
                case["design_evidence_level"] = str(selected.get("evidence_level", "unknown"))
                case["business_assertions"] = list(selected.get("candidate_assertions", selected.get("assertions", [])))
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
                    "endpoint_id": endpoint_id,
                    "openapi_operation": f"{endpoint.get('method')} {endpoint.get('path')}",
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
                    "evidence_level": str(selected.get("evidence_level", "unknown")),
                    "evidence_quote": str(selected.get("evidence", {}).get("quote", selected.get("content", "")[:2000])),
                    "derivation": str(selected.get("derivation", "")),
                    "business_assertions": list(selected.get("candidate_assertions", selected.get("assertions", []))),
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
            rendered_step = {
                "operation": step.get("operation"),
                "case_id": case_id,
                "business_assertions": list(step.get("business_assertions", [])),
            }
            for field in ("capture", "capture_paths", "uses", "assert_absent"):
                if field in step:
                    rendered_step[field] = copy.deepcopy(step[field])
            steps.append(rendered_step)
        rendered_flow: dict[str, Any] = {
            "id": flow.get("id"),
            "source": "design",
            "design_rule_ids": [str(step.get("rule_id")) for step in flow.get("steps", [])],
            "steps": steps,
            "business_assertions": list(flow.get("business_assertions", [])),
            "data_transfer": list(flow.get("data_transfer", [])),
            "final_status": str(flow.get("final_status", "")),
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
