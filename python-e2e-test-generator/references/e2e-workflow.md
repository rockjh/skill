# Python E2E Workflow Contract

The unit of planning, generation, and maintenance is one scenario directory. `E2E_PLAN.md` may contain project-wide strategy, configuration conventions, commands, and constraints, but never a scenario inventory.

## Scenario definition

Each `scenarios/<中文业务名称>/场景定义.yaml` is the authoritative contract for that scenario. Use only these top-level sections: `meta`, `preconditions`, `integrations`, `steps`, `cleanup`, and `source`.

```yaml
meta:
  id: MNO_REALNAME_DUAL_OPERATOR_RENEWAL
  name: 实名后双运营商套餐阈值与跨月续订
  status: pending_environment
  actor: 已实名车主

preconditions:
  - 测试车辆存在移动与联通有效车卡关系
  - 移动20GB大包和联通周期套餐可唯一匹配
  - 环境支持第二个月时间控制

integrations:
  http:
    - mno-traffic
    - mno-operator
  kafka: true
  mysql: true
  redis: false
  emq: false

steps:
  - id: real_name
    action: send_real_name_notification
    data_ref: 业务数据.yaml#/real_name
    expect:
      - real_name_persisted

  - id: activate
    action: activate_salable_plan
    data_ref: 业务数据.yaml#/activate
    expect:
      - mobile_atom_success
      - unicom_atom_success

  - id: threshold
    action: send_dual_operator_threshold
    data_ref: 业务数据.yaml#/threshold
    expect:
      - kafka_reminder_published
      - mysql_reminder_persisted

  - id: next_month
    action: advance_to_next_month
    expect:
      - dual_operator_renewal_created

cleanup:
  strategy: api
  actions:
    - unsubscribe_salable_order

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

Rules:

- `meta.id` is stable and appears outside this file only in exactly one pytest marker.
- `meta.status` is `ready`, `pending_environment`, or `contract_blocked`.
- `integrations.http` lists required service clients. Each component boolean is explicit; `true` means required and `false` means unused.
- Every step owns its action, optional data reference, and expected business outcomes. Do not split them into top-level `steps`, `checkpoints`, and `expected_outcomes`.
- `data_ref` is a local JSON Pointer into `业务数据.yaml`. It must not point to secrets or environment connection data.
- Actions and expectations are stable business symbols, not endpoint paths, error-code catalogs, URLs, or prose.
- `cleanup.actions` contains source-confirmed, idempotent business cleanup actions. Generated code must guarantee them with `finally`, pytest finalizers, or an equivalent context manager.
- Each source entry keeps only the repository name, exact commit, and high-value anchors. Git history is the change log.
- Omit unsupported optional explanation fields instead of filling them with placeholders.

## Business data

Keep static business inputs separate from Python orchestration:

```yaml
real_name:
  carrier: 1
  customer_type: 1
  oper_type: 1

activate:
  plan_id: ${MNO_TEST_REALNAME_SALABLE_PLAN_ID}

threshold:
  mobile_usage_mb: 2048
  mobile_total_usage_mb: 20480
  unicom_percent: 100
```

Unresolved placeholders remain inert during import and collection. Resolve them only during preflight. Do not store generated order numbers, secrets, endpoints, credentials, or mutable runtime results in this file.

## Generation gates

1. Source discovery establishes the public entrypoint, business steps, correlation keys, assertions, cleanup, and source anchors.
2. `场景定义.yaml`, `业务数据.yaml`, and `业务流程图.md` exist before or with the generated test.
3. Missing environment values do not block complete code generation or collection.
4. `pytest --collect-only` succeeds before claiming the scenario is generated.
5. Runtime execution performs non-destructive preflight before business side effects.
6. When an environment exists, focused execution verifies application responses, downstream evidence, field-level reconciliation, and cleanup.
7. Incremental updates pass the source-impact gate in [version-sync-policy.md](version-sync-policy.md).

## Reconciliation

`scripts/check_scenarios.py` discovers `scenarios/*/场景定义.yaml` directly and checks at minimum:

- only the compact top-level schema is used and all required fields have valid types;
- every definition has sibling `业务数据.yaml`, `业务流程图.md`, and `test_<中文业务名称>.py`;
- `自动化测试流程图.md` is optional, but any scenario diagram uses Mermaid `sequenceDiagram`, never `flowchart`; real branches use `alt`/`else` and failure branches include source-confirmed business error codes;
- the stable ID appears only in the definition and exactly one pytest marker, with no duplicate IDs;
- source entries have `repo`, a full commit hash, and non-empty anchors;
- test files contain no hardcoded VIN, ICCID, order number, plan/package ID, payload block, or SQL statement;
- Python modules, classes, and functions contain Chinese docstrings;
- Kafka, MySQL, Redis, and EMQ imports/fixtures agree with `integrations`;
- every scenario has guaranteed cleanup using `finally` or an equivalent pytest finalizer/context manager.

`scripts/check_source_versions.py` reads the same definitions and emits an independent source-impact result per scenario/repository. Neither script reads, creates, or updates a global registry.

Static checks should parse YAML and Python with real parsers where possible. Heuristic checks for sensitive literals and SQL must report the exact file and line and allow narrowly documented false-positive suppressions.
