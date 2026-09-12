#!/usr/bin/env python3
"""Extract a small, deterministic endpoint inventory from a local OpenAPI file."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


HTTP_METHODS = {
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "head",
    "options",
    "trace",
}


def load_document(path: Path) -> dict[str, Any]:
    try:
        if path.suffix.lower() == ".json":
            return json.loads(path.read_text(encoding="utf-8"))
        import yaml  # type: ignore[import-not-found]

        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("the OpenAPI document must be an object")
        return loaded
    except ModuleNotFoundError as exc:
        raise SystemExit("YAML input requires an existing PyYAML installation") from exc
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(f"cannot parse {path}: {exc}") from exc


def stable_id(method: str, path: str, operation: dict[str, Any]) -> str:
    operation_id = operation.get("operationId")
    if isinstance(operation_id, str) and operation_id.strip():
        raw = f"{operation_id}_{method}_{path}"
    else:
        raw = f"{method}_{path}"
    value = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_")
    return value.upper() or "ENDPOINT"


def parameter_summary(parameter: Any) -> dict[str, Any]:
    if not isinstance(parameter, dict):
        return {"raw": parameter}
    result = {
        "name": parameter.get("name"),
        "in": parameter.get("in"),
        "required": bool(parameter.get("required", False)),
    }
    if "schema" in parameter:
        result["schema"] = parameter["schema"]
    elif "type" in parameter:
        result["type"] = parameter["type"]
    return result


def extract(path: Path, document: dict[str, Any]) -> dict[str, Any]:
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise SystemExit("OpenAPI document has no object-valued paths field")

    version = document.get("openapi") or document.get("swagger") or "unknown"
    endpoints: list[dict[str, Any]] = []
    for route, path_item in paths.items():
        if not isinstance(route, str) or not isinstance(path_item, dict):
            continue
        inherited_parameters = path_item.get("parameters", [])
        for method, operation in path_item.items():
            if method.lower() not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            parameters = []
            for item in [*inherited_parameters, *operation.get("parameters", [])]:
                parameters.append(parameter_summary(item))
            body = operation.get("requestBody")
            if body is None:
                body = next(
                    (item for item in operation.get("parameters", [])
                     if isinstance(item, dict) and item.get("in") == "body"),
                    None,
                )
            endpoints.append(
                {
                    "id": stable_id(method.upper(), route, operation),
                    "method": method.upper(),
                    "path": route,
                    "operation_id": operation.get("operationId"),
                    "tags": operation.get("tags", []),
                    "summary": operation.get("summary"),
                    "parameters": parameters,
                    "request_body": body,
                    "responses": operation.get("responses", {}),
                    "security": operation.get("security", document.get("security")),
                }
            )

    endpoints.sort(key=lambda item: (item["path"], item["method"]))
    return {
        "version": 1,
        "source": {"file": str(path), "spec_version": str(version)},
        "endpoints": endpoints,
    }


def module_matches(endpoint: dict[str, Any], module: dict[str, Any]) -> int:
    tags = set(endpoint.get("tags") or [])
    module_tags = set(module.get("swagger_tags") or module.get("tags") or [])
    tag_score = 1000 if tags.intersection(module_tags) else 0
    path = str(endpoint.get("path", ""))
    prefixes = [str(item).rstrip("/") or "/" for item in module.get("path_prefixes", [])]
    prefix_lengths = [
        len(prefix)
        for prefix in prefixes
        if (path == "/" if prefix == "/" else path == prefix or path.startswith(prefix + "/"))
    ]
    if not tag_score and not prefix_lengths:
        return 0
    return tag_score + max(prefix_lengths or [0])


def partition_manifest(manifest: dict[str, Any], module_map: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    modules = module_map.get("modules") if isinstance(module_map, dict) else None
    if not isinstance(modules, list):
        raise SystemExit("module map must contain a modules list")
    grouped = {str(module.get("id")): [] for module in modules if module.get("id")}
    if len(grouped) != len(modules):
        raise SystemExit("every module map entry must have a unique id")

    for endpoint in manifest["endpoints"]:
        matches = [
            (module_id, module_matches(endpoint, module))
            for module_id, module in ((str(item["id"]), item) for item in modules)
        ]
        best_score = max((score for _, score in matches), default=0)
        best = [module_id for module_id, score in matches if score == best_score and score > 0]
        if len(best) != 1:
            if not best:
                raise SystemExit(
                    f"endpoint {endpoint['method']} {endpoint['path']} is not assigned to a module"
                )
            raise SystemExit(
                f"endpoint {endpoint['method']} {endpoint['path']} matches multiple primary modules: {', '.join(best)}"
            )
        endpoint["module"] = best[0]
        grouped[best[0]].append(endpoint)
    return grouped


def write_partitioned(manifest: dict[str, Any], module_map_path: Path, output_dir: Path) -> None:
    module_map = load_document(module_map_path)
    grouped = partition_manifest(manifest, module_map)
    output_dir.mkdir(parents=True, exist_ok=True)
    index = {
        "version": 1,
        "source": manifest["source"],
        "module_map": str(module_map_path),
        "modules": [],
    }
    for module_id, endpoints in sorted(grouped.items()):
        module_dir = output_dir / module_id
        module_dir.mkdir(parents=True, exist_ok=True)
        module_manifest = {
            "version": 1,
            "module": module_id,
            "source": manifest["source"],
            "endpoints": endpoints,
        }
        (module_dir / "endpoints.yaml").write_text(
            render_manifest(module_manifest, module_dir / "endpoints.yaml"),
            encoding="utf-8",
        )
        index["modules"].append(
            {
                "id": module_id,
                "endpoints_file": str((module_dir / "endpoints.yaml").relative_to(output_dir.parent)),
                "logic_file": str((module_dir / "logic.yaml").relative_to(output_dir.parent)),
                "cases_file": str((module_dir / "cases.yaml").relative_to(output_dir.parent)),
                "flows_file": str((module_dir / "flows.yaml").relative_to(output_dir.parent)),
                "endpoint_count": len(endpoints),
                "case_count": 0,
            }
        )
    (output_dir.parent / "index.yaml").write_text(
        render_manifest(index, output_dir.parent / "index.yaml"),
        encoding="utf-8",
    )


def render_manifest(manifest: dict[str, Any], output: Path | None) -> str:
    if output and output.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore[import-not-found]
        except ModuleNotFoundError as exc:
            raise SystemExit("YAML output requires an existing PyYAML installation") from exc
        return yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False)
    return json.dumps(manifest, ensure_ascii=True, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path, help="local JSON or YAML OpenAPI/Swagger file")
    parser.add_argument("-o", "--output", type=Path, help="write JSON manifest to this file")
    parser.add_argument("--module-map", type=Path, help="module mapping YAML/JSON")
    parser.add_argument("--output-dir", type=Path, help="write one endpoint manifest per module")
    args = parser.parse_args()

    if not args.spec.is_file():
        parser.error(f"offline specification does not exist: {args.spec}")
    if bool(args.module_map) != bool(args.output_dir):
        parser.error("--module-map and --output-dir must be provided together")
    if args.output and args.output_dir:
        parser.error("--output and --output-dir cannot be combined")
    manifest = extract(args.spec, load_document(args.spec))
    if args.module_map:
        if not args.module_map.is_file():
            parser.error(f"module map does not exist: {args.module_map}")
        write_partitioned(manifest, args.module_map, args.output_dir)
        module_count = len(load_document(args.module_map).get("modules", []))
        print(f"wrote {len(manifest['endpoints'])} endpoints across {module_count} modules")
        return 0
    rendered = render_manifest(manifest, args.output)
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
