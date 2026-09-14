# E2E Fixture and Configuration Policy

Use the narrowest fixture scope that preserves isolation:

- **session:** parsed technical configuration and immutable service metadata;
- **run:** environment profile, unique run ID, isolation namespace, and reporting context;
- **scenario:** actor/session, Kafka consumer, scenario-owned records, and mutable settings;
- **step:** short-lived values that cannot safely share scenario scope.

## Configuration split

Technical filenames and keys remain English. Keep shared behavior separate from environment values.

`config/common.yaml`:

```yaml
integrations:
  kafka:
    enabled: true
  mysql:
    enabled: true
  redis:
    enabled: false

polling:
  interval_seconds: 1
  timeout_seconds: 60
```

`config/environments/icv-test.yaml`:

```yaml
services:
  mno_traffic:
    base_url: ${MNO_TRAFFIC_BASE_URL}

integrations:
  kafka:
    bootstrap_servers: ${KAFKA_BOOTSTRAP_SERVERS}
  mysql:
    dsn: ${MYSQL_DSN}

authentication:
  operator_info: ${OPERATOR_INFO}
```

`config/environments/example.yaml` documents required keys with environment-variable placeholders but contains no usable credentials. A scenario definition declares only dependency kind and required/optional semantics:

```yaml
integration_dependencies:
  - kind: kafka
    required: true
  - kind: mysql
    required: true
```

Do not repeat endpoints, credentials, authentication, polling defaults, or `enabled` switches in `场景定义.yaml`.

## Collection before environment availability

Configuration modules may parse files at import time only if unresolved placeholders remain inert. They must not require environment variables, open sockets, create clients, or run health checks during module import, test module import, marker registration, or test collection.

Generate complete clients, adapters, fixtures, scenario steps, assertions, and cleanup from source-confirmed contracts even when runtime values are missing. `pytest --collect-only` must succeed in that state.

Resolve and validate runtime values inside a preflight fixture or explicit preflight call that runs before any business request, seed write, subscription side effect beyond observer setup, or mutable configuration change. On missing required values:

- set/report `pending_environment`;
- fail with a clear configuration error;
- list missing keys without printing secret values;
- do not call `pytest.skip`, `xfail`, or return a passing no-op result.

Use `contract_blocked` only when source cannot establish a required interface, schema/message shape, correlation rule, or expected business outcome.

## Isolation and cleanup

- Select one named environment profile per run; never fall back to a developer URL or implicit shared environment.
- Require project-approved isolation for mutable boundaries such as tenant/account, namespace/schema, Kafka consumer group, cache prefix, or object path.
- Use run and scenario-derived values where the contract permits them.
- Register cleanup immediately after creating each owned resource.
- Prefer business API cleanup. Direct database cleanup requires a dedicated test boundary and exact correlated keys.
- Snapshot and restore mutable global/scenario settings under the project's approved lock; mark the scenario serial when isolation is impossible.

## Headers and signing

Transport helpers expose per-request headers. Unless the source contract says otherwise, merge ordinary headers in this order:

```text
common < environment < scenario < request
```

Apply signing after the merge. The signer removes stale reserved headers, then owns their final values. Read signing enablement, algorithm, canonicalization, header names, and secret references from the selected environment profile, not from scenario definitions or hardcoded constants. Missing signing runtime values are detected by preflight, not during collection.

For the Seres contract, use [request-signing-template.md](request-signing-template.md). When disabled, send no `sign`, `timestamp`, or `accesskey`. When enabled, require the source-confirmed raw-body SHA-256 contract and never log the secret, access key, canonical string, authorization headers, or complete sensitive payload.
