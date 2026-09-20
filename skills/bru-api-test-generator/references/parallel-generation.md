# Parallel Module Generation And Execution

Use workers only after the coordinator freezes the OpenAPI inventory, module map, shared execution configuration, and `qa/constraints/rules.yaml`.

Before dispatching each assignment, record its current-workspace boundary:

```bash
dev-ai api-test worker-start --module <module>
```

## Ownership

| Owner | Writable scope |
| --- | --- |
| Coordinator | Global contracts, locks, constraint merges, execution configuration, collection files, cross-module flows, and final reports |
| Module worker | Its `contracts/modules/<directory>/`, `bruno/<directory>/`, `results/modules/<id>/`, `results/modules/evidence/<id>/`, and `results/logs/modules/<id>/` only |

Workers may read shared contracts, configuration, prior evidence, and execution-support source. Source inspection is limited to runtime configuration, authentication/header setup, fixtures, test-data preparation, and mock toggles; it must not supply business rules or expected results. They must not write business code, another module, `index.yaml`, `generation-state.yaml`, `qa-lock.yaml`, `version-lock.yaml`, `collection.bru`, shared environments, or cross-module flows.

The coordinator records each assignment and gives the constraint validator the worker role, assigned module, and changed paths. `module-worker-boundary` and `business-code-immutable` fail any path outside the table above.

## Worker Sequence

1. Read the assigned module contracts and the shared machine rules.
2. Inspect runtime configuration, authentication/signature/header setup, fixtures, database test-data preparation, upload templates, and external-service mock toggles needed to execute the assigned cases.
3. Read the assigned reviewed design rules and OpenAPI contract, then write module design references, `logic.yaml`, `cases.yaml`, and explicit flows/exclusions when applicable. Source discovery remains execution support only and cannot create business cases or expected values.
4. Run module materialization. It writes only the module Bruno directory, `materialization-state.yaml`, `module-lock.yaml`, and module documentation.
5. Run `dev-ai api-test run --module <module>`. It validates the module lock and writes module-local evidence, results, and logs.
6. Run `dev-ai api-test worker-check --module <module> --stage post-execution`. Changed paths are calculated from the recorded snapshot.
7. Return rule/case/logic/flow IDs, result/evidence paths, failures by category, and any blocking manual confirmations.

A worker failure affects only that module. Other workers continue.

## Independence And Flows

A module run cannot depend on another module having passed. Shared setup or captured identifiers require a coordinator-owned explicit flow. Never encode a dependency through filenames, module order, or shared mutable fixtures.

Parallel execution additionally requires isolated accounts, tenants, records, and fixture paths. Without demonstrated isolation, workers may generate concurrently but the coordinator executes modules sequentially.

## Coordinator Merge

After workers finish, the coordinator:

1. validates each worker snapshot boundary and module lock;
2. validates shared design rules and aggregates observed evidence for audit only;
3. regenerates `index.yaml` and `generation-state.yaml` without resetting unchanged successful cases;
4. materializes globally and refreshes `qa-lock.yaml`;
5. validates cross-module flows;
6. runs `dev-ai api-test aggregate` to reconcile independent module reports and evidence; and
7. runs the all-module collection only when a coordinator-owned cross-module flow requires it.

Worker reports are inputs, not proof. Shared constraints, global reconciliation, and execution evidence are authoritative.
