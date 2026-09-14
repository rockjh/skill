#!/usr/bin/env python3
"""Load the minimal QA config and the extended Bruno environment format."""

from __future__ import annotations

import json
import re
import shutil
import stat
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

RUNTIME_CONFIG_ENV = "__QA_EXECUTION_CONFIG"
COLLECTION_MARKER = "bru-api-test-generator: runtime-config"
COLLECTION_END_MARKER = "bru-api-test-generator: runtime-config-end"
SIGN_MODES = {"disabled", "seres-sign"}
SIGN_ENV_NAMES = ("ACCESS_KEY", "SECRET_KEY")
ENVIRONMENT_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
VARIABLE_RE = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")

DEFAULT_CONFIG_TEMPLATE = """active_environment: local
sign: disabled
"""

DEFAULT_ENVIRONMENT_TEMPLATE = """vars {
  baseUrl: http://127.0.0.1:9527
  AUTH_TOKEN: ""
  SESSION_COOKIE: ""
  ACCESS_KEY: ""
  SECRET_KEY: ""
}

headers {
  Authorization: "Bearer {{AUTH_TOKEN}}"
  Cookie: "{{SESSION_COOKIE}}"
}
"""

DEFAULT_PLANS_TEMPLATE = """plans:
  smoke:
    risks:
      - read-only
    max_cases_per_module: 3

  regression:
    risks:
      - read-only
      - isolated-write

  full:
    risks:
      - read-only
      - isolated-write
      - destructive
      - external-side-effect
    require_confirm: true
"""

BRUNO_JSON_TEMPLATE = {
    "version": "1",
    "name": "API QA",
    "type": "collection",
    "ignore": ["node_modules", ".git"],
}

RUN_BAT_TEMPLATE = r"""@echo off
setlocal
python "%~dp0..\scripts\mno_bruno_qa.py" run --qa-root "%~dp0.." %*
exit /b %errorlevel%
"""

RUN_SH_TEMPLATE = """#!/usr/bin/env sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$SCRIPT_DIR/../scripts/mno_bruno_qa.py" run --qa-root "$SCRIPT_DIR/.." "$@"
"""

SHARED_RUN_BAT_TEMPLATE = r"""@echo off
setlocal
mno-bruno-qa run --qa-root "%~dp0.." %*
exit /b %errorlevel%
"""

SHARED_RUN_SH_TEMPLATE = """#!/usr/bin/env sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec mno-bruno-qa run --qa-root "$SCRIPT_DIR/.." "$@"
"""

EXECUTION_README_TEMPLATE = """# Bruno 执行入口

`config.yaml` 只选择环境并控制 SERES 签名；公共 Header 在当前环境文件的
`headers {}` 块中维护，由 `collection.bru` 统一注入，请求自身 Header 优先。

```bat
qa\\execution\\run.bat --module ac --risk read-only
qa\\execution\\run.bat --module ac --risk isolated-write --confirm-write
qa\\execution\\run.bat --plan smoke
```

```sh
./qa/execution/run.sh --module ac --risk read-only
./qa/execution/run.sh --plan regression --confirm-write
```

默认只执行 `read-only`。写入、破坏性操作和外部副作用分别需要对应确认参数。
模块运行只报告该模块，不更新 `contracts/version-lock.yaml`。
"""

COLLECTION_TEMPLATE = f'''auth {{
  mode: none
}}

script:pre-request {{
  // {COLLECTION_MARKER}
  const rawConfig = bru.getEnvVar("{RUNTIME_CONFIG_ENV}");
  if (!rawConfig) throw new Error("runtime execution config is missing");
  const runtimeConfig = JSON.parse(rawConfig);

  const setCommonHeader = (name, value) => {{
    if (value === undefined || value === null || value === "") return;
    const current = req.getHeader(name);
    if (current === undefined || current === null) req.setHeader(name, String(value));
  }};
  const requireEnv = (name) => {{
    const value = bru.getEnvVar(name);
    if (!value) throw new Error(`${{name}} must be configured in the Bruno environment`);
    return value;
  }};

  Object.entries(runtimeConfig.headers || {{}}).forEach(([name, value]) => {{
    setCommonHeader(name, value);
  }});

  if (runtimeConfig.sign === "seres-sign") {{
    const CryptoJS = require("crypto-js");
    const accessKey = requireEnv("ACCESS_KEY");
    const secretKey = requireEnv("SECRET_KEY");
    const requestPath = req.getPath() || "/";
    const queryString = req.getQueryString() || "";
    const params = {{}};
    queryString.split("&").filter(Boolean).forEach((pair) => {{
      const separator = pair.indexOf("=");
      const rawKey = separator >= 0 ? pair.slice(0, separator) : pair;
      const rawValue = separator >= 0 ? pair.slice(separator + 1) : "";
      const key = decodeURIComponent(rawKey.replace(/\\+/g, " "));
      const value = decodeURIComponent(rawValue.replace(/\\+/g, " "));
      if (Object.prototype.hasOwnProperty.call(params, key)) {{
        params[key] = Array.isArray(params[key]) ? params[key].concat(value) : [params[key], value];
      }} else {{
        params[key] = value;
      }}
    }});
    const configuredTimestamp = req.getHeader("timestamp");
    params.timestamp = configuredTimestamp === undefined || configuredTimestamp === null
      ? Date.now().toString()
      : String(configuredTimestamp);
    Object.entries(params).forEach(([key, value]) => {{
      if (value === null || value === undefined || value === "") delete params[key];
      else if (Array.isArray(value)) params[key] = `[${{value.sort().join(",")}}]`;
    }});
    const signParams = Object.keys(params).sort().map((key) => `${{key}}=${{params[key]}}`).join("&");
    const rawBody = req.getBody();
    const body = typeof rawBody === "string" ? rawBody : (rawBody ? JSON.stringify(rawBody) : "");
    const signText = body
      ? `${{requestPath}}&${{body}}&${{signParams}}&${{secretKey}}`
      : `${{requestPath}}&${{signParams}}&${{secretKey}}`;
    setCommonHeader("sign", CryptoJS.SHA256(signText).toString());
    setCommonHeader("timestamp", params.timestamp);
    setCommonHeader("accesskey", accessKey);
  }}
  // {COLLECTION_END_MARKER}
}}
'''


def _reject_unknown(document: dict[str, Any], allowed: set[str], location: str) -> None:
    unknown = sorted(set(document) - allowed)
    if unknown:
        raise ValueError(f"{location} contains unsupported field(s): {', '.join(unknown)}")


def validate_execution_config(document: Any) -> dict[str, str]:
    if not isinstance(document, dict):
        raise ValueError("execution config must contain an object")
    _reject_unknown(document, {"active_environment", "sign"}, "execution config")
    active = document.get("active_environment")
    if not isinstance(active, str) or not active.strip():
        raise ValueError("active_environment must be a non-empty environment name")
    active = active.strip()
    if active.lower().endswith(".bru") or not ENVIRONMENT_FILE_RE.fullmatch(active):
        raise ValueError("active_environment must be a safe file name without a path or .bru suffix")
    sign = document.get("sign", "disabled")
    if not isinstance(sign, str) or sign not in SIGN_MODES:
        raise ValueError("sign must be one of: disabled, seres-sign")
    return {"active_environment": active, "sign": sign}


def load_execution_config(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ValueError(f"execution config does not exist: {path}")
    try:
        import yaml  # type: ignore[import-not-found]

        document = yaml.safe_load(path.read_text(encoding="utf-8", errors="strict"))
    except ModuleNotFoundError as exc:
        raise ValueError("execution config requires PyYAML") from exc
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:  # type: ignore[attr-defined]
        raise ValueError(f"cannot parse execution config {path}: {exc}") from exc
    return validate_execution_config(document)


def environment_file(config_path: Path, config: dict[str, Any]) -> Path:
    return config_path.parent / "environments" / f"{config['active_environment']}.bru"


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def load_bruno_environment_document(path: Path) -> dict[str, dict[str, str]]:
    """Parse vars/vars:secret and the skill-owned headers block."""

    if not path.is_file():
        raise ValueError(f"Bruno environment does not exist: {path}")
    try:
        lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read Bruno environment {path}: {exc}") from exc
    result: dict[str, dict[str, str]] = {"vars": {}, "headers": {}}
    section: str | None = None
    for line_no, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not section:
            match = re.fullmatch(r"(vars(?::secret)?|headers)\s*\{", line)
            if match:
                section = "headers" if match.group(1) == "headers" else "vars"
            elif line and not line.startswith(("#", "//", "~")):
                raise ValueError(f"unsupported environment syntax at {path}:{line_no}: {line}")
            continue
        if line == "}":
            section = None
            continue
        if not line or line.startswith(("#", "//", "~")):
            continue
        match = re.match(r"^([^:]+):\s*(.*)$", line)
        if not match:
            raise ValueError(f"invalid {section} entry at {path}:{line_no}")
        name = match.group(1).strip()
        value = _unquote(match.group(2))
        if section == "vars" and not ENV_NAME_RE.fullmatch(name):
            raise ValueError(f"invalid environment variable name at {path}:{line_no}: {name!r}")
        if section == "headers" and not HEADER_NAME_RE.fullmatch(name):
            raise ValueError(f"invalid HTTP Header name at {path}:{line_no}: {name!r}")
        result[section][name] = value
    if section:
        raise ValueError(f"unterminated {section} block in {path}")
    return result


def load_bruno_environment(path: Path) -> dict[str, str]:
    """Compatibility helper returning only Bruno variables."""

    return load_bruno_environment_document(path)["vars"]


def resolved_environment_headers(document: dict[str, dict[str, str]]) -> dict[str, str]:
    variables = document["vars"]
    headers: dict[str, str] = {}
    for name, template in document["headers"].items():
        missing = [key for key in VARIABLE_RE.findall(template) if not variables.get(key)]
        if missing:
            continue
        value = VARIABLE_RE.sub(lambda match: variables[match.group(1)], template)
        if value:
            headers[name] = value
    return headers


def required_environment_names(config: dict[str, Any]) -> list[str]:
    return list(SIGN_ENV_NAMES if config["sign"] == "seres-sign" else ())


def runtime_payload(config: dict[str, Any], environment: dict[str, dict[str, str]] | None = None) -> str:
    payload = {
        "sign": config["sign"],
        "headers": resolved_environment_headers(environment) if environment else {},
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def render_runtime_environment(document: dict[str, dict[str, str]]) -> str:
    """Render only native Bruno vars; the custom headers block stays tool-owned."""

    lines = ["vars {"]
    for name, value in document["vars"].items():
        lines.append(f"  {name}: {json.dumps(value, ensure_ascii=False)}")
    return "\n".join([*lines, "}", ""])


def _write_if_missing(path: Path, content: str, newline: str = "\n") -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.replace("\n", newline), encoding="utf-8", newline="")
    return True


def migrate_legacy_execution_config(config_path: Path, environments_root: Path) -> list[Path]:
    """Move legacy auth/custom_headers values into the active environment."""

    if not config_path.is_file():
        return []
    try:
        import yaml  # type: ignore[import-not-found]

        legacy = yaml.safe_load(config_path.read_text(encoding="utf-8", errors="strict"))
    except ModuleNotFoundError as exc:
        raise ValueError("legacy config migration requires PyYAML") from exc
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:  # type: ignore[attr-defined]
        raise ValueError(f"cannot migrate legacy execution config {config_path}: {exc}") from exc
    if not isinstance(legacy, dict) or not ({"auth", "custom_headers"} & set(legacy)):
        return []
    active = str(legacy.get("active_environment") or "local")
    auth = legacy.get("auth", {}) if isinstance(legacy.get("auth"), dict) else {}
    mode = str(auth.get("mode") or "none")
    sign = "seres-sign" if mode == "seres-sign" else "disabled"
    env_path = environments_root / f"{active}.bru"
    document = load_bruno_environment_document(env_path) if env_path.is_file() else {"vars": {}, "headers": {}}
    headers = document["headers"]
    auth_headers = {
        "bearer": ("Authorization", f"Bearer {{{{{auth.get('token_env', 'ACCESS_TOKEN')}}}}}"),
        "api-key": ("X-API-Key", f"{{{{{auth.get('key_env', 'API_KEY')}}}}}"),
        "cookie": ("Cookie", f"{{{{{auth.get('cookie_env', 'SESSION_COOKIE')}}}}}"),
    }
    if mode in auth_headers:
        name, value = auth_headers[mode]
        headers.setdefault(name, value)
    custom = legacy.get("custom_headers", {}) if isinstance(legacy.get("custom_headers"), dict) else {}
    for name, settings in custom.items():
        if not isinstance(name, str) or not isinstance(settings, dict):
            continue
        if isinstance(settings.get("env"), str):
            headers.setdefault(name, f"{{{{{settings['env']}}}}}")
        elif isinstance(settings.get("value"), str):
            headers.setdefault(name, settings["value"])
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(
        render_runtime_environment(document)
        + ("headers {\n" + "".join(f"  {name}: {json.dumps(value, ensure_ascii=False)}\n" for name, value in headers.items()) + "}\n" if headers else ""),
        encoding="utf-8",
    )
    config_path.write_text(f"active_environment: {active}\nsign: {sign}\n", encoding="utf-8")
    return [config_path, env_path]


def migrate_legacy_base_url(config_path: Path, environments_root: Path) -> list[Path]:
    """Rename the old BASE_URL variable to the business-readable baseUrl key."""

    if not config_path.is_file():
        return []
    try:
        import yaml  # type: ignore[import-not-found]

        config = yaml.safe_load(config_path.read_text(encoding="utf-8", errors="strict"))
    except (ModuleNotFoundError, OSError, UnicodeDecodeError, yaml.YAMLError) as exc:  # type: ignore[attr-defined]
        raise ValueError(f"cannot migrate base URL variable: {exc}") from exc
    if not isinstance(config, dict):
        return []
    active = str(config.get("active_environment") or "local")
    env_path = environments_root / f"{active}.bru"
    if not env_path.is_file():
        return []
    document = load_bruno_environment_document(env_path)
    variables = document["vars"]
    if "BASE_URL" not in variables or "baseUrl" in variables:
        return []
    variables["baseUrl"] = variables.pop("BASE_URL")
    env_path.write_text(render_runtime_environment(document) + render_headers_block(document), encoding="utf-8")
    return [env_path]


def render_headers_block(document: dict[str, dict[str, str]]) -> str:
    headers = document.get("headers", {})
    if not headers:
        return ""
    return "headers {\n" + "".join(
        f"  {name}: {json.dumps(value, ensure_ascii=False)}\n" for name, value in headers.items()
    ) + "}\n"


def _tooling_mode(path: Path, requested_local_scripts: bool | None) -> bool:
    if requested_local_scripts is not None:
        return requested_local_scripts
    if not path.is_file():
        return True
    try:
        import yaml  # type: ignore[import-not-found]

        document = yaml.safe_load(path.read_text(encoding="utf-8", errors="strict"))
    except (ModuleNotFoundError, OSError, UnicodeDecodeError):
        return True
    return not (isinstance(document, dict) and document.get("tooling") == "shared-cli")


def initialize_execution_layout(qa_root: Path, local_scripts: bool | None = None) -> list[Path]:
    """Create reusable execution assets and synchronize the project scripts."""

    qa_root = qa_root.resolve()
    contracts_root = qa_root / "contracts"
    bruno_root = qa_root / "bruno"
    execution_root = qa_root / "execution"
    old_environments = bruno_root / "environments"
    new_environments = execution_root / "environments"
    config_path = execution_root / "config.yaml"
    qa_config_path = qa_root / "qa.yaml"
    local_scripts = _tooling_mode(qa_config_path, local_scripts)
    changed: list[Path] = []
    if old_environments.exists():
        if new_environments.exists():
            raise ValueError(
                f"cannot migrate Bruno environments because both directories exist: {old_environments}, {new_environments}"
            )
        execution_root.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old_environments), str(new_environments))
        changed.append(new_environments)
    else:
        new_environments.mkdir(parents=True, exist_ok=True)

    changed.extend(migrate_legacy_execution_config(config_path, new_environments))
    changed.extend(migrate_legacy_base_url(config_path, new_environments))

    qa_config = f"version: 1\ntooling: {'project-scripts' if local_scripts else 'shared-cli'}\n"
    if not qa_config_path.is_file() or qa_config_path.read_text(encoding="utf-8", errors="strict") != qa_config:
        qa_config_path.write_text(qa_config, encoding="utf-8")
        changed.append(qa_config_path)
    run_bat_template = RUN_BAT_TEMPLATE if local_scripts else SHARED_RUN_BAT_TEMPLATE
    run_sh_template = RUN_SH_TEMPLATE if local_scripts else SHARED_RUN_SH_TEMPLATE
    files = (
        (config_path, DEFAULT_CONFIG_TEMPLATE, "\n"),
        (new_environments / "local.bru", DEFAULT_ENVIRONMENT_TEMPLATE, "\n"),
        (execution_root / "plans.yaml", DEFAULT_PLANS_TEMPLATE, "\n"),
        (execution_root / "run.bat", run_bat_template, "\r\n"),
        (execution_root / "run.sh", run_sh_template, "\n"),
        (execution_root / "README.md", EXECUTION_README_TEMPLATE, "\n"),
        (bruno_root / "collection.bru", COLLECTION_TEMPLATE, "\n"),
        (bruno_root / "bruno.json", json.dumps(BRUNO_JSON_TEMPLATE, ensure_ascii=False, indent=2) + "\n", "\n"),
    )
    for path, content, newline in files:
        if _write_if_missing(path, content, newline):
            changed.append(path)
    changed.extend(migrate_legacy_base_url(config_path, new_environments))
    for path, desired, known in (
        (execution_root / "run.bat", run_bat_template.replace("\n", "\r\n"), {RUN_BAT_TEMPLATE.replace("\n", "\r\n"), SHARED_RUN_BAT_TEMPLATE.replace("\n", "\r\n")}),
        (execution_root / "run.sh", run_sh_template, {RUN_SH_TEMPLATE, SHARED_RUN_SH_TEMPLATE}),
    ):
        current = path.read_text(encoding="utf-8", errors="strict")
        if current in known and current != desired:
            path.write_text(desired, encoding="utf-8", newline="")
            changed.append(path)
    collection_path = bruno_root / "collection.bru"
    if collection_path.is_file():
        current_collection = collection_path.read_text(encoding="utf-8", errors="strict")
        if COLLECTION_MARKER in current_collection and current_collection != COLLECTION_TEMPLATE:
            collection_path.write_text(COLLECTION_TEMPLATE, encoding="utf-8")
            changed.append(collection_path)
    run_sh = execution_root / "run.sh"
    run_sh.chmod(run_sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    config = load_execution_config(config_path)
    load_bruno_environment_document(environment_file(config_path, config))

    if local_scripts:
        try:
            from scripts_manager import sync_scripts

            changed.extend(sync_scripts(qa_root, Path(__file__).resolve().parent))
        except ImportError:
            pass
    for obsolete in (contracts_root / "request-auth.yaml", contracts_root / "request-context.yaml"):
        if obsolete.is_file():
            obsolete.unlink()
            changed.append(obsolete)
    return list(dict.fromkeys(changed))
