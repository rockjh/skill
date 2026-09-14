#!/usr/bin/env python3
"""Synchronize and verify the reusable project-local QA script bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

from tool_version import SCRIPTS_VERSION, SKILL_VERSION, SOURCE_REPOSITORY


README_FALLBACK = """# QA 脚本工具链

此目录由 `mno-bruno-qa scripts sync` 管理。使用 `mno-bruno-qa --help` 查看
初始化、增量生成、检查、预检、风险执行、对账和脚本同步命令。
`scripts-version.yaml` 记录版本、来源、聚合/逐文件 SHA 和同步时间。
"""

SCRIPT_NAMES = (
    "analyze_java_logic.py",
    "analyze_source_logic.py",
    "check_api_coverage.py",
    "check_artifact_safety.py",
    "check_version_compatibility.py",
    "execution_config.py",
    "fetch_local_openapi.py",
    "manifest_io.py",
    "materialize_missing_bru.py",
    "mno_bruno_qa.py",
    "normalize_bruno_report.py",
    "parse_openapi.py",
    "qa_lock.py",
    "run_bruno.py",
    "runtime_preflight.py",
    "scripts_manager.py",
    "tool_version.py",
    "validate_flow_execution.py",
)


def source_files(source_root: Path) -> list[Path]:
    return [source_root / name for name in SCRIPT_NAMES if (source_root / name).is_file()]


def file_hashes(root: Path, names: list[str]) -> dict[str, str]:
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in names
        if (root / name).is_file()
    }


def bundle_sha(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for name, value in sorted(files.items()):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(value.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def metadata(source_root: Path, synchronized_at: str | None = None) -> dict[str, Any]:
    names = [path.name for path in source_files(source_root)]
    hashes = file_hashes(source_root, names)
    return {
        "skill_version": SKILL_VERSION,
        "scripts_version": SCRIPTS_VERSION,
        "source_repository": SOURCE_REPOSITORY,
        "scripts_sha256": bundle_sha(hashes),
        "synchronized_at": synchronized_at or datetime.now(timezone.utc).isoformat(),
        "files": hashes,
    }


def load_metadata(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        import yaml  # type: ignore[import-not-found]

        value = yaml.safe_load(path.read_text(encoding="utf-8", errors="strict"))
    except ModuleNotFoundError as exc:
        raise ValueError("script synchronization requires PyYAML") from exc
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:  # type: ignore[attr-defined]
        raise ValueError(f"cannot parse {path}: {exc}") from exc
    return value if isinstance(value, dict) else {}


def render_metadata(value: dict[str, Any]) -> str:
    try:
        import yaml  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise ValueError("script synchronization requires PyYAML") from exc
    return yaml.safe_dump(value, allow_unicode=True, sort_keys=False)


def check_scripts(qa_root: Path, source_root: Path) -> list[str]:
    target = qa_root.resolve() / "scripts"
    expected = metadata(source_root.resolve(), synchronized_at="")
    actual_hashes = file_hashes(target, list(expected["files"]))
    errors: list[str] = []
    missing = sorted(set(expected["files"]) - set(actual_hashes))
    changed = sorted(
        name for name, digest in actual_hashes.items()
        if expected["files"].get(name) != digest
    )
    if missing:
        errors.append("missing project QA scripts: " + ", ".join(missing))
    if changed:
        errors.append("outdated project QA scripts: " + ", ".join(changed))
    readme = target / "README.md"
    if not readme.is_file():
        errors.append("qa/scripts/README.md is missing")
    version_path = target / "scripts-version.yaml"
    try:
        current = load_metadata(version_path)
    except ValueError as exc:
        errors.append(str(exc))
        current = {}
    for field in ("skill_version", "scripts_version", "source_repository", "scripts_sha256"):
        if current.get(field) != expected.get(field):
            errors.append(
                f"qa/scripts/scripts-version.yaml {field} is {current.get(field)!r}, expected {expected.get(field)!r}"
            )
    return errors


def sync_scripts(qa_root: Path, source_root: Path) -> list[Path]:
    source_root = source_root.resolve()
    target = qa_root.resolve() / "scripts"
    target.mkdir(parents=True, exist_ok=True)
    expected = metadata(source_root)
    version_path = target / "scripts-version.yaml"
    try:
        current = load_metadata(version_path)
    except ValueError:
        current = {}
    changed: list[Path] = []
    for source in source_files(source_root):
        destination = target / source.name
        if source.resolve() == destination.resolve():
            continue
        if not destination.is_file() or destination.read_bytes() != source.read_bytes():
            shutil.copy2(source, destination)
            changed.append(destination)
    source_readme = source_root / "README.md"
    target_readme = target / "README.md"
    readme_content = source_readme.read_text(encoding="utf-8", errors="strict") if source_readme.is_file() else README_FALLBACK
    if source_readme.resolve() != target_readme.resolve():
        if not target_readme.is_file() or target_readme.read_text(encoding="utf-8", errors="strict") != readme_content:
            target_readme.write_text(readme_content, encoding="utf-8")
            changed.append(target_readme)
    same_release = all(
        current.get(field) == expected.get(field)
        for field in ("skill_version", "scripts_version", "source_repository", "scripts_sha256")
    )
    if not same_release:
        version_path.write_text(render_metadata(expected), encoding="utf-8")
        changed.append(version_path)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("sync", "check"))
    parser.add_argument("--qa-root", type=Path, default=Path("qa"))
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    if args.action == "sync":
        changed = sync_scripts(args.qa_root, args.source_root)
        result = {"status": "synchronized", "changed": [str(path) for path in changed]}
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.as_json else f"synchronized {len(changed)} file(s)")
        return 0
    errors = check_scripts(args.qa_root, args.source_root)
    result = {"status": "current" if not errors else "outdated", "errors": errors}
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.as_json else ("scripts are current" if not errors else "\n".join(f"ERROR: {error}" for error in errors)))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
