"""CLI for source-backed business-flow design documents."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ...core.schema import BUSINESS_FLOW_INDEX_SCHEMA, BUSINESS_FLOW_REPORT_SCHEMA, validate_schema
from .discovery import scan
from .documents import apply_module_map, coverage, write_artifacts, write_discovery
from .git import changed_paths, working_tree_paths
from .models import EntryPoint


def _project(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", type=Path, default=Path("."))
    parser.add_argument("--docs-root", type=Path, default=Path("docs/business-flow"))


def _docs_root(project: Path, value: Path) -> Path:
    return (value if value.is_absolute() else project / value).resolve()


def init_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="dev-ai business-flow init")
    _project(parser)
    args = parser.parse_args(argv)
    root = args.project.resolve()
    if not (root / ".git").exists() and not (root / ".git").is_file():
        parser.error(f"git repository does not exist: {root}")
    docs_root = _docs_root(root, args.docs_root)
    docs_root.mkdir(parents=True, exist_ok=True)
    print(f"initialized business-flow project={root} docs_root={docs_root}")
    return 0


def _old_index(docs_root: Path) -> dict[str, object]:
    path = docs_root / "business-flow-index.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_json(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def discover_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="dev-ai business-flow discover")
    _project(parser)
    parser.add_argument("--commit")
    args = parser.parse_args(argv)
    project = args.project.resolve()
    docs_root = _docs_root(project, args.docs_root)
    result = scan(project, args.commit)
    discovery_path, module_map_path = write_discovery(result, docs_root)
    print(
        f"discovered entries={len(result.entries)} unresolved={len(result.unresolved)} "
        f"inventory={discovery_path} module_map={module_map_path}"
    )
    return 0


def _change_summary(project: Path, old: dict[str, object], entries: list[EntryPoint], target: str) -> dict[str, object]:
    old_records = {
        str(item.get("id")): item
        for item in old.get("entries", [])
        if isinstance(item, dict) and item.get("id")
    }
    old_entries = set(old_records)
    new_entries = {entry.entry_id for entry in entries}
    modules = {entry.module for entry in entries}

    def current_signature(entry: EntryPoint) -> tuple[object, ...]:
        return (
            entry.kind, entry.identifier, entry.handler, entry.module, f"{entry.file}:{entry.line}",
            entry.caller, entry.input_summary, tuple(entry.functions),
            tuple((error.code, error.condition, f"{error.file}:{error.line}") for error in entry.errors),
            tuple((behavior.kind, behavior.statement, f"{behavior.file}:{behavior.line}") for behavior in entry.behaviors),
        )

    def stored_signature(item: dict[str, object]) -> tuple[object, ...]:
        return (
            item.get("type"), item.get("identifier"), item.get("handler"), item.get("module"), item.get("source"),
            item.get("caller"), item.get("input_summary"), tuple(item.get("core_capabilities", [])),
            tuple(
                (error.get("code"), error.get("condition"), error.get("source"))
                for error in item.get("errors", []) if isinstance(error, dict)
            ),
            tuple(
                (behavior.get("kind"), behavior.get("statement"), behavior.get("source"))
                for behavior in item.get("behaviors", []) if isinstance(behavior, dict)
            ),
        )

    semantic_changes = {
        entry.entry_id
        for entry in entries
        if entry.entry_id in old_records and current_signature(entry) != stored_signature(old_records[entry.entry_id])
    }
    old_rationales = {
        str(item.get("name")): str(item.get("rationale"))
        for item in old.get("modules", [])
        if isinstance(item, dict) and item.get("name")
    }
    semantic_changes.update(
        entry.entry_id
        for entry in entries
        if old_rationales.get(entry.module) != entry.module_rationale
    )
    old_commit = str(old.get("effective_git", {}).get("commit")) if isinstance(old.get("effective_git"), dict) else ""
    if not old_commit:
        return {
            "comparison": "unavailable",
            "added_entries": sorted(new_entries),
            "updated_entries": [],
            "deleted_entries": [],
            "version_only_documents": [],
            "business_changed_documents": sorted(modules),
        }
    try:
        changes, error = changed_paths(project, old_commit, target)
    except Exception as exc:
        changes, error = [], str(exc)
    if error:
        return {
            "comparison": "old_version_unavailable",
            "added_entries": sorted(new_entries - old_entries),
            "updated_entries": [],
            "deleted_entries": sorted(old_entries - new_entries),
            "version_only_documents": [],
            "business_changed_documents": sorted(modules),
            "comparison_error": error,
        }
    config_markers = {
        "pom.xml", "build.gradle", "build.gradle.kts", "package.json", "pyproject.toml", "requirements.txt",
        "application.yml", "application.yaml", "application.properties", "package-lock.json", "pnpm-lock.yaml",
        "yarn.lock", "tsconfig.json", "application.json", "routes.json", "config.json", ".yml", ".yaml",
        ".properties", ".toml", ".xml",
    }

    def evidence_paths(item: dict[str, object]) -> set[str]:
        paths: set[str] = set()

        def add_source(value: object) -> None:
            if not isinstance(value, str) or not value:
                return
            source = value.split(":", 1)[0].replace("\\", "/").lower()
            if source:
                paths.add(source)

        add_source(item.get("source"))
        for field in ("errors", "behaviors"):
            values = item.get(field, [])
            if isinstance(values, list):
                for value in values:
                    if isinstance(value, dict):
                        add_source(value.get("source"))
        capabilities = item.get("core_capabilities", [])
        if isinstance(capabilities, list):
            for capability in capabilities:
                if isinstance(capability, str) and ":" in capability:
                    add_source(capability)
        return paths

    referenced_paths: set[str] = set()
    for entry in entries:
        referenced_paths.update(evidence_paths({
            "source": f"{entry.file}:{entry.line}",
            "core_capabilities": entry.functions,
            "errors": [
                {"source": f"{error.file}:{error.line}"}
                for error in entry.errors
            ],
            "behaviors": [
                {"source": f"{behavior.file}:{behavior.line}"}
                for behavior in entry.behaviors
            ],
        }))
    for item in old_records.values():
        if isinstance(item, dict):
            referenced_paths.update(evidence_paths(item))

    def status_path(status: str) -> str:
        value = status.rsplit("\t", 1)[-1].strip('"')
        if len(value) > 3 and value[2].isspace():
            value = value[3:].strip()
        else:
            value = value.strip()
        return value.rsplit(" -> ", 1)[-1].strip('"').replace("\\", "/").lower()

    def is_business_path(status: str) -> bool:
        path = status_path(status)
        if path in referenced_paths:
            return True
        filename = path.rsplit("/", 1)[-1]
        return (
            filename in config_markers
            or path.endswith(tuple(marker for marker in config_markers if marker.startswith(".")))
            or "/config/" in path
            or path.startswith("config/")
        )

    business_changes = [item for item in changes if is_business_path(item)]
    dirty_changes = [f"WORKTREE\t{item}" for item in working_tree_paths(project) if is_business_path(item)]
    business_changes.extend(dirty_changes)
    changed_business = bool(business_changes or semantic_changes or new_entries != old_entries)
    return {
        "comparison": "business_changed" if changed_business else "version_only",
        "added_entries": sorted(new_entries - old_entries),
        "updated_entries": sorted((new_entries & old_entries) if business_changes else semantic_changes),
        "deleted_entries": sorted(old_entries - new_entries),
        "version_only_documents": [] if changed_business else sorted(modules),
        "business_changed_documents": sorted(modules) if changed_business else [],
        "changed_paths": changes,
        "business_changed_paths": business_changes,
    }


def generate_command(argv: list[str], *, incremental: bool = False) -> int:
    command = "update" if incremental else "generate"
    parser = argparse.ArgumentParser(prog=f"dev-ai business-flow {command}")
    _project(parser)
    parser.add_argument("--module")
    parser.add_argument("--commit", help="Git commit or ref; defaults to HEAD")
    parser.add_argument("--full", action="store_true", help="Accepted for shared CLI compatibility")
    args = parser.parse_args(argv)
    project = args.project.resolve()
    docs_root = _docs_root(project, args.docs_root)
    result = scan(project, args.commit)
    module_errors = apply_module_map(result, docs_root / "business-flow-modules.json")
    if module_errors:
        for error in module_errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 8
    if result.unresolved:
        for finding in result.unresolved:
            print(f"ERROR: unresolved source evidence: {finding}", file=sys.stderr)
        return 8
    if args.module:
        if not any(entry.module == args.module for entry in result.entries):
            parser.error(f"business module does not exist in source: {args.module}")
    previous = _old_index(docs_root)
    if previous:
        previous_errors = validate_schema(BUSINESS_FLOW_INDEX_SCHEMA, previous)
        if previous_errors:
            print("ERROR: existing business-flow index is invalid: " + "; ".join(previous_errors), file=sys.stderr)
            return 8
    changes = _change_summary(project, previous, result.entries, result.git.target)
    index_path, report_path, report = write_artifacts(
        result,
        docs_root,
        comparison=str(changes.get("comparison", "current")),
        old_commit=str(previous.get("effective_git", {}).get("commit")) if isinstance(previous.get("effective_git"), dict) else None,
        changed=changes,
        module_filter=args.module,
    )
    coverage_failures = [
        name for name, value in report["coverage"].items()
        if isinstance(value, list) and value
    ]
    if coverage_failures:
        print(
            "ERROR: generated business-flow coverage failed: " + ", ".join(coverage_failures),
            file=sys.stderr,
        )
        return 8
    print(
        f"generated modules={report['module_count']} entries={report['entry_count']} "
        f"error_codes={report['active_error_code_count']} report={report_path} index={index_path}"
    )
    return 0


def check_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="dev-ai business-flow check")
    _project(parser)
    parser.add_argument("--module")
    parser.add_argument("--commit")
    args = parser.parse_args(argv)
    result = scan(args.project.resolve(), args.commit)
    docs_root = _docs_root(args.project.resolve(), args.docs_root)
    module_errors = apply_module_map(result, docs_root / "business-flow-modules.json")
    if module_errors:
        for error in module_errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 8
    if args.module and not any(entry.module == args.module for entry in result.entries):
        parser.error(f"business module does not exist in source: {args.module}")
    index = _old_index(docs_root)
    if not index:
        print("business-flow index is missing", file=sys.stderr)
        return 1
    index_errors = validate_schema(BUSINESS_FLOW_INDEX_SCHEMA, index)
    if index_errors:
        print("business-flow index is invalid: " + "; ".join(index_errors), file=sys.stderr)
        return 1
    stored_report = _read_json(docs_root / "business-flow-report.json")
    if not stored_report:
        print("business-flow report is missing", file=sys.stderr)
        return 1
    report_errors = validate_schema(BUSINESS_FLOW_REPORT_SCHEMA, stored_report)
    if report_errors:
        print("business-flow report is invalid: " + "; ".join(report_errors), file=sys.stderr)
        return 1
    result_coverage = coverage(result, index, docs_root, args.module)
    expected_commit = str(index.get("effective_git", {}).get("commit")) if isinstance(index.get("effective_git"), dict) else ""
    version_ok = expected_commit == result.git.target
    fingerprint_ok = index.get("source_fingerprint") == result.source_fingerprint
    report_version_ok = stored_report.get("effective_git", {}).get("commit") == result.git.target
    report_fingerprint_ok = stored_report.get("source_fingerprint") == result.source_fingerprint
    clean_ok = not result.git.includes_uncommitted or bool(index.get("effective_git", {}).get("includes_uncommitted_changes"))
    payload = {
        "coverage": result_coverage,
        "version_match": version_ok,
        "source_fingerprint_match": fingerprint_ok,
        "report_version_match": report_version_ok,
        "report_source_fingerprint_match": report_fingerprint_ok,
        "workspace_dirty_acknowledged": clean_ok,
        "unresolved": result.unresolved,
    }
    print(json.dumps(payload, ensure_ascii=False))
    markdown_failures = any(
        result_coverage[name]
        for name in (
            "markdown_missing_documents", "markdown_missing_entries", "markdown_missing_error_codes",
            "markdown_stale_entries", "markdown_missing_error_evidence", "markdown_stale_error_evidence",
            "markdown_diagram_mismatches", "markdown_version_mismatches",
        )
    )
    if (
        result_coverage["missing_entries"]
        or result_coverage["stale_entries"]
        or result_coverage["missing_error_codes"]
        or result_coverage["stale_error_codes"]
        or result_coverage["missing_error_evidence"]
        or result_coverage["stale_error_evidence"]
        or markdown_failures
        or result.unresolved
        or not version_ok
        or not fingerprint_ok
        or not report_version_ok
        or not report_fingerprint_ok
        or not clean_ok
    ):
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    commands = {
        "init": init_command,
        "discover": discover_command,
        "generate": generate_command,
        "update": lambda args: generate_command(args, incremental=True),
        "check": check_command,
    }
    parser = argparse.ArgumentParser(prog="dev-ai business-flow")
    parser.add_argument("command", nargs="?", choices=tuple(commands))
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        parser.parse_args(args)
        return 0
    if args[0] not in commands:
        parser.error(f"invalid choice: {args[0]!r}")
    return commands[args[0]](args[1:])
