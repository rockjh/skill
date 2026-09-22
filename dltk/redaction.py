"""Redact credentials before data reaches stdout or persisted reports."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


SENSITIVE_KEY = re.compile(
    r"(?:password|passwd|passphrase|secret|token|authorization|credential|cookie|"
    r"api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret|dsn)$",
    re.IGNORECASE,
)
AUTH_HEADER = re.compile(
    r"(?im)\b(authorization|proxy-authorization|cookie|set-cookie)\s*:\s*[^\r\n]+"
)
AUTH_SCHEME = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")
URL_USERINFO = re.compile(r"(?i)(://[^/@:\s]+:)[^/@\s]+(@)")
SENSITIVE_TEXT = re.compile(
    r"(?i)(\b(?:password|passwd|passphrase|secret|token|credential|api[_-]?key|"
    r"access[_-]?key|private[_-]?key|client[_-]?secret|dsn)\s*[:=]\s*)([^\s,;]+)"
)


def redact(value: Any, key: str = "") -> Any:
    if SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(name): redact(item, str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = AUTH_HEADER.sub(lambda match: f"{match.group(1)}: [REDACTED]", value)
        value = AUTH_SCHEME.sub(lambda match: f"{match.group(1)} [REDACTED]", value)
        value = URL_USERINFO.sub(r"\1[REDACTED]\2", value)
        return SENSITIVE_TEXT.sub(r"\1[REDACTED]", value)
    return value
