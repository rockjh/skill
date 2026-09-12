---
name: python-e2e-test-generator
description: Run a gated workflow for designing and implementing black-box Python/pytest business E2E scenarios across microservices, APIs, asynchronous components, and data stores from application code, contracts, and user requirements. Use for an independent E2E test project; do not use for single-endpoint Bruno coverage.
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

1. **Discover.** Inspect the independent E2E project's `AGENTS.md`, `pyproject.toml`, fixtures, clients, reporting, and test commands. Inspect every relevant service repository, its build/deployment manifests, API/event contracts, database migrations, job definitions, and the user's business description. Build a service and dependency map: entrypoints, downstream calls, topics/queues, tables, keys, jobs, and observable completion signals. Identify actors, preconditions, state transitions, data ownership, external dependencies, and the business project's configuration contract. Do not invent generic configuration names or defaults; use the project-defined sources, values, and fixture scopes.
2. **Plan only.** Before writing test code, produce or update `E2E_PLAN.md` and machine-readable `scenarios.yaml`. Each scenario gets a stable ID and status (`planned`, `implemented`, `blocked`, or `excluded`), actor, preconditions, involved services/interfaces, required or optional integration dependencies, configuration source and scope, business steps, observable checkpoints, expected outcomes, cleanup strategy, and dependencies on API case IDs. A checkpoint must identify the owning service, evidence source (HTTP/application response, database row, message, cache entry, job execution, file, or other project-approved observable), and its correlation key. Separate applicable scenarios from explicit exclusions with reasons. Stop for review by default; a user request that explicitly asks to implement or generate the scripts is approval to continue after the plan is recorded, not permission to skip planning.
3. **Golden sample.** After approval, implement one representative login/permission flow and one representative cross-service or state-transition flow. When the system uses an enabled asynchronous component, the sample must demonstrate its adapter, bounded polling, correlation, assertion, and cleanup. Run the samples in the real test environment and use review feedback to update the local `AGENTS.md` or workflow policy.
4. **Batch implementation.** Generate at most one or two related business workflows per batch. Keep the scenario ID visible in pytest markers and report output. Add only the integration adapters required by the approved scenarios; keep transport clients, component adapters, and business intent separate. After each batch, run the narrow tests, inspect failures and diagnostics, update the plan, and only then continue.
5. **Full verification.** Run the full pytest command, scenario reconciliation, and all planned database, message, cache, job, file, or artifact checks. Verify cleanup and isolation, including owned middleware records. Report the exact environment/build version, command, pass/fail result, skipped scenarios, missing observables, and remaining blockers.

## Test Design Rules

- `base_url`, credentials, tokens, tenant, feature flags, and any other runtime settings come from the business project's approved `conftest.py` fixtures, environment contract, or configuration provider; no secrets, developer URLs, or invented defaults in source.
- Record the actual configuration source and scope in the scenario plan. Immutable settings may be session-scoped, run-level settings may be shared only within one isolated test run, and mutable settings must be scenario-owned or restored during cleanup.
- Use an API client/helper for transport, but keep business intent visible in the scenario test. Do not hide the entire workflow in generic helper code.
- Model each microservice boundary explicitly. Use the public entrypoint for the user action, then use project-approved clients or read-only observers for downstream evidence; do not fake internal calls that the deployed system would make.
- Every scenario asserts the important business outcome, not merely that a request returned `2xx`. Include HTTP/application codes where they are part of the contract, plus at least one non-HTTP checkpoint whenever the business outcome crosses an asynchronous, persistence, cache, or scheduling boundary.
- Enable Kafka, MySQL, EMQ/EMQX, Redis, XXL-JOB, or another component only when the business project uses it and the approved scenario needs it. Resolve its client, endpoint, credentials, serialization, topic/table/key conventions, and cleanup rules from the project; never hardcode a generic component configuration. Read [references/integration-policy.md](references/integration-policy.md) for component-specific evidence and isolation rules.
- Classify each integration dependency as required or optional. A required dependency that is unavailable, unauthorized, or misconfigured fails the precondition and blocks the scenario; an optional dependency may be skipped only with an explicit plan reason and report entry. Never silently downgrade an integration checkpoint to an HTTP-only assertion.
- Run a non-destructive preflight for every involved service and enabled observer before creating business data. Verify connectivity, credentials, protocol/schema access, and required read permissions; record the exact failure instead of masking it with retries.
- Correlate every side-effect assertion with a run/scenario identifier, business key, trace ID, or project-defined equivalent. Poll with a bounded deadline and report the last observed state; never consume or query unscoped data and call it a pass.
- Writes use unique run/case identifiers and have teardown that runs on failure as well as success. Prefer API cleanup; use direct DB cleanup only where it is necessary and documented.
- Tests must be independently repeatable. Do not rely on execution order, a developer's existing records, or a previous test's token/session.
- Parallel execution is allowed only when service, tenant, topic, consumer, database, cache, job, and mutable configuration ownership is scenario-safe. If a project-wide setting cannot be isolated, mark the affected scenarios serial and snapshot/restore it with an explicit lock.
- Use polling with a bounded timeout for eventual consistency. Do not use blind sleeps or broad retries that conceal defects.
- Keep fixtures narrowly scoped and make ownership explicit: session, run, scenario, and step data should not leak across tests.
- A Python scenario can reference Bruno/API case IDs, but it does not satisfy endpoint coverage unless the manifest explicitly says so.
- Generate a project-specific reconciliation check that fails on plan IDs without tests, tests without plan IDs, enabled integrations without checkpoints, or scenarios without cleanup evidence. Keep its command in the project test instructions and CI configuration.
- Reuse existing approved dependencies. Any new client library needed for an integration must be explicitly approved, pinned in the E2E project's dependency file, and covered by the plan; never install an unpinned client as an implicit implementation detail.
- Do not modify application source, schema, production data, or deployment configuration as part of test generation.

## Generated Deliverables

After approval, produce the smallest complete set of project-conforming
artifacts: the approved scenario plan, pytest scenarios, shared fixtures and
transport clients, only the required component adapters, configuration and
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
    conftest.py
    scenarios/
      test_user_lifecycle.py
  clients/
  integrations/                 # only project-required component adapters
  fixtures/
  scripts/
    check_scenarios.py
```

Keep the endpoint/case manifest owned by the business repository or consume the exact versioned artifact published by it. Never silently test an unpinned "latest" build.

## Completion

The workflow is complete only when the approved scenario inventory has no unexplained missing or unimplemented scenarios, every integration enabled by an approved scenario has a verified evidence checkpoint and cleanup result, the targeted and full pytest commands have real results, cleanup is verified, and failures are actionable. If the environment, configuration contract, or business description is insufficient, stop at the plan and state the missing input instead of inventing domain behavior.

Read [references/e2e-workflow.md](references/e2e-workflow.md) for the scenario-plan schema and review gates, [references/fixture-policy.md](references/fixture-policy.md) for isolation and cleanup conventions, and [references/integration-policy.md](references/integration-policy.md) when a scenario uses a cross-service or middleware observable.
