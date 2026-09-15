from __future__ import annotations

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml


ROOT = Path(__file__).parents[1]
os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
sys.dont_write_bytecode = True


def load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    return module


def static_preflight_inputs(root: Path) -> tuple[Path, Path]:
    openapi = root / "openapi.json"
    static_results = root / "static-coverage.json"
    openapi.write_text('{"openapi":"3.0.0","paths":{}}', encoding="utf-8")
    digest = hashlib.sha256(openapi.read_bytes()).hexdigest()
    static_results.write_text(json.dumps({"static_ready": True, "openapi_sha256": digest}), encoding="utf-8")
    return openapi, static_results


def execution_fixture(
    root: Path,
    sign: str = "disabled",
    headers: dict[str, str] | None = None,
    environment: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    execution = root / "execution"
    environments = execution / "environments"
    environments.mkdir(parents=True, exist_ok=True)
    config = execution / "config.yaml"
    config.write_text(json.dumps({
        "active_environment": "local",
        "tooling": "project-scripts",
        "coverage_profile": "full-matrix",
        "sign": {"provider": "seres", "version": "v1"} if sign in {"seres", "seres-sign"} else {"provider": "disabled"},
    }, ensure_ascii=False), encoding="utf-8")
    env_file = environments / "local.bru"
    values = {"BASE_URL": "http://127.0.0.1:18080", **(environment or {})}
    env_file.write_text(
        "vars {\n" + "".join(f"  {name}: {value}\n" for name, value in values.items()) + "}\n"
        + ("headers {\n" + "".join(f"  {name}: {value}\n" for name, value in (headers or {}).items()) + "}\n" if headers else ""),
        encoding="utf-8",
    )
    return config, env_file


class RegressionTests(unittest.TestCase):
    def test_raw_bruno_assertion_failure_is_not_passed(self):
        coverage = load_script("check_api_coverage")
        raw = [
            {
                "results": [
                    {
                        "name": "CASE_OK",
                        "status": "pass",
                        "assertionResults": [{"status": "pass"}],
                        "testResults": [],
                    },
                    {
                        "name": "CASE_BAD",
                        "status": "pass",
                        "assertionResults": [{"status": "fail"}],
                        "testResults": [],
                    },
                ]
            }
        ]
        evidence = coverage.execution_evidence(raw)
        self.assertEqual(evidence["executed"], ["CASE_OK", "CASE_BAD"])
        self.assertEqual(evidence["passed"], ["CASE_OK"])

    def test_artifact_safety_detects_jwt(self):
        safety = load_script("check_artifact_safety")
        token = "eyJ" + "a" * 30 + "." + "b" * 12 + "." + "c" * 12
        self.assertTrue(any(label == "JWT" and pattern.search(token) for label, pattern in safety.PATTERNS))

    def test_object_query_detection_pattern(self):
        coverage = load_script("check_api_coverage")
        import re

        name = re.escape("user")
        pattern = rf"[?&]{name}=\{{\{{[^}}]+\}}\}}"
        self.assertRegex("/list?user={{user}}", pattern)
        self.assertNotRegex("/list?userName=admin&pageNum=1", pattern)

    def test_utf8_integrity_reports_file_and_line(self):
        coverage = load_script("check_api_coverage")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.yaml"
            path.write_text("ok: true\nmessage: �\n", encoding="utf-8")
            findings = coverage.text_integrity_errors(path)
        self.assertEqual(len(findings), 1)
        self.assertIn("cases.yaml:2", findings[0])

    def test_case_fingerprint_detects_identical_contract(self):
        coverage = load_script("check_api_coverage")
        first = {
            "endpoint_id": "CAR_OPTION_CREATE",
            "scenario": "missing_operator",
            "request": {"body": {"name": "x"}},
            "assertions": [{"path": "$.msg", "contains": "operator"}],
        }
        second = dict(first, id="CAR_OPTION_CREATE_MISSING_OPERATOR_2")
        self.assertEqual(coverage.case_fingerprint(first), coverage.case_fingerprint(second))

    def test_raw_report_dict_uses_assertions_not_top_level_status(self):
        coverage = load_script("check_api_coverage")
        raw = {
            "results": [
                {
                    "name": "CASE_BAD",
                    "status": "pass",
                    "assertionResults": [{"status": "fail"}],
                    "testResults": [],
                }
            ]
        }
        self.assertEqual(coverage.execution_evidence(raw)["passed"], [])

    def test_raw_report_without_observations_is_not_passed(self):
        coverage = load_script("check_api_coverage")
        raw = {
            "results": [
                {
                    "name": "CASE_EMPTY",
                    "status": "pass",
                    "assertionResults": [],
                    "testResults": [],
                }
            ]
        }
        self.assertEqual(coverage.execution_evidence(raw)["executed"], ["CASE_EMPTY"])
        self.assertEqual(coverage.execution_evidence(raw)["passed"], [])

    def test_untagged_partition_without_module_map_returns_error(self):
        coverage = load_script("check_api_coverage")
        errors = coverage.validate_tag_partition(
            None,
            [("things", {"id": "THING_LIST", "method": "GET", "path": "/things", "tags": []})],
            [{"id": "THING_LIST", "method": "GET", "path": "/things", "tags": []}],
        )
        self.assertTrue(any("module-map.yaml is required" in error for error in errors))

    def test_execution_config_is_strict_and_has_no_version_field(self):
        config = load_script("execution_config")
        valid = config.validate_execution_config({
            "active_environment": "local",
            "tooling": "project-scripts",
            "coverage_profile": "full-matrix",
            "sign": {"provider": "disabled"},
        })
        self.assertEqual(valid, {
            "active_environment": "local",
            "tooling": "project-scripts",
            "coverage_profile": "full-matrix",
            "sign": {"provider": "disabled"},
        })
        for invalid in (
            {"version": 1, **valid},
            {**valid, "active_environment": "local.bru"},
            {**valid, "active_environment": "local:bad"},
            {**valid, "sign": {"provider": "seres"}},
            {**valid, "sign": {"provider": "unknown"}},
            {**valid, "auth": {}},
            {**valid, "custom_headers": {}},
        ):
            with self.assertRaises(ValueError):
                config.validate_execution_config(invalid)

    def test_collection_runtime_replaces_per_request_authentication(self):
        config = load_script("execution_config")
        coverage = load_script("check_api_coverage")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = root / "things"
            module.mkdir()
            (root / "collection.bru").write_text(config.COLLECTION_TEMPLATE, encoding="utf-8")
            request = module / "01-查询事物成功.bru"
            request.write_text(
                "meta {\n  name: THING_OK\n  type: http\n}\n"
                "get {\n  url: {{BASE_URL}}/things\n}\nassert {\n  res.status: eq 200\n}\n",
                encoding="utf-8",
            )
            self.assertEqual(coverage.collection_runtime_errors(root), [])
            request.write_text(
                request.read_text(encoding="utf-8")
                + "// bru-api-test-generator: auth-start\n",
                encoding="utf-8",
            )
            self.assertTrue(any(
                "legacy per-request authentication" in error
                for error in coverage.collection_runtime_errors(root)
            ))

    def test_local_openapi_ref_is_resolved_for_query_schema(self):
        parser = load_script("parse_openapi")
        document = {
            "openapi": "3.0.0",
            "components": {"schemas": {"Filter": {"type": "object", "properties": {"name": {"type": "string"}}}}},
            "paths": {
                "/things": {
                    "get": {
                        "operationId": "listThings",
                        "parameters": [{"name": "filter", "in": "query", "schema": {"$ref": "#/components/schemas/Filter"}}],
                    }
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            spec_path = Path(directory) / "openapi.json"
            spec_path.write_text(json.dumps(document), encoding="utf-8")
            manifest = parser.extract(spec_path, document)
        schema = manifest["endpoints"][0]["parameters"][0]["schema"]
        self.assertEqual(schema["properties"]["name"]["type"], "string")
        self.assertEqual(schema["$ref"], "#/components/schemas/Filter")

    def test_swagger2_parameter_and_response_refs_are_kept_local(self):
        parser = load_script("parse_openapi")
        document = {
            "swagger": "2.0",
            "parameters": {"Trace": {"name": "X-Trace", "in": "header", "type": "string"}},
            "responses": {"Ok": {"description": "ok", "schema": {"$ref": "#/definitions/Thing"}}},
            "definitions": {"Thing": {"type": "object", "properties": {"id": {"type": "string"}}}},
            "paths": {
                "/things": {
                    "get": {
                        "operationId": "listThings",
                        "tags": ["things"],
                        "parameters": [{"$ref": "#/parameters/Trace"}],
                        "responses": {"200": {"$ref": "#/responses/Ok"}},
                    }
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec_path = root / "swagger.json"
            spec_path.write_text(json.dumps(document), encoding="utf-8")
            manifest = parser.extract(spec_path, document)
            module_map = {"modules": [{"id": "things", "name": "things", "swagger_tags": ["things"]}]}
            grouped = parser.partition_manifest(manifest, module_map)
            components = parser.local_components(manifest, grouped["things"])
        self.assertIn("Trace", components["parameters"])
        self.assertIn("Ok", components["responses"])
        self.assertIn("Thing", components["schemas"])

    def test_empty_flow_skips_without_flow_requirement_but_blocks_explicit_flow(self):
        script = ROOT / "scripts/validate_flow_execution.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            flows = root / "flows.yaml"
            results = root / "results.json"
            endpoints = root / "endpoints.yaml"
            flows.write_text(json.dumps({"flows": []}), encoding="utf-8")
            results.write_text(json.dumps({"flows": {}}), encoding="utf-8")
            endpoints.write_text(json.dumps({"flow_required": True, "endpoints": [{"id": "CREATE", "method": "POST"}]}), encoding="utf-8")
            skipped = subprocess.run(
                [sys.executable, str(script), str(flows), "--results", str(results)],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(skipped.returncode, 0)
            self.assertIn("skipped: no declared flows", skipped.stdout)
            blocked = subprocess.run(
                [sys.executable, str(script), str(flows), "--endpoints", str(endpoints), "--results", str(results)],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(blocked.returncode, 1)
            self.assertIn("flow_required", blocked.stdout)

    def test_partition_writes_security_profile_and_draft_index(self):
        parser = load_script("parse_openapi")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec_path = root / "openapi.json"
            map_path = root / "module-map.yaml"
            output_dir = root / "contracts" / "modules"
            document = {
                "openapi": "3.0.0",
                "tags": [{"name": "things", "description": "Thing management"}],
                "paths": {"/things": {"get": {
                    "operationId": "listThings",
                    "tags": ["things"],
                    "security": [{"bearerAuth": []}],
                }}},
            }
            spec_path.write_text(json.dumps(document), encoding="utf-8")
            map_path.write_text(json.dumps({
                "modules": [{"id": "things", "name": "things", "swagger_tags": ["things"]}],
            }), encoding="utf-8")
            legacy_environment = root / "bruno" / "environments"
            legacy_environment.mkdir(parents=True)
            (legacy_environment / "local.bru").write_text(
                "vars {\n  BASE_URL: http://127.0.0.1:18080\n  OPERATOR_INFO:\n}\n",
                encoding="utf-8",
            )
            parser.write_partitioned(parser.extract(spec_path, document), map_path, output_dir)
            endpoints = parser.load_document(output_dir / "things" / "endpoints.yaml")
            self.assertEqual(endpoints["endpoints"][0]["tag_description"], "Thing management")
            self.assertTrue((root / "contracts" / "security-profile.yaml").is_file())
            config_text = (root / "execution" / "config.yaml").read_text(encoding="utf-8")
            self.assertIn("active_environment: local", config_text)
            self.assertIn("coverage_profile: full-matrix", config_text)
            self.assertIn("sign:\n  provider: disabled", config_text)
            self.assertFalse((root / "qa.yaml").exists())
            self.assertFalse((root / "execution" / "plans.yaml").exists())
            self.assertNotIn("auth:", config_text)
            self.assertNotIn("custom_headers:", config_text)
            self.assertNotIn("version:", config_text)
            self.assertTrue((root / "execution" / "environments" / "local.bru").is_file())
            self.assertFalse((root / "bruno" / "environments").exists())
            self.assertTrue((root / "execution" / "run.bat").is_file())
            self.assertTrue((root / "execution" / "run.sh").is_file())
            self.assertIn("mno_bruno_qa.py\" run", (root / "execution" / "run.bat").read_text(encoding="utf-8"))
            self.assertIn('mno_bruno_qa.py" run', (root / "execution" / "run.sh").read_text(encoding="utf-8"))
            self.assertEqual(
                sorted(path.name for path in (root / "execution" / "environments").glob("*.bru")),
                ["local.bru"],
            )
            self.assertTrue((root / "bruno" / "collection.bru").is_file())
            self.assertTrue((root / "bruno" / "bruno.json").is_file())
            index = parser.load_document(root / "contracts" / "index.yaml")
            self.assertIn("execution_config_file", index)
            self.assertNotIn("request_auth_file", index)
            self.assertEqual(index["generation_status"], "draft")
            self.assertEqual(index["inventory_endpoints"], 1)
            overview = (root / "contracts" / "README.md").read_text(encoding="utf-8")
            self.assertIn("业务范围：", overview)
            self.assertIn("包含内容：", overview)
            self.assertIn("CASES.md", overview)
            module_doc = (output_dir / "things" / "CASES.md").read_text(encoding="utf-8")
            self.assertIn("## 模块内容", module_doc)
            self.assertIn("## 接口清单", module_doc)

            document["paths"]["/things"]["get"]["parameters"] = [
                {"name": "pageNum", "in": "query", "schema": {"type": "integer"}},
            ]
            parser.write_partitioned(parser.extract(spec_path, document), map_path, output_dir)
            parameters = parser.load_document(output_dir / "things" / "parameters.yaml")
            self.assertEqual(parameters["parameters"][0]["items"][0]["name"], "pageNum")
            endpoints = parser.load_document(output_dir / "things" / "endpoints.yaml")
            endpoints["endpoints"][0]["case_ids"] = ["THING_LIST_OK"]
            endpoints["endpoints"][0]["scenario_matrix"] = {"success": {"applicable": True}}
            (output_dir / "things" / "endpoints.yaml").write_text(
                parser.render_manifest(endpoints, output_dir / "things" / "endpoints.yaml"),
                encoding="utf-8",
            )
            cases = {
                "version": 1,
                "module": "things",
                "swagger_tag": "things",
                "cases": [{
                    "id": "THING_LIST_OK",
                    "title": "查询事物成功",
                    "description": "验证能够查询事物列表。",
                    "endpoint_id": "LISTTHINGS_GET_THINGS",
                    "scenario": "success",
                    "expected": {"http_status": 200},
                    "assertions": [{"path": "$.data", "equals": {}}],
                }],
            }
            (output_dir / "things" / "cases.yaml").write_text(
                parser.render_manifest(cases, output_dir / "things" / "cases.yaml"),
                encoding="utf-8",
            )
            parser.write_partitioned(parser.extract(spec_path, document), map_path, output_dir)
            refreshed_endpoints = parser.load_document(output_dir / "things" / "endpoints.yaml")
            self.assertEqual(refreshed_endpoints["endpoints"][0]["case_ids"], ["THING_LIST_OK"])
            self.assertIn("scenario_matrix", refreshed_endpoints["endpoints"][0])
            refreshed_index = parser.load_document(root / "contracts" / "index.yaml")
            self.assertEqual(refreshed_index["generated_cases"], 1)
            self.assertEqual(refreshed_index["modules"][0]["case_count"], 1)
            self.assertIn(
                "<!-- CASE_START: THING_LIST_OK -->",
                (output_dir / "things" / "CASES.md").read_text(encoding="utf-8"),
            )
            materialized = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/materialize_missing_bru.py"),
                    str(root / "contracts"),
                    str(root / "bruno"),
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(materialized.returncode, 0, materialized.stderr)
            checked = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/check_api_coverage.py"),
                    str(root / "contracts"),
                    str(root / "bruno"),
                    "--all",
                    "--json",
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            self.assertTrue(json.loads(checked.stdout)["static_ok"])

    def test_partition_supports_explicit_path_prefix_fallback_for_untagged_operations(self):
        parser = load_script("parse_openapi")
        manifest = {
            "source": {"file": "openapi.json"},
            "endpoints": [{"id": "THING_LIST", "method": "GET", "path": "/things", "tags": []}],
        }
        module_map = {"modules": [{"id": "things", "path_prefixes": ["/things"]}]}
        grouped = parser.partition_manifest(manifest, module_map)
        self.assertEqual([item["id"] for item in grouped["things"]], ["THING_LIST"])
        with self.assertRaisesRegex(ValueError, "no Swagger tag"):
            parser.partition_manifest(manifest, {"modules": [
                {"id": "other", "name": "other", "swagger_tags": []},
                {"id": "another", "name": "another", "swagger_tags": []},
            ]})

    def test_chinese_tag_gets_a_display_directory_without_changing_id(self):
        parser = load_script("parse_openapi")
        manifest = {
            "source": {"file": "openapi.json", "sha256": "abc"},
            "tag_descriptions": {"用户管理": "用户账户管理"},
            "endpoints": [{
                "id": "USER_LIST",
                "method": "GET",
                "path": "/users",
                "tags": ["用户管理"],
                "parameters": [],
                "responses": {},
            }],
        }
        module_map = parser.generate_module_map(manifest)
        self.assertEqual(module_map["modules"][0]["directory"], "用户管理")
        self.assertTrue(module_map["modules"][0]["id"].startswith("tag-"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_path = root / "module-map.yaml"
            map_path.write_text(json.dumps(module_map), encoding="utf-8")
            parser.write_partitioned(manifest, map_path, root / "contracts" / "modules")
            self.assertTrue((root / "contracts" / "modules" / "用户管理" / "endpoints.yaml").is_file())
            self.assertIn("用户管理", (root / "contracts" / "README.md").read_text(encoding="utf-8"))

    def test_tagged_module_rejects_an_unrelated_directory_name(self):
        parser = load_script("parse_openapi")
        manifest = {"endpoints": [{"tags": ["用户管理"]}]}
        module_map = {
            "modules": [{
                "id": "user-management",
                "name": "用户管理",
                "directory": "自定义目录",
                "swagger_tags": ["用户管理"],
            }]
        }
        with self.assertRaisesRegex(ValueError, "directory must come from Swagger tag"):
            parser.validate_module_map(manifest, module_map)

    def test_tag_directory_collision_is_blocking(self):
        parser = load_script("parse_openapi")
        manifest = {
            "source": {},
            "tag_descriptions": {},
            "endpoints": [
                {"id": "ONE", "tags": ["模块/查询"]},
                {"id": "TWO", "tags": ["模块:查询"]},
            ],
        }
        with self.assertRaisesRegex(ValueError, "same safe module directory"):
            parser.generate_module_map(manifest)

    def test_coverage_maps_display_directory_back_to_stable_module_id(self):
        coverage = load_script("check_api_coverage")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module_dir = root / "modules" / "用户管理"
            module_dir.mkdir(parents=True)
            (module_dir / "endpoints.yaml").write_text(json.dumps({"endpoints": []}), encoding="utf-8")
            modules = coverage.module_dirs(
                root,
                {"modules": [{"id": "tag-user", "directory": "用户管理", "swagger_tags": ["用户管理"]}]},
            )
            self.assertEqual(modules, [("tag-user", module_dir)])

    def test_materializer_uses_chinese_case_title_for_filename(self):
        scripts_path = str(ROOT / "scripts")
        sys.path.insert(0, scripts_path)
        try:
            materializer = load_script("materialize_missing_bru")
        finally:
            sys.path.remove(scripts_path)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = root / "contracts" / "modules" / "用户管理"
            module.mkdir(parents=True)
            (module / "endpoints.yaml").write_text(json.dumps({
                "endpoints": [{
                    "id": "USER_CREATE",
                    "method": "POST",
                    "path": "/users",
                    "summary": "创建用户",
                }]
            }), encoding="utf-8")
            (module / "cases.yaml").write_text(json.dumps({
                "cases": [{
                    "id": "USER_CREATE_OK",
                    "title": "创建用户",
                    "endpoint_id": "USER_CREATE",
                    "expected": {"http_status": 200},
                    "assertions": [{"path": "$.data", "equals": {}}],
                }]
            }), encoding="utf-8")
            (module / "CASES.md").write_text(
                "# 用户管理\n\n这里是需要保留的人工业务说明。\n",
                encoding="utf-8",
            )
            (root / "contracts" / "module-map.yaml").write_text(json.dumps({
                "modules": [{"id": "用户管理", "name": "用户管理", "business_scope": "账号生命周期管理"}],
            }), encoding="utf-8")
            config, _ = execution_fixture(root)
            created = materializer.materialize(
                root / "contracts", root / "bruno", execution_config_path=config
            )
            bru_files = [
                path for path in created
                if path.suffix == ".bru" and path.name != "collection.bru"
            ]
            self.assertEqual(len(bru_files), 1)
            self.assertEqual(bru_files[0].parent.name, "用户管理")
            self.assertEqual(bru_files[0].name, "01-创建用户.bru")
            bru_text = bru_files[0].read_text(encoding="utf-8")
            self.assertIn("## 用例标题：创建用户", bru_text)
            self.assertIn("响应校验覆盖状态码、业务结果和具体字段。", bru_text)
            self.assertIn("type: http", bru_text)
            case_doc = (module / "CASES.md").read_text(encoding="utf-8")
            self.assertIn("### 创建用户", case_doc)
            self.assertIn("业务范围：账号生命周期管理", case_doc)
            self.assertIn("简短描述：", case_doc)
            self.assertIn("sequenceDiagram", case_doc)
            self.assertIn("<!-- CASE_START: USER_CREATE_OK -->", case_doc)
            self.assertIn("这里是需要保留的人工业务说明。", case_doc)

    def test_business_filename_uses_sequence_and_chinese_case_title(self):
        materializer = load_script("materialize_missing_bru")
        path = materializer.display_case_file(
            {"id": "traffic_query_success", "title": "查询车辆/流量信息成功"},
            1,
        )
        self.assertEqual(path.name, "01-查询车辆流量信息成功.bru")
        rendered = materializer.render_case(
            {"id": "traffic_query_success", "title": "查询车辆流量信息成功"},
            {"method": "GET", "path": "/traffic"},
        )
        self.assertIn("name: traffic_query_success", rendered)
        self.assertIn("url: {{baseUrl}}/traffic", rendered)

    def test_materializer_rejects_explicit_case_id_filename(self):
        materializer = load_script("materialize_missing_bru")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = root / "contracts" / "modules" / "流量查询"
            module.mkdir(parents=True)
            (module / "endpoints.yaml").write_text(json.dumps({"endpoints": [{
                "id": "TRAFFIC", "method": "GET", "path": "/traffic", "summary": "查询车辆流量",
            }]}), encoding="utf-8")
            (module / "cases.yaml").write_text(json.dumps({"cases": [{
                "id": "traffic_query_success", "endpoint_id": "TRAFFIC",
                "title": "查询车辆流量成功", "bru": "01-traffic_query_success.bru",
            }]}), encoding="utf-8")
            config, _ = execution_fixture(root)
            with self.assertRaisesRegex(SystemExit, "invalid business Bruno filename"):
                materializer.materialize(
                    root / "contracts", root / "bruno", execution_config_path=config
                )

    def test_materializer_blocks_without_chinese_case_title(self):
        materializer = load_script("materialize_missing_bru")
        with self.assertRaisesRegex(SystemExit, "Chinese case.title"):
            materializer.display_case_file(
                {"id": "traffic_query_success", "title": "traffic_query_success"},
                1,
            )

    def test_materializer_reports_filename_collision(self):
        materializer = load_script("materialize_missing_bru")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = root / "contracts" / "modules" / "流量查询"
            module.mkdir(parents=True)
            (module / "endpoints.yaml").write_text(json.dumps({"endpoints": [{
                "id": "TRAFFIC", "method": "GET", "path": "/traffic", "summary": "查询车辆流量",
            }]}), encoding="utf-8")
            cases = [
                {"id": case_id, "endpoint_id": "TRAFFIC", "title": "查询车辆流量", "sequence": 1}
                for case_id in ("traffic_one", "traffic_two")
            ]
            (module / "cases.yaml").write_text(json.dumps({"cases": cases}), encoding="utf-8")
            config, _ = execution_fixture(root)
            with self.assertRaisesRegex(SystemExit, "filename collision"):
                materializer.materialize(
                    root / "contracts", root / "bruno", execution_config_path=config
                )

    def test_repeated_materialization_preserves_filename_and_number(self):
        materializer = load_script("materialize_missing_bru")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = root / "contracts" / "modules" / "流量查询"
            module.mkdir(parents=True)
            (module / "endpoints.yaml").write_text(json.dumps({"endpoints": [{
                "id": "TRAFFIC", "method": "GET", "path": "/traffic", "summary": "查询车辆流量",
            }]}), encoding="utf-8")
            (module / "cases.yaml").write_text(json.dumps({"cases": [{
                "id": "traffic_query_success", "endpoint_id": "TRAFFIC", "title": "查询车辆流量成功",
            }]}), encoding="utf-8")
            config, _ = execution_fixture(root)
            materializer.materialize(
                root / "contracts", root / "bruno", execution_config_path=config
            )
            first = sorted(
                path for path in (root / "bruno").rglob("*.bru")
                if path.name != "collection.bru"
            )
            cases_document = load_script("manifest_io").load_data(module / "cases.yaml")
            self.assertEqual(cases_document["cases"][0]["bru"], "01-查询车辆流量成功.bru")
            second_changes = materializer.materialize(
                root / "contracts", root / "bruno", execution_config_path=config
            )
            self.assertEqual([path.name for path in first], ["01-查询车辆流量成功.bru"])
            self.assertEqual(second_changes, [])
            original = first[0].read_text(encoding="utf-8")
            first[0].write_text(original.replace("/traffic", "/wrong"), encoding="utf-8")
            drift = materializer.materialize(
                root / "contracts",
                root / "bruno",
                dry_run=True,
                execution_config_path=config,
                module_filter="流量查询",
                check=True,
            )
            self.assertIn(first[0].name, {path.name for path in drift})
            self.assertIn("/wrong", first[0].read_text(encoding="utf-8"))

    def test_existing_filename_number_survives_manifest_reordering(self):
        materializer = load_script("materialize_missing_bru")
        coverage = load_script("check_api_coverage")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = root / "contracts" / "modules" / "车辆查询"
            module.mkdir(parents=True)
            endpoint = {"id": "TRAFFIC", "method": "GET", "path": "/traffic"}
            (module / "endpoints.yaml").write_text(json.dumps({"endpoints": [endpoint]}), encoding="utf-8")
            cases = [
                {
                    "id": "traffic_first", "endpoint_id": "TRAFFIC", "title": "查询第一辆车成功",
                    "expected": {"http_status": 200}, "assertions": [{"path": "$.data", "equals": 1}],
                },
                {
                    "id": "traffic_second", "endpoint_id": "TRAFFIC", "title": "查询第二辆车成功",
                    "expected": {"http_status": 200}, "assertions": [{"path": "$.data", "equals": 1}],
                },
            ]
            cases_path = module / "cases.yaml"
            cases_path.write_text(json.dumps({"cases": cases}), encoding="utf-8")
            config, _ = execution_fixture(root)

            materializer.materialize(
                root / "contracts", root / "bruno", execution_config_path=config
            )
            persisted = load_script("manifest_io").load_data(cases_path)["cases"]
            cases_path.write_text(json.dumps({"cases": list(reversed(persisted))}), encoding="utf-8")

            changes = materializer.materialize(
                root / "contracts", root / "bruno", execution_config_path=config
            )
            self.assertFalse(any(path.suffix == ".bru" for path in changes))
            reordered = load_script("manifest_io").load_data(cases_path)["cases"]
            self.assertEqual(reordered[0]["bru"], "02-查询第二辆车成功.bru")
            self.assertEqual(reordered[1]["bru"], "01-查询第一辆车成功.bru")
            _, _, _, errors = coverage.case_files(reordered, root / "bruno" / "车辆查询")
            self.assertFalse(any("filename must match" in error for error in errors))

    def test_coverage_enforces_chinese_case_title_and_exempts_collection_config(self):
        coverage = load_script("check_api_coverage")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "environments").mkdir()
            case = {
                "id": "traffic_query_success",
                "title": "查询车辆流量成功",
                "bru": "01-查询车辆流量成功.bru",
                "expected": {"http_status": 200},
                "assertions": [{"path": "$.data", "equals": 1}],
            }
            (root / "01-查询车辆流量成功.bru").write_text(
                "meta {\n  name: traffic_query_success\n  type: http\n}\nget {\n  url: {{BASE_URL}}/traffic\n}\nassert {\n  res.status: eq 200\n  res.body.data: eq 1\n}\n",
                encoding="utf-8",
            )
            (root / "environments" / "local.bru").write_text("vars { BASE_URL: http://localhost }\n", encoding="utf-8")
            (root / "collection.bru").write_text("meta { name: collection }\n", encoding="utf-8")
            covered, _, _, errors = coverage.case_files([case], root)
            self.assertEqual(covered, {"traffic_query_success"})
            self.assertEqual(errors, [])

            case["bru"] = "01-traffic_query_success.bru"
            (root / "01-traffic_query_success.bru").write_text(
                "meta {\n  name: traffic_query_success\n  type: http\n}\nget {\n  url: {{BASE_URL}}/traffic\n}\nassert {\n  res.status: eq 200\n}\n",
                encoding="utf-8",
            )
            _, _, _, errors = coverage.case_files([case], root)
            self.assertTrue(any("sanitized Chinese case.title" in error for error in errors))

    def test_coverage_rejects_a_different_chinese_title(self):
        coverage = load_script("check_api_coverage")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = {
                "id": "traffic_query_success",
                "title": "查询车辆流量成功",
                "bru": "01-删除车辆成功.bru",
                "expected": {"http_status": 200},
                "assertions": [{"path": "$.data", "equals": 1}],
            }
            (root / case["bru"]).write_text(
                "meta {\n  name: traffic_query_success\n  type: http\n}\nget {\n  url: {{BASE_URL}}/traffic\n}\nassert {\n  res.status: eq 200\n  res.body.data: eq 1\n}\n",
                encoding="utf-8",
            )
            _, _, _, errors = coverage.case_files([case], root)
            self.assertTrue(any("sanitized Chinese case.title" in error for error in errors))

    def test_seed_cases_are_contract_only_and_review_required(self):
        parser = load_script("parse_openapi")
        seeded = parser.seed_contract_cases({
            "id": "UPLOAD_LIST",
            "method": "POST",
            "parameters": [
                {"name": "status", "required": True, "schema": {"enum": ["ON", "OFF"]}},
                {"name": "pageNum", "schema": {"type": "integer"}},
                {"name": "operatorInfo", "in": "header", "required": True, "schema": {"type": "string"}},
            ],
            "request_body": {"content": {"multipart/form-data": {"schema": {
                "type": "object",
                "required": ["file"],
                "properties": {"file": {"type": "string", "format": "binary"}},
            }}}},
            "responses": {"201": {}, "400": {}},
        })
        self.assertEqual(
            {case["scenario"] for case in seeded},
            {"success", "validation", "query", "file"},
        )
        self.assertTrue(all(case["review_required"] is True for case in seeded))
        self.assertTrue(all("business_error" != case["scenario"] for case in seeded))
        header_case = next(case for case in seeded if case["id"].endswith("MISSING_OPERATORINFO"))
        self.assertEqual(header_case["request"]["omit_common_headers"], ["operatorInfo"])

    def test_seed_cases_populate_success_body_and_omit_each_required_field(self):
        parser = load_script("parse_openapi")
        seeded = parser.seed_contract_cases({
            "id": "THING_CREATE",
            "method": "POST",
            "request_body": {"content": {"application/json": {"schema": {
                "type": "object",
                "required": ["name", "count"],
                "properties": {
                    "name": {"type": "string", "example": "demo"},
                    "count": {"type": "integer", "default": 2},
                },
            }}}},
            "responses": {"200": {}, "400": {}},
        })
        by_id = {case["id"]: case for case in seeded}
        self.assertEqual(by_id["THING_CREATE_SUCCESS"]["request"]["body"], {"name": "demo", "count": 2})
        self.assertEqual(by_id["THING_CREATE_MISSING_BODY_NAME"]["request"]["body"], {"count": 2})
        self.assertEqual(by_id["THING_CREATE_MISSING_BODY_COUNT"]["request"]["body"], {"name": "demo"})

    def test_seed_cases_require_declared_response_statuses(self):
        parser = load_script("parse_openapi")
        endpoint = {
            "id": "THING_CREATE",
            "method": "POST",
            "parameters": [
                {"name": "status", "in": "query", "required": True, "schema": {"enum": ["ON", "OFF"]}},
            ],
            "request_body": {"content": {"application/json": {"schema": {
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
            }}}},
        }

        self.assertEqual(parser.seed_contract_cases(endpoint), [])

        endpoint["responses"] = {"204": {}}
        seeded = parser.seed_contract_cases(endpoint)
        self.assertEqual({case["scenario"] for case in seeded}, {"success"})
        self.assertTrue(all(case["expected"]["http_status"] == 204 for case in seeded))

        endpoint["responses"] = {"422": {}}
        seeded = parser.seed_contract_cases(endpoint)
        self.assertEqual({case["scenario"] for case in seeded}, {"validation"})
        self.assertTrue(all(case["expected"]["http_status"] == 422 for case in seeded))

    def test_optional_common_header_does_not_block_preflight(self):
        execution_config = load_script("execution_config")
        preflight = load_script("runtime_preflight")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, env_file = execution_fixture(
                root,
                headers={"operatorInfo": "{{OPERATOR_INFO}}"},
            )
            loaded = execution_config.load_execution_config(config)
            self.assertEqual(execution_config.required_environment_names(loaded), [])
            openapi, static_results = static_preflight_inputs(root)
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/runtime_preflight.py"),
                    "--execution-config", str(config),
                    "--env-file", str(env_file),
                    "--bruno-cli", "node",
                    "--openapi", str(openapi),
                    "--static-results", str(static_results),
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            report = json.loads(result.stdout)
            self.assertTrue(report["static_ready"])
            self.assertTrue(report["context_ready"])
            self.assertFalse(report["execution_ready"])
            self.assertEqual(report["status"], "draft")

    def test_coverage_detects_method_url_query_and_body_drift(self):
        coverage = load_script("check_api_coverage")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = {
                "id": "thing_update",
                "endpoint_id": "THING_UPDATE",
                "bru": "01-更新事物成功.bru",
                "request": {"query": {"tenant": "t"}, "body_type": "application/json", "body": {"name": "n"}},
                "expected": {"http_status": 200},
                "assertions": [{"path": "$.data.name", "equals": "n"}],
            }
            (root / case["bru"]).write_text(
                "meta {\n  name: thing_update\n  type: http\n}\nget {\n  url: {{BASE_URL}}/wrong\n  body: none\n}\nassert {\n  res.status: eq 200\n  res.body.data.name: eq \"n\"\n}\n",
                encoding="utf-8",
            )
            _, _, _, errors = coverage.case_files(
                [case], root, {"THING_UPDATE": {"id": "THING_UPDATE", "method": "POST", "path": "/things"}}
            )
            self.assertTrue(any("method" in error for error in errors))
            self.assertTrue(any("URL path" in error for error in errors))
            self.assertTrue(any("Bruno query" in error for error in errors))
            self.assertTrue(any("body type" in error for error in errors))

    def test_coverage_allows_required_body_field_to_be_omitted_by_negative_case(self):
        coverage = load_script("check_api_coverage")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = {
                "id": "thing_missing_name",
                "endpoint_id": "THING_CREATE",
                "bru": "01-缺少必填字段名称.bru",
                "request": {"body_type": "application/json", "body": {}},
                "expected": {"http_status": 400},
                "assertions": [{"path": "$.message", "equals": "名称不能为空"}],
            }
            (root / case["bru"]).write_text(
                "meta {\n  name: thing_missing_name\n  type: http\n}\npost {\n  url: {{BASE_URL}}/things\n  body: json\n}\nbody:json {\n{}\n}\nassert {\n  res.status: eq 400\n  res.body.message: eq \"名称不能为空\"\n}\n",
                encoding="utf-8",
            )
            endpoint = {
                "id": "THING_CREATE",
                "method": "POST",
                "path": "/things",
                "request_body": {
                    "content": {"application/json": {"schema": {"required": ["name"]}}}
                },
            }
            _, _, _, errors = coverage.case_files([case], root, {"THING_CREATE": endpoint})
            self.assertFalse(any("request field name" in error for error in errors), errors)

    def test_coverage_detects_body_value_drift(self):
        coverage = load_script("check_api_coverage")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = {
                "id": "thing_create",
                "title": "创建事物成功",
                "endpoint_id": "THING_CREATE",
                "bru": "01-创建事物成功.bru",
                "request": {"body_type": "application/json", "body": {"name": "alice"}},
                "expected": {"http_status": 200},
                "assertions": [{"path": "$.data.name", "equals": "alice"}],
            }
            (root / case["bru"]).write_text(
                "meta {\n  name: thing_create\n  type: http\n}\npost {\n  url: {{BASE_URL}}/things\n  body: json\n}\nbody:json {\n{\"name\":\"bob\"}\n}\nassert {\n  res.status: eq 200\n  res.body.data.name: eq \"alice\"\n}\n",
                encoding="utf-8",
            )
            endpoint = {"id": "THING_CREATE", "method": "POST", "path": "/things"}
            _, _, _, errors = coverage.case_files([case], root, {"THING_CREATE": endpoint})
            self.assertTrue(any("body content" in error for error in errors))

    def test_materializer_renders_combined_assertions_array_items_and_capture(self):
        materializer = load_script("materialize_missing_bru")
        rendered = materializer.render_case(
            {
                "id": "THING_LIST",
                "assertions": [{
                    "path": "$.data",
                    "type": "array",
                    "length": 2,
                    "items": {"type": "string"},
                    "capture_as": "things",
                }],
            },
            {"method": "GET", "path": "/things"},
            sequence=2,
        )
        self.assertIn("seq: 2", rendered)
        self.assertIn("res.body.data: isArray", rendered)
        self.assertIn("res.body.data: length 2", rendered)
        self.assertIn('bru.setVar("things", res.body.data)', rendered)
        self.assertIn("res.body.data.forEach", rendered)

    def test_coverage_requires_every_declared_assertion_constraint(self):
        coverage = load_script("check_api_coverage")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = {
                "id": "thing_list",
                "title": "查询事物成功",
                "bru": "01-查询事物成功.bru",
                "expected": {"http_status": 200},
                "assertions": [{"path": "$.data", "type": "array", "length": 2}],
            }
            (root / case["bru"]).write_text(
                "meta {\n  name: thing_list\n  type: http\n}\nget {\n  url: {{BASE_URL}}/things\n}\nassert {\n  res.status: eq 200\n  res.body.data: isArray\n}\n",
                encoding="utf-8",
            )
            _, _, _, errors = coverage.case_files([case], root)
            self.assertTrue(any("missing length" in error for error in errors))

    def test_version_lock_can_initialize_a_draft_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            contracts = root / "contracts"
            repo.mkdir()
            contracts.mkdir()
            (repo / "main.go").write_text("package main\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts/check_version_compatibility.py"), str(repo), str(contracts), "--init", "--json"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            lock = load_script("manifest_io").load_data(contracts / "version-lock.yaml")
            self.assertEqual(lock["status"], "draft")
            self.assertEqual(lock["business"]["baseline_status"], "draft")

    def test_module_materialization_does_not_touch_other_modules_or_index(self):
        materializer = load_script("materialize_missing_bru")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contracts = root / "contracts"
            for name, title in (("模块一", "查询模块一成功"), ("模块二", "查询模块二成功")):
                module = contracts / "modules" / name
                module.mkdir(parents=True)
                (module / "endpoints.yaml").write_text(json.dumps({"module": name, "endpoints": [{
                    "id": name, "method": "GET", "path": f"/{name}",
                }]}), encoding="utf-8")
                (module / "cases.yaml").write_text(json.dumps({"cases": [{
                    "id": f"{name}_OK", "title": title, "endpoint_id": name,
                    "expected": {"http_status": 200}, "assertions": [{"path": "$.data", "equals": {}}],
                }]}), encoding="utf-8")
            index = contracts / "index.yaml"
            index.write_text(json.dumps({"generated_cases": 0, "modules": []}), encoding="utf-8")
            config, _ = execution_fixture(root)
            before = index.read_text(encoding="utf-8")
            materializer.materialize(
                contracts,
                root / "bruno",
                execution_config_path=config,
                module_filter="模块一",
            )
            self.assertTrue((root / "bruno" / "模块一" / "01-查询模块一成功.bru").is_file())
            self.assertFalse((root / "bruno" / "模块二").exists())
            self.assertEqual(index.read_text(encoding="utf-8"), before)

    def test_full_and_module_scoped_materialization_are_identical(self):
        materializer = load_script("materialize_missing_bru")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contracts = root / "contracts"
            modules = (("module-one", "模块一"), ("module-two", "模块二"))
            for module_id, tag in modules:
                module = contracts / "modules" / tag
                module.mkdir(parents=True)
                endpoint_id = f"{module_id}-list"
                (module / "endpoints.yaml").write_text(json.dumps({
                    "module": module_id,
                    "swagger_tag": tag,
                    "endpoints": [{"id": endpoint_id, "method": "GET", "path": f"/{module_id}"}],
                }), encoding="utf-8")
                (module / "cases.yaml").write_text(json.dumps({"cases": [{
                    "id": f"{module_id}_success",
                    "title": f"查询{tag}成功",
                    "endpoint_id": endpoint_id,
                    "expected": {"http_status": 200},
                    "assertions": [{"path": "$.data", "equals": []}],
                }]}), encoding="utf-8")
            config, _ = execution_fixture(root)
            full = root / "full"
            scoped = root / "scoped"
            materializer.materialize(contracts, full, execution_config_path=config)
            for module_id, _ in modules:
                materializer.materialize(
                    contracts,
                    scoped,
                    execution_config_path=config,
                    module_filter=module_id,
                )
            full_files = {path.relative_to(full): path.read_bytes() for path in full.rglob("*.bru")}
            scoped_files = {path.relative_to(scoped): path.read_bytes() for path in scoped.rglob("*.bru")}
            self.assertEqual(scoped_files, full_files)

    def test_openapi_identity_ignores_deployment_metadata(self):
        fetcher = load_script("fetch_local_openapi")
        first = {"openapi": "3.0.0", "servers": [{"url": "http://one"}], "paths": {"/x": {}}, "provenance": {"pid": 1}}
        second = {"openapi": "3.0.0", "servers": [{"url": "http://two"}], "paths": {"/x": {}}, "provenance": {"pid": 2}}
        self.assertEqual(fetcher.contract_identity(first), fetcher.contract_identity(second))
        second["paths"]["/y"] = {}
        self.assertNotEqual(fetcher.contract_identity(first), fetcher.contract_identity(second))

    def test_case_documentation_validator_rejects_missing_swimlane(self):
        coverage = load_script("check_api_coverage")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "CASES.md"
            path.write_text(
                "# 用户管理模块\n\n- 业务范围：用户维护\n\n"
                "## 模块内容\n\n## 接口清单\n\n## 自动化用例\n\n"
                "<!-- AUTO_CASES_START -->\n"
                "<!-- CASE_START: USER_CREATE_OK -->\n"
                "### 创建用户成功\n\n简短描述：验证合法用户能够创建成功。\n"
                "<!-- CASE_END: USER_CREATE_OK -->\n"
                "<!-- AUTO_CASES_END -->\n",
                encoding="utf-8",
            )
            errors = coverage.validate_module_documentation(path, [{"id": "USER_CREATE_OK"}])
            self.assertTrue(any("sequenceDiagram" in error for error in errors))

    def test_seres_sign_algorithm_and_header_names_are_fixed(self):
        execution_config = load_script("execution_config")
        config = execution_config.validate_execution_config({
            "active_environment": "local",
            "tooling": "project-scripts",
            "coverage_profile": "full-matrix",
            "sign": {"provider": "seres", "version": "v1"},
        })
        script = execution_config.COLLECTION_TEMPLATE
        self.assertEqual(
            execution_config.required_environment_names(config),
            ["ACCESS_KEY", "SECRET_KEY"],
        )
        self.assertIn('CryptoJS.SHA256(signText)', script)
        self.assertIn('setCommonHeader("sign"', script)
        self.assertIn('setCommonHeader("timestamp"', script)
        self.assertIn('setCommonHeader("accesskey"', script)
        with self.assertRaises(ValueError):
            execution_config.validate_execution_config({
                "active_environment": "local",
                "tooling": "project-scripts",
                "coverage_profile": "full-matrix",
                "sign": {"provider": "SHA512"},
            })

    def test_environment_headers_are_resolved_and_injected(self):
        execution_config = load_script("execution_config")
        config = execution_config.validate_execution_config({
            "active_environment": "local",
            "tooling": "project-scripts",
            "coverage_profile": "full-matrix",
            "sign": {"provider": "disabled"},
        })
        environment = {
            "vars": {"BASE_URL": "http://localhost", "SERVICE_TOKEN": "secret"},
            "headers": {"Authorization": "Bearer {{SERVICE_TOKEN}}", "operatorInfo": "qa"},
        }
        payload = json.loads(execution_config.runtime_payload(config, environment))
        self.assertEqual(payload["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(payload["headers"]["operatorInfo"], "qa")
        script = execution_config.COLLECTION_TEMPLATE
        self.assertIn("runtimeConfig.headers", script)
        self.assertIn("if (value === undefined || value === null || value === \"\") return", script)
        self.assertIn("const current = req.getHeader(name)", script)
        self.assertIn("if (current === undefined || current === null)", script)

    def test_path_parameters_and_common_header_exclusions_are_preserved(self):
        scripts_path = str(ROOT / "scripts")
        sys.path.insert(0, scripts_path)
        try:
            materializer = load_script("materialize_missing_bru")
        finally:
            sys.path.remove(scripts_path)
        self.assertEqual(
            materializer.request_url({"path": "/users/{id}"}, {}, "SERVICE_URL"),
            "{{SERVICE_URL}}/users/{{id}}",
        )
        self.assertEqual(
            materializer.request_url(
                {"path": "/users"},
                {"query": {"status": "ON", "page": 0, "tenant": "{{TENANT_ID}}"}},
                "SERVICE_URL",
            ),
            "{{SERVICE_URL}}/users?status=ON&page=0&tenant={{TENANT_ID}}",
        )
        original = "script:pre-request {\n  console.log('业务前置');\n}\nassert {\n}\n"
        excluded_case = {
            "id": "USER_WITHOUT_OPERATOR",
            "request": {"omit_common_headers": ["operatorInfo", "X-Trace"]},
        }
        merged = materializer.ensure_request_script(original, excluded_case)
        self.assertEqual(merged.count("script:pre-request"), 1)
        self.assertIn("console.log('业务前置')", merged)
        self.assertIn('req.deleteHeaders(["operatorInfo", "X-Trace"])', merged)
        disabled = materializer.ensure_request_script(merged, {"id": "USER_NORMAL"})
        self.assertNotIn(materializer.OMIT_MARKER, disabled)
        self.assertIn("console.log('业务前置')", disabled)

    def test_materializer_supports_non_json_bodies_and_response_targets(self):
        materializer = load_script("materialize_missing_bru")
        case = {
            "id": "UPLOAD_TEXT",
            "title": "上传文本",
            "request": {
                "body_type": "text/plain",
                "body": "hello world",
                "headers": {"X-Request-ID": "{{request_id}}"},
            },
            "expected": {"http_status": 201},
            "assertions": [
                {"target": "header", "path": "ETag", "equals": "abc"},
                {"target": "header", "path": "X-Trace-Id", "equals": "trace"},
                {"target": "text", "contains": "accepted"},
            ],
        }
        rendered = materializer.render_case(
            case, {"id": "UPLOAD", "method": "POST", "path": "/upload"}
        )
        self.assertIn("body: text", rendered)
        self.assertIn("body:text {", rendered)
        self.assertIn("hello world", rendered)
        self.assertIn("res.headers.ETag: eq", rendered)
        self.assertIn("res.headers['X-Trace-Id']: eq", rendered)
        self.assertIn("res.body: contains", rendered)
        inferred = materializer.render_case(
            {"id": "XML_CASE", "request": {"body": "<x/>"}, "assertions": []},
            {"id": "XML", "method": "POST", "path": "/xml", "request_body": {"content": {"application/xml": {}}}},
        )
        self.assertIn("body: xml", inferred)
        self.assertIn("body:xml {", inferred)
        empty_multipart = materializer.render_case(
            {"id": "EMPTY_UPLOAD", "request": {"body": {}}, "assertions": []},
            {
                "id": "UPLOAD",
                "method": "POST",
                "path": "/upload",
                "request_body": {"content": {"multipart/form-data": {}}},
            },
        )
        self.assertIn("body: none", empty_multipart)
        self.assertNotIn("body:multipart-form", empty_multipart)

    def test_materializer_preserves_raw_json_body(self):
        materializer = load_script("materialize_missing_bru")
        rendered = materializer.render_case(
            {
                "id": "RAW_JSON",
                "request": {"body_type": "application/json", "body": '{"name":"alice"}'},
                "expected": {"http_status": 200},
                "assertions": [{"path": "$.data", "exists": True}],
            },
            {"id": "THING", "method": "POST", "path": "/things"},
        )
        self.assertIn('body:json {\n  {"name":"alice"}\n}', rendered)
        self.assertNotIn('body:json {\n"{\\"name\\":', rendered)

    def test_materializer_refreshes_generated_index_counts(self):
        materializer = load_script("materialize_missing_bru")
        parser = load_script("parse_openapi")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contracts = root / "contracts"
            module = contracts / "modules" / "things"
            module.mkdir(parents=True)
            (contracts / "index.yaml").write_text(
                json.dumps({"generation_status": "draft", "generated_cases": 0, "modules": [{"id": "things", "directory": "things", "case_count": 0, "endpoint_count": 0}]}),
                encoding="utf-8",
            )
            (module / "endpoints.yaml").write_text(json.dumps({"endpoints": [{"id": "THING_LIST"}]}), encoding="utf-8")
            (module / "cases.yaml").write_text(json.dumps({"cases": [{"id": "THING_OK"}]}), encoding="utf-8")
            materializer.sync_index_counts(contracts)
            index = parser.load_document(contracts / "index.yaml")
            self.assertEqual(index["generated_cases"], 1)
            self.assertEqual(index["modules"][0]["case_count"], 1)
            self.assertEqual(index["modules"][0]["endpoint_count"], 1)

    def test_sync_index_cli_does_not_require_bruno_root(self):
        with tempfile.TemporaryDirectory() as directory:
            contracts = Path(directory)
            (contracts / "modules").mkdir()
            (contracts / "index.yaml").write_text('{"modules": []}', encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/materialize_missing_bru.py"),
                    str(contracts),
                    "--sync-index",
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_runtime_preflight_rejects_missing_and_unknown_execution_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unknown = root / "unknown.yaml"
            unknown.write_text(
                "active_environment: local\nauth:\n  mode: unknown\n",
                encoding="utf-8",
            )
            for config in (unknown, root / "missing.yaml"):
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "scripts/runtime_preflight.py"),
                        "--execution-config", str(config),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn('"status": "blocked"', result.stdout)

    def test_filesystem_version_digest_change_is_conservative(self):
        checker = load_script("check_version_compatibility")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            contracts = root / "contracts"
            repo.mkdir()
            contracts.mkdir()
            source = repo / "main.go"
            source.write_text("package main\n", encoding="utf-8")
            digest = checker.source_digest(repo)
            (contracts / "version-lock.yaml").write_text(
                json.dumps({"version": 1, "business": {"commit": f"filesystem:{digest[:16]}", "source_digest": digest}}),
                encoding="utf-8",
            )
            source.write_text("package main\nfunc changed() {}\n", encoding="utf-8")
            report = root / "evidence.json"
            report.write_text(json.dumps({"status": "verified", "completion_ok": True}), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/check_version_compatibility.py"),
                    str(repo),
                    str(contracts),
                    "--write",
                    "--completion-report",
                    str(report),
                    "--json",
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(result.returncode, 3)
            self.assertEqual(json.loads(result.stdout)["impact"], "api-impact")

    def test_flow_evidence_is_required_when_results_are_present(self):
        coverage = load_script("check_api_coverage")
        flow = {"id": "THING_FLOW", "steps": [{"operation": "create", "case_id": "THING_OK"}]}
        errors = coverage.flow_execution_errors([flow], {"executed": ["THING_OK"], "passed": ["THING_OK"]})
        self.assertIn("flow THING_FLOW has no execution evidence", errors)

    def test_authorization_api_key_and_cookie_are_plain_environment_headers(self):
        execution_config = load_script("execution_config")
        document = {
            "vars": {"ACCESS_TOKEN": "token", "API_KEY": "key", "SESSION": "cookie"},
            "headers": {
                "Authorization": "Bearer {{ACCESS_TOKEN}}",
                "X-API-Key": "{{API_KEY}}",
                "Cookie": "{{SESSION}}",
            },
        }
        self.assertEqual(execution_config.resolved_environment_headers(document), {
            "Authorization": "Bearer token",
            "X-API-Key": "key",
            "Cookie": "cookie",
        })

    def test_cross_language_source_scanner_is_not_java_only(self):
        scanner = load_script("analyze_source_logic")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "routes.py"
            source.write_text("@app.post('/things')\ndef create():\n    if not valid: raise ValueError()\n", encoding="utf-8")
            result = scanner.scan([root])
        self.assertEqual(result["source"]["languages"], ["python"])
        self.assertTrue(any(item["kind"] == "normal_entrypoint" for item in result["candidates"]))
        self.assertTrue(any(item["kind"] == "observable_branch" for item in result["candidates"]))

    def test_source_scanner_deduplicates_identical_candidates_in_one_file(self):
        scanner = load_script("analyze_source_logic")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "routes.py").write_text("if invalid: raise ValueError()\nif invalid: raise ValueError()\n", encoding="utf-8")
            result = scanner.scan([root], ["routes.py"])
        branches = [item for item in result["candidates"] if item["kind"] == "observable_branch"]
        self.assertEqual(len(branches), 1)

    def test_source_scanner_cli_blocks_unmapped_coverage_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            contracts = root / "contracts"
            (contracts / "modules").mkdir(parents=True)
            source.mkdir()
            (source / "routes.py").write_text(
                "def create():\n    raise BusinessException(143000)\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/analyze_source_logic.py"),
                    str(source),
                    "--output", str(root / "candidates.json"),
                    "--contracts-root", str(contracts),
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("cannot be uniquely mapped to an endpoint", result.stderr)

    def test_source_scanner_and_version_gate_cover_additional_http_languages(self):
        scanner = load_script("analyze_source_logic")
        checker = load_script("check_version_compatibility")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "routes.kt").write_text("@GetMapping(\"/things\")\nfun listThings() = 1\n", encoding="utf-8")
            (root / "routes.rb").write_text("get '/things' do\n  status 200\nend\n", encoding="utf-8")
            result = scanner.scan([root])
        self.assertEqual(result["source"]["languages"], ["kotlin", "ruby"])
        self.assertEqual(checker.classify(["src/routes.kt"], {}), "api-impact")
        self.assertEqual(checker.classify(["lib/things.rb"], {}), "api-impact")

    def test_version_impact_defaults_cover_non_java_source_files(self):
        checker = load_script("check_version_compatibility")
        self.assertEqual(checker.classify(["main.go"], {}), "api-impact")
        self.assertEqual(checker.classify(["src/routes.ts"], {}), "api-impact")
        self.assertEqual(checker.classify(["Program.cs"], {}), "api-impact")

    def test_preflight_uses_base_url_from_active_bruno_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, env_file = execution_fixture(
                root,
                environment={"BASE_URL": "http://127.0.0.1:18080/api"},
            )
            environment = dict(os.environ)
            environment.pop("BASE_URL", None)
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/runtime_preflight.py"),
                    "--execution-config",
                    str(config),
                    "--env-file",
                    str(env_file),
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=environment,
            )
            report = json.loads(result.stdout)
            self.assertEqual(report["base_url"], "http://127.0.0.1:18080/api")
            self.assertFalse(any("base URL is missing" in error for error in report["errors"]))

    def test_preflight_does_not_read_base_url_from_process_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, env_file = execution_fixture(root, environment={"BASE_URL": ""})
            environment = dict(os.environ, BASE_URL="http://127.0.0.1:18080")
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/runtime_preflight.py"),
                    "--execution-config",
                    str(config),
                    "--env-file",
                    str(env_file),
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=environment,
            )
            report = json.loads(result.stdout)
            self.assertTrue(any("base URL is missing" in error for error in report["errors"]))

    def test_preflight_requires_a_representative_route_for_execution_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, env_file = execution_fixture(root)
            openapi, static_results = static_preflight_inputs(root)
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/runtime_preflight.py"),
                    "--execution-config",
                    str(config),
                    "--env-file",
                    str(env_file),
                    "--bruno-cli",
                    "node",
                    "--openapi",
                    str(openapi),
                    "--static-results",
                    str(static_results),
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            report = json.loads(result.stdout)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertTrue(report["static_ready"])
            self.assertTrue(report["context_ready"])
            self.assertFalse(report["execution_ready"])
            self.assertIn("representative route is not configured", result.stdout)

    def test_preflight_does_not_claim_static_ready_without_static_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, env_file = execution_fixture(root)
            openapi = root / "openapi.json"
            openapi.write_text('{"openapi":"3.0.0","paths":{}}', encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/runtime_preflight.py"),
                    "--execution-config", str(config),
                    "--env-file", str(env_file),
                    "--openapi", str(openapi),
                    "--bruno-cli", "node",
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            report = json.loads(result.stdout)
            self.assertFalse(report["static_ready"])
            self.assertIn("static coverage results are missing", result.stdout)

    def test_strict_scenarios_require_a_dedicated_success_case(self):
        coverage = load_script("check_api_coverage")
        parser = load_script("parse_openapi")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = root / "module"
            bru = root / "bruno"
            module.mkdir()
            bru.mkdir()
            decisions = {
                category: ({"applicable": True} if category == "success" else {"applicable": False, "reason": "不适用"})
                for category in coverage.SCENARIO_CATEGORIES
            }
            endpoint = {
                "id": "THING_LIST",
                "method": "GET",
                "path": "/things",
                "case_ids": ["THING_LIST_VALIDATION"],
                "scenario_matrix": decisions,
            }
            case = {
                "id": "THING_LIST_VALIDATION",
                "endpoint_id": "THING_LIST",
                "scenario": "validation",
                "expected": {"http_status": 400},
                "assertions": [{"path": "$.message", "exists": True}],
                "bru": "01-参数校验失败.bru",
            }
            (module / "endpoints.yaml").write_text(json.dumps({"endpoints": [endpoint]}), encoding="utf-8")
            (module / "cases.yaml").write_text(json.dumps({"cases": [case]}), encoding="utf-8")
            (module / "flows.yaml").write_text(json.dumps({"flows": []}), encoding="utf-8")
            (module / "CASES.md").write_text(
                parser.render_module_document({"id": "things", "name": "事物查询"}, [endpoint], [case]),
                encoding="utf-8",
            )
            (bru / "01-参数校验失败.bru").write_text(
                "meta { name: THING_LIST_VALIDATION }\nget { url: {{BASE_URL}}/things }\n"
                "assert {\n  res.status: eq 400\n  res.body.message: exists\n}\n",
                encoding="utf-8",
            )
            _, errors = coverage.check_module("things", module, bru, None, require_scenarios=True)
            self.assertTrue(any("success=true has no dedicated linked case" in error for error in errors))

    def test_coverage_without_execution_is_draft_not_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contracts = root / "contracts"
            module = contracts / "things"
            bru = root / "bruno" / "things"
            module.mkdir(parents=True)
            bru.mkdir(parents=True)
            endpoint = {
                "version": 1,
                "module": "things",
                "endpoints": [{"id": "THING_LIST", "method": "GET", "path": "/things", "case_ids": ["THING_LIST_OK"]}],
            }
            case = {
                "version": 1,
                "module": "things",
                "cases": [{
                    "id": "THING_LIST_OK",
                    "title": "查询事物列表成功",
                    "endpoint_id": "THING_LIST",
                    "expected": {"http_status": 200, "business_code": 0},
                    "assertions": [{"path": "$.data", "equals": {}}],
                    "bru": "01-查询事物列表成功.bru",
                }],
            }
            (module / "endpoints.yaml").write_text(json.dumps(endpoint), encoding="utf-8")
            (module / "cases.yaml").write_text(json.dumps(case), encoding="utf-8")
            (module / "flows.yaml").write_text(json.dumps({"flows": []}), encoding="utf-8")
            (module / "CASES.md").write_text(
                "# Things 模块\n\n- 业务范围：查询事物列表\n\n"
                "## 模块内容\n\n## 接口清单\n\n## 自动化用例\n\n"
                "<!-- AUTO_CASES_START -->\n\n"
                "<!-- CASE_START: THING_LIST_OK -->\n"
                "### 查询事物列表成功\n\n"
                "简短描述：验证能够正常查询事物列表。\n\n"
                "```mermaid\nsequenceDiagram\n"
                "    participant C as 自动化用例\n"
                "    participant S as 业务服务\n"
                "    C->>S: GET /things\n"
                "    S-->>C: 返回查询结果\n"
                "```\n"
                "<!-- CASE_END: THING_LIST_OK -->\n"
                "<!-- AUTO_CASES_END -->\n",
                encoding="utf-8",
            )
            (bru / "01-查询事物列表成功.bru").write_text(
                "meta {\n  name: THING_LIST_OK\n  type: http\n  tags: [read-only]\n}\nget {\n  url: {{BASE_URL}}/things\n}\nassert {\n  res.status: eq 200\n  res.body.code: eq 0\n  res.body.data: eq {}\n}\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts/check_api_coverage.py"), str(contracts), str(root / "bruno"), "--all", "--json"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            report = json.loads(result.stdout)
            self.assertTrue(report["static_ok"])
            self.assertFalse(report["completion_ok"])
            self.assertEqual(report["status"], "draft")
            self.assertEqual(report["inventory_endpoints"], 1)
            self.assertEqual(report["endpoints_with_cases"], 1)
            evidence = root / "evidence.json"
            evidence.write_text(json.dumps({"executed": ["THING_LIST_OK"], "passed": ["THING_LIST_OK"]}), encoding="utf-8")
            verified = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/check_api_coverage.py"),
                    str(contracts),
                    str(root / "bruno"),
                    "--all",
                    "--results",
                    str(evidence),
                    "--json",
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(verified.returncode, 1)
            verified_report = json.loads(verified.stdout)
            self.assertFalse(verified_report["completion_ok"])
            self.assertEqual(verified_report["status"], "blocked")
            self.assertTrue(any("completion requires --require-scenarios" in error for error in verified_report["errors"]))

    def test_module_execution_reports_verified_without_changing_global_state(self):
        parser = load_script("parse_openapi")
        execution_config = load_script("execution_config")
        qa_lock = load_script("qa_lock")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contracts = root / "contracts" / "things"
            bru_root = root / "bruno"
            bru = bru_root / "things"
            contracts.mkdir(parents=True)
            bru.mkdir(parents=True)
            decisions = {
                category: (
                    {"applicable": True, "status": "confirmed"}
                    if category == "success"
                    else {"applicable": False, "status": "confirmed", "reason": "不适用"}
                )
                for category in load_script("check_api_coverage").SCENARIO_CATEGORIES
            }
            endpoint = {
                "id": "THING_LIST",
                "method": "GET",
                "path": "/things",
                "case_ids": ["THING_LIST_OK"],
                "scenario_matrix": decisions,
            }
            case = {
                "id": "THING_LIST_OK",
                "title": "查询事物列表成功",
                "endpoint_id": "THING_LIST",
                "scenario": "success",
                "risk": "read-only",
                "expected": {"http_status": 200, "business_code": 0},
                "assertions": [{"path": "$.data", "equals": {}}],
                "bru": "01-查询事物列表成功.bru",
            }
            (contracts / "endpoints.yaml").write_text(
                json.dumps({"module": "things", "endpoints": [endpoint]}, ensure_ascii=False),
                encoding="utf-8",
            )
            (contracts / "cases.yaml").write_text(
                json.dumps({"module": "things", "cases": [case]}, ensure_ascii=False),
                encoding="utf-8",
            )
            (contracts / "flows.yaml").write_text('{"flows": []}', encoding="utf-8")
            (contracts / "CASES.md").write_text(
                parser.render_module_document({"id": "things", "name": "事物查询"}, [endpoint], [case]),
                encoding="utf-8",
            )
            (bru / case["bru"]).write_text(
                "meta {\n  name: THING_LIST_OK\n  type: http\n  tags: [read-only]\n}\n"
                "get {\n  url: {{BASE_URL}}/things\n}\n"
                "assert {\n  res.status: eq 200\n  res.body.code: eq 0\n  res.body.data: eq {}\n}\n",
                encoding="utf-8",
            )
            (bru_root / "collection.bru").write_text(execution_config.COLLECTION_TEMPLATE, encoding="utf-8")
            config, _ = execution_fixture(root)
            openapi = contracts / "openapi.json"
            openapi.write_text(json.dumps({
                "openapi": "3.0.0",
                "paths": {"/things": {"get": {
                    "operationId": "THING_LIST",
                    "responses": {"200": {"description": "ok"}},
                }}},
            }), encoding="utf-8")
            generation_state = contracts / "generation-state.yaml"
            generation_state.write_text(
                json.dumps({"openapi_sha256": hashlib.sha256(openapi.read_bytes()).hexdigest()}),
                encoding="utf-8",
            )
            qa_lock.write(contracts)
            evidence = root / "evidence.json"
            evidence.write_text(json.dumps({"executed": [case["id"]], "passed": [case["id"]]}), encoding="utf-8")
            preflight = root / "preflight.json"
            preflight.write_text('{"status": "passed"}', encoding="utf-8")
            version_lock = contracts / "version-lock.yaml"
            version_lock.write_text("status: draft\n", encoding="utf-8")
            index = contracts / "index.yaml"
            index.write_text("generation_status: draft\n", encoding="utf-8")
            before = {path: path.read_bytes() for path in (generation_state, version_lock, index)}

            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/check_api_coverage.py"),
                    str(contracts),
                    str(bru_root),
                    "--module", "things",
                    "--results", str(evidence),
                    "--preflight-results", str(preflight),
                    "--openapi", str(openapi),
                    "--require-scenarios",
                    "--require-auth",
                    "--execution-config", str(config),
                    "--json",
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["module_status"], "verified")
            self.assertTrue(report["module_completion_ok"])
            self.assertEqual(report["status"], "draft")
            self.assertFalse(report["completion_ok"])
            self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_nested_json_body_is_compared_structurally(self):
        coverage = load_script("check_api_coverage")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            title = "批量更新 AC 信息成功"
            case = {
                "id": "AC_BATCH_UPDATE_SUCCESS",
                "title": title,
                "endpoint_id": "AC_BATCH_UPDATE",
                "bru": f"01-{title}.bru",
                "risk": "isolated-write",
                "request": {
                    "body_type": "application/json",
                    "body": {
                        "payload": {
                            "pdid": "p-1",
                            "items": [{"url": "https://example.invalid/ac", "totalNum": 2}],
                        }
                    },
                },
                "expected": {"http_status": 200},
                "assertions": [{"path": "$.data.accepted", "equals": True}],
            }
            (root / case["bru"]).write_text(
                "meta {\n  name: AC_BATCH_UPDATE_SUCCESS\n  type: http\n  tags: [isolated-write]\n}\n"
                "post {\n  url: {{BASE_URL}}/ac/batch\n  body: json\n}\n"
                "body:json {\n"
                "  {\n    \"payload\": {\n      \"pdid\": \"p-1\",\n"
                "      \"items\": [{\"url\": \"https://example.invalid/ac\", \"totalNum\": 2}]\n    }\n  }\n"
                "}\nassert {\n  res.status: eq 200\n  res.body.data.accepted: eq true\n}\n",
                encoding="utf-8",
            )
            endpoint = {"id": "AC_BATCH_UPDATE", "method": "POST", "path": "/ac/batch"}
            _, _, _, errors = coverage.case_files([case], root, {endpoint["id"]: endpoint})
            self.assertEqual(errors, [])

            changed = (root / case["bru"]).read_text(encoding="utf-8").replace('"totalNum": 2', '"totalNum": 3')
            (root / case["bru"]).write_text(changed, encoding="utf-8")
            _, _, _, errors = coverage.case_files([case], root, {endpoint["id"]: endpoint})
            self.assertTrue(any("body content does not match" in error for error in errors))

    def test_invalid_bruno_json_body_has_an_explicit_parse_error(self):
        coverage = load_script("check_api_coverage")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            title = "新增 AC 信息成功"
            case = {
                "id": "AC_CREATE_SUCCESS",
                "title": title,
                "endpoint_id": "AC_CREATE",
                "bru": f"01-{title}.bru",
                "risk": "isolated-write",
                "request": {"body_type": "application/json", "body": {"pdid": "p-1"}},
                "expected": {"http_status": 200},
                "assertions": [{"path": "$.data.id", "equals": "1"}],
            }
            (root / case["bru"]).write_text(
                "meta {\n  name: AC_CREATE_SUCCESS\n  type: http\n  tags: [isolated-write]\n}\n"
                "post {\n  url: {{BASE_URL}}/ac\n  body: json\n}\n"
                "body:json {\n  {\"pdid\": \"p-1\",}\n}\n"
                "assert {\n  res.status: eq 200\n  res.body.data.id: eq \"1\"\n}\n",
                encoding="utf-8",
            )
            endpoint = {"id": "AC_CREATE", "method": "POST", "path": "/ac"}
            _, _, _, errors = coverage.case_files([case], root, {endpoint["id"]: endpoint})
            self.assertTrue(any("invalid JSON at line" in error and "column" in error for error in errors), errors)

    def test_seed_titles_use_endpoint_business_semantics(self):
        parser = load_script("parse_openapi")
        cases = (
            ({"id": "LIST", "method": "GET", "operation_id": "listByPage", "tags": ["AC"], "responses": {"200": {}}}, "分页查询 AC 信息成功"),
            ({"id": "GROUP", "method": "POST", "operation_id": "batchChangeGroup", "tags": ["AC"], "responses": {"200": {}}}, "批量变更 AC 分组成功"),
            ({"id": "DELETE", "method": "DELETE", "operation_id": "batchDelete", "tags": ["AC"], "responses": {"200": {}}}, "批量删除 AC 信息成功"),
            ({"id": "IMPORT", "method": "POST", "operation_id": "importAc", "tags": ["AC"], "responses": {"202": {}}}, "导入 AC 文件成功受理"),
        )
        for endpoint, expected in cases:
            self.assertEqual(parser.seed_contract_cases(endpoint)[0]["title"], expected)

    def test_scenario_matrix_is_inferred_from_real_contract_features(self):
        parser = load_script("parse_openapi")
        endpoint = {
            "id": "AC_LIST",
            "method": "GET",
            "path": "/v1/admin/ac",
            "security": [{"bearerAuth": []}],
            "parameters": [
                {"name": "pageNum", "in": "query", "schema": {"type": "integer", "minimum": 1}},
                {"name": "status", "in": "query", "schema": {"type": "string", "enum": ["ON", "OFF"]}},
                {"name": "operatorInfo", "in": "header", "required": True, "schema": {"type": "string"}},
            ],
            "responses": {"200": {}, "400": {}},
        }
        matrix = parser.inferred_scenario_matrix(endpoint)
        self.assertTrue(matrix["authentication"]["applicable"])
        self.assertFalse(matrix["authorization"]["applicable"])
        self.assertEqual(matrix["authorization"]["status"], "confirmed")
        self.assertTrue(matrix["query"]["applicable"])
        self.assertTrue(matrix["validation"]["applicable"])
        self.assertTrue(all(item["status"] in {"inferred", "confirmed"} for item in matrix.values()))
        seeded_ids = {case["id"] for case in parser.seed_contract_cases(endpoint)}
        self.assertIn("AC_LIST_MISSING_OPERATORINFO", seeded_ids)
        self.assertIn("AC_LIST_INVALID_STATUS", seeded_ids)
        self.assertIn("AC_LIST_BOUNDARY_PAGENUM", seeded_ids)

    def test_script_bundle_sync_is_versioned_and_checkable(self):
        manager = load_script("scripts_manager")
        with tempfile.TemporaryDirectory() as directory:
            qa_root = Path(directory) / "qa"
            changed = manager.sync_scripts(qa_root, ROOT / "scripts")
            self.assertTrue(changed)
            self.assertEqual(manager.check_scripts(qa_root, ROOT / "scripts"), [])
            metadata = load_script("manifest_io").load_data(qa_root / "scripts" / "scripts-version.yaml")
            for key in ("skill_version", "scripts_version", "source_repository", "scripts_sha256", "synchronized_at", "files"):
                self.assertIn(key, metadata)
            (qa_root / "scripts" / "run_bruno.py").write_text("outdated", encoding="utf-8")
            self.assertTrue(any("outdated" in error for error in manager.check_scripts(qa_root, ROOT / "scripts")))

    def test_scope_risks_and_confirmations_are_enforced(self):
        runner = load_script("run_bruno")
        with tempfile.TemporaryDirectory() as directory:
            contracts = Path(directory) / "contracts"
            module = contracts / "modules" / "APP"
            module.mkdir(parents=True)
            (module / "endpoints.yaml").write_text(json.dumps({
                "endpoints": [{"id": "APP_GET", "method": "GET"}, {"id": "APP_POST", "method": "POST"}],
            }), encoding="utf-8")
            (module / "cases.yaml").write_text(json.dumps({
                "cases": [
                    {"id": "APP_GET_OK", "endpoint_id": "APP_GET", "risk": "read-only"},
                    {"id": "APP_POST_OK", "endpoint_id": "APP_POST", "risk": "isolated-write"},
                ],
            }), encoding="utf-8")
            risks = runner.scope_risks(contracts, "APP")
            self.assertEqual(risks, {"read-only", "isolated-write"})
            self.assertIn("--confirm-write", runner.confirmation_error(risks, False, False, False))
            self.assertIsNone(runner.confirmation_error(risks, True, False, False))
            self.assertIn(
                "--confirm-destructive",
                runner.confirmation_error({"destructive"}, True, False, False),
            )
            self.assertIn(
                "--confirm-external",
                runner.confirmation_error({"external-side-effect"}, False, False, False),
            )

    def test_materializer_removes_obsolete_plan_tags(self):
        materializer = load_script("materialize_missing_bru")
        content = (
            "meta {\n  name: APP_LIST_OK\n  type: http\n"
            "  tags: [read-only, plan-smoke, plan-full]\n}\n"
            "get {\n  url: {{baseUrl}}/app\n}\n"
        )
        updated = materializer.ensure_execution_tags(content, ["read-only"])
        self.assertIn("tags: [read-only]", updated)
        self.assertNotIn("plan-", updated)
        rendered = materializer.render_case(
            {
                "id": "APP_LIST_OK",
                "title": "查询应用列表成功",
                "risk": "read-only",
                "_execution_tags": ["plan-smoke"],
                "expected": {"http_status": 200},
                "assertions": [{"path": "$.data", "equals": {}}],
            },
            {"method": "GET", "path": "/app"},
        )
        self.assertIn("tags: [read-only]", rendered)
        self.assertNotIn("plan-", rendered)

    def test_full_matrix_adds_query_and_declared_file_constraint_cases(self):
        parser = load_script("parse_openapi")
        endpoint = {
            "id": "APP_IMPORT",
            "method": "POST",
            "path": "/app/import",
            "parameters": [
                {"name": "pageSize", "in": "query", "schema": {"type": "integer", "minimum": 1}},
                {"name": "status", "in": "query", "schema": {"type": "string", "enum": ["ON"]}},
            ],
            "request_body": {
                "content": {
                    "multipart/form-data": {
                        "schema": {
                            "type": "object",
                            "required": ["file"],
                            "properties": {
                                "file": {
                                    "type": "string",
                                    "format": "binary",
                                    "x-allowed-extensions": ["csv"],
                                    "maxLength": 1024,
                                },
                            },
                        },
                    },
                },
            },
            "responses": {"200": {}, "400": {}},
        }
        contract_ids = {case["id"] for case in parser.seed_contract_cases(endpoint, "contract-draft")}
        full_ids = {case["id"] for case in parser.seed_contract_cases(endpoint, "full-matrix")}
        self.assertNotIn("APP_IMPORT_QUERY_STATUS", contract_ids)
        self.assertIn("APP_IMPORT_QUERY_STATUS", full_ids)
        self.assertIn("APP_IMPORT_QUERY_COMBINED", full_ids)
        self.assertIn("APP_IMPORT_INVALID_FILE_EXTENSION", full_ids)
        self.assertIn("APP_IMPORT_OVERSIZED_UPLOAD_FILE", full_ids)

    def test_incremental_generation_skips_unchanged_module_and_marks_manual_case(self):
        parser = load_script("parse_openapi")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = root / "contracts" / "openapi.json"
            module_map = root / "contracts" / "module-map.yaml"
            output = root / "contracts" / "modules"
            spec.parent.mkdir(parents=True)
            document = {
                "openapi": "3.0.0",
                "paths": {"/ac": {"get": {
                    "operationId": "listByPage",
                    "tags": ["AC"],
                    "responses": {"200": {}},
                }}},
            }
            spec.write_text(json.dumps(document), encoding="utf-8")
            module_map.write_text(json.dumps({"modules": [{"id": "ac", "name": "AC", "directory": "AC", "swagger_tags": ["AC"]}]}), encoding="utf-8")
            manifest = parser.extract(spec, document)
            parser.write_partitioned(manifest, module_map, output, seed_cases=True)
            second = parser.write_partitioned(manifest, module_map, output, seed_cases=True, incremental=True)
            self.assertEqual(second["changed_modules"], [])
            self.assertEqual(second["skipped_modules"], ["ac"])
            cases_path = output / "AC" / "cases.yaml"
            cases = parser.load_document(cases_path)
            cases["cases"][0]["description"] = "人工调整后的业务说明"
            cases_path.write_text(parser.render_manifest(cases, cases_path), encoding="utf-8")
            third = parser.write_partitioned(manifest, module_map, output, seed_cases=True, incremental=True)
            self.assertIn(cases["cases"][0]["id"], third["manual_review_cases"])
            preserved = parser.load_document(cases_path)["cases"][0]
            self.assertEqual(preserved["description"], "人工调整后的业务说明")
            self.assertTrue(preserved["manual_review"])
            self.assertTrue((root / "contracts" / "generation-state.yaml").is_file())
            self.assertTrue((root / "contracts" / "qa-lock.yaml").is_file())

    def test_source_scanner_marks_business_errors_and_required_headers_for_coverage(self):
        scanner = load_script("analyze_source_logic")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "AcController.java").write_text(
                'class AcController {\n'
                '  private final AcApplication application;\n'
                '  @DeleteMapping\n'
                '  public Object deleteAc() { request.getHeader("operatorInfo"); return application.deleteAc(); }\n'
                '}\n',
                encoding="utf-8",
            )
            (root / "AcApplication.java").write_text(
                'class AcApplication {\n'
                '  private final AcDomainService domainService;\n'
                '  public Object deleteAc() { return domainService.deleteAc(); }\n'
                '}\n',
                encoding="utf-8",
            )
            (root / "AcDomainService.java").write_text(
                'class AcDomainService {\n'
                '  public Object deleteAc() { throw new MnoTrafficApplicationException(MnoTrafficErrorCodeEnum.GROUP_IN_USE); }\n'
                '}\n',
                encoding="utf-8",
            )
            (root / "MnoTrafficErrorCodeEnum.java").write_text(
                'enum MnoTrafficErrorCodeEnum { GROUP_IN_USE(143000); }\n',
                encoding="utf-8",
            )
            result = scanner.scan([root])
        required = [item for item in result["candidates"] if item.get("coverage_required")]
        self.assertTrue(any(item.get("required_header") == "operatorInfo" for item in required))
        self.assertTrue(any("143000" in item.get("expected_business_codes", []) for item in required))

    def test_source_enhancement_seeds_required_header_and_business_error_drafts(self):
        scanner = load_script("analyze_source_logic")
        parser = load_script("parse_openapi")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "AcController.java").write_text(
                'class AcController {\n'
                '  private final AcApplication application;\n'
                '  @DeleteMapping\n'
                '  public Object deleteAc() { request.getHeader("operatorInfo"); return application.deleteAc(); }\n'
                '}\n',
                encoding="utf-8",
            )
            (source / "AcApplication.java").write_text(
                'class AcApplication {\n'
                '  private final AcDomainService domainService;\n'
                '  public Object deleteAc() { return domainService.deleteAc(); }\n'
                '}\n',
                encoding="utf-8",
            )
            (source / "AcDomainService.java").write_text(
                'class AcDomainService {\n'
                '  public Object deleteAc() { throw new MnoTrafficApplicationException(MnoTrafficErrorCodeEnum.GROUP_IN_USE); }\n'
                '}\n',
                encoding="utf-8",
            )
            (source / "MnoTrafficErrorCodeEnum.java").write_text(
                'enum MnoTrafficErrorCodeEnum { GROUP_IN_USE(143000); }\n',
                encoding="utf-8",
            )
            contracts = root / "qa" / "contracts"
            module = contracts / "modules" / "AC"
            module.mkdir(parents=True)
            (contracts / "security-profile.yaml").write_text(
                yaml.safe_dump({
                    "admin-operator-context": {
                        "type": "audit-context",
                        "header": "operatorInfo",
                        "probe_result": {"missing_header_status": 400},
                    },
                }, sort_keys=False),
                encoding="utf-8",
            )
            endpoint = {
                "id": "AC_DELETE",
                "method": "DELETE",
                "path": "/v0/admin/ac/{id}",
                "operation_id": "deleteAc",
                "tags": ["AC"],
                "responses": {"200": {}, "400": {}},
            }
            endpoint["scenario_matrix"] = parser.inferred_scenario_matrix(endpoint)
            (module / "endpoints.yaml").write_text(
                yaml.safe_dump({
                    "version": 1,
                    "module": "ac",
                    "name": "AC",
                    "swagger_tag": "AC",
                    "endpoints": [endpoint],
                }, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
            (module / "cases.yaml").write_text(
                yaml.safe_dump({"version": 1, "module": "ac", "swagger_tag": "AC", "cases": []}),
                encoding="utf-8",
            )
            (module / "logic.yaml").write_text(
                yaml.safe_dump({"version": 1, "module": "ac", "swagger_tag": "AC", "logic": []}),
                encoding="utf-8",
            )
            result = scanner.scan([source])
            self.assertEqual(scanner.apply_candidates(result, contracts), [])
            cases = parser.load_document(module / "cases.yaml")["cases"]
            header_case = next(case for case in cases if case["id"].endswith("MISSING_OPERATORINFO"))
            self.assertEqual(header_case["request"]["omit_common_headers"], ["operatorInfo"])
            self.assertEqual(header_case["scenario"], "validation")
            self.assertTrue(any(case.get("expected", {}).get("business_code") == "143000" for case in cases))
            updated_endpoint = parser.load_document(module / "endpoints.yaml")["endpoints"][0]
            self.assertFalse(updated_endpoint["scenario_matrix"]["authentication"]["applicable"])
            self.assertTrue(updated_endpoint["scenario_matrix"]["validation"]["applicable"])
            self.assertTrue(updated_endpoint["scenario_matrix"]["business_error"]["applicable"])
            logic = parser.load_document(module / "logic.yaml")["logic"]
            self.assertTrue(all(item.get("case_ids") for item in logic))

    def test_legacy_execution_config_is_migrated_to_environment_headers(self):
        execution_config = load_script("execution_config")
        with tempfile.TemporaryDirectory() as directory:
            qa_root = Path(directory) / "qa"
            execution = qa_root / "execution"
            environments = execution / "environments"
            environments.mkdir(parents=True)
            (qa_root / "qa.yaml").write_text("version: 1\ntooling: shared-cli\n", encoding="utf-8")
            (execution / "config.yaml").write_text(
                "active_environment: local\n"
                "auth:\n"
                "  mode: bearer\n"
                "  token_env: AUTH_TOKEN\n"
                "custom_headers:\n"
                "  operatorInfo:\n"
                "    env: OPERATOR_INFO\n",
                encoding="utf-8",
            )
            (execution / "README.md").write_text("run --plan smoke\nrun --risk read-only\n", encoding="utf-8")
            (environments / "local.bru").write_text(
                "vars {\n  BASE_URL: http://localhost\n  AUTH_TOKEN: token\n  OPERATOR_INFO: operator\n}\n",
                encoding="utf-8",
            )
            execution_config.initialize_execution_layout(qa_root)
            self.assertEqual(
                execution_config.load_execution_config(execution / "config.yaml"),
                {
                    "active_environment": "local",
                    "tooling": "shared-cli",
                    "coverage_profile": "full-matrix",
                    "sign": {"provider": "disabled"},
                },
            )
            self.assertFalse((qa_root / "qa.yaml").exists())
            self.assertFalse((qa_root / "scripts").exists())
            readme = (execution / "README.md").read_text(encoding="utf-8")
            self.assertIn("--all", readme)
            self.assertNotIn("--plan", readme)
            self.assertNotIn("--risk", readme)
            environment = execution_config.load_bruno_environment_document(environments / "local.bru")
            self.assertEqual(environment["headers"]["Authorization"], "Bearer {{AUTH_TOKEN}}")
            self.assertEqual(environment["headers"]["operatorInfo"], "{{OPERATOR_INFO}}")

    def test_shared_cli_mode_keeps_asset_only_layout(self):
        execution_config = load_script("execution_config")
        with tempfile.TemporaryDirectory() as directory:
            qa_root = Path(directory) / "qa"
            execution_config.initialize_execution_layout(qa_root, local_scripts=False)
            self.assertFalse((qa_root / "scripts").exists())
            self.assertFalse((qa_root / "qa.yaml").exists())
            self.assertFalse((qa_root / "execution" / "plans.yaml").exists())
            self.assertIn("tooling: shared-cli", (qa_root / "execution" / "config.yaml").read_text(encoding="utf-8"))
            self.assertIn("mno-bruno-qa run", (qa_root / "execution" / "run.bat").read_text(encoding="utf-8"))
            self.assertIn("mno-bruno-qa run", (qa_root / "execution" / "run.sh").read_text(encoding="utf-8"))

    def test_public_cli_routes_help_to_the_subcommand(self):
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "mno_bruno_qa.py"), "generate", "--help"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("--incremental", completed.stdout)
        self.assertIn("--shared-cli", completed.stdout)
        run_help = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "mno_bruno_qa.py"), "run", "--help"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(run_help.returncode, 0)
        self.assertIn("--all", run_help.stdout)
        self.assertIn("--module", run_help.stdout)
        self.assertIn("--confirm-destructive", run_help.stdout)
        self.assertNotIn("--plan", run_help.stdout)
        self.assertNotIn("--risk", run_help.stdout)

    def test_qa_lock_accepts_yaml_openapi(self):
        qa_lock = load_script("qa_lock")
        with tempfile.TemporaryDirectory() as directory:
            contracts = Path(directory)
            openapi = contracts / "openapi.yaml"
            openapi.write_text("openapi: 3.0.0\npaths: {}\n", encoding="utf-8")
            digest = hashlib.sha256(openapi.read_bytes()).hexdigest()
            state = {
                "openapi_sha256": digest,
                "modules": {},
                "cases": {},
            }
            (contracts / "generation-state.yaml").write_text(
                yaml.safe_dump(state, sort_keys=False),
                encoding="utf-8",
            )
            qa_lock.write(contracts)
            self.assertEqual(qa_lock.check(contracts), [])

    def test_qa_lock_detects_case_asset_drift(self):
        qa_lock = load_script("qa_lock")
        with tempfile.TemporaryDirectory() as directory:
            contracts = Path(directory)
            module = contracts / "modules" / "things"
            module.mkdir(parents=True)
            case = {"id": "THING_OK", "endpoint_id": "THING", "title": "查询事物成功"}
            cases_path = module / "cases.yaml"
            cases_path.write_text(
                yaml.safe_dump({"module": "things", "cases": [case]}, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
            (contracts / "generation-state.yaml").write_text(
                yaml.safe_dump({"openapi_sha256": None, "modules": {}, "cases": {}}, sort_keys=False),
                encoding="utf-8",
            )
            qa_lock.refresh_generation_state_cases(contracts)
            qa_lock.write(contracts)
            self.assertEqual(qa_lock.check(contracts), [])
            case["title"] = "人工修改后的查询事物成功"
            cases_path.write_text(
                yaml.safe_dump({"module": "things", "cases": [case]}, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
            self.assertTrue(any("case fingerprints" in error for error in qa_lock.check(contracts)))


if __name__ == "__main__":
    unittest.main()
