# Bruno Execution Configuration

`qa/execution/config.yaml` has exactly four responsibilities:

```yaml
active_environment: local
tooling: project-scripts
coverage_profile: full-matrix
sign:
  provider: disabled
```

Allowed tool modes are `project-scripts` and `shared-cli`. Allowed coverage profiles are `contract-draft` and `full-matrix`; `verified` is an execution state, not a profile.

SERES signing is structured:

```yaml
active_environment: local
tooling: project-scripts
coverage_profile: full-matrix
sign:
  provider: seres
  version: v1
```

Unknown fields or unsupported signing providers/versions are errors. Do not store authentication modes, Header/Cookie values, token/key selectors, URLs, or path rules in this file.

## Environment Format

Keep environments outside the Bruno collection under `qa/execution/environments/`. The skill extends Bruno's environment syntax with one readable `headers {}` block:

```bru
vars {
  baseUrl: http://127.0.0.1:9527
  AUTH_TOKEN: ""
  SESSION_COOKIE: ""
  OPERATOR_INFO: ""
  ACCESS_KEY: ""
  SECRET_KEY: ""
}

headers {
  Authorization: "Bearer {{AUTH_TOKEN}}"
  Cookie: "{{SESSION_COOKIE}}"
  operatorInfo: "{{OPERATOR_INFO}}"
}
```

Every non-empty Header becomes a request Header. An unresolved or empty sensitive value is not injected. A request-local Header wins. A negative case omits a common Header with:

```yaml
request:
  omit_common_headers:
    - Authorization
```

The runner parses the custom Header block, resolves variables, passes Bruno a temporary native environment, and sends the resolved map to the collection pre-request script. Do not duplicate common Header logic in request files.

When `sign.provider` is `seres`, `collection.bru` reads `ACCESS_KEY` and `SECRET_KEY` from the active environment. Version `v1` uses the fixed SHA-256 algorithm and `sign`, `timestamp`, and `accesskey` Headers. A disabled provider adds no signing Headers.

## Execution Scope

The launcher has one optional scope selector:

```bat
qa\execution\run.bat
qa\execution\run.bat --module "AC-信息"
```

No argument runs every module. `--module` accepts a module ID, display name, directory, or OpenAPI Tag and runs every registered request in that module. The generated launchers contain these examples as comments. There are no all/read/write/external-confirmation, named-plan, or risk-filter parameters.

Risk is retained in case metadata and the run report. It never changes the selected scope or requires an execution flag.

Bruno executes a directory directly instead of using tag filtering:

```text
no argument -> bru run qa/bruno -r
--module x  -> bru run qa/bruno/<module-directory> -r
```

`.bru` tags contain only the case risk for inspection and reporting.

## Output And Logs

The runner prints stage progress and a `PASS` or `FAIL` line for every selected case. Its final summary contains total, success count, failure count, failed case IDs, and version warnings. Every invocation creates a plain-text log under `qa/logs/`, named with a high-resolution timestamp and `run-bruno-<scope>`.

## Local And Remote Versions

Local source and `version-lock.yaml` compatibility are checked before execution. Any mismatch is a red warning, repeated in the summary, and does not block the run.

For a remote target, add the version endpoint to the active environment:

```bru
vars {
  baseUrl: https://api.example.com
  versionPath: /actuator/info
  expectedVersion: 1.8.2
  versionJsonPath: build.version
  versionHeader: ""
}
```

`expectedVersion` is optional and defaults to the business commit in `version-lock.yaml`. `versionJsonPath` and `versionHeader` are optional selectors; without them the runner tries common JSON version and Git commit fields. `versionPath` may be relative or absolute but must remain on the `baseUrl` origin so environment credentials are not sent elsewhere. Missing configuration, unreachable endpoints, and mismatches warn and continue. Remote execution skips the local OpenAPI producer PID check.

## Tooling Modes And Migration

`project-scripts` keeps a synchronized `qa/scripts` bundle. Its `scripts-version.yaml` records skill/script versions, source, aggregate and per-file SHA, and synchronization time:

```bash
mno-bruno-qa scripts sync
mno-bruno-qa scripts check
```

`shared-cli` keeps only QA assets and calls the installed `mno-bruno-qa` command from the launchers.

Initialization migrates supported legacy runtime fields into the active environment, moves tooling into `execution/config.yaml`, and removes obsolete `qa.yaml` and `execution/plans.yaml`.

Module execution produces module-local evidence and `module_status`. It never updates the global version lock, generation state, index completion, or verified state.
