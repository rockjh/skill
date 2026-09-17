"""Resolve one executable identically for preflight and real execution."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


WINDOWS_EXECUTABLE_SUFFIXES = (".cmd", ".exe", ".bat", "", ".ps1")


def _cmd_quote(value: str) -> str:
    if "\0" in value or "\r" in value or "\n" in value:
        raise ValueError("Windows command arguments cannot contain NUL or newlines")
    return '"' + value.replace("%", "^%").replace('"', '""') + '"'


def resolve_executable(executable: str) -> str | None:
    """Resolve a command predictably, preferring npm's Windows launcher."""

    if os.name != "nt" or Path(executable).suffix:
        return shutil.which(executable)
    for suffix in WINDOWS_EXECUTABLE_SUFFIXES:
        resolved = shutil.which(f"{executable}{suffix}")
        if resolved:
            return resolved
    return None


def command_argv(executable: str, *arguments: str) -> list[str] | str:
    resolved = resolve_executable(executable)
    if not resolved:
        raise FileNotFoundError(executable)
    if os.name == "nt" and Path(resolved).suffix.lower() in {".cmd", ".bat"}:
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        command = " ".join(_cmd_quote(value) for value in (resolved, *arguments))
        return f"{subprocess.list2cmdline([comspec])} /d /s /c \"{command}\""
    return [resolved, *arguments]
