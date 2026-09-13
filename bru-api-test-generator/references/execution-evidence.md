# Bruno Execution Evidence

Static manifests prove intent; CI must also publish a small normalized JSON evidence file so flow order, captures, and cleanup can be checked independently of Bruno CLI report formats.

The coverage checker reports two independent gates:

- `static_ok` means manifests, UTF-8 text, Bruno registration, assertions, and
  the offline OpenAPI inventory reconcile.
- `completion_ok` means every required case was executed and passed, flow
  cleanup was verified, and no pending exclusion remains. The status is
  `draft` without execution evidence, `runnable` after a passed runtime
  preflight, `verified` only after completion, and `blocked` for any failure.

The JSON report also separates `inventory_endpoints`, `endpoints_with_cases`,
`excluded_endpoints`, `generated_cases`, `executed_cases`, `passed_cases`,
`happy_path_endpoints`, and `verified_flows`; do not summarize inventory as
coverage. `pending_success_endpoints` identifies reachable operations that
still lack a dedicated success case; `blocked_modules` identifies module-local
failures. A report with
`contract_provenance_unverified: true` cannot reach `verified`.

Example `execution-evidence.json`:

```json
{
  "executed": ["USER_CREATE_OK", "USER_UPDATE_OK", "USER_QUERY_AFTER_UPDATE", "USER_DELETE_OK", "USER_QUERY_AFTER_DELETE"],
  "passed": ["USER_CREATE_OK", "USER_UPDATE_OK", "USER_QUERY_AFTER_UPDATE", "USER_DELETE_OK", "USER_QUERY_AFTER_DELETE"],
  "flows": {
    "USER_CRUD_FLOW": {
      "status": "passed",
      "cleanup_verified": true,
      "steps": [
        {"case_id": "USER_CREATE_OK", "status": "passed", "captures": ["user_id"], "used_captures": []},
        {"case_id": "USER_UPDATE_OK", "status": "passed", "captures": [], "used_captures": ["user_id"]},
        {"case_id": "USER_QUERY_AFTER_UPDATE", "status": "passed", "captures": [], "used_captures": ["user_id"]},
        {"case_id": "USER_DELETE_OK", "status": "passed", "captures": [], "used_captures": ["user_id"]},
        {"case_id": "USER_QUERY_AFTER_DELETE", "status": "passed", "captures": [], "used_captures": ["user_id"], "asserted_absent": true}
      ]
    }
  }
}
```

The project-local Bruno scripts or CI adapter may translate the installed Bruno report into this shape. The exact Bruno CLI output format is version-dependent, so keep this normalized contract stable and review the adapter with the collection.

Run:

```bash
python qa/scripts/normalize_bruno_report.py bruno-report.json --output execution-evidence.json
python qa/scripts/runtime_preflight.py --base-url "${BASE_URL}" --auth-config qa/contracts/request-auth.yaml --output preflight.json

Add `--public-path`/`--admin-path` when the service exposes those probes. They
are optional capabilities; use `--require-public-route` or
`--require-admin-baseline` only when the project declares them mandatory.
python qa/scripts/check_api_coverage.py qa/contracts qa/bruno --openapi qa/contracts/openapi.json --require-scenarios --require-auth --auth-config qa/contracts/request-auth.yaml --preflight-results preflight.json --results execution-evidence.json --json > execution-evidence.coverage.json
python qa/scripts/validate_flow_execution.py qa/contracts/modules/<module-directory>/flows.yaml --contracts-root qa/contracts --endpoints qa/contracts/modules/<module-directory>/endpoints.yaml --results execution-evidence.json
```

The coverage checker accepts either the normalized object above or a raw Bruno
JSON report. When given a raw report it derives `passed` from every
`assertionResults[].status` and `testResults[].status`; do not use Bruno's
top-level request status alone, because Bruno can report `status: pass` while
an inline assertion failed. For an isolated module report, pass that module's
contracts directory to the checker and run OpenAPI reconciliation separately
from the global contracts root.

Evidence is environment- and business-SHA-specific. Do not commit raw Bruno reports when they can contain authorization headers or response tokens; keep only a normalized, redacted report with the SHA, environment name, executed/passed case IDs, and flow captures metadata. A module report must not be presented as global evidence: run the coverage check once per module or emit a normalized report containing all modules.

Every generated request includes the Bruno `script:pre-request` block selected
by `request-auth.yaml`. The default `seres-sign` mode reads `BASE_URL`,
`SECRET_KEY`, and `ACCESS_KEY` from the selected Bruno environment, adds
`timestamp`, `accesskey`, and the SHA-256 `sign` header, and fails before
sending when any required variable is missing. Its URL/body/query inputs,
timestamp parameter, signing Header names, and extra environment-backed
Headers may be configured in the template. Bearer, API-key, and custom Header
token modes use their configured environment names. Keep all values in the
environment; never place them in manifests or `.bru` files.

Before committing the normalized report, run:

```bash
python scripts/check_artifact_safety.py qa/bruno qa/contracts
```
