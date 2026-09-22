---
name: get-my-dev-lifecycle-toolkit
description: Use the unified dltk CLI for auditable API tests, cross-service E2E scenarios, and source-backed business-flow documents.
---

# Development Lifecycle Toolkit

Use the installed `dltk` command for every operation. The three domains share
one output envelope, semantic exit codes, redaction rules, project locks, and
versioned schemas. Do not copy toolkit source into generated projects.

## Domains

- `api-test`: derive Bruno API cases from reviewed design and a local OpenAPI
  contract. Design owns business expectations; source and runtime provide only
  execution support and observed evidence.
- `e2e`: discover protocols and controls, materialize source-tracked pytest
  scenarios, then run bounded checks with isolation, authorization, cleanup,
  restoration, and evidence.
- `business-flow`: discover implemented entry points and reachable errors,
  assign each entry to one module, generate source-backed documents, and check
  bidirectional coverage and Git/version evidence.

## Workflow

Read `AGENTS.md`, initialize the relevant project, then use the domain's
discover/understand, generate, check, and run commands. The machine contract is
authoritative: query `dltk schema`, `dltk schema <domain.command>`, and the
artifact scopes before constructing files or arguments. Pipeline output is JSON
and interactive output is Markdown; progress is stderr. Use `--full` only when
complete diagnostics are required.

Locks bind the installed dltk release, domain, and independent domain schema
version. A missing or mismatched lock is a precondition failure; never fall
back to copied project code or an older tool. Reports and artifacts are written
to their domain-owned paths and are redacted before output or persistence.

## Safety and ownership

Never write production, `prod`, `prd`, `live`, or protected environments.
Dangerous controls require per-run authorization bound to the active
environment. Prefer public APIs and read-only observation. Any test-owned write
must be isolated, parameterized, idempotently cleaned in reverse order, and
verified restored. Runtime failures remain test failures.

The main agent owns shared assets, discovery, locks, versions, execution,
restoration, and final reports. E2E scenario agents may edit only their assigned
`scenarios/<scenario>/` directory. API workers may edit only their module-owned
Bruno and evidence paths. Business-flow module ownership must be confirmed
before generation.

## References

Read only the needed detail from `references/`: API CLI and generation policy
(`cli.md`, `case-matrix.md`, `coverage-manifest.md`, `database-access.md`,
`execution-config.md`, `execution-evidence.md`, `incremental-generation.md`,
`offline-swagger.md`, `parallel-generation.md`, `version-management.md`), E2E
discovery and execution policy (`discovery-and-control-policy.md`,
`scenario-artifact-policy.md`, `fixture-policy.md`, `integration-policy.md`,
`e2e-workflow.md`, `execution-script-policy.md`, `version-sync-policy.md`), or
business-flow analysis and examples (`analysis-policy.md`, `examples.md`,
`migration.md`, `generated-example/`).
