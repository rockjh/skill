"""用临时源码仓库验证已安装门禁的完整正向流程。"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


INSTALLER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "install_e2e_gates.py"
INSTALLER_SPEC = importlib.util.spec_from_file_location("install_e2e_gates_forward", INSTALLER_PATH)
assert INSTALLER_SPEC and INSTALLER_SPEC.loader
INSTALLER = importlib.util.module_from_spec(INSTALLER_SPEC)
INSTALLER_SPEC.loader.exec_module(INSTALLER)


def _run(command: list[str], cwd: Path, environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """运行临时工程命令并捕获诊断。"""

    return subprocess.run(command, cwd=cwd, env=environment, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")


class ForwardProjectTests(unittest.TestCase):
    """验证发现、契约、静态和源码版本入口可连续通过。"""

    def test_installed_gates_accept_complete_project(self) -> None:
        """完整且安全的合成工程应通过所有环境无关门禁。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            project = root / "generated-e2e"
            source.mkdir()
            project.mkdir()
            (source / "pom.xml").write_text(
                "<project><modelVersion>4.0.0</modelVersion><groupId>example</groupId><artifactId>app</artifactId><version>1</version></project>\n",
                encoding="utf-8",
            )
            (source / "application.properties").write_text(
                "SourceAnchor.name=evidence\nSourceAnchor.connection=${EVIDENCE_CONNECTION}\n",
                encoding="utf-8",
            )
            (source / "repository.py").write_text(
                '"""合成只读仓库。"""\n\ndef SourceAnchor(cursor, key):\n    """执行参数化只读查询。"""\n    return cursor.execute("SELECT value FROM evidence WHERE id=?", (key,))\n',
                encoding="utf-8",
            )
            for command in (
                ["git", "init", "-q"],
                ["git", "config", "user.email", "test@example.invalid"],
                ["git", "config", "user.name", "E2E Test"],
                ["git", "add", "."],
                ["git", "commit", "-q", "-m", "fixture"],
            ):
                completed = _run(command, source)
                self.assertEqual(0, completed.returncode, completed.stderr)
            commit = _run(["git", "rev-parse", "HEAD"], source).stdout.strip()

            scenario = project / "scenarios" / "读取证据"
            environment = project / "config" / "environments"
            discovery = project / "discovery"
            scenario.mkdir(parents=True)
            environment.mkdir(parents=True)
            discovery.mkdir(parents=True)
            (discovery / "workspace.yaml").write_text(self._workspace_yaml(commit), encoding="utf-8")
            (project / "config" / "config.yaml").write_text(self._config_yaml(), encoding="utf-8")
            (environment / "test.yaml").write_text(self._environment_yaml(), encoding="utf-8")
            (scenario / "场景定义.yaml").write_text(self._scenario_yaml(commit), encoding="utf-8")
            (scenario / "业务数据.json").write_text(
                '{"test":{"query":{"key":"${TEST_QUERY_KEY}","kind":"source-confirmed"}}}\n', encoding="utf-8"
            )
            (scenario / "业务流程图.md").write_text(
                "# 读取证据\n\n1. 查询证据。\n2. 验证结果。\n\n```mermaid\nsequenceDiagram\n    participant A as 调用方\n    participant B as 证据系统\n    A->>B: 查询\n    B-->>A: 返回结果\n```\n",
                encoding="utf-8",
            )
            (scenario / "步骤.py").write_text(self._scenario_steps(), encoding="utf-8")
            (scenario / "test_读取证据.py").write_text(self._scenario_test(), encoding="utf-8")
            (project / "pyproject.toml").write_text(
                '[project]\nname = "generated-e2e"\nversion = "0.0.0"\ndependencies = ["pytest", "PyYAML"]\n\n'
                '[tool.pytest.ini_options]\nmarkers = ["business_e2e: 业务端到端场景", "scenario_id(value): 稳定场景编号", "read_only_smoke: 只读冒烟"]\n',
                encoding="utf-8",
            )
            INSTALLER.install(project)

            missing_environment = os.environ.copy()
            missing_environment.pop("EVIDENCE_CONNECTION", None)
            missing_environment.pop("TEST_QUERY_KEY", None)
            discovery_check = _run(
                [sys.executable, "scripts/check_scenarios.py", "--gate", "discovery"], project, missing_environment
            )
            self.assertEqual(0, discovery_check.returncode, discovery_check.stdout + discovery_check.stderr)
            missing_check = _run(
                [sys.executable, "scripts/check_scenarios.py", "--gate", "contracts"], project, missing_environment
            )
            self.assertNotEqual(0, missing_check.returncode)
            self.assertIn("ready-runtime-values", missing_check.stderr)

            commands = (
                *(
                    [sys.executable, "scripts/check_scenarios.py", "--gate", gate]
                    for gate in (
                        "workspace_inventory", "dependency_topology", "initial_configuration", "runtime_probe",
                        "control_matrix", "scenario_split", "scenario_ownership", "shared_integration",
                    )
                ),
                [sys.executable, "scripts/check_scenarios.py", "--gate", "static"],
                [sys.executable, "scripts/check_source_versions.py"],
            )
            process_environment = os.environ.copy()
            process_environment.update({"EVIDENCE_CONNECTION": "driver-reference", "TEST_QUERY_KEY": "query-key"})
            for command in commands:
                completed = _run(command, project, process_environment)
                self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    @staticmethod
    def _workspace_yaml(commit: str) -> str:
        """生成完整发现契约。"""

        template = textwrap.dedent(
            f"""\
            # 用途：记录合成工作区发现事实；禁止保存凭据值。
            schema_version: 1
            # 工作区清单：覆盖源码仓库和构建工程。
            inventory:
              roots: [../source]
              repositories:
                - id: repo
                  root: ../source
                  commit: {commit}
                  build_files: [pom.xml]
                  modules:
                    - id: app
                      path: .
                      kind: application
              existing_e2e: []
            # 依赖拓扑：当前场景只涉及单一模块。
            topology:
              nodes:
                - id: repo:app
                  relevant: true
              edges: []
              searches:
                http_rpc:
                  queries: [HTTP 和 RPC 调用入口]
                  evidence: []
                  conclusion: 未发现 HTTP 或 RPC 调用
                messages:
                  queries: [消息生产与消费入口]
                  evidence: []
                  conclusion: 未发现消息链路
                database:
                  queries: [数据库访问入口]
                  evidence: [repo#SourceAnchor]
                  conclusion: 发现只读证据访问契约
                cache:
                  queries: [缓存访问入口]
                  evidence: []
                  conclusion: 未发现缓存依赖
                jobs:
                  queries: [定时任务与受控触发入口]
                  evidence: []
                  conclusion: 未发现任务入口
                configuration:
                  queries: [配置来源与覆盖入口]
                  evidence: [repo#SourceAnchor.name]
                  conclusion: 发现文件配置来源
            # 配置发现：所有值均记录最终来源。
            configuration:
              sources:
                - id: source-file
                  owner: repo:app
                  kind: file
                  location: application.properties
                  profile: null
                  overrides: []
                  evidence: [repo#SourceAnchor.name]
              precedence: [source-file]
              services: []
              data_sources:
                - id: evidence-store
                  owner: repo:app
                  type: relational
                  name:
                    value: evidence
                    effective_source: source-file
                    resolution: resolved
                    source_key: SourceAnchor.name
                  connection_source:
                    reference: ${{EVIDENCE_CONNECTION}}
                    effective_source: source-file
                    resolution: unresolved
                    source_key: SourceAnchor.connection
              middleware: []
              controls: []
            # 运行探测：本次合成验证未声明应用已启动。
            runtime_probe:
              requested: false
              outcome: not_requested
              blockers: []
              listeners: []
              processes: []
              associations: []
              read_only_smoke: []
            # 阶段门禁：发现事实已完整复核。
            gates:
              inventory_complete: true
              topology_complete: true
              configuration_complete: true
              runtime_probe_complete: true
            """
        )
        return template

    @staticmethod
    def _config_yaml() -> str:
        """生成默认关闭危险能力的运行配置。"""

        template = textwrap.dedent(
            """\
            # 用途：选择合成测试环境；禁止保存地址和凭据。
            active_environment: test
            # 公共默认值：危险能力必须保持关闭。
            defaults:
              safety:
                database_control_enabled: false
                mutable_configuration_enabled: false
                message_publish_enabled: false
            """
        )
        return template

    @staticmethod
    def _environment_yaml() -> str:
        """生成场景所需的环境映射。"""

        return textwrap.dedent(
            """\
            # 用途：映射合成测试环境；敏感值仅使用精确占位符。
            # 服务映射：当前场景不依赖服务。
            services: {}
            # 组件映射：连接值在运行预检时解析。
            components:
              evidence-store:
                type: relational
                connection: ${EVIDENCE_CONNECTION}
            # 安全边界：只允许显式测试环境行为。
            safety:
              test_environment: true
              side_effects_allowed: false
              protected: false
            """
        )

    @staticmethod
    def _scenario_yaml(commit: str) -> str:
        """生成完整只读场景契约。"""

        entries = []
        for name in (
            "public_api", "test_or_admin_api", "mocks_and_faults", "dynamic_configuration",
            "scheduled_jobs", "messages", "database_control",
        ):
            extra = "\n    safety: null" if name == "database_control" else ""
            entries.append(
                f"  {name}:\n    status: not_applicable\n    assessment: 当前场景不需要该能力\n    evidence: []\n    planned_use: []{extra}"
            )
        unused_controls = "\n".join(entries)
        template = textwrap.dedent(
            f"""\
            # 用途：定义只读证据场景；禁止保存凭据或连接值。
            # 场景元数据：稳定标识只在测试 marker 中复用。
            meta:
              id: READ_EVIDENCE
              name: 读取证据
              status: ready
              actor: 查询方
            # 生成职责：单场景由主代理负责。
            generation:
              mode: main_agent
              owner: main-agent
              write_scope: scenarios/读取证据
              degradation_reason: null
            # 就绪判定：源码、控制、配置和数据均已确认。
            readiness:
              source_contract: confirmed
              safe_control: confirmed
              runtime_configuration: confirmed
              test_data: confirmed
              blockers: []
            # 业务前置：查询键属于当前环境测试数据。
            preconditions: [测试查询键已配置]
            # 运行依赖：只使用发现的数据源组件。
            integrations:
              services: []
              components:
                - id: evidence-store
                  type: relational
                  required: true
            # 控制矩阵：全部类别均完成评估。
            controls:
            __UNUSED_CONTROLS__
              database_read:
                status: usable
                assessment: 允许参数化只读查询
                evidence: [repo#SourceAnchor]
                planned_use: [query_evidence]
              observability:
                status: usable
                assessment: 查询结果提供业务证据
                evidence: [repo#SourceAnchor]
                planned_use: [observe_outcome]
                correlation_keys: [query-key]
                business_evidence: [observe_outcome]
                recovery: [verify-no-mutation, no-owned-resource]
              decision:
                safe_control_path: true
                blockers: []
            # 隔离边界：查询键和命名空间由当前场景独占。
            isolation:
              namespace: read-evidence
              correlation_keys: [query-key]
              owned_resources: []
              mutable_controls: []
              serial_lock: null
            # 业务步骤：仅执行只读观察。
            steps:
              - id: observe
                action: query_evidence
                control: database_read
                side_effect: read
                data_ref: 业务数据.json#/query
                expect: [observe_outcome]
            # 清理恢复：验证场景未产生可变状态。
            cleanup:
              strategy: fixture
              actions: [verify-no-mutation]
              verifies: [no-owned-resource]
            # 源码基线：提交和锚点均可解析。
            source:
              - repo: repo
                commit: {commit}
                anchors: [SourceAnchor]
            """
        )
        return template.replace("__UNUSED_CONTROLS__", unused_controls)

    @staticmethod
    def _scenario_test() -> str:
        """生成可收集且具有预检和清理结构的测试入口。"""

        return textwrap.dedent(
            '''\
            """编排只读证据业务场景。"""

            from pathlib import Path

            import pytest

            from common.e2e_runtime import preflight, record_business_entry
            from scenarios.读取证据.步骤 import observe_evidence, verify_no_mutation


            @pytest.mark.business_e2e
            @pytest.mark.scenario_id("READ_EVIDENCE")
            def test_read_evidence() -> None:
                """验证可关联的只读业务证据。"""

                preflight(Path(__file__).resolve().parents[2], "读取证据")
                record_business_entry("读取证据")
                try:
                    assert observe_evidence()["observed"] is True
                finally:
                    verify_no_mutation()
            '''
        )

    @staticmethod
    def _scenario_steps() -> str:
        """生成与 pytest 编排分离的场景动作和清理观察器。"""

        return textwrap.dedent(
            '''\
            """提供合成只读场景的动作和清理观察器。"""


            def observe_evidence() -> dict[str, bool]:
                """返回合成的只读业务观察结果。"""

                return {"observed": True}


            def verify_no_mutation() -> None:
                """确认合成只读场景没有拥有资源。"""
            '''
        )


if __name__ == "__main__":
    unittest.main()
