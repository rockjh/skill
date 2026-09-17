"""Source, SQL, credential, pytest, diagram, and project checks."""

from __future__ import annotations

import ast
import configparser
import json
import re
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from .contracts import (
    CONTROL_NAMES,
    SQL_RE,
    URL_RE,
    _scenario_directories,
    _source_reference_exists,
    contract_errors,
)
from .discovery import (
    PLACEHOLDER_RE,
    REFERENCE_RE,
    SKIP_DIRS,
    _contains_usable_credential_text,
    _error,
    _git,
    _load_yaml,
    _resolve,
    _safe_credential_literal,
    _secret_errors,
    _semantic_evidence,
    _sensitive_name,
    _walk_files,
)


MUTATING_SQL_RE = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE|ALTER|DROP|TRUNCATE|REPLACE)\b", re.IGNORECASE)


PARAMETER_RE = re.compile(r"(\?|%s|%\([A-Za-z_][A-Za-z0-9_]*\)s|:[A-Za-z_][A-Za-z0-9_]*|\$[1-9][0-9]*)")


CHINESE_RE = re.compile(r"[\u3400-\u9fff]")


def _asset_errors(project_root: Path) -> list[str]:
    """Reject the removed project-local gate bundle."""

    manifest_path = project_root / ".e2e-gates.json"
    if not manifest_path.exists():
        return []
    return [_error(manifest_path, "legacy-gate-bundle", "project-local dev-ai gate sources are forbidden")]


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
    is_gate_engine = False
    is_fixed_gate_asset = False
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
    if any(name in source for name in ("E2E_EVIDENCE_DIR", "E2E_RUN_ID")):
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
            if not is_gate_engine and leaf in {"_emit_event", "_record_restoration"}:
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
    """Reject launchers from the removed copied-script execution mode."""

    scripts = project_root / "scripts"
    if scripts.is_dir():
        return [
            _error(path, "project-tool-source-forbidden", "run E2E gates through installed dev-ai")
            for path in sorted(item for item in scripts.rglob("*") if item.is_file())
        ]
    return []


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


__all__ = ["static_errors"]
