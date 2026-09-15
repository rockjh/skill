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

There are only two scopes:

```bat
qa\execution\run.bat --all
qa\execution\run.bat --module ac
qa\execution\run.bat --module "APP车辆用量查询"
```

`--all` and `--module` are mutually exclusive. Module selection accepts module ID, display name, directory, or OpenAPI Tag. The selected scope always runs all registered business requests. There are no named plans and risk is not a filter.

## Risk Confirmation

Before preflight or any HTTP request, the runner reads all selected `cases.yaml` files and computes the included risks:

| Risk | Required flags |
| --- | --- |
| `read-only` | none |
| `isolated-write` | `--confirm-write` |
| `destructive` | `--confirm-write --confirm-destructive` |
| `external-side-effect` | `--confirm-external` |

Example full confirmation:

```bat
qa\execution\run.bat --all --confirm-write --confirm-destructive --confirm-external
qa\execution\run.bat --module ac --confirm-write --confirm-destructive --confirm-external
```

Missing flags fail the whole scope before any request. The runner never executes a safe subset first.

Bruno executes a directory directly instead of using tag filtering:

```text
--all       -> bru run qa/bruno -r
--module x  -> bru run qa/bruno/<module-directory> -r
```

`.bru` tags contain only the case risk for inspection and reporting.

## Tooling Modes And Migration

`project-scripts` keeps a synchronized `qa/scripts` bundle. Its `scripts-version.yaml` records skill/script versions, source, aggregate and per-file SHA, and synchronization time:

```bash
mno-bruno-qa scripts sync
mno-bruno-qa scripts check
```

`shared-cli` keeps only QA assets and calls the installed `mno-bruno-qa` command from the launchers.

Initialization migrates supported legacy runtime fields into the active environment, moves tooling into `execution/config.yaml`, and removes obsolete `qa.yaml` and `execution/plans.yaml`.

Module execution produces module-local evidence and `module_status`. It never updates the global version lock, generation state, index completion, or verified state.
