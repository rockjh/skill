# Bruno Execution Evidence And Results

Static manifests prove intent. Normalized evidence proves what ran, and result reports explain what happened.

The runner keeps Bruno's raw report only in a temporary directory. It removes headers, redacts sensitive keys and token-shaped values, limits response size/depth, and persists normalized evidence under:

```text
qa/results/global/evidence/<timestamp>-evidence.json
qa/results/modules/evidence/<module-id>/<timestamp>-evidence.json
```

Pass status requires an executed request, successful request status, no runtime error, at least one assertion/test observation, and every observation passing. A top-level Bruno `pass` value alone is insufficient.

Database assertion steps register Bruno `test` observations and fail the owning case on connection, query, expectation, or cleanup errors. Persist only the bounded assertion outcome in Bruno's normalized evidence; do not retain connection strings, credentials, or full database rows/documents.

Normalized evidence contains `executed`, `passed`, and per-case actual HTTP status, redacted response body/shape, and assertion/request failure detail. Successful observations may be written to `observed-rules.yaml` for audit only; they are never reused to change design expectations or generated assertions.

For every design-generated `flows.yaml` entry, normalized evidence also records
ordered step status, capture/use names, absence verification, and cleanup
verification. Capture/use/absence names come only from passing, uniquely named
`dev-ai:flow:*` Bruno test observations in the raw reporter output; the
materialized case contract supplies only the expected names. Missing events,
duplicate case IDs, failed events, or out-of-order steps fail reconciliation.

## Result Report

Every attempted run writes an immutable report under `qa/results/global/` or `qa/results/modules/<module-id>/`, including static/preflight failures that prevented requests.

```json
{
  "summary": {
    "total": 12,
    "executed": 10,
    "passed": 8,
    "failed": 2,
    "not_executed": 2
  },
  "modules": [],
  "failures": [],
  "not_executed": [],
  "manual_confirmation": []
}
```

Every failed or not-executed row contains module, case ID, interface, request summary, expected result/assertions, actual result, failure reason, failure categories, and `needs_manual_confirmation`.

Allowed categories are:

- `generation_failure`
- `insufficient_data`
- `environment_unavailable`
- `endpoint_unreachable`
- `request_failure`
- `assertion_failure`
- `insufficient_source_evidence`
- `manual_confirmation`

Do not replace these with one generic status. Static coverage, OpenAPI obligations, design traceability, and version warnings are supplemental fields.

## Module Evidence

Module runs validate `module-lock.yaml`, write only module-owned logs/evidence/results, and never update global locks or completion state. Generation must reach zero `manual_confirmation` items before a module is runnable; execution reports retain the field only to expose a gate failure, never to waive completion requirements.

The coordinator validates and merges module evidence before the final all-module run.

Run `dev-ai api-test aggregate` after independent module execution. It
requires the latest module report to contain exactly the module's current case
IDs, rejects missing or repeated evidence, reruns post-execution constraints,
and writes merged global evidence plus a result-first global report. A failed
module remains visible without hiding successful modules.

Before retaining any evidence or report, the shared post-execution constraints scan contracts, Bruno requests, evidence, and results for credentials.

## Mock-data Ledger

Each preparation attempt writes a redacted ledger below `qa/results/mock-data/`, including the environment, run namespace, selected modules, target data sources, estimate, one write-authorization decision, reused ranges, possible or verified creations, failures, cleanup authorization, cleanup results, and absence-verification results. Before every write, the ledger freezes that step's exact cleanup and verification scripts plus its non-secret runtime identifiers. Later cleanup uses only this frozen run-owned contract, so it remains available after source or case files change; it still refuses a changed environment or connection target.

If preparation is denied or fails, cases with setup steps are reported as `not_executed` with an insufficient-data reason; unrelated cases remain runnable. A retained or interrupted run can be cleaned later by its run ID.
