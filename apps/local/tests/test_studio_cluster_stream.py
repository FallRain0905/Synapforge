"""集群角色流式：stream-json 行 → local_cluster_steps + broker 事件。"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from synapforge import commands as base
from synapforge.db import open_store
from synapforge.studio import commands as studio_commands
from synapforge.studio import store as studio_store
from synapforge.studio.step_parser import StepParser
from synapforge.studio.supervisor import SupervisorManager


class StepParserTests(unittest.TestCase):
    def _parser(self) -> tuple[StepParser, list, list]:
        steps: list[dict] = []
        updates: list[dict] = []
        counter = {"n": 0}

        def emit(kind, title, detail, payload):
            counter["n"] += 1
            step = {"id": f"step-{counter['n']}", "seq": counter["n"], "kind": kind,
                    "title": title, "detail": detail, "payload": payload or {}}
            steps.append(step)
            return step

        def update(step_id, title, detail):
            for step in steps:
                if step["id"] == step_id:
                    step["title"] = title
                    step["detail"] = detail

        def publish(event):
            updates.append(event)

        return StepParser(emit=emit, update=update, publish=publish), steps, updates

    def test_partial_text_accumulates(self) -> None:
        parser, steps, updates = self._parser()
        clock = {"t": 0.0}
        parser._now = lambda: clock["t"]  # noqa: SLF001
        parser.feed(json.dumps({"type": "stream_event", "event": {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text"}}}))
        for chunk in ("你", "好", "世界"):
            clock["t"] += 1.0
            parser.feed(json.dumps({"type": "stream_event", "event": {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": chunk}}}))
        parser.feed(json.dumps({"type": "stream_event", "event": {
            "type": "content_block_stop", "index": 0}}))
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["detail"], "你好世界")
        partials = [u["step"]["payload"]["partial"] for u in updates]
        self.assertIn(True, partials)
        self.assertEqual(partials[-1], False)

    def test_thinking_and_tool(self) -> None:
        parser, steps, _ = self._parser()
        parser.feed(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "想一想"},
            {"type": "tool_use", "name": "Write", "input": {"file_path": "a.md"}},
        ]}}))
        kinds = [s["kind"] for s in steps]
        self.assertEqual(kinds, ["thinking", "tool"])
        self.assertEqual(steps[1]["payload"]["tool"], "Write")

    def test_result_sets_state(self) -> None:
        parser, steps, _ = self._parser()
        parser.feed(json.dumps({"type": "result", "subtype": "success",
                                "result": "done", "session_id": "s1",
                                "total_cost_usd": 0.1}))
        self.assertTrue(parser.ok)
        self.assertEqual(parser.session_id, "s1")
        self.assertEqual(steps[0]["kind"], "result")
        self.assertAlmostEqual(parser.usage["total_cost_usd"], 0.1)

    def test_streamed_blocks_not_duplicated(self) -> None:
        parser, steps, _ = self._parser()
        parser.feed(json.dumps({"type": "stream_event", "event": {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text"}}}))
        parser.feed(json.dumps({"type": "stream_event", "event": {
            "type": "content_block_stop", "index": 0}}))
        # 之后到达的完整 assistant 不应再补一条
        parser.feed(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "完整文本"}]}}))
        self.assertEqual(len(steps), 1)

    def test_command_backend_lines(self) -> None:
        parser, steps, _ = self._parser()
        parser.feed("hello", backend="command")
        self.assertEqual(steps[0]["kind"], "output")


class ClusterStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sf-cluster-stream-")
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "state.db"
        store = open_store(self.db)
        studio_store.ensure_schema(store)
        self.cluster = studio_commands.create_cluster(
            store, name="流式集群", objective="验证角色步骤流",
            workspace_root=str(Path(self.tmp.name) / "ws"),
            lead={"name": "Lead", "backend": "command", "args": ["fake"]},
            roles=[{"name": "Worker", "backend": "command", "args": ["fake"]}],
            policy={"auto_approve": True, "max_rounds": 10, "role_timeout_seconds": 20})
        self.cid = self.cluster["id"]
        self.pid = self.cluster["project_id"]
        roles = studio_store.list_roles(store, self.cid)
        self.lead, self.worker = roles[0], roles[1]
        base.create_task(store, project_id=self.pid, title="做一件事",
                         description="写个文件", assignee_agent_id=self.worker["agent_id"],
                         requires_review=False)
        store.close()

    def test_steps_land_in_db_and_broker(self) -> None:
        """跑真实的 _run_process 泵：假可执行文件输出 stream-json，断言 step 落库。"""

        lines = [
            json.dumps({"type": "system", "session_id": "sess-1", "model": "m"}),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Write", "input": {"file_path": "x.md"}}]}}),
            json.dumps({"type": "result", "subtype": "success", "result": "ok",
                        "session_id": "sess-1"}),
        ]
        script = Path(self.tmp.name) / "emit.py"
        script.write_text(
            "import sys, json\n"
            f"lines = {lines!r}\n"
            "for line in lines:\n"
            "    print(line, flush=True)\n",
            encoding="utf-8")

        manager = SupervisorManager(self.db, interval=0.2)
        self.addCleanup(manager.shutdown)
        # 把角色换成直接跑 python 脚本，走真实的解析链路
        store = open_store(self.db)
        try:
            import sys
            studio_store.update_role(
                store, self.worker["id"], backend="command",
                args=[sys.executable, str(script)])
            studio_store.update_role(
                store, self.lead["id"], backend="command",
                args=[sys.executable, "-c", "print('{}')"])
        finally:
            store.close()

        manager.start(self.cid, kickoff=False)
        deadline = time.time() + 15
        steps: list[dict] = []
        while time.time() < deadline:
            store = open_store(self.db)
            try:
                run = studio_store.latest_run(store, self.cid)
                if run:
                    steps = studio_store.list_cluster_steps(store, run["id"])
            finally:
                store.close()
            if any(s["kind"] == "result" for s in steps):
                break
            time.sleep(0.2)
        manager.stop(self.cid)

        self.assertTrue(steps, "应该有步骤落库")
        kinds = {s["kind"] for s in steps}
        self.assertIn("tool", kinds)
        self.assertIn("result", kinds)
        # seq 按角色自增
        by_role: dict[str, list[int]] = {}
        for step in steps:
            by_role.setdefault(step["role_id"], []).append(step["seq"])
        for role_id, seqs in by_role.items():
            self.assertEqual(seqs, sorted(seqs), role_id)
            self.assertEqual(seqs[0], 1, role_id)


if __name__ == "__main__":
    unittest.main()
