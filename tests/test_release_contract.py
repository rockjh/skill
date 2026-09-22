from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NPM = ROOT / "npm"


class ReleaseContractTests(unittest.TestCase):
    def test_python_and_npm_publish_one_matching_cli(self) -> None:
        python = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        npm = json.loads((NPM / "package.json").read_text(encoding="utf-8"))

        self.assertEqual(python["project"]["version"], npm["version"])
        self.assertEqual({"dltk": "dltk.cli:console_main"}, python["project"]["scripts"])
        self.assertEqual({"dltk": "bin/dltk.js"}, npm["bin"])
        self.assertIn("scripts/", npm["files"])
        self.assertIn("dist/", npm["files"])

    @unittest.skipUnless(shutil.which("node"), "node is required to verify the npm runtime")
    def test_npm_runtime_resolves_the_pipx_executable_without_path_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bin_dir = Path(temporary).resolve()
            executable = bin_dir / ("dltk.exe" if os.name == "nt" else "dltk")
            executable.touch()
            script = "console.log(require('./npm/scripts/install').resolveDltk())"
            environment = {**os.environ, "PIPX_BIN_DIR": str(bin_dir)}
            result = subprocess.run(
                [shutil.which("node") or "node", "-e", script],
                cwd=ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )

        self.assertEqual(executable, Path(result.stdout.strip()))
        wrapper = (NPM / "bin" / "dltk.js").read_text(encoding="utf-8")
        self.assertIn("resolveDltk()", wrapper)
        self.assertNotIn('spawnSync("dltk"', wrapper)
        self.assertNotIn("spawnSync('dltk'", wrapper)


if __name__ == "__main__":
    unittest.main()
