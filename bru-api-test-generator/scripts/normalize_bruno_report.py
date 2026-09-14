#!/usr/bin/env python3
"""Convert a Bruno JSON report into stable, secret-free execution evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

try:
    from check_api_coverage import execution_evidence
except ImportError:
    from scripts.check_api_coverage import execution_evidence


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
    normalized: dict[str, Any] = {
        "version": 1,
        "executed": evidence.get("executed", []),
        "passed": evidence.get("passed", []),
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
