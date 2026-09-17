---
name: bru-api-test-generator
description: Generate, materialize, check, execute, and incrementally maintain auditable Bruno API tests from local OpenAPI, current workspace source, configuration, SQL, tests, and redacted runtime evidence. Use for module-owned HTTP API QA; do not use for browser E2E workflows.
---

# Bruno API Test Generator

Use the CLI as the workflow coordinator. `scripts/qa_constraints.py` is the sole authority for mandatory behavior and lifecycle gates; do not restate or reinterpret its rules in this file.

## Workflow

1. Read the target repository's current workspace instructions, build files, source, configuration, migrations, tests, and existing QA assets.
2. Select a user-provided local OpenAPI file or an existing checked-in contract. Only fetch from an already-running loopback service when no local contract exists.
3. Initialize the QA directory:

   ```bash
   bruno-api-test-generator init --qa-root qa
   ```

4. Review or create `module-map.yaml`, then generate from every relevant current-workspace source root:

   ```bash
   bruno-api-test-generator generate --qa-root qa \
     --openapi qa/data/contracts/openapi.yaml \
     --source-root . --incremental --coverage-profile full-matrix
   ```

5. Resolve any reported gate failures by correcting source evidence, contracts, cases, fixtures, variables, or assertions. Do not weaken the rule manifest.
6. Materialize and check the selected scope:

   ```bash
   bruno-api-test-generator materialize --qa-root qa
   bruno-api-test-generator check --qa-root qa --all
   ```

7. Run the preflight and collection. A module selector limits execution to that module directory:

   ```bash
   bruno-api-test-generator preflight --qa-root qa
   bruno-api-test-generator run --qa-root qa
   bruno-api-test-generator run --qa-root qa --module users
   ```

8. Use the generated result report and observed evidence as the handoff. For delegated module work, initialize assignments first and aggregate only after every module result is available.

## References

Read only the reference needed for the current operation:

- [references/cli.md](references/cli.md) for CLI installation and script synchronization.
- [references/execution-config.md](references/execution-config.md) for environment and launcher configuration.
- [references/offline-swagger.md](references/offline-swagger.md) for local contract acquisition.
- [references/incremental-generation.md](references/incremental-generation.md) for regeneration and preserved edits.
- [references/execution-evidence.md](references/execution-evidence.md) for reports and retained evidence.
- [references/parallel-generation.md](references/parallel-generation.md) only when the user explicitly requests delegated module work.
