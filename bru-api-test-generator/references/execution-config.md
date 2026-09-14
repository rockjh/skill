# Bruno Execution Configuration

`qa/execution/config.yaml` only selects the active environment and controls the
signing algorithm. Unknown fields are errors.

```yaml
active_environment: local
sign: disabled # disabled | seres-sign
```

Do not put authentication modes, Header/Cookie values, variable selectors, or
path rules in this file. Existing legacy settings are migrated once into the
active environment by `mno-bruno-qa init`.

## Environment Format

Keep environments outside the Bruno collection under
`qa/execution/environments/`. The skill extends Bruno's environment format with
one readable `headers {}` block:

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

Every non-empty KV in `headers` becomes a request Header. Cookie has no special
case. Values can reference variables from the same environment; an unresolved
or empty sensitive value is not injected. A request-local Header wins over the
environment Header. To test a missing common Header, declare:

```yaml
request:
  omit_common_headers:
    - operatorInfo
```

Bruno does not natively understand this custom block. The CLI parses it,
resolves references, passes Bruno a temporary environment containing only
native `vars`, and sends the resolved Header map to the collection-level
pre-request script. Do not duplicate this logic in request files.

When `sign: seres-sign`, `collection.bru` reads `ACCESS_KEY` and `SECRET_KEY`
from the active environment. The SHA-256 input, millisecond timestamp, and
`sign`, `timestamp`, and `accesskey` Header names are fixed. With
`sign: disabled`, the collection adds no signing Header.

## Risk Plans

`qa/execution/plans.yaml` defines reusable scopes:

```yaml
plans:
  smoke:
    risks: [read-only]
    max_cases_per_module: 3
  regression:
    risks: [read-only, isolated-write]
  full:
    risks: [read-only, isolated-write, destructive, external-side-effect]
    require_confirm: true
```

The default is `read-only`. Confirmations are mandatory:

| Risk | Required flags |
| --- | --- |
| `read-only` | none |
| `isolated-write` | `--confirm-write` |
| `destructive` | `--confirm-write --confirm-destructive` |
| `external-side-effect` | `--confirm-external` |

```bat
qa\execution\run.bat --module ac --risk read-only
qa\execution\run.bat --module ac --risk isolated-write --confirm-write
qa\execution\run.bat --module ac --risk destructive --confirm-write --confirm-destructive
qa\execution\run.bat --module ac --risk external-side-effect --confirm-external
qa\execution\run.bat --plan smoke
qa\execution\run.bat --plan regression --confirm-write
```

Risk and plan tags are materialized into `.bru` metadata and selected through
Bruno's tag filter. A destructive case outside the selected scope does not
block a normal read-only run. Module runs never advance global completion or
`version-lock.yaml`.

## Tooling Modes

The default transition mode stores a synchronized `qa/scripts` bundle. Its
`README.md` and `scripts-version.yaml` record purpose, skill/script versions,
source, aggregate SHA, per-file SHA, and synchronization time:

```bash
mno-bruno-qa scripts sync
mno-bruno-qa scripts check
```

For repositories using an installed package, run
`mno-bruno-qa init --shared-cli`. `qa/qa.yaml` then selects `shared-cli`, the
launchers call the installed command, and the repository does not need Python
script copies.
