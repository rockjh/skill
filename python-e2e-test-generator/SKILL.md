---
name: python-e2e-test-generator
description: Design, generate, and incrementally maintain independent black-box Python/pytest business E2E scenarios across services, APIs, Kafka, databases, Redis, and EMQ. Use when tests must be derived from service source, stay collectable without runtime environment values, and keep concise Chinese scenario-owned contracts; do not use for exhaustive single-endpoint coverage.
---

# Python E2E Test Generator

Build an independent E2E project that behaves as an external consumer of deployed services. Inspect application repositories read-only to recover public entrypoints, business rules, messages, persistence, jobs, configuration, and observable completion signals. Never modify application code to make generated tests pass.

## Core workflow

1. Read the E2E project's `AGENTS.md`, `pyproject.toml`, configuration, helpers, fixtures, and validation commands.
2. Inspect the relevant application source and identify actors, preconditions, public actions, state transitions, correlation keys, observable evidence, cleanup, and concise source anchors.
3. Create or update the scenario contract, business data, diagrams, and complete collectable pytest code. Missing URLs, credentials, VINs, ICCIDs, brokers, or database values must not defer code generation.
4. Run non-destructive preflight immediately before runtime side effects. Missing runtime values produce `pending_environment` and a clear preflight failure; never `skip`, `xfail`, or a passing no-op.
5. Run `pytest --collect-only`, `python scripts/check_scenarios.py`, focused tests when an environment exists, and then the full suite.
6. Before regenerating an existing scenario, apply [references/version-sync-policy.md](references/version-sync-policy.md) and change only source-affected artifacts.

If the user asks only for analysis or planning, stop after the requested contract/design artifacts. An explicit generation or implementation request authorizes creating the complete test project after the contract is established.

Use `contract_blocked` only when source discovery cannot establish a required interface, schema, message contract, correlation rule, or expected business outcome. Environment unavailability is always `pending_environment`.

## Scenario ownership

Keep technical directories and reusable module names in English. Each `scenarios/<中文业务名称>/` directory owns one business journey and contains:

```text
场景定义.yaml
业务数据.yaml
业务流程图.md
test_<中文业务名称>.py
```

Add `自动化测试流程图.md` only when test orchestration materially differs from the business flow. Add `步骤.py`, `断言.py`, or `清理.py` only when splitting them keeps the pytest entrypoint materially easier to read. Do not create `场景说明.md`, `版本变更记录.md`, empty placeholders, or a global scenario manifest.

Keep the stable scenario ID only in `场景定义.yaml` and exactly one pytest marker. Keep purpose, preconditions, key steps, and outcomes in the short introduction before the business diagram. Keep source version data in the definition's `source` section and rely on Git history for change history.

Read [references/e2e-workflow.md](references/e2e-workflow.md) for the required schema and validation gates, and [references/scenario-artifact-policy.md](references/scenario-artifact-policy.md) before creating or changing scenario artifacts.

## Project structure and code boundaries

Use this structure, omitting unused modules:

```text
mno-e2e/
  pyproject.toml
  AGENTS.md
  E2E_PLAN.md
  config/
    common.yaml
    environments/
      example.yaml
      <profile>.yaml
  common/
    clients/
    builders/
    repositories/
    assertions/
    fixtures/
    integrations/
  scenarios/
    <中文业务名称>/
      场景定义.yaml
      业务数据.yaml
      业务流程图.md
      test_<中文业务名称>.py
  scripts/
    check_scenarios.py
    check_source_versions.py
```

- Put static scenario values and environment placeholders in `业务数据.yaml`.
- Put reusable payload construction in `common/builders/`; do not keep long payloads in tests.
- Put read-only SQL and row mapping in `common/repositories/`; do not write SQL in tests.
- Put reusable HTTP, fulfillment, reminder, and cross-system assertions in `common/assertions/`.
- Keep reusable transport and observers in `common/clients/`, `common/integrations/`, and `common/fixtures/`.
- Keep scenario-only actions, mappings, assertions, and cleanup beside that scenario.
- Keep `test_*.py` as orchestration: preflight, actions, assertions, and guaranteed cleanup.

Name reusable modules by responsibility when they are needed, for example `real_name_builder.py`, `software_sale_builder.py`, `mobile_reminder_builder.py`, `unicom_reminder_builder.py`, `mno_traffic_repository.py`, `mno_operator_repository.py`, `http_assertions.py`, `reminder_assertions.py`, `fulfillment_assertions.py`, and `kafka_db_assertions.py`. Do not create unused placeholders.

Every generated Python module, class, and function has a concise Chinese docstring describing its business purpose. Add Chinese comments for business branches, bounded asynchronous waits, and cleanup; do not comment trivial assignments. Use builders, repositories, and assertion helpers rather than embedding payloads, SQL, or long assertion blocks in the test entrypoint.

## Configuration and integrations

`config/common.yaml` owns shared technical behavior and capability switches. `config/environments/<profile>.yaml` owns endpoints, credential references, authentication, TLS, topic prefixes, and component connection settings. Secrets resolve through environment variables or an approved provider and never enter scenario files.

Each scenario declares its dependencies only in `integrations`: HTTP service names plus booleans for Kafka, MySQL, Redis, and EMQ. A `true` dependency is required; `false` means the scenario must not instantiate or preflight it. A disabled shared capability cannot be re-enabled by a scenario.

No connection, client construction, credential resolution, or health check may happen during import or pytest collection. Read [references/fixture-policy.md](references/fixture-policy.md) for lazy configuration, fixture scopes, isolation, and cleanup. Read [references/integration-policy.md](references/integration-policy.md) whenever a scenario crosses a service boundary or uses Kafka, MySQL, Redis, EMQ, or another observer. Read [references/request-signing-template.md](references/request-signing-template.md) only when source confirms Seres signing.

## Test invariants

- Call public business entrypoints and use approved read-only observers for downstream evidence.
- Assert application results, not only HTTP status codes.
- Correlate asynchronous and persistence evidence with source-confirmed keys such as `orderNo`, `taskId`, `orderId`, or `iccid`.
- Subscribe Kafka or EMQ observers before the business action, and use bounded polling with useful last-state diagnostics instead of sleeps.
- When Kafka and a database are both evidence sources, prove publication and persistence independently, then compare every source-confirmed shared business field.
- Register idempotent cleanup immediately after resource creation. Prefer business APIs and restore mutable scenario configuration.
- Keep tests repeatable, independent of order, and isolated from pre-existing developer data.
- Redis is supporting evidence, never the sole primary business assertion when API, Kafka, or database evidence exists.
- Redis cleanup is limited to scenario-owned keys with an enforced test prefix. Never expose `flushdb` or clear business keys.
- EMQ publishers may publish only to configured test-topic prefixes; observers use a unique client ID per scenario and deterministic disconnect.

## Completion

Work is complete only when:

- `pytest --collect-only` succeeds without environment secrets or endpoints;
- `python scripts/check_scenarios.py` validates the compact schema, required artifacts, stable ID placement, and optional diagram rules;
- all Mermaid scenario diagrams use `sequenceDiagram`; real branches use `alt`/`else`, and failure branches show source-confirmed business error codes;
- every Python module, class, and function has a Chinese docstring;
- test entrypoints contain no hardcoded VIN, ICCID, order number, plan/package ID, long payload, or SQL;
- Kafka, MySQL, Redis, and EMQ use matches each scenario's declaration;
- every scenario guarantees cleanup with `finally` or an equivalent pytest finalizer/context manager;
- real execution cannot pass preflight until every declared runtime dependency is valid;
- Kafka and database checkpoints are independently correlated and field-level reconciliation passes;
- `scripts/check_source_versions.py` reports a source-impact result per scenario without maintaining a second source of truth.

Do not claim success for gates that were not run. Report unavailable runtime verification separately from successful collection and static validation.
