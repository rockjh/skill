from __future__ import annotations

import importlib
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class DesignUnderstandingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.design_rules = importlib.import_module("dev_ai.domains.api_test.design_rules")

    def test_prose_is_understood_without_fixed_markers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            design = root / "design.md"
            design.write_text(
                "The operator calls POST /records/check. The check only returns a preview and does not write to the database. "
                "After confirmation the detail status becomes SUCCESS.",
                encoding="utf-8",
            )
            manifest = {"endpoints": [
                {"id": "CHECK", "method": "POST", "path": "/records/check", "responses": {"200": {}}},
            ]}
            document, errors = self.design_rules.build_rules(root, [design], manifest)
            self.assertTrue(document["rules"])
            rule = document["rules"][0]
            self.assertEqual("derived", rule["evidence_level"])
            self.assertTrue(document["documents"][0]["parser_gap"])
            self.assertTrue(document["parser_diagnostics"][0]["parser_gap"])
            from dev_ai.core.schema import get_schema, validate_schema
            self.assertEqual(validate_schema(get_schema("api-test.design-rules")["document"], document), [])
            self.assertEqual("exact", rule["mapping_category"])
            self.assertEqual([{"path": "$.status", "equals": "SUCCESS"}], rule["assertions"])
            self.assertTrue(rule["understanding"]["negative_constraints"])
            self.assertFalse(any("missing design documentation" in error for error in errors))

    def test_prose_status_values_are_not_limited_to_uppercase(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            design = root / "design.md"
            design.write_text(
                "POST /jobs returns status completed; state changes from pending to running.",
                encoding="utf-8",
            )
            document, errors = self.design_rules.build_rules(root, [design], {
                "endpoints": [{"id": "JOB_CREATE", "method": "POST", "path": "/jobs", "responses": {"200": {}}}],
            })
            self.assertEqual([], errors)
            rule = document["rules"][0]
            self.assertEqual(["completed"], rule["states"])
            self.assertEqual(["pending -> running"], rule["transitions"])
            self.assertEqual([{"path": "$.status", "equals": "completed"}], rule["assertions"])

    def test_path_parameter_alias_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            design = root / "design.md"
            design.write_text("Fetch GET /records/{uuid}; status becomes READY.", encoding="utf-8")
            document, errors = self.design_rules.build_rules(root, [design], {
                "endpoints": [{"id": "GET_RECORD", "method": "GET", "path": "/records/{recordId}", "responses": {"200": {}}}],
            })
            rule = document["rules"][0]
            self.assertEqual("parameter_alias", rule["mapping_category"])
            self.assertEqual({"uuid": "recordId"}, rule["parameter_aliases"])
            self.assertEqual([], [error for error in errors if "not present in OpenAPI" in error])

    def test_curl_url_is_treated_as_the_same_design_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            design = root / "design.md"
            design.write_text("curl -X POST http://localhost:8080/records/check; status becomes READY.", encoding="utf-8")
            document, _ = self.design_rules.build_rules(root, [design], {
                "endpoints": [{"id": "CHECK", "method": "POST", "path": "/records/check", "responses": {"200": {}}}],
            })
            self.assertEqual("/records/check", document["rules"][0]["path"])

    def test_unique_business_semantics_can_be_an_openapi_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            design = root / "design.md"
            design.write_text("The create record operation succeeds with status CREATED.", encoding="utf-8")
            document, _ = self.design_rules.build_rules(root, [design], {
                "endpoints": [{
                    "id": "CREATE", "method": "POST", "path": "/records",
                    "operation_id": "createRecord", "summary": "Create record", "responses": {"200": {}},
                }],
            })
            self.assertEqual("semantic_candidate", document["rules"][0]["mapping_category"])

    def test_cjk_business_semantics_can_be_an_openapi_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            design = root / "design.md"
            design.write_text("\u67e5\u8be2\u8bb0\u5f55\u63a5\u53e3\u8fd4\u56de status READY.", encoding="utf-8")
            document, errors = self.design_rules.build_rules(root, [design], {"endpoints": [
                {
                    "id": "LIST", "method": "GET", "path": "/records",
                    "summary": "\u67e5\u8be2\u8bb0\u5f55", "responses": {},
                },
            ]})
            self.assertEqual([], errors)
            self.assertEqual("semantic_candidate", document["rules"][0]["mapping_category"])
            self.assertEqual("GET /records", document["rules"][0]["matched_operation"])
            self.assertEqual("derived", document["rules"][0]["evidence_level"])

    def test_design_without_interface_becomes_an_unknown_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            design = root / "design.md"
            design.write_text("The nightly reconciliation must eventually settle all records.", encoding="utf-8")
            document, errors = self.design_rules.build_rules(root, [design], {"endpoints": []})
            self.assertEqual([], document["rules"])
            self.assertEqual("unknown", document["documents"][0]["understanding"])
            self.assertTrue(document["manual_confirmations"])
            self.assertIn("identify the interface in the design", document["manual_confirmations"][0]["options"])
            self.assertEqual([], errors)

    def test_ordered_prose_calls_are_flow_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            design = root / "design.md"
            design.write_text(
                "1. POST /jobs accepts the job and returns status ACCEPTED.\n"
                "2. GET /jobs/{jobId} eventually returns status COMPLETED.",
                encoding="utf-8",
            )
            document, _ = self.design_rules.build_rules(root, [design], {"endpoints": [
                {"id": "CREATE", "method": "POST", "path": "/jobs", "responses": {"202": {}}},
                {"id": "STATUS", "method": "GET", "path": "/jobs/{jobId}", "responses": {"200": {}}},
            ]})
            self.assertEqual(1, len(document["flow_candidates"]))
            candidate = document["flow_candidates"][0]
            self.assertFalse(candidate["can_generate"])
            self.assertEqual([1, 2], [step["order"] for step in candidate["steps"]])

    def test_semantic_safety_facts_are_preserved_as_candidate_assertions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            design = root / "design.md"
            design.write_text(
                "POST /callbacks Duplicate callbacks remain idempotent and do not write duplicate records. "
                "Retry after dependency failure. The callback status becomes SUCCESS.",
                encoding="utf-8",
            )
            document, errors = self.design_rules.build_rules(root, [design], {
                "endpoints": [{"id": "CALLBACK", "method": "POST", "path": "/callbacks", "responses": {"200": {}}}],
            })
            rule = document["rules"][0]
            self.assertTrue(errors)
            self.assertTrue(rule["idempotency"])
            self.assertTrue(rule["retries"])
            self.assertTrue(rule["side_effects"])
            self.assertTrue(any(item["kind"] == "idempotency" for item in rule["candidate_assertions"]))
            self.assertTrue(document["understanding"][0]["side_effects"])
            self.assertTrue(document["manual_confirmations"])

    def test_retry_prose_is_normalized_without_inventing_execution_support(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            design = root / "design.md"
            design.write_text(
                "POST /records/retry. "
                "\u9875\u9762\u91cd\u8bd5\u53ea\u5c06\u7b2c\u4e00\u4e2a\u5931\u8d25\u6b65\u9aa4\u91cd\u7f6e\u4e3a PENDING\uff0c"
                "\u524d\u9762\u6210\u529f\u6b65\u9aa4\u4e0d\u91cd\u590d\u6267\u884c\u3002",
                encoding="utf-8",
            )
            document, errors = self.design_rules.build_rules(root, [design], {"endpoints": [
                {"id": "RETRY", "method": "POST", "path": "/records/retry", "responses": {"200": {}}},
            ]})
            self.assertTrue(errors)
            retry = next(item for item in document["rules"][0]["candidate_assertions"] if item["kind"] == "retry")
            self.assertEqual([{"subject": "first_failed_step", "to": "PENDING"}], retry["state_changes"])
            self.assertTrue(retry["successful_steps_unchanged"])
            self.assertTrue(retry["successful_steps_not_reexecuted"])
            self.assertFalse(retry["executable"])

    def test_natural_language_http_status_is_checked_against_openapi(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            design = root / "design.md"
            design.write_text(
                "POST /orders rejects invalid requests and returns HTTP 400 with business error code E100.",
                encoding="utf-8",
            )
            document, errors = self.design_rules.build_rules(root, [design], {"endpoints": [
                {"id": "ORDERS", "method": "POST", "path": "/orders", "responses": {"400": {}}},
            ]})
            rule = document["rules"][0]
            self.assertEqual([400], rule["http_statuses"])
            self.assertEqual(["E100"], rule["business_codes"])
            self.assertFalse(any("HTTP status is not defined" in error for error in errors))

    def test_mixed_heading_and_inline_operations_are_both_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            design = root / "design.md"
            design.write_text(
                "## GET /records\nRule ID: LIST\nAssert: $.data = []\n\n"
                "The confirmation endpoint is POST /records/confirm and returns status SUCCESS.",
                encoding="utf-8",
            )
            document, _ = self.design_rules.build_rules(root, [design], {"endpoints": [
                {"id": "LIST", "method": "GET", "path": "/records", "responses": {"200": {}}},
                {"id": "CONFIRM", "method": "POST", "path": "/records/confirm", "responses": {"200": {}}},
            ]})
            self.assertEqual({"GET /records", "POST /records/confirm"}, {
                f"{item['method']} {item['path']}" for item in document["rules"]
            })
            self.assertTrue(document["documents"][0]["parser_gap"])

    def test_mutating_ordered_prose_waits_for_a_cleanup_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            design = root / "design.md"
            design.write_text(
                "1. POST /start returns status ACCEPTED.\n"
                "2. GET /finish returns status COMPLETED.",
                encoding="utf-8",
            )
            document, errors = self.design_rules.build_rules(root, [design], {"source": {"sha256": "a" * 64}, "endpoints": [
                {"id": "START", "method": "POST", "path": "/start", "responses": {"200": {}}},
                {"id": "FINISH", "method": "GET", "path": "/finish", "responses": {"200": {}}},
            ]})
            self.assertTrue(any("flow candidate" in error for error in errors), errors)
            self.assertEqual([], document["flows"])
            self.assertEqual("COMPLETED", document["flow_candidates"][0]["final_status"])
            self.assertIn("owned test-data isolation and cleanup action", document["flow_candidates"][0]["unknown"])
            self.assertTrue(document["manual_confirmations"])

    def test_negative_response_boundary_materializes_as_not_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            design = root / "design.md"
            design.write_text(
                "POST /preview returns status SUCCESS and does not return $.writeId.",
                encoding="utf-8",
            )
            document, _ = self.design_rules.build_rules(root, [design], {"endpoints": [
                {"id": "PREVIEW", "method": "POST", "path": "/preview", "responses": {"200": {}}},
            ]})
            self.assertIn({"path": "$.writeId", "exists": False}, document["rules"][0]["assertions"])

    def test_semantic_mapping_records_equally_plausible_openapi_operations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            design = root / "design.md"
            design.write_text("The record lookup operation returns status READY.", encoding="utf-8")
            document, _ = self.design_rules.build_rules(root, [design], {"endpoints": [
                {"id": "LIST", "method": "GET", "path": "/records", "operation_id": "recordLookup", "summary": "Record lookup", "responses": {}},
                {"id": "SEARCH", "method": "POST", "path": "/records/search", "operation_id": "searchRecords", "summary": "Record lookup search", "responses": {}},
            ]})
            self.assertEqual(1, document["mapping"]["counts"]["multiple_candidates"])
            self.assertTrue(any(item["rule_id"].startswith("UNKNOWN-DESIGN-") for item in document["manual_confirmations"]))
            self.assertEqual(document["manual_confirmations"][-1]["rule_id"], document["understanding"][0]["rule_id"])

    def test_conflicting_design_documents_create_a_specific_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.md"
            second = root / "second.md"
            first.write_text("## GET /records\nRule ID: RECORDS_READY\nAssert: $.status = READY\n", encoding="utf-8")
            second.write_text("## GET /records\nRule ID: RECORDS_PENDING\nAssert: $.status = PENDING\n", encoding="utf-8")
            document, errors = self.design_rules.build_rules(root, [first, second], {"endpoints": [
                {"id": "LIST", "method": "GET", "path": "/records", "responses": {"200": {}}},
            ]})
            self.assertTrue(any("conflicting design documents" in error for error in errors), errors)
            conflict = next(item for item in document["manual_confirmations"] if item["rule_id"].startswith("DESIGN-CONFLICT-"))
            self.assertEqual("GET /records", conflict["related_interface"])
            self.assertIn("authoritative design", " ".join(conflict["options"]))
            self.assertTrue(conflict["design_quote"])
            self.assertTrue(all(item["can_generate"] is False for item in document["understanding"]))
            self.assertTrue(all(any("differently" in reason for reason in item["unknown"]) for item in document["understanding"]))

    def test_unordered_endpoint_catalog_is_not_promoted_to_a_flow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            design = root / "design.md"
            design.write_text(
                "POST /records returns status CREATED.\n\nGET /records returns status READY.",
                encoding="utf-8",
            )
            document, errors = self.design_rules.build_rules(root, [design], {"endpoints": [
                {"id": "CREATE", "method": "POST", "path": "/records", "responses": {}},
                {"id": "LIST", "method": "GET", "path": "/records", "responses": {}},
            ]})
            self.assertFalse(errors)
            self.assertEqual([], document["flows"])
            self.assertEqual([], document["flow_candidates"])

    def test_flowchart_and_acceptance_lines_preserve_explicit_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            design = root / "design.md"
            design.write_text(
                "```mermaid\n"
                "sequenceDiagram\n"
                "A->>S: POST /jobs\n"
                "S-->>A: GET /jobs/{id}\n"
                "```\n"
                "- [ ] POST /jobs returns status accepted\n"
                "- [ ] GET /jobs/{id} returns status completed\n",
                encoding="utf-8",
            )
            document, errors = self.design_rules.build_rules(root, [design], {
                "endpoints": [
                    {"id": "JOB_CREATE", "method": "POST", "path": "/jobs", "responses": {"200": {}}},
                    {"id": "JOB_GET", "method": "GET", "path": "/jobs/{id}", "responses": {"200": {}}},
                ],
            })
            self.assertTrue(any("flow candidate" in error for error in errors), errors)
            self.assertEqual(1, len(document["flow_candidates"]))
            self.assertEqual(
                ["POST /jobs", "GET /jobs/{id}"],
                [step["operation"] for step in document["flow_candidates"][0]["steps"]],
            )

    def test_blocked_generation_still_reports_all_output_categories(self) -> None:
        cli = importlib.import_module("dev_ai.domains.api_test.cli")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            qa_root = root / "qa"
            design = root / "design.md"
            design.write_text("POST /things rejects invalid requests without a defined business error code.", encoding="utf-8")
            spec = root / "openapi.json"
            spec.write_text(json.dumps({
                "openapi": "3.0.0",
                "paths": {"/things": {"post": {"responses": {"400": {"description": "bad"}}}}},
            }), encoding="utf-8")
            self.assertEqual(0, cli.init_command(["--qa-root", str(qa_root), "--design-file", str(design)]))
            output = io.StringIO()
            errors = io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                result = cli.generate_command([
                    "--qa-root", str(qa_root), "--openapi", str(spec), "--design-file", str(design),
                ])
            self.assertEqual(2, result)
            self.assertIn("status=blocked", output.getvalue())
            self.assertIn("formal_cases=0", output.getvalue())
            self.assertIn("protocol_cases=0", output.getvalue())
            self.assertIn("pending_confirmations=", output.getvalue())
            self.assertIn("gate_failures=", output.getvalue())

    def test_missing_design_writes_a_blocked_audit_report(self) -> None:
        cli = importlib.import_module("dev_ai.domains.api_test.cli")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            qa_root = root / "qa"
            spec = root / "openapi.json"
            spec.write_text(json.dumps({"openapi": "3.0.0", "paths": {}}), encoding="utf-8")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = cli.generate_command([
                    "--qa-root", str(qa_root), "--openapi", str(spec), "--design-file", str(root / "missing.md"),
                ])
            self.assertEqual(2, result)
            report = json.loads((qa_root / "results" / "design-generation-report.json").read_text(encoding="utf-8"))
            self.assertEqual("blocked", report["status"])
            self.assertIn("no design source", report["gate_failures"])

    def test_generation_exception_is_reported_as_failed_gate(self) -> None:
        cli = importlib.import_module("dev_ai.domains.api_test.cli")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            qa_root = root / "qa"
            design = root / "design.md"
            design.write_text(
                "## GET /things\nRule ID: THINGS_LIST\nHTTP status: 200\n"
                "Business code: 0\nAssert: $.data = []\n",
                encoding="utf-8",
            )
            spec = root / "openapi.json"
            spec.write_text(json.dumps({
                "openapi": "3.0.0",
                "paths": {"/things": {"get": {"responses": {"200": {"description": "ok"}}}}},
            }), encoding="utf-8")
            self.assertEqual(0, cli.init_command(["--qa-root", str(qa_root), "--design-file", str(design)]))
            with mock.patch.object(cli, "apply_to_contracts", side_effect=ValueError("flow owner mismatch")):
                result = cli.generate_command([
                    "--qa-root", str(qa_root), "--openapi", str(spec), "--design-file", str(design),
                ])
            self.assertEqual(2, result)
            report = json.loads((qa_root / "results" / "design-generation-report.json").read_text(encoding="utf-8"))
            self.assertEqual("failed", report["status"])
            self.assertTrue(any("flow owner mismatch" in item for item in report["gate_failures"]))

    def test_successful_generation_refreshes_final_design_report(self) -> None:
        cli = importlib.import_module("dev_ai.domains.api_test.cli")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            qa_root = root / "qa"
            design = root / "design.md"
            design.write_text(
                "## GET /things\nRule ID: THINGS_LIST\nHTTP status: 200\n"
                "Business code: 0\nAssert: $.data = []\n",
                encoding="utf-8",
            )
            spec = root / "openapi.json"
            spec.write_text(json.dumps({
                "openapi": "3.0.0",
                "tags": [{"name": "things"}],
                "paths": {"/things": {"get": {
                    "operationId": "listThings", "tags": ["things"],
                    "responses": {"200": {"description": "ok", "content": {
                        "application/json": {"example": {"code": 0, "data": []}},
                    }}},
                }}},
            }), encoding="utf-8")
            self.assertEqual(0, cli.init_command(["--qa-root", str(qa_root), "--design-file", str(design)]))
            self.assertEqual(0, cli.generate_command([
                "--qa-root", str(qa_root), "--openapi", str(spec), "--design-file", str(design),
            ]))
            report = json.loads((qa_root / "results" / "design-generation-report.json").read_text(encoding="utf-8"))
            self.assertEqual("complete", report["status"])
            self.assertEqual("not_started", report["execution"])
            self.assertTrue(report["formal_tests"])
            self.assertFalse(report["pending_confirmations"])
            self.assertTrue(report["unexecuted"])


if __name__ == "__main__":
    unittest.main()
