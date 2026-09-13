#!/usr/bin/env python3
"""Check the minimum runtime contract before generating or executing Bruno cases."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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


MODE_ALIASES = {
    "seres.sign": "seres-sign",
    "seres_sign": "seres-sign",
    "seres-sign": "seres-sign",
    "oauth2-client-credentials": "oauth2",
    "oauth2-authorization-code": "oauth2",
    "session": "cookie",
    "cookie-session": "cookie",
}


def canonical_mode(value: Any) -> str:
    name = str(value or "").strip().lower()
    return MODE_ALIASES.get(name, name)


def mode_settings(config: dict[str, Any], mode: str) -> dict[str, Any]:
    modes = config.get("modes") if isinstance(config.get("modes"), dict) else {}
    for name, value in modes.items():
        if canonical_mode(name) == mode and isinstance(value, dict):
            return value
    return {}


def configured_env_name(value: Any, default: str, label: str) -> str:
    """Return a usable environment variable name or reject bad config."""

    candidate = default if value is None else str(value).strip()
    if not candidate or candidate.lower() in {"none", "null"}:
        raise ValueError(f"{label} must name an environment variable")
    return candidate


def load_auth_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"authentication config does not exist: {path}")
    try:
        import yaml  # type: ignore[import-not-found]
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
    except ModuleNotFoundError as exc:
        raise ValueError("authentication YAML requires PyYAML") from exc
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:  # type: ignore[attr-defined]
        raise ValueError(f"cannot parse authentication config {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise ValueError(f"authentication config must contain an object: {path}")
    return config


def configured_base_url_env(path: Path) -> str:
    config = load_auth_config(path)
    return configured_env_name(config.get("base_url_env"), "BASE_URL", "base_url_env") if config else "BASE_URL"


def configured_auth_envs(path: Path) -> list[str]:
    config = load_auth_config(path)
    mode_value = config.get("mode")
    modes = config.get("modes") if isinstance(config.get("modes"), dict) else {}
    enabled_modes = {
        canonical_mode(name)
        for name, value in modes.items()
        if isinstance(value, dict) and value.get("enabled") is True
    }
    enabled_modes.update(
        canonical_mode(name)
        for name, value in config.items()
        if name not in {"mode", "modes", "version", "base_url_env"} and value is True
    )
    if not isinstance(mode_value, str) or not mode_value.strip():
        if len(enabled_modes) > 1:
            raise ValueError("authentication config enables more than one mode: " + ", ".join(sorted(enabled_modes)))
        mode_value = next(iter(enabled_modes), "seres-sign")
    mode = canonical_mode(mode_value)
    allowed = {
        "none", "disabled", "seres-sign", "bearer", "bearer-token", "token",
        "oauth2", "cookie", "api-key", "apikey", "headers", "custom", "custom-headers",
    }
    if mode not in allowed:
        raise ValueError(f"unsupported authentication mode: {mode}")
    if enabled_modes - {mode}:
        raise ValueError("authentication config enables more than one mode: " + ", ".join(sorted(enabled_modes)))
    if config.get(str(mode_value)) is False or config.get(mode) is False or mode in {"none", "disabled"}:
        return [configured_env_name(config.get("base_url_env"), "BASE_URL", "base_url_env")]
    settings = mode_settings(config, mode)
    if settings.get("enabled") is False:
        return [configured_env_name(config.get("base_url_env"), "BASE_URL", "base_url_env")]
    envs = [configured_env_name(config.get("base_url_env"), "BASE_URL", "base_url_env")]
    if mode == "seres-sign":
        signature = settings.get("signature") if isinstance(settings.get("signature"), dict) else {}
        parameter_config = signature.get("parameters") if isinstance(signature.get("parameters"), dict) else {}
        envs.extend([
            configured_env_name(
                parameter_config.get("secret_key_env", signature.get("secret_key_env", settings.get("secret_key_env"))),
                "SECRET_KEY",
                "secret_key_env",
            ),
            configured_env_name(
                parameter_config.get("access_key_env", signature.get("access_key_env", settings.get("access_key_env"))),
                "ACCESS_KEY",
                "access_key_env",
            ),
        ])
        extra_headers = settings.get("extra_headers") if isinstance(settings.get("extra_headers"), dict) else {}
        for value in extra_headers.values():
            envs.append(
                configured_env_name(value.get("env"), "", "extra header env")
                if isinstance(value, dict)
                else configured_env_name(value, "", "extra header env")
            )
    elif mode in {"bearer", "bearer-token", "token", "oauth2", "api-key", "apikey"}:
        envs.append(
            configured_env_name(
                settings.get("token_env"),
                "ACCESS_TOKEN" if mode not in {"api-key", "apikey"} else "API_KEY",
                "token_env",
            )
        )
    elif mode == "cookie":
        envs.append(
            configured_env_name(
                settings.get("cookie_env", settings.get("token_env")),
                "SESSION_COOKIE",
                "cookie_env",
            )
        )
    elif mode in {"headers", "custom", "custom-headers"}:
        headers = settings.get("headers") if isinstance(settings.get("headers"), dict) else {}
        if not headers:
            raise ValueError("custom request authentication mode requires modes.custom.headers")
        envs.extend(
            configured_env_name(value.get("env"), "", "custom header env")
            if isinstance(value, dict)
            else configured_env_name(value, "", "custom header env")
            for value in headers.values()
        )
    return list(dict.fromkeys(envs))


def atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent, prefix=f".{path.name}.") as handle:
        handle.write(payload)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", type=validate_base_url)
    parser.add_argument("--public-path", help="public route used to prove routing exists")
    parser.add_argument("--public-method", default="GET")
    parser.add_argument("--public-status", type=parse_statuses, default=parse_statuses("200-399"))
    parser.add_argument(
        "--require-public-route",
        action="store_true",
        help="make the optional public route probe a required capability",
    )
    parser.add_argument("--admin-path", help="secured route requested without credentials")
    parser.add_argument("--admin-method", default="GET")
    parser.add_argument("--admin-status", type=parse_statuses, default=parse_statuses("200,401,403"))
    parser.add_argument("--admin-code", help="expected error-envelope code for the unauthenticated admin request")
    parser.add_argument(
        "--require-admin-baseline",
        action="store_true",
        help="make the optional unauthenticated protected-route probe a required capability",
    )
    parser.add_argument("--openapi", type=Path, help="saved offline OpenAPI document to fingerprint")
    parser.add_argument("--expected-openapi-sha256")
    parser.add_argument("--auth-config", type=Path, help="request-auth.yaml used to derive required credential variables")
    parser.add_argument("--require-env", action="append", default=[])
    parser.add_argument("--fixture", action="append", type=Path, default=[])
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    checks: list[dict[str, Any]] = []
    failures: list[str] = []
    base_url_env = "BASE_URL"
    auth_config_error: ValueError | None = None
    if args.auth_config:
        try:
            base_url_env = configured_base_url_env(args.auth_config)
        except ValueError as exc:
            auth_config_error = exc
            failures.append(str(exc))
    if not args.base_url and os.environ.get(base_url_env):
        try:
            args.base_url = validate_base_url(str(os.environ[base_url_env]))
        except argparse.ArgumentTypeError as exc:
            failures.append(f"environment variable {base_url_env} has an invalid base URL: {exc}")
    if not args.base_url:
        failures.append(f"base URL is missing; provide --base-url or {base_url_env}")
    if args.timeout <= 0:
        failures.append("timeout must be positive")

    required_envs = list(args.require_env)
    if args.auth_config and auth_config_error is None:
        try:
            configured_envs = configured_auth_envs(args.auth_config)
            # An explicit CLI URL is an intentional override for the
            # configured base-url variable; credential variables remain
            # mandatory because Bruno still needs them at request time.
            if args.base_url:
                configured_envs = [name for name in configured_envs if name != base_url_env]
            required_envs.extend(configured_envs)
        except ValueError as exc:
            failures.append(str(exc))
    for name in dict.fromkeys(required_envs):
        present = bool(os.environ.get(name))
        checks.append({"name": f"env:{name}", "ok": present})
        if not present:
            failures.append(f"required environment variable is missing: {name}")
    for fixture in args.fixture:
        present = fixture.is_file()
        checks.append({"name": f"fixture:{fixture}", "ok": present})
        if not present:
            failures.append(f"required fixture is missing: {fixture}")

    if args.openapi:
        if not args.openapi.is_file():
            failures.append(f"offline OpenAPI document is missing: {args.openapi}")
        else:
            digest = hashlib.sha256(args.openapi.read_bytes()).hexdigest()
            match = not args.expected_openapi_sha256 or digest == args.expected_openapi_sha256.lower()
            checks.append({"name": "openapi_fingerprint", "ok": match, "sha256": digest})
            if not match:
                failures.append(f"offline OpenAPI fingerprint mismatch: expected {args.expected_openapi_sha256}, got {digest}")

    if args.base_url and args.public_path:
        result = request(join_url(args.base_url, args.public_path), args.public_method, args.timeout)
        ok = result["status"] in args.public_status
        checks.append({"name": "public_route", "ok": ok, "status": result["status"], "path": args.public_path})
        if not ok:
            failures.append(f"public route {args.public_method} {args.public_path} returned {result['status']!r}; expected one of {sorted(args.public_status)}")
    elif args.base_url and args.require_public_route:
        failures.append("public route is not configured; provide --public-path")

    if args.base_url and args.admin_path:
        result = request(join_url(args.base_url, args.admin_path), args.admin_method, args.timeout)
        code = body_code(result.get("body"))
        status_ok = result["status"] in args.admin_status
        code_ok = (args.admin_code is not None and str(code) == str(args.admin_code)) or (
            args.admin_code is None and result["status"] != 200
        )
        ok = status_ok and code_ok
        checks.append({"name": "admin_auth_baseline", "ok": ok, "status": result["status"], "code": code, "path": args.admin_path})
        if not ok:
            failures.append(f"admin baseline {args.admin_method} {args.admin_path} returned status={result['status']!r}, code={code!r}; expected status={sorted(args.admin_status)} code={args.admin_code!r}")
    elif args.base_url and args.require_admin_baseline:
        failures.append("admin auth baseline is not configured; provide --admin-path")

    report = {
        "version": 1,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "status": "runnable" if not failures else "blocked",
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
