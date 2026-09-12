# Business-Code Version Management

Keep a module-independent lock file at contracts/version-lock.yaml:

    version: 1
    business:
      repo: /path/to/business-repository
      commit: abc123...
      ref: main
      updated_at: 2026-09-12T00:00:00+00:00
      impact_review: api-impact
      changed_files:
        - ruoyi-admin/src/main/java/.../SysUserController.java

The commit is the business code version that the Bruno collection was reviewed and executed against. It is not the Git commit of a separate Bruno repository. When Bruno files live in the business repository, update this lock in the same commit as the adapted collection.

Use:

    python qa/scripts/check_version_compatibility.py \
      /path/to/business-repository \
      qa/contracts \
      --rules qa/contracts/impact-rules.yaml

Exit meanings:

- 0: lock is current, or it was updated successfully.
- 2: lock is stale but no API-impacting file was detected; advance it after review.
- 3: lock is stale and API-impacting, or the lock is invalid.

After adapting and executing affected tests:

    python qa/scripts/check_version_compatibility.py \
      /path/to/business-repository \
      qa/contracts \
      --rules qa/contracts/impact-rules.yaml \
      --write \
      --tests-adapted

For a non-API change, --write can advance the lock without modifying Bruno files. For an API-impacting change, --tests-adapted is intentionally required.

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
