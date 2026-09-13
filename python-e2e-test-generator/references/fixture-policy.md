# E2E Fixture Policy

Use the narrowest fixture scope that provides isolation. The concrete settings and scope mapping are defined by the business project; this policy does not prescribe generic field names or defaults:

- **session:** immutable configuration and service health check;
- **run:** unique run ID, test tenant, and shared reporting context;
- **scenario:** actor/session and scenario-owned records;
- **step:** transient data only when a step cannot safely share scenario state.

## Environment isolation

- Select one named environment profile per run from the project's approved
  configuration provider. Record its source, build/version, and whether it is
  local, shared, staging, or another project-defined class.
- Require a project-approved isolation primitive for every mutable boundary:
  tenant/account, namespace/schema, topic/consumer identity, cache prefix,
  object path, or equivalent. Include the run and scenario IDs in owned names
  when the project convention allows it.
- If a profile or isolation value is absent, fail preflight or mark the scenario
  blocked. Never fall back to a developer URL, shared tenant, global topic, or
  broad database/cache cleanup.
- Keep profile selection immutable at session/run scope. Snapshot and restore
  mutable project settings per scenario under the project's approved lock; do
  not claim parallel safety when a setting is global.

## Headers and optional signing

- Transport helpers expose per-request headers and document merge precedence
  among common, environment, scenario, and request values. Prefer the
  deterministic order common < environment < scenario < request unless the
  project contract requires another order. Scenario headers remain in the
  scenario artifact directory or approved configuration source.
- Merge ordinary headers first, then run the signer last. The configured
  signature headers (`sign`, `timestamp`, `accesskey` for Seres) are reserved:
  request/scenario overrides cannot replace them when signing is enabled, and
  stale values are removed when signing is disabled.
- Model signing as a configuration object with an explicit `enabled` switch,
  algorithm, canonicalization, key source, and generated header names. When
  disabled, the request must contain no signature-derived headers or fields;
  tests should assert this when the contract makes it observable.
- Treat the plan's `request.signing.enabled` as a recorded mirror of the runtime
  `seres.sign` value, not a second override. A mismatch is a preflight or
  reconciliation failure.
- For the supplied Seres contract, resolve `signing.enabled` from the project
  key `seres.sign`. When true, preserve this exact canonicalization: URL path
  from the request path segments; query parameters plus the millisecond
  `timestamp`, empty values removed, array values sorted and joined with commas,
  remaining keys sorted and joined as `key=value`; raw body kept unchanged; then
  append `secretKey` and hash the resulting string with SHA-256 to lowercase hex.
  Set the `sign`, `timestamp`, and `accesskey` headers using the configured
  `SECRET_KEY` and `ACCESS_KEY` sources. Do not reinterpret this as HMAC/RSA or
  URL-encode/reorder values unless the service contract explicitly changes.
- When signing is enabled, require a raw request body whenever the contract
  includes a body. Fail preflight for form-data, binary, multipart, or unknown
  body modes instead of silently signing a missing body. An explicitly bodyless
  request may use the no-body branch.
- When enabled for another project, use only its declared algorithm and
  canonicalization (such as HMAC-SHA256 or RSA-SHA256) and approved key provider.
  Reject an unknown algorithm instead of silently choosing one. Never log or
  commit keys, raw authorization headers, complete signed payloads, or the
  assembled sign string.
- Correlate signed requests with the scenario/run ID where the API contract
  permits it, and preserve only redacted signing diagnostics on failure.

Rules:

- Read `base_url`, credentials, tokens, tenant IDs, feature flags, and other runtime settings from the business project's approved environment contract, `conftest.py`, or configuration provider; never commit secrets or hardcode project-specific defaults.
- Document each setting's source and fixture scope in `E2E_PLAN.md` or `scenarios.yaml`. Keep immutable settings session-scoped, share run settings only within one isolated run, and isolate or restore mutable settings per scenario.
- Mark integration settings as required or optional. Required settings must fail preflight when absent or unauthorized; optional settings may skip only the explicitly dependent scenarios with a recorded reason.
- Generate unique names using the run ID and scenario ID.
- Prefer API cleanup. If database cleanup is necessary, use a dedicated test schema and document the exact tables/keys.
- Register cleanup immediately after creating a resource so teardown still runs after a later failure.
- Poll for eventual consistency with a deadline and useful diagnostics; avoid unbounded sleeps.
- Never depend on test ordering or records left by another test.
- Permit parallel execution only when every owned namespace and mutable setting is scenario-safe. Otherwise mark the affected scenarios serial and protect snapshot/restore with the project's approved lock.
- Preserve request/response diagnostics while redacting secrets.
- Add Chinese docstrings/comments to generated fixtures and helpers explaining
  environment selection, scope/ownership, header precedence, signing branches,
  cleanup, and polling decisions. Keep comments useful rather than restating
  trivial assignments.

Use the copy-ready, heavily commented implementation in
[references/request-signing-template.md](request-signing-template.md) whenever
the Seres signing contract is selected.
