#!/usr/bin/env python3
"""Collect reviewable HTTP logic candidates across common source languages.

This is deliberately a candidate scanner, not a branch-coverage engine. It
lets the contract workflow continue for non-Java repositories and for projects
that have no source access; adapters can add deeper framework-specific logic.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


LANGUAGE_BY_SUFFIX = {
    ".java": "java",
    ".go": "go",
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".cs": "csharp",
    ".fs": "fsharp",
    ".php": "php",
    ".rs": "rust",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".rb": "ruby",
    ".scala": "scala",
    ".swift": "swift",
    ".ex": "elixir",
    ".exs": "elixir",
    ".dart": "dart",
}

ENTRYPOINT_RE = re.compile(
    r"@(Get|Post|Put|Patch|Delete|Request)Mapping\b|"
    r"@(?:app|router)\.(get|post|put|patch|delete)\b|"
    r"\.(?:get|post|put|patch|delete)\s*\(|"
    r"\b(?:HandleFunc|HttpGet|HttpPost|HttpPut|HttpPatch|HttpDelete)\b",
    re.IGNORECASE,
)
BRANCH_RE = re.compile(
    r"\b(?:if|else\s+if|switch|case|catch|throw|return|raise|abort|forbidden|unauthor|duplicate|exists|not\s*found|error|fail|status|code)\b",
    re.IGNORECASE,
)


def candidate_id(kind: str, path: Path, line_no: int, evidence: str) -> str:
    key = f"{kind}|{path}|{line_no}|{evidence}"
    return f"{kind.upper()}_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]}"


def scan(roots: list[Path]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    languages: set[str] = set()
    for root in roots:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in LANGUAGE_BY_SUFFIX:
                continue
            if any(part in {".git", "target", "build", "node_modules", "vendor", ".venv", "venv"} for part in path.parts):
                continue
            language = LANGUAGE_BY_SUFFIX[path.suffix.lower()]
            languages.add(language)
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for line_no, line in enumerate(lines, start=1):
                evidence = " ".join(line.strip().split())[:300]
                if ENTRYPOINT_RE.search(line):
                    kind = "normal_entrypoint"
                elif BRANCH_RE.search(line):
                    kind = "observable_branch"
                else:
                    continue
                candidates.append(
                    {
                        "id": candidate_id(kind, path, line_no, evidence),
                        "kind": kind,
                        "file": str(path),
                        "line": line_no,
                        "language": language,
                        "evidence": evidence,
                        "needs_case": True,
                    }
                )
    return {
        "version": 1,
        "source": {"roots": [str(root) for root in roots], "languages": sorted(languages)},
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
    print(f"wrote {len(result['candidates'])} cross-language logic candidates to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
