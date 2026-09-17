# Execution configuration

`qa/execution/config.yaml` contains only execution choices; tooling is always the installed shared CLI.

```yaml
active_environment: local
tooling: shared-cli
coverage_profile: full-matrix
cli_timeout: 60
sign:
  provider: disabled
```

Allowed coverage profiles are `contract-draft` and `full-matrix`. `verified` is an execution result, not a profile. The active Bruno environment lives at `qa/execution/environments/<active_environment>.bru` and may contain variable placeholders and common headers. Store credential values only in the runtime environment, never in contracts or reports.

The generated launchers are thin convenience assets and call the installed `dev-ai api-test` commands; they contain no Python implementation. Run launchers cover tests plus standalone mock-data generation and cleanup on CMD and Shell. `dev-ai api-test scripts` reports the installed domain schema and does not synchronize files.

Preflight validates the project lock, QA lock, execution configuration, environment syntax, static coverage, target URL, required variables, tool availability, and report destinations before Bruno runs. Authoritative reports remain below `qa/results/`.

The active environment name is also the mock-data safety boundary. Production aliases and environments listed by the operator as protected reject creation and deletion before authorization is considered. Data-source credentials are read from process environment variables named in `constraints/mock-data.yaml`; only non-sensitive defaults and empty credential placeholders may appear in the checked-in Bruno environment.
