# Workspace Discovery and Test-Control Policy

This policy is the first gate for every generation or material update. Discovery is workspace-wide until the inventory and topology exist; after that, inspect only modules that participate in requested business chains.

## Workspace contract

Create `discovery/workspace.yaml` before scenario code. It is a compact evidence record, not a scenario inventory and not a place for secrets. Use discovered stable IDs; the metavariables below are not literal names to copy.

```yaml
# 用途：记录工作区、依赖、配置和只读运行探测证据；禁止保存凭据值或业务数据。
schema_version: 1

# 工作区清单：列出扫描范围内全部仓库、构建工程、模块和已有 E2E 工程。
inventory:
  roots:
    - <workspace-root-reference>
  repositories:
    - id: <repository-id>
      root: <relative-or-resolvable-path>
      commit: <40-character-git-sha>
      build_files:
        - <build-descriptor-path>
      modules:
        - id: <module-id>
          path: <module-path>
          kind: <application|sdk|starter|client|facade|library|e2e|other>
  existing_e2e:
    - <relative-path>

# 依赖拓扑：每条边必须有构建描述或源码调用证据。
topology:
  nodes:
    - id: <repository-id>:<module-id>
      relevant: true
  edges:
    - from: <node-id>
      to: <node-id>
      mechanism: <build|http|rpc|message|database|cache|job|configuration|embedded>
      evidence:
        - <from-repository-id>#<source-anchor>
  searches:
    http_rpc:
      queries: [<search-expression-or-symbol-family>]
      evidence: [<repository-id>#<source-anchor>]
      conclusion: <source-backed-conclusion-or-explicit-not-found>
    messages:
      queries: [<search-expression-or-symbol-family>]
      evidence: []
      conclusion: <source-backed-conclusion-or-explicit-not-found>
    database:
      queries: [<search-expression-or-symbol-family>]
      evidence: []
      conclusion: <source-backed-conclusion-or-explicit-not-found>
    cache:
      queries: [<search-expression-or-symbol-family>]
      evidence: []
      conclusion: <source-backed-conclusion-or-explicit-not-found>
    jobs:
      queries: [<search-expression-or-symbol-family>]
      evidence: []
      conclusion: <source-backed-conclusion-or-explicit-not-found>
    configuration:
      queries: [<search-expression-or-symbol-family>]
      evidence: [<repository-id>#<source-anchor>]
      conclusion: <source-backed-conclusion-or-explicit-not-found>

# 配置发现：只记录非敏感值或敏感值来源；precedence 稳定列出来源，同一 owner 内从低到高排列。
configuration:
  sources:
    - id: <configuration-source-id>
      owner: <node-id>
      kind: <file|profile|environment|config-center|command-line|local-override|other>
      location: <path-or-non-secret-reference>
      profile: <profile-or-null>
      overrides: [<lower-priority-source-id>]
      evidence: [<repository-id>#<source-anchor>]
  precedence:
    - <configuration-source-id>
  services:
    - id: <service-id>
      owner: <node-id>
      port:
        value: <non-secret-value-or-null>
        effective_source: <configuration-source-id>
        resolution: <resolved|unresolved>
        source_key: <exact-key-in-effective-source>
      context_path:
        value: <non-secret-value-or-null>
        effective_source: <configuration-source-id>
        resolution: <resolved|unresolved>
        source_key: <exact-key-in-effective-source>
      health:
        value: <non-secret-value-or-null>
        effective_source: <configuration-source-id>
        resolution: <resolved|unresolved>
        source_key: <exact-key-in-effective-source>
      openapi:
        value: <non-secret-value-or-null>
        effective_source: <configuration-source-id>
        resolution: <resolved|unresolved>
        source_key: <exact-key-in-effective-source>
  data_sources:
    - id: <data-source-id>
      owner: <node-id>
      type: <discovered-type>
      name:
        value: <non-secret-value-or-null>
        effective_source: <configuration-source-id>
        resolution: <resolved|unresolved>
        source_key: <exact-key-in-effective-source>
      connection_source:
        reference: <credential-or-config-reference-only>
        effective_source: <configuration-source-id>
        resolution: <resolved|unresolved>
        source_key: <exact-key-in-effective-source>
  middleware:
    - id: <middleware-id>
      owner: <node-id>
      capability: <messages|cache>
      type: <discovered-type>
      logical_name:
        value: <non-secret-name-or-null>
        effective_source: <configuration-source-id>
        resolution: <resolved|unresolved>
        source_key: <exact-key-in-effective-source>
      connection_source:
        reference: <credential-or-config-reference-only>
        effective_source: <configuration-source-id>
        resolution: <resolved|unresolved>
        source_key: <exact-key-in-effective-source>
  controls:
    - id: <control-id>
      owner: <node-id>
      capability: <jobs|test_or_admin_api|mocks_and_faults|dynamic_configuration|scheduled_jobs>
      type: <mock|test-api|fault-injection|job|dynamic-config|other>
      source: <repository-id>#<source-anchor>

# 运行探测：用户未说明服务已启动时 outcome 必须为 not_requested。
runtime_probe:
  requested: false
  outcome: not_requested
  blockers: []
  listeners: []
  processes: []
  associations: []
  read_only_smoke: []

# 阶段门禁：仅在相应证据完整后由主代理置为 true。
gates:
  inventory_complete: true
  topology_complete: true
  configuration_complete: true
  runtime_probe_complete: true
```

`check_scenarios.py --gate discovery` is an aggregate diagnostic only; it never writes an ordering seal. The four ordered discovery gates validate the complete nested shape, referenced repository/module IDs, resolvable files, Git commits, topology endpoints, all six source-search categories, evidence anchors, configuration provenance, resolved/unresolved consistency, runtime-probe disposition, and all four gate booleans. They scan configured workspace roots read-only and fail when a discovered Git root, source build root, build-backed module, or existing E2E project is absent from the inventory. Build files inside an explicitly inventoried standalone E2E project are covered by that E2E entry instead of being reported twice. Repository, module, build, and file-backed configuration paths must remain within their declared workspace/owner repository boundaries. A conservative source scan also fails when it detects an integration category but the corresponding search evidence is empty.

The checker rejects usable credential values in every project YAML/YML/JSON/XML document, Python constants/defaults/call keywords, Markdown, TOML, properties, shell text, reports, connection strings, command arguments, and authorization headers, including non-string credential values. It rejects `.pem`, `.key`, `.p12`, `.pfx`, `.jks`, and `.keystore` artifacts outright. Exact environment placeholders and typed non-secret references remain valid. It permits non-sensitive observed values such as a port or context path only when their provenance and override level are recorded.

Build inventory is semantic, not filename-only. The gate parses supported build descriptors at the recorded Git commit, rejects empty or unparseable descriptors, derives local module dependencies, requires their topology edges, and accepts `build`/`embedded` edge evidence only when the anchor is an actual caller dependency. A comment or arbitrary symbol in a build file is not dependency evidence.

## Inventory and topology procedure

1. Find all Git roots, nested build descriptors, modules, and existing E2E projects under the agreed workspace roots.
2. Read build descriptors before source-wide searching. Record module membership and dependencies on SDKs, starters, clients, facades, libraries, plugins, and generated contracts.
3. Search for service entrypoints and outbound HTTP/RPC clients, message producers/consumers, database access, cache access, scheduled jobs, and configuration-center clients.
4. Build the relevant directed topology with a caller/producer module-owned anchor for every edge. For every source-confirmed HTTP/RPC, message, database, cache, job, or configuration capability owner in a multi-module chain, the matching outgoing topology edge is mandatory; one unrelated edge cannot satisfy all modules.
5. Mark unrelated nodes `relevant: false`; do not deeply traverse their source after the global inventory is complete.

The main agent must run and pass the discovery gate before writing `test_*.py`, scenario action modules, builders, assertions, or cleanup. Existing artifacts may be read during discovery, but no scenario contract may be marked `ready` until the topology and configuration gates pass.

## Configuration discovery

For every relevant runnable node, discover and record:

- service port, context path, health endpoint, and OpenAPI source;
- database type, logical data-source name, and connection source;
- every discovered broker, cache, scheduler, device/message gateway, and similar dependency, including Kafka-, Redis-, MQTT/EMQ-, or scheduler-specific configuration when present;
- configuration center, environment variables, profiles, command-line arguments, and local overrides;
- mocks, test/admin controllers, failure injection, controlled jobs, and dynamic switches;
- the actual low-to-high override order within each module owner. A source lists only lower-priority same-owner sources whose keys it really overrides; same-owner sources with disjoint keys need no artificial edge, and independent modules never claim cross-project overrides.

A source-code default is only one low-priority source. It is not a runtime value unless the complete owner-specific override chain proves that no higher source replaces it. Each discovered property records its exact `source_key`, effective source, and `resolved`/`unresolved` disposition. File/profile/local-override sources must be structured YAML, JSON, TOML, properties, or INI at the recorded commit; the gate resolves `source_key` in that exact file and compares resolved non-secret values. Environment sources bind `source_key` to the exact environment reference. Unverifiable config-center, command-line, or other sources remain unresolved until runtime evidence exists. The discovery gate rejects `resolved` paired with `null`, a blank value, or a value that differs from the final source. Credential/connection references use an exact `${ENV_NAME}` placeholder or a typed non-secret reference beginning with `environment:`, `env:`, `secret-store:`, `vault:`, `config:`, `config-center:`, `file-key:`, or `provider:`. An arbitrary non-empty string is rejected. Never print or persist the resolved credential value.

Source capability discovery and configuration inventory are one gate. Valid HTTP/RPC, database, message/cache, or scheduled-job search evidence requires, respectively, a discovered `services`, `data_sources`, `middleware`, or `controls` record with the same most-specific owner module and matching `capability`. Another module or another middleware/control type cannot satisfy the requirement. A search hit cannot coexist with a missing owner-specific record and still claim `configuration_complete`.

## Read-only runtime probe

When the user says applications are running, `runtime_probe.requested` must be `true`; `not_requested` is then invalid. Probe read-only:

1. inspect listening ports and process metadata without stopping or reconfiguring processes;
2. associate command lines, working directories, artifacts, and embedded dependencies with topology nodes;
3. call health, OpenAPI, query endpoints, or a lookup with a guaranteed nonexistent identifier;
4. record request method, redacted target identity, response status, and a bounded response summary;
5. keep side-effecting endpoints untouched until scenario isolation and cleanup pass preflight.

When populated, the exact list item schemas are:

```yaml
processes:
  - id: <process-id>
    pid: <positive-observed-process-id>
    command_reference: <non-secret-command-or-artifact-reference>
    evidence: [<read-only-observation>]
listeners:
  - id: <listener-id>
    host: <observed-local-bind-or-loopback-host>
    port: <observed-port>
    protocol: <observed-protocol>
    evidence: [<read-only-observation>]
associations:
  - process: <process-id>
    listener: <listener-id>
    node: <topology-node-id>
read_only_smoke:
  - node: <topology-node-id>
    method: <GET|HEAD|READ>
    target_ref: <credential-free-local-url-or-source-proven-read-reference>
    result: <status:NNN-for-http-or-bounded-read-result>
```

The checker rejects an unassociated application listener, an unresolved process/listener reference, or a smoke record with an unsafe method. A completed probe must associate and smoke every relevant `application` node; one service's result cannot stand in for the others. The ordered `runtime_probe` gate re-reads each PID command line, rejects non-local hosts, confirms the declared PID owns the listener, opens it with a bounded TCP connection, and repeats local GET/HEAD calls. The actual status must match `result`, and HTTP 5xx is always failure. Aggregate diagnostics and later static gates do not repeat live I/O. Source-proven read-only RPC uses a `read_only_rpc` common adapter with an exact repository anchor and positive timeout because the generic gate cannot invoke an unknown protocol.

Allowed outcomes are `completed` or `blocked` when requested, and `not_requested` otherwise. `completed` requires non-empty processes, listeners, associations, and read-only smoke results, with every process/listener associated exactly to a known topology node. `blocked` requires a non-empty reason showing why the read-only probe itself could not run. Individual refused connections, timeouts, unexpected statuses, or contract mismatches belong in a completed probe's evidence and remain failures when the live smoke suite runs.

## Scenario control matrix

Every `场景定义.yaml` contains all of these control categories, even when a category has no usable capability:

- `public_api`
- `test_or_admin_api`
- `mocks_and_faults`
- `dynamic_configuration`
- `scheduled_jobs`
- `messages`
- `database_read`
- `database_control`
- `observability`

Each category uses an exact mapping with `status`, non-empty `assessment`, `evidence`, and `planned_use`. `status` is `usable`, `unusable`, `not_found`, or `not_applicable`; `assessment` states the source-backed conclusion; every evidence item resolves to a source anchor declared by the scenario; `planned_use` is a list of business-purpose symbols. Every step action must appear in its own control's `planned_use`, and every planned use must map back to that control's step; observability may map to any step expectation. A `write` step cannot claim `database_read` or `observability`. `observability` additionally contains non-empty `correlation_keys`, `business_evidence`, and `recovery` when the scenario is executable. `database_control` additionally contains `safety`, which is null when unused and uses the exact controlled-SQL mapping below when planned.

The matrix ends with:

```yaml
decision:
  safe_control_path: true
  blockers: []
```

The checker enforces:

- every category is present with a non-empty assessment; `usable`, `unusable`, and `not_found` entries have non-empty evidence, while `not_applicable` states why the category cannot affect this scenario;
- every planned control maps to a step, assertion, precondition, or cleanup action;
- `ready` requires `safe_control_path: true`, usable observability, non-empty correlation/recovery, resolved runtime configuration, and isolated test data;
- `pending_environment` requires a complete source contract and safe control path; its blocker set must exactly equal the active environment mappings/placeholders or business-data placeholders that are currently missing;
- `contract_blocked` requires `safe_control_path: false`, matching readiness/decision blockers, and source-backed `unusable` or `not_found` results for every candidate API, test/admin, mock/fault, dynamic-configuration, job, message, and database-control path. `not_applicable` cannot close this matrix. Each blocker is either `control:<category>` bound to an unavailable source-backed capability or `contract:<repository>#<anchor>`; it is invalid merely because no HTTP endpoint exists;
- a scenario with control SQL cannot execute unless all SQL-control fields below are present and the separate per-run authorization gate passes.

## Scenario assignment and isolation

Before assignment, the main agent keeps each control matrix as discovery working state and uses it to decide whether implementation is safe. After assignment, the owning subagent materializes that reviewed matrix in its `场景定义.yaml` and may only tighten or source-correct it; any changed safe-control decision returns to the main agent for review before code generation.

Each scenario contract records generation ownership:

```yaml
generation:
  mode: delegated
  owner: <subagent-task-id>
  write_scope: scenarios/<中文业务名称>
  degradation_reason: null
```

Allowed modes are `delegated`, `main_agent`, and `sequential_degraded`. A single scenario may use `main_agent`. With multiple scenarios, the checker requires unique `delegated` owners; when subagents are unavailable, every affected scenario uses `sequential_degraded` with a non-empty reason. The main agent must report the degraded mode and may not label it delegated.

Subagents write only their exact `write_scope`. Requests for shared clients, fixtures, assertions, or validators are returned to the main agent as suggestions. Before integration, the main agent compares correlation-key sources, generated identifiers, namespaces, mutable configuration, message consumer groups/client IDs, rows, files, cache keys, and cleanup selectors across scenarios. Every write scenario declares at least one exact owned resource with cleanup, restoration, and verification symbols, and every `mutable_controls` identity is one of those owned resource identities. Any possible collision blocks runtime execution until the resources are genuinely isolated; a declared lock name alone is not accepted as proof.

## Controlled SQL

Observation SQL is parameterized, read-only, and belongs in `common/repositories/`. Control SQL exists only for preparation, bounded time advancement, expiry simulation, or a source-confirmed trigger that cannot be safely reached through a normal business interface. It cannot replace the business action being tested.

Database control is disabled by default. Runtime preflight requires an explicit per-run authorization reference for the exact test environment and rejects protected or production environments. The main agent must not infer, mint, persist, or reuse that authorization. Missing authorization fails that execution preflight; it does not turn a source-complete scenario into `pending_environment`. The process environment supplies `E2E_ENABLE_DATABASE_CONTROL=true`, `E2E_CONTROL_AUTHORIZATION_REF`, and the exact `E2E_CONTROL_ENVIRONMENT` for that run only. The scenario's `database_control.safety` mapping records no authorization value and embeds no SQL:

```yaml
safety:
  authorization_required: true
  target_environment: <exact-test-environment>
  purpose: <preparation|time_advance|expiry_simulation|state_trigger>
  consumer_source: <repository-id>#<source-symbol>
  exact_selector: <scenario-owned-selector-symbol>
  expected_rows: 1
  snapshot: <snapshot-operation-symbol>
  mutation: <bounded-control-operation-symbol>
  trigger: <business-trigger-or-wait-symbol>
  verification: <business-verification-symbol>
  restoration: <restoration-operation-symbol>
  restoration_verification: <restoration-verification-symbol>
```

Generated control code must:

1. query and retain original values before mutation;
2. use parameter binding and an exact scenario correlation key;
3. require `expected_rows: 1`, capture the actual affected-row count, compare it with one, report both, and reject zero or multiple rows before continuing;
4. trigger or wait for source-confirmed business logic and verify the business result;
5. restore original values in `finally` or a fixture finalizer;
6. verify restored values and row count;
7. preserve an earlier test failure while attaching cleanup/restoration failure to the report.

Use `controlled_database_state(...)` directly as a `with` context and place the normal business trigger/wait, real observation, and non-tautological final assertion inside its body. Pass the complete object returned by scenario `preflight`; do not pass a selector, expected environment, or row count separately. The helper obtains `target_environment`, `expected_rows`, and `exact_selector` from the validated scenario contract, then passes that exact selector to `snapshot(selector_ref)`, `mutate(selector_ref)`, `restore(original, selector_ref)`, and `verify_restored(original, selector_ref)`. The four roles use distinct named callbacks defined beside the protected operation, consume `selector_ref`, and cannot be called directly. `consumer_source` must resolve to source with database-consumer or scheduled-job semantics, not merely an arbitrary symbol. Mutating and restoration SQL may live only in those registered callbacks and must use a single statically resolved statement whose `WHERE` consists only of parameterized equality predicates joined by `AND`; comments, statement chaining, `OR`, tautologies, fuzzy predicates, subqueries, and selector use only in `SET` are rejected. Snapshot and verification execute real read operations; restoration binds `original` to the update value before the `WHERE` parameters, and verification compares the observed value with `original`. String echo, callback reuse, dead branches, and an empty context body are rejected.

For non-database writes, call `restoration_guard` with the complete `scenario_context`; callers cannot provide resource labels. `restore(resource_ref)` and `verify(resource_ref)` are distinct same-module named functions: restore returns a real mutation result using the resource, while verify returns a non-constant observer result using it. Lambdas, unresolved callbacks, role reuse, and no-op callbacks are rejected. The guard derives the exact restoration resource set from `isolation.owned_resources`, and the runner compares successful restoration events with that set only after the scenario emitted `business_entered`.

Static validation rejects an update/delete without a bounded exact selector, string-built SQL, control SQL in tests or read repositories, a default-enabled control switch, missing snapshot/restore code, or a control operation without source and authorization evidence. Runtime preflight rejects missing authorization, mismatched environments, non-test targets, and selectors that are not owned by the current scenario.

## Gate tests

The installed project adds focused invalid fixtures to `tests/test_e2e_guard.py` and shared runtime tests to `tests/test_e2e_runtime.py`. Cover every rule family, including discovery completeness/path boundaries, runtime-probe disposition, status transitions, delegated ownership, global isolation under `--scenario`, unsafe SQL, dangerous-control authorization, evidence identity, argument filtering, redaction, restoration, and stage ordering. Every invalid fixture must produce a non-zero result or the exact rejected exception.

## Enforcement map

| Invariant | Enforcing mechanism |
| --- | --- |
| Complete workspace inventory before topology | `--gate workspace_inventory` scans roots and cross-checks inventory |
| Topology before scenario code | main-agent write barrier plus content-addressed discovery seal required by later gates |
| Configuration values have provenance, owner-specific precedence, and capability coverage | discovery schema and `--gate initial_configuration` |
| Running applications receive read-only probing | runtime-probe disposition rules and smoke-method validation |
| Every scenario has a complete control matrix | `--gate control_matrix` exact schema/evidence rules plus ordered stage seals |
| Multi-scenario work uses one subagent per scenario or truthful degradation | unique owner/write-scope checks and final-report requirement |
| Shared code is main-agent owned and scenario data is isolated | ownership boundary plus cross-scenario static collision check |
| Collection performs no I/O | AST/import checks and environment-free `pytest --collect-only` |
| Side effects wait for isolation, authorization, and recovery | scenario runtime preflight |
| Control SQL is exact, authorized, reversible, and verified | static SQL rules, runtime authorization, row-count checks, and finalizers |
| Runtime evidence follows a real operation and owned correlation | adapter-only AST causal binding plus runner correlation/resource checks |
| Runtime failures stay failures | execution orchestrator and no skip/xfail/no-op rule |
| Unexecuted validation is never reported as success | structured stage outcomes and mandatory `N/A` reporting |
