# Python E2E Workflow Contract

The plan is the reviewable contract for the independent E2E project. Keep it machine-readable as `scenarios.yaml` and human-readable as `E2E_PLAN.md`.

```yaml
version: 1
system:
  project: <business-project-defined-name>
  build: <business-project>:<commit-or-tag>
  environment: <business-project-defined-environment>
  service_catalog: <business-project-defined-artifact-or-source>
  enabled_integrations:
    - mysql
scenarios:
  - id: ORDER_CREATE_001
    status: planned
    name: customer creates an order and downstream services publish its result
    actor: <business-project-defined-actor>
    preconditions:
      - actor and required permissions exist
      - the project-defined test tenant or namespace is isolated
    services:
      - <entry-service>
      - <downstream-service>
    interfaces:
      - service: <entry-service>
        kind: <http-or-grpc>
        name: <public-entrypoint>
      - service: <downstream-service>
        kind: <event-or-job>
        name: <project-defined-boundary>
    integration_dependencies:
      - kind: mysql
        enabled: true
        required: true
        configuration_source: <business-project-defined-source>
      - kind: kafka
        enabled: false
        required: false
        skip_reason: <recorded-only-when-not-enabled>
        configuration_source: <business-project-defined-source>
    configuration:
      source: <business-project-defined-source>
      scopes:
        session: <project-defined-immutable-settings>
        run: <project-defined-run-settings>
        scenario: <project-defined-mutable-or-owned-settings>
    steps:
      - submit a uniquely identified order through the public entrypoint
      - wait for the project-defined downstream completion signal
    checkpoints:
      - id: request_accepted
        owner_service: <entry-service>
        observable: http_or_grpc_application_result
        correlation: <business-key-or-trace-id>
      - id: persisted_order
        owner_service: <downstream-service>
        observable: mysql_row_or_project-approved-store
        correlation: <business-key-or-trace-id>
      - id: published_event
        owner_service: <downstream-service>
        observable: kafka_message_or_other_project-approved-observer
        enabled: false
        correlation: <business-key-or-trace-id>
    expected_outcomes:
      - each required application code is correct
      - downstream persistence and enabled integration evidence match the order
    cleanup: <project-defined-idempotent-cleanup>
    api_case_ids: []
```

The field names above describe planning metadata, not runtime configuration
variables. Replace placeholders with the business project's service IDs,
configuration source, integration conventions, and observable contracts. Do not
copy example values into a test project without verifying them during discovery.

Review gates:

1. No test code before the scenario inventory is approved.
2. A golden sample is executed before batch generation.
3. Each batch records its command and result.
4. A scenario is complete only when it is implemented, executed, and reconciled with the plan.
5. Every cross-service boundary has a named observable checkpoint; every integration enabled by an approved scenario has a verified configuration source, correlation rule, and cleanup result.
6. Unknown project configuration or missing observability is recorded as a blocker or explicit exclusion, never replaced with a guessed default or an HTTP-only assertion.
7. The generated reconciliation command reports plan IDs without tests, tests without plan IDs, enabled integrations without checkpoints, and missing cleanup evidence.

If the user describes a business rule that cannot be observed through the available APIs, identify the missing observable or test fixture instead of inventing an assertion.
