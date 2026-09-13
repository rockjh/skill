#!/usr/bin/env python3
"""Find high-confidence credentials accidentally committed with API artifacts."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


TEXT_SUFFIXES = {
    ".bru",
    ".json",
    ".yaml",
    ".yml",
    ".txt",
    ".log",
    ".md",
    ".properties",
    ".xml",
}
PATTERNS = (
    ("JWT", re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}(?![A-Za-z0-9_-])")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("URL credentials", re.compile(r"https?://[^\s/@:]+:[^\s/@]+@", re.IGNORECASE)),
    ("literal bearer token", re.compile(r"(?i)\b(?:authorization|token)\s*[:=]\s*(?:bearer\s+)?eyJ[A-Za-z0-9_-]{20,}")),
)


def iter_files(root: Path):
    if root.is_file():
        yield root
        return
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES and ".git" not in path.parts:
            yield path


def scan(paths: list[Path]) -> list[str]:
    findings: list[str] = []
    seen: set[Path] = set()
    for root in paths:
        for path in iter_files(root):
            path = path.resolve()
            if path in seen:
                continue
            seen.add(path)
            try:
                lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
            except (OSError, UnicodeDecodeError) as exc:
                findings.append(f"{path}: cannot read artifact: {exc}")
                continue
            for line_no, line in enumerate(lines, 1):
                for label, pattern in PATTERNS:
                    if pattern.search(line):
                        findings.append(f"{path}:{line_no}: {label}")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="artifact files or directories to scan")
    args = parser.parse_args()
    missing = [str(path) for path in args.paths if not path.exists()]
    if missing:
        parser.error("path(s) do not exist: " + ", ".join(missing))
    findings = scan(args.paths)
    if findings:
        print("artifact safety check failed")
        for finding in findings:
            print(f"ERROR: {finding}")
        return 1
    print("artifact safety check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
