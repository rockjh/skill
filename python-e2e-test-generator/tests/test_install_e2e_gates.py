"""验证版本化 E2E 门禁资产的安装与防漂移行为。"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "install_e2e_gates.py"
SPEC = importlib.util.spec_from_file_location("install_e2e_gates", MODULE_PATH)
assert SPEC and SPEC.loader
INSTALLER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALLER)


class InstallerTests(unittest.TestCase):
    """覆盖安装、核验、冲突拒绝和统一启动器。"""

    def test_install_and_check_round_trip(self) -> None:
        """安装后的摘要清单应完整核验全部固定资产。"""

        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / "scenarios" / "示例场景").mkdir(parents=True)
            manifest = INSTALLER.install(project)

            self.assertTrue(manifest.is_file())
            self.assertEqual([], INSTALLER.check(project))
            self.assertFalse(any("__pycache__" in item.parts for item in INSTALLER._planned_files(project)))
            shell = (project / "scripts" / "run.sh").read_text(encoding="utf-8")
            batch = (project / "scripts" / "run.bat").read_text(encoding="ascii")
            self.assertIn('run.sh --scenario "scenario"', shell)
            self.assertIn('run.bat --scenario "scenario"', batch)
            self.assertIn("PYTHONDONTWRITEBYTECODE=1", shell)
            self.assertIn("PYTHONDONTWRITEBYTECODE=1", batch)
            self.assertFalse((project / "scripts" / "run_示例场景.sh").exists())
            self.assertFalse((project / "scripts" / "run_all.sh").exists())

    def test_scenario_directories_do_not_create_launchers(self) -> None:
        """任何场景目录都不得再生成独立启动器。"""

        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / "scenarios" / "__pycache__").mkdir(parents=True)
            INSTALLER.install(project)
            self.assertFalse((project / "scripts" / "run___pycache__.sh").exists())
            self.assertEqual(
                {"scripts/run.sh", "scripts/run.bat"},
                {path.as_posix() for path in INSTALLER._planned_files(project) if path.suffix in {".sh", ".bat"}},
            )
            self.assertEqual([], INSTALLER.check(project))

    def test_modified_asset_requires_force(self) -> None:
        """已安装文件漂移后默认拒绝覆盖，显式 force 才能恢复。"""

        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            INSTALLER.install(project)
            target = project / "scripts" / "run_e2e.py"
            target.write_text("changed\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "拒绝覆盖"):
                INSTALLER.install(project)
            INSTALLER.install(project, force=True)
            self.assertEqual([], INSTALLER.check(project))

    def test_check_reports_content_drift(self) -> None:
        """摘要核验必须报告被修改的门禁文件。"""

        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            INSTALLER.install(project)
            (project / "common" / "e2e_runtime.py").write_text("changed\n", encoding="utf-8")

            errors = INSTALLER.check(project)
            self.assertTrue(any("内容漂移" in error for error in errors))

    def test_check_rejects_manifest_entry_omission(self) -> None:
        """从摘要清单删除固定资产条目也必须失败。"""

        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            manifest_path = INSTALLER.install(project)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"].pop("scripts/run_e2e.py")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            self.assertTrue(any("清单遗漏" in error for error in INSTALLER.check(project)))

    def test_upgrade_removes_managed_legacy_launchers(self) -> None:
        """升级时必须移除清单中未漂移的旧版统一及逐场景启动器。"""

        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            scripts = project / "scripts"
            scripts.mkdir()
            old_files = {
                "scripts/run_all.sh": b"old-all\n",
                "scripts/run_旧场景.bat": b"old-scenario\r\n",
            }
            for relative, data in old_files.items():
                (project / relative).write_bytes(data)
            manifest = {
                "version": 3,
                "files": {
                    relative: INSTALLER._digest(data)
                    for relative, data in old_files.items()
                },
            }
            (project / ".e2e-gates.json").write_text(json.dumps(manifest), encoding="utf-8")

            INSTALLER.install(project)

            self.assertFalse((project / "scripts" / "run_all.sh").exists())
            self.assertFalse((project / "scripts" / "run_旧场景.bat").exists())
            self.assertEqual([], INSTALLER.check(project))


if __name__ == "__main__":
    unittest.main()
