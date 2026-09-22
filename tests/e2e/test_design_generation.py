from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from dltk.schema import get_schema, validate_schema
from dltk.e2e_contracts import (
    _generation_artifact_errors,
    generate_artifacts,
    map_design_to_protocol,
    parse_design_documents,
    parse_protocol_documents,
)


class DesignGenerationTests(unittest.TestCase):
    def test_design_rule_keeps_business_evidence_separate_from_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            design = root / "flow.md"
            design.write_text(
                "# Flow\nParticipant: A\nParticipant: B\nPOST /orders\nRule ID: ORDER_CREATE\n"
                "State: COMPLETED\nAssertion: order.status = COMPLETED\n",
                encoding="utf-8",
            )
            protocol = root / "openapi.json"
            protocol.write_text(json.dumps({"openapi": "3.0.0", "paths": {"/orders": {"post": {"responses": {"202": {}}}}}}), encoding="utf-8")
            parsed_design = parse_design_documents([design])
            parsed_protocol = parse_protocol_documents([protocol])
            self.assertEqual([], map_design_to_protocol(parsed_design, parsed_protocol))
            self.assertEqual("design", parsed_design["source"])
            self.assertEqual("protocol", parsed_protocol["source"])
            self.assertEqual("COMPLETED", parsed_design["rules"][0]["states"][0])

    def test_generation_stops_without_reviewed_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value_path = root / "config" / "value-resolution.yaml"
            value_path.parent.mkdir()
            value_path.write_text("sentinel: keep\n", encoding="utf-8")
            legacy = root / "source-rules.yaml"
            legacy.write_text("legacy: true\n", encoding="utf-8")
            result, errors = generate_artifacts(root)
            self.assertTrue(errors)
            self.assertIn("design documents are required", errors[0])
            self.assertEqual([], result["design"]["files"])
            self.assertEqual("sentinel: keep\n", value_path.read_text(encoding="utf-8"))
            self.assertTrue(legacy.is_file())
            self.assertFalse((root / "discovery" / "design-rules.yaml").exists())

    def test_missing_generation_artifacts_always_block_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            errors = _generation_artifact_errors(Path(temporary), {})
            self.assertTrue(any("generation-artifact-required" in error for error in errors))

    def test_openapi_body_and_response_constraints_are_extracted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            contract = Path(temporary) / "openapi.json"
            contract.write_text(json.dumps({
                "openapi": "3.0.0",
                "paths": {"/orders": {"post": {
                    "operationId": "createOrder",
                    "requestBody": {"required": True, "content": {"application/json": {"schema": {
                        "type": "object", "required": ["amount"], "properties": {
                            "amount": {"type": "integer", "minimum": 1, "maximum": 100},
                        },
                    }}}},
                    "responses": {"201": {"description": "created", "content": {"application/json": {"schema": {
                        "type": "object", "required": ["status"], "properties": {
                            "status": {"type": "string", "enum": ["COMPLETED"]},
                        },
                    }}}}},
                }}},
            }), encoding="utf-8")
            operation = parse_protocol_documents([contract])["operations"][0]
            self.assertEqual({"path": "amount", "required": True, "type": "integer", "minimum": 1, "maximum": 100}, operation["request_fields"][0])
            self.assertEqual("status", operation["response_fields"][0]["path"])
            self.assertEqual(["COMPLETED"], operation["response_fields"][0]["enum"])

    def test_swagger2_body_and_response_schema_are_extracted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            contract = Path(temporary) / "swagger.json"
            document = {
                "swagger": "2.0",
                "paths": {"/orders": {"post": {
                    "operationId": "createOrder",
                    "parameters": [{"name": "body", "in": "body", "required": True, "schema": {
                        "type": "object", "required": ["amount"], "properties": {"amount": {"type": "integer", "minimum": 1}},
                    }}],
                    "responses": {"201": {"schema": {
                        "type": "object", "properties": {"status": {"type": "string", "enum": ["COMPLETED"]}},
                    }}},
                }}}
            }
            contract.write_text(json.dumps(document), encoding="utf-8")
            operation = parse_protocol_documents([contract])["operations"][0]
            self.assertTrue(operation["request_body"]["required"])
            self.assertEqual("amount", operation["request_fields"][0]["path"])
            self.assertEqual("status", operation["response_fields"][0]["path"])

    def test_graphql_arguments_are_required_protocol_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            contract = Path(temporary) / "schema.graphql"
            contract.write_text("type Mutation { createOrder(id: ID!, amount: Int!): Order! }\ntype Order { status: String! }\n", encoding="utf-8")
            operation = parse_protocol_documents([contract])["operations"][0]
            self.assertEqual({"id", "amount"}, {item["path"] for item in operation["request_fields"]})
            self.assertTrue(all(item["required"] for item in operation["request_fields"]))

    def test_asyncapi_proto_and_graphql_operations_are_structured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            asyncapi = root / "asyncapi.yaml"
            asyncapi.write_text(yaml.safe_dump({
                "asyncapi": "2.6.0",
                "channels": {"orders.completed": {"subscribe": {
                    "operationId": "consumeOrderCompleted",
                    "message": {"name": "OrderCompleted", "payload": {
                        "type": "object", "required": ["orderId"],
                        "properties": {"orderId": {"type": "string", "format": "uuid"}},
                    }},
                }}},
            }), encoding="utf-8")
            proto = root / "jobs.proto"
            proto.write_text(
                "message TriggerRequest { string job_id = 1; }\n"
                "message TriggerReply { string status = 1; }\n"
                "service Jobs { rpc Trigger (TriggerRequest) returns (TriggerReply); }\n",
                encoding="utf-8",
            )
            graphql = root / "schema.graphql"
            graphql.write_text("type Mutation { createOrder(id: ID!): Order! }\n", encoding="utf-8")

            parsed = parse_protocol_documents([asyncapi, proto, graphql])
            self.assertEqual([], parsed["errors"])
            by_id = {item["id"]: item for item in parsed["operations"]}
            self.assertEqual("orderId", by_id["consumeOrderCompleted"]["message_fields"][0]["path"])
            self.assertEqual("job_id", by_id["Jobs.Trigger"]["request_fields"][0]["name"])
            self.assertEqual("mutation", by_id["createOrder"]["operation_type"])
            self.assertTrue(by_id["createOrder"]["arguments"][0]["required"])

    def test_protocol_required_request_fields_are_checked_against_scenario_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scenario = root / "scenarios" / "订单"
            scenario.mkdir(parents=True)
            (root / "config" / "environments").mkdir(parents=True)
            (root / "config" / "config.yaml").write_text("active_environment: test\n", encoding="utf-8")
            (root / "config" / "environments" / "test.yaml").write_text("services: {}\ncomponents: {}\n", encoding="utf-8")
            (root / "discovery").mkdir()
            (root / "discovery" / "protocol-rules.yaml").write_text(yaml.safe_dump({
                "source": "protocol",
                "operations": [{"id": "createOrder", "kind": "http", "method": "POST", "path": "/orders", "parameters": [], "request_fields": [{"path": "amount", "required": True}], "response_fields": []}],
            }, allow_unicode=True), encoding="utf-8")
            (scenario / "业务数据.json").write_text(json.dumps({"test": {"order": {"name": "missing-amount"}}}), encoding="utf-8")
            definition = {
                "meta": {"status": "pending_environment"},
                "steps": [{"id": "create", "protocol_ref": "createOrder", "data_ref": "业务数据.json#/order"}],
            }
            errors = []
            from dltk.e2e_contracts import _scenario_artifact_errors
            errors.extend(_scenario_artifact_errors(root, scenario, definition))
            self.assertTrue(any("protocol-required-field" in error for error in errors))

    def test_protocol_response_assertion_must_name_a_contract_field(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scenario = root / "scenarios" / "order"
            scenario.mkdir(parents=True)
            (root / "config" / "environments").mkdir(parents=True)
            (root / "config" / "config.yaml").write_text("active_environment: test\n", encoding="utf-8")
            (root / "config" / "environments" / "test.yaml").write_text("services: {}\ncomponents: {}\n", encoding="utf-8")
            (root / "discovery").mkdir()
            (root / "discovery" / "protocol-rules.yaml").write_text(yaml.safe_dump({
                "source": "protocol",
                "operations": [{"id": "createOrder", "kind": "http", "method": "POST", "path": "/orders", "parameters": [], "request_fields": [], "response_fields": [{"path": "status", "required": True, "enum": ["COMPLETED"]}]}],
            }, allow_unicode=True), encoding="utf-8")
            (scenario / "业务数据.json").write_text(json.dumps({"test": {"order": {"amount": 1}}}, ensure_ascii=False), encoding="utf-8")
            definition = {
                "meta": {"status": "pending_environment"},
                "steps": [{"id": "create", "protocol_ref": "createOrder", "data_ref": "业务数据.json#/order", "expect": ["missing=COMPLETED"]}],
            }
            from dltk.e2e_contracts import _scenario_artifact_errors
            errors = _scenario_artifact_errors(root, scenario, definition)
            self.assertTrue(any("protocol-response-field" in error for error in errors))

    def test_unused_formal_operation_does_not_require_a_design_rule(self) -> None:
        design = {"rules": [{"id": "ORDER", "method": "POST", "path": "/orders", "protocol_refs": []}], "errors": []}
        protocol = {"operations": [
            {"id": "create", "kind": "http", "method": "POST", "path": "/orders"},
            {"id": "health", "kind": "http", "method": "GET", "path": "/health"},
        ], "errors": []}
        self.assertEqual([], map_design_to_protocol(design, protocol))
        self.assertEqual(["create"], design["rules"][0]["protocol_refs"])

    def test_successful_generation_preserves_support_values_and_detects_design_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            discovery = root / "discovery"
            config = root / "config"
            discovery.mkdir()
            config.mkdir()
            (discovery / "workspace.yaml").write_text(yaml.safe_dump({
                "inventory": {"repositories": []},
                "topology": {"nodes": [{"id": "orders"}, {"id": "inventory"}]},
                "configuration": {"services": []},
            }, allow_unicode=True), encoding="utf-8")
            values = {
                "version": 1,
                "source": "support-only",
                "values": [{"id": "fixture", "source": "fixture", "target": "request.customer", "value_ref": "fixture:customer", "evidence": []}],
                "business_expectations": [],
            }
            (config / "value-resolution.yaml").write_text(yaml.safe_dump(values, allow_unicode=True), encoding="utf-8")
            design = root / "flow.md"
            design.write_text(
                "# Create order\nRule ID: ORDER_CREATE\nParticipant: orders\nParticipant: inventory\n"
                "POST /orders\nBusiness flow: create order\nState: COMPLETED\nAssertion: status=COMPLETED\n",
                encoding="utf-8",
            )
            protocol = root / "openapi.json"
            protocol.write_text(json.dumps({
                "openapi": "3.0.0",
                "paths": {"/orders": {"post": {"operationId": "createOrder", "responses": {"201": {"description": "created"}}}}},
            }), encoding="utf-8")

            result, errors = generate_artifacts(root, design_files=[design], openapi_files=[protocol])
            self.assertEqual([], errors)
            self.assertTrue(result["artifacts"])
            persisted = yaml.safe_load((config / "value-resolution.yaml").read_text(encoding="utf-8"))
            self.assertEqual(values, persisted)
            for scope, relative in (
                ("e2e.design-rules", "discovery/design-rules.yaml"),
                ("e2e.protocol-rules", "discovery/protocol-rules.yaml"),
                ("e2e.logic", "discovery/logic.yaml"),
                ("e2e.scenario-plan", "discovery/scenario-plan.yaml"),
                ("e2e.value-resolution", "config/value-resolution.yaml"),
                ("e2e.version-lock", "discovery/version-lock.yaml"),
            ):
                document = yaml.safe_load((root / relative).read_text(encoding="utf-8"))
                self.assertEqual([], validate_schema(get_schema(scope)["document"], document), scope)
            self.assertEqual([], _generation_artifact_errors(root, {}))

            design.write_text(design.read_text(encoding="utf-8").replace("COMPLETED", "FAILED", 1), encoding="utf-8")
            drift = _generation_artifact_errors(root, {})
            self.assertTrue(any("version-lock-stale" in error and "document changed" in error for error in drift))

    def test_chinese_design_markers_are_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            design = Path(temporary) / "流程.md"
            design.write_text(
                "# 订单流程\n参与方：订单服务\n参与方：库存服务\n"
                "规则 ID：ORDER_CREATE\n业务流程：创建订单并扣减库存\n"
                "前置条件：用户已登录\n状态流转：CREATED -> COMPLETED\n"
                "断言：订单状态为 COMPLETED\n副作用：发布订单完成事件\n"
                "幂等规则：相同幂等键不得重复扣款\n重试规则：网络超时最多重试一次\n"
                "并发规则：同一订单串行处理\n",
                encoding="utf-8",
            )
            parsed = parse_design_documents([design])
            self.assertEqual([], parsed["errors"])
            rule = parsed["rules"][0]
            self.assertEqual("ORDER_CREATE", rule["id"])
            self.assertEqual(["订单服务", "库存服务"], rule["participants"])
            self.assertEqual(["用户已登录"], rule["preconditions"])
            self.assertEqual(["相同幂等键不得重复扣款"], rule["idempotency"])
            self.assertEqual(["网络超时最多重试一次"], rule["retries"])
            self.assertEqual(["同一订单串行处理"], rule["concurrency"])
            self.assertFalse(rule["manual_confirmation"])

    def test_logic_artifact_rejects_observed_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "discovery").mkdir()
            (root / "discovery" / "design-rules.yaml").write_text(
                "source: design\nrules:\n  - id: RULE_1\n", encoding="utf-8"
            )
            (root / "discovery" / "logic.yaml").write_text(
                "source: observed\nlogic:\n  - source: observed\n", encoding="utf-8"
            )
            errors = _generation_artifact_errors(root, {})
            self.assertTrue(any("logic-source" in error for error in errors))

    def test_business_expectation_must_match_design_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            discovery = root / "discovery"
            discovery.mkdir()
            (discovery / "design-rules.yaml").write_text(
                "source: design\nrules:\n  - id: RULE_1\n    assertions: [status=COMPLETED]\n",
                encoding="utf-8",
            )
            (discovery / "protocol-rules.yaml").write_text(
                "source: protocol\noperations:\n  - id: create\n",
                encoding="utf-8",
            )
            errors = _generation_artifact_errors(
                root,
                {
                    "meta": {"name": "order"},
                    "steps": [{
                        "id": "CREATE",
                        "control": "public_api",
                        "design_rule_id": "RULE_1",
                        "protocol_ref": "create",
                        "side_effect": "write",
                        "expect": ["status=PAID"],
                    }],
                },
            )
            self.assertTrue(any("expectation-design-trace" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
