# Per-Scenario Source Version and Impact Policy

Every `scenarios/*/场景定义.yaml` records its own compact source basis. There is no global source-version file, scenario manifest, or `版本变更记录.md`; Git history is the change record.

## Source contract

```yaml
source:
  - repo: mno-traffic
    commit: 61604f3dd84cea56cc29732faea2dbd727a6e906
    anchors:
      - SoftwareSaleSubscriptionController
      - OperatorThresholdFulfillmentService
  - repo: mno-operator
    commit: 16640b8fb5dd10c7d1e94eed16b3e35a3cb09077
    anchors:
      - OperatorBusinessOperatorApplication
```

- `repo` is the stable repository name. Resolve its local path from project convention or an explicit command argument, not from each scenario.
- `commit` is the newest committed source revision reviewed against the current scenario.
- `anchors` are a short set of high-value entrypoints, services, producers/consumers, models, tables, jobs, or configuration symbols that make impact rediscovery reliable.

Do not add branch names, duplicate commit fields, review timestamps, narrative notes, or copied diffs to the scenario definition.

When migrating the old `generated_from_commit` / `last_reviewed_commit` format, do not mechanically choose the newer value. Use the generated revision as the initial comparison basis, complete the impact review, regenerate affected artifacts when needed, and only then write the reviewed `HEAD` to `source.commit`.

## Review algorithm

For each scenario and source repository:

1. Resolve `source.commit`, current `HEAD`, and staged, unstaged, and untracked changes.
2. Verify the recorded commit exists; otherwise perform full rediscovery.
3. Inspect `git diff --name-status <commit>..<HEAD>` and the actual old/new contents of potentially relevant files.
4. Re-resolve every anchor and trace affected callers, request/response models, message producers/consumers, schemas, jobs, and configuration.
5. Inspect relevant dirty contents; an unchanged anchor file does not prove no impact.
6. Decide whether changes affect preconditions, data, actions, expectations, correlation, integration mappings, timing, cleanup, or diagrams.
7. When update authorization exists, regenerate only affected artifacts and set `source.commit` to current `HEAD` after the review succeeds. The resulting repository diff is the audit trail.

Relevant dirty source cannot be represented by the compact commit field. Do not claim the scenario is synchronized to dirty source. Ask for a commit before treating it as authoritative, or clearly report a draft result and leave `source.commit` unchanged.

## Outcomes

- `unchanged`: `HEAD` equals `source.commit` and no relevant dirty changes exist.
- `no_relevant_change`: committed changes were inspected and do not affect the scenario. With update authorization, advance `source.commit` to `HEAD`; do not rewrite other artifacts.
- `affected`: committed changes affect the scenario. Update only affected contract, data, code, diagrams, or cleanup, then advance `source.commit` to `HEAD`.
- `full_rediscovery_required`: the old commit is unavailable or anchors cannot be resolved reliably. Repeat source discovery before updating.
- `dirty_review_required`: relevant uncommitted source exists. Report it and do not advance `source.commit` or claim synchronization.

## Script behavior

`scripts/check_source_versions.py` discovers `scenarios/*/场景定义.yaml` and emits one result per scenario/repository with recorded/current commits, dirty state, relevant files, affected anchors, decision, and concise reason.

The check command is read-only. It must not silently update definitions, regenerate tests, discard working-tree changes, fetch or rewrite Git history, or collapse multiple scenarios into one project-level result. An explicit update workflow may write `source.commit` only after content inspection and impact analysis finish.
