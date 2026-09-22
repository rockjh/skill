---
name: bru-api-test-generator
description: Generate auditable Bruno API tests from reviewed design documents and a local OpenAPI contract. Source code is execution support only; do not use this Skill for browser or cross-service E2E workflows.
---

# Bruno API Test Generator

Use `dev-ai api-test` for every operation. Design defines business scenarios,
flows, states, errors, safety, async outcomes, and assertions; OpenAPI defines
HTTP method/path, parameters, payload/response shape, media types, statuses,
and protocol checks. Source and runtime responses are evidence only.

## Workflow

1. Read `AGENTS.md` and locate reviewed design documents under
   `docs/design`, `design`, or `doc/design`; use `--design-root` or
   `--design-file` when ambiguous. No design source blocks generation.
2. Before generation, read every selected document, including prose, tables,
   code/curl blocks, diagrams, ordered steps, acceptance lists, and migration
   notes. Build a temporary design-understanding matrix
   for each rule: ID, source quote, name, candidate method/path, OpenAPI match,
   preconditions, request/results, errors, effects, idempotency, retry,
   concurrency, async, consistency, evidence, derivation, unknowns, and
   executability. Missing headings or `Assert` markers are parser gaps, not
   proof that no rule exists.
3. Run `dev-ai api-test init --qa-root qa --design-root docs/design`, then
   complete the mandatory understanding phase with
   `dev-ai api-test understand --qa-root qa --openapi qa/contracts/openapi.json
   --design-root docs/design`. Review the persisted matrix at
   `qa/constraints/design-rules.yaml` before running
   `dev-ai api-test generate --qa-root qa --openapi qa/contracts/openapi.json
   --design-root docs/design`. Generation refuses an incomplete or stale
   matrix. Tag facts `explicit`, `derived` (retain quote/derivation), or
   `unknown`; unknowns become concrete confirmations, never values from source,
   examples, or responses.
4. Resolve every mapping drift, conflict, missing fact, and
   `manual_confirmation`. Reports distinguish exact, path-parameter alias,
   semantic candidate, design-only, OpenAPI-only, and multiple-candidate
   mappings. Parameter aliases are candidates and must remain visible.
5. Read source roots only for execution support: ports, paths, headers/signing,
   auth, fixtures, setup/cleanup, uploads, mocks, and startup. These values
   may populate `value-resolution.yaml`, never business assertions, expected
   states, or business error codes.
6. Materialize, preflight, execute, and reconcile through `dev-ai api-test`.
   Responses decide pass/fail and may be recorded in `observed-rules.yaml`,
   never used to change design expectations. A multi-request behavior is
   executable only when reviewed flow steps reference reviewed rule IDs;
   async behavior additionally needs bounded polling/convergence, and fault
   injection needs authorization and restoration evidence.

Blocked generation still writes an audit report separating formal scripts,
protocol cases, pending candidates, unsupported/uncovered mappings, unexecuted
cases, and gate reasons to `qa/results/design-generation-report.json`.

Prose becomes candidate assertions: states become state checks; idempotency,
retry, and negative effects become flows/absence checks only with an observable
design boundary. Protocol checks never count as business coverage. Ordered
calls stay flows; unspecified captures, convergence, or cleanup remain
`flow_candidates` for confirmation.

Database access is a last resort: use owned, namespaced, parameterized data
with idempotent cleanup and absence verification. Production/protected
environments are hard-blocked; write/cleanup flags are explicit.

## Gates

- Every OpenAPI endpoint maps to reviewed design or an approved exclusion; each
  design operation maps to OpenAPI or a concrete pending item.
- `logic.yaml` and business cases use `source: design`, cite a design rule ID,
  preserve quote/evidence level/derivation, and carry business assertions.
  OpenAPI-only cases are transport obligations.
- Successful cases assert a concrete result/state. Repeated, concurrent, or
  acceptance/final behavior is blocked unless its flow is executable; a
  sequential flow cannot prove concurrency.
- Values are reproducible, namespaced, cleanable, and recoverable; incremental
  generation fingerprints both sources.
- Completion output separates formal scripts, protocol-only cases, pending
  candidates, unsupported/uncovered and unexecuted cases, and each gate reason.

## References

Read only the needed reference: `cli.md`, `offline-swagger.md`,
`case-matrix.md`, `coverage-manifest.md`, `database-access.md`,
`execution-config.md`, `execution-evidence.md`, `incremental-generation.md`,
`parallel-generation.md` (delegated work only), or `version-management.md`.

Use `dev-ai schema api-test.<command>` for commands and
`dev-ai schema api-test.<artifact>` for persisted contracts.
