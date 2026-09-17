#!/usr/bin/env python3
"""Fetch a validated OpenAPI document from a running loopback service."""

from __future__ import annotations

import argparse
import copy
import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
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


DEFAULT_PATHS = (
    "/v3/api-docs",
    "/v3/api-docs.yaml",
    "/v2/api-docs",
    "/swagger/v1/swagger.json",
    "/swagger.json",
    "/openapi.json",
    "/api-docs",
)
MAX_BYTES = 16 * 1024 * 1024


def is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not is_loopback(parsed.hostname):
        raise argparse.ArgumentTypeError("base URL must be an http(s) loopback URL")
    if parsed.username or parsed.password:
        raise argparse.ArgumentTypeError("base URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise argparse.ArgumentTypeError("base URL must not contain a query or fragment")
    return value.rstrip("/")


def listening_ports() -> set[int]:
    """Read TCP listeners using whichever standard system tool is available."""

    commands = (
        ("lsof", "-nP", "-iTCP", "-sTCP:LISTEN"),
        ("ss", "-ltn"),
        ("netstat", "-an"),
    )
    for command in commands:
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=2,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode not in {0, 1}:
            continue
        ports: set[int] = set()
        for line in result.stdout.splitlines():
            upper = line.upper()
            if "LISTEN" not in upper:
                continue
            # lsof/ss use ':port'; BSD netstat commonly uses '.port'.
            for match in re.findall(r"(?:[:.])(\d+)(?=\s|\(|$)", line):
                port = int(match)
                if 1 <= port <= 65535:
                    ports.add(port)
        if ports:
            return ports
    return set()


def join_url(base_url: str, path: str) -> str:
    parsed = urllib.parse.urlsplit(base_url)
    base_path = parsed.path.rstrip("/")
    suffix = "/" + path.lstrip("/")
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, f"{base_path}{suffix}", "", "")
    )


def parse_document(payload: bytes) -> dict[str, Any] | None:
    try:
        loaded = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        try:
            import yaml  # type: ignore[import-not-found]
        except ModuleNotFoundError:
            return None
        try:
            loaded = yaml.safe_load(payload.decode("utf-8-sig"))
        except (UnicodeDecodeError, yaml.YAMLError):  # type: ignore[attr-defined]
            return None
    if not isinstance(loaded, dict) or not isinstance(loaded.get("paths"), dict):
        return None
    if not (loaded.get("openapi") or loaded.get("swagger")):
        return None
    return loaded


def render_document(document: dict[str, Any], output: Path) -> bytes:
    if output.suffix.lower() == ".json":
        return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    try:
        import yaml  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise SystemExit("YAML output requires an existing PyYAML installation") from exc
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode("utf-8")


def contract_identity(document: dict[str, Any]) -> dict[str, Any]:
    """Ignore deployment/provenance fields while comparing API identity."""

    normalized = copy.deepcopy(document)
    normalized.pop("provenance", None)
    normalized.pop("host", None)
    normalized.pop("schemes", None)

    def strip_servers(value: Any) -> None:
        if isinstance(value, dict):
            value.pop("servers", None)
            for child in value.values():
                strip_servers(child)
        elif isinstance(value, list):
            for child in value:
                strip_servers(child)

    strip_servers(normalized)
    return normalized


def write_atomic(output: Path, payload: bytes) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.replace(temporary, output)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def probe(url: str, timeout: float) -> tuple[dict[str, Any] | None, str]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json, application/yaml, text/yaml"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final_url = response.geturl()
            if not is_loopback(urllib.parse.urlsplit(final_url).hostname):
                return None, "redirected away from loopback"
            if not 200 <= response.status < 300:
                return None, f"HTTP {response.status}"
            payload = response.read(MAX_BYTES + 1)
            if len(payload) > MAX_BYTES:
                return None, f"response exceeds {MAX_BYTES} bytes"
            document = parse_document(payload)
            if document is None:
                return None, "response is not a valid OpenAPI/Swagger document"
            return document, "ok"
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        TimeoutError,
        socket.timeout,
        OSError,
        ValueError,
    ) as exc:
        return None, str(exc)


def candidate_urls(
    base_urls: list[str], ports: set[int], paths: tuple[str, ...]
) -> list[str]:
    bases = list(base_urls)
    for port in sorted(ports):
        bases.extend((f"http://127.0.0.1:{port}", f"http://[::1]:{port}"))
    seen: set[str] = set()
    candidates: list[str] = []
    for base in bases:
        for path in paths:
            url = join_url(base, path)
            if url not in seen:
                seen.add(url)
                candidates.append(url)
    return candidates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path("."),
        help="target application repository (default: current directory)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("qa/data/contracts/openapi.json"),
        help="offline output path (default: qa/data/contracts/openapi.json)",
    )
    parser.add_argument(
        "--base-url",
        action="append",
        type=validate_base_url,
        default=[],
        help="loopback application URL; may be repeated",
    )
    parser.add_argument(
        "--port",
        action="append",
        type=int,
        default=[],
        help="local HTTP port; may be repeated",
    )
    parser.add_argument(
        "--path",
        action="append",
        dest="paths",
        default=None,
        help="OpenAPI path to probe; may be repeated",
    )
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--application-sha", default=os.environ.get("BUSINESS_GIT_SHA"))
    parser.add_argument("--application-pid", default=os.environ.get("APP_PID"))
    parser.add_argument("--startup-command", default=os.environ.get("APP_STARTUP_COMMAND"))
    args = parser.parse_args()
    project_root = args.project_root.resolve()
    if not project_root.is_dir():
        parser.error(f"project root does not exist: {project_root}")
    output = args.output if args.output.is_absolute() else project_root / args.output
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if any(port < 1 or port > 65535 for port in args.port):
        parser.error("--port must be between 1 and 65535")

    ports = set(args.port)
    if not args.base_url and not args.port:
        ports |= listening_ports()
    paths = tuple(args.paths or DEFAULT_PATHS)
    urls = candidate_urls(args.base_url, ports, paths)
    if not urls:
        print(
            "BLOCKED: no local HTTP listener was found; start the target application "
            "or provide --base-url/--port.",
            file=sys.stderr,
        )
        return 2

    failures: list[str] = []
    valid: list[tuple[str, dict[str, Any]]] = []
    for url in urls:
        document, reason = probe(url, args.timeout)
        if document is None:
            failures.append(f"{url}: {reason}")
            continue
        if not any(contract_identity(existing) == contract_identity(document) for _, existing in valid):
            valid.append((url, document))

    if len(valid) > 1:
        print(
            "BLOCKED: multiple different local OpenAPI/Swagger documents were found; "
            "rerun with --base-url and/or --path to select the target application.",
            file=sys.stderr,
        )
        for url, _ in valid:
            print(f"  valid document: {url}", file=sys.stderr)
        return 3
    if valid:
        url, document = valid[0]
        raw_contract = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        provenance = {
            "source_url": url,
            "acquired_at": datetime.now(timezone.utc).isoformat(),
            "contract_sha256": hashlib.sha256(raw_contract).hexdigest(),
            "application_sha": args.application_sha,
            "application_pid": args.application_pid,
            "startup_command": args.startup_command,
            "status": "verified"
            if args.application_sha and args.application_pid and args.startup_command
            else "contract_provenance_unverified",
        }
        document = dict(document)
        document["provenance"] = provenance
        try:
            write_atomic(output, render_document(document, output))
        except (OSError, SystemExit) as exc:
            print(f"BLOCKED: cannot write offline specification {output}: {exc}", file=sys.stderr)
            return 3
        version = document.get("openapi") or document.get("swagger")
        print(f"downloaded {url} -> {output} (spec_version={version})")
        return 0

    print("BLOCKED: no valid OpenAPI/Swagger document was found at the local candidate endpoints.", file=sys.stderr)
    for failure in failures[:8]:
        print(f"  {failure}", file=sys.stderr)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
