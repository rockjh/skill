#!/usr/bin/env python3
"""Load, validate, and materialize the shared Bruno execution configuration."""

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
AUTH_MODES = {"none", "seres-sign", "bearer", "api-key", "cookie"}
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ENVIRONMENT_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


DEFAULT_CONFIG_TEMPLATE = """# 当前激活环境。
# 这里填写环境名称，不包含 .bru 后缀。
# local 对应 environments/local.bru。
active_environment: local

auth:
  # 请求认证模式，可选值：
  # none：不添加认证信息，适用于默认本地访问。
  # seres-sign：使用 SERES Sign 签名。
  # bearer：添加 Authorization: Bearer <token>。
  # api-key：添加 X-API-Key。
  # cookie：添加 Cookie。
  mode: none

  # mode 为 seres-sign 时取消下面两行注释。
  # 配置值是 Bruno 环境变量名，不是真实密钥。
  # access_key_env: ACCESS_KEY
  # secret_key_env: SECRET_KEY

  # mode 为 bearer 时配置令牌对应的环境变量名。
  # token_env: ACCESS_TOKEN

  # mode 为 api-key 时配置 API Key 对应的环境变量名。
  # key_env: API_KEY

  # mode 为 cookie 时配置完整 Cookie 对应的环境变量名。
  # cookie_env: SESSION_COOKIE

custom_headers:
  # 节点名称就是实际发送的 HTTP Header 名称。
  operatorInfo:
    # env 表示从当前 Bruno 环境读取值。
    # local.bru 中该值为空时，不发送这个 Header。
    env: OPERATOR_INFO

    # 只向匹配的请求路径注入，匹配时忽略查询参数。
    paths:
      - "/v*/admin/**"

  # 非敏感固定值使用 value；value 和 env 不能同时配置。
  # X-Gray-Traffic:
  #   value: "true"
  #   paths:
  #     - "/v*/internal/**"
"""


BRUNO_JSON_TEMPLATE = {
    "version": "1",
    "name": "API QA",
    "type": "collection",
    "ignore": ["node_modules", ".git"],
}


RUN_BAT_TEMPLATE = r"""@echo off
setlocal

rem 默认执行全部模块。
rem 执行指定模块示例：qa\execution\run.bat --module "车辆管理"
python "%~dp0..\scripts\run_bruno.py" %*
exit /b %errorlevel%
"""


RUN_SH_TEMPLATE = """#!/usr/bin/env sh
set -eu

# 默认执行全部模块。
# 执行指定模块示例：./qa/execution/run.sh --module "车辆管理"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$SCRIPT_DIR/../scripts/run_bruno.py" "$@"
"""


EXECUTION_README_TEMPLATE = r"""# Bruno 执行入口

`config.yaml` 是唯一公共运行配置。环境文件独立放在 `environments/`，认证和公共 Header 由 `../bruno/collection.bru` 在运行时统一注入。

Windows：

```bat
qa\execution\run.bat
qa\execution\run.bat --module "车辆管理"
```

Linux/macOS：

```sh
./qa/execution/run.sh
./qa/execution/run.sh --module "车辆管理"
```

模块运行只报告该模块结果且不会更新 `contracts/version-lock.yaml`。完整运行通过全部校验后才允许更新版本锁。

Bruno GUI 不会自动发现集合外的环境文件。GUI 调试时需要手工导入 `environments/*.bru`，且 GUI 不会自动读取 `config.yaml`；标准执行方式是本目录的 BAT/SH 入口。
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

  const auth = runtimeConfig.auth;
  if (auth.mode === "bearer") {{
    setCommonHeader("Authorization", `Bearer ${{requireEnv(auth.token_env)}}`);
  }} else if (auth.mode === "api-key") {{
    setCommonHeader("X-API-Key", requireEnv(auth.key_env));
  }} else if (auth.mode === "cookie") {{
    setCommonHeader("Cookie", requireEnv(auth.cookie_env));
  }} else if (auth.mode === "seres-sign") {{
    const CryptoJS = require("crypto-js");
    const secretKey = requireEnv(auth.secret_key_env);
    const accessKey = requireEnv(auth.access_key_env);
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

  const requestPath = (req.getPath() || "/").split("?", 1)[0];
  const globMatches = (pattern, value) => {{
    let patternIndex = 0;
    let valueIndex = 0;
    let starPattern = -1;
    let starValue = -1;
    let starAllowsSlash = false;
    while (valueIndex < value.length) {{
      if (patternIndex < pattern.length && (pattern[patternIndex] === "?" || pattern[patternIndex] === value[valueIndex])) {{
        patternIndex += 1;
        valueIndex += 1;
      }} else if (patternIndex < pattern.length && pattern[patternIndex] === "*") {{
        starAllowsSlash = pattern[patternIndex + 1] === "*";
        patternIndex += starAllowsSlash ? 2 : 1;
        starPattern = patternIndex;
        starValue = valueIndex;
      }} else if (starPattern >= 0 && (starAllowsSlash || value[starValue] !== "/")) {{
        starValue += 1;
        valueIndex = starValue;
        patternIndex = starPattern;
      }} else {{
        return false;
      }}
    }}
    while (patternIndex < pattern.length && pattern[patternIndex] === "*") patternIndex += 1;
    return patternIndex === pattern.length;
  }};
  Object.entries(runtimeConfig.custom_headers || {{}}).forEach(([name, settings]) => {{
    if (settings.paths && !settings.paths.some((pattern) => globMatches(pattern, requestPath))) return;
    const value = Object.prototype.hasOwnProperty.call(settings, "env")
      ? bru.getEnvVar(settings.env)
      : settings.value;
    setCommonHeader(name, value);
  }});
  // {COLLECTION_END_MARKER}
}}
'''


def _reject_unknown(document: dict[str, Any], allowed: set[str], location: str) -> None:
    unknown = sorted(set(document) - allowed)
    if unknown:
        raise ValueError(f"{location} contains unsupported field(s): {', '.join(unknown)}")


def _environment_name(value: Any, location: str) -> str:
    if not isinstance(value, str) or not ENV_NAME_RE.fullmatch(value.strip()):
        raise ValueError(f"{location} must name a Bruno environment variable")
    return value.strip()


def validate_execution_config(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ValueError("execution config must contain an object")
    _reject_unknown(document, {"active_environment", "auth", "custom_headers"}, "execution config")

    active = document.get("active_environment")
    if not isinstance(active, str) or not active.strip():
        raise ValueError("active_environment must be a non-empty environment name")
    active = active.strip()
    if active.lower().endswith(".bru") or not ENVIRONMENT_FILE_RE.fullmatch(active):
        raise ValueError("active_environment must be a safe file name without a path or .bru suffix")

    auth = document.get("auth")
    if not isinstance(auth, dict):
        raise ValueError("auth must contain an object")
    mode = auth.get("mode")
    if not isinstance(mode, str) or mode not in AUTH_MODES:
        raise ValueError("auth.mode must be one of: none, seres-sign, bearer, api-key, cookie")
    mode_fields = {
        "none": set(),
        "seres-sign": {"access_key_env", "secret_key_env"},
        "bearer": {"token_env"},
        "api-key": {"key_env"},
        "cookie": {"cookie_env"},
    }
    _reject_unknown(auth, {"mode", *mode_fields[mode]}, "auth")
    for field in sorted(mode_fields[mode]):
        if field not in auth:
            raise ValueError(f"auth.{field} is required when auth.mode is {mode}")
        _environment_name(auth[field], f"auth.{field}")

    custom_headers = document.get("custom_headers", {})
    if custom_headers is None:
        custom_headers = {}
    if not isinstance(custom_headers, dict):
        raise ValueError("custom_headers must contain an object")
    normalized_headers: dict[str, dict[str, Any]] = {}
    for raw_name, raw_settings in custom_headers.items():
        if not isinstance(raw_name, str):
            raise ValueError("custom_headers keys must be HTTP Header names")
        name = raw_name
        if not HEADER_NAME_RE.fullmatch(name):
            raise ValueError(f"custom_headers contains an invalid HTTP Header name: {name!r}")
        if not isinstance(raw_settings, dict):
            raise ValueError(f"custom_headers.{name} must contain an object")
        _reject_unknown(raw_settings, {"env", "value", "paths"}, f"custom_headers.{name}")
        has_env = "env" in raw_settings
        has_value = "value" in raw_settings
        if has_env == has_value:
            raise ValueError(f"custom_headers.{name} must configure exactly one of env or value")
        settings = dict(raw_settings)
        if has_env:
            settings["env"] = _environment_name(settings["env"], f"custom_headers.{name}.env")
        elif not isinstance(settings["value"], str) or not settings["value"]:
            raise ValueError(f"custom_headers.{name}.value must be a non-empty string")
        paths = settings.get("paths")
        if paths is not None:
            if not isinstance(paths, list) or not paths:
                raise ValueError(f"custom_headers.{name}.paths must be a non-empty list")
            if any(not isinstance(path, str) or not path.startswith("/") or "?" in path or "#" in path for path in paths):
                raise ValueError(f"custom_headers.{name}.paths must contain request-path patterns without query strings")
            settings["paths"] = list(dict.fromkeys(paths))
        normalized_headers[name] = settings
    return {
        "active_environment": active,
        "auth": {"mode": mode, **{field: auth[field].strip() for field in mode_fields[mode]}},
        "custom_headers": normalized_headers,
    }


def load_execution_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"execution config does not exist: {path}")
    try:
        import yaml  # type: ignore[import-not-found]

        document = yaml.safe_load(path.read_text(encoding="utf-8", errors="strict"))
    except ModuleNotFoundError as exc:
        raise ValueError("execution config requires the existing PyYAML dependency") from exc
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:  # type: ignore[attr-defined]
        raise ValueError(f"cannot parse execution config {path}: {exc}") from exc
    return validate_execution_config(document)


def environment_file(config_path: Path, config: dict[str, Any]) -> Path:
    return config_path.parent / "environments" / f"{config['active_environment']}.bru"


def load_bruno_environment(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ValueError(f"Bruno environment does not exist: {path}")
    try:
        lines = path.read_text(encoding="utf-8", errors="strict").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read Bruno environment {path}: {exc}") from exc
    values: dict[str, str] = {}
    in_vars = False
    for raw_line in lines:
        line = raw_line.strip()
        if not in_vars:
            match = re.match(r"^vars(?::secret)?\s*\{(.*)$", line)
            if not match:
                continue
            in_vars = True
            line = match.group(1).strip()
        if "}" in line:
            line, _ = line.split("}", 1)
            closes = True
        else:
            closes = False
        if line and not line.startswith(("#", "//", "~")):
            match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", line)
            if match:
                value = match.group(2).strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                    value = value[1:-1]
                values[match.group(1)] = value
        if closes:
            in_vars = False
    return values


def required_environment_names(config: dict[str, Any]) -> list[str]:
    auth = config["auth"]
    fields = {
        "seres-sign": ("access_key_env", "secret_key_env"),
        "bearer": ("token_env",),
        "api-key": ("key_env",),
        "cookie": ("cookie_env",),
    }
    return ["BASE_URL", *(auth[field] for field in fields.get(auth["mode"], ()))]


def runtime_payload(config: dict[str, Any]) -> str:
    payload = {"auth": config["auth"], "custom_headers": config["custom_headers"]}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _write_if_missing(path: Path, content: str, newline: str = "\n") -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.replace("\n", newline), encoding="utf-8", newline="")
    return True


def initialize_execution_layout(qa_root: Path) -> list[Path]:
    """Create the shared runtime files and migrate a legacy environment directory."""

    qa_root = qa_root.resolve()
    contracts_root = qa_root / "contracts"
    bruno_root = qa_root / "bruno"
    execution_root = qa_root / "execution"
    old_environments = bruno_root / "environments"
    new_environments = execution_root / "environments"
    config_path = execution_root / "config.yaml"
    if config_path.exists():
        load_execution_config(config_path)
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

    files = (
        (config_path, DEFAULT_CONFIG_TEMPLATE, "\n"),
        (execution_root / "run.bat", RUN_BAT_TEMPLATE, "\r\n"),
        (execution_root / "run.sh", RUN_SH_TEMPLATE, "\n"),
        (execution_root / "README.md", EXECUTION_README_TEMPLATE, "\n"),
        (bruno_root / "collection.bru", COLLECTION_TEMPLATE, "\n"),
        (bruno_root / "bruno.json", json.dumps(BRUNO_JSON_TEMPLATE, ensure_ascii=False, indent=2) + "\n", "\n"),
    )
    for path, content, newline in files:
        if _write_if_missing(path, content, newline):
            changed.append(path)
    run_sh = execution_root / "run.sh"
    if run_sh.exists():
        run_sh.chmod(run_sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    load_execution_config(config_path)
    for obsolete in (contracts_root / "request-auth.yaml", contracts_root / "request-context.yaml"):
        if obsolete.is_file():
            obsolete.unlink()
            changed.append(obsolete)
    return changed
