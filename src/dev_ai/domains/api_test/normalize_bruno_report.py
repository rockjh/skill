#!/usr/bin/env python3
"""Convert a Bruno JSON report into stable, secret-free execution evidence."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

from .check_api_coverage import execution_evidence


SENSITIVE_KEY_RE = re.compile(
    r"(?:authorization|cookie|password|passwd|secret|token|access.?key|private.?key|session)",
    re.IGNORECASE,
)


def safe_value(value: Any, key: str = "", depth: int = 0) -> Any:
    """Keep useful response evidence while excluding credentials and huge payloads."""

    if SENSITIVE_KEY_RE.search(key):
        return "<redacted>"
    if depth >= 6:
        return "<max-depth>"
    if isinstance(value, dict):
        return {
            str(child_key): safe_value(child, str(child_key), depth + 1)
            for child_key, child in list(value.items())[:100]
        }
    if isinstance(value, list):
        return [safe_value(child, key, depth + 1) for child in value[:20]]
    if isinstance(value, str):
        if value.startswith("eyJ") and value.count(".") == 2:
            return "<redacted>"
        return value[:1000]
    return value


def response_shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): response_shape(child) for key, child in list(value.items())[:100]}
    if isinstance(value, list):
        return {"type": "array", "length": len(value), "items": response_shape(value[0]) if value else None}
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"


def normalized_case_results(raw: Any) -> dict[str, dict[str, Any]]:
    reports = [raw] if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    items = [
        item
        for report in reports
        if isinstance(report, dict)
        for item in report.get("results", [])
        if isinstance(item, dict)
    ]
    results: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []
    passed = set(execution_evidence(raw).get("passed", []))
    for item in items:
        test = item.get("test") if isinstance(item.get("test"), dict) else {}
        filename = str(test.get("filename", ""))
        name = str(item.get("name") or (Path(filename).stem if filename else ""))
        if not name:
            continue
        if name in results:
            duplicates.append(name)
        response = item.get("response") if isinstance(item.get("response"), dict) else {}
        body = response.get("data", response.get("body"))
        failures: list[str] = []
        for key in ("assertionResults", "testResults"):
            for observation in item.get(key, []) if isinstance(item.get(key), list) else []:
                if not isinstance(observation, dict) or str(observation.get("status", "")).lower() in {"pass", "passed", "success"}:
                    continue
                detail = observation.get("error") or observation.get("message") or observation.get("name") or key
                failures.append(str(detail)[:500])
        if item.get("error"):
            failures.append(str(item["error"])[:500])
        flow_events = {"capture": [], "use": [], "absence": []}
        for key in ("assertionResults", "testResults"):
            observations = item.get(key) if isinstance(item.get(key), list) else []
            for observation in observations:
                if not isinstance(observation, dict):
                    continue
                status = str(observation.get("status", "")).lower()
                label = str(observation.get("name") or observation.get("title") or observation.get("description") or "")
                if status not in {"pass", "passed", "success"} or not label.startswith("dev-ai:flow:"):
                    continue
                kind, _, value = label[len("dev-ai:flow:"):].partition(":")
                if kind in flow_events and value:
                    flow_events[kind].append(value)
        results[name] = {
            "status": "passed" if name in passed else "failed",
            "actual": {
                "http_status": response.get("status", response.get("statusCode")),
                "body": safe_value(body),
                "response_shape": response_shape(body),
            },
            "failure_reason": "; ".join(dict.fromkeys(failures)) or None,
            "flow_events": {key: list(dict.fromkeys(value)) for key, value in flow_events.items() if value},
        }
    normalized_case_results.duplicates = sorted(set(duplicates))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="raw Bruno JSON report")
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--business-sha")
    parser.add_argument("--environment")
    args = parser.parse_args()
    try:
        raw = json.loads(args.report.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        parser.error(f"cannot read Bruno report: {exc}")
    evidence = execution_evidence(raw)
    normalized_cases = normalized_case_results(raw)
    duplicates = getattr(normalized_case_results, "duplicates", [])
    if duplicates:
        print("duplicate Bruno case IDs in reporter output: " + ", ".join(duplicates), file=sys.stderr)
        return 1
    normalized: dict[str, Any] = {
        "version": 1,
        "executed": evidence.get("executed", []),
        "passed": evidence.get("passed", []),
        "cases": normalized_cases,
    }
    if isinstance(raw, dict) and isinstance(raw.get("flows"), dict):
        flows: dict[str, Any] = {}
        for flow_id, flow in raw["flows"].items():
            if not isinstance(flow, dict):
                continue
            safe_flow = {
                "status": flow.get("status"),
                "cleanup_verified": bool(flow.get("cleanup_verified")),
                "steps": [],
            }
            for step in flow.get("steps", []):
                if not isinstance(step, dict):
                    continue
                safe_flow["steps"].append(
                    {
                        "case_id": step.get("case_id"),
                        "status": step.get("status"),
                        "captures": step.get("captures", []),
                        "used_captures": step.get("used_captures", []),
                        "asserted_absent": bool(step.get("asserted_absent")),
                    }
                )
            flows[str(flow_id)] = safe_flow
        normalized["flows"] = flows
    metadata = {}
    if args.business_sha:
        metadata["business_sha"] = args.business_sha
    if args.environment:
        metadata["environment"] = args.environment
    if metadata:
        normalized["metadata"] = metadata
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(normalized, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(f"wrote normalized evidence: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
