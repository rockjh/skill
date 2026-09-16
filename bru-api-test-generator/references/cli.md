# mno-bruno-qa CLI

The skill is installable as a Python package with the `mno-bruno-qa` console
command:

```bash
pipx install /path/to/bru-api-test-generator
```

Supported commands:

```text
mno-bruno-qa init
mno-bruno-qa generate
mno-bruno-qa materialize
mno-bruno-qa check
mno-bruno-qa run
mno-bruno-qa preflight
mno-bruno-qa reconcile
mno-bruno-qa scripts sync
mno-bruno-qa scripts check
```

Transition mode is the default: `init` synchronizes the complete Python bundle
into `qa/scripts`, writes `README.md`, and creates `scripts-version.yaml` with
skill/script versions, source, aggregate/per-file SHA, and synchronization
time. Run `scripts check` after upgrading the installed skill and `scripts
sync` to update a project explicitly.

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
