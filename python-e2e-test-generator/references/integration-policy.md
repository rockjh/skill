# Cross-Service Integration Policy

Read this reference when a scenario crosses a service boundary or uses messages, a data store, cache, scheduler, configuration center, device gateway, or another observer/control.

## Boundaries

Keep these concerns separate:

1. `common/clients/` calls public or approved test/admin interfaces.
2. `common/builders/` explicitly maps reusable source-confirmed payloads.
3. `common/repositories/` owns parameterized read-only queries and stable record mapping.
4. `common/controls/` owns default-off authorized and reversible controls.
5. `common/integrations/` provides generic adapters for discovered component types.
6. `common/fixtures/` owns adapter lifecycle, authorization, snapshots, and isolation.
7. `common/assertions/` compares protocol, message, persistence, and observable evidence.
8. Scenario modules express scenario-only actions, mappings, assertions, and cleanup.

Generic modules accept discovered configuration and scenario mappings. Never embed a project name, business endpoint, table, topic, configuration key, credential, enum, state, or environment address. Reuse an already approved pinned dependency; do not add a client library silently.

Instantiate and preflight only the services and components declared by the scenario. No adapter construction, connection, subscription, process inspection, or health check occurs during import or collection.

## HTTP and RPC

- Derive base address, context path, protocol, authentication, headers, serialization, timeout, and TLS from discovery and the active environment.
- Use health, OpenAPI, query, or guaranteed-miss requests for read-only smoke.
- Direct `requests`, `httpx`, and `urllib` calls use an explicit positive timeout. `urlopen` with request data is a write and is forbidden in smoke. An arbitrary fixture method such as `client.get()` is not transport proof.
- A read-only RPC smoke call goes through a common `read_only_rpc` adapter that declares `READ`/`RPC`, a source anchor from discovery, one bounded non-write operation, and a returned real result.
- Validate transport status and the source-defined business envelope separately.
- Record `verified=True` only from a validation expression that consumes that call's result. HTTP smoke status is an integer and 5xx always fails.
- A successful transport with a failed business result is a failure.
- Do not retry a non-idempotent call unless source declares an idempotency key and semantics.
- Redact credentials and sensitive payload fields in diagnostics while retaining method, non-secret target identity, correlation key name, status, and bounded response summary.

## Messages

For any discovered broker or gateway:

1. create a unique run/scenario consumer identity;
2. subscribe or capture starting position before the business action and wait for readiness;
3. observe only a bounded offset/time window;
4. match with source-confirmed correlation fields;
5. assert channel identity, key/headers, schema/version, and relevant business fields;
6. close deterministically after success or failure.

Publishing is disabled by default. It requires a source-confirmed simulation contract, per-run authorization, and a configured test-only destination prefix or exact allowlist. Reject retained messages, unrestricted wildcards, or business destinations unless the source-backed test contract explicitly requires them and the user authorized the exact test environment.

For Kafka-like logs, wait for assignment and capture starting offsets. For MQTT-like brokers, use a unique client ID, source-confirmed QoS/TLS/session behavior, and deterministic disconnect. For other systems, preserve the same readiness, bounded observation, exact correlation, and cleanup invariants without forcing Kafka or MQTT terminology into generated code.

## Database observation

Observation repositories expose only parameterized read methods. Poll by the same scenario correlation key with a monotonic deadline and report the last observed record. Assert ownership, the requested business state/fields, and relevant relationships.

A message proves publication; a database record proves persistence or consumption. When both are evidence sources, assert them independently, then compare every shared source-confirmed field. Define explicit mappings for different field names or representations. A time-range-only row, broad message match, or equal correlation ID alone is insufficient.

Database control follows [discovery-and-control-policy.md](discovery-and-control-policy.md). Keep control operations out of read repositories and test entrypoints. Never use control SQL as the business action under test.

## Cache observation

Expose the smallest source-required read surface, such as exact-key value, existence, TTL, or exact hash fields. Cache evidence is secondary when a public API, event, operation record, or database provides stronger business evidence.

Load endpoint, credentials, logical database/index, TLS, serialization, and test prefix from the active environment. Bounded waits use monotonic deadlines and last-state diagnostics.

If cleanup is necessary, a fixture may delete only an exact scenario-owned key under the configured test prefix. Reject wildcard deletion, namespace-wide clearing, database flushing, and deletion of pre-existing business keys.

## Schedulers and jobs

Prefer an approved trigger or admin interface discovered in source. Correlate trigger arguments, execution record, and downstream state. A scheduler acknowledgement does not prove the job's business result.

If time advancement or expiry simulation is needed, use a source-confirmed clock/configuration control or the controlled SQL policy. Snapshot the original state, isolate the target from other scenarios, trigger or await the job with a bounded deadline, verify its result, and restore the state.

Do not invoke an uncontrolled production schedule, alter global time, or call an undocumented scheduler endpoint.

## Dynamic configuration, mocks, and failure injection

- Use only controls found in application source/configuration and present in the scenario matrix.
- Record the control scope, affected component, correlation/isolation method, prior value, intended value, and restoration evidence.
- Default controls off. Enabling requires the exact target test environment and per-run authorization.
- Apply controls as narrowly as the platform permits; reject global changes when unrelated traffic can be affected.
- Snapshot before mutation, verify that the application consumed the change, and restore in guaranteed cleanup.
- A mock response or injected failure must still be verified through the public business result and downstream evidence relevant to the scenario.

## Runtime diagnostics

Every adapter reports only non-secret endpoint identity, component type, correlation-key name, bounded deadline, and last observed state. Never include credential values, complete connection strings, authorization headers, raw sensitive messages, or full database rows.

Control and endpoint evidence is adapter-owned. Record it immediately after the real external call in the same straight-line block, derive endpoint status, summary, and verification from the returned object, and pass the same scenario-owned correlation value in a resource/key/selector or request-payload argument to both a write operation and its control event. Logging, headers, or tracing metadata do not establish isolation. Scenario steps and test entrypoints cannot emit these events.

Connection or runtime failures after preflight are failed smoke/business checks. They cannot be converted to `pending_environment`, `contract_blocked`, `skip`, or `xfail`.
