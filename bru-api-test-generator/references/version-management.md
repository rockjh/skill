# Business And QA Version Management

Keep two independent locks under `qa/data/contracts`.

`version-lock.yaml` records a digest of current business files outside generated QA and build directories. The checker reads only the current workspace; it does not inspect version-control history. A digest change is conservatively classified as API-impacting because a filesystem snapshot cannot prove which individual behavior changed.

`qa-lock.yaml` records the current OpenAPI, module, case, and generation-state fingerprints. Generation and materialization refresh it; checks and execution reject stale fingerprints.

Initialize and gate the business lock:

```bash
python qa/scripts/check_version_compatibility.py APP qa/data/contracts --init
python qa/scripts/check_version_compatibility.py APP qa/data/contracts --phase before-generate
python qa/scripts/check_version_compatibility.py APP qa/data/contracts --phase before-execute
python qa/scripts/check_version_compatibility.py APP qa/data/contracts --phase complete \
  --completion-report execution-evidence.coverage.json --write --tests-adapted
```

Initialization creates a draft baseline. A completion write requires a successful v2 `full-matrix-strict` global report with every strict check enabled, no errors, `status: verified`, and `completion_ok: true`. API-impacting digest changes also require `--tests-adapted`. Pre-execution and completion reject a draft or missing lock.

For a remote `baseUrl`, configure `versionPath` in the active Bruno environment when the deployment exposes a version endpoint. `versionJsonPath`, `versionHeader`, or `expectedVersion` can select a non-standard value. The endpoint must share the `baseUrl` origin, and a failed or mismatched version gate must be resolved before execution.

`impact-rules.yaml` remains available for classifying named current-workspace paths supplied by other tooling. Unknown business paths are treated conservatively.
