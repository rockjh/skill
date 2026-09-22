from __future__ import annotations

import json
import ast
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dev_ai.core.schema import E2E_CANDIDATE_KINDS
from dev_ai.domains.e2e.contracts import merge_protocol_operations, parse_design_documents, parse_protocol_documents
from dev_ai.domains.e2e.data import classify_data_requirements, generate_request_data
from dev_ai.domains.e2e.discovery import discover_protocols, read_only_protocol_probe
from dev_ai.domains.e2e.materialize import _candidate_matrix, _definition, _test_source


class GenericCapabilityTests(unittest.TestCase):
    def test_context_uses_design_vocabulary_and_tracks_observation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "flow.md"
            path.write_text(
                "# Register\nParticipant: alpha\nParticipant: beta\nEntity: Asset\n"
                "POST /assets\nState: ACCEPTED\nAssertion: status=ACCEPTED\n"
                "Correlation key: asset_key\nObservation: GET /assets/{id}\n",
                encoding="utf-8",
            )
            rule = parse_design_documents([path])["rules"][0]
            self.assertEqual("confirmed", rule["status"])
            self.assertEqual(["Asset"], rule["context"]["entities"])
            self.assertEqual(["/assets"], rule["context"]["entry_operations"])
            self.assertEqual(["asset_key"], rule["context"]["correlation_keys"])

    def test_protocol_merge_retains_conflict_and_sources(self) -> None:
        operations, conflicts = merge_protocol_operations([
            {"id": "one", "kind": "http", "method": "GET", "path": "/state", "source": {"file": "one"}},
            {"id": "two", "kind": "http", "method": "GET", "path": "/state", "status_codes": ["200"], "source": {"file": "two"}},
        ])
        self.assertEqual(["200"], operations[0]["status_codes"])
        self.assertEqual({"one", "two"}, {item["file"] for item in operations[0]["sources"]})
        self.assertEqual("needs_manual_confirmation", conflicts[0]["classification"])

    def test_natural_language_rules_keep_retry_idempotency_async_and_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "flow.md"
            path.write_text(
                "# Process\nParticipant: alpha\nParticipant: beta\nPOST /items\n"
                "The operation is idempotent and retries once after a timeout. "
                "It is asynchronous and recovery restores the prior state.\n"
                "Assertion: status=COMPLETED\n",
                encoding="utf-8",
            )
            rule = parse_design_documents([path])["rules"][0]
            self.assertTrue(rule["idempotency"])
            self.assertTrue(rule["retries"])
            self.assertTrue(rule["async_behavior"])
            self.assertTrue(rule["recovery"])

    def test_chinese_prose_extracts_async_business_facts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "flow.md"
            path.write_text(
                "# flow\n"
                "\u53c2\u4e0e\u65b9\uff1a\u670d\u52a1A\u3001\u670d\u52a1B\n"
                "POST /items\n"
                "\u8be5\u64cd\u4f5c\u5f02\u6b65\uff0c\u5931\u8d25\u540e\u91cd\u8bd5\u5e76\u4fdd\u6301\u5e42\u7b49\uff0c\u6062\u590d\u65f6\u56de\u6eda\u3002\n"
                "\u65ad\u8a00\uff1astatus=COMPLETED\n",
                encoding="utf-8",
            )
            rule = parse_design_documents([path])["rules"][0]
            self.assertEqual("confirmed", rule["status"])
            self.assertTrue(rule["async"])
            self.assertTrue(rule["retries"])
            self.assertTrue(rule["idempotency"])
            self.assertTrue(rule["recovery"])

    def test_request_data_is_generated_from_transport_constraints(self) -> None:
        data = generate_request_data({
            "request_fields": [
                {"path": "asset_id", "required": True, "type": "string", "format": "uuid"},
                {"path": "amount", "required": True, "type": "integer", "minimum": 2, "maximum": 2},
            ],
        }, namespace="case-a")
        self.assertEqual(2, data["amount"])
        self.assertRegex(data["asset_id"], r"^[0-9a-f-]{36}$")
        self.assertEqual("pending_environment", classify_data_requirements({"tenant": "${TENANT_ID}"})["status"])

    def test_nested_required_objects_are_constructed_from_protocol_fields(self) -> None:
        data = generate_request_data({
            "request_fields": [
                {"path": "entity", "required": True, "type": "object"},
                {"path": "entity.id", "required": True, "type": "string"},
            ],
        }, namespace="nested")
        self.assertIsInstance(data["entity"], dict)
        self.assertTrue(data["entity"]["id"])

    def test_runtime_protocol_probe_is_get_only_and_redacts_no_credentials(self) -> None:
        class Response:
            status = 200

            def read(self, _amount: int) -> bytes:
                return json.dumps({"openapi": "3.0.0", "paths": {}}).encode()

            def getheader(self, _name: str) -> str:
                return "application/json"

        class Connection:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def request(self, method: str, target: str, **_kwargs: object) -> None:
                self.calls.append((method, target))

            def getresponse(self) -> Response:
                return Response()

            def close(self) -> None:
                pass

        connection = Connection()
        with patch("dev_ai.domains.e2e.discovery.http.client.HTTPConnection", return_value=connection):
            result = read_only_protocol_probe(["http://127.0.0.1:8080/openapi.json"])
        self.assertEqual("runtime_url", result["sources"][0]["source_type"])
        self.assertTrue(all(method == "GET" for method, _ in connection.calls))

    def test_source_protocol_discovery_does_not_require_workspace_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "controller.py").write_text(
                '@router.post("/assets")\ndef create_asset():\n    pass\n',
                encoding="utf-8",
            )
            discovery = discover_protocols(root)
            parsed = parse_protocol_documents(discovery.files)
            self.assertEqual(["controller.py"], [path.name for path in discovery.files])
            self.assertEqual("source_definition", parsed["documents"][0]["source_type"])
            self.assertIn(
                ("POST", "/assets"),
                {(item.get("method"), item.get("path")) for item in parsed["operations"]},
            )

    def test_control_matrix_includes_read_only_paths_and_generated_async_polling(self) -> None:
        candidates = _candidate_matrix(
            source_ref="repo#anchor", component="service", control="observability", usable=True,
            side_effect="read", correlation="owned-key", cleanup="cleanup", action="observe",
            available_controls={"observability"},
        )
        self.assertIn("database_read", E2E_CANDIDATE_KINDS)
        self.assertIn("observability", {item["kind"] for item in candidates})
        self.assertEqual("usable", next(item["status"] for item in candidates if item["kind"] == "observability"))
        source = _test_source("async", {
            "meta": {"id": "ASYNC", "name": "async"},
            "steps": [{
                "id": "OBSERVE", "status": "executable", "protocol_ref": "observe",
                "expect": ["status=200"], "evidence": [],
                "async": {"timeout_seconds": 2, "interval_seconds": 0.1},
            }],
        })
        ast.parse(source)
        self.assertIn("poll_until", source)
        self.assertIn("step_guard", source)

    def test_mutating_http_scenario_requires_formal_cleanup_operation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "discovery").mkdir()
            (root / "config" / "environments").mkdir(parents=True)
            (root / "config" / "config.yaml").write_text("active_environment: test\n", encoding="utf-8")
            (root / "config" / "environments" / "test.yaml").write_text(
                "services: {svc: http://127.0.0.1:1}\n", encoding="utf-8"
            )
            (root / "discovery" / "workspace.yaml").write_text(
                "inventory:\n  repositories:\n    - id: repo\n      commit: " + "a" * 40 + "\n      build_files: [build]\n"
                "configuration:\n  services: [{id: svc}]\n  sources: [{evidence: [repo#Anchor]}]\n",
                encoding="utf-8",
            )
            rule = {
                "id": "CREATE_ITEM", "title": "Create item", "status": "confirmed",
                "participants": ["svc"], "assertions": ["status=CREATED"],
                "protocol_refs": ["create", "delete"],
            }
            protocols = {"operations": [
                {"id": "create", "kind": "http", "method": "POST", "path": "/items", "service": "svc"},
                {"id": "delete", "kind": "http", "method": "DELETE", "path": "/items/{id}", "service": "svc"},
            ]}
            _, definition, _, _ = _definition(root, rule, protocols, owner="test")
            self.assertEqual("protocol:delete", definition["cleanup"]["actions"][0])
            self.assertTrue(definition["isolation"]["owned_resources"])
            self.assertEqual("pending_environment", definition["meta"]["status"])

            rule["protocol_refs"] = ["create"]
            _, blocked, _, _ = _definition(root, rule, {"operations": protocols["operations"][:1]}, owner="test")
            self.assertEqual("contract_blocked", blocked["meta"]["status"])
            self.assertEqual("control_gap", blocked["steps"][0]["status"])


if __name__ == "__main__":
    unittest.main()
