#!/usr/bin/env python3
"""Small JSON/YAML loader shared by the manifest validation scripts."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True


def load_data(path: Path) -> Any:
    if not path.is_file():
        raise SystemExit(f"manifest does not exist: {path}")
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"cannot parse {path}: {exc}") from exc
    try:
        import yaml  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise SystemExit("YAML manifests require an existing PyYAML installation") from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:  # type: ignore[attr-defined]
        raise SystemExit(f"cannot parse {path}: {exc}") from exc


def list_at(document: Any, key: str) -> list[dict[str, Any]]:
    """Collect lists under a named key, including nested endpoint cases."""
    found: list[dict[str, Any]] = []
    if isinstance(document, dict):
        value = document.get(key)
        if isinstance(value, list):
            found.extend(item for item in value if isinstance(item, dict))
        for child in document.values():
            found.extend(list_at(child, key))
    elif isinstance(document, list):
        for child in document:
            found.extend(list_at(child, key))
    return found


def first_list(document: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(document, dict) and isinstance(document.get(key), list):
        return [item for item in document[key] if isinstance(item, dict)]
    if isinstance(document, list):
        return [item for item in document if isinstance(item, dict)]
    return []
