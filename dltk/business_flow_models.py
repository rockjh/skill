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
    capture_boundary: str = "代码中未确认"
    propagation: str = "代码中未确认"
    consequence: str = "代码中未确认"
    phase: str = "sync"
    recovery: str = "代码中未确认"


@dataclass(slots=True)
class BehaviorEvidence:
    kind: str
    statement: str
    file: str
    line: int


@dataclass(slots=True)
class FlowStep:
    kind: str
    text: str
    source: str
    participant: str = "当前系统"


@dataclass(slots=True)
class EntryReview:
    review_id: str
    trigger: str
    purpose: str
    input: str
    outcome: str
    failure: str
    steps: list[FlowStep] = field(default_factory=list)
    status: str = "draft"
    confirmed_by: str = ""


@dataclass(slots=True)
class FunctionInfo:
    name: str
    start: int
    end: int
    body: str
    calls: list[str] = field(default_factory=list)
    errors: list[ErrorEvidence] = field(default_factory=list)
    owner: str = ""
    qualified_calls: list[tuple[str, str]] = field(default_factory=list)


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
    review: EntryReview | None = None
    binding_confirmed: bool = True
    handler_confirmed: bool = True

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
    discovered_entry_count: int = -1
    discovered_binding_count: int = -1
    discovered_handler_count: int = -1

    @property
    def candidate_entry_count(self) -> int:
        return len(self.entries) if self.discovered_entry_count < 0 else self.discovered_entry_count

    @property
    def confirmed_binding_count(self) -> int:
        return (
            sum(entry.binding_confirmed and entry.identifier != "代码中未确认" for entry in self.entries)
            if self.discovered_binding_count < 0 else self.discovered_binding_count
        )

    @property
    def confirmed_handler_count(self) -> int:
        return (
            len({entry.handler for entry in self.entries if entry.handler_confirmed and entry.handler != "代码中未确认"})
            if self.discovered_handler_count < 0 else self.discovered_handler_count
        )
