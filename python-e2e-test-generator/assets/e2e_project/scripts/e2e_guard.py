#!/usr/bin/env python3
"""统一实现 E2E 发现、契约、静态、源码版本和执行门禁。"""

from __future__ import annotations

import argparse
import ast
import configparser
import datetime as dt
import hashlib
import http.client
import importlib.util
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import tomllib
import uuid
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml


sys.dont_write_bytecode = True

RULES_VERSION = 4
FIXED_ASSETS = {
    "common/e2e_runtime.py",
    "scripts/check_scenarios.py",
    "scripts/check_source_versions.py",
    "scripts/e2e_guard.py",
    "scripts/run.bat",
    "scripts/run.sh",
    "scripts/run_e2e.py",
    "tests/test_e2e_guard.py",
    "tests/test_e2e_runtime.py",
}
DISCOVERY_KEYS = {"schema_version", "inventory", "topology", "configuration", "runtime_probe", "gates"}
SEARCH_CATEGORIES = {"http_rpc", "messages", "database", "cache", "jobs", "configuration"}
SCENARIO_KEYS = {
    "meta",
    "generation",
    "readiness",
    "preconditions",
    "integrations",
    "controls",
    "isolation",
    "steps",
    "cleanup",
    "source",
}
CONTROL_NAMES = (
    "public_api",
    "test_or_admin_api",
    "mocks_and_faults",
    "dynamic_configuration",
    "scheduled_jobs",
    "messages",
    "database_read",
    "database_control",
    "observability",
)
CONTROL_STATUSES = {"usable", "unusable", "not_found", "not_applicable"}
WRITE_CONTROLS = {
    "public_api", "test_or_admin_api", "mocks_and_faults", "dynamic_configuration",
    "scheduled_jobs", "messages", "database_control",
}
INHERENT_WRITE_CONTROLS = {
    "test_or_admin_api", "mocks_and_faults", "dynamic_configuration",
    "scheduled_jobs", "messages", "database_control",
}
BUILD_NAMES = {
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "tox.ini",
    "package.json",
    "go.mod",
    "Cargo.toml",
}
MODULE_KINDS = {"application", "sdk", "starter", "client", "facade", "library", "e2e", "other"}
EDGE_MECHANISMS = {"build", "http", "rpc", "message", "database", "cache", "job", "configuration", "embedded"}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "target", "build", "dist", "__pycache__"}
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
ID_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")
ENV_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
PENDING_BLOCKER_RE = re.compile(r"^(config|credential|connection|business_data):[A-Za-z0-9_./#${}-]+$")
PLACEHOLDER_RE = re.compile(r"^\$\{[A-Z][A-Z0-9_]*\}$")
SOURCE_PLACEHOLDER_RE = re.compile(r"^\$\{[^{}:\s]+(?::[^{}]*)?\}$")
MUTATING_SQL_RE = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE|ALTER|DROP|TRUNCATE|REPLACE)\b", re.IGNORECASE)
SQL_RE = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|MERGE|ALTER|DROP|TRUNCATE|REPLACE)\b", re.IGNORECASE)
PARAMETER_RE = re.compile(r"(\?|%s|%\([A-Za-z_][A-Za-z0-9_]*\)s|:[A-Za-z_][A-Za-z0-9_]*|\$[1-9][0-9]*)")
URL_RE = re.compile(r"https?://", re.IGNORECASE)
CHINESE_RE = re.compile(r"[\u3400-\u9fff]")
REFERENCE_RE = re.compile(
    r"^(?:environment|env|secret-store|vault|config|config-center|file-key|provider):[A-Za-z0-9_./#${}-]+$"
)
SENSITIVE_TEXT_RE = re.compile(
    r"(?:\b(?:password|secret|token|authorization|auth|api[_-]?key|access[_-]?(?:key|token)|client[_-]?secret|private[_-]?key|ssh[_-]?key|passphrase|cookie|dsn)\s*[:=]\s*[^\s,;&]+"
    r"|--(?:password|secret|token|authorization|auth|api-key|access-key|access-token|client-secret|private-key|ssh-key|passphrase|cookie|dsn)(?:=|\s+)\S+)",
    re.IGNORECASE,
)
SAFE_CREDENTIAL_REFERENCE = (
    r"(?:\$\{[A-Z][A-Z0-9_]*\}"
    r"|(?:environment|env|secret-store|vault|config|config-center|file-key|provider):[A-Za-z0-9_./#${}-]+)"
)


def _line(path: Path, needle: str) -> int:
    """为诊断定位第一个相关文本行。"""

    try:
        for index, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if needle in text:
                return index
    except (OSError, UnicodeError):
        return 1
    return 1


def _error(path: Path, rule: str, message: str, needle: str = "") -> str:
    """生成带文件和行号的稳定诊断。"""

    return f"{path}:{_line(path, needle)}: {rule}: {message}"


def _load_yaml(path: Path, errors: list[str]) -> dict[str, Any]:
    """解析 YAML 映射并把错误加入诊断列表。"""

    if not path.is_file():
        errors.append(_error(path, "file-required", "文件不存在"))
        return {}
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        errors.append(_error(path, "yaml-parse", f"YAML 无法解析: {exc}"))
        return {}
    if not isinstance(document, dict):
        errors.append(_error(path, "yaml-shape", "顶层必须是映射"))
        return {}
    return document


def _load_json(path: Path, errors: list[str]) -> Any:
    """解析标准 JSON 并把错误加入诊断列表。"""

    if not path.is_file():
        errors.append(_error(path, "file-required", "文件不存在"))
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(_error(path, "json-parse", f"JSON 无法解析: {exc}"))
        return None


def _exact_keys(path: Path, value: Any, expected: set[str], rule: str, errors: list[str]) -> bool:
    """校验映射只包含规定键。"""

    if not isinstance(value, dict):
        errors.append(_error(path, rule, "必须是映射"))
        return False
    actual = set(value)
    if actual != expected:
        errors.append(_error(path, rule, f"键集合不符: expected={sorted(expected)}, actual={sorted(actual)}"))
        return False
    return True


def _strings(value: Any, *, nonempty: bool = True) -> bool:
    """判断值是否为去重的非空字符串列表。"""

    if not isinstance(value, list) or (nonempty and not value):
        return False
    if not all(isinstance(item, str) and item.strip() for item in value):
        return False
    return len(value) == len(set(value))


def _missing_placeholders(value: Any, prefix: str = "$") -> list[str]:
    """列出当前进程未提供的精确环境占位符路径。"""

    missing: list[str] = []
    if isinstance(value, str) and PLACEHOLDER_RE.fullmatch(value):
        name = value[2:-1]
        if not os.environ.get(name, "").strip():
            missing.append(f"{prefix} ({name})")
    elif isinstance(value, dict):
        for key, child in value.items():
            missing.extend(_missing_placeholders(child, f"{prefix}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            missing.extend(_missing_placeholders(child, f"{prefix}[{index}]"))
    return missing


def _missing_placeholder_blockers(value: Any, kind: str, path: str = "") -> set[str]:
    """把当前进程缺失的精确占位符转换为可校验的阻塞标识。"""

    blockers: set[str] = set()
    if isinstance(value, str) and PLACEHOLDER_RE.fullmatch(value):
        name = value[2:-1]
        if not os.environ.get(name, "").strip():
            normalized = re.sub(r"[^a-z0-9]", "", path.casefold())
            if kind == "business_data":
                blocker_kind = kind
            elif any(fragment in normalized for fragment in ("password", "secret", "token", "auth", "credential", "apikey", "accesskey")):
                blocker_kind = "credential"
            elif any(fragment in normalized for fragment in ("url", "uri", "host", "port", "endpoint", "address", "connection", "broker", "datasource", "dsn")):
                blocker_kind = "connection"
            else:
                blocker_kind = "config"
            blockers.add(f"{blocker_kind}:{name}")
    elif isinstance(value, dict):
        for key, child in value.items():
            blockers.update(_missing_placeholder_blockers(child, kind, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            blockers.update(_missing_placeholder_blockers(child, kind, f"{path}[{index}]"))
    return blockers


def _has_constructed_business_value(value: Any) -> bool:
    """判断业务输入是否含有无需环境注入即可构造的实际值。"""

    if isinstance(value, dict):
        return any(_has_constructed_business_value(child) for child in value.values())
    if isinstance(value, list):
        return any(_has_constructed_business_value(child) for child in value)
    return value not in (None, "") and not (isinstance(value, str) and PLACEHOLDER_RE.fullmatch(value))


def _resolve(project_root: Path, text: str) -> Path:
    """相对 E2E 工程解析可移植路径。"""

    expanded = Path(os.path.expandvars(text))
    return expanded.resolve() if expanded.is_absolute() else (project_root / expanded).resolve()


def _inside(path: Path, root: Path) -> bool:
    """判断解析后的路径是否仍位于声明根目录内。"""

    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _walk_files(root: Path) -> Iterable[Path]:
    """遍历工作区文件并跳过构建产物和依赖目录。"""

    for current, directories, files in os.walk(root):
        directories[:] = [name for name in directories if name not in SKIP_DIRS]
        current_path = Path(current)
        for name in files:
            yield current_path / name


def _detected_integration_signals(roots: Iterable[Path]) -> set[str]:
    """保守扫描源码与配置文本中的外部调用和基础设施信号。"""

    patterns = {
        "http_rpc": re.compile(r"https?://|restcontroller|webclient|feignclient|grpc|xmlrpc|requests\.", re.IGNORECASE),
        "messages": re.compile(r"kafka|mqtt|amqp|messageproducer|messageconsumer|\bpublish\s*\(", re.IGNORECASE),
        "database": re.compile(r"\b(select|insert|update|delete)\b|datasource|jdbc|sqlalchemy|@repository|jparepository|crudrepository", re.IGNORECASE),
        "cache": re.compile(r"redis|memcached|cacheable|cachemanager", re.IGNORECASE),
        "jobs": re.compile(r"scheduled|cron|quartz|celery|scheduler", re.IGNORECASE),
        "configuration": re.compile(r"config(?:uration)?|properties|profiles?|getenv|environment", re.IGNORECASE),
    }
    suffixes = {".py", ".java", ".kt", ".kts", ".go", ".rs", ".js", ".ts", ".cs", ".xml", ".yml", ".yaml", ".toml", ".properties", ".gradle"}
    detected: set[str] = set()
    for root in roots:
        for path in _walk_files(root):
            if path.suffix.casefold() not in suffixes:
                continue
            try:
                if path.stat().st_size > 2_000_000:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            detected.update(name for name, pattern in patterns.items() if pattern.search(text))
            if detected == SEARCH_CATEGORIES:
                return detected
    return detected


def _git_roots(root: Path) -> set[Path]:
    """发现工作区内所有 Git 根目录。"""

    found: set[Path] = set()
    for current, directories, _ in os.walk(root):
        current_path = Path(current)
        if ".git" in directories or (current_path / ".git").is_file():
            found.add(current_path.resolve())
            directories[:] = [name for name in directories if name != ".git"]
        directories[:] = [name for name in directories if name not in SKIP_DIRS]
    return found


def _e2e_roots(root: Path) -> set[Path]:
    """按目录命名和构建标记发现已有 E2E 工程。"""

    found: set[Path] = set()
    pattern = re.compile(r"(^|[-_.])(e2e|end[-_]?to[-_]?end)([-_.]|$)", re.IGNORECASE)
    for current, directories, files in os.walk(root):
        directories[:] = [name for name in directories if name not in SKIP_DIRS]
        current_path = Path(current)
        if pattern.search(current_path.name) and set(files).intersection({"pyproject.toml", "pytest.ini", "package.json", "pom.xml"}):
            found.add(current_path.resolve())
    return found


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """以只读方式调用 Git 并捕获输出。"""

    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _git_file(repo: Path, commit: str, path: Path) -> str | None:
    """读取提交中的仓库相对文件，避免用工作树漂移内容证明契约。"""

    try:
        relative = path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return None
    result = _git(repo, "show", f"{commit}:{relative}")
    return result.stdout if result.returncode == 0 else None


def _anchor_contexts(repo: Path, commit: str, anchor: str) -> list[tuple[str, str]]:
    """返回固定提交中锚点的全部文件及邻近源码。"""

    result = _git(repo, "grep", "-n", "--fixed-strings", anchor, commit, "--", ".")
    if result.returncode != 0:
        return []
    contexts: list[tuple[str, str]] = []
    cached_files: dict[str, list[str] | None] = {}
    for line in result.stdout.splitlines():
        match = re.match(r"^(?:[^:]+:)?(.+?):([1-9][0-9]*):(.*)$", line)
        if match is None:
            continue
        relative, line_text = match.group(1), match.group(3)
        if relative not in cached_files:
            content = _git(repo, "show", f"{commit}:{relative}")
            cached_files[relative] = content.stdout.splitlines() if content.returncode == 0 else None
        lines = cached_files[relative]
        if lines is None:
            contexts.append((relative, line_text))
            continue
        index = int(match.group(2)) - 1
        contexts.append((relative, "\n".join(lines[max(0, index - 2):index + 3])))
    return contexts


def _semantic_evidence(repo: Path, commit: str, anchor: str, category: str) -> bool:
    """核对锚点邻近源码与声明的集成类别相符。"""

    contexts = _anchor_contexts(repo, commit, anchor)
    if not contexts:
        return False
    patterns = {
        "http_rpc": r"https?://|rest|webclient|feign|requests\.|httpx\.|urlopen|grpc|rpc|stub\.|\.call\s*\(|\.invoke\s*\(",
        "http": r"https?://|rest|webclient|feign|requests\.|httpx\.|urlopen|\.get\s*\(|\.head\s*\(|\.post\s*\(|\.put\s*\(|\.patch\s*\(|\.delete\s*\(",
        "rpc": r"grpc|rpc|stub\.|\.call\s*\(|\.invoke\s*\(",
        "messages": r"kafka|mqtt|amqp|broker|producer|consumer|publish\s*\(|produce\s*\(|send\s*\(",
        "message": r"kafka|mqtt|amqp|broker|producer|consumer|publish\s*\(|produce\s*\(|send\s*\(",
        "database": r"\bselect\b|\binsert\b|\bupdate\b|\bdelete\b|datasource|jdbc|sqlalchemy|repository|cursor\.",
        "cache": r"redis|memcached|cacheable|cachemanager|cache\.",
        "jobs": r"scheduled|cron|quartz|celery|scheduler|trigger\s*\(",
        "job": r"scheduled|cron|quartz|celery|scheduler|trigger\s*\(",
        "configuration": r"config(?:uration)?|properties|profiles?|getenv|environment|config[-_]?center",
    }
    pattern = patterns.get(category)
    return any(
        category == "configuration"
        and Path(relative).suffix.casefold() in {".yml", ".yaml", ".json", ".toml", ".properties", ".ini", ".cfg", ".env"}
        or pattern is not None and re.search(pattern, context, re.IGNORECASE) is not None
        for relative, context in contexts
    )


def _anchor_owners(
    repo_id: str,
    repo: Path,
    commit: str,
    anchor: str,
    module_roots: Mapping[str, Path],
) -> set[str]:
    """Resolve every anchor occurrence to the most specific owning module."""

    owners: set[str] = set()
    for relative, _ in _anchor_contexts(repo, commit, anchor):
        source_path = (repo / relative).resolve()
        candidates = [
            node_id for node_id, module_root in module_roots.items()
            if node_id.split(":", 1)[0] == repo_id and _inside(source_path, module_root)
        ]
        if candidates:
            depth = max(len(module_roots[node_id].parts) for node_id in candidates)
            owners.update(node_id for node_id in candidates if len(module_roots[node_id].parts) == depth)
    return owners


def _build_descriptor_facts(path: Path, text: str) -> tuple[set[str], set[str]]:
    """从常见构建描述中提取工程标识与实际依赖标识。"""

    identifiers: set[str] = set()
    dependencies: set[str] = set()

    def add(target: set[str], value: Any) -> None:
        """加入非空构建标识。"""

        if isinstance(value, str) and value.strip():
            target.add(value.strip())

    name = path.name
    try:
        if name == "pom.xml":
            root = ET.fromstring(text)
            local = lambda tag: tag.rsplit("}", 1)[-1]
            for element in root.iter():
                if local(element.tag) in {"artifactId", "groupId", "module"}:
                    add(identifiers, element.text)
            for dependency in (item for item in root.iter() if local(item.tag) == "dependency"):
                for child in dependency:
                    if local(child.tag) == "artifactId":
                        add(dependencies, child.text)
            for module in (item for item in root.iter() if local(item.tag) == "module"):
                add(dependencies, module.text)
        elif name == "pyproject.toml":
            document = tomllib.loads(text)
            project = document.get("project", {})
            if isinstance(project, dict):
                add(identifiers, project.get("name"))
                for dependency in project.get("dependencies", []) if isinstance(project.get("dependencies"), list) else []:
                    match = re.match(r"\s*([A-Za-z0-9_.-]+)", str(dependency))
                    if match:
                        add(dependencies, match.group(1))
            poetry = document.get("tool", {}).get("poetry", {}) if isinstance(document.get("tool"), dict) else {}
            if isinstance(poetry, dict):
                add(identifiers, poetry.get("name"))
                if isinstance(poetry.get("dependencies"), dict):
                    dependencies.update(str(item) for item in poetry["dependencies"] if str(item).casefold() != "python")
            cargo = document.get("package", {})
            if isinstance(cargo, dict):
                add(identifiers, cargo.get("name"))
        elif name == "package.json":
            document = json.loads(text)
            if isinstance(document, dict):
                add(identifiers, document.get("name"))
                for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                    if isinstance(document.get(key), dict):
                        dependencies.update(str(item) for item in document[key])
                workspaces = document.get("workspaces", [])
                if isinstance(workspaces, list):
                    dependencies.update(str(item) for item in workspaces)
        elif name == "Cargo.toml":
            document = tomllib.loads(text)
            package = document.get("package", {})
            if isinstance(package, dict):
                add(identifiers, package.get("name"))
            for key in ("dependencies", "dev-dependencies", "build-dependencies"):
                if isinstance(document.get(key), dict):
                    dependencies.update(str(item) for item in document[key])
            workspace = document.get("workspace", {})
            if isinstance(workspace, dict) and isinstance(workspace.get("members"), list):
                dependencies.update(str(item) for item in workspace["members"])
        elif name == "go.mod":
            module = re.search(r"(?m)^\s*module\s+(\S+)", text)
            if module:
                add(identifiers, module.group(1))
            dependencies.update(re.findall(r"(?m)^\s*(?:require|replace)\s+([^\s(]+)", text))
            for block in re.findall(r"(?ms)^\s*require\s*\((.*?)\)", text):
                dependencies.update(re.findall(r"(?m)^\s*([^\s/][^\s]*)\s+v\S+", block))
        elif name in {"build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"}:
            identifiers.update(re.findall(r"(?:rootProject\.name\s*=|archivesBaseName\s*=)\s*['\"]([^'\"]+)", text))
            dependencies.update(re.findall(r"project\s*\(\s*['\"]:([^'\"]+)['\"]\s*\)", text))
            dependencies.update(re.findall(r"\binclude\s*\(?\s*['\"]:([^'\"]+)['\"]", text))
            for coordinate in re.findall(r"\b(?:implementation|api|compileOnly|runtimeOnly|testImplementation)\s*\(?\s*['\"]([^'\"]+)['\"]", text):
                parts = coordinate.split(":")
                add(dependencies, parts[1] if len(parts) > 1 else parts[0])
        elif name in {"setup.cfg", "tox.ini"}:
            parser = configparser.ConfigParser(interpolation=None)
            parser.read_string(text)
            if parser.has_option("metadata", "name"):
                add(identifiers, parser.get("metadata", "name"))
            for section, key in (("options", "install_requires"), ("tox", "requires")):
                if parser.has_option(section, key):
                    dependencies.update(
                        match.group(1) for line in parser.get(section, key).splitlines()
                        if (match := re.match(r"\s*([A-Za-z0-9_.-]+)", line))
                    )
        elif name == "setup.py":
            for match in re.finditer(r"\bname\s*=\s*['\"]([^'\"]+)['\"]", text):
                add(identifiers, match.group(1))
            dependencies.update(re.findall(r"['\"]([A-Za-z0-9_.-]+)(?:[<>=!~].*)?['\"]", text))
        elif path.suffix in {".sln", ".csproj"}:
            identifiers.update(re.findall(r"<AssemblyName>([^<]+)</AssemblyName>|Project\([^)]*\)\s*=\s*\"([^\"]+)\"", text))
            dependencies.update(re.findall(r"<(?:ProjectReference|PackageReference)[^>]+(?:Include|Update)=\"([^\"]+)\"", text))
    except (ET.ParseError, tomllib.TOMLDecodeError, json.JSONDecodeError, configparser.Error):
        return set(), set()
    identifiers = {item for value in identifiers for item in (value if isinstance(value, tuple) else (value,)) if item}
    return identifiers, dependencies


def _flatten_configuration(path: Path, text: str) -> dict[str, Any] | None:
    """把常见本地配置文件解析成可核对的点分键。"""

    def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
        """递归生成点分配置键。"""

        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, child in value.items():
                child_prefix = f"{prefix}.{key}" if prefix else str(key)
                result.update(flatten(child, child_prefix))
            return result
        return {prefix: value}

    try:
        suffix = path.suffix.casefold()
        if suffix in {".yaml", ".yml"}:
            return flatten(yaml.safe_load(text))
        if suffix == ".json":
            return flatten(json.loads(text))
        if suffix == ".toml":
            return flatten(tomllib.loads(text))
        if suffix in {".properties", ".env"}:
            values: dict[str, str] = {}
            for line in text.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith(("#", "!")) or "=" not in stripped:
                    continue
                key, value = stripped.split("=", 1)
                values[key.strip()] = value.strip()
            return values
        if suffix in {".ini", ".cfg"}:
            parser = configparser.ConfigParser(interpolation=None)
            parser.read_string(text)
            return {
                f"{section}.{key}": value
                for section in parser.sections() for key, value in parser.items(section)
            }
    except (OSError, UnicodeError, yaml.YAMLError, json.JSONDecodeError, tomllib.TOMLDecodeError, configparser.Error):
        return None
    return None


def _contains_usable_credential_text(value: str) -> bool:
    """识别自由文本中的可用凭据，同时允许精确占位符和类型化来源引用。"""

    masked = value
    safe_patterns = (
        rf"\b(?:authorization|auth)\s*[:=]\s*(?:Bearer|Basic)\s+{SAFE_CREDENTIAL_REFERENCE}",
        rf"\b(?:password|secret|token|authorization|auth|api[_-]?key|access[_-]?(?:key|token)|client[_-]?secret|private[_-]?key|ssh[_-]?key|passphrase|cookie|dsn)\s*[:=]\s*{SAFE_CREDENTIAL_REFERENCE}",
        rf"--(?:password|secret|token|authorization|auth|api-key|access-key|access-token|client-secret|private-key|ssh-key|passphrase|cookie|dsn)(?:=|\s+){SAFE_CREDENTIAL_REFERENCE}",
        rf"\b(?:Bearer|Basic)\s+{SAFE_CREDENTIAL_REFERENCE}",
    )
    for pattern in safe_patterns:
        masked = re.sub(pattern, "<credential-reference>", masked, flags=re.IGNORECASE)
    return bool(
        re.search(r"://[^/@:\s]+:[^/@\s]+@", masked)
        or re.search(r"[?&](?:password|secret|token|auth|access[_-]?(?:token|key)|api[_-]?key|client[_-]?secret|private[_-]?key|ssh[_-]?key|passphrase)=[^&\s]+", masked, re.IGNORECASE)
        or re.search(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", masked, re.IGNORECASE)
        or SENSITIVE_TEXT_RE.search(masked)
    )


def _sensitive_name(value: str) -> bool:
    """Recognize identifier names that conventionally carry credentials."""

    normalized = re.sub(r"[^a-z0-9]", "", value.casefold())
    return any(fragment in normalized for fragment in (
        "password", "secret", "token", "authorization", "apikey", "accesskey",
        "credential", "cookie", "connection", "dsn", "privatekey", "sshkey",
        "passphrase", "clientsecret", "auth",
    ))


def _safe_credential_literal(value: ast.AST | Any) -> bool:
    """Allow only exact placeholders or typed references in sensitive literals."""

    if isinstance(value, ast.Constant):
        value = value.value
    return isinstance(value, str) and bool(PLACEHOLDER_RE.fullmatch(value) or REFERENCE_RE.fullmatch(value))


def _secret_errors(path: Path, value: Any, prefix: str = "$") -> list[str]:
    """拒绝在发现契约中保存可用凭据。"""

    errors: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            child_path = f"{prefix}.{key_text}"
            normalized = re.sub(r"[^a-z0-9]", "", key_text.casefold())
            sensitive = any(fragment in normalized for fragment in (
                "password", "secret", "token", "authorization", "apikey", "accesskey",
                "credential", "cookie", "connection", "dsn", "privatekey", "sshkey", "passphrase", "clientsecret", "auth",
            ))
            if sensitive:
                if isinstance(child, str) and child.strip() and not PLACEHOLDER_RE.fullmatch(child):
                    typed_reference = "reference" in normalized and REFERENCE_RE.fullmatch(child)
                    if not typed_reference:
                        errors.append(_error(path, "secret-free", f"敏感字段必须只保存精确占位符或类型化引用: {child_path}", key_text))
                elif child is not None and not isinstance(child, str) and (
                    normalized in {"password", "secret", "token", "authorization", "auth", "apikey", "accesskey", "accesstoken", "credential", "cookie", "dsn", "privatekey", "sshkey", "passphrase", "clientsecret"}
                    or normalized.endswith(("password", "secret", "token", "apikey", "accesskey", "accesstoken", "credential", "cookie", "dsn", "privatekey", "sshkey", "passphrase", "clientsecret"))
                ):
                    errors.append(_error(path, "secret-free", f"敏感字段必须只保存精确占位符或类型化引用: {child_path}", key_text))
            errors.extend(_secret_errors(path, child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            errors.extend(_secret_errors(path, child, f"{prefix}[{index}]"))
    elif isinstance(value, str) and _contains_usable_credential_text(value):
        errors.append(_error(path, "secret-free", f"文本包含可用凭据: {prefix}"))
    return errors


def discovery_errors(project_root: Path) -> tuple[list[str], dict[str, Any]]:
    """校验工作区清单、拓扑、配置来源和只读探测契约。"""

    path = project_root / "discovery" / "workspace.yaml"
    errors: list[str] = []
    document = _load_yaml(path, errors)
    if not document:
        return errors, {}
    _exact_keys(path, document, DISCOVERY_KEYS, "discovery-schema", errors)
    if document.get("schema_version") != 1:
        errors.append(_error(path, "discovery-version", "schema_version 必须为 1", "schema_version"))

    inventory = document.get("inventory")
    if not _exact_keys(path, inventory, {"roots", "repositories", "existing_e2e"}, "inventory-schema", errors):
        return errors, document
    roots_raw = inventory.get("roots", [])
    if not _strings(roots_raw):
        errors.append(_error(path, "workspace-roots", "roots 必须是非空去重字符串列表", "roots"))
        roots_raw = []
    roots = [_resolve(project_root, item) for item in roots_raw]
    for root in roots:
        if not root.is_dir():
            errors.append(_error(path, "workspace-root-exists", f"工作区根目录不存在: {root}", "roots"))

    repositories = inventory.get("repositories", [])
    if not isinstance(repositories, list) or not repositories:
        errors.append(_error(path, "repository-required", "repositories 必须是非空列表", "repositories"))
        repositories = []
    repo_map: dict[str, dict[str, Any]] = {}
    repo_paths: set[Path] = set()
    repo_roots: dict[str, Path] = {}
    module_ids: set[str] = set()
    module_paths: set[Path] = set()
    module_roots: dict[str, Path] = {}
    application_nodes: set[str] = set()
    relevant_nodes: set[str] = set()
    confirmed_capabilities: dict[str, set[str]] = {}
    declared_builds: set[Path] = set()
    build_commits: dict[Path, tuple[str, str]] = {}
    for repo in repositories:
        if not isinstance(repo, dict) or set(repo) != {"id", "root", "commit", "build_files", "modules"}:
            errors.append(_error(path, "repository-schema", "仓库条目键集合无效", "repositories"))
            continue
        repo_id = repo.get("id")
        if not isinstance(repo_id, str) or not repo_id.strip() or repo_id in repo_map:
            errors.append(_error(path, "repository-id", f"仓库 ID 无效或重复: {repo_id}", "id:"))
            continue
        repo_root = _resolve(project_root, str(repo.get("root", "")))
        repo_map[repo_id] = repo
        repo_roots[repo_id] = repo_root
        repo_paths.add(repo_root)
        if roots and not any(_inside(repo_root, workspace_root) for workspace_root in roots):
            errors.append(_error(path, "repository-boundary", f"仓库根目录不在声明工作区内: {repo_root}", str(repo.get("root", ""))))
        if not (repo_root / ".git").exists():
            errors.append(_error(path, "repository-root", f"仓库根目录无 .git: {repo_root}", str(repo.get("root", ""))))
        commit = repo.get("commit")
        if not isinstance(commit, str) or not SHA_RE.fullmatch(commit):
            errors.append(_error(path, "repository-commit", f"仓库提交无效: {repo_id}", "commit"))
        elif repo_root.is_dir() and _git(repo_root, "cat-file", "-e", f"{commit}^{{commit}}").returncode != 0:
            errors.append(_error(path, "repository-commit", f"仓库中不存在提交: {repo_id}#{commit}", commit))
        build_files = repo.get("build_files")
        if not isinstance(build_files, list):
            errors.append(_error(path, "build-files", f"build_files 必须是列表: {repo_id}", "build_files"))
            build_files = []
        for relative in build_files:
            build_path = (repo_root / str(relative)).resolve()
            if not _inside(build_path, repo_root):
                errors.append(_error(path, "build-file-boundary", f"构建描述逃逸仓库边界: {build_path}", str(relative)))
                continue
            declared_builds.add(build_path)
            build_commits[build_path] = (repo_id, str(commit or ""))
            if not build_path.is_file():
                errors.append(_error(path, "build-file-exists", f"构建描述不存在: {build_path}", str(relative)))
        modules = repo.get("modules")
        if not isinstance(modules, list) or not modules:
            errors.append(_error(path, "module-required", f"仓库模块列表为空: {repo_id}", "modules"))
            continue
        for module in modules:
            if not isinstance(module, dict) or set(module) != {"id", "path", "kind"}:
                errors.append(_error(path, "module-schema", f"模块条目键集合无效: {repo_id}", "modules"))
                continue
            if module.get("kind") not in MODULE_KINDS:
                errors.append(_error(path, "module-kind", f"模块类型无效: {module.get('kind')}", "kind:"))
            node_id = f"{repo_id}:{module.get('id')}"
            if node_id in module_ids:
                errors.append(_error(path, "module-id", f"模块 ID 重复: {node_id}", str(module.get("id", ""))))
            module_ids.add(node_id)
            if module.get("kind") == "application":
                application_nodes.add(node_id)
            module_path = (repo_root / str(module.get("path", ""))).resolve()
            if not _inside(module_path, repo_root):
                errors.append(_error(path, "module-boundary", f"模块路径逃逸仓库边界: {module_path}", str(module.get("path", ""))))
                continue
            module_paths.add(module_path)
            module_roots[node_id] = module_path
            if not module_path.exists():
                errors.append(_error(path, "module-path", f"模块路径不存在: {module_path}", str(module.get("path", ""))))

    build_facts: dict[Path, tuple[set[str], set[str]]] = {}
    build_dependencies: dict[str, set[str]] = {}
    build_identifiers: dict[str, set[str]] = {node_id: set() for node_id in module_ids}
    for build_path, (repo_id, commit) in build_commits.items():
        content = _git_file(repo_roots[repo_id], commit, build_path) if commit else None
        identifiers, dependencies = _build_descriptor_facts(build_path, content or "")
        build_facts[build_path] = (identifiers, dependencies)
        owner = max(
            (node_id for node_id, module_root in module_roots.items() if _inside(build_path, module_root)),
            key=lambda node_id: len(module_roots[node_id].parts),
            default=None,
        )
        if not identifiers and not dependencies:
            errors.append(_error(path, "build-descriptor-semantic", f"构建描述无法解析出工程或依赖语义: {build_path}", build_path.name))
        if owner is not None:
            build_identifiers[owner].update(identifiers)
            build_dependencies.setdefault(owner, set()).update(dependencies)

    module_aliases: dict[str, set[str]] = {}
    for node_id, module_root in module_roots.items():
        module_aliases[node_id] = {
            node_id.split(":", 1)[1].casefold(),
            module_root.name.casefold(),
            *(item.casefold() for item in build_identifiers.get(node_id, set())),
        }
    required_build_edges: set[tuple[str, str]] = set()
    for caller, dependencies in build_dependencies.items():
        normalized = {
            alias.casefold().replace("\\", "/").rsplit("/", 1)[-1]
            for dependency in dependencies
            for alias in (dependency, dependency.split(":")[-1])
        }
        for target, aliases in module_aliases.items():
            if target != caller and normalized.intersection(aliases):
                required_build_edges.add((caller, target))

    existing_e2e = inventory.get("existing_e2e")
    if not _strings(existing_e2e, nonempty=False):
        errors.append(_error(path, "existing-e2e", "existing_e2e 必须是去重字符串列表", "existing_e2e"))
        existing_e2e = []
    declared_e2e = {_resolve(project_root, item) for item in existing_e2e}
    for e2e_root in declared_e2e:
        if not e2e_root.is_dir():
            errors.append(_error(path, "existing-e2e-exists", f"已有 E2E 工程不存在: {e2e_root}", "existing_e2e"))

    for root in roots:
        if not root.is_dir():
            continue
        missing_repos = _git_roots(root) - repo_paths
        errors.extend(_error(path, "inventory-git-complete", f"清单遗漏 Git 仓库: {item}", "repositories") for item in sorted(missing_repos))
        found_builds = {item.resolve() for item in _walk_files(root) if item.name in BUILD_NAMES or item.suffix in {".sln", ".csproj"}}
        source_builds = {
            item for item in found_builds
            if not any(_inside(item, e2e_root) for e2e_root in declared_e2e)
        }
        errors.extend(_error(path, "inventory-build-complete", f"清单遗漏构建工程: {item}", "build_files") for item in sorted(source_builds - declared_builds))
        missing_modules = {
            item.parent for item in source_builds
            if item in declared_builds and item.parent not in module_paths
        }
        errors.extend(_error(path, "inventory-module-complete", f"构建工程缺少对应模块条目: {item}", "modules") for item in sorted(missing_modules))
        found_e2e = _e2e_roots(root) - {project_root.resolve()}
        errors.extend(_error(path, "inventory-e2e-complete", f"清单遗漏已有 E2E 工程: {item}", "existing_e2e") for item in sorted(found_e2e - declared_e2e))

    topology = document.get("topology")
    if _exact_keys(path, topology, {"nodes", "edges", "searches"}, "topology-schema", errors):
        nodes = topology.get("nodes", [])
        node_map: set[str] = set()
        if not isinstance(nodes, list):
            errors.append(_error(path, "topology-nodes", "nodes 必须是列表", "nodes"))
            nodes = []
        for node in nodes:
            if not isinstance(node, dict) or set(node) != {"id", "relevant"} or not isinstance(node.get("relevant"), bool):
                errors.append(_error(path, "topology-node-schema", "拓扑节点无效", "nodes"))
                continue
            node_map.add(str(node.get("id")))
            if node.get("relevant") is True:
                relevant_nodes.add(str(node.get("id")))
        if node_map != module_ids:
            errors.append(_error(path, "topology-node-complete", f"拓扑节点与模块清单不一致: modules={sorted(module_ids)}, nodes={sorted(node_map)}", "nodes"))
        edges = topology.get("edges", [])
        if not isinstance(edges, list):
            errors.append(_error(path, "topology-edges", "edges 必须是列表", "edges"))
            edges = []
        valid_edges: list[dict[str, Any]] = []
        edge_keys: set[tuple[str, str, str]] = set()
        for edge in edges:
            if not isinstance(edge, dict) or set(edge) != {"from", "to", "mechanism", "evidence"}:
                errors.append(_error(path, "topology-edge-schema", "拓扑边无效", "edges"))
                continue
            if edge.get("from") not in node_map or edge.get("to") not in node_map:
                errors.append(_error(path, "topology-edge-node", f"拓扑边引用未知节点: {edge}", "edges"))
            if edge.get("mechanism") not in EDGE_MECHANISMS:
                errors.append(_error(path, "topology-edge-mechanism", f"拓扑机制无效: {edge.get('mechanism')}", "mechanism:"))
            key = (str(edge.get("from")), str(edge.get("to")), str(edge.get("mechanism")))
            if edge.get("from") == edge.get("to") or key in edge_keys:
                errors.append(_error(path, "topology-edge-identity", f"拓扑边不得自环或重复: {key}", "edges"))
            edge_keys.add(key)
            valid_edges.append(edge)
            if not _strings(edge.get("evidence")):
                errors.append(_error(path, "topology-edge-evidence", "拓扑边缺少源码证据", "evidence"))
            else:
                for reference in edge["evidence"]:
                    if "#" not in reference:
                        errors.append(_error(path, "topology-edge-evidence", f"拓扑边证据必须使用 repository#anchor: {reference}", str(reference)))
                        continue
                    repo_id, anchor = reference.split("#", 1)
                    repo = repo_map.get(repo_id)
                    source_repo = str(edge.get("from", "")).split(":", 1)[0]
                    if repo_id != source_repo:
                        errors.append(_error(path, "topology-edge-source", f"拓扑边证据必须来自调用方或生产方仓库: {reference}", str(reference)))
                    if not repo or not anchor or _git(repo_roots[repo_id], "grep", "-q", "--fixed-strings", anchor, str(repo.get("commit")), "--", ".").returncode != 0:
                        errors.append(_error(path, "topology-edge-evidence", f"拓扑边证据无法解析: {reference}", str(reference)))
                    elif edge.get("from") not in _anchor_owners(
                        repo_id,
                        repo_roots[repo_id],
                        str(repo.get("commit", "")),
                        anchor,
                        module_roots,
                    ):
                        errors.append(_error(path, "topology-edge-owner", f"拓扑边证据不属于 from 模块: {reference}", str(reference)))
                    if edge.get("mechanism") in {"build", "embedded"}:
                        semantic_dependencies = build_dependencies.get(str(edge.get("from")), set())
                        if anchor not in semantic_dependencies:
                            errors.append(_error(path, "topology-build-evidence", f"构建拓扑证据不是调用方构建描述中的真实依赖: {reference}", str(reference)))
                    elif repo and anchor and edge.get("mechanism") in {"http", "rpc", "message", "database", "cache", "job", "configuration"}:
                        if not _semantic_evidence(repo_roots[repo_id], str(repo.get("commit", "")), anchor, str(edge.get("mechanism"))):
                            errors.append(_error(path, "topology-edge-semantic", f"拓扑锚点邻近源码与机制不匹配: {reference}", str(reference)))

        declared_build_edges = {
            (str(edge.get("from")), str(edge.get("to")))
            for edge in valid_edges if edge.get("mechanism") in {"build", "embedded"}
        }
        for caller, target in sorted(required_build_edges - declared_build_edges):
            errors.append(_error(path, "topology-build-omission", f"构建描述中的本地模块依赖未进入拓扑: {caller} -> {target}", "edges:"))

        searches = topology.get("searches")
        if not isinstance(searches, dict) or set(searches) != SEARCH_CATEGORIES:
            errors.append(_error(path, "topology-search-schema", f"源码能力搜索必须完整包含: {sorted(SEARCH_CATEGORIES)}", "searches:"))
        else:
            for category, search in searches.items():
                if not isinstance(search, dict) or set(search) != {"queries", "evidence", "conclusion"}:
                    errors.append(_error(path, "topology-search-entry", f"源码搜索条目结构无效: {category}", f"{category}:"))
                    continue
                if not _strings(search.get("queries")) or not _strings(search.get("evidence"), nonempty=False):
                    errors.append(_error(path, "topology-search-content", f"源码搜索查询或证据无效: {category}", f"{category}:"))
                if not isinstance(search.get("conclusion"), str) or not search["conclusion"].strip():
                    errors.append(_error(path, "topology-search-conclusion", f"源码搜索缺少结论: {category}", "conclusion:"))
                evidence_valid = True
                evidence_owners: set[str] = set()
                evidence = search.get("evidence", [])
                for reference in evidence:
                    if "#" not in reference:
                        evidence_valid = False
                        errors.append(_error(path, "topology-search-evidence", f"搜索证据必须使用 repository#anchor: {reference}", str(reference)))
                        continue
                    repo_id, anchor = reference.split("#", 1)
                    repo = repo_map.get(repo_id)
                    if not repo or not anchor or _git(repo_roots[repo_id], "grep", "-q", "--fixed-strings", anchor, str(repo.get("commit")), "--", ".").returncode != 0:
                        evidence_valid = False
                        errors.append(_error(path, "topology-search-evidence", f"搜索证据无法解析: {reference}", str(reference)))
                    elif not _semantic_evidence(repo_roots[repo_id], str(repo.get("commit", "")), anchor, category):
                        evidence_valid = False
                        errors.append(_error(path, "topology-search-semantic", f"搜索锚点邻近源码与类别不匹配: {category}={reference}", str(reference)))
                    else:
                        owners = _anchor_owners(
                            repo_id,
                            repo_roots[repo_id],
                            str(repo.get("commit", "")),
                            anchor,
                            module_roots,
                        )
                        if not owners:
                            evidence_valid = False
                            errors.append(_error(path, "topology-search-owner", f"搜索锚点无法归属到具体模块: {reference}", str(reference)))
                        evidence_owners.update(owners)
                if evidence and evidence_valid:
                    confirmed_capabilities.setdefault(category, set()).update(evidence_owners)
            detected = _detected_integration_signals(repo_roots.values())
            for category in sorted(detected):
                search = searches.get(category, {})
                if isinstance(search, dict) and not search.get("evidence"):
                    errors.append(_error(path, "topology-search-omission", f"源码扫描发现 {category} 信号，但搜索证据为空", f"{category}:"))
            if len(relevant_nodes) > 1:
                linked_mechanisms = {
                    "http_rpc": {"http", "rpc"},
                    "messages": {"message"},
                    "database": {"database"},
                    "cache": {"cache"},
                    "jobs": {"job"},
                    "configuration": {"configuration"},
                }
                for category, mechanisms in linked_mechanisms.items():
                    for owner in sorted(confirmed_capabilities.get(category, set()) & relevant_nodes):
                        if not any(
                            edge.get("mechanism") in mechanisms
                            and edge.get("from") == owner
                            and edge.get("to") in relevant_nodes
                            for edge in valid_edges
                        ):
                            errors.append(_error(
                                path,
                                "topology-search-edge",
                                f"模块源码已确认 {category} 信号，但缺少该模块的对应出边: {owner}",
                                f"{category}:",
                            ))

    errors.extend(_configuration_errors(
        path,
        document.get("configuration"),
        module_ids,
        relevant_nodes,
        repo_roots,
        {repo_id: str(repo.get("commit", "")) for repo_id, repo in repo_map.items()},
        confirmed_capabilities,
        module_roots,
    ))
    errors.extend(_runtime_probe_errors(path, document.get("runtime_probe"), module_ids, application_nodes & relevant_nodes))
    gates = document.get("gates")
    expected_gates = {"inventory_complete", "topology_complete", "configuration_complete", "runtime_probe_complete"}
    if _exact_keys(path, gates, expected_gates, "discovery-gates", errors):
        for name in expected_gates:
            if gates.get(name) is not True:
                errors.append(_error(path, "discovery-gate-closed", f"发现门禁未通过: {name}", name))
    errors.extend(_secret_errors(path, document))
    return errors, document


def _configuration_errors(
    path: Path,
    configuration: Any,
    node_ids: set[str],
    relevant_nodes: set[str],
    repo_roots: Mapping[str, Path],
    repo_commits: Mapping[str, str] | None = None,
    required_capabilities: Mapping[str, set[str]] | set[str] | None = None,
    module_roots: Mapping[str, Path] | None = None,
) -> list[str]:
    """校验配置来源、优先级和最终来源引用。"""

    errors: list[str] = []
    expected = {"sources", "precedence", "services", "data_sources", "middleware", "controls"}
    if not _exact_keys(path, configuration, expected, "configuration-schema", errors):
        return errors
    sources = configuration.get("sources", [])
    source_ids: set[str] = set()
    source_map: dict[str, dict[str, Any]] = {}
    source_values: dict[str, dict[str, Any]] = {}
    if not isinstance(sources, list) or not sources:
        errors.append(_error(path, "configuration-sources", "sources 必须是非空列表", "sources"))
        sources = []
    for source in sources:
        required = {"id", "owner", "kind", "location", "profile", "overrides", "evidence"}
        if not isinstance(source, dict) or set(source) != required:
            errors.append(_error(path, "configuration-source-schema", "配置来源条目无效", "sources"))
            continue
        source_id = source.get("id")
        if not isinstance(source_id, str) or not source_id.strip() or source_id in source_ids:
            errors.append(_error(path, "configuration-source-id", f"配置来源 ID 无效或重复: {source_id}", "id"))
        source_ids.add(str(source_id))
        source_map[str(source_id)] = source
        if source.get("owner") not in node_ids:
            errors.append(_error(path, "configuration-source-owner", f"配置来源引用未知节点: {source.get('owner')}", "owner"))
        if source.get("kind") not in {"file", "profile", "environment", "config-center", "command-line", "local-override", "other"}:
            errors.append(_error(path, "configuration-source-kind", f"配置来源类型无效: {source.get('kind')}", "kind:"))
        if not isinstance(source.get("location"), str) or not source["location"].strip():
            errors.append(_error(path, "configuration-source-location", "配置来源位置不得为空", "location:"))
        if source.get("profile") is not None and (not isinstance(source.get("profile"), str) or not source["profile"].strip()):
            errors.append(_error(path, "configuration-source-profile", "profile 必须为 null 或非空字符串", "profile:"))
        if not _strings(source.get("overrides"), nonempty=False):
            errors.append(_error(path, "configuration-source-overrides", "overrides 必须是去重字符串列表", "overrides:"))
        if not _strings(source.get("evidence")):
            errors.append(_error(path, "configuration-source-evidence", "每个配置来源必须有源码证据锚点", "evidence:"))
        owner = str(source.get("owner", ""))
        repo_id = owner.split(":", 1)[0]
        source_content: str | None = None
        source_location: Path | None = None
        if source.get("kind") in {"file", "profile", "local-override"} and repo_id in repo_roots:
            source_location = (repo_roots[repo_id] / str(source.get("location", ""))).resolve()
            commit = (repo_commits or {}).get(repo_id)
            source_content = _git_file(repo_roots[repo_id], commit, source_location) if commit else None
            owner_root = (module_roots or {}).get(owner)
            if owner_root is not None and not _inside(source_location, owner_root):
                errors.append(_error(path, "configuration-source-owner-boundary", f"配置来源不属于声明 owner 模块: {source.get('location')}", "location:"))
            if not _inside(source_location, repo_roots[repo_id]) or not source_location.exists() or source_content is None:
                errors.append(_error(path, "configuration-source-resolve", f"配置来源不存在、逃逸 owner 仓库或不在声明提交中: {source.get('location')}", "location:"))
            else:
                parsed = _flatten_configuration(source_location, source_content)
                if parsed is None:
                    errors.append(_error(path, "configuration-source-format", f"文件配置来源必须使用可结构化解析的 YAML/JSON/TOML/properties/INI: {source.get('location')}", "location:"))
                else:
                    source_values[str(source_id)] = parsed
        elif source.get("kind") == "environment":
            location = str(source.get("location", ""))
            if re.fullmatch(r"(?:environment|env):[A-Z][A-Z0-9_]*", location) is None:
                errors.append(_error(path, "configuration-environment-location", "环境配置来源必须使用 environment:NAME 或 env:NAME", "location:"))
        for reference in source.get("evidence", []) if isinstance(source.get("evidence"), list) else []:
            if "#" not in reference:
                errors.append(_error(path, "configuration-source-evidence", f"配置来源证据必须使用 repository#anchor: {reference}", str(reference)))
                continue
            evidence_repo, anchor = reference.split("#", 1)
            commit = (repo_commits or {}).get(evidence_repo)
            if evidence_repo != repo_id or not anchor or evidence_repo not in repo_roots:
                errors.append(_error(path, "configuration-source-evidence", f"配置来源证据必须解析到 owner 仓库: {reference}", str(reference)))
            elif source_content is not None and anchor not in source_content:
                errors.append(_error(path, "configuration-source-location-evidence", f"配置来源证据不在声明的 location 文件中: {reference}", str(reference)))
            elif source_content is None and commit and (
                _git(repo_roots[evidence_repo], "grep", "-q", "--fixed-strings", anchor, commit, "--", ".").returncode != 0
                or not _semantic_evidence(repo_roots[evidence_repo], commit, anchor, "configuration")
            ):
                errors.append(_error(path, "configuration-source-evidence", f"非文件配置来源证据必须解析到真实配置读取语义: {reference}", str(reference)))
            elif source.get("kind") == "environment" and anchor != str(source.get("location", "")).split(":", 1)[-1]:
                errors.append(_error(path, "configuration-source-evidence", f"环境来源证据必须使用精确环境键: {reference}", str(reference)))
            elif commit and module_roots is not None and owner not in _anchor_owners(
                evidence_repo,
                repo_roots[evidence_repo],
                commit,
                anchor,
                module_roots,
            ):
                errors.append(_error(path, "configuration-source-owner", f"配置来源证据不属于声明 owner 模块: {reference}", str(reference)))
    source_owners = {str(source.get("owner")) for source in sources if isinstance(source, dict)}
    uncovered = relevant_nodes - source_owners
    if uncovered:
        errors.append(_error(path, "configuration-owner-complete", f"相关模块缺少配置来源盘点: {sorted(uncovered)}", "sources:"))
    source_keys: dict[str, set[str]] = {
        source_id: set(values) for source_id, values in source_values.items()
    }
    for source_id, source in source_map.items():
        if source.get("kind") == "environment":
            source_keys[source_id] = {str(source.get("location", "")).split(":", 1)[-1]}
    precedence = configuration.get("precedence")
    if not _strings(precedence) or set(precedence) != source_ids:
        errors.append(_error(path, "configuration-precedence", "precedence 必须无遗漏地按低到高列出 source ID", "precedence"))
    else:
        positions = {source_id: index for index, source_id in enumerate(precedence)}
        for index, source_id in enumerate(precedence):
            overrides = source_map.get(source_id, {}).get("overrides")
            owner = source_map.get(source_id, {}).get("owner")
            if not isinstance(overrides, list):
                continue
            for lower_id in overrides:
                if (
                    lower_id not in source_map
                    or source_map[lower_id].get("owner") != owner
                    or positions.get(lower_id, index) >= index
                ):
                    errors.append(_error(
                        path,
                        "configuration-precedence-binding",
                        f"配置来源 {source_id} 只能覆盖同一 owner 的已声明低优先级来源: {lower_id}",
                        "overrides:",
                    ))
            for lower_id in precedence[:index]:
                if source_map.get(lower_id, {}).get("owner") != owner:
                    continue
                overlap = source_keys.get(source_id, set()) & source_keys.get(lower_id, set())
                if overlap and lower_id not in overrides:
                    errors.append(_error(
                        path,
                        "configuration-precedence-binding",
                        f"配置来源 {source_id} 覆盖键 {sorted(overlap)}，必须声明低优先级来源 {lower_id}",
                        "overrides:",
                    ))

    discovered_ids: set[str] = set()

    def source_binding_errors(item: Mapping[str, Any], label: str, owner: str, *, reference: bool = False) -> None:
        """核对属性键和值确实来自声明的最终配置来源。"""

        source_id = str(item.get("effective_source", ""))
        source = source_map.get(source_id, {})
        source_key = item.get("source_key")
        if not isinstance(source_key, str) or not source_key.strip():
            errors.append(_error(path, "configuration-source-key", f"配置属性缺少非空 source_key: {label}", label))
            return
        kind = source.get("kind")
        if source.get("owner") != owner:
            errors.append(_error(path, "configuration-effective-owner", f"最终配置来源不属于组件 owner: {label}", label))
        if isinstance(precedence, list) and source_id in precedence:
            source_index = precedence.index(source_id)
            for higher_id in precedence[source_index + 1:]:
                higher = source_map.get(higher_id, {})
                if higher.get("owner") == owner and source_key in source_keys.get(higher_id, set()):
                    errors.append(_error(path, "configuration-effective-source", f"配置属性未使用覆盖链中的最终来源: {label} -> {higher_id}", label))
                    break
        if kind in {"file", "profile", "local-override"}:
            values = source_values.get(source_id)
            if values is None or source_key not in values:
                errors.append(_error(path, "configuration-source-key", f"source_key 未解析到最终来源文件: {label}={source_key}", label))
                return
            actual = values[source_key]
            if item.get("resolution") == "resolved":
                expected = item.get("reference") if reference else item.get("value")
                if isinstance(actual, str) and (match := SOURCE_PLACEHOLDER_RE.fullmatch(actual)):
                    body = actual[2:-1]
                    name, separator, default = body.partition(":")
                    actual = os.environ.get(name, default if separator else actual)
                if actual != expected and str(actual) != str(expected):
                    errors.append(_error(path, "configuration-effective-value", f"声明值与最终来源文件不一致: {label}", label))
            elif not (
                isinstance(actual, str)
                and (SOURCE_PLACEHOLDER_RE.fullmatch(actual) or REFERENCE_RE.fullmatch(actual))
            ):
                errors.append(_error(path, "configuration-unresolved-source", f"unresolved 属性的来源值必须是占位符或类型化引用: {label}", label))
        elif kind == "environment":
            location_key = str(source.get("location", "")).split(":", 1)[-1]
            if source_key != location_key:
                errors.append(_error(path, "configuration-environment-key", f"source_key 与环境来源不一致: {label}", label))
            if reference:
                valid = {f"environment:{source_key}", f"env:{source_key}", f"${{{source_key}}}"}
                if item.get("reference") not in valid:
                    errors.append(_error(path, "configuration-environment-reference", f"连接引用与环境来源不一致: {label}", label))
            elif item.get("resolution") == "resolved":
                actual = os.environ.get(source_key)
                if actual is None or str(item.get("value")) != actual:
                    errors.append(_error(path, "configuration-environment-value", f"resolved 环境值必须与当前进程一致: {label}", label))
        elif item.get("resolution") == "resolved":
            errors.append(_error(path, "configuration-effective-unproven", f"非文件/环境来源缺少可复核运行证据，不得声明 resolved: {label}", label))

    def value_errors(item: Any, label: str, owner: str) -> None:
        """校验非敏感运行值及其最终来源。"""

        if not isinstance(item, dict) or set(item) != {"value", "effective_source", "resolution", "source_key"}:
            errors.append(_error(path, "configuration-value-schema", f"配置属性结构无效: {label}", label))
        elif item.get("effective_source") not in source_ids or item.get("resolution") not in {"resolved", "unresolved"}:
            errors.append(_error(path, "configuration-value-source", f"配置属性来源或状态无效: {label}", label))
        elif item.get("resolution") == "unresolved" and item.get("value") is not None:
            errors.append(_error(path, "configuration-unresolved-value", f"未解析属性必须使用 null: {label}", label))
        elif item.get("resolution") == "resolved" and (
            item.get("value") is None or isinstance(item.get("value"), str) and not item["value"].strip()
        ):
            errors.append(_error(path, "configuration-resolved-value", f"已解析属性必须记录非空运行值: {label}", label))
        else:
            source_binding_errors(item, label, owner)

    def reference_errors(item: Any, label: str, owner: str) -> None:
        """校验敏感连接只保存来源引用和最终覆盖来源。"""

        if not isinstance(item, dict) or set(item) != {"reference", "effective_source", "resolution", "source_key"}:
            errors.append(_error(path, "configuration-reference-schema", f"连接来源结构无效: {label}", label))
        elif item.get("effective_source") not in source_ids or item.get("resolution") not in {"resolved", "unresolved"}:
            errors.append(_error(path, "configuration-reference-source", f"连接来源或状态无效: {label}", label))
        elif not isinstance(item.get("reference"), str) or not (
            PLACEHOLDER_RE.fullmatch(item["reference"]) or REFERENCE_RE.fullmatch(item["reference"])
        ):
            errors.append(_error(path, "configuration-reference", f"连接来源必须保存非敏感引用: {label}", label))
        else:
            source_binding_errors(item, label, owner, reference=True)

    for service in configuration.get("services", []) if isinstance(configuration.get("services"), list) else []:
        required = {"id", "owner", "port", "context_path", "health", "openapi"}
        if not isinstance(service, dict) or set(service) != required or service.get("owner") not in node_ids:
            errors.append(_error(path, "configuration-service-schema", "服务配置发现条目无效", "services"))
            continue
        service_id = service.get("id")
        if not isinstance(service_id, str) or not service_id.strip() or service_id in discovered_ids:
            errors.append(_error(path, "configuration-component-id", f"发现组件 ID 无效或重复: {service_id}", "id"))
        discovered_ids.add(str(service_id))
        for field in ("port", "context_path", "health", "openapi"):
            value_errors(service.get(field), field, str(service.get("owner")))

    for collection in ("data_sources", "middleware", "controls"):
        value = configuration.get(collection)
        if not isinstance(value, list):
            errors.append(_error(path, "configuration-list", f"{collection} 必须是列表", collection))
            continue
        expected_keys = {
            "data_sources": {"id", "owner", "type", "name", "connection_source"},
            "middleware": {"id", "owner", "capability", "type", "logical_name", "connection_source"},
            "controls": {"id", "owner", "capability", "type", "source"},
        }[collection]
        for item in value:
            if not isinstance(item, dict) or set(item) != expected_keys or item.get("owner") not in node_ids:
                errors.append(_error(path, "configuration-owner", f"{collection} 引用未知节点", collection))
                continue
            component_id = item.get("id")
            if not isinstance(component_id, str) or not component_id.strip() or component_id in discovered_ids:
                errors.append(_error(path, "configuration-component-id", f"发现组件 ID 无效或重复: {component_id}", "id"))
            discovered_ids.add(str(component_id))
            if collection == "data_sources":
                if not isinstance(item.get("type"), str) or not item["type"].strip():
                    errors.append(_error(path, "configuration-component-type", "数据源类型不得为空", "type:"))
                value_errors(item.get("name"), "name", str(item.get("owner")))
                reference_errors(item.get("connection_source"), "connection_source", str(item.get("owner")))
            elif collection == "middleware":
                if item.get("capability") not in {"messages", "cache"}:
                    errors.append(_error(path, "configuration-component-capability", "中间件 capability 必须是 messages 或 cache", "capability:"))
                if not isinstance(item.get("type"), str) or not item["type"].strip():
                    errors.append(_error(path, "configuration-component-type", "中间件类型不得为空", "type:"))
                value_errors(item.get("logical_name"), "logical_name", str(item.get("owner")))
                reference_errors(item.get("connection_source"), "connection_source", str(item.get("owner")))
            else:
                if item.get("capability") not in {"jobs", "test_or_admin_api", "mocks_and_faults", "dynamic_configuration", "scheduled_jobs"}:
                    errors.append(_error(path, "configuration-component-capability", "测试控制 capability 无效", "capability:"))
                if not isinstance(item.get("source"), str) or not item["source"].strip():
                    errors.append(_error(path, "configuration-control-source", "测试控制必须有源码或配置来源", "source:"))
                elif "#" not in item["source"]:
                    errors.append(_error(path, "configuration-control-source", "测试控制 source 必须使用 repository#anchor", "source:"))
                else:
                    source_repo, anchor = item["source"].split("#", 1)
                    owner = str(item.get("owner", ""))
                    commit = (repo_commits or {}).get(source_repo, "")
                    categories = {"jobs"} if item.get("capability") in {"jobs", "scheduled_jobs"} else {"configuration", "http_rpc"}
                    if (
                        source_repo != owner.split(":", 1)[0]
                        or source_repo not in repo_roots
                        or not anchor
                        or not any(_semantic_evidence(repo_roots[source_repo], commit, anchor, category) for category in categories)
                        or module_roots is not None and owner not in _anchor_owners(
                            source_repo, repo_roots[source_repo], commit, anchor, module_roots
                        )
                    ):
                        errors.append(_error(path, "configuration-control-source", "测试控制 source 必须解析到 owner 模块中的匹配控制语义", "source:"))
    capability_collections = {
        "http_rpc": "services",
        "database": "data_sources",
        "messages": "middleware",
        "cache": "middleware",
        "jobs": "controls",
    }
    if isinstance(required_capabilities, Mapping):
        required_by_owner = {str(capability): set(owners) for capability, owners in required_capabilities.items()}
    else:
        required_by_owner = {
            str(capability): set(relevant_nodes or node_ids)
            for capability in required_capabilities or set()
        }
    for capability, owners in sorted(required_by_owner.items()):
        collection = capability_collections.get(capability)
        for owner in sorted(owners):
            entries = configuration.get(collection, []) if collection else []
            matched = any(
                isinstance(item, dict)
                and item.get("owner") == owner
                and (collection not in {"middleware", "controls"} or item.get("capability") == capability)
                for item in entries if isinstance(entries, list)
            )
            if collection and not matched:
                errors.append(_error(
                    path,
                    "configuration-capability-omission",
                    f"源码搜索已确认 {owner} 的 {capability} 能力，但配置清单遗漏匹配条目",
                    f"{collection}:",
                ))
    return errors


def _runtime_probe_errors(
    path: Path,
    probe: Any,
    node_ids: set[str],
    runnable_nodes: set[str] | None = None,
) -> list[str]:
    """校验只读运行探测状态和证据。"""

    errors: list[str] = []
    expected = {"requested", "outcome", "blockers", "listeners", "processes", "associations", "read_only_smoke"}
    if not _exact_keys(path, probe, expected, "runtime-probe-schema", errors):
        return errors
    requested = probe.get("requested")
    outcome = probe.get("outcome")
    blockers = probe.get("blockers")
    if not isinstance(requested, bool):
        errors.append(_error(path, "runtime-probe-requested", "requested 必须是布尔值", "requested"))
    if requested is True and outcome not in {"completed", "blocked"}:
        errors.append(_error(path, "runtime-probe-outcome", "已请求探测时 outcome 必须为 completed 或 blocked", "outcome"))
    if requested is False and outcome != "not_requested":
        errors.append(_error(path, "runtime-probe-outcome", "未请求探测时 outcome 必须为 not_requested", "outcome"))
    if outcome == "blocked" and not _strings(blockers):
        errors.append(_error(path, "runtime-probe-blocker", "blocked 探测必须记录原因", "blockers"))
    if outcome != "blocked" and blockers != []:
        errors.append(_error(path, "runtime-probe-blocker", "非 blocked 探测不得记录 blockers", "blockers"))
    for name in ("listeners", "processes", "associations", "read_only_smoke"):
        if not isinstance(probe.get(name), list):
            errors.append(_error(path, "runtime-probe-list", f"{name} 必须是列表", name))
    if outcome == "completed":
        for name in ("listeners", "processes", "associations", "read_only_smoke"):
            if not probe.get(name):
                errors.append(_error(path, "runtime-probe-complete", f"已完成探测必须包含非空 {name}", name))
    processes = probe.get("processes", []) if isinstance(probe.get("processes"), list) else []
    process_ids: set[str] = set()
    for item in processes:
        if (
            not isinstance(item, dict)
            or set(item) != {"id", "pid", "command_reference", "evidence"}
            or not isinstance(item.get("pid"), int)
            or item["pid"] < 1
            or not isinstance(item.get("command_reference"), str)
            or not item["command_reference"].strip()
            or not _strings(item.get("evidence"))
        ):
            errors.append(_error(path, "runtime-process-schema", "进程探测条目结构无效", "processes"))
            continue
        process_id = str(item.get("id", ""))
        if not process_id or process_id in process_ids:
            errors.append(_error(path, "runtime-process-id", f"进程 ID 无效或重复: {process_id}", "id:"))
        process_ids.add(process_id)
    listeners = probe.get("listeners", []) if isinstance(probe.get("listeners"), list) else []
    listener_ids: set[str] = set()
    for item in listeners:
        if (
            not isinstance(item, dict)
            or set(item) != {"id", "host", "port", "protocol", "evidence"}
            or not isinstance(item.get("host"), str)
            or not item["host"].strip()
            or not isinstance(item.get("port"), int)
            or not 1 <= item["port"] <= 65535
            or not isinstance(item.get("protocol"), str)
            or not item["protocol"].strip()
            or not _strings(item.get("evidence"))
        ):
            errors.append(_error(path, "runtime-listener-schema", "监听探测条目结构无效", "listeners"))
            continue
        listener_id = str(item.get("id", ""))
        if not listener_id or listener_id in listener_ids:
            errors.append(_error(path, "runtime-listener-id", f"监听 ID 无效或重复: {listener_id}", "id:"))
        listener_ids.add(listener_id)
    associated_processes: set[str] = set()
    associated_listeners: set[str] = set()
    association_keys: set[tuple[str, str, str]] = set()
    for item in probe.get("associations", []) if isinstance(probe.get("associations"), list) else []:
        if not isinstance(item, dict) or set(item) != {"process", "listener", "node"}:
            errors.append(_error(path, "runtime-association-schema", "运行关联条目结构无效", "associations"))
        elif item.get("process") not in process_ids or item.get("listener") not in listener_ids or item.get("node") not in node_ids:
            errors.append(_error(path, "runtime-association-reference", "运行关联引用未知进程、监听或拓扑节点", "associations"))
        else:
            key = (str(item["process"]), str(item["listener"]), str(item["node"]))
            if key in association_keys:
                errors.append(_error(path, "runtime-association-duplicate", f"运行关联重复: {key}", "associations"))
            association_keys.add(key)
            associated_processes.add(key[0])
            associated_listeners.add(key[1])
    if outcome == "completed" and (process_ids != associated_processes or listener_ids != associated_listeners):
        errors.append(_error(path, "runtime-association-complete", "每个运行进程和监听都必须关联到拓扑节点", "associations"))
    associated_nodes = {
        str(item.get("node")) for item in probe.get("associations", [])
        if isinstance(item, dict) and item.get("node") in node_ids
    }
    required_runtime_nodes = runnable_nodes or set()
    if outcome == "completed" and not required_runtime_nodes.issubset(associated_nodes):
        errors.append(_error(
            path,
            "runtime-node-coverage",
            f"运行探测未覆盖全部相关应用节点: {sorted(required_runtime_nodes - associated_nodes)}",
            "associations",
        ))
    smoke_nodes: set[str] = set()
    for item in probe.get("read_only_smoke", []) if isinstance(probe.get("read_only_smoke"), list) else []:
        if not isinstance(item, dict) or set(item) != {"node", "method", "target_ref", "result"}:
            errors.append(_error(path, "runtime-smoke-schema", "只读冒烟证据结构无效", "read_only_smoke"))
        elif item.get("node") not in node_ids:
            errors.append(_error(path, "runtime-smoke-node", f"只读冒烟引用未知拓扑节点: {item.get('node')}", "node"))
        elif str(item.get("method", "")).upper() not in {"GET", "HEAD", "READ"}:
            errors.append(_error(path, "runtime-smoke-method", "只读冒烟只允许 GET、HEAD 或只读 RPC", "method"))
        elif not isinstance(item.get("target_ref"), str) or not item["target_ref"].strip() or not isinstance(item.get("result"), str) or not item["result"].strip():
            errors.append(_error(path, "runtime-smoke-content", "只读冒烟目标引用和结果不得为空", "read_only_smoke"))
        else:
            smoke_nodes.add(str(item["node"]))
    if outcome == "completed" and not required_runtime_nodes.issubset(smoke_nodes):
        errors.append(_error(
            path,
            "runtime-smoke-coverage",
            f"只读冒烟未覆盖全部相关应用节点: {sorted(required_runtime_nodes - smoke_nodes)}",
            "read_only_smoke",
        ))
    return errors


def _observed_process_command(pid: int) -> str | None:
    """只读获取指定 PID 的实际命令行。"""

    try:
        if os.name == "nt":
            result = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                    f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}').CommandLine",
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None
        proc_path = Path("/proc") / str(pid) / "cmdline"
        if proc_path.is_file():
            return proc_path.read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace").strip() or None
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None
    except OSError:
        return None


def _same_local_host(listener_host: str, target_host: str) -> bool:
    """判断声明监听与冒烟目标是否指向同一本地主机。"""

    if not _is_local_host(listener_host) or not _is_local_host(target_host):
        return False
    if listener_host.casefold() == target_host.casefold():
        return True
    if listener_host not in {"0.0.0.0", "::"}:
        return False
    if target_host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(target_host).is_loopback
    except ValueError:
        return False


def _is_local_host(host: str) -> bool:
    """只接受回环、通配监听、本机名或本机接口地址。"""

    normalized = host.strip().strip("[]").casefold()
    if not normalized:
        return False
    if normalized in {"localhost", "0.0.0.0", "::", socket.gethostname().casefold(), socket.getfqdn().casefold()}:
        return True
    try:
        address = ipaddress.ip_address(normalized)
        if address.is_loopback or address.is_unspecified:
            return True
    except ValueError:
        pass
    local_addresses: set[str] = set()
    for name in {socket.gethostname(), socket.getfqdn(), "localhost"}:
        try:
            local_addresses.update(item[4][0].split("%", 1)[0].casefold() for item in socket.getaddrinfo(name, None))
        except OSError:
            continue
    try:
        resolved = {item[4][0].split("%", 1)[0].casefold() for item in socket.getaddrinfo(host, None)}
    except OSError:
        return False
    return bool(resolved) and resolved.issubset(local_addresses)


def _listener_owner_pids(port: int) -> set[int]:
    """只读查询本机 TCP LISTEN 端口的拥有进程。"""

    try:
        if os.name == "nt":
            result = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                    f"Get-NetTCPConnection -State Listen -LocalPort {port} -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess",
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            return {
                int(line.strip()) for line in result.stdout.splitlines()
                if re.fullmatch(r"[1-9][0-9]*", line.strip())
            }
        else:
            result = subprocess.run(
                ["ss", "-ltnp", f"sport = :{port}"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            return {int(item) for item in re.findall(r"\bpid=([1-9][0-9]*)\b", result.stdout)}
    except OSError:
        return set()


def _runtime_probe_live_errors(path: Path, probe: Any) -> list[str]:
    """在有序运行探测阶段重新核验进程、监听和本地 HTTP 结果。"""

    if not isinstance(probe, dict) or probe.get("requested") is not True or probe.get("outcome") != "completed":
        return []
    errors: list[str] = []
    for process in probe.get("processes", []):
        if not isinstance(process, dict) or not isinstance(process.get("pid"), int):
            continue
        observed = _observed_process_command(process["pid"])
        reference = str(process.get("command_reference", ""))
        if observed is None or reference not in observed:
            errors.append(_error(
                path,
                "runtime-process-live",
                f"PID {process['pid']} 不存在或实际命令行不包含声明引用",
                str(process.get("id", "processes")),
            ))

    listeners = {
        str(item.get("id")): item for item in probe.get("listeners", []) if isinstance(item, dict)
    }
    process_pids = {
        str(item.get("id")): int(item["pid"])
        for item in probe.get("processes", [])
        if isinstance(item, dict) and isinstance(item.get("pid"), int)
    }
    for listener in listeners.values():
        host, port = listener.get("host"), listener.get("port")
        if not isinstance(host, str) or not isinstance(port, int):
            continue
        if not _is_local_host(host):
            errors.append(_error(
                path,
                "runtime-listener-nonlocal",
                f"拒绝连接非本地主机监听: {listener.get('id')}",
                str(listener.get("id", "listeners")),
            ))
            continue
        try:
            connection = socket.create_connection((host, port), timeout=2.0)
            connection.close()
        except OSError as exc:
            errors.append(_error(
                path,
                "runtime-listener-live",
                f"监听无法只读连接: {listener.get('id')} ({type(exc).__name__})",
                str(listener.get("id", "listeners")),
            ))

    for association in probe.get("associations", []):
        if not isinstance(association, dict):
            continue
        listener = listeners.get(str(association.get("listener")))
        expected_pid = process_pids.get(str(association.get("process")))
        if listener is None or expected_pid is None or not isinstance(listener.get("port"), int):
            continue
        owners = _listener_owner_pids(listener["port"])
        if expected_pid not in owners:
            errors.append(_error(
                path,
                "runtime-association-live",
                f"监听 {association.get('listener')} 未由声明进程 {expected_pid} 持有",
                str(association.get("listener", "associations")),
            ))

    node_listeners: dict[str, list[dict[str, Any]]] = {}
    for association in probe.get("associations", []):
        if not isinstance(association, dict):
            continue
        listener = listeners.get(str(association.get("listener")))
        if listener is not None:
            node_listeners.setdefault(str(association.get("node")), []).append(listener)
    for smoke in probe.get("read_only_smoke", []):
        if not isinstance(smoke, dict) or str(smoke.get("method", "")).upper() not in {"GET", "HEAD"}:
            continue
        target = str(smoke.get("target_ref", ""))
        try:
            parsed = urllib.parse.urlsplit(target)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError:
            parsed = urllib.parse.SplitResult("", "", "", "", "")
            port = 0
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            errors.append(_error(path, "runtime-smoke-live-target", "GET/HEAD 冒烟 target_ref 必须是无凭据 URL", target))
            continue
        scheme, host = parsed.scheme, parsed.hostname
        candidates = node_listeners.get(str(smoke.get("node")), [])
        if not any(item.get("port") == port and _same_local_host(str(item.get("host")), host) for item in candidates):
            errors.append(_error(path, "runtime-smoke-live-target", "冒烟 URL 未绑定到该节点的已核验本地监听", target))
            continue
        request_target = parsed.path or "/"
        if parsed.query:
            request_target += f"?{parsed.query}"
        connection_type = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        try:
            connection = connection_type(host, port, timeout=3.0)
            connection.request(str(smoke["method"]).upper(), request_target)
            response = connection.getresponse()
            response.read(1024)
            connection.close()
        except (OSError, http.client.HTTPException) as exc:
            errors.append(_error(path, "runtime-smoke-live", f"只读冒烟调用失败: {type(exc).__name__}", target))
            continue
        expected = re.fullmatch(r"status:([1-5][0-9]{2})", str(smoke.get("result", "")))
        if response.status >= 500:
            errors.append(_error(path, "runtime-smoke-live-status", f"只读冒烟返回服务端失败状态: {response.status}", target))
        elif expected is None or int(expected.group(1)) != response.status:
            errors.append(_error(
                path,
                "runtime-smoke-live-result",
                f"只读冒烟实际状态与记录不符: actual={response.status}",
                str(smoke.get("result", "read_only_smoke")),
            ))
    return errors


def _scenario_directories(project_root: Path, selected: str | None, errors: list[str]) -> list[Path]:
    """发现直接场景目录并安全解析可选过滤器。"""

    root = project_root / "scenarios"
    if not root.is_dir():
        errors.append(_error(root, "scenarios-required", "scenarios 目录不存在"))
        return []
    directories = sorted(path for path in root.iterdir() if path.is_dir() and path.name not in SKIP_DIRS)
    if not directories:
        errors.append(_error(root, "scenario-required", "至少需要一个场景目录"))
        return []
    if selected is None:
        return directories
    if Path(selected).name != selected or selected in {".", ".."}:
        errors.append(_error(root, "scenario-selector", f"场景名不能是路径: {selected}"))
        return []
    matches = [path for path in directories if path.name == selected]
    if len(matches) != 1:
        errors.append(_error(root, "scenario-selector", f"场景不存在或不唯一: {selected}"))
    return matches


def _derived_status(readiness: Mapping[str, Any], decision: Mapping[str, Any]) -> str | None:
    """根据契约、控制、配置和数据状态推导唯一场景状态。"""

    source_contract = readiness.get("source_contract")
    safe_control = readiness.get("safe_control")
    runtime = readiness.get("runtime_configuration")
    test_data = readiness.get("test_data")
    blockers = readiness.get("blockers")
    if (source_contract == "blocked" or safe_control == "blocked") and decision.get("safe_control_path") is False:
        return "contract_blocked" if isinstance(blockers, list) and blockers and decision.get("blockers") else None
    if source_contract != "confirmed" or safe_control != "confirmed" or decision.get("safe_control_path") is not True:
        return None
    if runtime == "confirmed" and test_data == "confirmed" and blockers == []:
        return "ready"
    if runtime in {"confirmed", "missing"} and test_data in {"confirmed", "missing"} and "missing" in {runtime, test_data}:
        valid = isinstance(blockers, list) and blockers and all(
            isinstance(blocker, str) and PENDING_BLOCKER_RE.fullmatch(blocker) for blocker in blockers
        )
        return "pending_environment" if valid else None
    return None


def _control_errors(
    path: Path,
    controls: Any,
    steps: list[Any],
    source_symbols: set[str],
) -> list[str]:
    """校验完整控制矩阵及其步骤映射。"""

    errors: list[str] = []
    expected = set(CONTROL_NAMES) | {"decision"}
    if not _exact_keys(path, controls, expected, "control-matrix-schema", errors):
        return errors
    for name in CONTROL_NAMES:
        item = controls.get(name)
        required = {"status", "assessment", "evidence", "planned_use"}
        if name == "observability":
            required |= {"correlation_keys", "business_evidence", "recovery"}
        if name == "database_control":
            required |= {"safety"}
        if not isinstance(item, dict) or set(item) != required:
            errors.append(_error(path, "control-entry-schema", f"控制类别结构无效: {name}", f"{name}:"))
            continue
        status = item.get("status")
        if status not in CONTROL_STATUSES:
            errors.append(_error(path, "control-status", f"控制状态无效: {name}={status}", "status:"))
        if not isinstance(item.get("assessment"), str) or not item["assessment"].strip():
            errors.append(_error(path, "control-assessment", f"控制类别缺少评估: {name}", "assessment:"))
        evidence = item.get("evidence")
        planned = item.get("planned_use")
        if not _strings(evidence, nonempty=status != "not_applicable"):
            errors.append(_error(path, "control-evidence", f"控制证据无效: {name}", "evidence:"))
        elif any(reference not in source_symbols for reference in evidence):
            errors.append(_error(path, "control-evidence-source", f"控制证据未解析到场景源码锚点: {name}", "evidence:"))
        if not _strings(planned, nonempty=False):
            errors.append(_error(path, "control-plan", f"控制用途必须是去重字符串列表: {name}", "planned_use:"))
            planned = []
        if status != "usable" and planned:
            errors.append(_error(path, "control-plan-status", f"非 usable 控制不得计划使用: {name}", "planned_use:"))
        controlled_steps = [
            step for step in steps
            if isinstance(step, dict) and (step.get("control") == name or name == "observability")
        ]
        contract_symbols = {
            str(step.get("action")) for step in controlled_steps if isinstance(step.get("action"), str)
        }
        contract_symbols.update(
            str(expectation)
            for step in controlled_steps
            for expectation in step.get("expect", [])
        )
        unmapped = set(planned) - contract_symbols
        if unmapped:
            errors.append(_error(path, "control-plan-mapping", f"控制用途未映射到步骤: {name}={sorted(unmapped)}", "planned_use:"))
        step_actions = {
            str(step.get("action")) for step in steps
            if isinstance(step, dict) and step.get("control") == name
        }
        missing_plans = step_actions - set(planned)
        if missing_plans:
            errors.append(_error(path, "control-step-plan", f"每个步骤动作必须进入对应控制 planned_use: {name}={sorted(missing_plans)}", "planned_use:"))
        if name == "observability" and status == "usable":
            for field in ("correlation_keys", "business_evidence", "recovery"):
                if not _strings(item.get(field)):
                    errors.append(_error(path, "observability-proof", f"可执行场景缺少 {field}", field))
        if name == "database_control":
            safety = item.get("safety")
            safety_keys = {
                "authorization_required",
                "target_environment",
                "purpose",
                "consumer_source",
                "exact_selector",
                "expected_rows",
                "snapshot",
                "mutation",
                "trigger",
                "verification",
                "restoration",
                "restoration_verification",
            }
            if status == "usable":
                if not isinstance(safety, dict) or set(safety) != safety_keys:
                    errors.append(_error(path, "database-control-safety", "database_control.safety 键集合无效", "safety:"))
                elif (
                    safety.get("authorization_required") is not True
                    or not isinstance(safety.get("expected_rows"), int)
                    or isinstance(safety.get("expected_rows"), bool)
                    or safety["expected_rows"] != 1
                ):
                    errors.append(_error(path, "database-control-safety", "数据库控制必须要求授权且 expected_rows 必须精确为 1", "authorization_required"))
                elif not all(isinstance(safety.get(field), str) and safety[field].strip() for field in safety_keys - {"authorization_required", "expected_rows"}):
                    errors.append(_error(path, "database-control-safety", "数据库控制 safety 字符串字段不得为空", "safety:"))
                elif safety.get("purpose") not in {"preparation", "time_advance", "expiry_simulation", "state_trigger"}:
                    errors.append(_error(path, "database-control-purpose", "控制 SQL purpose 只能用于准备、时间推进、过期模拟或状态触发", "purpose:"))
            elif safety is not None:
                errors.append(_error(path, "database-control-safety", "未使用数据库控制时 safety 必须为 null", "safety:"))
    decision = controls.get("decision")
    if not isinstance(decision, dict) or set(decision) != {"safe_control_path", "blockers"} or not isinstance(decision.get("safe_control_path"), bool) or not _strings(decision.get("blockers"), nonempty=False):
        errors.append(_error(path, "control-decision", "控制决策结构无效", "decision:"))
    return errors


def _isolation_errors(path: Path, isolation: Any, steps: list[Any]) -> list[str]:
    """校验场景拥有资源、关联键和串行锁声明。"""

    errors: list[str] = []
    expected = {"namespace", "correlation_keys", "owned_resources", "mutable_controls", "serial_lock"}
    if not _exact_keys(path, isolation, expected, "isolation-schema", errors):
        return errors
    if not isinstance(isolation.get("namespace"), str) or not isolation["namespace"].strip():
        errors.append(_error(path, "isolation-namespace", "namespace 不得为空", "namespace:"))
    for field in ("correlation_keys", "mutable_controls"):
        if not _strings(isolation.get(field), nonempty=field == "correlation_keys"):
            errors.append(_error(path, "isolation-list", f"{field} 必须是去重字符串列表", field))
    resources = isolation.get("owned_resources")
    identities: set[str] = set()
    if not isinstance(resources, list):
        errors.append(_error(path, "isolation-resources", "owned_resources 必须是列表", "owned_resources"))
    else:
        for item in resources:
            if not isinstance(item, dict) or set(item) != {"kind", "identity", "cleanup", "restore", "verify"}:
                errors.append(_error(path, "isolation-resource-schema", "拥有资源条目无效", "owned_resources"))
                continue
            if not all(isinstance(item.get(field), str) and item[field].strip() for field in item):
                errors.append(_error(path, "isolation-resource-value", "拥有资源字段不得为空", "owned_resources"))
            if item.get("identity") in identities:
                errors.append(_error(path, "isolation-resource-duplicate", f"拥有资源重复: {item.get('identity')}", "identity:"))
            identities.add(str(item.get("identity")))
    mutable_controls = isolation.get("mutable_controls", [])
    if isinstance(mutable_controls, list):
        undeclared = set(str(item) for item in mutable_controls) - identities
        if undeclared:
            errors.append(_error(
                path,
                "isolation-mutable-resource",
                f"每个可变控制都必须作为 owned_resources 精确声明并恢复: {sorted(undeclared)}",
                "mutable_controls:",
            ))
    serial_lock = isolation.get("serial_lock")
    if serial_lock is not None:
        errors.append(_error(path, "isolation-serial-lock", "通用门禁不接受未验证的共享串行锁；资源必须真正隔离", "serial_lock:"))
    if any(isinstance(step, dict) and step.get("side_effect") == "write" for step in steps) and not resources:
        errors.append(_error(path, "isolation-write-resource", "写场景必须声明至少一个精确拥有资源及其清理、恢复和验证动作", "owned_resources:"))
    return errors


def _scenario_errors(
    project_root: Path,
    directory: Path,
    definition: dict[str, Any],
    discovery: dict[str, Any],
) -> list[str]:
    """校验一个场景的完整契约、数据、来源和恢复。"""

    path = directory / "场景定义.yaml"
    errors: list[str] = []
    _exact_keys(path, definition, SCENARIO_KEYS, "scenario-schema", errors)

    meta = definition.get("meta")
    if _exact_keys(path, meta, {"id", "name", "status", "actor"}, "scenario-meta", errors):
        if not isinstance(meta.get("id"), str) or not ID_RE.fullmatch(meta["id"]):
            errors.append(_error(path, "scenario-id", "场景 ID 格式无效", "id:"))
        if meta.get("status") not in {"ready", "pending_environment", "contract_blocked"}:
            errors.append(_error(path, "scenario-status", "场景状态无效", "status:"))
        for field in ("name", "actor"):
            if not isinstance(meta.get(field), str) or not meta[field].strip():
                errors.append(_error(path, "scenario-meta-value", f"{field} 不得为空", f"{field}:"))

    generation = definition.get("generation")
    if _exact_keys(path, generation, {"mode", "owner", "write_scope", "degradation_reason"}, "generation-schema", errors):
        mode = generation.get("mode")
        if mode not in {"delegated", "main_agent", "sequential_degraded"}:
            errors.append(_error(path, "generation-mode", f"生成模式无效: {mode}", "mode:"))
        if not isinstance(generation.get("owner"), str) or not generation["owner"].strip():
            errors.append(_error(path, "generation-owner", "owner 不得为空", "owner:"))
        expected_scope = f"scenarios/{directory.name}"
        if generation.get("write_scope") != expected_scope:
            errors.append(_error(path, "generation-scope", f"write_scope 必须为 {expected_scope}", "write_scope:"))
        reason = generation.get("degradation_reason")
        if mode == "sequential_degraded" and (not isinstance(reason, str) or not reason.strip()):
            errors.append(_error(path, "generation-degradation", "降级模式必须说明原因", "degradation_reason:"))
        if mode != "sequential_degraded" and reason is not None:
            errors.append(_error(path, "generation-degradation", "非降级模式的 degradation_reason 必须为 null", "degradation_reason:"))

    readiness = definition.get("readiness")
    readiness_keys = {"source_contract", "safe_control", "runtime_configuration", "test_data", "blockers"}
    if _exact_keys(path, readiness, readiness_keys, "readiness-schema", errors):
        if readiness.get("source_contract") not in {"confirmed", "blocked"} or readiness.get("safe_control") not in {"confirmed", "blocked"}:
            errors.append(_error(path, "readiness-contract", "源码契约和安全控制状态无效", "readiness:"))
        if readiness.get("runtime_configuration") not in {"confirmed", "missing"} or readiness.get("test_data") not in {"confirmed", "missing"}:
            errors.append(_error(path, "readiness-environment", "运行配置或测试数据状态无效", "readiness:"))
        if not _strings(readiness.get("blockers"), nonempty=False):
            errors.append(_error(path, "readiness-blockers", "blockers 必须是去重字符串列表", "blockers:"))

    if not _strings(definition.get("preconditions")):
        errors.append(_error(path, "preconditions", "preconditions 必须是非空去重字符串列表", "preconditions:"))

    steps = definition.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append(_error(path, "steps-required", "steps 必须是非空列表", "steps:"))
        steps = []
    step_ids: set[str] = set()
    for step in steps:
        if not isinstance(step, dict) or set(step) not in ({"id", "action", "control", "side_effect", "expect"}, {"id", "action", "control", "side_effect", "data_ref", "expect"}):
            errors.append(_error(path, "step-schema", "步骤键集合无效", "steps:"))
            continue
        if not isinstance(step.get("id"), str) or not step["id"].strip() or step["id"] in step_ids:
            errors.append(_error(path, "step-id", f"步骤 ID 无效或重复: {step.get('id')}", "id:"))
        step_ids.add(str(step.get("id")))
        if step.get("control") not in CONTROL_NAMES or step.get("side_effect") not in {"none", "read", "write"}:
            errors.append(_error(path, "step-control", f"步骤控制或副作用无效: {step.get('id')}", "control:"))
        if step.get("side_effect") == "write" and step.get("control") not in WRITE_CONTROLS:
            errors.append(_error(path, "step-side-effect-control", f"写步骤不得声明为只读或可观测控制: {step.get('id')}", "side_effect:"))
        if step.get("control") in INHERENT_WRITE_CONTROLS and step.get("side_effect") != "write":
            errors.append(_error(path, "step-side-effect-control", f"危险控制步骤必须声明 side_effect=write: {step.get('id')}", "side_effect:"))
        if step.get("control") in {"database_read", "observability"} and step.get("side_effect") == "write":
            errors.append(_error(path, "step-readonly-control", f"只读控制不得承担写步骤: {step.get('id')}", "control:"))
        if not isinstance(step.get("action"), str) or not step["action"].strip() or not _strings(step.get("expect")):
            errors.append(_error(path, "step-content", f"步骤动作或期望无效: {step.get('id')}", "action:"))
        if "data_ref" in step and (not isinstance(step["data_ref"], str) or not step["data_ref"].startswith("业务数据.json#/")):
            errors.append(_error(path, "data-ref", f"data_ref 无效: {step.get('data_ref')}", "data_ref:"))

    source_symbols = {
        f"{item.get('repo')}#{anchor}"
        for item in definition.get("source", []) if isinstance(item, dict)
        for anchor in item.get("anchors", [])
    }
    errors.extend(_control_errors(path, definition.get("controls"), steps, source_symbols))
    errors.extend(_isolation_errors(path, definition.get("isolation"), steps))
    controls = definition.get("controls", {})
    if isinstance(controls, dict):
        control_semantics = {
            "public_api": {"http_rpc"},
            "test_or_admin_api": {"http_rpc"},
            "mocks_and_faults": {"http_rpc", "configuration"},
            "dynamic_configuration": {"configuration"},
            "scheduled_jobs": {"jobs"},
            "messages": {"messages"},
            "database_read": {"database"},
            "database_control": {"database", "jobs"},
            "observability": {"http_rpc", "messages", "database", "cache", "jobs"},
        }
        for name, categories in control_semantics.items():
            item = controls.get(name, {})
            if not isinstance(item, dict) or item.get("status") in {"not_found", "not_applicable"}:
                continue
            for reference in item.get("evidence", []):
                if not _source_reference_semantic(project_root, discovery, str(reference), categories):
                    errors.append(_error(path, "control-evidence-semantic", f"控制证据与能力类别不匹配: {name}={reference}", str(reference)))
        for step in steps:
            if not isinstance(step, dict):
                continue
            control = controls.get(str(step.get("control")), {})
            if not isinstance(control, dict) or control.get("status") != "usable":
                errors.append(_error(path, "step-control-usable", f"步骤引用的控制能力不可用: {step.get('control')}", "control:"))
    decision = controls.get("decision", {}) if isinstance(controls, dict) else {}
    derived = _derived_status(readiness if isinstance(readiness, dict) else {}, decision if isinstance(decision, dict) else {})
    observability = controls.get("observability", {}) if isinstance(controls, dict) else {}
    if derived == "ready" and (not isinstance(observability, dict) or observability.get("status") != "usable"):
        errors.append(_error(path, "ready-observability", "ready 场景必须有 usable 的可观测证据与恢复能力", "observability:"))
        derived = None
    database_control = controls.get("database_control", {}) if isinstance(controls, dict) else {}
    if isinstance(database_control, dict) and database_control.get("status") == "usable":
        safety = database_control.get("safety", {})
        isolation = definition.get("isolation", {})
        if isinstance(safety, dict) and isinstance(isolation, dict):
            owned_selectors = set(str(item) for item in isolation.get("correlation_keys", []))
            owned_selectors.update(
                str(item.get("identity")) for item in isolation.get("owned_resources", []) if isinstance(item, dict)
            )
            if safety.get("exact_selector") not in owned_selectors:
                errors.append(_error(path, "database-selector-owned", "控制 SQL exact_selector 必须属于当前场景隔离契约", "exact_selector:"))
            if safety.get("consumer_source") not in source_symbols:
                errors.append(_error(path, "database-consumer-source", "consumer_source 必须解析到场景源码锚点", "consumer_source:"))
            elif not _source_reference_semantic(
                project_root,
                discovery,
                str(safety.get("consumer_source")),
                {"database", "jobs"},
            ):
                errors.append(_error(path, "database-consumer-semantic", "consumer_source 必须定位后续数据库消费或任务触发逻辑", "consumer_source:"))
            cleanup = definition.get("cleanup", {})
            trace_symbols = set(str(item) for item in definition.get("preconditions", []))
            trace_symbols.update(str(step.get("action")) for step in steps if isinstance(step, dict))
            trace_symbols.update(
                str(expectation)
                for step in steps if isinstance(step, dict)
                for expectation in step.get("expect", [])
            )
            if isinstance(cleanup, dict):
                trace_symbols.update(str(item) for item in cleanup.get("actions", []))
                trace_symbols.update(str(item) for item in cleanup.get("verifies", []))
            for field in ("snapshot", "mutation", "trigger", "verification", "restoration", "restoration_verification"):
                if safety.get(field) not in trace_symbols:
                    errors.append(_error(path, "database-control-trace", f"数据库控制符号未映射到场景链路: {field}", f"{field}:"))
            business_trigger_steps = [
                step for step in steps
                if isinstance(step, dict)
                and step.get("control") != "database_control"
                and step.get("action") == safety.get("trigger")
            ]
            if not business_trigger_steps or not any(
                safety.get("verification") in step.get("expect", []) for step in business_trigger_steps
            ):
                errors.append(_error(
                    path,
                    "database-control-business-path",
                    "控制 SQL 只能准备或推进测试状态，必须由独立正常业务步骤触发并验证最终结果",
                    "trigger:",
                ))
            target = safety.get("target_environment")
            if not isinstance(target, str) or not ENV_RE.fullmatch(target):
                errors.append(_error(path, "database-target-environment", "target_environment 必须是精确测试环境标识", "target_environment:"))
    isolation = definition.get("isolation", {})
    cleanup = definition.get("cleanup", {})
    if isinstance(observability, dict) and observability.get("status") == "usable" and isinstance(isolation, dict):
        observed_keys = set(str(item) for item in observability.get("correlation_keys", []))
        isolated_keys = set(str(item) for item in isolation.get("correlation_keys", []))
        if observed_keys != isolated_keys:
            errors.append(_error(path, "observability-correlation-mapping", "observability.correlation_keys 必须与场景隔离关联键完全一致", "correlation_keys:"))
        expected_evidence = {
            str(expectation)
            for step in steps if isinstance(step, dict)
            for expectation in step.get("expect", [])
        }
        if set(str(item) for item in observability.get("business_evidence", [])) != expected_evidence:
            errors.append(_error(path, "observability-evidence-mapping", "observability.business_evidence 必须完整映射步骤期望", "business_evidence:"))
        cleanup_symbols = set()
        if isinstance(cleanup, dict):
            cleanup_symbols.update(str(item) for item in cleanup.get("actions", []))
            cleanup_symbols.update(str(item) for item in cleanup.get("verifies", []))
        if set(str(item) for item in observability.get("recovery", [])) != cleanup_symbols:
            errors.append(_error(path, "observability-recovery-mapping", "observability.recovery 必须完整映射 cleanup 动作和验证", "recovery:"))
    if isinstance(meta, dict) and meta.get("status") != derived:
        errors.append(_error(path, "scenario-status-derived", f"声明状态与推导状态不符: declared={meta.get('status')}, derived={derived}", "status:"))

    if isinstance(meta, dict) and meta.get("status") == "contract_blocked" and isinstance(controls, dict):
        readiness_blockers = readiness.get("blockers", []) if isinstance(readiness, dict) else []
        decision_blockers = decision.get("blockers", []) if isinstance(decision, dict) else []
        if readiness_blockers != decision_blockers:
            errors.append(_error(path, "contract-blocker-consistency", "contract_blocked 的 readiness 与 control decision 必须记录同一组阻塞", "blockers:"))
        control_candidates = set(CONTROL_NAMES) - {"database_read", "observability"}
        for blocker in decision_blockers if isinstance(decision_blockers, list) else []:
            if str(blocker).startswith("control:"):
                name = str(blocker).split(":", 1)[1]
                item = controls.get(name, {})
                if name not in control_candidates or not isinstance(item, dict) or item.get("status") not in {"unusable", "not_found"} or not item.get("evidence"):
                    errors.append(_error(path, "contract-control-blocker", f"控制阻塞未绑定不可用能力及源码证据: {blocker}", str(blocker)))
            elif str(blocker).startswith("contract:"):
                reference = str(blocker).split(":", 1)[1]
                if reference not in source_symbols:
                    errors.append(_error(path, "contract-source-blocker", f"契约阻塞未绑定场景源码锚点: {blocker}", str(blocker)))
            else:
                errors.append(_error(path, "contract-blocker-format", f"契约阻塞必须使用 control:<name> 或 contract:<repo>#<anchor>: {blocker}", str(blocker)))
        if isinstance(readiness, dict) and readiness.get("safe_control") == "blocked":
            incomplete = [
                name for name in control_candidates
                if not isinstance(controls.get(name), dict)
                or controls[name].get("status") not in {"unusable", "not_found"}
                or not controls[name].get("evidence")
            ]
            if incomplete:
                errors.append(_error(path, "contract-safe-control", f"contract_blocked 前必须以源码证据确认全部候选控制不可用: {sorted(incomplete)}", "safe_control:"))

    errors.extend(_integration_errors(path, definition.get("integrations"), discovery))
    configuration = discovery.get("configuration", {}) if isinstance(discovery, dict) else {}
    owner_by_integration = {
        str(item.get("id")): str(item.get("owner"))
        for collection in ("services", "data_sources", "middleware", "controls")
        for item in configuration.get(collection, []) if isinstance(item, dict)
    }
    integrations = definition.get("integrations", {})
    used_ids = set(integrations.get("services", [])) if isinstance(integrations, dict) and isinstance(integrations.get("services"), list) else set()
    if isinstance(integrations, dict) and isinstance(integrations.get("components"), list):
        used_ids.update(str(item.get("id")) for item in integrations["components"] if isinstance(item, dict))
    required_source_repos = {
        owner_by_integration[item].split(":", 1)[0]
        for item in used_ids if item in owner_by_integration
    }
    declared_source_repos = {
        str(item.get("repo")) for item in definition.get("source", []) if isinstance(item, dict)
    }
    if required_source_repos - declared_source_repos:
        errors.append(_error(
            path,
            "source-owner-coverage",
            f"场景源码基线遗漏集成所有者仓库: {sorted(required_source_repos - declared_source_repos)}",
            "source:",
        ))
    errors.extend(_cleanup_errors(path, definition.get("cleanup"), steps, definition.get("isolation")))
    errors.extend(_source_errors(path, definition.get("source"), discovery, project_root))
    errors.extend(_scenario_artifact_errors(project_root, directory, definition))
    return errors


def _integration_errors(path: Path, integrations: Any, discovery: dict[str, Any]) -> list[str]:
    """校验场景依赖只引用发现契约中的服务和组件。"""

    errors: list[str] = []
    if not _exact_keys(path, integrations, {"services", "components"}, "integration-schema", errors):
        return errors
    configuration = discovery.get("configuration", {}) if isinstance(discovery, dict) else {}
    service_ids = {str(item.get("id")) for item in configuration.get("services", []) if isinstance(item, dict)}
    component_types = {
        str(item.get("id")): str(item.get("type"))
        for name in ("data_sources", "middleware", "controls")
        for item in configuration.get(name, [])
        if isinstance(item, dict)
    }
    services = integrations.get("services")
    if not _strings(services, nonempty=False) or not set(services).issubset(service_ids):
        errors.append(_error(path, "integration-service", "services 包含未发现或重复的服务", "services:"))
    components = integrations.get("components")
    if not isinstance(components, list):
        errors.append(_error(path, "integration-components", "components 必须是列表", "components:"))
        return errors
    seen: set[str] = set()
    for component in components:
        if not isinstance(component, dict) or set(component) != {"id", "type", "required"}:
            errors.append(_error(path, "integration-component-schema", "组件依赖条目无效", "components:"))
            continue
        component_id = component.get("id")
        if component_id not in component_types or component_id in seen or not isinstance(component.get("required"), bool):
            errors.append(_error(path, "integration-component", f"组件依赖无效或重复: {component_id}", "id:"))
        elif str(component.get("type")) != component_types[component_id]:
            errors.append(_error(path, "integration-component-type", f"组件类型与发现结果不符: {component_id}", "type:"))
        seen.add(str(component_id))
    return errors


def _cleanup_errors(path: Path, cleanup: Any, steps: list[Any], isolation: Any) -> list[str]:
    """校验写步骤具有明确且可验证的清理契约。"""

    errors: list[str] = []
    if not _exact_keys(path, cleanup, {"strategy", "actions", "verifies"}, "cleanup-schema", errors):
        return errors
    if not isinstance(cleanup.get("strategy"), str) or not cleanup["strategy"].strip() or not _strings(cleanup.get("actions")) or not _strings(cleanup.get("verifies")):
        errors.append(_error(path, "cleanup-content", "清理策略、动作和验证均不得为空", "cleanup:"))
    if any(isinstance(step, dict) and step.get("side_effect") == "write" for step in steps) and not cleanup.get("actions"):
        errors.append(_error(path, "cleanup-write-required", "写步骤必须声明清理动作", "cleanup:"))
    if isinstance(isolation, dict) and isinstance(cleanup, dict):
        actions = set(str(item) for item in cleanup.get("actions", []))
        verifies = set(str(item) for item in cleanup.get("verifies", []))
        for resource in isolation.get("owned_resources", []):
            if not isinstance(resource, dict):
                continue
            missing_actions = {
                str(resource.get(field)) for field in ("cleanup", "restore")
                if str(resource.get(field)) not in actions
            }
            if missing_actions:
                errors.append(_error(path, "cleanup-resource-action", f"拥有资源的清理/恢复动作未进入 finally 契约: {sorted(missing_actions)}", "actions:"))
            if str(resource.get("verify")) not in verifies:
                errors.append(_error(path, "cleanup-resource-verify", f"拥有资源的恢复验证未进入契约: {resource.get('verify')}", "verifies:"))
    return errors


def _source_errors(path: Path, source: Any, discovery: dict[str, Any], project_root: Path) -> list[str]:
    """校验场景源码基线可解析且锚点非空。"""

    errors: list[str] = []
    repositories = {
        str(item.get("id")): item
        for item in discovery.get("inventory", {}).get("repositories", [])
        if isinstance(item, dict)
    }
    if not isinstance(source, list) or not source:
        return [_error(path, "source-required", "source 必须是非空列表", "source:")]
    seen: set[str] = set()
    for item in source:
        if not isinstance(item, dict) or set(item) != {"repo", "commit", "anchors"}:
            errors.append(_error(path, "source-schema", "源码条目无效", "source:"))
            continue
        repo_id = item.get("repo")
        if repo_id not in repositories or repo_id in seen:
            errors.append(_error(path, "source-repository", f"源码仓库未知或重复: {repo_id}", "repo:"))
            continue
        seen.add(str(repo_id))
        commit = item.get("commit")
        if not isinstance(commit, str) or not SHA_RE.fullmatch(commit) or not _strings(item.get("anchors")):
            errors.append(_error(path, "source-contract", f"源码提交或锚点无效: {repo_id}", "commit:"))
            continue
        repo_root = _resolve(project_root, str(repositories[repo_id].get("root", "")))
        if commit != repositories[repo_id].get("commit"):
            errors.append(_error(path, "source-commit-coherence", f"场景源码提交必须等于工作区发现快照: {repo_id}", "commit:"))
        if _git(repo_root, "cat-file", "-e", f"{commit}^{{commit}}").returncode != 0:
            errors.append(_error(path, "source-commit", f"源码提交不存在: {repo_id}#{commit}", commit))
        for anchor in item["anchors"]:
            if _git(repo_root, "grep", "-q", "--fixed-strings", str(anchor), commit, "--", ".").returncode != 0:
                errors.append(_error(path, "source-anchor", f"源码锚点无法解析: {repo_id}#{anchor}", str(anchor)))
    relevant_repositories = {
        str(node.get("id", "")).split(":", 1)[0]
        for node in discovery.get("topology", {}).get("nodes", [])
        if isinstance(node, dict) and node.get("relevant") is True and ":" in str(node.get("id", ""))
    }
    missing_repositories = relevant_repositories - seen
    if missing_repositories:
        errors.append(_error(path, "source-relevant-coverage", f"场景源码基线遗漏相关依赖仓库: {sorted(missing_repositories)}", "source:"))
    return errors


def _scenario_artifact_errors(project_root: Path, directory: Path, definition: dict[str, Any]) -> list[str]:
    """校验场景必需文件、环境数据路径和稳定标识。"""

    errors: list[str] = []
    definition_path = directory / "场景定义.yaml"
    required = [directory / "业务数据.json", directory / "业务流程图.md", directory / f"test_{directory.name}.py"]
    for path in required:
        if not path.is_file():
            errors.append(_error(path, "scenario-artifact", "场景必需文件不存在"))
    runtime_path = project_root / "config" / "config.yaml"
    runtime = _load_yaml(runtime_path, errors)
    active = runtime.get("active_environment")
    if not isinstance(active, str) or not ENV_RE.fullmatch(active):
        errors.append(_error(runtime_path, "active-environment", "active_environment 格式无效", "active_environment"))
    environment_path = project_root / "config" / "environments" / f"{active}.yaml" if isinstance(active, str) else None
    if isinstance(active, str) and environment_path is not None and not environment_path.is_file():
        errors.append(_error(runtime_path, "active-environment-file", f"缺少环境文件: {active}.yaml", active))
    environment: dict[str, Any] = {}
    if environment_path is not None and environment_path.is_file():
        environment = _load_yaml(environment_path, errors)
    integrations = definition.get("integrations", {})
    missing_runtime: list[str] = []
    runtime_blockers: set[str] = set()
    if definition.get("meta", {}).get("status") == "ready" and isinstance(integrations, dict):
        available_services = environment.get("services", {}) if isinstance(environment.get("services"), dict) else {}
        available_components = environment.get("components", {}) if isinstance(environment.get("components"), dict) else {}
        missing_services = [item for item in integrations.get("services", []) if item not in available_services]
        missing_components = [
            item.get("id") for item in integrations.get("components", [])
            if isinstance(item, dict) and item.get("required") is True and item.get("id") not in available_components
        ]
        if missing_services or missing_components:
            errors.append(_error(directory / "场景定义.yaml", "ready-runtime-mapping", f"ready 场景缺少激活环境映射: services={missing_services}, components={missing_components}", "integrations:"))
        missing_runtime.extend(f"services.{item}" for item in missing_services)
        missing_runtime.extend(f"components.{item}" for item in missing_components)
        runtime_blockers.update(f"config:services/{item}" for item in missing_services)
        runtime_blockers.update(f"config:components/{item}" for item in missing_components)
    if isinstance(integrations, dict):
        available_services = environment.get("services", {}) if isinstance(environment.get("services"), dict) else {}
        available_components = environment.get("components", {}) if isinstance(environment.get("components"), dict) else {}
        selected_runtime = {
            "services": {item: available_services[item] for item in integrations.get("services", []) if item in available_services},
            "components": {
                item.get("id"): available_components[item.get("id")]
                for item in integrations.get("components", []) if isinstance(item, dict)
                and item.get("required") is True and item.get("id") in available_components
            },
        }
        missing_runtime.extend(_missing_placeholders(selected_runtime, "$.configuration"))
        runtime_blockers.update(_missing_placeholder_blockers(selected_runtime, "config"))
    database_control = definition.get("controls", {}).get("database_control", {})
    if isinstance(database_control, dict) and database_control.get("status") == "usable":
        safety = database_control.get("safety", {})
        if isinstance(safety, dict) and definition.get("meta", {}).get("status") == "ready" and safety.get("target_environment") != active:
            errors.append(_error(directory / "场景定义.yaml", "database-target-active", "ready 场景的数据库控制环境必须等于 active_environment", "target_environment:"))
    data = _load_json(directory / "业务数据.json", errors)
    if isinstance(data, dict):
        errors.extend(_secret_errors(directory / "业务数据.json", data))
        logical_paths: dict[str, set[str]] = {}
        for environment, values in data.items():
            if not isinstance(values, dict):
                errors.append(_error(directory / "业务数据.json", "business-data-environment", f"环境数据必须是映射: {environment}"))
                continue
            paths: set[str] = set()

            def collect(value: Any, prefix: str = "") -> None:
                """收集环境对象的逻辑叶子路径。"""

                if isinstance(value, dict):
                    for key, child in value.items():
                        collect(child, f"{prefix}/{key}")
                elif isinstance(value, list):
                    for index, child in enumerate(value):
                        collect(child, f"{prefix}/{index}")
                else:
                    paths.add(prefix)
                    if isinstance(value, str) and (URL_RE.search(value) or SQL_RE.search(value)):
                        errors.append(_error(directory / "业务数据.json", "business-data-content", f"业务数据不得保存端点或 SQL: {environment}{prefix}"))

            collect(values)
            logical_paths[str(environment)] = paths
        if logical_paths and len({frozenset(paths) for paths in logical_paths.values()}) > 1:
            errors.append(_error(directory / "业务数据.json", "business-data-shape", "所有环境必须暴露相同逻辑数据路径"))
        status = definition.get("meta", {}).get("status")
        if status == "ready" and not isinstance(data.get(active), dict):
            errors.append(_error(directory / "业务数据.json", "active-business-data", f"ready 场景缺少环境数据: {active}"))
        missing_test_data = [] if isinstance(data.get(active), dict) else [f"business_data:{active}"]
        data_blockers = set() if isinstance(data.get(active), dict) else {f"business_data:{active}"}
        if isinstance(data.get(active), dict):
            missing_test_data.extend(_missing_placeholders(data[active], "$.business_data"))
            data_blockers.update(_missing_placeholder_blockers(data[active], "business_data"))
        if status == "ready" and (missing_runtime or missing_test_data):
            errors.append(_error(directory / "场景定义.yaml", "ready-runtime-values", f"ready 场景仍缺少运行值: runtime={missing_runtime}, test_data={missing_test_data}", "status: ready"))
        readiness = definition.get("readiness", {})
        if status == "pending_environment" and isinstance(readiness, dict):
            blockers = set(str(item) for item in readiness.get("blockers", []))
            expected_blockers = runtime_blockers | data_blockers
            if blockers != expected_blockers:
                errors.append(_error(
                    directory / "场景定义.yaml",
                    "pending-blocker-binding",
                    f"pending_environment 阻塞必须与当前实际缺失项精确一致: expected={sorted(expected_blockers)}, actual={sorted(blockers)}",
                    "blockers:",
                ))
            if readiness.get("runtime_configuration") != ("missing" if runtime_blockers else "confirmed"):
                errors.append(_error(directory / "场景定义.yaml", "pending-runtime-state", "runtime_configuration 与实际缺失配置不一致", "runtime_configuration:"))
            if readiness.get("test_data") != ("missing" if data_blockers else "confirmed"):
                errors.append(_error(directory / "场景定义.yaml", "pending-data-state", "test_data 与实际缺失数据不一致", "test_data:"))
        for step in definition.get("steps", []):
            if not isinstance(step, dict) or "data_ref" not in step:
                continue
            pointer = str(step["data_ref"]).split("#/", 1)[-1]
            for environment, values in data.items():
                current = values
                try:
                    for part in pointer.split("/"):
                        key = part.replace("~1", "/").replace("~0", "~")
                        current = current[int(key)] if isinstance(current, list) else current[key]
                except (KeyError, IndexError, TypeError, ValueError):
                    errors.append(_error(directory / "业务数据.json", "data-ref-resolve", f"data_ref 在环境 {environment} 中无法解析: {step['data_ref']}"))
                    continue
                if not _has_constructed_business_value(current):
                    errors.append(_error(
                        directory / "业务数据.json",
                        "business-data-overinjected",
                        f"data_ref 在环境 {environment} 中全部依赖变量注入；应按源码契约保留可构造字面值或在运行期生成随机值: {step['data_ref']}",
                    ))
    test_path = directory / f"test_{directory.name}.py"
    stable_id = definition.get("meta", {}).get("id")
    if test_path.is_file() and isinstance(stable_id, str):
        content = test_path.read_text(encoding="utf-8")
        marker = f'scenario_id("{stable_id}")'
        if content.count(marker) != 1:
            errors.append(_error(test_path, "scenario-marker", "稳定 ID 必须只出现在一个 scenario_id marker 中", "scenario_id"))
        if content.count("business_e2e") != 1:
            errors.append(_error(test_path, "business-marker", "场景测试必须有且只有一个 business_e2e marker", "business_e2e"))
        if content.count(stable_id) != 1:
            errors.append(_error(test_path, "scenario-id-unique", "稳定 ID 在场景测试中只能出现于唯一 marker", stable_id))
        generated_roots = [project_root / name for name in ("common", "config", "scripts", "tests", "scenarios")]
        for root in generated_roots:
            if not root.is_dir():
                continue
            for other in _walk_files(root):
                if other in {definition_path, test_path} or other.suffix.lower() not in {".py", ".yaml", ".yml", ".json", ".md", ".toml"}:
                    continue
                try:
                    if stable_id in other.read_text(encoding="utf-8"):
                        errors.append(_error(other, "scenario-id-leak", "稳定 ID 不得复制到 marker 之外"))
                except (OSError, UnicodeError):
                    continue
    return errors


def contract_errors(project_root: Path, selected: str | None = None) -> tuple[list[str], list[tuple[Path, dict[str, Any]]], dict[str, Any]]:
    """校验全部或指定场景契约，并执行跨场景所有权检查。"""

    errors, discovery = discovery_errors(project_root)
    directories = _scenario_directories(project_root, None, errors)
    all_scenarios: list[tuple[Path, dict[str, Any]]] = []
    for directory in directories:
        definition_path = directory / "场景定义.yaml"
        definition = _load_yaml(definition_path, errors)
        if definition:
            all_scenarios.append((directory, definition))
            errors.extend(_scenario_errors(project_root, directory, definition, discovery))
    errors.extend(_cross_scenario_errors(all_scenarios))
    if selected is None:
        return errors, all_scenarios, discovery
    selected_directories = _scenario_directories(project_root, selected, errors)
    selected_paths = set(selected_directories)
    return errors, [item for item in all_scenarios if item[0] in selected_paths], discovery


def _cross_scenario_errors(scenarios: list[tuple[Path, dict[str, Any]]]) -> list[str]:
    """校验多场景委派和资源隔离不发生冲突。"""

    errors: list[str] = []
    if len(scenarios) > 1:
        modes = {str(definition.get("generation", {}).get("mode")) for _, definition in scenarios}
        if modes not in ({"delegated"}, {"sequential_degraded"}):
            errors.append(_error(scenarios[0][0] / "场景定义.yaml", "multi-scenario-mode", "多场景必须全部 delegated，或全部明确 sequential_degraded"))
        owners = [str(definition.get("generation", {}).get("owner")) for _, definition in scenarios]
        if "delegated" in modes and len(owners) != len(set(owners)):
            errors.append(_error(scenarios[0][0] / "场景定义.yaml", "multi-scenario-owner", "delegated 场景 owner 必须唯一"))

    namespaces: dict[str, Path] = {}
    correlations: dict[str, tuple[Path, str | None]] = {}
    resources: dict[str, tuple[Path, str | None]] = {}
    mutable: dict[str, tuple[Path, str | None]] = {}
    for directory, definition in scenarios:
        path = directory / "场景定义.yaml"
        isolation = definition.get("isolation", {})
        if not isinstance(isolation, dict):
            continue
        namespace = isolation.get("namespace")
        if isinstance(namespace, str) and namespace:
            if namespace in namespaces:
                errors.append(_error(path, "isolation-namespace-collision", f"命名空间与 {namespaces[namespace]} 冲突: {namespace}", "namespace:"))
            namespaces[namespace] = path
        lock = isolation.get("serial_lock") if isinstance(isolation.get("serial_lock"), str) else None
        for correlation in isolation.get("correlation_keys", []):
            correlation = str(correlation)
            if correlation in correlations:
                errors.append(_error(path, "isolation-correlation-collision", f"关联键与 {correlations[correlation][0]} 冲突: {correlation}", "correlation_keys:"))
            correlations[correlation] = (path, lock)
        for resource in isolation.get("owned_resources", []):
            if not isinstance(resource, dict):
                continue
            identity = str(resource.get("identity", ""))
            if identity in resources:
                errors.append(_error(path, "isolation-resource-collision", f"拥有资源与 {resources[identity][0]} 冲突: {identity}", "identity:"))
            resources[identity] = (path, lock)
        for control in isolation.get("mutable_controls", []):
            control = str(control)
            if control in mutable:
                errors.append(_error(path, "isolation-control-collision", f"可变控制与 {mutable[control][0]} 冲突: {control}", "mutable_controls:"))
            mutable[control] = (path, lock)
    return errors


def _asset_errors(project_root: Path) -> list[str]:
    """校验安装清单中的门禁文件未缺失、漂移或逃逸工程目录。"""

    manifest_path = project_root / ".e2e-gates.json"
    errors: list[str] = []
    manifest = _load_json(manifest_path, errors)
    if not isinstance(manifest, dict) or set(manifest) != {"version", "files"}:
        return errors + [_error(manifest_path, "asset-manifest", "门禁资产清单结构无效")]
    if manifest.get("version") != RULES_VERSION or not isinstance(manifest.get("files"), dict):
        errors.append(_error(manifest_path, "asset-version", "门禁资产清单版本无效"))
        return errors
    root = project_root.resolve()
    expected_files = set(FIXED_ASSETS)
    actual_files = set(manifest["files"])
    for relative in sorted(expected_files - actual_files):
        errors.append(_error(manifest_path, "asset-manifest-missing", f"门禁资产清单遗漏: {relative}"))
    for relative in sorted(actual_files - expected_files):
        errors.append(_error(manifest_path, "asset-manifest-extra", f"门禁资产清单包含未知文件: {relative}"))
    for relative, expected in manifest["files"].items():
        if not isinstance(relative, str) or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            errors.append(_error(manifest_path, "asset-entry", f"门禁资产条目无效: {relative}"))
            continue
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            errors.append(_error(manifest_path, "asset-path", f"门禁资产路径逃逸工程: {relative}"))
            continue
        if not path.is_file():
            errors.append(_error(path, "asset-missing", "已安装门禁资产缺失"))
        elif hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            errors.append(_error(path, "asset-drift", "已安装门禁资产内容漂移"))
    return errors


def _python_paths(project_root: Path) -> list[Path]:
    """返回工程内全部自有 Python 文件，跳过依赖与构建目录。"""

    return sorted(path for path in _walk_files(project_root) if path.suffix == ".py")


def _expression_name(current: ast.AST) -> str:
    """Convert a name or attribute expression into a dotted name."""

    parts: list[str] = []
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _call_name(node: ast.Call) -> str:
    """将调用目标转换为用于窄规则匹配的点分名称。"""

    return _expression_name(node.func)


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    """Return module and from-import aliases for canonical call matching."""

    aliases: dict[str, str] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                aliases[alias.asname or alias.name.split(".", 1)[0]] = alias.name
        elif isinstance(statement, ast.ImportFrom) and statement.module:
            for alias in statement.names:
                aliases[alias.asname or alias.name] = f"{statement.module}.{alias.name}"
    return aliases


def _resolved_call_name(node: ast.Call, aliases: Mapping[str, str]) -> str:
    """Resolve the leading import alias in a dotted call name."""

    name = _call_name(node)
    return _resolved_name(name, aliases)


def _resolved_name(name: str, aliases: Mapping[str, str]) -> str:
    """Resolve the leading alias in an already dotted name."""

    head, separator, tail = name.partition(".")
    target = aliases.get(head)
    return f"{target}.{tail}" if target and separator else target or name


def _call_argument(node: ast.Call, name: str, position: int) -> ast.AST | None:
    """读取调用的位置参数或同名关键字参数。"""

    if len(node.args) > position:
        return node.args[position]
    return next((keyword.value for keyword in node.keywords if keyword.arg == name), None)


def _enclosing_function(
    node: ast.AST,
    parents: Mapping[ast.AST, ast.AST],
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """返回节点所在的最近函数。"""

    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current
        current = parents.get(current)
    return None


def _contains_expression(node: ast.AST, expression: ast.AST) -> bool:
    """判断节点树中是否包含完全相同的运行表达式。"""

    expected = ast.dump(expression, include_attributes=False)
    return any(ast.dump(child, include_attributes=False) == expected for child in ast.walk(node))


def _assigned_names(node: ast.AST, parents: Mapping[ast.AST, ast.AST]) -> set[str]:
    """收集包含该表达式的赋值语句所绑定的简单名称。"""

    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, ast.Assign):
            return {
                child.id for target in current.targets for child in ast.walk(target)
                if isinstance(child, ast.Name)
            }
        if isinstance(current, ast.AnnAssign):
            return {child.id for child in ast.walk(current.target) if isinstance(child, ast.Name)}
        if isinstance(current, (ast.Expr, ast.Return, ast.Raise)):
            break
    return set()


def _direct_statement(node: ast.AST, function: ast.FunctionDef | ast.AsyncFunctionDef, parents: Mapping[ast.AST, ast.AST]) -> ast.stmt | None:
    """返回函数体中的直接语句；分支、循环、异常和上下文内部一律不算支配。"""

    current = node
    while current in parents and parents[current] is not function:
        current = parents[current]
        if isinstance(current, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith, ast.Match)):
            return None
    return current if isinstance(current, ast.stmt) and parents.get(current) is function else None


def _straight_line_precedes(
    operation: ast.Call,
    evidence: ast.Call,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    parents: Mapping[ast.AST, ast.AST],
) -> bool:
    """证明操作和证据位于同一函数体基本块且操作严格在前。"""

    operation_statement = _direct_statement(operation, function, parents)
    evidence_statement = _direct_statement(evidence, function, parents)
    if operation_statement is None or evidence_statement is None:
        return False
    operation_index = function.body.index(operation_statement)
    evidence_index = function.body.index(evidence_statement)
    if operation_index >= evidence_index:
        return False
    between = function.body[operation_index + 1:evidence_index]
    if any(
        isinstance(statement, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith, ast.Match, ast.Return, ast.Raise))
        for statement in between
    ):
        return False
    result_names = _assigned_names(operation, parents)
    return not any(
        result_names.intersection(
            child.id for node in ast.walk(statement) if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            for child in ast.walk(target) if isinstance(child, ast.Name)
        )
        for statement in between
    )


def _return_uses_call(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    call: ast.Call,
    parents: Mapping[ast.AST, ast.AST],
) -> bool:
    """证明函数返回值由同一基本块中的真实调用结果派生。"""

    statement = _direct_statement(call, function, parents)
    if statement is None:
        return False
    if isinstance(statement, ast.Return):
        return True
    names = _assigned_names(call, parents)
    if not names:
        return False
    statement_index = function.body.index(statement)
    for candidate in function.body[statement_index + 1:]:
        if isinstance(candidate, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith, ast.Match, ast.Raise)):
            return False
        if isinstance(candidate, ast.Return) and candidate.value is not None:
            returned = {node.id for node in ast.walk(candidate.value) if isinstance(node, ast.Name)}
            return bool(names.intersection(returned))
        assigned = {
            child.id for node in ast.walk(candidate) if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            for child in ast.walk(target) if isinstance(child, ast.Name)
        }
        if names.intersection(assigned):
            return False
    return False


def _literal_text(expression: ast.AST | None, function: ast.AST, before_line: int) -> str | None:
    """保守解析直接字面量或当前函数内先前绑定的字符串。"""

    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        return expression.value
    if not isinstance(expression, ast.Name):
        return None
    assignments = [
        node for node in ast.walk(function)
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and getattr(node, "lineno", before_line) < before_line
    ]
    for assignment in sorted(assignments, key=lambda item: item.lineno, reverse=True):
        targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
        if not any(isinstance(target, ast.Name) and target.id == expression.id for target in targets):
            continue
        value = assignment.value
        return value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else None
    return None


def _resolved_expression(
    expression: ast.AST | None,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    before: tuple[int, int],
    parents: Mapping[ast.AST, ast.AST],
) -> ast.AST | None:
    """解析同一函数基本块中最近一次简单名称赋值。"""

    if not isinstance(expression, ast.Name):
        return expression
    candidates: list[ast.Assign | ast.AnnAssign] = []
    for statement in function.body:
        position = (getattr(statement, "lineno", 0), getattr(statement, "col_offset", 0))
        if position >= before or not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        if any(isinstance(target, ast.Name) and target.id == expression.id for target in targets):
            candidates.append(statement)
    if not candidates:
        return expression
    assignment = candidates[-1]
    if _direct_statement(assignment, function, parents) is None:
        return None
    return assignment.value


def _expression_uses_names(expression: ast.AST | None, names: set[str]) -> bool:
    """判断表达式是否消费指定真实操作结果。"""

    return expression is not None and bool({node.id for node in ast.walk(expression) if isinstance(node, ast.Name)} & names)


def _operation_binds_identity(operation: ast.Call, identity: ast.AST) -> bool:
    """要求关联值进入资源定位参数或业务载荷，而非日志/追踪等旁路字段。"""

    leaf = _call_name(operation).rsplit(".", 1)[-1].casefold()
    if leaf in {"execute", "executemany"}:
        return _contains_expression(_call_argument(operation, "parameters", 1) or operation, identity)

    def payload_binds(expression: ast.AST) -> bool:
        """拒绝仅把关联值塞进追踪元数据。"""

        if isinstance(expression, ast.Dict):
            for key, value in zip(expression.keys, expression.values):
                key_text = key.value.casefold() if isinstance(key, ast.Constant) and isinstance(key.value, str) else ""
                if key_text in {"tracking", "trace", "telemetry", "metadata", "headers", "logging", "log"}:
                    continue
                if payload_binds(value):
                    return True
            return False
        if isinstance(expression, (ast.List, ast.Tuple, ast.Set)):
            return any(payload_binds(item) for item in expression.elts)
        return _contains_expression(expression, identity)

    if any(payload_binds(argument) for argument in operation.args):
        return True
    identity_keywords = {"json", "data", "params", "body", "payload", "key", "id", "identity", "resource", "resource_ref", "correlation", "correlation_ref", "selector", "url", "target"}
    return any(
        keyword.arg in identity_keywords and payload_binds(keyword.value)
        for keyword in operation.keywords if keyword.arg is not None
    )


def _constant_or_tautology(
    expression: ast.AST,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    parents: Mapping[ast.AST, ast.AST],
) -> bool:
    """识别常量别名和明显恒等比较。"""

    resolved = _resolved_expression(
        expression,
        function,
        (getattr(expression, "lineno", 0), getattr(expression, "col_offset", 0)),
        parents,
    )
    if resolved is None or isinstance(resolved, ast.Constant):
        return True
    if isinstance(resolved, ast.Compare):
        operands = [resolved.left, *resolved.comparators]
        dumps = [ast.dump(item, include_attributes=False) for item in operands]
        if len(set(dumps)) == 1:
            return True
    if isinstance(resolved, ast.BoolOp):
        if isinstance(resolved.op, ast.Or) and any(isinstance(item, ast.Constant) and item.value is True for item in resolved.values):
            return True
        if isinstance(resolved.op, ast.And) and any(isinstance(item, ast.Constant) and item.value is False for item in resolved.values):
            return True
    return False


def _sql_where_binds_selector(call: ast.Call, function: ast.AST) -> bool:
    """证明 execute 参数把 selector_ref 绑定到 WHERE 占位符位置。"""

    sql = _literal_text(_call_argument(call, "operation", 0), function, call.lineno)
    parameters = _call_argument(call, "parameters", 1)
    if sql is None or parameters is None:
        return False
    where = re.search(r"\bWHERE\b", sql, re.IGNORECASE)
    if where is None or not _exact_sql_where(sql):
        return False
    placeholders = list(PARAMETER_RE.finditer(sql))
    where_placeholders = [(index, match.group(0)) for index, match in enumerate(placeholders) if match.start() > where.start()]
    if not where_placeholders:
        return False

    def is_selector(expression: ast.AST) -> bool:
        """仅接受契约回调参数本身作为选择器值。"""

        return isinstance(expression, ast.Name) and expression.id == "selector_ref"

    if isinstance(parameters, (ast.Tuple, ast.List)):
        values = list(parameters.elts)
        for position, token in where_placeholders:
            if token.startswith("$") and token[1:].isdigit():
                position = int(token[1:]) - 1
            if token in {"?", "%s"} or token.startswith("$"):
                if 0 <= position < len(values) and is_selector(values[position]):
                    return True
    if isinstance(parameters, ast.Dict):
        values = {
            str(key.value): value
            for key, value in zip(parameters.keys, parameters.values)
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        for _, token in where_placeholders:
            if token.startswith("%("):
                key = token[2:-2]
            elif token.startswith(":"):
                key = token[1:]
            else:
                continue
            if key in values and is_selector(values[key]):
                return True
    return False


def _sql_set_binds_original(call: ast.Call, function: ast.AST) -> bool:
    """Prove the saved original value is bound before the exact WHERE clause."""

    sql = _literal_text(_call_argument(call, "operation", 0), function, call.lineno)
    parameters = _call_argument(call, "parameters", 1)
    if sql is None or parameters is None:
        return False
    where = re.search(r"\bWHERE\b", sql, re.IGNORECASE)
    if where is None:
        return False
    placeholders = list(PARAMETER_RE.finditer(sql))
    prefix = [(index, match.group(0)) for index, match in enumerate(placeholders) if match.start() < where.start()]
    if isinstance(parameters, (ast.Tuple, ast.List)):
        for position, token in prefix:
            if token.startswith("$") and token[1:].isdigit():
                position = int(token[1:]) - 1
            if 0 <= position < len(parameters.elts):
                value = parameters.elts[position]
                if isinstance(value, ast.Name) and value.id == "original":
                    return True
    if isinstance(parameters, ast.Dict):
        values = {
            str(key.value): value for key, value in zip(parameters.keys, parameters.values)
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        for _, token in prefix:
            key = token[2:-2] if token.startswith("%(") else token[1:] if token.startswith(":") else ""
            value = values.get(key)
            if isinstance(value, ast.Name) and value.id == "original":
                return True
    return False


def _exact_sql_where(sql: str) -> bool:
    """保守接受仅由等值参数谓词组成的单语句 WHERE。"""

    if re.search(r"(?:--|/\*|\*/|#|;|\bOR\b|\bUNION\b|\bLIKE\b|\bBETWEEN\b|\bIN\s*\(|\bEXISTS\b)", sql, re.IGNORECASE):
        return False
    match = re.search(r"\bWHERE\b(?P<where>.+)$", sql, re.IGNORECASE | re.DOTALL)
    if match is None:
        return False
    identifier = r'(?:[A-Za-z_][A-Za-z0-9_$]*|"[^"]+"|`[^`]+`|\[[^\]]+\])'
    qualified = rf"{identifier}(?:\s*\.\s*{identifier})?"
    placeholder = r"(?:\?|%s|%\([A-Za-z_][A-Za-z0-9_]*\)s|:[A-Za-z_][A-Za-z0-9_]*|\$[1-9][0-9]*)"
    predicate = rf"\s*{qualified}\s*=\s*{placeholder}\s*"
    return re.fullmatch(rf"{predicate}(?:\bAND\b{predicate})*", match.group("where"), re.IGNORECASE) is not None


def _source_reference_exists(project_root: Path, reference: str) -> bool:
    """核对 repository#anchor 是否存在于发现契约固定的提交。"""

    if "#" not in reference:
        return False
    repo_id, anchor = reference.split("#", 1)
    try:
        document = yaml.safe_load((project_root / "discovery" / "workspace.yaml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return False
    repositories = document.get("inventory", {}).get("repositories", []) if isinstance(document, dict) else []
    for repository in repositories:
        if not isinstance(repository, dict) or repository.get("id") != repo_id:
            continue
        root = _resolve(project_root, str(repository.get("root", "")))
        commit = str(repository.get("commit", ""))
        return bool(anchor) and _git(root, "grep", "-q", "--fixed-strings", anchor, commit, "--", ".").returncode == 0
    return False


def _source_reference_semantic(
    project_root: Path,
    discovery: Mapping[str, Any],
    reference: str,
    categories: set[str],
) -> bool:
    """Verify a scenario source reference has one of the required source semantics."""

    if "#" not in reference:
        return False
    repo_id, anchor = reference.split("#", 1)
    repositories = discovery.get("inventory", {}).get("repositories", [])
    for repository in repositories if isinstance(repositories, list) else []:
        if not isinstance(repository, dict) or repository.get("id") != repo_id:
            continue
        root = _resolve(project_root, str(repository.get("root", "")))
        commit = str(repository.get("commit", ""))
        return bool(anchor) and any(_semantic_evidence(root, commit, anchor, category) for category in categories)
    return False


def _rpc_function_method(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    project_root: Path,
) -> str | None:
    """验证带源码锚点、超时和结果返回的只读 RPC 适配器。"""

    decorator = next(
        (item for item in function.decorator_list if isinstance(item, ast.Call) and _call_name(item).rsplit(".", 1)[-1] == "read_only_rpc"),
        None,
    )
    if decorator is None:
        return None
    method = _call_argument(decorator, "method", 0)
    source_ref = next((keyword.value for keyword in decorator.keywords if keyword.arg == "source_ref"), None)
    if not (
        isinstance(method, ast.Constant) and method.value in {"READ", "RPC"}
        and isinstance(source_ref, ast.Constant) and isinstance(source_ref.value, str)
        and _source_reference_exists(project_root, source_ref.value)
    ):
        return None
    calls = [call for statement in function.body for call in ast.walk(statement) if isinstance(call, ast.Call)]
    candidates = [
        call for call in calls
        if _call_name(call).rsplit(".", 1)[-1].casefold()
        not in {"record_endpoint", "str", "int", "bool", "dict", "list", "set", "tuple", "len"}
    ]
    if len(candidates) != 1:
        return None
    operation = candidates[0]
    leaf = _call_name(operation).rsplit(".", 1)[-1].casefold()
    if leaf in {"post", "put", "patch", "delete", "publish", "produce", "trigger", "execute", "executemany", "send", "create", "update", "save", "write", "set", "clear", "flush", "enqueue", "schedule", "commit"}:
        return None
    timeout = next((keyword.value for keyword in operation.keywords if keyword.arg == "timeout"), None)
    if not isinstance(timeout, ast.Constant) or not isinstance(timeout.value, (int, float)) or timeout.value <= 0:
        return None
    local_parents = {child: parent for parent in ast.walk(function) for child in ast.iter_child_nodes(parent)}
    return str(method.value) if _return_uses_call(function, operation, local_parents) else None


def _rpc_import_methods(tree: ast.Module, project_root: Path) -> dict[str, str]:
    """解析 tests/runtime 从公共适配器导入的可信只读 RPC 函数。"""

    methods: dict[str, str] = {}
    for statement in tree.body:
        if not isinstance(statement, ast.ImportFrom) or not statement.module or not statement.module.startswith(("common.clients.", "common.integrations.")):
            continue
        module_path = project_root.joinpath(*statement.module.split(".")).with_suffix(".py")
        try:
            module_tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
        except (OSError, UnicodeError, SyntaxError):
            continue
        functions = {
            item.name: item for item in module_tree.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for alias in statement.names:
            function = functions.get(alias.name)
            method = _rpc_function_method(function, project_root) if function is not None else None
            if method is not None:
                methods[alias.asname or alias.name] = method
    return methods


def _proven_smoke_method(call: ast.Call, rpc_methods: Mapping[str, str] | None = None) -> str | None:
    """返回可由已知直接 HTTP transport 静态证明的只读方法。"""

    name = _call_name(call)
    if rpc_methods and name in rpc_methods:
        return rpc_methods[name]
    if name in {"requests.get", "httpx.get"}:
        return "GET"
    if name in {"requests.head", "httpx.head"}:
        return "HEAD"
    if name in {"requests.request", "httpx.request"}:
        method = _call_argument(call, "method", 0)
        if isinstance(method, ast.Constant) and isinstance(method.value, str) and method.value.upper() in {"GET", "HEAD"}:
            return method.value.upper()
    if name == "urllib.request.urlopen":
        data = _call_argument(call, "data", 1)
        if data is not None and not (isinstance(data, ast.Constant) and data.value is None):
            return None
        request = _call_argument(call, "url", 0)
        if isinstance(request, ast.Call) and _call_name(request) in {"urllib.request.Request", "Request"}:
            request_data = _call_argument(request, "data", 1)
            if request_data is not None and not (isinstance(request_data, ast.Constant) and request_data.value is None):
                return None
            method = _call_argument(request, "method", 4)
            if method is not None:
                return method.value.upper() if isinstance(method, ast.Constant) and isinstance(method.value, str) and method.value.upper() in {"GET", "HEAD"} else None
        return "GET"
    return None


def _operation_method(call: ast.Call) -> str | None:
    """从常见 HTTP/RPC 调用形态提取真实方法。"""

    leaf = _call_name(call).rsplit(".", 1)[-1].casefold()
    direct = {"get": "GET", "head": "HEAD", "post": "POST", "put": "PUT", "patch": "PATCH", "delete": "DELETE", "read": "READ", "call": "RPC", "invoke": "RPC"}
    if leaf in direct:
        return direct[leaf]
    if leaf == "request":
        method = _call_argument(call, "method", 0)
        return method.value.upper() if isinstance(method, ast.Constant) and isinstance(method.value, str) else None
    if leaf == "urlopen":
        return _proven_smoke_method(call)
    return None


def _python_errors(path: Path, project_root: Path) -> list[str]:
    """用 AST 校验文档、导入期副作用、等待、SQL 和运行安全规则。"""

    errors: list[str] = []
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        return [_error(path, "python-parse", f"Python 无法解析: {exc}")]

    def diagnostic(rule: str, message: str, node: ast.AST) -> None:
        """追加带 AST 行号的静态诊断。"""

        errors.append(f"{path}:{getattr(node, 'lineno', 1)}: {rule}: {message}")

    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    relative = path.relative_to(project_root).as_posix()
    in_smoke = relative.startswith("tests/runtime/")
    in_scenario = relative.startswith("scenarios/")
    in_repository = relative.startswith("common/repositories/")
    in_control = relative.startswith("common/controls/")
    in_adapter = any(relative.startswith(prefix) for prefix in (
        "common/clients/", "common/integrations/", "common/repositories/", "common/controls/",
    ))
    is_gate_engine = relative == "scripts/e2e_guard.py"
    is_fixed_gate_asset = relative in {
        "common/e2e_runtime.py",
        "scripts/check_scenarios.py",
        "scripts/check_source_versions.py",
        "scripts/e2e_guard.py",
        "scripts/run_e2e.py",
        "tests/test_e2e_guard.py",
        "tests/test_e2e_runtime.py",
    }
    if is_fixed_gate_asset:
        return errors

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("pytest_"):
            diagnostic("pytest-hook-forbidden", "生成工程不得定义可改变收集、执行或退出状态的 pytest hook", node)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            forbidden = {
                target.id for target in targets
                if isinstance(target, ast.Name) and target.id in {"pytest_plugins", "collect_ignore", "collect_ignore_glob"}
            }
            if forbidden:
                diagnostic("pytest-hook-forbidden", f"生成工程不得声明 pytest 收集或插件入口: {sorted(forbidden)}", node)

    for node in [tree, *(item for item in ast.walk(tree) if isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)))]:
        doc = ast.get_docstring(node, clean=False)
        if not doc or not CHINESE_RE.search(doc):
            diagnostic("chinese-docstring", "模块、类和函数必须有简洁中文 docstring", node)

    import_aliases = _import_aliases(tree)

    if in_scenario:
        forbidden_marks = {
            "pytest.mark.skip", "pytest.mark.skipif", "pytest.mark.xfail",
            "unittest.skip", "unittest.skipIf", "unittest.skipUnless",
        }
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for decorator in node.decorator_list:
                    target = decorator.func if isinstance(decorator, ast.Call) else decorator
                    if _resolved_name(_expression_name(target), import_aliases) in forbidden_marks:
                        diagnostic("runtime-must-fail", "业务场景不得使用 skip、skipif 或 xfail 标记", decorator)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(target, ast.Name) and target.id == "pytestmark" for target in targets):
                    values = {
                        _resolved_name(_expression_name(child.func if isinstance(child, ast.Call) else child), import_aliases)
                        for child in ast.walk(node.value)
                        if isinstance(child, (ast.Call, ast.Attribute, ast.Name))
                    }
                    if values & forbidden_marks:
                        diagnostic("runtime-must-fail", "业务场景不得通过 pytestmark 跳过或预期失败", node)

    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            for target in targets:
                names = [
                    child.id if isinstance(child, ast.Name) else child.attr
                    for child in ast.walk(target)
                    if isinstance(child, (ast.Name, ast.Attribute))
                ]
                if any(_sensitive_name(name) for name in names) and isinstance(value, ast.Constant) and not _safe_credential_literal(value):
                    diagnostic("python-secret", "敏感变量只能保存精确占位符或类型化引用", node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            positional = [*node.args.posonlyargs, *node.args.args]
            defaults = [None] * (len(positional) - len(node.args.defaults)) + list(node.args.defaults)
            pairs = list(zip(positional, defaults)) + list(zip(node.args.kwonlyargs, node.args.kw_defaults))
            for argument, default in pairs:
                if default is not None and _sensitive_name(argument.arg) and isinstance(default, ast.Constant) and not _safe_credential_literal(default):
                    diagnostic("python-secret", "敏感参数默认值只能是精确占位符或类型化引用", default)
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg and _sensitive_name(keyword.arg) and isinstance(keyword.value, ast.Constant) and not _safe_credential_literal(keyword.value):
                    diagnostic("python-secret", "敏感调用参数只能是精确占位符或类型化引用", keyword.value)

    class ImportTimeCalls(ast.NodeVisitor):
        """Collect expressions executed while importing without entering callable bodies."""

        def __init__(self) -> None:
            self.calls: list[ast.Call] = []

        def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - ast visitor API
            self.calls.append(node)
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802 - ast visitor API
            for value in [*node.decorator_list, *node.args.defaults, *(item for item in node.args.kw_defaults if item is not None)]:
                self.visit(value)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802 - ast visitor API
            for value in [*node.args.defaults, *(item for item in node.args.kw_defaults if item is not None)]:
                self.visit(value)

    import_time = ImportTimeCalls()
    for statement in tree.body:
        import_time.visit(statement)
    allowed_import_calls = {
        "Path", "pathlib.Path", "dataclass", "dataclasses.dataclass", "contextmanager", "contextlib.contextmanager",
        "read_only_rpc", "pytest.fixture", "pytest.mark.business_e2e", "pytest.mark.scenario_id", "pytest.mark.read_only_smoke",
    }
    for node in import_time.calls:
        resolved = _resolved_call_name(node, import_aliases)
        if resolved in allowed_import_calls or resolved.endswith(".resolve"):
            continue
        diagnostic("import-time-io", "导入期函数调用默认禁止；只允许纯声明装饰器和 Path 定位", node)

    rpc_methods = _rpc_import_methods(tree, project_root) if in_smoke else {}
    if in_adapter and not is_fixed_gate_asset:
        for function in (item for item in tree.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))):
            if any(_call_name(item).rsplit(".", 1)[-1] == "read_only_rpc" for item in function.decorator_list):
                if _rpc_function_method(function, project_root) is None:
                    diagnostic("rpc-readonly-contract", "read_only_rpc 适配器必须绑定有效源码锚点、单次非写 RPC、正数 timeout 和真实返回值", function)
    if relative not in {"common/e2e_runtime.py", "scripts/e2e_guard.py", "tests/test_e2e_guard.py", "tests/test_e2e_runtime.py"} and any(
        name in source for name in ("E2E_EVIDENCE_DIR", "E2E_RUN_ID")
    ):
        errors.append(_error(path, "evidence-environment-internal", "业务代码不得读取证据目录或运行 ID 环境变量"))
    mutating_calls = {
        "post", "put", "patch", "delete", "publish", "produce", "trigger", "execute", "executemany", "send",
        "create", "update", "save", "write", "set", "clear", "flush", "enqueue", "schedule", "commit",
    }
    external_calls = mutating_calls | {"get", "head", "read", "request", "urlopen", "fetch", "query", "call", "invoke", "consume", "receive"}
    smoke_benign_calls = {
        "record_endpoint", "json", "raise_for_status", "len", "str", "int", "bool", "dict", "list", "set", "tuple",
        "isinstance", "getattr", "resolve", "Path",
    }
    control_sql_nodes: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _call_name(node)
            resolved_name = _resolved_call_name(node, import_aliases)
            leaf = resolved_name.rsplit(".", 1)[-1]
            if resolved_name in {"time.sleep", "asyncio.sleep"} or name in {"time.sleep", "sleep", "asyncio.sleep"}:
                diagnostic("blind-sleep", "禁止盲等；使用单调时钟和有限轮询", node)
            if name in {
                "requests.get", "requests.head", "requests.post", "requests.put", "requests.patch", "requests.delete", "requests.request",
                "httpx.get", "httpx.head", "httpx.post", "httpx.put", "httpx.patch", "httpx.delete", "httpx.request",
                "urllib.request.urlopen",
            }:
                timeout = next((keyword.value for keyword in node.keywords if keyword.arg == "timeout"), None)
                if timeout is None and name == "urllib.request.urlopen" and len(node.args) > 2:
                    timeout = node.args[2]
                if timeout is None or not isinstance(timeout, ast.Constant) or not isinstance(timeout.value, (int, float)) or timeout.value <= 0:
                    diagnostic("transport-timeout", "直接 HTTP transport 必须声明正数 timeout", node)
            if in_smoke and leaf.casefold() in mutating_calls:
                diagnostic("smoke-side-effect", f"只读冒烟不得调用潜在写操作: {name}", node)
            if in_smoke and leaf.casefold() == "request":
                method_node = node.args[0] if node.args else next(
                    (keyword.value for keyword in node.keywords if keyword.arg == "method"), None
                )
                method = method_node.value.upper() if isinstance(method_node, ast.Constant) and isinstance(method_node.value, str) else None
                if method not in {"GET", "HEAD"}:
                    diagnostic("smoke-request-method", "通用 request/send 调用必须静态证明为 GET 或 HEAD", node)
            if in_smoke and (leaf.casefold() in {"get", "head", "read", "request", "urlopen"} or name in rpc_methods) and _proven_smoke_method(node, rpc_methods) is None:
                diagnostic("smoke-transport-unproven", f"冒烟调用必须直接使用可静态识别的只读 HTTP transport: {name}", node)
            if (
                in_smoke
                and leaf.casefold() not in external_calls
                and name not in rpc_methods
                and leaf not in smoke_benign_calls
                and not name.startswith("pytest.mark.")
                and name != "pytest.fixture"
            ):
                diagnostic("smoke-call-unproven", f"只读冒烟不得调用无法静态证明安全的辅助函数: {name}", node)
            skip_calls = {
                "pytest.skip", "pytest.xfail", "pytest.importorskip", "unittest.SkipTest",
            }
            if in_scenario and resolved_name in skip_calls:
                diagnostic("runtime-must-fail", "业务场景不得 skip、xfail 或 importorskip", node)
            if not is_gate_engine and relative != "common/e2e_runtime.py" and leaf in {"_emit_event", "_record_restoration"}:
                diagnostic("evidence-internal", "业务代码不得直接写运行证据事件", node)
            if leaf in {"record_control", "record_endpoint"} and not is_fixed_gate_asset:
                if in_scenario:
                    diagnostic("evidence-adapter-owned", "场景代码不得直接生成控制或接口证据；证据必须由公共适配器记录", node)
                elif not in_smoke and not in_adapter:
                    diagnostic("evidence-adapter-owned", "控制和业务接口证据只能由公共客户端、集成、仓库或控制适配器记录", node)
            if not in_adapter and not is_fixed_gate_asset and isinstance(node.func, ast.Attribute):
                direct_write_sinks = {
                    "post", "put", "patch", "delete", "publish", "produce", "trigger", "execute", "executemany",
                    "send", "sendall", "create", "update", "save", "write", "write_text", "write_bytes", "clear",
                    "flush", "enqueue", "schedule", "commit", "unlink", "remove", "rename", "replace", "mkdir", "rmdir", "touch",
                }
                if leaf.casefold() in direct_write_sinks or resolved_name in {"subprocess.run", "subprocess.Popen", "os.system", "os.popen"}:
                    diagnostic("side-effect-adapter-required", "场景、fixture 和普通模块不得直接执行写操作；必须调用受门禁的公共适配器", node)
            if not in_adapter and not is_fixed_gate_asset and resolved_name in {"open", "builtins.open"}:
                mode = _call_argument(node, "mode", 1)
                if mode is not None and not (
                    isinstance(mode, ast.Constant)
                    and isinstance(mode.value, str)
                    and not set(mode.value).intersection("wax+")
                ):
                    diagnostic("side-effect-adapter-required", "直接文件写入必须封装在受门禁适配器中", node)

            if leaf.casefold() in {"execute", "executemany"} and not is_gate_engine:
                function = _enclosing_function(node, parents)
                sql = _literal_text(_call_argument(node, "operation", 0), function or tree, node.lineno)
                if in_repository:
                    if sql is None or re.match(r"^\s*(?:SELECT|WITH)\b", sql, re.IGNORECASE) is None or MUTATING_SQL_RE.search(sql):
                        diagnostic("observation-sql-unproven", "观察仓库 execute 必须使用可静态解析的只读 SQL", node)
                elif in_control:
                    if sql is None or not MUTATING_SQL_RE.search(sql):
                        diagnostic("control-sql-unproven", "控制 execute 必须使用可静态解析的受限变更 SQL", node)
                else:
                    diagnostic("sql-placement", "execute/executemany 只能位于只读仓库或受控数据库模块", node)

        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        text = node.value
        if _contains_usable_credential_text(text):
            diagnostic("python-secret", "Python 源码不得包含可用凭据文本", node)
        if URL_RE.search(text) and (relative.startswith("common/") or relative.startswith("scripts/")):
            diagnostic("hardcoded-endpoint", "公共代码和脚本不得硬编码端点", node)
        if not SQL_RE.search(text):
            continue
        if is_gate_engine:
            continue
        if in_scenario or in_smoke:
            diagnostic("sql-placement", "场景和冒烟测试不得内嵌 SQL", node)
            continue
        if not (in_repository or in_control):
            diagnostic("sql-placement", "SQL 只能位于只读仓库或受控数据库模块", node)
            continue
        if not PARAMETER_RE.search(text):
            diagnostic("sql-parameterized", "SQL 必须使用驱动参数占位符", node)
        if in_repository and MUTATING_SQL_RE.search(text):
            diagnostic("observation-sql-readonly", "观察 SQL 必须只读", node)
        if in_control:
            control_sql_nodes.append(node)
            if not MUTATING_SQL_RE.search(text):
                diagnostic("control-sql-mutation", "控制模块中的 SQL 必须是明确的受控变更", node)
            upper = text.upper()
            if any(word in upper for word in ("UPDATE", "DELETE", "MERGE")) and "WHERE" not in upper:
                diagnostic("control-sql-bounded", "控制 SQL 必须有精确 WHERE 条件", node)
            if "WHERE" in upper and not PARAMETER_RE.search(text[upper.index("WHERE"):]):
                diagnostic("control-sql-selector", "控制 SQL 的 WHERE 条件必须由参数绑定精确定位", node)
            if re.search(r"\b(ALTER|DROP|TRUNCATE|REPLACE)\b", text, re.IGNORECASE):
                diagnostic("control-sql-destructive", "控制 SQL 禁止 DDL、TRUNCATE 和 REPLACE", node)
            if re.search(r"\bLIKE\b|\bIN\s*\(\s*SELECT\b", text, re.IGNORECASE):
                diagnostic("control-sql-fuzzy", "控制 SQL 禁止模糊或子查询批量选择", node)
            if not _exact_sql_where(text):
                diagnostic("control-sql-exact-where", "控制 SQL 的 WHERE 只能由 AND 连接的等值参数谓词组成", node)

    for node in ast.walk(tree):
        if is_gate_engine:
            break
        if isinstance(node, ast.JoinedStr):
            literal = " ".join(item.value for item in node.values if isinstance(item, ast.Constant) and isinstance(item.value, str))
            if SQL_RE.search(literal):
                diagnostic("sql-string-built", "SQL 禁止通过格式化字符串拼接", node)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            literal = " ".join(item.value for item in ast.walk(node) if isinstance(item, ast.Constant) and isinstance(item.value, str))
            if SQL_RE.search(literal):
                diagnostic("sql-string-built", "SQL 禁止通过字符串加法拼接", node)

    evidence_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _resolved_call_name(node, import_aliases).rsplit(".", 1)[-1] in {"record_control", "record_endpoint"}
    ]
    for evidence_call in evidence_calls:
        if is_fixed_gate_asset or (not in_smoke and not in_adapter):
            continue
        function = _enclosing_function(evidence_call, parents)
        if function is None:
            diagnostic("evidence-causal-call", "运行证据必须在执行真实操作的函数内记录", evidence_call)
            continue
        prior_operations = [
            call for call in ast.walk(function)
            if isinstance(call, ast.Call)
            and call.lineno < evidence_call.lineno
            and (
                _call_name(call).rsplit(".", 1)[-1].casefold() in external_calls
                or _call_name(call) in rpc_methods
            )
            and _straight_line_precedes(call, evidence_call, function, parents)
        ]
        leaf = _resolved_call_name(evidence_call, import_aliases).rsplit(".", 1)[-1]
        if leaf == "record_endpoint":
            scenario_argument = _call_argument(evidence_call, "scenario", 0)
            phase = _call_argument(evidence_call, "phase", 1)
            method = _call_argument(evidence_call, "method", 2)
            status = _call_argument(evidence_call, "status", 4)
            summary = _call_argument(evidence_call, "summary", 5)
            verified = _call_argument(evidence_call, "verified", 6)
            phase_text = phase.value if isinstance(phase, ast.Constant) and isinstance(phase.value, str) else None
            method_text = method.value.upper() if isinstance(method, ast.Constant) and isinstance(method.value, str) else None
            if phase_text not in {"smoke", "business"} or method_text is None:
                diagnostic("endpoint-evidence-literal", "接口证据的 phase 和 method 必须是可静态核验的字面量", evidence_call)
            if in_smoke and not (
                isinstance(scenario_argument, ast.Constant)
                and isinstance(scenario_argument.value, str)
                and scenario_argument.value.strip()
            ):
                diagnostic("smoke-scenario-literal", "冒烟证据必须使用精确场景目录名字符串", evidence_call)
            if not prior_operations:
                diagnostic(
                    "smoke-evidence-causal" if in_smoke else "endpoint-evidence-causal",
                    "接口证据前必须在同一函数中直接执行真实传输调用",
                    evidence_call,
                )
                continue
            operation = max(prior_operations, key=lambda item: item.lineno)
            operation_leaf = _call_name(operation).rsplit(".", 1)[-1].casefold()
            if in_smoke:
                expected_method = _proven_smoke_method(operation, rpc_methods)
                if expected_method != method_text:
                    diagnostic("smoke-evidence-causal", "冒烟证据方法必须匹配同一函数中紧邻的 GET、HEAD 或只读 RPC 调用", evidence_call)
            else:
                expected_method = _operation_method(operation)
                if expected_method is None or expected_method != method_text:
                    diagnostic("endpoint-evidence-method", "业务接口证据方法必须匹配同一函数中的真实 HTTP/RPC 操作", evidence_call)
            result_names = _assigned_names(operation, parents)
            position = (evidence_call.lineno, evidence_call.col_offset)
            resolved_status = _resolved_expression(status, function, position, parents)
            resolved_summary = _resolved_expression(summary, function, position, parents)
            resolved_verified = _resolved_expression(verified, function, position, parents)
            if (
                status is None or summary is None or verified is None
                or resolved_verified is None
                or _constant_or_tautology(resolved_verified, function, parents)
                or not _expression_uses_names(resolved_status, result_names)
                or not _expression_uses_names(resolved_summary, result_names)
                or not _expression_uses_names(resolved_verified, result_names)
            ):
                diagnostic("endpoint-evidence-result", "status、summary 和 verified 必须由前一真实调用的返回值生成，不得使用常量", evidence_call)
        else:
            correlation = _call_argument(evidence_call, "correlation_ref", 3)
            kind = _call_argument(evidence_call, "kind", 1)
            action = _call_argument(evidence_call, "action", 2)
            side_effect = _call_argument(evidence_call, "side_effect", 4)
            kind_text = kind.value if isinstance(kind, ast.Constant) and isinstance(kind.value, str) else None
            allowed_operations = {
                "public_api": external_calls,
                "test_or_admin_api": mutating_calls,
                "mocks_and_faults": mutating_calls,
                "dynamic_configuration": mutating_calls,
                "scheduled_jobs": {"post", "put", "trigger", "invoke"},
                "messages": {"post", "publish", "produce", "send"},
                "database_read": {"execute", "fetch", "query", "get", "read"},
                "database_control": {"execute", "executemany"},
                "observability": {"execute", "fetch", "query", "get", "head", "read", "consume", "receive"},
            }.get(kind_text, set())
            matching_operations = [
                operation for operation in prior_operations
                if _call_name(operation).rsplit(".", 1)[-1].casefold() in allowed_operations
            ]
            if kind_text not in CONTROL_NAMES or not matching_operations:
                diagnostic("control-evidence-operation", "控制证据类别必须匹配同一函数中先前的真实操作类型", evidence_call)
            if not isinstance(action, ast.Constant) or not isinstance(action.value, str) or not action.value.strip():
                diagnostic("control-evidence-literal", "控制证据 action 必须是非空字面量以绑定场景契约", evidence_call)
            actual_side_effect = "write" if any(
                _call_name(operation).rsplit(".", 1)[-1].casefold() in mutating_calls
                for operation in matching_operations
            ) else "read"
            if not isinstance(side_effect, ast.Constant) or side_effect.value != actual_side_effect:
                diagnostic("control-evidence-side-effect", f"控制证据 side_effect 必须与真实操作一致: {actual_side_effect}", evidence_call)
            if correlation is None or isinstance(correlation, ast.Constant):
                diagnostic("control-evidence-correlation", "控制证据关联键必须是传给真实操作的运行值，不得使用常量", evidence_call)
            elif not any(_operation_binds_identity(operation, correlation) for operation in matching_operations):
                diagnostic("control-evidence-causal", "控制证据关联键必须传入同一函数中先前的真实控制操作", evidence_call)

    if in_adapter and not is_fixed_gate_asset and not in_control:
        for operation in [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and _call_name(node).rsplit(".", 1)[-1].casefold() in mutating_calls
            and (
                _call_name(node).rsplit(".", 1)[-1].casefold() not in {"execute", "executemany"}
                or (
                    (function := _enclosing_function(node, parents)) is not None
                    and (sql := _literal_text(_call_argument(node, "operation", 0), function, node.lineno)) is not None
                    and MUTATING_SQL_RE.search(sql)
                )
            )
        ]:
            function = _enclosing_function(operation, parents)
            bound = any(
                _enclosing_function(evidence_call, parents) is function
                and evidence_call.lineno > operation.lineno
                and _straight_line_precedes(operation, evidence_call, function, parents)
                and _resolved_call_name(evidence_call, import_aliases).rsplit(".", 1)[-1] == "record_control"
                and (correlation := _call_argument(evidence_call, "correlation_ref", 3)) is not None
                and not isinstance(correlation, ast.Constant)
                and _operation_binds_identity(operation, correlation)
                for evidence_call in evidence_calls
            )
            if not bound:
                diagnostic("mutation-isolation-binding", "适配器写操作必须用传入该操作的运行关联键记录控制证据", operation)

    controlled_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _resolved_call_name(node, import_aliases).rsplit(".", 1)[-1] == "controlled_database_state"
    ]
    callback_roles: list[tuple[str, str]] = []
    for call in controlled_calls:
        for role in ("snapshot", "mutate", "restore", "verify_restored"):
            value = next((keyword.value for keyword in call.keywords if keyword.arg == role), None)
            if not isinstance(value, ast.Name):
                diagnostic("control-sql-callback", f"{role} 必须是当前模块命名回调，禁止 lambda 或动态回调", value or call)
            else:
                callback_roles.append((value.id, role))
    function_map = {
        node.name: node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    reused_database_callbacks = {
        callback for callback, _ in callback_roles
        if sum(name == callback for name, _ in callback_roles) > 1
    }
    for callback in sorted(reused_database_callbacks):
        diagnostic("control-sql-callback-role", f"数据库控制的四个角色必须使用不同回调: {callback}", function_map.get(callback) or tree)
    for callback, role in callback_roles:
        function = function_map.get(callback)
        expected_args = ["selector_ref"] if role in {"snapshot", "mutate"} else ["original", "selector_ref"]
        if function is None or [argument.arg for argument in function.args.args] != expected_args:
            diagnostic("control-sql-callback", f"{role} 回调必须是当前模块中参数为 {expected_args} 的命名函数", function or tree)
            continue
        if role in {"mutate", "restore"}:
            executions = [
                inner_call for inner_call in ast.walk(function)
                if isinstance(inner_call, ast.Call)
                and _call_name(inner_call).rsplit(".", 1)[-1] in {"execute", "executemany"}
            ]
            if (
                len(executions) != 1
                or not all(_sql_where_binds_selector(inner_call, function) for inner_call in executions)
                or role == "restore" and not all(_sql_set_binds_original(inner_call, function) for inner_call in executions)
                or not any(_return_uses_call(function, inner_call, parents) for inner_call in executions)
            ):
                diagnostic(
                    "control-sql-selector-binding",
                    f"{role} SQL 必须把 selector_ref 绑定到精确 WHERE，且返回值由实际执行结果派生",
                    function,
                )
        else:
            read_operations = {"get", "read", "fetch", "fetchone", "fetchall", "query", "load", "select", "find"}
            observer_calls = [
                inner_call for inner_call in ast.walk(function)
                if isinstance(inner_call, ast.Call)
                and _call_name(inner_call).rsplit(".", 1)[-1].casefold() in read_operations
                and _contains_expression(inner_call, ast.Name(id="selector_ref", ctx=ast.Load()))
                and _direct_statement(inner_call, function, parents) is not None
            ]
            if role == "snapshot":
                valid_observer = any(_return_uses_call(function, inner_call, parents) for inner_call in observer_calls)
            else:
                valid_observer = False
                for inner_call in observer_calls:
                    result_names = _assigned_names(inner_call, parents)
                    for return_node in function.body:
                        if not isinstance(return_node, ast.Return) or return_node.value is None:
                            continue
                        if (
                            result_names
                            and _expression_uses_names(return_node.value, result_names)
                            and _contains_expression(return_node.value, ast.Name(id="original", ctx=ast.Load()))
                            and not _constant_or_tautology(return_node.value, function, parents)
                        ):
                            valid_observer = True
            if not valid_observer:
                diagnostic(
                    "control-sql-observer-binding",
                    f"{role} 必须使用 selector_ref 执行真实只读观察；恢复校验还必须比较观察结果与 original",
                    function,
                )
    database_callbacks = {name for name, _ in callback_roles}
    for call in controlled_calls:
        current: ast.AST | None = parents.get(call)
        while current is not None and not isinstance(current, (ast.With, ast.AsyncWith)):
            current = parents.get(current)
        if not isinstance(current, ast.With) or not any(item.context_expr is call for item in current.items):
            diagnostic("control-sql-business-body", "controlled_database_state 必须直接作为 with 上下文包围业务触发、观察和断言", call)
            continue
        business_calls = [
            child for statement in current.body for child in ast.walk(statement)
            if isinstance(child, ast.Call)
            and _resolved_call_name(child, import_aliases).rsplit(".", 1)[-1]
            not in database_callbacks | {
                "record_control", "record_endpoint", "record_business_entry", "print", "str", "int", "bool",
                "dict", "list", "set", "tuple", "len", "isinstance", "getattr",
            }
        ]
        result_names = {name for business_call in business_calls for name in _assigned_names(business_call, parents)}
        assertions = [child for statement in current.body for child in ast.walk(statement) if isinstance(child, ast.Assert)]
        if not business_calls or not result_names or not any(
            _expression_uses_names(assertion.test, result_names)
            and not _constant_or_tautology(assertion.test, _enclosing_function(assertion, parents) or tree, parents)
            for assertion in assertions
        ):
            diagnostic("control-sql-business-body", "控制 SQL 变更后必须在 with 正文触发或等待业务逻辑，并基于真实观察结果断言", current)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {name for name, _ in callback_roles}:
            diagnostic("control-sql-direct-call", f"控制 SQL 回调只能交给 controlled_database_state，不得直接调用: {node.func.id}", node)
    for sql_node in control_sql_nodes:
        current = parents.get(sql_node)
        enclosing_name: str | None = None
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                enclosing_name = current.name
                break
            current = parents.get(current)
        if enclosing_name not in {name for name, _ in callback_roles}:
            diagnostic("control-sql-guard", "每条控制 SQL 必须位于 controlled_database_state 的 mutate/restore 回调内", sql_node)

    restoration_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _resolved_call_name(node, import_aliases).rsplit(".", 1)[-1] == "restoration_guard"
    ]
    restoration_roles: list[tuple[str, str]] = []
    for call in restoration_calls:
        for role in ("restore", "verify"):
            callback = next((keyword.value for keyword in call.keywords if keyword.arg == role), None)
            if not isinstance(callback, ast.Name):
                diagnostic("restoration-callback", f"{role} 必须是当前模块的命名函数，禁止 lambda 或动态回调", callback or call)
                continue
            restoration_roles.append((callback.id, role))
    reused_restoration_callbacks = {
        callback for callback, _ in restoration_roles
        if sum(name == callback for name, _ in restoration_roles) > 1
    }
    for callback in sorted(reused_restoration_callbacks):
        diagnostic("restoration-callback-role", f"restore 与 verify 必须使用不同回调: {callback}", function_map.get(callback) or tree)
    for callback, role in restoration_roles:
        function = function_map.get(callback)
        if function is None or [argument.arg for argument in function.args.args] != ["resource_ref"]:
            diagnostic("restoration-callback", f"{role} 回调必须是当前模块中参数恰为 resource_ref 的命名函数", function or tree)
            continue
        operations = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
        resource = ast.Name(id="resource_ref", ctx=ast.Load())
        if role == "restore":
            real = [
                call for call in operations
                if _call_name(call).rsplit(".", 1)[-1].casefold() in mutating_calls
                and _operation_binds_identity(call, resource)
                and _direct_statement(call, function, parents) is not None
            ]
            if not real or not any(_return_uses_call(function, call, parents) for call in real):
                diagnostic("restoration-operation", "restore 必须执行使用 resource_ref 的真实恢复写操作", function)
        else:
            valid_return = any(
                isinstance(return_node, ast.Return)
                and return_node.value is not None
                and not isinstance(return_node.value, ast.Constant)
                and any(
                    isinstance(call, ast.Call)
                    and _call_name(call).rsplit(".", 1)[-1].casefold() in external_calls - mutating_calls
                    and _operation_binds_identity(call, resource)
                    and _direct_statement(call, function, parents) is not None
                    for call in ast.walk(return_node.value)
                )
                for return_node in ast.walk(function)
            )
            if not valid_return:
                diagnostic("restoration-verification", "verify 必须返回使用 resource_ref 的真实观察结果，不得返回常量", function)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {name for name, _ in restoration_roles}:
            diagnostic("restoration-direct-call", f"恢复回调只能交给 restoration_guard，不得直接调用: {node.func.id}", node)

    if in_smoke:
        for function in (node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))):
            decorator_names = {
                _resolved_name(_expression_name(item.func if isinstance(item, ast.Call) else item), import_aliases)
                for item in function.decorator_list
            }
            if "pytest.fixture" in decorator_names:
                diagnostic("smoke-fixture-forbidden", "只读冒烟不得定义 fixture", function)
            if function.name.startswith("test_") and (
                function.args.posonlyargs or function.args.args or function.args.kwonlyargs or function.args.vararg or function.args.kwarg
            ):
                diagnostic("smoke-fixture-forbidden", "只读冒烟测试不得依赖 fixture 参数", function)
        declared_scenarios = {
            str(argument.value)
            for call in evidence_calls if _resolved_call_name(call, import_aliases).rsplit(".", 1)[-1] == "record_endpoint"
            if (argument := _call_argument(call, "scenario", 0)) is not None
            and isinstance(argument, ast.Constant) and isinstance(argument.value, str)
        }
        if len(declared_scenarios) != 1:
            errors.append(_error(path, "smoke-scenario-scope", "每个只读冒烟模块必须只绑定一个场景目录名", "record_endpoint"))
        if source.count("read_only_smoke") != 1:
            errors.append(_error(path, "smoke-marker", "冒烟测试文件必须有且只有一个 read_only_smoke marker", "read_only_smoke"))
        runtime_call_leaves = {
            _resolved_call_name(call, import_aliases).rsplit(".", 1)[-1]
            for call in ast.walk(tree) if isinstance(call, ast.Call)
        }
        if runtime_call_leaves & {"controlled_database_state", "record_business_entry"}:
            errors.append(_error(path, "smoke-control", "只读冒烟不得引用控制 SQL 或业务执行入口"))
        if not any(_resolved_call_name(call, import_aliases).rsplit(".", 1)[-1] == "record_endpoint" for call in evidence_calls):
            errors.append(_error(path, "smoke-evidence", "冒烟测试必须记录脱敏接口调用结果", "def test_"))
    if in_scenario and path.name.startswith("test_"):
        scenario_name = Path(relative).parts[1] if len(Path(relative).parts) > 1 else ""
        local_functions = {
            node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("test_")
        }
        business_functions = [
            node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any("business_e2e" in ast.unparse(decorator) for decorator in node.decorator_list)
        ]
        for function in business_functions:
            calls = [
                node for statement in function.body for node in ast.walk(statement)
                if isinstance(node, ast.Call)
            ]
            names = [_resolved_call_name(node, import_aliases).rsplit(".", 1)[-1] for node in calls]
            if "preflight" not in names:
                diagnostic("business-preflight", "业务测试必须在运行操作前调用 preflight", function)
            if "record_business_entry" not in names:
                diagnostic("business-entry-evidence", "业务测试必须在首个真实业务动作前记录进入事件", function)
            for call in calls:
                leaf = _resolved_call_name(call, import_aliases).rsplit(".", 1)[-1]
                if leaf not in {"preflight", "record_business_entry"}:
                    continue
                argument = _call_argument(
                    call,
                    "scenario_name" if leaf == "preflight" else "scenario",
                    1 if leaf == "preflight" else 0,
                )
                if not (
                    isinstance(argument, ast.Constant)
                    and isinstance(argument.value, str)
                    and argument.value == scenario_name
                ):
                    diagnostic("scenario-runtime-identity", f"{leaf} 必须使用当前场景目录名字面量: {scenario_name}", call)
            if "preflight" in names and "record_business_entry" in names:
                preflight_position = min((node.lineno, node.col_offset) for node in calls if _resolved_call_name(node, import_aliases).rsplit(".", 1)[-1] == "preflight")
                entry_position = min((node.lineno, node.col_offset) for node in calls if _resolved_call_name(node, import_aliases).rsplit(".", 1)[-1] == "record_business_entry")
                if preflight_position >= entry_position:
                    diagnostic("preflight-order", "preflight 必须先于真实业务进入事件", function)
                benign_before_entry = {
                    "preflight", "record_business_entry", "Path", "resolve", "str", "int", "bool", "dict", "list", "set", "tuple",
                }
                for call in calls:
                    leaf = _resolved_call_name(call, import_aliases).rsplit(".", 1)[-1]
                    if (call.lineno, call.col_offset) >= entry_position or leaf in benign_before_entry:
                        continue
                    current: ast.AST | None = call
                    nested_in_preflight = False
                    while current is not None and current is not function:
                        if isinstance(current, ast.Call) and _resolved_call_name(current, import_aliases).rsplit(".", 1)[-1] == "preflight":
                            nested_in_preflight = True
                            break
                        current = parents.get(current)
                    if not nested_in_preflight:
                        diagnostic("business-entry-order", "record_business_entry 前不得执行准备、写入或业务调用", call)
            ignored = {
                "preflight", "record_business_entry", "record_endpoint", "record_control", "Path",
                "resolve", "restoration_guard", "controlled_database_state", "len", "print", "str", "dict", "list", "set", "tuple",
            }
            action_names = [name for name in names if name not in ignored and name not in local_functions]
            if not action_names:
                diagnostic("business-action-required", "业务测试必须调用至少一个从场景/公共适配器导入的真实动作或观察器", function)
            assertions = [node for node in ast.walk(function) if isinstance(node, ast.Assert)]
            action_calls = [
                call for call in calls
                if _resolved_call_name(call, import_aliases).rsplit(".", 1)[-1] not in ignored
                and _resolved_call_name(call, import_aliases).rsplit(".", 1)[-1] not in local_functions
            ]
            action_results = {name for call in action_calls for name in _assigned_names(call, parents)}
            valid_assertion = any(
                not _constant_or_tautology(assertion.test, function, parents)
                and (
                    _expression_uses_names(assertion.test, action_results)
                    or any(call in set(ast.walk(assertion.test)) for call in action_calls)
                )
                for assertion in assertions
            )
            if not valid_assertion:
                diagnostic("business-assertion-required", "业务测试必须包含至少一个基于运行结果的非常量断言", function)
            guaranteed = any(
                isinstance(node, ast.Try) and node.finalbody
                and any(
                    isinstance(child, ast.Call)
                    and _resolved_call_name(child, import_aliases).rsplit(".", 1)[-1] not in ignored
                    and _resolved_call_name(child, import_aliases).rsplit(".", 1)[-1] not in local_functions
                    for statement in node.finalbody for child in ast.walk(statement)
                )
                for node in ast.walk(function)
            ) or any(name in {"addfinalizer", "restoration_guard", "controlled_database_state"} for name in names)
            if not guaranteed:
                diagnostic("cleanup-guaranteed", "场景必须通过非空 finally、finalizer 或受控上下文保证清理", function)
    for mapping in (node for node in ast.walk(tree) if isinstance(node, ast.Dict)):
        for key, value in zip(mapping.keys, mapping.values):
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                continue
            normalized = re.sub(r"[^a-z0-9]", "", key.value.casefold())
            if not any(fragment in normalized for fragment in ("password", "secret", "token", "authorization", "apikey", "accesskey", "cookie", "privatekey", "clientsecret", "credential")):
                continue
            if isinstance(value, ast.Constant) and isinstance(value.value, str) and not (
                PLACEHOLDER_RE.fullmatch(value.value) or REFERENCE_RE.fullmatch(value.value)
            ):
                diagnostic("python-secret", f"敏感键 {key.value} 只能保存占位符或类型化引用", value)
    return errors


def _yaml_comment_errors(path: Path) -> list[str]:
    """校验生成 YAML 的用途头和顶层块说明注释。"""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return [_error(path, "yaml-comments", f"无法读取 YAML: {exc}")]
    first = next((line.strip() for line in lines if line.strip()), "")
    errors: list[str] = []
    if not first.startswith("#") or not CHINESE_RE.search(first):
        errors.append(_error(path, "yaml-purpose-comment", "首个非空行必须是中文用途或敏感值说明"))
    for index, line in enumerate(lines):
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_-]*:\s*(?:#.*)?$", line):
            continue
        previous = index - 1
        while previous >= 0 and not lines[previous].strip():
            previous -= 1
        if previous < 0 or not lines[previous].lstrip().startswith("#"):
            errors.append(f"{path}:{index + 1}: yaml-block-comment: 顶层块前必须有说明注释")
    return errors


def _diagram_errors(project_root: Path) -> list[str]:
    """校验所有生成图都使用业务或系统交互粒度的 Mermaid 流程表达。"""

    errors: list[str] = []
    for path in _walk_files(project_root):
        if path.suffix.casefold() != ".md":
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            errors.append(_error(path, "diagram-read", f"无法读取图形文档: {exc}"))
            continue
        if "图" not in path.stem and "```mermaid" not in content.casefold():
            continue
        blocks = re.findall(r"```mermaid\s*(.*?)```", content, re.IGNORECASE | re.DOTALL)
        if not blocks:
            errors.append(_error(path, "diagram-mermaid", "生成图形必须使用 Mermaid 流程图或序列图", "```mermaid"))
            continue
        kinds: list[str] = []
        for block in blocks:
            lines = [line.strip() for line in block.splitlines() if line.strip() and not line.lstrip().startswith("%%")]
            kind = lines[0].split(maxsplit=1)[0] if lines else ""
            kinds.append(kind)
            if kind not in {"sequenceDiagram", "flowchart"}:
                errors.append(_error(path, "diagram-type", "生成图形只允许业务序列图或高层流程/服务交互图", kind or "```mermaid"))
        if path.name == "业务流程图.md" and kinds != ["sequenceDiagram"]:
            errors.append(_error(path, "business-diagram", "业务流程图必须且只能使用一个 Mermaid sequenceDiagram", "```mermaid"))
        if re.search(r"\b(pytest|fixture|preflight|assert_|test_|def|class)\b|\.py\b|::", content, re.IGNORECASE):
            errors.append(_error(path, "diagram-scope", "图形只能表达业务阶段或系统交互，不得展开测试、函数或代码级细节"))
        if re.search(r"\belse\b", content) and not re.search(r"\balt\b", content):
            errors.append(_error(path, "business-diagram-branch", "else 必须对应源码确认的 alt 分支", "else"))
    return errors


def _launcher_errors(project_root: Path) -> list[str]:
    """校验统一启动器默认运行全部并允许显式选择场景。"""

    errors: list[str] = []
    scripts = project_root / "scripts"
    for obsolete in sorted([*scripts.glob("run_*.sh"), *scripts.glob("run_*.bat")]) if scripts.is_dir() else []:
        errors.append(_error(obsolete, "scenario-launcher-forbidden", "禁止逐场景或 run_all 启动脚本；统一使用 run.sh/run.bat --scenario 场景名"))
    for suffix in (".sh", ".bat"):
        path = scripts / f"run{suffix}"
        if not path.is_file():
            errors.append(_error(path, "launcher-required", "缺少统一运行启动器"))
            continue
        data = path.read_bytes()
        if suffix == ".sh":
            text = data.decode("utf-8", errors="replace")
            for token in ("set -eu", "SCRIPT_DIR=", "exec python", '"$@"', "Usage:", "--scenario"):
                if token not in text:
                    errors.append(_error(path, "shell-launcher", f"Shell 启动器缺少: {token}"))
            if b"\r\n" in data:
                errors.append(_error(path, "shell-line-ending", "Shell 启动器必须使用 LF"))
            if os.name != "nt" and path.stat().st_mode & 0o111 == 0:
                errors.append(_error(path, "shell-executable", "Shell 启动器必须具有可执行位"))
        else:
            try:
                text = data.decode("ascii")
            except UnicodeDecodeError:
                errors.append(_error(path, "bat-ascii", "Bat 启动器正文必须是 ASCII"))
                continue
            for token in ("setlocal", '"%SCRIPT_DIR%run_e2e.py"', "%*", "ERRORLEVEL", "exit /b", "Usage:", "--scenario"):
                if token not in text:
                    errors.append(_error(path, "bat-launcher", f"Bat 启动器缺少: {token}"))
    return errors


def _project_errors(project_root: Path) -> list[str]:
    """校验目标工程声明运行门禁所需依赖和 pytest 标记。"""

    path = project_root / "pyproject.toml"
    if not path.is_file():
        return [_error(path, "pyproject-required", "生成工程必须包含 pyproject.toml")]
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        return [_error(path, "pyproject-parse", f"pyproject.toml 无法解析: {exc}")]
    declared: set[str] = set()

    def dependency_name(value: str) -> str:
        """从标准依赖说明中提取规范化包名。"""

        return re.split(r"[<>=!~\[ ;]", value.casefold(), 1)[0].replace("_", "-")

    project = document.get("project", {})
    if isinstance(project, dict):
        for item in project.get("dependencies", []) if isinstance(project.get("dependencies"), list) else []:
            if isinstance(item, str):
                declared.add(dependency_name(item))
        optional = project.get("optional-dependencies", {})
        if isinstance(optional, dict):
            for group in optional.values():
                for item in group if isinstance(group, list) else []:
                    if isinstance(item, str):
                        declared.add(dependency_name(item))

    def collect_named_dependencies(value: Any, parent: str = "") -> None:
        """兼容收集 Poetry 和 dependency-groups 的依赖名。"""

        if isinstance(value, dict):
            if parent in {"dependencies", "dev-dependencies"}:
                declared.update(str(key).casefold().replace("_", "-") for key in value if str(key).casefold() != "python")
            for key, child in value.items():
                collect_named_dependencies(child, str(key))
        elif isinstance(value, list) and parent in {"dependency-groups", "dev-dependencies"}:
            for item in value:
                if isinstance(item, str):
                    declared.add(dependency_name(item))

    collect_named_dependencies(document.get("tool", {}))
    groups = document.get("dependency-groups", {})
    if isinstance(groups, dict):
        for group in groups.values():
            for item in group if isinstance(group, list) else []:
                if isinstance(item, str):
                    declared.add(dependency_name(item))
    errors: list[str] = []
    for dependency in ("pytest", "pyyaml"):
        if dependency not in declared:
            errors.append(_error(path, "runtime-dependency", f"工程必须声明运行依赖: {dependency}"))
    ini_options = document.get("tool", {}).get("pytest", {}).get("ini_options", {})
    if not isinstance(ini_options, dict):
        errors.append(_error(path, "pytest-configuration", "tool.pytest.ini_options 必须是映射", "ini_options"))
        ini_options = {}
    dangerous_options = {
        "addopts", "required_plugins", "testpaths", "python_files", "python_classes", "python_functions",
        "norecursedirs", "continue_on_collection_errors", "usefixtures",
    }
    configured_dangerous = sorted(dangerous_options.intersection(ini_options))
    if configured_dangerous:
        errors.append(_error(
            path,
            "pytest-configuration",
            f"生成工程不得通过 pytest 配置改变插件加载、测试选择或收集语义: {configured_dangerous}",
            configured_dangerous[0],
        ))
    markers = ini_options.get("markers", [])
    marker_text = "\n".join(markers) if isinstance(markers, list) and all(isinstance(item, str) for item in markers) else ""
    for marker in ("business_e2e", "scenario_id", "read_only_smoke"):
        if marker not in marker_text:
            errors.append(_error(path, "pytest-marker", f"必须注册 pytest marker: {marker}", "markers"))
    for alternate in (project_root / "pytest.ini", project_root / "tox.ini", project_root / "setup.cfg"):
        if not alternate.is_file():
            continue
        if alternate.name == "pytest.ini":
            errors.append(_error(alternate, "pytest-configuration", "pytest 配置只能位于 pyproject.toml"))
            continue
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(alternate, encoding="utf-8")
        except (OSError, UnicodeError, configparser.Error) as exc:
            errors.append(_error(alternate, "pytest-configuration", f"无法核对 pytest 配置: {exc}"))
            continue
        if parser.has_section("pytest"):
            errors.append(_error(alternate, "pytest-configuration", "禁止通过备用配置文件声明 [pytest]"))
    for conftest in project_root.rglob("conftest.py"):
        if not any(part in SKIP_DIRS for part in conftest.parts):
            errors.append(_error(conftest, "pytest-conftest-forbidden", "生成工程不得使用隐式 conftest fixture 或 hook；fixture 必须显式导入"))
    return errors


def static_errors(project_root: Path, selected: str | None = None) -> list[str]:
    """执行环境无关的资产、源码、脚本、SQL 和图表静态门禁。"""

    errors, _, _ = contract_errors(project_root, selected)
    directory_errors: list[str] = []
    all_scenarios: list[tuple[Path, dict[str, Any]]] = []
    for directory in _scenario_directories(project_root, None, directory_errors):
        definition = _load_yaml(directory / "场景定义.yaml", directory_errors)
        if definition:
            all_scenarios.append((directory, definition))
    errors.extend(directory_errors)
    errors.extend(_project_errors(project_root))
    errors.extend(_asset_errors(project_root))
    for path in _python_paths(project_root):
        errors.extend(_python_errors(path, project_root))
    yaml_paths = [project_root / "discovery" / "workspace.yaml", project_root / "config" / "config.yaml"]
    yaml_paths.extend((project_root / "config" / "environments").glob("*.yaml") if (project_root / "config" / "environments").is_dir() else [])
    yaml_paths.extend(directory / "场景定义.yaml" for directory, _ in all_scenarios)
    for path in yaml_paths:
        if path.is_file():
            errors.extend(_yaml_comment_errors(path))
            document = _load_yaml(path, errors)
            if document:
                errors.extend(_secret_errors(path, document))
    credential_artifacts = {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"}
    text_suffixes = {
        ".md", ".toml", ".ini", ".cfg", ".properties", ".env", ".txt", ".sh", ".bat",
        ".yaml", ".yml", ".json", ".xml",
    }
    for path in project_root.rglob("*"):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
            continue
        suffix = path.suffix.casefold()
        if suffix in credential_artifacts:
            errors.append(_error(path, "credential-artifact", "生成工程不得包含私钥、密钥库或客户端证书凭据文件"))
            continue
        if suffix not in text_suffixes:
            continue
        try:
            text = path.read_text(encoding="utf-8")
            errors.extend(_secret_errors(path, text))
            if suffix == ".json":
                errors.extend(_secret_errors(path, json.loads(text)))
            elif suffix in {".yaml", ".yml"}:
                errors.extend(_secret_errors(path, yaml.safe_load(text)))
            elif suffix == ".xml":
                root = ET.fromstring(text)
                for element in root.iter():
                    if _sensitive_name(element.tag.rsplit("}", 1)[-1]) and element.text and not _safe_credential_literal(element.text.strip()):
                        errors.append(_error(path, "secret-free", f"XML 敏感元素必须只保存精确占位符或类型化引用: {element.tag}"))
        except (json.JSONDecodeError, yaml.YAMLError, ET.ParseError):
            pass
        except (OSError, UnicodeError) as exc:
            errors.append(_error(path, "secret-scan", f"无法读取文本文件进行凭据扫描: {exc}"))
    legacy_config = project_root / "config" / "runtime.yaml"
    if legacy_config.exists():
        errors.append(_error(legacy_config, "legacy-config-name", "运行配置已统一为 config/config.yaml，禁止保留 runtime.yaml"))
    runtime_path = project_root / "config" / "config.yaml"
    runtime = _load_yaml(runtime_path, errors)
    defaults = runtime.get("defaults", {}) if isinstance(runtime, dict) else {}
    safety = defaults.get("safety", {}) if isinstance(defaults, dict) else {}
    required_safety = {"database_control_enabled", "mutable_configuration_enabled", "message_publish_enabled"}
    if not isinstance(safety, dict) or set(safety) != required_safety:
        errors.append(_error(runtime_path, "safety-default-schema", f"defaults.safety 必须包含且仅包含: {sorted(required_safety)}", "safety:"))
    else:
        enabled = [name for name, value in safety.items() if value is not False]
        if enabled:
            errors.append(_error(runtime_path, "dangerous-default", f"提交的危险能力默认值必须全部为 false: {enabled}", "safety:"))
    errors.extend(_diagram_errors(project_root))
    errors.extend(_launcher_errors(project_root))
    return errors


def source_version_results(project_root: Path, selected: str | None = None) -> tuple[list[str], list[dict[str, Any]]]:
    """只读比较每个场景的源码基线、当前提交和工作区状态。"""

    errors, scenarios, discovery = contract_errors(project_root, selected)
    repositories = {
        str(item.get("id")): item
        for item in discovery.get("inventory", {}).get("repositories", [])
        if isinstance(item, dict)
    }
    results: list[dict[str, Any]] = []
    for directory, definition in scenarios:
        for source in definition.get("source", []):
            if not isinstance(source, dict) or source.get("repo") not in repositories:
                continue
            repo_id = str(source["repo"])
            repo_root = _resolve(project_root, str(repositories[repo_id].get("root", "")))
            recorded = str(source.get("commit", ""))
            head_call = _git(repo_root, "rev-parse", "HEAD")
            current = head_call.stdout.strip() if head_call.returncode == 0 else None
            status_call = _git(repo_root, "status", "--porcelain", "--untracked-files=all")
            dirty_files = [line[3:] for line in status_call.stdout.splitlines() if len(line) > 3] if status_call.returncode == 0 else []
            diff_call = _git(repo_root, "diff", "--name-only", f"{recorded}..{current}") if current else None
            changed = diff_call.stdout.splitlines() if diff_call and diff_call.returncode == 0 else []
            unresolved = [
                anchor for anchor in source.get("anchors", [])
                if _git(repo_root, "grep", "-q", "--fixed-strings", str(anchor), current or "HEAD", "--", ".").returncode != 0
            ]
            exists = SHA_RE.fullmatch(recorded) and _git(repo_root, "cat-file", "-e", f"{recorded}^{{commit}}").returncode == 0
            git_failed = status_call.returncode != 0 or (diff_call is not None and diff_call.returncode != 0)
            if not exists or unresolved or current is None or git_failed:
                outcome = "full_rediscovery_required"
                reason = "基线提交、当前提交、源码锚点或 Git 状态无法解析"
            elif dirty_files:
                outcome = "dirty_review_required"
                reason = "源码工作区存在未提交改动，不能声明同步"
            elif recorded == current:
                outcome = "unchanged"
                reason = "当前提交等于已审查基线且工作区干净"
            else:
                outcome = "affected"
                reason = "存在尚未完成人工影响审查的提交变更"
            results.append({
                "scenario": directory.name,
                "repository": repo_id,
                "recorded_commit": recorded,
                "current_commit": current,
                "dirty": bool(dirty_files),
                "dirty_files": dirty_files,
                "changed_files": changed,
                "unresolved_anchors": unresolved,
                "outcome": outcome,
                "reason": reason,
            })
    return errors, results


def _run(command: list[str], project_root: Path, environment: Mapping[str, str] | None = None) -> int:
    """不经过 Shell 执行固定阶段命令并返回退出状态。"""

    completed = subprocess.run(command, cwd=project_root, env=dict(environment) if environment else None, check=False)
    return completed.returncode


def _pytest_junit_passed(path: Path) -> bool:
    """Require at least one executed test and reject skipped, xfailed, failed, or errored nodes."""

    try:
        root = ET.fromstring(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ET.ParseError):
        return False
    suites = [root] if root.tag.rsplit("}", 1)[-1] == "testsuite" else [
        item for item in root.iter() if item.tag.rsplit("}", 1)[-1] == "testsuite"
    ]
    if not suites:
        return False
    totals = {
        name: sum(int(suite.get(name, "0")) for suite in suites)
        for name in ("tests", "failures", "errors", "skipped")
    }
    return totals["tests"] > 0 and all(totals[name] == 0 for name in ("failures", "errors", "skipped"))


def _events(
    evidence_root: Path,
    run_id: str,
    paths: Iterable[Path] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """严格读取只属于本次运行且结构完整的 JSON 证据事件。"""

    events: list[dict[str, Any]] = []
    errors: list[str] = []
    if not evidence_root.is_dir():
        return events, errors
    schemas = {
        "endpoint": {"phase", "method", "target_ref", "status", "summary", "verified"},
        "business_entered": set(),
        "control": {"control_kind", "action", "correlation_ref", "side_effect"},
        "restoration": {"status", "resources"},
    }
    project_root = evidence_root.parents[2]
    scenario_root = project_root / "scenarios"
    scenario_names = {path.name for path in scenario_root.iterdir() if path.is_dir()} if scenario_root.is_dir() else set()
    event_paths = sorted(paths) if paths is not None else sorted(evidence_root.glob("*.json"))
    for path in event_paths:
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors.append(_error(path, "evidence-parse", f"运行证据无法解析: {exc}"))
            continue
        if not isinstance(event, dict) or set(event) != {"version", "run_id", "kind", "scenario", "details"}:
            errors.append(_error(path, "evidence-schema", "运行证据顶层结构无效"))
            continue
        kind = event.get("kind")
        details = event.get("details")
        if event.get("version") != 1 or event.get("run_id") != run_id or kind not in schemas:
            errors.append(_error(path, "evidence-identity", "运行证据版本、run_id 或 kind 无效"))
            continue
        if not isinstance(event.get("scenario"), str) or not event["scenario"].strip() or not isinstance(details, dict):
            errors.append(_error(path, "evidence-content", "运行证据场景和 details 无效"))
            continue
        if event["scenario"] not in scenario_names:
            errors.append(_error(path, "evidence-scenario", f"运行证据引用未知场景: {event['scenario']}"))
            continue
        expected = schemas[kind]
        actual = set(details)
        if kind == "restoration":
            if (
                actual not in (expected, expected | {"error"})
                or details.get("status") not in {"passed", "failed"}
                or not _strings(details.get("resources"))
                or (details.get("status") == "failed") != ("error" in details)
            ):
                errors.append(_error(path, "evidence-restoration", "恢复证据结构或状态无效"))
                continue
        elif actual != expected:
            errors.append(_error(path, "evidence-details", f"{kind} 证据字段无效"))
            continue
        elif kind == "endpoint" and (
            details.get("phase") not in {"smoke", "business"}
            or not isinstance(details.get("method"), str)
            or not details["method"].strip()
            or details.get("phase") == "smoke" and details["method"].upper() not in {"GET", "HEAD", "READ"}
            or not isinstance(details.get("target_ref"), str)
            or not details["target_ref"].strip()
            or details.get("verified") is not True
            or str(details.get("method", "")).upper() in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}
            and (
                not isinstance(details.get("status"), int)
                or isinstance(details.get("status"), bool)
                or not 100 <= details["status"] < 500
            )
        ):
            errors.append(_error(path, "evidence-endpoint", "接口证据阶段、方法或目标引用无效"))
            continue
        elif kind == "control" and (
            details.get("control_kind") not in CONTROL_NAMES
            or not isinstance(details.get("action"), str)
            or not details["action"].strip()
            or not isinstance(details.get("correlation_ref"), str)
            or not details["correlation_ref"].strip()
            or details.get("side_effect") not in {"read", "write"}
        ):
            errors.append(_error(path, "evidence-control", "控制证据类别、动作或关联引用无效"))
            continue
        events.append(event)
    return events, errors


def _report_scenarios(
    scenarios: Sequence[tuple[Path, dict[str, Any]]],
    events: Sequence[dict[str, Any]],
    executions: Mapping[str, dict[str, Any]],
    smoke_status: str,
) -> list[dict[str, Any]]:
    """把静态契约和真实运行事件聚合为逐场景验收结果。"""

    reports: list[dict[str, Any]] = []
    for directory, definition in scenarios:
        scenario_events = [event for event in events if event.get("scenario") == directory.name]
        controls = definition.get("controls", {})
        reports.append({
            "name": directory.name,
            "owner": definition.get("generation", {}).get("owner"),
            "generation_mode": definition.get("generation", {}).get("mode"),
            "degradation_reason": definition.get("generation", {}).get("degradation_reason"),
            "status": definition.get("meta", {}).get("status"),
            "planned_controls": [
                name for name in CONTROL_NAMES
                if isinstance(controls.get(name), dict) and controls[name].get("planned_use")
            ],
            "used_controls": [event.get("details") for event in scenario_events if event.get("kind") == "control"],
            "endpoint_calls": [event.get("details") for event in scenario_events if event.get("kind") == "endpoint"],
            "business_entered": any(event.get("kind") == "business_entered" for event in scenario_events),
            "smoke": (
                "N/A" if smoke_status == "N/A" else
                "passed" if smoke_status == "passed" and any(
                    event.get("kind") == "endpoint" and event.get("details", {}).get("phase") == "smoke"
                    for event in scenario_events
                ) else "failed"
            ),
            "business": executions.get(directory.name, {"status": "N/A", "exit_code": None, "reason": "未执行"}),
            "restoration": [event.get("details") for event in scenario_events if event.get("kind") == "restoration"],
        })
    return reports


def _unexpected_control_correlations(
    definition: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> set[str]:
    """返回未绑定到场景隔离契约的运行控制关联值。"""

    isolation = definition.get("isolation", {})
    if not isinstance(isolation, Mapping):
        return {
            str(event.get("details", {}).get("correlation_ref"))
            for event in events if event.get("kind") == "control"
        }
    allowed = set(str(item) for item in isolation.get("correlation_keys", []))
    allowed.update(str(item) for item in isolation.get("mutable_controls", []))
    allowed.update(
        str(item.get("identity")) for item in isolation.get("owned_resources", []) if isinstance(item, Mapping)
    )
    observed = {
        str(event.get("details", {}).get("correlation_ref"))
        for event in events if event.get("kind") == "control"
    }
    return observed - allowed


def _write_report(project_root: Path, report: dict[str, Any]) -> Path:
    """原子写入机器可读运行报告。"""

    path = project_root / "artifacts" / "e2e-run.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(_redact_report(report), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _redact_report(value: Any, key: str = "") -> Any:
    """递归脱敏报告中的凭据键、认证文本和带密钥查询参数。"""

    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    fragments = {
        "password", "secret", "token", "authorization", "apikey", "accesskey", "credential",
        "cookie", "connection", "dsn", "privatekey", "sshkey", "passphrase", "clientsecret", "auth",
    }
    sensitive_key = any(fragment in normalized for fragment in fragments)
    if sensitive_key and normalized != "connectionsource":
        return "<redacted>"
    if isinstance(value, dict):
        return {str(child_key): _redact_report(child, str(child_key)) for child_key, child in value.items()}
    if isinstance(value, list):
        return [_redact_report(item) for item in value]
    if sensitive_key:
        return "<redacted>"
    if isinstance(value, str) and _contains_usable_credential_text(value):
        return "<redacted>"
    return value


def _pytest_arg_errors(arguments: Sequence[str]) -> list[str]:
    """只允许不会改变测试选择或成功语义的展示型 pytest 参数。"""

    exact = {"-q", "-v", "-vv", "-vvv", "-s", "--showlocals", "--disable-warnings"}
    prefixes = ("--tb=", "--durations=", "--color=", "--capture=", "--log-cli-level=")
    return [argument for argument in arguments if argument not in exact and not argument.startswith(prefixes)]


def _smoke_paths(project_root: Path, scenario_names: set[str]) -> list[Path]:
    """按 record_endpoint 的场景字面量选择只读冒烟模块。"""

    selected: list[Path] = []
    root = project_root / "tests" / "runtime"
    if not root.is_dir():
        return selected
    for path in _walk_files(root):
        if path.suffix != ".py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeError, SyntaxError):
            continue
        aliases = _import_aliases(tree)
        declared = {
            str(argument.value)
            for call in ast.walk(tree)
            if isinstance(call, ast.Call) and _resolved_call_name(call, aliases).rsplit(".", 1)[-1] == "record_endpoint"
            if (argument := _call_argument(call, "scenario", 0)) is not None
            and isinstance(argument, ast.Constant) and isinstance(argument.value, str)
        }
        if declared.intersection(scenario_names):
            selected.append(path)
    return sorted(selected)


def _gate_digest(paths: Iterable[Path]) -> str:
    """计算一组门禁输入文件的稳定内容摘要。"""

    digest = hashlib.sha256()
    for path in sorted(set(item.resolve() for item in paths)):
        digest.update(str(path).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes() if path.is_file() else b"<missing>")
        digest.update(b"\0")
    return digest.hexdigest()


def _seal_path(project_root: Path, gate: str) -> Path:
    """返回顺序门禁摘要文件路径。"""

    return project_root / ".e2e-state" / f"{gate}.json"


ORDERED_GATE_SEQUENCE = (
    "workspace_inventory", "dependency_topology", "initial_configuration", "runtime_probe",
    "control_matrix", "scenario_split", "scenario_ownership", "shared_integration", "static",
)


def _gate_session_path(project_root: Path) -> Path:
    """Return the active ordered-gate session file."""

    return project_root / ".e2e-state" / "session.json"


def _start_gate_session(project_root: Path) -> None:
    """Invalidate all historical ordering seals by starting a new run."""

    path = _gate_session_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "version": 1,
        "run_id": uuid.uuid4().hex,
        "next_gate": ORDERED_GATE_SEQUENCE[0],
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def _gate_session_errors(project_root: Path, stage: str) -> list[str]:
    """Require the next stage of a fresh, single ordered gate run."""

    path = _gate_session_path(project_root)
    errors: list[str] = []
    session = _load_json(path, errors)
    try:
        started = dt.datetime.fromisoformat(str(session.get("started_at"))) if isinstance(session, dict) else None
        fresh = started is not None and dt.datetime.now(dt.timezone.utc) - started <= dt.timedelta(hours=1)
    except ValueError:
        fresh = False
    if (
        not isinstance(session, dict)
        or session.get("version") != 1
        or session.get("next_gate") != stage
        or not fresh
    ):
        errors.append(_error(path, "gate-order", f"当前门禁会话不允许执行 {stage}；必须从 workspace_inventory 开始连续执行"))
    return errors


def _advance_gate_session(project_root: Path, stage: str) -> None:
    """Advance the active gate session after one successful stage."""

    path = _gate_session_path(project_root)
    session = json.loads(path.read_text(encoding="utf-8"))
    index = ORDERED_GATE_SEQUENCE.index(stage)
    session["next_gate"] = ORDERED_GATE_SEQUENCE[index + 1] if index + 1 < len(ORDERED_GATE_SEQUENCE) else "complete"
    path.write_text(json.dumps(session, indent=2) + "\n", encoding="utf-8")


def _write_seal(project_root: Path, gate: str, paths: Iterable[Path]) -> None:
    """记录已通过门禁的输入摘要供下一阶段核验。"""

    path = _seal_path(project_root, gate)
    path.parent.mkdir(parents=True, exist_ok=True)
    session_errors: list[str] = []
    session = _load_json(_gate_session_path(project_root), session_errors)
    run_id = session.get("run_id") if isinstance(session, dict) else None
    path.write_text(json.dumps({"version": 1, "run_id": run_id, "digest": _gate_digest(paths)}, indent=2) + "\n", encoding="utf-8")


def _seal_errors(project_root: Path, gate: str, paths: Iterable[Path]) -> list[str]:
    """拒绝缺失、损坏或输入已变化的前置门禁摘要。"""

    path = _seal_path(project_root, gate)
    errors: list[str] = []
    seal = _load_json(path, errors)
    session = _load_json(_gate_session_path(project_root), errors)
    expected = _gate_digest(paths)
    if (
        not isinstance(seal, dict)
        or seal.get("version") != 1
        or seal.get("digest") != expected
        or not isinstance(session, dict)
        or seal.get("run_id") != session.get("run_id")
    ):
        errors.append(_error(path, "gate-order", f"{gate} 门禁未执行或输入已变化，必须按顺序重新执行"))
    return errors


def _discovery_inputs(project_root: Path) -> list[Path]:
    """列出发现门禁摘要覆盖的文件。"""

    return [project_root / "discovery" / "workspace.yaml"]


def _contract_inputs(project_root: Path, selected: str | None) -> list[Path]:
    """列出契约门禁摘要覆盖的发现和场景定义文件。"""

    paths = _discovery_inputs(project_root)
    root = project_root / "scenarios"
    if root.is_dir():
        paths.extend(path / "场景定义.yaml" for path in root.iterdir() if path.is_dir())
    return paths


def _diagnostic_rule(error: str) -> str:
    """从带路径和行号的诊断中提取稳定规则名。"""

    match = re.search(r":\d+: ([a-z][a-z0-9-]+):", error)
    return match.group(1) if match else "unclassified"


def _scoped_errors(errors: list[str], stage: str) -> list[str]:
    """把聚合校验诊断稳定归入不可合并的执行阶段。"""

    discovery_prefixes = {
        "workspace_inventory": ("discovery-", "inventory-", "workspace-", "repository-", "build-", "module-", "existing-"),
        "dependency_topology": ("topology-",),
        "initial_configuration": ("configuration-", "secret-free"),
        "runtime_probe": ("runtime-",),
    }
    contract_prefixes = {
        "control_matrix": (
            "scenario-schema", "scenario-meta", "scenario-id", "scenario-status", "readiness-", "preconditions",
            "steps-", "step-", "control-", "ready-", "database-", "integration-", "pending-", "contract-",
        ),
        "scenario_split": ("generation-schema", "generation-mode", "generation-degradation", "multi-scenario-mode"),
        "scenario_ownership": ("generation-owner", "generation-scope", "multi-scenario-owner"),
    }
    if stage in discovery_prefixes:
        assigned = discovery_prefixes
    elif stage in contract_prefixes:
        assigned = contract_prefixes
    elif stage == "shared_integration":
        known = tuple(prefix for prefixes in contract_prefixes.values() for prefix in prefixes)
        return [error for error in errors if not _diagnostic_rule(error).startswith(known)]
    else:
        return errors
    prefixes = assigned[stage]
    selected = [error for error in errors if _diagnostic_rule(error).startswith(prefixes)]
    if stage == "workspace_inventory":
        known = tuple(prefix for values in discovery_prefixes.values() for prefix in values)
        selected.extend(error for error in errors if not _diagnostic_rule(error).startswith(known))
    return list(dict.fromkeys(selected))


def _ordered_stage_errors(project_root: Path, stage: str, selected: str | None) -> list[str]:
    """执行单个发现或契约阶段，并校验前一阶段内容摘要。"""

    discovery_order = ("workspace_inventory", "dependency_topology", "initial_configuration", "runtime_probe")
    contract_order = ("control_matrix", "scenario_split", "scenario_ownership", "shared_integration")
    if stage == "workspace_inventory":
        _start_gate_session(project_root)
    session_errors = _gate_session_errors(project_root, stage)
    if session_errors:
        return session_errors
    if stage in discovery_order:
        index = discovery_order.index(stage)
        errors: list[str] = []
        if index:
            errors.extend(_seal_errors(project_root, discovery_order[index - 1], _discovery_inputs(project_root)))
        aggregate, discovery = discovery_errors(project_root)
        errors.extend(_scoped_errors(aggregate, stage))
        if stage == "runtime_probe" and not errors:
            errors.extend(_runtime_probe_live_errors(
                project_root / "discovery" / "workspace.yaml",
                discovery.get("runtime_probe"),
            ))
        if not errors:
            _write_seal(project_root, stage, _discovery_inputs(project_root))
            if stage == "runtime_probe":
                _write_seal(project_root, "discovery", _discovery_inputs(project_root))
            _advance_gate_session(project_root, stage)
        return errors
    if stage in contract_order:
        index = contract_order.index(stage)
        previous_gate = "discovery" if index == 0 else contract_order[index - 1]
        previous_inputs = _discovery_inputs(project_root) if index == 0 else _contract_inputs(project_root, selected)
        errors = _seal_errors(project_root, previous_gate, previous_inputs)
        aggregate, _, _ = contract_errors(project_root, selected)
        errors.extend(_scoped_errors(aggregate, stage))
        if not errors:
            _write_seal(project_root, stage, _contract_inputs(project_root, selected))
            if stage == "shared_integration":
                _write_seal(project_root, "contracts", _contract_inputs(project_root, selected))
            _advance_gate_session(project_root, stage)
        return errors
    raise ValueError(f"未知有序阶段: {stage}")


def run_ordered(project_root: Path, scenario: str | None, pytest_args: list[str], *, static_only: bool) -> int:
    """按不可跳过的固定顺序运行门禁、冒烟、业务和恢复汇总。"""

    project_root = project_root.resolve()
    run_id = uuid.uuid4().hex
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    evidence_root = project_root / "artifacts" / "evidence" / run_id
    environment = os.environ.copy()
    environment.pop("PYTEST_ADDOPTS", None)
    environment.pop("PYTEST_PLUGINS", None)
    environment.pop("E2E_EVIDENCE_DIR", None)
    environment.pop("E2E_RUN_ID", None)
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    evidence_environment = environment.copy()
    evidence_environment["E2E_EVIDENCE_DIR"] = str(evidence_root)
    evidence_environment["E2E_RUN_ID"] = run_id
    stages = [
        "workspace_inventory", "dependency_topology", "initial_configuration", "runtime_probe",
        "control_matrix", "scenario_split", "scenario_ownership", "shared_integration",
        "environment_tests", "static", "source_versions", "collect", "read_only_smoke",
        "business", "restoration", "summary",
    ]
    outcomes = {name: {"status": "N/A", "exit_code": None} for name in stages}
    report: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "started_at": started,
        "finished_at": None,
        "selected_scenario": scenario,
        "static_only": static_only,
        "stages": outcomes,
        "discovery": {},
        "source_versions": [],
        "scenarios": [],
        "evidence_diagnostics": [],
    }
    executions: dict[str, dict[str, Any]] = {}
    run_event_paths: set[Path] = set()

    def finish(exit_code: int) -> int:
        """聚合当前证据、写报告并保留原始失败状态。"""

        events, evidence_errors = _events(evidence_root, run_id, run_event_paths)
        if evidence_errors:
            report["evidence_diagnostics"] = evidence_errors
            if exit_code == 0:
                exit_code = 1
        contract_diagnostics, selected_scenarios, discovery = contract_errors(project_root, scenario)
        inventory = discovery.get("inventory", {}) if isinstance(discovery, dict) else {}
        report["discovery"] = {
            "repositories": inventory.get("repositories", []),
            "existing_e2e": inventory.get("existing_e2e", []),
            "topology": discovery.get("topology", {}),
            "configuration": discovery.get("configuration", {}),
            "runtime_probe": discovery.get("runtime_probe", {}),
            "diagnostics": contract_diagnostics if exit_code else [],
        }
        report["scenarios"] = _report_scenarios(
            selected_scenarios,
            events,
            executions,
            outcomes["read_only_smoke"]["status"],
        )
        report["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        outcomes["summary"] = {"status": "passed" if exit_code == 0 else "failed", "exit_code": exit_code}
        path = _write_report(project_root, report)
        print(f"E2E 报告: {path}")
        return exit_code

    invalid_pytest_args = _pytest_arg_errors(pytest_args)
    if invalid_pytest_args:
        print(f"不允许改变业务测试选择或成功语义的 pytest 参数: {invalid_pytest_args}", file=sys.stderr)
        outcomes["business"] = {"status": "failed", "exit_code": 2}
        return finish(2)

    selector = ["--scenario", scenario] if scenario else []
    check_script = str(project_root / "scripts" / "check_scenarios.py")
    source_script = str(project_root / "scripts" / "check_source_versions.py")
    for stage in ("workspace_inventory", "dependency_topology", "initial_configuration", "runtime_probe"):
        code = _run([sys.executable, check_script, "--gate", stage], project_root, environment)
        outcomes[stage] = {"status": "passed" if code == 0 else "failed", "exit_code": code}
        if code:
            return finish(code)

    for stage in ("control_matrix", "scenario_split", "scenario_ownership", "shared_integration"):
        code = _run([sys.executable, check_script, "--gate", stage, *selector], project_root, environment)
        outcomes[stage] = {"status": "passed" if code == 0 else "failed", "exit_code": code}
        if code:
            return finish(code)

    static_code = _run([sys.executable, check_script, "--gate", "static", *selector], project_root, environment)
    outcomes["static"] = {"status": "passed" if static_code == 0 else "failed", "exit_code": static_code}
    if static_code:
        return finish(static_code)

    tests_code = _run([
        sys.executable, "-m", "pytest", "tests", "-m", "not read_only_smoke and not business_e2e", "--maxfail=1",
    ], project_root, environment)
    outcomes["environment_tests"] = {"status": "passed" if tests_code == 0 else "failed", "exit_code": tests_code}
    if tests_code:
        return finish(tests_code)

    source_code = _run([sys.executable, source_script, *selector], project_root, environment)
    source_errors_found, source_results = source_version_results(project_root, scenario)
    report["source_versions"] = source_results
    outcomes["source_versions"] = {"status": "passed" if source_code == 0 else "failed", "exit_code": source_code}
    if source_code or source_errors_found:
        return finish(source_code or 1)

    collect_target = str(project_root / "scenarios" / scenario) if scenario else str(project_root / "scenarios")
    collect_code = _run([sys.executable, "-m", "pytest", "--collect-only", collect_target], project_root, environment)
    outcomes["collect"] = {"status": "passed" if collect_code == 0 else "failed", "exit_code": collect_code}
    if collect_code:
        return finish(collect_code)
    if static_only:
        return finish(0)

    _, selected_scenarios, discovery = contract_errors(project_root, scenario)
    probe = discovery.get("runtime_probe", {}) if isinstance(discovery, dict) else {}
    if probe.get("requested") is True:
        for directory, _ in selected_scenarios:
            smoke_targets = _smoke_paths(project_root, {directory.name})
            if not smoke_targets:
                print(f"场景缺少可静态绑定的只读冒烟模块: {directory.name}", file=sys.stderr)
                outcomes["read_only_smoke"] = {"status": "failed", "exit_code": 1}
                return finish(1)
            before = set(evidence_root.glob("*.json")) if evidence_root.is_dir() else set()
            smoke_junit = evidence_root / f"smoke-{directory.name}.xml"
            smoke_junit.parent.mkdir(parents=True, exist_ok=True)
            smoke_code = _run([
                sys.executable, "-m", "pytest", *(str(path) for path in smoke_targets),
                "-m", "read_only_smoke", "--maxfail=1",
                f"--confcutdir={project_root / 'tests' / 'runtime'}", f"--junitxml={smoke_junit}",
            ], project_root, evidence_environment)
            created = set(evidence_root.glob("*.json")) - before if evidence_root.is_dir() else set()
            run_event_paths.update(created)
            smoke_events, smoke_evidence_errors = _events(evidence_root, run_id, created)
            smoke_scenarios = {
                str(event.get("scenario")) for event in smoke_events
                if event.get("kind") == "endpoint" and event.get("details", {}).get("phase") == "smoke"
            }
            if smoke_code or not _pytest_junit_passed(smoke_junit) or smoke_evidence_errors or smoke_scenarios != {directory.name}:
                print(
                    f"只读冒烟必须由当前场景新增合法 endpoint 证据: expected={directory.name}, actual={sorted(smoke_scenarios)}",
                    file=sys.stderr,
                )
                outcomes["read_only_smoke"] = {"status": "failed", "exit_code": smoke_code or 1}
                return finish(smoke_code or 1)
        outcomes["read_only_smoke"] = {"status": "passed", "exit_code": 0}

    non_ready = [item for item in selected_scenarios if item[1].get("meta", {}).get("status") != "ready"]
    if non_ready:
        for directory, definition in non_ready:
            executions[directory.name] = {
                "status": "N/A",
                "exit_code": None,
                "reason": f"静态状态为 {definition.get('meta', {}).get('status')}，真实业务未执行",
            }
        print("选中范围包含非 ready 场景；业务与恢复阶段保持 N/A", file=sys.stderr)
        return finish(2)
    ready = selected_scenarios
    runtime_spec = importlib.util.spec_from_file_location("e2e_runtime", project_root / "common" / "e2e_runtime.py")
    if runtime_spec is None or runtime_spec.loader is None:
        outcomes["business"] = {"status": "failed", "exit_code": 1}
        return finish(1)
    runtime_module = importlib.util.module_from_spec(runtime_spec)
    runtime_spec.loader.exec_module(runtime_module)
    business_code = 0
    for index, (directory, definition) in enumerate(ready):
        try:
            runtime_module.preflight(project_root, directory.name, environ=environment)
        except Exception as exc:
            print(f"运行预检失败 [{directory.name}]: {exc}", file=sys.stderr)
            executions[directory.name] = {"status": "failed", "exit_code": 1, "reason": f"运行预检失败: {exc}"}
            business_code = 1
            break
        target = str(directory / f"test_{directory.name}.py")
        before = set(evidence_root.glob("*.json")) if evidence_root.is_dir() else set()
        business_junit = evidence_root / f"business-{directory.name}.xml"
        business_junit.parent.mkdir(parents=True, exist_ok=True)
        code = _run([
            sys.executable, "-m", "pytest", target, "-m", "business_e2e",
            f"--confcutdir={directory}", f"--junitxml={business_junit}", *pytest_args,
        ], project_root, evidence_environment)
        created = set(evidence_root.glob("*.json")) - before if evidence_root.is_dir() else set()
        run_event_paths.update(created)
        current_events, current_evidence_errors = _events(evidence_root, run_id, created)
        scenario_events = [event for event in current_events if event.get("scenario") == directory.name]
        foreign_events = [event for event in current_events if event.get("scenario") != directory.name]
        entered = any(event.get("kind") == "business_entered" for event in scenario_events)
        endpoints = [event for event in scenario_events if event.get("kind") == "endpoint" and event.get("details", {}).get("phase") == "business"]
        observed_controls = {
            (
                str(event.get("details", {}).get("control_kind")),
                str(event.get("details", {}).get("action")),
                str(event.get("details", {}).get("side_effect")),
            )
            for event in scenario_events if event.get("kind") == "control"
        }
        controls = definition.get("controls", {})
        planned = {
            (str(step.get("control")), str(step.get("action")), str(step.get("side_effect")))
            for step in definition.get("steps", []) if isinstance(step, dict)
        }
        missing_controls = planned - observed_controls
        unexpected_correlations = _unexpected_control_correlations(definition, scenario_events)
        planned_api = any(name in {"public_api", "test_or_admin_api"} for name, _, _ in planned)
        evidence_failure = bool(current_evidence_errors or foreign_events or not entered or missing_controls or unexpected_correlations)
        if planned_api and not endpoints:
            evidence_failure = True
        if evidence_failure:
            print(
                f"业务证据不完整 [{directory.name}]: entered={entered}, missing_controls={sorted(missing_controls)}, "
                f"unexpected_correlations={sorted(unexpected_correlations)}, foreign_events={len(foreign_events)}, business_endpoints={len(endpoints)}",
                file=sys.stderr,
            )
        scenario_code = code or (1 if evidence_failure or not _pytest_junit_passed(business_junit) else 0)
        executions[directory.name] = {
            "status": "passed" if scenario_code == 0 else "failed",
            "exit_code": scenario_code,
            "reason": "pytest 与运行证据均通过" if scenario_code == 0 else "pytest 失败或运行证据不完整",
        }
        if scenario_code:
            business_code = scenario_code
            for remaining, _ in ready[index + 1:]:
                executions[remaining.name] = {"status": "N/A", "exit_code": None, "reason": "前序业务场景失败后停止"}
            break
    outcomes["business"] = {"status": "passed" if business_code == 0 else "failed", "exit_code": business_code}

    events, evidence_errors = _events(evidence_root, run_id, run_event_paths)
    write_definitions = {
        directory.name: definition for directory, definition in ready
        if executions.get(directory.name, {}).get("status") in {"passed", "failed"}
        and any(
            event.get("kind") == "business_entered" and event.get("scenario") == directory.name
            for event in events
        )
        and any(isinstance(step, dict) and step.get("side_effect") == "write" for step in definition.get("steps", []))
    }
    restoration_events = [
        event for event in events
        if event.get("kind") == "restoration" and event.get("scenario") in write_definitions
    ]
    failed_restore = any(event.get("details", {}).get("status") != "passed" for event in restoration_events)
    restored_resources = {
        scenario_name: {
            str(resource)
            for event in restoration_events
            if event.get("scenario") == scenario_name and event.get("details", {}).get("status") == "passed"
            for resource in event.get("details", {}).get("resources", [])
        }
        for scenario_name in write_definitions
    }
    expected_resources = {
        scenario_name: {
            str(resource.get("identity"))
            for resource in definition.get("isolation", {}).get("owned_resources", [])
            if isinstance(resource, dict)
        }
        for scenario_name, definition in write_definitions.items()
    }
    resource_mismatch = {
        name: {"expected": sorted(expected_resources[name]), "actual": sorted(restored_resources[name])}
        for name in write_definitions if restored_resources[name] != expected_resources[name]
    }
    if not write_definitions:
        restoration_code = 0
    else:
        restoration_code = 1 if evidence_errors or failed_restore or resource_mismatch else 0
        if resource_mismatch:
            print(f"恢复资源与场景拥有资源不一致: {resource_mismatch}", file=sys.stderr)
        outcomes["restoration"] = {
            "status": "passed" if restoration_code == 0 else "failed",
            "exit_code": restoration_code,
        }
    return finish(business_code or restoration_code)


def main_checker(argv: list[str] | None = None) -> int:
    """实现 check_scenarios.py 的命令行入口。"""

    parser = argparse.ArgumentParser(description="校验 E2E 发现、契约和静态安全门禁")
    ordered_gates = (
        "workspace_inventory", "dependency_topology", "initial_configuration", "runtime_probe",
        "control_matrix", "scenario_split", "scenario_ownership", "shared_integration",
    )
    parser.add_argument("--gate", required=True, choices=(*ordered_gates, "discovery", "contracts", "static", "all"))
    parser.add_argument("--scenario")
    args = parser.parse_args(argv)
    project_root = Path(__file__).resolve().parents[1]
    if args.gate in ordered_gates:
        errors = _ordered_stage_errors(project_root, args.gate, args.scenario)
    elif args.gate == "discovery":
        errors, _ = discovery_errors(project_root)
    elif args.gate == "contracts":
        errors, _, _ = contract_errors(project_root, args.scenario)
    elif args.gate == "static":
        errors = _gate_session_errors(project_root, "static")
        errors.extend(_seal_errors(project_root, "discovery", _discovery_inputs(project_root)))
        errors.extend(_seal_errors(project_root, "contracts", _contract_inputs(project_root, args.scenario)))
        errors.extend(static_errors(project_root, args.scenario))
        if not errors:
            _write_seal(project_root, "static", _contract_inputs(project_root, args.scenario))
            _advance_gate_session(project_root, "static")
    else:
        errors = static_errors(project_root, args.scenario)
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


def main_source(argv: list[str] | None = None) -> int:
    """实现 check_source_versions.py 的只读命令行入口。"""

    parser = argparse.ArgumentParser(description="比较逐场景源码基线")
    parser.add_argument("--scenario")
    args = parser.parse_args(argv)
    project_root = Path(__file__).resolve().parents[1]
    errors, results = source_version_results(project_root, args.scenario)
    for error in errors:
        print(error, file=sys.stderr)
    print(json.dumps({"schema_version": 1, "results": results}, ensure_ascii=False, indent=2))
    failing = {"affected", "full_rediscovery_required", "dirty_review_required"}
    return 1 if errors or any(item["outcome"] in failing for item in results) else 0


def main_runner(argv: list[str] | None = None) -> int:
    """实现 run_e2e.py 的有序编排命令行入口。"""

    parser = argparse.ArgumentParser(
        description="按固定顺序执行 E2E 门禁和真实场景；默认运行全部场景",
        epilog="示例: run_e2e.py --scenario <场景名>",
    )
    parser.add_argument("--scenario", metavar="场景名", help="仅运行指定场景；省略时运行全部场景")
    parser.add_argument("--static-only", action="store_true", help="仅执行到静态检查和 collect-only")
    args, pytest_args = parser.parse_known_args(argv)
    project_root = Path(__file__).resolve().parents[1]
    return run_ordered(project_root, args.scenario, pytest_args, static_only=args.static_only)
