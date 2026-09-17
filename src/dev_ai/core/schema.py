"""Scoped public command and domain contracts; no runtime plugin discovery."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any


API_TEST_SCHEMA_VERSION = "5.4"
E2E_GATE_SCHEMA_VERSION = "4"

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
    "api-test.init": {"options": {"--qa-root": "path"}},
    "api-test.generate": {
        "options": {
            "--qa-root": "path",
            "--openapi": "path",
            "--module-map": "path",
            "--incremental": "boolean",
            "--no-seed-cases": "boolean",
            "--source-root": "path[]",
            "--exception-type": "string",
            "--error-code-type": "string",
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
    "e2e.init": {"options": {"--project": "path"}},
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
        properties["safety"] = {
            "oneOf": [
                {"type": "null"},
                _object(
                    {
                        "authorization_required": {"const": True},
                        "target_environment": NONEMPTY_STRING,
                        "purpose": {
                            "type": "string",
                            "enum": ["preparation", "time_advance", "expiry_simulation", "state_trigger"],
                        },
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
                ),
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


SCENARIO_REQUIRED = (
    "meta",
    "generation",
    "readiness",
    "preconditions",
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
            }
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
                },
                ("id", "action", "control", "side_effect", "expect"),
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
                            "evidence": NONEMPTY_STRING_LIST,
                        }
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
            }
        ),
        "gates": _object(
            {
                "inventory_complete": {"const": True},
                "topology_complete": {"const": True},
                "configuration_complete": {"const": True},
                "runtime_probe_complete": {"const": True},
            }
        ),
    }
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
    }
)
ENDPOINT_EVENT_SCHEMA = _object(
    {
        "phase": {"type": "string", "enum": ["smoke", "business"]},
        "method": NONEMPTY_STRING,
        "target_ref": NONEMPTY_STRING,
        "status": {},
        "summary": {},
        "verified": {"const": True},
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
    }
)

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
                    "diagnostics": STRING_LIST,
                }
            ),
            "source_versions": _array(SOURCE_VERSION_SCHEMA),
            "scenarios": _array(REPORT_SCENARIO_SCHEMA),
            "evidence_diagnostics": STRING_LIST,
        }
    ),
}

CONTRACT_SCHEMAS = {
    "e2e.scenario": SCENARIO_SCHEMA,
    "e2e.workspace": WORKSPACE_SCHEMA,
    "e2e.config": CONFIG_SCHEMA,
    "e2e.report": REPORT_SCHEMA,
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
            }
        }
    if scope in CONTRACT_SCHEMAS:
        return deepcopy(CONTRACT_SCHEMAS[scope])
    if scope not in COMMAND_SCHEMAS:
        raise KeyError(scope)
    schema = deepcopy(COMMAND_SCHEMAS[scope])
    schema["schema_version"] = API_TEST_SCHEMA_VERSION if scope.startswith("api-test.") else E2E_GATE_SCHEMA_VERSION
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
