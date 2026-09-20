---
name: python-e2e-test-generator
description: Discover multi-repository business flows and generate source-tracked Python/pytest E2E scenarios with runtime probes, safe control gates, cleanup, and real evidence. Use for cross-service E2E generation or maintenance; do not use for exhaustive single-endpoint coverage.
---

Build an independent black-box E2E project from reviewed design documents and formal protocol contracts. Design owns cross-service flow, states, errors, idempotency, async completion, results, and side effects; OpenAPI/protocol owns transport shape. Source, current state, messages, and runtime results never create business expectations.

Use `dev-ai e2e init/generate` with `--design-root/--design-file` and `--openapi-root/--openapi-file` for explicit inputs. Generation stops without writing on missing, ambiguous, conflicting, or `manual_confirmation` evidence. `discovery/design-rules.yaml` and `logic.yaml` are design-only; `protocol-rules.yaml` owns call shape; `scenario-plan.yaml` maps every non-excluded design rule to formal calls; `config/value-resolution.yaml` is support-only; `version-lock.yaml` records all input changes; `source-rules.yaml` is retired. `generate` does not invent executable scenario code: scenario agents materialize `scenarios/<scenario>/` only from the validated plan, then `dev-ai e2e check --gate contracts` proves coverage.

Use only installed `dev-ai e2e` commands. `dev-ai e2e run` owns gate order, pytest, evidence, restoration, and `artifacts/e2e-run.json`; do not reproduce these gates in project scripts.

For execution requests, first complete the read-only local runtime probe: listeners and owning processes, startup arguments/working directory/profile, effective configuration precedence, health/OpenAPI or other read-only calls, and discovered databases, middleware, and schedulers. A missing E2E environment variable does not prove that the environment is absent.

Before declaring any precondition or step blocked, fill its eight-path constructability matrix from source or runtime evidence: public API, approved test/admin API, database control, messages, jobs, mocks/faults, dynamic configuration, and existing test data. Safely constructible scenario-owned data is test setup, not an environment prerequisite. Execute every independently safe earlier step; report later gaps per step and never promote partial execution to success.

## Safety

- Never write to production, conventional prod/prd/live names, or any protected environment.
- Prefer public business APIs and read-only observation.
- Test/admin endpoints, mocks, faults, dynamic configuration, jobs, message publication, database controls, and other dangerous capabilities require explicit authorization for each run, bound to the active test environment.
- Every write requires owned test data, exact correlation, isolation, idempotent cleanup, and verified restoration.
- Every write probe is a normal controlled write: register its cleanup before execution and verify no residual state.
- Database preparation uses ordered single-row parameterized operations, verifies each mutation, restores in reverse order on every exit path, and never manufactures the final business result.
- Record credential references only. Never persist credential values in discovery, scenario, evidence, or reports.
- Runtime failure is a test failure, not static success or a pending status.

## Ownership

Each `scenarios/<business-name>/` directory owns one journey, inputs, orchestration, assertions, and cleanup. Scenario agents edit only their assigned directory and request shared changes from the main agent.

The main agent owns discovery, topology, shared configuration/fixtures, validators, isolation, source versions, execution, restoration, and the final report. Assign one directory per independent scenario; otherwise process sequentially and report it.

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
