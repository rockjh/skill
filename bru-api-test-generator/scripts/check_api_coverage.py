#!/usr/bin/env python3
"""Reconcile modular endpoint, logic, case, and flow manifests with Bruno files.

The checker intentionally validates both directions: manifests must map to
unique Bruno files, and every Bruno file must be registered. When an offline
OpenAPI document is supplied, the endpoint union is checked against it too.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

try:
    from manifest_io import first_list, list_at, load_data
except ImportError:  # Keep a copied checker runnable without the helper module.
    def load_data(path: Path) -> Any:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            return json.loads(text)
        try:
            import yaml  # type: ignore[import-not-found]
        except ModuleNotFoundError as exc:
            raise SystemExit("YAML manifests require PyYAML") from exc
        return yaml.safe_load(text)

    def first_list(document: Any, key: str) -> list[dict[str, Any]]:
        if isinstance(document, dict) and isinstance(document.get(key), list):
            return [item for item in document[key] if isinstance(item, dict)]
        if isinstance(document, list):
            return [item for item in document if isinstance(item, dict)]
        return []

    def list_at(document: Any, key: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        if isinstance(document, dict):
            found.extend(item for item in document.get(key, []) if isinstance(item, dict))
            for child in document.values():
                found.extend(list_at(child, key))
        elif isinstance(document, list):
            for child in document:
                found.extend(list_at(child, key))
        return found
try:
    from parse_openapi import extract, load_document
except ImportError:  # The checker is also intended to be copied on its own.
    def load_document(path: Path) -> dict[str, Any]:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            loaded = json.loads(text)
        else:
            try:
                import yaml  # type: ignore[import-not-found]
            except ModuleNotFoundError as exc:
                raise SystemExit("YAML OpenAPI input requires PyYAML") from exc
            loaded = yaml.safe_load(text)
        if not isinstance(loaded, dict):
            raise ValueError("OpenAPI document must contain an object")
        return loaded

    def stable_id(method: str, path: str, operation: dict[str, Any]) -> str:
        operation_id = operation.get("operationId")
        raw = f"{operation_id}_{method}_{path}" if operation_id else f"{method}_{path}"
        return re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_").upper() or "ENDPOINT"

    def extract(path: Path, document: dict[str, Any]) -> dict[str, Any]:
        paths = document.get("paths")
        if not isinstance(paths, dict):
            raise ValueError("OpenAPI document has no object-valued paths field")
        methods = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
        endpoints = []
        for route, path_item in paths.items():
            if not isinstance(route, str) or not isinstance(path_item, dict):
                continue
            for method, operation in path_item.items():
                if method.lower() in methods and isinstance(operation, dict):
                    endpoints.append({
                        "id": stable_id(method.upper(), route, operation),
                        "method": method.upper(),
                        "path": route,
                    })
        return {"endpoints": sorted(endpoints, key=lambda item: (item["path"], item["method"]))}

SCENARIO_CATEGORIES = (
    "success",
    "authentication",
    "authorization",
    "validation",
    "business_error",
    "query",
    "safety",
    "file",
)

INTEGRITY_SUFFIXES = {".yaml", ".yml", ".md", ".bru"}
MOJIBAKE_RE = re.compile(r"(?:\ufffd|(?:Ã|Â|å|æ|ç)[\x80-\xBF]|â(?:€|™|œ|€�))")
SIGNING_MARKERS = ("script:pre-request", "SECRET_KEY", "ACCESS_KEY", "SHA256", "timestamp", "accesskey")
MODE_ALIASES = {
    "seres.sign": "seres-sign",
    "seres_sign": "seres-sign",
    "seres-sign": "seres-sign",
    "oauth2-client-credentials": "oauth2",
    "oauth2-authorization-code": "oauth2",
    "session": "cookie",
    "cookie-session": "cookie",
}
CASE_DOCS_START = "<!-- AUTO_CASES_START -->"
CASE_DOCS_END = "<!-- AUTO_CASES_END -->"


def canonical_mode(value: Any) -> str:
    name = str(value or "").strip().lower()
    return MODE_ALIASES.get(name, name)


def auth_mode_settings(config: dict[str, Any], mode: str) -> dict[str, Any]:
    modes = config.get("modes") if isinstance(config.get("modes"), dict) else {}
    for name, value in modes.items():
        if canonical_mode(name) == mode and isinstance(value, dict):
            return value
    return {}


def validate_auth_config_document(config: Any) -> list[str]:
    """Validate mode selection without requiring runtime credentials.

    The preflight script performs the same validation before execution. The
    coverage checker keeps a small local copy so it remains usable when copied
    into a business repository without the rest of this skill's scripts.
    """

    if not isinstance(config, dict):
        return ["authentication config must contain an object"]
    aliases = MODE_ALIASES
    mode_value = config.get("mode")
    modes = config.get("modes") if isinstance(config.get("modes"), dict) else {}
    enabled_modes = {
        aliases.get(str(name).strip().lower(), str(name).strip().lower())
        for name, value in modes.items()
        if isinstance(value, dict) and value.get("enabled") is True
    }
    enabled_modes.update(
        aliases.get(str(name).strip().lower(), str(name).strip().lower())
        for name, value in config.items()
        if name not in {"mode", "modes", "version", "base_url_env"} and value is True
    )
    selected = str(mode_value).strip().lower() if isinstance(mode_value, str) and mode_value.strip() else None
    selected = aliases.get(selected, selected) if selected else None
    if selected is None:
        if len(enabled_modes) > 1:
            return ["authentication config enables more than one mode: " + ", ".join(sorted(enabled_modes))]
        selected = next(iter(enabled_modes), "none")
    allowed = {
        "none", "disabled", "seres-sign", "bearer", "bearer-token", "token",
        "oauth2", "cookie", "api-key", "apikey", "headers", "custom", "custom-headers",
    }
    errors: list[str] = []
    if selected not in allowed:
        errors.append(f"unsupported authentication mode: {selected}")
    if enabled_modes - {selected}:
        errors.append("authentication config enables more than one mode: " + ", ".join(sorted(enabled_modes)))
    settings = auth_mode_settings(config, selected)
    if settings.get("enabled") is False:
        return errors
    def check_env(value: Any, default: str, label: str) -> None:
        candidate = default if value is None else str(value).strip()
        if not candidate or candidate.lower() in {"none", "null"}:
            errors.append(f"{label} must name an environment variable")

    check_env(config.get("base_url_env"), "BASE_URL", "base_url_env")
    if selected == "seres-sign" and str(settings.get("algorithm", "SHA256")).upper() != "SHA256":
        errors.append("seres-sign currently supports only algorithm: SHA256")
    if selected == "seres-sign":
        signature = settings.get("signature") if isinstance(settings.get("signature"), dict) else {}
        parameters = signature.get("parameters") if isinstance(signature.get("parameters"), dict) else {}
        check_env(
            parameters.get("secret_key_env", signature.get("secret_key_env", settings.get("secret_key_env"))),
            "SECRET_KEY",
            "secret_key_env",
        )
        check_env(
            parameters.get("access_key_env", signature.get("access_key_env", settings.get("access_key_env"))),
            "ACCESS_KEY",
            "access_key_env",
        )
        extra_headers = settings.get("extra_headers") if isinstance(settings.get("extra_headers"), dict) else {}
        for header, value in extra_headers.items():
            check_env(value.get("env") if isinstance(value, dict) else value, "", f"extra header {header} env")
    elif selected in {"bearer", "bearer-token", "token", "oauth2", "api-key", "apikey"}:
        check_env(
            settings.get("token_env"),
            "API_KEY" if selected in {"api-key", "apikey"} else "ACCESS_TOKEN",
            "token_env",
        )
    elif selected == "cookie":
        check_env(settings.get("cookie_env", settings.get("token_env")), "SESSION_COOKIE", "cookie_env")
    if selected in {"custom", "custom-headers", "headers"}:
        headers = settings.get("headers") if isinstance(settings.get("headers"), dict) else {}
        if not headers:
            errors.append("custom request authentication mode requires modes.custom.headers")
        for header, value in headers.items():
            check_env(value.get("env") if isinstance(value, dict) else value, "", f"custom header {header} env")
    return errors


def auth_markers(config: Any) -> tuple[str, ...]:
    if not isinstance(config, dict):
        return SIGNING_MARKERS
    mode_value = config.get("mode")
    if not isinstance(mode_value, str) or not mode_value.strip():
        modes = config.get("modes") if isinstance(config.get("modes"), dict) else {}
        candidates = [canonical_mode(name) for name, value in modes.items() if isinstance(value, dict) and value.get("enabled") is True]
        candidates.extend(canonical_mode(name) for name, value in config.items() if name not in {"modes", "version", "base_url_env"} and value is True)
        # A present but empty/disabled config means no authentication. The
        # legacy signing markers are reserved for a completely missing config
        # (handled by auth_markers(None)).
        mode_value = next(iter(dict.fromkeys(candidates)), "none")
    mode = canonical_mode(mode_value)
    if config.get(str(mode_value)) is False or config.get(mode) is False:
        return ()
    settings = auth_mode_settings(config, mode)
    if settings.get("enabled") is False:
        return ()
    if mode in {"none", "disabled"}:
        return ()
    if mode in {"bearer", "bearer-token", "token", "oauth2"}:
        return ("script:pre-request", str(settings.get("token_env", "ACCESS_TOKEN")), str(settings.get("header", "Authorization")))
    if mode == "cookie":
        return ("script:pre-request", str(settings.get("cookie_env", settings.get("token_env", "SESSION_COOKIE"))), str(settings.get("header", "Cookie")))
    if mode in {"api-key", "apikey"}:
        return ("script:pre-request", str(settings.get("token_env", "API_KEY")), str(settings.get("header", "X-API-Key")))
    if mode in {"headers", "custom", "custom-headers"}:
        headers = settings.get("headers") if isinstance(settings.get("headers"), dict) else {}
        values = [str(value.get("env")) if isinstance(value, dict) else str(value) for value in headers.values()]
        return ("script:pre-request", *values)
    if mode == "seres-sign":
        signature = settings.get("signature") if isinstance(settings.get("signature"), dict) else {}
        parameter_config = signature.get("parameters") if isinstance(signature.get("parameters"), dict) else {}
        signature_headers = settings.get("headers") if isinstance(settings.get("headers"), dict) else {}
        extra_headers = settings.get("extra_headers") if isinstance(settings.get("extra_headers"), dict) else {}
        return (
            "script:pre-request",
            str(parameter_config.get("secret_key_env", signature.get("secret_key_env", settings.get("secret_key_env", "SECRET_KEY")))),
            str(parameter_config.get("access_key_env", signature.get("access_key_env", settings.get("access_key_env", "ACCESS_KEY")))),
            str(settings.get("algorithm", "SHA256")).upper(),
            str(parameter_config.get("timestamp", signature.get("timestamp_parameter", "timestamp"))),
            *[str(value) for value in signature_headers.values()],
            *[str(value.get("env")) if isinstance(value, dict) else str(value) for value in extra_headers.values()],
        )
    return (
        "script:pre-request",
        str(settings.get("secret_key_env", "SECRET_KEY")),
        str(settings.get("access_key_env", "ACCESS_KEY")),
        "SHA256",
        "timestamp",
        "accesskey",
    )


def iter_integrity_files(*roots: Path):
    """Yield scoped text artifacts without following repository metadata."""

    seen: set[Path] = set()
    for root in roots:
        if root.is_file():
            candidates = [root]
        elif root.is_dir():
            candidates = root.rglob("*")
        else:
            continue
        for path in candidates:
            if not path.is_file() or path.suffix.lower() not in INTEGRITY_SUFFIXES:
                continue
            if ".git" in path.parts or path.resolve() in seen:
                continue
            seen.add(path.resolve())
            yield path


def text_integrity_errors(*roots: Path) -> list[str]:
    """Reject replacement characters, decode failures, and common mojibake."""

    errors: list[str] = []
    for path in iter_integrity_files(*roots):
        raw = b""
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            line_no = raw[:exc.start].count(b"\n") + 1
            errors.append(f"UTF-8 integrity failure {path}:{line_no}: {exc}")
            continue
        except OSError as exc:
            errors.append(f"UTF-8 integrity failure {path}: {exc}")
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            match = MOJIBAKE_RE.search(line)
            if match:
                errors.append(f"UTF-8 integrity failure {path}:{line_no}: {match.group(0)!r}")
    return errors


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def case_fingerprint(case: dict[str, Any]) -> str:
    """Fingerprint the observable request/scenario/assertion contract."""

    payload = {
        "endpoint_id": case.get("endpoint_id"),
        "scenario": case.get("scenario", case.get("scenarios")),
        "request": case.get("request"),
        "assertions": case.get("assertions"),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def case_covers_scenario(case: dict[str, Any], category: str) -> bool:
    direct = str(case.get("scenario") or case.get("category") or "").strip().lower()
    if direct == category:
        return True
    scenarios = case.get("scenarios")
    return (
        isinstance(scenarios, dict)
        and isinstance(scenarios.get(category), dict)
        and scenarios[category].get("applicable") is True
    )


def exclusion_status(item: dict[str, Any]) -> str:
    return str(item.get("status", "approved")).strip().lower() or "approved"


def is_approved_exclusion(item: dict[str, Any]) -> bool:
    return exclusion_status(item) == "approved" and bool(str(item.get("reason", "")).strip())


def ids(items: list[dict[str, Any]]) -> set[str]:
    return {str(item["id"]) for item in items if item.get("id")}


def flow_required(endpoints_doc: Any, endpoints: list[dict[str, Any]]) -> bool:
    """Require an ordered flow only when the manifest explicitly opts in."""

    if isinstance(endpoints_doc, dict) and endpoints_doc.get("flow_required") is True:
        return True
    return any(
        endpoint.get("flow_required") is True or bool(str(endpoint.get("flow_kind", "")).strip())
        for endpoint in endpoints
    )


def flow_execution_errors(flow_items: list[dict[str, Any]], results: dict[str, Any]) -> list[str]:
    """Require a minimal execution record for every declared module flow.

    The dedicated flow validator still checks captures and cleanup shape in
    detail. The coverage gate must at least reject missing or failed flow
    evidence before it can claim a verified collection.
    """

    actual_flows = results.get("flows") if isinstance(results.get("flows"), dict) else {}
    errors: list[str] = []
    for flow in flow_items:
        flow_id = str(flow.get("id", ""))
        actual = actual_flows.get(flow_id)
        if not isinstance(actual, dict):
            errors.append(f"flow {flow_id} has no execution evidence")
            continue
        if actual.get("status") != "passed":
            errors.append(f"flow {flow_id} status is {actual.get('status')}")
        expected_ids = [step.get("case_id") for step in flow.get("steps", []) if isinstance(step, dict)]
        actual_ids = [step.get("case_id") for step in actual.get("steps", []) if isinstance(step, dict)]
        if actual_ids != expected_ids:
            errors.append(f"flow {flow_id} executed order {actual_ids}, expected {expected_ids}")
        creates = any(
            isinstance(step, dict) and str(step.get("operation", "")).lower() == "create"
            for step in flow.get("steps", [])
        )
        if creates and not actual.get("cleanup_verified") and not str(flow.get("cleanup", "")).strip():
            errors.append(f"flow {flow_id} has no verified cleanup evidence")
    return errors


def bruno_assertion_paths(json_path: Any) -> set[str]:
    """Return accepted Bruno expressions for a JSON, text, header, or cookie assertion."""

    value = str(json_path or "")
    if isinstance(json_path, dict):
        target = str(json_path.get("target") or json_path.get("kind") or "").strip().lower()
        value = str(json_path.get("path") or "")
        if target in {"header", "response.header", "headers", "response.headers"}:
            name = value.removeprefix("$response.headers.").removeprefix("$.headers.")
            return {f"res.headers.{name}", f"res.headers['{name}']"} if name else set()
        if target in {"cookie", "response.cookie", "cookies", "response.cookies"}:
            name = value.removeprefix("$response.cookies.").removeprefix("$.cookies.")
            return {f"res.cookies.{name}", f"res.cookies['{name}']"} if name else set()
        if target in {"text", "body_text", "response.body", "raw", "xml", "binary", "file"}:
            return {"res.body"}
    if value == "$":
        return {"res.body"}
    if value.startswith("$."):
        return {"res.body" + value[1:]}
    if value.startswith("$response.headers."):
        header = value[len("$response.headers."):]
        return {f"res.headers['{header}']", f"res.headers.{header}"}
    return set()


def module_directory_name(module: Any, module_id: str) -> str:
    value = module.get("directory", module_id) if isinstance(module, dict) else module_id
    directory = str(value).strip()
    if not directory or directory in {".", ".."} or Path(directory).name != directory:
        raise ValueError(f"module {module_id} directory must be one safe path segment")
    return directory


def module_dirs(contracts_root: Path, module_map: Any = None) -> list[tuple[str, Path]]:
    if (contracts_root / "endpoints.yaml").is_file():
        return [(contracts_root.name, contracts_root)]
    modules_root = contracts_root / "modules"
    if not modules_root.is_dir():
        modules_root = contracts_root
    directory_to_id = {}
    if isinstance(module_map, dict) and isinstance(module_map.get("modules"), list):
        for module in module_map["modules"]:
            if isinstance(module, dict) and module.get("id"):
                module_id = str(module["id"])
                try:
                    directory_to_id[module_directory_name(module, module_id)] = module_id
                except ValueError:
                    continue
    found = [
        (directory_to_id.get(path.name, path.name), path)
        for path in sorted(modules_root.iterdir())
        if path.is_dir() and (path / "endpoints.yaml").is_file()
    ]
    if not found:
        raise SystemExit(f"no module endpoints.yaml files found below {contracts_root}")
    return found


def module_tag_from_document(document: Any, module_id: str) -> str | None:
    if not isinstance(document, dict):
        return None
    modules = document.get("modules")
    if not isinstance(modules, list):
        return None
    for module in modules:
        if isinstance(module, dict) and str(module.get("id")) == module_id:
            tags = module.get("swagger_tags")
            if isinstance(tags, list) and len(tags) == 1 and isinstance(tags[0], str) and tags[0].strip():
                return tags[0].strip()
            return str(module.get("tag") or module.get("name") or module_id).strip()
    return None


def fallback_module_for_endpoint(endpoint: dict[str, Any], modules: list[dict[str, Any]]) -> str | None:
    operation_id = str(endpoint.get("operation_id") or "")
    path = str(endpoint.get("path") or "")
    matches: list[str] = []
    for module in modules:
        if not isinstance(module, dict) or not module.get("id"):
            continue
        ids = module.get("operation_ids", [])
        prefixes = module.get("path_prefixes", [])
        ids = [ids] if isinstance(ids, str) else ids
        prefixes = [prefixes] if isinstance(prefixes, str) else prefixes
        if operation_id and operation_id in {str(item) for item in ids if item is not None}:
            matches.append(str(module["id"]))
        elif any(path == str(prefix) or path.startswith(str(prefix).rstrip("/") + "/") for prefix in prefixes):
            matches.append(str(module["id"]))
    if len(matches) == 1:
        return matches[0]
    defaults = [str(module["id"]) for module in modules if isinstance(module, dict) and module.get("id") and module.get("default") is True]
    if len(defaults) == 1:
        return defaults[0]
    if len(modules) == 1 and isinstance(modules[0], dict) and modules[0].get("id"):
        return str(modules[0]["id"])
    return None


def validate_tag_partition(
    module_map: Any,
    endpoint_records: list[tuple[str, dict[str, Any]]],
    offline_endpoints: list[dict[str, Any]],
) -> list[str]:
    """Validate Tag ownership, or the explicit fallback owner for untagged operations."""

    errors: list[str] = []
    # Keep this initialized even when module-map.yaml is absent or malformed.
    # Untagged operations still need a deterministic error instead of leaking
    # an UnboundLocalError while the checker is constructing that error.
    modules: list[dict[str, Any]] = []
    map_tags: dict[str, str] = {}
    module_to_tag: dict[str, str] = {}
    module_ids: set[str] = set()
    if isinstance(module_map, dict):
        modules = module_map.get("modules", [])
        if not isinstance(modules, list):
            errors.append("module-map.yaml modules must be a list")
            modules = []
        else:
            for module in modules:
                if not isinstance(module, dict):
                    errors.append("module-map.yaml contains a non-object module")
                    continue
                module_id = str(module.get("id", ""))
                if module_id in module_ids:
                    errors.append(f"module-map.yaml repeats module id {module_id}")
                module_ids.add(module_id)
                if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", module_id):
                    errors.append(f"module id {module_id} is not a stable ASCII slug")
                tags = module.get("swagger_tags")
                if tags is not None and (
                    not isinstance(tags, list)
                    or len(tags) > 1
                    or any(not isinstance(tag, str) or not tag.strip() for tag in tags)
                ):
                    errors.append(f"module {module_id} swagger_tags must contain zero or one non-empty value")
                    continue
                if isinstance(tags, list) and len(tags) == 1:
                    tag = tags[0].strip()
                    if "name" in module and module.get("name") != tag:
                        errors.append(f"module {module_id} name does not preserve Swagger tag {tag!r}")
                    if tag in map_tags and map_tags[tag] != module_id:
                        errors.append(f"Swagger tag {tag!r} is assigned to modules {map_tags[tag]} and {module_id}")
                    map_tags[tag] = module_id
                    module_to_tag[module_id] = tag
                else:
                    module_to_tag[module_id] = str(module.get("tag") or module.get("name") or module_id).strip()
    elif endpoint_records:
        errors.append("module-map.yaml is required for strict Swagger-tag partitioning")

    offline_by_id = {str(item.get("id")): item for item in offline_endpoints if item.get("id")}
    offline_tags = {
        str(tag).strip()
        for item in offline_endpoints
        for tag in (item.get("tags") or [])
        if isinstance(tag, str) and tag.strip()
    }
    errors.extend(
        f"Swagger tag {tag!r} is not assigned to a module"
        for tag in sorted(offline_tags - set(map_tags))
    )
    errors.extend(
        f"module-map.yaml tag {tag!r} is not present in OpenAPI"
        for tag in sorted(set(map_tags) - offline_tags)
    )
    for module_id, endpoint in endpoint_records:
        endpoint_id = str(endpoint.get("id", ""))
        offline = offline_by_id.get(endpoint_id)
        tags = endpoint.get("tags")
        if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag.strip() for tag in tags):
            errors.append(f"endpoint {endpoint_id} has invalid Swagger tags")
            continue
        tags = list(dict.fromkeys(tag.strip() for tag in tags))
        source_tags = offline.get("tags", []) if offline else tags
        if not source_tags:
            owner = fallback_module_for_endpoint(endpoint, modules if isinstance(module_map, dict) else [])
            if owner != module_id:
                errors.append(
                    f"untagged operation {endpoint.get('method')} {endpoint.get('path')} belongs to module "
                    f"{owner or '<unmapped>'}, not {module_id}"
                )
        elif set(tags) != set(str(tag).strip() for tag in source_tags):
            errors.append(f"endpoint {endpoint_id} tags do not match offline OpenAPI tags")
        primary = endpoint.get("primary_tag") or endpoint.get("swagger_tag")
        if len(tags) == 1:
            primary = primary or tags[0]
        if not isinstance(primary, str) or not primary.strip():
            errors.append(f"endpoint {endpoint_id} has no unique primary_tag")
            continue
        primary = primary.strip()
        if source_tags and primary not in tags:
            errors.append(f"endpoint {endpoint_id} primary_tag {primary!r} is not declared in tags")
        owner = map_tags.get(primary) if source_tags else module_id
        if not source_tags:
            owner = fallback_module_for_endpoint(endpoint, modules if isinstance(module_map, dict) else [])
        if owner != module_id:
            errors.append(f"endpoint {endpoint_id} Tag {primary!r} belongs to module {owner or '<unmapped>'}, not {module_id}")
        module_tag = module_to_tag.get(module_id)
        if module_tag and module_tag != primary:
            errors.append(f"endpoint {endpoint_id} Tag {primary!r} does not match module {module_id} Tag {module_tag!r}")
        if endpoint.get("module") and str(endpoint.get("module")) != module_id:
            errors.append(f"endpoint {endpoint_id} declares module {endpoint.get('module')} but is stored under {module_id}")

    mapped_ids = {module_id for module_id, _ in endpoint_records}
    errors.extend(
        f"module-map.yaml module {module_id} has no endpoints.yaml inventory"
        for module_id in sorted(set(module_to_tag) - mapped_ids)
    )
    return errors


def validate_cross_module_flows(contracts_root: Path, case_modules: dict[str, str]) -> list[str]:
    """Keep journeys spanning Tags in the coordinator-owned flow manifest."""

    path = contracts_root / "flows" / "cross-module.yaml"
    if not path.is_file():
        return []
    try:
        document = load_data(path)
    except SystemExit as exc:
        return [str(exc)]
    errors: list[str] = []
    for flow in first_list(document, "flows"):
        flow_id = str(flow.get("id", ""))
        modules = set()
        for step in flow.get("steps", []):
            case_id = str(step.get("case_id", ""))
            if case_id not in case_modules:
                errors.append(f"cross-module flow {flow_id} references unknown case {case_id}")
            else:
                modules.add(case_modules[case_id])
        if len(modules) < 2:
            errors.append(f"cross-module flow {flow_id} does not span multiple Tag modules")
    return errors


def validate_module_artifact(
    path: Path,
    module_id: str,
    module_tag: str | None,
    endpoint_ids: set[str],
    collection_key: str,
) -> list[str]:
    if not path.is_file():
        return [f"module {module_id} is missing {path.name}"]
    document = load_data(path)
    errors: list[str] = []
    if isinstance(document, dict) and document.get("module") and str(document.get("module")) != module_id:
        errors.append(f"{path.name} declares module {document.get('module')} but is stored under {module_id}")
    if module_tag and isinstance(document, dict) and document.get("swagger_tag") != module_tag:
        errors.append(f"{path.name} Swagger tag does not match module {module_id}")
    for item in first_list(document, collection_key):
        endpoint_id = item.get("endpoint_id")
        if endpoint_id and str(endpoint_id) not in endpoint_ids:
            errors.append(f"{path.name} references endpoint outside module {module_id}: {endpoint_id}")
    return errors


def validate_module_documentation(path: Path, cases: list[dict[str, Any]]) -> list[str]:
    """Require one human-readable Chinese-first documentation block per case."""

    if not path.is_file():
        return [f"module documentation is missing: {path}"]
    try:
        content = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as exc:
        return [f"cannot read module documentation {path}: {exc}"]
    errors: list[str] = []
    for marker in ("业务范围：", "## 模块内容", "## 接口清单", CASE_DOCS_START, CASE_DOCS_END):
        if marker not in content:
            errors.append(f"{path.name} is missing required section marker {marker}")
    documented_ids = re.findall(r"<!-- CASE_START: ([^\s>]+) -->", content)
    declared_ids = {str(case.get("id")) for case in cases if case.get("id")}
    for case_id in sorted(declared_ids):
        if documented_ids.count(case_id) != 1:
            errors.append(f"case {case_id} must have exactly one documentation block in {path.name}")
            continue
        match = re.search(
            rf"<!-- CASE_START: {re.escape(case_id)} -->\s*(.*?)\s*<!-- CASE_END: {re.escape(case_id)} -->",
            content,
            re.DOTALL,
        )
        if match is None:
            errors.append(f"case {case_id} documentation block is not closed correctly in {path.name}")
            continue
        block = match.group(1)
        if not re.search(r"(?m)^#{2,6}\s+\S", block):
            errors.append(f"case {case_id} documentation has no title")
        if not re.search(r"(?m)^简短描述：\s*\S", block):
            errors.append(f"case {case_id} documentation has no short description")
        if not re.search(r"```mermaid\s*\n\s*sequenceDiagram\b", block):
            errors.append(f"case {case_id} documentation has no Mermaid sequenceDiagram swimlane")
        if len(re.findall(r"(?m)^\s*participant\s+", block)) < 2 or "->>" not in block:
            errors.append(f"case {case_id} Mermaid swimlane has insufficient participants or messages")
    for case_id in sorted(set(documented_ids) - declared_ids):
        errors.append(f"{path.name} documents unknown case {case_id}")
    return errors


def validate_contracts_readme(contracts_root: Path, modules: list[tuple[str, Path]]) -> list[str]:
    path = contracts_root / "README.md"
    if not path.is_file():
        return [f"missing contracts module overview: {path}"]
    try:
        content = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as exc:
        return [f"cannot read contracts module overview {path}: {exc}"]
    errors: list[str] = []
    for module_id, _ in modules:
        start_marker = f"<!-- MODULE_START: {module_id} -->"
        end_marker = f"<!-- MODULE_END: {module_id} -->"
        if content.count(start_marker) != 1 or content.count(end_marker) != 1:
            errors.append(f"README.md must contain exactly one overview block for module {module_id}")
            continue
        match = re.search(
            rf"<!-- MODULE_START: {re.escape(module_id)} -->\s*(.*?)\s*<!-- MODULE_END: {re.escape(module_id)} -->",
            content,
            re.DOTALL,
        )
        if match is None:
            errors.append(f"README.md has no overview for module {module_id}")
            continue
        block = match.group(1)
        if not re.search(r"(?m)^- 业务范围：\s*\S", block):
            errors.append(f"README.md module {module_id} has no business scope")
        if not re.search(r"(?m)^- 包含内容：\s*\S", block):
            errors.append(f"README.md module {module_id} has no content summary")
        if "CASES.md" not in block:
            errors.append(f"README.md module {module_id} has no module documentation link")
    return errors


def referenced_components(value: Any) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str):
            match = re.match(r"^#/components/(schemas|parameters|responses)/([^/]+)$", ref)
            if match:
                found.add((match.group(1), match.group(2)))
            match = re.match(r"^#/(parameters|responses)/([^/]+)$", ref)
            if match:
                found.add((match.group(1), match.group(2)))
            match = re.match(r"^#/definitions/([^/]+)$", ref)
            if match:
                found.add(("schemas", match.group(1)))
        for child in value.values():
            found.update(referenced_components(child))
    elif isinstance(value, list):
        for child in value:
            found.update(referenced_components(child))
    return found


def case_files(
    cases: list[dict[str, Any]],
    bru_root: Path,
    require_signing: bool = False,
    required_auth_markers: tuple[str, ...] = SIGNING_MARKERS,
    required_base_url_env: str | None = None,
) -> tuple[set[str], set[Path], dict[str, Path], list[str]]:
    if not bru_root.is_dir():
        return set(), set(), {}, [f"Bruno directory does not exist: {bru_root}"]
    errors: list[str] = []
    known_files = {path.resolve(): path for path in bru_root.rglob("*.bru")}
    contents: dict[Path, str] = {}
    for path in known_files:
        try:
            contents[path] = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError) as exc:
            errors = [f"UTF-8 integrity failure {path}: {exc}"]
            return set(), set(), {}, errors
    if require_signing:
        for path, content in contents.items():
            missing = [marker for marker in required_auth_markers if marker not in content]
            if missing:
                errors.append(
                    f"Bruno file {path} has no complete configured pre-request script; missing {', '.join(missing)}"
                )
            if re.search(r"\burl:\s+https?://(?!\{\{)", content, re.IGNORECASE):
                errors.append(f"Bruno file {path} hard-codes a URL; use the configured environment base URL")
            if required_base_url_env and not re.search(
                rf"\burl:\s*\{{\{{{re.escape(required_base_url_env)}\}}\}}(?:/|\?|\s|$)",
                content,
            ):
                errors.append(
                    f"Bruno file {path} does not use the configured base URL environment "
                    f"{{{{{required_base_url_env}}}}}"
                )
    covered: set[str] = set()
    used_paths: set[Path] = set()
    case_paths: dict[str, Path] = {}
    for case in cases:
        case_id = str(case.get("id", ""))
        if not case_id:
            errors.append("case without id")
            continue
        configured = case.get("bru") or case.get("bru_file")
        matched = None
        if isinstance(configured, str):
            candidate = (bru_root / configured).resolve()
            try:
                candidate.relative_to(bru_root.resolve())
            except ValueError:
                errors.append(f"case {case_id} points outside Bruno directory: {configured}")
            if candidate in known_files:
                matched = candidate
        if matched is None:
            marker = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(case_id)}(?![A-Za-z0-9_])")
            candidates = sorted(
                path for path, content in contents.items()
                if marker.search(content) or marker.search(path.stem)
            )
            if len(candidates) > 1:
                errors.append(
                    f"case {case_id} ambiguously matches .bru files: "
                    + ", ".join(str(item) for item in candidates)
                )
            matched = candidates[0] if candidates else None
        if matched is None:
            errors.append(f"case {case_id} has no matching .bru file")
            continue
        if matched in used_paths:
            errors.append(f"case {case_id} reuses Bruno file already mapped to another case: {matched}")
        used_paths.add(matched)
        case_paths[case_id] = matched
        if "assert {" not in contents[matched]:
            errors.append(f"case {case_id} Bruno file has no assert block: {matched}")
        covered.add(case_id)
        assertions = case.get("assertions")
        if not isinstance(assertions, list) or not any(
            isinstance(item, dict)
            and (
                str(item.get("target") or item.get("kind") or "").strip().lower()
                in {"header", "response.header", "headers", "response.headers", "cookie", "response.cookie", "cookies", "response.cookies", "text", "body_text", "response.body", "raw", "xml", "binary", "file"}
                or str(item.get("path", "")) not in {"$.code", "$.status", "$.http_status"}
            )
            for item in assertions
        ):
            errors.append(f"case {case_id} has no concrete response assertion beyond status/code")
        for assertion in assertions if isinstance(assertions, list) else []:
            if not isinstance(assertion, dict):
                continue
            expected_paths = bruno_assertion_paths(assertion)
            if expected_paths and not any(path_expr in contents[matched] for path_expr in expected_paths):
                errors.append(
                    f"case {case_id} manifest assertion {assertion.get('path')} "
                    "is not represented in its Bruno assert block"
                )
        expected = case.get("expected") if isinstance(case.get("expected"), dict) else {}
        business_code_path = expected.get("business_code_path", "$.code")
        business_expression = next(iter(bruno_assertion_paths({"path": business_code_path})), "res.body.code")
        for field, expression in (("http_status", "res.status"), ("business_code", business_expression)):
            if field not in expected or expected[field] is None:
                continue
            literal = re.escape(str(expected[field]))
            serialized = re.escape(json.dumps(expected[field], ensure_ascii=False, separators=(",", ":")))
            expected_value = rf"(?:{literal}|{serialized}|\"{literal}\"|'{literal}')"
            if not re.search(rf"(?m)^\s*{re.escape(expression)}\s*:\s*eq\s+{expected_value}\s*$", contents[matched]):
                errors.append(
                    f"case {case_id} expected {field}={expected[field]} is not asserted exactly in Bruno"
                )
    errors.extend(f"unregistered Bruno file: {path}" for path in sorted(set(known_files) - used_paths))
    return covered, used_paths, case_paths, errors


def auth_file_errors(
    bru_root: Path,
    required_auth_markers: tuple[str, ...],
    required_base_url_env: str | None,
    excluded_roots: list[Path] | None = None,
) -> list[str]:
    """Check Bruno files outside module-owned roots for the selected mode."""

    if not bru_root.is_dir():
        return [f"Bruno directory does not exist: {bru_root}"]
    excluded = [root.resolve() for root in (excluded_roots or []) if root.is_dir()]
    errors: list[str] = []
    for path in bru_root.rglob("*.bru"):
        resolved = path.resolve()
        if any(root == resolved or root in resolved.parents for root in excluded):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"UTF-8 integrity failure {path}: {exc}")
            continue
        missing = [marker for marker in required_auth_markers if marker not in content]
        if missing:
            errors.append(f"Bruno file {path} has no complete configured pre-request script; missing {', '.join(missing)}")
        if re.search(r"\burl:\s+https?://(?!\{\{)", content, re.IGNORECASE):
            errors.append(f"Bruno file {path} hard-codes a URL; use the configured environment base URL")
        if required_base_url_env and not re.search(
            rf"\burl:\s*\{{\{{{re.escape(required_base_url_env)}\}}\}}(?:/|\?|\s|$)",
            content,
        ):
            errors.append(f"Bruno file {path} does not use the configured base URL environment {{{{{required_base_url_env}}}}}")
    return errors


def manifest_cases(document: Any) -> list[dict[str, Any]]:
    cases = list_at(document, "cases")
    if cases:
        return cases
    return document if isinstance(document, list) else []


def execution_evidence(document: Any) -> dict[str, Any]:
    """Accept normalized evidence or a Bruno JSON report.

    Bruno's top-level result status can remain ``pass`` when an assertion
    failed, so raw reports are considered passed only when every assertion and
    test result is explicitly passed.
    """

    if isinstance(document, dict) and isinstance(document.get("executed"), list):
        executed = [str(item) for item in document.get("executed", []) if str(item)]
        passed = [str(item) for item in document.get("passed", []) if str(item)]
        return {
            **document,
            "executed": list(dict.fromkeys(executed)),
            "passed": list(dict.fromkeys(item for item in passed if item in executed)),
        }
    if isinstance(document, dict):
        reports = [document]
    else:
        reports = document if isinstance(document, list) else []
    items: list[dict[str, Any]] = []
    for report in reports:
        if isinstance(report, dict) and isinstance(report.get("results"), list):
            items.extend(item for item in report["results"] if isinstance(item, dict))
    executed: list[str] = []
    passed: list[str] = []
    for item in items:
        test = item.get("test") if isinstance(item.get("test"), dict) else {}
        filename = str(test.get("filename", ""))
        name = str(item.get("name") or (Path(filename).stem if filename else ""))
        if not name:
            continue
        executed.append(name)
        assertions = item.get("assertionResults")
        tests = item.get("testResults")
        assertion_ok = isinstance(assertions, list) and all(
            isinstance(value, dict) and str(value.get("status", "")).lower() in {"pass", "passed", "success"}
            for value in assertions
        )
        test_ok = isinstance(tests, list) and all(
            isinstance(value, dict) and str(value.get("status", "")).lower() in {"pass", "passed", "success"}
            for value in tests
        )
        has_observations = bool(assertions or tests)
        if (
            str(item.get("status", "")).lower() in {"pass", "passed", "success"}
            and not item.get("error")
            and has_observations
            and assertion_ok
            and test_ok
        ):
            passed.append(name)
    return {
        "executed": list(dict.fromkeys(executed)),
        "passed": list(dict.fromkeys(passed)),
    }


def check_module(
    module_id: str,
    module_dir: Path,
    bru_root: Path,
    results: dict[str, Any] | None,
    require_scenarios: bool = False,
    module_tag: str | None = None,
    strict_bru_modules: bool = False,
    require_signing: bool = False,
    required_auth_markers: tuple[str, ...] = SIGNING_MARKERS,
    bru_module_name: str | None = None,
    required_base_url_env: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    endpoints_doc = load_data(module_dir / "endpoints.yaml")
    cases_doc = load_data(module_dir / "cases.yaml") if (module_dir / "cases.yaml").exists() else endpoints_doc
    logic_doc = load_data(module_dir / "logic.yaml") if (module_dir / "logic.yaml").exists() else {}
    flows_doc = load_data(module_dir / "flows.yaml") if (module_dir / "flows.yaml").exists() else {}
    exclusions_doc = load_data(module_dir / "exclusions.yaml") if (module_dir / "exclusions.yaml").exists() else {}

    endpoints = first_list(endpoints_doc, "endpoints")
    cases = manifest_cases(cases_doc)
    exclusions = list_at(endpoints_doc, "exclusions") + list_at(exclusions_doc, "exclusions")
    excluded_endpoint_ids = {
        str(item.get("endpoint_id"))
        for item in exclusions
        if item.get("endpoint_id") and str(item.get("reason", "")).strip()
    }
    approved_excluded_endpoint_ids = {
        str(item.get("endpoint_id"))
        for item in exclusions
        if item.get("endpoint_id") and is_approved_exclusion(item)
    }
    pending_excluded_endpoint_ids = excluded_endpoint_ids - approved_excluded_endpoint_ids
    endpoint_ids = ids(endpoints)
    case_ids = ids(cases)
    declared_module_tags = {
        str(value).strip()
        for document in (endpoints_doc, cases_doc, logic_doc, flows_doc, exclusions_doc)
        for value in ([document.get("swagger_tag")] if isinstance(document, dict) and document.get("swagger_tag") else [])
        if str(value).strip()
    }
    if module_tag and declared_module_tags and declared_module_tags != {module_tag}:
        errors.append(f"module {module_id} artifacts disagree on Swagger tag: {sorted(declared_module_tags)}")
    if module_tag and not declared_module_tags:
        errors.append(f"module {module_id} does not declare its Swagger tag")
    if not endpoint_ids:
        errors.append("endpoints.yaml has no endpoints")
    if not case_ids and endpoint_ids - approved_excluded_endpoint_ids:
        errors.append("cases.yaml has no cases")
    errors.extend(validate_module_documentation(module_dir / "CASES.md", cases))
    if module_tag:
        errors.extend(
            validate_module_artifact(
                module_dir / "parameters.yaml",
                module_id,
                module_tag,
                endpoint_ids,
                "parameters",
            )
        )
        parameter_doc = load_data(module_dir / "parameters.yaml") if (module_dir / "parameters.yaml").is_file() else {}
        definition_doc = load_data(module_dir / "definitions.yaml") if (module_dir / "definitions.yaml").is_file() else {}
        response_doc = load_data(module_dir / "responses.yaml") if (module_dir / "responses.yaml").is_file() else {}
        local_components_by_kind = {
            "schemas": set(definition_doc.get("definitions", {}).keys()) if isinstance(definition_doc, dict) and isinstance(definition_doc.get("definitions"), dict) else set(),
            "parameters": set(parameter_doc.get("definitions", {}).keys()) if isinstance(parameter_doc, dict) and isinstance(parameter_doc.get("definitions"), dict) else set(),
            "responses": set(response_doc.get("definitions", {}).keys()) if isinstance(response_doc, dict) and isinstance(response_doc.get("definitions"), dict) else set(),
        }
        for endpoint in endpoints:
            for kind, name in referenced_components(endpoint).copy():
                if name not in local_components_by_kind.get(kind, set()):
                    errors.append(f"endpoint {endpoint.get('id')} references non-local {kind} definition {name}")
        errors.extend(
            validate_module_artifact(
                module_dir / "responses.yaml",
                module_id,
                module_tag,
                endpoint_ids,
                "responses",
            )
        )
        errors.extend(
            validate_module_artifact(
                module_dir / "definitions.yaml",
                module_id,
                module_tag,
                endpoint_ids,
                "definitions",
            )
        )

    cases_by_id = {str(case["id"]): case for case in cases if case.get("id")}
    fingerprint_ids: dict[str, str] = {}
    duplicate_fingerprints: set[str] = set()
    for case in cases:
        case_id = str(case.get("id", ""))
        if not case_id:
            continue
        fingerprint = case_fingerprint(case)
        previous = fingerprint_ids.get(fingerprint)
        if previous and previous != case_id and fingerprint not in duplicate_fingerprints:
            errors.append(f"cases {previous} and {case_id} duplicate endpoint/scenario/request/assertions")
            duplicate_fingerprints.add(fingerprint)
        fingerprint_ids[fingerprint] = case_id
    for case in cases:
        if not str(case.get("endpoint_id", "")).strip():
            errors.append(f"case {case.get('id')} has no endpoint_id")
        expected = case.get("expected") if isinstance(case.get("expected"), dict) else {}
        if expected.get("http_status") is None:
            errors.append(f"case {case.get('id')} has no expected.http_status")
        endpoint_id = case.get("endpoint_id")
        if endpoint_id and endpoint_id not in endpoint_ids:
            errors.append(f"case {case.get('id')} points to unknown endpoint {endpoint_id}")
        if module_tag and case.get("swagger_tag") and str(case.get("swagger_tag")).strip() != module_tag:
            errors.append(f"case {case.get('id')} Swagger tag does not match module {module_id}")
    for endpoint in endpoints:
        endpoint_cases = endpoint.get("case_ids", [])
        if not endpoint_cases:
            endpoint_cases = [item.get("id") for item in list_at(endpoint, "cases")]
        if not endpoint_cases and str(endpoint.get("id")) not in approved_excluded_endpoint_ids:
            errors.append(f"endpoint {endpoint.get('id')} has no case_ids")
        for case_id in endpoint_cases:
            case = cases_by_id.get(str(case_id))
            if case is None:
                errors.append(f"endpoint {endpoint.get('id')} references unknown case {case_id}")
            elif case.get("endpoint_id") and case.get("endpoint_id") != endpoint.get("id"):
                errors.append(f"case {case_id} points to endpoint {case.get('endpoint_id')}, not {endpoint.get('id')}")

        endpoint_case_objects = [
            cases_by_id[str(case_id)]
            for case_id in endpoint_cases
            if str(case_id) in cases_by_id
        ]
        if require_scenarios and str(endpoint.get("id")) not in approved_excluded_endpoint_ids:
            endpoint_decisions = endpoint.get("scenario_matrix") or endpoint.get("scenarios")
            for category in SCENARIO_CATEGORIES:
                decisions: list[dict[str, Any]] = []
                if isinstance(endpoint_decisions, dict) and isinstance(endpoint_decisions.get(category), dict):
                    decisions.append(endpoint_decisions[category])
                for case in endpoint_case_objects:
                    scenarios = case.get("scenarios")
                    if isinstance(scenarios, dict) and isinstance(scenarios.get(category), dict):
                        decisions.append(scenarios[category])
                if not decisions:
                    errors.append(f"endpoint {endpoint.get('id')} has no scenario decision for {category}")
                    continue
                applicable = [item for item in decisions if item.get("applicable") is True]
                inapplicable = [item for item in decisions if item.get("applicable") is False]
                if not applicable and not inapplicable:
                    errors.append(f"endpoint {endpoint.get('id')} has invalid scenario decision for {category}")
                if inapplicable and any(not str(item.get("reason", "")).strip() for item in inapplicable):
                    errors.append(f"endpoint {endpoint.get('id')} scenario {category}=false has no reason")
                if applicable and not any(case_covers_scenario(case, category) for case in endpoint_case_objects):
                    errors.append(
                        f"endpoint {endpoint.get('id')} scenario {category}=true has no dedicated linked case"
                    )

        success_decision = None
        endpoint_decisions = endpoint.get("scenario_matrix") or endpoint.get("scenarios")
        if isinstance(endpoint_decisions, dict) and isinstance(endpoint_decisions.get("success"), dict):
            success_decision = endpoint_decisions["success"]
        success_cases = [case for case in endpoint_case_objects if case_covers_scenario(case, "success")]
        if require_scenarios and (
            str(endpoint.get("id")) not in approved_excluded_endpoint_ids
            and (success_decision is None or success_decision.get("applicable") is not False)
            and not success_cases
        ):
            errors.append(f"endpoint {endpoint.get('id')} has no success case")

    bruno_name = bru_module_name or module_id
    module_bru_root = bru_root / bruno_name if strict_bru_modules else (
        bru_root / bruno_name if (bru_root / bruno_name).is_dir() else bru_root
    )
    covered_cases, _, case_paths, file_errors = case_files(
        cases,
        module_bru_root,
        require_signing=require_signing,
        required_auth_markers=required_auth_markers,
        required_base_url_env=required_base_url_env,
    )
    errors.extend(file_errors)

    # A query parameter whose schema is an object must be represented as the
    # framework expects (usually flattened fields), not as one stringified
    # variable. The latter is a common source of false 400s in generated tests.
    endpoints_by_id = {str(item.get("id")): item for item in endpoints}
    for case in cases:
        case_id = str(case.get("id", ""))
        endpoint = endpoints_by_id.get(str(case.get("endpoint_id")))
        path = case_paths.get(case_id)
        if not endpoint or not path:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"UTF-8 integrity failure {path}: {exc}")
            continue
        for parameter in endpoint.get("parameters", []):
            if not isinstance(parameter, dict) or parameter.get("in") != "query":
                continue
            schema = parameter.get("schema")
            if not isinstance(schema, dict) or not (schema.get("$ref") or schema.get("properties")):
                continue
            name = re.escape(str(parameter.get("name", "")))
            if name and re.search(rf"[?&]{name}=\{{\{{[^}}]+\}}\}}", content):
                errors.append(
                    f"case {case_id} stringifies object query parameter {parameter.get('name')}; "
                    "flatten fields or document supported serialization"
                )

    logic_items = first_list(logic_doc, "logic") or (logic_doc if isinstance(logic_doc, list) else [])
    for logic in logic_items:
        if module_tag and logic.get("swagger_tag") and str(logic.get("swagger_tag")).strip() != module_tag:
            errors.append(f"logic {logic.get('id')} Swagger tag does not match module {module_id}")
        if not logic.get("case_ids"):
            errors.append(f"logic {logic.get('id')} has no linked case_ids")
        for case_id in logic.get("case_ids", []):
            if case_id not in case_ids:
                errors.append(f"logic {logic.get('id')} references unknown case {case_id}")

    flow_items = first_list(flows_doc, "flows") or (flows_doc if isinstance(flows_doc, list) else [])
    requires_ordered_flow = flow_required(endpoints_doc, endpoints)
    flow_exclusions = [
        item for item in exclusions
        if str(item.get("kind", item.get("type", ""))).lower() in {"flow", "cleanup"}
        or item.get("flow_id")
    ]
    approved_flow_exclusion = any(
        is_approved_exclusion(item) and str(item.get("cleanup_plan", item.get("reset_procedure", ""))).strip()
        for item in flow_exclusions
    )
    if not flow_items and requires_ordered_flow:
        if not flow_exclusions:
            errors.append("module declares flow_required operations but flows.yaml declares no flow or exclusion")
        elif any(is_approved_exclusion(item) for item in flow_exclusions) and not approved_flow_exclusion:
            errors.append("approved flow exclusion must include a cleanup/reset plan")
    for flow in flow_items:
        captures: set[str] = set()
        seen_steps: set[str] = set()
        for index, step in enumerate(flow.get("steps", []), start=1):
            case_id = step.get("case_id")
            if case_id in seen_steps:
                errors.append(f"flow {flow.get('id')} repeats case {case_id} at step {index}")
            seen_steps.add(str(case_id))
            if case_id not in case_ids:
                errors.append(f"flow {flow.get('id')} step {index} references unknown case {case_id}")
            uses = [step["uses"]] if isinstance(step.get("uses"), str) else step.get("uses", [])
            for used in uses:
                if used not in captures:
                    errors.append(f"flow {flow.get('id')} step {index} uses uncaptured value {used}")
            captured = step.get("capture")
            captures.update([captured] if isinstance(captured, str) else [str(item) for item in captured or []])

    for exclusion in exclusions:
        endpoint_id = exclusion.get("endpoint_id")
        if endpoint_id and str(endpoint_id) not in endpoint_ids:
            errors.append(f"exclusion references unknown endpoint {endpoint_id}")
    for exclusion in exclusions:
        if not str(exclusion.get("reason", "")).strip():
            errors.append(f"exclusion {exclusion.get('method')} {exclusion.get('path')} has no reason")

    if results is not None:
        executed = set(results.get("executed", []))
        passed = set(results.get("passed", executed))
        required_case_ids = {
            case_id for case_id, case in cases_by_id.items()
            if str(case.get("endpoint_id")) not in approved_excluded_endpoint_ids
        }
        errors.extend(f"case {case_id} was not executed" for case_id in sorted(required_case_ids - executed))
        errors.extend(f"case {case_id} failed" for case_id in sorted(required_case_ids & executed - passed))
        if flow_items:
            errors.extend(flow_execution_errors(flow_items, results))

    endpoints_with_cases = 0
    for endpoint in endpoints:
        endpoint_case_ids = [
            case_id for case_id in (endpoint.get("case_ids") or [item.get("id") for item in list_at(endpoint, "cases")])
            if str(case_id) in cases_by_id
        ]
        if endpoint_case_ids or any(
            str(case.get("endpoint_id")) == str(endpoint.get("id")) for case in cases
        ):
            endpoints_with_cases += 1
    happy_path_endpoints = sum(
        any(
            case_covers_scenario(case, "success")
            for case in cases
            if str(case.get("endpoint_id")) == str(endpoint.get("id"))
        )
        for endpoint in endpoints
        if str(endpoint.get("id")) not in approved_excluded_endpoint_ids
    )
    required_success_endpoints = sum(
        str(endpoint.get("id")) not in approved_excluded_endpoint_ids for endpoint in endpoints
    )
    success_case_count = sum(
        1
        for case in cases
        if case_covers_scenario(case, "success")
    )
    verified_flows = 0
    if results and isinstance(results.get("flows"), dict):
        verified_flows = sum(
            isinstance(value, dict) and value.get("status") == "passed"
            for value in results["flows"].values()
        )
    return {
        "endpoints": len(endpoint_ids),
        "inventory_endpoints": len(endpoint_ids),
        "endpoints_with_cases": endpoints_with_cases,
        "excluded_endpoints": len(excluded_endpoint_ids),
        "pending_exclusions": len(pending_excluded_endpoint_ids),
        "generated_cases": len(case_ids),
        "cases": len(case_ids),
        "bru_cases": len(covered_cases),
        "executed_cases": len(set(results.get("executed", []))) if results else 0,
        "passed_cases": len(set(results.get("passed", []))) if results else 0,
        "happy_path_endpoints": happy_path_endpoints,
        "pending_success_endpoints": max(required_success_endpoints - happy_path_endpoints, 0),
        "verified_flows": verified_flows,
        "registered_cases": len(case_ids),
        "successful_cases": len(
            {
                case_id
                for case_id in (set(results.get("passed", [])) if results else set())
                if case_id in case_ids
            }
        ),
        "success_cases": success_case_count,
        "swagger_tag": module_tag,
    }, errors


def sync_global_index(path: Path, status: str, totals: dict[str, int], module_reports: dict[str, dict[str, Any]]) -> None:
    """Keep the generated index counts and state aligned with reconciliation."""

    if not path.is_file():
        return
    index = load_data(path)
    if not isinstance(index, dict):
        return
    index["generation_status"] = status
    index["inventory_endpoints"] = totals.get("inventory_endpoints", 0)
    index["generated_cases"] = totals.get("generated_cases", 0)
    index["blocked_modules"] = sum(
        value.get("status") == "blocked" for value in module_reports.values()
    )
    by_id = {
        str(item.get("id")): item
        for item in index.get("modules", [])
        if isinstance(item, dict) and item.get("id")
    }
    for module_id, report in module_reports.items():
        entry = by_id.get(module_id)
        if entry is None:
            continue
        entry["endpoint_count"] = report.get("endpoints", 0)
        entry["case_count"] = report.get("cases", 0)
    try:
        import yaml  # type: ignore[import-not-found]

        rendered = yaml.safe_dump(index, allow_unicode=True, sort_keys=False)
    except ModuleNotFoundError:
        rendered = json.dumps(index, ensure_ascii=True, indent=2) + "\n"
    path.write_text(rendered, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("contracts_root", type=Path)
    parser.add_argument("bru_root", type=Path)
    parser.add_argument("--results", type=Path, help="normalized evidence or raw Bruno JSON execution report")
    parser.add_argument(
        "--preflight-results",
        type=Path,
        help="normalized runtime preflight report; a passed report marks the collection runnable",
    )
    parser.add_argument(
        "--openapi",
        type=Path,
        help="offline OpenAPI/Swagger document (defaults to contracts_root/openapi.json when present; required for completion)",
    )
    parser.add_argument(
        "--require-scenarios",
        action="store_true",
        help="require a decision for every case-matrix category on every endpoint",
    )
    parser.add_argument(
        "--require-signing",
        action="store_true",
        help="require the shared SECRET_KEY/ACCESS_KEY pre-request signing script in every Bruno file",
    )
    parser.add_argument(
        "--require-auth",
        action="store_true",
        help="require the pre-request script selected by request-auth.yaml in every Bruno file",
    )
    parser.add_argument(
        "--auth-config",
        type=Path,
        help="request-auth.yaml selecting the expected pre-request mode",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    results = execution_evidence(load_data(args.results)) if args.results else None
    preflight = load_data(args.preflight_results) if args.preflight_results else None
    preflight_ok = (
        isinstance(preflight, dict)
        and preflight.get("status") in {"runnable", "passed", "ok"}
        and not preflight.get("errors")
    )
    auth_config_path = args.auth_config or (args.contracts_root / "request-auth.yaml")
    auth_config = None
    auth_config_load_error: str | None = None
    if auth_config_path.is_file():
        try:
            auth_config = load_data(auth_config_path)
        except SystemExit as exc:
            auth_config_load_error = str(exc)
    required_auth_markers = auth_markers(auth_config)
    auth_config_valid = True
    errors: list[str] = text_integrity_errors(args.contracts_root, args.bru_root)
    if args.results:
        if args.openapi is None:
            errors.append("completion requires explicit --openapi")
        if not args.require_scenarios:
            errors.append("completion requires --require-scenarios")
        if not args.require_auth:
            errors.append("completion requires --require-auth")
        if args.auth_config is None:
            errors.append("completion requires explicit --auth-config")
        if not args.preflight_results:
            errors.append("completion requires --preflight-results")
        elif not preflight_ok:
            errors.append("runtime preflight did not pass")
        if not auth_config_path.is_file():
            errors.append(f"completion requires request authentication config: {auth_config_path}")
    if args.require_auth:
        if not auth_config_path.is_file():
            auth_config_valid = False
            errors.append(f"--require-auth requires request authentication config: {auth_config_path}")
        elif auth_config_load_error:
            auth_config_valid = False
            errors.append(f"invalid request authentication config {auth_config_path}: {auth_config_load_error}")
        else:
            auth_errors = validate_auth_config_document(auth_config)
            auth_config_valid = not auth_errors
            errors.extend(f"invalid request authentication config {auth_config_path}: {error}" for error in auth_errors)
    required_base_url_env = str(auth_config.get("base_url_env", "BASE_URL")) if isinstance(auth_config, dict) else "BASE_URL"
    totals = {
        "endpoints": 0,
        "inventory_endpoints": 0,
        "endpoints_with_cases": 0,
        "excluded_endpoints": 0,
        "pending_exclusions": 0,
        "generated_cases": 0,
        "cases": 0,
        "registered_cases": 0,
        "bru_cases": 0,
        "executed_cases": 0,
        "passed_cases": 0,
        "successful_cases": 0,
        "success_cases": 0,
        "happy_path_endpoints": 0,
        "pending_success_endpoints": 0,
        "verified_flows": 0,
    }
    declared: dict[str, dict[str, Any]] = {}
    module_map_path = args.contracts_root / "module-map.yaml"
    module_map_doc: Any = load_data(module_map_path) if module_map_path.is_file() else None
    modules = module_dirs(args.contracts_root, module_map_doc)
    if (args.require_auth or args.require_signing) and (not args.require_auth or auth_config_valid):
        strict_bru_modules = (args.contracts_root / "modules").is_dir()
        module_bru_roots = []
        for _, module_dir in modules:
            candidate = args.bru_root / module_dir.name
            module_bru_roots.append(candidate if strict_bru_modules or candidate.is_dir() else args.bru_root)
        errors.extend(
            auth_file_errors(
                args.bru_root,
                required_auth_markers,
                required_base_url_env if args.require_auth else None,
                list(dict.fromkeys(module_bru_roots)),
            )
        )
    endpoint_records: list[tuple[str, dict[str, Any]]] = []
    all_case_ids: dict[str, str] = {}
    all_case_fingerprints: dict[str, str] = {}
    required_case_ids_global: set[str] = set()
    all_flow_ids: dict[str, str] = {}
    all_logic_ids: dict[str, str] = {}
    offline_inventory_count: int | None = None
    contract_provenance_unverified = False
    for module_id, module_dir in modules:
        endpoint_doc = load_data(module_dir / "endpoints.yaml")
        for endpoint in first_list(endpoint_doc, "endpoints"):
            if endpoint.get("module") and str(endpoint.get("module")) != module_id:
                errors.append(
                    f"endpoint {endpoint.get('id')} declares module {endpoint.get('module')} "
                    f"but is stored under {module_id}"
                )
            endpoint_records.append((module_id, endpoint))
        case_path = module_dir / "cases.yaml"
        if case_path.is_file():
            module_cases = manifest_cases(load_data(case_path))
        else:
            module_cases = manifest_cases(endpoint_doc)
        if module_cases:
            module_exclusions = list_at(endpoint_doc, "exclusions")
            exclusions_path = module_dir / "exclusions.yaml"
            if exclusions_path.is_file():
                module_exclusions += list_at(load_data(exclusions_path), "exclusions")
            approved_endpoint_ids = {
                str(item.get("endpoint_id"))
                for item in module_exclusions
                if item.get("endpoint_id") and is_approved_exclusion(item)
            }
            for case in module_cases:
                case_id = str(case.get("id", ""))
                if not case_id:
                    continue
                previous = all_case_ids.get(case_id)
                if previous:
                    errors.append(f"case id {case_id} is duplicated in modules {previous} and {module_id}")
                all_case_ids[case_id] = module_id
                fingerprint = case_fingerprint(case)
                previous_fingerprint = all_case_fingerprints.get(fingerprint)
                if previous_fingerprint and previous_fingerprint != case_id:
                    errors.append(
                        f"cases {previous_fingerprint} and {case_id} duplicate endpoint/scenario/request/assertions"
                    )
                all_case_fingerprints[fingerprint] = case_id
                if str(case.get("endpoint_id")) not in approved_endpoint_ids:
                    required_case_ids_global.add(case_id)
        flow_path = module_dir / "flows.yaml"
        if flow_path.is_file():
            flow_doc = load_data(flow_path)
            for flow in first_list(flow_doc, "flows"):
                flow_id = str(flow.get("id", ""))
                if flow_id and flow_id in all_flow_ids:
                    errors.append(f"flow id {flow_id} is duplicated in modules {all_flow_ids[flow_id]} and {module_id}")
                if flow_id:
                    all_flow_ids[flow_id] = module_id
        logic_path = module_dir / "logic.yaml"
        if logic_path.is_file():
            logic_doc = load_data(logic_path)
            for logic in first_list(logic_doc, "logic"):
                logic_id = str(logic.get("id", ""))
                if logic_id and logic_id in all_logic_ids:
                    errors.append(f"logic id {logic_id} is duplicated in modules {all_logic_ids[logic_id]} and {module_id}")
                if logic_id:
                    all_logic_ids[logic_id] = module_id

    errors.extend(validate_cross_module_flows(args.contracts_root, all_case_ids))

    endpoint_ids = [str(endpoint.get("id", "")) for _, endpoint in endpoint_records]
    duplicate_endpoint_ids = sorted({item for item in endpoint_ids if item and endpoint_ids.count(item) > 1})
    errors.extend(
        f"endpoint id {endpoint_id} is duplicated across module manifests"
        for endpoint_id in duplicate_endpoint_ids
    )

    openapi_path = args.openapi
    if openapi_path is None:
        candidate = args.contracts_root / "openapi.json"
        if candidate.is_file():
            openapi_path = candidate
    if args.results and openapi_path is None:
        errors.append("completion requires an offline OpenAPI document; pass --openapi")
    global_contracts = (args.contracts_root / "modules").is_dir()
    if global_contracts:
        errors.extend(validate_contracts_readme(args.contracts_root, modules))
    offline_endpoints: list[dict[str, Any]] = []
    if openapi_path is not None:
        if not openapi_path.is_file():
            errors.append(f"offline OpenAPI document does not exist: {openapi_path}")
        else:
            try:
                offline_document = load_document(openapi_path)
                provenance = offline_document.get("provenance") if isinstance(offline_document, dict) else None
                contract_provenance_unverified = isinstance(provenance, dict) and provenance.get("status") == "contract_provenance_unverified"
                offline = extract(openapi_path, offline_document)
                offline_endpoints = offline["endpoints"]
                offline_inventory_count = len(offline["endpoints"])
                if global_contracts:
                    offline_by_id = {str(item["id"]): item for item in offline["endpoints"]}
                    manifest_by_id = {
                        str(item.get("id")): item
                        for _, item in endpoint_records
                        if item.get("id")
                    }
                    errors.extend(
                        f"offline endpoint {endpoint_id} is not assigned to a module"
                        for endpoint_id in sorted(set(offline_by_id) - set(manifest_by_id))
                    )
                    errors.extend(
                        f"manifest endpoint {endpoint_id} is not present in offline OpenAPI"
                        for endpoint_id in sorted(set(manifest_by_id) - set(offline_by_id))
                    )
                    offline_keys = {
                        (item["method"], item["path"]): str(item["id"])
                        for item in offline["endpoints"]
                    }
                    manifest_keys: dict[tuple[str, str], str] = {}
                    for _, item in endpoint_records:
                        key = (
                            str(item.get("method", "")).upper(),
                            str(item.get("path", "")),
                        )
                        if key in manifest_keys and manifest_keys[key] != str(item.get("id")):
                            errors.append(f"manifest has duplicate endpoint operation {key[0]} {key[1]}")
                        manifest_keys[key] = str(item.get("id"))
                    errors.extend(
                        f"offline operation {method} {path} is not assigned to a module"
                        for method, path in sorted(set(offline_keys) - set(manifest_keys))
                    )
                    errors.extend(
                        f"manifest operation {method} {path} is not present in offline OpenAPI"
                        for method, path in sorted(set(manifest_keys) - set(offline_keys))
                    )
            except (SystemExit, ValueError, TypeError) as exc:
                errors.append(f"cannot reconcile offline OpenAPI {openapi_path}: {exc}")
    if offline_endpoints:
        errors.extend(validate_tag_partition(module_map_doc, endpoint_records, offline_endpoints))
    index_path = args.contracts_root / "index.yaml"
    if not index_path.is_file() and global_contracts:
        errors.append(f"missing global index.yaml: {index_path}")
    if index_path.is_file():
        index = load_data(index_path)
        index_modules = first_list(index, "modules")
        index_ids = [str(item.get("id", "")) for item in index_modules]
        duplicate_index_ids = sorted({item for item in index_ids if item and index_ids.count(item) > 1})
        errors.extend(f"index.yaml repeats module id {module_id}" for module_id in duplicate_index_ids)
        declared = {str(item.get("id")): item for item in index_modules}
        actual = {module_id for module_id, _ in modules}
        errors.extend(f"index.yaml lists missing module {module_id}" for module_id in sorted(set(declared) - actual))
        errors.extend(f"module {module_id} is missing from index.yaml" for module_id in sorted(actual - set(declared)))

    module_reports = {}
    module_tags = {
        module_id: module_tag_from_document(module_map_doc, module_id)
        for module_id, _ in modules
    }
    if isinstance(module_map_doc, dict) and isinstance(module_map_doc.get("modules"), list):
        inverse_tags = {
            str(module.get("id")): (
                str(module.get("swagger_tags", [""])[0]).strip()
                if isinstance(module.get("swagger_tags"), list) and len(module.get("swagger_tags")) == 1
                else str(module.get("tag") or module.get("name") or module.get("id") or "").strip()
            )
            for module in module_map_doc.get("modules", [])
            if isinstance(module, dict)
            and module.get("id")
        }
        module_tags.update(inverse_tags)
    for module_id, module_dir in modules:
        report, module_errors = check_module(
            module_id,
            module_dir,
            args.bru_root,
            results,
            require_scenarios=args.require_scenarios,
            module_tag=module_tags.get(module_id),
            strict_bru_modules=(args.contracts_root / "modules").is_dir(),
            require_signing=args.require_signing or (args.require_auth and auth_config_valid),
            required_auth_markers=required_auth_markers,
            bru_module_name=module_dir.name,
            required_base_url_env=required_base_url_env if args.require_auth else None,
        )
        module_reports[module_id] = report
        for key in totals:
            totals[key] += report[key]
        errors.extend(f"[{module_id}] {error}" for error in module_errors)
        report["status"] = "blocked" if module_errors else (
            "verified" if results else ("runnable" if preflight_ok else "draft")
        )
        if module_id in declared:
            expected = declared[module_id]
            count_keys = {"endpoint_count": "endpoints", "case_count": "cases"}
            for index_key, report_key in count_keys.items():
                if index_key in expected and expected[index_key] != report[report_key]:
                    errors.append(
                        f"[{module_id}] index.yaml {index_key}={expected[index_key]} but actual={report[report_key]}"
                    )

    if offline_inventory_count is not None:
        totals["inventory_endpoints"] = offline_inventory_count
    runtime_error_re = re.compile(r"(?:^|\]) case [^ ]+ (?:was not executed|failed)$")
    errors = list(dict.fromkeys(errors))
    static_errors = [error for error in errors if not runtime_error_re.search(error)]
    if args.results and contract_provenance_unverified:
        errors.append("offline OpenAPI provenance is unverified; completion evidence is blocked")
        errors = list(dict.fromkeys(errors))
    static_ok = not static_errors
    if args.results:
        required_case_ids = required_case_ids_global
        executed = set(results.get("executed", [])) if results else set()
        passed = set(results.get("passed", [])) if results else set()
        unknown_executed = executed - set(all_case_ids)
        errors.extend(f"execution evidence references unknown case {case_id}" for case_id in sorted(unknown_executed))
        completion_ok = static_ok and not errors and required_case_ids.issubset(executed) and required_case_ids.issubset(passed) \
            and not totals["pending_exclusions"]
        status = "verified" if completion_ok else "blocked"
    elif preflight_ok and static_ok:
        completion_ok = False
        status = "runnable"
    elif args.preflight_results and static_ok:
        completion_ok = False
        status = "blocked"
    else:
        completion_ok = False
        status = "draft" if static_ok else "blocked"
    report = {
        **totals,
        "modules": module_reports,
        "blocked_modules": sum(value.get("status") == "blocked" for value in module_reports.values()),
        "errors": errors,
        "static_ok": static_ok,
        "completion_ok": completion_ok,
        "status": status,
        "ok": completion_ok,
        "contract_provenance_unverified": contract_provenance_unverified,
        "by_tag": {
            module_id: {
                "swagger_tag": report.get("swagger_tag"),
                "registered_endpoints": report.get("endpoints", 0),
                "registered_cases": report.get("registered_cases", report.get("cases", 0)),
                "successful_cases": report.get("successful_cases", 0),
                "success_cases": report.get("success_cases", 0),
                "excluded_endpoints": report.get("excluded_endpoints", 0),
                "executed_cases": report.get("executed_cases", 0),
                "passed_cases": report.get("passed_cases", 0),
            }
            for module_id, report in module_reports.items()
        },
    }
    sync_global_index(args.contracts_root / "index.yaml", status, totals, module_reports)
    if args.as_json:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    else:
        print(
            f"status={status} modules={len(modules)} inventory_endpoints={totals['inventory_endpoints']} "
            f"endpoints_with_cases={totals['endpoints_with_cases']} generated_cases={totals['generated_cases']} "
            f"executed_cases={totals['executed_cases']} passed_cases={totals['passed_cases']} "
            f"happy_path_endpoints={totals['happy_path_endpoints']} pending_success_endpoints={totals['pending_success_endpoints']} "
            f"verified_flows={totals['verified_flows']}"
        )
        for error in errors:
            print(f"ERROR: {error}")
        if errors:
            print(f"coverage check failed: {len(errors)} error(s)")
        elif completion_ok:
            print("coverage check passed: verified")
        else:
            print(f"coverage check is static-valid but incomplete: {status}")
    return 0 if static_ok and status != "blocked" and (not args.results or completion_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
