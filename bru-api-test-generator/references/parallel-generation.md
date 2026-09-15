# Parallel Module Generation

Use parallel workers only after the coordinator has completed the global inventory. The point of parallelism is independent module ownership; it is not permission to skip contract reconciliation or run shared test data concurrently.

## Phases

1. **Coordinator, sequential:** discover repository rules, locate the offline OpenAPI file, parse every operation, inspect source mappings and shared security/fixtures, classify the business-code version, freeze `module-map.yaml`, and identify cross-module dependencies. Select the local script bundle or installed shared CLI before workers start.
2. **Workers, parallel where independent:** start one worker per independent module. Each worker generates and tests only its assigned module.
3. **Coordinator, sequential:** review worker reports, regenerate `index.yaml`, `generation-state.yaml`, and `qa-lock.yaml`, reconcile all modules, validate cross-module flows, confirm every included risk, run the final collection, and advance `version-lock.yaml` only after all required evidence is present.

## Ownership

| Owner | Writable scope |
| --- | --- |
| Coordinator | `README.md`, `module-map.yaml`, `index.yaml`, `generation-state.yaml`, `qa-lock.yaml`, `version-lock.yaml`, `impact-rules.yaml`, `flows/cross-module.yaml`, `execution/config.yaml`, `execution/environments/`, `bruno/`, script synchronization metadata, and business-repository metadata |
| Module worker | `contracts/modules/<module-directory>/{logic,cases}.yaml` within its assigned module |

Workers must not edit endpoint inventories, `.bru` files, another module,
business source code, shared credentials, or coordinator-owned files. A worker
may read shared files and must return its case IDs, logic IDs, changed paths,
and unresolved blockers to the coordinator. The coordinator alone materializes
requests so sequential and parallel generation use identical naming, common
Header exclusions, and collection-level runtime behavior.

## Scheduling

- Workers may generate modules concurrently when their endpoint sets, fixtures, and flows are independent.
- A module that requires another module's captured ID, shared mutable account, or ordered setup belongs in the coordinator phase or waits for its dependency.
- Parallel execution requires isolated users, tenants, database records, and environment variables. Without demonstrated isolation, execute module collections sequentially even if generation was parallel.
- A worker failure does not authorize silently dropping the module. The coordinator records the failure and the final coverage check remains failing until the module is repaired or explicitly excluded with a reason.
- Record a failed worker as that module's `blocked` status while allowing unrelated workers to leave reviewable `draft` or `runnable` artifacts. Schedule by endpoint volume when a one-worker-per-module split would leave a large module unbalanced; keep each module's write scope disjoint.

## Handoff

Each worker reports:

- module ID and owned paths;
- generated endpoint, logic, case, and flow IDs;
- cases that require Bruno materialization;
- missing operations for explicitly flow-required modules or exclusions with reasons;
- blockers and required coordinator actions.

The coordinator treats worker reports as input, not proof. The global coverage and flow validators, plus the final Bruno run, are authoritative.
