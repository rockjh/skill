#!/usr/bin/env python3
"""Canonical QA layout paths and the one-time legacy layout migration."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.dont_write_bytecode = True


DATA = Path("data")
BRUNO = DATA / "bruno"
CONTRACTS = DATA / "contracts"
CONSTRAINTS = DATA / "constraints"
EXECUTION = Path("execution")
SCRIPTS = Path("scripts")
RESULTS = Path("results")
GLOBAL_RESULTS = RESULTS / "global"
MODULE_RESULTS = RESULTS / "modules"
GLOBAL_EVIDENCE = GLOBAL_RESULTS / "evidence"
MODULE_EVIDENCE = MODULE_RESULTS / "evidence"
LOGS = RESULTS / "logs"


def canonicalize_legacy_path(qa_root: Path, path: Path) -> Path:
    """Redirect an explicit legacy QA path to its canonical location."""

    qa_root = qa_root.resolve()
    resolved = path.resolve()
    for source, target in (
        (qa_root / "evidence" / "modules", qa_root / MODULE_EVIDENCE),
        (qa_root / "evidence" / "global", qa_root / GLOBAL_EVIDENCE),
        (qa_root / "logs", qa_root / LOGS),
        (qa_root / "bruno", qa_root / BRUNO),
        (qa_root / "contracts", qa_root / CONTRACTS),
        (qa_root / "constraints", qa_root / CONSTRAINTS),
    ):
        try:
            return target / resolved.relative_to(source)
        except ValueError:
            continue
    return resolved


def _move_if_target_missing(source: Path, target: Path, changed: list[Path]) -> None:
    if not source.exists() or target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    changed.append(target)


def _recorded_path_replacements(qa_root: Path) -> tuple[tuple[str, str], ...]:
    relative = (
        ("qa/evidence/modules", "qa/results/modules/evidence"),
        ("qa/evidence/global", "qa/results/global/evidence"),
        ("evidence/modules", "results/modules/evidence"),
        ("evidence/global", "results/global/evidence"),
        ("qa/logs", "qa/results/logs"),
        ("qa/bruno", "qa/data/bruno"),
        ("qa/contracts", "qa/data/contracts"),
        ("qa/constraints", "qa/data/constraints"),
    )
    absolute = tuple(
        (str(qa_root / source), str(qa_root / target))
        for source, target in (
            (Path("evidence") / "modules", MODULE_EVIDENCE),
            (Path("evidence") / "global", GLOBAL_EVIDENCE),
            (Path("logs"), LOGS),
            (Path("bruno"), BRUNO),
            (Path("contracts"), CONTRACTS),
            (Path("constraints"), CONSTRAINTS),
        )
    )
    slash_absolute = tuple((old.replace("\\", "/"), new.replace("\\", "/")) for old, new in absolute)
    backslash_relative = tuple((old.replace("/", "\\"), new.replace("/", "\\")) for old, new in relative)
    return (*absolute, *slash_absolute, *relative, *backslash_relative)


def _rewrite_recorded_paths(qa_root: Path, changed: list[Path]) -> bool:
    contracts = qa_root / CONTRACTS
    candidates = [
        contracts / name
        for name in ("index.yaml", "generation-state.yaml", "qa-lock.yaml", "version-lock.yaml")
    ]
    candidates.extend(contracts.glob("modules/*/module-lock.yaml"))
    candidates.extend(contracts.glob("modules/*/observed-rules.yaml"))
    candidates.extend((qa_root / CONSTRAINTS).glob("*observed-rules.yaml"))
    candidates.extend((qa_root / RESULTS).rglob("*-result.json"))
    candidates.extend((qa_root / RESULTS).rglob("*-evidence.json"))
    replacements = _recorded_path_replacements(qa_root)
    state_changed = False
    for path in dict.fromkeys(candidates):
        if not path.is_file():
            continue
        current = path.read_text(encoding="utf-8", errors="strict")
        updated = current
        for old, new in replacements:
            updated = updated.replace(old, new)
        if updated == current:
            continue
        path.write_text(updated, encoding="utf-8")
        changed.append(path)
        state_changed = state_changed or path.name == "generation-state.yaml"
    return state_changed


def migrate_legacy_layout(qa_root: Path) -> list[Path]:
    """Move legacy QA directories once; canonical paths win on conflicts."""

    qa_root = qa_root.resolve()
    changed: list[Path] = []
    for source, target in (
        (qa_root / "bruno", qa_root / BRUNO),
        (qa_root / "contracts", qa_root / CONTRACTS),
        (qa_root / "constraints", qa_root / CONSTRAINTS),
        (qa_root / "evidence" / "global", qa_root / GLOBAL_EVIDENCE),
        (qa_root / "evidence" / "modules", qa_root / MODULE_EVIDENCE),
        (qa_root / "logs", qa_root / LOGS),
    ):
        _move_if_target_missing(source, target, changed)
    legacy_evidence = qa_root / "evidence"
    if legacy_evidence.is_dir() and not any(legacy_evidence.iterdir()):
        legacy_evidence.rmdir()
        changed.append(legacy_evidence)
    state_changed = _rewrite_recorded_paths(qa_root, changed)
    if state_changed and (qa_root / CONTRACTS / "qa-lock.yaml").is_file():
        from qa_lock import write

        write(qa_root / CONTRACTS)
        changed.append(qa_root / CONTRACTS / "qa-lock.yaml")
    return list(dict.fromkeys(changed))
