# Business And QA Version Management

Keep two independent locks under `qa/contracts`.

`version-lock.yaml` records a digest of current business files outside generated QA and build directories, plus independent OpenAPI and selected design-document summaries. The checker reads only the current workspace; it does not inspect version-control history. A digest change is conservatively classified as API-impacting because a filesystem snapshot cannot prove which individual behavior changed.

`qa-lock.yaml` records the current OpenAPI, module, case, and generation-state fingerprints. Generation and materialization refresh it; checks and execution reject stale fingerprints.

Initialize and gate the business lock through the shared CLI workflow:

```bash
dltk api-test init --qa-root qa --design-root docs/design
dltk api-test generate --qa-root qa --openapi qa/contracts/openapi.json --design-root docs/design --source-root APP
dltk api-test preflight --qa-root qa
dltk api-test run --qa-root qa
```

The shared runtime checks the source/version lock during generation, preflight,
and execution. Initialization creates a draft baseline. The first orchestrated
preflight/run may use that draft only while its source digest is unchanged; a
normal standalone `before-execute` check still requires `current`. Completion requires a
successful v2 `full-matrix-strict` global report with every strict check enabled,
no errors, `status: verified`, and `completion_ok: true`. API-impacting digest
changes require the affected module tests to be adapted and rerun before the
lock can advance.

The runner advances a successful global execution automatically. The same
version operations are available explicitly without project-local scripts:

```bash
dltk api-test scripts version-check --qa-root qa --phase before-generate
dltk api-test scripts version-check --qa-root qa --phase before-execute
dltk api-test scripts version-complete --qa-root qa --completion-report qa/results/<strict-report>.json --tests-adapted
```

For a remote `baseUrl`, configure `versionPath` in the active Bruno environment when the deployment exposes a version endpoint. `versionJsonPath`, `versionHeader`, or `expectedVersion` can select a non-standard value. The endpoint must share the `baseUrl` origin, and a failed or mismatched version gate must be resolved before execution.

`impact-rules.yaml` remains available for classifying named current-workspace paths supplied by other tooling. Unknown business paths are treated conservatively.
