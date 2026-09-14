# Python E2E Workflow Contract

The unit of planning and maintenance is one scenario directory. There is no global machine-readable scenario inventory. `E2E_PLAN.md` contains only project-level strategy, environment/configuration conventions, commands, batch notes, and explicit project-wide constraints.

## Scenario definition

Each `scenarios/<中文业务名称>/场景定义.yaml` is the authoritative contract for that scenario. Use source-confirmed names and omit optional sections that do not apply.

```yaml
id: MNO_REALNAME_DUAL_OPERATOR_RENEWAL
name: 实名后开通双运营商套餐并验证跨月续订
execution_status: pending_environment

actor: 已实名车主
preconditions:
  - 车辆与车主关系有效
  - 双运营商套餐可售

services:
  - mno-traffic
  - mno-operator

interfaces:
  - service: mno-traffic
    kind: http
    name: SoftwareSaleSubscriptionController

integration_dependencies:
  - kind: kafka
    required: true
  - kind: mysql
    required: true

steps:
  - 开通双运营商套餐
  - 推进并验证跨月续订

checkpoints:
  - id: subscription_accepted
    owner_service: mno-traffic
    observable: http_application_result
    correlation: order_id
  - id: fulfillment_published
    owner_service: mno-traffic
    observable: kafka_message
    correlation: atomic_order_id
  - id: fulfillment_persisted
    owner_service: mno-operator
    observable: mysql_row
    correlation: atomic_order_id

expected_outcomes:
  - 两个运营商套餐均成功续订

cleanup:
  - 通过业务 API 删除测试数据
  - 关闭场景独立 Kafka consumer
  - 恢复场景修改过的配置

source_versions:
  - repository: mno-traffic
    path: ../mno-traffic
    generated_from_commit: 500832c542f12fced10a92bedd8225e64f635abd
    last_reviewed_commit: 500832c542f12fced10a92bedd8225e64f635abd
    branch: develop
    dirty: false
    source_anchors:
      - SoftwareSaleSubscriptionController
      - RealNameController
      - OperatorThresholdFulfillmentService
      - SalablePlanFulfillmentReceiver
  - repository: mno-operator
    path: ../mno-operator
    generated_from_commit: 3023bfb40db540376b6a9dbd2be73b5e5a8d006a
    last_reviewed_commit: 3023bfb40db540376b6a9dbd2be73b5e5a8d006a
    branch: develop
    dirty: false
    source_anchors:
      - <source-confirmed-mno-operator-anchor>
```

For a relevant dirty working tree, also record each relevant staged, unstaged, or untracked file and its SHA-256. `generated_from_commit` is the committed source basis for the current test; `dirty: true` plus file hashes records the additional uncommitted basis.

Allowed execution states include:

- `ready`: required runtime configuration passed preflight.
- `pending_environment`: code is complete/collectable, but runtime endpoints, credentials, test identifiers, or integration access are missing.
- `contract_blocked`: an interface, message/schema contract, or business rule cannot be established from source.

Do not use `pending_environment` to defer code generation. Do not use `contract_blocked` for missing runtime values.

## Generation gates

1. Source discovery identifies the public entrypoint, downstream boundaries, correlation keys, assertions, cleanup, and source anchors.
2. The scenario definition and both diagrams are written before or with the generated test.
3. Missing environment values do not block complete code generation or collection.
4. `pytest --collect-only` passes before claiming the scenario is generated.
5. Real execution runs non-destructive preflight before any business request or seed write.
6. When runtime configuration exists, focused execution verifies the business response, required downstream evidence, cleanup, and restoration.
7. Incremental updates pass the per-scenario source impact gate in `version-sync-policy.md`.

## Reconciliation

`scripts/check_scenarios.py` scans `scenarios/*/场景定义.yaml`; it must not read or generate a registry. At minimum it checks:

- the directory and business artifact names follow the Chinese naming boundary;
- the stable ID exists only in the definition and exactly one pytest marker;
- the test and both diagrams exist;
- dependency declarations do not contain endpoints, credentials, or `enabled` duplicates;
- source repositories include both commit fields, dirty state, and source anchors;
- dirty relevant files include SHA-256 values;
- every discovered definition maps to its sibling test and there are no duplicate IDs.

`scripts/check_source_versions.py` scans the same definitions and reports each scenario independently as `unchanged`, `no_relevant_change`, `affected`, `full_rediscovery_required`, or `dirty_review_required`. It does not update files unless the user requested an update.

## Unknowns and exclusions

Keep scenario-specific unknowns and exclusion reasons in that scenario's definition or explanation. If a requested business rule cannot be observed, identify the missing contract, permission, or test hook. Never replace a required Kafka/database outcome with an HTTP-only assertion merely to make execution pass.
