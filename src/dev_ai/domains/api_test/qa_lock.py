#!/usr/bin/env python3
"""Verify or refresh the QA asset lock independently of the business version lock."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

from .manifest_io import load_data


def fingerprint(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def current_contract_state(contracts_root: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Return endpoint and module contract fingerprints from current manifests."""

    endpoints: dict[str, str] = {}
    modules: dict[str, dict[str, str]] = {}
    modules_root = contracts_root / "modules"
    if not modules_root.is_dir():
        return endpoints, {}
    for endpoints_path in sorted(modules_root.glob("*/endpoints.yaml")):
        document = load_data(endpoints_path)
        if not isinstance(document, dict):
            continue
        module_id = str(document.get("module", endpoints_path.parent.name))
        module_endpoints: dict[str, str] = {}
        for endpoint in document.get("endpoints", []):
            if not isinstance(endpoint, dict) or not endpoint.get("id"):
                continue
            endpoint_id = str(endpoint["id"])
            contract = {
                key: value
                for key, value in endpoint.items()
                if key not in {"case_ids", "cases", "scenario_matrix", "scenarios"}
            }
            value = fingerprint(contract)
            endpoints[endpoint_id] = value
            module_endpoints[endpoint_id] = value
        modules[module_id] = fingerprint(module_endpoints)
    return endpoints, modules


def current_case_state(contracts_root: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    modules_root = contracts_root / "modules"
    if not modules_root.is_dir():
        return result
    for cases_path in sorted(modules_root.glob("*/cases.yaml")):
        document = load_data(cases_path)
        module = str(document.get("module", cases_path.parent.name)) if isinstance(document, dict) else cases_path.parent.name
        cases = document.get("cases", []) if isinstance(document, dict) else []
        for case in cases if isinstance(cases, list) else []:
            if not isinstance(case, dict) or not case.get("id"):
                continue
            case_id = str(case["id"])
            contract = {
                key: value
                for key, value in case.items()
                if key not in {"bru", "bru_file", "file_name", "manual_review"}
            }
            result[case_id] = {
                "module": module,
                "endpoint_id": str(case.get("endpoint_id", "")),
                "fingerprint": fingerprint(contract),
                "manual_review": case.get("manual_review") is True,
            }
    return result


def refresh_generation_state_cases(contracts_root: Path) -> None:
    try:
        import yaml  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise ValueError("QA generation state requires PyYAML") from exc
    state_path = contracts_root / "generation-state.yaml"
    state = load_data(state_path)
    if not isinstance(state, dict):
        raise ValueError("generation-state.yaml must contain an object")
    state = dict(state)
    state["cases"] = current_case_state(contracts_root)
    rendered = yaml.safe_dump(state, allow_unicode=True, sort_keys=False)
    if state_path.read_text(encoding="utf-8", errors="strict") != rendered:
        state_path.write_text(rendered, encoding="utf-8")


def expected_lock(contracts_root: Path) -> dict[str, Any]:
    state_path = contracts_root / "generation-state.yaml"
    state = load_data(state_path)
    if not isinstance(state, dict):
        raise ValueError("generation-state.yaml must contain an object")
    modules = state.get("modules", {}) if isinstance(state.get("modules"), dict) else {}
    cases = state.get("cases", {}) if isinstance(state.get("cases"), dict) else {}
    return {
        "version": 1,
        "openapi_sha256": state.get("openapi_sha256"),
        "design_sha256": state.get("design_sha256"),
        "module_fingerprints": {
            key: value.get("contract_fingerprint")
            for key, value in modules.items()
            if isinstance(value, dict)
        },
        "case_fingerprints": {
            key: value.get("fingerprint")
            for key, value in cases.items()
            if isinstance(value, dict)
        },
        "generation_state_fingerprint": fingerprint(state),
    }


def check(contracts_root: Path) -> list[str]:
    lock_path = contracts_root / "qa-lock.yaml"
    if not lock_path.is_file():
        return ["qa-lock.yaml is missing"]
    try:
        actual = load_data(lock_path)
        expected = expected_lock(contracts_root)
    except (OSError, UnicodeDecodeError, ValueError, TypeError) as exc:
        return [f"cannot validate QA lock: {exc}"]
    errors = []
    if actual != expected:
        errors.append("qa-lock.yaml does not match generation-state.yaml")
    state = load_data(contracts_root / "generation-state.yaml")
    if isinstance(state, dict):
        current_endpoints, current_modules = current_contract_state(contracts_root)
        state_endpoints = state.get("endpoints")
        if isinstance(state_endpoints, dict):
            expected_endpoints = {
                str(key): str(value.get("fingerprint"))
                for key, value in state_endpoints.items()
                if isinstance(value, dict) and value.get("fingerprint") is not None
            }
            if expected_endpoints != current_endpoints:
                errors.append("generation-state.yaml endpoint fingerprints do not match current endpoints.yaml assets")
        state_modules = state.get("modules")
        if isinstance(state_modules, dict):
            expected_modules = {
                str(key): str(value.get("contract_fingerprint"))
                for key, value in state_modules.items()
                if isinstance(value, dict) and value.get("contract_fingerprint") is not None
            }
            if expected_modules != current_modules:
                errors.append("generation-state.yaml module fingerprints do not match current endpoints.yaml assets")
    state_cases = state.get("cases", {}) if isinstance(state, dict) and isinstance(state.get("cases"), dict) else {}
    actual_cases = current_case_state(contracts_root)
    if {
        key: value.get("fingerprint") for key, value in state_cases.items() if isinstance(value, dict)
    } != {
        key: value.get("fingerprint") for key, value in actual_cases.items()
    }:
        errors.append("generation-state.yaml case fingerprints do not match current cases.yaml assets")
    openapi = next(
        (
            path
            for path in (
                contracts_root / "openapi.json",
                contracts_root / "openapi.yaml",
                contracts_root / "openapi.yml",
            )
            if path.is_file()
        ),
        None,
    )
    if openapi is not None:
        digest = hashlib.sha256(openapi.read_bytes()).hexdigest()
        if expected.get("openapi_sha256") != digest:
            errors.append(
                f"OpenAPI SHA mismatch: generation state has {expected.get('openapi_sha256')!r}, file has {digest}"
            )
    design_path = contracts_root.parent / "constraints" / "design-rules.yaml"
    if design_path.is_file():
        from .design_rules import summary as design_summary

        current_design = design_summary(load_data(design_path))["sha256"]
        if expected.get("design_sha256") != current_design:
            errors.append(
                f"design SHA mismatch: generation state has {expected.get('design_sha256')!r}, file has {current_design}"
            )
    return errors


def write(contracts_root: Path) -> None:
    try:
        import yaml  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise ValueError("QA lock requires PyYAML") from exc
    path = contracts_root / "qa-lock.yaml"
    rendered = yaml.safe_dump(expected_lock(contracts_root), allow_unicode=True, sort_keys=False)
    if not path.is_file() or path.read_text(encoding="utf-8", errors="strict") != rendered:
        path.write_text(rendered, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("contracts_root", type=Path)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    if args.write:
        write(args.contracts_root)
        print("QA lock synchronized")
        return 0
    errors = check(args.contracts_root)
    for error in errors:
        print(f"ERROR: {error}")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
