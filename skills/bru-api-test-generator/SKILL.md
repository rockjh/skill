---
name: bru-api-test-generator
description: Generate auditable Bruno API tests from reviewed design documents and a local OpenAPI contract. Source code is execution support only; do not use this Skill for browser or cross-service E2E workflows.
---

# Bruno API Test Generator

Use `dev-ai api-test` for every operation. The design document is the sole
authority for business rules, flows, states, business errors, idempotency,
concurrency, retries, asynchronous outcomes, side effects, and business
assertions. OpenAPI is the sole authority for HTTP method/path, parameters,
types, formats, media types, transport status codes, and protocol validation.
Source and runtime responses are support/evidence only.

## Workflow

1. Read repository instructions and locate reviewed design documents. Discovery
   checks root `AGENTS.md`/`README`, then `docs/design`, `docs/详细设计`,
   `design`, and `doc/design`. Use `--design-root` or `--design-file` when
   discovery is ambiguous. No design source means generation is blocked.
2. Run `dev-ai api-test init --qa-root qa --design-root docs/design`, then
   `dev-ai api-test generate --qa-root qa --openapi qa/contracts/openapi.json
   --design-root docs/design`. Generation builds a bidirectional OpenAPI /
   design mapping and writes `qa/constraints/design-rules.yaml`.
3. Resolve mapping drift, conflicting documents, missing rules, and every
   `manual_confirmation` item before generation can continue. Never repair a design expectation from source
   or observed behaviour. Auxiliary endpoints may be excluded only through an
   approved `exclusions.yaml` entry with scope and reason.
4. Source roots are read in a separate execution-preparation phase only for
   ports, context paths, environment/header/signing setup, safe fixture values,
   test data preparation, upload templates, mocks, and local startup. Such
   values are `support-only` and may populate `value-resolution.yaml`, never
   `logic.yaml`, business assertions, expected states, or business error codes.
5. Materialize, preflight, execute, and reconcile through `dev-ai api-test`.
   Actual responses decide pass/fail and may be recorded in
   `observed-rules.yaml`, but they must never update design expectations.
   Ordered multi-request behavior is executable only when the reviewed design
   declares a `Test Flow` whose steps reference reviewed rule IDs. The runner
   derives flow evidence from the real Bruno case order and uniquely named
   `dev-ai:flow:*` reporter events. Async rules additionally require a bounded
   polling/reconciliation contract; external-failure rules require authorized
   injection plus restoration evidence, otherwise generation stops with
   `manual_confirmation`.

Database access is a last resort: use namespaced, owned, parameterized data
with idempotent reverse cleanup and absence verification. Production/protected
environments are hard-blocked; write and cleanup flags are explicit.

## Gates

- Every formal OpenAPI endpoint maps to one design section, or to an approved
  exclusion. Every design `METHOD /path` exists in OpenAPI.
- `logic.yaml` entries and business cases have `source: design` and cite a
  design rule ID. OpenAPI-only entries are transport/protocol obligations.
- Successful cases assert a concrete business result or state change; async
  cases distinguish acceptance from final outcome. A rule that needs a
  repeated, concurrent, or acceptance/final multi-request flow is blocked
  unless that flow is executable; metadata alone never counts as coverage.
- Concurrent behavior remains blocked unless the runtime can issue genuinely
  concurrent requests; a sequential `Test Flow` cannot satisfy that rule.
- Request values carry config/fixture/support-source provenance and remain
  reproducible, namespaced, cleanable, and recoverable.
- Incremental generation checks both OpenAPI and design fingerprints. The
  version lock records summaries for both sources.

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

Use `dev-ai schema api-test.<command>` for commands and `dev-ai schema
api-test.<artifact>` for persisted design, logic, value-resolution, and version
lock contracts.
