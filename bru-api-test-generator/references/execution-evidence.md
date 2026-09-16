# Bruno Execution Evidence And Results

Static manifests prove intent. Normalized evidence proves what ran, and result reports explain what happened.

The runner keeps Bruno's raw report only in a temporary directory. It removes headers, redacts sensitive keys and token-shaped values, limits response size/depth, and persists normalized evidence under:

```text
qa/evidence/global/<timestamp>-evidence.json
qa/evidence/modules/<module-id>/<timestamp>-evidence.json
```

Pass status requires an executed request, successful request status, no runtime error, at least one assertion/test observation, and every observation passing. A top-level Bruno `pass` value alone is insufficient.

Normalized evidence contains `executed`, `passed`, and per-case actual HTTP status, redacted response body/shape, and assertion/request failure detail. Successful observations are also written to `observed-rules.yaml` and reused during later generation to improve request values and exact assertions.

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

Do not replace these with one generic status. Static coverage, OpenAPI obligations, source mappings, and version warnings are supplemental fields.

## Module Evidence

Module runs validate `module-lock.yaml`, write only module-owned logs/evidence/results, and never update global locks or completion state. Explained review cases remain visible in `manual_confirmation` but are excluded from module completion requirements; their failure does not downgrade unrelated cases.

The coordinator validates and merges module evidence before the final all-module run.

Run `bruno-api-test-generator aggregate` after independent module execution. It
requires the latest module report to contain exactly the module's current case
IDs, rejects missing or repeated evidence, reruns post-execution constraints,
and writes merged global evidence plus a result-first global report. A failed
module remains visible without hiding successful modules.

Before retaining any evidence or report, the shared post-execution constraints scan contracts, Bruno requests, evidence, and results for credentials.
