# Bruno Execution Evidence

Static manifests prove intent; normalized execution evidence proves results.
The checker reports independent gates:

- `static_ok`: OpenAPI, manifests, UTF-8, Bruno registration, complete JSON
  bodies, request structure, exact assertions, scenario decisions, QA lock, and
  source-logic links reconcile.
- `completion_ok`: every case in the selected risk scope executed and passed,
  required flow cleanup passed, and no pending exclusion remains.

States are `draft`, `runnable`, `verified`, or `blocked`. Inventory counts are
kept separate from generated, executed, passed, happy-path, and verified-flow
counts. A static draft is never reported as verified.

The runner stores the raw Bruno report in a temporary directory, derives pass
status from every assertion/test observation, and writes only normalized,
redacted evidence. Bruno's top-level request status alone is insufficient.

```json
{
  "executed": ["USER_CREATE_OK", "USER_DELETE_OK"],
  "passed": ["USER_CREATE_OK", "USER_DELETE_OK"],
  "flows": {
    "USER_CRUD_FLOW": {
      "status": "passed",
      "cleanup_verified": true,
      "steps": [
        {"case_id": "USER_CREATE_OK", "status": "passed", "captures": ["user_id"]},
        {"case_id": "USER_DELETE_OK", "status": "passed", "used_captures": ["user_id"]}
      ]
    }
  }
}
```

Use the platform launchers or shared CLI:

```bash
mno-bruno-qa run --plan smoke
mno-bruno-qa run --plan regression --confirm-write
mno-bruno-qa run --module ac --risk read-only
mno-bruno-qa reconcile --results execution-evidence.json \
  --preflight-results preflight.json
```

The runner validates the business version, project script version or shared
CLI mode, QA lock, minimal execution config, active environment, risk
confirmations, offline OpenAPI fingerprint, Bruno CLI, and a representative
route before execution. Module evidence remains module-local and never updates
the global business lock.

Every newly generated request uses `{{baseUrl}}` (legacy `{{BASE_URL}}` remains
readable). The CLI parses environment
`headers {}`, resolves its variables, and injects the resulting map through
`collection.bru`; request-local Headers win. Signing is disabled unless
`config.yaml` selects `seres-sign`, which reads `ACCESS_KEY` and `SECRET_KEY`
from the environment.

Before committing evidence, run:

```bash
python qa/scripts/check_artifact_safety.py qa/bruno qa/contracts
```
