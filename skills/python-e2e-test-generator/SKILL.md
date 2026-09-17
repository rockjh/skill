---
name: python-e2e-test-generator
description: Discover multi-repository business flows and generate source-tracked Python/pytest E2E scenarios with runtime probes, safe control gates, cleanup, and real evidence. Use for cross-service E2E generation or maintenance; do not use for exhaustive single-endpoint coverage.
---

# Python E2E Test Generator

Build an independent black-box E2E project from current application behavior and configuration. Never modify application source to make a test pass.

Use only the installed `dev-ai e2e` commands for initialization, validation, source checks, and execution. `dev-ai e2e run` owns the fixed gate order, pytest execution, evidence checks, restoration verification, and `artifacts/e2e-run.json`; do not bypass or reproduce those gates in project scripts.

## Safety

- Never write to production, conventional prod/prd/live names, or any protected environment.
- Prefer public business APIs and read-only observation.
- Test/admin endpoints, mocks, faults, dynamic configuration, jobs, message publication, database controls, and other dangerous capabilities require explicit authorization for each run, bound to the active test environment.
- Every write requires owned test data, exact correlation, isolation, idempotent cleanup, and verified restoration.
- Record credential references only. Never persist credential values in discovery, scenario, evidence, or reports.
- Runtime failure is a test failure, not static success or a pending status.

## Ownership

Each direct `scenarios/<business-name>/` directory owns one journey, its inputs, orchestration, assertions, and cleanup. A scenario agent may edit only its assigned scenario directory and must request shared changes from the main agent.

The main agent owns workspace discovery, topology, shared configuration, common clients and fixtures, validators, isolation review, source-version checks, execution, restoration, and the final report. For multiple independent scenarios, assign one directory per scenario agent. If delegation is unavailable, process them sequentially and report that fact.

## References

Read only the reference needed now:

- [references/discovery-and-control-policy.md](references/discovery-and-control-policy.md) before discovery, control selection, or scenario planning.
- [references/scenario-artifact-policy.md](references/scenario-artifact-policy.md) before editing a scenario directory.
- [references/fixture-policy.md](references/fixture-policy.md) for configuration, data, isolation, and cleanup.
- [references/integration-policy.md](references/integration-policy.md) for service, message, database, cache, scheduler, or observer integration.
- [references/e2e-workflow.md](references/e2e-workflow.md) for workflow decisions and result interpretation.
- [references/execution-script-policy.md](references/execution-script-policy.md) for the shared CLI execution contract.
- [references/version-sync-policy.md](references/version-sync-policy.md) before regenerating existing scenarios.

Use `dev-ai schema e2e.scenario`, `e2e.workspace`, `e2e.config`, and `e2e.report` for machine-readable contracts, and `dev-ai schema e2e.<command>` for command fields. Keep project structure, field schemas, statuses, gate order, and report format out of this file.
