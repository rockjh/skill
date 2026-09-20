from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from dev_ai.cli import console_main
from dev_ai.core.schema import get_schema, validate_schema
from dev_ai.domains.business_flow.discovery import scan


class BusinessFlowContractTests(unittest.TestCase):
    def invoke(self, *arguments: str) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = console_main([*arguments, "--json"])
        return code, json.loads(output.getvalue())

    def project(self, root: Path) -> None:
        (root / "app.py").write_text(
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            "@app.get('/resources')\n"
            "def list_resources():\n"
            "    raise BusinessError('RESOURCE_NOT_FOUND', 'missing')\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
        subprocess.run(["git", "add", "app.py"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=root, check=True)

    def initialize_and_confirm(self, root: Path) -> Path:
        code, result = self.invoke("business-flow", "init", "--project", str(root))
        self.assertEqual(0, code, result)
        code, result = self.invoke("business-flow", "discover", "--project", str(root))
        self.assertEqual(0, code, result)
        module_map = root / "docs" / "business-flow" / "business-flow-modules.json"
        document = json.loads(module_map.read_text(encoding="utf-8"))
        document["confirmed"] = True
        for module in document["modules"]:
            module["rationale"] = "入口围绕同一业务对象、路径和处理能力划分。"
        module_map.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        return module_map

    def test_registration_schema_and_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.project(root)
            code, result = self.invoke("schema", "business-flow.generate")
            self.assertEqual(0, code)
            self.assertEqual("2", result["data"]["schema_version"])
            self.assertIn("--commit", result["data"]["options"])
            code, result = self.invoke("schema", "business-flow.discover")
            self.assertEqual(0, code)
            self.assertIn("--commit", result["data"]["options"])
            code, result = self.invoke("schema", "business-flow.module-map")
            self.assertEqual(0, code)
            self.assertIn("confirmed", result["data"]["properties"])
            code, result = self.invoke("schema", "business-flow.report")
            self.assertEqual(0, code)
            self.assertEqual("2", result["data"]["schema_version"])

            code, result = self.invoke("business-flow", "init", "--project", str(root))
            self.assertEqual(0, code, result)
            code, result = self.invoke("business-flow", "discover", "--project", str(root))
            self.assertEqual(0, code, result)
            code, result = self.invoke("business-flow", "generate", "--project", str(root))
            self.assertEqual(8, code, result)
            self.initialize_and_confirm(root)
            code, result = self.invoke("business-flow", "generate", "--project", str(root))
            self.assertEqual(0, code, result)
            report = root / "docs" / "business-flow" / "business-flow-report.json"
            document = root / "docs" / "business-flow" / "00-resources.md"
            self.assertTrue(report.is_file())
            self.assertTrue(document.is_file())
            self.assertEqual(1, json.loads(report.read_text(encoding="utf-8"))["entry_count"])
            text = document.read_text(encoding="utf-8")
            self.assertIn("RESOURCE_NOT_FOUND", text)
            self.assertIn("sequenceDiagram", text)
            self.assertIn("autonumber", text)
            self.assertIn("raise BusinessError", text)
            self.assertNotIn("else 成功", text)
            self.assertIn("结果代码中未确认", text)

            code, result = self.invoke("business-flow", "check", "--project", str(root))
            self.assertEqual(0, code, result)
            self.assertEqual(0, len(result["data"]["coverage"]["missing_entries"]))
            docs = root / "docs" / "business-flow"
            for name, scope in (
                ("business-flow-discovery.json", "business-flow.discovery"),
                ("business-flow-modules.json", "business-flow.module-map"),
                ("business-flow-index.json", "business-flow.index"),
                ("business-flow-report.json", "business-flow.report"),
            ):
                value = json.loads((docs / name).read_text(encoding="utf-8"))
                self.assertEqual([], validate_schema(get_schema(scope), value), name)
                self.assertTrue(value["source_fingerprint"])

    def test_missing_lock_is_a_gate_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.project(root)
            code, result = self.invoke("business-flow", "generate", "--project", str(root))
            self.assertEqual(8, code)
            self.assertEqual("GATE_FAILED", result["error"]["code"])

    def test_non_business_git_change_only_updates_document_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.project(root)
            self.initialize_and_confirm(root)
            self.invoke("business-flow", "generate", "--project", str(root))
            docs = root / "docs" / "business-flow"
            document = docs / "00-resources.md"
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "record generated docs"], cwd=root, check=True)
            self.invoke("business-flow", "generate", "--project", str(root))
            before = document.read_text(encoding="utf-8").splitlines()
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "record current version"], cwd=root, check=True)
            (root / "README.md").write_text("documentation only\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "docs-only change"], cwd=root, check=True)
            code, result = self.invoke("business-flow", "update", "--project", str(root))
            self.assertEqual(0, code, result)
            after = document.read_text(encoding="utf-8").splitlines()
            strip_version = lambda lines: [line for line in lines if not line.startswith("> 生效 Git 版本：")]
            self.assertEqual(strip_version(before), strip_version(after))

    def test_unreferenced_source_change_only_updates_document_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.project(root)
            self.initialize_and_confirm(root)
            self.invoke("business-flow", "generate", "--project", str(root))
            docs = root / "docs" / "business-flow"
            document = docs / "00-resources.md"
            before = document.read_text(encoding="utf-8").splitlines()
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "record generated docs"], cwd=root, check=True)

            (root / "unrelated.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "unrelated.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "unrelated source change"], cwd=root, check=True)
            self.initialize_and_confirm(root)
            code, result = self.invoke("business-flow", "update", "--project", str(root))
            self.assertEqual(0, code, result)
            after = document.read_text(encoding="utf-8").splitlines()
            strip_version = lambda lines: [line for line in lines if not line.startswith("> 生效 Git 版本：")]
            self.assertEqual(strip_version(before), strip_version(after))
            report = json.loads((docs / "business-flow-report.json").read_text(encoding="utf-8"))
            self.assertEqual("version_only", report["comparison"])

    def test_unresolved_call_requires_evidence_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.project(root)
            path = root / "app.py"
            path.write_text(path.read_text(encoding="utf-8").replace(
                "    raise BusinessError", "    unresolved_domain_call()\n    raise BusinessError"
            ), encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "add unresolved call"], cwd=root, check=True)
            module_map = self.initialize_and_confirm(root)
            code, result = self.invoke("business-flow", "generate", "--project", str(root))
            self.assertEqual(8, code, result)
            self.assertEqual("GATE_FAILED", result["error"]["code"])
            discovery = json.loads((module_map.parent / "business-flow-discovery.json").read_text(encoding="utf-8"))
            document = json.loads(module_map.read_text(encoding="utf-8"))
            document["resolutions"] = [{
                "finding": discovery["unresolved"][0],
                "resolution": "调用由运行框架提供，人工检查确认不会主动抛出业务错误码。",
                "evidence": ["app.py:5"],
            }]
            module_map.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
            code, result = self.invoke("business-flow", "generate", "--project", str(root))
            self.assertEqual(0, code, result)

    def test_check_reads_markdown_and_rejects_missing_document(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.project(root)
            self.initialize_and_confirm(root)
            self.invoke("business-flow", "generate", "--project", str(root))
            (root / "docs" / "business-flow" / "00-resources.md").unlink()
            code, result = self.invoke("business-flow", "check", "--project", str(root))
            self.assertEqual(8, code, result)
            self.assertIn("00-resources.md", result["error"]["message"])

    def test_caught_error_is_not_documented_and_markdown_is_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.project(root)
            (root / "app.py").write_text(
                "from fastapi import FastAPI\n"
                "app = FastAPI()\n"
                "def load():\n"
                "    try:\n"
                "        raise BusinessError('CAUGHT_ERROR', 'token=TOPSECRET')\n"
                "    except BusinessError:\n"
                "        return []\n"
                "@app.get('/resources')\n"
                "def list_resources():\n"
                "    load()\n"
                "    raise BusinessError('VISIBLE_ERROR', 'token=TOPSECRET')\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "app.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "caught and visible errors"], cwd=root, check=True)
            self.initialize_and_confirm(root)
            code, result = self.invoke("business-flow", "generate", "--project", str(root))
            self.assertEqual(0, code, result)
            text = (root / "docs" / "business-flow" / "00-resources.md").read_text(encoding="utf-8")
            self.assertIn("VISIBLE_ERROR", text)
            self.assertNotIn("CAUGHT_ERROR", text)
            self.assertNotIn("TOPSECRET", text)
            self.assertIn("[REDACTED]", text)

    def test_dirty_business_source_forces_business_update(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.project(root)
            self.initialize_and_confirm(root)
            self.invoke("business-flow", "generate", "--project", str(root))
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "baseline docs"], cwd=root, check=True)
            app = root / "app.py"
            app.write_text(app.read_text(encoding="utf-8").replace("RESOURCE_NOT_FOUND", "RESOURCE_CHANGED"), encoding="utf-8")
            code, result = self.invoke("business-flow", "discover", "--project", str(root))
            self.assertEqual(0, code, result)
            module_map = root / "docs" / "business-flow" / "business-flow-modules.json"
            document = json.loads(module_map.read_text(encoding="utf-8"))
            document["confirmed"] = True
            for module in document["modules"]:
                module["rationale"] = "入口围绕同一业务对象、路径和处理能力划分。"
            module_map.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
            code, result = self.invoke("business-flow", "update", "--project", str(root))
            self.assertEqual(0, code, result)
            report = json.loads((root / "docs" / "business-flow" / "business-flow-report.json").read_text(encoding="utf-8"))
            self.assertEqual("business_changed", report["comparison"])
            self.assertTrue(report["business_changed_documents"])

    def test_stale_confirmed_module_map_is_rejected_after_source_edit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.project(root)
            self.initialize_and_confirm(root)
            app = root / "app.py"
            app.write_text(app.read_text(encoding="utf-8").replace("RESOURCE_NOT_FOUND", "RESOURCE_CHANGED"), encoding="utf-8")
            code, result = self.invoke("business-flow", "generate", "--project", str(root))
            self.assertEqual(8, code, result)
            self.assertIn("source fingerprint is stale", result["error"]["message"])

    def test_check_requires_each_error_condition_in_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.project(root)
            self.initialize_and_confirm(root)
            self.invoke("business-flow", "generate", "--project", str(root))
            document = root / "docs" / "business-flow" / "00-resources.md"
            text = document.read_text(encoding="utf-8")
            document.write_text(text.replace("raise BusinessError('RESOURCE_NOT_FOUND', 'missing')", ""), encoding="utf-8")
            code, result = self.invoke("business-flow", "check", "--project", str(root))
            self.assertEqual(8, code, result)
            self.assertIn("markdown_missing_error_evidence", result["error"]["message"])

    def test_discovery_covers_protocol_graphql_jobs_messages_and_cli_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "service.proto").write_text(
                "service Resource {\n  rpc Allocate (Request) returns (Reply);\n}\n",
                encoding="utf-8",
            )
            (root / "schema.graphql").write_text(
                "type Query {\n  resource(id: ID!): Resource\n}\n",
                encoding="utf-8",
            )
            (root / "Jobs.java").write_text(
                "class Jobs {\n"
                "  @XxlJob(\"inventory-check\")\n"
                "  public void inventory() { throw new BizException(\"NO_STOCK\"); }\n"
                "  @KafkaListener(topics = {\"created\", \"updated\"})\n"
                "  public void consume() { return; }\n"
                "}\n",
                encoding="utf-8",
            )
            (root / "ResourceController.kt").write_text(
                "@Controller(\"/resource\")\n"
                "class ResourceController {\n"
                "  @Get(\"/one\")\n"
                "  fun one(): String { return \"ok\" }\n"
                "}\n",
                encoding="utf-8",
            )
            (root / "commands.py").write_text("def reconcile():\n    return 0\n", encoding="utf-8")
            (root / "pyproject.toml").write_text(
                "[project]\nname='sample'\nversion='1'\n[project.scripts]\nreconcile='commands:reconcile'\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "entry matrix"], cwd=root, check=True)

            result = scan(root)
            identifiers = {entry.identifier for entry in result.entries}
            self.assertTrue({
                "Resource/Allocate", "Query/resource", "inventory-check", "created", "updated", "reconcile",
                "GET /resource/one",
            }.issubset(identifiers))
            self.assertIn("NO_STOCK", {code for entry in result.entries for code in entry.error_codes()})
            self.assertTrue(result.source_fingerprint)

    def test_domain_and_subcommand_help_are_statically_registered(self) -> None:
        for arguments, expected in (
            (["business-flow", "--help"], "discover"),
            (["business-flow", "discover", "--help"], "--commit"),
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                code = console_main(arguments)
            self.assertEqual(0, code)
            self.assertIn(expected, output.getvalue())


if __name__ == "__main__":
    unittest.main()
