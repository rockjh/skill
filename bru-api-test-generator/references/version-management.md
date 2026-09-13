# Business-Code Version Management

Keep a module-independent lock file at contracts/version-lock.yaml:

    version: 1
    business:
      repo: /path/to/business-repository
      commit: abc123...
      source_digest: sha256...
      ref: main
      updated_at: 2026-09-12T00:00:00+00:00
      impact_review: api-impact
      changed_files:
        - ruoyi-admin/src/main/java/.../SysUserController.java

The commit is the business code version that the Bruno collection was reviewed and executed against. It is not the Git commit of a separate Bruno repository. When Bruno files live in the business repository, the checker also records a digest of tracked files outside `qa/**`; QA-only commits therefore do not invalidate the business compatibility lock. Non-Java repositories use the same digest with cross-language defaults for Go, Python, Node/TypeScript, .NET, Rust, and PHP. A source checkout without Git can use a filesystem digest in draft mode; it has no commit identity, and any changed digest is conservatively treated as API-impacting until a verified review re-baselines it.

The compatibility check also inspects tracked, uncommitted worktree changes.
An unchanged `HEAD` is not sufficient evidence when a controller, service,
configuration, or other API-impacting file is dirty; the check returns a dirty
status and refuses completion until the change is committed/stashed or the
tests are reviewed against that exact worktree state.

Use:

    python qa/scripts/check_version_compatibility.py \
      /path/to/business-repository \
      qa/contracts \
      --rules qa/contracts/impact-rules.yaml \
      --phase before-generate

Run the same check with `--phase before-execute` immediately before Bruno
execution; a stale API-impacting source or dirty tracked business file blocks
execution evidence.

Exit meanings:

- 0: lock is current, or it was updated successfully.
- 2: lock is stale but no API-impacting file was detected; review it and advance only after the verified completion gate.
- 3: lock is stale and API-impacting, or the lock is invalid.

After adapting and executing affected tests:

    python qa/scripts/check_version_compatibility.py \
      /path/to/business-repository \
      qa/contracts \
      --rules qa/contracts/impact-rules.yaml \
      --write \
      --tests-adapted \
      --phase complete \
      --completion-report execution-evidence.coverage.json

`--write` is refused unless the supplied coverage report has
`status: verified` and `completion_ok: true`. This keeps a stale or partially
executed collection from advancing the lock, including for non-API changes.

impact-rules.yaml may override api_patterns and ignore_patterns for the repository. Keep the rules conservative: classify a file as API-impacting when in doubt, then let the module review prove that no test change is needed.

Example override:

    api_patterns:
      - "**/controller/**"
      - "**/service/**"
      - "**/dto/**"
      - "**/exception/**"
      - "**/resources/application*.yml"
      - "**/resources/application*.yaml"
    ignore_patterns:
      - "**/target/**"
      - "**/*.md"
