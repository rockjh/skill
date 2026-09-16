"""验证 E2E 门禁的关键拒绝路径和顺序摘要。"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GUARD_SPEC = importlib.util.spec_from_file_location("e2e_guard", PROJECT_ROOT / "scripts" / "e2e_guard.py")
assert GUARD_SPEC and GUARD_SPEC.loader
GUARD = importlib.util.module_from_spec(GUARD_SPEC)
GUARD_SPEC.loader.exec_module(GUARD)


class StaticGuardTests(unittest.TestCase):
    """覆盖 Python、SQL、冒烟和跨场景隔离规则。"""

    def test_discovery_rejects_declared_build_without_module(self) -> None:
        """构建描述即使已登记，也不能遗漏其对应模块。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "generated-e2e"
            source = Path(temporary) / "source"
            (root / "discovery").mkdir(parents=True)
            (source / ".git").mkdir(parents=True)
            (source / "nested").mkdir()
            (source / "pom.xml").write_text("<project/>", encoding="utf-8")
            (source / "nested" / "pom.xml").write_text("<project/>", encoding="utf-8")
            document = {
                "schema_version": 1,
                "inventory": {
                    "roots": ["../source"],
                    "repositories": [{
                        "id": "repo",
                        "root": "../source",
                        "commit": "0" * 40,
                        "build_files": ["pom.xml", "nested/pom.xml"],
                        "modules": [{"id": "root", "path": ".", "kind": "application"}],
                    }],
                    "existing_e2e": [],
                },
                "topology": {},
                "configuration": {},
                "runtime_probe": {},
                "gates": {},
            }
            (root / "discovery" / "workspace.yaml").write_text(
                GUARD.yaml.safe_dump(document, allow_unicode=True), encoding="utf-8"
            )

            errors, _ = GUARD.discovery_errors(root)
            self.assertTrue(any("inventory-module-complete" in error for error in errors))
            self.assertTrue(any("build-descriptor-semantic" in error for error in errors))

    def test_semantic_evidence_checks_all_anchor_matches(self) -> None:
        """同名配置键不得遮蔽后续具有真实语义的源码锚点。"""

        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "application.properties").write_text("SourceAnchor.name=value\n", encoding="utf-8")
            (source / "repository.py").write_text(
                'def SourceAnchor(cursor, key):\n    return cursor.execute("SELECT value FROM evidence WHERE id=?", (key,))\n',
                encoding="utf-8",
            )
            for command in (
                ["git", "init", "-q"],
                ["git", "config", "user.email", "test@example.invalid"],
                ["git", "config", "user.name", "E2E Test"],
                ["git", "add", "."],
                ["git", "commit", "-q", "-m", "fixture"],
            ):
                completed = subprocess.run(command, cwd=source, check=False, capture_output=True, text=True)
                self.assertEqual(0, completed.returncode, completed.stderr)
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=source, check=True, capture_output=True, text=True
            ).stdout.strip()

            self.assertTrue(GUARD._semantic_evidence(source, commit, "SourceAnchor", "database"))

    def test_multi_module_http_signal_requires_topology_edge(self) -> None:
        """多相关模块已有 HTTP 源码证据时不能用空拓扑边通过。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "generated-e2e"
            source = Path(temporary) / "source"
            (root / "discovery").mkdir(parents=True)
            (source / "first").mkdir(parents=True)
            (source / "second").mkdir()
            (source / "first" / "pom.xml").write_text(
                "<project><artifactId>first</artifactId></project>", encoding="utf-8"
            )
            (source / "first" / "client.py").write_text(
                "def HttpAnchor(client):\n    return requests.get('/health')\n", encoding="utf-8"
            )
            (source / "second" / "pom.xml").write_text(
                "<project><artifactId>second</artifactId></project>", encoding="utf-8"
            )
            for command in (
                ["git", "init", "-q"],
                ["git", "config", "user.email", "test@example.invalid"],
                ["git", "config", "user.name", "E2E Test"],
                ["git", "add", "."],
                ["git", "commit", "-q", "-m", "fixture"],
            ):
                completed = subprocess.run(command, cwd=source, check=False, capture_output=True, text=True)
                self.assertEqual(0, completed.returncode, completed.stderr)
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=source, check=True, capture_output=True, text=True
            ).stdout.strip()
            searches = {
                name: {"queries": [name], "evidence": [], "conclusion": "未发现"}
                for name in GUARD.SEARCH_CATEGORIES
            }
            searches["http_rpc"] = {
                "queries": ["HTTP"], "evidence": ["repo#HttpAnchor"], "conclusion": "发现模块调用",
            }
            document = {
                "schema_version": 1,
                "inventory": {
                    "roots": ["../source"],
                    "repositories": [{
                        "id": "repo", "root": "../source", "commit": commit,
                        "build_files": ["first/pom.xml", "second/pom.xml"],
                        "modules": [
                            {"id": "first", "path": "first", "kind": "application"},
                            {"id": "second", "path": "second", "kind": "application"},
                        ],
                    }],
                    "existing_e2e": [],
                },
                "topology": {
                    "nodes": [
                        {"id": "repo:first", "relevant": True},
                        {"id": "repo:second", "relevant": True},
                    ],
                    "edges": [],
                    "searches": searches,
                },
                "configuration": {},
                "runtime_probe": {},
                "gates": {},
            }
            (root / "discovery" / "workspace.yaml").write_text(
                GUARD.yaml.safe_dump(document, allow_unicode=True), encoding="utf-8"
            )

            errors, _ = GUARD.discovery_errors(root)
            self.assertTrue(any("topology-search-edge" in error for error in errors))

    def test_configuration_rejects_resolved_empty_value(self) -> None:
        """resolved 不能用于掩盖仍未解析的空配置值。"""

        configuration = {
            "sources": [{
                "id": "environment",
                "owner": "repo:app",
                "kind": "environment",
                "location": "environment:PORT",
                "profile": None,
                "overrides": [],
                "evidence": ["repo#PORT"],
            }],
            "precedence": ["environment"],
            "services": [{
                "id": "service",
                "owner": "repo:app",
                "port": {"value": None, "effective_source": "environment", "resolution": "resolved", "source_key": "PORT"},
                "context_path": {"value": "/", "effective_source": "environment", "resolution": "resolved", "source_key": "PORT"},
                "health": {"value": None, "effective_source": "environment", "resolution": "unresolved", "source_key": "PORT"},
                "openapi": {"value": None, "effective_source": "environment", "resolution": "unresolved", "source_key": "PORT"},
            }],
            "data_sources": [],
            "middleware": [],
            "controls": [],
        }

        errors = GUARD._configuration_errors(
            Path("workspace.yaml"), configuration, {"repo:app"}, {"repo:app"}, {"repo": Path(".")}
        )
        self.assertTrue(any("configuration-resolved-value" in error for error in errors))

        configuration["services"] = []
        configuration["data_sources"] = [{
            "id": "store",
            "owner": "repo:app",
            "type": "relational",
            "name": {"value": "store", "effective_source": "environment", "resolution": "resolved", "source_key": "PORT"},
            "connection_source": {
                "reference": "plain-secret-value",
                "effective_source": "environment",
                "resolution": "resolved",
                "source_key": "PORT",
            },
        }]
        errors = GUARD._configuration_errors(
            Path("workspace.yaml"), configuration, {"repo:app"}, {"repo:app"}, {"repo": Path(".")}
        )
        self.assertTrue(any("configuration-reference" in error for error in errors))
        self.assertTrue(GUARD._secret_errors(Path("config.yaml"), {"credential_reference": "plain-secret-value"}))

    def test_configuration_precedence_requires_explicit_override_chain(self) -> None:
        """配置优先级不能只靠任意排序声明，必须绑定逐层覆盖关系。"""

        configuration = {
            "sources": [
                {
                    "id": "base", "owner": "repo:app", "kind": "environment", "location": "environment:RUNTIME",
                    "profile": None, "overrides": [], "evidence": ["repo#base"],
                },
                {
                    "id": "runtime", "owner": "repo:app", "kind": "environment", "location": "environment:RUNTIME",
                    "profile": None, "overrides": [], "evidence": ["repo#runtime"],
                },
            ],
            "precedence": ["base", "runtime"],
            "services": [],
            "data_sources": [],
            "middleware": [],
            "controls": [],
        }

        errors = GUARD._configuration_errors(
            Path("workspace.yaml"), configuration, {"repo:app"}, {"repo:app"}, {"repo": Path(".")}
        )
        self.assertTrue(any("configuration-precedence-binding" in error for error in errors))

    def test_configuration_precedence_keeps_independent_owners_separate(self) -> None:
        """不同模块的配置来源不应被强制声明不存在的跨项目覆盖关系。"""

        configuration = {
            "sources": [
                {
                    "id": "first", "owner": "first:app", "kind": "environment",
                    "location": "environment:FIRST_PORT", "profile": None,
                    "overrides": [], "evidence": ["first#FIRST_PORT"],
                },
                {
                    "id": "second", "owner": "second:app", "kind": "environment",
                    "location": "environment:SECOND_PORT", "profile": None,
                    "overrides": [], "evidence": ["second#SECOND_PORT"],
                },
            ],
            "precedence": ["first", "second"],
            "services": [], "data_sources": [], "middleware": [], "controls": [],
        }

        errors = GUARD._configuration_errors(
            Path("workspace.yaml"), configuration,
            {"first:app", "second:app"}, {"first:app", "second:app"},
            {"first": Path("."), "second": Path(".")},
        )
        self.assertFalse(any("configuration-precedence-binding" in error for error in errors))

    def test_configuration_file_source_binds_location_key_and_value(self) -> None:
        """文件配置证据、属性键和已解析值必须来自固定提交中的同一文件。"""

        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "application.properties").write_text(
                "service.port=8080\nservice.context=/\nservice.health=/health\nservice.openapi=/openapi\n",
                encoding="utf-8",
            )
            (source / "other.txt").write_text("OtherAnchor\n", encoding="utf-8")
            for command in (
                ["git", "init", "-q"],
                ["git", "config", "user.email", "test@example.invalid"],
                ["git", "config", "user.name", "E2E Test"],
                ["git", "add", "."],
                ["git", "commit", "-q", "-m", "fixture"],
            ):
                completed = subprocess.run(command, cwd=source, check=False, capture_output=True, text=True)
                self.assertEqual(0, completed.returncode, completed.stderr)
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=source, check=True, capture_output=True, text=True
            ).stdout.strip()
            configuration = {
                "sources": [{
                    "id": "file", "owner": "repo:app", "kind": "file",
                    "location": "application.properties", "profile": None,
                    "overrides": [], "evidence": ["repo#OtherAnchor"],
                }],
                "precedence": ["file"],
                "services": [{
                    "id": "service", "owner": "repo:app",
                    "port": {"value": 9090, "effective_source": "file", "resolution": "resolved", "source_key": "service.port"},
                    "context_path": {"value": "/", "effective_source": "file", "resolution": "resolved", "source_key": "service.context"},
                    "health": {"value": "/health", "effective_source": "file", "resolution": "resolved", "source_key": "service.health"},
                    "openapi": {"value": "/openapi", "effective_source": "file", "resolution": "resolved", "source_key": "service.openapi"},
                }],
                "data_sources": [], "middleware": [], "controls": [],
            }

            errors = GUARD._configuration_errors(
                Path("workspace.yaml"), configuration, {"repo:app"}, {"repo:app"},
                {"repo": source}, {"repo": commit},
            )
            self.assertTrue(any("configuration-source-location-evidence" in error for error in errors))
            self.assertTrue(any("configuration-effective-value" in error for error in errors))

    def test_configuration_inventory_covers_confirmed_source_capabilities(self) -> None:
        """源码已确认的外部能力必须进入对应的运行配置清单。"""

        configuration = {
            "sources": [{
                "id": "environment", "owner": "repo:app", "kind": "environment",
                "location": "environment:APP_VALUE", "profile": None,
                "overrides": [], "evidence": ["repo#APP_VALUE"],
            }],
            "precedence": ["environment"],
            "services": [], "data_sources": [], "middleware": [], "controls": [],
        }
        errors = GUARD._configuration_errors(
            Path("workspace.yaml"), configuration, {"repo:app"}, {"repo:app"}, {"repo": Path(".")},
            required_capabilities={"http_rpc", "database", "messages", "cache", "jobs"},
        )
        omissions = [error for error in errors if "configuration-capability-omission" in error]
        self.assertEqual(5, len(omissions))

    def test_control_plan_must_map_to_its_own_step(self) -> None:
        """控制用途不能借用其他控制步骤中的同名动作。"""

        controls = {
            name: {
                "status": "not_applicable",
                "assessment": "当前场景不需要",
                "evidence": [],
                "planned_use": [],
                **({"safety": None} if name == "database_control" else {}),
                **({"correlation_keys": [], "business_evidence": [], "recovery": []} if name == "observability" else {}),
            }
            for name in GUARD.CONTROL_NAMES
        }
        controls["public_api"].update(status="usable", evidence=["repo#anchor"], planned_use=["same_action"])
        controls["database_read"].update(status="usable", evidence=["repo#anchor"], planned_use=["same_action"])
        controls["decision"] = {"safe_control_path": True, "blockers": []}
        steps = [{"action": "same_action", "control": "database_read", "expect": ["observed"]}]

        errors = GUARD._control_errors(Path("scenario.yaml"), controls, steps, {"repo#anchor"})
        self.assertTrue(any("control-plan-mapping" in error and "public_api" in error for error in errors))

    def test_write_isolation_requires_owned_resource(self) -> None:
        """写场景不能通过空拥有资源列表绕过跨场景隔离。"""

        isolation = {
            "namespace": "scenario",
            "correlation_keys": ["key"],
            "owned_resources": [],
            "mutable_controls": [],
            "serial_lock": None,
        }
        errors = GUARD._isolation_errors(
            Path("scenario.yaml"), isolation, [{"side_effect": "write"}]
        )
        self.assertTrue(any("isolation-write-resource" in error for error in errors))

    def test_rejects_blind_sleep_and_embedded_test_sql(self) -> None:
        """场景测试中的盲等和内嵌 SQL 必须同时失败。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "scenarios" / "场景" / "test_场景.py"
            path.parent.mkdir(parents=True)
            sql = "UP" + "DATE sample SET value = ? WHERE id = ?"
            path.write_text(
                '"""场景模块。"""\nimport time\n\n'
                'def test_case():\n    """执行场景。"""\n'
                f'    statement = {sql!r}\n    time.sleep(1)\n',
                encoding="utf-8",
            )

            errors = GUARD._python_errors(path, root)
            self.assertTrue(any("blind-sleep" in error for error in errors))
            self.assertTrue(any("sql-placement" in error for error in errors))

    def test_rejects_smoke_write_call(self) -> None:
        """只读冒烟模块不得调用常见写操作。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "tests" / "runtime" / "test_probe.py"
            path.parent.mkdir(parents=True)
            path.write_text(
                '"""只读探测模块。"""\n\n'
                'def probe(client):\n    """执行只读探测。"""\n    client.post("target")\n',
                encoding="utf-8",
            )

            errors = GUARD._python_errors(path, root)
            self.assertTrue(any("smoke-side-effect" in error for error in errors))

    def test_smoke_evidence_requires_matching_direct_read_call(self) -> None:
        """导入的未知 probe 不能在执行写操作后伪报 GET 冒烟证据。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "tests" / "runtime" / "test_probe.py"
            path.parent.mkdir(parents=True)
            path.write_text(
                '"""只读探测模块。"""\n\n'
                'def test_probe():\n    """执行冒烟。"""\n'
                '    result = probe()\n'
                '    record_endpoint("场景", phase="smoke", method="GET", target_ref="service.health", '
                'status=result.status, summary=result.body)\n'
                '\ntest_probe.pytestmark = read_only_smoke\n',
                encoding="utf-8",
            )

            errors = GUARD._python_errors(path, root)
            self.assertTrue(any("smoke-call-unproven" in error for error in errors))
            self.assertTrue(any("smoke-evidence-causal" in error for error in errors))

    def test_scenario_helper_cannot_emit_runtime_evidence(self) -> None:
        """场景步骤辅助模块也不能绕过适配器直接写控制证据。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "scenarios" / "场景" / "步骤.py"
            path.parent.mkdir(parents=True)
            path.write_text(
                '"""场景步骤模块。"""\n\n'
                'def action(key):\n    """伪造控制证据。"""\n'
                '    record_control("场景", kind="messages", action="publish", correlation_ref=key, side_effect="write")\n',
                encoding="utf-8",
            )

            errors = GUARD._python_errors(path, root)
            self.assertTrue(any("evidence-adapter-owned" in error for error in errors))

    def test_adapter_evidence_is_bound_to_operation_and_result(self) -> None:
        """适配器证据必须复用真实写操作的关联值和返回结果。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "common" / "clients" / "api.py"
            path.parent.mkdir(parents=True)
            path.write_text(
                '"""公共接口适配器。"""\n\n'
                'def create(client, key):\n    """执行写操作并记录证据。"""\n'
                '    response = client.post("target", json={"other": "value"})\n'
                '    record_endpoint("场景", phase="business", method="POST", target_ref="service.create", '
                'status=200, summary={"ok": True})\n'
                '    record_control("场景", kind="public_api", action="create", correlation_ref=key, side_effect="write")\n'
                '    return response\n',
                encoding="utf-8",
            )

            errors = GUARD._python_errors(path, root)
            self.assertTrue(any("endpoint-evidence-result" in error for error in errors))
            self.assertTrue(any("control-evidence-causal" in error for error in errors))
            self.assertTrue(any("mutation-isolation-binding" in error for error in errors))

    def test_runtime_control_correlation_must_be_declared(self) -> None:
        """即使事件结构合法，未声明的外部资源关联值也不能验收。"""

        definition = {
            "isolation": {
                "correlation_keys": ["owned-key"],
                "owned_resources": [{"identity": "owned-row"}],
                "mutable_controls": ["owned-switch"],
            }
        }
        events = [{
            "kind": "control",
            "details": {"control_kind": "messages", "action": "publish", "correlation_ref": "shared-key"},
        }]
        self.assertEqual({"shared-key"}, GUARD._unexpected_control_correlations(definition, events))

    def test_rejects_control_sql_outside_registered_callback(self) -> None:
        """控制 SQL 不能与授权上下文并列出现后谎称已受保护。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "common" / "controls" / "state.py"
            path.parent.mkdir(parents=True)
            mutation = "UP" + "DATE sample SET value = ? WHERE id = ?"
            path.write_text(
                '"""数据库控制模块。"""\n\n'
                'def unsafe(cursor, selector_ref):\n    """在保护器外执行变更。"""\n'
                f'    cursor.execute({mutation!r}, (1, selector_ref))\n\n'
                'def pretend(context):\n    """只构造保护器但不包围变更。"""\n'
                '    controlled_database_state("场景", scenario_context=context, snapshot=snapshot, mutate=mutate, restore=restore, verify_restored=verify)\n',
                encoding="utf-8",
            )

            errors = GUARD._python_errors(path, root)
            self.assertTrue(any("control-sql-guard" in error for error in errors))

    def test_accepts_selector_bound_control_sql_callbacks(self) -> None:
        """契约 selector 绑定且只由保护器调用的变更与恢复 SQL 应可通过。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "common" / "controls" / "state.py"
            path.parent.mkdir(parents=True)
            mutation = "UP" + "DATE sample SET value = ? WHERE id = ?"
            restoration = "UP" + "DATE sample SET value = ? WHERE id = ?"
            path.write_text(
                '"""数据库控制模块。"""\n\n'
                'def snapshot(selector_ref):\n    """按契约选择器保存原值。"""\n'
                '    return repository.load(selector_ref)\n\n'
                'def mutate(selector_ref):\n    """按契约选择器推进状态。"""\n'
                f'    return cursor.execute({mutation!r}, ("next", selector_ref)).rowcount\n\n'
                'def restore(original, selector_ref):\n    """按原始值恢复契约行。"""\n'
                f'    return cursor.execute({restoration!r}, (original, selector_ref)).rowcount\n\n'
                'def verify(original, selector_ref):\n    """按契约选择器核对原值。"""\n'
                '    actual = repository.load(selector_ref)\n    return actual == original\n\n'
                'def apply(context):\n    """只通过数据库保护器调用控制回调。"""\n'
                '    with controlled_database_state("场景", scenario_context=context, snapshot=snapshot, mutate=mutate, restore=restore, verify_restored=verify):\n'
                '        result = trigger_and_observe()\n        assert result == "done"\n',
                encoding="utf-8",
            )

            errors = GUARD._python_errors(path, root)
            self.assertFalse(any("control-sql-guard" in error for error in errors), errors)
            self.assertFalse(any("control-sql-selector-binding" in error for error in errors), errors)

    def test_rejects_selector_bound_to_set_instead_of_where(self) -> None:
        """selector_ref 出现在参数中但未绑定 WHERE 时仍必须失败。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "common" / "controls" / "state.py"
            path.parent.mkdir(parents=True)
            mutation = "UP" + "DATE sample SET state = ? WHERE id = ?"
            path.write_text(
                '"""数据库控制模块。"""\n\n'
                'def snapshot(selector_ref):\n    """保存原值。"""\n    return repository.load(selector_ref)\n\n'
                'def mutate(selector_ref):\n    """错误绑定选择器。"""\n'
                f'    return cursor.execute({mutation!r}, (selector_ref, "shared-row")).rowcount\n\n'
                'def restore(original, selector_ref):\n    """错误恢复选择器。"""\n'
                f'    return cursor.execute({mutation!r}, (selector_ref, "shared-row")).rowcount\n\n'
                'def verify(original, selector_ref):\n    """伪造恢复验证。"""\n'
                '    log(selector_ref)\n    return True\n\n'
                'def apply(context):\n    """注册控制回调。"""\n'
                '    return controlled_database_state("场景", scenario_context=context, snapshot=snapshot, '
                'mutate=mutate, restore=restore, verify_restored=verify)\n',
                encoding="utf-8",
            )

            errors = GUARD._python_errors(path, root)
            self.assertTrue(any("control-sql-selector-binding" in error for error in errors))
            self.assertTrue(any("control-sql-observer-binding" in error for error in errors))

    def test_rejects_direct_evidence_forgery_and_constant_assertion(self) -> None:
        """测试入口不能直接伪造控制证据或以常量断言冒充业务验证。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "scenarios" / "场景" / "test_场景.py"
            path.parent.mkdir(parents=True)
            path.write_text(
                '"""场景模块。"""\nimport os\nimport pytest\n\n'
                '@pytest.mark.business_e2e\ndef test_case():\n    """伪造业务成功。"""\n'
                '    preflight()\n    record_business_entry()\n    record_control(kind="observability", action="observe", correlation_ref="key")\n'
                '    os.environ["E2E_RUN_ID"]\n    len([])\n    assert 1\n    try:\n        pass\n    finally:\n        print("done")\n',
                encoding="utf-8",
            )

            errors = GUARD._python_errors(path, root)
            self.assertTrue(any("evidence-adapter-owned" in error for error in errors))
            self.assertTrue(any("evidence-environment-internal" in error for error in errors))
            self.assertTrue(any("business-action-required" in error for error in errors))
            self.assertTrue(any("business-assertion-required" in error for error in errors))
            self.assertTrue(any("cleanup-guaranteed" in error for error in errors))

    def test_detects_cross_scenario_correlation_collision(self) -> None:
        """无共同串行锁的关联键冲突必须阻塞多场景执行。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scenarios = []
            for name, owner in (("甲", "agent-a"), ("乙", "agent-b")):
                directory = root / name
                definition = {
                    "generation": {"mode": "delegated", "owner": owner},
                    "isolation": {
                        "namespace": name,
                        "correlation_keys": ["shared-key"],
                        "owned_resources": [],
                        "mutable_controls": [],
                        "serial_lock": None,
                    },
                }
                scenarios.append((directory, definition))

            errors = GUARD._cross_scenario_errors(scenarios)
            self.assertTrue(any("isolation-correlation-collision" in error for error in errors))

    def test_gate_seal_detects_changed_input(self) -> None:
        """前置门禁输入变化后摘要必须失效。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.yaml"
            source.write_text("value: 1\n", encoding="utf-8")
            GUARD._start_gate_session(root)
            GUARD._write_seal(root, "discovery", [source])
            self.assertEqual([], GUARD._seal_errors(root, "discovery", [source]))

            source.write_text("value: 2\n", encoding="utf-8")
            self.assertTrue(any("gate-order" in error for error in GUARD._seal_errors(root, "discovery", [source])))

    def test_pending_status_rejects_authorization_blocker(self) -> None:
        """每次运行授权缺失不得伪装成 pending_environment。"""

        readiness = {
            "source_contract": "confirmed",
            "safe_control": "confirmed",
            "runtime_configuration": "missing",
            "test_data": "confirmed",
            "blockers": ["authorization:approval"],
        }
        decision = {"safe_control_path": True, "blockers": []}
        self.assertIsNone(GUARD._derived_status(readiness, decision))
        readiness["blockers"] = ["config:services.entry.base_url"]
        self.assertEqual("pending_environment", GUARD._derived_status(readiness, decision))

        with patch.dict(GUARD.os.environ, {}, clear=True):
            self.assertEqual(
                {"connection:SERVICE_URL", "credential:SERVICE_TOKEN", "config:FEATURE_FLAG"},
                GUARD._missing_placeholder_blockers({
                    "base_url": "${SERVICE_URL}",
                    "auth_token": "${SERVICE_TOKEN}",
                    "feature": "${FEATURE_FLAG}",
                }, "config"),
            )

    def test_contract_blocked_requires_every_candidate_control_to_be_closed(self) -> None:
        """缺少 HTTP 不能单独阻塞契约，全部候选控制都必须有不可用证据。"""

        controls = {
            name: {
                "status": "not_found",
                "assessment": "源码确认不可用",
                "evidence": ["repo#Anchor"],
                "planned_use": [],
                **({"safety": None} if name == "database_control" else {}),
                **({"correlation_keys": [], "business_evidence": [], "recovery": []} if name == "observability" else {}),
            }
            for name in GUARD.CONTROL_NAMES
        }
        controls["public_api"].update(status="not_applicable", evidence=[])
        controls["database_read"].update(status="usable", planned_use=["读取证据"])
        controls["observability"].update(
            status="usable", correlation_keys=["case-key"], business_evidence=["状态记录"], recovery=["无写入"]
        )
        controls["decision"] = {"safe_control_path": False, "blockers": ["control:public_api"]}
        definition = {
            "meta": {"id": "BLOCKED_CASE", "name": "阻塞场景", "status": "contract_blocked", "actor": "调用方"},
            "generation": {"mode": "main_agent", "owner": "main-agent", "write_scope": "scenarios/阻塞场景", "degradation_reason": None},
            "readiness": {
                "source_contract": "blocked", "safe_control": "blocked",
                "runtime_configuration": "confirmed", "test_data": "confirmed",
                "blockers": ["control:public_api"],
            },
            "preconditions": ["源码已固定"],
            "integrations": {"services": [], "components": []},
            "controls": controls,
            "isolation": {
                "namespace": "blocked-case", "correlation_keys": ["case-key"],
                "owned_resources": [], "mutable_controls": [], "serial_lock": None,
            },
            "steps": [{
                "id": "read", "action": "读取证据", "control": "database_read",
                "side_effect": "read", "expect": ["状态记录"],
            }],
            "cleanup": {"strategy": "只读无需恢复", "actions": ["确认无写入"], "verifies": ["状态未改变"]},
            "source": [{"repo": "repo", "commit": "0" * 40, "anchors": ["Anchor"]}],
        }
        discovery = {"inventory": {"repositories": []}, "configuration": {}}

        errors = GUARD._scenario_errors(Path("."), Path("scenarios/阻塞场景"), definition, discovery)
        self.assertTrue(any("contract-control-blocker" in error for error in errors))
        self.assertTrue(any("contract-safe-control" in error for error in errors))

    def test_scenario_source_covers_every_integration_owner_repository(self) -> None:
        """跨仓库场景必须为每个被用集成的所有者仓库记录源码基线。"""

        definition = {
            "integrations": {
                "services": ["entry"],
                "components": [{"id": "broker", "type": "message", "required": True}],
            },
            "source": [{"repo": "entry-repo", "commit": "0" * 40, "anchors": ["EntryAnchor"]}],
        }
        discovery = {
            "inventory": {"repositories": [
                {"id": "entry-repo", "root": ".", "commit": "0" * 40},
                {"id": "broker-repo", "root": ".", "commit": "0" * 40},
            ]},
            "configuration": {
                "services": [{"id": "entry", "owner": "entry-repo:app"}],
                "data_sources": [],
                "middleware": [{"id": "broker", "owner": "broker-repo:starter", "type": "message"}],
                "controls": [],
            },
        }

        errors = GUARD._scenario_errors(Path("."), Path("scenarios/跨仓库"), definition, discovery)
        self.assertTrue(any("source-owner-coverage" in error and "broker-repo" in error for error in errors))

    def test_pending_blocker_must_match_actual_missing_placeholder(self) -> None:
        """pending blocker 不能用不存在的路径替代真实缺失环境值。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scenario = root / "scenarios" / "场景"
            environment = root / "config" / "environments"
            scenario.mkdir(parents=True)
            environment.mkdir(parents=True)
            (root / "config" / "config.yaml").write_text(
                "active_environment: test\ndefaults: {}\n", encoding="utf-8"
            )
            (environment / "test.yaml").write_text(
                "services:\n  service:\n    base_url: ${E2E_MISSING_BINDING_TEST}\ncomponents: {}\n",
                encoding="utf-8",
            )
            (scenario / "业务数据.json").write_text('{"test": {}}', encoding="utf-8")
            definition = {
                "meta": {"id": "SCENARIO", "status": "pending_environment"},
                "readiness": {
                    "runtime_configuration": "missing",
                    "test_data": "confirmed",
                    "blockers": ["config:NOT_THE_REAL_VALUE"],
                },
                "integrations": {"services": ["service"], "components": []},
                "controls": {"database_control": {"status": "not_applicable"}},
                "steps": [],
            }

            errors = GUARD._scenario_artifact_errors(root, scenario, definition)
            self.assertTrue(any("pending-blocker-binding" in error for error in errors))

    def test_business_data_reference_cannot_be_all_environment_placeholders(self) -> None:
        """可按源码构造的业务数据不能全部转嫁给环境变量。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scenario = root / "scenarios" / "场景"
            environment = root / "config" / "environments"
            scenario.mkdir(parents=True)
            environment.mkdir(parents=True)
            (root / "config" / "config.yaml").write_text(
                "active_environment: test\ndefaults: {}\n", encoding="utf-8"
            )
            (environment / "test.yaml").write_text(
                "services: {}\ncomponents: {}\n", encoding="utf-8"
            )
            data_path = scenario / "业务数据.json"
            data_path.write_text('{"test":{"request":{"external":"${EXTERNAL_ID}"}}}', encoding="utf-8")
            definition = {
                "meta": {"id": "SCENARIO", "status": "pending_environment"},
                "readiness": {
                    "runtime_configuration": "confirmed",
                    "test_data": "missing",
                    "blockers": ["business_data:EXTERNAL_ID"],
                },
                "integrations": {"services": [], "components": []},
                "controls": {"database_control": {"status": "not_applicable"}},
                "steps": [{"data_ref": "业务数据.json#/request"}],
            }

            errors = GUARD._scenario_artifact_errors(root, scenario, definition)
            self.assertTrue(any("business-data-overinjected" in error for error in errors))

            data_path.write_text(
                '{"test":{"request":{"external":"${EXTERNAL_ID}","label":"source-valid"}}}',
                encoding="utf-8",
            )
            errors = GUARD._scenario_artifact_errors(root, scenario, definition)
            self.assertFalse(any("business-data-overinjected" in error for error in errors))

    def test_all_generated_diagrams_use_business_or_system_flow_granularity(self) -> None:
        """非场景主图也不得使用代码级或非流程型图表。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            diagrams = root / "scenarios" / "场景"
            diagrams.mkdir(parents=True)
            (diagrams / "业务流程图.md").write_text(
                "```mermaid\nsequenceDiagram\nA->>B: 业务动作\n```\n", encoding="utf-8"
            )
            interaction = diagrams / "服务交互图.md"
            interaction.write_text(
                "```mermaid\nclassDiagram\nclass InternalHandler\n```\n", encoding="utf-8"
            )
            detail = diagrams / "自动化测试流程图.md"
            detail.write_text(
                "```mermaid\nflowchart LR\nA[pytest fixture] --> B[业务系统]\n```\n", encoding="utf-8"
            )

            errors = GUARD._diagram_errors(root)
            self.assertTrue(any("diagram-type" in error and str(interaction) in error for error in errors))
            self.assertTrue(any("diagram-scope" in error and str(detail) in error for error in errors))

            interaction.write_text(
                "```mermaid\nflowchart LR\nA[入口服务] --> B[下游服务]\n```\n", encoding="utf-8"
            )
            detail.unlink()
            errors = GUARD._diagram_errors(root)
            self.assertFalse(any("diagram-type" in error or "diagram-scope" in error for error in errors), errors)

    def test_unified_launchers_replace_scenario_launchers(self) -> None:
        """运行入口必须统一，并在脚本注释中说明场景参数。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            (scripts / "run.sh").write_bytes((PROJECT_ROOT / "scripts" / "run.sh").read_bytes())
            (scripts / "run.bat").write_bytes((PROJECT_ROOT / "scripts" / "run.bat").read_bytes())
            if os.name != "nt":
                (scripts / "run.sh").chmod(0o755)
            self.assertEqual([], GUARD._launcher_errors(root))

            (scripts / "run_场景.sh").write_text("#!/usr/bin/env sh\n", encoding="utf-8")
            self.assertTrue(any("scenario-launcher-forbidden" in error for error in GUARD._launcher_errors(root)))

    def test_legacy_runtime_config_name_is_rejected(self) -> None:
        """旧 runtime.yaml 不得与统一后的 config.yaml 并存或继续生效。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "config" / "runtime.yaml"
            legacy.parent.mkdir()
            legacy.write_text("active_environment: test\n", encoding="utf-8")

            errors = GUARD.static_errors(root)
            self.assertTrue(any("legacy-config-name" in error for error in errors))

    def test_runner_defaults_to_all_and_accepts_scenario_name(self) -> None:
        """统一运行器省略场景时运行全部，显式参数原样选择单场景。"""

        with patch.object(GUARD, "run_ordered", return_value=0) as run:
            self.assertEqual(0, GUARD.main_runner([]))
            self.assertIsNone(run.call_args.args[1])

            self.assertEqual(0, GUARD.main_runner(["--scenario", "场景"]))
            self.assertEqual("场景", run.call_args.args[1])

    def test_static_only_runner_writes_ordered_na_report(self) -> None:
        """静态运行应保持固定顺序并把运行阶段明确标为 N/A。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch.object(GUARD, "_run", return_value=0) as run,
                patch.object(GUARD, "source_version_results", return_value=([], [])),
                patch.object(GUARD, "contract_errors", return_value=([], [], {})),
            ):
                result = GUARD.run_ordered(root, None, [], static_only=True)

            self.assertEqual(0, result)
            self.assertEqual(12, run.call_count)
            ordered_gates = [
                call.args[0][call.args[0].index("--gate") + 1]
                for call in run.call_args_list[:8]
            ]
            self.assertEqual([
                "workspace_inventory", "dependency_topology", "initial_configuration", "runtime_probe",
                "control_matrix", "scenario_split", "scenario_ownership", "shared_integration",
            ], ordered_gates)
            report = GUARD._load_json(root / "artifacts" / "e2e-run.json", [])
            self.assertEqual("passed", report["stages"]["collect"]["status"])
            self.assertEqual("N/A", report["stages"]["read_only_smoke"]["status"])
            self.assertEqual("N/A", report["stages"]["business"]["status"])
            self.assertEqual("N/A", report["stages"]["restoration"]["status"])

    def test_runner_stops_after_first_failed_gate(self) -> None:
        """早期门禁失败后不得启动任何后续命令。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch.object(GUARD, "_run", return_value=7) as run,
                patch.object(GUARD, "contract_errors", return_value=([], [], {})),
            ):
                result = GUARD.run_ordered(root, None, [], static_only=False)

            self.assertEqual(7, result)
            self.assertEqual(1, run.call_count)
            report = GUARD._load_json(root / "artifacts" / "e2e-run.json", [])
            self.assertEqual("failed", report["stages"]["workspace_inventory"]["status"])
            self.assertEqual("N/A", report["stages"]["control_matrix"]["status"])

    def test_runner_attributes_second_discovery_stage_failure(self) -> None:
        """拓扑失败必须归入拓扑阶段，不能误报为工作区清单失败。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch.object(GUARD, "_run", side_effect=[0, 7]),
                patch.object(GUARD, "contract_errors", return_value=([], [], {})),
            ):
                result = GUARD.run_ordered(root, None, [], static_only=False)

            self.assertEqual(7, result)
            report = GUARD._load_json(root / "artifacts" / "e2e-run.json", [])
            self.assertEqual("passed", report["stages"]["workspace_inventory"]["status"])
            self.assertEqual("failed", report["stages"]["dependency_topology"]["status"])
            self.assertEqual("N/A", report["stages"]["initial_configuration"]["status"])

    def test_rejects_pytest_selection_and_collect_arguments(self) -> None:
        """调用方不得把真实业务执行降级成收集或子集选择。"""

        errors = GUARD._pytest_arg_errors(["--collect-only", "-k", "subset", "-vv"])
        self.assertEqual(["--collect-only", "-k", "subset"], errors)

    def test_project_rejects_pytest_plugin_and_collection_configuration(self) -> None:
        """项目配置不得显式加载插件或改变测试收集范围。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pyproject.toml").write_text(
                '[project]\nname="e2e"\nversion="0"\ndependencies=["pytest", "PyYAML"]\n\n'
                '[tool.pytest.ini_options]\n'
                'markers=["business_e2e: business", "scenario_id(value): id", "read_only_smoke: smoke"]\n'
                'addopts="-p injected_plugin --collect-only"\n'
                'required_plugins=["injected-plugin"]\n',
                encoding="utf-8",
            )
            self.assertTrue(any("pytest-configuration" in item for item in GUARD._project_errors(root)))

    def test_evidence_requires_matching_run_id_and_schema(self) -> None:
        """其他运行或结构不完整的 JSON 不得成为有效证据。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "event.json").write_text(
                json.dumps({
                    "version": 1,
                    "run_id": "other",
                    "kind": "business_entered",
                    "scenario": "场景",
                    "details": {},
                }),
                encoding="utf-8",
            )
            events, errors = GUARD._events(root, "current")
            self.assertEqual([], events)
            self.assertTrue(any("evidence-identity" in error for error in errors))

    def test_report_redacts_key_variants_and_query_credentials(self) -> None:
        """报告必须脱敏驼峰密钥、Cookie 和 URL 查询凭据。"""

        redacted = GUARD._redact_report({
            "accessToken": "value",
            "set-cookie": "value",
            "private_key": "value",
            "auth": {"value": "nested-secret"},
            "connection_source": {"reference": "environment:STORE_DSN", "resolution": "unresolved"},
            "nested": {"url": "https://example.invalid/path?client_secret=value"},
        })
        self.assertEqual("<redacted>", redacted["accessToken"])
        self.assertEqual("<redacted>", redacted["set-cookie"])
        self.assertEqual("<redacted>", redacted["private_key"])
        self.assertEqual("<redacted>", redacted["auth"])
        self.assertEqual("environment:STORE_DSN", redacted["connection_source"]["reference"])
        self.assertEqual("<redacted>", redacted["nested"]["url"])

    def test_selected_contract_still_checks_sibling_collisions(self) -> None:
        """按场景过滤执行时仍必须检查全部兄弟场景隔离。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, owner in (("甲", "agent-a"), ("乙", "agent-b")):
                directory = root / "scenarios" / name
                directory.mkdir(parents=True)
                definition = {
                    "generation": {"mode": "delegated", "owner": owner},
                    "isolation": {
                        "namespace": name,
                        "correlation_keys": ["shared-key"],
                        "owned_resources": [],
                        "mutable_controls": [],
                        "serial_lock": None,
                    },
                }
                (directory / "场景定义.yaml").write_text(
                    GUARD.yaml.safe_dump(definition, allow_unicode=True), encoding="utf-8"
                )
            with (
                patch.object(GUARD, "discovery_errors", return_value=([], {})),
                patch.object(GUARD, "_scenario_errors", return_value=[]),
            ):
                errors, selected, _ = GUARD.contract_errors(root, "甲")
            self.assertEqual(["甲"], [directory.name for directory, _ in selected])
            self.assertTrue(any("isolation-correlation-collision" in error for error in errors))

    def test_completed_probe_requires_full_association_and_smoke(self) -> None:
        """completed 运行探测不得遗漏监听关联或只读接口结果。"""

        probe = {
            "requested": True,
            "outcome": "completed",
            "blockers": [],
            "listeners": [{"id": "listener", "host": "127.0.0.1", "port": 8080, "protocol": "http", "evidence": ["socket"]}],
            "processes": [{"id": "process", "pid": 123, "command_reference": "command", "evidence": ["pid"]}],
            "associations": [],
            "read_only_smoke": [],
        }
        errors = GUARD._runtime_probe_errors(Path("workspace.yaml"), probe, {"repo:app"})
        self.assertTrue(any("runtime-probe-complete" in error for error in errors))

    def test_ordered_runtime_probe_rechecks_live_process_listener_and_http(self) -> None:
        """completed 探测必须在有序门禁中重新执行只读系统与 HTTP 核验。"""

        class Connection:
            """提供可关闭的合成 TCP 连接。"""

            def close(self) -> None:
                """关闭合成连接。"""

        class Response:
            """提供合成 HTTP 响应。"""

            status = 204

            def read(self, amount: int) -> bytes:
                """返回有界响应体。"""

                return b""

        class HttpConnection:
            """记录合成本地 HTTP 调用。"""

            def request(self, method: str, target: str) -> None:
                """接受安全方法和目标。"""

            def getresponse(self) -> Response:
                """返回合成响应。"""

                return Response()

            def close(self) -> None:
                """关闭合成 HTTP 连接。"""

        probe = {
            "requested": True,
            "outcome": "completed",
            "blockers": [],
            "processes": [{"id": "process", "pid": 123, "command_reference": "service-ref", "evidence": ["process-list"]}],
            "listeners": [{"id": "listener", "host": "127.0.0.1", "port": 8080, "protocol": "http", "evidence": ["socket"]}],
            "associations": [{"process": "process", "listener": "listener", "node": "repo:app"}],
            "read_only_smoke": [{
                "node": "repo:app", "method": "HEAD", "target_ref": "http://127.0.0.1:8080/health", "result": "status:204",
            }],
        }
        with (
            patch.object(GUARD, "_observed_process_command", return_value="python service-ref"),
            patch.object(GUARD, "_listener_owner_pids", return_value={123}),
            patch.object(GUARD.socket, "create_connection", return_value=Connection()),
            patch.object(GUARD.http.client, "HTTPConnection", return_value=HttpConnection()),
        ):
            self.assertEqual([], GUARD._runtime_probe_live_errors(Path("workspace.yaml"), probe))
            probe["read_only_smoke"][0]["result"] = "status:200"
            errors = GUARD._runtime_probe_live_errors(Path("workspace.yaml"), probe)
        self.assertTrue(any("runtime-smoke-live-result" in error for error in errors))

    def test_business_ast_rejects_trivial_success_and_empty_cleanup(self) -> None:
        """assert True 与 finally pass 不能冒充真实业务和恢复。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "scenarios" / "场景" / "test_场景.py"
            path.parent.mkdir(parents=True)
            path.write_text(
                '"""场景模块。"""\nimport pytest\n\n'
                '@pytest.mark.business_e2e\ndef test_case():\n    """执行场景。"""\n'
                '    preflight()\n    record_business_entry()\n    action()\n'
                '    try:\n        assert True\n    finally:\n        pass\n',
                encoding="utf-8",
            )
            errors = GUARD._python_errors(path, root)
            self.assertTrue(any("business-assertion-required" in error for error in errors))
            self.assertTrue(any("cleanup-guaranteed" in error for error in errors))

    def test_runner_requires_runtime_evidence_and_reports_per_scenario(self) -> None:
        """业务成功必须有进入与控制证据，并写出逐场景结论。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scenario = root / "scenarios" / "场景"
            common = root / "common"
            scenario.mkdir(parents=True)
            common.mkdir()
            (scenario / "test_场景.py").write_text("", encoding="utf-8")
            (common / "e2e_runtime.py").write_text(
                '"""运行桩。"""\n\ndef preflight(project_root, scenario_name, environ=None):\n'
                '    """返回合成预检结果。"""\n    return {}\n',
                encoding="utf-8",
            )
            definition = {
                "meta": {"status": "ready"},
                "generation": {
                    "owner": "main-agent", "mode": "sequential_degraded",
                    "degradation_reason": "运行环境不支持子代理",
                },
                "controls": {"observability": {"planned_use": ["observe"]}},
                "steps": [{"control": "observability", "action": "observe", "side_effect": "read"}],
                "isolation": {"correlation_keys": ["selector"], "owned_resources": [], "mutable_controls": []},
            }
            contract_result = ([], [(scenario, definition)], {"runtime_probe": {"requested": False}})
            calls = {"count": 0}

            def run(command: list[str], project_root: Path, environment: dict[str, str]) -> int:
                """在业务子进程位置写入本次运行的合法证据。"""

                calls["count"] += 1
                if calls["count"] == 13:
                    evidence = Path(environment["E2E_EVIDENCE_DIR"])
                    evidence.mkdir(parents=True, exist_ok=True)
                    base = {
                        "version": 1,
                        "run_id": environment["E2E_RUN_ID"],
                        "scenario": "场景",
                    }
                    (evidence / "entry.json").write_text(
                        json.dumps({**base, "kind": "business_entered", "details": {}}), encoding="utf-8"
                    )
                    (evidence / "control.json").write_text(
                        json.dumps({
                            **base,
                            "kind": "control",
                            "details": {
                                "control_kind": "observability", "action": "observe",
                                "correlation_ref": "selector", "side_effect": "read",
                            },
                        }),
                        encoding="utf-8",
                    )
                    junit = next(Path(item.split("=", 1)[1]) for item in command if item.startswith("--junitxml="))
                    junit.write_text('<testsuite tests="1" failures="0" errors="0" skipped="0"/>', encoding="utf-8")
                return 0

            with (
                patch.object(GUARD, "_run", side_effect=run),
                patch.object(GUARD, "source_version_results", return_value=([], [])),
                patch.object(GUARD, "contract_errors", return_value=contract_result),
            ):
                result = GUARD.run_ordered(root, None, [], static_only=False)

            self.assertEqual(0, result)
            report = GUARD._load_json(root / "artifacts" / "e2e-run.json", [])
            self.assertEqual("passed", report["scenarios"][0]["business"]["status"])
            self.assertEqual("运行环境不支持子代理", report["scenarios"][0]["degradation_reason"])
            self.assertEqual("N/A", report["stages"]["restoration"]["status"])

    def test_runner_does_not_reuse_evidence_created_before_a_scenario(self) -> None:
        """前置阶段预写的后序场景事件不能替代该场景子进程的新证据。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            common = root / "common"
            common.mkdir()
            (common / "e2e_runtime.py").write_text(
                '"""运行桩。"""\n\ndef preflight(project_root, scenario_name, environ=None):\n'
                '    """返回合成预检结果。"""\n    return {}\n',
                encoding="utf-8",
            )
            scenarios = []
            for name in ("前序", "后序"):
                directory = root / "scenarios" / name
                directory.mkdir(parents=True)
                (directory / f"test_{name}.py").write_text("", encoding="utf-8")
                scenarios.append((directory, {
                    "meta": {"status": "ready"},
                    "generation": {"owner": name, "mode": "delegated", "degradation_reason": None},
                    "controls": {},
                    "steps": [],
                    "isolation": {"correlation_keys": [], "owned_resources": [], "mutable_controls": []},
                }))
            contract_result = ([], scenarios, {"runtime_probe": {"requested": False}})
            calls = {"count": 0}
            run_id = "fixed-run-id"
            evidence = root / "artifacts" / "evidence" / run_id
            evidence.mkdir(parents=True)
            (evidence / "preexisting.json").write_text(json.dumps({
                "version": 1,
                "run_id": run_id,
                "kind": "business_entered",
                "scenario": "后序",
                "details": {},
            }), encoding="utf-8")

            def run(command: list[str], project_root: Path, environment: dict[str, str]) -> int:
                """只为前序业务子进程新增本次事件。"""

                calls["count"] += 1
                if calls["count"] < 13:
                    self.assertNotIn("E2E_EVIDENCE_DIR", environment)
                    self.assertNotIn("E2E_RUN_ID", environment)
                if calls["count"] == 13:
                    event_root = Path(environment["E2E_EVIDENCE_DIR"])
                    event_root.mkdir(parents=True, exist_ok=True)
                    (evidence / f"{calls['count']}.json").write_text(json.dumps({
                        "version": 1,
                        "run_id": environment["E2E_RUN_ID"],
                        "kind": "business_entered",
                        "scenario": "前序",
                        "details": {},
                    }), encoding="utf-8")
                    junit = next(Path(item.split("=", 1)[1]) for item in command if item.startswith("--junitxml="))
                    junit.write_text('<testsuite tests="1" failures="0" errors="0" skipped="0"/>', encoding="utf-8")
                return 0

            with (
                patch.object(GUARD.uuid, "uuid4") as uuid4,
                patch.object(GUARD, "_run", side_effect=run),
                patch.object(GUARD, "source_version_results", return_value=([], [])),
                patch.object(GUARD, "contract_errors", return_value=contract_result),
            ):
                uuid4.return_value.hex = run_id
                result = GUARD.run_ordered(root, None, [], static_only=False)

            self.assertEqual(1, result)
            report = GUARD._load_json(root / "artifacts" / "e2e-run.json", [])
            self.assertEqual("passed", report["scenarios"][0]["business"]["status"])
            self.assertEqual("failed", report["scenarios"][1]["business"]["status"])

    def test_runner_rejects_restoration_for_wrong_resource(self) -> None:
        """恢复事件必须精确覆盖写场景声明的拥有资源。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scenario = root / "scenarios" / "场景"
            common = root / "common"
            scenario.mkdir(parents=True)
            common.mkdir()
            (scenario / "test_场景.py").write_text("", encoding="utf-8")
            (common / "e2e_runtime.py").write_text(
                '"""运行桩。"""\n\ndef preflight(project_root, scenario_name, environ=None):\n'
                '    """返回合成预检结果。"""\n    return {}\n',
                encoding="utf-8",
            )
            definition = {
                "meta": {"status": "ready"},
                "generation": {"owner": "agent", "mode": "main_agent"},
                "controls": {},
                "steps": [{"side_effect": "write"}],
                "isolation": {"owned_resources": [{"identity": "owned-key"}]},
            }
            contract_result = ([], [(scenario, definition)], {"runtime_probe": {"requested": False}})
            calls = {"count": 0}

            def run(command: list[str], project_root: Path, environment: dict[str, str]) -> int:
                """在业务子进程位置写入错误资源的恢复证据。"""

                calls["count"] += 1
                if calls["count"] == 13:
                    evidence = Path(environment["E2E_EVIDENCE_DIR"])
                    evidence.mkdir(parents=True, exist_ok=True)
                    base = {"version": 1, "run_id": environment["E2E_RUN_ID"], "scenario": "场景"}
                    (evidence / "entry.json").write_text(
                        json.dumps({**base, "kind": "business_entered", "details": {}}), encoding="utf-8"
                    )
                    (evidence / "restore.json").write_text(
                        json.dumps({
                            **base,
                            "kind": "restoration",
                            "details": {"status": "passed", "resources": ["other-key"]},
                        }),
                        encoding="utf-8",
                    )
                    junit = next(Path(item.split("=", 1)[1]) for item in command if item.startswith("--junitxml="))
                    junit.write_text('<testsuite tests="1" failures="0" errors="0" skipped="0"/>', encoding="utf-8")
                return 0

            with (
                patch.object(GUARD, "_run", side_effect=run),
                patch.object(GUARD, "source_version_results", return_value=([], [])),
                patch.object(GUARD, "contract_errors", return_value=contract_result),
            ):
                result = GUARD.run_ordered(root, None, [], static_only=False)

            self.assertEqual(1, result)
            report = GUARD._load_json(root / "artifacts" / "e2e-run.json", [])
            self.assertEqual("failed", report["stages"]["restoration"]["status"])

    def test_runner_rejects_mixed_ready_and_non_ready_scope(self) -> None:
        """默认全场景运行不得执行 ready 子集并静默忽略其他场景。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ready = root / "scenarios" / "就绪"
            pending = root / "scenarios" / "待环境"
            ready.mkdir(parents=True)
            pending.mkdir(parents=True)
            scenarios = [
                (ready, {"meta": {"status": "ready"}, "generation": {}, "controls": {}, "steps": []}),
                (pending, {"meta": {"status": "pending_environment"}, "generation": {}, "controls": {}, "steps": []}),
            ]
            contract_result = ([], scenarios, {"runtime_probe": {"requested": False}})
            with (
                patch.object(GUARD, "_run", return_value=0) as run,
                patch.object(GUARD, "source_version_results", return_value=([], [])),
                patch.object(GUARD, "contract_errors", return_value=contract_result),
            ):
                result = GUARD.run_ordered(root, None, [], static_only=False)

            self.assertEqual(2, result)
            self.assertEqual(12, run.call_count)
            report = GUARD._load_json(root / "artifacts" / "e2e-run.json", [])
            self.assertEqual("N/A", report["stages"]["business"]["status"])
            self.assertEqual("failed", report["stages"]["summary"]["status"])

    def test_runner_keeps_restoration_na_when_preflight_fails(self) -> None:
        """写场景未进入业务步骤时不应伪报恢复失败。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scenario = root / "scenarios" / "场景"
            common = root / "common"
            scenario.mkdir(parents=True)
            common.mkdir()
            (scenario / "test_场景.py").write_text("", encoding="utf-8")
            (common / "e2e_runtime.py").write_text(
                '"""运行桩。"""\n\ndef preflight(project_root, scenario_name, environ=None):\n'
                '    """拒绝合成预检。"""\n    raise RuntimeError("preflight")\n',
                encoding="utf-8",
            )
            definition = {
                "meta": {"status": "ready"},
                "generation": {"owner": "agent", "mode": "main_agent"},
                "controls": {},
                "steps": [{"side_effect": "write"}],
                "isolation": {"owned_resources": [{"identity": "owned-key"}]},
            }
            contract_result = ([], [(scenario, definition)], {"runtime_probe": {"requested": False}})
            with (
                patch.object(GUARD, "_run", return_value=0),
                patch.object(GUARD, "source_version_results", return_value=([], [])),
                patch.object(GUARD, "contract_errors", return_value=contract_result),
            ):
                result = GUARD.run_ordered(root, None, [], static_only=False)

            self.assertEqual(1, result)
            report = GUARD._load_json(root / "artifacts" / "e2e-run.json", [])
            self.assertEqual("failed", report["stages"]["business"]["status"])
            self.assertEqual("N/A", report["stages"]["restoration"]["status"])

    def test_aggregate_gates_do_not_write_ordering_seals(self) -> None:
        """聚合诊断模式不得授权后续 static 阶段。"""

        with (
            patch.object(GUARD, "discovery_errors", return_value=([], {})),
            patch.object(GUARD, "contract_errors", return_value=([], [], {})),
            patch.object(GUARD, "_write_seal") as write_seal,
        ):
            self.assertEqual(0, GUARD.main_checker(["--gate", "discovery"]))
            self.assertEqual(0, GUARD.main_checker(["--gate", "contracts"]))
        write_seal.assert_not_called()

    def test_root_conftest_is_scanned_and_cannot_forge_evidence(self) -> None:
        """根 conftest 也必须受证据环境变量门禁约束。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "conftest.py"
            path.write_text(
                '"""测试配置。"""\nimport os\npytest_plugins = ["injected_plugin"]\n\ndef forge():\n'
                '    """读取内部证据键。"""\n    return os.environ["E2E_RUN_ID"]\n',
                encoding="utf-8",
            )
            self.assertIn(path, GUARD._python_paths(root))
            errors = GUARD._python_errors(path, root)
            self.assertTrue(any("evidence-environment-internal" in item for item in errors))
            self.assertTrue(any("pytest-hook-forbidden" in item for item in errors))

    def test_scenario_discovery_ignores_cache_directories(self) -> None:
        """解释器缓存目录不得被识别成业务场景。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scenarios" / "场景").mkdir(parents=True)
            (root / "scenarios" / "__pycache__").mkdir()
            errors: list[str] = []
            self.assertEqual([root / "scenarios" / "场景"], GUARD._scenario_directories(root, None, errors))
            self.assertEqual([], errors)

    def test_smoke_requires_known_transport_timeout_and_result_verification(self) -> None:
        """任意 fixture、写式 urlopen 和常量 verified 都不能伪造冒烟。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "tests" / "runtime" / "test_probe.py"
            path.parent.mkdir(parents=True)
            path.write_text(
                '"""冒烟模块。"""\n\ndef test_probe(client, payload):\n    """执行反例。"""\n'
                '    client.get("target")\n'
                '    response = urllib.request.urlopen("target", data=payload, timeout=1)\n'
                '    verified = response.status == response.status\n'
                '    record_endpoint("场景", phase="smoke", method="GET", target_ref="service.health", '
                'status=response.status, summary=response.body, verified=verified)\n'
                '\ntest_probe.pytestmark = read_only_smoke\n',
                encoding="utf-8",
            )
            errors = GUARD._python_errors(path, root)
            self.assertTrue(any("smoke-transport-unproven" in item for item in errors))
            self.assertTrue(any("endpoint-evidence-result" in item for item in errors))

    def test_read_only_rpc_contract_accepts_one_bounded_returned_call(self) -> None:
        """只读 RPC 适配器必须绑定源码、正数超时和真实返回结果。"""

        valid_tree = ast.parse(
            '@read_only_rpc("READ", source_ref="repo#LookupAnchor")\n'
            'def lookup(client):\n    result = client.query(timeout=2)\n    return result\n'
        )
        invalid_tree = ast.parse(
            '@read_only_rpc("READ", source_ref="repo#LookupAnchor")\n'
            'def lookup(client):\n    client.query(timeout=0)\n    return True\n'
        )
        valid = next(item for item in valid_tree.body if isinstance(item, ast.FunctionDef))
        invalid = next(item for item in invalid_tree.body if isinstance(item, ast.FunctionDef))
        with patch.object(GUARD, "_source_reference_exists", return_value=True):
            self.assertEqual("READ", GUARD._rpc_function_method(valid, Path(".")))
            self.assertIsNone(GUARD._rpc_function_method(invalid, Path(".")))

    def test_urlopen_accepts_standard_positional_timeout(self) -> None:
        """标准库 urlopen 的第三个位置参数应被识别为有界超时。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "common" / "clients" / "http.py"
            path.parent.mkdir(parents=True)
            path.write_text(
                '"""只读 HTTP 适配器。"""\nimport urllib.request\n\ndef fetch(url):\n'
                '    """使用位置参数超时读取。"""\n    return urllib.request.urlopen(url, None, 2)\n',
                encoding="utf-8",
            )
            errors = GUARD._python_errors(path, root)
            self.assertFalse(any("transport-timeout" in item for item in errors))

    def test_control_kind_must_match_real_operation_and_basic_block(self) -> None:
        """消息证据不能借用读取调用或死分支发布。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "common" / "integrations" / "message.py"
            path.parent.mkdir(parents=True)
            path.write_text(
                '"""消息适配器。"""\n\ndef publish(producer, correlation_ref):\n    """伪造消息证据。"""\n'
                '    if False:\n        producer.publish(correlation_ref)\n'
                '    result = producer.get(correlation_ref)\n'
                '    record_control("场景", kind="messages", action="publish", correlation_ref=correlation_ref, side_effect="write")\n'
                '    return result\n',
                encoding="utf-8",
            )
            errors = GUARD._python_errors(path, root)
            self.assertTrue(any("control-evidence-operation" in item for item in errors))

    def test_business_entry_precedes_actions_and_assertions_are_not_tautologies(self) -> None:
        """业务动作必须在进入事件后，断言不能是常量别名或自比较。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "scenarios" / "场景" / "test_case.py"
            path.parent.mkdir(parents=True)
            path.write_text(
                '"""业务场景。"""\nimport pytest\n\n@pytest.mark.business_e2e\ndef test_case():\n'
                '    """执行反例。"""\n    preflight()\n    result = do_write()\n    record_business_entry()\n'
                '    assert result or True\n    try:\n        observe(result)\n    finally:\n        cleanup(result)\n',
                encoding="utf-8",
            )
            errors = GUARD._python_errors(path, root)
            self.assertTrue(any("business-entry-order" in item for item in errors))
            self.assertTrue(any("business-assertion-required" in item for item in errors))

    def test_identity_binding_accepts_real_target_and_rejects_tracking_only(self) -> None:
        """资源参数可以是请求目标，但追踪元数据不能证明写隔离。"""

        tree = ast.parse(
            'client.delete(correlation_ref)\nclient.post("shared", json={"tracking": correlation_ref})\n'
        )
        calls = [item for item in ast.walk(tree) if isinstance(item, ast.Call)]
        identity = ast.Name(id="correlation_ref", ctx=ast.Load())
        self.assertTrue(GUARD._operation_binds_identity(calls[0], identity))
        self.assertFalse(GUARD._operation_binds_identity(calls[1], identity))

    def test_control_sql_rejects_or_tautology_and_dynamic_statements(self) -> None:
        """控制 SQL 必须是可解析的精确等值 WHERE。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "common" / "controls" / "state.py"
            path.parent.mkdir(parents=True)
            path.write_text(
                '"""控制模块。"""\n\ndef mutate(selector_ref):\n    """执行宽泛变更。"""\n'
                '    return cursor.execute("UPDATE sample SET state=? WHERE id=? OR 1=1", ("x", selector_ref)).rowcount\n\n'
                'def build(cursor, selector_ref):\n    """执行动态语句。"""\n'
                '    sql = "DE" + "LETE FROM sample WHERE id=?"\n    return cursor.execute(sql, (selector_ref,)).rowcount\n',
                encoding="utf-8",
            )
            errors = GUARD._python_errors(path, root)
            self.assertTrue(any("control-sql-exact-where" in item for item in errors))
            self.assertTrue(any("control-sql-unproven" in item for item in errors))

    def test_restoration_rejects_lambdas_and_role_reuse(self) -> None:
        """恢复和验证必须是两个可证明的命名回调。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "common" / "controls" / "cleanup.py"
            path.parent.mkdir(parents=True)
            path.write_text(
                '"""恢复模块。"""\n\ndef inspect(resource_ref):\n    """只读取资源。"""\n    return store.get(resource_ref)\n\n'
                'def guard(context):\n    """注册伪恢复。"""\n'
                '    restoration_guard("场景", scenario_context=context, restore=inspect, verify=inspect)\n'
                '    restoration_guard("场景", scenario_context=context, restore=lambda resource_ref: None, verify=inspect)\n',
                encoding="utf-8",
            )
            errors = GUARD._python_errors(path, root)
            self.assertTrue(any("restoration-callback-role" in item for item in errors))
            self.assertTrue(any("restoration-callback" in item for item in errors))
            self.assertTrue(any("restoration-operation" in item for item in errors))

    def test_restoration_verify_rejects_tracking_only_identity(self) -> None:
        """追踪字段不能证明恢复校验观察了场景自有资源。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "common" / "controls" / "cleanup.py"
            path.parent.mkdir(parents=True)
            path.write_text(
                '"""Restoration callbacks."""\n\ndef restore(resource_ref):\n'
                '    """Restore the owned resource."""\n    return client.delete(resource_ref)\n\n'
                'def verify(resource_ref):\n    """Observe an unrelated shared target."""\n'
                '    return bool(client.get("shared-target", params={"tracking": resource_ref}))\n\n'
                'def guard(context):\n    """Register restoration."""\n'
                '    return restoration_guard("scenario", scenario_context=context, restore=restore, verify=verify)\n',
                encoding="utf-8",
            )

            errors = GUARD._python_errors(path, root)
            self.assertTrue(any("restoration-verification" in item for item in errors))

    def test_database_callbacks_cannot_reuse_roles_or_hide_in_dead_branches(self) -> None:
        """数据库快照与变更角色不得复用，执行也不能藏在死分支。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "common" / "controls" / "database.py"
            path.parent.mkdir(parents=True)
            path.write_text(
                '"""数据库控制。"""\n\ndef mutate(selector_ref):\n    """死分支变更。"""\n'
                '    if False:\n        return cursor.execute("UPDATE sample SET state=? WHERE id=?", ("x", selector_ref)).rowcount\n    return 1\n\n'
                'def restore(original, selector_ref):\n    """恢复数据。"""\n    return cursor.execute("UPDATE sample SET state=? WHERE id=?", (original, selector_ref)).rowcount\n\n'
                'def verify(original, selector_ref):\n    """验证数据。"""\n    return repository.matches(original, selector_ref)\n\n'
                'def apply(context):\n    """注册回调。"""\n    return controlled_database_state("场景", scenario_context=context, snapshot=mutate, mutate=mutate, restore=restore, verify_restored=verify)\n',
                encoding="utf-8",
            )
            errors = GUARD._python_errors(path, root)
            self.assertTrue(any("control-sql-callback-role" in item for item in errors))
            self.assertTrue(any("control-sql-selector-binding" in item for item in errors))

    def test_import_time_io_alias_sleep_and_missing_timeout_are_rejected(self) -> None:
        """收集前禁止网络 I/O、sleep 别名和无超时直连。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = root / "common" / "clients" / "api.py"
            adapter.parent.mkdir(parents=True)
            adapter.write_text('"""接口模块。"""\nimport httpx\nRESULT = httpx.get(target)\n', encoding="utf-8")
            errors = GUARD._python_errors(adapter, root)
            self.assertTrue(any("import-time-io" in item for item in errors))
            self.assertTrue(any("transport-timeout" in item for item in errors))

            scenario = root / "scenarios" / "场景" / "步骤.py"
            scenario.parent.mkdir(parents=True)
            scenario.write_text(
                '"""步骤模块。"""\nfrom time import sleep as wait_a_bit\n\ndef wait():\n    """执行盲等。"""\n    wait_a_bit(1)\n',
                encoding="utf-8",
            )
            self.assertTrue(any("blind-sleep" in item for item in GUARD._python_errors(scenario, root)))

    def test_sensitive_free_text_is_rejected_and_redacted(self) -> None:
        """自由文本中的命令行凭据和值赋值也必须脱敏。"""

        value = {"evidence": [
            "password=actual-secret",
            "--client-secret actual-secret",
            "private_key = actual-private-key",
            "ssh_key: actual-ssh-key",
            "passphrase=actual-passphrase",
            "auth = actual-auth",
        ]}
        self.assertTrue(GUARD._secret_errors(Path("workspace.yaml"), value))
        redacted = GUARD._redact_report(value)
        self.assertEqual(["<redacted>"] * len(value["evidence"]), redacted["evidence"])
        self.assertTrue(GUARD._secret_errors(Path("runtime.toml"), {"password": 123456}))
        self.assertFalse(GUARD._secret_errors(
            Path("runtime.toml"),
            {"password": "${DB_PASSWORD}", "private_key_reference": "secret-store:test/private-key"},
        ))

    def test_static_scans_plain_text_credentials_but_allows_references(self) -> None:
        """文本凭据必须被拦截，但精确占位符和类型化引用仍可使用。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            diagram = root / "scenarios" / "case" / "业务流程图.md"
            diagram.parent.mkdir(parents=True)
            diagram.write_text("Authorization: Bearer actual-secret-value\n", encoding="utf-8")
            errors = GUARD.static_errors(root)
            self.assertTrue(any("secret-free" in item and str(diagram) in item for item in errors))

            diagram.write_text("Authorization: Bearer ${API_TOKEN}\n", encoding="utf-8")
            errors = GUARD.static_errors(root)
            self.assertFalse(any("secret-free" in item and str(diagram) in item for item in errors))

    def test_python_source_rejects_literal_credentials(self) -> None:
        """Python 常量和敏感映射也不得包含真实凭据。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "common" / "clients" / "api.py"
            path.parent.mkdir(parents=True)
            path.write_text(
                '"""接口模块。"""\n\ndef headers():\n    """返回错误凭据。"""\n'
                '    return {"Authorization": "Bearer actual-secret-value", "private_key": "actual-private-key"}\n',
                encoding="utf-8",
            )
            self.assertTrue(any("python-secret" in item for item in GUARD._python_errors(path, root)))

    def test_same_owner_independent_environment_sources_need_no_fake_override(self) -> None:
        """同一模块中键不重叠的环境来源不得被迫声明虚假覆盖。"""

        configuration = {
            "sources": [
                {
                    "id": "first", "owner": "repo:app", "kind": "environment",
                    "location": "environment:FIRST_VALUE", "profile": None,
                    "overrides": [], "evidence": ["repo#FIRST_VALUE"],
                },
                {
                    "id": "second", "owner": "repo:app", "kind": "environment",
                    "location": "environment:SECOND_VALUE", "profile": None,
                    "overrides": [], "evidence": ["repo#SECOND_VALUE"],
                },
            ],
            "precedence": ["first", "second"],
            "services": [], "data_sources": [], "middleware": [], "controls": [],
        }
        errors = GUARD._configuration_errors(
            Path("workspace.yaml"), configuration, {"repo:app"}, {"repo:app"}, {"repo": Path(".")}
        )
        self.assertFalse(any("configuration-precedence-binding" in item for item in errors), errors)

    def test_capability_inventory_cannot_borrow_another_owner_or_type(self) -> None:
        """能力清单必须由实际能力所属模块和正确组件类型共同满足。"""

        def source(identifier: str, owner: str, key: str) -> dict[str, object]:
            return {
                "id": identifier, "owner": owner, "kind": "environment",
                "location": f"environment:{key}", "profile": None,
                "overrides": [], "evidence": [f"repo#{key}"],
            }

        def reference(source_id: str, key: str) -> dict[str, str]:
            return {
                "reference": f"environment:{key}", "effective_source": source_id,
                "resolution": "unresolved", "source_key": key,
            }

        def value(source_id: str, key: str) -> dict[str, object]:
            return {"value": None, "effective_source": source_id, "resolution": "unresolved", "source_key": key}

        configuration = {
            "sources": [
                source("first-source", "repo:first", "FIRST_VALUE"),
                source("second-source", "repo:second", "SECOND_VALUE"),
            ],
            "precedence": ["first-source", "second-source"],
            "services": [], "data_sources": [],
            "middleware": [
                {
                    "id": "wrong-type", "owner": "repo:first", "capability": "cache", "type": "cache",
                    "logical_name": value("first-source", "FIRST_VALUE"),
                    "connection_source": reference("first-source", "FIRST_VALUE"),
                },
                {
                    "id": "wrong-owner", "owner": "repo:second", "capability": "messages", "type": "broker",
                    "logical_name": value("second-source", "SECOND_VALUE"),
                    "connection_source": reference("second-source", "SECOND_VALUE"),
                },
            ],
            "controls": [],
        }
        errors = GUARD._configuration_errors(
            Path("workspace.yaml"), configuration,
            {"repo:first", "repo:second"}, {"repo:first", "repo:second"}, {"repo": Path(".")},
            required_capabilities={"messages": {"repo:first"}},
        )
        self.assertTrue(any("configuration-capability-omission" in item and "repo:first" in item for item in errors))

    def test_mutable_control_must_be_an_owned_restorable_resource(self) -> None:
        """动态配置等可变控制必须进入拥有资源和恢复契约。"""

        isolation = {
            "namespace": "case", "correlation_keys": ["case-key"],
            "owned_resources": [{
                "kind": "row", "identity": "owned-row", "cleanup": "delete-row",
                "restore": "restore-row", "verify": "verify-row",
            }],
            "mutable_controls": ["shared-switch"], "serial_lock": None,
        }
        errors = GUARD._isolation_errors(Path("scenario.yaml"), isolation, [{"side_effect": "write"}])
        self.assertTrue(any("isolation-mutable-resource" in item for item in errors))

    def test_database_control_rejects_echo_observers_and_empty_body(self) -> None:
        """字符串回显不能充当数据库观察，空 with 正文不能充当业务触发。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "common" / "controls" / "state.py"
            path.parent.mkdir(parents=True)
            path.write_text(
                '"""数据库控制反例。"""\n\n'
                'def snapshot(selector_ref):\n    """伪造快照。"""\n    return str(selector_ref)\n\n'
                'def mutate(selector_ref):\n    """推进单行。"""\n'
                '    return cursor.execute("UPDATE sample SET state=? WHERE id=?", ("next", selector_ref)).rowcount\n\n'
                'def restore(original, selector_ref):\n    """恢复单行。"""\n'
                '    return cursor.execute("UPDATE sample SET state=? WHERE id=?", (original, selector_ref)).rowcount\n\n'
                'def verify(original, selector_ref):\n    """伪造恢复核验。"""\n'
                '    return str(selector_ref) == str(original)\n\n'
                'def apply(context):\n    """用空正文伪造受控流程。"""\n'
                '    with controlled_database_state("场景", scenario_context=context, snapshot=snapshot, '
                'mutate=mutate, restore=restore, verify_restored=verify):\n        pass\n',
                encoding="utf-8",
            )
            errors = GUARD._python_errors(path, root)
            self.assertTrue(any("control-sql-observer-binding" in item for item in errors))
            self.assertTrue(any("control-sql-business-body" in item for item in errors))

    def test_scenario_aliases_cannot_hide_skip_evidence_or_direct_writes(self) -> None:
        """导入别名不得隐藏跳过、证据伪造或直接外部写入。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "scenarios" / "场景" / "步骤.py"
            path.parent.mkdir(parents=True)
            path.write_text(
                '"""别名绕过反例。"""\nimport subprocess\nfrom pathlib import Path\n'
                'from pytest import skip as abandon\n'
                'from common.e2e_runtime import record_control as note\n\n'
                'def act(client, key):\n    """尝试绕过门禁。"""\n'
                '    client.post("target", json={"id": key})\n'
                '    Path("result.txt").write_text(key)\n'
                '    subprocess.run(["tool", key])\n'
                '    note("场景", kind="public_api", action="create", correlation_ref=key, side_effect="write")\n'
                '    abandon("hidden")\n',
                encoding="utf-8",
            )
            errors = GUARD._python_errors(path, root)
            self.assertTrue(any("runtime-must-fail" in item for item in errors))
            self.assertTrue(any("evidence-adapter-owned" in item for item in errors))
            self.assertGreaterEqual(sum("side-effect-adapter-required" in item for item in errors), 3)

    def test_import_time_factory_and_skip_decorator_alias_are_rejected(self) -> None:
        """任意模块级工厂和别名 skip 装饰器都不能在收集阶段执行。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "scenarios" / "场景" / "test_case.py"
            path.parent.mkdir(parents=True)
            path.write_text(
                '"""收集期绕过反例。"""\nimport pytest as pt\n\n'
                '@build_marker()\n@pt.mark.skip(reason="hidden")\ndef test_case():\n'
                '    """不得被静默跳过。"""\n    assert observe()\n',
                encoding="utf-8",
            )
            errors = GUARD._python_errors(path, root)
            self.assertTrue(any("import-time-io" in item for item in errors))
            self.assertTrue(any("runtime-must-fail" in item for item in errors))

    def test_static_scans_extra_structured_secrets_and_credential_artifacts(self) -> None:
        """任意额外结构化配置和密钥文件都必须进入凭据扫描。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "extra.json").write_text('{"access_token":"actual-value"}', encoding="utf-8")
            (root / "extra.yaml").write_text("client_secret: actual-value\n", encoding="utf-8")
            (root / "extra.xml").write_text("<root><password>actual-value</password></root>", encoding="utf-8")
            (root / "client.key").write_text("private material", encoding="utf-8")
            errors = GUARD.static_errors(root)
            for name in ("extra.json", "extra.yaml", "extra.xml"):
                self.assertTrue(any("secret-free" in item and name in item for item in errors), (name, errors))
            self.assertTrue(any("credential-artifact" in item and "client.key" in item for item in errors))

    def test_python_secret_detection_covers_names_defaults_and_keywords(self) -> None:
        """敏感变量、默认参数和调用关键字中的字面量都必须被拒绝。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "common" / "clients" / "api.py"
            path.parent.mkdir(parents=True)
            path.write_text(
                '"""凭据反例。"""\nAPI_TOKEN = "actual-value"\n\n'
                'def call(client_secret="actual-default"):\n    """传递错误凭据。"""\n'
                '    return connect(password="actual-password")\n',
                encoding="utf-8",
            )
            errors = GUARD._python_errors(path, root)
            self.assertGreaterEqual(sum("python-secret" in item for item in errors), 3)

    def test_project_rejects_alternate_pytest_configuration_and_conftest(self) -> None:
        """备用 pytest 配置与隐式 conftest 均不得改变收集语义。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pyproject.toml").write_text(
                '[project]\nname="e2e"\nversion="0"\ndependencies=["pytest", "PyYAML"]\n\n'
                '[tool.pytest.ini_options]\nmarkers=["business_e2e: b", "scenario_id(value): s", "read_only_smoke: r"]\n',
                encoding="utf-8",
            )
            (root / "pytest.ini").write_text("[pytest]\naddopts=--collect-only\n", encoding="utf-8")
            (root / "tox.ini").write_text("[pytest]\naddopts=-k subset\n", encoding="utf-8")
            (root / "conftest.py").write_text('"""隐式插件。"""\n', encoding="utf-8")
            errors = GUARD._project_errors(root)
            self.assertGreaterEqual(sum("pytest-configuration" in item for item in errors), 2)
            self.assertTrue(any("pytest-conftest-forbidden" in item for item in errors))

    def test_junit_requires_executed_unskipped_success(self) -> None:
        """零测试、skip、xfail、失败和错误都不能成为业务成功。"""

        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "junit.xml"
            report.write_text('<testsuite tests="1" failures="0" errors="0" skipped="1"/>', encoding="utf-8")
            self.assertFalse(GUARD._pytest_junit_passed(report))
            report.write_text('<testsuite tests="0" failures="0" errors="0" skipped="0"/>', encoding="utf-8")
            self.assertFalse(GUARD._pytest_junit_passed(report))
            report.write_text('<testsuite tests="1" failures="0" errors="0" skipped="0"/>', encoding="utf-8")
            self.assertTrue(GUARD._pytest_junit_passed(report))

    def test_gate_session_rejects_replay_and_out_of_order_stage(self) -> None:
        """历史 seal 和非下一阶段都不得复用于新的门禁会话。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.yaml"
            source.write_text("value: 1\n", encoding="utf-8")
            GUARD._start_gate_session(root)
            GUARD._write_seal(root, "workspace_inventory", [source])
            self.assertTrue(GUARD._gate_session_errors(root, "dependency_topology"))
            GUARD._start_gate_session(root)
            self.assertTrue(GUARD._seal_errors(root, "workspace_inventory", [source]))

    def test_database_control_expected_rows_must_be_exactly_one(self) -> None:
        """控制 SQL 不得通过声明较大影响行数扩大修改范围。"""

        controls = {
            name: {
                "status": "not_applicable", "assessment": "当前场景不使用",
                "evidence": [], "planned_use": [],
                **({"safety": None} if name == "database_control" else {}),
                **({"correlation_keys": [], "business_evidence": [], "recovery": []} if name == "observability" else {}),
            }
            for name in GUARD.CONTROL_NAMES
        }
        controls["database_control"] = {
            "status": "usable", "assessment": "单行受控推进", "evidence": ["repo#DatabaseConsumer"],
            "planned_use": ["advance"],
            "safety": {
                "authorization_required": True, "target_environment": "test", "purpose": "time_advance",
                "consumer_source": "repo#DatabaseConsumer", "exact_selector": "owned-row", "expected_rows": 2,
                "snapshot": "snapshot", "mutation": "advance", "trigger": "trigger",
                "verification": "verified", "restoration": "restore", "restoration_verification": "restored",
            },
        }
        controls["decision"] = {"safe_control_path": True, "blockers": []}
        errors = GUARD._control_errors(
            Path("scenario.yaml"), controls,
            [{"action": "advance", "control": "database_control", "side_effect": "write", "expect": ["prepared"]}],
            {"repo#DatabaseConsumer"},
        )
        self.assertTrue(any("database-control-safety" in item for item in errors))

    def test_consumer_source_requires_database_or_job_semantics(self) -> None:
        """普通同名函数不能冒充会消费控制字段的数据库或任务源码锚点。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "logic.py").write_text(
                'def OrdinaryAnchor(value):\n    return value.upper()\n\n'
                'def DatabaseConsumer(cursor, key):\n    return cursor.execute("SELECT state FROM sample WHERE id=?", (key,))\n',
                encoding="utf-8",
            )
            for command in (
                ["git", "init", "-q"],
                ["git", "config", "user.email", "test@example.invalid"],
                ["git", "config", "user.name", "E2E Test"],
                ["git", "add", "."],
                ["git", "commit", "-q", "-m", "fixture"],
            ):
                completed = subprocess.run(command, cwd=source, check=False, capture_output=True, text=True)
                self.assertEqual(0, completed.returncode, completed.stderr)
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=source, check=True, capture_output=True, text=True
            ).stdout.strip()
            discovery = {"inventory": {"repositories": [{"id": "repo", "root": "source", "commit": commit}]}}
            self.assertFalse(GUARD._source_reference_semantic(root, discovery, "repo#OrdinaryAnchor", {"database", "jobs"}))
            self.assertTrue(GUARD._source_reference_semantic(root, discovery, "repo#DatabaseConsumer", {"database", "jobs"}))

    def test_source_contract_requires_discovery_commit_and_all_relevant_repositories(self) -> None:
        """场景不得引用其他提交或省略拓扑中的相关仓库。"""

        discovery_commit = "1" * 40
        scenario_commit = "2" * 40
        discovery = {
            "inventory": {"repositories": [
                {"id": "first", "root": ".", "commit": discovery_commit},
                {"id": "second", "root": ".", "commit": discovery_commit},
            ]},
            "topology": {"nodes": [
                {"id": "first:app", "relevant": True},
                {"id": "second:client", "relevant": True},
            ]},
        }
        with patch.object(GUARD, "_git", return_value=subprocess.CompletedProcess([], 0, "", "")):
            errors = GUARD._source_errors(
                Path("scenario.yaml"),
                [{"repo": "first", "commit": scenario_commit, "anchors": ["Anchor"]}],
                discovery,
                Path("."),
            )
        self.assertTrue(any("source-commit-coherence" in item for item in errors))
        self.assertTrue(any("source-relevant-coverage" in item and "second" in item for item in errors))

    def test_source_version_git_status_or_diff_failure_requires_rediscovery(self) -> None:
        """Git 状态或差异命令失败不得被误报为工作区干净。"""

        recorded = "1" * 40
        current = "2" * 40
        definition = {"source": [{"repo": "repo", "commit": recorded, "anchors": ["Anchor"]}]}
        discovery = {"inventory": {"repositories": [{"id": "repo", "root": "source"}]}}
        for failed_command in ("status", "diff"):
            with self.subTest(failed_command=failed_command), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                scenario = root / "scenarios" / "场景"

                def fake_git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
                    command = arguments[0]
                    if command == "rev-parse":
                        return subprocess.CompletedProcess(arguments, 0, current + "\n", "")
                    if command == failed_command:
                        return subprocess.CompletedProcess(arguments, 1, "", "failed")
                    return subprocess.CompletedProcess(arguments, 0, "", "")

                with (
                    patch.object(GUARD, "contract_errors", return_value=([], [(scenario, definition)], discovery)),
                    patch.object(GUARD, "_git", side_effect=fake_git),
                ):
                    errors, results = GUARD.source_version_results(root)
                self.assertEqual([], errors)
                self.assertEqual("full_rediscovery_required", results[0]["outcome"])


if __name__ == "__main__":
    unittest.main()
