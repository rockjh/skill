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

The project-local runner translates the installed Bruno report into this shape.
The exact Bruno CLI output format is version-dependent, so keep this normalized
contract stable and review the adapter with the collection.

Use the platform entry point:

```bat
qa\execution\run.bat
qa\execution\run.bat --module "车辆管理"
```

```bash
./qa/execution/run.sh
./qa/execution/run.sh --module "车辆管理"
```

Both launchers call `run_bruno.py`, which validates `execution/config.yaml`,
loads `execution/environments/<active_environment>.bru` through `--env-file`,
runs static coverage and runtime preflight, invokes Bruno, normalizes the
temporary raw report, and reconciles execution evidence. The internal
preflight requires at least one representative route; without a successful
probe `execution_ready` remains false.

The coverage checker accepts either the normalized object above or a raw Bruno
JSON report. When given a raw report it derives `passed` from every
`assertionResults[].status` and `testResults[].status`; do not use Bruno's
top-level request status alone, because Bruno can report `status: pass` while
an inline assertion failed. For an isolated module report, pass that module's
contracts directory to the checker and run OpenAPI reconciliation separately
from the global contracts root.

Evidence is environment- and business-SHA-specific. Raw Bruno reports remain
in a temporary directory and omit all Headers and bodies. Keep only normalized,
redacted evidence with the SHA, environment name, executed/passed case IDs,
and flow capture names. A module run reports only that module, must not be
presented as global evidence, and cannot update `version-lock.yaml`.

Every generated request uses `{{BASE_URL}}`; authentication and common Headers
are injected once by `collection.bru` from the validated runtime payload. The
default `auth.mode: none` adds no authentication Header. `seres-sign` reads its
two configured credential variables from the selected Bruno environment and
uses fixed SHA-256 behavior plus fixed `sign`, `timestamp`, and `accesskey`
Header names. Bearer, API-key, and cookie modes read their configured variable.
OAuth2 bootstrap publishes a token consumed as bearer. Keep all sensitive
values in the environment; never place them in config, manifests, reports, or
request files.

Before committing the normalized report, run:

```bash
python scripts/check_artifact_safety.py qa/bruno qa/contracts
```
