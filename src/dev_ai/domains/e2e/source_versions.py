"""Per-scenario source snapshot validation."""

from pathlib import Path
from typing import Any

from ...core.schema import E2E_GATE_SCHEMA_VERSION
from .contracts import contract_errors
from .discovery import SHA_RE, _git, _resolve


RULES_VERSION = int(E2E_GATE_SCHEMA_VERSION)


def source_version_results(project_root: Path, selected: str | None = None) -> tuple[list[str], list[dict[str, Any]]]:
    """只读比较每个场景的源码基线、当前提交和工作区状态。"""

    errors, scenarios, discovery = contract_errors(project_root, selected)
    repositories = {
        str(item.get("id")): item
        for item in discovery.get("inventory", {}).get("repositories", [])
        if isinstance(item, dict)
    }
    results: list[dict[str, Any]] = []
    for directory, definition in scenarios:
        for source in definition.get("source", []):
            if not isinstance(source, dict) or source.get("repo") not in repositories:
                continue
            repo_id = str(source["repo"])
            repo_root = _resolve(project_root, str(repositories[repo_id].get("root", "")))
            recorded = str(source.get("commit", ""))
            head_call = _git(repo_root, "rev-parse", "HEAD")
            current = head_call.stdout.strip() if head_call.returncode == 0 else None
            status_call = _git(repo_root, "status", "--porcelain", "--untracked-files=all")
            dirty_files = [line[3:] for line in status_call.stdout.splitlines() if len(line) > 3] if status_call.returncode == 0 else []
            diff_call = _git(repo_root, "diff", "--name-only", f"{recorded}..{current}") if current else None
            changed = diff_call.stdout.splitlines() if diff_call and diff_call.returncode == 0 else []
            unresolved = [
                anchor for anchor in source.get("anchors", [])
                if _git(repo_root, "grep", "-q", "--fixed-strings", str(anchor), current or "HEAD", "--", ".").returncode != 0
            ]
            exists = SHA_RE.fullmatch(recorded) and _git(repo_root, "cat-file", "-e", f"{recorded}^{{commit}}").returncode == 0
            git_failed = status_call.returncode != 0 or (diff_call is not None and diff_call.returncode != 0)
            if not exists or unresolved or current is None or git_failed:
                outcome = "full_rediscovery_required"
                reason = "基线提交、当前提交、源码锚点或 Git 状态无法解析"
            elif dirty_files:
                outcome = "dirty_review_required"
                reason = "源码工作区存在未提交改动，不能声明同步"
            elif recorded == current:
                outcome = "unchanged"
                reason = "当前提交等于已审查基线且工作区干净"
            else:
                outcome = "affected"
                reason = "存在尚未完成人工影响审查的提交变更"
            results.append({
                "scenario": directory.name,
                "repository": repo_id,
                "recorded_commit": recorded,
                "current_commit": current,
                "dirty": bool(dirty_files),
                "dirty_files": dirty_files,
                "changed_files": changed,
                "unresolved_anchors": unresolved,
                "outcome": outcome,
                "reason": reason,
            })
    return errors, results

__all__ = ['source_version_results']
