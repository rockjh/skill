# Bruno Execution Evidence

Static manifests prove intent; CI must also publish a small normalized JSON evidence file so flow order, captures, and cleanup can be checked independently of Bruno CLI report formats.

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
python qa/scripts/check_api_coverage.py qa/contracts qa/bruno --openapi qa/contracts/openapi.json --results execution-evidence.json
python qa/scripts/validate_flow_execution.py qa/contracts/flows.yaml --results execution-evidence.json
```

The coverage checker accepts either the normalized object above or a raw Bruno
JSON report. When given a raw report it derives `passed` from every
`assertionResults[].status` and `testResults[].status`; do not use Bruno's
top-level request status alone, because Bruno can report `status: pass` while
an inline assertion failed. For an isolated module report, pass that module's
contracts directory to the checker and run OpenAPI reconciliation separately
from the global contracts root.

Evidence is environment- and business-SHA-specific. Do not commit raw Bruno reports when they can contain authorization headers or response tokens; keep only a normalized, redacted report with the SHA, environment name, executed/passed case IDs, and flow captures metadata. A module report must not be presented as global evidence: run the coverage check once per module or emit a normalized report containing all modules.

Before committing the normalized report, run:

```bash
python scripts/check_artifact_safety.py qa/bruno qa/contracts
```
