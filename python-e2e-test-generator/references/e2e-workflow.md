# Python E2E Workflow Contract

The unit of planning, generation, and maintenance is one scenario directory. `E2E_PLAN.md` may contain project-wide strategy, configuration conventions, commands, and constraints, but never a scenario inventory.

## Contract trace

Before generating assertions, maintain a temporary working trace:

```text
用户预期 -> 源码入口 -> 请求/响应模型 -> 状态变化 -> 关联键 -> 可观测证据 -> 清理方式
```

Every assertion and cleanup action must be justified by this trace. It is discovery working state, not another generated manifest. Do not change the user's expectation to make it agree with current source. When the trace cannot confirm an interface, test-control capability, request/response or message contract, correlation rule, or expected result, set `contract_blocked` and identify the missing contract. Reserve `pending_environment` for missing addresses, credentials, and environment-specific test data.

## Scenario definition

Each `scenarios/<中文业务名称>/场景定义.yaml` is the authoritative contract for that scenario. Use only these top-level sections: `meta`, `preconditions`, `integrations`, `steps`, `cleanup`, and `source`.

```yaml
# 用途：定义一个业务 E2E 场景的契约；禁止保存凭据、地址或其他敏感运行值。
# 场景元数据：稳定 ID 仅用于定义和一个 pytest marker。
meta:
  id: MNO_REALNAME_DUAL_OPERATOR_RENEWAL
  name: 实名后双运营商套餐阈值与跨月续订
  # 状态枚举：ready=契约与环境已就绪，pending_environment=仅缺环境值，contract_blocked=源码契约无法确认。
  status: pending_environment
  actor: 已实名车主

# 业务前置条件：描述可验证的环境和数据条件，不填写连接信息。
preconditions:
  - 测试车辆存在移动与联通有效车卡关系
  - 移动20GB大包和联通周期套餐可唯一匹配
  - 环境支持第二个月时间控制

# 外部依赖：HTTP 项为客户端名称；布尔值表示场景是否必须使用对应组件。
integrations:
  http:
    - mno-traffic
    - mno-operator
  kafka: true
  mysql: true
  redis: false
  emq: false

# 业务步骤：data_ref 是指向同目录业务数据文件的 JSON Pointer。
steps:
  - id: real_name
    action: send_real_name_notification
    data_ref: 业务数据.json#/real_name
    expect:
      - real_name_persisted

  - id: activate
    action: activate_salable_plan
    data_ref: 业务数据.json#/activate
    expect:
      - mobile_atom_success
      - unicom_atom_success

  - id: threshold
    action: send_dual_operator_threshold
    data_ref: 业务数据.json#/threshold
    expect:
      - kafka_reminder_published
      - mysql_reminder_persisted

  - id: next_month
    action: advance_to_next_month
    expect:
      - dual_operator_renewal_created

# 清理契约：动作必须由源码确认、可重复执行，并在资源创建后立即注册。
cleanup:
  strategy: api
  actions:
    - unsubscribe_salable_order

# 源码基线：commit 为 40 位 Git SHA；anchors 是与本场景直接关联的非空源码符号。
source:
  - repo: mno-traffic
    commit: 61604f3dd84cea56cc29732faea2dbd727a6e906
    anchors:
      - SoftwareSaleSubscriptionController
      - OperatorThresholdFulfillmentService

  - repo: mno-operator
    commit: 16640b8fb5dd10c7d1e94eed16b3e35a3cb09077
    anchors:
      - OperatorBusinessOperatorApplication
```

Nested schema:

- The top level contains exactly `meta`, `preconditions`, `integrations`, `steps`, `cleanup`, and `source`; unknown top-level or nested keys fail validation.
- `meta` contains exactly non-empty string `id`, `name`, `status`, and `actor`. `id` matches `[A-Z][A-Z0-9_]+`; `status` is `ready`, `pending_environment`, or `contract_blocked`.
- `preconditions` is a non-empty list of non-empty strings.
- `integrations` contains exactly `http`, `kafka`, `mysql`, `redis`, and `emq`. `http` is a duplicate-free list of non-empty service names; every component value is a boolean.
- `steps` is a non-empty list of mappings with exactly `id`, `action`, `expect`, and optional `data_ref`. Step IDs are unique non-empty strings; `action` is a non-empty business symbol; `expect` is a non-empty list of unique non-empty business symbols.
- `data_ref`, when present, has the exact form `业务数据.json#/<pointer>`. After selecting the active environment object, resolve `<pointer>` with RFC 6901 rules. It must not point to credentials or connection data.
- `cleanup` contains exactly non-empty string `strategy` and non-empty unique string list `actions`. Actions are source-confirmed and idempotent; generated code guarantees them with `finally`, pytest finalizers, or an equivalent context manager.
- `source` is a non-empty list whose mappings contain exactly non-empty string `repo`, 40-character hexadecimal `commit`, and a non-empty unique string list `anchors`.
- `meta.id` is stable and appears outside this file only in exactly one pytest marker. Actions and expectations are stable business symbols, not endpoint paths, error-code catalogs, URLs, or prose.

## Business data

Keep business inputs separate from Python orchestration and isolated by environment at the JSON root:

```json
{
  "local": {
    "real_name": {
      "carrier": 1,
      "customer_type": 1,
      "oper_type": 1
    },
    "activate": {
      "plan_id": "${LOCAL_TEST_PLAN_ID}"
    },
    "threshold": {
      "mobile_usage_mb": 2048,
      "mobile_total_usage_mb": 20480,
      "unicom_percent": 100
    }
  },
  "sit": {
    "real_name": {
      "carrier": 1,
      "customer_type": 1,
      "oper_type": 1
    },
    "activate": {
      "plan_id": "${SIT_TEST_PLAN_ID}"
    },
    "threshold": {
      "mobile_usage_mb": 4096,
      "mobile_total_usage_mb": 30720,
      "unicom_percent": 100
    }
  }
}
```

Environment names match `config/environments/<environment>.yaml` basenames and use `[a-z][a-z0-9_-]*`; `example` is forbidden. All environment objects present in one file expose the same logical paths, while values may differ. Select `active_environment` before resolving `data_ref`; never merge or fall back to another environment. A missing active key is missing environment test data and produces `pending_environment` during preflight.

Standard JSON comments and synthetic comment fields are not allowed. Keep source/protocol field semantics and placeholder format/source in the contract trace, explicit builders, focused tests, and environment-prefixed variable names. Unresolved placeholders remain inert during import and collection and resolve only during preflight. Do not store generated order numbers, credentials, endpoints, or mutable runtime results in this file.

## YAML comment policy

Every generated or maintained `.yaml`/`.yml` file in the E2E project must contain useful Chinese comments while preserving source and protocol field names:

- The first non-blank line states the file's purpose and whether sensitive values are allowed. Generated E2E YAML files normally prohibit usable secrets.
- A meaningful comment immediately precedes every top-level block and explains its role rather than repeating its key.
- Each exact environment placeholder documents its meaning, expected format, and source without showing a real value.
- Traffic, capacity, time, ratio, count, and similar values state their units.
- Enumerations, special values such as `-1`, association keys, and non-obvious constraints state their business semantics.
- Do not require a comment on every line and reject comments that merely restate field names.

## Generation gates

1. Source discovery establishes the public entrypoint, business steps, correlation keys, assertions, cleanup, and source anchors.
2. `场景定义.yaml`, `业务数据.json`, and `业务流程图.md` exist before or with the generated test.
3. Missing environment values do not block complete code generation or collection.
4. `pytest --collect-only` succeeds before claiming the scenario is generated.
5. Runtime execution performs non-destructive preflight before business side effects.
6. Every side-effecting request verifies its business response before downstream polling. When an environment exists, focused execution verifies downstream evidence, field-level reconciliation, and cleanup.
7. Incremental updates pass the source-impact gate in [version-sync-policy.md](version-sync-policy.md).

## Reconciliation

`scripts/check_scenarios.py` discovers `scenarios/*/场景定义.yaml` directly and checks at minimum:

- the complete nested schema above, including rejection of unknown nested keys, required cardinalities, types, enum values, and uniqueness constraints;
- `config/runtime.yaml` selects a safe environment name with a matching environment file; `example.yaml` and an `example` environment key are forbidden;
- every environment file includes source-required service URLs, explicit authentication, custom headers, and declared middleware connection settings using inert placeholders rather than usable credentials, and does not redefine shared `enabled` switches;
- every definition has sibling `业务数据.json`, `业务流程图.md`, and `test_<中文业务名称>.py`;
- every business-data file parses as standard JSON, has only known environment keys, gives each present environment the same logical data paths, and contains the active key before runtime execution;
- every `data_ref` targets the sibling `业务数据.json` and resolves within every environment object present in that file, after environment selection rather than before it;
- every generated YAML file passes the file-header, top-level-block, and environment-placeholder comment rules above; also validate unit, enum, special-value, and association-key comments where those values occur;
- `自动化测试流程图.md` is optional, but every diagram uses Mermaid `sequenceDiagram`, never `flowchart`; source-confirmed real branches use paired `alt`/`else`, and failure branches contain the exact source-confirmed business error code rather than an invented value;
- the stable ID appears only in the definition and exactly one pytest marker, with no duplicate IDs;
- source entries have `repo`, a 40-character hexadecimal commit, and non-empty anchors that resolve under the project's source-repository convention;
- every scenario and all-scenario execution script exists, invokes the correct checks/test target, uses `python -m pytest`, and forwards caller arguments;
- test files contain no hardcoded VIN, ICCID, order number, plan/package ID, payload block, or SQL statement;
- Python files parse with `ast`, referenced local or installed imports resolve without importing application modules, and every module, class, and function has a Chinese docstring;
- declared HTTP and Kafka/MySQL/Redis/EMQ integrations agree with clients, fixtures, and preflight validation; unused integrations are neither instantiated nor checked;
- every scenario has guaranteed cleanup using `finally` or an equivalent pytest finalizer/context manager.

`scripts/check_source_versions.py` reads the same definitions and emits an independent source-impact result per scenario/repository. Neither script reads, creates, or updates a global registry.

Use the existing YAML parser, the standard-library JSON parser, and Python `ast` before text heuristics. Mermaid branch/error validation must compare with source evidence resolved from the recorded anchors; if that evidence cannot be established, fail the check and keep the scenario `contract_blocked`. Heuristics are limited to rules such as sensitive literals, long payloads, SQL, and script text. Every heuristic diagnostic reports file and line, and suppressions are narrow, inline, rule-specific, and documented; there is no file-wide blanket suppression.

Add a small test module for `check_scenarios.py` using the project's existing test dependencies. One representative invalid fixture per rule class is enough; parameterize cases where convenient and assert a non-zero exit code plus the file-and-line diagnostic. Do not create another checker or introduce another test framework.

## Shared-logic tests

Add focused tests only for non-trivial reusable behavior:

- recursive configuration merge and recursive exact-placeholder resolution;
- missing and blank environment variables;
- active-environment selection, unknown environments, and missing active business data;
- the same `data_ref` returning isolated values for two environments with no cross-environment fallback;
- builder field names, nesting, unit conversion, enums, and time formatting;
- business-response validation for write requests;
- cleanup failure handling when the test body has already raised.

Use the project's existing test dependency. These environment-independent tests are mandatory before runtime E2E execution and are included by `run_all`; do not expand them into per-function suites or test scenario prose.

## Result reporting

Report these results independently:

- scenario artifact coverage;
- pytest collection;
- static validation;
- source synchronization;
- percentage of attempted scenarios that entered business steps;
- requirement-semantic coverage;
- business correctness;
- code coverage.

Use `N/A` when no real business execution or coverage collection occurred. Collection and static validation cannot be reported as requirement coverage, correctness, or code coverage. Server-side coverage may be reported when available, but it is not a mandatory black-box E2E gate.
