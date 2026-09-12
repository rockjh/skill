#!/usr/bin/env python3
"""Reconcile modular endpoint, logic, case, and flow manifests with Bruno files."""

from __future__ import annotations

import argparse
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


def ids(items: list[dict[str, Any]]) -> set[str]:
    return {str(item["id"]) for item in items if item.get("id")}


def module_dirs(contracts_root: Path) -> list[tuple[str, Path]]:
    if (contracts_root / "endpoints.yaml").is_file():
        return [(contracts_root.name, contracts_root)]
    modules_root = contracts_root / "modules"
    if not modules_root.is_dir():
        modules_root = contracts_root
    found = [
        (path.name, path)
        for path in sorted(modules_root.iterdir())
        if path.is_dir() and (path / "endpoints.yaml").is_file()
    ]
    if not found:
        raise SystemExit(f"no module endpoints.yaml files found below {contracts_root}")
    return found


def case_files(cases: list[dict[str, Any]], bru_root: Path) -> tuple[set[str], set[Path], dict[str, Path], list[str]]:
    if not bru_root.is_dir():
        return set(), set(), {}, [f"Bruno directory does not exist: {bru_root}"]
    known_files = {path.resolve(): path for path in bru_root.rglob("*.bru")}
    contents = {path: path.read_text(encoding="utf-8", errors="replace") for path in known_files}
    covered: set[str] = set()
    used_paths: set[Path] = set()
    case_paths: dict[str, Path] = {}
    errors: list[str] = []
    for case in cases:
        case_id = str(case.get("id", ""))
        if not case_id:
            errors.append("case without id")
            continue
        configured = case.get("bru")
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
            and str(item.get("path", "")) not in {"$.code", "$.status", "$.http_status"}
            for item in assertions
        ):
            errors.append(f"case {case_id} has no concrete response assertion beyond status/code")
    errors.extend(f"unregistered Bruno file: {path}" for path in sorted(set(known_files) - used_paths))
    return covered, used_paths, case_paths, errors


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
        return document
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
            isinstance(value, dict) and value.get("status") == "pass" for value in assertions
        )
        test_ok = isinstance(tests, list) and all(
            isinstance(value, dict) and value.get("status") == "pass" for value in tests
        )
        # An empty assertion/test list is valid for an intentionally
        # assertion-free request; the static manifest checker still rejects
        # assertion-light cases.
        if item.get("status") in {"pass", "passed", "success"} and not item.get("error") and assertion_ok and test_ok:
            passed.append(name)
    return {"executed": executed, "passed": passed}


def check_module(
    module_id: str,
    module_dir: Path,
    bru_root: Path,
    results: dict[str, Any] | None,
    require_scenarios: bool = False,
) -> tuple[dict[str, int], list[str]]:
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
    endpoint_ids = ids(endpoints)
    case_ids = ids(cases)
    if not endpoint_ids:
        errors.append("endpoints.yaml has no endpoints")
    if not case_ids:
        errors.append("cases.yaml has no cases")

    cases_by_id = {str(case["id"]): case for case in cases if case.get("id")}
    for case in cases:
        endpoint_id = case.get("endpoint_id")
        if endpoint_id and endpoint_id not in endpoint_ids:
            errors.append(f"case {case.get('id')} points to unknown endpoint {endpoint_id}")
    for endpoint in endpoints:
        endpoint_cases = endpoint.get("case_ids", [])
        if not endpoint_cases:
            endpoint_cases = [item.get("id") for item in list_at(endpoint, "cases")]
        if not endpoint_cases and str(endpoint.get("id")) not in excluded_endpoint_ids:
            errors.append(f"endpoint {endpoint.get('id')} has no case_ids")
        for case_id in endpoint_cases:
            case = cases_by_id.get(str(case_id))
            if case is None:
                errors.append(f"endpoint {endpoint.get('id')} references unknown case {case_id}")
            elif case.get("endpoint_id") and case.get("endpoint_id") != endpoint.get("id"):
                errors.append(f"case {case_id} points to endpoint {case.get('endpoint_id')}, not {endpoint.get('id')}")

        if require_scenarios and str(endpoint.get("id")) not in excluded_endpoint_ids:
            endpoint_case_objects = [
                cases_by_id[str(case_id)]
                for case_id in endpoint_cases
                if str(case_id) in cases_by_id
            ]
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
                if applicable and not endpoint_case_objects:
                    errors.append(f"endpoint {endpoint.get('id')} scenario {category}=true has no linked case")

    module_bru_root = bru_root / module_id if (bru_root / module_id).is_dir() else bru_root
    covered_cases, _, case_paths, file_errors = case_files(cases, module_bru_root)
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
        content = path.read_text(encoding="utf-8", errors="replace")
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
        if not logic.get("case_ids"):
            errors.append(f"logic {logic.get('id')} has no linked case_ids")
        for case_id in logic.get("case_ids", []):
            if case_id not in case_ids:
                errors.append(f"logic {logic.get('id')} references unknown case {case_id}")

    flow_items = first_list(flows_doc, "flows") or (flows_doc if isinstance(flows_doc, list) else [])
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
        errors.extend(f"case {case_id} was not executed" for case_id in sorted(case_ids - executed))
        errors.extend(f"case {case_id} failed" for case_id in sorted(executed - passed))

    return {"endpoints": len(endpoint_ids), "cases": len(case_ids), "bru_cases": len(covered_cases)}, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("contracts_root", type=Path)
    parser.add_argument("bru_root", type=Path)
    parser.add_argument("--results", type=Path, help="normalized JSON execution evidence")
    parser.add_argument(
        "--openapi",
        type=Path,
        help="offline OpenAPI/Swagger document (defaults to contracts_root/openapi.json when present)",
    )
    parser.add_argument(
        "--require-scenarios",
        action="store_true",
        help="require a decision for every case-matrix category on every endpoint",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    results = execution_evidence(load_data(args.results)) if args.results else None
    errors: list[str] = []
    totals = {"endpoints": 0, "cases": 0, "bru_cases": 0}
    declared: dict[str, dict[str, Any]] = {}
    modules = module_dirs(args.contracts_root)
    endpoint_records: list[tuple[str, dict[str, Any]]] = []
    all_case_ids: dict[str, str] = {}
    all_flow_ids: dict[str, str] = {}
    all_logic_ids: dict[str, str] = {}
    for module_id, module_dir in modules:
        endpoint_doc = load_data(module_dir / "endpoints.yaml")
        endpoint_records.extend((module_id, item) for item in first_list(endpoint_doc, "endpoints"))
        case_path = module_dir / "cases.yaml"
        if case_path.is_file():
            for case in manifest_cases(load_data(case_path)):
                case_id = str(case.get("id", ""))
                if not case_id:
                    continue
                previous = all_case_ids.get(case_id)
                if previous:
                    errors.append(f"case id {case_id} is duplicated in modules {previous} and {module_id}")
                all_case_ids[case_id] = module_id
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
    global_contracts = (args.contracts_root / "modules").is_dir()
    if openapi_path is not None and global_contracts:
        if not openapi_path.is_file():
            errors.append(f"offline OpenAPI document does not exist: {openapi_path}")
        else:
            try:
                offline = extract(openapi_path, load_document(openapi_path))
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
    elif openapi_path is not None and not global_contracts:
        # A single module manifest cannot be compared with the full document
        # without falsely reporting every sibling module as missing. Run the
        # OpenAPI reconciliation from the contracts root for the global gate.
        pass
    index_path = args.contracts_root / "index.yaml"
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
    for module_id, module_dir in modules:
        report, module_errors = check_module(
            module_id,
            module_dir,
            args.bru_root,
            results,
            require_scenarios=args.require_scenarios,
        )
        module_reports[module_id] = report
        for key in totals:
            totals[key] += report[key]
        errors.extend(f"[{module_id}] {error}" for error in module_errors)
        if module_id in declared:
            expected = declared[module_id]
            count_keys = {"endpoint_count": "endpoints", "case_count": "cases"}
            for index_key, report_key in count_keys.items():
                if index_key in expected and expected[index_key] != report[report_key]:
                    errors.append(
                        f"[{module_id}] index.yaml {index_key}={expected[index_key]} but actual={report[report_key]}"
                    )

    report = {**totals, "modules": module_reports, "errors": errors, "ok": not errors}
    if args.as_json:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    else:
        print(f"modules={len(modules)} endpoints={totals['endpoints']} cases={totals['cases']} bru_cases={totals['bru_cases']}")
        for error in errors:
            print(f"ERROR: {error}")
        print("coverage check passed" if not errors else f"coverage check failed: {len(errors)} error(s)")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
