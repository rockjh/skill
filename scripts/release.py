"""Build the dltk wheel and stage the npm installer payload."""

from __future__ import annotations

import shutil
import subprocess
import sys
import json
import tomllib
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NPM = ROOT / "npm"


def main() -> int:
    dist = ROOT / "dist"
    python_version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    npm_version = json.loads((NPM / "package.json").read_text(encoding="utf-8"))["version"]
    if python_version != npm_version:
        raise RuntimeError(f"release version mismatch: Python={python_version}, npm={npm_version}")
    build = ROOT / "build"
    if build.exists():
        shutil.rmtree(build)
    dist.mkdir(parents=True, exist_ok=True)
    for old_wheel in dist.glob("*.whl"):
        old_wheel.unlink()
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(dist), str(ROOT)],
        cwd=ROOT,
        check=True,
    )
    wheels = sorted(dist.glob("dltk-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected one dltk wheel, found {len(wheels)}")
    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())
        if any(Path(name).name == "_engine.py" for name in names):
            raise RuntimeError("wheel contains the removed E2E legacy engine")
        entry_points = archive.read(
            f"dltk-{python_version}.dist-info/entry_points.txt"
        ).decode("utf-8").strip()
        if entry_points != "[console_scripts]\ndltk = dltk.cli:console_main":
            raise RuntimeError(f"unexpected wheel entry points: {entry_points}")
    npm_dist = NPM / "dist"
    if npm_dist.exists():
        shutil.rmtree(npm_dist)
    npm_dist.mkdir(parents=True, exist_ok=True)
    shutil.copy2(wheels[0], npm_dist / wheels[0].name)
    target_skills = NPM / "skills"
    if target_skills.exists():
        shutil.rmtree(target_skills)
    shutil.copytree(ROOT / "skills" / "get-my-dev-lifecycle-toolkit", target_skills / "get-my-dev-lifecycle-toolkit")
    payload = sorted(path.relative_to(target_skills).parts[0] for path in target_skills.iterdir())
    if payload != ["get-my-dev-lifecycle-toolkit"]:
        raise RuntimeError(f"unexpected npm Skill payload: {payload}")
    npm = "npm.cmd" if sys.platform == "win32" else "npm"
    subprocess.run([npm, "pack", "--ignore-scripts"], cwd=NPM, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
