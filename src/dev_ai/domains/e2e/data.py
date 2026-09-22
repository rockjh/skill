"""Generic scenario-owned data and environment dependency classification."""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping
from typing import Any


ENV_RE = re.compile(r"^\$\{[A-Z][A-Z0-9_]*\}$")


def _seed(namespace: str, path: str) -> str:
    return hashlib.sha256(f"{namespace}:{path}".encode()).hexdigest()


def generate_value(field: Mapping[str, Any], *, namespace: str, path: str) -> Any:
    """Generate a deterministic legal value from transport constraints only."""

    enum = field.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    kind = str(field.get("type", "string"))
    fmt = str(field.get("format", ""))
    seed = _seed(namespace, path)
    if fmt == "uuid":
        return str(uuid.UUID(seed[:32]))
    if fmt in {"date", "date-time"}:
        return "2025-01-01T00:00:00Z" if fmt == "date-time" else "2025-01-01"
    if kind in {"integer", "number"}:
        minimum = field.get("minimum", field.get("exclusiveMinimum", 1))
        maximum = field.get("maximum")
        value = int(minimum) + (int(seed[:8], 16) % 17)
        return min(value, int(maximum)) if maximum is not None else value
    if kind == "boolean":
        return True
    if kind == "object":
        return {}
    if kind == "array":
        minimum_items = int(field.get("minItems", 1) or 1)
        maximum_items = int(field.get("maxItems", minimum_items) or minimum_items)
        count = max(1, min(minimum_items, maximum_items))
        return [
            generate_value(field.get("items", {}), namespace=namespace, path=f"{path}[{index}]")
            for index in range(count)
        ]
    minimum = int(field.get("minLength", 1) or 1)
    value = f"e2e-{seed[:12]}"
    if field.get("pattern"):
        # Preserve a legal, deterministic literal when a protocol only gives a
        # regular expression.  Exact pattern synthesis belongs to the protocol
        # adapter; the fallback remains explicit rather than silently invalid.
        pattern = str(field["pattern"])
        if pattern in {"^[0-9a-f-]{36}$", "^[0-9a-fA-F-]{36}$"}:
            return str(uuid.UUID(seed[:32]))
    return value if len(value) >= minimum else value.ljust(minimum, "x")


def generate_request_data(operation: Mapping[str, Any], *, namespace: str) -> dict[str, Any]:
    """Build only required/formal fields; business expectations stay in design."""

    result: dict[str, Any] = {}
    fields = operation.get("request_fields", []) if isinstance(operation.get("request_fields"), list) else []
    for field in fields:
        if not isinstance(field, Mapping) or field.get("required") is not True:
            continue
        path = str(field.get("path") or field.get("name") or "field")
        current: Any = result
        tokens = re.findall(r"[^.\[\]]+", path)
        for token in tokens[:-1]:
            if not isinstance(current, dict):
                break
            current = current.setdefault(token, {})
        if isinstance(current, dict) and tokens:
            current[tokens[-1]] = generate_value(field, namespace=namespace, path=path)
    for parameter in operation.get("parameters", []) if isinstance(operation.get("parameters"), list) else []:
        if isinstance(parameter, Mapping) and parameter.get("required") is True:
            name = str(parameter.get("name", "parameter"))
            result.setdefault(name, generate_value(parameter.get("schema", {}), namespace=namespace, path=name))
    return result


def _operation_key(operation: Mapping[str, Any], index: int) -> str:
    """Return a stable scenario-local key without using project vocabulary."""

    raw = str(operation.get("id") or operation.get("event") or operation.get("task") or f"step_{index}")
    key = re.sub(r"[^A-Za-z0-9_]+", "_", raw).strip("_").lower()
    return key or f"step_{index}"


def generate_scenario_data(
    operations: list[Mapping[str, Any]],
    *,
    namespace: str,
    environments: tuple[str, ...] = ("test",),
    correlation_keys: list[str] | tuple[str, ...] = (),
) -> dict[str, dict[str, Any]]:
    """Generate isolated request, message, response and correlation data.

    Only transport constraints are used.  Business expectations remain in the
    design rule and are never inferred from generated literals.
    """

    generated: dict[str, Any] = {}
    for index, operation in enumerate(operations):
        if not isinstance(operation, Mapping):
            continue
        key = _operation_key(operation, index)
        request = generate_request_data(operation, namespace=namespace)
        message_fields = operation.get("message_fields") if isinstance(operation.get("message_fields"), list) else []
        for field in message_fields:
            if not isinstance(field, Mapping) or field.get("required") is not True:
                continue
            path = str(field.get("path") or field.get("name") or "payload.field")
            tokens = re.findall(r"[^.\[\]]+", path)
            if path.startswith("payload."):
                target = request.setdefault("payload", {})
                tokens = tokens[1:]
            elif path.startswith("headers."):
                target = request.setdefault("headers", {})
                tokens = tokens[1:]
            else:
                target = request
            for token in tokens[:-1]:
                target = target.setdefault(token, {}) if isinstance(target, dict) else {}
            if isinstance(target, dict) and tokens:
                target[tokens[-1]] = generate_value(field, namespace=namespace, path=f"{key}.{path}")
        response_fields = operation.get("response_fields") if isinstance(operation.get("response_fields"), list) else []
        response: dict[str, Any] = {}
        for field in response_fields:
            if not isinstance(field, Mapping) or field.get("required") is not True:
                continue
            field_path = str(field.get("path") or field.get("name") or "")
            if not field_path:
                continue
            target: Any = response
            tokens = re.findall(r"[^.\[\]]+", field_path)
            for token in tokens[:-1]:
                if isinstance(target, dict):
                    target = target.setdefault(token, {})
            if isinstance(target, dict) and tokens:
                target[tokens[-1]] = generate_value(
                    field, namespace=namespace, path=f"{key}.response.{field_path}"
                )
        if not response and isinstance(operation.get("status_codes"), list) and operation["status_codes"]:
            response = {"status": str(operation["status_codes"][0])}
        generated[key] = request
        if response:
            generated[f"{key}_response"] = response
    for key in correlation_keys:
        name = str(key).strip()
        if name:
            generated[name] = f"e2e-{_seed(namespace, name)[:16]}"
    generated["simulated_time"] = "2025-01-01T00:00:00Z"
    return {environment: dict(generated) for environment in dict.fromkeys(environments) if str(environment).strip()}


def classify_data_requirements(value: Any) -> dict[str, Any]:
    """Separate safely generated literals from environment-owned references."""

    environment: list[str] = []
    generated: list[str] = []

    def walk(item: Any, path: str) -> None:
        if isinstance(item, str) and ENV_RE.fullmatch(item):
            environment.append(f"{path}:{item[2:-1]}")
        elif isinstance(item, Mapping):
            for key, child in item.items():
                walk(child, f"{path}.{key}")
        elif isinstance(item, list):
            for index, child in enumerate(item):
                walk(child, f"{path}[{index}]")
        elif item not in (None, ""):
            generated.append(path)

    walk(value, "$")
    return {
        "generated": generated,
        "environment_owned": environment,
        "status": "pending_environment" if environment else "ready",
    }
