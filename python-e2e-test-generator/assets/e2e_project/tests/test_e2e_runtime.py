"""验证 E2E 运行预检、占位符和可恢复控制。"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SPEC = importlib.util.spec_from_file_location("e2e_runtime", PROJECT_ROOT / "common" / "e2e_runtime.py")
assert RUNTIME_SPEC and RUNTIME_SPEC.loader
RUNTIME = importlib.util.module_from_spec(RUNTIME_SPEC)
RUNTIME_SPEC.loader.exec_module(RUNTIME)


class RuntimeTests(unittest.TestCase):
    """覆盖配置选择、授权、行数校验和异常保留。"""

    @staticmethod
    def _write_yaml(path: Path, value: object) -> None:
        """写入合成 YAML 映射。"""

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(RUNTIME.yaml.safe_dump(value, allow_unicode=True), encoding="utf-8")

    @staticmethod
    def _write_context() -> dict[str, object]:
        """返回声明单一拥有资源的最小写场景上下文。"""

        return {
            "definition": {
                "isolation": {
                    "owned_resources": [{"identity": "owned-key"}],
                }
            }
        }

    def test_deep_merge_replaces_only_leaf(self) -> None:
        """递归合并应保留未覆盖兄弟键。"""

        merged = RUNTIME.deep_merge({"a": {"x": 1, "y": 2}}, {"a": {"x": 3}})
        self.assertEqual({"a": {"x": 3, "y": 2}}, merged)

    def test_placeholder_requires_exact_nonblank_environment_value(self) -> None:
        """只解析完整占位符并拒绝缺失或空白值。"""

        self.assertEqual("prefix-${VALUE}", RUNTIME.resolve_placeholders("prefix-${VALUE}", environ={"VALUE": "ok"}))
        self.assertEqual("ok", RUNTIME.resolve_placeholders("${VALUE}", environ={"VALUE": "ok"}))
        with self.assertRaises(RUNTIME.PreflightError):
            RUNTIME.resolve_placeholders("${VALUE}", environ={"VALUE": " "})

    def test_preflight_resolves_only_declared_integrations(self) -> None:
        """无关服务缺少凭据时不得阻塞当前场景。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scenario = root / "scenarios" / "读取场景"
            environment = root / "config" / "environments"
            scenario.mkdir(parents=True)
            environment.mkdir(parents=True)
            (root / "config" / "config.yaml").write_text(
                "active_environment: test\ndefaults:\n  safety:\n"
                "    database_control_enabled: false\n"
                "    mutable_configuration_enabled: false\n"
                "    message_publish_enabled: false\n",
                encoding="utf-8",
            )
            (environment / "test.yaml").write_text(
                "safety:\n  test_environment: true\n  side_effects_allowed: false\n  protected: false\n"
                "services:\n  used:\n    base_url: ${USED_URL}\n  unused:\n    base_url: ${MISSING_URL}\ncomponents: {}\n",
                encoding="utf-8",
            )
            (scenario / "场景定义.yaml").write_text(
                "meta:\n  status: ready\nintegrations:\n  services: [used]\n  components: []\n"
                "controls:\n  database_control:\n    status: not_applicable\n    planned_use: []\nsteps: []\nisolation:\n  namespace: read\n",
                encoding="utf-8",
            )
            (scenario / "业务数据.json").write_text('{"test": {}}', encoding="utf-8")

            context = RUNTIME.preflight(root, "读取场景", environ={"USED_URL": "http://example.invalid"})
            self.assertEqual({"used"}, set(context["configuration"]["services"]))

    def test_preflight_requires_per_run_message_authorization(self) -> None:
        """消息发布控制必须针对精确测试环境获得本次运行授权。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scenario = root / "scenarios" / "发布场景"
            environment = root / "config" / "environments"
            scenario.mkdir(parents=True)
            environment.mkdir(parents=True)
            (root / "config" / "config.yaml").write_text(
                "active_environment: test\ndefaults:\n  safety:\n"
                "    database_control_enabled: false\n"
                "    mutable_configuration_enabled: false\n"
                "    message_publish_enabled: false\n",
                encoding="utf-8",
            )
            (environment / "test.yaml").write_text(
                "safety:\n  test_environment: true\n  side_effects_allowed: true\n  protected: false\n"
                "services: {}\ncomponents: {}\n",
                encoding="utf-8",
            )
            (scenario / "场景定义.yaml").write_text(
                "meta:\n  status: ready\nintegrations:\n  services: []\n  components: []\n"
                "controls:\n  messages:\n    status: usable\n    planned_use: [publish]\n"
                "  database_control:\n    status: not_applicable\n    planned_use: []\n"
                "steps:\n  - control: messages\n    side_effect: write\n"
                "isolation:\n  namespace: publish\n",
                encoding="utf-8",
            )
            (scenario / "业务数据.json").write_text('{"test": {}}', encoding="utf-8")

            with self.assertRaises(RUNTIME.PreflightError):
                RUNTIME.preflight(root, "发布场景", environ={})
            authorized = {
                "E2E_ENABLE_MESSAGE_PUBLISH": "true",
                "E2E_MESSAGE_PUBLISH_AUTHORIZATION_REF": "approval-reference",
                "E2E_CONTROL_ENVIRONMENT": "test",
            }
            context = RUNTIME.preflight(root, "发布场景", environ=authorized)
            self.assertEqual("test", context["active_environment"])

    def test_database_control_restores_after_row_count_failure(self) -> None:
        """控制 SQL 影响行数异常时仍必须恢复原值。"""

        restored: list[dict[str, int]] = []
        environment = {
            "E2E_ENABLE_DATABASE_CONTROL": "true",
            "E2E_CONTROL_AUTHORIZATION_REF": "approval-reference",
            "E2E_CONTROL_ENVIRONMENT": "test",
        }
        context = {
            "active_environment": "test",
            "definition": {
                "controls": {
                    "database_control": {
                        "status": "usable",
                        "safety": {
                            "target_environment": "test",
                            "expected_rows": 1,
                            "exact_selector": "owned-key",
                        },
                    }
                }
            },
        }
        with patch.dict(os.environ, environment, clear=False):
            with self.assertRaises(AssertionError):
                with RUNTIME.controlled_database_state(
                    "场景",
                    scenario_context=context,
                    snapshot=lambda selector: {"value": 1},
                    mutate=lambda selector: 2,
                    restore=lambda original, selector: restored.append(original) or 1,
                    verify_restored=lambda original, selector: original == {"value": 1},
                ):
                    pass
        self.assertEqual([{"value": 1}], restored)

    def test_database_control_derives_selector_from_contract(self) -> None:
        """控制回调只能接收场景契约派生的精确选择器。"""

        context = {
            "active_environment": "test",
            "definition": {
                "controls": {
                    "database_control": {
                        "status": "usable",
                        "safety": {
                            "target_environment": "test",
                            "expected_rows": 1,
                            "exact_selector": "owned-key",
                        },
                    }
                }
            },
        }
        observed: list[str] = []
        environment = {
            "E2E_ENABLE_DATABASE_CONTROL": "true",
            "E2E_CONTROL_AUTHORIZATION_REF": "approval-reference",
            "E2E_CONTROL_ENVIRONMENT": "test",
        }
        with patch.dict(os.environ, environment, clear=False):
            with RUNTIME.controlled_database_state(
                "场景",
                scenario_context=context,
                snapshot=lambda selector: observed.append(selector) or {},
                mutate=lambda selector: observed.append(selector) or 1,
                restore=lambda original, selector: observed.append(selector) or 1,
                verify_restored=lambda original, selector: observed.append(selector) or True,
            ):
                pass
        self.assertEqual(["owned-key"] * 4, observed)

    def test_database_control_rejects_boolean_row_count(self) -> None:
        """布尔值不能冒充 expected_rows 或驱动返回的行数。"""

        context = {
            "active_environment": "test",
            "definition": {"controls": {"database_control": {
                "status": "usable",
                "safety": {"target_environment": "test", "expected_rows": True, "exact_selector": "owned-key"},
            }}},
        }
        environment = {
            "E2E_ENABLE_DATABASE_CONTROL": "true",
            "E2E_CONTROL_AUTHORIZATION_REF": "approval-reference",
            "E2E_CONTROL_ENVIRONMENT": "test",
        }
        with patch.dict(os.environ, environment, clear=False), self.assertRaises(RUNTIME.PreflightError):
            with RUNTIME.controlled_database_state(
                "场景",
                scenario_context=context,
                snapshot=lambda selector: {},
                mutate=lambda selector: True,
                restore=lambda original, selector: True,
                verify_restored=lambda original, selector: True,
            ):
                pass

    def test_poll_until_uses_bounded_deadline(self) -> None:
        """有限轮询必须在单调截止时间结束并报告最后状态。"""

        ticks = iter((0.0, 0.0, 1.0, 2.0))
        sleeps: list[float] = []
        with self.assertRaisesRegex(AssertionError, "最后"):
            RUNTIME.poll_until(
                lambda: "last",
                lambda value: False,
                timeout_seconds=2.0,
                interval_seconds=1.0,
                clock=lambda: next(ticks),
                sleeper=sleeps.append,
            )
        self.assertEqual([1.0, 1.0], sleeps)

    def test_smoke_endpoint_rejects_write_method(self) -> None:
        """运行时证据层也必须拒绝把写请求标成只读冒烟。"""

        with self.assertRaises(RUNTIME.PreflightError):
            RUNTIME.record_endpoint(
                "场景",
                phase="smoke",
                method="POST",
                target_ref="service.health",
                status=200,
                summary={"ok": True},
                verified=True,
            )

    def test_smoke_endpoint_rejects_server_error_and_string_status(self) -> None:
        """HTTP 冒烟不得把 5xx 或字符串状态码记录为成功。"""

        for status in (503, "503"):
            with self.subTest(status=status), self.assertRaises(RUNTIME.PreflightError):
                RUNTIME.record_endpoint(
                    "场景",
                    phase="smoke",
                    method="GET",
                    target_ref="service.health",
                    status=status,
                    summary={"status": status},
                    verified=True,
                )

    def test_business_endpoint_rejects_server_error_and_string_status(self) -> None:
        """业务接口失败必须保留为失败，不能只生成成功证据。"""

        for status in (503, "200"):
            with self.subTest(status=status), self.assertRaises(RUNTIME.PreflightError):
                RUNTIME.record_endpoint(
                    "场景",
                    phase="business",
                    method="POST",
                    target_ref="service.action",
                    status=status,
                    summary={"status": status},
                    verified=True,
                )

    def test_preflight_rejects_production_name_even_when_flags_claim_test(self) -> None:
        """生产惯用环境名不得通过自报测试安全标记绕过。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scenario = root / "scenarios" / "场景"
            environment = root / "config" / "environments"
            scenario.mkdir(parents=True)
            environment.mkdir(parents=True)
            self._write_yaml(root / "config" / "config.yaml", {
                "active_environment": "production",
                "defaults": {"safety": {
                    "database_control_enabled": False,
                    "mutable_configuration_enabled": False,
                    "message_publish_enabled": False,
                }},
            })
            self._write_yaml(environment / "production.yaml", {
                "services": {}, "components": {},
                "safety": {"test_environment": True, "side_effects_allowed": True, "protected": False},
            })
            self._write_yaml(scenario / "场景定义.yaml", {
                "meta": {"status": "ready"}, "steps": [],
                "controls": {}, "integrations": {"services": [], "components": []},
                "isolation": {"namespace": "owned"},
            })
            (scenario / "业务数据.json").write_text('{"production": {}}', encoding="utf-8")
            with self.assertRaisesRegex(RUNTIME.PreflightError, "生产|在线"):
                RUNTIME.preflight(root, "场景", environ={})

    def test_dangerous_control_requires_authorization_despite_read_claim(self) -> None:
        """危险控制授权不能由场景自报的只读副作用规避。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scenario = root / "scenarios" / "场景"
            environment = root / "config" / "environments"
            scenario.mkdir(parents=True)
            environment.mkdir(parents=True)
            self._write_yaml(root / "config" / "config.yaml", {
                "active_environment": "test",
                "defaults": {"safety": {
                    "database_control_enabled": False,
                    "mutable_configuration_enabled": False,
                    "message_publish_enabled": False,
                }},
            })
            self._write_yaml(environment / "test.yaml", {
                "services": {}, "components": {},
                "safety": {"test_environment": True, "side_effects_allowed": True, "protected": False},
            })
            self._write_yaml(scenario / "场景定义.yaml", {
                "meta": {"status": "ready"},
                "steps": [{"control": "scheduled_jobs", "side_effect": "read"}],
                "controls": {"scheduled_jobs": {"status": "usable", "planned_use": ["trigger"]}},
                "integrations": {"services": [], "components": []},
                "isolation": {"namespace": "owned"},
            })
            (scenario / "业务数据.json").write_text('{"test": {}}', encoding="utf-8")
            with self.assertRaisesRegex(RUNTIME.PreflightError, "危险控制"):
                RUNTIME.preflight(root, "场景", environ={})

    def test_preflight_rejects_protected_write_environment(self) -> None:
        """任何写场景都不得在受保护环境执行。"""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scenario = root / "scenarios" / "场景"
            environment = root / "config" / "environments"
            scenario.mkdir(parents=True)
            environment.mkdir(parents=True)
            self._write_yaml(root / "config" / "config.yaml", {
                "active_environment": "test",
                "defaults": {"safety": {"database_control_enabled": False}},
            })
            self._write_yaml(environment / "test.yaml", {
                "services": {}, "components": {},
                "safety": {"test_environment": True, "side_effects_allowed": True, "protected": True},
            })
            self._write_yaml(scenario / "场景定义.yaml", {
                "meta": {"status": "ready"},
                "steps": [{"side_effect": "write", "control": "messages"}],
                "controls": {"messages": {"status": "usable", "planned_use": ["publish"]}},
                "integrations": {"services": [], "components": []},
                "isolation": {"namespace": "owned"},
            })
            (scenario / "业务数据.json").write_text('{"test": {}}', encoding="utf-8")
            with self.assertRaises(RUNTIME.PreflightError):
                RUNTIME.preflight(root, "场景")

    def test_redaction_hides_sensitive_parent_but_keeps_reference_metadata(self) -> None:
        """敏感父映射整体脱敏，连接来源只保留非敏感引用。"""

        redacted = RUNTIME._redact({
            "auth": {"value": "secret"},
            "connection_source": {
                "reference": "environment:CONNECTION_REF",
                "private_key": "secret",
            },
        })
        self.assertEqual("<redacted>", redacted["auth"])
        self.assertEqual("environment:CONNECTION_REF", redacted["connection_source"]["reference"])
        self.assertEqual("<redacted>", redacted["connection_source"]["private_key"])

    def test_restoration_failure_keeps_original_exception(self) -> None:
        """业务和恢复同时失败时业务异常保持为主异常。"""

        with self.assertRaisesRegex(ValueError, "business") as raised:
            with RUNTIME.restoration_guard(
                "场景",
                scenario_context=self._write_context(),
                restore=lambda resource_ref: (_ for _ in ()).throw(RuntimeError(f"cleanup:{resource_ref}")),
                verify=lambda resource_ref: True,
            ):
                raise ValueError("business")
        self.assertTrue(any("cleanup" in note for note in getattr(raised.exception, "__notes__", [])))

    def test_restoration_evidence_failure_keeps_original_exception(self) -> None:
        """恢复与证据写入均失败时仍以原始业务异常为主。"""

        with patch.object(RUNTIME, "_emit_event", side_effect=OSError("evidence")):
            with self.assertRaisesRegex(ValueError, "business") as raised:
                with RUNTIME.restoration_guard(
                    "场景",
                    scenario_context=self._write_context(),
                    restore=lambda resource_ref: (_ for _ in ()).throw(RuntimeError(f"cleanup:{resource_ref}")),
                    verify=lambda resource_ref: True,
                ):
                    raise ValueError("business")
        notes = getattr(raised.exception, "__notes__", [])
        self.assertTrue(any("cleanup" in note for note in notes))
        self.assertTrue(any("evidence" in note for note in notes))

    def test_restoration_resources_are_derived_from_contract(self) -> None:
        """调用方不得用自报资源标识伪造恢复覆盖。"""

        with self.assertRaises(RUNTIME.PreflightError):
            with RUNTIME.restoration_guard(
                "场景",
                scenario_context={"definition": {"isolation": {"owned_resources": []}}},
                restore=lambda resource_ref: None,
                verify=lambda resource_ref: True,
            ):
                pass

    def test_restoration_attempts_every_owned_resource(self) -> None:
        """一个资源恢复失败后仍必须尝试其余拥有资源。"""

        attempted: list[str] = []
        context = {"definition": {"isolation": {"owned_resources": [
            {"identity": "first"}, {"identity": "second"},
        ]}}}

        def restore(resource_ref: str) -> None:
            """记录恢复尝试并让第一个资源失败。"""

            attempted.append(resource_ref)
            if resource_ref == "first":
                raise RuntimeError("first failed")

        with self.assertRaises(RuntimeError):
            with RUNTIME.restoration_guard(
                "场景",
                scenario_context=context,
                restore=restore,
                verify=lambda resource_ref: True,
            ):
                pass
        self.assertEqual(["first", "second"], attempted)


if __name__ == "__main__":
    unittest.main()
