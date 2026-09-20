# E2E Fixture and Configuration Policy

Use the narrowest fixture scope that preserves isolation:

- **session:** parsed technical configuration and immutable discovered metadata;
- **run:** active environment, unique run ID, authorization context, isolation namespace, and report context;
- **scenario:** actor/session, observers, owned records, mutable settings, snapshots, and cleanup stack;
- **step:** short-lived values that cannot safely share scenario scope.

## Configuration split

Keep generated E2E configuration separate from application-source configuration:

- `discovery/workspace.yaml` records where application values originate and how they are overridden.
- `config/config.yaml` selects one E2E environment and holds only shared technical defaults and default-off safety switches.
- `config/environments/<environment>.yaml` maps discovered services/components to environment-specific addresses, authentication references, headers, TLS, and connection references.
- `业务数据.json` contains environment-isolated scenario inputs only.

Generated E2E keys such as `active_environment` are part of this skill's schema. Application ports, context paths, source configuration keys, profile names, endpoints, topics, table names, and authentication fields must be discovered; never copy a concrete example from this skill.

`config/config.yaml` has this minimum shape:

```yaml
# 用途：选择 E2E 环境并定义公共技术默认值；禁止保存地址、凭据或业务数据。
active_environment: <selected-test-environment>

# 公共默认值：环境文件只能覆盖对应叶子，不能开启默认关闭的危险能力。
defaults:
  polling:
    # 轮询间隔，单位为秒。
    interval_seconds: 1
    # 单次有限等待上限，单位为秒。
    timeout_seconds: 60
  safety:
    database_control_enabled: false
    mutable_configuration_enabled: false
    message_publish_enabled: false
```

The angle-bracket environment value is a metavariable and must be replaced with an environment discovered or explicitly selected for this task. Safety switches are false in committed files. A scenario cannot enable them. A runtime control requires explicit per-run authorization, exact environment identity, and the relevant source-backed control contract.

Runtime authorization variables are process-only. Database control uses `E2E_ENABLE_DATABASE_CONTROL` and `E2E_CONTROL_AUTHORIZATION_REF`; mutable configuration uses `E2E_ENABLE_MUTABLE_CONFIGURATION` and `E2E_MUTABLE_CONFIGURATION_AUTHORIZATION_REF`; message publication uses `E2E_ENABLE_MESSAGE_PUBLISH` and `E2E_MESSAGE_PUBLISH_AUTHORIZATION_REF`; other dangerous test/admin, mock/fault, or job controls use `E2E_ENABLE_DANGEROUS_CONTROL` and `E2E_DANGEROUS_CONTROL_AUTHORIZATION_REF`. Every form also requires `E2E_CONTROL_ENVIRONMENT` to equal the active environment. Never persist these values.

Each environment file contains only discovered entries. Conceptually:

```yaml
# 用途：将已发现的服务和组件映射到当前测试环境；敏感值只允许使用精确占位符。
services:
  <discovered-service-id>:
    base_url: ${<ENVIRONMENT_SPECIFIC_BASE_URL_REFERENCE>}
    auth:
      type: <source-confirmed-auth-type>
      values:
        <source-confirmed-auth-field>: ${<ENVIRONMENT_SPECIFIC_SECRET_REFERENCE>}
    headers:
      <source-confirmed-header-name>: ${<ENVIRONMENT_SPECIFIC_VALUE_REFERENCE>}

components:
  <discovered-component-id>:
    type: <discovered-component-type>
    connection:
      <driver-defined-field>: ${<ENVIRONMENT_SPECIFIC_CONNECTION_REFERENCE>}
```

Angle-bracket values are schema metavariables, not literal keys. Generate only entries that discovery found. Authentication shape, header names, client options, health path, OpenAPI path, broker settings, data-source names, and scheduler controls come from source/configuration evidence. Do not impose a fixed provider or protocol.

Create additional named environment files only for environments the user actually targets. Keep the same logical shape across files but use environment-specific exact placeholders. Do not generate empty example environments. Credentials remain environment variables or approved provider references and are never resolved into committed files or logs.

## Selection and precedence

Use one deterministic chain:

1. Parse `config.yaml` and validate `active_environment` against `[a-z][a-z0-9_-]*`.
   Conventional production identifiers containing `prod`, `production`, `prd`, or `live` as a segment are rejected regardless of safety flags.
2. Require exactly one matching environment file; never fall back to another environment.
3. Recursively deep-merge E2E defaults with that environment mapping. Mapping values merge by key; a non-mapping replaces only its corresponding leaf.
4. Ensure every referenced service/component exists in `discovery/workspace.yaml` and the scenario contract.
5. Load the scenario's `业务数据.json`, select its exact active-environment root, and resolve `data_ref` only inside that object.
6. During runtime preflight, recursively resolve only strings that exactly match `${ENV_NAME}`. Never interpolate partial strings.

Business data is not configuration. Inspect the formal request models, validators, enums, length/range rules, and design-defined downstream correlation use before choosing each input:

- keep protocol-valid constants, boundary values, prefixes, and request shapes as explicit JSON literals;
- construct values that the scenario owns instead of adding an environment variable for each field;
- generate unique random strings after preflight with the standard-library `secrets` module or `uuid`, preserving protocol-defined length, alphabet, and format constraints;
- use exact `${ENV_NAME}` placeholders only for pre-existing environment-owned data that the test cannot safely create, such as an approved test account or seeded external identifier.

Every `data_ref` subtree must contain at least one constructible protocol-valid literal value. The contract gate rejects a referenced subtree made entirely from environment placeholders. A protocol-valid scenario that truly has no business input omits `data_ref` rather than inventing an empty injected object.

A precondition is test-owned and constructible when design defines its business meaning, the formal protocol defines its format, it does not depend on real users, devices, or protected resources, it has a run-unique key, and exact cleanup/restoration is possible. The test must build that data through the first safe usable candidate path and may not ask the user for an environment variable instead.

The contract gate also checks only the active scenario's required integrations and business data. A `ready` scenario fails when a required mapping is absent or an exact placeholder has no current value; unrelated service credentials never block it. `pending_environment` blockers are derived from and must exactly equal the missing active-environment values; they do not include per-run control authorization.

This generated merge does not replace application configuration discovery. `discovery/workspace.yaml.configuration.precedence` records the application's actual low-to-high sources. A source default remains unresolved when a higher profile, environment variable, configuration center, command-line value, or local override may replace it.

An unset or blank placeholder is a configuration failure. Report only the configuration/data path and placeholder name, never its resolved value. Parse ports, booleans, durations, and lists only according to the discovered configuration format.

## Collection without an environment

Configuration and data modules may parse committed files at import time only while placeholders remain inert. They must not read secrets, instantiate clients, open sockets, inspect processes, run health checks, or connect to any component during module import, marker registration, or pytest collection.

Generate formal-protocol clients and design-traced assertions; use source/configuration only for repositories, controls, fixtures, steps, and cleanup when runtime values are missing. Delay imports of optional component libraries when importing them would otherwise break collection.

`python -m pytest --collect-only` must succeed with no endpoint, credential, broker, database, cache, scheduler, or environment test value available.

## Runtime preflight

Resolve and validate runtime values immediately before the first runtime action. Preflight is scenario-aware and:

- validates the selected environment and its allowed test/isolation boundary;
- rejects contradictory `test_environment: true` plus `protected: true`, and rejects every write when `protected` is true, including API, message, job, mock/fault, dynamic-configuration, test/admin, and database controls;
- checks only the services and components declared by that scenario;
- verifies address and credential references without printing values;
- runs non-destructive reachability checks before any subscription, seed write, message publication, job trigger, control SQL, or mutable configuration;
- confirms unique run/scenario namespaces and exact correlation-key ownership;
- confirms cleanup and restoration operations are registered and source-backed;
- checks per-run authorization for every dangerous control;
- permits a `pending_environment` scenario to resolve available values and execute its explicitly `executable` prefix while leaving unavailable placeholders inert for later blocked steps;
- reports missing values per affected step and never treats a partial run as success.

Never call `pytest.skip`, `xfail`, `importorskip`, `unittest.SkipTest`, their aliases, or return a passing no-op for missing runtime configuration. Project `conftest.py` and implicit `usefixtures` are forbidden. Once preflight has resolved the environment and a real request is attempted, an unexpected runtime result is an ordinary failed test.

Every scenario test records exactly one runtime status event for each contract step. `environment_missing` is only for inaccessible required runtime configuration, `authorization_missing` for absent per-run permission, `control_gap` for a fully explored control gap, `product_gap` for missing design-defined product behavior, and `runtime_failure` for a call or observation that ran but did not meet the contract. Business mismatches, absent downstream effects, wrong transitions, and successful transports with wrong state are never environment blockers.

## Isolation and cleanup

- Never fall back to a developer URL or implicit shared environment.
- Derive namespaces, consumer groups, client IDs, cache/file prefixes, and other permitted identifiers from unique run and scenario IDs.
- Register idempotent cleanup immediately after each resource is acquired.
- Prefer the same business API for cleanup. Use a source-confirmed test/admin control only when the public contract cannot restore state safely.
- Direct database cleanup or state control requires the policy in [discovery-and-control-policy.md](discovery-and-control-policy.md).
- Cache cleanup is allowed only for exact scenario-owned keys under a configured test prefix. Never expose whole-database, wildcard, or business-key clearing.
- Snapshot mutable configuration before change, require every mutable control identity to be an exact `owned_resources.identity`, and verify restoration under `finally` or a finalizer.
- Pass the full preflight `scenario_context` to the shared restoration guard. It derives resource identities from the validated contract; scenario code never supplies restoration resource labels.
- A cleanup call after the last assertion is insufficient unless guaranteed on every exception path.
- When test and cleanup both fail, preserve the test exception and attach cleanup/restoration failure as a separate diagnostic. Raise cleanup failure normally only if no earlier failure exists.

The main agent compares isolation resources across all scenarios before runtime. A collision blocks execution until identifiers are unique; declaring the same lock name does not bypass the gate.

## Request construction and signing

Transport helpers expose per-request headers and preserve raw request bytes when source contracts require them. Merge ordinary headers according to the discovered application/client precedence; do not assume a universal order.

Implement authentication or signing only after source confirms its algorithm, canonicalization, encoding, timestamp/nonce, body handling, and reserved fields. Keep it scenario-owned until a second scenario truly shares the same contract. Add a fixed-vector unit test for non-trivial signing. Never log secrets, canonical strings containing secrets, authorization headers, or complete sensitive payloads.

Do not retain provider-specific signing templates in this generic skill.

## Focused shared tests

Use the project's existing test dependencies to cover recursive merge, exact placeholder resolution, missing/blank variables, strict type parsing, active-environment selection, no cross-environment fallback, declared-integration gating, preflight-before-side-effect ordering, isolation collisions, authorization checks, original-exception preservation, and verified restoration. One representative test per invariant is enough.
