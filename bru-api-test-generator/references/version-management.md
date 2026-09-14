# Business And QA Version Management

Keep two independent locks under `qa/contracts`.

`version-lock.yaml` records a non-empty business Git commit and a digest of
tracked files outside `qa/**`. Its dirty check also excludes `qa/**`, so
regenerating manifests, Bruno files, or reports does not invalidate business
compatibility. An empty `git rev-parse HEAD` result is an error and is never
written.

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
| business code | Controller, DTO, Service, errors, validation, security, config, Flyway | review affected API modules |
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
`status: verified` and `completion_ok: true`. API-impacting changes also require
`--tests-adapted`. A checkout without Git uses a filesystem digest in draft
mode and cannot fabricate a commit identity.

`impact-rules.yaml` can narrow repository-specific business patterns, but keep
Controller, DTO, Service, error-code, security, configuration, migration, and
exception/serialization paths conservative. The checker reports the three
change classes separately where Git path evidence is available.

Refresh `qa-lock.yaml` only from the current `generation-state.yaml`; `check`
and `reconcile` reject a lock whose OpenAPI, module, case, or generation-state
fingerprint differs.
