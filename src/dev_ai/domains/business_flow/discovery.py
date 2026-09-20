"""Conservative source discovery for business entry points and active errors."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import textwrap
import tomllib
from pathlib import Path

from .git import source_view
from .models import BehaviorEvidence, EntryPoint, ErrorEvidence, FunctionInfo, ScanResult


EXCLUDED_DIRS = {".git", ".venv", "venv", "node_modules", "dist", "build", "target", "vendor", "__pycache__"}
EXTENSIONS = {
    ".py": "Python", ".java": "Java", ".kt": "Kotlin", ".go": "Go", ".js": "JavaScript",
    ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript", ".cs": "C#", ".rb": "Ruby",
    ".php": "PHP", ".rs": "Rust", ".proto": "Protocol Buffers", ".graphql": "GraphQL",
    ".graphqls": "GraphQL", ".scala": "Scala", ".groovy": "Groovy", ".dart": "Dart",
    ".ex": "Elixir", ".exs": "Elixir", ".fs": "F#", ".fsx": "F#", ".lua": "Lua",
    ".c": "C", ".cc": "C++", ".cpp": "C++", ".h": "C/C++", ".hpp": "C++",
    ".sh": "Shell", ".ps1": "PowerShell", ".bat": "Batch", ".cmd": "Batch",
}
CONFIG_NAMES = {
    "application.yml", "application.yaml", "application.properties", "bootstrap.yml", "bootstrap.yaml",
    "build.gradle", "build.gradle.kts", "package.json", "pom.xml", "pyproject.toml", "routes.rb",
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "tsconfig.json", "application.json",
    "routes.json", "config.json", "web.xml",
}
CONFIG_SUFFIXES = {".properties", ".toml", ".xml", ".yaml", ".yml"}

ERROR_RE = re.compile(r"\b(?:raise|throw|panic)\b[^\n;]*", re.IGNORECASE)
CODE_RE = re.compile(r"[\"']((?:[A-Z][A-Z0-9_.:-]{2,}|\d{3,}))[\"']")
ENUM_RE = re.compile(r"\b(?:ErrorCode|Errors?|StatusCode)\.([A-Z][A-Z0-9_]+)")
STATUS_CODE_RE = re.compile(r"\b(?:status_code|code)\s*=\s*(\d{3,})\b", re.IGNORECASE)
CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
IGNORED_EXTERNAL_CALLS = {
    "get", "post", "put", "patch", "delete", "head", "request", "fetch", "save", "insert", "update",
    "create", "publish", "send", "emit", "execute", "query", "commit", "rollback", "close", "log",
    "abs", "all", "any", "bool", "bytes", "callable", "dict", "dir", "enumerate", "filter", "float",
    "format", "getattr", "hasattr", "hash", "int", "isinstance", "issubclass", "iter", "len", "list",
    "map", "max", "min", "next", "object", "open", "ord", "pow", "print", "range", "repr", "reversed",
    "round", "set", "setattr", "sorted", "str", "sum", "super", "tuple", "type", "vars", "zip",
    "append", "extend", "items", "keys", "values", "loads", "dumps", "now", "strftime",
}


def _module_name(relative: Path, identifier: str) -> str:
    parts = [part for part in relative.parts[:-1] if part.lower() not in {"src", "app", "api", "controller", "controllers", "service", "services"}]
    route_parts = [part for part in identifier.split(" ", 1)[-1].strip("/").split("/") if part]
    route_parts = [part for part in route_parts if part.lower() not in {"api", "admin", "public", "internal", "private"} and not re.fullmatch(r"v\d+", part, re.I)]
    route = route_parts[0] if route_parts else ""
    candidate = route if route and not route.startswith("{") else (parts[-1] if parts else relative.stem)
    candidate = re.sub(r"[^A-Za-z0-9_-]+", "-", candidate).strip("-") or "公共能力"
    return candidate.replace("_", "-").lower()


def _error(line: str, file: str, number: int) -> ErrorEvidence:
    code_match = CODE_RE.search(line) or ENUM_RE.search(line) or STATUS_CODE_RE.search(line)
    code = code_match.group(1) if code_match else "代码中未确认"
    return ErrorEvidence(code=code, condition=line.strip(), file=file, line=number)


def _functions(text: str, language: str, relative: str) -> list[FunctionInfo]:
    lines = text.splitlines()
    starts: list[tuple[int, str, int]] = []
    if language == "Python":
        try:
            root = ast.parse(text)
        except SyntaxError:
            root = None
        if root is not None:
            nodes = sorted(
                (node for node in ast.walk(root) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))),
                key=lambda node: node.lineno,
            )
            return [
                FunctionInfo(
                    node.name,
                    node.lineno,
                    node.end_lineno or node.lineno,
                    "\n".join(lines[node.lineno - 1:node.end_lineno]),
                    _calls("\n".join(lines[node.lineno - 1:node.end_lineno])),
                    _python_errors("\n".join(lines[node.lineno - 1:node.end_lineno]), relative, node.lineno),
                )
                for node in nodes
            ]
        pattern = re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(")
        for index, line in enumerate(lines):
            match = pattern.match(line)
            if match:
                starts.append((index, match.group(1), len(line) - len(line.lstrip())))
        result: list[FunctionInfo] = []
        for position, (start, name, indent) in enumerate(starts):
            end = len(lines)
            for next_start, _, next_indent in starts[position + 1:]:
                if next_indent <= indent:
                    end = next_start
                    if next_start > start and lines[next_start - 1].lstrip().startswith("@"):
                        end = next_start - 1
                    break
            body = "\n".join(lines[start:end])
            result.append(FunctionInfo(name, start + 1, end, body, _calls(body), _python_errors(body, relative, start + 1)))
        return result
    pattern = re.compile(
        r"\b(?:public|private|protected|static|final|async|override|function)?\s*[\w<>\[\],.?]+\s+([A-Za-z_]\w*)\s*\([^;{}]*\)\s*(?:throws [^{]+)?\{|"
        r"\bfun\s+([A-Za-z_]\w*)\s*\([^;{}]*\)[^{]*\{"
    )
    for index, line in enumerate(lines):
        match = pattern.search(line)
        if match:
            starts.append((index, match.group(1) or match.group(2), 0))
    result = []
    for position, (start, name, _) in enumerate(starts):
        end = len(lines)
        depth = 0
        for index in range(start, len(lines)):
            depth += lines[index].count("{") - lines[index].count("}")
            if index > start and depth <= 0:
                end = index + 1
                break
        if position + 1 < len(starts):
            end = min(end, starts[position + 1][0])
        body = "\n".join(lines[start:end])
        result.append(FunctionInfo(name, start + 1, end, body, _calls(body), _c_style_errors(body, relative, start + 1)))
    return result


def _calls(body: str) -> list[str]:
    ignored = {"if", "for", "while", "switch", "catch", "return", "raise", "throw", "def", "class"}
    names = [name for name in CALL_RE.findall(body) if name not in ignored]
    names.extend(
        name
        for name in re.findall(r"\b(?:submit|create_task|spawn|schedule|enqueue|delay)\s*\(\s*([A-Za-z_]\w*)", body)
        if name not in ignored
    )
    return list(dict.fromkeys(names))


def _unresolved_call(name: str) -> bool:
    if name.lower() in IGNORED_EXTERNAL_CALLS:
        return False
    if name.endswith(("Error", "Exception", "ErrorCode")):
        return False
    return True


def _errors(body: str, relative: str, offset: int) -> list[ErrorEvidence]:
    lines = body.splitlines()
    found: list[ErrorEvidence] = []
    for index, line in enumerate(lines):
        if line.lstrip().startswith(("#", "//", "/*", "*")) or not ERROR_RE.search(line):
            continue
        condition = line.strip()
        guard = next(
            (
                candidate.strip()
                for candidate in reversed(lines[max(0, index - 4):index])
                if re.search(r"\b(?:if|unless|when)\b", candidate)
            ),
            "",
        )
        if guard:
            condition = f"{guard} -> {condition}"
        found.append(_error(condition, relative, offset + index))
    return found


def _python_errors(body: str, relative: str, offset: int) -> list[ErrorEvidence]:
    try:
        root = ast.parse(textwrap.dedent(body))
    except SyntaxError:
        return _errors(body, relative, offset)
    function = next((node for node in root.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
    if function is None:
        return _errors(body, relative, offset)
    found: list[ErrorEvidence] = []

    def exception_name(node: ast.AST | None) -> str:
        if isinstance(node, ast.Call):
            node = node.func
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return ""

    normalized = textwrap.dedent(body)

    def source_of(node: ast.AST) -> str:
        return ast.get_source_segment(normalized, node) or "代码中未确认"

    def visit(statements: list[ast.stmt], caught: set[str], conditions: tuple[str, ...] = ()) -> None:
        terminated = False
        for statement in statements:
            if terminated:
                break
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(statement, ast.Raise):
                name = exception_name(statement.exc)
                if name and (name in caught or "*" in caught or "Exception" in caught or "BaseException" in caught):
                    continue
                source = source_of(statement)
                condition = f"{' 且 '.join(conditions)} -> {source}" if conditions else source
                found.append(_error(condition, relative, offset + statement.lineno - 1))
                terminated = True
                continue
            if isinstance(statement, ast.Try):
                handler_types = {exception_name(handler.type) or "*" for handler in statement.handlers}
                visit(statement.body, caught | handler_types, conditions)
                for handler in statement.handlers:
                    visit(handler.body, caught, conditions)
                visit(statement.orelse, caught, conditions)
                visit(statement.finalbody, caught, conditions)
                continue
            if isinstance(statement, ast.If):
                condition = source_of(statement.test)
                visit(statement.body, caught, (*conditions, condition))
                visit(statement.orelse, caught, (*conditions, f"非（{condition}）"))
                continue
            if isinstance(statement, (ast.Return, ast.Break, ast.Continue)):
                terminated = True
                continue
            child_blocks = [
                value for _, value in ast.iter_fields(statement)
                if isinstance(value, list) and value and all(isinstance(item, ast.stmt) for item in value)
            ]
            for block in child_blocks:
                visit(block, caught, conditions)

    visit(function.body, set())
    return found


def _caught_calls(body: str) -> set[str]:
    try:
        root = ast.parse(textwrap.dedent(body))
    except SyntaxError:
        return set()
    caught: set[str] = set()
    def exception_name(node: ast.AST | None) -> str:
        if isinstance(node, ast.Call):
            node = node.func
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return ""

    for statement in ast.walk(root):
        if not isinstance(statement, ast.Try):
            continue
        types = {"*"}
        explicit = {exception_name(handler.type) for handler in statement.handlers if handler.type is not None}
        if explicit:
            types = explicit
        if not types:
            continue
        for nested in ast.walk(ast.Module(body=statement.body, type_ignores=[])):
            if not isinstance(nested, ast.Call) or not isinstance(nested.func, ast.Name):
                continue
            if "*" in types or types & {"Exception", "BaseException", "BusinessError", "Error"}:
                caught.add(nested.func.id)
    return caught


def _c_style_errors(body: str, relative: str, offset: int) -> list[ErrorEvidence]:
    caught_lines: set[int] = set()
    pattern = re.compile(
        r"try\s*\{(?P<body>.*?)\}\s*catch\s*\((?P<parameter>[^)]*)\)",
        re.DOTALL,
    )
    for match in pattern.finditer(body):
        type_match = re.search(r"\b([A-Za-z_]\w*(?:Exception|Error|Throwable))\b", match.group("parameter"))
        catch_type = type_match.group(1) if type_match else "*"
        start_line = body[:match.start("body")].count("\n")
        for throw in re.finditer(r"\bthrow\s+new\s+([A-Za-z_]\w*)", match.group("body")):
            thrown = throw.group(1)
            if catch_type == "*" or catch_type in {thrown, "Exception", "RuntimeException", "Throwable", "Error"}:
                caught_lines.add(start_line + match.group("body")[:throw.start()].count("\n"))
    return [
        error
        for error in _errors(body, relative, offset)
        if error.line - offset not in caught_lines
    ]


def _c_style_caught_calls(body: str) -> set[str]:
    caught: set[str] = set()
    pattern = re.compile(
        r"try\s*\{(?P<body>.*?)\}\s*catch\s*\((?P<parameter>[^)]*)\)",
        re.DOTALL,
    )
    for match in pattern.finditer(body):
        parameter = match.group("parameter")
        if re.search(r"(?:Exception|Error|Throwable)|^\s*[A-Za-z_]\w*\s*$", parameter):
            caught.update(
                name for name in CALL_RE.findall(match.group("body"))
                if name not in {"if", "for", "while", "try", "catch", "throw", "return"}
            )
    return caught


def _exception_mappings(text: str, relative: str) -> dict[str, ErrorEvidence]:
    lines = text.splitlines()
    mappings: dict[str, ErrorEvidence] = {}
    for index, line in enumerate(lines):
        marker = re.search(
            r"(?:@ExceptionHandler|exception_handler|errorhandler|exceptionHandler)\s*\(\s*([A-Za-z_]\w*)",
            line,
        )
        if not marker:
            continue
        exception = marker.group(1)
        for offset, candidate in enumerate(lines[index:index + 20]):
            code = CODE_RE.search(candidate) or ENUM_RE.search(candidate) or STATUS_CODE_RE.search(candidate)
            if code:
                mappings[exception] = ErrorEvidence(code.group(1), f"统一异常处理器将 {exception} 转换为 {code.group(1)}", relative, index + offset + 1)
                break
    return mappings


BEHAVIOR_PATTERNS = (
    ("校验", re.compile(r"\b(assert|validate|validation|require|check|guard|verify)\b|\bif\s*\(", re.I)),
    ("事务", re.compile(r"@Transactional|\b(transaction|commit|rollback)\b", re.I)),
    ("锁与幂等", re.compile(r"\b(lock|unlock|synchronized|idempot|dedup|compareAndSet|nonce)\b", re.I)),
    ("持久化", re.compile(r"\b(repository|dao|save|insert|update|delete|persist|select|query|findBy)\b", re.I)),
    ("外部调用", re.compile(r"\b(requests|httpx|urllib|RestTemplate|WebClient|grpc|axios|fetch|httpClient)\b", re.I)),
    ("消息", re.compile(r"\b(kafka|rabbit|rocketmq|pulsar|publish|producer|send|emit)\b", re.I)),
    ("异步", re.compile(r"\b(async|await|executor|threadPool|future|submit)\b", re.I)),
    ("缓存或文件", re.compile(r"\b(cache|redis|memcached|open|read|write|file|path)\b", re.I)),
    ("状态变化", re.compile(r"\b(status|state)\b\s*(?:=|\.|,)|setStatus|setState", re.I)),
    ("结果", re.compile(r"^\s*(?:return|yield)\b", re.I)),
)


def _behaviors(function: FunctionInfo, relative: str) -> list[BehaviorEvidence]:
    evidence: list[BehaviorEvidence] = []
    for offset, line in enumerate(function.body.splitlines()):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//", "/*", "*")):
            continue
        for kind, pattern in BEHAVIOR_PATTERNS:
            if pattern.search(stripped):
                evidence.append(BehaviorEvidence(kind, stripped, relative, function.start + offset))
    return evidence


def _declaration_behaviors(function: FunctionInfo, relative: str, text: str) -> list[BehaviorEvidence]:
    lines = text.splitlines()
    start = max(0, function.start - 8)
    evidence: list[BehaviorEvidence] = []
    for index in range(start, function.start - 1):
        stripped = lines[index].strip()
        if not stripped.startswith(("@", "[")):
            continue
        for kind, pattern in BEHAVIOR_PATTERNS:
            if pattern.search(stripped):
                evidence.append(BehaviorEvidence(kind, stripped, relative, index + 1))
    return evidence


def _decorated_entries(text: str, language: str, relative: str) -> list[tuple[str, str, int, str]]:
    lines = text.splitlines()
    found: list[tuple[str, str, int, str]] = []
    base_path = ""
    proto_service = ""
    graphql_owner = ""
    for index, line in enumerate(lines):
        stripped = line.strip()
        annotation_text = stripped
        if stripped.startswith("@") and stripped.count("(") > stripped.count(")"):
            block = [stripped]
            balance = stripped.count("(") - stripped.count(")")
            for continuation in lines[index + 1:index + 12]:
                block.append(continuation.strip())
                balance += continuation.count("(") - continuation.count(")")
                if balance <= 0:
                    break
            annotation_text = " ".join(block)
        annotation = annotation_text.lstrip("@")
        kind = ""
        identifier = ""
        if language == "Protocol Buffers":
            service = re.match(r"service\s+([A-Za-z_]\w*)", stripped)
            if service:
                proto_service = service.group(1)
                continue
            rpc = re.match(r"rpc\s+([A-Za-z_]\w*)\s*\(", stripped)
            if rpc:
                kind, identifier = "rpc", f"{proto_service or '代码中未确认'}/{rpc.group(1)}"
        elif language == "GraphQL":
            owner = re.match(r"type\s+(Query|Mutation|Subscription)\b", stripped)
            if owner:
                graphql_owner = owner.group(1)
                continue
            field = re.match(r"([A-Za-z_]\w*)\s*(?:\([^)]*\))?\s*:", stripped)
            if graphql_owner and field:
                kind, identifier = "rpc", f"{graphql_owner}/{field.group(1)}"
        csharp = re.match(r"\[Http(Get|Post|Put|Patch|Delete)\s*\(\s*[\"']([^\"']*)", stripped, re.I)
        ruby = re.match(r"(get|post|put|patch|delete)\s+[\"']([^\"']+)", stripped, re.I)
        laravel = re.match(r"Route::(get|post|put|patch|delete)\s*\(\s*[\"']([^\"']+)", stripped, re.I)
        quartz = re.search(r"\bclass\s+([A-Za-z_]\w*)\b[^\n{]*\bimplements\s+[^\n{]*\bJob\b", stripped)
        if kind:
            # Protocol/GraphQL entries already have their identifier.
            pass
        elif csharp:
            kind, identifier = "url", f"{csharp.group(1).upper()} {csharp.group(2) or '代码中未确认'}"
        elif ruby:
            kind, identifier = "url", f"{ruby.group(1).upper()} {ruby.group(2)}"
        elif laravel:
            kind, identifier = "url", f"{laravel.group(1).upper()} {laravel.group(2)}"
        elif quartz:
            kind, identifier = "scheduled", quartz.group(1)
        elif re.match(r"@Controller\b", annotation_text) and re.search(r"\bclass\s+[A-Za-z_]\w*", " ".join(lines[index + 1:index + 5])):
            value = re.search(r"@Controller\s*\(\s*[\"']([^\"']+)", annotation_text)
            base_path = value.group(1) if value else ""
            continue
        elif stripped.startswith("@") and re.match(r"(?:app|router|blueprint|bp)\.(?:get|post|put|patch|delete|options|head)\s*\(", annotation, re.I):
            method = re.search(r"\.([A-Za-z]+)\s*\(", annotation).group(1).upper()
            value = re.search(r"\(\s*[\"']([^\"']+)", annotation)
            kind, identifier = "url", f"{method} {(value.group(1) if value else '代码中未确认')}"
        elif stripped.startswith("@") and re.match(r"(?:app|blueprint|bp)\.route\s*\(", annotation, re.I):
            value = re.search(r"\(\s*[\"']([^\"']+)", annotation)
            methods = re.search(r"methods\s*=\s*\[([^\]]+)\]", annotation, re.I)
            method = "REQUEST"
            if methods:
                method_values = re.findall(r"[\"']([A-Za-z]+)[\"']", methods.group(1))
                method = "/".join(item.upper() for item in method_values) or method
            kind, identifier = "url", f"{method} {(value.group(1) if value else '代码中未确认')}"
        elif re.search(r"(?:websocket|WebSocket)\s*\(", annotation):
            value = re.search(r"\(\s*[\"']([^\"']+)", annotation)
            kind, identifier = "websocket", f"WEBSOCKET {(value.group(1) if value else '代码中未确认')}"
        elif re.search(r"@(?:Get|Post|Put|Patch|Delete|Options|Head)\s*(?:\(|$)", annotation_text):
            mapping = re.search(r"@(Get|Post|Put|Patch|Delete|Options|Head)\s*(?:\(\s*[\"']([^\"']*)[\"'])?", annotation_text)
            method = mapping.group(1).upper() if mapping else "REQUEST"
            path = mapping.group(2) if mapping and mapping.group(2) else ""
            full_path = f"{base_path.rstrip('/')}/{path.lstrip('/')}" if base_path and path else (path or base_path or "代码中未确认")
            kind, identifier = "url", f"{method} {full_path}"
        elif re.search(r"(?:Get|Post|Put|Patch|Delete|Request)Mapping\s*\(", annotation_text):
            method_match = re.search(r"@(Get|Post|Put|Patch|Delete|Request)Mapping", annotation_text)
            method = (method_match.group(1).replace("Mapping", "") if method_match else "REQUEST").upper()
            if method == "REQUEST":
                method = "REQUEST"
            value = re.search(r"(?:value|path)?\s*=\s*(?:\{)?\s*[\"']([^\"']+)|\(\s*[\"']([^\"']+)", annotation_text)
            path = next((item for item in value.groups() if item), "代码中未确认") if value else "代码中未确认"
            if method == "REQUEST" and re.search(r"\bclass\b", " ".join(lines[index + 1:index + 5])):
                base_path = path
                continue
            full_path = f"{base_path.rstrip('/')}/{path.lstrip('/')}" if base_path and path != "代码中未确认" else path
            kind, identifier = "url", f"{method} {full_path}"
        elif re.search(r"@(Query|Mutation|Subscription)Mapping\b", annotation_text):
            mapping = re.search(r"@(Query|Mutation|Subscription)Mapping\b", annotation_text)
            kind, identifier = "rpc", mapping.group(1) if mapping else "代码中未确认"
        elif re.search(r"(?:KafkaListener|RabbitListener|JmsListener|PulsarListener|RocketMQMessageListener)\s*\(", annotation_text):
            value = re.search(
                r"(?:topics|queues|destination|topic|value)\s*=\s*(\{[^}]*\}|[\"'][^\"']+[\"'])|\(\s*[\"']([^\"']+)",
                annotation_text,
            )
            topics = re.findall(r"[\"']([^\"']+)[\"']", value.group(1) if value and value.group(1) else (value.group(2) if value else ""))
            kind, identifier = "message", (topics[0] if topics else "代码中未确认")
        elif re.search(r"(?:XxlJob|JobHandler|Scheduled|Cron|schedule|task)\s*\(", annotation_text, re.I):
            value = re.search(r"(?:cron|fixedDelay|fixedRate|fixedDelayString|fixedRateString|name|value)\s*=\s*[\"']([^\"']+)|\(\s*[\"']([^\"']+)", annotation_text, re.I)
            kind, identifier = "scheduled", next((item for item in (value.groups() if value else ()) if item), "代码中未确认")
        elif re.search(r"(?:EventListener|receiver|event_handler|on_event|EventPattern|MessagePattern|Transition|Workflow|state_machine)", stripped, re.I):
            value = re.search(r"(?:classes|value|pattern|event)\s*=\s*[\"']([^\"']+)", stripped, re.I)
            kind, identifier = "event", (value.group(1) if value else "代码中未确认")
        elif re.search(r"(?:click\.command|app\.command|typer\.command|Command\s*\()", stripped, re.I):
            value = re.search(r"(?:name|value)\s*=\s*[\"']([^\"']+)|\(\s*[\"']([^\"']+)", stripped, re.I)
            kind, identifier = "cli", next((item for item in (value.groups() if value else ()) if item), "代码中未确认")
        elif re.search(r"@OnMessage\b|(?:socketio|io)\.on\s*\(", stripped, re.I):
            value = re.search(r"\(\s*[\"']([^\"']+)", stripped)
            kind, identifier = "websocket", (value.group(1) if value else "代码中未确认")
        elif re.search(r"(?:FileListener|OnFile|FileSystemEventHandler|watchdog)", stripped, re.I):
            kind, identifier = "file", "代码中未确认"
        elif re.search(r"@Bean\b", stripped) and re.search(r"\bJob\s+[A-Za-z_]\w*\s*\(", " ".join(lines[index + 1:index + 6])):
            job = re.search(r"\bJob\s+([A-Za-z_]\w*)\s*\(", " ".join(lines[index + 1:index + 6]))
            kind, identifier = "batch", job.group(1) if job else "代码中未确认"
        if kind:
            if kind == "url" and re.search(r"event[-_]stream|SseEmitter|EventSource", annotation_text, re.I):
                kind = "sse"
            if kind == "url" and "webhook" in identifier.lower():
                kind = "webhook"
            handler = "代码中未确认"
            for candidate in lines[index + 1:index + 8]:
                match = re.search(
                    r"(?:async\s+)?def\s+([A-Za-z_]\w*)|\b(?:fun\s+)?([A-Za-z_]\w*)\s*\([^;{}]*\)\s*\{",
                    candidate,
                )
                if match:
                    handler = match.group(1) or match.group(2)
                    break
            if kind == "rpc" and identifier in {"Query", "Mutation", "Subscription"} and handler != "代码中未确认":
                identifier = f"{identifier}/{handler}"
            if kind == "message":
                topic_values = re.findall(
                    r"(?:topics|queues|destination|topic|value)\s*=\s*(?:\{([^}]*)\}|([\"'][^\"']+[\"']))",
                    annotation_text,
                    re.IGNORECASE,
                )
                identifiers = [
                    topic
                    for group in topic_values
                    for topic in re.findall(r"[\"']([^\"']+)[\"']", " ".join(group))
                ] or [identifier]
                found.extend((kind, topic, index + 1, handler) for topic in dict.fromkeys(identifiers))
            else:
                found.append((kind, identifier, index + 1, handler))
    for index, line in enumerate(lines):
        if line.lstrip().startswith("@"):
            continue
        match = re.search(
            r"\b(?:app|router|r|mux)\.(get|post|put|patch|delete)\s*\(\s*[\"']([^\"']+)[\"'](?P<tail>.*)",
            line,
            re.I,
        )
        if match:
            handler_match = re.search(r",\s*([A-Za-z_]\w*)\s*\)?\s*;?\s*$", match.group("tail"))
            found.append((
                "url", f"{match.group(1).upper()} {match.group(2)}", index + 1,
                handler_match.group(1) if handler_match else "代码中未确认",
            ))
        django = re.search(r"\b(?:path|re_path)\s*\(\s*[\"']([^\"']+)[\"']\s*,\s*([A-Za-z_]\w*)", line)
        if django and "urlpatterns" in text:
            found.append(("url", f"REQUEST {django.group(1)}", index + 1, django.group(2)))
        route = re.search(r"Route::(get|post|put|patch|delete)\s*\(\s*[\"']([^\"']+)", line, re.I)
        if route and not any(item[2] == index + 1 and item[1].endswith(route.group(2)) for item in found):
            found.append(("url", f"{route.group(1).upper()} {route.group(2)}", index + 1, "代码中未确认"))
        go_http = re.search(r"\b(?:http|mux)\.HandleFunc\s*\(\s*[\"']([^\"']+)[\"']\s*,\s*([A-Za-z_]\w*)", line)
        if go_http:
            found.append(("url", f"REQUEST {go_http.group(1)}", index + 1, go_http.group(2)))
        defaults = re.search(r"\.set_defaults\s*\([^)]*\bfunc\s*=\s*([A-Za-z_]\w*)", line)
        if defaults:
            prior = "\n".join(lines[max(0, index - 8):index + 1])
            command = re.findall(r"\.add_parser\s*\(\s*[\"']([^\"']+)", prior)
            found.append(("cli", command[-1] if command else "代码中未确认", index + 1, defaults.group(1)))
    call_pattern = re.compile(
        r"\b(?:app|router|blueprint|bp|r|mux)\.(get|post|put|patch|delete|options|head)\s*\(\s*[\"']([^\"']+)[\"'](?P<tail>[^)]*)\)",
        re.IGNORECASE | re.DOTALL,
    )
    for match in call_pattern.finditer(text):
        line = text[:match.start()].count("\n") + 1
        identifier = f"{match.group(1).upper()} {match.group(2)}"
        handler_match = re.search(r"(?:endpoint|view_func|handler)\s*=\s*([A-Za-z_]\w*)|,\s*([A-Za-z_]\w*)\s*(?:,|$)", match.group("tail"))
        handler = (handler_match.group(1) or handler_match.group(2)) if handler_match else "代码中未确认"
        existing = next((position for position, item in enumerate(found) if item[2] == line and item[1] == identifier), None)
        if existing is None:
            found.append(("url", identifier, line, handler))
        elif found[existing][3] == "代码中未确认" and handler != "代码中未确认":
            found[existing] = ("url", identifier, line, handler)
    if language == "Protocol Buffers":
        for service in re.finditer(r"\bservice\s+([A-Za-z_]\w*)\s*\{(?P<body>.*?)\}", text, re.DOTALL):
            for rpc in re.finditer(r"\brpc\s+([A-Za-z_]\w*)\s*\(", service.group("body")):
                line = text[:service.start("body") + rpc.start()].count("\n") + 1
                item = ("rpc", f"{service.group(1)}/{rpc.group(1)}", line, "代码中未确认")
                if not any(existing[0:3] == item[0:3] for existing in found):
                    found.append(item)
    if language == "GraphQL":
        for owner in re.finditer(r"\btype\s+(Query|Mutation|Subscription)\b[^\{]*\{(?P<body>.*?)\}", text, re.DOTALL):
            for field in re.finditer(r"^\s*([A-Za-z_]\w*)\s*(?:\([^\n]*\))?\s*:", owner.group("body"), re.MULTILINE):
                line = text[:owner.start("body") + field.start()].count("\n") + 1
                item = ("rpc", f"{owner.group(1)}/{field.group(1)}", line, "代码中未确认")
                if not any(existing[0:3] == item[0:3] for existing in found):
                    found.append(item)
    if language == "Python":
        guard = re.search(r"^\s*if\s+__name__\s*==\s*[\"']__main__[\"']\s*:", text, re.MULTILINE)
        if guard:
            following = text[guard.end():]
            call = re.search(r"^\s*([A-Za-z_]\w*)\s*\(", following, re.MULTILINE)
            handler = call.group(1) if call else "代码中未确认"
            item = ("cli", relative, text[:guard.start()].count("\n") + 1, handler)
            if not any(existing[0:3] == item[0:3] for existing in found):
                found.append(item)
    if language in {"Java", "Kotlin", "Scala", "Groovy"} and re.search(r"@GrpcService\b|\bImplBase\b|@DubboService\b", text):
        owner = re.search(r"\bclass\s+([A-Za-z_]\w*)", text)
        for method in re.finditer(
            r"@Override\s+(?:public\s+)?(?:suspend\s+)?[\w<>,.?\[\]]+\s+([A-Za-z_]\w*)\s*\(",
            text,
            re.DOTALL,
        ):
            item = (
                "rpc", f"{owner.group(1) if owner else '代码中未确认'}/{method.group(1)}",
                text[:method.start()].count("\n") + 1, method.group(1),
            )
            if not any(existing[0:3] == item[0:3] for existing in found):
                found.append(item)
    if language in {"Shell", "PowerShell", "Batch"} and any(
        part.lower() in {"bin", "commands", "jobs", "scripts", "tasks"} for part in Path(relative).parts
    ):
        kind = "batch" if any(part.lower() in {"jobs", "tasks"} for part in Path(relative).parts) else "cli"
        found.append((kind, relative, 1, Path(relative).name))
    return found


def _unrecognized_registration_lines(text: str, language: str, known_lines: set[int]) -> list[int]:
    if language in {"Protocol Buffers", "GraphQL", "Configuration"}:
        return []
    patterns = (
        r"@(?:Get|Post|Put|Patch|Delete)Mapping\b",
        r"@(?:Get|Post|Put|Patch|Delete|Options|Head)\s*(?:\(|$)",
        r"\b(?:app|router|blueprint|bp|r|mux)\.(?:get|post|put|patch|delete|route)\s*\(",
        r"@(?:KafkaListener|RabbitListener|JmsListener|PulsarListener|RocketMQMessageListener)\b",
        r"@(?:XxlJob|JobHandler|Scheduled|Cron)\b",
        r"@(?:EventListener|MessagePattern|OnEvent)\b",
        r"@(?:Controller|RestController|GrpcService|DubboService)\b",
        r"\b(?:websocket|WebSocket)\s*\(",
        r"\b(?:socketio|io)\.on\s*\(",
        r"\b(?:routes?|url_patterns?)\s*=\s*[\[{]",
    )
    marker = re.compile("|".join(patterns), re.IGNORECASE)
    return [index for index, line in enumerate(text.splitlines(), 1) if marker.search(line) and index not in known_lines]


def _configuration_entries(text: str, relative: str) -> list[tuple[str, str, int, str]]:
    name = Path(relative).name.lower()
    found: list[tuple[str, str, int, str]] = []

    def line_of(needle: str) -> int:
        return next((index for index, line in enumerate(text.splitlines(), 1) if needle in line), 1)

    try:
        if name == "pyproject.toml":
            document = tomllib.loads(text)
            project = document.get("project", {}) if isinstance(document, dict) else {}
            for group in ("scripts", "gui-scripts"):
                scripts = project.get(group, {}) if isinstance(project, dict) else {}
                if isinstance(scripts, dict):
                    for command, target in scripts.items():
                        handler = str(target).split(":")[-1].strip() or "代码中未确认"
                        found.append(("cli", str(command), line_of(str(command)), handler))
        elif name == "package.json":
            document = json.loads(text)
            bins = document.get("bin", {}) if isinstance(document, dict) else {}
            if isinstance(bins, str):
                bins = {str(document.get("name", "代码中未确认")): bins}
            if isinstance(bins, dict):
                for command, target in bins.items():
                    found.append(("cli", str(command), line_of(str(command)), str(target)))
    except (json.JSONDecodeError, tomllib.TOMLDecodeError, TypeError, ValueError):
        return []
    return found


def _frameworks(files: list[Path]) -> list[str]:
    names: set[str] = set()
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        checks = {
            "Spring": ("org.springframework", "@RestController", "@GetMapping"),
            "FastAPI": ("fastapi", "FastAPI(", "APIRouter("),
            "Flask": ("flask", "@app.route", "Blueprint("),
            "Express": ("express", "router.get(", "app.post("),
            "NestJS": ("@Controller", "@MessagePattern", "@Cron("),
            "Django": ("django", "urlpatterns", "path("),
            "gRPC": ("grpc", "GrpcService", "ImplBase"),
        }
        for name, needles in checks.items():
            if any(needle in text for needle in needles):
                names.add(name)
    return sorted(names)


def scan(project_root: Path, target: str | None = None) -> ScanResult:
    with source_view(project_root, target) as (root, git):
        files = [
            path for path in root.rglob("*")
            if path.is_file()
            and (path.suffix.lower() in EXTENSIONS or path.name.lower() in CONFIG_NAMES or path.suffix.lower() in CONFIG_SUFFIXES)
            and not any(part in EXCLUDED_DIRS for part in path.parts)
        ]
        entries: list[EntryPoint] = []
        unresolved: list[str] = []
        source_lines: dict[str, int] = {}
        language_names: set[str] = set()
        records: list[tuple[Path, str, str, list[FunctionInfo], dict[str, FunctionInfo], dict[str, ErrorEvidence]]] = []
        global_functions: dict[str, list[tuple[str, FunctionInfo]]] = {}
        global_mappings: dict[str, list[ErrorEvidence]] = {}
        source_texts: dict[str, str] = {}
        for path in files:
            language = EXTENSIONS.get(path.suffix.lower(), "Configuration")
            if language != "Configuration":
                language_names.add(language)
            relative = path.relative_to(root).as_posix()
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                unresolved.append(f"{relative}: cannot read source: {exc}")
                continue
            source_lines[relative] = len(text.splitlines())
            source_texts[relative] = text
            funcs = _functions(text, language, relative) if language != "Configuration" else []
            by_name = {item.name: item for item in funcs}
            mappings = _exception_mappings(text, relative)
            records.append((path, relative, text, funcs, by_name, mappings))
            for exception, mapping in mappings.items():
                global_mappings.setdefault(exception, []).append(mapping)
            for function in funcs:
                global_functions.setdefault(function.name, []).append((relative, function))

        for path, relative, text, funcs, by_name, mappings in records:
            language = EXTENSIONS.get(path.suffix.lower(), "Configuration")
            discovered = (
                _configuration_entries(text, relative)
                if language == "Configuration"
                else _decorated_entries(text, language, relative)
            )
            known_lines = {item[2] for item in discovered}
            for registration_line in _unrecognized_registration_lines(text, language, known_lines):
                registration_text = text.splitlines()[registration_line - 1]
                if discovered and (
                    re.search(r"(?:urlpatterns|routes?|url_patterns?)\s*=", registration_text, re.I)
                    or re.search(r"@(?:Controller|RestController|GrpcService|DubboService)\b", registration_text, re.I)
                ):
                    continue
                unresolved.append(
                    f"{relative}:{registration_line}: registered business entry could not be mapped to a handler"
                )
            if language == "Configuration":
                registration = re.compile(
                    r"^\s*(?:routes?|consumers?|listeners?|jobs?|commands?|workflows?|websockets?|grpc[_-]?services?)\s*[:=]|"
                    r"<\s*(?:route|consumer|listener|job|task|workflow)\b",
                    re.IGNORECASE,
                )
                for number, candidate in enumerate(text.splitlines(), 1):
                    if registration.search(candidate) and not discovered:
                        unresolved.append(
                            f"{relative}:{number}: possible business entry registration in configuration requires review"
                        )
            for kind, identifier, line, handler in discovered:
                local = by_name.get(handler)
                global_candidates = global_functions.get(handler, [])
                selected = local or (global_candidates[0][1] if len(global_candidates) == 1 else None)
                if kind == "url" and selected and re.search(
                    r"text/event-stream|SseEmitter|EventSource|StreamingResponse", selected.body, re.IGNORECASE
                ):
                    kind = "sse"
                errors: list[ErrorEvidence] = []
                functions: list[str] = []
                bodies: list[str] = []
                behaviors: list[BehaviorEvidence] = []
                if selected:
                    pending: list[tuple[str, str]] = [(relative, selected.name)]
                    seen: set[tuple[str, str]] = set()
                    while pending:
                        current_file, name = pending.pop(0)
                        key = (current_file, name)
                        if key in seen:
                            continue
                        seen.add(key)
                        candidates = [(current_file, by_name[name])] if current_file == relative and name in by_name else global_functions.get(name, [])
                        if not candidates:
                            if not _unresolved_call(name):
                                continue
                            unresolved.append(f"{relative}:{line}: called function {name} is outside this repository or not statically resolvable")
                            continue
                        if len(candidates) > 1:
                            unresolved.append(f"{relative}:{line}: call {name} has multiple possible definitions")
                        for candidate_file, function in candidates:
                            functions.append(f"{candidate_file}:{function.name}" if candidate_file != relative else function.name)
                            bodies.append(function.body)
                            errors.extend(function.errors)
                            behaviors.extend(
                                _declaration_behaviors(function, candidate_file, source_texts.get(candidate_file, ""))
                            )
                            behaviors.extend(_behaviors(function, candidate_file))
                            caught_calls = _caught_calls(function.body) | _c_style_caught_calls(function.body)
                            pending.extend(
                                (candidate_file, call)
                                for call in function.calls
                                if call not in caught_calls and (candidate_file, call) not in seen
                            )
                else:
                    unresolved.append(f"{relative}:{line}: handler for {identifier} is not statically resolvable")
                entry_id = f"{kind}:{identifier}:{relative}:{handler}"
                mapped_errors: list[ErrorEvidence] = []
                for error in errors:
                    if error.code == "代码中未确认":
                        exception = re.search(r"(?:raise|throw\s+new)\s+([A-Za-z_]\w*)", error.condition)
                        candidates = global_mappings.get(exception.group(1), []) if exception else []
                        if len(candidates) == 1:
                            mapping = candidates[0]
                            error = ErrorEvidence(
                                mapping.code,
                                f"{error.condition}；{mapping.condition}（映射证据：{mapping.file}:{mapping.line}）",
                                error.file,
                                error.line,
                            )
                        elif len(candidates) > 1:
                            unresolved.append(f"{relative}:{line}: exception {exception.group(1)} has multiple outward mappings")
                    mapped_errors.append(error)
                unique_errors: list[ErrorEvidence] = []
                seen_errors: set[tuple[str, str, int]] = set()
                for error in mapped_errors:
                    key = (error.code, error.file, error.line)
                    if key not in seen_errors:
                        seen_errors.add(key)
                        unique_errors.append(error)
                for error in unique_errors:
                    if error.code == "代码中未确认":
                        unresolved.append(
                            f"{error.file}:{error.line}: active error code for {identifier} is not statically resolvable"
                        )
                if "代码中未确认" in identifier:
                    unresolved.append(f"{relative}:{line}: business entry identifier is not statically resolvable")
                entries.append(EntryPoint(
                    entry_id=entry_id,
                    kind=kind,
                    identifier=identifier,
                    handler=handler,
                    file=relative,
                    line=line,
                    module=_module_name(Path(relative), identifier),
                    source=relative,
                    functions=functions,
                    errors=unique_errors,
                    behaviors=behaviors,
                    caller={
                        "url": "HTTP 客户端", "webhook": "Webhook 调用方", "websocket": "WebSocket 客户端",
                        "sse": "SSE 客户端",
                        "rpc": "RPC 客户端", "message": "消息生产方", "scheduled": "调度系统",
                        "event": "事件发布方", "cli": "命令行调用方", "file": "文件提供方",
                        "batch": "批处理调度方",
                    }.get(kind, "代码中未确认"),
                    input_summary=(selected.body.splitlines()[0].strip() if selected else "代码中未确认"),
                    has_loop=bool(bodies and re.search(r"\b(for|while|foreach)\b", "\n".join(bodies))),
                    has_external_call=bool(bodies and re.search(r"\b(requests|httpx|urllib|RestTemplate|WebClient|grpc|axios|fetch)\b", "\n".join(bodies), re.I)),
                    has_persistence=bool(bodies and re.search(r"\b(save|insert|update|delete|persist|repository|dao|\.create\s*\()", "\n".join(bodies), re.I)),
                    has_async=bool(bodies and re.search(r"\b(async|await|thread|executor|queue|publish|send)\b", "\n".join(bodies), re.I)),
                ))
        entries.sort(key=lambda item: (item.module, item.kind, item.identifier, item.file, item.line))
        digest = hashlib.sha256()
        for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
            relative = path.relative_to(root).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            try:
                digest.update(path.read_bytes())
            except OSError as exc:
                unresolved.append(f"{relative}: cannot fingerprint source: {exc}")
        source_fingerprint = digest.hexdigest()
        return ScanResult(
            root=project_root.resolve(),
            git=git,
            languages=sorted(language_names),
            frameworks=_frameworks(files),
            entries=entries,
            files=[path.relative_to(root).as_posix() for path in files],
            unresolved=sorted(dict.fromkeys(unresolved)),
            source_fingerprint=source_fingerprint,
            source_lines=source_lines,
        )
