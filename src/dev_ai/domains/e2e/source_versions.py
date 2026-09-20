"""Per-scenario and generation-input version validation."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from ... import __version__
from ...core.schema import E2E_GATE_SCHEMA_VERSION
from .discovery import SHA_RE, _git, _resolve


RULES_VERSION = int(E2E_GATE_SCHEMA_VERSION)


def _sha256(path: Path) -> str | None:
    """Hash one readable file without interpreting its contents."""

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def input_summary(document: dict[str, Any]) -> dict[str, Any]:
    """Build a stable summary for parsed design or protocol documents."""

    documents = document.get("documents", []) if isinstance(document.get("documents"), list) else []
    normalized = [
        {key: item.get(key) for key in ("path", "sha256", "version") if key in item}
        for item in documents if isinstance(item, dict)
    ]
    count_key = "rule_count" if document.get("source") == "design" else "operation_count"
    values_key = "rules" if count_key == "rule_count" else "operations"
    return {
        "documents": normalized,
        "sha256": hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
        count_key: len(document.get(values_key, [])) if isinstance(document.get(values_key), list) else 0,
    }


def source_snapshot(project_root: Path) -> list[dict[str, str]]:
    """Capture repository commits as support-only provenance."""

    workspace = project_root / "discovery" / "workspace.yaml"
    try:
        document = yaml.safe_load(workspace.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return []
    repositories = document.get("inventory", {}).get("repositories", []) if isinstance(document, dict) else []
    return sorted(
        (
            {"repo": str(item.get("id")), "commit": str(item.get("commit"))}
            for item in repositories
            if isinstance(item, dict) and item.get("id") and item.get("commit")
        ),
        key=lambda item: item["repo"],
    )


def scenario_snapshot(project_root: Path) -> dict[str, str]:
    """Fingerprint scenario business data and cleanup contracts separately."""

    data_digest = hashlib.sha256()
    cleanup_digest = hashlib.sha256()
    scenarios_root = project_root / "scenarios"
    if scenarios_root.is_dir():
        for directory in sorted(path for path in scenarios_root.iterdir() if path.is_dir()):
            data = directory / "业务数据.json"
            definition = directory / "场景定义.yaml"
            if data.is_file():
                data_digest.update(directory.name.encode("utf-8"))
                data_digest.update(data.read_bytes())
            if definition.is_file():
                try:
                    parsed = yaml.safe_load(definition.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, yaml.YAMLError):
                    parsed = None
                cleanup = parsed.get("cleanup", {}) if isinstance(parsed, dict) else {}
                cleanup_digest.update(directory.name.encode("utf-8"))
                cleanup_digest.update(json.dumps(cleanup, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return {"data_sha256": data_digest.hexdigest(), "cleanup_sha256": cleanup_digest.hexdigest()}


def support_snapshot(project_root: Path, value_resolution: dict[str, Any] | None = None) -> dict[str, str | None]:
    """Fingerprint source-derived execution support without creating expectations."""

    workspace = project_root / "discovery" / "workspace.yaml"
    value_path = project_root / "config" / "value-resolution.yaml"
    if value_resolution is None:
        try:
            loaded = yaml.safe_load(value_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            loaded = None
        value_resolution = loaded if isinstance(loaded, dict) else None
    if value_resolution is not None:
        value_digest = hashlib.sha256(
            yaml.safe_dump(value_resolution, allow_unicode=True, sort_keys=True).encode("utf-8")
        ).hexdigest()
    else:
        value_digest = None
    return {"workspace_sha256": _sha256(workspace), "value_resolution_sha256": value_digest}


def build_generation_lock(
    project_root: Path,
    design: dict[str, Any],
    protocol: dict[str, Any],
    *,
    value_resolution: dict[str, Any],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create the complete generation-input lock and change summary."""

    current_design = input_summary(design)
    current_protocol = input_summary(protocol)
    current_source = source_snapshot(project_root)
    current_support = support_snapshot(project_root, value_resolution)
    current_scenarios = scenario_snapshot(project_root)
    previous = previous if isinstance(previous, dict) else {}
    return {
        "version": 1,
        "tool": {"name": "dev-ai", "version": __version__, "e2e_schema": E2E_GATE_SCHEMA_VERSION},
        "design": current_design,
        "protocol": current_protocol,
        "source": current_source,
        "support": current_support,
        "scenario_generation": {"version": 1, "generated_at": datetime.now(timezone.utc).isoformat()},
        "changes": {
            "design_changed": current_design != previous.get("design"),
            "protocol_changed": current_protocol != previous.get("protocol"),
            "source_changed": current_source != previous.get("source"),
            "support_changed": current_support != previous.get("support"),
            "scenario_data_changed": current_scenarios.get("data_sha256") != previous.get("scenarios", {}).get("data_sha256"),
            "cleanup_changed": current_scenarios.get("cleanup_sha256") != previous.get("scenarios", {}).get("cleanup_sha256"),
        },
        "scenarios": current_scenarios,
    }


def generation_lock_errors(project_root: Path, lock: Any) -> list[str]:
    """Reject missing, malformed, or stale generation inputs."""

    if not isinstance(lock, dict):
        return ["version-lock.yaml must contain a mapping"]
    required = {
        "version", "tool", "design", "protocol", "source", "support",
        "scenario_generation", "changes", "scenarios",
    }
    errors = [f"version-lock.yaml missing fields: {sorted(required - set(lock))}"] if not required.issubset(lock) else []
    tool = lock.get("tool")
    if not isinstance(tool, dict) or tool.get("name") != "dev-ai" or tool.get("e2e_schema") != E2E_GATE_SCHEMA_VERSION:
        errors.append("version-lock.yaml tool or E2E schema version does not match the installed generator")
    for kind in ("design", "protocol"):
        summary = lock.get(kind)
        documents = summary.get("documents", []) if isinstance(summary, dict) else []
        if not isinstance(documents, list) or not documents:
            errors.append(f"version-lock.yaml {kind} documents are required")
            continue
        for item in documents:
            if not isinstance(item, dict) or not item.get("path") or not item.get("sha256"):
                errors.append(f"version-lock.yaml {kind} document entry is invalid")
                continue
            path = Path(str(item["path"]))
            path = path if path.is_absolute() else project_root / path
            actual = _sha256(path)
            if actual is None:
                errors.append(f"locked {kind} document is missing: {path}")
            elif actual != item["sha256"]:
                errors.append(f"locked {kind} document changed: {path}")
        normalized = [
            {key: item.get(key) for key in ("path", "sha256", "version") if key in item}
            for item in documents if isinstance(item, dict)
        ]
        digest = hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        if isinstance(summary, dict) and summary.get("sha256") != digest:
            errors.append(f"version-lock.yaml {kind} aggregate digest is invalid")
    if lock.get("source") != source_snapshot(project_root):
        errors.append("participating repository source versions changed after generation")
    if lock.get("support") != support_snapshot(project_root):
        errors.append("workspace or value-resolution support configuration changed after generation")
    if lock.get("scenarios") != scenario_snapshot(project_root):
        errors.append("scenario data or cleanup contract changed after generation")
    return errors


def source_version_results(project_root: Path, selected: str | None = None) -> tuple[list[str], list[dict[str, Any]]]:
    """只读比较每个场景的源码基线、当前提交和工作区状态。"""

    from .contracts import contract_errors

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

__all__ = [
    "RULES_VERSION", "build_generation_lock", "generation_lock_errors", "input_summary",
    "scenario_snapshot", "source_snapshot", "source_version_results", "support_snapshot",
]
