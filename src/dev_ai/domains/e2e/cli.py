"""Command parser for the Python E2E domain."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ...core.errors import ExitCode
from .assets import initialize
from .contracts import generate_artifacts
from .discovery import PROTOCOL_SUFFIXES, discover_documents, discover_protocols, read_only_environment_probe
from .runner import ORDERED_GATES, check_gate, pytest_arg_errors, run_ordered
from .source_versions import RULES_VERSION, source_version_results


def init_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="dev-ai e2e init")
    parser.add_argument("--project", type=Path, default=Path("."))
    parser.add_argument("--design-root", action="append", type=Path, default=[])
    parser.add_argument("--design-file", action="append", type=Path, default=[])
    parser.add_argument("--openapi-root", action="append", type=Path, default=[])
    parser.add_argument("--openapi-file", action="append", type=Path, default=[])
    parser.add_argument("--runtime-url", "--protocol-url", dest="runtime_urls", action="append", default=[])
    args = parser.parse_args(argv)
    project = args.project.resolve()
    changed = initialize(project)
    if args.design_root or args.design_file or args.openapi_root or args.openapi_file or args.runtime_urls:
        result, errors = generate_artifacts(
            project,
            design_roots=args.design_root,
            design_files=args.design_file,
            openapi_roots=args.openapi_root,
            openapi_files=args.openapi_file,
            runtime_urls=args.runtime_urls,
        )
        print(json.dumps(result, ensure_ascii=False))
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        if errors:
            return int(ExitCode.GATE_FAILED)
    else:
        design = discover_documents(project)
        protocol = discover_protocols(project)
        if not design.files:
            print("design documents not found; provide --design-root or --design-file before generation", file=sys.stderr)
        if not protocol.files:
            print("formal protocol documents not found; provide --openapi-root or --openapi-file before generation", file=sys.stderr)
    print(f"initialized {project} ({len(changed)} file(s) changed)")
    return 0


def discover_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="dev-ai e2e discover")
    parser.add_argument("--project", type=Path, default=Path("."))
    parser.add_argument("--design-root", action="append", type=Path, default=[])
    parser.add_argument("--design-file", action="append", type=Path, default=[])
    parser.add_argument("--openapi-root", action="append", type=Path, default=[])
    parser.add_argument("--openapi-file", action="append", type=Path, default=[])
    parser.add_argument("--runtime-url", "--protocol-url", dest="runtime_urls", action="append", default=[])
    args = parser.parse_args(argv)
    project = args.project.resolve()
    design = discover_documents(project, roots=args.design_root, files=args.design_file)
    protocol = discover_protocols(project, roots=args.openapi_root, files=args.openapi_file)
    has_formal_protocol = any(path.suffix.casefold() in PROTOCOL_SUFFIXES for path in protocol.files)
    runtime = read_only_environment_probe(project) if not has_formal_protocol and not (args.openapi_root or args.openapi_file or args.runtime_urls) else {}
    if args.runtime_urls:
        from .discovery import read_only_protocol_probe
        probe = read_only_protocol_probe(
            ({"url": value, "source_type": "user_url", "user_confirmed": True} for value in args.runtime_urls),
            allow_external=True,
        )
        runtime = {
            "protocol_sources": probe.get("sources", []),
            "protocol_candidates": args.runtime_urls,
            "classifications": probe.get("classifications", ["protocol_unknown"]),
            "failure_details": probe.get("failure_details", []),
        }
    runtime_sources = runtime.get("protocol_sources", []) if isinstance(runtime, dict) else []
    print(json.dumps({
        "design": {"files": [str(path) for path in design.files], "candidates": [str(path) for path in design.candidates]},
        "protocol": {"files": [str(path) for path in protocol.files], "candidates": [str(path) for path in protocol.candidates], "runtime_sources": runtime_sources},
        "runtime": {"outcome": runtime.get("classifications", []) if isinstance(runtime, dict) else "not_requested", "protocol_candidates": runtime.get("protocol_candidates", []) if isinstance(runtime, dict) else []},
    }, ensure_ascii=False))
    if not design.files or (not protocol.files and not runtime_sources):
        return int(ExitCode.NOT_FOUND)
    if not (args.design_root or args.design_file) and len(design.candidates) > 1:
        return int(ExitCode.AMBIGUOUS)
    if not (args.openapi_root or args.openapi_file) and len(protocol.candidates) > 1:
        return int(ExitCode.AMBIGUOUS)
    return 0


def generate_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="dev-ai e2e generate")
    parser.add_argument("--project", type=Path, default=Path("."))
    parser.add_argument("--design-root", action="append", type=Path, default=[])
    parser.add_argument("--design-file", action="append", type=Path, default=[])
    parser.add_argument("--openapi-root", action="append", type=Path, default=[])
    parser.add_argument("--openapi-file", action="append", type=Path, default=[])
    parser.add_argument("--runtime-url", "--protocol-url", dest="runtime_urls", action="append", default=[])
    args = parser.parse_args(argv)
    result, errors = generate_artifacts(
        args.project.resolve(),
        design_roots=args.design_root,
        design_files=args.design_file,
        openapi_roots=args.openapi_root,
        openapi_files=args.openapi_file,
        runtime_urls=args.runtime_urls,
    )
    print(json.dumps(result, ensure_ascii=False))
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    return int(ExitCode.GATE_FAILED) if errors else 0


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
    return int(ExitCode.GATE_FAILED) if errors else 0


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
    return int(ExitCode.GATE_FAILED) if errors or any(item["outcome"] in failing for item in results) else 0


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
    commands = {
        "init": init_command,
        "discover": discover_command,
        "generate": generate_command,
        "check": check_command,
        "source-status": source_status_command,
        "run": run_command,
    }
    parser = argparse.ArgumentParser(prog="dev-ai e2e")
    parser.add_argument("command", nargs="?", choices=tuple(commands))
    if not argv or argv[0] in {"-h", "--help"}:
        parser.parse_args(argv)
        return 0
    if argv[0] not in commands:
        parser.error(f"invalid choice: {argv[0]!r}")
    return commands[argv[0]](argv[1:])
