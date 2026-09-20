from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dev_ai.domains.e2e import assets


DOMAIN_ROOT = Path(__file__).resolve().parents[2] / "src" / "dev_ai" / "domains" / "e2e"


class AssetTests(unittest.TestCase):
    def test_guard_implementation_is_partitioned_by_domain_responsibility(self) -> None:
        self.assertFalse((DOMAIN_ROOT / "_engine.py").exists())
        self.assertFalse((DOMAIN_ROOT / "design.py").exists())
        self.assertFalse((DOMAIN_ROOT / "generation.py").exists())
        expected = {
            "discovery.py": "def discovery_errors(",
            "contracts.py": "def contract_errors(",
            "static_checks.py": "def static_errors(",
            "runner.py": "def run_ordered(",
        }
        for filename, definition in expected.items():
            source = (DOMAIN_ROOT / filename).read_text(encoding="utf-8")
            self.assertIn(definition, source)
            self.assertNotIn("from ._engine import", source)

    def test_initialize_creates_business_shape_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            changed = assets.initialize(root)
            self.assertEqual(
                {str((root / name).resolve()) for name in assets.INITIAL_FILES},
                {str(path) for path in changed},
            )
            for name in ("discovery", "config", "common", "scenarios", "tests", "artifacts"):
                self.assertTrue((root / name).is_dir())
            self.assertFalse((root / "scripts").exists())
            self.assertFalse((root / "common" / "e2e_runtime.py").exists())
            self.assertEqual([], list(root.rglob("*.py")))
            self.assertIn("dev-ai e2e run", (root / "run-e2e.sh").read_text(encoding="utf-8"))
            self.assertIn("database_control_enabled: false", (root / "config" / "config.template.yaml").read_text(encoding="utf-8"))
            self.assertEqual([], assets.project_errors(root))

    def test_initialize_preserves_existing_pyproject(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pyproject = root / "pyproject.toml"
            pyproject.write_text("[project]\nname='owned'\n", encoding="utf-8")
            changed = assets.initialize(root)
            self.assertNotIn(pyproject.resolve(), changed)
            self.assertIn("name='owned'", pyproject.read_text(encoding="utf-8"))
            self.assertEqual([], assets.initialize(root))


if __name__ == "__main__":
    unittest.main()
