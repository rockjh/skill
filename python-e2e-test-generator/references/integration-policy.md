# Cross-Service Integration Policy

Read this reference when a scenario crosses a service boundary or uses Kafka, MySQL, EMQ/EMQX, Redis, XXL-JOB, or another observable component.

## Boundaries

Keep three concerns separate:

1. transport clients call public business interfaces;
2. generic component adapters observe source-confirmed topics, rows, keys, or jobs;
3. scenario tests express business actions, mappings, correlation, assertions, and cleanup.

Reusable adapters live under `common/integrations/` and accept environment configuration plus scenario mappings. Do not embed topic names, tables, business rules, or secrets in a generic adapter. Reuse an approved pinned dependency; do not add a client implicitly.

Shared integration capability is configured once in `config/common.yaml`. Connection details live in the selected `config/environments/<profile>.yaml`. A scenario only declares that it needs a component and whether the dependency is required or optional. Effective use requires both a shared `enabled: true` capability and a scenario dependency declaration.

No integration connection, client construction, or health check occurs during pytest collection. Required runtime access is validated during preflight immediately before business side effects. A missing required component produces `pending_environment` and a preflight failure, not `skip` and not an HTTP-only fallback. A source-unknown contract produces `contract_blocked`.

## Kafka publication evidence

For a scenario that must prove a Kafka publication:

1. create a unique scenario consumer group before the business action;
2. subscribe to the exact source-confirmed topic and wait until assignment is ready;
3. record partition starting offsets after assignment;
4. invoke the public business API;
5. consume only from the bounded starting-offset/time window;
6. filter by the source-confirmed order ID, atomic order ID, trace ID, or equivalent key;
7. assert key headers, schema/version, and business payload fields;
8. close the consumer in teardown even after failures.

Subscribing after the business request creates a race and is not acceptable evidence. An unrelated message, a broad topic match, or merely knowing that producer code exists does not prove publication.

## Database processing evidence

Use read-only correlated queries where possible. Poll with a bounded deadline and assert the row state, ownership, meaningful columns, and relationships. When Kafka drives the write, use the same correlation key used for the message assertion.

Kafka and database evidence prove different facts:

- Kafka proves the expected message was published.
- The database proves downstream processing completed and persisted its result.

Assert both independently when the scenario requires both, then compare source-confirmed fields across the message and row. Do not let either checkpoint stand in for the other.

Direct database setup or cleanup is allowed only when the business API cannot perform it and a dedicated test boundary is approved. Never truncate, broadly delete, or mutate production/shared data.

## Other observers

- **EMQ/EMQX:** subscribe before triggering, use a unique client identity, exact topic/filter, bounded wait, source-confirmed QoS/properties, and deterministic disconnect.
- **Redis:** assert the exact namespace, value/encoding, TTL/version, or stream semantics; clean only scenario-owned keys and never flush a shared database.
- **XXL-JOB:** use the approved trigger, correlate arguments and execution records, assert both job outcome and downstream business state, and restore any owned configuration.

Each observer reports the relevant endpoint identifier, correlation key, bounded deadline, and last observed state with secrets redacted.

## Automated test order

When Kafka and MySQL are both required, preserve this orchestration in code and `自动化测试流程图.md`:

```text
load environment -> preflight -> create/subscribe consumer -> record offsets
-> invoke API -> assert HTTP/application result -> assert correlated Kafka message
-> poll/assert correlated database row -> compare message and row
-> API cleanup -> close consumer -> restore scenario configuration
```

Register cleanup handlers as soon as each resource is acquired so later assertion failures cannot bypass cleanup.
