"""Command parser for the Python E2E domain."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .assets import initialize
from .runner import ORDERED_GATES, check_gate, pytest_arg_errors, run_ordered
from .source_versions import RULES_VERSION, source_version_results


def init_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="dev-ai e2e init")
    parser.add_argument("--project", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    changed = initialize(args.project)
    print(f"initialized {args.project.resolve()} ({len(changed)} file(s) changed)")
    return 0


def check_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="dev-ai e2e check")
    parser.add_argument("--project", type=Path, default=Path("."))
    parser.add_argument("--gate", required=True, choices=(*ORDERED_GATES, "discovery", "contracts", "static", "all"))
    parser.add_argument("--scenario")
    args = parser.parse_args(argv)
    root = args.project.resolve()
    errors = check_gate(root, args.gate, args.scenario)
    for error in errors:
        print(error, file=sys.stderr)
    if not errors:
        print(f"gate passed: {args.gate}")
    return 1 if errors else 0


def source_status_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="dev-ai e2e source-status")
    parser.add_argument("--project", type=Path, default=Path("."))
    parser.add_argument("--scenario")
    args = parser.parse_args(argv)
    errors, results = source_version_results(args.project.resolve(), args.scenario)
    for error in errors:
        print(error, file=sys.stderr)
    print(json.dumps({"schema_version": RULES_VERSION, "results": results}, ensure_ascii=False))
    failing = {"affected", "full_rediscovery_required", "dirty_review_required"}
    return 1 if errors or any(item["outcome"] in failing for item in results) else 0


def run_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="dev-ai e2e run")
    parser.add_argument("--project", type=Path, default=Path("."))
    parser.add_argument("--scenario")
    parser.add_argument("--static-only", action="store_true")
    args, pytest_args = parser.parse_known_args(argv)
    invalid = pytest_arg_errors(pytest_args)
    if invalid:
        parser.error(f"unsupported pytest arguments: {', '.join(invalid)}")
    return run_ordered(args.project.resolve(), args.scenario, pytest_args, static_only=args.static_only)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = {"init": init_command, "check": check_command, "source-status": source_status_command, "run": run_command}
    parser = argparse.ArgumentParser(prog="dev-ai e2e")
    parser.add_argument("command", nargs="?", choices=tuple(commands))
    if not argv or argv[0] in {"-h", "--help"}:
        parser.parse_args(argv)
        return 0
    if argv[0] not in commands:
        parser.error(f"invalid choice: {argv[0]!r}")
    return commands[argv[0]](argv[1:])
