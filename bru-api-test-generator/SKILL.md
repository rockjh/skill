---
name: bru-api-test-generator
description: Generate and maintain auditable Bruno API tests inside a business-code repository from offline Swagger/OpenAPI files and source code. Partition contracts by business module, cover declared normal/error logic, exact response assertions, ordered CRUD flows, and reconciliation; do not use for multi-step system E2E workflows.
---

# Bruno API Test Generator

Use this skill when the requested deliverable is a Bruno collection that lives with the business application and is checked in with the API implementation.

## Boundary

This skill owns single-endpoint API and contract coverage:

- HTTP method/path coverage from an offline OpenAPI/Swagger contract.
- authentication and authorization failures.
- request validation, boundary values, business errors, and response contracts.
- normal and error paths declared by controllers, services, validators, and exception handlers.
- ordered module flows such as create -> update -> query -> delete -> query when those operations exist.
- Bruno collection layout, environment variables, cleanup, execution, and coverage reconciliation.

Do not use Bruno tests as a substitute for multi-step business workflows, database-heavy assertions, browser journeys, or asynchronous system scenarios. Those belong in the project's Python E2E workflow.

## Version Gate

The Bruno collection is compatible with a specific business-code Git commit. Keep contracts/version-lock.yaml under version control with the collection. Before generating or declaring completion, run scripts/check_version_compatibility.py against the business repository's current HEAD. When the locked SHA differs, inspect the changed files and classify the change:

- API-impacting: controllers, request/response DTOs, services and business errors, validation, security/permissions, exception/serialization handling, OpenAPI/Swagger, relevant configuration, or database changes that alter observable behavior.
- non-API: documentation, comments, formatting, unrelated frontend/assets, or other changes proven not to affect an API contract or module flow.

For an API-impacting change, identify affected modules, update their endpoints/logic/cases/flows and Bruno files, run the affected collections and reconciliation, then update version-lock.yaml to the new business SHA in the same change. For a non-API change, tests do not need script edits, but version-lock.yaml must still be advanced to the new business SHA after the impact review. Never silently carry an old compatibility SHA.

## Execution Model: Staged Module Parallelism

When the runtime supports sub-agents and the project has two or more independent modules, the coordinator must use a coordinator-plus-workers model and start one worker per independent module, bounded by available concurrency. The coordinator must complete the global discovery phase sequentially before starting workers:

1. locate or acquire and parse the offline OpenAPI/Swagger file;
2. inspect source mappings, security setup, shared fixtures, exception handling, and the current compatibility SHA;
3. assign every operation to exactly one module;
4. identify cross-module flows, shared mutable data, authentication/bootstrap dependencies, and modules that must remain sequential;
5. freeze the module map and worker ownership plan.

After that freeze, one worker owns each independent module. A worker may read the whole repository, but may write only its assigned `qa/contracts/modules/<module>/` files and `qa/bruno/<module>/` files. Workers must not modify another module, `module-map.yaml`, `index.yaml`, `version-lock.yaml`, shared environments, business source code, or cross-module flows. Workers report generated IDs, test results, blockers, and changed files back to the coordinator.

The coordinator owns shared setup, cross-module journeys, global manifests, coverage reconciliation, final execution, and version-lock updates. Module generation may run in parallel, but each module's CRUD flow remains ordered internally. Test execution may run in parallel only when credentials, database fixtures, tenants, and generated data are isolated; otherwise generate in parallel and execute sequentially. If sub-agents are unavailable or dependencies are not independent, fall back to the sequential module workflow without weakening any coverage rule. Read `references/parallel-generation.md` for the ownership and handoff protocol.

## Required Workflow

Work in explicit phases. Do not claim completion without execution evidence.

1. Discover the repository. Read the repository's AGENTS.md and existing test conventions. Inspect the build files, security configuration, global exception handling, controller mappings, validators, services, fixtures, and any existing Bruno collection. Preserve the repository's existing location; otherwise use a top-level qa/bruno (or equivalent) directory, never the application source package.
2. Acquire and parse one offline Swagger/OpenAPI file. Use the user-provided local path first; otherwise locate a checked-in swagger.json, openapi.json, swagger.yaml, or openapi.yaml. If no local file exists, inspect the repository's startup configuration/process information for its port and context path, then check whether the target application is already running and execute the bundled scripts/fetch_local_openapi.py from the business repository. Do not emit the missing-specification blocker before this fallback has been attempted. That helper probes detected TCP listeners (or an explicitly supplied loopback --base-url/--port) at common documentation paths, validates that the response is a Swagger/OpenAPI document with a paths object, and atomically saves it under the contracts root. A loopback download is an acquisition step only: after it succeeds, use the saved file for all parsing and reconciliation. Do not start the application automatically, send credentials, call a remote host, or invent an API from prose. If no local listener exists, a running local application does not expose a valid document, or multiple local documents are ambiguous, report the exact probe result as a blocker. Parse JSON with the standard library and YAML with an available YAML parser. Review or create module-map.yaml, then partition every operation into a business module by Swagger tag, path prefix, or controller mapping. Use scripts/parse_openapi.py --module-map ... --output-dir .... A parse failure or unassigned operation is a blocker unless the operation is explicitly excluded with a reason.
3. Compare implementation mappings. Compare each module's offline inventory with controller/runtime mappings and record the union. Record deliberate exclusions with a reason. Use the repository's CodeGraph index first when a .codegraph directory exists; otherwise use the project's normal source search tools. Do not silently drop an operation that exists in only one source or silently assign an operation to multiple primary modules.
4. Inventory source logic and exceptions. Run scripts/analyze_java_logic.py on focused application roots (controllers, services, validators, exception handlers, and their direct domain modules), not the whole repository's utility/generated code. Review its candidates and inspect controller branches, Bean Validation annotations, service normal paths, duplicate/not-found/state/permission branches, @ControllerAdvice and @ExceptionHandler mappings, custom exceptions, and application error-code builders. Produce module-local logic.yaml mapping each observable normal or error path to a source symbol/condition, expected HTTP/application result, and one or more case IDs. The scanner is a candidate generator, not proof of branch coverage. A declared path without a case is a coverage gap unless explicitly excluded with a reason.
5. Create module manifests before tests. Produce or update module-map.yaml, a generated global index.yaml, and one directory per module containing endpoints.yaml, logic.yaml, cases.yaml, flows.yaml, exclusions.yaml, and CASES.md. Keep global IDs unique with a module prefix. Every case declares its endpoint_id, expected status, business code when present, and concrete response assertions. Every module flow declares the available CRUD operations and their order. In parallel mode, the coordinator freezes this map and assigns disjoint module write scopes before workers start. If no approved module map or manifest exists, stop after presenting it for review instead of silently generating a large collection. An explicit user instruction to skip review is the exception.
   For each endpoint, also record a scenario decision for the applicable case-matrix categories (`success`, `authentication`, `authorization`, `validation`, `business_error`, `query`, `safety`, and `file`). Use `applicable: false` with a reason when a category does not apply; an omitted decision is a coverage gap. Do not use a single generic fixture for an object-valued query parameter: inspect the framework binding and emit flattened query fields, a supported serialized representation, or an explicit exclusion.
6. Generate in small batches or independent module workers. Use one worker per independent module only after the coordinator has completed the global phase. A worker must inspect the actual Bruno syntax in the collection before writing new files; do not invent syntax from memory. Keep one meaningful scenario per .bru file and make its case ID discoverable from the file name or supported metadata. Use deterministic numeric ordering for flow steps, for example 01-create, 02-update, 03-query, 04-delete, 05-query-deleted. Modules with cross-module dependencies remain coordinator-owned or sequential.
7. Implement module flows. For every module with the relevant operations, execute the declared order (normally create -> update -> query -> delete -> query), capture IDs and other values from earlier responses, and pass them to later requests. If a module lacks part of CRUD, execute the applicable subsequence and record the missing operation and reason; never invent an endpoint. A flow is additional to, not a replacement for, isolated authentication, validation, and error cases. Cross-module flows are coordinated after module workers complete.
8. Configure safely. Base URL, credentials, tokens, tenant IDs, and secrets come from Bruno environments or scripts. Never commit real secrets or hard-code a developer's URL. Reuse the repository's login/token setup. Use unique test data and register cleanup immediately after each write so cleanup still runs after a later failure.
   Add an execution preflight for every credential or one-time fixture (captcha, CSRF token, signed URL, upload file, tenant). The preflight must fail with an actionable message when the fixture is missing or expired; it must not silently reuse a stale token. Keep raw Bruno reports outside the repository when they can contain credentials, and run the bundled `scripts/check_artifact_safety.py qa/bruno qa/contracts` (or an equivalent reviewed check) before committing evidence; it rejects JWTs, private keys, and URLs with credentials.
9. Assert exact observable values. Each case must assert HTTP status, the RuoYi/application business code when present, response shape, and concrete field values or relations. For success, compare returned values with submitted values or captured IDs; for errors, assert the expected error code and relevant message/fields; for deletion, assert the record is absent. Dynamic values may be captured, but must be compared later. A test containing only status and business-code assertions is incomplete.
10. Run and reconcile. Workers may run their assigned module collection and produce normalized evidence, but the coordinator must regenerate or verify the global index, run the repository's Bruno CLI command and environment, and perform the final checks. Copy or adapt scripts/check_api_coverage.py into the business repository's qa/scripts (or use an equivalent reviewed script). Run it against the contracts root so it iterates every module and validates the global index.yaml. It compares stable endpoint, logic, case, and flow IDs to module .bru files and actual execution results. Use scripts/validate_flow_execution.py with the execution-evidence JSON for each module or cross-module flow to verify declared order, captured variables, dependent values, and cleanup. The checks must report missing, unregistered, out-of-order, assertion-light, excluded-without-reason, and failed items. URL string grep alone is insufficient. Preserve failure output and do not mark a case complete when it was not executed. Only after this coordinator-owned reconciliation may version-lock.yaml be advanced.
   The coverage command must reconcile against the saved offline OpenAPI document (for example, `--openapi qa/contracts/openapi.json`), not just compare manifests with themselves. Run strict matrix validation (`--require-scenarios`) for the completion gate. It must reject duplicate case/endpoint IDs, duplicate case-to-file mappings, unregistered `.bru` files, cases pointing at unknown endpoints, and logic entries without linked case IDs. A normalized report is valid only when every required case is executed and passed; module-level reports must be reconciled independently rather than treating one module's evidence as global evidence.

## Module Layout

The module layout is mandatory for more than one small domain. Module manifests are the source of truth; index.yaml is generated and is the only global summary.

qa/
  bruno/
    system-user/
    system-role/
  contracts/
    module-map.yaml
    index.yaml
    version-lock.yaml
    impact-rules.yaml
    modules/
      system-user/
        endpoints.yaml
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

Partition priority is Swagger tag, path prefix, controller mapping, then a reviewed manual mapping. Every operation has one primary module. Cross-module business journeys belong in contracts/flows/cross-module.yaml, not in an arbitrary module.

## Hard Rules

- Do not modify business code to make a Bruno test pass.
- Do not use only HTTP status or only a business error code as the assertion.
- Do not access a remote Swagger/OpenAPI endpoint. The only permitted network acquisition is the bundled helper probing the target application's loopback listener; once saved, the downloaded file is the offline contract.
- Do not mark source-declared normal/error paths covered without a linked case ID.
- Do not run module CRUD steps in an implicit or filesystem-dependent order.
- Do not keep all modules in one giant manifest; module manifests are the source of truth and index.yaml is generated.
- Do not run or publish a collection with a stale business-code compatibility SHA.
- Do not update the compatibility SHA after an API-impacting change until the affected module tests have been adapted and executed.
- In parallel mode, do not let workers write shared manifests, environments, version locks, cross-module flows, or another worker's module.
- Do not execute modules in parallel when they share mutable fixtures or test data unless isolation is demonstrated; parallel generation does not imply parallel execution.
- Do not assign an endpoint to multiple primary modules or leave it unassigned without an exclusion reason.
- Do not treat a static flow manifest as proof that the flow actually ran in order; require execution evidence. For a create flow, require a passed delete plus a passed post-delete absence assertion, unless the flow carries a documented cleanup exclusion with an owner and reset procedure.
- Do not treat a Python workflow test as endpoint coverage unless it is explicitly registered for that case ID.
- Do not hide environment failures, authentication failures, or flaky behavior with blind retries.
- Keep API tests deterministic, independently runnable, and safe to rerun. Do not retain stale execution reports or reports containing secrets as if they were current evidence; name evidence with the business SHA/environment and replace it atomically after a run.

## Completion

The task is complete only when every module's collection executes in the intended test environment with every required case passed, the global coverage check reconciles the saved offline OpenAPI document with no unexplained endpoint/logic/case/flow difference, every endpoint has a complete scenario decision (or a reasoned exclusion), all exclusions and incomplete CRUD modules have reasons, exact response assertions are present in both the manifest and the Bruno file, flow cleanup is evidenced, checked-in evidence is secret-free and tied to the current business SHA/environment, version-lock.yaml matches the business repository HEAD, and the response includes the exact commands and results. Static counts, a parser-only run, or a partial token-injected collection are not completion evidence. If the offline specification is unavailable after checking the repository and attempting loopback acquisition from an already-running application, or if source code, module map, version lock, environment, or required fixtures are unavailable, report the blocker and leave a reviewable manifest; do not claim that tests are complete.

Read references/offline-swagger.md when locating or partitioning the local specification, references/case-matrix.md when selecting applicable API scenarios, references/coverage-manifest.md when designing module manifests and the global index, references/execution-evidence.md when wiring Bruno results into CI, and references/parallel-generation.md when coordinating module workers.
