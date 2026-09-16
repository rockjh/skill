# bruno-api-test-generator CLI

The skill is installable as a Python package with the `bruno-api-test-generator` console
command:

```bash
pipx install /path/to/bru-api-test-generator
```

Supported commands:

```text
bruno-api-test-generator init
bruno-api-test-generator generate
bruno-api-test-generator materialize
bruno-api-test-generator check
bruno-api-test-generator run
bruno-api-test-generator preflight
bruno-api-test-generator reconcile
bruno-api-test-generator worker-start --module <module>
bruno-api-test-generator worker-check --module <module> --stage generation
bruno-api-test-generator aggregate
bruno-api-test-generator scripts sync
bruno-api-test-generator scripts check
```

Transition mode is the default: `init` synchronizes the complete Python bundle
into `qa/scripts`, writes `README.md`, and creates `scripts-version.yaml` with
skill/script versions, source, aggregate/per-file SHA, and synchronization
time. Run `scripts check` after upgrading the installed skill and `scripts
sync` to update a project explicitly.

`worker-start` records the current Git HEAD and workspace file hashes for one
assigned module. `worker-check` derives changed paths from that snapshot and
fails when a module worker touched coordinator-owned, cross-module, or business
paths. The snapshot itself is coordinator-owned.

Shared mode is opt-in with `init --shared-cli` or `generate --shared-cli`.
`qa/execution/config.yaml` records `tooling: shared-cli`; launchers call the
installed command and the business repository keeps only Bruno, contracts,
and execution assets.

`generate --incremental` updates only affected modules and accepts repeated
`--source-root` arguments for source enhancement. `check` performs strict
static reconciliation. `preflight` builds a fresh static report and evaluates
runtime readiness. `reconcile` requires both normalized results and preflight
results. `run` defaults to all modules and accepts only the optional `--module`
scope selector. It performs version and QA-lock checks, preflight, Bruno
execution, normalization, reconciliation, per-case reporting, timestamped
logging, and the global completion pipeline. Version drift warns without
blocking execution.

`aggregate` merges the latest result and evidence artifact for every module,
checks missing/stale case IDs, runs post-execution constraints, and writes a
timestamped report under `qa/results/global/` plus merged evidence under
`qa/evidence/global/`. Its exit code is nonzero when a required case or
reconciliation check fails.
