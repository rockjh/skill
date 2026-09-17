"""Installation diagnostics for the shared runtime."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from typing import Any


def diagnose() -> tuple[dict[str, Any], bool]:
    checks = {
        "python": {"ok": sys.version_info >= (3, 11), "value": sys.version.split()[0], "required": ">=3.11"},
        "PyYAML": {"ok": importlib.util.find_spec("yaml") is not None},
        "pytest": {"ok": importlib.util.find_spec("pytest") is not None},
        "git": {"ok": shutil.which("git") is not None},
        "bruno": {"ok": shutil.which("bru") is not None, "required_for": "api-test.run"},
    }
    required = ("python", "PyYAML", "pytest", "git")
    return {"checks": checks}, all(checks[name]["ok"] for name in required)
