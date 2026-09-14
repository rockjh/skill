# Scenario Artifact Policy

Each Chinese-named directory under `scenarios/` owns one complete business journey. A reviewer should be able to understand, collect, run, and maintain it without consulting a global scenario list.

## Required layout

```text
scenarios/<中文业务名称>/
  场景定义.yaml
  场景说明.md
  业务流程图.md
  自动化测试流程图.md
  版本变更记录.md
  test_<中文业务名称>.py
```

The directory, Markdown artifact names, and pytest business-name suffix are Chinese. Technical directories and configuration remain English. The stable ID appears only in `场景定义.yaml` and a pytest marker, for example:

```python
import pytest


@pytest.mark.scenario_id("MNO_REALNAME_DUAL_OPERATOR_RENEWAL")
def test_实名后开通双运营商套餐并验证跨月续订(...):
    ...
```

Do not copy the stable ID into the directory, filename suffix, test function name, headings, logs, or a global manifest. Human-facing scenario artifacts use the business name.

Scenario-owned payloads, expected data, SQL mappings, message mappings, snapshots, and one-off helpers stay inside the scenario directory and use clear Chinese business names. Shared modules contain only reusable transport clients, generic integration adapters, signing, and scope-safe fixtures; they accept configuration and mappings rather than embedding business rules.

## Scenario explanation

`场景说明.md` records the purpose, actor, preconditions, business rules, observable outcomes, environment prerequisites, run command, correlation keys, and cleanup. It may describe `pending_environment` inputs, but must not substitute placeholders for source-confirmed contracts.

## Business flow diagram

`业务流程图.md` contains only real business actors, systems/services, business messages/actions, and state transitions. It must not include pytest, fixtures, test runners, Kafka consumers used only for observation, database observers, polling mechanics, or test cleanup implementation.

## Automated test flow diagram

`自动化测试流程图.md` documents the executable orchestration and must show, when applicable:

1. load the selected environment and run preflight;
2. create a unique Kafka consumer group and subscribe before the business request;
3. record the starting offset, then invoke the public business API;
4. validate HTTP and application-level response fields;
5. consume by order ID or atomic order ID to prove the message was published;
6. poll the database with the same correlation key to prove processing and persistence;
7. compare correlated Kafka and database fields;
8. run API cleanup, close the consumer, and restore scenario configuration.

Kafka publication evidence and database processing/persistence evidence are separate checkpoints. One cannot substitute for the other.

The two diagrams may use Mermaid, but they must model distinct audiences and never be duplicates with renamed headings. Update both whenever an affected source change alters their respective flow.

## Version change record

`版本变更记录.md` is append-oriented and records, per review:

- review time and repository;
- previous and current commit;
- branch and dirty state;
- changed and inspected files, including relevant dirty-file SHA-256 values;
- affected source anchors and caller/contract traversal;
- impact decision and rationale;
- actual changes to the definition, pytest, diagrams, data, or cleanup;
- `last_reviewed_commit` and `generated_from_commit` after the decision.

A no-impact review still records the evidence and `last_reviewed_commit` update. Do not rewrite history merely to make the file appear current.

## Generated Python

Use Chinese comments/docstrings where they clarify business intent, fixture ownership, correlation, bounded polling, restoration, or cleanup. Avoid comments on trivial assignments. Keep source identifiers, protocol fields, configuration keys, and reusable technical modules in their source-defined language.
