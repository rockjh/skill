#!/usr/bin/env python3
"""Build reviewable Java/Spring logic candidates for API test planning.

This is an optional Java/Spring adapter. The generic workflow uses
``analyze_source_logic.py`` for cross-language candidates. It finds evidence
for review; it does not claim that regex matching proves branch coverage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


MAPPING_RE = re.compile(r"@(Get|Post|Put|Patch|Delete|Request)Mapping\s*(?:\(([^)]*)\))?")
CLASS_RE = re.compile(r"\bclass\s+(\w+)")
METHOD_RE = re.compile(r"\b(?:public|protected|private)\s+(?:static\s+)?[\w<>, ?\[\].]+\s+(\w+)\s*\(")
EXCEPTION_RE = re.compile(r"(?:throw\s+new\s+([A-Za-z_][\w.]*)|@ExceptionHandler\s*\(([^)]*)\))")
VALIDATION_RE = re.compile(r"@(NotNull|NotBlank|NotEmpty|Size|Length|Pattern|Email|Min|Max|Positive|Negative)\b")
ERROR_RE = re.compile(r"(?:\.)(error|fail|failure)\s*\(")
BRANCH_RE = re.compile(r"\b(if|else\s+if|switch|case|catch|\?)[\s(]")


def clean_evidence(line: str) -> str:
    return " ".join(line.strip().split())[:300]


def add_candidate(items: list[dict[str, Any]], kind: str, path: Path, line_no: int, symbol: str, line: str) -> None:
    evidence = clean_evidence(line)
    stable_key = f"{kind}|{path.stem}|{symbol}|{evidence}"
    stable_id = "_".join(
        part for part in (kind.upper(), path.stem.upper(), symbol.replace(".", "_").upper()) if part
    ) + "_" + hashlib.sha1(stable_key.encode("utf-8")).hexdigest()[:10]
    items.append(
        {
            "id": stable_id,
            "kind": kind,
            "file": str(path),
            "line": line_no,
            "symbol": symbol,
            "evidence": evidence,
            "needs_case": True,
        }
    )


def scan(roots: list[Path]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    paths = {
        path
        for root in roots
        for path in root.rglob("*.java")
        if not any(part in {"target", "build", ".git", "node_modules"} for part in path.parts)
    }
    for path in sorted(paths):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        current_class = path.stem
        current_method = ""
        for line_no, line in enumerate(lines, start=1):
            class_match = CLASS_RE.search(line)
            if class_match:
                current_class = class_match.group(1)
            method_match = METHOD_RE.search(line)
            if method_match:
                current_method = method_match.group(1)
            symbol = f"{current_class}.{current_method}" if current_method else current_class
            if MAPPING_RE.search(line):
                add_candidate(candidates, "normal_entrypoint", path, line_no, symbol, line)
            if EXCEPTION_RE.search(line):
                add_candidate(candidates, "exception", path, line_no, symbol, line)
            if VALIDATION_RE.search(line):
                add_candidate(candidates, "validation", path, line_no, symbol, line)
            if ERROR_RE.search(line):
                add_candidate(candidates, "error_builder", path, line_no, symbol, line)
            observable = re.search(
                r"\b(?:throw|return|catch|permission|forbidden|unauthor|duplicate|exists|not\s*found|error|fail|status|code)\b",
                line,
                re.IGNORECASE,
            )
            if BRANCH_RE.search(line) and observable:
                add_candidate(candidates, "branch", path, line_no, symbol, line)
    return {
        "version": 1,
        "source": {"roots": [str(root) for root in roots], "language": "java"},
        "candidates": candidates,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_roots", type=Path, nargs="+", help="focused Java source roots")
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()
    missing = [str(root) for root in args.source_roots if not root.is_dir()]
    if missing:
        parser.error(f"source root(s) do not exist: {', '.join(missing)}")
    result = scan(args.source_roots)
    args.output.write_text(json.dumps(result, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(result['candidates'])} logic candidates to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
