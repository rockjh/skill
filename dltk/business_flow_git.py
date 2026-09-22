"""Git metadata and read-only source snapshots for business-flow analysis."""

from __future__ import annotations

import subprocess
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .business_flow_models import GitInfo


def _run(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    return result.stdout.strip()


def git_info(root: Path, target: str | None = None) -> GitInfo:
    head = _run(root, "rev-parse", "HEAD")
    branch = _run(root, "branch", "--show-current") or "(detached)"
    dirty = bool(_run(root, "status", "--porcelain"))
    target_hash = _run(root, "rev-parse", target or "HEAD")
    return GitInfo(
        branch=branch,
        head=head,
        target=target_hash,
        dirty=dirty,
        includes_uncommitted=dirty and target_hash == head,
        comparison="current" if target_hash == head else "snapshot",
    )


@contextmanager
def source_view(root: Path, target: str | None = None) -> Iterator[tuple[Path, GitInfo]]:
    """Yield the worktree for HEAD, or an extracted read-only target snapshot."""

    info = git_info(root, target)
    if info.target == info.head:
        yield root, info
        return
    with tempfile.TemporaryDirectory(prefix="dltk-business-flow-") as temporary:
        archive = Path(temporary) / "source.zip"
        subprocess.run(["git", "archive", "--format=zip", "-o", str(archive), info.target], cwd=root, check=True)
        with zipfile.ZipFile(archive) as zipped:
            zipped.extractall(temporary)
        yield Path(temporary), info


def changed_paths(root: Path, old: str, new: str) -> tuple[list[str], str | None]:
    try:
        old_hash = _run(root, "rev-parse", old)
        new_hash = _run(root, "rev-parse", new)
        output = _run(root, "diff", "--name-status", old_hash, new_hash)
    except (OSError, subprocess.CalledProcessError) as exc:
        return [], str(exc)
    return [line for line in output.splitlines() if line.strip()], None


def working_tree_paths(root: Path) -> list[str]:
    """Return porcelain paths so uncommitted business edits affect incremental review."""

    try:
        return [line for line in _run(root, "status", "--porcelain").splitlines() if line.strip()]
    except (OSError, subprocess.CalledProcessError):
        return []
