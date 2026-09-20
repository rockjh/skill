"""Scoped public command and domain contracts; no runtime plugin discovery."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any


API_TEST_SCHEMA_VERSION = "5.9"
BUSINESS_FLOW_SCHEMA_VERSION = "2"
E2E_GATE_SCHEMA_VERSION = "7"

E2E_SCENARIO_STATUSES = ("ready", "pending_environment", "contract_blocked")
E2E_GENERATION_MODES = ("delegated", "main_agent", "sequential_degraded")
E2E_CONTROL_NAMES = (
    "public_api",
    "test_or_admin_api",
    "mocks_and_faults",
    "dynamic_configuration",
    "scheduled_jobs",
    "messages",
    "database_read",
    "database_control",
    "observability",
)
E2E_CONTROL_STATUSES = ("usable", "unusable", "not_found", "not_applicable")
E2E_CANDIDATE_STATUSES = ("usable", "unusable", "not_found")
E2E_STEP_STATUSES = (
    "executable",
    "environment_missing",
    "authorization_missing",
    "control_gap",
    "product_gap",
    "runtime_failure",
)
E2E_CANDIDATE_KINDS = (
    "public_api",
    "test_or_admin_api",
    "database_control",
    "messages",
    "scheduled_jobs",
    "mocks_and_faults",
    "dynamic_configuration",
    "existing_test_data",
)
E2E_ORDERED_GATES = (
    "workspace_inventory",
    "dependency_topology",
    "initial_configuration",
    "runtime_probe",
    "control_matrix",
    "scenario_split",
    "scenario_ownership",
    "shared_integration",
)
E2E_ORDERED_GATE_SEQUENCE = (*E2E_ORDERED_GATES, "static")
E2E_RUN_STAGES = (
    *E2E_ORDERED_GATES,
    "static",
    "environment_tests",
    "source_versions",
    "collect",
    "read_only_smoke",
    "business",
    "restoration",
    "summary",
)


COMMAND_SCHEMAS: dict[str, dict[str, Any]] = {
    "api-test.init": {
        "options": {"--qa-root": "path", "--design-root": "path[]", "--design-file": "path[]"}
    },
    "api-test.generate": {
        "options": {
            "--qa-root": "path",
            "--openapi": "path",
            "--module-map": "path",
            "--incremental": "boolean",
            "--no-seed-cases": "boolean",
            "--source-root": "path[]",
            "--design-root": "path[]",
            "--design-file": "path[]",
            "--coverage-profile": ["contract-draft", "full-matrix"],
            "--base-url": "loopback-url[]",
            "--port": "integer[]",
            "--path": "string[]",
            "--timeout": "number",
        }
    },
    "api-test.materialize": {
        "options": {"--qa-root": "path", "--module": "string", "--check": "boolean"}
    },
    "api-test.check": {
        "options": {
            "--qa-root": "path",
            "--all": "boolean",
            "--module": "string",
            "--results": "path",
            "--preflight-results": "path",
            "--write-status": "boolean",
        },
        "one_of": ["--all", "--module"],
    },
    "api-test.preflight": {
        "options": {
            "--qa-root": "path",
            "--public-path": "string",
            "--public-method": "string",
            "--public-status": "http-status-range",
            "--require-public-route": "boolean",
            "--admin-path": "string",
            "--admin-method": "string",
            "--admin-status": "http-status-range",
            "--admin-code": "string",
            "--require-admin-baseline": "boolean",
            "--openapi": "path",
            "--expected-openapi-sha256": "sha256",
            "--execution-config": "path",
            "--env-file": "path",
            "--require-env": "string[]",
            "--fixture": "path[]",
            "--timeout": "number",
            "--bruno-cli": "path-or-command",
            "--cli-timeout": "number",
        }
    },
    "api-test.run": {
        "options": {
            "--qa-root": "path",
            "--module": "string",
            "--bruno-cli": "path-or-command",
            "--cli-timeout": "number",
            "--write-mock-data": "boolean",
            "--clean-mock-data": "boolean",
        }
    },
    "api-test.mock-data-generate": {
        "options": {
            "--qa-root": "path", "--module": "string[]", "--run-id": "string", "--allow-write": "boolean",
        }
    },
    "api-test.mock-data-clean": {
        "options": {
            "--qa-root": "path", "--module": "string[]", "--run-id": "string", "--allow-cleanup": "boolean",
        }
    },
    "api-test.reconcile": {
        "options": {
            "--qa-root": "path",
            "--all": "boolean",
            "--module": "string",
            "--results": "path",
            "--preflight-results": "path",
            "--write-status": "boolean",
        },
        "one_of": ["--all", "--module"],
        "required": ["--results", "--preflight-results"],
    },
    "api-test.aggregate": {"options": {"--qa-root": "path"}},
    "api-test.worker-start": {"options": {"--qa-root": "path", "--module": "string"}, "required": ["--module"]},
    "api-test.worker-check": {
        "options": {
            "--qa-root": "path",
            "--module": "string",
            "--stage": ["generation", "materialization", "pre-execution", "post-execution"],
        },
        "required": ["--module"],
    },
    "api-test.scripts": {
        "arguments": {"action": ["status", "version-init", "version-check", "version-complete"]},
        "options": {
            "--qa-root": "path",
            "--business-repo": "path",
            "--phase": ["before-generate", "before-execute"],
            "--completion-report": "path",
            "--tests-adapted": "boolean",
            "--rules": "path",
        },
    },
    "e2e.init": {
        "options": {
            "--project": "path", "--design-root": "path[]", "--design-file": "path[]",
            "--openapi-root": "path[]", "--openapi-file": "path[]",
        }
    },
    "e2e.discover": {
        "options": {
            "--project": "path", "--design-root": "path[]", "--design-file": "path[]",
            "--openapi-root": "path[]", "--openapi-file": "path[]",
        }
    },
    "e2e.generate": {
        "options": {
            "--project": "path", "--design-root": "path[]", "--design-file": "path[]",
            "--openapi-root": "path[]", "--openapi-file": "path[]",
        }
    },
    "e2e.check": {
        "options": {
            "--project": "path",
            "--gate": [*E2E_ORDERED_GATES, "discovery", "contracts", "static", "all"],
            "--scenario": "string",
        },
        "required": ["--gate"],
    },
    "e2e.source-status": {"options": {"--project": "path", "--scenario": "string"}},
    "e2e.run": {
        "options": {
            "--project": "path",
            "--scenario": "string",
            "--static-only": "boolean",
            "pytest_args": "restricted-string[]",
        }
    },
    "business-flow.init": {"options": {"--project": "path", "--docs-root": "path"}},
    "business-flow.discover": {
        "options": {"--project": "path", "--docs-root": "path", "--commit": "string"}
    },
    "business-flow.generate": {
        "options": {
            "--project": "path", "--docs-root": "path", "--module": "string", "--commit": "string",
        }
    },
    "business-flow.update": {
        "options": {
            "--project": "path", "--docs-root": "path", "--module": "string", "--commit": "string",
        }
    },
    "business-flow.check": {
        "options": {"--project": "path", "--docs-root": "path", "--module": "string", "--commit": "string"}
    },
}


def _object(properties: dict[str, Any], required: tuple[str, ...] | None = None, *, additional: bool = False) -> dict[str, Any]:
    return {
        "type": "object",
        "required": list(required or properties),
        "additionalProperties": additional,
        "properties": properties,
    }


def _array(items: dict[str, Any], *, minimum: int = 0, unique: bool = False) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "array", "items": items}
    if minimum:
        schema["minItems"] = minimum
    if unique:
        schema["uniqueItems"] = True
    return schema


NONEMPTY_STRING = {"type": "string", "minLength": 1}
STRING_LIST = _array(NONEMPTY_STRING, unique=True)
NONEMPTY_STRING_LIST = _array(NONEMPTY_STRING, minimum=1, unique=True)


def _control_schema(name: str) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "status": {"type": "string", "enum": list(E2E_CONTROL_STATUSES)},
        "assessment": NONEMPTY_STRING,
        "evidence": STRING_LIST,
        "planned_use": STRING_LIST,
    }
    if name == "database_control":
        operation_schema = _object(
            {
                "id": NONEMPTY_STRING,
                "depends_on": STRING_LIST,
                "consumer_source": NONEMPTY_STRING,
                "exact_selector": NONEMPTY_STRING,
                "expected_rows": {"const": 1},
                "snapshot": NONEMPTY_STRING,
                "mutation": NONEMPTY_STRING,
                "verification": NONEMPTY_STRING,
                "restoration": NONEMPTY_STRING,
                "restoration_verification": NONEMPTY_STRING,
            },
            (
                "id", "depends_on", "consumer_source", "exact_selector", "expected_rows",
                "snapshot", "mutation", "verification", "restoration", "restoration_verification",
            ),
        )
        common = {
            "authorization_required": {"const": True},
            "target_environment": NONEMPTY_STRING,
            "purpose": {
                "type": "string",
                "enum": ["preparation", "time_advance", "expiry_simulation", "state_trigger"],
            },
        }
        single = {
            **common,
            "consumer_source": NONEMPTY_STRING,
            "exact_selector": NONEMPTY_STRING,
            "expected_rows": {"const": 1},
            "snapshot": NONEMPTY_STRING,
            "mutation": NONEMPTY_STRING,
            "trigger": NONEMPTY_STRING,
            "verification": NONEMPTY_STRING,
            "restoration": NONEMPTY_STRING,
            "restoration_verification": NONEMPTY_STRING,
        }
        multi = {
            **common,
            "trigger": NONEMPTY_STRING,
            "verification": NONEMPTY_STRING,
            "operations": _array(operation_schema, minimum=1),
        }
        properties["safety"] = {
            "oneOf": [
                {"type": "null"},
                _object(single, (
                    "authorization_required", "target_environment", "purpose", "consumer_source", "exact_selector",
                    "expected_rows", "snapshot", "mutation", "trigger", "verification", "restoration",
                    "restoration_verification",
                )),
                _object(multi),
            ]
        }
    if name == "observability":
        properties.update(
            {
                "correlation_keys": STRING_LIST,
                "business_evidence": STRING_LIST,
                "recovery": STRING_LIST,
            }
        )
    return _object(properties)


def _candidate_schema() -> dict[str, Any]:
    return _object(
        {
            "kind": {"type": "string", "enum": list(E2E_CANDIDATE_KINDS)},
            "status": {"type": "string", "enum": list(E2E_CANDIDATE_STATUSES)},
            "component": NONEMPTY_STRING,
            "consumer_source": NONEMPTY_STRING,
            "control": NONEMPTY_STRING,
            "side_effect": {"type": "string", "enum": ["none", "read", "write"]},
            "trigger": NONEMPTY_STRING,
            "observation": NONEMPTY_STRING,
            "isolation": NONEMPTY_STRING,
            "cleanup": NONEMPTY_STRING,
            "evidence": NONEMPTY_STRING_LIST,
        }
    )


SCENARIO_REQUIRED = (
    "meta",
    "generation",
    "readiness",
    "preconditions",
    "constructability",
    "integrations",
    "controls",
    "isolation",
    "steps",
    "cleanup",
    "source",
)

SCENARIO_DOCUMENT_SCHEMA = _object(
    {
        "meta": _object(
            {
                "id": {"type": "string", "pattern": "^[A-Z][A-Z0-9_]+$"},
                "name": NONEMPTY_STRING,
                "status": {"type": "string", "enum": list(E2E_SCENARIO_STATUSES)},
                "actor": NONEMPTY_STRING,
                "participants": STRING_LIST,
            },
            ("id", "name", "status", "actor"),
        ),
        "generation": _object(
            {
                "mode": {"type": "string", "enum": list(E2E_GENERATION_MODES)},
                "owner": NONEMPTY_STRING,
                "write_scope": {"type": "string", "pattern": "^scenarios/[^/]+$"},
                "degradation_reason": {"type": ["string", "null"]},
            }
        ),
        "readiness": _object(
            {
                "source_contract": {"type": "string", "enum": ["confirmed", "blocked"]},
                "safe_control": {"type": "string", "enum": ["confirmed", "blocked"]},
                "runtime_configuration": {"type": "string", "enum": ["confirmed", "missing"]},
                "test_data": {"type": "string", "enum": ["confirmed", "missing"]},
                "blockers": STRING_LIST,
            }
        ),
        "constructability": _object(
            {
                "preconditions": _array(
                    _object(
                        {
                            "id": NONEMPTY_STRING,
                            "data_ownership": {"type": "string", "enum": ["test_owned", "environment_owned", "not_data"]},
                            "constructible": {"type": "boolean"},
                            "candidates": _array(_candidate_schema(), minimum=1),
                        },
                        ("id", "data_ownership", "constructible", "candidates"),
                    ),
                    unique=False,
                ),
                "steps": _array(
                    _object(
                        {
                            "step_id": NONEMPTY_STRING,
                            "candidates": _array(_candidate_schema(), minimum=1),
                        },
                        ("step_id", "candidates"),
                    ),
                    unique=False,
                ),
            },
            ("preconditions", "steps"),
        ),
        "preconditions": NONEMPTY_STRING_LIST,
        "integrations": _object(
            {
                "services": STRING_LIST,
                "components": _array(
                    _object(
                        {
                            "id": NONEMPTY_STRING,
                            "type": NONEMPTY_STRING,
                            "required": {"type": "boolean"},
                        }
                    )
                ),
            }
        ),
        "controls": _object(
            {
                **{name: _control_schema(name) for name in E2E_CONTROL_NAMES},
                "decision": _object(
                    {"safe_control_path": {"type": "boolean"}, "blockers": STRING_LIST}
                ),
            }
        ),
        "isolation": _object(
            {
                "namespace": NONEMPTY_STRING,
                "correlation_keys": NONEMPTY_STRING_LIST,
                "owned_resources": _array(
                    _object(
                        {
                            "kind": NONEMPTY_STRING,
                            "identity": NONEMPTY_STRING,
                            "cleanup": NONEMPTY_STRING,
                            "restore": NONEMPTY_STRING,
                            "verify": NONEMPTY_STRING,
                        }
                    )
                ),
                "mutable_controls": STRING_LIST,
                "serial_lock": {"type": "null"},
            }
        ),
        "steps": _array(
            _object(
                {
                    "id": NONEMPTY_STRING,
                    "action": NONEMPTY_STRING,
                    "control": {"type": "string", "enum": list(E2E_CONTROL_NAMES)},
                    "side_effect": {"type": "string", "enum": ["none", "read", "write"]},
                    "data_ref": {"type": "string", "pattern": "^业务数据\\.json#/"},
                    "expect": NONEMPTY_STRING_LIST,
                    "status": {"type": "string", "enum": list(E2E_STEP_STATUSES)},
                    "status_reason": NONEMPTY_STRING,
                    "evidence": STRING_LIST,
                    "design_rule_id": NONEMPTY_STRING,
                    "protocol_ref": NONEMPTY_STRING,
                    "phase": {"type": "string", "enum": ["request", "message_acceptance", "processing", "final_business", "side_effect"]},
                },
                ("id", "action", "control", "side_effect", "expect", "status", "status_reason", "evidence"),
            ),
            minimum=1,
        ),
        "cleanup": _object(
            {
                "strategy": NONEMPTY_STRING,
                "actions": NONEMPTY_STRING_LIST,
                "verifies": NONEMPTY_STRING_LIST,
            }
        ),
        "source": _array(
            _object(
                {
                    "repo": NONEMPTY_STRING,
                    "commit": {"type": "string", "pattern": "^[0-9a-fA-F]{40}$"},
                    "anchors": NONEMPTY_STRING_LIST,
                }
            ),
            minimum=1,
        ),
    },
    SCENARIO_REQUIRED,
)

SCENARIO_SCHEMA: dict[str, Any] = {
    "schema_version": E2E_GATE_SCHEMA_VERSION,
    "contract": "e2e.scenario",
    "path": "scenarios/<scenario>/场景定义.yaml",
    "required": list(SCENARIO_REQUIRED),
    "status": list(E2E_SCENARIO_STATUSES),
    "generation_mode": list(E2E_GENERATION_MODES),
    "control_names": list(E2E_CONTROL_NAMES),
    "control_status": list(E2E_CONTROL_STATUSES),
    "step_status": list(E2E_STEP_STATUSES),
    "candidate_kinds": list(E2E_CANDIDATE_KINDS),
    "candidate_status": list(E2E_CANDIDATE_STATUSES),
    "document": SCENARIO_DOCUMENT_SCHEMA,
    "required_sibling_artifacts": ["业务数据.json", "业务流程图.md", "test_<scenario>.py"],
    "machine_gate": "dev-ai e2e check --gate contracts",
}


CONFIG_VALUE_SCHEMA = _object(
    {
        "value": {},
        "effective_source": NONEMPTY_STRING,
        "resolution": {"type": "string", "enum": ["resolved", "unresolved"]},
        "source_key": NONEMPTY_STRING,
    }
)
CONFIG_REFERENCE_SCHEMA = _object(
    {
        "reference": {
            "type": "string",
            "pattern": "^(\\$\\{[A-Z][A-Z0-9_]*\\}|(?:environment|env|secret-store|vault|config|config-center|file-key|provider):.+)$",
        },
        "effective_source": NONEMPTY_STRING,
        "resolution": {"type": "string", "enum": ["resolved", "unresolved"]},
        "source_key": NONEMPTY_STRING,
    }
)
SEARCH_SCHEMA = _object(
    {"queries": NONEMPTY_STRING_LIST, "evidence": STRING_LIST, "conclusion": NONEMPTY_STRING}
)

WORKSPACE_DOCUMENT_SCHEMA = _object(
    {
        "schema_version": {"const": 1},
        "inventory": _object(
            {
                "roots": NONEMPTY_STRING_LIST,
                "repositories": _array(
                    _object(
                        {
                            "id": NONEMPTY_STRING,
                            "root": NONEMPTY_STRING,
                            "commit": {"type": "string", "pattern": "^[0-9a-fA-F]{40}$"},
                            "build_files": STRING_LIST,
                            "modules": _array(
                                _object(
                                    {
                                        "id": NONEMPTY_STRING,
                                        "path": NONEMPTY_STRING,
                                        "kind": {
                                            "type": "string",
                                            "enum": ["application", "sdk", "starter", "client", "facade", "library", "e2e", "other"],
                                        },
                                    }
                                ),
                                minimum=1,
                            ),
                        }
                    ),
                    minimum=1,
                ),
                "existing_e2e": STRING_LIST,
            }
        ),
        "topology": _object(
            {
                "nodes": _array(_object({"id": NONEMPTY_STRING, "relevant": {"type": "boolean"}})),
                "edges": _array(
                    _object(
                        {
                            "from": NONEMPTY_STRING,
                            "to": NONEMPTY_STRING,
                            "mechanism": {
                                "type": "string",
                                "enum": ["build", "http", "rpc", "message", "database", "cache", "job", "configuration", "embedded"],
                            },
                            "evidence": NONEMPTY_STRING_LIST,
                        }
                    )
                ),
                "searches": _object(
                    {name: SEARCH_SCHEMA for name in ("http_rpc", "messages", "database", "cache", "jobs", "configuration")}
                ),
            }
        ),
        "configuration": _object(
            {
                "sources": _array(
                    _object(
                        {
                            "id": NONEMPTY_STRING,
                            "owner": NONEMPTY_STRING,
                            "kind": {
                                "type": "string",
                                "enum": ["file", "profile", "environment", "config-center", "command-line", "local-override", "other"],
                            },
                            "location": NONEMPTY_STRING,
                            "profile": {"type": ["string", "null"]},
                            "overrides": STRING_LIST,
                            "evidence": NONEMPTY_STRING_LIST,
                        }
                    ),
                    minimum=1,
                ),
                "precedence": NONEMPTY_STRING_LIST,
                "services": _array(
                    _object(
                        {
                            "id": NONEMPTY_STRING,
                            "owner": NONEMPTY_STRING,
                            "port": CONFIG_VALUE_SCHEMA,
                            "context_path": CONFIG_VALUE_SCHEMA,
                            "health": CONFIG_VALUE_SCHEMA,
                            "openapi": CONFIG_VALUE_SCHEMA,
                        }
                    )
                ),
                "data_sources": _array(
                    _object(
                        {
                            "id": NONEMPTY_STRING,
                            "owner": NONEMPTY_STRING,
                            "type": NONEMPTY_STRING,
                            "name": CONFIG_VALUE_SCHEMA,
                            "connection_source": CONFIG_REFERENCE_SCHEMA,
                        }
                    )
                ),
                "middleware": _array(
                    _object(
                        {
                            "id": NONEMPTY_STRING,
                            "owner": NONEMPTY_STRING,
                            "capability": {"type": "string", "enum": ["messages", "cache"]},
                            "type": NONEMPTY_STRING,
                            "logical_name": CONFIG_VALUE_SCHEMA,
                            "connection_source": CONFIG_REFERENCE_SCHEMA,
                        }
                    )
                ),
                "controls": _array(
                    _object(
                        {
                            "id": NONEMPTY_STRING,
                            "owner": NONEMPTY_STRING,
                            "capability": {
                                "type": "string",
                                "enum": ["jobs", "test_or_admin_api", "mocks_and_faults", "dynamic_configuration", "scheduled_jobs"],
                            },
                            "type": NONEMPTY_STRING,
                            "source": NONEMPTY_STRING,
                        }
                    )
                ),
            }
        ),
        "runtime_probe": _object(
            {
                "requested": {"type": "boolean"},
                "outcome": {"type": "string", "enum": ["completed", "blocked", "not_requested"]},
                "blockers": STRING_LIST,
                "listeners": _array(
                    _object(
                        {
                            "id": NONEMPTY_STRING,
                            "host": NONEMPTY_STRING,
                            "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                            "protocol": NONEMPTY_STRING,
                            "evidence": NONEMPTY_STRING_LIST,
                        }
                    )
                ),
                "processes": _array(
                    _object(
                        {
                            "id": NONEMPTY_STRING,
                            "pid": {"type": "integer", "minimum": 1},
                            "command_reference": NONEMPTY_STRING,
                            "startup_arguments": STRING_LIST,
                            "working_directory": NONEMPTY_STRING,
                            "profile": {"type": ["string", "null"]},
                            "evidence": NONEMPTY_STRING_LIST,
                        },
                        ("id", "pid", "command_reference", "evidence"),
                    )
                ),
                "associations": _array(
                    _object({"process": NONEMPTY_STRING, "listener": NONEMPTY_STRING, "node": NONEMPTY_STRING})
                ),
                "read_only_smoke": _array(
                    _object(
                        {
                            "node": NONEMPTY_STRING,
                            "method": {"type": "string", "enum": ["GET", "HEAD", "READ"]},
                            "target_ref": NONEMPTY_STRING,
                            "result": NONEMPTY_STRING,
                        }
                    )
                ),
                "configuration_checks": _array(
                    _object(
                        {
                            "id": NONEMPTY_STRING,
                            "node": NONEMPTY_STRING,
                            "profile": {"type": ["string", "null"]},
                            "sources": NONEMPTY_STRING_LIST,
                            "effective": {"type": "string", "enum": ["confirmed", "unconfirmed"]},
                            "evidence": NONEMPTY_STRING_LIST,
                        }
                    )
                ),
            },
            ("requested", "outcome", "blockers", "listeners", "processes", "associations", "read_only_smoke"),
        ),
        "design": {"type": "object", "additionalProperties": True},
        "protocol": {"type": "object", "additionalProperties": True},
        "gates": _object(
            {
                "inventory_complete": {"const": True},
                "topology_complete": {"const": True},
                "configuration_complete": {"const": True},
                "runtime_probe_complete": {"const": True},
            }
        ),
    },
    ("schema_version", "inventory", "topology", "configuration", "runtime_probe", "gates"),
)

WORKSPACE_SCHEMA: dict[str, Any] = {
    "schema_version": E2E_GATE_SCHEMA_VERSION,
    "contract": "e2e.workspace",
    "path": "discovery/workspace.yaml",
    "document": WORKSPACE_DOCUMENT_SCHEMA,
    "machine_gate": "dev-ai e2e check --gate discovery",
}


CONFIG_SCHEMA: dict[str, Any] = {
    "schema_version": E2E_GATE_SCHEMA_VERSION,
    "contract": "e2e.config",
    "files": {
        "config/config.yaml": _object(
            {
                "active_environment": {"type": "string", "pattern": "^[a-z][a-z0-9_-]*$"},
                "defaults": _object(
                    {
                        "polling": _object(
                            {
                                "interval_seconds": {"type": "number", "exclusiveMinimum": 0},
                                "timeout_seconds": {"type": "number", "exclusiveMinimum": 0},
                            }
                        ),
                        "safety": _object(
                            {
                                "database_control_enabled": {"const": False},
                                "mutable_configuration_enabled": {"const": False},
                                "message_publish_enabled": {"const": False},
                            }
                        ),
                    },
                    ("safety",),
                ),
            }
        ),
        "config/environments/<active_environment>.yaml": _object(
            {
                "services": {"type": "object", "additionalProperties": {"type": "object"}},
                "components": {"type": "object", "additionalProperties": {"type": "object"}},
                "safety": _object(
                    {
                        "test_environment": {"type": "boolean"},
                        "side_effects_allowed": {"type": "boolean"},
                        "protected": {"type": "boolean"},
                    }
                ),
            },
            additional=True,
        ),
        "scenarios/<scenario>/业务数据.json": {
            "type": "object",
            "additionalProperties": {"type": "object"},
            "description": "Top-level keys are exact environment names; data_ref resolves only inside the active environment.",
        },
    },
    "runtime_authorization": {
        "environment": "E2E_CONTROL_ENVIRONMENT",
        "database_control": ["E2E_ENABLE_DATABASE_CONTROL", "E2E_CONTROL_AUTHORIZATION_REF"],
        "mutable_configuration": ["E2E_ENABLE_MUTABLE_CONFIGURATION", "E2E_MUTABLE_CONFIGURATION_AUTHORIZATION_REF"],
        "message_publish": ["E2E_ENABLE_MESSAGE_PUBLISH", "E2E_MESSAGE_PUBLISH_AUTHORIZATION_REF"],
        "other_dangerous_control": ["E2E_ENABLE_DANGEROUS_CONTROL", "E2E_DANGEROUS_CONTROL_AUTHORIZATION_REF"],
    },
    "machine_gate": "dev-ai e2e check --gate static",
}


CONTROL_EVENT_SCHEMA = _object(
    {
        "control_kind": {"type": "string", "enum": list(E2E_CONTROL_NAMES)},
        "action": NONEMPTY_STRING,
        "correlation_ref": NONEMPTY_STRING,
        "side_effect": {"type": "string", "enum": ["read", "write"]},
        "step_id": NONEMPTY_STRING,
        "protocol_ref": NONEMPTY_STRING,
        "protocol_path": {"type": ["string", "null"]},
    },
    ("control_kind", "action", "correlation_ref", "side_effect"),
)
ENDPOINT_EVENT_SCHEMA = _object(
    {
        "phase": {"type": "string", "enum": ["smoke", "business"]},
        "method": NONEMPTY_STRING,
        "target_ref": NONEMPTY_STRING,
        "status": {},
        "summary": {},
        "verified": {"const": True},
        "step_id": {"type": ["string", "null"]},
        "protocol_ref": {"type": ["string", "null"]},
        "protocol_path": {"type": ["string", "null"]},
    }
)
RESTORATION_EVENT_SCHEMA = _object(
    {
        "status": {"type": "string", "enum": ["passed", "failed"]},
        "resources": NONEMPTY_STRING_LIST,
        "error": NONEMPTY_STRING,
    },
    ("status", "resources"),
)
SOURCE_VERSION_SCHEMA = _object(
    {
        "scenario": NONEMPTY_STRING,
        "repository": NONEMPTY_STRING,
        "recorded_commit": NONEMPTY_STRING,
        "current_commit": {"type": ["string", "null"]},
        "dirty": {"type": "boolean"},
        "dirty_files": STRING_LIST,
        "changed_files": STRING_LIST,
        "unresolved_anchors": STRING_LIST,
        "outcome": {
            "type": "string",
            "enum": ["unchanged", "affected", "dirty_review_required", "full_rediscovery_required"],
        },
        "reason": NONEMPTY_STRING,
    }
)

REPORT_SCENARIO_SCHEMA = _object(
    {
        "name": NONEMPTY_STRING,
        "owner": {"type": ["string", "null"]},
        "generation_mode": {"type": ["string", "null"], "enum": [*E2E_GENERATION_MODES, None]},
        "degradation_reason": {"type": ["string", "null"]},
        "status": {"type": ["string", "null"], "enum": [*E2E_SCENARIO_STATUSES, None]},
        "participants": STRING_LIST,
        "integrations": _object({
            "services": STRING_LIST,
            "components": _array(_object({"id": NONEMPTY_STRING, "type": NONEMPTY_STRING, "required": {"type": "boolean"}})),
        }),
        "design_rule_ids": STRING_LIST,
        "protocol_refs": STRING_LIST,
        "planned_controls": _array({"type": "string", "enum": list(E2E_CONTROL_NAMES)}, unique=True),
        "used_controls": _array(CONTROL_EVENT_SCHEMA),
        "endpoint_calls": _array(ENDPOINT_EVENT_SCHEMA),
        "business_entered": {"type": "boolean"},
        "smoke": {"type": "string", "enum": ["N/A", "passed", "failed"]},
        "business": _object(
            {
                "status": {"type": "string", "enum": ["N/A", "passed", "failed"]},
                "exit_code": {"type": ["integer", "null"]},
                "reason": NONEMPTY_STRING,
            }
        ),
        "restoration": _array(RESTORATION_EVENT_SCHEMA),
        "step_results": _array(
            _object(
                {
                    "id": NONEMPTY_STRING,
                    "status": {"type": "string", "enum": list(E2E_STEP_STATUSES)},
                    "reason": NONEMPTY_STRING,
                    "evidence": STRING_LIST,
                    "design_rule_id": {"type": ["string", "null"]},
                    "protocol_ref": {"type": ["string", "null"]},
                    "phase": {"type": ["string", "null"], "enum": ["request", "message_acceptance", "processing", "final_business", "side_effect", None]},
                    "expected": STRING_LIST,
                    "actual": {},
                },
                ("id", "status", "reason", "evidence"),
            )
        ),
        "execution_rate": {"type": "number", "minimum": 0, "maximum": 1},
        "coverage_rate": {"type": "number", "minimum": 0, "maximum": 1},
        "business_correctness": {"type": "string", "enum": ["unknown", "passed", "failed"]},
        "classification": {"type": "string", "enum": ["static_complete", "executed", "partially_covered", "business_failure", "blocked"]},
    }
)

REPORT_DESIGN_SCHEMA = _object({
    "documents": _array(_object({
        "path": NONEMPTY_STRING, "sha256": NONEMPTY_STRING,
        "version": {"type": ["string", "null"]}, "sections": {"type": "integer", "minimum": 0},
    }, ("path", "sha256", "version"))),
    "rules": _array(_object({"id": NONEMPTY_STRING, "section": NONEMPTY_STRING, "source": {"type": "object", "additionalProperties": True}})),
    "manual_confirmation": STRING_LIST,
})
REPORT_PROTOCOL_SCHEMA = _object({
    "documents": _array(_object({
        "path": NONEMPTY_STRING, "sha256": NONEMPTY_STRING, "version": {"type": ["string", "null"]},
    })),
    "operations": _array(_object({
        "id": NONEMPTY_STRING, "kind": NONEMPTY_STRING, "method": {"type": ["string", "null"]},
        "path": {"type": ["string", "null"]}, "source": {"type": "object", "additionalProperties": True},
    })),
})
REPORT_COVERAGE_SCHEMA = _object({
    "design_to_scenario": _object({
        "rule_count": {"type": "integer", "minimum": 0}, "rule_ids": STRING_LIST,
        "scenario_references": {"type": "integer", "minimum": 0}, "unreferenced_rules": STRING_LIST,
    }),
    "protocol_to_call": _object({
        "operation_count": {"type": "integer", "minimum": 0}, "operation_ids": STRING_LIST,
        "calls": {"type": "integer", "minimum": 0}, "unused_operations": STRING_LIST,
    }),
})
REPORT_DIFFERENCE_SCHEMA = _object({
    "scenario": NONEMPTY_STRING, "step": NONEMPTY_STRING,
    "design_rule_id": {"type": ["string", "null"]}, "protocol_ref": {"type": ["string", "null"]},
    "expected": STRING_LIST, "actual": {}, "status": NONEMPTY_STRING, "reason": NONEMPTY_STRING,
})

REPORT_SCHEMA: dict[str, Any] = {
    "schema_version": E2E_GATE_SCHEMA_VERSION,
    "contract": "e2e.report",
    "path": "artifacts/e2e-run.json",
    "gate_order": list(E2E_RUN_STAGES),
    "stage_status": ["N/A", "passed", "failed"],
    "document": _object(
        {
            "schema_version": {"const": 1},
            "run_id": NONEMPTY_STRING,
            "started_at": NONEMPTY_STRING,
            "finished_at": {"type": ["string", "null"]},
            "selected_scenario": {"type": ["string", "null"]},
            "static_only": {"type": "boolean"},
            "stages": _object(
                {
                    name: _object(
                        {
                            "status": {"type": "string", "enum": ["N/A", "passed", "failed"]},
                            "exit_code": {"type": ["integer", "null"]},
                        }
                    )
                    for name in E2E_RUN_STAGES
                }
            ),
            "discovery": _object(
                {
                    "repositories": {
                        "anyOf": [
                            WORKSPACE_DOCUMENT_SCHEMA["properties"]["inventory"]["properties"]["repositories"],
                            {"type": "array", "maxItems": 0},
                        ]
                    },
                    "existing_e2e": STRING_LIST,
                    "topology": {
                        "anyOf": [
                            WORKSPACE_DOCUMENT_SCHEMA["properties"]["topology"],
                            {"type": "object", "maxProperties": 0},
                        ]
                    },
                    "configuration": {
                        "anyOf": [
                            WORKSPACE_DOCUMENT_SCHEMA["properties"]["configuration"],
                            {"type": "object", "maxProperties": 0},
                        ]
                    },
                    "runtime_probe": {
                        "anyOf": [
                            WORKSPACE_DOCUMENT_SCHEMA["properties"]["runtime_probe"],
                            {"type": "object", "maxProperties": 0},
                        ]
                    },
                    "active_runtime_probe": WORKSPACE_DOCUMENT_SCHEMA["properties"]["runtime_probe"],
                    "diagnostics": STRING_LIST,
                }
            ),
            "design": REPORT_DESIGN_SCHEMA,
            "protocol": REPORT_PROTOCOL_SCHEMA,
            "coverage": REPORT_COVERAGE_SCHEMA,
            "exclusions": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
            "support_only": _object({
                "repository_versions": _array(_object({"repo": NONEMPTY_STRING, "commit": NONEMPTY_STRING})),
                "configuration": {"type": "object", "additionalProperties": True},
                "value_resolution": {"type": "object", "additionalProperties": True},
            }),
            "differences": _array(REPORT_DIFFERENCE_SCHEMA),
            "source_versions": _array(SOURCE_VERSION_SCHEMA),
            "scenarios": _array(REPORT_SCENARIO_SCHEMA),
            "evidence_diagnostics": STRING_LIST,
        }
    ),
}

API_TEST_EVIDENCE_SCHEMA = _object({
    "source_kind": {"type": "string", "enum": ["design", "openapi", "config", "fixture", "support-source"]},
    "file": NONEMPTY_STRING,
    "symbol": NONEMPTY_STRING,
    "line": {"type": "integer", "minimum": 1},
    "endpoint_scope": NONEMPTY_STRING_LIST,
    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
})
API_TEST_DESIGN_DOCUMENT_SCHEMA = _object({
    "path": NONEMPTY_STRING,
    "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    "sections": {"type": "integer", "minimum": 0},
})
API_TEST_FLOW_STEP_SCHEMA = _object({
    "rule_id": NONEMPTY_STRING,
    "operation": NONEMPTY_STRING,
    "capture": NONEMPTY_STRING_LIST,
    "capture_paths": {
        "type": "object",
        "minProperties": 1,
        "additionalProperties": NONEMPTY_STRING,
    },
    "uses": NONEMPTY_STRING_LIST,
    "assert_absent": {},
}, ("rule_id", "operation"))
API_TEST_FLOW_SCHEMA = _object({
    "id": NONEMPTY_STRING,
    "mode": {"const": "sequential"},
    "source": {"const": "design"},
    "design_rule_ids": NONEMPTY_STRING_LIST,
    "steps": _array(API_TEST_FLOW_STEP_SCHEMA, minimum=2),
    "cleanup": NONEMPTY_STRING,
}, ("id", "mode", "source", "design_rule_ids", "steps"))
API_TEST_DESIGN_RULE_SCHEMA = _object(
    {
        "id": NONEMPTY_STRING,
        "method": NONEMPTY_STRING,
        "path": NONEMPTY_STRING,
        "title": NONEMPTY_STRING,
        "content": {"type": "string"},
        "scenario": {"type": "string", "enum": [
            "success", "authentication", "authorization", "query", "business_error", "safety",
        ]},
        "condition": {"type": "string"},
        "business_codes": {"type": "array", "items": {}},
        "http_statuses": _array({"type": "integer", "minimum": 100, "maximum": 599}),
        "states": STRING_LIST,
        "transitions": STRING_LIST,
        "side_effects": STRING_LIST,
        "idempotency": STRING_LIST,
        "retries": STRING_LIST,
        "concurrency": STRING_LIST,
        "external_failures": STRING_LIST,
        "async": {"type": "boolean"},
        "acceptance_statuses": STRING_LIST,
        "final_statuses": STRING_LIST,
        "assertions": _array(_object({"path": NONEMPTY_STRING, "equals": {}}, ("path", "equals"))),
        "request": {"type": ["object", "null"], "additionalProperties": True},
        "request_declared": {"type": "boolean"},
        "marker_errors": STRING_LIST,
        "section_line": {"type": "integer", "minimum": 1},
        "section_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "evidence": API_TEST_EVIDENCE_SCHEMA,
        "endpoint_id": {"type": ["string", "null"]},
        "manual_confirmation": _object({
            "required": {"const": True},
            "reasons": NONEMPTY_STRING_LIST,
        }),
    },
    (
        "id", "method", "path", "title", "content", "scenario", "condition", "business_codes",
        "http_statuses", "states", "transitions", "side_effects", "idempotency", "retries",
        "concurrency", "external_failures", "async", "acceptance_statuses", "final_statuses",
        "assertions", "request", "request_declared", "marker_errors", "section_line", "section_sha256",
        "evidence", "endpoint_id",
    ),
)
API_TEST_DESIGN_RULES_SCHEMA: dict[str, Any] = {
    "schema_version": API_TEST_SCHEMA_VERSION,
    "contract": "api-test.design-rules",
    "path": "qa/constraints/design-rules.yaml",
    "document": _object({
        "version": {"const": 1},
        "source": {"const": "design"},
        "documents": _array(API_TEST_DESIGN_DOCUMENT_SCHEMA),
        "rules": _array(API_TEST_DESIGN_RULE_SCHEMA),
        "flows": _array(API_TEST_FLOW_SCHEMA),
        "exclusions": _array({"type": "object", "additionalProperties": True}),
        "manual_confirmations": _array(_object({
            "rule_id": NONEMPTY_STRING,
            "reasons": NONEMPTY_STRING_LIST,
            "evidence": API_TEST_EVIDENCE_SCHEMA,
        })),
        "coverage": _object({
            "openapi_endpoints": {"type": "integer", "minimum": 0},
            "documented_endpoints": {"type": "integer", "minimum": 0},
            "excluded_endpoints": {"type": "integer", "minimum": 0},
        }),
    }, ("version", "source", "documents", "rules", "flows", "exclusions", "manual_confirmations", "coverage")),
}
API_TEST_LOGIC_SCHEMA: dict[str, Any] = {
    "schema_version": API_TEST_SCHEMA_VERSION,
    "contract": "api-test.logic",
    "path": "qa/contracts/modules/<module>/logic.yaml",
    "document": _object({
        "version": {"const": 1},
        "module": NONEMPTY_STRING,
        "swagger_tag": {"type": ["string", "null"]},
        "logic": _array(_object({
            "id": NONEMPTY_STRING,
            "status": {"const": "confirmed"},
            "source": {"const": "design"},
            "source_symbol": NONEMPTY_STRING,
            "condition": NONEMPTY_STRING,
            "expected_http_status": {"type": ["integer", "null"]},
            "expected_business_code": {},
            "expected_state": {},
            "transitions": STRING_LIST,
            "side_effects": STRING_LIST,
            "idempotency": STRING_LIST,
            "retries": STRING_LIST,
            "concurrency": STRING_LIST,
            "external_failures": STRING_LIST,
            "async": {"type": "boolean"},
            "acceptance_status": {"type": ["string", "null"]},
            "final_status": {"type": ["string", "null"]},
            "design_rule_id": NONEMPTY_STRING,
            "evidence": _array(API_TEST_EVIDENCE_SCHEMA, minimum=1),
            "case_ids": NONEMPTY_STRING_LIST,
        })),
    }, ("version", "module", "logic")),
}
API_TEST_VALUE_RESOLUTION_SCHEMA: dict[str, Any] = {
    "schema_version": API_TEST_SCHEMA_VERSION,
    "contract": "api-test.value-resolution",
    "path": "qa/contracts/modules/<module>/value-resolution.yaml",
    "document": _object({
        "version": {"const": 1},
        "module": NONEMPTY_STRING,
        "fields": _array(_object({
            "endpoint_id": NONEMPTY_STRING,
            "field_path": NONEMPTY_STRING,
            "status": {"const": "resolved"},
            "value": {},
            "value_source": {"type": "string", "enum": ["config", "fixture", "support-source"]},
            "evidence": API_TEST_EVIDENCE_SCHEMA,
        })),
    }),
}
API_TEST_VERSION_LOCK_SCHEMA: dict[str, Any] = {
    "schema_version": API_TEST_SCHEMA_VERSION,
    "contract": "api-test.version-lock",
    "path": "qa/contracts/version-lock.yaml",
    "document": _object({
        "version": {"const": 1},
        "status": NONEMPTY_STRING,
        "business": {"type": "object", "additionalProperties": True},
        "openapi": _object({
            "file": NONEMPTY_STRING,
            "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        }),
        "design": _object({
            "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "documents": _array(_object({
                "path": NONEMPTY_STRING,
                "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            })),
            "rule_count": {"type": "integer", "minimum": 0},
        }),
    }, ("version", "status", "business")),
}

E2E_INPUT_DOCUMENT_SCHEMA = _object({
    "path": NONEMPTY_STRING,
    "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    "version": {"type": ["string", "null"]},
    "sections": {"type": "integer", "minimum": 0},
}, ("path", "sha256", "version"))
E2E_EVIDENCE_SOURCE_SCHEMA = _object({
    "source_kind": {"type": "string", "enum": ["design", "protocol"]},
    "file": NONEMPTY_STRING,
    "section": NONEMPTY_STRING,
    "line": {"type": "integer", "minimum": 1},
    "operation": NONEMPTY_STRING,
}, ("source_kind", "file"))
E2E_DESIGN_RULE_SCHEMA = _object({
    "id": NONEMPTY_STRING,
    "title": NONEMPTY_STRING,
    "type": {"type": "string", "enum": ["cross_service", "business"]},
    "manual_confirmation": {"type": "boolean"},
    "method": {"type": ["string", "null"]},
    "path": {"type": ["string", "null"]},
    "event": {"type": ["string", "null"]},
    "task": {"type": ["string", "null"]},
    "calls": _array(_object({
        "kind": {"type": "string", "enum": ["http", "message", "task"]},
        "method": NONEMPTY_STRING,
        "path": NONEMPTY_STRING,
        "event": NONEMPTY_STRING,
        "task": NONEMPTY_STRING,
    }, ("kind",))),
    "participants": STRING_LIST,
    "states": STRING_LIST,
    "transitions": STRING_LIST,
    "acceptance_statuses": STRING_LIST,
    "final_statuses": STRING_LIST,
    "preconditions": STRING_LIST,
    "business_codes": STRING_LIST,
    "exceptions": STRING_LIST,
    "branches": STRING_LIST,
    "assertions": STRING_LIST,
    "final_result": STRING_LIST,
    "side_effects": STRING_LIST,
    "idempotency": STRING_LIST,
    "retries": STRING_LIST,
    "concurrency": STRING_LIST,
    "async_behavior": STRING_LIST,
    "cleanup": STRING_LIST,
    "recovery": STRING_LIST,
    "async": {"type": "boolean"},
    "section": NONEMPTY_STRING,
    "line": {"type": "integer", "minimum": 1},
    "section_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    "protocol_refs": STRING_LIST,
    "source": E2E_EVIDENCE_SOURCE_SCHEMA,
})
E2E_PROTOCOL_OPERATION_SCHEMA = _object({
    "id": NONEMPTY_STRING,
    "kind": {"type": "string", "enum": ["http", "message", "rpc", "graphql", "task"]},
    "method": NONEMPTY_STRING,
    "path": NONEMPTY_STRING,
    "status_codes": STRING_LIST,
    "parameters": _array({"type": "object", "additionalProperties": True}),
    "request_body": {"type": "object", "additionalProperties": True},
    "request_fields": _array({"type": "object", "additionalProperties": True}),
    "response_fields": _array({"type": "object", "additionalProperties": True}),
    "responses": {"type": "object", "additionalProperties": True},
    "channel": NONEMPTY_STRING,
    "direction": {"type": "string", "enum": ["publish", "subscribe"]},
    "event": NONEMPTY_STRING,
    "message_version": {"type": ["string", "null"]},
    "message_fields": _array({"type": "object", "additionalProperties": True}),
    "header_fields": _array({"type": "object", "additionalProperties": True}),
    "message_schema": {"type": "object", "additionalProperties": True},
    "service": NONEMPTY_STRING,
    "request_type": NONEMPTY_STRING,
    "response_type": NONEMPTY_STRING,
    "operation_type": {"type": "string", "enum": ["query", "mutation", "subscription"]},
    "arguments": _array({"type": "object", "additionalProperties": True}),
    "source": E2E_EVIDENCE_SOURCE_SCHEMA,
}, ("id", "kind", "source"))

E2E_DESIGN_RULES_SCHEMA: dict[str, Any] = {
    "schema_version": E2E_GATE_SCHEMA_VERSION,
    "contract": "e2e.design-rules",
    "path": "discovery/design-rules.yaml",
    "document": _object({
        "version": {"const": 1}, "source": {"const": "design"},
        "documents": _array(E2E_INPUT_DOCUMENT_SCHEMA, minimum=1),
        "rules": _array(E2E_DESIGN_RULE_SCHEMA, minimum=1),
        "errors": STRING_LIST,
    }),
}
E2E_PROTOCOL_RULES_SCHEMA: dict[str, Any] = {
    "schema_version": E2E_GATE_SCHEMA_VERSION,
    "contract": "e2e.protocol-rules",
    "path": "discovery/protocol-rules.yaml",
    "document": _object({
        "version": {"const": 1}, "source": {"const": "protocol"},
        "documents": _array(E2E_INPUT_DOCUMENT_SCHEMA, minimum=1),
        "operations": _array(E2E_PROTOCOL_OPERATION_SCHEMA, minimum=1),
        "errors": STRING_LIST,
    }),
}
E2E_LOGIC_ITEM_SCHEMA = _object({
    "id": NONEMPTY_STRING, "source": {"const": "design"}, "design_rule_id": NONEMPTY_STRING,
    "title": NONEMPTY_STRING, "participants": STRING_LIST, "protocol_refs": STRING_LIST,
    "preconditions": STRING_LIST, "states": STRING_LIST, "transitions": STRING_LIST,
    "branches": STRING_LIST, "exceptions": STRING_LIST, "assertions": STRING_LIST,
    "final_result": STRING_LIST, "side_effects": STRING_LIST, "idempotency": STRING_LIST,
    "retries": STRING_LIST, "concurrency": STRING_LIST, "async_behavior": STRING_LIST,
    "async": {"type": "boolean"}, "acceptance_status": {"type": ["string", "null"]},
    "final_status": {"type": ["string", "null"]}, "evidence": _array(E2E_EVIDENCE_SOURCE_SCHEMA, minimum=1),
})
E2E_LOGIC_SCHEMA: dict[str, Any] = {
    "schema_version": E2E_GATE_SCHEMA_VERSION,
    "contract": "e2e.logic",
    "path": "discovery/logic.yaml",
    "document": _object({
        "version": {"const": 1}, "source": {"const": "design"},
        "logic": _array(E2E_LOGIC_ITEM_SCHEMA, minimum=1),
    }),
}
E2E_VALUE_ITEM_SCHEMA = _object({
    "id": NONEMPTY_STRING,
    "source": {"type": "string", "enum": ["config", "fixture", "support-source", "runtime-preparation"]},
    "target": NONEMPTY_STRING,
    "value_ref": NONEMPTY_STRING,
    "evidence": STRING_LIST,
    "cleanup_ref": {"type": ["string", "null"]},
}, ("id", "source", "target", "value_ref"))
E2E_VALUE_RESOLUTION_SCHEMA: dict[str, Any] = {
    "schema_version": E2E_GATE_SCHEMA_VERSION,
    "contract": "e2e.value-resolution",
    "path": "config/value-resolution.yaml",
    "document": _object({
        "version": {"const": 1}, "source": {"const": "support-only"},
        "values": _array(E2E_VALUE_ITEM_SCHEMA),
        "business_expectations": {"type": "array", "maxItems": 0},
    }),
}
E2E_INPUT_SUMMARY_SCHEMA = _object({
    "documents": _array(E2E_INPUT_DOCUMENT_SCHEMA, minimum=1),
    "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    "rule_count": {"type": "integer", "minimum": 0},
    "operation_count": {"type": "integer", "minimum": 0},
}, ("documents", "sha256"))
E2E_VERSION_LOCK_SCHEMA: dict[str, Any] = {
    "schema_version": E2E_GATE_SCHEMA_VERSION,
    "contract": "e2e.version-lock",
    "path": "discovery/version-lock.yaml",
    "document": _object({
        "version": {"const": 1},
        "tool": _object({"name": {"const": "dev-ai"}, "version": NONEMPTY_STRING, "e2e_schema": {"const": E2E_GATE_SCHEMA_VERSION}}),
        "design": E2E_INPUT_SUMMARY_SCHEMA,
        "protocol": E2E_INPUT_SUMMARY_SCHEMA,
        "source": _array(_object({"repo": NONEMPTY_STRING, "commit": {"type": "string", "pattern": "^[0-9a-fA-F]{40}$"}}), unique=True),
        "support": _object({
            "workspace_sha256": {"type": ["string", "null"]},
            "value_resolution_sha256": {"type": ["string", "null"]},
        }),
        "scenario_generation": _object({"version": {"const": 1}, "generated_at": NONEMPTY_STRING}),
        "changes": _object({
            "design_changed": {"type": "boolean"}, "protocol_changed": {"type": "boolean"},
            "source_changed": {"type": "boolean"}, "support_changed": {"type": "boolean"},
            "scenario_data_changed": {"type": "boolean"}, "cleanup_changed": {"type": "boolean"},
        }),
        "scenarios": _object({"data_sha256": NONEMPTY_STRING, "cleanup_sha256": NONEMPTY_STRING}),
    }),
}
E2E_SCENARIO_PLAN_SCHEMA: dict[str, Any] = {
    "schema_version": E2E_GATE_SCHEMA_VERSION,
    "contract": "e2e.scenario-plan",
    "path": "discovery/scenario-plan.yaml",
    "document": _object({
        "version": {"const": 1}, "source": {"const": "design"},
        "scenarios": _array(_object({
            "id": NONEMPTY_STRING, "title": NONEMPTY_STRING, "participants": NONEMPTY_STRING_LIST,
            "design_rule_ids": NONEMPTY_STRING_LIST, "protocol_refs": NONEMPTY_STRING_LIST,
            "required_coverage": _object({
                "preconditions": STRING_LIST, "transitions": STRING_LIST, "branches": STRING_LIST,
                "exceptions": STRING_LIST, "final_result": STRING_LIST, "side_effects": STRING_LIST,
            }),
        }), minimum=1),
    }),
}

BUSINESS_FLOW_ERROR_SCHEMA = _object({
    "code": NONEMPTY_STRING, "condition": NONEMPTY_STRING, "source": NONEMPTY_STRING,
})
BUSINESS_FLOW_BEHAVIOR_SCHEMA = _object({
    "kind": NONEMPTY_STRING, "statement": NONEMPTY_STRING, "source": NONEMPTY_STRING,
})

BUSINESS_FLOW_DISCOVERY_SCHEMA: dict[str, Any] = _object(
    {
        "schema_version": {"const": 2},
        "source_fingerprint": NONEMPTY_STRING,
        "effective_git": _object({
            "commit": NONEMPTY_STRING, "branch": NONEMPTY_STRING, "workspace_dirty": {"type": "boolean"},
            "includes_uncommitted_changes": {"type": "boolean"},
        }),
        "languages": STRING_LIST,
        "frameworks": STRING_LIST,
        "source_files": STRING_LIST,
        "entries": _array(_object({
            "id": NONEMPTY_STRING, "type": NONEMPTY_STRING, "identifier": NONEMPTY_STRING,
            "handler": NONEMPTY_STRING, "source": NONEMPTY_STRING, "suggested_module": NONEMPTY_STRING,
            "non_business_candidate": {"type": "boolean"}, "core_capabilities": STRING_LIST,
            "errors": _array(BUSINESS_FLOW_ERROR_SCHEMA),
        })),
        "unresolved": STRING_LIST,
    }
)

BUSINESS_FLOW_MODULE_MAP_SCHEMA: dict[str, Any] = _object(
    {
        "schema_version": {"const": 2},
        "source_fingerprint": NONEMPTY_STRING,
        "effective_git": NONEMPTY_STRING,
        "confirmed": {"type": "boolean"},
        "resolutions": _array(_object({
            "finding": NONEMPTY_STRING, "resolution": NONEMPTY_STRING, "evidence": NONEMPTY_STRING_LIST,
        })),
        "entry_overrides": _array(_object({
            "id": NONEMPTY_STRING, "type": NONEMPTY_STRING, "identifier": NONEMPTY_STRING,
            "handler": NONEMPTY_STRING, "caller": NONEMPTY_STRING,
            "input_summary": NONEMPTY_STRING, "core_capabilities": STRING_LIST,
            "errors": _array(BUSINESS_FLOW_ERROR_SCHEMA), "behaviors": _array(BUSINESS_FLOW_BEHAVIOR_SCHEMA),
        }, ("id",))),
        "additional_entries": _array(_object({
            "id": NONEMPTY_STRING, "type": NONEMPTY_STRING, "identifier": NONEMPTY_STRING,
            "handler": NONEMPTY_STRING, "source": NONEMPTY_STRING, "caller": NONEMPTY_STRING,
            "input_summary": NONEMPTY_STRING, "core_capabilities": STRING_LIST,
            "errors": _array(BUSINESS_FLOW_ERROR_SCHEMA), "behaviors": _array(BUSINESS_FLOW_BEHAVIOR_SCHEMA),
        })),
        "exclusions": _array(_object({
            "candidate": NONEMPTY_STRING, "reason": NONEMPTY_STRING, "evidence": NONEMPTY_STRING_LIST,
        })),
        "modules": _array(_object({
            "name": NONEMPTY_STRING, "display_name": NONEMPTY_STRING, "rationale": NONEMPTY_STRING,
            "entry_ids": STRING_LIST,
        })),
    }
)

BUSINESS_FLOW_INDEX_SCHEMA: dict[str, Any] = _object(
    {
        "schema_version": {"const": 2},
        "source_fingerprint": NONEMPTY_STRING,
        "effective_git": _object({
            "commit": NONEMPTY_STRING,
            "branch": NONEMPTY_STRING,
            "workspace_dirty": {"type": "boolean"},
            "includes_uncommitted_changes": {"type": "boolean"},
        }),
        "comparison": NONEMPTY_STRING,
        "old_commit": {"type": ["string", "null"]},
        "languages": STRING_LIST,
        "frameworks": STRING_LIST,
        "modules": _array(_object({
            "name": NONEMPTY_STRING, "file": NONEMPTY_STRING, "rationale": NONEMPTY_STRING,
            "entry_ids": STRING_LIST,
        })),
        "entries": _array(_object({
            "id": NONEMPTY_STRING, "type": NONEMPTY_STRING, "identifier": NONEMPTY_STRING,
            "handler": NONEMPTY_STRING, "module": NONEMPTY_STRING, "source": NONEMPTY_STRING,
            "caller": NONEMPTY_STRING, "input_summary": NONEMPTY_STRING, "core_capabilities": STRING_LIST,
            "error_codes": STRING_LIST,
            "errors": _array(BUSINESS_FLOW_ERROR_SCHEMA),
            "behaviors": _array(BUSINESS_FLOW_BEHAVIOR_SCHEMA),
        })),
        "counts": _object({
            "entries": {"type": "integer", "minimum": 0},
            "modules": {"type": "integer", "minimum": 0},
            "error_codes": {"type": "integer", "minimum": 0},
            "by_type": {"type": "object", "additionalProperties": {"type": "integer", "minimum": 0}},
        }),
        "unresolved": STRING_LIST,
    }
)

BUSINESS_FLOW_REPORT_SCHEMA: dict[str, Any] = _object(
    {
        "schema_version": {"const": 2},
        "source_fingerprint": NONEMPTY_STRING,
        "effective_git": BUSINESS_FLOW_INDEX_SCHEMA["properties"]["effective_git"],
        "project": NONEMPTY_STRING,
        "scope": {"type": ["string", "null"]},
        "languages": STRING_LIST,
        "frameworks": STRING_LIST,
        "module_count": {"type": "integer", "minimum": 0},
        "document_count": {"type": "integer", "minimum": 0},
        "url_entry_count": {"type": "integer", "minimum": 0},
        "scheduled_task_count": {"type": "integer", "minimum": 0},
        "message_consumer_count": {"type": "integer", "minimum": 0},
        "other_entry_count": {"type": "integer", "minimum": 0},
        "active_error_code_count": {"type": "integer", "minimum": 0},
        "entry_count": {"type": "integer", "minimum": 0},
        "added_entries": STRING_LIST,
        "updated_entries": STRING_LIST,
        "deleted_entries": STRING_LIST,
        "version_only_documents": STRING_LIST,
        "business_changed_documents": STRING_LIST,
        "comparison": NONEMPTY_STRING,
        "comparison_error": {"type": ["string", "null"]},
        "exclusions": STRING_LIST,
        "unresolved": STRING_LIST,
        "coverage": _object({
            "code_entry_count": {"type": "integer", "minimum": 0},
            "documented_entry_count": {"type": "integer", "minimum": 0},
            "missing_entries": STRING_LIST,
            "stale_entries": STRING_LIST,
            "code_error_code_count": {"type": "integer", "minimum": 0},
            "documented_error_code_count": {"type": "integer", "minimum": 0},
            "missing_error_codes": STRING_LIST,
            "stale_error_codes": STRING_LIST,
            "missing_error_evidence": STRING_LIST,
            "stale_error_evidence": STRING_LIST,
            "unique_module_count": {"type": "integer", "minimum": 0},
            "markdown_document_count": {"type": "integer", "minimum": 0},
            "markdown_entry_count": {"type": "integer", "minimum": 0},
            "markdown_missing_documents": STRING_LIST,
            "markdown_missing_entries": STRING_LIST,
            "markdown_stale_entries": STRING_LIST,
            "markdown_missing_error_codes": STRING_LIST,
            "markdown_missing_error_evidence": STRING_LIST,
            "markdown_stale_error_evidence": STRING_LIST,
            "markdown_diagram_mismatches": STRING_LIST,
            "markdown_version_mismatches": STRING_LIST,
        }),
        "index_path": NONEMPTY_STRING,
    }
)

BUSINESS_FLOW_DISCOVERY_SCHEMA["schema_version"] = BUSINESS_FLOW_SCHEMA_VERSION
BUSINESS_FLOW_DISCOVERY_SCHEMA["contract"] = "business-flow.discovery"
BUSINESS_FLOW_MODULE_MAP_SCHEMA["schema_version"] = BUSINESS_FLOW_SCHEMA_VERSION
BUSINESS_FLOW_MODULE_MAP_SCHEMA["contract"] = "business-flow.module-map"
BUSINESS_FLOW_INDEX_SCHEMA["schema_version"] = BUSINESS_FLOW_SCHEMA_VERSION
BUSINESS_FLOW_INDEX_SCHEMA["contract"] = "business-flow.index"
BUSINESS_FLOW_REPORT_SCHEMA["schema_version"] = BUSINESS_FLOW_SCHEMA_VERSION
BUSINESS_FLOW_REPORT_SCHEMA["contract"] = "business-flow.report"

CONTRACT_SCHEMAS = {
    "api-test.design-rules": API_TEST_DESIGN_RULES_SCHEMA,
    "api-test.logic": API_TEST_LOGIC_SCHEMA,
    "api-test.value-resolution": API_TEST_VALUE_RESOLUTION_SCHEMA,
    "api-test.version-lock": API_TEST_VERSION_LOCK_SCHEMA,
    "business-flow.discovery": BUSINESS_FLOW_DISCOVERY_SCHEMA,
    "business-flow.module-map": BUSINESS_FLOW_MODULE_MAP_SCHEMA,
    "business-flow.index": BUSINESS_FLOW_INDEX_SCHEMA,
    "business-flow.report": BUSINESS_FLOW_REPORT_SCHEMA,
    "e2e.scenario": SCENARIO_SCHEMA,
    "e2e.workspace": WORKSPACE_SCHEMA,
    "e2e.config": CONFIG_SCHEMA,
    "e2e.report": REPORT_SCHEMA,
    "e2e.design-rules": E2E_DESIGN_RULES_SCHEMA,
    "e2e.protocol-rules": E2E_PROTOCOL_RULES_SCHEMA,
    "e2e.logic": E2E_LOGIC_SCHEMA,
    "e2e.scenario-plan": E2E_SCENARIO_PLAN_SCHEMA,
    "e2e.value-resolution": E2E_VALUE_RESOLUTION_SCHEMA,
    "e2e.version-lock": E2E_VERSION_LOCK_SCHEMA,
}


def get_schema(scope: str | None = None) -> dict[str, Any]:
    if scope is None:
        return {
            "domains": {
                "api-test": {
                    "schema_version": API_TEST_SCHEMA_VERSION,
                    "commands": [name.split(".", 1)[1] for name in COMMAND_SCHEMAS if name.startswith("api-test.")],
                },
                "e2e": {
                    "schema_version": E2E_GATE_SCHEMA_VERSION,
                    "commands": [name.split(".", 1)[1] for name in COMMAND_SCHEMAS if name.startswith("e2e.")],
                },
                "business-flow": {
                    "schema_version": BUSINESS_FLOW_SCHEMA_VERSION,
                    "commands": [name.split(".", 1)[1] for name in COMMAND_SCHEMAS if name.startswith("business-flow.")],
                },
            }
        }
    if scope in CONTRACT_SCHEMAS:
        return deepcopy(CONTRACT_SCHEMAS[scope])
    if scope not in COMMAND_SCHEMAS:
        raise KeyError(scope)
    schema = deepcopy(COMMAND_SCHEMAS[scope])
    if scope.startswith("api-test."):
        schema["schema_version"] = API_TEST_SCHEMA_VERSION
    elif scope.startswith("business-flow."):
        schema["schema_version"] = BUSINESS_FLOW_SCHEMA_VERSION
    else:
        schema["schema_version"] = E2E_GATE_SCHEMA_VERSION
    return schema


def validate_schema(schema: dict[str, Any], value: Any, path: str = "$") -> list[str]:
    """Validate the JSON Schema subset emitted by this module."""

    errors: list[str] = []
    if not schema:
        return errors
    if "anyOf" in schema:
        if not any(not validate_schema(branch, value, path) for branch in schema["anyOf"]):
            errors.append(f"{path}: does not match any allowed schema")
        return errors
    if "oneOf" in schema:
        matches = sum(not validate_schema(branch, value, path) for branch in schema["oneOf"])
        if matches != 1:
            errors.append(f"{path}: must match exactly one allowed schema")
        return errors

    expected = schema.get("type")
    expected_types = [expected] if isinstance(expected, str) else expected
    type_checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    if expected_types and not any(type_checks[name](value) for name in expected_types):
        return [f"{path}: expected {' or '.join(expected_types)}"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected constant {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: value is not in the allowed enum")

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{path}: missing required property {name}")
        for name, child in value.items():
            if name in properties:
                errors.extend(validate_schema(properties[name], child, f"{path}.{name}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected property {name}")
            elif isinstance(schema.get("additionalProperties"), dict):
                errors.extend(validate_schema(schema["additionalProperties"], child, f"{path}.{name}"))
        if len(value) > schema.get("maxProperties", len(value)):
            errors.append(f"{path}: too many properties")
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: too few items")
        if len(value) > schema.get("maxItems", len(value)):
            errors.append(f"{path}: too many items")
        if schema.get("uniqueItems") and any(item in value[:index] for index, item in enumerate(value)):
            errors.append(f"{path}: items must be unique")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(validate_schema(item_schema, item, f"{path}[{index}]"))
    elif isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: string is too short")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            errors.append(f"{path}: string does not match required pattern")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: number is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: number is above maximum")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: number must be greater than the exclusive minimum")
    return errors
