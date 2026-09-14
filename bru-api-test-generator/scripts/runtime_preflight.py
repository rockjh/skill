#!/usr/bin/env python3
"""Check the minimum runtime contract before generating or executing Bruno cases."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

from execution_config import (
    environment_file,
    load_bruno_environment,
    load_execution_config,
    required_environment_names,
)


def validate_base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise argparse.ArgumentTypeError("base URL must be an http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise argparse.ArgumentTypeError("base URL must not contain credentials, query, or fragment")
    return value.rstrip("/")


def join_url(base: str, path: str) -> str:
    parsed = urllib.parse.urlsplit(base)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, f"{parsed.path.rstrip('/')}/{path.lstrip('/')}", "", ""))


def parse_statuses(value: str) -> set[int]:
    statuses: set[int] = set()
    for item in value.split(","):
        token = item.strip()
        if not token:
            continue
        if "-" in token:
            start, end = token.split("-", 1)
            statuses.update(range(int(start), int(end) + 1))
        else:
            statuses.add(int(token))
    if not statuses or any(status < 100 or status > 599 for status in statuses):
        raise argparse.ArgumentTypeError("status list must contain HTTP codes or ranges")
    return statuses


def request(url: str, method: str, timeout: float, headers: dict[str, str] | None = None) -> dict[str, Any]:
    req = urllib.request.Request(url, method=method.upper(), headers=headers or {"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = response.read(2 * 1024 * 1024)
            try:
                body: Any = json.loads(payload.decode("utf-8")) if payload else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                body = payload.decode("utf-8", errors="replace")
            return {"status": response.status, "body": body, "error": None}
    except urllib.error.HTTPError as exc:
        payload = exc.read(2 * 1024 * 1024)
        try:
            body = json.loads(payload.decode("utf-8")) if payload else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = payload.decode("utf-8", errors="replace")
        return {"status": exc.code, "body": body, "error": str(exc)}
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        return {"status": None, "body": None, "error": str(exc)}


def body_code(body: Any) -> Any:
    if isinstance(body, dict):
        for key in ("code", "errorCode", "status"):
            if key in body:
                return body[key]
        data = body.get("data")
        if isinstance(data, dict) and "code" in data:
            return data["code"]
    return None


MIN_BRUNO_VERSION = (4, 1, 0)


def bruno_cli_version(executable: str) -> tuple[tuple[int, int, int] | None, str]:
    resolved = shutil.which(executable)
    if not resolved:
        return None, ""
    try:
        completed = subprocess.run(
            [resolved, "--version"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None, ""
    output = (completed.stdout or completed.stderr).strip()
    match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", output)
    return (tuple(int(value) for value in match.groups()) if match else None), output


def atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent, prefix=f".{path.name}.") as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-path", help="public route used to prove routing exists")
    parser.add_argument("--public-method", default="GET")
    parser.add_argument("--public-status", type=parse_statuses, default=parse_statuses("200-399"))
    parser.add_argument(
        "--require-public-route",
        action="store_true",
        help="require the representative probe to use --public-path",
    )
    parser.add_argument("--admin-path", help="secured route requested without credentials")
    parser.add_argument("--admin-method", default="GET")
    parser.add_argument("--admin-status", type=parse_statuses, default=parse_statuses("200,401,403"))
    parser.add_argument("--admin-code", help="expected error-envelope code for the unauthenticated admin request")
    parser.add_argument(
        "--require-admin-baseline",
        action="store_true",
        help="require the representative probe to include --admin-path",
    )
    parser.add_argument("--openapi", type=Path, help="saved offline OpenAPI document to fingerprint")
    parser.add_argument("--expected-openapi-sha256")
    parser.add_argument("--static-results", type=Path, help="successful static coverage report produced with --openapi")
    parser.add_argument("--execution-config", type=Path, help="qa/execution/config.yaml")
    parser.add_argument("--env-file", type=Path, help="active Bruno .bru environment file")
    parser.add_argument("--require-env", action="append", default=[])
    parser.add_argument("--fixture", action="append", type=Path, default=[])
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--bruno-cli", default="bru", help="Bruno CLI executable checked for execution readiness")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    checks: list[dict[str, Any]] = []
    static_failures: list[str] = []
    context_failures: list[str] = []
    execution_failures: list[str] = []
    config_path = args.execution_config or (Path(__file__).resolve().parents[1] / "execution" / "config.yaml")
    config: dict[str, Any] | None = None
    environment_values: dict[str, str] = {}
    active_environment: str | None = None
    try:
        config = load_execution_config(config_path)
        active_environment = config["active_environment"]
    except ValueError as exc:
        static_failures.append(str(exc))
    env_path = args.env_file or (environment_file(config_path, config) if config else None)
    if env_path is None:
        context_failures.append("active Bruno environment cannot be resolved")
    else:
        try:
            environment_values = load_bruno_environment(env_path)
        except ValueError as exc:
            context_failures.append(str(exc))
    static_report: dict[str, Any] | None = None
    if not args.static_results:
        static_failures.append("static coverage results are missing; provide --static-results")
    elif not args.static_results.is_file():
        static_failures.append(f"static coverage results are missing: {args.static_results}")
    else:
        try:
            loaded_static_report = json.loads(args.static_results.read_text(encoding="utf-8", errors="strict"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            static_failures.append(f"cannot read static coverage results {args.static_results}: {exc}")
        else:
            static_report = loaded_static_report if isinstance(loaded_static_report, dict) else None
            static_ok = static_report is not None and (
                static_report.get("static_ready") is True or static_report.get("static_ok") is True
            )
            checks.append({"name": "static_coverage", "ok": static_ok, "file": str(args.static_results)})
            if not static_ok:
                static_failures.append("static coverage results did not pass")
    base_url: str | None = None
    if environment_values.get("BASE_URL"):
        try:
            base_url = validate_base_url(environment_values["BASE_URL"])
        except argparse.ArgumentTypeError as exc:
            execution_failures.append(f"Bruno environment variable BASE_URL has an invalid base URL: {exc}")
    if not base_url:
        execution_failures.append("base URL is missing; configure BASE_URL in the active Bruno environment")
    if args.timeout <= 0:
        execution_failures.append("timeout must be positive")

    cli_version, cli_output = bruno_cli_version(args.bruno_cli)
    cli_available = cli_version is not None
    cli_supported = cli_version is not None and cli_version >= MIN_BRUNO_VERSION
    checks.append({
        "name": "bruno_cli",
        "ok": cli_supported,
        "executable": args.bruno_cli,
        "version": ".".join(str(value) for value in cli_version) if cli_version else None,
    })
    if not cli_available:
        execution_failures.append(f"Bruno CLI is unavailable or did not report a semantic version: {args.bruno_cli}")
    elif not cli_supported:
        execution_failures.append(
            f"Bruno CLI {cli_output} is unsupported; version 4.1.0 or newer is required"
        )

    required_envs = list(args.require_env)
    if config:
        required_envs.extend(required_environment_names(config))
    for name in dict.fromkeys(required_envs):
        present = bool(environment_values.get(name))
        checks.append({"name": f"env:{name}", "ok": present})
        if not present:
            context_failures.append(f"required Bruno environment variable is missing: {name}")
    for fixture in args.fixture:
        present = fixture.is_file()
        checks.append({"name": f"fixture:{fixture}", "ok": present})
        if not present:
            context_failures.append(f"required fixture is missing: {fixture}")

    if not args.openapi:
        static_failures.append("offline OpenAPI document is missing; provide --openapi")
    elif args.openapi:
        if not args.openapi.is_file():
            static_failures.append(f"offline OpenAPI document is missing: {args.openapi}")
        else:
            digest = hashlib.sha256(args.openapi.read_bytes()).hexdigest()
            expected_match = not args.expected_openapi_sha256 or digest == args.expected_openapi_sha256.lower()
            static_match = True
            if static_report is not None:
                static_digest = str(static_report.get("openapi_sha256") or "").lower()
                if static_digest != digest:
                    static_match = False
                    static_failures.append(
                        f"static coverage OpenAPI fingerprint mismatch: expected {digest}, got {static_digest or '<missing>'}"
                    )
            match = expected_match and static_match
            checks.append({"name": "openapi_fingerprint", "ok": match, "sha256": digest})
            if not expected_match:
                static_failures.append(f"offline OpenAPI fingerprint mismatch: expected {args.expected_openapi_sha256}, got {digest}")

    if base_url and args.public_path:
        result = request(join_url(base_url, args.public_path), args.public_method, args.timeout)
        ok = result["status"] in args.public_status
        checks.append({"name": "public_route", "ok": ok, "status": result["status"], "path": args.public_path})
        if not ok:
            execution_failures.append(f"public route {args.public_method} {args.public_path} returned {result['status']!r}; expected one of {sorted(args.public_status)}")
    elif base_url and args.require_public_route:
        execution_failures.append("public route is not configured; provide --public-path")

    if base_url and args.admin_path:
        result = request(join_url(base_url, args.admin_path), args.admin_method, args.timeout)
        code = body_code(result.get("body"))
        status_ok = result["status"] in args.admin_status
        code_ok = (args.admin_code is not None and str(code) == str(args.admin_code)) or (
            args.admin_code is None and result["status"] != 200
        )
        ok = status_ok and code_ok
        checks.append({"name": "admin_auth_baseline", "ok": ok, "status": result["status"], "code": code, "path": args.admin_path})
        if not ok:
            execution_failures.append(f"admin baseline {args.admin_method} {args.admin_path} returned status={result['status']!r}, code={code!r}; expected status={sorted(args.admin_status)} code={args.admin_code!r}")
    elif base_url and args.require_admin_baseline:
        execution_failures.append("admin auth baseline is not configured; provide --admin-path")

    if base_url and not args.public_path and not args.admin_path:
        execution_failures.append(
            "representative route is not configured; provide --public-path or --admin-path"
        )

    static_ready = not static_failures
    context_ready = not context_failures
    execution_ready = not execution_failures
    failures = [*static_failures, *context_failures, *execution_failures]

    report = {
        "version": 1,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "environment": active_environment,
        "status": "runnable" if static_ready and context_ready and execution_ready else ("draft" if static_ready else "blocked"),
        "static_ready": static_ready,
        "context_ready": context_ready,
        "execution_ready": execution_ready,
        "checks": checks,
        "errors": failures,
    }
    rendered = json.dumps(report, ensure_ascii=True, indent=2) + "\n"
    if args.output:
        atomic_write(args.output, rendered)
    sys.stdout.write(rendered)
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
