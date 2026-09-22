"""Unified command line entrypoint for every development skill domain."""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Callable

from . import __version__
from .artifacts import require_lock, write_lock
from .doctor import diagnose
from .envelope import failure, render, success
from .errors import DltkError, ExitCode, classify_failure
from .redaction import redact
from .schema import (
    API_TEST_SCHEMA_VERSION,
    BUSINESS_FLOW_SCHEMA_VERSION,
    E2E_GATE_SCHEMA_VERSION,
    get_schema,
)


DOMAINS: dict[str, Callable[[list[str]], int]] = {}


def _domains() -> dict[str, Callable[[list[str]], int]]:
    if not DOMAINS:
        from .api_test_cli import main as api_test_main
        from .business_flow_cli import main as business_flow_main
        from .e2e_cli import main as e2e_main

        DOMAINS.update({"api-test": api_test_main, "business-flow": business_flow_main, "e2e": e2e_main})
    return DOMAINS


def _option_path(arguments: list[str], name: str, default: str) -> Path:
    for index, value in enumerate(arguments):
        if value == name and index + 1 < len(arguments):
            return Path(arguments[index + 1]).resolve()
        if value.startswith(name + "="):
            return Path(value.split("=", 1)[1]).resolve()
    return Path(default).resolve()


def _project_relative_path(arguments: list[str], name: str, project: Path, default: str) -> Path:
    for index, value in enumerate(arguments):
        if value == name and index + 1 < len(arguments):
            candidate = Path(arguments[index + 1])
            break
        if value.startswith(name + "="):
            candidate = Path(value.split("=", 1)[1])
            break
    else:
        candidate = Path(default)
    return (candidate if candidate.is_absolute() else project / candidate).resolve()


def _prepare_lock(domain: str, command: str, arguments: list[str]) -> tuple[Path | None, str]:
    if domain == "api-test":
        root = _option_path(arguments, "--qa-root", "qa")
        schema_version = API_TEST_SCHEMA_VERSION
    elif domain == "business-flow":
        root = _option_path(arguments, "--project", ".")
        schema_version = BUSINESS_FLOW_SCHEMA_VERSION
    else:
        root = _option_path(arguments, "--project", ".")
        schema_version = E2E_GATE_SCHEMA_VERSION
    if command != "init":
        require_lock(
            root,
            tool_version=__version__,
            domain=domain,
            schema_version=schema_version,
        )
    return root, schema_version


def _summary(stdout: str, stderr: str, *, full: bool) -> dict[str, object]:
    output_lines = [line for line in stdout.splitlines() if line.strip()]
    error_lines = [line for line in stderr.splitlines() if line.strip()]
    data: dict[str, object] = {
        "summary": output_lines[-1] if output_lines else "completed",
        "diagnostics": error_lines[:5],
    }
    if full:
        data["stdout"] = output_lines
        data["stderr"] = error_lines
    return redact(data)


def _artifact_path(domain: str, command: str, arguments: list[str], stdout: str) -> str:
    if domain == "api-test" and command in {"understand", "generate"}:
        path = _option_path(arguments, "--qa-root", "qa") / "results" / "design-generation-report.json"
        return str(path) if path.is_file() else ""
    if domain == "e2e" and command == "run":
        path = _option_path(arguments, "--project", ".") / "artifacts" / "e2e-run.json"
        return str(path) if path.is_file() else ""
    if domain == "e2e" and command in {"generate", "discover"}:
        path = _option_path(arguments, "--project", ".") / "discovery" / (
            "design-rules.yaml" if command == "generate" else "discovery.json"
        )
        return str(path) if path.is_file() else ""
    if domain == "business-flow" and command in {"discover", "generate", "update"}:
        project = _option_path(arguments, "--project", ".")
        docs_root = _project_relative_path(arguments, "--docs-root", project, "docs/business-flow")
        filename = "business-flow-discovery.json" if command == "discover" else "business-flow-report.json"
        path = docs_root / filename
        return str(path) if path.is_file() else ""
    for line in reversed(stdout.splitlines()):
        if " ledger=" in line:
            return line.rsplit(" ledger=", 1)[1].strip()
    for line in reversed(stdout.splitlines()):
        if line.startswith(("result_report=", "E2E report: ", "E2E 报告: ")):
            return line.split("=", 1)[-1].split(": ", 1)[-1].strip()
    return ""


def _run_domain(domain: str, arguments: list[str], *, full: bool) -> tuple[dict[str, object], int]:
    if not arguments:
        raise DltkError("INVALID_ARGUMENT", f"missing {domain} command", ExitCode.ARGUMENT)
    command, remainder = arguments[0], arguments[1:]
    try:
        root, schema_version = _prepare_lock(domain, command, remainder)
    except DltkError as exc:
        return failure(f"{domain}.{command}", exc), int(exc.exit_code)
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = _domains()[domain](arguments)
    except SystemExit as exc:
        code = int(exc.code) if isinstance(exc.code, int) else int(ExitCode.ARGUMENT)
    output = stdout.getvalue()
    diagnostics = stderr.getvalue()
    progress = [line for line in output.splitlines()[:-1] if line.strip()]
    progress.extend(line for line in diagnostics.splitlines() if line.strip())
    for line in progress:
        print(redact(line), file=sys.stderr)
    artifact = _artifact_path(domain, command, remainder, output)
    if code:
        message_lines = [line for line in diagnostics.splitlines() if line.strip()]
        message_lines.extend(line for line in output.splitlines() if line.strip())
        message = "\n".join(message_lines[:5]) or f"{domain}.{command} failed"
        error = classify_failure(f"{domain}.{command}", message, code)
        error.details_path = artifact
        return failure(f"{domain}.{command}", error), int(error.exit_code)
    if command == "init" and root is not None:
        write_lock(root, tool_version=__version__, domain=domain, schema_version=schema_version)
    data = _summary(output, diagnostics, full=full)
    if domain == "e2e" and command == "source-status" and output.strip():
        try:
            data = json.loads(output.splitlines()[-1])
        except json.JSONDecodeError:
            pass
    if domain == "business-flow" and command == "check" and output.strip():
        try:
            data = json.loads(output.splitlines()[-1])
        except json.JSONDecodeError:
            pass
    return success(f"{domain}.{command}", data, artifact), int(ExitCode.OK)


def _help_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dltk", description="Shared runtime for development AI skills")
    parser.add_argument("command", nargs="?", choices=("version", "doctor", "schema", "api-test", "business-flow", "e2e"))
    parser.epilog = "Use 'dltk schema' to list domain commands and 'dltk schema <domain.command>' for one contract."
    return parser


def console_main(argv: list[str] | None = None) -> int:
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    arguments = list(sys.argv[1:] if argv is None else argv)
    force_json = "--json" in arguments
    force_markdown = "--markdown" in arguments
    full = "--full" in arguments
    arguments = [value for value in arguments if value not in {"--json", "--markdown", "--full"}]
    markdown = force_markdown or (not force_json and sys.stdout.isatty())
    if not arguments or arguments[0] in {"-h", "--help"}:
        _help_parser().print_help()
        return int(ExitCode.OK)
    command = arguments[0]
    if command in _domains() and (
        len(arguments) == 1 or any(value in {"-h", "--help"} for value in arguments[1:])
    ):
        try:
            return _domains()[command](arguments[1:] or ["--help"])
        except SystemExit as exc:
            return int(exc.code) if isinstance(exc.code, int) else int(ExitCode.ARGUMENT)
    try:
        if command == "version":
            if len(arguments) != 1:
                raise DltkError("INVALID_ARGUMENT", "version accepts no arguments", ExitCode.ARGUMENT)
            document = success("version", {
                "version": __version__,
                "api_test_schema": API_TEST_SCHEMA_VERSION,
                "business_flow_schema": BUSINESS_FLOW_SCHEMA_VERSION,
                "e2e_gate_schema": E2E_GATE_SCHEMA_VERSION,
            })
            code = ExitCode.OK
        elif command == "doctor":
            if len(arguments) != 1:
                raise DltkError("INVALID_ARGUMENT", "doctor accepts no arguments", ExitCode.ARGUMENT)
            data, healthy = diagnose()
            data["healthy"] = healthy
            if healthy:
                document = success("doctor", data)
                code = ExitCode.OK
            else:
                failed = [name for name, check in data["checks"].items() if not check["ok"] and "required_for" not in check]
                error = DltkError(
                    "EXTERNAL_UNAVAILABLE",
                    f"required installation checks failed: {', '.join(failed)}",
                    ExitCode.UNAVAILABLE,
                    "Install the missing requirements and run dltk doctor again.",
                )
                document = failure("doctor", error)
                code = ExitCode.UNAVAILABLE
        elif command == "schema":
            if len(arguments) > 2:
                raise DltkError("INVALID_ARGUMENT", "schema accepts at most one scope", ExitCode.ARGUMENT)
            scope = arguments[1] if len(arguments) == 2 else None
            try:
                data = get_schema(scope)
            except KeyError as exc:
                raise DltkError("INVALID_ARGUMENT", f"unknown schema scope: {scope}", ExitCode.ARGUMENT) from exc
            document = success("schema" if scope is None else f"schema.{scope}", data)
            code = ExitCode.OK
        elif command in _domains():
            document, raw_code = _run_domain(command, arguments[1:], full=full)
            code = ExitCode(raw_code)
        else:
            raise DltkError("INVALID_ARGUMENT", f"unknown command: {command}", ExitCode.ARGUMENT)
    except DltkError as exc:
        document = failure(command, exc)
        code = exc.exit_code
    except Exception as exc:
        error = DltkError("INTERNAL_ERROR", str(exc) or exc.__class__.__name__, ExitCode.INTERNAL)
        document = failure(command, error)
        code = ExitCode.INTERNAL
    print(render(document, markdown=markdown))
    return int(code)
