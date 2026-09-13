from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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

    def test_require_auth_rejects_missing_or_invalid_config(self):
        coverage = load_script("check_api_coverage")
        self.assertTrue(any("unsupported authentication mode" in error for error in coverage.validate_auth_config_document({"mode": "unknown"})))
        self.assertTrue(any("requires modes.custom.headers" in error for error in coverage.validate_auth_config_document({"mode": "custom", "modes": {"custom": {"enabled": True}}})))
        self.assertEqual(coverage.auth_markers({"seres.sign": False}), ())
        self.assertTrue(any("custom header X-Token env" in error for error in coverage.validate_auth_config_document({
            "mode": "custom",
            "modes": {"custom": {"enabled": True, "headers": {"X-Token": {}}}},
        })))

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
            parser.write_partitioned(parser.extract(spec_path, document), map_path, output_dir)
            self.assertTrue((root / "contracts" / "security-profile.yaml").is_file())
            auth_text = (root / "contracts" / "request-auth.yaml").read_text(encoding="utf-8")
            self.assertIn("seres.sign: true", auth_text)
            self.assertIn("timestamp: timestamp", auth_text)
            index = parser.load_document(root / "contracts" / "index.yaml")
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
                    "assertions": [{"path": "$.data", "exists": True}],
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
            (root / "contracts" / "request-auth.yaml").write_text(
                json.dumps({"mode": "none"}),
                encoding="utf-8",
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
                    "--json",
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(checked.returncode, 0, checked.stdout)
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

    def test_materializer_uses_chinese_summary_for_case_filename(self):
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
                    "endpoint_id": "USER_CREATE",
                    "expected": {"http_status": 200},
                    "assertions": [{"path": "$.data", "exists": True}],
                }]
            }), encoding="utf-8")
            (module / "CASES.md").write_text(
                "# 用户管理\n\n这里是需要保留的人工业务说明。\n",
                encoding="utf-8",
            )
            (root / "contracts" / "module-map.yaml").write_text(json.dumps({
                "modules": [{"id": "用户管理", "name": "用户管理", "business_scope": "账号生命周期管理"}],
            }), encoding="utf-8")
            config = root / "auth.json"
            config.write_text(json.dumps({"mode": "none"}), encoding="utf-8")
            created = materializer.materialize(root / "contracts", root / "bruno", auth_config_path=config)
            bru_files = [path for path in created if path.suffix == ".bru"]
            self.assertEqual(len(bru_files), 1)
            self.assertEqual(bru_files[0].parent.name, "用户管理")
            self.assertTrue(bru_files[0].name.startswith("创建用户-"))
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

    def test_seres_sign_alias_and_signature_headers_are_configurable(self):
        scripts_path = str(ROOT / "scripts")
        sys.path.insert(0, scripts_path)
        try:
            materializer = load_script("materialize_missing_bru")
        finally:
            sys.path.remove(scripts_path)
        config = {
            "mode": "seres.sign",
            "seres.sign": True,
            "base_url_env": "SERVICE_URL",
            "modes": {
                "seres.sign": {
                    "enabled": True,
                    "algorithm": "SHA256",
                    "signature": {
                        "parameters": {
                            "url": "request.path",
                            "body": "request.body",
                            "query": "request.query",
                            "timestamp": "ts",
                            "secret_key_env": "SIGN_SECRET",
                            "access_key_env": "SIGN_ACCESS",
                        },
                    },
                    "headers": {"sign": "X-Sign", "timestamp": "X-Time", "accesskey": "X-Access"},
                }
            },
        }
        self.assertEqual(materializer.auth_mode(config), "seres-sign")
        script = materializer.auth_script(config)
        self.assertIn('bru.getEnvVar("SIGN_SECRET")', script)
        self.assertIn('params["ts"]', script)
        self.assertIn('req.setHeader("X-Sign"', script)
        self.assertIn('req.setHeader("X-Time"', script)
        self.assertIn('req.setHeader("X-Access"', script)
        self.assertIn("&${secretKey}", script)

    def test_custom_header_objects_and_preflight_variables_are_supported(self):
        scripts_path = str(ROOT / "scripts")
        sys.path.insert(0, scripts_path)
        try:
            materializer = load_script("materialize_missing_bru")
            preflight = load_script("runtime_preflight")
            coverage = load_script("check_api_coverage")
        finally:
            sys.path.remove(scripts_path)
        config = {
            "mode": "custom",
            "modes": {
                "custom": {
                    "enabled": True,
                    "headers": {"X-Service-Token": {"env": "SERVICE_TOKEN", "prefix": "Bearer"}},
                }
            },
        }
        self.assertIn("Bearer ${token_0}", materializer.auth_script(config))
        with self.assertRaises(ValueError):
            preflight.configured_auth_envs(Path("/dev/null"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request-auth.yaml"
            path.write_text(json.dumps(config), encoding="utf-8")
            self.assertEqual(preflight.configured_auth_envs(path), ["BASE_URL", "SERVICE_TOKEN"])
            sign_path = Path(directory) / "seres-auth.yaml"
            sign_path.write_text(json.dumps({
                "mode": "seres.sign",
                "modes": {"seres.sign": {
                    "enabled": True,
                    "signature": {"parameters": {"secret_key_env": "SIGN_SECRET", "access_key_env": "SIGN_ACCESS"}},
                    "extra_headers": {"X-Tenant": {"env": "TENANT_ID"}},
                }},
            }), encoding="utf-8")
            self.assertEqual(
                preflight.configured_auth_envs(sign_path),
                ["BASE_URL", "SIGN_SECRET", "SIGN_ACCESS", "TENANT_ID"],
            )
        markers = coverage.auth_markers({
            "mode": "seres.sign",
            "modes": {"seres.sign": {"enabled": True, "signature": {"parameters": {"timestamp": "ts"}}, "headers": {"sign": "X-Sign", "timestamp": "X-Time", "accesskey": "X-Access"}}},
        })
        self.assertIn("ts", markers)
        self.assertIn("X-Sign", markers)

    def test_path_parameters_and_existing_pre_request_script_are_preserved(self):
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
        original = "script:pre-request {\n  console.log('业务前置');\n}\nassert {\n}\n"
        merged = materializer.ensure_auth_script(original, materializer.DEFAULT_AUTH_CONFIG)
        self.assertEqual(merged.count("script:pre-request"), 1)
        self.assertIn("console.log('业务前置')", merged)
        self.assertIn(materializer.AUTH_MARKER, merged)
        self.assertIn('bru.getEnvVar("SECRET_KEY")', merged)
        bearer = materializer.auth_script({
            "mode": "bearer",
            "modes": {"bearer": {"enabled": True, "token_env": "OLD_TOKEN"}},
        })
        switched = materializer.ensure_auth_script(bearer, materializer.DEFAULT_AUTH_CONFIG)
        self.assertNotIn("OLD_TOKEN", switched)
        self.assertIn(f"{materializer.AUTH_MARKER} seres-sign", switched)
        self.assertEqual(switched.count(materializer.AUTH_MARKER), 1)
        changed_config = json.loads(json.dumps(materializer.DEFAULT_AUTH_CONFIG))
        changed_config["modes"]["seres-sign"]["signature"]["parameters"]["secret_key_env"] = "ROTATED_SECRET"
        rotated = materializer.ensure_auth_script(merged, changed_config)
        self.assertIn('bru.getEnvVar("ROTATED_SECRET")', rotated)
        self.assertNotIn('bru.getEnvVar("SECRET_KEY")', rotated)
        disabled = materializer.ensure_auth_script(rotated, {"mode": "none"})
        self.assertNotIn(materializer.AUTH_MARKER, disabled)
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
        rendered = materializer.render_case(case, {"id": "UPLOAD", "method": "POST", "path": "/upload"}, {"mode": "none", "base_url_env": "SERVICE_URL"})
        self.assertIn("body: text", rendered)
        self.assertIn("body:text {", rendered)
        self.assertIn("hello world", rendered)
        self.assertIn("res.headers.ETag: eq", rendered)
        self.assertIn("res.headers['X-Trace-Id']: eq", rendered)
        self.assertIn("res.body: contains", rendered)
        inferred = materializer.render_case(
            {"id": "XML_CASE", "request": {"body": "<x/>"}, "assertions": []},
            {"id": "XML", "method": "POST", "path": "/xml", "request_body": {"content": {"application/xml": {}}}},
            {"mode": "none"},
        )
        self.assertIn("body: xml", inferred)
        self.assertIn("body:xml {", inferred)

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
            {"mode": "none"},
        )
        self.assertIn('body:json {\n{"name":"alice"}\n}', rendered)
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

    def test_runtime_preflight_rejects_missing_and_unknown_auth_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unknown = root / "unknown.yaml"
            unknown.write_text("mode: unknown\n", encoding="utf-8")
            environment = dict(os.environ, BASE_URL="http://127.0.0.1:18080")
            for config in (unknown, root / "missing.yaml"):
                result = subprocess.run(
                    [sys.executable, str(ROOT / "scripts/runtime_preflight.py"), "--auth-config", str(config)],
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    env=environment,
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

    def test_oauth_and_cookie_auth_are_environment_backed(self):
        materializer = load_script("materialize_missing_bru")
        preflight = load_script("runtime_preflight")
        oauth = {"mode": "oauth2", "modes": {"oauth2": {"enabled": True, "token_env": "OAUTH_TOKEN", "prefix": "Token"}}}
        self.assertIn('bru.getEnvVar("OAUTH_TOKEN")', materializer.auth_script(oauth))
        self.assertIn('req.setHeader("Authorization", `${prefix} ${token}`)', materializer.auth_script(oauth))
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "auth.json"
            config.write_text(json.dumps({"mode": "cookie", "modes": {"cookie": {"enabled": True, "cookie_env": "SESSION"}}}), encoding="utf-8")
            self.assertEqual(preflight.configured_auth_envs(config), ["BASE_URL", "SESSION"])

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

    def test_preflight_uses_configured_base_url_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "request-auth.yaml"
            config.write_text(json.dumps({"mode": "none", "base_url_env": "SERVICE_URL"}), encoding="utf-8")
            environment = dict(os.environ)
            environment.pop("BASE_URL", None)
            environment["SERVICE_URL"] = "http://127.0.0.1:18080/api"
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/runtime_preflight.py"),
                    "--auth-config",
                    str(config),
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

    def test_preflight_cli_base_url_overrides_base_url_environment_requirement(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "request-auth.yaml"
            config.write_text(json.dumps({"mode": "none"}), encoding="utf-8")
            environment = dict(os.environ)
            environment.pop("BASE_URL", None)
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/runtime_preflight.py"),
                    "--base-url",
                    "http://127.0.0.1:18080",
                    "--auth-config",
                    str(config),
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=environment,
            )
            report = json.loads(result.stdout)
            self.assertNotIn("required environment variable is missing: BASE_URL", report["errors"])

    def test_preflight_route_probes_are_optional_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "request-auth.yaml"
            config.write_text(json.dumps({"mode": "none"}), encoding="utf-8")
            environment = dict(os.environ)
            environment["BASE_URL"] = "http://127.0.0.1:18080"
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts/runtime_preflight.py"), "--auth-config", str(config)],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=environment,
            )
            self.assertEqual(result.returncode, 0)
            self.assertNotIn("public route is not configured", result.stdout)
            self.assertNotIn("admin auth baseline is not configured", result.stdout)

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
                "bru": "validation.bru",
            }
            (module / "endpoints.yaml").write_text(json.dumps({"endpoints": [endpoint]}), encoding="utf-8")
            (module / "cases.yaml").write_text(json.dumps({"cases": [case]}), encoding="utf-8")
            (module / "flows.yaml").write_text(json.dumps({"flows": []}), encoding="utf-8")
            (module / "CASES.md").write_text(
                parser.render_module_document({"id": "things", "name": "事物查询"}, [endpoint], [case]),
                encoding="utf-8",
            )
            (bru / "validation.bru").write_text(
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
                    "endpoint_id": "THING_LIST",
                    "expected": {"http_status": 200, "business_code": 0},
                    "assertions": [{"path": "$.data", "exists": True}],
                    "bru": "list.bru",
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
            (bru / "list.bru").write_text(
                "meta { name: THING_LIST_OK }\nget { url: http://localhost/things }\nassert {\n  res.status: eq 200\n  res.body.code: eq 0\n  res.body.data: exists\n}\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts/check_api_coverage.py"), str(contracts), str(root / "bruno"), "--json"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(result.returncode, 0)
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


if __name__ == "__main__":
    unittest.main()
