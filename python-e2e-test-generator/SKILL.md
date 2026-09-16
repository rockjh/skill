---
name: python-e2e-test-generator
description: Discover multi-repository business flows and generate source-tracked Python/pytest E2E scenarios with runtime probes, control-capability gates, safe cleanup, and real execution evidence. Use for cross-service E2E generation or maintenance from application source and local configuration; do not use for exhaustive single-endpoint coverage.
---

# Python E2E Test Generator

Build an independent black-box E2E project from the behavior and configuration of the application workspace. The generated tests may observe deployed services, messages, databases, caches, schedulers, and configuration systems, but must never modify application source to make a test pass.

## Mandatory stage order

Execute these stages in order and do not combine their outcomes:

1. Inventory the whole workspace: Git repositories, build roots, modules, and existing E2E projects.
2. Build the relevant dependency topology from build descriptors and source evidence.
3. Discover initial configuration, its sources, profiles, local overrides, and precedence.
4. If the user says the applications are running, perform read-only local runtime probing and associate processes/listeners with topology nodes. Otherwise record this stage as `not_requested`.
5. Complete the control-capability matrix for every requested scenario.
6. Split scenario ownership and, for a multi-scenario request, assign one scenario to each independent subagent.
7. Let the main agent integrate shared configuration, clients, fixtures, assertions, and validators, then verify scenario data and cleanup isolation.
8. Run environment-independent tests, static validation, source-version checks, and `pytest --collect-only`.
9. Run read-only endpoint smoke checks when a runtime environment is available.
10. Run real business scenarios only after isolation, side effects, and recovery have passed preflight.
11. Restore controlled SQL changes, mutable configuration, and test data, then verify restoration.
12. Produce the final evidence report.

The topology gate must pass before any scenario test code is written. The control-matrix and ownership gates must pass before scenario implementation. Static success never authorizes runtime side effects.

For a completed local probe, record the observed PID, non-secret command reference, local host, port, node association, and credential-free read target. The ordered runtime gate must revalidate the command, local listener ownership by that PID, bounded connectivity, and non-5xx HTTP result; a filled YAML record alone is not probe evidence.

Read [references/discovery-and-control-policy.md](references/discovery-and-control-policy.md) before planning or generating any scenario. It defines the workspace contract, runtime probing, scenario ownership, status rules, control matrix, SQL safety, and mandatory gate behavior.

## Source and contract rules

- Keep a temporary trace from user expectation to public entrypoint, request/response or message model, state transition, correlation key, observable evidence, and cleanup.
- Treat the user's expected semantics as an input contract. Report source discrepancies; never rewrite expectations merely to agree with current behavior.
- Record concise source anchors for every assertion, control, and cleanup action.
- Use discovered identifiers and contracts. Never embed a project name, endpoint path, table, topic, source configuration key, domain enum, business state, or provider-specific assumption from an example in this skill.
- Do not treat a source default as a runtime value. Record the final source, exact source key, and owner-specific override chain. Independent modules do not override one another. File values must be parsed and matched at the recorded commit; record credential locations or references, never credential values.
- Bind every source anchor to its most specific owning module. A topology edge is valid only when its evidence belongs to the declared caller/producer module, and every source-confirmed capability owner has its own matching edge and owner/type-specific configuration record.
- Complete code generation when only environment values are missing. Runtime resolution remains lazy so collection does not need endpoints, brokers, credentials, or test data.

Status meanings are strict:

- `ready`: source contract, safe control path, runtime configuration, and isolated test data are confirmed.
- `pending_environment`: only an address, credential, middleware connection, or environment-specific test datum is missing. Per-run control authorization is an execution gate and never changes the static scenario status.
- `contract_blocked`: the complete control matrix proves that API, test/admin controls, mocks or fault injection, dynamic configuration, jobs, messages, and safe database controls cannot establish the required contract or state.

A runtime request failure is a test failure. It is never `pending_environment`, `contract_blocked`, or static success.

## Scenario ownership

Each `scenarios/<中文业务名称>/` owns one business journey:

```text
场景定义.yaml
业务数据.json
业务流程图.md
test_<中文业务名称>.py
```

Add `自动化测试流程图.md`, `步骤.py`, `断言.py`, or `清理.py` only when they materially improve a non-trivial scenario. Do not create empty placeholders, a narrative scenario file, a version-history file, or a global scenario manifest.

The main agent owns workspace discovery, topology, shared configuration, common clients, fixtures, repositories, assertions, validators, isolation review, and the final report. In a multi-scenario request, each subagent owns exactly one scenario directory and only that directory. It sends common-module change requests to the main agent instead of editing common files. If delegation is unavailable, the main agent processes scenarios sequentially with isolated context and reports the degradation; it must not claim subagent execution.

Read [references/e2e-workflow.md](references/e2e-workflow.md) for the scenario schema, validation gates, and result report. Read [references/scenario-artifact-policy.md](references/scenario-artifact-policy.md) before changing scenario artifacts.

## Generated project boundaries

Use the following shape and omit unused modules:

```text
<e2e-project>/
  pyproject.toml
  AGENTS.md
  E2E_PLAN.md
  discovery/
    workspace.yaml
  config/
    config.yaml
    environments/
      <environment>.yaml
  common/
    clients/
    builders/
    repositories/
    controls/
    assertions/
    fixtures/
    integrations/
  scenarios/
    <中文业务名称>/
  tests/
    test_e2e_guard.py
    test_e2e_runtime.py
    runtime/
  scripts/
    check_scenarios.py
    check_source_versions.py
    run_e2e.py
    run.sh
    run.bat
```

Before scenario implementation, install this skill's versioned gate assets with `scripts/install_e2e_gates.py <e2e-project>`. The installer provides one unified launcher pair; do not generate per-scenario scripts. Run `scripts/run.sh` or `scripts/run.bat` with no scenario to run all scenarios, or pass `--scenario <中文场景名称>` to select one. Each launcher contains usage comments. The installer refuses to overwrite drifted gate files unless `--force` is explicitly supplied, and `--check` verifies the SHA-256 manifest.

The generated `pyproject.toml` must declare `pytest` and `PyYAML` using versions compatible with the target workspace, and register `business_e2e`, `scenario_id`, and `read_only_smoke`. The static gate rejects a missing declaration; do not install or upgrade dependencies silently.

`ready` is checked against the active environment: every required service/component mapping and every exact environment placeholder used by that scenario must resolve in the current process. Missing values require `pending_environment` with a typed `config:`, `credential:`, `connection:`, or `business_data:` blocker. Runtime control authorization remains separate and never changes this status.

- `discovery/workspace.yaml` is the workspace and runtime-discovery fact source, not a scenario list.
- `场景定义.yaml` is the only scenario contract and contains its control matrix, ownership, status, source anchors, and cleanup contract.
- `业务数据.json` contains environment-isolated inputs; it contains no connections, credentials, mutable results, or cross-environment fallback. Inspect source DTOs, validation rules, enums, and field semantics before choosing data. Keep source-valid literals and constructible synthetic values in the scenario instead of injecting every leaf through environment variables; reserve exact placeholders for values that genuinely belong to the selected environment. Generate unique random strings at runtime with `secrets` or `uuid`, never as fixed shared IDs or environment variables.
- `common/repositories/` owns parameterized read-only observation queries. `common/controls/` may contain authorized, reversible database or configuration controls.
- Scenario modules own scenario-specific orchestration, assertions, and cleanup. The main agent alone promotes genuinely reused code into `common/`.
- Every generated Python module, class, and function has a concise Chinese docstring. Comments explain only business branches, bounded waits, safety controls, and cleanup.

Read [references/fixture-policy.md](references/fixture-policy.md) for configuration, lazy preflight, isolation, and cleanup. Read [references/integration-policy.md](references/integration-policy.md) for HTTP, RPC, messages, data stores, caches, schedulers, and other observers. Read [references/execution-script-policy.md](references/execution-script-policy.md) for ordered validation and execution scripts. Apply [references/version-sync-policy.md](references/version-sync-policy.md) before regenerating an existing scenario.

## Runtime invariants

- Prefer public business APIs. Use test/admin endpoints, mocks, dynamic configuration, jobs, messages, or controlled SQL only when source and safety evidence justify them.
- Before any write, assert test-data ownership, exact correlation, allowed target environment, expected cleanup, and recovery path.
- Dangerous test/admin, mock/fault, dynamic-configuration, job, message-publish, and database controls require an explicit per-run authorization reference bound to the active test environment. Committed switches remain false.
- Validate transport and business responses before polling downstream evidence.
- Emit endpoint/control evidence only inside the common adapter that made the real call. Bind write evidence to an identity-bearing resource or payload argument, and derive endpoint status, summary, and verification from the returned object in the same straight-line block.
- Call `record_business_entry` immediately after successful preflight and before any setup, mutation, or business call; both calls bind to the current scenario directory literal. Project-owned root modules and `conftest.py` are subject to the same static rules; do not define pytest hooks, plugin lists, ignore lists, or pytest configuration that changes selection, collection, or exit status.
- Evidence variables exist only in the isolated smoke/business subprocess. Each scenario is credited only with event files newly created by its own invocation; sibling or earlier evidence cannot satisfy it.
- Every `record_control` event declares `side_effect: read|write`, and the runner compares the exact `(control, action, side_effect)` tuple with the scenario step. Dangerous controls remain writes for static validation and per-run authorization even when generated code claims otherwise.
- Subscribe or capture offsets before an asynchronous action. Poll with bounded monotonic deadlines and useful last-state diagnostics; do not use blind sleeps.
- Prove business transitions through direct state, logs, messages, or persistence evidence. Finding a resource alone proves identity, not the transition.
- Register idempotent cleanup immediately after acquiring each owned resource. Preserve the original test exception when cleanup also fails, and disclose cleanup failure separately.
- Derive restoration resource identities from the validated scenario contract; never accept caller-supplied evidence labels.
- Every `mutable_controls` identity also appears in `owned_resources` with cleanup, restore, and verification symbols. Scenario source commits equal the discovery snapshot and cover every relevant topology repository.
- Never use unbounded or dynamically built SQL, `OR`/tautological/fuzzy selectors, production or protected-environment writes, cache-wide clearing, unrestricted business-topic publication, or irreversible configuration mutation.
- Treat conventional `prod`, `production`, `prd`, and `live` environment names as protected regardless of self-reported YAML flags. Reject project `conftest.py`, alternate pytest configuration, skip/xfail paths, credential artifacts, import-time factories, and direct scenario/helper writes.
- Draw every generated diagram as a Mermaid business-flow or system-interaction view. `业务流程图.md` uses a business `sequenceDiagram`; topology, service-interaction, or automation views may use a high-level `sequenceDiagram` or `flowchart`. Show business stages, services/modules, external systems, evidence, and source-backed branches only; omit functions, classes, pytest/fixture mechanics, and fine-grained code control flow.

## Completion

Work is complete only when the ordered gates were actually run and reported. At minimum, `check_scenarios.py` validates discovery completeness, topology ordering, configuration provenance, runtime-probe disposition, control matrices, delegation metadata, status consistency, SQL safety declarations, isolation, artifacts, scripts, and cleanup; `pytest --collect-only` succeeds without runtime secrets; environment-independent tests pass; and source-version checks emit a result per scenario and repository.

Each discovery and contract substage writes a content-addressed stage seal bound to a fresh run ID and fixed next stage; restarting inventory invalidates historical seals, and sessions older than one hour cannot be replayed. Static validation rejects missing or stale discovery/contract seals. Smoke and business subprocesses must also produce JUnit with at least one executed test and zero skipped, xfailed, failed, or errored tests. The runner emits `artifacts/e2e-run.json`; this report, not console prose or generated files, is the source for executed, failed, and `N/A` outcomes.

A prose-only constraint is incomplete. Implement each invariant in the generated static checker, runtime preflight, execution orchestrator, or guaranteed finalizer named by the enforcement map, and leave a focused failing fixture or shared-logic test for every non-trivial rule. Update missing gate code before generating scenario test code in an existing E2E project.

When runtime access exists, separately report read-only smoke and real scenario results. Always report restoration verification. Use `N/A` for every validation that did not run; never use generated artifacts, collection, or static validation as evidence of business correctness.
