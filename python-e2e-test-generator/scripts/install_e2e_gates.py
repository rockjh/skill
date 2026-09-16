#!/usr/bin/env python3
"""将版本固定的 E2E 门禁资产安装到目标 pytest 工程。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path


sys.dont_write_bytecode = True

ASSET_ROOT = Path(__file__).resolve().parents[1] / "assets" / "e2e_project"
MANIFEST_NAME = ".e2e-gates.json"
ASSET_VERSION = 4


def _digest(data: bytes) -> str:
    """计算门禁资产内容摘要。"""

    return hashlib.sha256(data).hexdigest()


def _planned_files(project_root: Path) -> dict[Path, bytes]:
    """收集版本固定的公共门禁和统一运行脚本。"""

    return {
        path.relative_to(ASSET_ROOT): path.read_bytes()
        for path in sorted(ASSET_ROOT.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}
    }


def install(project_root: Path, *, force: bool = False) -> Path:
    """安装资产，并拒绝静默覆盖内容不同的已有文件。"""

    project_root = project_root.resolve()
    if not project_root.is_dir():
        raise ValueError(f"目标 E2E 工程不存在: {project_root}")

    planned = _planned_files(project_root)
    manifest_path = project_root / MANIFEST_NAME
    previous_files: dict[str, str] = {}
    if manifest_path.is_file():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(previous, dict) and isinstance(previous.get("files"), dict):
                previous_files = {
                    str(path): str(digest) for path, digest in previous["files"].items()
                }
        except (OSError, UnicodeError, json.JSONDecodeError):
            previous_files = {}
    planned_names = {path.as_posix() for path in planned}
    obsolete_launchers = {
        relative: digest for relative, digest in previous_files.items()
        if relative not in planned_names and re.fullmatch(r"scripts/run_[^/]+\.(?:sh|bat)", relative)
    }
    conflicts = [
        str(relative)
        for relative, data in planned.items()
        if (project_root / relative).is_file()
        and (project_root / relative).read_bytes() != data
    ]
    for relative, digest in obsolete_launchers.items():
        path = project_root / Path(relative)
        if path.is_file() and _digest(path.read_bytes()) != digest:
            conflicts.append(relative)
    if conflicts and not force:
        raise ValueError("门禁资产已被修改，拒绝覆盖: " + ", ".join(conflicts))

    digests: dict[str, str] = {}
    for relative, data in planned.items():
        destination = project_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        if destination.suffix == ".sh" and os.name != "nt":
            destination.chmod(destination.stat().st_mode | 0o111)
        digests[relative.as_posix()] = _digest(data)
    for relative in obsolete_launchers:
        destination = (project_root / Path(relative)).resolve()
        if project_root not in destination.parents:
            raise ValueError(f"旧启动器路径逃逸目标工程: {relative}")
        if destination.is_file():
            destination.unlink()

    manifest = {
        "version": ASSET_VERSION,
        "files": digests,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def check(project_root: Path) -> list[str]:
    """核对已安装资产与摘要清单是否一致。"""

    project_root = project_root.resolve()
    manifest_path = project_root / MANIFEST_NAME
    if not manifest_path.is_file():
        return [f"缺少门禁资产清单: {manifest_path}"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"门禁资产清单无法解析: {exc}"]
    if manifest.get("version") != ASSET_VERSION or not isinstance(manifest.get("files"), dict):
        return [f"门禁资产清单版本无效: {manifest_path}"]

    errors: list[str] = []
    planned = _planned_files(project_root)
    expected_paths = {path.as_posix() for path in planned}
    actual_paths = set(manifest["files"])
    for relative in sorted(expected_paths - actual_paths):
        errors.append(f"门禁资产清单遗漏: {relative}")
    for relative in sorted(actual_paths - expected_paths):
        errors.append(f"门禁资产清单包含未知文件: {relative}")
    for relative, data in planned.items():
        relative_text = relative.as_posix()
        expected = manifest["files"].get(relative_text)
        if expected is None:
            continue
        path = project_root / relative
        if not path.is_file():
            errors.append(f"门禁资产缺失: {path}")
        elif _digest(path.read_bytes()) != expected or expected != _digest(data):
            errors.append(f"门禁资产内容漂移: {path}")
    return errors


def main(argv: list[str] | None = None) -> int:
    """解析安装或核验命令并返回进程状态。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path, help="目标 E2E 工程根目录")
    parser.add_argument("--force", action="store_true", help="显式覆盖内容不同的门禁资产")
    parser.add_argument("--check", action="store_true", help="只核对已安装资产")
    args = parser.parse_args(argv)
    try:
        if args.check:
            errors = check(args.project)
            for error in errors:
                print(error, file=sys.stderr)
            return 1 if errors else 0
        path = install(args.project, force=args.force)
        print(path)
        return 0
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
