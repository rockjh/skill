# Bruno Execution Evidence

Static manifests prove intent; normalized execution evidence proves results.
The checker reports independent gates:

- `static_ok`: OpenAPI, manifests, UTF-8, Bruno registration, complete JSON
  bodies, request structure, exact assertions, scenario decisions, QA lock, and
  source-logic links reconcile.
- `completion_ok`: every case in the default all-module scope executed and passed,
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
mno-bruno-qa run
mno-bruno-qa run --module "AC-信息"
mno-bruno-qa reconcile --all --results execution-evidence.json \
  --preflight-results preflight.json
```

The runner validates the business version, project script version or shared
CLI mode, QA lock, execution config, active environment, offline OpenAPI
fingerprint, Bruno CLI, and a representative route before execution. Business
and deployment version mismatches are red warnings rather than execution
blockers. Module evidence remains module-local and never updates the global
business lock.

The console and `qa/logs/` record stage progress, every case status, final
success/failure totals, failed case IDs, and repeated version warnings. Each
invocation creates a new timestamped log.

Every newly generated request uses `{{baseUrl}}` (legacy `{{BASE_URL}}` remains
readable). The CLI parses environment
`headers {}`, resolves its variables, and injects the resulting map through
`collection.bru`; request-local Headers win. Signing is disabled unless
`config.yaml` selects `sign.provider: seres`, which reads `ACCESS_KEY` and `SECRET_KEY`
from the environment.

Before committing evidence, run:

```bash
python qa/scripts/check_artifact_safety.py qa/bruno qa/contracts
```
