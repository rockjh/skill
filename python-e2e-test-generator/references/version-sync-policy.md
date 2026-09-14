# Per-Scenario Source Version and Impact Policy

Every `scenarios/*/场景定义.yaml` independently records the repositories and source anchors used to generate that scenario. There is no global source-version file or scenario manifest.

## Required fields

For every relevant repository, record:

```yaml
source_versions:
  - repository: mno-traffic
    path: ../mno-traffic
    generated_from_commit: 500832c542f12fced10a92bedd8225e64f635abd
    last_reviewed_commit: 500832c542f12fced10a92bedd8225e64f635abd
    branch: develop
    dirty: false
    source_anchors:
      - SoftwareSaleSubscriptionController
      - OperatorThresholdFulfillmentService
```

- `generated_from_commit` is the committed source basis used by the current generated test.
- `last_reviewed_commit` is the newest commit whose impact on this scenario has been reviewed.
- `branch` records the branch observed at review/generation time; the commit remains authoritative.
- `dirty` states whether relevant staged, unstaged, or untracked source was part of the review basis.
- `source_anchors` are concrete entrypoints, callers, request/response models, producers/consumers, tables/migrations, jobs, or configuration symbols that allow impact rediscovery.

When `dirty: true`, add relevant files and SHA-256 values:

```yaml
    dirty_files:
      - path: src/main/java/.../OperatorThresholdFulfillmentService.java
        state: unstaged
        sha256: <lowercase-hex>
```

Use `state: staged`, `unstaged`, or `untracked`. If a file has different staged and working-tree contents, record both snapshots separately or use distinct `index_sha256` and `worktree_sha256` fields. Never imply that `HEAD` alone represents dirty source.

## Review algorithm

For each scenario and each repository, independently:

1. Read `last_reviewed_commit`, `generated_from_commit`, and `source_anchors` from that scenario.
2. Resolve the repository path and capture current `HEAD`, branch, staged changes, unstaged changes, and untracked files.
3. Verify that the old commit exists. If it does, run `git diff --name-status <last_reviewed_commit>..<HEAD>`.
4. Inspect the actual old and new contents of potentially relevant tracked files. File names and diff status alone are not enough.
5. Re-resolve every source anchor and trace affected callers, request/response models, message producers and consumers, schemas/migrations/tables, jobs, and configuration dependencies.
6. Inspect relevant staged, unstaged, and untracked contents. Hash each relevant dirty snapshot with SHA-256.
7. Decide whether the changes alter the scenario's inputs, preconditions, steps, expected outcomes, assertions, correlation keys, integration mappings, timing/polling, configuration restoration, or cleanup.
8. Append evidence and the decision to `版本变更记录.md`, then update version fields according to the outcome below.

Do not treat an unchanged anchor file as proof of no impact: a caller, model, schema, consumer, configuration, or job reachable from the anchor may have changed.

## Outcomes

### `unchanged`

`HEAD` equals `last_reviewed_commit` and there are no relevant dirty changes. Do not rewrite artifacts.

### `no_relevant_change`

The repository changed, but inspected committed/dirty changes do not affect the scenario. Keep the test and business artifacts unchanged, append the evidence, and set `last_reviewed_commit` to current `HEAD`. Leave `generated_from_commit` unchanged.

### `affected`

The source changes affect this scenario. Update the scenario definition, pytest, both diagrams, and any affected data/mappings/cleanup. Set both commit fields to current `HEAD` after regeneration/review. Record relevant dirty files and hashes when applicable; `generated_from_commit` then means `HEAD` plus the explicitly recorded dirty snapshots, not `HEAD` alone.

### `full_rediscovery_required`

The old commit is unavailable, history was rewritten, or anchors can no longer be resolved reliably. Repeat full source discovery for this scenario, rebuild its anchors and contract, then update both commit fields. Record why incremental comparison was impossible.

### `dirty_review_required`

Relevant uncommitted changes exist but their impact has not been reviewed. Do not claim the scenario is current. After review, resolve to `no_relevant_change` or `affected`, set `dirty: true`, and record hashes.

## Script behavior

`scripts/check_source_versions.py` discovers `scenarios/*/场景定义.yaml` and emits one decision per scenario/repository. Its output includes old/current commit, branch, dirty state, changed files, affected anchors, decision, and reason. It must not silently update `last_reviewed_commit`, regenerate tests, discard working-tree changes, fetch/rewrite Git history, or collapse multiple scenarios into a single project-level result.

An update/regeneration command may write version fields and `版本变更记录.md` only after it has completed content inspection and impact analysis. Failure to access a repository or inspect a required file is an actionable incomplete review, not `no_relevant_change`.
