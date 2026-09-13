---
name: python-e2e-test-generator
description: Run a gated workflow for designing and implementing black-box Python/pytest business E2E scenarios across microservices, APIs, asynchronous components, and data stores, with isolated environments, configurable headers/signatures, and scenario-owned artifacts. Use for an independent E2E test project; do not use for single-endpoint Bruno coverage.
---

# Python E2E Workflow

Use this skill as a workflow, not as a blind test-file generator. The independent Python project should behave like an external consumer of the deployed system. A business system may contain several service repositories; inspect them read-only to reconstruct the service graph, contracts, and observable side effects. The business repositories must not be modified to make E2E tests pass.

Invoke it from either the business workspace or an existing E2E project. If no
independent E2E project exists, identify a user-approved location during
discovery and create the test project there after plan approval; do not scatter
test code into individual service repositories.

## Boundary

This workflow owns cross-interface business behavior:

- multi-step journeys and state transitions.
- cross-service workflows spanning HTTP/gRPC, events, jobs, and callbacks.
- role/tenant/data-permission flows.
- database-, message-, cache-, and job-visible side effects when API assertions are insufficient.
- asynchronous processing, polling, files, external dependencies, idempotency, and recovery.
- deterministic fixtures, cleanup, reporting, and CI execution.

The Bruno collection owns the exhaustive single-endpoint matrix. Do not duplicate every Bruno validation case in Python. If a browser is required, use the project's approved Playwright/Selenium layer and keep the scenario model and cleanup rules below.

## Gated Phases

1. **Discover.** Inspect the independent E2E project's `AGENTS.md`, `pyproject.toml`, fixtures, clients, reporting, and test commands. Inspect every relevant service repository, its build/deployment manifests, API/event contracts, database migrations, job definitions, and the user's business description. Build a service and dependency map: entrypoints, downstream calls, topics/queues, tables, keys, jobs, and observable completion signals. Identify actors, preconditions, state transitions, data ownership, external dependencies, environment profiles, isolation primitives, request-header/signature rules, and the business project's configuration contract. Do not invent generic configuration names or defaults; use the project-defined sources, values, and fixture scopes.
2. **Plan only.** Before writing test code, produce or update `E2E_PLAN.md` and machine-readable `scenarios.yaml`. Each scenario gets a stable ID and status (`planned`, `implemented`, `blocked`, or `excluded`), an `artifact_dir`, a required swimlane diagram path, actor, preconditions, involved services/interfaces, request headers and optional signing configuration, required or optional integration dependencies, configuration source and scope, environment/isolation contract, business steps, observable checkpoints, expected outcomes, cleanup strategy, and dependencies on API case IDs. A checkpoint must identify the owning service, evidence source (HTTP/application response, database row, message, cache entry, job execution, file, or other project-approved observable), and its correlation key. Separate applicable scenarios from explicit exclusions with reasons. Stop for review by default; a user request that explicitly asks to implement or generate the scripts is approval to continue after the plan is recorded, not permission to skip planning.
3. **Golden sample.** After approval, implement one representative login/permission flow and one representative cross-service or state-transition flow. When the system uses an enabled asynchronous component, the sample must demonstrate its adapter, bounded polling, correlation, assertion, and cleanup. Run the samples in the real test environment and use review feedback to update the local `AGENTS.md` or workflow policy.
4. **Batch implementation.** Generate at most one or two related business workflows per batch. Keep the scenario ID visible in pytest markers and report output. Add only the integration adapters required by the approved scenarios; keep transport clients, component adapters, and business intent separate. After each batch, run the narrow tests, inspect failures and diagnostics, update the plan, and only then continue.
5. **Full verification.** Run the full pytest command, scenario reconciliation, and all planned database, message, cache, job, file, or artifact checks. Verify cleanup and isolation, including owned middleware records. Report the exact environment/build version, command, pass/fail result, skipped scenarios, missing observables, and remaining blockers.

## Test Design Rules

- `base_url`, credentials, tokens, tenant, feature flags, and any other runtime settings come from the business project's approved `conftest.py` fixtures, environment contract, or configuration provider; no secrets, developer URLs, or invented defaults in source.
- Every run selects a named project environment profile and an explicit isolation mechanism (for example, tenant, namespace, schema, topic prefix, cache prefix, or equivalent). Fail preflight when the profile or isolation value is missing; never silently fall back to a developer or shared environment. Record the source and scope in the scenario plan. Immutable settings may be session-scoped, run-level settings may be shared only within one isolated test run, and mutable settings must be scenario-owned or restored during cleanup.
- Transport clients must accept a per-request `headers` mapping. Merge project/common, environment, scenario, and request headers using the project's documented precedence, and redact sensitive values in diagnostics. Apply signing after ordinary header merging; configured signature headers are reserved and cannot be overridden by scenario/request headers. Do not hide scenario-specific headers in a global fixture.
- Support request signing behind an explicit project configuration switch. For the supplied Seres contract, `seres.sign=true` enables the exact SHA-256 procedure and `seres.sign=false` disables it; do not calculate a signature or send signature-derived headers when disabled. When enabled, require the project-declared algorithm, canonicalization, key source, header names, and raw-body mode; reject unknown algorithms or non-raw body modes, never guess defaults, and never commit keys. Read [references/fixture-policy.md](references/fixture-policy.md) and [references/request-signing-template.md](references/request-signing-template.md) for the header/signing contract and commented templates.
- Use an API client/helper for transport, but keep business intent visible in the scenario test. Do not hide the entire workflow in generic helper code.
- Model each microservice boundary explicitly. Use the public entrypoint for the user action, then use project-approved clients or read-only observers for downstream evidence; do not fake internal calls that the deployed system would make.
- Every scenario asserts the important business outcome, not merely that a request returned `2xx`. Include HTTP/application codes where they are part of the contract, plus at least one non-HTTP checkpoint whenever the business outcome crosses an asynchronous, persistence, cache, or scheduling boundary.
- MySQL, Kafka, EMQ/EMQX, Redis, XXL-JOB, and other public components use shared, generic adapters with project-provided mapping/configuration. The effective setting is `system.integration_config.<kind>.enabled AND scenario dependency exists AND scenario.integration_dependencies[].enabled`; a scenario may narrow a globally enabled component but may not override a global disable. Keep `enabled_integrations` as a derived compatibility list. If disabled or not opted in, do not instantiate its client, run its health check, create its fixture, or claim its checkpoint; if enabled, verify its configuration and lifecycle. Read [references/integration-policy.md](references/integration-policy.md) for component-specific evidence and isolation rules.
- Classify each integration dependency as required or optional. A required dependency that is unavailable, unauthorized, or misconfigured fails the precondition and blocks the scenario; an optional dependency may be skipped only with an explicit plan reason and report entry. Never silently downgrade an integration checkpoint to an HTTP-only assertion.
- Run a non-destructive preflight for every involved service and enabled observer before creating business data. Verify connectivity, credentials, protocol/schema access, and required read permissions; record the exact failure instead of masking it with retries.
- Correlate every side-effect assertion with a run/scenario identifier, business key, trace ID, or project-defined equivalent. Poll with a bounded deadline and report the last observed state; never consume or query unscoped data and call it a pass.
- Writes use unique run/case identifiers and have teardown that runs on failure as well as success. Prefer API cleanup; use direct DB cleanup only where it is necessary and documented.
- Tests must be independently repeatable. Do not rely on execution order, a developer's existing records, or a previous test's token/session.
- Parallel execution is allowed only when service, tenant, topic, consumer, database, cache, job, and mutable configuration ownership is scenario-safe. If a project-wide setting cannot be isolated, mark the affected scenarios serial and snapshot/restore it with an explicit lock.
- Use polling with a bounded timeout for eventual consistency. Do not use blind sleeps or broad retries that conceal defects.
- Keep fixtures narrowly scoped and make ownership explicit: session, run, scenario, and step data should not leak across tests.
- Keep non-public code, data, scripts, and explanatory documents inside `scenarios/<SCENARIO_ID>/`; only genuinely reusable transport, fixtures, signing helpers, and component adapters belong in shared modules. Every scenario must include a Mermaid swimlane diagram covering actors, service boundaries, enabled observers, correlation, and cleanup. Read [references/scenario-artifact-policy.md](references/scenario-artifact-policy.md).
- Generated Python must use Chinese comments/docstrings at high density for modules, classes, fixtures, public helpers, scenario steps, configuration fields, non-trivial branches, correlation, polling, and cleanup. Keep identifiers aligned with the project, but explain business intent and operational ownership in Chinese; do not emit unexplained English-only scaffolding.
- A Python scenario can reference Bruno/API case IDs, but it does not satisfy endpoint coverage unless the manifest explicitly says so.
- Generate a project-specific reconciliation check that fails on plan IDs without tests, tests without plan IDs, conflicting integration/signing enablement sources, enabled integrations without checkpoints, non-raw signed bodies, unmaterialized template paths, or scenarios without cleanup evidence. Keep its command in the project test instructions and CI configuration.
- Reuse existing approved dependencies. Any new client library needed for an integration must be explicitly approved, pinned in the E2E project's dependency file, and covered by the plan; never install an unpinned client as an implicit implementation detail.
- Do not modify application source, schema, production data, or deployment configuration as part of test generation.

## Generated Deliverables

After approval, produce the smallest complete set of project-conforming
artifacts: the approved scenario plan, one artifact directory per scenario
containing its pytest code, docs/templates, data, scripts, README, and swimlane, shared generic
fixtures/transport/signing clients, and generic component adapters activated
only when configured and needed by an approved scenario, plus configuration,
preflight documentation, and scenario reconciliation/verification scripts.
The project must document required versus optional integrations and the command
that checks plan-to-test coverage. Keep service and component identifiers
visible in test names, markers, logs, and reports.
Do not generate placeholder tests for unknown services, configuration, or
observables; leave those scenarios planned with an explicit missing-input or
blocked reason.

## Recommended Project Shape

```text
e2e-project/
  pyproject.toml
  AGENTS.md
  E2E_PLAN.md
  scenarios.yaml
  tests/
    conftest.py                 # project-wide fixtures only
  scenarios/
    ORDER_CREATE_001/
      test_order_create.py
      README.md
      swimlane.md
      docs/                      # materialized templates and detailed docs
      data/                      # scenario-owned request/expected data
      scripts/                   # scenario-owned setup/verification helpers
  common/
    clients/                     # reusable transport and signing clients
    integrations/                # generic MySQL/Kafka/EMQ/Redis adapters
    fixtures/                    # reusable, scope-safe fixtures
  scripts/
    check_scenarios.py
```

Keep the endpoint/case manifest owned by the business repository or consume the exact versioned artifact published by it. Never silently test an unpinned "latest" build.

## Completion

The workflow is complete only when the approved scenario inventory has no unexplained missing or unimplemented scenarios, every integration enabled by an approved scenario has a verified evidence checkpoint and cleanup result, every scenario has an up-to-date swimlane and scenario-owned artifacts, the targeted and full pytest commands have real results, cleanup is verified, and failures are actionable. If the environment, configuration contract, or business description is insufficient, stop at the plan and state the missing input instead of inventing domain behavior.

Read [references/e2e-workflow.md](references/e2e-workflow.md) for the scenario-plan schema and review gates, [references/fixture-policy.md](references/fixture-policy.md) for environment isolation and header/signing conventions, [references/integration-policy.md](references/integration-policy.md) when a scenario uses a cross-service or middleware observable, and [references/scenario-artifact-policy.md](references/scenario-artifact-policy.md) for per-scenario files, swimlanes, and Chinese comments.
