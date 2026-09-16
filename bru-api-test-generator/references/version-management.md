# Business And QA Version Management

Keep two independent locks under `qa/contracts`.

`version-lock.yaml` records a non-empty business Git commit and a digest of
tracked files outside `qa/**`. Its dirty check also excludes `qa/**`, so
regenerating manifests, Bruno files, or reports does not invalidate business
compatibility. An empty `git rev-parse HEAD` result is an error and is never
written.

The standalone checker keeps non-zero exit codes for CI and explicit lock
maintenance. The generated Bruno runner treats those exit codes as red
warnings, repeats them in its final summary, and continues the current test
execution.

`qa-lock.yaml` records:

```yaml
version: 1
openapi_sha256: ...
module_fingerprints: {}
case_fingerprints: {}
generation_state_fingerprint: ...
```

Classify repository changes as:

| Class | Examples | Effect |
| --- | --- | --- |
| business code | Controller, application, domain, repository, integration, infrastructure, adapter, DTO, errors, validation, security, config, SQL/Flyway | review affected API modules |
| QA assets | `qa/**` contracts, Bruno requests, locks, reports | refresh QA lock; business lock stays current |
| unrelated | docs, comments, formatting, unrelated assets | record review; no case rewrite unless behavior changed |

Initialize and gate the business lock:

```bash
python qa/scripts/check_version_compatibility.py APP qa/contracts --init
python qa/scripts/check_version_compatibility.py APP qa/contracts --phase before-generate
python qa/scripts/check_version_compatibility.py APP qa/contracts --phase before-execute
python qa/scripts/check_version_compatibility.py APP qa/contracts --phase complete \
  --completion-report execution-evidence.coverage.json --write --tests-adapted
```

Initialization is `draft`, not execution evidence. A completion write requires
The completion report must be v2 `full-matrix-strict`, cover the global `all`
scope, set every strict-check field, contain no errors, and report both
`status: verified` and `completion_ok: true`. API-impacting changes also require
`--tests-adapted`. A successful explicit update advances the lock to
`status: current`; `before-execute` and `complete` reject draft or missing lock
status. A checkout without Git uses a filesystem digest in draft mode and
cannot fabricate a commit identity.

For a remote `baseUrl`, local Git state cannot prove which build is deployed.
Configure `versionPath` in the active Bruno environment so the runner can query
the deployment directly. It compares common JSON version/Git fields with the
locked business commit, or with `expectedVersion` when supplied. Use
`versionJsonPath` or `versionHeader` for a non-standard response. The endpoint
must share the `baseUrl` origin. An absent endpoint, failed probe, or mismatch
is warning-only and is always preserved in the timestamped execution log.

`impact-rules.yaml` can narrow repository-specific business patterns. By default only explicit test and documentation patterns are non-API; unknown business paths remain API-impacting. Keep Controller, application, domain, repository, integration, infrastructure, adapter, DTO, error-code, security, configuration, migration, and exception/serialization paths conservative. The checker reports the three
change classes separately where Git path evidence is available.

Refresh `qa-lock.yaml` only from the current `generation-state.yaml`; `check`
and `reconcile` reject a lock whose OpenAPI, module, case, or generation-state
fingerprint differs.
