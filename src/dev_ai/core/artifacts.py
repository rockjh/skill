"""Small helpers for redacted dev-ai artifacts and version locks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import DevAIError, ExitCode
from .redaction import redact


LOCK_NAME = ".dev-ai.lock.json"


def state_root() -> Path:
    return Path.home() / ".local" / "state" / "dev-ai" / "artifacts"


def write_json(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(redact(value), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def write_lock(project_root: Path, *, tool_version: str, domain: str, schema_version: str) -> Path:
    return write_json(project_root / LOCK_NAME, {
        "tool": "dev-ai",
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
        raise DevAIError(
            "GATE_FAILED",
            f"dev-ai lock does not exist: {path}",
            ExitCode.GATE_FAILED,
            "Run the domain init command with the installed dev-ai version.",
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DevAIError("GATE_FAILED", f"cannot read dev-ai lock {path}: {exc}", ExitCode.GATE_FAILED) from exc
    if value.get("tool") != "dev-ai" or value.get("domain") != domain:
        raise DevAIError("GATE_FAILED", f"dev-ai lock has the wrong domain: {path}", ExitCode.GATE_FAILED)
    if value.get("tool_version") != tool_version:
        raise DevAIError(
            "GATE_FAILED",
            f"project requires dev-ai {value.get('tool_version')}, installed version is {tool_version}",
            ExitCode.GATE_FAILED,
            "Use the locked dev-ai version or explicitly reinitialize the project.",
        )
    if value.get("schema_version") != schema_version:
        raise DevAIError(
            "GATE_FAILED",
            f"project requires {domain} schema {value.get('schema_version')}, installed schema is {schema_version}",
            ExitCode.GATE_FAILED,
            "Use the locked domain schema or explicitly reinitialize the project.",
        )
    return value
