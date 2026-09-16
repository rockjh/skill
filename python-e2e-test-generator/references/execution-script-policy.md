# Execution Script Policy

Keep validation and execution entrypoints in the generated project's top-level `scripts/` directory:

```text
scripts/
  check_scenarios.py
  check_source_versions.py
  run_e2e.py
  run.sh
  run.bat
```

`run_e2e.py` is the single cross-platform stage orchestrator. Shell and Bat files are thin unified launchers so validation order and exit behavior cannot drift between platforms. Generate no per-scenario launcher. With no `--scenario`, every direct scenario directory runs; `--scenario <中文场景名称>` selects exactly one.

All entrypoints derive the project root from their own location, quote paths, use the current Python interpreter for child modules, forward only presentation/reporting pytest arguments to final business tests, and return the first failed gate's non-zero status. Selection, marker, collection-only, fail-fast, plugin, and positional arguments are rejected. Entry points never inject fallback URLs, credentials, business IDs, source-specific configuration keys, or dangerous control authorization.

## Required orchestration

`run_e2e.py` accepts optional `--scenario <中文场景名称>`, optional `--static-only`, and final-business-only pytest arguments. It invokes subprocesses without a shell and executes exactly this order:

1. `check_scenarios.py --gate workspace_inventory`
2. `check_scenarios.py --gate dependency_topology`
3. `check_scenarios.py --gate initial_configuration`
4. `check_scenarios.py --gate runtime_probe`
5. `check_scenarios.py --gate control_matrix [--scenario ...]`
6. `check_scenarios.py --gate scenario_split [--scenario ...]`
7. `check_scenarios.py --gate scenario_ownership [--scenario ...]`
8. `check_scenarios.py --gate shared_integration [--scenario ...]`
9. `check_scenarios.py --gate static [--scenario ...]`
10. environment-independent checker/shared-logic tests (`environment_tests` in the report)
11. `check_source_versions.py [--scenario ...]`
12. `python -m pytest --collect-only` for the selected scenario or all scenarios
13. read-only smoke tests when `discovery/workspace.yaml.runtime_probe.requested` is true
14. selected `business_e2e` scenario tests
15. restoration result verification against every scenario-owned resource

Stop before the next stage after any failure. Scenario fixtures must still finish registered cleanup and restoration when a business test fails. The orchestrator retains that failing status after cleanup diagnostics are written.

When runtime probing was not requested, read-only smoke is reported `N/A` and no endpoint call is made. When it was requested, each selected scenario must emit a safe-method endpoint event; missing per-scenario coverage or a failed smoke request is a failed gate. The business stage never runs after failed smoke. Static-only work runs through collection and reports smoke, business, and restoration as `N/A`; do not make a full runtime runner silently pass by skipping missing environment values.

For `requested: true` and `outcome: completed`, the ordered runtime-probe gate performs bounded live checks rather than trusting YAML text: PID command references must still match, declared listeners must accept a TCP connection, and credential-free local GET/HEAD targets must return the recorded status. These checks never run in aggregate diagnostic modes.

The orchestrator runs ready scenarios one at a time and does not infer business success from exit code alone. A passed scenario must emit a run-bound `business_entered` event and exact `(control, action, side_effect)` evidence for every planned step; API controls additionally require a business-phase endpoint event. The adapter records `side_effect` from the real operation, not from an untrusted scenario claim. Each business and smoke invocation is validated only against JSON files newly created by that invocation, and an invocation that emits another scenario's identity fails. Earlier, sibling, asset-test, or stale events cannot satisfy a later scenario. Scenario files cannot emit control or endpoint evidence. Only common adapters that execute the operation may emit it, in the same function, using a non-constant result and a correlation value passed to the operation. The runner rejects correlation values outside the scenario's declared keys, owned resources, and mutable controls. Any non-ready scenario in the selected scope prevents all business execution, so a default all-scenario run cannot silently pass a ready subset. The report records each scenario as `passed`, `failed`, or `N/A` with a reason.

Every run atomically writes `artifacts/e2e-run.json` with this stable top-level schema:

```json
{
  "schema_version": 1,
  "run_id": "<opaque-run-id>",
  "started_at": "<UTC-ISO-8601>",
  "finished_at": "<UTC-ISO-8601>",
  "selected_scenario": null,
  "static_only": false,
  "stages": {"<stage>": {"status": "passed|failed|N/A", "exit_code": null}},
  "discovery": {},
  "source_versions": [],
  "scenarios": [],
  "evidence_diagnostics": []
}
```

`discovery` contains the actual repository/module inventory, topology, configuration provenance, and runtime-probe record. Each scenario result contains owner, generation mode, nullable degradation reason, per-scenario smoke status, explicit business outcome, planned and observed controls, redacted endpoint calls, `business_entered`, and all restoration events. Evidence files have exact schemas and must carry the current opaque `run_id`; malformed, stale, unsafe-method, unknown-scenario, or non-delta events cannot satisfy the run. For a write scenario that entered business execution, the union of successful restoration resources must exactly equal its contract-derived owned-resource identities. Restoration for a preflight failure, a never-entered business step, or an executed read-only scope is `N/A`. The entire report is recursively redacted before persistence, while non-secret `connection_source` reference metadata remains visible.

## Launchers

Write Shell files with LF endings and executable mode where supported.

`scripts/run.sh`:

```sh
#!/usr/bin/env sh
set -eu

# Usage: ./scripts/run.sh                         # run all scenarios
# Usage: ./scripts/run.sh --scenario "scenario"  # run one scenario
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PYTHONDONTWRITEBYTECODE=1
exec python "$SCRIPT_DIR/run_e2e.py" "$@"
```

Bat launchers keep ASCII bodies and forward the same optional selector.

`scripts/run.bat`:

```bat
@echo off
setlocal
rem Usage: scripts\run.bat                         (run all scenarios)
rem Usage: scripts\run.bat --scenario "scenario"  (run one scenario)
set "SCRIPT_DIR=%~dp0"
set "PYTHONDONTWRITEBYTECODE=1"
python "%SCRIPT_DIR%run_e2e.py" %*
set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%
```

## Orchestrator safeguards

- Resolve scenario names only as exact direct children of `scenarios/`; reject missing, duplicate, path-like, or traversal values.
- Use `sys.executable`, argument arrays, `cwd=project_root`, and `check=False`; never build command strings.
- Keep environment-independent tests separate from runtime smoke so lack of endpoints cannot hide static failures.
- Run static/asset/AST validation before importing any project test. Project `conftest.py`, `pytest.ini`, and pytest sections in `tox.ini` or `setup.cfg` are forbidden; fixture modules must be imported explicitly. Clear inherited `PYTEST_ADDOPTS` and `PYTEST_PLUGINS`, reject project `addopts`, `required_plugins`, `usefixtures`, collection-selection settings, `pytest_plugins`, and collection-ignore globals, disable third-party plugin autoload, and set `PYTHONDONTWRITEBYTECODE=1` for every runner subprocess and launcher.
- Keep `E2E_EVIDENCE_DIR` and `E2E_RUN_ID` out of discovery, static, environment-independent tests, source-version checks, and collection. Add them only to the isolated smoke and business subprocess environment.
- Write a separate JUnit XML file for every smoke and business subprocess and reject missing/malformed output, zero executed tests, or any skip/xfail/failure/error. Use a fixed `--confcutdir` so parent fixtures cannot alter execution.
- Pass only allowlisted display/reporting pytest arguments to the final business invocation. Fixed validation/collection/smoke gates and business selection cannot be weakened with collection, `-k`, marker filters, max-fail, fail-fast, path, or plugin options.
- Select runtime tests by a registered `read_only_smoke` marker and their literal `record_endpoint` scenario identity, so a single-scenario launcher cannot execute sibling smoke modules. Select business tests by a registered `business_e2e` marker. The checker requires every scenario test to carry exactly one business marker.
- Read-only smoke tests may use direct GET/HEAD or a common `read_only_rpc` adapter bound to a source anchor and positive timeout. Each endpoint event must match a preceding direct safe transport call in the same straight-line block and derive status, summary, and `verified` from its result. HTTP 5xx is failure even when declared expected. The checker rejects unknown helpers, fixture `.get()`, `urlopen(data=...)`, write methods, message publication, job triggers, mutable configuration, and control SQL in smoke modules.
- Business execution relies on scenario preflight for runtime values, isolation, authorization, and cleanup registration. Missing requirements fail rather than skip.
- Restoration verification is part of finalizers and the structured result. A cleanup failure cannot be swallowed, even when the primary test already failed.

## Checker contract

Both checkers accept optional `--scenario`. `check_scenarios.py` additionally requires an ordered stage gate (`workspace_inventory`, `dependency_topology`, `initial_configuration`, `runtime_probe`, `control_matrix`, `scenario_split`, `scenario_ownership`, or `shared_integration`) or aggregate `discovery|contracts|static|all`. Aggregate `discovery`, `contracts`, and `all` are diagnostics and never write predecessor seals; only the eight ordered gates authorize `static`. Ordered seals are bound to a fresh opaque session/run ID, a fixed next stage, and a one-hour lifetime; a new inventory run invalidates every historical seal. Without a scenario, discover direct scenario directories; do not use a global manifest.

Extend the existing checker rather than creating another overlapping validator. In addition to [e2e-workflow.md](e2e-workflow.md), validate:

- the unified `run.sh`, `run.bat`, and `run_e2e.py` exist, and no `run_all.*` or per-scenario launcher exists;
- launchers resolve their own directory, document all-scenario and single-scenario usage, quote paths, forward arguments, and return the child status;
- Shell uses `set -eu`, `exec`, LF, and executable mode where supported;
- Bat uses an ASCII body, `setlocal`, `%*`, captured status, and `exit /b`;
- `run_e2e.py` uses the exact stage order, filters only the final business target with caller arguments, and invokes no shell;
- discovery determines whether smoke is run or reported `N/A`;
- smoke selection cannot execute side effects, generic `send`, or a `request` whose GET/HEAD method is not statically proven, and business selection cannot bypass preflight;
- project-owned Python is scanned recursively; cache, virtualenv, dependency, and build directories are skipped. Generated projects may not contain `conftest.py`, define pytest hooks/plugin lists/ignore lists, use skip/xfail/importorskip (including aliases), or configure options that alter plugin loading, collection, execution, or exit status;
- scripts contain no environment override, default connection, credential, business identifier, source-specific key, or control authorization.
- the installed manifest contains exactly every versioned fixed asset; upgrading removes unchanged legacy `run_all.*` and per-scenario launchers and refuses to remove a drifted legacy launcher without `--force`; the Shell launcher receives executable mode on Unix.

Script validation may use Python AST and narrowly scoped line rules for Shell/Bat. Every diagnostic includes file and line. Add one representative invalid fixture for each rule class to `tests/test_check_scenarios.py` and assert a non-zero result.
