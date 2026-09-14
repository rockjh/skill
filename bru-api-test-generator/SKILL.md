---
name: bru-api-test-generator
description: Generate and maintain auditable Bruno HTTP API tests inside a business-code repository from offline Swagger/OpenAPI files, optional source adapters, and environment configuration. Partition contracts by business module, cover declared normal/error logic, exact response assertions, explicit flows, and reconciliation; do not use for multi-step system E2E workflows.
---

# Bruno API Test Generator

Use this skill when the requested deliverable is a Bruno collection that lives with the business application and is checked in with the API implementation.

## Boundary

This skill owns single-endpoint API and contract coverage:

- HTTP method/path coverage from an offline OpenAPI/Swagger contract.
- authentication and authorization failures.
- request validation, boundary values, business errors, and response contracts for JSON, text, XML, form, multipart, and binary payloads.
- normal and error paths declared by controllers, services, validators, and exception handlers.
- explicitly declared ordered module flows such as create -> update -> query -> delete -> query.
- Bruno collection layout, environment variables, cleanup, execution, and coverage reconciliation.

Do not use Bruno tests as a substitute for multi-step business workflows, database-heavy assertions, browser journeys, or asynchronous system scenarios. Those belong in the project's Python E2E workflow.

## Adapter Boundaries

The contract model is HTTP-oriented and does not require a particular business
language or framework. OpenAPI/Swagger remains the required inventory input for
this skill. Bruno is the selected materialization and execution adapter; the
target service may use any language or framework supported by its HTTP
contract. Source scanners are optional evidence adapters, and must never be
treated as the only source of endpoint coverage.

## State Model And Version Gate

Generation and verification are separate gates. Preflight reports `static_ready`
(OpenAPI, manifests, Bruno syntax, and mappings), `context_ready` (authentication,
audit/tenant context, and fixtures for the selected scope), and `execution_ready`
(Bruno CLI, service URL, and route probes). Every run has one explicit state:
`draft` (static artifacts exist), `runnable` (all three readiness gates pass),
`verified` (all required cases and flows execute and pass), or `blocked`
(environment, authentication, fixture, contract, integrity, or assertion
failure). Static inventory is never completion evidence. The coverage checker
must emit both `static_ok` and `completion_ok`; without execution evidence it
emits `static_ok: true`, `completion_ok: false`, `status: draft`.

The Bruno collection is compatible with a reviewed business-code version. Keep
contracts/version-lock.yaml under version control with the collection. Run
scripts/check_version_compatibility.py in three phases:

    python qa/scripts/check_version_compatibility.py APP qa/contracts --phase before-generate
    python qa/scripts/check_version_compatibility.py APP qa/contracts --phase before-execute
    python qa/scripts/check_version_compatibility.py APP qa/contracts --phase complete --completion-report execution-evidence.coverage.json --write --tests-adapted

Initialize a new draft baseline with `scripts/check_version_compatibility.py APP qa/contracts --init`.
The lock records the commit and a digest of tracked files outside `qa/**`, so
QA-only changes in the same repository do not self-invalidate the business
compatibility. A completion write is refused unless the coverage report says
`status: verified` and `completion_ok: true`. When the locked version differs,
inspect the changed files and classify the change:

- API-impacting: controllers, request/response DTOs, services and business errors, validation, security/permissions, exception/serialization handling, OpenAPI/Swagger, relevant configuration, or database changes that alter observable behavior.
- non-API: documentation, comments, formatting, unrelated frontend/assets, or other changes proven not to affect an API contract or module flow.

For an API-impacting change, identify affected modules, update their endpoints/logic/cases/flows and Bruno files, run the affected collections and reconciliation, then update version-lock.yaml after the verified completion gate. For a non-API change, the source digest may remain current without rewriting Bruno files. Never silently carry an old compatibility SHA or digest. The checker recognizes common Java, Go, Python, Node/TypeScript, .NET, Rust, PHP, Kotlin, Ruby, Scala, Swift, Elixir, and Dart source/build files by default; tune `impact-rules.yaml` when a repository has a narrower API boundary. A source checkout without Git uses a filesystem source digest in draft mode and has no commit identity.

## Execution Model: Staged Module Parallelism

When the runtime supports sub-agents and the project has two or more independent modules, the coordinator must use a coordinator-plus-workers model and start one worker per independent module, bounded by available concurrency. The coordinator must complete the global discovery phase sequentially before starting workers:

1. locate or acquire and parse the offline OpenAPI/Swagger file;
2. inspect source mappings, security setup, shared fixtures, exception handling, and the current compatibility SHA;
3. assign every operation to exactly one module;
4. identify cross-module flows, shared mutable data, authentication/bootstrap dependencies, and modules that must remain sequential;
5. freeze the module map and worker ownership plan.

After that freeze, one worker owns each independent module. A worker may read the whole repository, but may write only its assigned module's `logic.yaml` and `cases.yaml`. Workers do not materialize `.bru` files and must not modify another module, `module-map.yaml`, `index.yaml`, `version-lock.yaml`, shared environments, business source code, or cross-module flows. The coordinator alone materializes `.bru` files and synchronizes the global index, so parallel and sequential generation use the same naming implementation.

The coordinator owns shared setup, cross-module journeys, global manifests, coverage reconciliation, final execution, and version-lock updates. Module generation may run in parallel, but each explicitly declared module flow remains ordered internally. Test execution may run in parallel only when credentials, database fixtures, tenants, and generated data are isolated; otherwise generate in parallel and execute sequentially. If sub-agents are unavailable or dependencies are not independent, fall back to the sequential module workflow without weakening any coverage rule. Read `references/parallel-generation.md` for the ownership and handoff protocol.

## Required Workflow

Work in explicit phases. Do not claim completion without execution evidence.

1. Discover the repository. Read the repository's AGENTS.md and existing test conventions. Inspect the build files, security configuration, global exception handling, controller mappings, validators, services, fixtures, and any existing Bruno collection. Preserve the repository's existing location; otherwise use a top-level qa/bruno (or equivalent) directory, never the application source package.
2. Acquire and parse one offline Swagger/OpenAPI file. Use the user-provided local path first; otherwise locate a checked-in swagger.json, openapi.json, swagger.yaml, or openapi.yaml. If no local file exists, inspect the repository's startup configuration/process information for its port and context path, then check whether the target application is already running and execute the bundled scripts/fetch_local_openapi.py from the business repository. Do not emit the missing-specification blocker before this fallback has been attempted. That helper probes detected TCP listeners (or an explicitly supplied loopback --base-url/--port) at common documentation paths, validates that the response is a Swagger/OpenAPI document with a paths object, and atomically saves it under the contracts root. A loopback download is an acquisition step only: after it succeeds, use the saved file for all parsing and reconciliation. Preserve the saved document SHA and provenance; if the application build SHA/PID/startup command cannot be established, mark provenance `contract_provenance_unverified`. Do not start the application automatically, send credentials, call a remote host, or invent an API from prose. If no local listener exists, a running local application does not expose a valid document, or multiple local documents are ambiguous, report the exact probe result as a blocker. Parse JSON with the standard library and YAML with an available YAML parser; resolve local `$ref` definitions before selecting request fields and concrete assertions. Generate a reviewable map with `scripts/parse_openapi.py SPEC --write-module-map qa/contracts/module-map.yaml`, then run `scripts/parse_openapi.py SPEC --module-map qa/contracts/module-map.yaml --output-dir qa/contracts/modules`. A Tag is the preferred owner, but an untagged operation may be assigned by explicit `operation_ids`, `path_prefixes`, a single `default: true` module, or a single-module map. An unresolved owner or unresolved multi-Tag primary owner is a blocker.
3. Compare implementation mappings. Compare each module's offline inventory with route/runtime mappings and record the union. Record deliberate exclusions with a reason. Use the repository's CodeGraph index first when a .codegraph directory exists; otherwise use the project's normal source search tools. Do not silently drop an operation that exists in only one source or silently assign an operation to multiple primary modules.
4. Inventory source logic and exceptions when source is available. Run scripts/analyze_source_logic.py on focused application roots (route handlers, validators, services, exception handlers, and direct domain modules). It supports common source extensions and reports candidates for review; run scripts/analyze_java_logic.py only as an optional deeper Java/Spring adapter when Java source is present. If source is unavailable, use contract-only mode and mark source-derived logic as unavailable rather than blocking the HTTP inventory. Review adapter candidates and actual source branches, then produce module-local logic.yaml mapping each observable normal or error path to a source symbol/condition, expected HTTP/application result, and one or more case IDs. Scanners are candidate generators, not proof of branch coverage. A declared path without a case is a coverage gap unless explicitly excluded with a reason.
5. Create module manifests before tests. Produce or update module-map.yaml, the single `execution/config.yaml`, a generated global index.yaml, a generated contracts/README.md, and one directory per module containing endpoints.yaml, logic.yaml, cases.yaml, flows.yaml, exclusions.yaml, and CASES.md. Keep Bruno environments outside the collection under `execution/environments/`; migrate only environments that already exist and do not fabricate deployment names. The contracts README must list every module, its business scope, included artifact types, endpoint inventory, and a link to its module document. Each module CASES.md must explain that module's business scope, owned files, endpoint inventory, and automated cases. Keep global IDs unique with a module prefix. Every endpoint records the original Tag list and one `primary_tag`; every case declares its endpoint_id, expected status, business code when present, risk classification, and concrete response assertions. Every reachable endpoint needs a distinct success case; an endpoint without one stays `pending` and is not valid coverage. A flow manifest declares the available operations and their order only when the module or endpoint is explicitly flow-required. In parallel mode, the coordinator freezes this map and assigns disjoint module write scopes before workers start. A problem in one module is module-local `blocked` state and must not prevent unrelated modules from producing reviewable drafts. An explicit user instruction to skip review is the exception.
   For each endpoint, also record a scenario decision for the applicable case-matrix categories (`success`, `authentication`, `authorization`, `validation`, `business_error`, `query`, `safety`, and `file`). Use `applicable: false` with a reason when a category does not apply; an omitted decision is a coverage gap. Do not use a single generic fixture for an object-valued query parameter: inspect the binding and emit flattened query fields, a supported serialized representation, or an explicit exclusion. An endpoint is flow-required only when it or its module declares `flow_required: true` or a non-empty `flow_kind`; HTTP method alone never implies CRUD.
6. Generate module manifests in small batches or independent workers, then let the coordinator materialize Bruno. `parse_openapi.py --seed-cases` may add only OpenAPI-provable success, missing-required-field, invalid-enum, pagination-boundary, and missing-upload-file drafts; every seed is `review_required: true`, existing cases are preserved, and business errors are never invented. Workers edit only their module's `logic.yaml` and `cases.yaml`. The coordinator runs `materialize_missing_bru.py`; `--module` is for isolated module work and never refreshes the shared index, while `--sync-index` is a separate coordinator action. Bruno files live under `qa/bruno/<OpenAPI Tag>/`; multi-Tag operations require `primary_tag`. Apply the naming rules below and block when `case.title` is absent or not Chinese. Keep the stable English case ID only in `meta.name`. Use a module-level case table and one shared Mermaid sequence diagram; retain case-level Mermaid only for `flow_required` and cross-module flows.
7. Implement module flows. Only endpoints or modules explicitly marked `flow_required: true` (or with a declared `flow_kind`) require an ordered flow. Execute the declared order (normally create -> update -> query -> delete -> query), capture IDs and other values from earlier responses, and pass them to later requests. `POST /login`, search, calculation, command, webhook, and event endpoints do not require CRUD merely because of their method. If a flow-required module lacks an operation, record the missing capability and reason; never invent an endpoint. A flow is additional to, not a replacement for, isolated authentication, validation, and error cases. Cross-module flows are coordinated after module workers complete.
8. Configure safely. `qa/execution/config.yaml` is the only shared runtime configuration. It selects one external Bruno environment and one of `none`, `seres-sign`, `bearer`, `api-key`, or `cookie`; OAuth2 bootstrap remains project-owned and publishes a token consumed as `bearer`. Declare audit, tenant, internal-user, and other common Headers under path-scoped `custom_headers`, using either an environment variable or a non-sensitive fixed value. The collection-level `qa/bruno/collection.bru` injects authentication and common Headers at runtime, so generated requests never duplicate that script. Request-local Headers win; a case that must omit configured Headers declares `request.omit_common_headers`. SERES signing always uses SHA-256, the fixed timestamp rule, and the fixed `sign`, `timestamp`, and `accesskey` Header names. Never commit real secrets or hard-code a developer's URL. Classify every case as `read-only`, `isolated-write`, `destructive`, or `external-side-effect`; require explicit confirmation that the target permits writes before running a collection containing write or external-side-effect cases. Run the static coverage checker with the offline OpenAPI first, then pass that report through `runtime_preflight.py --static-results` together with `--openapi`, `--execution-config`, the active `--env-file`, and at least one representative route probe. A missing runtime may leave `static_ready: true` and status `draft`; `mode: none` never implies that execution is ready.
   Add an execution preflight for every credential or one-time fixture (captcha, CSRF token, signed URL, upload file, tenant). The preflight must fail with an actionable message when the fixture is missing or expired; it must not silently reuse a stale token. Keep raw Bruno reports outside the repository when they can contain credentials, and run the bundled `scripts/check_artifact_safety.py qa/bruno qa/contracts` (or an equivalent reviewed check) before committing evidence; it rejects JWTs, private keys, and URLs with credentials.
9. Assert exact observable values. Each case must declare an expected HTTP status and assert the response contract using the payload kind declared by the case: JSON paths, raw text/XML/body content, response headers, cookies, binary body content, type, length, range, nullability, and array element type as applicable. An application business code is asserted only when the manifest declares it, and its path may be configured with `business_code_path` instead of assuming `res.body.code`. For success, compare returned values with submitted values or captured IDs; for errors, assert the expected error code and relevant message/fields; for deletion, assert the record is absent. Capture dynamic response values with `captures` or assertion `capture_as`, then compare them in later cases with `equals_variable`. A test containing only status assertions is incomplete.
10. Run and reconcile. The coordinator synchronizes the index, runs Bruno, normalizes reports, and performs the final coverage check. A pass requires every assertion/test observation to pass, not only Bruno's request status. The checker classifies only files with `meta.type: http` and an HTTP request block as cases; environment, folder, and collection configuration are excluded. It reconciles IDs, Tag ownership, filenames, `meta.name`, method, URL path, query, body type, key request fields, precise assertions, module artifacts, shared module documentation, flows, UTF-8 integrity, and execution evidence. `exists` alone is auxiliary and cannot satisfy precise assertions after review. Preserve failure output and advance version-lock.yaml only after verified coordinator reconciliation.
   The coverage command must reconcile against the saved offline OpenAPI document (for example, `--openapi qa/contracts/openapi.json`), not just compare manifests with themselves. When execution evidence is supplied, the checker requires `--openapi`, `--require-scenarios`, `--require-auth`, `--execution-config`, and a passed `--preflight-results`; omitting one keeps the run blocked instead of verified. Run strict matrix validation (`--require-scenarios`) for the completion gate. It must reject duplicate case/endpoint IDs, duplicate case-to-file mappings, unregistered `.bru` files, cases pointing at unknown endpoints, cases without an endpoint or expected status, and logic entries without linked case IDs. A normalized report is valid only when every required case and every declared flow has passed execution evidence; module-level reports must be reconciled independently rather than treating one module's evidence as global evidence. Users execute through `qa/execution/run.bat` or `run.sh`; `run_bruno.py` is the shared internal implementation. A module-scoped run reports only that module and never updates `version-lock.yaml` or global completion state.

## Bruno File Naming

- Every business-request `.bru` filename must use its Chinese `case.title` after sanitizing filename-illegal characters.
- Use `两位及以上序号-中文 case.title.bru` and enforce `^\d{2,}-`, for example `01-查询车辆流量信息成功.bru`. Necessary business and protocol abbreviations may remain inside the Chinese title.
- Keep the stable English case ID only in manifests, execution evidence, reconciliation documents, and the request's `meta.name`. Never use that ID directly as a business-request filename.
- Explicit `bru`, `bru_file`, and `file_name` values must obey the same rule. The manifest path must exactly match the actual file.
- Number cases by their stable order in the module's `cases.yaml`; honor an existing explicit sequence. Repeated generation must preserve an existing compliant path and must not renumber it because unrelated cases changed.
- Do not fall back to Tag, endpoint summary, stable ID, or a hash. Remove filename-illegal characters `\/:*?"<>|`; if `case.title` is absent or contains no Chinese, report a blocker.
- Treat a duplicate computed path as a blocking collision and require distinct scenario titles. Do not add random or hash suffixes.
- This rule applies only to registered business requests. It does not apply to `environments/*.bru`, `collection.bru`, or other non-request collection configuration.

## Module Layout

The module layout is mandatory for more than one small domain. Module manifests are the source of truth; index.yaml is generated and is the only global summary.

qa/
  bruno/
    bruno.json
    collection.bru
    system-user/
    system-role/
  contracts/
    README.md
    module-map.yaml
    security-profile.yaml
    index.yaml
    version-lock.yaml
    impact-rules.yaml
    modules/
      system-user/
        endpoints.yaml
        parameters.yaml
        definitions.yaml
        responses.yaml
        logic.yaml
        cases.yaml
        flows.yaml
        exclusions.yaml
        CASES.md
      system-role/
        endpoints.yaml
        logic.yaml
        cases.yaml
        flows.yaml
        exclusions.yaml
        CASES.md
    flows/
      cross-module.yaml
  execution/
    config.yaml
    environments/
      local.bru
    run.bat
    run.sh
    README.md
  scripts/
    execution_config.py
    run_bruno.py

Partition rule uses the OpenAPI Tag produced by Java `@Tag`/`@Tags`, or the equivalent module marker in another framework. Each unique Tag has exactly one stable ASCII `id` and one preserved Tag directory. An operation with multiple Tags requires an explicit `primary_tag` mapping. An untagged operation requires an explicit `operation_ids`/`path_prefixes` match, a single `default: true` module, or a single-module map. Cross-module business journeys belong in contracts/flows/cross-module.yaml, not in an arbitrary module.

## Hard Rules

- Do not modify business code to make a Bruno test pass.
- Do not use only HTTP status or only a business error code as the assertion.
- Do not group operations by URL prefix when `operation.tags` exists; every tagged operation has exactly one Tag owner.
- Do not accept an untagged operation without an explicit fallback owner, a Tag assigned to multiple modules, a missing Tag mapping, or a multi-Tag operation without an explicit `primary_tag`.
- Do not let `endpoints.yaml`, `cases.yaml`, `logic.yaml`, or `.bru` files cross their owning Tag module; cross-Tag flows belong only in `contracts/flows/cross-module.yaml`.
- Do not guess authentication headers or fixed tokens when OpenAPI security is empty or ambiguous; write `security-profile.yaml`, use environment variables, and let the preflight establish the real baseline.
- Do not treat endpoint inventory as endpoint coverage: report inventory, cases, execution, and passed counts separately, and require one success case for every reachable endpoint.
- Do not treat a static checker result as completion; without execution evidence the status is `draft`, not `ok`.
- Do not ignore text corruption: reject invalid UTF-8, U+FFFD, and common mojibake in `.yaml`, `.md`, and `.bru` with file and line diagnostics.
- Do not access a remote Swagger/OpenAPI endpoint. The only permitted network acquisition is the bundled helper probing the target application's loopback listener; once saved, the downloaded file is the offline contract.
- Do not mark source-declared normal/error paths covered without a linked case ID.
- Do not run explicitly flow-required steps in an implicit or filesystem-dependent order.
- Do not keep all modules in one giant manifest; module manifests are the source of truth and index.yaml is generated.
- Do not run or publish a collection with a stale business-code compatibility SHA.
- Do not update the compatibility SHA after an API-impacting change until the affected module tests have been adapted and executed.
- In parallel mode, do not let workers write shared manifests, environments, version locks, cross-module flows, or another worker's module.
- Do not execute modules in parallel when they share mutable fixtures or test data unless isolation is demonstrated; parallel generation does not imply parallel execution.
- Do not assign an endpoint to multiple primary modules or leave it unassigned without an exclusion reason.
- Do not treat a static flow manifest as proof that the flow actually ran in order; require execution evidence. For a create flow, require a passed delete plus a passed post-delete absence assertion, unless the flow carries a documented cleanup exclusion with an owner and reset procedure.
- Do not treat a Python workflow test as endpoint coverage unless it is explicitly registered for that case ID.
- Do not hide environment failures, authentication failures, or flaky behavior with blind retries.
- Do not maintain authentication or common Header logic in individual requests; the collection-level runtime script is the single implementation.
- Do not treat module-scoped execution as global completion or update `version-lock.yaml` from it.
- Do not batch-template authentication expectations before one representative no-token and invalid-token probe establishes the actual transport status and error envelope.
- Do not use a pending exclusion to pass completion; only an approved exclusion with a reason (and cleanup/reset plan for flow exclusions) may remove work from the completion set.
- Do not allow duplicate cases with the same endpoint, scenario, request, and assertions even when their IDs differ.
- Do not leave a module undocumented or a case without a title and short business description. Use one shared module sequence diagram; require a case-level diagram only for flow-required or cross-module flows.
- Do not create or accept a registered business request whose filename lacks a two-or-more-digit sequence and a Chinese `case.title`. Do not use Tag, summary, stable ID, or hash fallback, including through `bru`, `bru_file`, or `file_name`.
- Do not treat `environments/*.bru` or `collection.bru` as business requests when enforcing the business filename rule.
- Keep API tests deterministic, independently runnable, and safe to rerun. Do not retain stale execution reports or reports containing secrets as if they were current evidence; name evidence with the business SHA/environment and replace it atomically after a run.

## Completion

Tag completion also requires isolated module parameters, definitions, and
responses, an up-to-date module CASES.md whose documented case IDs exactly
match cases.yaml, a valid `execution/config.yaml`, and a parseable collection-level
runtime script. Global completion also requires contracts/README.md to describe
every module's business scope and included content.

The task is complete only when every module's required execution scope passes, the global coverage check reconciles the saved offline OpenAPI document with no unexplained endpoint/logic/case/flow difference, every registered request uses `qa/bruno/<OpenAPI Tag>/<NN>-<中文 case.title>.bru`, its manifest path and `meta.name` agree with the actual file and stable English case ID, and no fallback-named, stale, or duplicate request remains. Static reconciliation must classify HTTP requests separately from environment/folder/collection files and compare method, URL path, query, body type, and key request fields. Exempt configuration files remain unaffected; precise assertions, flow cleanup, secret-free evidence, and the version gate remain required. Parallel and sequential generation must produce identical paths, a second generation must leave Git unchanged, and Python tooling must run as UTF-8 without bytecode cache artifacts. A missing runtime may still deliver `static_ready: true`, `context_ready: false` or `execution_ready: false`, and `status: draft`; it may not claim `verified`.

Read references/offline-swagger.md when locating or partitioning the local specification, references/case-matrix.md when selecting applicable API scenarios, references/coverage-manifest.md when designing isolated module manifests, references/execution-config.md when configuring environments, authentication, or common Headers, references/execution-evidence.md when wiring Bruno results into CI, and references/parallel-generation.md when coordinating module workers.
