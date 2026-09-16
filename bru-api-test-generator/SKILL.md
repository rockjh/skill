---
name: bru-api-test-generator
description: Generate, execute, and incrementally maintain auditable Bruno HTTP API tests from local OpenAPI, project source, prior QA constraints, and execution evidence. Use for module-owned API QA with exact assertions, narrow database setup or verification when an API cannot provide it, machine-enforced lifecycle validation, independent module runs, and result-oriented failure reports; do not use for browser or database-heavy system E2E workflows.
---

# Bruno API Test Generator

Build a checked-in Bruno collection, attempt execution whenever the contract came from a running loopback service, and report concrete results. Execution scope is determined only by the selected module directory.

## Required Layout

Use the repository's existing QA location; otherwise use `qa/`:

```text
qa/
  bruno/
    bruno.json
    collection.bru
    <module-directory>/
  contracts/
    openapi.yaml
    module-map.yaml
    security-profile.yaml
    index.yaml
    generation-state.yaml
    qa-lock.yaml
    version-lock.yaml
    worker-assignments/<module-id>.yaml
    modules/<module-directory>/
      endpoints.yaml
      parameters.yaml
      definitions.yaml
      responses.yaml
      logic.yaml
      cases.yaml
      flows.yaml
      exclusions.yaml
      source-rules.yaml
      observed-rules.yaml
      materialization-state.yaml
      module-lock.yaml
      CASES.md
  constraints/
    rules.yaml
    source-rules.yaml
    observed-rules.yaml
  execution/
    config.yaml
    environments/local.bru
    run.bat
    run.sh
  evidence/
    global/
    modules/<module-id>/
  results/
    global/
    modules/<module-id>/
  logs/
```

`constraints/rules.yaml` is mandatory and machine readable. The same `qa_constraints.py` engine validates it during generation, materialization, pre-execution, and post-execution. Text in this file or a reference never substitutes for a machine rule.

Read [references/execution-config.md](references/execution-config.md) before initializing, migrating, or executing a collection.

## Coordinator Workflow

1. Read the business repository's `AGENTS.md`, build files, existing tests, security configuration, exception handling, fixtures, and existing QA assets.
2. Prefer a user-provided local OpenAPI file, then a checked-in contract. If absent, use `fetch_local_openapi.py` only against an already-running loopback service. Do not start the service or fetch a remote contract.
3. Run `bruno-api-test-generator init`, freeze `module-map.yaml`, and assign every operation to exactly one module. Method plus path is endpoint identity; IDs must also be unique.
4. Read Controller, Application, Domain Service, DTO, Output, Entity, Repository, exception/error-code enums, configuration, Flyway SQL, and existing tests. Run generation with every relevant source root:

   ```bash
   bruno-api-test-generator generate --openapi qa/contracts/openapi.yaml \
     --source-root . --incremental --coverage-profile full-matrix
   ```

5. Reuse `qa/constraints/source-rules.yaml`, global and module `observed-rules.yaml`, prior execution evidence, and named variables from the active local Bruno environment before inventing data. Environment values are referenced as `{{VARIABLE}}`; their literal values are never copied into contracts or requests. Source-derived values must populate cases when OpenAPI omits examples/defaults.
6. If an unavailable prerequisite API or omitted response state requires direct database access, add case-owned `database_steps` under the rules below. Otherwise do not connect to a database from an API case.
7. Materialize and validate all registered requests. Generated `.bru` files have no `meta.tags` line.
8. If OpenAPI provenance has a loopback `source_url`, generation must immediately invoke the default all-module run. A failed case must not stop later cases.
9. Merge module results, run global reconciliation, and write the final result report. The report, not static inventory, is the primary handoff.

Read [references/offline-swagger.md](references/offline-swagger.md), [references/case-matrix.md](references/case-matrix.md), [references/coverage-manifest.md](references/coverage-manifest.md), and [references/incremental-generation.md](references/incremental-generation.md) for schemas and detailed behavior.

## Source Constraints

Source scanning must inventory these evidence kinds even when one kind yields no rule:

```text
Controller -> Application -> Domain Service -> Repository / Integration
DTO / Output / CommonResponse -> Entity -> configuration -> Flyway -> existing tests
ControllerAdvice -> application exception -> error-code enum
```

Extract field type, requiredness, length, pattern, enum, min/max, default, unique markers, error codes, response envelopes, pagination, timestamps, and realistic examples. Store the file, line, source kind, and symbol with each inferred rule.

The project constraint library contains only rules proven by the target project's OpenAPI, source, configuration, migrations, tests, or successful local evidence. Do not inject built-in business-field examples, pagination defaults, error-code formats, or response conventions. Never extract or persist secrets as examples.

## Machine Gates

Every enabled rule in `qa/constraints/rules.yaml` is required. A violation fails the affected action. The shared engine enforces at least:

- one owner per endpoint and one module per owner;
- unique endpoint identity and case ID;
- every case registered to exactly one `.bru`, and no unregistered request;
- parseable request values and JSON bodies;
- every `review-*` placeholder has a non-empty `review_reasons` entry;
- no `review-*` placeholder remains when a source rule or active local environment variable provides the field value;
- no case/request drift and no silent overwrite of manual changes;
- module workers write only their assigned module-owned paths;
- module workers do not update `index.yaml`, `generation-state.yaml`, or `qa-lock.yaml`;
- business source files are never modified by QA commands;
- QA contracts, requests, evidence, and reports contain no credentials;
- database steps use one of the two allowed reasons, cite source evidence, and declare exact verification or cleanup;
- successful case inputs satisfy active source/domain field rules.

Static checks, execution preflight, and post-execution reconciliation call this same engine. Do not implement a second copy of a rule in prose or a stage-specific checker.

## Cases And Review Placeholders

Generate all scenarios supported by OpenAPI, source, local configuration, existing tests, or execution evidence. A missing OpenAPI example is not enough reason to create `review-*`; inspect the entire source/evidence chain first.

When a value still cannot be obtained, use a stable placeholder and record why:

```yaml
request:
  body:
    tenantId: review-tenantId
review_required: true
review_reasons:
  review-tenantId: No value exists in OpenAPI, source, tests, prior evidence, or local environment
```

An unexplained placeholder fails generation and checking. An explained placeholder places only that case in `manual_confirmation`; it does not prevent unrelated cases from running or passing. A manual-confirmation case is excluded from scope-completion requirements until resolved, but its observed execution result is still reported.

## Exact Assertions

Every non-review success case must assert:

- exact HTTP status and business success code;
- at least one exact key result, request/response relation, or allowed database-state assertion when the response omits that state;
- list item structure and length where applicable;
- page number, page size, total, and records/content structure for pagination;
- created/updated resource identifiers or captured IDs for write flows.

Derive assertions from DTO/Output/CommonResponse schemas, entities/repositories, fixed source values, OpenAPI examples, and redacted successful response evidence. A long-lived `status == 200`-only case is invalid. Execution retains redacted response values and shapes under `qa/evidence/` and writes observed rules for the next incremental generation.

## Direct Database Access

Direct MySQL, Elasticsearch, MongoDB, or other datastore statements are allowed only when:

1. the module has no public or approved test interface for creating prerequisite data required by its API cases; or
2. the response omits the state required to verify that an API operation succeeded.

Declare those operations as `database_steps` in the owning case. A `setup` step uses reason `missing_prerequisite_api`; an `assertion` step uses `missing_response_state`. Keep the statement directly in the case-owned Bruno script, use source-backed exact keys and expectations, load every connection value from the active environment, and clean up setup writes. Do not build a shared adapter for a one-off statement.

Database access is a fallback, not a replacement for an available API. It must not become the business action under test or weaken response assertions the API can support. Database connection, statement, assertion, and cleanup failures fail the case.

Read [references/database-access.md](references/database-access.md) before adding or executing a database step. It defines the manifest shape, Bruno script placement, dependency handling, examples, and safety limits.

## Execution

The launcher accepts only an optional module selector:

```bat
qa\execution\run.bat
qa\execution\run.bat --module "users"
```

No argument runs every module. `--module` accepts an ID, display name, directory, or OpenAPI Tag. Execution scope is directory based and never selected by tags.

Before requests, validate variables, files, locks, constraints, and a representative route. Bruno runs the complete selected directory; one failed request does not stop the remaining requests. Raw reports stay temporary. Normalized, redacted evidence and immutable timestamped result reports are retained.

Module execution uses its module-local lock and never updates global completion or version state. Global execution uses `qa-lock.yaml` and may advance the business version lock only after complete success.

Read [references/execution-evidence.md](references/execution-evidence.md) and [references/version-management.md](references/version-management.md).

## Parallel Module Work

Use parallel agents only when the user explicitly requests delegation. The coordinator initializes global assets, freezes module ownership, records shared configuration, assigns one worker per module, and performs the final merge.

A module worker may read shared assets and business source. It may write only:

```text
qa/contracts/modules/<its-module>/
qa/bruno/<its-module>/
qa/results/modules/<its-module-id>/
qa/evidence/modules/<its-module-id>/
qa/logs/modules/<its-module-id>/
```

Before dispatch, the coordinator runs `bruno-api-test-generator worker-start --module <module>`. The worker extracts module evidence, updates module cases/logic, materializes the module, writes `module-lock.yaml`, executes the module, and returns its result path. It must not edit business code, other modules, credentials, global indexes/locks, collection configuration, or cross-module flows. After each lifecycle stage it runs `worker-check --module <module> --stage <stage>`; changed paths are derived from the recorded Git snapshot and any out-of-scope path fails validation.

Modules run independently. Cross-module dependencies must be explicit in a coordinator-owned flow and use captures; never rely on directory order or another module's success. Read [references/parallel-generation.md](references/parallel-generation.md) only for delegated work.

After all module workers return, the coordinator runs `bruno-api-test-generator aggregate`. It reconciles current case IDs with the latest module reports, merges evidence, reruns post-execution constraints, and writes the global result-first report.

## Result Report

The final report begins with:

- total cases;
- executed, passed, failed, and not-executed counts;
- each module's status;
- failed case list;
- not-executed case list;
- manual-confirmation list.

Every failure row includes module, case ID, interface, request summary, expected result, actual result, reason, and manual-confirmation flag. Classify failures as `generation_failure`, `insufficient_data`, `environment_unavailable`, `endpoint_unreachable`, `request_failure`, `assertion_failure`, `insufficient_source_evidence`, and/or `manual_confirmation`. Do not collapse these into a generic state.

Static coverage, OpenAPI obligations, source mappings, and version warnings are supplemental sections.

## Incremental And Audit Rules

- New endpoints affect only their modules and new cases.
- Changed endpoints update only affected cases; unchanged successful cases retain state and evidence.
- Preserve manual case or `.bru` edits and fail on concurrent drift.
- Reuse source rules, response evidence, and execution evidence.
- Keep OpenAPI origin/SHA, generation time, source evidence, execution evidence, failures, manual confirmations, and state changes traceable.
- Do not modify business code to make a test pass.
- Do not put credentials in contracts, requests, evidence, or reports.
- `check` does not write assets unless the coordinator explicitly requests global status writing.

Use [references/cli.md](references/cli.md) for installation and script synchronization.
