from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from dev_ai.cli import console_main


class CliContractTests(unittest.TestCase):
    def invoke(self, *arguments: str) -> tuple[int, dict[str, object]]:
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = console_main([*arguments, "--json"])
        return code, json.loads(stream.getvalue())

    def test_scoped_schema_does_not_expand_other_commands(self) -> None:
        code, result = self.invoke("schema", "api-test.generate")
        self.assertEqual(0, code)
        self.assertTrue(result["ok"])
        self.assertIn("--openapi", result["data"]["options"])
        self.assertNotIn("domains", result["data"])

    def test_design_understanding_command_is_registered_and_persisted(self) -> None:
        code, schema = self.invoke("schema", "api-test.understand")
        self.assertEqual(0, code, schema)
        self.assertIn("--openapi", schema["data"]["options"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            qa_root = root / "qa"
            design = root / "design.md"
            design.write_text("POST /things returns status SUCCESS.", encoding="utf-8")
            spec = root / "openapi.json"
            spec.write_text(json.dumps({
                "openapi": "3.0.0",
                "paths": {"/things": {"post": {"responses": {"200": {"description": "ok"}}}}},
            }), encoding="utf-8")
            code, initialized = self.invoke("api-test", "init", "--qa-root", str(qa_root), "--design-file", str(design))
            self.assertEqual(0, code, initialized)
            code, understood = self.invoke(
                "api-test", "understand", "--qa-root", str(qa_root), "--openapi", str(spec), "--design-file", str(design),
            )
            self.assertEqual(0, code, understood)
            self.assertTrue((qa_root / "constraints" / "design-rules.yaml").is_file())
            self.assertTrue(understood["ok"])
            self.assertEqual(
                str((qa_root / "results" / "design-generation-report.json").resolve()),
                understood["artifact_path"],
            )

    def test_mock_data_commands_have_scoped_multi_module_contracts(self) -> None:
        code, generate = self.invoke("schema", "api-test.mock-data-generate")
        self.assertEqual(0, code)
        self.assertEqual("string[]", generate["data"]["options"]["--module"])
        self.assertIn("--allow-write", generate["data"]["options"])
        code, clean = self.invoke("schema", "api-test.mock-data-clean")
        self.assertEqual(0, code)
        self.assertIn("--run-id", clean["data"]["options"])
        self.assertIn("--allow-cleanup", clean["data"]["options"])

    def test_api_test_persisted_contract_schemas_are_scoped(self) -> None:
        for scope, source in (
            ("api-test.design-rules", "design"),
            ("api-test.logic", None),
            ("api-test.value-resolution", None),
            ("api-test.version-lock", None),
        ):
            with self.subTest(scope=scope):
                code, result = self.invoke("schema", scope)
                self.assertEqual(0, code, result)
                self.assertEqual(scope, result["data"]["contract"])
                self.assertIn("document", result["data"])
                if source:
                    self.assertEqual(source, result["data"]["document"]["properties"]["source"]["const"])

    def test_mock_data_commands_return_public_envelopes_and_semantic_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            qa_root = root / "qa"
            design = root / "design.md"
            design.write_text("# Reviewed design\n", encoding="utf-8")
            code, initialized = self.invoke(
                "api-test", "init", "--qa-root", str(qa_root), "--design-file", str(design),
            )
            self.assertEqual(0, code, initialized)
            code, generated = self.invoke("api-test", "mock-data-generate", "--qa-root", str(qa_root))
            self.assertEqual(0, code, generated)
            self.assertTrue(generated["ok"])
            self.assertIn("results\\mock-data", generated["artifact_path"])
            code, missing = self.invoke("api-test", "mock-data-clean", "--qa-root", str(qa_root))
            self.assertEqual(4, code, missing)
            self.assertEqual("TARGET_NOT_FOUND", missing["error"]["code"])

    def test_mock_data_subcommand_help_is_registered(self) -> None:
        from dev_ai.domains.api_test.cli import main as api_test_main

        stream = io.StringIO()
        with redirect_stdout(stream), self.assertRaises(SystemExit) as exit_context:
            api_test_main(["mock-data-generate", "--help"])
        self.assertEqual(0, exit_context.exception.code)
        self.assertIn("--module", stream.getvalue())
        self.assertIn("--allow-write", stream.getvalue())

    def test_e2e_contract_schemas_are_scoped_and_complete(self) -> None:
        code, scenario = self.invoke("schema", "e2e.scenario")
        self.assertEqual(0, code)
        controls = scenario["data"]["document"]["properties"]["controls"]["properties"]
        self.assertIn("database_control", controls)
        self.assertIn("restoration_verification", controls["database_control"]["properties"]["safety"]["oneOf"][1]["properties"])

        code, report = self.invoke("schema", "e2e.report")
        self.assertEqual(0, code)
        self.assertEqual("workspace_inventory", report["data"]["gate_order"][0])
        self.assertLess(report["data"]["gate_order"].index("static"), report["data"]["gate_order"].index("environment_tests"))

        code, plan = self.invoke("schema", "e2e.scenario-plan")
        self.assertEqual(0, code)
        scenario_item = plan["data"]["document"]["properties"]["scenarios"]["items"]
        self.assertIn("design_rule_ids", scenario_item["properties"])
        self.assertIn("protocol_refs", scenario_item["properties"])

    def test_e2e_generation_commands_have_scoped_options_and_help(self) -> None:
        code, generate = self.invoke("schema", "e2e.generate")
        self.assertEqual(0, code)
        self.assertIn("--design-file", generate["data"]["options"])
        self.assertIn("--openapi-file", generate["data"]["options"])
        code, check = self.invoke("schema", "e2e.check")
        self.assertEqual(0, code)
        self.assertNotIn("--design-file", check["data"]["options"])

        from dev_ai.domains.e2e.cli import main as e2e_main

        for command in ("discover", "generate"):
            stream = io.StringIO()
            with redirect_stdout(stream), self.assertRaises(SystemExit) as exit_context:
                e2e_main([command, "--help"])
            self.assertEqual(0, exit_context.exception.code)
            self.assertIn("--design-root", stream.getvalue())

    def test_e2e_generate_failure_uses_public_error_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            code, initialized = self.invoke("e2e", "init", "--project", temporary)
            self.assertEqual(0, code, initialized)
            code, failed = self.invoke("e2e", "generate", "--project", temporary)
            self.assertNotEqual(0, code)
            self.assertFalse(failed["ok"])
            self.assertEqual("e2e.generate", failed["command"])
            self.assertEqual("GATE_FAILED", failed["error"]["code"])

    def test_domain_versions_are_separate_from_tool_version(self) -> None:
        code, result = self.invoke("version")
        self.assertEqual(0, code)
        data = result["data"]
        self.assertNotEqual(data["version"], data["api_test_schema"])
        self.assertNotEqual(data["version"], data["e2e_gate_schema"])

    def test_e2e_init_writes_assets_without_tool_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            code, result = self.invoke("e2e", "init", "--project", str(root))
            self.assertEqual(0, code, result)
            self.assertTrue((root / ".dev-ai.lock.json").is_file())
            self.assertFalse((root / "scripts").exists())
            self.assertFalse((root / "common" / "e2e_runtime.py").exists())
            self.assertTrue((root / "scenarios" / "scenario.template.yaml").is_file())
            self.assertTrue((root / "run-e2e.bat").is_file())

    def test_api_init_writes_flat_assets_without_tool_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            root = project / "qa"
            design = project / "design.md"
            design.write_text("# Reviewed design\n", encoding="utf-8")
            code, result = self.invoke(
                "api-test", "init", "--qa-root", str(root), "--design-file", str(design),
            )
            self.assertEqual(0, code, result)
            self.assertEqual(
                {"bruno", "contracts", "constraints", "execution", "results"},
                {path.name for path in root.iterdir() if path.is_dir()},
            )
            self.assertTrue((root / ".dev-ai.lock.json").is_file())
            self.assertTrue((root / "contracts" / "version-lock.yaml").is_file())
            self.assertFalse((root / "scripts").exists())
            for name in (
                "generate-mock-data.bat", "generate-mock-data.sh",
                "clean-mock-data.bat", "clean-mock-data.sh",
            ):
                self.assertTrue((root / "execution" / name).is_file())

    def test_api_version_lock_can_complete_through_the_public_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            qa_root = project / "quality-assets"
            design = project / "design.md"
            design.write_text("# Reviewed design\n", encoding="utf-8")
            code, result = self.invoke(
                "api-test", "init", "--qa-root", str(qa_root), "--design-file", str(design),
            )
            self.assertEqual(0, code, result)
            self.assertIn("status: draft", (qa_root / "contracts" / "version-lock.yaml").read_text(encoding="utf-8"))

            report = qa_root / "results" / "completion.json"
            report.parent.mkdir(parents=True, exist_ok=True)
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
                "completion_ok": True,
                "status": "verified",
                "errors": [],
            }), encoding="utf-8")
            code, result = self.invoke(
                "api-test", "scripts", "version-complete",
                "--qa-root", str(qa_root),
                "--completion-report", str(report),
            )
            self.assertEqual(0, code, result)
            self.assertIn("status: current", (qa_root / "contracts" / "version-lock.yaml").read_text(encoding="utf-8"))

    def test_missing_project_lock_is_a_gate_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            code, result = self.invoke("e2e", "source-status", "--project", temporary)
            self.assertEqual(8, code)
            self.assertEqual("GATE_FAILED", result["error"]["code"])
            self.assertEqual("e2e.source-status", result["command"])

    def test_domain_help_does_not_require_a_project_lock(self) -> None:
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = console_main(["e2e", "--help"])
        self.assertEqual(0, code)
        self.assertIn("source-status", stream.getvalue())

    def test_invalid_e2e_pytest_argument_is_an_argument_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            code, result = self.invoke("e2e", "init", "--project", temporary)
            self.assertEqual(0, code, result)
            code, result = self.invoke("e2e", "run", "--project", temporary, "--definitely-invalid")
            self.assertEqual(2, code)
            self.assertEqual("INVALID_ARGUMENT", result["error"]["code"])
            self.assertEqual("", result["error"]["details_path"])

    def test_doctor_fails_when_a_required_check_is_unhealthy(self) -> None:
        checks = {
            "checks": {
                "python": {"ok": True},
                "git": {"ok": False},
                "bruno": {"ok": False, "required_for": "api-test.run"},
            }
        }
        with patch("dev_ai.cli.diagnose", return_value=(checks, False)):
            code, result = self.invoke("doctor")
        self.assertEqual(6, code)
        self.assertEqual("EXTERNAL_UNAVAILABLE", result["error"]["code"])


if __name__ == "__main__":
    unittest.main()
