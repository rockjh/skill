# Cross-Service Integration Policy

Read this reference when a planned scenario crosses a service boundary or needs
evidence from Kafka, MySQL, EMQ/EMQX, Redis, XXL-JOB, or another infrastructure
component. The component list is optional. A business project decides which
components exist, which client libraries are approved, and which configuration
keys and observables are valid.

## Adapter boundary

Keep three concerns separate:

1. **Transport clients** call the public service interfaces used by the
   business journey.
2. **Component adapters** connect to project-approved observers such as a SQL
   reader, message consumer, cache reader, MQTT subscriber, or job monitor.
3. **Scenario tests** express business intent, checkpoints, correlation, and
   cleanup. They must not hide the complete journey inside a generic helper.

Create or enable an adapter only for an approved scenario that needs it. MySQL,
Kafka, EMQ/EMQX, Redis, and similar public components should have one generic,
configuration-driven adapter per component in the shared integration area; the
scenario supplies project-specific table/topic/key/payload mapping. Reuse a
client or library already used by the E2E project or business project where
possible; do not silently add a dependency or create a universal middleware
abstraction that hides business intent.

## Configuration and lifecycle

- Discover component endpoints, credentials, TLS, serialization, schemas,
  topic/table/key conventions, retention, and access restrictions from the
  business project's environment contract, deployment files, `conftest.py`,
  or configuration provider.
- Treat the names in this document as evidence categories, not required
  environment variable names or defaults. Record the actual source and fixture
  scope in the scenario plan.
- Keep immutable connectivity and health checks at session scope. Keep a run
  correlation ID and shared reporting context at run scope. Keep consumers,
  subscriptions, keys, rows, job arguments, and mutable settings at scenario
  scope unless the project proves they are safe to share.
- Register cleanup immediately after creating or claiming an owned resource.
  Cleanup must run after assertion failures and should be idempotent.
- Use a dedicated test tenant, namespace, schema, topic prefix, cache prefix,
  or equivalent isolation mechanism when the project provides one. Never use a
  broad delete, truncate, topic purge, or cache flush against shared data.
- An adapter may publish an external event, seed a record, or trigger a job only
  when the scenario explicitly models that actor and the business project
  approves the input interface. Mark the input as scenario-owned and clean it
  up or expire it according to the project convention.

## Enable/disable contract

- Declare every public component in the project configuration with `enabled`
  and `required` semantics, plus its configuration source and adapter scope.
- Treat `system.integration_config.<kind>.enabled` as the global capability and
  `scenario.integration_dependencies[].enabled` as the scenario opt-in. The
  effective value is their logical AND when the scenario dependency exists; no
  dependency entry means the scenario does not opt in. A scenario cannot
  broaden a global disable. Keep the legacy `enabled_integrations` list derived
  from the global map and fail reconciliation when it disagrees with that map.
- A missing or malformed `enabled` value is a configuration error; do not infer
  enabled or disabled from client-library availability or a guessed default.
- `enabled: false` means no client construction, connection, health check,
  fixture setup, polling, write, or evidence claim for that component. The
  generated report records it as disabled rather than as a passing checkpoint.
- `enabled: true` requires a non-destructive preflight before business data is
  created. Missing credentials, schema access, or isolation values are a
  blocked precondition for required scenarios and an explicit skip for optional
  scenarios only.
- Keep adapter APIs generic and typed around project-provided mappings. Do not
  put scenario business rules, hard-coded topics/tables/keys, or secrets in a
  shared adapter.

## Preflight and optional components

Before a scenario creates business data, run a non-destructive preflight for
every service and enabled component it names. Check the project-approved
endpoint, credentials, protocol/schema access, read permissions, and any
required namespace or topic/table/key convention. Keep preflight separate from
business assertions so an unavailable observer cannot look like a business
failure.

Each dependency is either:

- **required:** missing or unauthorized access is a failed precondition and the
  scenario is blocked; do not run a weaker assertion instead;
- **optional:** the dependent scenario may be skipped only when the plan names
  the condition and the report records the reason.

Do not silently convert an optional component into a global fixture. Enable its
adapter only for scenarios that need it, and do not install a client library
unless the E2E project approves and pins it.

## Evidence rules by component

### Kafka

- Observe the exact project-defined topic and message contract, including key,
  headers, schema/version, and payload fields that matter to the business
  outcome.
- Correlate messages with a scenario-owned business key, trace ID, or run ID.
  Consume from a bounded time/offset window and filter by that correlation;
  do not accept an unrelated message as evidence.
- Use a scenario-specific consumer identity or the project's test consumer
  convention. Close consumers and remove only scenario-owned test artifacts if
  the project supports cleanup.

### MySQL

- Prefer read-only queries for verification. Assert the row state, ownership,
  relevant columns, and transactionally meaningful relationships rather than
  merely checking that a row exists.
- Read using the project's consistency rules and poll when an asynchronous
  writer is involved. Document the exact tables and keys observed.
- Write or delete directly only when API cleanup or setup is impossible and a
  dedicated test schema/permission has been approved. Never mutate production
  data as part of E2E generation.

### EMQ/EMQX (MQTT)

- Subscribe to the exact project-defined topic and assert payload, QoS,
  retained/session behavior, and relevant properties when they are part of the
  contract.
- Use a unique client ID and scenario-owned topic filter where possible. Start
  the subscription before triggering the business action, use a bounded wait,
  then disconnect and remove owned subscriptions.
- Do not use a wildcard subscription as the only proof of a scenario result.

### Redis

- Assert the project-defined key namespace, value/encoding, TTL, version, or
  stream/list semantics that represent the business outcome.
- Use a run/scenario key prefix or another project-approved namespace. Clean up
  only owned keys and never flush a shared database.
- Account for eventual consistency and expiry with deadline-based polling;
  distinguish a missing key from an expired key when diagnostics matter.

### XXL-JOB

- Trigger a job only through the project-approved API, scheduler fixture, or
  test hook. Do not alter global scheduler settings unless the scenario owns
  and restores them.
- Correlate the job argument, execution record/log, and downstream effect with
  the scenario ID or business key. Assert both execution outcome and the
  business state it is expected to produce.
- Poll job status and downstream evidence with a deadline. Capture the last
  status/log reference on failure and clean up scenario-created job data or
  test hooks.

## Missing observability

If a requested business rule cannot be proven through the public interfaces or
an approved component observer, mark the scenario as blocked in the plan and
name the missing fixture, read permission, event contract, or test hook. Do not
replace a required side-effect assertion with an HTTP `2xx` assertion merely to
make the scenario executable.

## Failure diagnostics

Every integration checkpoint should retain redacted request/response context,
correlation values, query/topic/key identifiers, polling deadline, and the last
observed state. Secrets and message payload fields classified as sensitive by
the business project must be redacted before they reach pytest output or test
reports.

Generated adapter modules and fixtures must contain Chinese module/class/function
docstrings and comments for connection scope, enabled-state branching,
correlation, polling, redaction, and cleanup. Identifiers may remain in the
project's conventional language.
