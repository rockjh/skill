---
name: bru-api-test-generator
description: Generate, materialize, validate, execute, and incrementally maintain auditable Bruno API tests from local OpenAPI, current source, configuration, SQL, tests, and redacted runtime evidence. Use for module-owned HTTP API QA; do not use for browser or cross-service business E2E workflows.
---

# Bruno API Test Generator

Use the installed `dev-ai api-test` commands for every operation. `dev_ai.domains.api_test.constraints` is the rule authority; never weaken or reinterpret its failures.

## Workflow

1. Inspect current workspace instructions, build files, source, configuration, migrations, tests, and QA assets.
2. Use a local OpenAPI file or checked-in contract. Fetch only from an already-running loopback service when no local contract exists.
3. Run `dev-ai api-test init --qa-root qa`, then generate with the relevant source roots and reviewed module ownership. Generation records detected data sources, DDL/entities, explicit relationships, required fields, and safe environment references without persisting credentials.
4. Resolve gate failures in source evidence, contracts, cases, fixtures, variables, assertions, or mock-data ownership. Do not invent relationships or delete mandatory rules.
5. Materialize, check, preflight, and execute only through `dev-ai api-test`. A run analyzes all selected mock-data steps, asks once before the first write, prepares them in dependency order, then runs independent and data-dependent cases as authorized.
6. After a run creates data, answer the single cleanup decision or retain the run ledger for `dev-ai api-test mock-data-clean`. Treat `qa/results/` reports and mock-data ledgers as authoritative; console output is a redacted summary and pointer.

Database preparation or verification is allowed only when a public API cannot establish or observe the required state. It must use the current run namespace, narrowly scoped owned data, parameterized and idempotent operations, reverse-order cleanup, and absence verification. Production and protected environments are hard-blocked; flags cannot override them. Non-interactive writes and cleanup require their explicit command flags.

For parallel module work, run `worker-start` before delegation. Each worker owns exactly one module contract, Bruno directory, and module result area; it must not edit shared configuration or another module. The main agent owns shared assets, aggregation, and final reconciliation.

## References

Read only what the current operation needs:

- [references/cli.md](references/cli.md) for the unified command contract.
- [references/offline-swagger.md](references/offline-swagger.md) for local contract acquisition.
- [references/case-matrix.md](references/case-matrix.md) and [references/coverage-manifest.md](references/coverage-manifest.md) for generation and coverage work.
- [references/database-access.md](references/database-access.md) before database setup or verification.
- [references/execution-config.md](references/execution-config.md) and [references/execution-evidence.md](references/execution-evidence.md) for execution.
- [references/incremental-generation.md](references/incremental-generation.md) for regeneration.
- [references/parallel-generation.md](references/parallel-generation.md) only for explicitly delegated module work.
- [references/version-management.md](references/version-management.md) for project lock and source-version handling.

Use `dev-ai schema api-test.<command>` for command fields and schema versions.
