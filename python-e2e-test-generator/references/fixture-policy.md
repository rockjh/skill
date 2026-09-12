# E2E Fixture Policy

Use the narrowest fixture scope that provides isolation. The concrete settings and scope mapping are defined by the business project; this policy does not prescribe generic field names or defaults:

- **session:** immutable configuration and service health check;
- **run:** unique run ID, test tenant, and shared reporting context;
- **scenario:** actor/session and scenario-owned records;
- **step:** transient data only when a step cannot safely share scenario state.

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
