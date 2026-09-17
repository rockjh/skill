# dev-ai API Test CLI

Install the single `seres-dev-ai` package. There is no domain-specific Python package or compatibility command.

```text
dev-ai api-test init
dev-ai api-test generate
dev-ai api-test materialize
dev-ai api-test check
dev-ai api-test preflight
dev-ai api-test run
dev-ai api-test reconcile
dev-ai api-test aggregate
dev-ai api-test worker-start
dev-ai api-test worker-check
dev-ai api-test scripts
```

Use `--qa-root qa` when the QA root is not the default. `dev-ai schema api-test.<command>` is the source of truth for options. Pipeline output is a JSON envelope; an interactive terminal receives Markdown. Progress is written to stderr. Add `--full` only when complete diagnostics are needed.

`init` writes `.dev-ai.lock.json`. Every later command rejects a different installed tool version. The API contract schema version in that lock is independent from the dev-ai release version.

`scripts` does not synchronize project-local code. It reports the shared runtime
by default and exposes `version-init`, `version-check`, and `version-complete`
for the business source lock; see `dev-ai schema api-test.scripts`.

Generated QA projects contain only contracts, constraints, Bruno collections, execution configuration, and results. They never contain a Python copy of dev-ai.
