# Parallel Module Generation And Execution

Use workers only after the coordinator freezes the OpenAPI inventory, module map, shared execution configuration, and `qa/data/constraints/rules.yaml`.

Before dispatching each assignment, record its current-workspace boundary:

```bash
bruno-api-test-generator worker-start --module <module>
```

## Ownership

| Owner | Writable scope |
| --- | --- |
| Coordinator | Global contracts, locks, constraint merges, execution configuration, collection files, cross-module flows, and final reports |
| Module worker | Its `data/contracts/modules/<directory>/`, `data/bruno/<directory>/`, `results/modules/<id>/`, `results/modules/evidence/<id>/`, and `results/logs/modules/<id>/` only |

Workers may read shared contracts, configuration, prior evidence, and business source. They must not write business code, another module, `index.yaml`, `generation-state.yaml`, `qa-lock.yaml`, `version-lock.yaml`, `collection.bru`, shared environments, or cross-module flows.

The coordinator records each assignment and gives the constraint validator the worker role, assigned module, and changed paths. `module-worker-boundary` and `business-code-immutable` fail any path outside the table above.

## Worker Sequence

1. Read the assigned module contracts and the shared machine rules.
2. Inspect its reachable Controller, Application, Domain Service, DTO/Output, Entity, Repository, exception/error-code, configuration, Flyway, and test evidence.
3. Write module `source-rules.yaml`, `logic.yaml`, `cases.yaml`, and explicit flows/exclusions when applicable.
4. Run module materialization. It writes only the module Bruno directory, `materialization-state.yaml`, `module-lock.yaml`, and module documentation.
5. Run `bruno-api-test-generator run --module <module>`. It validates the module lock and writes module-local evidence, results, and logs.
6. Run `bruno-api-test-generator worker-check --module <module> --stage post-execution`. Changed paths are calculated from the recorded snapshot.
7. Return rule/case/logic/flow IDs, result/evidence paths, failures by category, and manual confirmations.

A worker failure affects only that module. Other workers continue.

## Independence And Flows

A module run cannot depend on another module having passed. Shared setup or captured identifiers require a coordinator-owned explicit flow. Never encode a dependency through filenames, module order, or shared mutable fixtures.

Parallel execution additionally requires isolated accounts, tenants, records, and fixture paths. Without demonstrated isolation, workers may generate concurrently but the coordinator executes modules sequentially.

## Coordinator Merge

After workers finish, the coordinator:

1. validates each worker snapshot boundary and module lock;
2. merges source and observed rules into the global constraint library;
3. regenerates `index.yaml` and `generation-state.yaml` without resetting unchanged successful cases;
4. materializes globally and refreshes `qa-lock.yaml`;
5. validates cross-module flows;
6. runs `bruno-api-test-generator aggregate` to reconcile independent module reports and evidence; and
7. runs the all-module collection only when a coordinator-owned cross-module flow requires it.

Worker reports are inputs, not proof. Shared constraints, global reconciliation, and execution evidence are authoritative.
