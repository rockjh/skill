# E2E Fixture and Configuration Policy

Use the narrowest fixture scope that preserves isolation:

- **session:** parsed technical configuration and immutable service metadata;
- **run:** selected environment profile, unique run ID, isolation namespace, and report context;
- **scenario:** actor/session, Kafka or EMQ observer, scenario-owned records, and mutable settings;
- **step:** short-lived values that cannot safely share scenario scope.

## Configuration split

Keep shared technical behavior separate from environment values.

`config/common.yaml`:

```yaml
integrations:
  kafka:
    enabled: true
  mysql:
    enabled: true
  redis:
    enabled: false
  emq:
    enabled: false

polling:
  interval_seconds: 1
  timeout_seconds: 60
```

`config/environments/<profile>.yaml`:

```yaml
services:
  mno_traffic:
    base_url: ${MNO_TRAFFIC_BASE_URL}

integrations:
  kafka:
    bootstrap_servers: ${KAFKA_BOOTSTRAP_SERVERS}
  mysql:
    dsn: ${MYSQL_DSN}
  redis:
    url: ${REDIS_URL}
    test_key_prefix: ${REDIS_TEST_KEY_PREFIX}
  emq:
    host: ${EMQ_HOST}
    port: ${EMQ_PORT}
    username: ${EMQ_USERNAME}
    password: ${EMQ_PASSWORD}
    tls: ${EMQ_TLS_ENABLED}
    qos: 1
    retain: false
    topic_prefix: ${EMQ_TOPIC_PREFIX}
    test_topic_prefix: ${EMQ_TEST_TOPIC_PREFIX}
```

`config/environments/example.yaml` documents required keys with unresolved environment-variable placeholders and contains no usable credentials. Scenario definitions contain only HTTP service names and explicit component booleans:

```yaml
integrations:
  http:
    - mno-traffic
    - mno-operator
  kafka: true
  mysql: true
  redis: false
  emq: false
```

Do not repeat endpoints, credentials, authentication, topic prefixes, polling defaults, or shared `enabled` switches in `场景定义.yaml`. Keep static business inputs and business-value placeholders in `业务数据.yaml`.

## Collection before environment availability

Configuration and data modules may parse files at import time only if unresolved placeholders remain inert. They must not require environment variables, instantiate runtime clients, open sockets, or run health checks during module import, marker registration, or test collection.

Generate complete clients, builders, repositories, assertions, fixtures, scenario steps, and cleanup from source-confirmed contracts even when runtime values are missing. Use delayed imports for optional integration packages when importing them would otherwise break collection.

Resolve and validate runtime values in a preflight fixture or explicit preflight call before subscriptions, business requests, seed writes, or mutable configuration changes. Preflight:

- validates the selected named environment and approved isolation;
- checks values required by the scenario's `integrations` declaration;
- validates service and component reachability without producing business data;
- lists missing configuration keys without printing values or secrets;
- reports `pending_environment` and fails clearly when requirements are unmet.

Never call `pytest.skip`, `xfail`, or return a passing no-op for missing environment configuration. Use `contract_blocked` only for missing source contracts.

## Isolation and cleanup

- Never fall back to a developer URL or implicit shared environment.
- Use run- and scenario-derived namespaces, Kafka consumer groups, EMQ client IDs, cache prefixes, and object paths where contracts permit.
- Register idempotent cleanup immediately after acquiring each owned resource.
- Prefer business API cleanup. Direct database cleanup requires an approved test boundary and exact correlated keys.
- Redis cleanup is permitted only for exact scenario-owned keys under the configured test prefix.
- Snapshot and restore mutable settings under the project's approved lock; mark the scenario serial when isolation is impossible.
- Guarantee cleanup through `finally`, `yield` fixture teardown, `request.addfinalizer`, `ExitStack`, or a context manager. A cleanup call placed only after assertions is insufficient.

## Headers and signing

Transport helpers expose per-request headers. Unless source says otherwise, merge ordinary headers in this order:

```text
common < environment < scenario < request
```

Apply signing after the merge. The signer removes stale reserved headers, then owns their final values. Signing settings and key references come from the selected environment profile, never from scenario definitions or hardcoded constants.

For the Seres contract, use [request-signing-template.md](request-signing-template.md). When disabled, send no `sign`, `timestamp`, or `accesskey`. When enabled, require the source-confirmed raw-body SHA-256 contract and never log the secret, access key, canonical string, authorization headers, or complete sensitive payload.
