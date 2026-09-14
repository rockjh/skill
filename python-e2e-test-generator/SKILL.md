---
name: python-e2e-test-generator
description: Design, generate, and incrementally maintain independent black-box Python/pytest business E2E scenarios across services, APIs, messages, and data stores. Use when tests must be derived from service source, remain collectable without runtime environment values, and keep Chinese scenario-owned artifacts with per-scenario Git impact tracking; do not use for exhaustive single-endpoint coverage.
---

# Python E2E Test Generator

Build an independent E2E project that behaves as an external consumer of the deployed system. Inspect business repositories read-only to reconstruct public entrypoints, service calls, messages, persistence, jobs, configuration, and observable completion signals. Never modify application repositories to make generated tests pass.

## Naming and ownership

Keep technical structure and configuration names in English:

- `config/`, `common/`, `scripts/`, `environments/`
- `common.yaml`, `example.yaml`, environment profile names, `pyproject.toml`, `conftest.py`
- reusable Python package/module identifiers

Use a clear Chinese business name for each directory under `scenarios/` and for scenario-owned business artifacts:

- `场景定义.yaml`
- `场景说明.md`
- `业务流程图.md`
- `自动化测试流程图.md`
- `版本变更记录.md`
- `test_<中文业务名称>.py`

Keep the stable scenario ID only in `场景定义.yaml` and a pytest marker. Do not put it in a directory name or maintain any global scenario manifest. `E2E_PLAN.md` may document project-wide strategy, commands, and constraints, but it must not duplicate a scenario inventory.

## Workflow

1. **Discover source and project conventions.** Read the E2E project's `AGENTS.md`, `pyproject.toml`, configuration, clients, fixtures, and commands. Inspect relevant application repositories, contracts, migrations, producers/consumers, jobs, and deployment configuration. Identify business actors, preconditions, state transitions, public inputs, correlation keys, observables, cleanup, environment contract, and source anchors.
2. **Write the scenario contract.** Create or update the scenario's own `场景定义.yaml`, explanation, and both diagrams. Record its stable ID, business definition, dependencies, checkpoints, cleanup, execution status, and `source_versions`. Do not create `scenarios.yaml`, `场景清单.yaml`, or an equivalent registry. Stop for review when the user asked only for planning; an explicit implementation/generation request authorizes continuing after the scenario contract is recorded.
3. **Generate collectable code.** Generate the complete pytest business flow, required reusable clients/adapters, assertions, and cleanup even when URLs, credentials, VIN, ICCID, brokers, or database connections are unavailable. Configuration access must be lazy enough that `pytest --collect-only` succeeds without runtime environment values.
4. **Preflight before side effects.** On real execution, validate the selected environment, isolation, service access, credentials, and required integrations before producing business data. Missing runtime values set the scenario execution status to `pending_environment` and fail preflight; do not use `skip` or treat the scenario as passed.
5. **Verify behavior.** When an environment is available, run focused tests, then the full suite and reconciliation scripts. Preserve redacted diagnostics, verify cleanup, and report the environment and source versions actually exercised.
6. **Incremental maintenance.** Before updating or regenerating a scenario, run the per-scenario Git impact workflow in [references/version-sync-policy.md](references/version-sync-policy.md). Change only scenarios whose source changes affect inputs, steps, assertions, correlation, or cleanup.

Use `contract_blocked` only when source discovery cannot establish an interface, message/schema contract, or expected business rule. Environment unavailability is `pending_environment`, not `contract_blocked`.

## Configuration contract

`config/common.yaml` contains only shared technical behavior such as integration capability switches and polling defaults. `config/environments/<profile>.yaml` contains environment-specific endpoints, credentials references, authentication, and integration connection settings. Resolve secrets through environment variables or an approved secret provider; never commit them.

A scenario declares only the integrations it needs and whether each is required or optional. It must not repeat connection details or capability switches. Instantiate and preflight an adapter only when the shared capability is enabled and the scenario declares the dependency. A disabled shared capability cannot be re-enabled by a scenario.

Transport clients accept per-request headers. Merge common, environment, scenario, and request headers using the project-defined precedence, then apply signing so reserved signing headers cannot be overridden. Signing settings and key references come from the selected environment configuration. Read [references/fixture-policy.md](references/fixture-policy.md) and [references/request-signing-template.md](references/request-signing-template.md).

## Test invariants

- Call the public business entrypoint; use approved read-only observers for downstream evidence.
- Assert business/application results, not only HTTP `2xx`.
- Correlate every asynchronous or persistence assertion by an order ID, atomic order ID, trace ID, or another source-confirmed business key.
- Use bounded polling with useful last-state diagnostics; do not use blind sleeps.
- Register idempotent cleanup immediately after resource creation. Prefer API cleanup and restore mutable scenario configuration.
- Keep tests repeatable and independent of execution order or pre-existing developer data.
- Put reusable transport, integration, and scope-safe fixture code under `common/`. Keep scenario actions, mappings, assertions, data, and cleanup inside the Chinese scenario directory.
- Use concise Chinese comments/docstrings where they explain business intent, ownership, correlation, polling, or cleanup. Keep technical identifiers aligned with source code.
- Reuse approved dependencies. Do not add an unpinned integration client implicitly.

For Kafka plus database workflows, create and subscribe a unique consumer group before the business request, record the starting offset, prove the correlated message was published, separately prove the same correlated result was consumed and persisted, then compare message and row fields. Read [references/integration-policy.md](references/integration-policy.md).

## Expected project shape

```text
mno-e2e/
  pyproject.toml
  AGENTS.md
  E2E_PLAN.md
  config/
    common.yaml
    environments/
      example.yaml
      icv-test.yaml
  common/
    clients/
    integrations/
    fixtures/
  scenarios/
    <中文业务名称>/
      场景定义.yaml
      场景说明.md
      业务流程图.md
      自动化测试流程图.md
      版本变更记录.md
      test_<中文业务名称>.py
  scripts/
    check_scenarios.py
    check_source_versions.py
```

Create additional scenario-owned business artifacts only when needed and give them clear Chinese business names. Never generate a global scenario list.

`scripts/check_scenarios.py` discovers `scenarios/*/场景定义.yaml` directly and checks definition/test/marker/artifact consistency. `scripts/check_source_versions.py` performs per-scenario repository state and impact checks; neither script owns a second source of truth.

## Completion

The work is complete when:

- every scenario directory has a self-contained definition, test, two distinct diagrams, and version-change record;
- every relevant repository has per-scenario source versions and anchors;
- source changes produce a per-scenario impact decision;
- `pytest --collect-only` succeeds without environment secrets or endpoints;
- real execution cannot pass preflight until required environment values are supplied;
- each required Kafka and database checkpoint is independently correlated and cleanup is verified;
- reconciliation finds no invalid directory names, duplicate stable IDs/markers, missing artifacts, or scenario directories without definitions.

Read [references/e2e-workflow.md](references/e2e-workflow.md) for the self-contained scenario schema and gates, [references/scenario-artifact-policy.md](references/scenario-artifact-policy.md) for naming and the two diagrams, and [references/version-sync-policy.md](references/version-sync-policy.md) whenever source versions are checked or updated.
