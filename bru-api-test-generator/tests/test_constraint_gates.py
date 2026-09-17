from __future__ import annotations

import argparse
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import yaml


ROOT = Path(__file__).parents[1]


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


def generated_project(root: Path) -> tuple[object, object, Path, Path]:
    cli = load_script("bruno_api_test_generator")
    constraints = load_script("qa_constraints")
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
    if cli.init_command(["--qa-root", str(qa_root), "--shared-cli"]) != 0:
        raise AssertionError("init failed")
    if cli.generate_command([
        "--qa-root", str(qa_root), "--openapi", str(spec), "--shared-cli",
    ]) != 0:
        raise AssertionError("generation failed")
    module = next((qa_root / "data" / "contracts" / "modules").iterdir())
    return cli, constraints, qa_root, module


class ConstraintGateTests(unittest.TestCase):
    def test_all_mandatory_rules_are_enabled_and_each_rejects_stage_reduction(self):
        constraints = load_script("qa_constraints")
        with tempfile.TemporaryDirectory() as directory:
            qa_root = Path(directory)
            rules_path = constraints.ensure_rule_library(qa_root)
            self.assertEqual(constraints.rule_library_errors(rules_path), [])
            original = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
            for required in constraints.MANDATORY_RULES:
                with self.subTest(rule=required["id"]):
                    document = copy.deepcopy(original)
                    configured = next(item for item in document["rules"] if item["id"] == required["id"])
                    configured["stages"] = configured["stages"][:-1]
                    rules_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
                    errors = constraints.rule_library_errors(rules_path)
                    self.assertTrue(any(required["id"] in error for error in errors), errors)
            rules_path.write_text(yaml.safe_dump(original, sort_keys=False), encoding="utf-8")

    def test_endpoint_scoped_status_values_do_not_cross_contaminate(self):
        source_constraints = load_script("source_constraints")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "AController.java").write_text(
                'class AController {\n'
                ' @PostMapping("/a")\n'
                ' public Object createA(@RequestBody ARequest request) { return request; }\n'
                '}',
                encoding="utf-8",
            )
            (source / "BController.java").write_text(
                'class BController {\n'
                ' @PostMapping("/b")\n'
                ' public Object createB(@RequestBody BRequest request) { return request; }\n'
                '}',
                encoding="utf-8",
            )
            (source / "ARequest.java").write_text(
                'class ARequest {\n private AStatus status;\n}', encoding="utf-8",
            )
            (source / "BRequest.java").write_text(
                'class BRequest {\n private BStatus status;\n}', encoding="utf-8",
            )
            (source / "AStatus.java").write_text('enum AStatus { ACTIVE, INACTIVE; }', encoding="utf-8")
            (source / "BStatus.java").write_text('enum BStatus { READY, CLOSED; }', encoding="utf-8")
            manifest = {"endpoints": [
                {
                    "id": "A_CREATE", "method": "POST", "path": "/a", "operation_id": "createA",
                    "request_body": {"content": {"application/json": {"schema": {
                        "type": "object", "properties": {"status": {"type": "string"}},
                    }}}}, "responses": {},
                },
                {
                    "id": "B_CREATE", "method": "POST", "path": "/b", "operation_id": "createB",
                    "request_body": {"content": {"application/json": {"schema": {
                        "type": "object", "properties": {"status": {"type": "string"}},
                    }}}}, "responses": {},
                },
            ]}
            document = source_constraints.write_source_constraints(root / "qa", [source], manifest)
            source_constraints.apply_constraints_to_manifest(manifest, document)
            a_status = manifest["endpoints"][0]["request_body"]["content"]["application/json"]["schema"]["properties"]["status"]
            b_status = manifest["endpoints"][1]["request_body"]["content"]["application/json"]["schema"]["properties"]["status"]
            self.assertEqual(a_status["enum"], ["ACTIVE", "INACTIVE"])
            self.assertEqual(b_status["enum"], ["READY", "CLOSED"])

    def test_available_flyway_test_or_config_value_forbids_review_placeholder(self):
        with tempfile.TemporaryDirectory() as directory:
            _, constraints, qa_root, module = generated_project(Path(directory))
            cases_path = module / "cases.yaml"
            base_cases = yaml.safe_load(cases_path.read_text(encoding="utf-8"))
            endpoint_id = base_cases["cases"][0]["endpoint_id"]
            for source_kind in ("flyway", "test", "config"):
                with self.subTest(source_kind=source_kind):
                    cases = copy.deepcopy(base_cases)
                    case = cases["cases"][0]
                    case["request"] = {"query": {"regionCode": "review-regionCode"}}
                    case["review_required"] = True
                    case["review_reasons"] = {"review-regionCode": "searched current workspace"}
                    case["manual_confirmation"] = {
                        "automation_blocker": "No reusable value was selected",
                        "search_records": [{
                            "source_kind": "openapi", "file": "openapi.json", "symbol": "listThings",
                            "line": 1, "endpoint_scope": [endpoint_id], "confidence": "high",
                        }],
                    }
                    cases_path.write_text(yaml.safe_dump(cases, sort_keys=False), encoding="utf-8")
                    source = {
                        "version": 1,
                        "field_rules": [{
                            "id": f"region-{source_kind}", "field": "regionCode", "field_names": ["regionCode"],
                            "endpoint_scope": [endpoint_id], "operation": "listThings", "constraints": {}, "example": "R1",
                            "evidence": [{
                                "source_kind": source_kind, "file": f"value.{source_kind}", "symbol": "regionCode",
                                "line": 1, "endpoint_scope": [endpoint_id], "confidence": "high",
                            }],
                        }],
                    }
                    (qa_root / "data" / "constraints" / "source-rules.yaml").write_text(
                        yaml.safe_dump(source, sort_keys=False), encoding="utf-8",
                    )
                    errors = constraints.validate_stage(qa_root, "generation")
                    self.assertTrue(any("[RES-001]" in error and "unauthorized" in error for error in errors), errors)

    def test_http_200_controller_advice_generates_exact_error_code_assertion(self):
        cli = load_script("bruno_api_test_generator")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            qa_root = root / "qa"
            source = root / "source"
            source.mkdir()
            (source / "CreateController.java").write_text(
                '@RequestMapping("/things") class CreateController {\n'
                ' @PostMapping public Object create(@RequestBody CreateRequest request) { return request; }\n}',
                encoding="utf-8",
            )
            (source / "CreateRequest.java").write_text(
                'class CreateRequest {\n @NotBlank\n private String name;\n}', encoding="utf-8",
            )
            (source / "AppException.java").write_text(
                'class AppException extends RuntimeException { AppException(AppErrorCode code) {} }', encoding="utf-8",
            )
            (source / "AppErrorCode.java").write_text(
                'enum AppErrorCode { INVALID_NAME("E100"); }', encoding="utf-8",
            )
            (source / "AppAdvice.java").write_text(
                '@ControllerAdvice class AppAdvice {\n'
                ' @ExceptionHandler(AppException.class) public Object handle(AppException error) {\n'
                '  CommonResponse response = new CommonResponse();\n'
                '  response.setErrorCode(AppErrorCode.INVALID_NAME);\n'
                '  return response;\n }\n}',
                encoding="utf-8",
            )
            spec = root / "openapi.json"
            spec.write_text(json.dumps({
                "openapi": "3.0.0", "tags": [{"name": "things"}],
                "paths": {"/things": {"post": {
                    "operationId": "create", "tags": ["things"],
                    "requestBody": {"content": {"application/json": {"schema": {
                        "type": "object", "required": ["name"], "properties": {"name": {"type": "string"}},
                    }}}},
                    "responses": {"200": {"content": {"application/json": {
                        "example": {"errorCode": "0", "data": {"id": "thing-1"}},
                    }}}},
                }}},
            }), encoding="utf-8")
            self.assertEqual(cli.init_command(["--qa-root", str(qa_root), "--shared-cli"]), 0)
            code = cli.generate_command([
                "--qa-root", str(qa_root), "--openapi", str(spec), "--source-root", str(source), "--shared-cli",
            ])
            self.assertEqual(code, 0)
            module = next((qa_root / "data" / "contracts" / "modules").iterdir())
            cases = yaml.safe_load((module / "cases.yaml").read_text(encoding="utf-8"))["cases"]
            validation = next(case for case in cases if case["scenario"] == "validation")
            self.assertEqual(validation["expected"]["http_status"], 200)
            self.assertEqual(validation["expected"]["business_code"], "E100")
            self.assertIn({"path": "$.errorCode", "equals": "E100"}, validation["assertions"])
            profile = yaml.safe_load(
                (qa_root / "data" / "contracts" / "exception-profile.yaml").read_text(encoding="utf-8")
            )
            self.assertEqual(profile["handlers"][0]["evidence"]["endpoint_scope"], [validation["endpoint_id"]])

    def test_excel_upload_generates_six_executable_fixture_types(self):
        cli = load_script("bruno_api_test_generator")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            qa_root = root / "qa"
            spec = root / "openapi.json"
            spec.write_text(json.dumps({
                "openapi": "3.0.0", "tags": [{"name": "imports"}],
                "paths": {"/imports": {"post": {
                    "operationId": "importThings", "tags": ["imports"],
                    "requestBody": {"content": {"multipart/form-data": {"schema": {
                        "type": "object", "required": ["file"], "properties": {"file": {
                            "type": "string", "format": "binary", "x-allowed-extensions": ["xlsx"],
                            "x-template-columns": ["id", "name"], "x-max-size": 1024,
                        }},
                    }}}},
                    "responses": {
                        "200": {"content": {"application/json": {"example": {"code": 0, "data": {"imported": 1}}}}},
                        "400": {"content": {"application/json": {"example": {"errorCode": "E_FILE", "message": "invalid"}}}},
                    },
                }}},
            }), encoding="utf-8")
            self.assertEqual(cli.init_command(["--qa-root", str(qa_root), "--shared-cli"]), 0)
            self.assertEqual(cli.generate_command([
                "--qa-root", str(qa_root), "--openapi", str(spec), "--shared-cli",
            ]), 0)
            manifest = yaml.safe_load((qa_root / "fixtures" / "generated" / "manifest.yaml").read_text(encoding="utf-8"))
            types = {item["type"] for item in manifest["fixtures"]}
            required = {"legal", "empty", "header-only", "missing-column", "invalid-content", "oversized"}
            self.assertTrue(required.issubset(types), types)
            self.assertTrue(all((qa_root / item["path"]).is_file() for item in manifest["fixtures"] if item["type"] in required))
            self.assertTrue(all(item["variable"] != "UPLOAD_FILE" for item in manifest["fixtures"]))

    def test_inapplicable_file_and_business_error_cases_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            _, constraints, qa_root, module = generated_project(Path(directory))
            cases_path = module / "cases.yaml"
            cases = yaml.safe_load(cases_path.read_text(encoding="utf-8"))
            endpoint_id = cases["cases"][0]["endpoint_id"]
            for scenario in ("file", "business_error"):
                mutated = copy.deepcopy(cases)
                mutated["cases"].append({
                    "id": f"INVALID_{scenario.upper()}", "endpoint_id": endpoint_id,
                    "scenario": scenario, "request": {}, "expected": {"http_status": 400}, "assertions": [],
                })
                cases_path.write_text(yaml.safe_dump(mutated, sort_keys=False), encoding="utf-8")
                errors = constraints.validate_stage(qa_root, "generation")
                self.assertTrue(any("[SCN-001]" in error and scenario in error for error in errors), errors)

    def test_manual_confirmation_requires_complete_search_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            _, constraints, qa_root, module = generated_project(Path(directory))
            cases_path = module / "cases.yaml"
            document = yaml.safe_load(cases_path.read_text(encoding="utf-8"))
            case = document["cases"][0]
            case["review_required"] = True
            case["manual_confirmation"] = {
                "automation_blocker": "No automatic source",
                "search_records": [{"source_kind": "source", "file": "Thing.java"}],
            }
            cases_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
            errors = constraints.validate_stage(qa_root, "generation")
            self.assertTrue(any("[MAN-001]" in error and "symbol" in error for error in errors), errors)
            self.assertTrue(any("[MAN-001]" in error and "line" in error for error in errors), errors)

    def test_failed_run_gate_never_invokes_bruno_process(self):
        runner = load_script("run_bruno")
        with tempfile.TemporaryDirectory() as directory:
            _, _, qa_root, _ = generated_project(Path(directory))
            rules_path = qa_root / "data" / "constraints" / "rules.yaml"
            rules = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
            next(item for item in rules["rules"] if item["id"] == "ASSERT-001")["enabled"] = False
            rules_path.write_text(yaml.safe_dump(rules, sort_keys=False), encoding="utf-8")
            args = argparse.Namespace(module=None, bruno_cli="bru", cli_timeout=None)
            execution_log = qa_root / "results" / "logs" / "gate.log"
            execution_log.parent.mkdir(parents=True, exist_ok=True)
            with mock.patch.object(runner.subprocess, "run") as process:
                code = runner.execute(args, qa_root, execution_log)
            self.assertNotEqual(code, 0)
            process.assert_not_called()

    def test_git_history_source_or_command_fails_src_001(self):
        with tempfile.TemporaryDirectory() as directory:
            _, constraints, qa_root, _ = generated_project(Path(directory))
            source_path = qa_root / "data" / "constraints" / "source-rules.yaml"
            source = yaml.safe_load(source_path.read_text(encoding="utf-8"))
            source["forbidden_command"] = "git show HEAD:service.java"
            source["evidence"] = {"source_kind": "git_history"}
            source_path.write_text(yaml.safe_dump(source, sort_keys=False), encoding="utf-8")
            errors = constraints.validate_stage(qa_root, "generation")
            self.assertTrue(any("[SRC-001]" in error and "Git history command" in error for error in errors), errors)
            self.assertTrue(any("[SRC-001]" in error and "git_history" in error for error in errors), errors)


if __name__ == "__main__":
    unittest.main()
