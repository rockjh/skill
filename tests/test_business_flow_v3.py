from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from dltk.business_flow_cli import _progress
from dltk.business_flow_discovery import scan
from dltk.business_flow_documents import _diagram, _validate_mermaid, apply_module_map, write_discovery
from dltk.business_flow_models import BehaviorEvidence, EntryPoint, GitInfo


class BusinessFlowV3Tests(unittest.TestCase):
    def _git(self, root: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)

    def test_registration_counts_ignore_comments_and_plain_task_methods(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text(
                "from fastapi import FastAPI\n"
                "app = FastAPI()\n"
                "# receiver('fake-topic')\n"
                "def task(value): return value\n"
                "@app.get('/resources')\n"
                "def list_resources(): return []\n",
                encoding="utf-8",
            )
            self._git(root)
            result = scan(root)
            self.assertEqual(1, len(result.entries))
            docs = root / "docs"
            write_discovery(result, docs)
            self.assertTrue((docs / "business-flow-modules-draft.json").is_file())
            mapping = json.loads((docs / "business-flow-modules.json").read_text(encoding="utf-8"))
            module = mapping["modules"][0]
            self.assertIn("responsibility", module)
            self.assertIn("file", module)
            self.assertEqual(result.entries[0].entry_id, mapping["entry_reviews"][0]["id"])

    def test_progress_redacts_errors_in_json_and_failure_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            docs_root = Path(temporary)
            _progress(docs_root, "generate", "failed", error="token=actual-secret")
            progress = (docs_root / "business-flow-progress.json").read_text(encoding="utf-8")
            failures = (docs_root / "business-flow-failures.log").read_text(encoding="utf-8")
            self.assertNotIn("actual-secret", progress)
            self.assertNotIn("actual-secret", failures)

    def test_same_named_definitions_are_not_merged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text(
                "from fastapi import FastAPI\napp = FastAPI()\n"
                "@app.get('/x')\ndef get_x():\n    save()\n    return []\n",
                encoding="utf-8",
            )
            (root / "one.py").write_text("def save(): raise E('ONE_ERROR')\n", encoding="utf-8")
            (root / "two.py").write_text("def save(): raise E('TWO_ERROR')\n", encoding="utf-8")
            self._git(root)
            result = scan(root)
            entry = next(item for item in result.entries if item.identifier == "GET /x")
            self.assertNotIn("ONE_ERROR", entry.error_codes())
            self.assertNotIn("TWO_ERROR", entry.error_codes())
            self.assertTrue(any("multiple possible definitions" in value for value in result.unresolved))

    def test_external_action_is_not_rendered_as_optional(self) -> None:
        entry = EntryPoint(
            "url:x", "url", "GET /x", "handler", "app.py", 1, "resources", "app.py",
            behaviors=[BehaviorEvidence("外部调用", "client.fetch()", "app.py", 2)],
        )
        diagram = _diagram(entry)
        self.assertNotIn("opt 外部调用", diagram)
        self.assertIn("External", diagram)

    def test_java_multi_route_binding_is_counted_separately(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Controller.java").write_text(
                "class Controller {\n"
                "  @GetMapping({\"/one\", \"/two\"})\n"
                "  String read() { return \"ok\"; }\n"
                "}\n",
                encoding="utf-8",
            )
            self._git(root)
            identifiers = {entry.identifier for entry in scan(root).entries}
            self.assertIn("GET /one", identifiers)
            self.assertIn("GET /two", identifiers)
            self.assertTrue(any("parser support for Java is heuristic" in item for item in scan(root).unresolved))

    def test_feign_mapping_is_outbound_not_an_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Client.java").write_text(
                "@FeignClient(name=\"resource\")\n"
                "interface Client { @GetMapping(\"/resources\") String read(); }\n",
                encoding="utf-8",
            )
            self._git(root)
            self.assertEqual([], scan(root).entries)

    def test_typed_receiver_selects_the_reachable_same_named_method(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text(
                "from fastapi import FastAPI\napp = FastAPI()\n"
                "class One:\n    def save(self): raise E('ONE_ERROR')\n"
                "class Two:\n    def save(self): raise E('TWO_ERROR')\n"
                "@app.get('/x')\n"
                "def get_x():\n    service: One = One()\n    service.save()\n",
                encoding="utf-8",
            )
            self._git(root)
            result = scan(root)
            entry = next(item for item in result.entries if item.identifier == "GET /x")
            self.assertIn("ONE_ERROR", entry.error_codes())
            self.assertNotIn("TWO_ERROR", entry.error_codes())

    def test_confirmed_map_requires_one_review_per_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text(
                "from fastapi import FastAPI\napp = FastAPI()\n"
                "@app.get('/x')\ndef get_x(): return []\n",
                encoding="utf-8",
            )
            self._git(root)
            result = scan(root)
            docs = root / "docs"
            write_discovery(result, docs)
            path = docs / "business-flow-modules.json"
            mapping = json.loads(path.read_text(encoding="utf-8"))
            mapping["confirmed"] = True
            mapping["entry_reviews"] = []
            path.write_text(json.dumps(mapping), encoding="utf-8")
            errors = apply_module_map(result, path)
            self.assertTrue(any("exactly one confirmed entry review" in error for error in errors))

    def test_draft_review_cannot_be_promoted_by_module_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text(
                "from fastapi import FastAPI\napp = FastAPI()\n"
                "@app.get('/x')\ndef get_x(): return []\n",
                encoding="utf-8",
            )
            self._git(root)
            result = scan(root)
            docs = root / "docs"
            write_discovery(result, docs)
            path = docs / "business-flow-modules.json"
            mapping = json.loads(path.read_text(encoding="utf-8"))
            mapping["confirmed"] = True
            path.write_text(json.dumps(mapping), encoding="utf-8")
            errors = apply_module_map(result, path)
            self.assertTrue(any("not explicitly human-confirmed" in error for error in errors))

    def test_module_file_must_be_markdown_and_active_after_exclusions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text(
                "from fastapi import FastAPI\napp = FastAPI()\n"
                "@app.get('/x')\ndef get_x(): return []\n",
                encoding="utf-8",
            )
            self._git(root)
            result = scan(root)
            docs = root / "docs"
            write_discovery(result, docs)
            path = docs / "business-flow-modules.json"
            mapping = json.loads(path.read_text(encoding="utf-8"))
            mapping["confirmed"] = True
            mapping["modules"][0]["file"] = "module.txt"
            mapping["modules"][0]["rationale"] = "按业务对象边界归组"
            mapping["modules"][0]["responsibility"] = "处理资源查询"
            for review in mapping["entry_reviews"]:
                review.update({
                    "status": "confirmed", "confirmed_by": "reviewer", "trigger": "调用方",
                    "purpose": "查询资源", "input": "资源标识", "outcome": "返回资源", "failure": "返回明确错误",
                })
                for step in review["steps"]:
                    step["text"] = "执行资源查询"
            path.write_text(json.dumps(mapping), encoding="utf-8")
            errors = apply_module_map(result, path)
            self.assertTrue(any("must be Markdown" in error for error in errors))

            mapping["modules"][0]["file"] = "module.md"
            mapping["entry_reviews"] = []
            candidate = mapping["modules"][0]["entry_ids"][0]
            mapping["exclusions"] = [{"candidate": candidate, "reason": "非业务入口", "evidence": ["app.py:3"]}]
            path.write_text(json.dumps(mapping), encoding="utf-8")
            result = scan(root)
            errors = apply_module_map(result, path)
            self.assertTrue(any("no active entries after exclusions" in error for error in errors))

    def test_mermaid_validator_rejects_unbalanced_control_block(self) -> None:
        self.assertTrue(_validate_mermaid("sequenceDiagram\nparticipant A as A\nalt x\nA->>A: y\n"))

    def test_mermaid_validator_checks_note_participants(self) -> None:
        self.assertTrue(_validate_mermaid(
            "sequenceDiagram\nparticipant A as A\nNote over A,B: missing\n"
        ))

    def test_unknown_qualified_receiver_does_not_use_global_method_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text(
                "from fastapi import FastAPI\napp = FastAPI()\n"
                "def save(): raise E('UNRELATED')\n"
                "@app.get('/x')\n"
                "def get_x():\n    client.save()\n    return []\n",
                encoding="utf-8",
            )
            self._git(root)
            result = scan(root)
            entry = next(item for item in result.entries if item.identifier == "GET /x")
            self.assertNotIn("UNRELATED", entry.error_codes())
            self.assertTrue(any("unknown receiver type" in item for item in result.unresolved))

    def test_caught_loop_item_error_is_kept_as_partial_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text(
                "from fastapi import FastAPI\napp = FastAPI()\n"
                "@app.post('/batch')\n"
                "def batch(items):\n"
                "    for item in items:\n"
                "        try:\n"
                "            raise BusinessError('ITEM_FAILED')\n"
                "        except BusinessError:\n"
                "            continue\n"
                "    return {'accepted': True}\n",
                encoding="utf-8",
            )
            self._git(root)
            result = scan(root)
            entry = next(item for item in result.entries if item.identifier == "POST /batch")
            error = next(item for item in entry.errors if item.code == "ITEM_FAILED")
            self.assertEqual("item", error.phase)
            self.assertIn("继续", error.consequence)

    def test_worker_error_is_not_reported_as_sync_response_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app.py").write_text(
                "from fastapi import FastAPI\napp = FastAPI()\n"
                "def worker():\n    raise BusinessError('WORKER_FAILED')\n"
                "@app.post('/jobs')\n"
                "def submit_job(executor):\n    executor.submit(worker)\n    return {'accepted': True}\n",
                encoding="utf-8",
            )
            self._git(root)
            result = scan(root)
            entry = next(item for item in result.entries if item.identifier == "POST /jobs")
            error = next(item for item in entry.errors if item.code == "WORKER_FAILED")
            self.assertEqual("worker", error.phase)
            self.assertIn("已受理", error.consequence)


if __name__ == "__main__":
    unittest.main()
