"""Small helpers for redacted dltk artifacts and version locks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import DltkError, ExitCode
from .redaction import redact


LOCK_NAME = ".dltk.lock.json"


def state_root() -> Path:
    return Path.home() / ".local" / "state" / "dltk" / "artifacts"


def write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(redact(value), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def write_lock(project_root: Path, *, tool_version: str, domain: str, schema_version: str) -> Path:
    return write_json(project_root / LOCK_NAME, {
        "tool": "dltk",
        "tool_version": tool_version,
        "domain": domain,
        "schema_version": schema_version,
    })


def require_lock(
    project_root: Path,
    *,
    tool_version: str,
    domain: str,
    schema_version: str,
) -> dict[str, Any]:
    path = project_root / LOCK_NAME
    if not path.is_file():
        raise DltkError(
            "GATE_FAILED",
            f"dltk lock does not exist: {path}",
            ExitCode.GATE_FAILED,
            "Run the domain init command with the installed dltk version.",
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DltkError("GATE_FAILED", f"cannot read dltk lock {path}: {exc}", ExitCode.GATE_FAILED) from exc
    if value.get("tool") != "dltk" or value.get("domain") != domain:
        raise DltkError("GATE_FAILED", f"dltk lock has the wrong domain: {path}", ExitCode.GATE_FAILED)
    if value.get("tool_version") != tool_version:
        raise DltkError(
            "GATE_FAILED",
            f"project requires dltk {value.get('tool_version')}, installed version is {tool_version}",
            ExitCode.GATE_FAILED,
            "Use the locked dltk version or explicitly reinitialize the project.",
        )
    if value.get("schema_version") != schema_version:
        raise DltkError(
            "GATE_FAILED",
            f"project requires {domain} schema {value.get('schema_version')}, installed schema is {schema_version}",
            ExitCode.GATE_FAILED,
            "Use the locked domain schema or explicitly reinitialize the project.",
        )
    return value
