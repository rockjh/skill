# Execution Script Policy

Keep every execution and validation entrypoint in the E2E project's single top-level `scripts/` directory:

```text
scripts/
  check_scenarios.py
  check_source_versions.py
  run_all.sh
  run_all.bat
  run_<中文场景名称>.sh
  run_<中文场景名称>.bat
```

Generate one Shell/Bat pair for every direct child of `scenarios/`. A scenario script derives the scenario name from its own `run_<中文场景名称>` filename, checks source and artifacts for that scenario, then runs only `scenarios/<中文场景名称>/test_<中文场景名称>.py`. The all-scenario scripts run both checks without `--scenario`, then run environment-independent tests under `tests/` and every scenario under `scenarios/`.

All scripts derive the project root from their own location, quote paths that may contain Chinese characters or spaces, use `python -m pytest`, forward every caller argument, and return the first failing command's non-zero status. They read `active_environment` through project configuration and must not set or override it, fallback URLs, credentials, VINs, ICCIDs, plan/package IDs, or other environment data.

## Shell templates

Write `.sh` files with LF endings. Set their executable bit when the repository/filesystem preserves executable modes.

`scripts/run_<中文场景名称>.sh`:

```sh
#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
SCRIPT_BASENAME=${0##*/}
SCENARIO_NAME=${SCRIPT_BASENAME#run_}
SCENARIO_NAME=${SCENARIO_NAME%.sh}

cd "$PROJECT_ROOT"
python "$SCRIPT_DIR/check_scenarios.py" --scenario "$SCENARIO_NAME"
python "$SCRIPT_DIR/check_source_versions.py" --scenario "$SCENARIO_NAME"
exec python -m pytest "scenarios/$SCENARIO_NAME/test_$SCENARIO_NAME.py" "$@"
```

`scripts/run_all.sh`:

```sh
#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

cd "$PROJECT_ROOT"
python "$SCRIPT_DIR/check_scenarios.py"
python "$SCRIPT_DIR/check_source_versions.py"
exec python -m pytest tests scenarios "$@"
```

With `set -e`, a failed check stops the script with that command's status. `exec` makes the final pytest status the script status.

## Bat templates

Avoid parenthesized error-handling blocks whose `%ERRORLEVEL%` expansion is easy to capture at the wrong time. Capture the status immediately after each command and use one exit path.

`scripts/run_<中文场景名称>.bat`:

```bat
@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "PROJECT_ROOT=%%~fI"
set "SCRIPT_BASENAME=%~n0"
set "SCENARIO_NAME=%SCRIPT_BASENAME:~4%"

pushd "%PROJECT_ROOT%"
if errorlevel 1 exit /b %ERRORLEVEL%

python "%SCRIPT_DIR%check_scenarios.py" --scenario "%SCENARIO_NAME%"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" goto finish

python "%SCRIPT_DIR%check_source_versions.py" --scenario "%SCENARIO_NAME%"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" goto finish

python -m pytest "scenarios\%SCENARIO_NAME%\test_%SCENARIO_NAME%.py" %*
set "EXIT_CODE=%ERRORLEVEL%"

:finish
popd
endlocal & exit /b %EXIT_CODE%
```

`scripts/run_all.bat`:

```bat
@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "PROJECT_ROOT=%%~fI"

pushd "%PROJECT_ROOT%"
if errorlevel 1 exit /b %ERRORLEVEL%

python "%SCRIPT_DIR%check_scenarios.py"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" goto finish

python "%SCRIPT_DIR%check_source_versions.py"
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" goto finish

python -m pytest "tests" "scenarios" %*
set "EXIT_CODE=%ERRORLEVEL%"

:finish
popd
endlocal & exit /b %EXIT_CODE%
```

`setlocal` contains variables, `%*` forwards the original arguments, and `endlocal & exit /b` returns the captured status. Keep generated Bat bodies ASCII by deriving the Chinese scenario name from `%~n0`; do not embed the literal scenario name or depend on a local code page.

## Checker contract

Both Python checkers accept optional `--scenario <中文场景名称>`. The value names exactly one direct scenario directory; missing, unknown, duplicated, or path-like values fail with a file-and-line diagnostic where applicable. Without the option, discover all scenario directories directly; do not use a global manifest.

Extend the existing `check_scenarios.py`; do not create another checker. In addition to the rules in [e2e-workflow.md](e2e-workflow.md), validate execution scripts:

- each scenario has both files and the all-scenario pair exists;
- each scenario script derives the exact scenario from its own filename, selects it in both checks, and targets its exact test file in pytest;
- all-scenario scripts call both checks without a scenario filter and target both environment-independent `tests/` and all of `scenarios/`;
- Shell uses `set -eu`, `"$@"`, LF, self-directory root resolution, and executable mode where supported;
- Bat uses an ASCII body, `setlocal`, `%~n0` name derivation, `%*`, self-directory root resolution, captured failure status, and `exit /b`;
- every pytest command is `python -m pytest`, and every path/value that may contain spaces is quoted;
- scripts contain no active-environment override, default URL, credential, VIN, ICCID, plan/package ID, or other business identifier.

Use parsed YAML and Python `ast` for scenario checks. Script checks may use narrowly scoped line-based rules because Shell and Bat parsers are not project dependencies; every such diagnostic includes file and line. Add one representative invalid fixture for each script-rule class to the existing checker test module and assert a non-zero exit code.
