from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

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
    openapi.write_text(json.dumps({
        "openapi": "3.0.0",
        "paths": {},
        "provenance": {
            "status": "verified",
            "application_sha": "test",
            "application_pid": os.getpid(),
        },
    }), encoding="utf-8")
    digest = hashlib.sha256(openapi.read_bytes()).hexdigest()
    (root / "generation-state.yaml").write_text(json.dumps({
        "openapi_sha256": digest,
        "modules": {},
        "cases": {},
    }), encoding="utf-8")
    load_script("qa_lock").write(root)
    static_results.write_text(json.dumps({
        "report_version": 2,
        "check_profile": "full-matrix-strict",
        "static_ok": True,
        "openapi_sha256": digest,
        "scenario_matrix_checked": True,
        "constraint_obligations_checked": True,
        "exact_assertions_checked": True,
        "variables_checked": True,
        "source_mapping_checked": True,
        "qa_lock_checked": True,
        "business_version_checked": True,
    }), encoding="utf-8")
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
        "sign": {"provider": "sha256", "version": "v1"} if sign in {"sha256", "sha256-v1"} else {"provider": "disabled"},
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
    def test_process_liveness_probe_does_not_terminate_current_windows_process(self):
        preflight = load_script("runtime_preflight")
        self.assertTrue(preflight.process_is_running(os.getpid()))
        self.assertFalse(preflight.process_is_running(-1))

    def test_bruno_cli_probe_classifies_not_found_timeout_and_unsupported(self):
        preflight = load_script("runtime_preflight")
        with mock.patch.object(preflight, "resolve_executable", return_value=None):
            missing = preflight.bruno_cli_probe("missing-bru")
        self.assertEqual(missing["timeout_seconds"], 60.0)
        self.assertEqual(missing["conclusion"], "not_found")
        self.assertIn("was not found", preflight.bruno_cli_failure(missing))

        with (
            mock.patch.object(preflight, "resolve_executable", return_value="C:/npm/bru.cmd"),
            mock.patch.object(preflight, "command_argv", return_value="bru --version"),
            mock.patch.object(
                preflight.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(
                    "bru --version", 90, output="starting", stderr="still loading",
                ),
            ),
        ):
            timed_out = preflight.bruno_cli_probe("bru", 90)
        self.assertEqual(timed_out["conclusion"], "timeout")
        self.assertEqual(timed_out["stdout"], "starting")
        self.assertEqual(timed_out["stderr"], "still loading")
        self.assertIn("timed out after 90 seconds", preflight.bruno_cli_failure(timed_out))

        completed = subprocess.CompletedProcess(["bru", "--version"], 0, "3.9.2\n", "legacy warning\n")
        with (
            mock.patch.object(preflight, "resolve_executable", return_value="C:/npm/bru.cmd"),
            mock.patch.object(preflight, "command_argv", return_value="bru --version"),
            mock.patch.object(preflight.subprocess, "run", return_value=completed),
        ):
            unsupported = preflight.bruno_cli_probe("bru")
        self.assertEqual(unsupported["conclusion"], "unsupported_version")
        self.assertEqual(unsupported["version"], "3.9.2")
        self.assertEqual(unsupported["stderr"], "legacy warning")
        self.assertIn("is unsupported", preflight.bruno_cli_failure(unsupported))

    def test_windows_executable_resolution_prefers_cmd_then_exe(self):
        command_execution = load_script("command_execution")
        fake_os = mock.Mock()
        fake_os.name = "nt"
        with (
            mock.patch.object(command_execution, "os", fake_os),
            mock.patch.object(
                command_execution.shutil,
                "which",
                side_effect=lambda value: {
                    "bru.cmd": "C:/npm/bru.cmd",
                    "bru.exe": "C:/bin/bru.exe",
                    "bru": "C:/ambiguous/bru",
                    "bru.ps1": "C:/npm/bru.ps1",
                }.get(value),
            ) as which,
        ):
            self.assertEqual(command_execution.resolve_executable("bru"), "C:/npm/bru.cmd")
        which.assert_called_once_with("bru.cmd")

        with (
            mock.patch.object(command_execution, "os", fake_os),
            mock.patch.object(
                command_execution.shutil,
                "which",
                side_effect=lambda value: "C:/bin/bru.exe" if value == "bru.exe" else None,
            ),
        ):
            self.assertEqual(command_execution.resolve_executable("bru"), "C:/bin/bru.exe")

    def test_success_rejects_http_style_status_when_error_code_is_business_failure(self):
        coverage = load_script("check_api_coverage")
        case = {
            "id": "THING_GET_SUCCESS",
            "scenario": "success",
            "expected": {"http_status": 200},
            "assertions": [
                {"path": "$.status", "equals": 200},
                {"path": "$.errorCode", "equals": 100009},
                {"path": "$.data.id", "equals": "thing-1"},
            ],
        }
        self.assertTrue(coverage.success_assertion_errors(case))
        case["assertions"][1]["equals"] = 0
        self.assertEqual(coverage.success_assertion_errors(case), [])

    def test_exists_assertion_is_draft_only_even_with_an_exact_assertion(self):
        coverage = load_script("check_api_coverage")
        errors = coverage.case_completion_errors({
            "id": "THING_GET",
            "status": "runnable",
            "assertions": [
                {"path": "$.data.id", "equals": "thing-1"},
                {"path": "$.data.name", "exists": True},
            ],
        })
        self.assertTrue(any("existence-only" in error for error in errors), errors)

    def test_response_envelope_prefers_error_code_and_requires_result_evidence(self):
        parser = load_script("parse_openapi")
        endpoint = {
            "responses": {"200": {"content": {"application/json": {"example": {
                "status": 200,
                "errorCode": 100009,
                "errorMsg": "failed",
                "data": {"id": "thing-1"},
            }}}}},
        }
        self.assertEqual(parser.exact_response_assertions(endpoint, 200), [])
        endpoint["responses"]["200"]["content"]["application/json"]["example"].update({
            "errorCode": 0,
            "errorMsg": "success",
        })
        self.assertEqual(parser.exact_response_assertions(endpoint, 200), [
            {"path": "$.errorCode", "equals": 0},
            {"path": "$.data.id", "equals": "thing-1"},
        ])

    def test_nested_body_and_file_obligations_are_all_seeded(self):
        parser = load_script("parse_openapi")
        endpoint = {
            "id": "IMPORT_CREATE",
            "method": "POST",
            "path": "/imports",
            "request_body": {"content": {"multipart/form-data": {"schema": {
                "type": "object",
                "required": ["profile", "file"],
                "properties": {
                    "profile": {
                        "type": "object",
                        "required": ["name"],
                        "properties": {"name": {
                            "type": "string", "pattern": "^[a-z]+$", "minLength": 2, "maxLength": 8,
                        }},
                    },
                    "file": {
                        "type": "string", "format": "binary", "x-min-size": 1,
                        "x-max-size": 1024, "x-allowed-extensions": ["csv"],
                        "x-allowed-mime-types": ["text/csv"],
                    },
                },
            }}}},
            "responses": {
                "200": {
                    "content": {"application/json": {"example": {"code": 0, "data": {"id": "1"}}}},
                },
                "400": {
                    "content": {"application/json": {"example": {"code": 4001, "msg": "invalid"}}},
                },
            },
        }
        endpoint["obligations"] = parser.constraint_obligations(endpoint)
        cases = parser.seed_contract_cases(endpoint, "full-matrix")
        obligations = {item["id"] for item in endpoint["obligations"]}
        covered = {coverage_id for case in cases for coverage_id in case.get("coverage_ids", [])}
        self.assertEqual(obligations - covered, set())
        success = next(case for case in cases if case["scenario"] == "success")
        self.assertEqual(success["request"]["body"]["file"], {"file": "{{IMPORT_CREATE_FILE_LEGAL}}"})

    def test_query_cases_require_result_specific_assertions(self):
        coverage = load_script("check_api_coverage")
        case = {
            "id": "THING_QUERY",
            "scenario": "query",
            "expected": {"http_status": 200},
            "request": {"query": {"name": "x"}},
            "assertions": [{"path": "$.code", "equals": 0}, {"path": "$.msg", "equals": "ok"}],
        }
        self.assertTrue(coverage.query_assertion_errors(case))
        case["assertions"].append({"path": "$.data.records", "length": 1})
        self.assertEqual(coverage.query_assertion_errors(case), [])

    @unittest.skipUnless(os.name == "nt", "Windows command wrapper regression")
    def test_windows_cmd_execution_preserves_spaces_json_and_shell_metacharacters(self):
        command_execution = load_script("command_execution")
        with tempfile.TemporaryDirectory(prefix="bru cmd ") as directory:
            root = Path(directory)
            helper = root / "args.py"
            helper.write_text("import json, sys; print(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
            wrapper = root / "bru.cmd"
            wrapper.write_text(f'@echo off\r\n"{sys.executable}" "{helper}" %*\r\n', encoding="ascii")
            arguments = ["--env-var", 'RUNTIME={"name":"a b&c"}']
            with mock.patch.dict(os.environ, {"PATH": str(root) + os.pathsep + os.environ["PATH"]}):
                completed = subprocess.run(
                    command_execution.command_argv("bru", *arguments),
                    check=False, capture_output=True, text=True, encoding="utf-8",
                )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout), arguments)

    def test_version_digest_uses_only_current_workspace_files(self):
        checker = load_script("check_version_compatibility")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            changed = root / "中文 空格.txt"
            changed.write_text("one\n", encoding="utf-8")
            baseline = checker.source_digest(root)
            metadata = root / ".git"
            metadata.mkdir()
            (metadata / "ignored").write_text("history", encoding="utf-8")
            self.assertEqual(checker.source_digest(root), baseline)
            changed.write_text("two\n", encoding="utf-8")
            self.assertNotEqual(checker.source_digest(root), baseline)

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
            "endpoint_id": "USER_CREATE",
            "scenario": "missing_tenant",
            "request": {"body": {"name": "x"}},
            "assertions": [{"path": "$.msg", "contains": "tenant"}],
        }
        second = dict(first, id="USER_CREATE_MISSING_TENANT_2")
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
            "cli_timeout": 60.0,
            "sign": {"provider": "disabled"},
        })
        for invalid in (
            {"version": 1, **valid},
            {**valid, "active_environment": "local.bru"},
            {**valid, "active_environment": "local:bad"},
            {**valid, "sign": {"provider": "sha256"}},
            {**valid, "sign": {"provider": "unknown"}},
            {**valid, "auth": {}},
            {**valid, "custom_headers": {}},
            {**valid, "cli_timeout": 0},
            {**valid, "cli_timeout": "60"},
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
            output_dir = root / "data" / "contracts" / "modules"
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
            load_script("qa_constraints").ensure_rule_library(root)
            (legacy_environment / "local.bru").write_text(
                "vars {\n  BASE_URL: http://127.0.0.1:18080\n  TENANT_ID:\n}\n",
                encoding="utf-8",
            )
            parser.write_partitioned(parser.extract(spec_path, document), map_path, output_dir)
            endpoints = parser.load_document(output_dir / "things" / "endpoints.yaml")
            self.assertEqual(endpoints["endpoints"][0]["tag_description"], "Thing management")
            self.assertTrue((root / "data" / "contracts" / "security-profile.yaml").is_file())
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
            self.assertIn("bruno_api_test_generator.py\" run", (root / "execution" / "run.bat").read_text(encoding="utf-8"))
            self.assertIn('bruno_api_test_generator.py" run', (root / "execution" / "run.sh").read_text(encoding="utf-8"))
            self.assertEqual(
                sorted(path.name for path in (root / "execution" / "environments").glob("*.bru")),
                ["local.bru"],
            )
            self.assertTrue((root / "data" / "bruno" / "collection.bru").is_file())
            self.assertTrue((root / "data" / "bruno" / "bruno.json").is_file())
            index = parser.load_document(root / "data" / "contracts" / "index.yaml")
            self.assertIn("execution_config_file", index)
            self.assertNotIn("request_auth_file", index)
            self.assertEqual(index["generation_status"], "draft")
            self.assertEqual(index["inventory_endpoints"], 1)
            overview = (root / "data" / "contracts" / "README.md").read_text(encoding="utf-8")
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
                    "expected": {"http_status": 200, "business_code": 0},
                    "assertions": [
                        {"path": "$.code", "equals": 0},
                        {"path": "$.data", "equals": {}},
                    ],
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
            refreshed_index = parser.load_document(root / "data" / "contracts" / "index.yaml")
            self.assertEqual(refreshed_index["generated_cases"], 1)
            self.assertEqual(refreshed_index["modules"][0]["case_count"], 1)
            self.assertIn(
                "<!-- CASE_START: THING_LIST_OK -->",
                (output_dir / "things" / "CASES.md").read_text(encoding="utf-8"),
            )
            constraints = load_script("qa_constraints")
            constraints.ensure_rule_library(root)
            (root / "data" / "constraints" / "source-rules.yaml").write_text(
                yaml.safe_dump({"version": 1, "field_rules": []}), encoding="utf-8",
            )
            (root / "data" / "contracts" / "exception-profile.yaml").write_text(
                yaml.safe_dump({"version": 1, "handlers": []}), encoding="utf-8",
            )
            (output_dir / "things" / "value-resolution.yaml").write_text(
                yaml.safe_dump({"version": 1, "fields": []}), encoding="utf-8",
            )
            materialized = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/materialize_missing_bru.py"),
                    str(root / "data" / "contracts"),
                    str(root / "data" / "bruno"),
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
                    str(root / "data" / "contracts"),
                    str(root / "data" / "bruno"),
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
            self.assertTrue((root / "data" / "contracts" / "modules" / "用户管理" / "endpoints.yaml").is_file())
            self.assertIn("用户管理", (root / "data" / "contracts" / "README.md").read_text(encoding="utf-8"))

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
            {"id": "product_query_success", "title": "查询商品/库存信息成功"},
            1,
        )
        self.assertEqual(path.name, "01-查询商品库存信息成功.bru")
        rendered = materializer.render_case(
            {"id": "product_query_success", "title": "查询商品库存信息成功"},
            {"method": "GET", "path": "/products"},
        )
        self.assertIn("name: product_query_success", rendered)
        self.assertIn("url: {{baseUrl}}/products", rendered)

    def test_materializer_rejects_explicit_case_id_filename(self):
        materializer = load_script("materialize_missing_bru")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = root / "contracts" / "modules" / "商品查询"
            module.mkdir(parents=True)
            (module / "endpoints.yaml").write_text(json.dumps({"endpoints": [{
                "id": "PRODUCT", "method": "GET", "path": "/products", "summary": "查询商品库存",
            }]}), encoding="utf-8")
            (module / "cases.yaml").write_text(json.dumps({"cases": [{
                "id": "product_query_success", "endpoint_id": "PRODUCT",
                "title": "查询商品库存成功", "bru": "01-product_query_success.bru",
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
                {"id": "product_query_success", "title": "product_query_success"},
                1,
            )

    def test_materializer_reports_filename_collision(self):
        materializer = load_script("materialize_missing_bru")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = root / "contracts" / "modules" / "商品查询"
            module.mkdir(parents=True)
            (module / "endpoints.yaml").write_text(json.dumps({"endpoints": [{
                "id": "PRODUCT", "method": "GET", "path": "/products", "summary": "查询商品库存",
            }]}), encoding="utf-8")
            cases = [
                {"id": case_id, "endpoint_id": "PRODUCT", "title": "查询商品库存", "sequence": 1}
                for case_id in ("product_one", "product_two")
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
            module = root / "contracts" / "modules" / "商品查询"
            module.mkdir(parents=True)
            (module / "endpoints.yaml").write_text(json.dumps({"endpoints": [{
                "id": "PRODUCT", "method": "GET", "path": "/products", "summary": "查询商品库存",
            }]}), encoding="utf-8")
            (module / "cases.yaml").write_text(json.dumps({"cases": [{
                "id": "product_query_success", "endpoint_id": "PRODUCT", "title": "查询商品库存成功",
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
            self.assertEqual(cases_document["cases"][0]["bru"], "01-查询商品库存成功.bru")
            second_changes = materializer.materialize(
                root / "contracts", root / "bruno", execution_config_path=config
            )
            self.assertEqual([path.name for path in first], ["01-查询商品库存成功.bru"])
            self.assertEqual(second_changes, [])
            original = first[0].read_text(encoding="utf-8")
            first[0].write_text(original.replace("/products", "/wrong"), encoding="utf-8")
            drift = materializer.materialize(
                root / "contracts",
                root / "bruno",
                dry_run=True,
                execution_config_path=config,
                module_filter="商品查询",
                check=True,
            )
            self.assertIn(first[0].name, {path.name for path in drift})
            self.assertIn("/wrong", first[0].read_text(encoding="utf-8"))

    def test_materializer_syncs_case_only_changes_and_blocks_concurrent_edits(self):
        materializer = load_script("materialize_missing_bru")
        manifests = load_script("manifest_io")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = root / "contracts" / "modules" / "用户"
            module.mkdir(parents=True)
            (module / "endpoints.yaml").write_text(json.dumps({"endpoints": [{
                "id": "USER_GET", "method": "GET", "path": "/users",
            }]}), encoding="utf-8")
            cases_path = module / "cases.yaml"
            cases_path.write_text(json.dumps({"cases": [{
                "id": "USER_GET_OK", "title": "查询用户成功", "endpoint_id": "USER_GET",
                "expected": {"http_status": 200},
                "assertions": [{"path": "$.data.id", "equals": "one"}],
            }]}), encoding="utf-8")
            config, _ = execution_fixture(root)
            materializer.materialize(root / "contracts", root / "bruno", execution_config_path=config)
            case = manifests.load_data(cases_path)
            case["cases"][0]["assertions"][0]["equals"] = "two"
            cases_path.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
            materializer.materialize(root / "contracts", root / "bruno", execution_config_path=config)
            bru = next(path for path in (root / "bruno" / "用户").glob("*.bru"))
            self.assertIn('res.body.data.id: eq "two"', bru.read_text(encoding="utf-8"))

            case = manifests.load_data(cases_path)
            case["cases"][0]["assertions"][0]["equals"] = "three"
            cases_path.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
            bru.write_text(bru.read_text(encoding="utf-8").replace("/users", "/manual"), encoding="utf-8")
            with self.assertRaises(SystemExit) as raised:
                materializer.materialize(root / "contracts", root / "bruno", execution_config_path=config)
            conflict = json.loads(str(raised.exception))
            self.assertEqual(conflict["error"], "materialize_conflict")
            self.assertTrue(conflict["diff"])

    def test_existing_filename_number_survives_manifest_reordering(self):
        materializer = load_script("materialize_missing_bru")
        coverage = load_script("check_api_coverage")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = root / "contracts" / "modules" / "商品查询"
            module.mkdir(parents=True)
            endpoint = {"id": "PRODUCT", "method": "GET", "path": "/products"}
            (module / "endpoints.yaml").write_text(json.dumps({"endpoints": [endpoint]}), encoding="utf-8")
            cases = [
                {
                    "id": "product_first", "endpoint_id": "PRODUCT", "title": "查询第一个商品成功",
                    "expected": {"http_status": 200}, "assertions": [{"path": "$.data", "equals": 1}],
                },
                {
                    "id": "product_second", "endpoint_id": "PRODUCT", "title": "查询第二个商品成功",
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
            self.assertEqual(reordered[0]["bru"], "02-查询第二个商品成功.bru")
            self.assertEqual(reordered[1]["bru"], "01-查询第一个商品成功.bru")
            _, _, _, errors = coverage.case_files(reordered, root / "bruno" / "商品查询")
            self.assertFalse(any("filename must match" in error for error in errors))

    def test_coverage_enforces_chinese_case_title_and_exempts_collection_config(self):
        coverage = load_script("check_api_coverage")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "environments").mkdir()
            case = {
                "id": "product_query_success",
                "title": "查询商品库存成功",
                "bru": "01-查询商品库存成功.bru",
                "expected": {"http_status": 200},
                "assertions": [{"path": "$.data", "equals": 1}],
            }
            (root / "01-查询商品库存成功.bru").write_text(
                "meta {\n  name: product_query_success\n  type: http\n}\nget {\n  url: {{BASE_URL}}/products\n}\nassert {\n  res.status: eq 200\n  res.body.data: eq 1\n}\n",
                encoding="utf-8",
            )
            (root / "environments" / "local.bru").write_text("vars { BASE_URL: http://localhost }\n", encoding="utf-8")
            (root / "collection.bru").write_text("meta { name: collection }\n", encoding="utf-8")
            covered, _, _, errors = coverage.case_files([case], root)
            self.assertEqual(covered, {"product_query_success"})
            self.assertEqual(errors, [])

            case["bru"] = "01-product_query_success.bru"
            (root / "01-product_query_success.bru").write_text(
                "meta {\n  name: product_query_success\n  type: http\n}\nget {\n  url: {{BASE_URL}}/products\n}\nassert {\n  res.status: eq 200\n}\n",
                encoding="utf-8",
            )
            _, _, _, errors = coverage.case_files([case], root)
            self.assertTrue(any("sanitized Chinese case.title" in error for error in errors))

    def test_coverage_rejects_a_different_chinese_title(self):
        coverage = load_script("check_api_coverage")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = {
                "id": "product_query_success",
                "title": "查询商品库存成功",
                "bru": "01-删除商品成功.bru",
                "expected": {"http_status": 200},
                "assertions": [{"path": "$.data", "equals": 1}],
            }
            (root / case["bru"]).write_text(
                "meta {\n  name: product_query_success\n  type: http\n}\nget {\n  url: {{BASE_URL}}/products\n}\nassert {\n  res.status: eq 200\n  res.body.data: eq 1\n}\n",
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
                {"name": "X-Tenant-Id", "in": "header", "required": True, "schema": {"type": "string"}},
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
        header_case = next(case for case in seeded if case["id"].endswith("MISSING_X_TENANT_ID"))
        self.assertEqual(header_case["request"]["omit_common_headers"], ["X-Tenant-Id"])

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
                headers={"X-Tenant-Id": "{{X_TENANT_ID}}"},
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
            self.assertEqual(report["status"], "failed")

    def test_preflight_uses_configured_cli_timeout_and_command_line_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, env_file = execution_fixture(root)
            document = json.loads(config.read_text(encoding="utf-8"))
            document["cli_timeout"] = 75
            config.write_text(json.dumps(document), encoding="utf-8")
            openapi, static_results = static_preflight_inputs(root)

            def invoke(*extra: str) -> dict[str, object]:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "scripts/runtime_preflight.py"),
                        "--execution-config", str(config),
                        "--env-file", str(env_file),
                        "--bruno-cli", "node",
                        "--openapi", str(openapi),
                        "--static-results", str(static_results),
                        *extra,
                    ],
                    check=False, capture_output=True, text=True, encoding="utf-8",
                )
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                return json.loads(result.stdout)

            configured = invoke()
            configured_cli = next(check for check in configured["checks"] if check["name"] == "bruno_cli")
            self.assertEqual(configured_cli["timeout_seconds"], 75.0)
            self.assertEqual(configured_cli["conclusion"], "ready")
            self.assertTrue(configured_cli["resolved_executable"])
            self.assertTrue(configured_cli["node_version"])
            self.assertIn(configured_cli["version"], configured_cli["stdout"])
            self.assertGreaterEqual(configured_cli["elapsed_seconds"], 0)

            overridden = invoke("--cli-timeout", "90")
            overridden_cli = next(check for check in overridden["checks"] if check["name"] == "bruno_cli")
            self.assertEqual(overridden_cli["timeout_seconds"], 90.0)

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

    def test_database_steps_render_setup_assertion_cleanup_and_enable_developer_sandbox(self):
        materializer = load_script("materialize_missing_bru")
        runner = load_script("run_bruno")
        coverage = load_script("check_api_coverage")
        case = {
            "id": "THING_EXECUTE",
            "title": "执行事物成功",
            "endpoint_id": "THING_EXECUTE_ENDPOINT",
            "bru": "01-执行事物成功.bru",
            "expected": {"http_status": 200, "business_code": 0},
            "assertions": [{"path": "$.code", "equals": 0}],
            "database_steps": [
                {
                    "phase": "setup",
                    "reason": "missing_prerequisite_api",
                    "engine": "mysql",
                    "evidence": ["ThingRepository.java:42"],
                    "script": "await connection.execute('INSERT INTO thing(id) VALUES (?)', [thingId]);",
                    "cleanup": "await connection.execute('DELETE FROM thing WHERE id = ?', [thingId]);",
                },
                {
                    "phase": "assertion",
                    "reason": "missing_response_state",
                    "engine": "mongodb",
                    "evidence": ["ThingDocument.java:18"],
                    "expected": {"status": "DONE"},
                    "script": "test('persisted state', () => expect(document.status).to.equal('DONE'));",
                },
            ],
        }
        endpoint = {"id": "THING_EXECUTE_ENDPOINT", "method": "POST", "path": "/things/execute"}
        rendered = materializer.render_case(case, endpoint)
        self.assertIn("database-setup mysql", rendered)
        self.assertIn("database-assertion mongodb", rendered)
        self.assertIn("database-cleanup mysql", rendered)
        self.assertIn("try {", rendered)
        self.assertIn("} finally {", rendered)
        self.assertTrue(runner.requires_developer_sandbox([case]))
        self.assertFalse(runner.requires_developer_sandbox([{"id": "HTTP_ONLY"}]))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / case["bru"]).write_text(rendered, encoding="utf-8")
            _, _, _, errors = coverage.case_files([case], root, {endpoint["id"]: endpoint})
            self.assertEqual(errors, [])

    def test_database_steps_are_limited_to_the_two_supported_reasons(self):
        constraints = load_script("qa_constraints")
        coverage = load_script("check_api_coverage")
        valid = {
            "id": "THING_EXECUTE",
            "scenario": "success",
            "expected": {"business_code": 0},
            "assertions": [{"path": "$.code", "equals": 0}],
            "database_steps": [{
                "phase": "assertion",
                "reason": "missing_response_state",
                "engine": "elasticsearch",
                "evidence": "ThingIndexRepository.java:27",
                "expected": {"status": "DONE"},
                "script": "test('indexed state', () => expect(hit._source.status).to.equal('DONE'));",
            }],
        }
        self.assertEqual(constraints.database_access_errors(valid), [])
        self.assertEqual(coverage.case_completion_errors(valid), [])
        self.assertEqual(coverage.success_assertion_errors(valid), [])

        invalid = dict(valid)
        invalid["database_steps"] = [{
            "phase": "setup",
            "reason": "missing_response_state",
            "engine": "mysql",
            "evidence": [],
            "script": "UPDATE thing SET status = 'READY'",
        }]
        errors = constraints.database_access_errors(invalid)
        self.assertTrue(any("reason must be missing_prerequisite_api" in error for error in errors))
        self.assertTrue(any("cleanup" in error for error in errors))

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
            blocked = subprocess.run(
                [
                    sys.executable, str(ROOT / "scripts/check_version_compatibility.py"),
                    str(repo), str(contracts), "--phase", "before-execute",
                ],
                check=False, capture_output=True, text=True, encoding="utf-8",
            )
            self.assertEqual(blocked.returncode, 3)
            self.assertIn("expected current", blocked.stdout)

            (repo / "main.go").write_text("package main\n// reviewed\n", encoding="utf-8")
            completion = root / "completion.json"
            completion.write_text(json.dumps({"completion_ok": True, "status": "verified"}), encoding="utf-8")
            forged = subprocess.run(
                [
                    sys.executable, str(ROOT / "scripts/check_version_compatibility.py"),
                    str(repo), str(contracts), "--write", "--tests-adapted",
                    "--completion-report", str(completion),
                ],
                check=False, capture_output=True, text=True, encoding="utf-8",
            )
            self.assertEqual(forged.returncode, 3)
            self.assertIn("full-matrix-strict", forged.stdout)
            completion.write_text(json.dumps({
                "report_version": 2,
                "check_profile": "full-matrix-strict",
                "scenario_matrix_checked": True,
                "constraint_obligations_checked": True,
                "exact_assertions_checked": True,
                "variables_checked": True,
                "source_mapping_checked": True,
                "qa_lock_checked": True,
                "business_version_checked": True,
                "execution_scope": "all",
                "completion_ok": True,
                "status": "verified",
                "errors": [],
            }), encoding="utf-8")
            updated = subprocess.run(
                [
                    sys.executable, str(ROOT / "scripts/check_version_compatibility.py"),
                    str(repo), str(contracts), "--write", "--tests-adapted",
                    "--completion-report", str(completion),
                ],
                check=False, capture_output=True, text=True, encoding="utf-8",
            )
            self.assertEqual(updated.returncode, 0, updated.stdout + updated.stderr)
            lock = load_script("manifest_io").load_data(contracts / "version-lock.yaml")
            self.assertEqual(lock["status"], "current")
            self.assertEqual(lock["business"]["baseline_status"], "current")

    def test_module_materialization_does_not_touch_other_modules_or_index(self):
        materializer = load_script("materialize_missing_bru")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contracts = root / "data" / "contracts"
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
            bruno_root = root / "data" / "bruno"
            bruno_root.mkdir()
            (bruno_root / "collection.bru").write_text(materializer.COLLECTION_TEMPLATE, encoding="utf-8")
            before = index.read_text(encoding="utf-8")
            materializer.materialize(
                contracts,
                bruno_root,
                execution_config_path=config,
                module_filter="模块一",
            )
            self.assertTrue((root / "data" / "bruno" / "模块一" / "01-查询模块一成功.bru").is_file())
            self.assertFalse((root / "data" / "bruno" / "模块二").exists())
            self.assertEqual(index.read_text(encoding="utf-8"), before)

    def test_full_and_module_scoped_materialization_are_identical(self):
        materializer = load_script("materialize_missing_bru")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contracts = root / "data" / "contracts"
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
            scoped.mkdir()
            (scoped / "collection.bru").write_text(materializer.COLLECTION_TEMPLATE, encoding="utf-8")
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

    def test_optional_sha256_sign_algorithm_and_header_names_are_fixed(self):
        execution_config = load_script("execution_config")
        config = execution_config.validate_execution_config({
            "active_environment": "local",
            "tooling": "project-scripts",
            "coverage_profile": "full-matrix",
            "sign": {"provider": "sha256", "version": "v1"},
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
            "headers": {"Authorization": "Bearer {{SERVICE_TOKEN}}", "X-Tenant-Id": "qa"},
        }
        payload = json.loads(execution_config.runtime_payload(config, environment))
        self.assertEqual(payload["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(payload["headers"]["X-Tenant-Id"], "qa")
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
            "id": "USER_WITHOUT_TENANT",
            "request": {"omit_common_headers": ["X-Tenant-Id", "X-Trace"]},
        }
        merged = materializer.ensure_request_script(original, excluded_case)
        self.assertEqual(merged.count("script:pre-request"), 1)
        self.assertIn("console.log('业务前置')", merged)
        self.assertIn('req.deleteHeaders(["X-Tenant-Id", "X-Trace"])', merged)
        disabled = materializer.ensure_request_script(merged, {"id": "USER_NORMAL"})
        self.assertNotIn(materializer.OMIT_MARKER, disabled)
        self.assertIn("console.log('业务前置')", disabled)

    def test_top_level_openapi_path_example_is_materialized_as_a_concrete_value(self):
        parser = load_script("parse_openapi")
        endpoint = {
            "id": "USER_GET",
            "method": "GET",
            "path": "/users/{id}",
            "parameters": [{
                "name": "id", "in": "path", "required": True,
                "example": "user-42", "schema": {"type": "string"},
            }],
            "responses": {"200": {"content": {"application/json": {"example": {
                "code": 0, "data": {"id": "user-42"},
            }}}}},
        }
        success = parser.seed_contract_cases(endpoint)[0]
        self.assertEqual(success["request"]["path_parameters"]["id"], "user-42")

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
                self.assertIn('"status": "failed"', result.stdout)

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
            report.write_text(json.dumps({
                "report_version": 2,
                "check_profile": "full-matrix-strict",
                "scenario_matrix_checked": True,
                "constraint_obligations_checked": True,
                "exact_assertions_checked": True,
                "variables_checked": True,
                "source_mapping_checked": True,
                "qa_lock_checked": True,
                "business_version_checked": True,
                "execution_scope": "all",
                "status": "verified",
                "completion_ok": True,
                "errors": [],
            }), encoding="utf-8")
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
            load_script("qa_constraints").ensure_rule_library(root)
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
                "meta {\n  name: THING_LIST_OK\n  type: http\n}\nget {\n  url: {{BASE_URL}}/things\n}\nassert {\n  res.status: eq 200\n  res.body.code: eq 0\n  res.body.data: eq {}\n}\n",
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
            self.assertEqual(verified_report["status"], "failed")
            self.assertTrue(any("completion requires --require-scenarios" in error for error in verified_report["errors"]))

    def test_module_execution_reports_verified_without_changing_global_state(self):
        parser = load_script("parse_openapi")
        execution_config = load_script("execution_config")
        qa_lock = load_script("qa_lock")
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            qa_root = project / "qa"
            (project / "business.txt").write_text("business\n", encoding="utf-8")
            checker = load_script("check_version_compatibility")
            business_digest = checker.source_digest(project)
            business_sha = f"filesystem:{business_digest[:16]}"
            contracts = qa_root / "data" / "contracts" / "things"
            bru_root = qa_root / "data" / "bruno"
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
            (contracts / "value-resolution.yaml").write_text(
                '{"version": 1, "module": "things", "fields": []}', encoding="utf-8",
            )
            (contracts.parent / "exception-profile.yaml").write_text(
                '{"version": 1, "handlers": []}', encoding="utf-8",
            )
            (contracts / "CASES.md").write_text(
                parser.render_module_document({"id": "things", "name": "事物查询"}, [endpoint], [case]),
                encoding="utf-8",
            )
            (bru / case["bru"]).write_text(
                "meta {\n  name: THING_LIST_OK\n  type: http\n}\n"
                "get {\n  url: {{BASE_URL}}/things\n}\n"
                "assert {\n  res.status: eq 200\n  res.body.code: eq 0\n  res.body.data: eq {}\n}\n",
                encoding="utf-8",
            )
            (bru_root / "collection.bru").write_text(execution_config.COLLECTION_TEMPLATE, encoding="utf-8")
            config, _ = execution_fixture(qa_root)
            openapi = contracts / "openapi.json"
            openapi.write_text(json.dumps({
                "openapi": "3.0.0",
                "provenance": {
                    "status": "verified",
                    "application_sha": business_sha,
                    "application_pid": os.getpid(),
                },
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
            qa_constraints = load_script("qa_constraints")
            qa_constraints.ensure_rule_library(qa_root)
            qa_constraints.write_module_lock(qa_root, "things")
            evidence = qa_root / "evidence.json"
            evidence.write_text(json.dumps({"executed": [case["id"]], "passed": [case["id"]]}), encoding="utf-8")
            preflight = qa_root / "preflight.json"
            preflight.write_text(json.dumps({
                "version": 2,
                "status": "runnable",
                "check_profile": "full-matrix-strict",
                "static_report_version": 2,
                "static_ready": True,
                "context_ready": True,
                "execution_ready": True,
                "openapi_sha256": hashlib.sha256(openapi.read_bytes()).hexdigest(),
                "errors": [],
            }), encoding="utf-8")
            version_lock = contracts / "version-lock.yaml"
            version_lock.write_text(json.dumps({
                "status": "current",
                "business": {
                    "commit": business_sha,
                    "source_digest": business_digest,
                },
            }), encoding="utf-8")
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

            openapi_document = json.loads(openapi.read_text(encoding="utf-8"))
            openapi_document["provenance"]["status"] = "contract_provenance_unverified"
            openapi.write_text(json.dumps(openapi_document), encoding="utf-8")
            generation_state.write_text(
                json.dumps({"openapi_sha256": hashlib.sha256(openapi.read_bytes()).hexdigest()}),
                encoding="utf-8",
            )
            qa_lock.write(contracts)
            warned = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/check_api_coverage.py"),
                    str(contracts),
                    str(bru_root),
                    "--module", "things",
                    "--openapi", str(openapi),
                    "--require-scenarios",
                    "--require-auth",
                    "--execution-config", str(config),
                    "--json",
                ],
                check=False, capture_output=True, text=True, encoding="utf-8",
            )
            warned_report = json.loads(warned.stdout)
            self.assertEqual(warned.returncode, 0, warned.stdout + warned.stderr)
            self.assertTrue(warned_report["static_ok"])
            self.assertTrue(any("provenance" in item for item in warned_report["warnings"]))

    def test_nested_json_body_is_compared_structurally(self):
        coverage = load_script("check_api_coverage")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            title = "批量更新资源成功"
            case = {
                "id": "RESOURCE_BATCH_UPDATE_SUCCESS",
                "title": title,
                "endpoint_id": "RESOURCE_BATCH_UPDATE",
                "bru": f"01-{title}.bru",
                "request": {
                    "body_type": "application/json",
                    "body": {
                        "payload": {
                            "pdid": "p-1",
                            "items": [{"url": "https://example.invalid/resources/1", "totalNum": 2}],
                        }
                    },
                },
                "expected": {"http_status": 200},
                "assertions": [{"path": "$.data.accepted", "equals": True}],
            }
            (root / case["bru"]).write_text(
                "meta {\n  name: RESOURCE_BATCH_UPDATE_SUCCESS\n  type: http\n}\n"
                "post {\n  url: {{BASE_URL}}/resources/batch\n  body: json\n}\n"
                "body:json {\n"
                "  {\n    \"payload\": {\n      \"pdid\": \"p-1\",\n"
                "      \"items\": [{\"url\": \"https://example.invalid/resources/1\", \"totalNum\": 2}]\n    }\n  }\n"
                "}\nassert {\n  res.status: eq 200\n  res.body.data.accepted: eq true\n}\n",
                encoding="utf-8",
            )
            endpoint = {"id": "RESOURCE_BATCH_UPDATE", "method": "POST", "path": "/resources/batch"}
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
            title = "新增资源成功"
            case = {
                "id": "RESOURCE_CREATE_SUCCESS",
                "title": title,
                "endpoint_id": "RESOURCE_CREATE",
                "bru": f"01-{title}.bru",
                "request": {"body_type": "application/json", "body": {"pdid": "p-1"}},
                "expected": {"http_status": 200},
                "assertions": [{"path": "$.data.id", "equals": "1"}],
            }
            (root / case["bru"]).write_text(
                "meta {\n  name: RESOURCE_CREATE_SUCCESS\n  type: http\n}\n"
                "post {\n  url: {{BASE_URL}}/resources\n  body: json\n}\n"
                "body:json {\n  {\"pdid\": \"p-1\",}\n}\n"
                "assert {\n  res.status: eq 200\n  res.body.data.id: eq \"1\"\n}\n",
                encoding="utf-8",
            )
            endpoint = {"id": "RESOURCE_CREATE", "method": "POST", "path": "/resources"}
            _, _, _, errors = coverage.case_files([case], root, {endpoint["id"]: endpoint})
            self.assertTrue(any("invalid JSON at line" in error and "column" in error for error in errors), errors)

    def test_seed_titles_use_endpoint_business_semantics(self):
        parser = load_script("parse_openapi")
        cases = (
            ({"id": "LIST", "method": "GET", "operation_id": "listByPage", "tags": ["Resource"], "responses": {"200": {}}}, "分页查询 Resource 信息成功"),
            ({"id": "GROUP", "method": "POST", "operation_id": "batchChangeGroup", "tags": ["Resource"], "responses": {"200": {}}}, "批量变更 Resource 分组成功"),
            ({"id": "DELETE", "method": "DELETE", "operation_id": "batchDelete", "tags": ["Resource"], "responses": {"200": {}}}, "批量删除 Resource 信息成功"),
            ({"id": "IMPORT", "method": "POST", "operation_id": "importResource", "tags": ["Resource"], "responses": {"202": {}}}, "导入 Resource 文件成功受理"),
        )
        for endpoint, expected in cases:
            self.assertEqual(parser.seed_contract_cases(endpoint)[0]["title"], expected)

    def test_scenario_matrix_is_inferred_from_real_contract_features(self):
        parser = load_script("parse_openapi")
        endpoint = {
            "id": "RESOURCE_LIST",
            "method": "GET",
            "path": "/v1/resources",
            "security": [{"bearerAuth": []}],
            "parameters": [
                {"name": "pageNum", "in": "query", "schema": {"type": "integer", "minimum": 1}},
                {"name": "status", "in": "query", "schema": {"type": "string", "enum": ["ON", "OFF"]}},
                {"name": "X-Tenant-Id", "in": "header", "required": True, "schema": {"type": "string"}},
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
        self.assertIn("RESOURCE_LIST_MISSING_X_TENANT_ID", seeded_ids)
        self.assertIn("RESOURCE_LIST_INVALID_STATUS", seeded_ids)
        self.assertIn("RESOURCE_LIST_BOUNDARY_PAGENUM", seeded_ids)

    def test_script_bundle_sync_is_versioned_and_checkable(self):
        manager = load_script("scripts_manager")
        with tempfile.TemporaryDirectory() as directory:
            qa_root = Path(directory) / "qa"
            changed = manager.sync_scripts(qa_root, ROOT / "scripts")
            self.assertTrue(changed)
            self.assertEqual(manager.check_scripts(qa_root, ROOT / "scripts"), [])
            metadata = load_script("manifest_io").load_data(qa_root / "scripts" / "scripts-version.yaml")
            for key in ("skill_version", "scripts_version", "source_repository", "paths", "scripts_sha256", "synchronized_at", "files"):
                self.assertIn(key, metadata)
            self.assertEqual(metadata["paths"]["contracts"], "data/contracts")
            self.assertEqual(metadata["paths"]["global_evidence"], "results/global/evidence")
            (qa_root / "scripts" / "run_bruno.py").write_text("outdated", encoding="utf-8")
            self.assertTrue(any("outdated" in error for error in manager.check_scripts(qa_root, ROOT / "scripts")))

    def test_scope_cases_do_not_filter_arbitrary_case_metadata(self):
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
                    {"id": "APP_GET_OK", "endpoint_id": "APP_GET", "legacy_classification": "a"},
                    {"id": "APP_POST_OK", "endpoint_id": "APP_POST", "legacy_classification": "b"},
                ],
            }), encoding="utf-8")
            cases = runner.scope_cases(contracts, "APP")
            self.assertEqual([case["id"] for case in cases], ["APP_GET_OK", "APP_POST_OK"])

    def test_runner_logs_case_summary_and_remote_version_warnings(self):
        runner = load_script("run_bruno")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contracts = root / "contracts"
            contracts.mkdir()
            (contracts / "version-lock.yaml").write_text(
                json.dumps({"business": {"commit": "abcdef1234567890"}}), encoding="utf-8",
            )
            environment = {
                "vars": {
                    "baseUrl": "https://api.example.com",
                    "versionPath": "/actuator/info",
                    "versionJsonPath": "git.commit.id",
                },
                "headers": {},
            }
            response = mock.MagicMock()
            response.status = 200
            response.headers = {}
            response.read.return_value = b'{"git":{"commit":{"id":"abcdef1"}}}'
            context = mock.MagicMock()
            context.__enter__.return_value = response
            with mock.patch("urllib.request.urlopen", return_value=context):
                self.assertEqual(runner.target_version_warnings(environment, contracts), [])

            environment["vars"]["expectedVersion"] = "different"
            with mock.patch("urllib.request.urlopen", return_value=context):
                warnings = runner.target_version_warnings(environment, contracts)
            self.assertTrue(any("mismatch" in warning for warning in warnings))

            environment["vars"].pop("versionPath")
            self.assertTrue(any("was not checked" in warning for warning in runner.target_version_warnings(environment, contracts)))

            first_log = runner.log_path(root, None)
            second_log = runner.log_path(root, "users")
            self.assertIn("run-bruno-all.log", first_log.name)
            self.assertIn("run-bruno-users.log", second_log.name)
            output = io.StringIO()
            with mock.patch("sys.stdout", output):
                total, executed, passed, failed, not_executed = runner.render_case_summary(
                    [{"id": "A", "title": "成功"}, {"id": "B", "title": "失败"}],
                    {"executed": ["A", "B"], "passed": ["A"]},
                )
            self.assertEqual((total, executed, passed, failed, not_executed), (2, 2, 1, ["B"], []))
            self.assertIn("[PASS] A", output.getvalue())
            self.assertIn("[FAIL] B", output.getvalue())

    def test_materializer_removes_all_meta_tags(self):
        materializer = load_script("materialize_missing_bru")
        content = (
            "meta {\n  name: APP_LIST_OK\n  type: http\n"
            "  tags: [legacy, plan-smoke, plan-full]\n}\n"
            "get {\n  url: {{baseUrl}}/app\n}\n"
        )
        updated = materializer.strip_meta_tags(content)
        self.assertNotIn("tags:", updated)
        rendered = materializer.render_case(
            {
                "id": "APP_LIST_OK",
                "title": "查询应用列表成功",
                "_execution_tags": ["plan-smoke"],
                "expected": {"http_status": 200},
                "assertions": [{"path": "$.data", "equals": {}}],
            },
            {"method": "GET", "path": "/app"},
        )
        self.assertNotIn("tags:", rendered)

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
            spec = root / "data" / "contracts" / "openapi.json"
            module_map = root / "data" / "contracts" / "module-map.yaml"
            output = root / "data" / "contracts" / "modules"
            spec.parent.mkdir(parents=True)
            document = {
                "openapi": "3.0.0",
                "paths": {"/resources": {"get": {
                    "operationId": "listByPage",
                    "tags": ["Resources"],
                    "responses": {"200": {}},
                }}},
            }
            spec.write_text(json.dumps(document), encoding="utf-8")
            module_map.write_text(json.dumps({"modules": [{"id": "resources", "name": "Resources", "directory": "Resources", "swagger_tags": ["Resources"]}]}), encoding="utf-8")
            manifest = parser.extract(spec, document)
            parser.write_partitioned(manifest, module_map, output, seed_cases=True)
            second = parser.write_partitioned(manifest, module_map, output, seed_cases=True, incremental=True)
            self.assertEqual(second["changed_modules"], [])
            self.assertEqual(second["skipped_modules"], ["resources"])
            cases_path = output / "Resources" / "cases.yaml"
            cases = parser.load_document(cases_path)
            cases["cases"][0]["description"] = "人工调整后的业务说明"
            cases_path.write_text(parser.render_manifest(cases, cases_path), encoding="utf-8")
            third = parser.write_partitioned(manifest, module_map, output, seed_cases=True, incremental=True)
            self.assertIn(cases["cases"][0]["id"], third["manual_review_cases"])
            preserved = parser.load_document(cases_path)["cases"][0]
            self.assertEqual(preserved["description"], "人工调整后的业务说明")
            self.assertTrue(preserved["manual_review"])
            self.assertTrue((root / "data" / "contracts" / "generation-state.yaml").is_file())
            self.assertTrue((root / "data" / "contracts" / "qa-lock.yaml").is_file())

    def test_source_scanner_marks_business_errors_and_required_headers_for_coverage(self):
        scanner = load_script("analyze_source_logic")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ResourceController.java").write_text(
                'class ResourceController {\n'
                '  private final ResourceApplication application;\n'
                '  @DeleteMapping\n'
                '  public Object deleteResource() { request.getHeader("X-Tenant-Id"); return application.deleteResource(); }\n'
                '}\n',
                encoding="utf-8",
            )
            (root / "ResourceApplication.java").write_text(
                'class ResourceApplication {\n'
                '  private final ResourceDomainService domainService;\n'
                '  public Object deleteResource() { return domainService.deleteResource(); }\n'
                '}\n',
                encoding="utf-8",
            )
            (root / "ResourceDomainService.java").write_text(
                'class ResourceDomainService {\n'
                '  public Object deleteResource() { throw new ResourceApplicationException(ResourceErrorCode.RESOURCE_IN_USE); }\n'
                '}\n',
                encoding="utf-8",
            )
            (root / "ResourceErrorCode.java").write_text(
                'enum ResourceErrorCode { RESOURCE_IN_USE(4091); }\n',
                encoding="utf-8",
            )
            result = scanner.scan([root])
        required = [item for item in result["candidates"] if item.get("coverage_required")]
        self.assertTrue(any(item.get("required_header") == "X-Tenant-Id" for item in required))
        self.assertTrue(any("4091" in item.get("expected_business_codes", []) for item in required))

    def test_java_structures_resolve_delegator_and_integration_entrypoints(self):
        scanner = load_script("analyze_java_logic")

        def write_sources(root: Path, sources: dict[str, str]) -> None:
            root.mkdir()
            for name, source in sources.items():
                (root / name).write_text(source, encoding="utf-8")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / "catalog"
            write_sources(catalog, {
                "CatalogController.java": (
                    '@RequestMapping("/v1/catalog")\nclass CatalogController {\n'
                    '  private final ApplicationDelegator delegator;\n'
                    '  @PostMapping(\n    path = "/items"\n  )\n'
                    '  public ResponseEntity<\n      Object> create() throws Exception {\n'
                    '    return delegator.apply(new CreateCatalogItemApplication());\n  }\n}\n'
                ),
                "CreateCatalogItemApplication.java": (
                    'class CreateCatalogItemApplication {\n  private final CatalogDomainService domainService;\n'
                    '  public Object doServe() throws Exception { return domainService.create(); }\n}\n'
                ),
                "CatalogDomainService.java": (
                    'class CatalogDomainService {\n  public Object create() { '
                    'throw new CatalogApplicationException(CatalogErrorCode.INVALID_ITEM); }\n}\n'
                ),
                "CatalogApplicationException.java": (
                    'class CatalogApplicationException extends RuntimeException {\n'
                    '  public CatalogApplicationException(CatalogErrorCode code) {}\n}\n'
                ),
                "CatalogErrorCode.java": 'enum CatalogErrorCode { INVALID_ITEM("CATALOG_INVALID_ITEM"); }\n',
                "CatalogAdvice.java": (
                    '@ControllerAdvice\nclass CatalogAdvice {\n'
                    '  @ExceptionHandler(CatalogApplicationException.class)\n'
                    '  public Object handle(CatalogApplicationException error) { return error; }\n}\n'
                ),
            })
            orders = root / "orders"
            write_sources(orders, {
                "OrderController.java": (
                    '@RequestMapping("/v1/orders")\nclass OrderController {\n'
                    '  private final ApplicationDelegator delegator;\n'
                    '  private final DeleteOrderItemApplication deleteApplication;\n'
                    '  @DeleteMapping(path = "items/{id}")\n'
                    '  public Object delete() throws Exception { return delegator.apply(deleteApplication); }\n}\n'
                ),
                "DeleteOrderItemApplication.java": (
                    'class DeleteOrderItemApplication {\n  private final OrderDomainService domainService;\n'
                    '  public Object doServe() { return domainService.delete(); }\n}\n'
                ),
                "OrderDomainService.java": (
                    'class OrderDomainService {\n  private final OrderIntegration integration;\n'
                    '  public Object delete() { return integration.delete(); }\n}\n'
                ),
                "OrderIntegration.java": (
                    'class OrderIntegration {\n  public Object delete() { '
                    'throw new OrderApplicationException(OrderErrorCode.ITEM_IN_USE); }\n}\n'
                ),
                "OrderApplicationException.java": (
                    'class OrderApplicationException extends RuntimeException {\n'
                    '  public OrderApplicationException(OrderErrorCode code) {}\n}\n'
                ),
                "OrderErrorCode.java": 'enum OrderErrorCode { ITEM_IN_USE(4091); }\n',
            })

            catalog_result = scanner.scan([catalog])
            order_result = scanner.scan([orders])

        self.assertEqual(catalog_result["errors"], [])
        self.assertEqual(catalog_result["entrypoint_count"], 1)
        self.assertEqual(catalog_result["exception_family"]["exception_type"], "CatalogApplicationException")
        catalog_business = [item for item in catalog_result["candidates"] if item["kind"] == "business_exception"]
        self.assertTrue(any("POST /v1/catalog/items" in item["endpoint_keys"] for item in catalog_business))
        self.assertTrue(any("CATALOG_INVALID_ITEM" in item.get("expected_business_codes", []) for item in catalog_business))

        self.assertEqual(order_result["errors"], [])
        self.assertEqual(order_result["entrypoint_count"], 1)
        self.assertEqual(order_result["exception_family"]["exception_type"], "OrderApplicationException")
        order_business = [item for item in order_result["candidates"] if item["kind"] == "business_exception"]
        self.assertTrue(any("DELETE /v1/orders/items/{id}" in item["endpoint_keys"] for item in order_business))
        self.assertTrue(any("4091" in item.get("expected_business_codes", []) for item in order_business))

    def test_java_scanner_blocks_when_mappings_exist_but_no_entrypoint_is_parsed(self):
        scanner = load_script("analyze_java_logic")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "BrokenController.java").write_text(
                'class BrokenController {\n  @GetMapping("/broken")\n  Object broken() { return null; }\n}\n',
                encoding="utf-8",
            )
            result = scanner.scan([root])
        self.assertEqual(result["mapping_annotation_count"], 1)
        self.assertEqual(result["entrypoint_count"], 0)
        self.assertTrue(any("recognized 0 controller entrypoints" in error for error in result["errors"]))

    def test_source_business_error_without_http_status_evidence_remains_unresolved(self):
        scanner = load_script("analyze_source_logic")
        with tempfile.TemporaryDirectory() as directory:
            contracts = Path(directory) / "contracts"
            module = contracts / "modules" / "resources"
            module.mkdir(parents=True)
            endpoint = {
                "id": "RESOURCE_DELETE", "method": "DELETE", "path": "/resources/{id}",
                "responses": {"200": {}}, "scenario_matrix": {}, "case_ids": [],
            }
            (module / "endpoints.yaml").write_text(
                yaml.safe_dump({"module": "resources", "endpoints": [endpoint]}, sort_keys=False), encoding="utf-8",
            )
            (module / "cases.yaml").write_text(
                yaml.safe_dump({"module": "resources", "cases": []}, sort_keys=False), encoding="utf-8",
            )
            (module / "logic.yaml").write_text(
                yaml.safe_dump({"module": "resources", "logic": []}, sort_keys=False), encoding="utf-8",
            )
            result = {"version": 2, "candidates": [{
                "id": "BUSINESS_NO_STATUS", "kind": "business_exception", "coverage_required": True,
                "endpoint_keys": ["DELETE /resources/{id}"], "expected_business_codes": ["4091"],
                "file": "ResourceDomainService.java", "line": 10, "evidence": "RESOURCE_IN_USE",
            }]}
            unresolved = scanner.apply_candidates(result, contracts)
            cases = load_script("manifest_io").first_list(
                load_script("manifest_io").load_data(module / "cases.yaml"), "cases",
            )
        self.assertEqual(unresolved, ["BUSINESS_NO_STATUS"])
        self.assertEqual(cases, [])

    def test_source_enhancement_seeds_required_header_and_business_error_drafts(self):
        scanner = load_script("analyze_source_logic")
        parser = load_script("parse_openapi")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "ResourceController.java").write_text(
                'class ResourceController {\n'
                '  private final ResourceApplication application;\n'
                '  @DeleteMapping\n'
                '  public Object deleteResource() { request.getHeader("X-Tenant-Id"); return application.deleteResource(); }\n'
                '}\n',
                encoding="utf-8",
            )
            (source / "ResourceApplication.java").write_text(
                'class ResourceApplication {\n'
                '  private final ResourceDomainService domainService;\n'
                '  public Object deleteResource() { return domainService.deleteResource(); }\n'
                '}\n',
                encoding="utf-8",
            )
            (source / "ResourceDomainService.java").write_text(
                'class ResourceDomainService {\n'
                '  public Object deleteResource() { throw new ResourceApplicationException(ResourceErrorCode.RESOURCE_IN_USE); }\n'
                '}\n',
                encoding="utf-8",
            )
            (source / "ResourceErrorCode.java").write_text(
                'enum ResourceErrorCode { RESOURCE_IN_USE(4091); }\n',
                encoding="utf-8",
            )
            contracts = root / "qa" / "contracts"
            module = contracts / "modules" / "resources"
            module.mkdir(parents=True)
            (contracts / "security-profile.yaml").write_text(
                yaml.safe_dump({
                    "required-header-x-tenant-id": {
                        "type": "required-header",
                        "header": "X-Tenant-Id",
                        "probe_result": {"missing_header_status": 400},
                    },
                }, sort_keys=False),
                encoding="utf-8",
            )
            endpoint = {
                "id": "RESOURCE_DELETE",
                "method": "DELETE",
                "path": "/v1/resources/{id}",
                "operation_id": "deleteResource",
                "tags": ["Resources"],
                "responses": {"200": {}, "400": {}},
            }
            endpoint["scenario_matrix"] = parser.inferred_scenario_matrix(endpoint)
            (module / "endpoints.yaml").write_text(
                yaml.safe_dump({
                    "version": 1,
                    "module": "resources",
                    "name": "Resources",
                    "swagger_tag": "Resources",
                    "endpoints": [endpoint],
                }, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
            (module / "cases.yaml").write_text(
                yaml.safe_dump({"version": 1, "module": "resources", "swagger_tag": "Resources", "cases": []}),
                encoding="utf-8",
            )
            (module / "logic.yaml").write_text(
                yaml.safe_dump({"version": 1, "module": "resources", "swagger_tag": "Resources", "logic": []}),
                encoding="utf-8",
            )
            result = scanner.scan([source])
            self.assertEqual(scanner.apply_candidates(result, contracts), [])
            cases = parser.load_document(module / "cases.yaml")["cases"]
            header_case = next(case for case in cases if case["id"].endswith("MISSING_X_TENANT_ID"))
            self.assertEqual(header_case["request"]["omit_common_headers"], ["X-Tenant-Id"])
            self.assertEqual(header_case["scenario"], "validation")
            self.assertTrue(any(case.get("expected", {}).get("business_code") == "4091" for case in cases))
            updated_endpoint = parser.load_document(module / "endpoints.yaml")["endpoints"][0]
            self.assertFalse(updated_endpoint["scenario_matrix"]["authentication"]["applicable"])
            self.assertTrue(updated_endpoint["scenario_matrix"]["validation"]["applicable"])
            self.assertTrue(updated_endpoint["scenario_matrix"]["business_error"]["applicable"])
            logic = parser.load_document(module / "logic.yaml")["logic"]
            self.assertTrue(all(item.get("case_ids") for item in logic))

    def test_source_constraints_extract_and_apply_project_evidence(self):
        source_constraints = load_script("source_constraints")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ProductOutput.java").write_text(
                "class ProductOutput {\n"
                "  @NotBlank\n  @Size(min = 8, max = 8)\n  private String sku;\n"
                "  private ProductStatus status;\n}\n",
                encoding="utf-8",
            )
            (root / "ZProductStatus.java").write_text(
                "enum ProductStatus {\n  ACTIVE,\n  INACTIVE;\n}\n",
                encoding="utf-8",
            )
            (root / "WarehouseRequest.kt").write_text(
                "data class WarehouseRequest(\n  @field:NotBlank\n  val warehouseCode: String\n)\n",
                encoding="utf-8",
            )
            (root / "application.yaml").write_text(
                "qa:\n  region-code: R1\n  password: must-not-be-recorded\n",
                encoding="utf-8",
            )
            flyway = root / "flyway"
            flyway.mkdir()
            (flyway / "V1__inventory.sql").write_text(
                "CREATE TABLE inventory (\n"
                "  serial_number VARCHAR(20) NOT NULL,\n"
                "  UNIQUE KEY uk_inventory_serial_number (serial_number)\n"
                ");\n",
                encoding="utf-8",
            )
            (root / "ProductOutputTest.java").write_text(
                'class ProductOutputTest { void test() { output.setRegionCode("R2"); } }\n',
                encoding="utf-8",
            )
            (root / "ErrorCode.java").write_text(
                'enum ErrorCode { INVALID_SKU("CATALOG_INVALID_SKU", "invalid sku"); }\n',
                encoding="utf-8",
            )

            document = source_constraints.extract_source_constraints([root])
            rules = {
                name.casefold(): rule
                for rule in document["field_rules"]
                for name in rule.get("field_names", [])
            }
            self.assertTrue(rules["sku"]["constraints"]["required"])
            self.assertEqual(rules["sku"]["constraints"]["minLength"], 8)
            self.assertEqual(rules["status"]["constraints"]["enum"], ["ACTIVE", "INACTIVE"])
            self.assertEqual(rules["regioncode"]["constraints"]["default"], "R1")
            self.assertEqual(rules["regioncode"]["example"], "R2")
            self.assertTrue(rules["serialnumber"]["constraints"]["unique"])
            self.assertNotIn("password", rules)
            self.assertEqual(document["error_codes"][0]["code"], "CATALOG_INVALID_SKU")
            self.assertTrue(any(item["type_name"] == "ProductOutput" for item in document["response_rules"]))
            for role in ("dto", "config", "flyway", "test", "exception_enum"):
                self.assertIn(role, document["source_inventory"])

            manifest = {"endpoints": [{
                "parameters": [],
                "request_body": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {"sku": {"type": "string"}, "status": {}, "regionCode": {}},
                }}}},
                "responses": {},
            }]}
            source_constraints.apply_constraints_to_manifest(manifest, document)
            schema = manifest["endpoints"][0]["request_body"]["content"]["application/json"]["schema"]
            self.assertIn("sku", schema["required"])
            self.assertEqual(schema["properties"]["status"]["enum"], ["ACTIVE", "INACTIVE"])
            self.assertEqual(schema["properties"]["regionCode"]["default"], "R1")

    def test_source_constraints_do_not_invent_domain_defaults(self):
        source_constraints = load_script("source_constraints")
        with tempfile.TemporaryDirectory() as directory:
            document = source_constraints.extract_source_constraints([Path(directory)])
        self.assertEqual(document["field_rules"], [])
        self.assertEqual(document["error_codes"], [])

    def test_shared_constraints_enforce_review_worker_boundary_and_secrets(self):
        constraints = load_script("qa_constraints")
        with tempfile.TemporaryDirectory() as directory:
            qa_root = Path(directory)
            module = qa_root / "data" / "contracts" / "modules" / "things"
            module.mkdir(parents=True)
            (module / "endpoints.yaml").write_text(json.dumps({
                "module": "things",
                "endpoints": [{"id": "THING_GET", "method": "GET", "path": "/things"}],
            }), encoding="utf-8")
            (module / "cases.yaml").write_text(json.dumps({"cases": [{
                "id": "THING_GET_OK",
                "endpoint_id": "THING_GET",
                "request": {"query": {"regionCode": "review-regionCode"}},
                "review_reasons": {"review-regionCode": "No source or local value exists"},
            }]}), encoding="utf-8")
            constraints.ensure_rule_library(qa_root)

            unexplained = {
                "id": "BAD",
                "request": {"body": {"sku": "review-sku"}},
            }
            self.assertTrue(constraints.review_reason_errors(unexplained))
            unexplained["review_reasons"] = {"review-sku": "No contract, source, evidence, or local value exists"}
            self.assertEqual(constraints.review_reason_errors(unexplained), [])

            missing_paths = constraints.validate_stage(
                qa_root, "generation", module="things", actor="module-worker",
            )
            self.assertTrue(any("explicit changed paths" in error for error in missing_paths))
            escaped = constraints.validate_stage(
                qa_root,
                "generation",
                module="things",
                actor="module-worker",
                changed_paths=[qa_root / "data" / "contracts" / "index.yaml"],
            )
            self.assertTrue(any("coordinator-owned path" in error for error in escaped))
            allowed = constraints.validate_stage(
                qa_root,
                "generation",
                module="things",
                actor="module-worker",
                changed_paths=[module / "cases.yaml"],
            )
            self.assertFalse(any("coordinator-owned path" in error for error in allowed))

            token = "eyJ" + "a" * 30 + "." + "b" * 12 + "." + "c" * 12
            (qa_root / "data" / "constraints" / "source-rules.yaml").write_text(
                yaml.safe_dump({"version": 1, "field_rules": [], "example": token}),
                encoding="utf-8",
            )
            secret_errors = constraints.validate_stage(qa_root, "generation", module="things")
            self.assertTrue(any("secret-free constraint" in error for error in secret_errors))

    def test_execution_evidence_is_redacted_classified_and_merged(self):
        normalizer = load_script("normalize_bruno_report")
        runner = load_script("run_bruno")
        raw = {"results": [{
            "name": "THING_LIST_OK",
            "assertionResults": [{"status": "pass"}],
            "testResults": [],
            "response": {"status": 200, "data": {
                "code": 0,
                "accessToken": "must-not-survive",
                "data": {"pageNum": 1, "pageSize": 20, "total": 1, "records": [{"id": "thing-1"}]},
            }},
        }]}
        normalized = normalizer.normalized_case_results(raw)
        self.assertEqual(normalized["THING_LIST_OK"]["actual"]["body"]["accessToken"], "<redacted>")
        cases = [
            {
                "id": "THING_LIST_OK", "endpoint_id": "THING_LIST", "_module": "things",
                "_module_directory": "things", "_endpoint": {"method": "GET", "path": "/things"},
                "expected": {"http_status": 200}, "assertions": [{"path": "$.code", "equals": 0}],
            },
            {
                "id": "THING_LIST_BAD", "endpoint_id": "THING_LIST", "_module": "things",
                "_module_directory": "things", "_endpoint": {"method": "GET", "path": "/things"},
                "expected": {"http_status": 200}, "assertions": [{"path": "$.code", "equals": 0}],
            },
            {
                "id": "THING_LIST_REVIEW", "endpoint_id": "THING_LIST", "_module": "things",
                "_module_directory": "things", "_endpoint": {"method": "GET", "path": "/things"},
                "request": {"query": {"regionCode": "review-regionCode"}},
                "review_reasons": {"review-regionCode": "Source evidence is unavailable"},
            },
        ]
        evidence = {
            "executed": ["THING_LIST_OK", "THING_LIST_BAD"],
            "passed": ["THING_LIST_OK"],
            "cases": {
                "THING_LIST_OK": normalized["THING_LIST_OK"],
                "THING_LIST_BAD": {
                    "status": "failed",
                    "actual": {"http_status": 500, "body": {"code": 500}},
                    "failure_reason": "expected 200 but received 500",
                },
            },
        }
        report = runner.result_report(cases, evidence, "all")
        self.assertEqual(report["summary"], {"total": 3, "executed": 2, "passed": 1, "failed": 1, "not_executed": 1})
        self.assertEqual([item["case_id"] for item in report["failures"]], ["THING_LIST_BAD"])
        self.assertEqual([item["case_id"] for item in report["not_executed"]], ["THING_LIST_REVIEW"])
        self.assertEqual(report["failures"][0]["failure_category"], "assertion_failure")
        self.assertEqual(report["manual_confirmation"][0]["failure_categories"], ["insufficient_source_evidence", "manual_confirmation"])
        for key in ("module", "case_id", "interface", "request_summary", "expected", "actual", "failure_reason", "needs_manual_confirmation"):
            self.assertIn(key, report["failures"][0])

        with tempfile.TemporaryDirectory() as directory:
            qa_root = Path(directory)
            first = {"version": 1, "executed": ["THING_LIST_OK"], "passed": ["THING_LIST_OK"], "cases": {
                "THING_LIST_OK": normalized["THING_LIST_OK"],
            }}
            first_report, _, _ = runner.persist_execution_artifacts(qa_root, cases, first, "things")
            second = {"version": 1, "executed": ["THING_LIST_BAD"], "passed": ["THING_LIST_BAD"], "cases": {
                "THING_LIST_BAD": {"actual": {"http_status": 200, "body": {"code": 0}}},
            }}
            runner.persist_execution_artifacts(qa_root, cases, second, "things")
            observed = yaml.safe_load(
                (qa_root / "data" / "contracts" / "modules" / "things" / "observed-rules.yaml").read_text(encoding="utf-8")
            )
            self.assertEqual({item["case_id"] for item in observed["observations"]}, {"THING_LIST_OK", "THING_LIST_BAD"})
            persisted_report = json.loads(first_report.read_text(encoding="utf-8"))
            self.assertTrue(persisted_report["execution_evidence"].startswith("results/modules/evidence/things/"))

    def test_successful_observation_upgrades_generated_assertions(self):
        parser = load_script("parse_openapi")
        source_constraints = load_script("source_constraints")
        with tempfile.TemporaryDirectory() as directory:
            qa_root = Path(directory)
            spec = qa_root / "openapi.json"
            document = {
                "openapi": "3.0.0",
                "tags": [{"name": "things"}],
                "paths": {"/things": {"get": {
                    "operationId": "listThings",
                    "tags": ["things"],
                    "responses": {"200": {"description": "ok", "content": {"application/json": {"schema": {
                        "type": "object", "properties": {"code": {"type": "integer"}, "data": {"type": "object"}},
                    }}}}},
                }}},
            }
            spec.write_text(json.dumps(document), encoding="utf-8")
            manifest = parser.extract(spec, document)
            endpoint_id = manifest["endpoints"][0]["id"]
            module_map = qa_root / "data" / "contracts" / "module-map.yaml"
            module_map.parent.mkdir(parents=True)
            module_map.write_text(json.dumps({
                "modules": [{"id": "things", "name": "things", "swagger_tags": ["things"]}],
            }), encoding="utf-8")
            output = qa_root / "data" / "contracts" / "modules"
            parser.write_partitioned(manifest, module_map, output, seed_cases=True)
            cases_path = output / "things" / "cases.yaml"
            first = parser.load_document(cases_path)["cases"]
            success = next(case for case in first if case["scenario"] == "success")
            self.assertTrue(success["review_required"])

            source_constraints.apply_observed_constraints(manifest, {"observations": [{
                "case_id": success["id"],
                "endpoint_id": endpoint_id,
                "evidence_file": "evidence/global/run.json",
                "response": {"http_status": 200, "body": {
                    "code": 0,
                    "data": {"pageNum": 1, "pageSize": 20, "total": 1, "records": [{"id": "thing-1"}]},
                }},
            }]})
            parser.write_partitioned(manifest, module_map, output, seed_cases=True, incremental=True)
            upgraded = next(
                case for case in parser.load_document(cases_path)["cases"] if case["id"] == success["id"]
            )
            self.assertFalse(upgraded["review_required"])
            assertions = {item["path"]: item for item in upgraded["assertions"]}
            for path in ("$.code", "$.data.pageNum", "$.data.pageSize", "$.data.total", "$.data.records"):
                self.assertIn(path, assertions)
            self.assertEqual(assertions["$.data.records"]["length"], 1)
            self.assertEqual(assertions["$.data.records"]["items"], {"type": "object"})

    def test_loopback_openapi_provenance_requires_execution(self):
        cli = load_script("bruno_api_test_generator")
        self.assertTrue(cli.is_loopback_openapi({"provenance": {"source_url": "http://127.0.0.1:8080/v3/api-docs"}}))
        self.assertTrue(cli.is_loopback_openapi({"provenance": {"source_url": "http://[::1]:8080/openapi"}}))
        self.assertFalse(cli.is_loopback_openapi({"provenance": {"source_url": "https://api.example.com/openapi"}}))

    def test_module_materialization_reuses_its_local_baseline(self):
        materializer = load_script("materialize_missing_bru")
        with tempfile.TemporaryDirectory() as directory:
            qa_root = Path(directory)
            module = qa_root / "data" / "contracts" / "modules" / "things"
            module.mkdir(parents=True)
            (module / "endpoints.yaml").write_text(json.dumps({
                "module": "things",
                "endpoints": [{"id": "THING_GET", "method": "GET", "path": "/things"}],
            }), encoding="utf-8")
            cases_path = module / "cases.yaml"
            cases_path.write_text(json.dumps({"module": "things", "cases": [{
                "id": "THING_GET_OK",
                "title": "查询事物成功",
                "endpoint_id": "THING_GET",
                "expected": {"http_status": 200},
                "assertions": [{"path": "$.data.id", "equals": "one"}],
            }]}), encoding="utf-8")
            config, _ = execution_fixture(qa_root)
            bruno = qa_root / "data" / "bruno"
            bruno.mkdir()
            (bruno / "collection.bru").write_text(materializer.COLLECTION_TEMPLATE, encoding="utf-8")
            materializer.materialize(
                qa_root / "data" / "contracts", bruno, execution_config_path=config, module_filter="things",
            )
            document = load_script("manifest_io").load_data(cases_path)
            document["cases"][0]["assertions"][0]["equals"] = "two"
            cases_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            materializer.materialize(
                qa_root / "data" / "contracts", bruno, execution_config_path=config, module_filter="things",
            )
            self.assertEqual(
                materializer.materialize(
                    qa_root / "data" / "contracts", bruno, execution_config_path=config, module_filter="things",
                ),
                [],
            )
            request = next((bruno / "things").glob("*.bru"))
            self.assertIn('res.body.data.id: eq "two"', request.read_text(encoding="utf-8"))

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
                "  X-Tenant-Id:\n"
                "    env: TENANT_ID\n",
                encoding="utf-8",
            )
            (execution / "README.md").write_text("run --plan smoke\nrun --all\n", encoding="utf-8")
            (environments / "local.bru").write_text(
                "vars {\n  BASE_URL: http://localhost\n  AUTH_TOKEN: token\n  TENANT_ID: tenant-1\n}\n",
                encoding="utf-8",
            )
            execution_config.initialize_execution_layout(qa_root)
            self.assertEqual(
                execution_config.load_execution_config(execution / "config.yaml"),
                {
                    "active_environment": "local",
                    "tooling": "shared-cli",
                    "coverage_profile": "full-matrix",
                    "cli_timeout": 60.0,
                    "sign": {"provider": "disabled"},
                },
            )
            self.assertFalse((qa_root / "qa.yaml").exists())
            self.assertFalse((qa_root / "scripts").exists())
            readme = (execution / "README.md").read_text(encoding="utf-8")
            self.assertIn('run.bat --module "users"', readme)
            self.assertNotIn("--all", readme)
            self.assertNotIn("--confirm-", readme)
            self.assertNotIn("--plan", readme)
            environment = execution_config.load_bruno_environment_document(environments / "local.bru")
            self.assertEqual(environment["headers"]["Authorization"], "Bearer {{AUTH_TOKEN}}")
            self.assertEqual(environment["headers"]["X-Tenant-Id"], "{{TENANT_ID}}")

    def test_shared_cli_mode_keeps_asset_only_layout(self):
        execution_config = load_script("execution_config")
        with tempfile.TemporaryDirectory() as directory:
            qa_root = Path(directory) / "qa"
            execution_config.initialize_execution_layout(qa_root, local_scripts=False)
            self.assertFalse((qa_root / "scripts").exists())
            self.assertFalse((qa_root / "qa.yaml").exists())
            self.assertFalse((qa_root / "execution" / "plans.yaml").exists())
            self.assertIn("tooling: shared-cli", (qa_root / "execution" / "config.yaml").read_text(encoding="utf-8"))
            self.assertIn("cli_timeout: 60", (qa_root / "execution" / "config.yaml").read_text(encoding="utf-8"))
            self.assertIn("bruno-api-test-generator run", (qa_root / "execution" / "run.bat").read_text(encoding="utf-8"))
            self.assertIn("bruno-api-test-generator run", (qa_root / "execution" / "run.sh").read_text(encoding="utf-8"))
            self.assertIn("BRUNO_NPM_BIN", (qa_root / "execution" / "run.bat").read_text(encoding="utf-8"))
            self.assertIn("BRUNO_NODE_HOME", (qa_root / "execution" / "run.sh").read_text(encoding="utf-8"))

    def test_run_accepts_cli_timeout_override(self):
        runner = load_script("run_bruno")
        with tempfile.TemporaryDirectory() as directory:
            qa_root = Path(directory) / "qa"
            observed: dict[str, object] = {}

            def execute(args, root, execution_log):
                observed.update({"timeout": args.cli_timeout, "root": root, "log": execution_log})
                return 0

            with mock.patch.object(runner, "execute", side_effect=execute):
                self.assertEqual(
                    runner.main(["--qa-root", str(qa_root), "--cli-timeout", "90"]),
                    0,
                )
            self.assertEqual(observed["timeout"], 90.0)
            self.assertEqual(observed["root"], qa_root.resolve())

    def test_init_creates_the_canonical_data_and_results_layout(self):
        cli = load_script("bruno_api_test_generator")
        with tempfile.TemporaryDirectory() as directory:
            qa_root = Path(directory) / "qa"
            self.assertEqual(cli.init_command(["--qa-root", str(qa_root)]), 0)
            self.assertEqual(
                {path.name for path in qa_root.iterdir() if path.is_dir()},
                {"data", "execution", "scripts", "results", "fixtures"},
            )
            self.assertEqual(
                {path.name for path in (qa_root / "data").iterdir() if path.is_dir()},
                {"bruno", "contracts", "constraints"},
            )
            for path in (
                qa_root / "results" / "global",
                qa_root / "results" / "global" / "evidence",
                qa_root / "results" / "modules",
                qa_root / "results" / "modules" / "evidence",
                qa_root / "results" / "logs",
            ):
                self.assertTrue(path.is_dir())

    def test_public_cli_pipeline_uses_only_the_canonical_layout(self):
        cli = load_script("bruno_api_test_generator")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            qa_root = root / "qa"
            spec = root / "openapi.json"
            spec.write_text(json.dumps({
                "openapi": "3.0.0",
                "tags": [{"name": "things"}],
                "paths": {"/things": {"get": {
                    "operationId": "listThings",
                    "tags": ["things"],
                    "responses": {"200": {"description": "ok", "content": {
                        "application/json": {"example": {"code": 0, "data": []}},
                    }}},
                }}},
            }), encoding="utf-8")

            self.assertEqual(cli.init_command(["--qa-root", str(qa_root)]), 0)
            self.assertEqual(cli.generate_command(["--qa-root", str(qa_root), "--openapi", str(spec)]), 0)
            checker = load_script("check_version_compatibility")
            digest = checker.source_digest(root)
            (qa_root / "data" / "contracts" / "version-lock.yaml").write_text(
                yaml.safe_dump({
                    "version": 1,
                    "status": "current",
                    "business": {"commit": f"filesystem:{digest[:16]}", "source_digest": digest},
                }), encoding="utf-8",
            )
            self.assertEqual(cli.materialize_command(["--qa-root", str(qa_root)]), 0)
            self.assertEqual(cli.coverage_command(["--qa-root", str(qa_root), "--all"], False), 0)
            self.assertEqual(cli.scripts_command(["check", "--qa-root", str(qa_root)]), 0)

            def preflight_run(command, **_kwargs):
                if any(str(value).endswith("check_api_coverage.py") for value in command):
                    return subprocess.CompletedProcess(command, 0, json.dumps({"static_ok": True}), "")
                output = Path(command[command.index("--output") + 1])
                report = {"version": 2, "status": "runnable", "errors": []}
                output.write_text(json.dumps(report), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, json.dumps(report), "")

            with mock.patch.object(cli.subprocess, "run", side_effect=preflight_run):
                self.assertEqual(cli.preflight_command(["--qa-root", str(qa_root)]), 0)
            self.assertTrue(list((qa_root / "results" / "global").glob("*-static-coverage.json")))
            self.assertTrue(list((qa_root / "results" / "global").glob("*-preflight.json")))
            self.assertTrue(list((qa_root / "results" / "logs").glob("*-preflight.log")))
            for name in ("bruno", "contracts", "constraints", "evidence", "logs"):
                self.assertFalse((qa_root / name).exists())

    def test_legacy_layout_migration_preserves_bru_and_refreshes_recorded_paths(self):
        paths = load_script("qa_paths")
        qa_lock = load_script("qa_lock")
        with tempfile.TemporaryDirectory() as directory:
            qa_root = Path(directory) / "qa"
            contracts = qa_root / "contracts"
            bruno = qa_root / "bruno"
            constraints = qa_root / "constraints"
            contracts.mkdir(parents=True)
            bruno.mkdir()
            constraints.mkdir()
            request = bruno / "case.bru"
            request_bytes = b"meta {\n  name: CASE\n}\n"
            request.write_bytes(request_bytes)
            state = {
                "openapi_sha256": None,
                "modules": {},
                "cases": {},
                "source_path": "qa/contracts/openapi.json",
            }
            (contracts / "generation-state.yaml").write_text(
                yaml.safe_dump(state, sort_keys=False), encoding="utf-8",
            )
            qa_lock.write(contracts)
            (contracts / "version-lock.yaml").write_text(
                yaml.safe_dump({"artifact": "qa/contracts/openapi.json"}), encoding="utf-8",
            )
            (qa_root / "evidence" / "global").mkdir(parents=True)
            (qa_root / "evidence" / "global" / "run-evidence.json").write_text("{}", encoding="utf-8")
            (qa_root / "logs").mkdir()
            (qa_root / "logs" / "run.log").write_text("ok\n", encoding="utf-8")

            paths.migrate_legacy_layout(qa_root)

            migrated_contracts = qa_root / "data" / "contracts"
            self.assertEqual((qa_root / "data" / "bruno" / "case.bru").read_bytes(), request_bytes)
            self.assertFalse((qa_root / "contracts").exists())
            self.assertFalse((qa_root / "bruno").exists())
            self.assertTrue((qa_root / "results" / "global" / "evidence" / "run-evidence.json").is_file())
            self.assertTrue((qa_root / "results" / "logs" / "run.log").is_file())
            self.assertIn(
                "qa/data/contracts/openapi.json",
                (migrated_contracts / "generation-state.yaml").read_text(encoding="utf-8"),
            )
            self.assertTrue(
                paths.canonicalize_legacy_path(
                    qa_root, qa_root / "contracts" / "openapi.json",
                ).as_posix().lower().endswith("/qa/data/contracts/openapi.json")
            )
            self.assertEqual(qa_lock.check(migrated_contracts), [])

    def test_public_cli_routes_help_to_the_subcommand(self):
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "bruno_api_test_generator.py"), "generate", "--help"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("--incremental", completed.stdout)
        self.assertIn("--shared-cli", completed.stdout)
        run_help = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "bruno_api_test_generator.py"), "run", "--help"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(run_help.returncode, 0)
        self.assertIn("--module", run_help.stdout)
        self.assertNotIn("--all", run_help.stdout)
        self.assertNotIn("--confirm-", run_help.stdout)
        self.assertNotIn("--plan", run_help.stdout)

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

    def test_generate_reuses_existing_source_rules_without_source_root(self):
        cli = load_script("bruno_api_test_generator")
        with tempfile.TemporaryDirectory() as directory:
            qa_root = Path(directory) / "qa"
            contracts = qa_root / "data" / "contracts"
            constraints = qa_root / "data" / "constraints"
            contracts.mkdir(parents=True)
            constraints.mkdir()
            spec = Path(directory) / "openapi.json"
            spec.write_text('{"openapi":"3.0.0","paths":{}}', encoding="utf-8")
            (contracts / "module-map.yaml").write_text('{"modules":[]}', encoding="utf-8")
            (constraints / "source-rules.yaml").write_text(yaml.safe_dump({
                "version": 1,
                "field_rules": [{
                    "id": "source-field-regioncode",
                    "field": "regionCode",
                    "field_names": ["regionCode"],
                    "constraints": {"type": "string", "required": True},
                    "example": "region-2",
                }],
            }), encoding="utf-8")
            manifest = {"endpoints": [{
                "id": "THING_LIST",
                "operation_id": "listThings",
                "method": "GET",
                "path": "/things",
                "parameters": [{
                    "name": "regionCode", "in": "query", "required": True,
                    "schema": {"type": "string"},
                }],
                "responses": {},
            }]}
            captured: dict[str, object] = {}

            def capture_partition(value, *_args, **_kwargs):
                captured["manifest"] = value
                return {"changed_modules": [], "skipped_modules": [], "deleted_endpoint_ids": [], "manual_review_cases": []}

            with (
                mock.patch.object(cli, "initialize_execution_layout"),
                mock.patch.object(cli, "ensure_rule_library"),
                mock.patch.object(cli, "extract", return_value=manifest),
                mock.patch.object(cli, "write_partitioned", side_effect=capture_partition),
                mock.patch.object(cli, "materialize"),
                mock.patch.object(cli, "write_qa_lock"),
                mock.patch.object(cli, "validate_stage", return_value=[]),
                mock.patch("execution_config.load_execution_config", return_value={
                    "coverage_profile": "contract-draft", "active_environment": "local",
                }),
            ):
                result = cli.generate_command(["--qa-root", str(qa_root), "--openapi", str(spec)])

            self.assertEqual(result, 0)
            parameter = captured["manifest"]["endpoints"][0]["parameters"][0]
            self.assertEqual(parameter["schema"]["example"], "region-2")
            self.assertEqual(parameter["schema"]["x-qa-rule-id"], "source-field-regioncode")

    def test_controller_return_type_builds_exact_response_assertions(self):
        source_constraints = load_script("source_constraints")
        parser = load_script("parse_openapi")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ProductController.java").write_text(
                "class ProductController {\n"
                "  @GetMapping(\"/products/{id}\")\n"
                "  public CommonResponse<ProductOutput> getProduct() { return null; }\n"
                "}\n",
                encoding="utf-8",
            )
            (root / "CommonResponse.java").write_text(
                "class CommonResponse<T> {\n"
                "  private Integer code = 0;\n"
                "  @NotNull\n  private T data;\n"
                "}\n",
                encoding="utf-8",
            )
            (root / "ProductOutput.java").write_text(
                "class ProductOutput {\n  @NotBlank\n  private String id;\n}\n",
                encoding="utf-8",
            )
            (root / "ProductOutputTest.java").write_text(
                'class ProductOutputTest { void example() { output.setId("product-1"); } }\n',
                encoding="utf-8",
            )
            rules = source_constraints.extract_source_constraints([root])
            endpoint = {
                "operation_id": "getProduct",
                "responses": {"200": {"content": {"application/json": {"schema": {}}}}},
            }
            manifest = {"endpoints": [endpoint]}
            source_constraints.apply_constraints_to_manifest(manifest, rules)
            schema = endpoint["responses"]["200"]["content"]["application/json"]["schema"]
            self.assertEqual(schema["properties"]["code"]["default"], 0)
            self.assertEqual(schema["properties"]["data"]["properties"]["id"]["example"], "product-1")
            assertions = parser.exact_response_assertions(endpoint, 200)
            self.assertIn({"path": "$.code", "equals": 0}, assertions)
            self.assertIn({"path": "$.data.id", "equals": "product-1"}, assertions)

    def test_local_environment_values_are_reused_as_templates(self):
        source_constraints = load_script("source_constraints")
        manifest = {"endpoints": [{
            "parameters": [{"name": "tenantId", "in": "path", "required": True, "schema": {"type": "string"}}],
            "request_body": {"content": {"application/json": {"schema": {
                "type": "object", "properties": {"regionCode": {"type": "string"}},
            }}}},
        }]}
        source_constraints.apply_environment_values(
            manifest, {"TENANT_ID": "private-tenant", "REGION_CODE": "private-region"},
        )
        endpoint = manifest["endpoints"][0]
        self.assertEqual(endpoint["parameters"][0]["schema"]["example"], "{{TENANT_ID}}")
        region = endpoint["request_body"]["content"]["application/json"]["schema"]["properties"]["regionCode"]
        self.assertEqual(region["example"], "{{REGION_CODE}}")
        self.assertNotIn("private-region", json.dumps(manifest))

    def test_machine_rule_stages_cannot_be_weakened(self):
        constraints = load_script("qa_constraints")
        with tempfile.TemporaryDirectory() as directory:
            qa_root = Path(directory)
            path = constraints.ensure_rule_library(qa_root)
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            document["rules"][0]["stages"].remove("post-execution")
            path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
            errors = constraints.rule_library_errors(path)
            self.assertTrue(any("incomplete stages" in error for error in errors))

    def test_source_required_unique_and_review_authorization_are_enforced(self):
        constraints = load_script("qa_constraints")
        with tempfile.TemporaryDirectory() as directory:
            qa_root = Path(directory)
            module = qa_root / "data" / "contracts" / "modules" / "things"
            module.mkdir(parents=True)
            endpoint = {
                "id": "THING_CREATE", "method": "POST", "path": "/things",
                "request_body": {"content": {"application/json": {"schema": {
                    "type": "object", "properties": {
                        "tenantId": {"x-qa-rule-id": "source-field-tenantid"},
                        "serial": {"x-qa-rule-id": "source-field-serial"},
                    },
                }}}},
            }
            (module / "endpoints.yaml").write_text(json.dumps({"module": "things", "endpoints": [endpoint]}), encoding="utf-8")
            (module / "cases.yaml").write_text(json.dumps({"module": "things", "cases": [
                {"id": "CREATE_ONE", "endpoint_id": "THING_CREATE", "scenario": "success", "request": {"body": {"serial": "same"}}},
                {
                    "id": "CREATE_TWO", "endpoint_id": "THING_CREATE", "scenario": "success",
                    "request": {"body": {"tenantId": "review-tenantId", "serial": "same"}},
                    "review_reasons": {"review-tenantId": "No value was found"},
                },
            ]}), encoding="utf-8")
            source = qa_root / "data" / "constraints" / "source-rules.yaml"
            source.parent.mkdir(parents=True)
            source.write_text(yaml.safe_dump({"field_rules": [
                {
                    "id": "source-field-tenantid", "field": "tenantId", "field_names": ["tenantId"],
                    "constraints": {"type": "string", "required": True}, "example": "tenant-1",
                },
                {
                    "id": "source-field-serial", "field": "serial", "field_names": ["serial"],
                    "constraints": {"type": "string", "unique": True},
                },
            ]}), encoding="utf-8")
            constraints.ensure_rule_library(qa_root)
            errors = constraints.validate_stage(qa_root, "generation")
            self.assertTrue(any("missing source-required field tenantId" in error for error in errors))
            self.assertTrue(any("reuse source-unique field serial" in error for error in errors))
            self.assertTrue(any("review placeholder review-tenantId is unauthorized" in error for error in errors))

    def test_worker_snapshot_enforces_current_workspace_boundary(self):
        constraints = load_script("qa_constraints")
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            qa_root = repository / "qa"
            module = qa_root / "data" / "contracts" / "modules" / "things"
            module.mkdir(parents=True)
            cases = module / "cases.yaml"
            (module / "endpoints.yaml").write_text(
                '{"module":"things","endpoints":[{"id":"THING_GET","method":"GET","path":"/things"}]}',
                encoding="utf-8",
            )
            cases.write_text('{"module":"things","cases":[]}', encoding="utf-8")
            constraints.ensure_rule_library(qa_root)
            constraints.write_worker_snapshot(qa_root, "things")

            cases.write_text('{"module":"things","cases":[],"worker_note":"ok"}', encoding="utf-8")
            allowed = constraints.validate_worker_snapshot(qa_root, "things", "generation")
            self.assertFalse(any("coordinator-owned path" in error for error in allowed))
            global_index = qa_root / "data" / "contracts" / "index.yaml"
            global_index.write_text("status: changed\n", encoding="utf-8")
            errors = constraints.validate_worker_snapshot(qa_root, "things", "generation")
            self.assertTrue(any("coordinator-owned path" in error and "index.yaml" in error for error in errors))

    def test_module_constraint_scope_still_enforces_global_identity_uniqueness(self):
        constraints = load_script("qa_constraints")
        with tempfile.TemporaryDirectory() as directory:
            qa_root = Path(directory)
            for module_id, path in (("alpha", "/alpha"), ("beta", "/beta")):
                module = qa_root / "data" / "contracts" / "modules" / module_id
                module.mkdir(parents=True)
                (module / "endpoints.yaml").write_text(json.dumps({
                    "module": module_id,
                    "endpoints": [{"id": "DUPLICATE_ENDPOINT", "method": "GET", "path": path}],
                }), encoding="utf-8")
                (module / "cases.yaml").write_text(json.dumps({
                    "module": module_id,
                    "cases": [{"id": "DUPLICATE_CASE", "endpoint_id": "DUPLICATE_ENDPOINT"}],
                }), encoding="utf-8")
            constraints.ensure_rule_library(qa_root)
            errors = constraints.validate_stage(qa_root, "generation", module="alpha")
            self.assertTrue(any("endpoint id DUPLICATE_ENDPOINT is duplicated" in error for error in errors))
            self.assertTrue(any("case id DUPLICATE_CASE is duplicated" in error for error in errors))

    def test_module_results_and_evidence_are_aggregated(self):
        runner = load_script("run_bruno")
        with tempfile.TemporaryDirectory() as directory:
            qa_root = Path(directory)
            for module_id, status in (("alpha", "passed"), ("beta", "failed")):
                module = qa_root / "data" / "contracts" / "modules" / module_id
                module.mkdir(parents=True)
                endpoint_id = f"{module_id.upper()}_GET"
                case_id = f"{endpoint_id}_OK"
                (module / "endpoints.yaml").write_text(json.dumps({
                    "module": module_id,
                    "endpoints": [{"id": endpoint_id, "method": "GET", "path": f"/{module_id}"}],
                }), encoding="utf-8")
                (module / "cases.yaml").write_text(json.dumps({
                    "module": module_id,
                    "cases": [{"id": case_id, "endpoint_id": endpoint_id, "expected": {"http_status": 200}}],
                }), encoding="utf-8")
                evidence_path = qa_root / "results" / "modules" / "evidence" / module_id / "20260101-evidence.json"
                evidence_path.parent.mkdir(parents=True)
                evidence_path.write_text(json.dumps({
                    "executed": [case_id], "passed": [case_id] if status == "passed" else [],
                    "cases": {case_id: {"actual": {"http_status": 200}, "failure_reason": "mismatch"}},
                }), encoding="utf-8")
                result_path = qa_root / "results" / "modules" / module_id / "20260101-result.json"
                result_path.parent.mkdir(parents=True)
                result_path.write_text(json.dumps({
                    "execution_evidence": evidence_path.relative_to(qa_root).as_posix(),
                    "cases": [{
                        "module": module_id, "case_id": case_id, "interface": f"GET /{module_id}",
                        "request_summary": {"method": "GET", "path": f"/{module_id}"},
                        "expected": {"http_status": 200}, "actual": {"http_status": 200},
                        "status": status, "failure_category": None if status == "passed" else "assertion_failure",
                        "failure_reason": None if status == "passed" else "mismatch",
                        "needs_manual_confirmation": False,
                    }],
                }), encoding="utf-8")

            with mock.patch.object(runner, "validate_stage", return_value=[]):
                report_path, evidence_path, report = runner.aggregate_module_results(qa_root)
            self.assertTrue(report_path.is_file())
            self.assertTrue(evidence_path.is_file())
            self.assertEqual(report["summary"], {"total": 2, "executed": 2, "passed": 1, "failed": 1, "not_executed": 0})
            self.assertEqual(report["failures"][0]["case_id"], "BETA_GET_OK")
            merged = json.loads(evidence_path.read_text(encoding="utf-8"))
            self.assertEqual(set(merged["executed"]), {"ALPHA_GET_OK", "BETA_GET_OK"})

    def test_post_execution_constraint_errors_are_persisted(self):
        runner = load_script("run_bruno")
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "result.json"
            report = {"summary": {"total": 1}}
            result = runner.persist_post_execution_constraints(
                report_path, report, ["response schema mismatch"], 0,
            )
            persisted = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(result, 1)
            self.assertEqual(persisted["status"], "failed")
            self.assertEqual(persisted["constraint_errors"], ["response schema mismatch"])

    def test_loopback_generation_invokes_run_command(self):
        cli = load_script("bruno_api_test_generator")
        with tempfile.TemporaryDirectory() as directory:
            qa_root = Path(directory) / "qa"
            contracts = qa_root / "data" / "contracts"
            contracts.mkdir(parents=True)
            (contracts / "module-map.yaml").write_text('{"modules":[]}', encoding="utf-8")
            spec = Path(directory) / "openapi.json"
            source_document = {
                "openapi": "3.0.0", "paths": {},
                "provenance": {"source_url": "http://127.0.0.1:8080/v3/api-docs"},
            }
            spec.write_text(json.dumps(source_document), encoding="utf-8")
            summary = {"changed_modules": [], "skipped_modules": [], "deleted_endpoint_ids": [], "manual_review_cases": []}
            with (
                mock.patch.object(cli, "initialize_execution_layout"),
                mock.patch.object(cli, "ensure_rule_library"),
                mock.patch.object(cli, "extract", return_value={"endpoints": []}),
                mock.patch.object(cli, "write_partitioned", return_value=summary),
                mock.patch.object(cli, "materialize"),
                mock.patch.object(cli, "write_qa_lock"),
                mock.patch.object(cli, "validate_stage", return_value=[]),
                mock.patch.object(cli, "run_command", return_value=7) as run,
                mock.patch("execution_config.load_execution_config", return_value={
                    "coverage_profile": "contract-draft", "active_environment": "local",
                }),
            ):
                result = cli.generate_command(["--qa-root", str(qa_root), "--openapi", str(spec)])
            self.assertEqual(result, 7)
            run.assert_called_once_with(["--qa-root", str(qa_root.resolve())])


if __name__ == "__main__":
    unittest.main()
