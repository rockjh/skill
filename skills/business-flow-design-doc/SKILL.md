---
name: business-flow-design-doc
description: Generate and maintain source-backed business-flow design documents for a repository, covering business entry points, reachable active error codes, Mermaid sequences, module ownership, and Git-version validity. Use for full, scoped, incremental, or coverage-consistency documentation; do not use for future designs, parameter manuals, database dictionaries, or test cases.
---

# Business Flow Design Document

Use the installed `dev-ai business-flow` commands for repository discovery, document generation, incremental maintenance, and coverage checks. The source at the selected Git revision is the only fact source.

## Workflow

1. Read repository instructions and existing design documents, then run `dev-ai business-flow init --project <path> --docs-root <path>`.
2. Run `dev-ai business-flow discover`. Review `business-flow-discovery.json` and `business-flow-modules.json`; assign one owner and rationale to every entry, record evidence-backed resolutions/overrides/additional entries, document exclusions, and set `confirmed: true`. The map is tied to a source fingerprint and becomes invalid after source/config changes.
3. Generate all modules with `dev-ai business-flow generate`. Use `--module` only when the request is explicitly scoped. Use `--commit` for a Git revision; a non-HEAD revision is analyzed from a read-only Git snapshot. Generation fails closed on unconfirmed mappings or unresolved evidence.
4. For an existing document set, use `dev-ai business-flow update`; it compares the recorded revision and source fingerprint with the target and distinguishes version-only changes from business changes. An unavailable old revision is a full-validation case, never an assumption of no impact.
5. Run `dev-ai business-flow check` after generation. Treat missing or stale entries, per-entry error evidence, module duplicates, version/fingerprint mismatches, dirty-worktree acknowledgement failures, Mermaid mismatches, and unresolved evidence as review items.

## Source and coverage rules

- Discover registered HTTP/HTTPS, WebSocket/SSE, RPC/gRPC, scheduled, message, event, file, webhook, CLI, batch, workflow, and framework-registered handlers from source/configuration. Exclude health, management, and static-resource endpoints only with an evidence note.
- Trace each handler through statically resolvable calls in the selected revision. Record validation order, rules, persistence, external calls, messages, async work, transaction/lock/idempotency evidence, state changes, success, and unknown behavior. Do not infer behavior from names or requirements.
- Record every reachable explicit `raise`/`throw` error with its trigger and file/line evidence. Include mapped errors only when the conversion is present in code. If a code, caller, branch, or module boundary cannot be proven, write `代码中未确认` and retain the evidence path.
- Assign one primary business module per entry. Cross-module calls are described in the owning entry and are not duplicated as entries.
- Every entry contains one independent `sequenceDiagram` with `autonumber`, actual participants, `alt` for exclusive outcomes, `opt` for optional work, and `loop` for repeated processing. The diagram must not invent a success or failure path absent from code.
- Every document and report records the effective commit. A dirty worktree must be marked as including uncommitted changes and cannot be described as valid for the commit alone.

## Safety and ownership

This skill is read-only with respect to application source and Git history. It may write only the requested docs root and the shared project lock created by `init`. Do not change business code to make coverage pass. The primary Agent owns the complete inventory, module map, shared index/report, and final check; any delegated reviewer may edit only its assigned module evidence and must return it to the primary Agent for reconciliation. Use `dev-ai schema business-flow.<command>` and the scoped artifact schemas for machine contracts; do not duplicate those contracts in this file.

## References

Read [references/analysis-policy.md](references/analysis-policy.md) when the repository uses non-HTTP entry points, has ambiguous module boundaries, or needs a detailed incremental/coverage review.
