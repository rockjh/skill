"""Create only business-project assets; the gate runtime stays installed."""

from __future__ import annotations

from pathlib import Path


PYPROJECT = '''[project]
name = "generated-e2e"
version = "0.0.0"
requires-python = ">=3.11"
dependencies = ["pytest>=8", "PyYAML>=6"]

[tool.pytest.ini_options]
markers = [
  "business_e2e: real business E2E scenario",
  "scenario_id(value): stable scenario identifier",
  "read_only_smoke: read-only runtime smoke check",
]
'''

WORKSPACE_TEMPLATE = '''# 用途：记录工作区、依赖、配置和只读运行探测证据；占位值必须替换为发现结果。
schema_version: 1

# 工作区清单。
inventory:
  roots: [<workspace-root>]
  repositories:
    - id: <repository-id>
      root: <repository-root>
      commit: <40-character-git-sha>
      build_files: [<build-file>]
      modules:
        - id: <module-id>
          path: <module-path>
          kind: application
  existing_e2e: []

# 依赖拓扑和源码能力搜索。
topology:
  nodes:
    - id: <repository-id>:<module-id>
      relevant: true
  edges: []
  searches:
    http_rpc: {queries: [<query>], evidence: [], conclusion: <conclusion>}
    messages: {queries: [<query>], evidence: [], conclusion: <conclusion>}
    database: {queries: [<query>], evidence: [], conclusion: <conclusion>}
    cache: {queries: [<query>], evidence: [], conclusion: <conclusion>}
    jobs: {queries: [<query>], evidence: [], conclusion: <conclusion>}
    configuration: {queries: [<query>], evidence: [], conclusion: <conclusion>}

# 配置来源和已发现的集成能力。
configuration:
  sources:
    - id: <source-id>
      owner: <repository-id>:<module-id>
      kind: file
      location: <relative-config-path>
      profile: null
      overrides: []
      evidence: [<repository-id>#<source-anchor>]
  precedence: [<source-id>]
  services: []
  data_sources: []
  middleware: []
  controls: []

# 用途：设计摘要是人工审查的业务权威；不得从源码或运行结果推导。
design:
  files: []
  candidates: []
  summary: {}

# 用途：正式协议摘要只定义传输结构和字段约束。
protocol:
  files: []
  candidates: []
  summary: {}

# 只读运行探测；未请求时保持空列表。
runtime_probe:
  requested: false
  outcome: not_requested
  blockers: []
  listeners: []
  processes: []
  associations: []
  read_only_smoke: []
  configuration_checks: []

# 发现证据完整后才可全部置为 true。
gates:
  inventory_complete: false
  topology_complete: false
  configuration_complete: false
  runtime_probe_complete: false
'''

CONFIG_TEMPLATE = '''# 用途：选择测试环境并定义安全默认值；复制为 config.yaml 后替换环境名。
active_environment: <selected-test-environment>

# 公共运行默认值；危险能力必须保持默认关闭。
defaults:
  polling:
    interval_seconds: 1
    timeout_seconds: 60
  safety:
    database_control_enabled: false
    mutable_configuration_enabled: false
    message_publish_enabled: false
'''

ENVIRONMENT_TEMPLATE = '''# 用途：映射一个已发现的测试环境；只保存精确环境变量占位符，不保存凭据值。
services: {}

# 组件键必须来自 discovery/workspace.yaml。
components: {}

# 环境安全边界；按真实环境能力审查后设置。
safety:
  test_environment: false
  side_effects_allowed: false
  protected: true
'''

SCENARIO_TEMPLATE = '''# 用途：场景定义模板；复制到 scenarios/<场景>/场景定义.yaml 后替换全部占位值。
meta:
  id: <STABLE_SCENARIO_ID>
  name: <业务名称>
  status: pending_environment
  participants: [<participant-service-a>, <participant-service-b>]
  actor: <业务参与者>

# 生成所有权。
generation:
  mode: main_agent
  owner: <owner-id>
  write_scope: scenarios/<场景>
  degradation_reason: null

# 契约和环境就绪状态。
readiness:
  source_contract: confirmed
  safe_control: confirmed
  runtime_configuration: missing
  test_data: missing
  blockers: [connection:<MISSING_VALUE>]

# 可验证的业务前置条件。
preconditions: [<precondition-symbol>]

# 可构造性：每个前置和步骤都必须穷尽八类控制路径；写入探针必须引用正式隔离与清理符号。
constructability:
  preconditions:
    - id: <precondition-symbol>
      data_ownership: test_owned
      constructible: true
      candidates: &candidate-matrix
        - {kind: public_api, status: usable, component: <component>, consumer_source: <repository-id>#<source-anchor>, control: <control-symbol>, side_effect: write, trigger: <trigger-symbol>, observation: <observation-symbol>, isolation: <owned-correlation>, cleanup: <cleanup-action>, evidence: [<repository-id>#<source-anchor>]}
        - {kind: test_or_admin_api, status: not_found, component: <component>, consumer_source: <repository-id>#<source-anchor>, control: <control-symbol>, side_effect: none, trigger: <trigger-symbol>, observation: <observation-symbol>, isolation: <owned-correlation>, cleanup: <cleanup-action>, evidence: [<repository-id>#<source-anchor>]}
        - {kind: database_control, status: not_found, component: <component>, consumer_source: <repository-id>#<source-anchor>, control: <control-symbol>, side_effect: none, trigger: <trigger-symbol>, observation: <observation-symbol>, isolation: <owned-correlation>, cleanup: <cleanup-action>, evidence: [<repository-id>#<source-anchor>]}
        - {kind: messages, status: not_found, component: <component>, consumer_source: <repository-id>#<source-anchor>, control: <control-symbol>, side_effect: none, trigger: <trigger-symbol>, observation: <observation-symbol>, isolation: <owned-correlation>, cleanup: <cleanup-action>, evidence: [<repository-id>#<source-anchor>]}
        - {kind: scheduled_jobs, status: not_found, component: <component>, consumer_source: <repository-id>#<source-anchor>, control: <control-symbol>, side_effect: none, trigger: <trigger-symbol>, observation: <observation-symbol>, isolation: <owned-correlation>, cleanup: <cleanup-action>, evidence: [<repository-id>#<source-anchor>]}
        - {kind: mocks_and_faults, status: not_found, component: <component>, consumer_source: <repository-id>#<source-anchor>, control: <control-symbol>, side_effect: none, trigger: <trigger-symbol>, observation: <observation-symbol>, isolation: <owned-correlation>, cleanup: <cleanup-action>, evidence: [<repository-id>#<source-anchor>]}
        - {kind: dynamic_configuration, status: not_found, component: <component>, consumer_source: <repository-id>#<source-anchor>, control: <control-symbol>, side_effect: none, trigger: <trigger-symbol>, observation: <observation-symbol>, isolation: <owned-correlation>, cleanup: <cleanup-action>, evidence: [<repository-id>#<source-anchor>]}
        - {kind: existing_test_data, status: not_found, component: <component>, consumer_source: <repository-id>#<source-anchor>, control: <control-symbol>, side_effect: none, trigger: <trigger-symbol>, observation: <observation-symbol>, isolation: <owned-correlation>, cleanup: <cleanup-action>, evidence: [<repository-id>#<source-anchor>]}
  steps:
    - step_id: <step-id>
      candidates: *candidate-matrix

# 发现契约中的服务和组件。
integrations:
  services: []
  components: []

# 完整控制矩阵。
controls:
  public_api: {status: not_applicable, assessment: <assessment>, evidence: [], planned_use: []}
  test_or_admin_api: {status: not_applicable, assessment: <assessment>, evidence: [], planned_use: []}
  mocks_and_faults: {status: not_applicable, assessment: <assessment>, evidence: [], planned_use: []}
  dynamic_configuration: {status: not_applicable, assessment: <assessment>, evidence: [], planned_use: []}
  scheduled_jobs: {status: not_applicable, assessment: <assessment>, evidence: [], planned_use: []}
  messages: {status: not_applicable, assessment: <assessment>, evidence: [], planned_use: []}
  database_read: {status: not_applicable, assessment: <assessment>, evidence: [], planned_use: []}
  database_control:
    status: not_applicable
    assessment: <assessment>
    evidence: []
    planned_use: []
    safety: null
  observability:
    status: not_applicable
    assessment: <assessment>
    evidence: []
    planned_use: []
    correlation_keys: []
    business_evidence: []
    recovery: []
  decision:
    safe_control_path: true
    blockers: []

# 场景隔离资源。
isolation:
  namespace: <unique-namespace>
  correlation_keys: [<owned-correlation>]
  owned_resources: []
  mutable_controls: []
  serial_lock: null

# 业务步骤。
steps:
  - id: <step-id>
    action: <business-action>
    control: public_api
    side_effect: read
    design_rule_id: <DESIGN_RULE_ID>
    protocol_ref: <FORMAL_PROTOCOL_OPERATION_ID>
    phase: final_business
    data_ref: 业务数据.json#/<json-pointer>
    expect: [<business-outcome>]
    status: executable
    status_reason: <source-backed-reason>
    evidence: [<repository-id>#<source-anchor>]

# 清理和恢复。
cleanup:
  strategy: <strategy>
  actions: [<cleanup-action>]
  verifies: [<restoration-check>]

# 场景源码基线。
source:
  - repo: <repository-id>
    commit: <40-character-git-sha>
    anchors: [<source-anchor>]
'''

RUN_BAT = '''@echo off
dev-ai e2e run --project "%~dp0" %*
'''

RUN_SH = '''#!/usr/bin/env sh
set -eu
project_root=$(CDPATH= cd -P -- "$(dirname -- "$0")" && pwd)
exec dev-ai e2e run --project "$project_root" "$@"
'''

INITIAL_FILES = {
    "pyproject.toml": PYPROJECT,
    "discovery/workspace.template.yaml": WORKSPACE_TEMPLATE,
    "config/config.template.yaml": CONFIG_TEMPLATE,
    "config/environments/environment.template.yaml": ENVIRONMENT_TEMPLATE,
    "scenarios/scenario.template.yaml": SCENARIO_TEMPLATE,
    "run-e2e.bat": RUN_BAT,
    "run-e2e.sh": RUN_SH,
}


def initialize(project_root: Path) -> list[Path]:
    project_root = project_root.resolve()
    project_root.mkdir(parents=True, exist_ok=True)
    for relative in (
        "discovery",
        "config/environments",
        "common",
        "scenarios",
        "tests",
        "artifacts",
    ):
        (project_root / relative).mkdir(parents=True, exist_ok=True)
    changed: list[Path] = []
    for relative, content in INITIAL_FILES.items():
        path = project_root / relative
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            changed.append(path)
    return changed


def project_errors(project_root: Path) -> list[str]:
    required = ("discovery", "config", "common", "scenarios", "tests", "artifacts", "pyproject.toml")
    return [f"missing generated E2E asset: {project_root / name}" for name in required if not (project_root / name).exists()]
