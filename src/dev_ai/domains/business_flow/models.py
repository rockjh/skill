"""Small value objects shared by business-flow discovery and rendering."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class GitInfo:
    branch: str
    head: str
    target: str
    dirty: bool
    includes_uncommitted: bool
    comparison: str = "current"


@dataclass(slots=True)
class ErrorEvidence:
    code: str
    condition: str
    file: str
    line: int


@dataclass(slots=True)
class BehaviorEvidence:
    kind: str
    statement: str
    file: str
    line: int


@dataclass(slots=True)
class FunctionInfo:
    name: str
    start: int
    end: int
    body: str
    calls: list[str] = field(default_factory=list)
    errors: list[ErrorEvidence] = field(default_factory=list)


@dataclass(slots=True)
class EntryPoint:
    entry_id: str
    kind: str
    identifier: str
    handler: str
    file: str
    line: int
    module: str
    source: str
    module_rationale: str = "代码中未确认"
    caller: str = "代码中未确认"
    input_summary: str = "代码中未确认"
    functions: list[str] = field(default_factory=list)
    errors: list[ErrorEvidence] = field(default_factory=list)
    behaviors: list[BehaviorEvidence] = field(default_factory=list)
    has_loop: bool = False
    has_external_call: bool = False
    has_persistence: bool = False
    has_async: bool = False

    def error_codes(self) -> list[str]:
        return list(dict.fromkeys(error.code for error in self.errors))


@dataclass(slots=True)
class ScanResult:
    root: Path
    git: GitInfo
    languages: list[str]
    frameworks: list[str]
    entries: list[EntryPoint]
    files: list[str]
    unresolved: list[str]
    source_fingerprint: str = ""
    source_lines: dict[str, int] = field(default_factory=dict)
    exclusions: list[str] = field(default_factory=list)
