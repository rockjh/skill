"""Workspace, topology, configuration, and runtime discovery gates."""

from __future__ import annotations

import ast
import configparser
import http.client
import ipaddress
import json
import os
import re
import socket
import subprocess
import tomllib
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml


DISCOVERY_KEYS = {"schema_version", "inventory", "topology", "configuration", "runtime_probe", "gates"}


SEARCH_CATEGORIES = {"http_rpc", "messages", "database", "cache", "jobs", "configuration"}


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


PLACEHOLDER_RE = re.compile(r"^\$\{[A-Z][A-Z0-9_]*\}$")


SOURCE_PLACEHOLDER_RE = re.compile(r"^\$\{[^{}:\s]+(?::[^{}]*)?\}$")


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


__all__ = ["discovery_errors"]
