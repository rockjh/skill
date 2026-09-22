#!/usr/bin/env python3
"""Validate ordered module-flow execution evidence emitted by Bruno tests."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

from .api_test_manifest_io import first_list, load_data


def as_names(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {str(item) for item in value}
    return set()


def case_module_index(contracts_root: Path) -> dict[str, str]:
    index: dict[str, str] = {}
    modules_root = contracts_root / "modules"
    if not modules_root.is_dir():
        return index
    for module_dir in sorted(path for path in modules_root.iterdir() if path.is_dir()):
        path = module_dir / "cases.yaml"
        if not path.is_file():
            continue
        for case in first_list(load_data(path), "cases"):
            if case.get("id"):
                index[str(case["id"])] = module_dir.name
    return index


def requires_flow(endpoints_doc: Any, endpoints: list[dict[str, Any]]) -> bool:
    """Use explicit manifest intent instead of inferring CRUD from HTTP methods."""

    if isinstance(endpoints_doc, dict) and endpoints_doc.get("flow_required") is True:
        return True
    return any(
        endpoint.get("flow_required") is True or bool(str(endpoint.get("flow_kind", "")).strip())
        for endpoint in endpoints
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("flows", type=Path)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--endpoints", type=Path, help="module endpoints.yaml used to detect explicitly flow-required operations")
    parser.add_argument("--exclusions", type=Path, help="module exclusions.yaml with an approved cleanup plan")
    parser.add_argument(
        "--contracts-root",
        type=Path,
        help="contracts root used to enforce module-local versus cross-module case ownership",
    )
    args = parser.parse_args()
    flows_doc = load_data(args.flows)
    results = load_data(args.results)
    declared = first_list(flows_doc, "flows") or (flows_doc if isinstance(flows_doc, list) else [])
    actual_flows = results.get("flows", {}) if isinstance(results, dict) else {}
    errors: list[str] = []
    case_modules = case_module_index(args.contracts_root) if args.contracts_root else {}
    flow_is_cross_module = args.flows.parent.name == "flows" and args.flows.name == "cross-module.yaml"

    if not declared:
        endpoint_items = []
        if args.endpoints and args.endpoints.is_file():
            endpoint_items = first_list(load_data(args.endpoints), "endpoints")
        has_crud = requires_flow(
            load_data(args.endpoints) if args.endpoints and args.endpoints.is_file() else {},
            endpoint_items,
        )
        exclusions = []
        if args.exclusions and args.exclusions.is_file():
            exclusions = first_list(load_data(args.exclusions), "exclusions")
        approved_cleanup = any(
            (item.get("approved") is True or str(item.get("status", "")).lower() == "approved")
            and str(item.get("reason", "")).strip()
            and str(item.get("cleanup_plan", item.get("reset_procedure", ""))).strip()
            and (str(item.get("kind", item.get("type", ""))).lower() in {"flow", "cleanup"} or item.get("flow_id"))
            for item in exclusions
        )
        if has_crud and not exclusions:
            print("flow execution check failed")
            print("ERROR: module declares flow_required operations but flows.yaml declares no flow or exclusion")
            return 1
        if has_crud and any(
            (item.get("approved") is True or str(item.get("status", "")).lower() == "approved")
            and str(item.get("reason", "")).strip()
            and not str(item.get("cleanup_plan", item.get("reset_procedure", ""))).strip()
            for item in exclusions
        ):
            print("flow execution check failed")
            print("ERROR: approved flow exclusion must include a cleanup/reset plan")
            return 1
        if has_crud and not approved_cleanup:
            print("skipped: no declared flows (pending exclusion)")
            return 0
        print("skipped: no declared flows")
        return 0

    for flow in declared:
        flow_id = str(flow.get("id"))
        expected_steps = flow.get("steps", [])
        actual = actual_flows.get(flow_id) if isinstance(actual_flows, dict) else None
        if not flow.get("id"):
            errors.append("flow without id")
            continue
        if not isinstance(expected_steps, list) or not expected_steps:
            errors.append(f"flow {flow_id} has no steps")
            continue
        if not isinstance(actual, dict):
            errors.append(f"flow {flow_id} has no execution evidence")
            actual = {}
        actual_steps = actual.get("steps", []) if isinstance(actual, dict) else []
        actual_ids = [step.get("case_id") for step in actual_steps if isinstance(step, dict)]
        expected_ids = [step.get("case_id") for step in expected_steps if isinstance(step, dict)]
        if case_modules:
            referenced_modules = {
                case_modules.get(str(case_id))
                for case_id in expected_ids
                if str(case_id) in case_modules
            }
            unknown_cases = sorted(str(case_id) for case_id in expected_ids if str(case_id) not in case_modules)
            errors.extend(
                f"flow {flow_id} references unknown case {case_id}"
                for case_id in unknown_cases
            )
            if flow_is_cross_module:
                if len(referenced_modules) < 2:
                    errors.append(f"cross-module flow {flow_id} does not span multiple Tag modules")
            else:
                inferred_module = args.flows.parent.name
                if any(module != inferred_module for module in referenced_modules):
                    errors.append(
                        f"module flow {flow_id} references another Tag module; move it to flows/cross-module.yaml"
                    )
        if actual_ids != expected_ids:
            errors.append(f"flow {flow_id} executed order {actual_ids}, expected {expected_ids}")
        if actual.get("status") != "passed":
            errors.append(f"flow {flow_id} status is {actual.get('status')}")

        captured: set[str] = set()
        for index, expected in enumerate(expected_steps):
            if index >= len(actual_steps):
                errors.append(f"flow {flow_id} is missing execution step {index + 1}")
                continue
            observed = actual_steps[index]
            if observed.get("status") != "passed":
                errors.append(f"flow {flow_id} step {index + 1} did not pass")
            expected_uses = as_names(expected.get("uses"))
            observed_uses = as_names(observed.get("used_captures"))
            if not expected_uses.issubset(captured):
                errors.append(f"flow {flow_id} step {index + 1} uses values before capture: {sorted(expected_uses - captured)}")
            if not expected_uses.issubset(observed_uses):
                errors.append(f"flow {flow_id} step {index + 1} did not report uses: {sorted(expected_uses - observed_uses)}")
            expected_capture = as_names(expected.get("capture"))
            observed_capture = as_names(observed.get("captures"))
            if not expected_capture.issubset(observed_capture):
                errors.append(f"flow {flow_id} step {index + 1} did not report captures: {sorted(expected_capture - observed_capture)}")
            captured.update(expected_capture)
            if expected.get("assert_absent") and not observed.get("asserted_absent"):
                errors.append(f"flow {flow_id} step {index + 1} did not verify absence")

        create_flow = any(step.get("operation") == "create" for step in expected_steps if isinstance(step, dict))
        delete_positions = [
            index for index, step in enumerate(expected_steps)
            if isinstance(step, dict) and str(step.get("operation", "")).lower() in {"delete", "cleanup"}
        ]
        absence_positions = [
            index for index, step in enumerate(expected_steps)
            if isinstance(step, dict)
            and str(step.get("operation", "")).lower() in {"query", "read", "get"}
            and step.get("assert_absent")
        ]
        cleanup_contract = str(flow.get("cleanup", "")).strip()
        cleanup_shape_ok = bool(delete_positions and any(pos > delete_positions[0] for pos in absence_positions))
        if create_flow and not cleanup_shape_ok and not cleanup_contract:
            errors.append(
                f"flow {flow_id} creates data but has no delete plus post-delete absence check "
                "or documented cleanup exclusion"
            )
        if create_flow and cleanup_shape_ok:
            delete_index = delete_positions[0]
            absence_index = min(pos for pos in absence_positions if pos > delete_index)
            relevant = [delete_index, absence_index]
            if not actual.get("cleanup_verified") or any(
                index >= len(actual_steps)
                or actual_steps[index].get("status") != "passed"
                or (index == absence_index and not actual_steps[index].get("asserted_absent"))
                for index in relevant
            ):
                errors.append(f"flow {flow_id} has no verified delete and post-delete absence evidence")
        elif create_flow and actual.get("cleanup_verified"):
            errors.append(f"flow {flow_id} reports cleanup_verified without a verifiable cleanup shape")

    if errors:
        print("flow execution check failed")
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(f"flow execution check passed: {len(declared)} flow(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
