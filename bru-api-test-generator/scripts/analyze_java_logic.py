#!/usr/bin/env python3
"""Find API-reachable Java/Spring logic through a lightweight call graph."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

CLASS_RE = re.compile(r"\b(?:class|interface|enum)\s+(\w+)")
METHOD_RE = re.compile(
    r"\b(?:public|protected|private)\s+(?:static\s+)?(?:final\s+)?"
    r"[\w.$<>?,\[\]\s]+?\s+(?P<name>\w+)\s*"
    r"\((?P<parameters>(?:[^()\"']|\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|\([^()]*\))*)\)\s*"
    r"(?:throws\s+[\w.$<>,?\s]+)?\{",
    re.MULTILINE,
)
MAPPING_RE = re.compile(r"@(Get|Post|Put|Patch|Delete|Request)Mapping\b", re.IGNORECASE)
MAPPING_ANNOTATION_RE = re.compile(
    r"@(Get|Post|Put|Patch|Delete|Request)Mapping\b(?:\s*\((.*?)\))?",
    re.IGNORECASE | re.DOTALL,
)
FIELD_RE = re.compile(
    r"\b(?:private|protected|public)\s+(?:static\s+)?(?:final\s+)?"
    r"([A-Z]\w*(?:<[^;=]+>)?)\s+(\w+)\s*[;=]"
)
PARAMETER_RE = re.compile(r"(?:final\s+)?(?:@[\w.]+(?:\([^)]*\))?\s+)*([A-Z]\w*)\s+(\w+)\b")
QUALIFIED_CALL_RE = re.compile(r"\b([a-zA-Z_]\w*)\.(\w+)\s*\(")
PLAIN_CALL_RE = re.compile(r"(?<![.@\w])([a-zA-Z_]\w*)\s*\(")
THROWN_EXCEPTION_RE = re.compile(r"\bthrow\s+new\s+([A-Za-z_]\w*Exception)\b")
NUMERIC_CODE_RE = re.compile(r"(?<!\d)([1-9]\d{3,8})(?!\d)")
VALIDATION_RE = re.compile(r"@(NotNull|NotBlank|NotEmpty|Size|Length|Pattern|Email|Min|Max|Positive|Negative)\b")
AUTHORIZATION_RE = re.compile(r"(?:PreAuthorize|RequiresPermissions|Secured|hasRole|hasAuthority)", re.IGNORECASE)
HEADER_RE = re.compile(r"(?:RequestHeader|getHeader)\s*\([^\n]*?[\"']([A-Za-z][A-Za-z0-9-]*)[\"']", re.IGNORECASE)
OBSERVABLE_BRANCH_RE = re.compile(
    r"\b(?:if|switch|case|catch)\b[^\n]*(?:throw|error|fail|forbidden|unauthor|duplicate|exists|notFound|status|code)",
    re.IGNORECASE,
)
FILE_CONSTRAINT_RE = re.compile(
    r"\b(?:MultipartFile|isEmpty\s*\(|getSize\s*\(|getOriginalFilename\s*\(|"
    r"EasyExcel|ExcelReader|header|column|sheet|fileSize|maxFileSize)\b",
    re.IGNORECASE,
)
EXCEPTION_HANDLER_RE = re.compile(r"@ExceptionHandler\s*\((.*?)\)", re.DOTALL)
ERROR_CODE_TYPE_RE = re.compile(r"\b([A-Z]\w*(?:ErrorCode(?:Enum)?|ErrorCodes))\b")
ERROR_CODE_VALUE_RE = re.compile(
    r"\b([A-Z][A-Z0-9_]*)\s*\(\s*(?:\"([^\"]+)\"|'([^']+)'|([A-Za-z0-9_.-]+))"
)
HTTP_STATUS_VALUES = {
    "OK": 200, "BAD_REQUEST": 400, "UNAUTHORIZED": 401, "FORBIDDEN": 403,
    "NOT_FOUND": 404, "CONFLICT": 409, "UNPROCESSABLE_ENTITY": 422,
    "INTERNAL_SERVER_ERROR": 500,
}
SKIP_PARTS = {".git", "qa", "target", "build", "node_modules", "vendor", ".venv", "venv", "test", "tests"}
JAVA_KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "throw", "new", "super", "this", "synchronized"}


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", lambda match: "\n" * match.group(0).count("\n"), text, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", text)


def _matching_brace(text: str, opening: int) -> int:
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(opening, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return len(text)


def _skip(path: Path) -> bool:
    lowered = {part.lower() for part in path.parts}
    return bool(lowered & SKIP_PARTS) or path.stem.endswith(("Test", "Tests", "ArchitectureTest", "ArchTest"))


def _candidate_id(kind: str, symbol: str, line: int, evidence: str) -> str:
    digest = hashlib.sha1(f"{kind}|{symbol}|{line}|{evidence}".encode("utf-8")).hexdigest()[:12]
    return f"{kind.upper()}_{digest}"


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _leading_annotations(text: str, offset: int) -> str:
    boundary = -1
    quote: str | None = None
    escaped = False
    parentheses = 0
    for index, char in enumerate(text[:offset]):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "(":
            parentheses += 1
        elif char == ")":
            parentheses = max(0, parentheses - 1)
        elif char == ";" or (char == "}" and parentheses == 0):
            boundary = index
    prefix = text[boundary + 1:offset]
    class_declaration = list(CLASS_RE.finditer(prefix))
    if class_declaration:
        opening = prefix.find("{", class_declaration[-1].end())
        if opening >= 0:
            prefix = prefix[opening + 1:]
    return prefix if "@" in prefix else ""


def _mapping_paths(annotation: str) -> list[str]:
    body_match = re.search(r"\((.*)\)", annotation, re.DOTALL)
    if not body_match:
        return [""]
    body = body_match.group(1)
    named = re.search(
        r"\b(?:value|path)\s*=\s*(\{.*?\}|[\"\'][^\"\']*[\"\'])",
        body,
        re.DOTALL,
    )
    source = named.group(1) if named else body
    if not named and not re.match(r"\s*(?:\{\s*)?[\"\']", source):
        return [""]
    paths = [value.strip() for value in re.findall(r'[\"\']([^\"\']*)[\"\']', source)]
    return list(dict.fromkeys(paths)) or [""]


def _join_route(prefix: str, suffix: str) -> str:
    value = "/" + "/".join(part.strip("/") for part in (prefix, suffix) if part.strip("/"))
    return re.sub(r"/{2,}", "/", value) or "/"


def _mapping_keys(class_annotations: str, method_annotations: str) -> list[str]:
    class_paths = [""]
    class_match = next(MAPPING_ANNOTATION_RE.finditer(class_annotations), None)
    if class_match:
        class_paths = _mapping_paths(class_match.group(0))
    keys: list[str] = []
    for match in MAPPING_ANNOTATION_RE.finditer(method_annotations):
        kind = match.group(1).lower()
        annotation = match.group(0)
        methods = [kind.upper()] if kind != "request" else re.findall(r"RequestMethod\.(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)", annotation)
        methods = methods or ["*"]
        for prefix in class_paths:
            for suffix in _mapping_paths(annotation):
                route = _join_route(prefix, suffix)
                keys.extend(f"{method.upper()} {route}" for method in methods)
    return list(dict.fromkeys(keys))


def _family_prefix(name: str) -> str:
    return re.sub(r"(?:Application|Business|Biz|Domain)?(?:Exception|ErrorCodeEnum|ErrorCode|ErrorCodes)$", "", name)


def _discover_exception_family(
    files: list[tuple[Path, str, str]],
    exception_override: str | None,
    error_code_override: str | None,
) -> tuple[str | None, str | None, list[str], str | None]:
    class_names = {class_name for _, class_name, _ in files}
    exception_codes: dict[str, set[str]] = {}
    handled: set[str] = set()
    for _, class_name, text in files:
        if "ControllerAdvice" in text or "RestControllerAdvice" in text:
            for handler in EXCEPTION_HANDLER_RE.findall(text):
                handled.update(re.findall(r"\b([A-Z]\w*Exception)\s*\.class\b", handler))
        if class_name.endswith("Exception"):
            for parameters in re.findall(rf"\b{re.escape(class_name)}\s*\(([^)]*)\)", text, re.DOTALL):
                exception_codes.setdefault(class_name, set()).update(ERROR_CODE_TYPE_RE.findall(parameters))

    thrown = {
        name
        for _, _, text in files
        for name in THROWN_EXCEPTION_RE.findall(text)
    }
    exceptions = {name for name in class_names if name.endswith("Exception")} | handled | thrown
    code_types = {name for name in class_names if ERROR_CODE_TYPE_RE.fullmatch(name)}
    if exception_override and exception_override not in exceptions:
        return None, None, [f"configured exception type does not exist: {exception_override}"], None
    if error_code_override and error_code_override not in code_types:
        return None, None, [f"configured error-code type does not exist: {error_code_override}"], None

    def pairs(exception_names: set[str]) -> set[tuple[str, str]]:
        found: set[tuple[str, str]] = set()
        for exception_name in exception_names:
            direct = exception_codes.get(exception_name, set())
            for code_name in direct:
                if code_name in code_types:
                    found.add((exception_name, code_name))
            if not direct:
                prefix = _family_prefix(exception_name)
                for code_name in code_types:
                    if prefix and _family_prefix(code_name) == prefix:
                        found.add((exception_name, code_name))
        return found

    candidates: set[tuple[str, str]]
    source: str | None = None
    if exception_override or error_code_override:
        candidates = {
            pair for pair in pairs({exception_override} if exception_override else exceptions)
            if (not exception_override or pair[0] == exception_override)
            and (not error_code_override or pair[1] == error_code_override)
        }
        if exception_override and error_code_override:
            candidates.add((exception_override, error_code_override))
        source = "explicit"
    else:
        candidates = pairs(handled)
        source = "controller-advice"
        if not candidates:
            candidates = {
                (exception_name, code_type)
                for exception_name, names in exception_codes.items()
                for code_type in names
                if exception_name in exceptions and code_type in code_types
            }
            source = "exception-constructor"
        if not candidates:
            candidates = pairs(exceptions)
            source = "unique-name-pair"
    if len(candidates) > 1:
        rendered = ", ".join(f"{exception}+{code}" for exception, code in sorted(candidates))
        return None, None, [f"multiple application exception families found: {rendered}; configure --exception-type and --error-code-type"], source
    if not candidates:
        return None, None, [], None
    exception_name, code_name = next(iter(candidates))
    return exception_name, code_name, [], source


def scan(
    roots: list[Path],
    exception_type: str | None = None,
    error_code_type: str | None = None,
) -> dict[str, Any]:
    files: list[tuple[Path, str, str]] = []
    mapping_annotation_count = 0
    for root in roots:
        for path in sorted(root.rglob("*.java")):
            if _skip(path) or not path.is_file():
                continue
            try:
                raw = path.read_text(encoding="utf-8", errors="strict")
            except (OSError, UnicodeDecodeError):
                continue
            text = _strip_comments(raw)
            class_match = CLASS_RE.search(text)
            if not class_match:
                continue
            mapping_annotation_count += len(MAPPING_RE.findall(text))
            files.append((path, class_match.group(1), text))

    exception_name, code_name, discovery_errors, discovery_source = _discover_exception_family(
        files, exception_type, error_code_type
    )
    error_codes: dict[str, str] = {}
    if code_name:
        for _, class_name, text in files:
            if class_name == code_name:
                for match in ERROR_CODE_VALUE_RE.finditer(text):
                    error_codes[match.group(1)] = next(value for value in match.groups()[1:] if value is not None)

    methods: dict[str, dict[str, Any]] = {}
    by_name: dict[str, list[str]] = {}
    receiver_types: dict[str, dict[str, str]] = {}
    entrypoints: dict[str, list[str]] = {}
    for path, class_name, text in files:
        receiver_types[class_name] = {
            name: re.sub(r"<.*", "", type_name)
            for type_name, name in FIELD_RE.findall(text)
        }
        class_match = CLASS_RE.search(text)
        class_annotations = _leading_annotations(text, class_match.start()) if class_match else ""
        for match in METHOD_RE.finditer(text):
            opening = text.find("{", match.start())
            closing = _matching_brace(text, opening)
            name = match.group("name")
            symbol = f"{class_name}.{name}"
            annotations = _leading_annotations(text, match.start())
            parameters = {name: type_name for type_name, name in PARAMETER_RE.findall(match.group("parameters") or "")}
            methods[symbol] = {
                "path": path,
                "class": class_name,
                "name": name,
                "annotations": annotations,
                "parameters": parameters,
                "body": text[opening + 1:closing],
                "body_offset": opening + 1,
                "text": text,
            }
            by_name.setdefault(name, []).append(symbol)
            mapping_keys = _mapping_keys(class_annotations, annotations)
            if mapping_keys:
                entrypoints[symbol] = mapping_keys

    errors = list(discovery_errors)
    if mapping_annotation_count and not entrypoints:
        errors.append(f"found {mapping_annotation_count} Spring Mapping annotation(s) but recognized 0 controller entrypoints")

    graph: dict[str, set[str]] = {symbol: set() for symbol in methods}
    for symbol, method in methods.items():
        receivers = {**receiver_types.get(method["class"], {}), **method.get("parameters", {})}
        for receiver, called in QUALIFIED_CALL_RE.findall(method["body"]):
            owner = receivers.get(receiver) or (method["class"] if receiver == "this" else "")
            target = f"{owner}.{called}"
            if target in methods:
                graph[symbol].add(target)
            elif len(by_name.get(called, [])) == 1:
                graph[symbol].add(by_name[called][0])
        for receiver, argument in re.findall(r"\b(\w+)\.apply\s*\(\s*(\w+)\b", method["body"]):
            receiver_type = receivers.get(receiver, "")
            argument_type = receivers.get(argument, "")
            if receiver_type.endswith("ApplicationDelegator") and f"{argument_type}.doServe" in methods:
                graph[symbol].add(f"{argument_type}.doServe")
        for receiver, application_type in re.findall(
            r"\b(\w+)\.apply\s*\(\s*([A-Z]\w*)\.class\b", method["body"]
        ):
            if receivers.get(receiver, "").endswith("ApplicationDelegator") and f"{application_type}.doServe" in methods:
                graph[symbol].add(f"{application_type}.doServe")
        for receiver, application_type in re.findall(
            r"\b(\w+)\.apply\s*\(\s*new\s+([A-Z]\w*)\b", method["body"]
        ):
            if receivers.get(receiver, "").endswith("ApplicationDelegator") and f"{application_type}.doServe" in methods:
                graph[symbol].add(f"{application_type}.doServe")
        for called in PLAIN_CALL_RE.findall(method["body"]):
            if called in JAVA_KEYWORDS:
                continue
            local = f"{method['class']}.{called}"
            if local in methods:
                graph[symbol].add(local)
            elif len(by_name.get(called, [])) == 1:
                graph[symbol].add(by_name[called][0])

    reachable_from: dict[str, set[str]] = {symbol: set() for symbol in methods}
    for entrypoint, endpoint_keys in entrypoints.items():
        pending = [entrypoint]
        seen: set[str] = set()
        while pending:
            symbol = pending.pop()
            if symbol in seen:
                continue
            seen.add(symbol)
            reachable_from[symbol].update(endpoint_keys)
            pending.extend(graph.get(symbol, set()) - seen)

    operation_ids_by_key = {
        key: methods[symbol]["name"]
        for symbol, keys in entrypoints.items()
        for key in keys
    }
    code_re = re.compile(rf"\b{re.escape(code_name)}\.([A-Z][A-Z0-9_]*)\b") if code_name else None
    candidates: list[dict[str, Any]] = []
    seen_candidates: set[tuple[str, str, int, str]] = set()
    for symbol, method in methods.items():
        endpoint_keys = sorted(reachable_from.get(symbol, set()))
        if not endpoint_keys:
            continue
        blocks = [("normal_entrypoint", method["annotations"])] if symbol in entrypoints else []
        blocks.append(("body", method["body"]))
        for block_kind, block in blocks:
            base_offset = method["text"].find(block) if block_kind == "normal_entrypoint" else method["body_offset"]
            for line_offset, line in enumerate(block.splitlines()):
                excerpt = " ".join(line.strip().split())[:300]
                if not excerpt:
                    continue
                thrown = THROWN_EXCEPTION_RE.search(line)
                selected_exception = bool(thrown and exception_name and thrown.group(1) == exception_name)
                selected_code = bool(code_re and code_re.search(line))
                kind: str | None = None
                coverage_required = False
                if block_kind == "normal_entrypoint" and MAPPING_RE.search(line):
                    kind = "normal_entrypoint"
                elif HEADER_RE.search(line):
                    kind, coverage_required = "required_header", True
                elif AUTHORIZATION_RE.search(line):
                    kind, coverage_required = "authorization", True
                elif selected_exception or selected_code:
                    kind, coverage_required = "business_exception", True
                elif FILE_CONSTRAINT_RE.search(line):
                    kind, coverage_required = "file_exception", True
                elif VALIDATION_RE.search(line):
                    kind, coverage_required = "validation", True
                elif OBSERVABLE_BRANCH_RE.search(line):
                    kind, coverage_required = "observable_branch", True
                if not kind:
                    continue
                line_no = _line_number(method["text"], base_offset) + line_offset
                key = (kind, symbol, line_no, excerpt)
                if key in seen_candidates:
                    continue
                seen_candidates.add(key)
                candidate: dict[str, Any] = {
                    "id": _candidate_id(kind, symbol, line_no, excerpt),
                    "kind": kind,
                    "file": str(method["path"]),
                    "line": line_no,
                    "language": "java",
                    "symbol": symbol,
                    "source_excerpt": excerpt,
                    "needs_case": coverage_required,
                    "coverage_required": coverage_required,
                    "endpoint_keys": endpoint_keys,
                    "endpoint_operation_ids": list(dict.fromkeys(operation_ids_by_key[key] for key in endpoint_keys)),
                    "source_evidence": {
                        "source_kind": kind,
                        "file": str(method["path"]),
                        "symbol": symbol,
                        "line": line_no,
                        "endpoint_scope": endpoint_keys,
                        "confidence": "high",
                    },
                }
                header = HEADER_RE.search(line)
                if header:
                    candidate["required_header"] = header.group(1)
                codes = [error_codes.get(name, name) for name in (code_re.findall(line) if code_re else [])]
                codes.extend(NUMERIC_CODE_RE.findall(line))
                if codes:
                    candidate["expected_business_codes"] = list(dict.fromkeys(codes))
                if selected_exception and thrown:
                    candidate["exception_type"] = thrown.group(1)
                if kind == "file_exception":
                    lowered = line.casefold()
                    candidate["fixture_type"] = (
                        "empty" if "isempty" in lowered
                        else "oversized" if "getsize" in lowered or "maxfilesize" in lowered or "filesize" in lowered
                        else "missing-column" if "column" in lowered or "header" in lowered
                        else "invalid-content"
                    )
                candidates.append(candidate)
    exception_handlers: list[dict[str, Any]] = []
    reachable_scopes = sorted({
        scope
        for candidate in candidates
        if candidate.get("kind") == "business_exception"
        for scope in candidate.get("endpoint_keys", [])
    })
    for path, class_name, text in files:
        if "ControllerAdvice" not in text and "RestControllerAdvice" not in text:
            continue
        for match in METHOD_RE.finditer(text):
            annotations = _leading_annotations(text, match.start())
            handler_match = EXCEPTION_HANDLER_RE.search(annotations)
            if not handler_match:
                continue
            opening = text.find("{", match.start())
            closing = _matching_brace(text, opening)
            body = text[opening + 1:closing]
            status_name = next(iter(re.findall(r"HttpStatus\.([A-Z_]+)", annotations + body)), None)
            numeric_status = next(iter(re.findall(r"(?:ResponseStatus|status)\s*\(\s*(\d{3})", annotations + body)), None)
            http_status = int(numeric_status) if numeric_status else HTTP_STATUS_VALUES.get(str(status_name), 200)
            combined = annotations + "\n" + body
            business_code_path = (
                "$.errorCode" if re.search(r"(?:setErrorCode|\berrorCode\b)", combined, re.IGNORECASE)
                else "$.code" if re.search(r"(?:setCode|\bcode\b)", combined, re.IGNORECASE)
                else None
            )
            exceptions = re.findall(r"\b([A-Z]\w*Exception)\s*\.class\b", handler_match.group(1))
            handler_codes = [error_codes.get(name, name) for name in (code_re.findall(body) if code_re else [])]
            handler_codes.extend(NUMERIC_CODE_RE.findall(body))
            exception_handlers.append({
                "id": f"{class_name}.{match.group('name')}",
                "exception_types": exceptions,
                "http_status": http_status,
                "business_code_path": business_code_path,
                "response_structure": list(dict.fromkeys(re.findall(r"\bset([A-Z]\w*)\s*\(", body))),
                "business_codes": list(dict.fromkeys(handler_codes)),
                "evidence": {
                    "source_kind": "controller_advice",
                    "file": str(path),
                    "symbol": f"{class_name}.{match.group('name')}",
                    "line": _line_number(text, match.start()),
                    "endpoint_scope": reachable_scopes or sorted({scope for values in entrypoints.values() for scope in values}),
                    "confidence": "high",
                },
            })
    return {
        "version": 2,
        "source": {"roots": [str(root) for root in roots], "language": "java"},
        "exception_family": {
            "exception_type": exception_name,
            "error_code_type": code_name,
            "source": discovery_source,
        },
        "mapping_annotation_count": mapping_annotation_count,
        "entrypoint_count": len(entrypoints),
        "errors": errors,
        "candidates": candidates,
        "exception_profile": {"version": 1, "handlers": exception_handlers},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_roots", type=Path, nargs="+")
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--exception-type")
    parser.add_argument("--error-code-type")
    args = parser.parse_args()
    missing = [str(root) for root in args.source_roots if not root.is_dir()]
    if missing:
        parser.error(f"source root(s) do not exist: {', '.join(missing)}")
    result = scan(args.source_roots, args.exception_type, args.error_code_type)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for error in result["errors"]:
        print(f"ERROR: {error}", file=sys.stderr)
    print(f"wrote {len(result['candidates'])} API-reachable Java logic candidates to {args.output}")
    return 2 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
