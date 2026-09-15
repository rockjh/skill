#!/usr/bin/env python3
"""Find API-reachable Java/Spring logic through a lightweight call graph."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

CLASS_RE = re.compile(r"\b(?:class|interface|enum)\s+(\w+)")
METHOD_RE = re.compile(
    r"(?P<annotations>(?:\s*@[^\n]+\n)*)\s*(?:public|protected|private)\s+"
    r"(?:static\s+)?[\w<>, ?\[\].]+\s+(?P<name>\w+)\s*\([^)]*\)\s*\{",
    re.MULTILINE,
)
MAPPING_RE = re.compile(r"@(Get|Post|Put|Patch|Delete|Request)Mapping\b", re.IGNORECASE)
FIELD_RE = re.compile(r"\b(?:private|protected|public)\s+(?:final\s+)?([A-Z]\w*)\s+(\w+)\s*[;=]")
QUALIFIED_CALL_RE = re.compile(r"\b([a-zA-Z_]\w*)\.(\w+)\s*\(")
PLAIN_CALL_RE = re.compile(r"(?<![.@\w])([a-zA-Z_]\w*)\s*\(")
EXCEPTION_RE = re.compile(
    r"\bthrow\s+new\s+((?!MnoRcpApplicationException\b)[A-Za-z_]\w*(?:Application|Business|Biz|Domain)?Exception)\b"
)
TRAFFIC_CODE_RE = re.compile(r"\bMnoTrafficErrorCodeEnum\.([A-Z][A-Z0-9_]*)\b")
NUMERIC_CODE_RE = re.compile(r"(?<!\d)([1-9]\d{4,8})(?!\d)")
VALIDATION_RE = re.compile(r"@(NotNull|NotBlank|NotEmpty|Size|Length|Pattern|Email|Min|Max|Positive|Negative)\b")
AUTHORIZATION_RE = re.compile(r"(?:PreAuthorize|RequiresPermissions|Secured|hasRole|hasAuthority)", re.IGNORECASE)
HEADER_RE = re.compile(r"(?:RequestHeader|getHeader)\s*\([^\n]*?[\"']([A-Za-z][A-Za-z0-9-]*)[\"']", re.IGNORECASE)
OBSERVABLE_BRANCH_RE = re.compile(
    r"\b(?:if|switch|case|catch)\b[^\n]*(?:throw|error|fail|forbidden|unauthor|duplicate|exists|notFound|status|code)",
    re.IGNORECASE,
)
SKIP_PARTS = {".git", "qa", "target", "build", "node_modules", "vendor", ".venv", "venv", "test", "tests"}
JAVA_KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "throw", "new", "super", "this", "synchronized"}


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", lambda match: "\n" * match.group(0).count("\n"), text, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", text)


def _matching_brace(text: str, opening: int) -> int:
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(opening, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return len(text)


def _skip(path: Path) -> bool:
    lowered = {part.lower() for part in path.parts}
    return bool(lowered & SKIP_PARTS) or path.stem.endswith(("Test", "Tests", "ArchitectureTest", "ArchTest"))


def _candidate_id(kind: str, symbol: str, line: int, evidence: str) -> str:
    digest = hashlib.sha1(f"{kind}|{symbol}|{line}|{evidence}".encode("utf-8")).hexdigest()[:12]
    return f"{kind.upper()}_{digest}"


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def scan(roots: list[Path]) -> dict[str, Any]:
    files: list[tuple[Path, str, str]] = []
    error_codes: dict[str, str] = {}
    for root in roots:
        for path in sorted(root.rglob("*.java")):
            if _skip(path) or not path.is_file():
                continue
            try:
                raw = path.read_text(encoding="utf-8", errors="strict")
            except (OSError, UnicodeDecodeError):
                continue
            text = _strip_comments(raw)
            class_match = CLASS_RE.search(text)
            if not class_match:
                continue
            class_name = class_match.group(1)
            if class_name == "MnoTrafficErrorCodeEnum":
                for name, value in re.findall(r"\b([A-Z][A-Z0-9_]*)\s*\(\s*([1-9]\d{4,8})\b", text):
                    error_codes[name] = value
                continue
            files.append((path, class_name, text))

    methods: dict[str, dict[str, Any]] = {}
    by_name: dict[str, list[str]] = {}
    receiver_types: dict[str, dict[str, str]] = {}
    entrypoints: dict[str, str] = {}
    for path, class_name, text in files:
        receiver_types[class_name] = {name: type_name for type_name, name in FIELD_RE.findall(text)}
        for match in METHOD_RE.finditer(text):
            opening = text.find("{", match.start())
            closing = _matching_brace(text, opening)
            name = match.group("name")
            symbol = f"{class_name}.{name}"
            methods[symbol] = {
                "path": path,
                "class": class_name,
                "name": name,
                "annotations": match.group("annotations") or "",
                "body": text[opening + 1:closing],
                "body_offset": opening + 1,
                "text": text,
            }
            by_name.setdefault(name, []).append(symbol)
            if MAPPING_RE.search(match.group("annotations") or ""):
                entrypoints[symbol] = name

    graph: dict[str, set[str]] = {symbol: set() for symbol in methods}
    for symbol, method in methods.items():
        receivers = receiver_types.get(method["class"], {})
        for receiver, called in QUALIFIED_CALL_RE.findall(method["body"]):
            owner = receivers.get(receiver) or (method["class"] if receiver == "this" else "")
            target = f"{owner}.{called}"
            if target in methods:
                graph[symbol].add(target)
            elif len(by_name.get(called, [])) == 1:
                graph[symbol].add(by_name[called][0])
        for called in PLAIN_CALL_RE.findall(method["body"]):
            if called in JAVA_KEYWORDS:
                continue
            local = f"{method['class']}.{called}"
            if local in methods:
                graph[symbol].add(local)
            elif len(by_name.get(called, [])) == 1:
                graph[symbol].add(by_name[called][0])

    reachable_from: dict[str, set[str]] = {symbol: set() for symbol in methods}
    for entrypoint, operation_id in entrypoints.items():
        pending = [entrypoint]
        seen: set[str] = set()
        while pending:
            symbol = pending.pop()
            if symbol in seen:
                continue
            seen.add(symbol)
            reachable_from[symbol].add(operation_id)
            pending.extend(graph.get(symbol, set()) - seen)

    candidates: list[dict[str, Any]] = []
    seen_candidates: set[tuple[str, str, int, str]] = set()
    for symbol, method in methods.items():
        operation_ids = sorted(reachable_from.get(symbol, set()))
        if not operation_ids:
            continue
        blocks = [("normal_entrypoint", method["annotations"])] if symbol in entrypoints else []
        blocks.append(("body", method["body"]))
        for block_kind, block in blocks:
            base_offset = method["text"].find(block) if block_kind == "normal_entrypoint" else method["body_offset"]
            for line_offset, line in enumerate(block.splitlines()):
                evidence = " ".join(line.strip().split())[:300]
                if not evidence:
                    continue
                kind: str | None = None
                coverage_required = False
                if block_kind == "normal_entrypoint" and MAPPING_RE.search(line):
                    kind = "normal_entrypoint"
                elif HEADER_RE.search(line):
                    kind, coverage_required = "required_header", True
                elif AUTHORIZATION_RE.search(line):
                    kind, coverage_required = "authorization", True
                elif EXCEPTION_RE.search(line) or TRAFFIC_CODE_RE.search(line):
                    kind, coverage_required = "business_exception", True
                elif VALIDATION_RE.search(line):
                    kind, coverage_required = "validation", True
                elif OBSERVABLE_BRANCH_RE.search(line):
                    kind, coverage_required = "observable_branch", True
                if not kind:
                    continue
                line_no = _line_number(method["text"], base_offset) + line_offset
                key = (kind, symbol, line_no, evidence)
                if key in seen_candidates:
                    continue
                seen_candidates.add(key)
                candidate: dict[str, Any] = {
                    "id": _candidate_id(kind, symbol, line_no, evidence),
                    "kind": kind,
                    "file": str(method["path"]),
                    "line": line_no,
                    "language": "java",
                    "symbol": symbol,
                    "evidence": evidence,
                    "needs_case": coverage_required,
                    "coverage_required": coverage_required,
                    "endpoint_operation_ids": operation_ids,
                }
                header = HEADER_RE.search(line)
                if header:
                    candidate["required_header"] = header.group(1)
                codes = [error_codes.get(name, name) for name in TRAFFIC_CODE_RE.findall(line)]
                codes.extend(NUMERIC_CODE_RE.findall(line))
                if codes:
                    candidate["expected_business_codes"] = list(dict.fromkeys(codes))
                exception = EXCEPTION_RE.search(line)
                if exception:
                    candidate["exception_type"] = exception.group(1)
                candidates.append(candidate)
    return {
        "version": 1,
        "source": {"roots": [str(root) for root in roots], "language": "java"},
        "candidates": candidates,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_roots", type=Path, nargs="+")
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()
    missing = [str(root) for root in args.source_roots if not root.is_dir()]
    if missing:
        parser.error(f"source root(s) do not exist: {', '.join(missing)}")
    result = scan(args.source_roots)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(result['candidates'])} API-reachable Java logic candidates to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
