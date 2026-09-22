# Python E2E Workflow Contract

Generation authority is ordered: reviewed design documents define business
expectations; OpenAPI or another formal protocol defines transport shape;
source/configuration only supplies execution support; runtime results only
decide pass or fail. Missing or conflicting design/protocol evidence blocks
generation. A source or observed rule must never be promoted into `logic.yaml`.

The unit of scenario planning, generation, and maintenance is one scenario directory. `discovery/workspace.yaml` owns workspace facts, and `E2E_PLAN.md` may explain project-wide strategy and commands; neither is a global scenario manifest.

## Contract trace

Before generating assertions, maintain a temporary authority-separated trace:

```text
设计规则 -> 正式协议入口与模型 -> 设计状态变化/结果 -> 设计关联键 -> 支持性观察方式 -> 控制方式 -> 清理/恢复
```

Every business action and assertion must cite a design rule, and every call must cite a formal protocol operation. Source anchors justify only controls, configuration, observation, fixtures, cleanup, and restoration. Do not change design expectations to match current source or runtime behavior.

## Scenario definition

Each `scenarios/<中文业务名称>/场景定义.yaml` is authoritative for that scenario. Use exactly these top-level sections: `meta`, `generation`, `readiness`, `preconditions`, `constructability`, `integrations`, `controls`, `isolation`, `steps`, `cleanup`, and `source`.

The following is a schema-shaped example. Values in angle brackets are metavariables and must be replaced with discovered values rather than copied:

```yaml
# 用途：定义一个业务 E2E 场景的来源、控制、步骤和恢复契约；禁止保存凭据或连接值。
meta:
  id: <STABLE_SCENARIO_ID>
  name: <中文业务名称>
  status: pending_environment
  participants: [<participant-service-a>, <participant-service-b>]
  actor: <业务参与者>

# 生成职责：多场景时每个场景必须有独立 delegated owner；降级时如实记录原因。
generation:
  mode: delegated
  owner: <subagent-task-id>
  write_scope: scenarios/<中文业务名称>
  degradation_reason: null

# 就绪判定：四项状态共同决定 meta.status，blockers 只写缺口而不写敏感值。
readiness:
  source_contract: confirmed
  safe_control: confirmed
  runtime_configuration: missing
  test_data: missing
  blockers:
    - connection:<MISSING_ENDPOINT_ENVIRONMENT_VARIABLE_NAME>
    - business_data:<MISSING_BUSINESS_DATA_VARIABLE_NAME>

# 业务前置：只写可验证条件，不写连接信息。
preconditions:
  - <design-defined-precondition>

# 可构造性：每个前置和步骤都完整评估 schema 定义的候选路径。
constructability:
  preconditions:
    - id: <design-defined-precondition>
      data_ownership: <test_owned|environment_owned|not_data>
      constructible: true
      candidates: <candidate-matrix-from-dev-ai-schema>
  steps:
    - step_id: <step-id>
      candidates: <candidate-matrix-from-dev-ai-schema>

# 运行依赖：类型和 ID 均来自工作区发现，不限定具体技术。
integrations:
  services:
    - <discovered-service-id>
  components:
    - id: <discovered-component-id>
      type: <discovered-component-type>
      required: true

# 控制矩阵：每类都必须完成评估，即使能力不存在。
controls:
  public_api:
    status: usable
    assessment: <source-backed-capability-conclusion>
    evidence:
      - <repository-id>#<source-symbol>
    planned_use:
      - <business-action-symbol>
  test_or_admin_api:
    status: not_found
    assessment: <searched-locations-and-conclusion>
    evidence:
      - <repository-id>#<search-or-source-symbol>
    planned_use: []
  mocks_and_faults:
    status: not_applicable
    assessment: <source-backed-relevance-conclusion>
    evidence: []
    planned_use: []
  dynamic_configuration:
    status: not_applicable
    assessment: <source-backed-relevance-conclusion>
    evidence: []
    planned_use: []
  scheduled_jobs:
    status: not_applicable
    assessment: <source-backed-relevance-conclusion>
    evidence: []
    planned_use: []
  messages:
    status: not_applicable
    assessment: <source-backed-relevance-conclusion>
    evidence: []
    planned_use: []
  database_read:
    status: usable
    assessment: <source-backed-capability-conclusion>
    evidence:
      - <repository-id>#<repository-method-symbol>
    planned_use:
      - <business-evidence-symbol>
  database_control:
    status: not_applicable
    assessment: <source-backed-relevance-conclusion>
    evidence: []
    planned_use: []
    safety: null
  observability:
    status: usable
    assessment: <source-backed-capability-conclusion>
    evidence:
      - <repository-id>#<query-or-event-symbol>
    planned_use:
      - <business-evidence-symbol>
    correlation_keys:
      - <design-defined-correlation-symbol>
    business_evidence:
      - <observable-outcome-symbol>
    recovery:
      - <idempotent-recovery-symbol>
  decision:
    safe_control_path: true
    blockers: []

# 隔离边界：关联键、资源和可变控制必须由当前场景真正独占；锁名称不能替代隔离证明。
isolation:
  namespace: <scenario-unique-namespace>
  correlation_keys:
    - <scenario-owned-correlation-reference>
  owned_resources:
    - kind: <record|message-client|cache-key|file|other>
      identity: <scenario-unique-resource-reference>
      cleanup: <idempotent-cleanup-symbol>
      restore: <restoration-symbol>
      verify: <restoration-verification-symbol>
  mutable_controls: []
  serial_lock: null

# 业务步骤：control 必须引用控制矩阵类别，side_effect 决定运行时门禁。
steps:
  - id: <step-id>
    action: <business-action-symbol>
    control: public_api
    side_effect: write
    design_rule_id: <DESIGN_RULE_ID>
    protocol_ref: <FORMAL_PROTOCOL_OPERATION_ID>
    phase: final_business
    data_ref: 业务数据.json#/<json-pointer>
    expect:
      - <business-outcome-symbol>
    status: executable
    status_reason: <design-and-execution-support-backed-reason>
    evidence:
      - <repository-id>#<source-symbol>

# 清理恢复：动作必须来源明确、幂等且按资源创建顺序立即注册。
cleanup:
  strategy: <api|fixture|control|composite>
  actions:
    - <cleanup-symbol>
  verifies:
    - <restoration-evidence-symbol>

# 源码基线：每项对应一个参与本场景的仓库。
source:
  - repo: <repository-id>
    commit: <40-character-git-sha>
    anchors:
      - <source-symbol>
```

Schema rules:

- Unknown top-level or nested keys fail validation.
- `meta` contains non-empty `id`, `name`, `status`, and `actor`. `id` matches `[A-Z][A-Z0-9_]+`; `status` is `ready`, `pending_environment`, or `contract_blocked`.
- `generation` contains `mode`, `owner`, `write_scope`, and nullable `degradation_reason`, following the delegation rules in [discovery-and-control-policy.md](discovery-and-control-policy.md).
- `readiness.source_contract` and `readiness.safe_control` are `confirmed` or `blocked`; `runtime_configuration` and `test_data` are `confirmed` or `missing`. `blockers` is a unique list of non-sensitive references. Pending blockers use exact canonical `config:`, `credential:`, `connection:`, or `business_data:` environment-variable references derived from the active configuration path and must equal the real missing set; authorization is forbidden here. Contract blockers use source-bound `control:<category>` or `contract:<repository>#<anchor>` values.
- `preconditions` is a non-empty unique list of design-defined statements. `constructability` maps every precondition and step in order and evaluates exactly the candidate kinds exposed by `dev-ai schema e2e.scenario`: `public_api`, `test_or_admin_api`, `database_control`, `messages`, `scheduled_jobs`, `mocks_and_faults`, `dynamic_configuration`, `existing_test_data`, `database_read`, and `observability`. Each candidate records status, component, consumer source, control, side effect, real trigger, observation, isolation, cleanup, and source/runtime evidence. Candidate status is `usable`, `unusable`, or `not_found`; `not_applicable` cannot close the analysis.
- A constructible `test_owned` precondition has a usable controlled construction path and cannot be reported as missing environment data. Every usable write candidate maps to declared isolation and cleanup/restoration symbols.
- `integrations.services` is a unique list of discovery service IDs. `components` contains unique `id`, discovered `type`, and boolean `required`; every item resolves to `discovery/workspace.yaml`.
- `controls` contains exactly the categories defined by the discovery policy plus `decision`. Capability entries have exact non-empty `assessment`, `status`, `evidence`, and `planned_use`; source evidence must match the control's semantic category. `observability` also has `correlation_keys`, `business_evidence`, and `recovery`, which exactly equal the isolation keys, all step expectations, and all cleanup actions/verifications. `database_control.safety` is null when unused and otherwise retains the single-operation summary fields and adds a non-empty ordered `operations` list. Every operation has a unique ID, backward-only dependencies, source consumer, owned exact selector, `expected_rows: 1`, snapshot, mutation verification, restoration, and restoration verification symbols.
- `isolation` contains exactly `namespace`, `correlation_keys`, `owned_resources`, `mutable_controls`, and `serial_lock`, which must be null. Every owned resource contains exact `kind`, `identity`, `cleanup`, `restore`, and `verify` symbols, all mapped into the cleanup contract. Every mutable control is also an owned resource identity. A write scenario has at least one owned resource, and every cross-scenario collision fails.
- Steps have exact `id`, `action`, `control`, `side_effect`, `expect`, `status`, `status_reason`, `evidence`, `design_rule_id`, optional `protocol_ref`, optional async `phase`, and optional `data_ref`. Every business expectation traces to `design_rule_id`; every HTTP/RPC/message/task call traces to `protocol_ref`. IDs are unique; `side_effect` is `none`, `read`, or `write`. Static statuses are `executable`, `environment_missing`, `authorization_missing`, `control_gap`, or `product_gap`; `runtime_failure` is emitted only as runtime evidence. Executable/environment/authorization states require a source-confirmed usable execution candidate. Control/product gaps require every candidate kind to be closed by evidence.
- `data_ref`, when present, has exact form `业务数据.json#/<pointer>` and resolves by RFC 6901 only after selecting the active environment.
- Every resolved `data_ref` subtree includes at least one protocol-valid non-placeholder literal. Environment placeholders are limited to pre-existing environment-owned data; scenario-owned unique strings are generated at runtime with `secrets` or `uuid` under protocol-defined format constraints.
- `cleanup` contains non-empty `strategy`, unique `actions`, and unique `verifies`. Cleanup is source-confirmed, idempotent, and guaranteed by `finally`, a finalizer, `ExitStack`, or a context manager.
- `source` is a non-empty list with exact `repo`, 40-character `commit`, and non-empty unique `anchors`. Every commit equals its discovery inventory snapshot, every relevant topology repository is covered, and every anchor resolves at that commit.
- Stable scenario ID appears outside its definition only in exactly one pytest marker. Actions, expectations, controls, and cleanup use stable business symbols, not endpoint paths, table names, topic names, copied SQL, URLs, or narrative prose.

## Deterministic status gate

`dev-ai e2e check --gate contracts` derives the permitted status:

- `ready`: all four readiness fields are `confirmed`, blockers are empty, `safe_control_path` is true, observability and recovery are usable, required service/component mappings exist, active-environment business data exists, and every exact placeholder used by this scenario currently resolves.
- `pending_environment`: source contract and safe control are `confirmed`; only runtime configuration or test data is `missing`; blockers exactly equal the missing active-environment placeholders/mappings or test-data placeholders. Per-run SQL/control authorization is not a status input.
- `contract_blocked`: source contract or safe control is `blocked`, `safe_control_path` is false, readiness and decision blockers match, every blocker resolves to unavailable control evidence or a scenario source anchor, and every candidate path for every precondition and step is source-backed `unusable` or `not_found`; `not_applicable` cannot close the matrix.

The checker fails a mismatched declared status. A missing HTTP interface alone cannot produce `contract_blocked`. A runtime failure after preflight never changes the static status and must remain a failed execution result.

## Business data

Keep business inputs in strict JSON, keyed first by environment:

```json
{
  "<selected-test-environment>": {
    "<step-data-key>": {
      "<protocol-field>": "<protocol-valid-synthetic-value>",
      "<environment-owned-field>": "${<SELECTED_ENVIRONMENT_VALUE_REFERENCE>}"
    }
  },
  "<named-test-environment>": {
    "<step-data-key>": {
      "<protocol-field>": "<protocol-valid-synthetic-value>",
      "<environment-owned-field>": "${TEST_ENV_SCENARIO_VALUE}"
    }
  }
}
```

The placeholder names and angle-bracket literals shown are metavariables. Inspect the formal protocol fields, validators, enums, and design-defined correlation rules, then replace them with protocol-valid values. Source may supply support-only fixture candidates but cannot override the contract. All environment objects in one file expose the same logical paths. Never merge or fall back across environments. A missing active root is `pending_environment` during preflight.

Do not turn every business field into an environment variable. Keep deterministic values that the test can safely construct as literals. Generate scenario-owned unique strings at runtime with `secrets` or `uuid` according to protocol-defined length/alphabet/format rules. Exact placeholders are only for pre-existing environment-owned values that cannot safely be constructed. Every subtree named by `data_ref` contains at least one non-placeholder literal; the checker rejects all-placeholder injection.

Do not store credentials, endpoints, generated identifiers, mutable results, SQL, or configuration-center values here. Standard JSON comments and synthetic comment fields are forbidden. Builders explicitly map every source DTO or schema field rather than passing through an entire loaded object.

## YAML comment policy

Every generated or maintained YAML file uses useful Chinese comments while preserving discovered source/protocol identifiers:

- the first non-blank line states the file's purpose and whether sensitive values are forbidden;
- a meaningful comment precedes each top-level block and explains its role;
- each exact environment placeholder documents meaning, expected format, and source without exposing a value;
- units, enums, special values, correlation keys, and non-obvious constraints retain their source-confirmed semantics;
- comments that merely repeat a field name do not satisfy the rule.

The static checker enforces file headers, top-level comments, and placeholder provenance. It checks units and other semantic comments only where such values occur; JSON remains comment-free.

## Generation and validation gates

The main agent and generated checker enforce this order:

1. `workspace_inventory`, `dependency_topology`, `initial_configuration`, and `runtime_probe` run as four ordered checks with separate outcomes.
2. `control_matrix`, `scenario_split`, `scenario_ownership`, and `shared_integration` run as four ordered checks with separate outcomes.
3. `static`: full asset, AST, script, control-SQL, and diagram checks pass before any project test is imported; project `conftest.py` and alternate pytest configuration are forbidden. Then environment-independent shared-logic tests and source-version checks pass.
4. `collect`: `python -m pytest --collect-only` succeeds with unresolved runtime placeholders inert.
5. `smoke`: read-only calls run only when runtime access exists and emit one valid endpoint event per selected scenario.
6. `business`: preflight authorizes executable side effects and runs every independently safe step. Each step emits exactly one status event; a blocked later step does not suppress earlier safe work and the scenario still fails rather than passing partially.
7. `restore`: every declared owned resource is restored and verified.

No later gate runs after an earlier failure. Each smoke/business subprocess must produce JUnit containing at least one executed test and zero skipped, xfailed, failed, or errored tests. Missing runtime values may leave `smoke`, `business`, and `restore` as `N/A`, but cannot weaken collection or static gates. Runtime smoke failure is reported as failure, not as static success.

## Static checker contract

Extend the generated project's single `dev-ai e2e check`; do not create overlapping validators. It accepts each ordered stage name plus aggregate `--gate discovery|contracts|static|all`, and optional `--scenario <中文场景名称>`. Each ordered stage validates the preceding content-addressed seal. Aggregate discovery/contracts/all modes are diagnostic-only and cannot create those seals or authorize `static`. It checks at minimum:

- the discovery contract and all cross-references described in the discovery policy;
- the complete scenario schema, status derivation, ownership, exact write scope, and unique multi-scenario delegated owners;
- environment-file selection and no cross-environment configuration or business-data fallback;
- control-matrix use, source evidence, write-step preconditions, correlation, cleanup, restoration, and database-control safeguards;
- complete per-precondition/per-step constructability, test-owned data classification, step evidence, partial-success rejection, and business-failure classification;
- cross-scenario collision checks for correlation sources, namespaces, generated IDs, mutable settings, consumer/client IDs, owned records, and cleanup selectors;
- sibling scenario artifacts, stable IDs, source anchors, all Mermaid diagrams, the shared-CLI launchers, and source-version entries;
- Python AST validity, import resolution without connecting externally, Chinese docstrings, and guaranteed cleanup;
- absence of copied credentials, concrete project-example identifiers, hardcoded endpoints, business IDs, topic/table names in generic helpers, raw SQL in tests, blind sleeps, unrestricted cache clearing, or unbounded publication.

Use YAML/JSON parsers and Python `ast` before narrow text heuristics. Use an installed SQL parser when available; otherwise conservatively reject control SQL that cannot be proven parameterized and exactly bounded. Every diagnostic includes file and line. Suppressions are inline, rule-specific, justified, and never file-wide.

Add focused checker tests for each rule class, including the discovery and SQL cases in [discovery-and-control-policy.md](discovery-and-control-policy.md). Add shared-logic tests only for non-trivial reusable behavior: deep merge, exact-placeholder resolution, environment selection, integration gating, response validation, deadline polling, isolation, control authorization, row-count enforcement, original-value restoration, and preservation of an earlier exception.

## Final report

The main agent reports, separately and without inferred success:

- repositories, build modules, existing E2E projects, and the relevant dependency edges actually found;
- configuration sources and precedence, with credential values redacted;
- runtime processes/listeners associated with source modules and read-only probe results;
- each scenario's owner, or the exact reason for sequential degradation;
- each scenario's selected API, message, job, configuration, database observation, or controlled SQL capabilities;
- each endpoint actually called, its method, redacted target identity, and result summary;
- scenarios that stopped at static generation versus those that entered real business steps;
- database, configuration, cache, message-client, and test-data restoration status;
- environment-independent tests, `pytest --collect-only`, static validation, source synchronization, read-only smoke, real execution, business-step entry rate, semantic coverage, business correctness, and optional code coverage.

Each scenario report includes `step_results`, `execution_rate`, `coverage_rate`, `business_correctness`, and one classification: `static_complete`, `executed`, `partially_covered`, `business_failure`, or `blocked`. A successful transport with an incorrect business result is `business_failure`; a report containing any unavailable step is never `executed` or successful.

Use `N/A` for every check not executed. Collection, generated code, diagrams, or static validation never substitute for requirement coverage or business correctness.
