---
name: python-e2e-test-generator
description: Design, generate, and incrementally maintain independent black-box Python/pytest business E2E scenarios across services, APIs, Kafka, databases, Redis, and EMQ. Use when tests must be derived from service source, stay collectable without runtime environment values, and keep concise Chinese scenario-owned contracts; do not use for exhaustive single-endpoint coverage.
---

# Python E2E Test Generator

Build an independent E2E project that behaves as an external consumer of deployed services. Inspect application repositories read-only to recover public entrypoints, business rules, messages, persistence, jobs, configuration, and observable completion signals. Never modify application code to make generated tests pass.

## Core workflow

1. Read the E2E project's `AGENTS.md`, `pyproject.toml`, configuration, helpers, fixtures, and validation commands.
2. Before writing assertions, keep a temporary trace from user expectation to source entrypoint, request/response model, state change, correlation key, observable evidence, and cleanup. Inspect the relevant application source and retain concise source anchors in the scenario definition.
3. Create or update the scenario contract, business data, diagrams, complete collectable pytest code, and execution scripts. Missing URLs, credentials, VINs, ICCIDs, brokers, or database values must not defer code generation.
4. Run non-destructive preflight immediately before runtime side effects. Missing runtime values produce `pending_environment` and a clear preflight failure; never `skip`, `xfail`, or a passing no-op.
5. Run environment-independent checker/shared-logic tests and `pytest --collect-only`, then the static and source-version checks, focused business tests when an environment exists, and the full suite through the generated scripts.
6. Before regenerating an existing scenario, apply [references/version-sync-policy.md](references/version-sync-policy.md) and change only source-affected artifacts.

If the user asks only for analysis or planning, stop after the requested contract/design artifacts. An explicit generation or implementation request authorizes creating the complete test project after the contract is established.

Treat the user's expected business semantics as an input contract. Never rewrite an expectation merely to match the current implementation; report the discrepancy. Use `contract_blocked` when a required interface, test-control ability, request/response or message contract, correlation rule, or expected outcome cannot be confirmed. Use `pending_environment` only when addresses, credentials, or environment-specific test data are missing.

## Scenario ownership

Keep technical directories and reusable module names in English. Each `scenarios/<中文业务名称>/` directory owns one business journey and contains:

```text
场景定义.yaml
业务数据.json
业务流程图.md
test_<中文业务名称>.py
```

Add `自动化测试流程图.md` only when test orchestration materially differs from the business flow. Add `步骤.py`, `断言.py`, or `清理.py` only when splitting them keeps the pytest entrypoint materially easier to read. Do not create `场景说明.md`, `版本变更记录.md`, empty placeholders, or a global scenario manifest.

Keep the stable scenario ID only in `场景定义.yaml` and exactly one pytest marker. Keep purpose, preconditions, key steps, and outcomes in the short introduction before the business diagram. Keep source version data in the definition's `source` section and rely on Git history for change history.

Read [references/e2e-workflow.md](references/e2e-workflow.md) for the required schema, YAML comments, validation gates, and result reporting. Read [references/scenario-artifact-policy.md](references/scenario-artifact-policy.md) before creating or changing scenario artifacts.

## Project structure and code boundaries

Use this structure, omitting unused modules:

```text
mno-e2e/
  pyproject.toml
  AGENTS.md
  E2E_PLAN.md
  config/
    runtime.yaml
    environments/
      local.yaml
      <environment>.yaml
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
      业务数据.json
      业务流程图.md
      test_<中文业务名称>.py
  tests/
    test_check_scenarios.py
    test_shared_logic.py
  scripts/
    check_scenarios.py
    check_source_versions.py
    run_all.sh
    run_all.bat
    run_<中文场景名称>.sh
    run_<中文场景名称>.bat
```

- Put environment-isolated scenario values and exact environment placeholders in `业务数据.json`, keyed first by environment name.
- Put reusable payload construction in `common/builders/`; do not keep long payloads in tests.
- Put read-only SQL and row mapping in `common/repositories/`; do not write SQL in tests.
- Put reusable HTTP, fulfillment, reminder, and cross-system assertions in `common/assertions/`.
- Keep reusable transport and observers in `common/clients/`, `common/integrations/`, and `common/fixtures/`.
- Keep scenario-only actions, mappings, assertions, and cleanup beside that scenario.
- Keep `test_*.py` as orchestration: preflight, actions, assertions, and guaranteed cleanup.

Name reusable modules by responsibility when they are needed, for example `real_name_builder.py`, `software_sale_builder.py`, `mobile_reminder_builder.py`, `unicom_reminder_builder.py`, `mno_traffic_repository.py`, `mno_operator_repository.py`, `http_assertions.py`, `reminder_assertions.py`, `fulfillment_assertions.py`, and `kafka_db_assertions.py`. Do not create unused placeholders.

Every generated Python module, class, and function has a concise Chinese docstring describing its business purpose. Add Chinese comments for business branches, bounded asynchronous waits, and cleanup; do not comment trivial assignments. Use builders, repositories, and assertion helpers rather than embedding payloads, SQL, or long assertion blocks in the test entrypoint.

Keep every execution and validation script in the single top-level `scripts/` directory. Generate one Shell/Bat pair per scenario plus the all-scenario pair, following [references/execution-script-policy.md](references/execution-script-policy.md).

## Configuration and integrations

`config/runtime.yaml` selects `active_environment` and owns shared technical defaults and capability switches. `config/environments/<environment>.yaml` owns that environment's service URLs, authentication, custom headers, credential references, TLS, topic prefixes, and middleware connection settings. Generate `local.yaml` as the documented placeholder-only example; never generate `example.yaml`. Secrets resolve through environment variables or an approved provider.

Validate that `active_environment` names an existing environment file. Deep-merge `runtime.yaml.defaults` with that environment mapping so an override cannot discard sibling defaults. Select the same root key from each scenario's `业务数据.json`, then resolve its `data_ref` relative to that selected object. Never merge or fall back across business-data environments. During preflight, recursively resolve only exact `${ENV_NAME}` placeholders and fail explicitly for missing or blank values. Do not resolve runtime values or connect to external services during pytest collection.

Each scenario declares its dependencies only in `integrations`: HTTP service names plus booleans for Kafka, MySQL, Redis, and EMQ. A `true` dependency is required; `false` means the scenario must not instantiate or preflight it. A disabled shared capability cannot be re-enabled by a scenario.

No connection, client construction, credential resolution, or health check may happen during import or pytest collection. Read [references/fixture-policy.md](references/fixture-policy.md) for lazy configuration, fixture scopes, isolation, and cleanup. Read [references/integration-policy.md](references/integration-policy.md) whenever a scenario crosses a service boundary or uses Kafka, MySQL, Redis, EMQ, or another observer. Read [references/request-signing-template.md](references/request-signing-template.md) only when source confirms Seres signing.

## Test invariants

- Call public business entrypoints and use approved read-only observers for downstream evidence.
- Build request payloads by explicitly mapping every source DTO or offline OpenAPI field, including nesting, units, enums, and time formats. Never pass a loaded JSON mapping directly into a request model or payload, even when names happen to match.
- For every side-effecting request, assert the transport and business response before polling downstream state.
- Correlate asynchronous and persistence evidence with source-confirmed keys such as `orderNo`, `taskId`, `orderId`, or `iccid`.
- Prove cancellation, unsubscription, fulfillment, and message publication through direct state, operation logs, messages, or persistence evidence. Resource existence alone does not prove a state transition.
- Subscribe Kafka or EMQ observers before the business action, and use bounded polling with useful last-state diagnostics instead of sleeps.
- When Kafka and a database are both evidence sources, prove publication and persistence independently, then compare every source-confirmed shared business field.
- Register idempotent cleanup immediately after resource creation. Prefer business APIs and restore mutable scenario configuration.
- If the test has already failed, record cleanup failures without replacing the original exception.
- Keep tests repeatable, independent of order, and isolated from pre-existing developer data.
- Redis is supporting evidence, never the sole primary business assertion when API, Kafka, or database evidence exists.
- Redis cleanup is limited to scenario-owned keys with an enforced test prefix. Never expose `flushdb` or clear business keys.
- EMQ publishers may publish only to configured test-topic prefixes; observers use a unique client ID per scenario and deterministic disconnect.

## Completion

Work is complete only when:

- `pytest --collect-only` succeeds without environment secrets or endpoints;
- environment-independent checker and shared-logic tests pass;
- `python scripts/check_scenarios.py` validates the complete nested schema and all static rules in [references/e2e-workflow.md](references/e2e-workflow.md);
- `scripts/run_all.sh` and `scripts/run_all.bat` run both checks before the complete suite, while each scenario script checks and runs only its scenario;
- all Mermaid scenario diagrams use `sequenceDiagram`; real branches use `alt`/`else`, and failure branches show source-confirmed business error codes;
- every Python module, class, and function has a Chinese docstring;
- test entrypoints contain no hardcoded VIN, ICCID, order number, plan/package ID, long payload, or SQL;
- Kafka, MySQL, Redis, and EMQ use matches each scenario's declaration;
- every scenario guarantees cleanup with `finally` or an equivalent pytest finalizer/context manager;
- real execution cannot pass preflight until every declared runtime dependency is valid;
- Kafka and database checkpoints are independently correlated and field-level reconciliation passes;
- `scripts/check_source_versions.py` reports a source-impact result per scenario without maintaining a second source of truth.

Do not claim success for gates that were not run. Report scenario artifact coverage, pytest collection, static validation, source synchronization, business-step entry rate, requirement-semantic coverage, business correctness, and code coverage separately. Use `N/A` when real business execution or coverage collection did not occur; collection or static success never substitutes for correctness. Server-side code coverage is not a mandatory black-box E2E gate.
