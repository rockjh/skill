---
name: bru-api-test-generator
description: Generate and maintain auditable Bruno HTTP API tests in a business-code repository from an offline OpenAPI contract, optional source evidence, and environment configuration. Use for module-partitioned API case generation, execution, risk confirmation, and strict coverage reconciliation; do not use for browser or database-heavy system E2E workflows.
---

# Bruno API Test Generator

Build a checked-in Bruno collection whose manifests, requests, source evidence, execution evidence, and version locks reconcile exactly.

## Boundary

This skill owns single-endpoint API and contract coverage:

- every reachable OpenAPI operation and its success path;
- authentication, authorization, validation, query, file, business-error, and safety scenarios supported by evidence;
- observable controller, application, domain-service, repository, and integration branches;
- explicitly declared ordered module or cross-module flows;
- Bruno materialization, preflight, safe execution, evidence normalization, and reconciliation.

Use the project's Python E2E workflow for browser journeys, database-heavy assertions, asynchronous system scenarios, and broad multi-service workflows.

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
    modules/<module-directory>/
      endpoints.yaml
      parameters.yaml
      definitions.yaml
      responses.yaml
      logic.yaml
      cases.yaml
      flows.yaml
      exclusions.yaml
      CASES.md
  execution/
    config.yaml
    environments/local.bru
    run.bat
    run.sh
    README.md
  scripts/
```

Do not create `qa.yaml` or `execution/plans.yaml`. `execution/config.yaml` owns environment selection, tool mode, coverage profile, and signing provider:

```yaml
active_environment: local
tooling: project-scripts
coverage_profile: full-matrix
sign:
  provider: disabled
```

For SERES signing use `provider: seres` and `version: v1`. Keep all URLs, Headers, Cookies, tokens, and keys in `execution/environments/<name>.bru`; never put secrets in `config.yaml`.

Read [references/execution-config.md](references/execution-config.md) before initializing, migrating, or executing a collection.

## Generation Workflow

1. Read the business repository's `AGENTS.md`, build files, existing test conventions, security configuration, exception handling, fixtures, and existing Bruno assets.
2. Use a user-provided offline OpenAPI/Swagger file first. Otherwise locate a checked-in document. If none exists, the bundled `fetch_local_openapi.py` may probe only an already-running loopback service and save the result locally. Never fetch a remote contract, start the service automatically, or invent an API from prose.
3. Partition every operation into exactly one module. Prefer its OpenAPI Tag. Multi-Tag operations need an explicit primary owner; untagged operations need an explicit `operation_ids`, `path_prefixes`, one default module, or a single-module map. Ambiguous or missing ownership is blocking.
4. Initialize with `mno-bruno-qa init`, then generate with:

   ```bash
   mno-bruno-qa generate --openapi qa/contracts/openapi.yaml --source-root . --incremental --coverage-profile full-matrix
   ```

5. Materialize `.bru` files centrally with `mno-bruno-qa materialize` or the project-local equivalent. Incremental generation preserves manually changed cases, marks them `manual_review`, and never silently overwrites them.

`contract-draft` creates only cases directly provable from OpenAPI. `full-matrix` creates every applicable scenario supported by contract, source, security profile, or probe evidence. `verified` is an execution result, never a generation profile.

Read [references/offline-swagger.md](references/offline-swagger.md) for acquisition and module ownership, [references/case-matrix.md](references/case-matrix.md) for scenario rules, [references/coverage-manifest.md](references/coverage-manifest.md) for schemas, and [references/incremental-generation.md](references/incremental-generation.md) for fingerprints.

## Scenario Matrix

Every endpoint records decisions for `success`, `authentication`, `authorization`, `validation`, `business_error`, `query`, `safety`, and `file`. An applicable decision links at least one dedicated case. A confirmed false decision includes concrete evidence and a reason.

- `success`: at least one distinct case for every reachable endpoint.
- `authentication`: distinguish `auth-token` authentication from `admin-operator-context` audit context. Generate missing/invalid-token cases only after representative probes establish actual status and response shape.
- `authorization`: require OpenAPI security/permission extensions, roles, source permission annotations, `security-profile.yaml`, or probe evidence. An `/admin` path alone is never authorization evidence.
- `validation`: cover required parameters/body fields, enum, pattern, minimum/maximum, minLength/maxLength, format, and wrong Content-Type when declared.
- `query`: cover pagination boundaries, filters, sorting, combinations, empty results, invalid page, and invalid pageSize when applicable.
- `file`: cover missing and empty files plus extension, MIME, and size constraints only when declared or source-proven.
- `business_error`: generate only from API-reachable source exceptions and real error codes.
- `safety`: generate only from declared idempotency, concurrency, or repeat-submission semantics.

Cases keep a `risk` field, concrete request data, expected HTTP status, expected business code when known, and exact response assertions. A status-only assertion is incomplete. Draft placeholders may use an existence assertion for review, but they cannot pass verified completion.

## Source Evidence

Source scanners produce candidates, not proof. For Java, trace API-reachable calls through:

```text
Controller -> Application -> Domain Service -> Repository / Integration
```

Follow calls to `MnoTrafficApplicationException` and `MnoTrafficErrorCodeEnum`. Do not substitute unrelated exception or error-code types. Exclude `src/test`, architecture tests, Javadoc, error-enum definitions, constant-only classes, generated/build directories, and branches not reachable from an API mapping.

Every coverage-required candidate must map to exactly one endpoint and one or more real case IDs. Ambiguous mapping is immediately blocking:

```text
ERROR: source candidate <id> cannot be uniquely mapped to an endpoint
```

Never summarize unresolved candidates as non-blocking review output.

## Bruno Materialization

- Materialize exactly one business request for each registered case.
- Keep only the case risk in `.bru` `meta.tags`; never generate `plan-*` tags and never use tags to select execution scope.
- Use a two-or-more-digit stable sequence plus the sanitized Chinese `case.title` for each business filename. Keep the stable English case ID in `meta.name` and manifests.
- Preserve exact method, path, query, body, Header omission, assertions, captures, and flow variables from `cases.yaml`.
- Inject common Headers and optional signing once in `collection.bru`; request-local Headers win.

## Execution

There are exactly two execution scopes:

```bat
qa\execution\run.bat --all
qa\execution\run.bat --module "APP车辆用量查询"
```

`--all` and `--module` are mutually exclusive. A module can be selected by module ID, display name, directory, or OpenAPI Tag. There is no `--plan` entry and no `--risk` case filter. The selected collection or module always runs all registered business requests.

Before any request, derive the selected scope's actual risks from `cases.yaml`:

| Included risk | Required confirmation |
| --- | --- |
| `read-only` only | none |
| `isolated-write` | `--confirm-write` |
| `destructive` | `--confirm-write --confirm-destructive` |
| `external-side-effect` | `--confirm-external` |

Missing confirmation fails before preflight or Bruno sends a request. After confirmation, run compatibility and QA-lock checks, preflight, the selected directory, evidence normalization, artifact-safety checks where evidence is retained, and strict reconciliation.

Run all modules with:

```bat
qa\execution\run.bat --all --confirm-write --confirm-destructive --confirm-external
```

Read [references/execution-evidence.md](references/execution-evidence.md) for evidence requirements and [references/version-management.md](references/version-management.md) for lock handling.

## Completion Gates

Global `status: verified` is valid only after an `--all` run proves:

- every endpoint has a success case;
- every applicable scenario has a linked case;
- every source candidate has one endpoint owner;
- every logic entry has case IDs;
- every case has exact assertions and passed execution evidence;
- every declared flow has ordered, passed evidence and verified cleanup;
- preflight passed;
- the offline OpenAPI, generation state, QA lock, and business version lock are current.

A module run reports `module_status: verified` and `module_completion_ok: true` when its scope passes. It must not write `version-lock.yaml`, global `generation-state.yaml`, global index completion, or global verified state.

Static inventory is never completion evidence. Without successful execution evidence, report `draft` or `runnable`, not `verified`. Keep raw Bruno reports outside the repository when they can contain secrets; before retaining artifacts, run:

```bash
python qa/scripts/check_artifact_safety.py qa/bruno qa/contracts
```

## Hard Rules

- Do not modify business code to make a test pass.
- Do not relax coverage, source mapping, exact assertion, flow, preflight, or lock checks.
- Do not guess credentials, authentication envelopes, permission behavior, business errors, file constraints, or destructive fixtures.
- Do not execute writes, destructive cases, or external side effects without their explicit confirmations.
- Do not update global completion or version locks from a module run.
- Do not ignore malformed JSON, invalid UTF-8, mojibake, duplicate IDs, duplicate case mappings, unregistered `.bru` files, or stale evidence.
- Keep cases deterministic, independently runnable where possible, and safe to rerun.

Use [references/cli.md](references/cli.md) for installation and project-script synchronization. Read [references/parallel-generation.md](references/parallel-generation.md) only when the user explicitly requests parallel agents or module delegation.
