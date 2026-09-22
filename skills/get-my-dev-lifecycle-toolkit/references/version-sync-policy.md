# Per-Scenario Source Version and Impact Policy

Every `scenarios/*/场景定义.yaml` records its own compact source basis. `discovery/workspace.yaml` maps stable repository IDs to resolvable roots, but there is no global scenario registry or duplicate scenario-version file. Git history is the change record.

## Source contract

```yaml
# 源码基线：每项对应一个被当前场景直接依赖的仓库。
source:
  - repo: <repository-id-from-discovery>
    # 40 位 Git SHA，表示已经完成场景影响审查的提交。
    commit: <40-character-git-sha>
    # 用于重新定位入口、模型、状态变化、控制和清理的高价值源码符号。
    anchors:
      - <source-symbol>
```

Angle-bracket values are metavariables. `repo` must resolve through the discovery inventory, and `commit` must equal that repository's reviewed discovery snapshot. `anchors` are a short set of entrypoints, services, producers/consumers, schemas, repositories, jobs, configuration symbols, controls, or cleanup methods that make rediscovery reliable. The source list covers every repository marked relevant to the scenario's topology; one caller repository cannot stand in for a downstream service, message, or data-store owner.

Do not add branch names, duplicate commit fields, timestamps, narrative notes, or copied diffs. Relevant dirty source cannot be represented by `commit` and must be reported separately.

When migrating an older source-version format, do not mechanically select a newer revision. Use the prior generated revision as the comparison basis, complete impact review, regenerate only affected artifacts, and then write reviewed `HEAD`.

## Review algorithm

For every scenario/repository pair:

1. Resolve the repository through `discovery/workspace.yaml`, then read recorded commit, current `HEAD`, and staged, unstaged, and untracked changes.
2. Verify the recorded commit exists; otherwise repeat full relevant discovery.
3. Inspect `git diff --name-status <commit>..<HEAD>` and actual old/new contents of potentially relevant files.
4. Re-resolve each anchor and trace affected callers, request/response or message models, state rules, correlation, persistence, jobs, configuration, controls, and cleanup.
5. Inspect relevant dirty contents; an unchanged anchor file does not prove no impact.
6. Decide whether changes affect topology, configuration precedence, preconditions, data, actions, controls, expectations, correlation, integrations, timing, cleanup, restoration, or diagrams.
7. With update authorization, regenerate only affected artifacts and update the discovery repository commit plus every affected `source.commit` to the same reviewed SHA. Re-run discovery before contract gates; the repository diff is the audit trail.

Do not claim synchronization to dirty relevant source. Ask for a commit before treating it as authoritative, or label generated work as draft and leave the recorded commit unchanged.

If new repositories, build modules, dependency edges, or configuration sources appear, update and revalidate workspace discovery before scenario regeneration. Topology cannot be inferred only from the prior scenario anchors.

## Outcomes

- `unchanged`: `HEAD` equals recorded commit and no relevant dirty changes exist.
- `no_relevant_change`: committed changes were inspected and do not affect the scenario. With authorization, advance the recorded commit without rewriting other artifacts.
- `affected`: committed changes affect the contract or generated implementation. Update only affected artifacts, then advance the commit.
- `full_rediscovery_required`: the old commit is unavailable, an anchor is unresolved, or workspace topology/configuration changed materially.
- `dirty_review_required`: relevant uncommitted source exists. Report it and do not advance the commit or claim synchronization.

## Script behavior

`dltk e2e source-status` discovers `scenarios/*/场景定义.yaml` directly and resolves repository IDs through discovery. It accepts optional exact `--scenario <中文场景名称>`; unknown, ambiguous, or path-like values are errors.

Emit one result per scenario/repository with recorded/current commit, clean/dirty state, relevant changed files, affected or unresolved anchors, exactly one outcome above, and a concise reason.

The command is read-only. It must not fetch, update definitions, regenerate tests, discard changes, rewrite history, or collapse several scenarios into one project-level decision. Updating source commits is a separate explicitly authorized workflow after content inspection.
