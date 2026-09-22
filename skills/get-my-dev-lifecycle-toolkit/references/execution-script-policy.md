# Shared E2E execution policy

Generated projects do not contain gate engines, checker scripts, Python runner scripts, or copies of the shared runtime. The optional root `run-e2e.bat` and `run-e2e.sh` launchers only forward arguments to the installed `dltk e2e run`; they contain no gate logic. Execute only the installed commands:

Scenario and common business modules import evidence, preflight, polling, and restoration helpers from `dltk.e2e_runtime`; never recreate those helpers under `common/`.

```text
dltk e2e init --project .
dltk e2e check --project . --gate <stage>
dltk e2e source-status --project .
dltk e2e run --project .
dltk e2e run --project . --scenario <scenario-name>
dltk e2e run --project . --static-only
```

The engine owns the fixed stage order, content-addressed seals, one-hour session boundary, an active read-only local environment probe for every run request, source checks, collection, read-only smoke, business execution, JUnit validation, restoration, and final report. Caller pytest arguments are accepted only for the final business invocation and cannot change selection or success semantics.

`dltk e2e run` removes inherited pytest plugin and option injection, invokes subprocesses without a shell, and writes the authoritative redacted report to `artifacts/e2e-run.json`. Console output contains only a bounded summary and report pointer unless `--full` is explicit.

The project `.dltk.lock.json` binds execution to an exact dltk release and a separate E2E gate schema version. Missing or mismatched locks are precondition failures; copied older code is never used as a fallback.
