from __future__ import annotations

import argparse
import copy
import importlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import yaml


ROOT = Path(__file__).parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def load_script(name: str):
    aliases = {"bruno_api_test_generator": "cli", "qa_constraints": "constraints"}
    return importlib.import_module(f"dltk.api_test_{aliases.get(name, name)}")


def generated_project(root: Path, design_text: str | None = None) -> tuple[object, object, Path, Path]:
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
            "parameters": [{"name": "mode", "in": "query", "schema": {"type": "string"}}],
            "responses": {"200": {"description": "ok", "content": {
                "application/json": {"example": {"code": 0, "data": []}},
            }}},
        }}},
    }), encoding="utf-8")
    design = root / "docs" / "design"
    design.mkdir(parents=True)
    (design / "things.md").write_text(
        design_text or (
            "# Things\n\n## GET /things\nRule ID: THINGS_LIST\nHTTP status: 200\n"
            "Business code: 0\nAssert: $.data = []\n"
        ),
        encoding="utf-8",
    )
    if cli.init_command(["--qa-root", str(qa_root)]) != 0:
        raise AssertionError("init failed")
    if cli.generate_command([
        "--qa-root", str(qa_root), "--openapi", str(spec),
    ]) != 0:
        raise AssertionError("generation failed")
    module = next((qa_root / "contracts" / "modules").iterdir())
    return cli, constraints, qa_root, module


class ConstraintGateTests(unittest.TestCase):
    def test_design_discovery_includes_markdown_extension(self):
        design_rules = load_script("design_rules")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            design = root / "docs" / "design"
            design.mkdir(parents=True)
            expected = design / "things.markdown"
            expected.write_text("## GET /things\nRule ID: THINGS_LIST\n", encoding="utf-8")

            discovered = design_rules.discover(root)

            self.assertEqual(discovered.files, (expected.resolve(),))

    def test_init_blocks_ambiguous_design_roots_without_writing_assets(self):
        cli = load_script("bruno_api_test_generator")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in (Path("docs/design"), Path("design")):
                candidate = root / relative
                candidate.mkdir(parents=True)
                (candidate / "things.md").write_text(
                    "## GET /things\nRule ID: THINGS\nAssert: $.data = []\n",
                    encoding="utf-8",
                )

            self.assertEqual(cli.init_command(["--qa-root", str(root / "qa")]), 2)
            self.assertFalse((root / "qa").exists())

    def test_design_values_remain_typed_and_async_false_is_not_async(self):
        design_rules = load_script("design_rules")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            design = root / "design.md"
            design.write_text(
                "## GET /things\nRule ID: THINGS\nAsync: false\nBusiness code: 1\n"
                "Assert: $.enabled = true\nAssert: $.items = []\n",
                encoding="utf-8",
            )
            document, errors = design_rules.build_rules(root, [design], {
                "endpoints": [{"id": "THINGS", "method": "GET", "path": "/things", "responses": {"200": {}}}],
            })

            self.assertEqual(errors, [])
            rule = document["rules"][0]
            self.assertEqual(rule["business_codes"], [1])
            self.assertEqual(rule["assertions"], [
                {"path": "$.enabled", "equals": True},
                {"path": "$.items", "equals": []},
            ])
            self.assertFalse(rule["async"])

    def test_business_branch_requires_request_and_openapi_request_shape_wins(self):
        design_rules = load_script("design_rules")
        manifest = {"endpoints": [{
            "id": "THINGS", "method": "GET", "path": "/things",
            "parameters": [{"name": "page", "in": "query", "schema": {"type": "integer"}}],
            "responses": {"200": {}},
        }]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            design = root / "design.md"
            design.write_text(
                "## GET /things\nRule ID: BUSY\nScenario: business_error\nAssert: $.code = 1001\n",
                encoding="utf-8",
            )
            document, errors = design_rules.build_rules(root, [design], manifest)
            self.assertTrue(any("explicit Request mapping" in error for error in errors), errors)
            self.assertTrue(document["manual_confirmations"])

            design.write_text(
                "## GET /things\nRule ID: BUSY\nScenario: business_error\n"
                "Request: {query: {unknown: 1}}\nAssert: $.code = 1001\n",
                encoding="utf-8",
            )
            _, errors = design_rules.build_rules(root, [design], manifest)
            self.assertTrue(any("conflicts with OpenAPI" in error and "unknown" in error for error in errors), errors)

    def test_design_request_body_and_headers_cannot_override_openapi(self):
        design_rules = load_script("design_rules")
        manifest = {"endpoints": [{
            "id": "THINGS", "method": "POST", "path": "/things",
            "parameters": [{"name": "X-Trace", "in": "header", "schema": {"type": "string"}}],
            "request_body": {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "additionalProperties": False,
                "required": ["name"], "properties": {"name": {"type": "string"}},
            }}}},
            "responses": {"200": {}},
        }]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            design = root / "design.md"
            design.write_text(
                "## POST /things\nRule ID: BAD_REQUEST\nScenario: business_error\n"
                "Request: {headers: {Authorization: token}, body: {name: 1, extra: x}}\n"
                "Assert: $.code = E1\n",
                encoding="utf-8",
            )
            _, errors = design_rules.build_rules(root, [design], manifest)
            self.assertTrue(any("headers" in error and "Authorization" in error for error in errors), errors)
            self.assertTrue(any("request.body.name" in error and "type string" in error for error in errors), errors)
            self.assertTrue(any("request.body.extra" in error and "absent from OpenAPI" in error for error in errors), errors)

    def test_source_business_inference_modules_are_removed(self):
        for name in ("analyze_source_logic", "analyze_java_logic", "source_constraints"):
            with self.subTest(module=name):
                self.assertIsNone(importlib.util.find_spec(f"dltk.api_test_{name}"))

    def test_design_exclusions_require_explicit_approval_when_declared(self):
        design_rules = load_script("design_rules")
        manifest = {"endpoints": [{"id": "MOCK", "method": "GET", "path": "/mock"}]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            design = root / "design.md"
            design.write_text("# Auxiliary endpoints\n", encoding="utf-8")
            exclusions = root / "exclusions.yaml"
            exclusions.write_text(yaml.safe_dump({"exclusions": [{
                "method": "GET", "path": "/mock", "reason": "mock endpoint",
            }]}), encoding="utf-8")
            _, implicit_errors = design_rules.build_rules(root, [design], manifest)
            self.assertTrue(any("missing design documentation" in error for error in implicit_errors))

            exclusions.write_text(yaml.safe_dump({"exclusions": [{
                "method": "GET", "path": "/mock", "approved": False, "reason": "mock endpoint",
            }]}), encoding="utf-8")

            _, pending_errors = design_rules.build_rules(root, [design], manifest)
            self.assertTrue(any("missing design documentation" in error for error in pending_errors))

            exclusions.write_text(yaml.safe_dump({"exclusions": [{
                "method": "GET", "path": "/mock", "status": "approved", "reason": "mock endpoint",
            }]}), encoding="utf-8")
            document, approved_errors = design_rules.build_rules(root, [design], manifest)
            self.assertEqual(approved_errors, [])
            self.assertEqual(document["coverage"]["excluded_endpoints"], 1)

    def test_approved_design_exclusion_removes_generated_endpoint_cases(self):
        cli = load_script("bruno_api_test_generator")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            qa_root = root / "quality-assets"
            spec = root / "openapi.json"
            spec.write_text(json.dumps({
                "openapi": "3.0.0",
                "paths": {"/mock": {"get": {"responses": {"200": {"description": "ok"}}}}},
            }), encoding="utf-8")
            design = root / "design.md"
            design.write_text("# Auxiliary endpoints\n", encoding="utf-8")
            exclusions = qa_root / "constraints" / "exclusions.yaml"
            exclusions.parent.mkdir(parents=True)
            exclusions.write_text(yaml.safe_dump({"exclusions": [{
                "method": "GET", "path": "/mock", "status": "approved", "reason": "mock endpoint",
            }]}), encoding="utf-8")

            self.assertEqual(cli.init_command([
                "--qa-root", str(qa_root), "--design-file", str(design),
            ]), 0)
            self.assertEqual(cli.generate_command([
                "--qa-root", str(qa_root), "--openapi", str(spec), "--design-file", str(design),
            ]), 0)

            module = next((qa_root / "contracts" / "modules").iterdir())
            self.assertEqual(yaml.safe_load((module / "cases.yaml").read_text(encoding="utf-8"))["cases"], [])
            module_exclusions = yaml.safe_load((module / "exclusions.yaml").read_text(encoding="utf-8"))["exclusions"]
            self.assertEqual(module_exclusions[0]["endpoint_id"], "GET_MOCK")
            self.assertTrue(module_exclusions[0]["design_coverage"])

    def test_openapi_vendor_flag_is_not_safety_authority(self):
        parser = load_script("parse_openapi")
        endpoint = {
            "id": "THING_CREATE", "method": "POST", "path": "/things",
            "x-idempotent": True, "responses": {"200": {}},
        }

        self.assertFalse(parser.inferred_scenario_matrix(endpoint)["safety"]["applicable"])
        self.assertFalse(any(case["scenario"] == "safety" for case in parser.seed_contract_cases(endpoint)))

    def test_design_mapping_is_bidirectional_and_blocks_path_drift(self):
        design_rules = load_script("design_rules")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            design = root / "design.md"
            design.write_text(
                "## GET /covered\nRule ID: COVERED\n\n## POST /stale\nRule ID: STALE\n",
                encoding="utf-8",
            )
            manifest = {"endpoints": [
                {"id": "COVERED", "method": "GET", "path": "/covered"},
                {"id": "MISSING", "method": "GET", "path": "/missing"},
            ]}
            document, errors = design_rules.build_rules(root, [design], manifest)
            self.assertEqual(document["coverage"]["documented_endpoints"], 1)
            self.assertTrue(any("POST /stale is not present in OpenAPI" in error for error in errors), errors)
            self.assertTrue(any("GET /missing" in error for error in errors), errors)

    def test_design_gate_rejects_non_design_business_logic(self):
        with tempfile.TemporaryDirectory() as directory:
            _, constraints, qa_root, module = generated_project(Path(directory))
            logic_path = module / "logic.yaml"
            document = yaml.safe_load(logic_path.read_text(encoding="utf-8"))
            document["logic"][0]["source"] = "source"
            document["logic"][0]["source_candidate_id"] = "SRC-1"
            logic_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
            errors = constraints.validate_stage(qa_root, "generation")
            self.assertTrue(any("[DESIGN-001]" in error and "must come from design" in error for error in errors), errors)
            self.assertTrue(any("[DESIGN-001]" in error and "non-design evidence" in error for error in errors), errors)

    def test_design_gate_requires_logic_openapi_traceability(self):
        with tempfile.TemporaryDirectory() as directory:
            _, constraints, qa_root, module = generated_project(Path(directory))
            logic_path = module / "logic.yaml"
            document = yaml.safe_load(logic_path.read_text(encoding="utf-8"))
            document["logic"][0].pop("openapi_operation")
            logic_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
            errors = constraints.validate_stage(qa_root, "generation")
            self.assertTrue(any("must reference OpenAPI operation" in error for error in errors), errors)

    def test_generated_design_artifacts_match_scoped_schemas(self):
        from dltk.schema import get_schema, validate_schema

        with tempfile.TemporaryDirectory() as directory:
            _, _, qa_root, module = generated_project(Path(directory))
            artifacts = {
                "api-test.design-rules": qa_root / "constraints" / "design-rules.yaml",
                "api-test.logic": module / "logic.yaml",
                "api-test.value-resolution": module / "value-resolution.yaml",
                "api-test.version-lock": qa_root / "contracts" / "version-lock.yaml",
            }
            for scope, path in artifacts.items():
                with self.subTest(scope=scope):
                    document = yaml.safe_load(path.read_text(encoding="utf-8"))
                    self.assertEqual(validate_schema(get_schema(scope)["document"], document), [])
            self.assertFalse((qa_root / "constraints" / "source-rules.yaml").exists())

    def test_multiple_design_rules_generate_distinct_cases_and_logic(self):
        design = (
            "# Things\n\n## GET /things\n"
            "Rule ID: THINGS_LIST_OK\nScenario: success\nHTTP status: 200\n"
            "Request: {query: {mode: normal}}\nBusiness code: 0\nAssert: $.data = []\n\n"
            "Rule ID: THINGS_LIST_BUSY\nScenario: business_error\nHTTP status: 200\n"
            "Request: {query: {mode: busy}}\nBusiness code: 1001\nAssert: $.code = 1001\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            _, _, _, module = generated_project(Path(directory), design)
            cases = yaml.safe_load((module / "cases.yaml").read_text(encoding="utf-8"))["cases"]
            logic = yaml.safe_load((module / "logic.yaml").read_text(encoding="utf-8"))["logic"]
            business = next(case for case in cases if case["scenario"] == "business_error")
            self.assertEqual(business["source"], "design")
            self.assertEqual(business["design_rule_ids"], ["THINGS_LIST_BUSY"])
            self.assertEqual(business["expected"]["http_status"], 200)
            self.assertEqual(business["expected"]["business_code"], 1001)
            self.assertEqual(business["assertions"], [{"path": "$.code", "equals": 1001}])
            self.assertEqual(business["request"], {"query": {"mode": "busy"}})
            self.assertFalse(business["review_required"])
            self.assertNotIn("manual_confirmation", business)
            self.assertEqual({item["design_rule_id"] for item in logic}, {"THINGS_LIST_OK", "THINGS_LIST_BUSY"})
            self.assertTrue(all(item["endpoint_id"] == business["endpoint_id"] for item in logic))
            self.assertTrue(all(item["openapi_operation"] == "GET /things" for item in logic))

    def test_design_http_status_must_exist_in_openapi_responses(self):
        design_rules = load_script("design_rules")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            design = root / "design.md"
            design.write_text("## GET /things\nRule ID: THINGS\nHTTP status: 409\n", encoding="utf-8")
            _, errors = design_rules.build_rules(root, [design], {
                "endpoints": [{"id": "THINGS", "method": "GET", "path": "/things", "responses": {"200": {}}}],
            })
            self.assertTrue(any("HTTP status 409" in error for error in errors), errors)

    def test_async_design_rule_requires_acceptance_and_final_status(self):
        design_rules = load_script("design_rules")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            design = root / "design.md"
            design.write_text(
                "## POST /jobs\nRule ID: JOBS_ASYNC\nScenario: success\nAsync: true\n"
                "HTTP status: 202\nAssert: $.jobId = 1\n",
                encoding="utf-8",
            )
            _, errors = design_rules.build_rules(root, [design], {
                "endpoints": [{"id": "JOBS", "method": "POST", "path": "/jobs", "responses": {"202": {}}}],
            })
            self.assertTrue(any("acceptance status and final status" in error for error in errors), errors)
            design.write_text(
                "## POST /jobs\nRule ID: JOBS_ASYNC\nScenario: success\nAsync: true\n"
                "HTTP status: 202\nAcceptance status: accepted\nFinal status: completed\n"
                "Assert: $.status = completed\n",
                encoding="utf-8",
            )
            _, errors = design_rules.build_rules(root, [design], {
                "endpoints": [{"id": "JOBS", "method": "POST", "path": "/jobs", "responses": {"202": {}}}],
            })
            self.assertTrue(any("executable acceptance/final flow" in error for error in errors), errors)

    def test_explicit_design_flow_makes_async_rule_executable(self):
        design_rules = load_script("design_rules")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            design = root / "design.md"
            design.write_text(
                "## POST /jobs\n"
                "Rule ID: JOB_ACCEPT\nScenario: success\nAsync: true\nHTTP status: 202\n"
                "Acceptance status: accepted\nFinal status: completed\nAssert: $.status = accepted\n"
                "Test Flow: {id: JOB_FLOW, steps: [{rule_id: JOB_ACCEPT, operation: submit, "
                "capture: {job_id: '$.jobId'}}, {rule_id: JOB_FINAL, operation: poll, uses: [job_id]}]}\n\n"
                "## GET /jobs/{jobId}\n"
                "Rule ID: JOB_FINAL\nScenario: success\nHTTP status: 200\n"
                "Request: {path_parameters: {jobId: '{{job_id}}'}}\nAssert: $.status = completed\n",
                encoding="utf-8",
            )
            manifest = {"endpoints": [
                {"id": "JOB_CREATE", "method": "POST", "path": "/jobs", "responses": {"202": {}}},
                {
                    "id": "JOB_STATUS", "method": "GET", "path": "/jobs/{jobId}",
                    "parameters": [{"name": "jobId", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "responses": {"200": {}},
                },
            ]}
            document, errors = design_rules.build_rules(root, [design], manifest)
            self.assertTrue(any("bounded final-state polling" in error for error in errors), errors)
            self.assertEqual("JOB_FLOW", document["flows"][0]["id"])
            self.assertEqual(["job_id"], document["flows"][0]["steps"][0]["capture"])
            self.assertEqual(["job_id"], document["flows"][0]["steps"][1]["uses"])
            self.assertTrue(document["manual_confirmations"])

    def test_design_flow_covers_idempotency_retry_and_external_failure(self):
        design_rules = load_script("design_rules")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            design = root / "design.md"
            design.write_text(
                "## POST /things\n"
                "Rule ID: THING_FIRST\nScenario: safety\nRequest: {}\n"
                "HTTP status: 200\nIdempotency: the same key creates one record\nAssert: $.status = created\n"
                "Test Flow: {id: THING_RETRY_FLOW, steps: [{rule_id: FAULT_ON, operation: fault-inject}, "
                "{rule_id: THING_FIRST, operation: submit}, {rule_id: THING_RETRY, operation: retry}]}\n\n"
                "Rule ID: THING_RETRY\nScenario: safety\nRequest: {}\n"
                "HTTP status: 200\nRetry: retry once after dependency failure\n"
                "External failure: dependency timeout is retried\nAssert: $.status = existing\n\n"
                "## POST /faults\nRule ID: FAULT_ON\nScenario: success\nHTTP status: 200\n"
                "Assert: $.enabled = true\n",
                encoding="utf-8",
            )
            document, errors = design_rules.build_rules(root, [design], {"endpoints": [
                {"id": "THING_CREATE", "method": "POST", "path": "/things", "responses": {"200": {}}},
                {"id": "FAULT_ENABLE", "method": "POST", "path": "/faults", "responses": {"200": {}}},
            ]})
            self.assertTrue(any("authorized fault injection" in error for error in errors), errors)
            self.assertTrue(document["manual_confirmations"])
            self.assertEqual(
                ["fault-inject", "submit", "retry"],
                [step["operation"] for step in document["flows"][0]["steps"]],
            )

    def test_design_flow_materializes_cases_capture_and_manifest(self):
        design_rules = load_script("design_rules")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contracts = root / "contracts"
            module = contracts / "modules" / "jobs"
            module.mkdir(parents=True)
            endpoints = {
                "module": "jobs",
                "endpoints": [
                    {"id": "JOB_CREATE", "method": "POST", "path": "/jobs", "responses": {"202": {}}},
                    {"id": "JOB_STATUS", "method": "GET", "path": "/jobs/{jobId}", "responses": {"200": {}}},
                ],
            }
            (module / "endpoints.yaml").write_text(yaml.safe_dump(endpoints), encoding="utf-8")
            (module / "cases.yaml").write_text(yaml.safe_dump({"cases": [
                {
                    "id": "JOB_CREATE_SUCCESS", "title": "提交任务成功", "endpoint_id": "JOB_CREATE",
                    "scenario": "success", "request": {}, "expected": {"http_status": 202},
                },
                {
                    "id": "JOB_STATUS_SUCCESS", "title": "查询任务成功", "endpoint_id": "JOB_STATUS",
                    "scenario": "success", "request": {}, "expected": {"http_status": 200},
                },
            ]}), encoding="utf-8")
            rules = {
                "rules": [
                    {
                        "id": "JOB_ACCEPT", "endpoint_id": "JOB_CREATE", "scenario": "success",
                        "title": "accepted", "evidence": {"file": "design.md", "line": 1},
                        "request": {}, "http_statuses": [202], "states": [], "business_codes": [],
                        "assertions": [{"path": "$.status", "equals": "accepted"}],
                    },
                    {
                        "id": "JOB_FINAL", "endpoint_id": "JOB_STATUS", "scenario": "success",
                        "title": "completed", "evidence": {"file": "design.md", "line": 10},
                        "request": {"path_parameters": {"jobId": "{{job_id}}"}},
                        "http_statuses": [200], "states": [], "business_codes": [],
                        "assertions": [{"path": "$.status", "equals": "completed"}],
                    },
                ],
                "flows": [{
                    "id": "JOB_FLOW", "mode": "sequential", "steps": [
                        {
                            "rule_id": "JOB_ACCEPT", "operation": "submit", "capture": ["job_id"],
                            "capture_paths": {"job_id": "$.jobId"},
                        },
                        {"rule_id": "JOB_FINAL", "operation": "poll", "uses": ["job_id"]},
                    ],
                }],
                "exclusions": [],
            }
            design_rules.apply_to_contracts(contracts, rules)
            cases = yaml.safe_load((module / "cases.yaml").read_text(encoding="utf-8"))["cases"]
            flow = yaml.safe_load((module / "flows.yaml").read_text(encoding="utf-8"))["flows"][0]
            self.assertEqual({"job_id": "$.jobId"}, cases[0]["captures"])
            self.assertEqual([case["id"] for case in cases], [step["case_id"] for step in flow["steps"]])
            self.assertEqual("design", flow["source"])
            endpoint_doc = yaml.safe_load((module / "endpoints.yaml").read_text(encoding="utf-8"))
            self.assertTrue(all(endpoint["flow_required"] for endpoint in endpoint_doc["endpoints"]))

    def test_openapi_query_and_auth_seeds_need_design_rules(self):
        design_rules = load_script("design_rules")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contracts = root / "contracts"
            module = contracts / "modules" / "things"
            module.mkdir(parents=True)
            (module / "endpoints.yaml").write_text(yaml.safe_dump({
                "module": "things",
                "endpoints": [{
                    "id": "THINGS", "method": "GET", "path": "/things",
                    "responses": {"200": {}},
                    "scenario_matrix": {},
                }],
            }), encoding="utf-8")
            (module / "cases.yaml").write_text(yaml.safe_dump({
                "cases": [
                    {"id": "THINGS_SUCCESS", "endpoint_id": "THINGS", "scenario": "success",
                     "source": "openapi", "request": {"query": {"page": 1}}, "expected": {"http_status": 200}},
                    {"id": "THINGS_QUERY", "endpoint_id": "THINGS", "scenario": "query",
                     "source": "openapi", "openapi_trace": "THINGS:query", "request": {}, "expected": {"http_status": 200}},
                    {"id": "THINGS_AUTH", "endpoint_id": "THINGS", "scenario": "authentication",
                     "source": "openapi", "openapi_trace": "THINGS:authentication", "request": {}, "expected": {"http_status": 401}},
                ],
            }), encoding="utf-8")
            design_rules.apply_to_contracts(contracts, {
                "rules": [{
                    "id": "THINGS_OK", "endpoint_id": "THINGS", "scenario": "success",
                    "title": "THINGS_OK", "http_statuses": [200], "business_codes": ["0"],
                    "assertions": [{"path": "$.data", "equals": []}],
                    "evidence": {"source_kind": "design", "file": "design.md", "line": 1},
                }],
                "exclusions": [],
            })
            cases = yaml.safe_load((module / "cases.yaml").read_text(encoding="utf-8"))["cases"]
            self.assertEqual({case["scenario"] for case in cases}, {"success"})
            endpoint = yaml.safe_load((module / "endpoints.yaml").read_text(encoding="utf-8"))["endpoints"][0]
            self.assertFalse(endpoint["scenario_matrix"]["query"]["applicable"])
            self.assertFalse(endpoint["scenario_matrix"]["authentication"]["applicable"])
            seeded = yaml.safe_load((module / "cases.yaml").read_text(encoding="utf-8"))
            seeded["cases"].append({
                "id": "THINGS_PAGE_2", "endpoint_id": "THINGS", "scenario": "query",
                "source": "openapi", "request": {"query": {"page": 2}}, "expected": {"http_status": 200},
            })
            (module / "cases.yaml").write_text(yaml.safe_dump(seeded), encoding="utf-8")
            design_rules.apply_to_contracts(contracts, {
                "rules": [{
                    "id": "THINGS_PAGE", "endpoint_id": "THINGS", "scenario": "query",
                    "title": "THINGS_PAGE", "http_statuses": [200],
                    "request": {"query": {"page": 2}},
                    "assertions": [{"path": "$.data", "equals": []}],
                    "evidence": {"source_kind": "design", "file": "design.md", "line": 2},
                }],
                "exclusions": [],
            })
            cases = yaml.safe_load((module / "cases.yaml").read_text(encoding="utf-8"))["cases"]
            query = next(case for case in cases if case["scenario"] == "query")
            self.assertEqual(query["request"], {"query": {"page": 2}})
            self.assertEqual(query["source"], "design")
            self.assertEqual(query["design_rule_ids"], ["THINGS_PAGE"])

    def test_generate_stops_when_design_documents_are_missing(self):
        cli = load_script("bruno_api_test_generator")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = root / "openapi.json"
            spec.write_text(json.dumps({
                "openapi": "3.0.0",
                "paths": {"/things": {"get": {"responses": {"200": {"description": "ok"}}}}},
            }), encoding="utf-8")
            self.assertEqual(cli.init_command(["--qa-root", str(root / "qa")]), 2)
            self.assertFalse((root / "qa").exists())

            self.assertEqual(cli.generate_command([
                "--qa-root", str(root / "qa"), "--openapi", str(spec),
            ]), 2)
            report = json.loads((root / "qa" / "results" / "design-generation-report.json").read_text(encoding="utf-8"))
            self.assertEqual("blocked", report["status"])
            self.assertIn("no design source", report["gate_failures"])

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

    def _retired_endpoint_scoped_status_values_do_not_cross_contaminate(self):
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
                    (qa_root / "constraints" / "source-rules.yaml").write_text(
                        yaml.safe_dump(source, sort_keys=False), encoding="utf-8",
                    )
                    errors = constraints.validate_stage(qa_root, "generation")
                    self.assertTrue(any("[MAN-002]" in error and "exceeds 0" in error for error in errors), errors)

    def test_design_overrides_source_controller_advice_business_code(self):
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
                    }}}, "400": {"description": "invalid"}},
                }}},
            }), encoding="utf-8")
            design = root / "design.md"
            design.write_text(
                "## POST /things\nRule ID: THING_CREATE\nHTTP status: 200\n"
                "Business code: E100\nAssert: $.errorCode = E100\nAssert: $.data.id = thing-1\n",
                encoding="utf-8",
            )
            self.assertEqual(cli.init_command([
                "--qa-root", str(qa_root), "--design-file", str(design),
            ]), 0)
            code = cli.generate_command([
                "--qa-root", str(qa_root), "--openapi", str(spec), "--design-file", str(design),
                "--source-root", str(source),
            ])
            self.assertEqual(code, 0)
            module = next((qa_root / "contracts" / "modules").iterdir())
            cases = yaml.safe_load((module / "cases.yaml").read_text(encoding="utf-8"))["cases"]
            success = next(case for case in cases if case["scenario"] == "success")
            self.assertEqual(success["source"], "design")
            self.assertEqual(success["expected"]["http_status"], 200)
            self.assertEqual(success["expected"]["business_code"], "E100")
            self.assertEqual(success["assertions"], [
                {"path": "$.errorCode", "equals": "E100"},
                {"path": "$.data.id", "equals": "thing-1"},
            ])
            self.assertFalse((qa_root / "contracts" / "exception-profile.yaml").exists())

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
            design = root / "design.md"
            design.write_text(
                "## POST /imports\nRule ID: IMPORT_THINGS\nHTTP status: 200\n"
                "Business code: 0\nAssert: $.data.imported = 1\n",
                encoding="utf-8",
            )
            self.assertEqual(cli.init_command([
                "--qa-root", str(qa_root), "--design-file", str(design),
            ]), 0)
            self.assertEqual(cli.generate_command([
                "--qa-root", str(qa_root), "--openapi", str(spec), "--design-file", str(design),
            ]), 0)
            manifest = yaml.safe_load((qa_root / "contracts" / "fixtures" / "generated" / "manifest.yaml").read_text(encoding="utf-8"))
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
            rules_path = qa_root / "constraints" / "rules.yaml"
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
            source_path = qa_root / "constraints" / "source-rules.yaml"
            source = {"version": 1, "field_rules": []}
            source["forbidden_command"] = "git show HEAD:service.java"
            source["evidence"] = {"source_kind": "git_history"}
            source_path.write_text(yaml.safe_dump(source, sort_keys=False), encoding="utf-8")
            errors = constraints.validate_stage(qa_root, "generation")
            self.assertTrue(any("[SRC-001]" in error and "Git history command" in error for error in errors), errors)
            self.assertTrue(any("[SRC-001]" in error and "git_history" in error for error in errors), errors)


if __name__ == "__main__":
    unittest.main()
