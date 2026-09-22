# dltk Repository Rules

These rules apply to the whole repository. A deeper `AGENTS.md` may add
constraints but may not relax them.

## Architecture

- The repository, Python package, public CLI, and npm installer are `dltk`;
  the npm package is `seres-dltk`.
- The only console entry point is `dltk = dltk.cli:console_main`.
  `python -m dltk` calls that same entry point.
- All public and domain modules live directly in `dltk/`, with domain-specific
  files named `api_test_*`, `e2e_*`, or `business_flow_*`; the sole Skill lives
  in `skills/get-my-dev-lifecycle-toolkit/`.
- Commands use static registration. Do not add dynamic plugin discovery,
  runtime scanning, compatibility facades, placeholder modules, or a second
  extension mechanism.
- Do not restore legacy package/command names, the old Skill directories, old
  npm wrappers, a second `pyproject.toml`, or copied toolkit source in generated
  projects.

## Contracts

`dltk/envelope.py`, `errors.py`, `schema.py`, `redaction.py`, and
`artifacts.py` are authoritative for envelopes, exit codes, scoped schemas,
redaction, artifacts, and locks. All domains route results through this core.
Pipelines default to JSON, TTYs to Markdown, progress goes to stderr, and
large results return a summary plus an authoritative path unless `--full` is
requested. Domain schema versions are independent of the tool version.

The active domains are `api-test`, `e2e`, and `business-flow`. Preserve their
existing business, safety, ownership, and report semantics while changing only
the package, CLI, Skill, state, install, and release structure.

## Generated assets and release

Generated projects contain business assets and thin launchers that call an
installed `dltk`. They must not contain toolkit source. The project lock is
`.dltk.lock.json`; shared state is `~/.local/state/dltk/`; the Skill target is
`DLTK_SKILL_HOME` or `~/.agents/skills/get-my-dev-lifecycle-toolkit/`.

The npm flow is embedded wheel in `npm/dist/`, pipx, one Skill directory, then
`dltk doctor`. The wrapper resolves the pipx-installed absolute path and never
recursively starts itself through `PATH`. `npx seres-dltk install` is the
explicit recovery command when npm postinstall is disabled.

Run `python scripts/migrate_state.py` once on a machine that has legacy local
state. Runtime code reads only the new state directory and never falls back to
the legacy path.

## Verification

For code/schema/template changes run the relevant tests, then:

```text
python -m pytest -q
python -m compileall -q dltk tests
git diff --check
```

Release changes additionally run `python scripts/release.py`, verify one wheel
with only the `dltk` entry point, one npm Skill payload, synchronized versions,
and the isolated npm -> pipx -> Skill sync -> `doctor` flow. Do not commit
`dist/`, npm wheel/vendor/tgz artifacts, caches, or local state.
