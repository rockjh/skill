"""Resolve one executable identically for preflight and real execution."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def _cmd_quote(value: str) -> str:
    if "\0" in value or "\r" in value or "\n" in value:
        raise ValueError("Windows command arguments cannot contain NUL or newlines")
    return '"' + value.replace("%", "^%").replace('"', '""') + '"'


def command_argv(executable: str, *arguments: str) -> list[str] | str:
    resolved = shutil.which(executable)
    if not resolved:
        raise FileNotFoundError(executable)
    if os.name == "nt" and Path(resolved).suffix.lower() in {".cmd", ".bat"}:
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        command = " ".join(_cmd_quote(value) for value in (resolved, *arguments))
        return f"{subprocess.list2cmdline([comspec])} /d /s /c \"{command}\""
    return [resolved, *arguments]
