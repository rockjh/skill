"""The single machine-readable output protocol used by every command."""

from __future__ import annotations

import json
from typing import Any

from .errors import DevAIError
from .redaction import redact


def success(command: str, data: Any, artifact_path: str = "") -> dict[str, Any]:
    return redact({
        "ok": True,
        "tool": "dev-ai",
        "command": command,
        "data": data,
        "artifact_path": artifact_path,
    })


def failure(command: str, error: DevAIError) -> dict[str, Any]:
    return redact({
        "ok": False,
        "tool": "dev-ai",
        "command": command,
        "error": {
            "code": error.code,
            "message": error.message,
            "hint": error.hint,
            "details_path": error.details_path,
        },
    })


def render(document: dict[str, Any], *, markdown: bool) -> str:
    if not markdown:
        return json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    if document["ok"]:
        lines = [f"# dev-ai {document['command']}", "", "Status: OK"]
        data = document.get("data")
        if isinstance(data, dict):
            for key, value in data.items():
                rendered = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
                lines.append(f"- {key}: {rendered}")
        elif data not in (None, ""):
            lines.append(str(data))
        if document.get("artifact_path"):
            lines.append(f"- artifact: {document['artifact_path']}")
        return "\n".join(lines)
    error = document["error"]
    lines = [f"# dev-ai {document['command']}", "", f"Status: {error['code']}", "", error["message"]]
    if error.get("hint"):
        lines.extend(("", f"Hint: {error['hint']}"))
    if error.get("details_path"):
        lines.extend(("", f"Details: {error['details_path']}"))
    return "\n".join(lines)
