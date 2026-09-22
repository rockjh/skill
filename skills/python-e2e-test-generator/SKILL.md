---
name: python-e2e-test-generator
description: Discover multi-service business flows and generate source-tracked Python/pytest E2E scenarios with read-only runtime probes, safe controls, cleanup, and evidence. Use for cross-service E2E generation or maintenance, not exhaustive endpoint coverage.
---

Build black-box Python/pytest E2E from the user's scenario, natural-language design, formal contracts, source evidence, runtime information, and approved controls. Keep it domain-neutral: all participants, services, entities, states, paths, fields, topics, codes, schedules, and data come from current inputs.

Create one context per design rule: participants/services, entities, entries, states/transitions, branches, exceptions, async behavior, dependencies, correlation keys, side effects, observations, cleanup, recovery, and evidence. Mark rules `confirmed`, `manual_confirmation`, `conflict`, `not_applicable`, or `missing_evidence`; incomplete rules block only their scenario. Design owns expectations; protocol owns transport; source/runtime only add entry or observation support.

Use only `dev-ai e2e` commands. Use explicit `--design-root/--design-file`, `--openapi-root/--openapi-file`, or `--runtime-url`; explicit URLs are user-confirmed and may be external, auto-discovered URLs remain local-only. If no static protocol exists and a service is running, probe listeners/processes, startup arguments, working directories, effective configuration, health, standard OpenAPI/Swagger/AsyncAPI URLs, read-only queries, and database/cache/message/job relationships. Classify failures as `service_not_found`, `protocol_unknown`, `incomplete_protocol`, `authentication_missing`, `read_only_failed`, or `environment_invalid`. Missing environment variables do not prove a service is absent. Record source type, service, URL/file, format, fetch time, digest, and confirmation without credentials, cookies, or authorization headers.

Merge protocol sources by operation identity. Union compatible fields and retain disagreements with sources and `auto_merge`, `supplement`, `needs_manual_confirmation`, or `unusable` classification. Only a conflict used by the current scenario blocks it. Persist design-only `design-rules.yaml`/`logic.yaml`, transport-only `protocol-rules.yaml`, `scenario-plan.yaml`, support-only `value-resolution.yaml`, and `version-lock.yaml`; never restore `source-rules.yaml`. Materialize scenarios only from the plan; `dev-ai e2e check --gate contracts` proves coverage.

Before blocking a step, fill its matrix: public API, approved test/admin API, database control, messages, jobs, mocks/faults, dynamic configuration, existing data, and read-only observation. Generate unique IDs, correlation keys, legal fields, thresholds, simulated time, and messages per scenario namespace. Environment data is only for immutable entities, real identities, protected data, fixed tenants/accounts, approved preparation, or existing resources. Execute safe earlier steps and report later gaps; partial execution is never success.

Async steps record trigger, correlation, expected state, polling, timeout, interval, retry/repeat policy, and final failure. Use bounded `poll_until`; fixed sleeps are forbidden. Isolate namespaces, consumers, message IDs, mocks, configuration, records, and selectors. Trace assertions to design rule, protocol operation, observation, and result. Writes declare ownership, isolation, idempotent cleanup, reverse restoration, verification, and failure-path cleanup.

## Safety

- Never write production, prod/prd/live, or protected environments.
- Dangerous controls require per-run authorization bound to the active test environment.
- Prefer public APIs and read-only observation.
- Database preparation is parameterized, exact, single-row, verified, restored in reverse order, and never the final business result.
- Store credential references only; redact all output, artifacts, and reports.
- Runtime failure is a test failure, never static success or `pending_environment`.

## Ownership

Each `scenarios/<business-name>/` directory owns one journey, inputs, assertions, and cleanup. The main agent owns discovery, topology, shared fixtures, validators, isolation, versions, execution, restoration, and the final report.

## References

Read the phase reference needed: [discovery-and-control-policy.md](references/discovery-and-control-policy.md), [scenario-artifact-policy.md](references/scenario-artifact-policy.md), [fixture-policy.md](references/fixture-policy.md), [integration-policy.md](references/integration-policy.md), [e2e-workflow.md](references/e2e-workflow.md), [execution-script-policy.md](references/execution-script-policy.md), or [version-sync-policy.md](references/version-sync-policy.md).

Use `dev-ai schema e2e.scenario`, `e2e.workspace`, `e2e.config`, `e2e.report`, and `dev-ai schema e2e.<command>` for machine contracts. Keep structures, statuses, gate order, and report formats out of this file.
