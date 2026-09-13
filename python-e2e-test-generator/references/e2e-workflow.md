# Python E2E Workflow Contract

The plan is the reviewable contract for the independent E2E project. Keep it machine-readable as `scenarios.yaml` and human-readable as `E2E_PLAN.md`.

```yaml
version: 1
system:
  project: <business-project-defined-name>
  build: <business-project>:<commit-or-tag>
  environment: <business-project-defined-environment>
  environment_profile:
    name: <project-defined-profile>
    source: <approved-config-source>
  isolation:
    run_id_source: <project-defined-source>
    tenant_or_namespace: <project-defined-isolation>
  service_catalog: <business-project-defined-artifact-or-source>
  enabled_integrations:
    - mysql
  integration_config:
    mysql:
      enabled: true
      required: true
      adapter_scope: shared
      configuration_source: <business-project-defined-source>
    kafka:
      enabled: false
      required: false
      adapter_scope: shared
      configuration_source: <business-project-defined-source>
scenarios:
  - id: ORDER_CREATE_001
    status: planned
    name: customer creates an order and downstream services publish its result
    artifact_dir: scenarios/ORDER_CREATE_001
    swimlane: scenarios/ORDER_CREATE_001/swimlane.md
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
      isolation:
        tenant_or_namespace: <scenario-owned-or-project-approved-value>
        resource_prefix: <run-and-scenario-prefix>
    request:
      headers:
        source: <approved-config-or-scenario-file>
        precedence: common < environment < scenario < request
        scenario_overrides: <scenario-owned-header-map-or-reference>
        request_overrides: <request-specific-header-map-or-reference>
        per_request: true
      signing:
        enabled: false
        toggle_key: seres.sign
        algorithm: SHA256
        canonicalization: <path-query-timestamp-raw-body-secret-suffix>
        body_mode: raw
        key_source:
          secret_key: SECRET_KEY
          access_key: ACCESS_KEY
        header_names:
          signature: sign
          timestamp: timestamp
          access_key: accesskey
        template: scenarios/ORDER_CREATE_001/docs/request-signing.md
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
`enabled_integrations` remains the compatibility list derived from
`integration_config`; scenario-level `integration_dependencies` is the opt-in
and may narrow the requirement but may not broaden a global disable. The
effective component value is the logical AND of the global flag and an existing
scenario opt-in flag; no dependency entry means the scenario does not use that
component.
For the supplied Seres contract, `request.signing.enabled` is resolved from
`seres.sign`. When it is false, the generated request must omit all
signature-derived headers and fields. When true, require `body_mode: raw` for
body-bearing requests and use the exact SHA-256 canonicalization in the
materialized scenario template.

Review gates:

1. No test code before the scenario inventory is approved.
2. A golden sample is executed before batch generation.
3. Each batch records its command and result.
4. A scenario is complete only when it is implemented, executed, and reconciled with the plan.
5. Every cross-service boundary has a named observable checkpoint; every integration enabled by an approved scenario has a verified configuration source, correlation rule, and cleanup result.
6. Unknown project configuration or missing observability is recorded as a blocker or explicit exclusion, never replaced with a guessed default or an HTTP-only assertion.
7. Every scenario has a directory under `artifact_dir` containing its test,
   data/scripts when needed, README, and Mermaid swimlane; shared code is
   limited to generic reusable modules.
8. Header precedence and signing enabled/disabled behavior are reconciled with
   the request contract; the plan signing mirror matches `seres.sign`, reserved
   signature headers are written last, body mode is raw when required, and
   disabled components have no instantiated adapter or claimed checkpoint.
9. The generated reconciliation command reports plan IDs without tests, tests
   without plan IDs, conflicting integration flags, enabled integrations without
   checkpoints, non-materialized template paths, missing cleanup evidence, and
   scenarios without a swimlane or artifact directory.
10. Generated Python has Chinese comments/docstrings for non-trivial logic,
    fixtures, configuration, correlation, and cleanup.

If the user describes a business rule that cannot be observed through the available APIs, identify the missing observable or test fixture instead of inventing an assertion.
