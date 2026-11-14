"""本地查看器测试（只读投影）。

重点：
- 群聊只列**本项目参与者**，不把别的项目的 Agent 混进来；
- 提问/回答要带"谁对谁说"，否则读不出一场对话；
- 机械事件（内容落盘、播种）不进对话流；
- **只读连接物理上不能写**（护栏一）。
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from synapforge import commands, viewer
from synapforge.db import open_readonly_connection, open_store


class GroupStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="synapforge-view-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = self.tmp / "state.db"
        store = open_store(self.db)
        self.pid = commands.ensure_project(store, "viewer-proj")["id"]
        self.a, self.b = "agent-front", "agent-back"
        for agent_id in (self.a, self.b):
            commands.ensure_participant(store, project_id=self.pid, agent_id=agent_id)
        # 另一个项目 + Agent：不该出现在本项目的群聊里
        other = commands.ensure_project(store, "other-proj")["id"]
        commands.ensure_participant(store, project_id=other, agent_id="agent-outsider")

        task = commands.create_task(store, project_id=self.pid, title="联调任务",
                                    assignee_agent_id=self.a)
        lease = commands.claim(store, project_id=self.pid, agent_id=self.a)
        commands.mark_running(store, task_id=task["id"], agent_id=self.a,
                              lease_token=lease["lease"]["lease_token"])
        asked = commands.ask(store, project_id=self.pid, agent_id=self.a,
                             provider_agent_id=self.b, request_type="interface",
                             question="接口字段叫什么？", reason="不知道就没法写",
                             task_id=task["id"])
        commands.answer(store, request_id=asked["request_id"], agent_id=self.b,
                        response={"status": "answered", "answer_summary": "字段是 title/status",
                                  "facts": [], "artifact_refs": [], "evidence_refs": [],
                                  "assumptions": []})
        self.task_id = str(task["id"])
        store.close()

    def _group(self) -> dict:
        conn = open_readonly_connection(self.db)
        try:
            return viewer.group_stream(conn, self.pid, 200)
        finally:
            conn.close()

    def test_only_project_participants_are_listed(self) -> None:
        data = self._group()
        self.assertEqual({a["agent_id"] for a in data["agents"]}, {self.a, self.b})

    def test_ask_and_answer_carry_direction(self) -> None:
        events = self._group()["events"]
        asked = [e for e in events if e["event_type"] == "project.information_request.created"]
        answered = [e for e in events if e["event_type"] == "project.information_request.answered"]
        self.assertEqual(len(asked), 1)
        self.assertEqual(asked[0]["from_name"], self.a)
        self.assertEqual(asked[0]["to_name"], self.b)
        self.assertEqual(len(answered), 1)
        self.assertEqual(answered[0]["from_name"], self.b)
        self.assertEqual(answered[0]["to_name"], self.a, "回答方与被回答方都要能读出来")
        self.assertIn("字段是", answered[0]["text"])

    def test_mechanical_events_are_filtered_out(self) -> None:
        types = {e["event_type"] for e in self._group()["events"]}
        self.assertNotIn("artifact.content_stored", types)
        self.assertNotIn("project.seeded", types)

    def test_task_title_is_resolved(self) -> None:
        claimed = [e for e in self._group()["events"] if e["event_type"] == "task.claimed"]
        self.assertTrue(claimed)
        self.assertEqual(claimed[0]["subject"], "联调任务")


class ReadOnlyGuardTests(unittest.TestCase):
    def test_readonly_connection_rejects_writes(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="synapforge-ro-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        db = tmp / "state.db"
        open_store(db).close()

        conn = open_readonly_connection(db)
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("INSERT INTO projects (id, name, created_at) VALUES ('x','x','x')")

    def test_agent_stream_reports_assigned_tasks(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="synapforge-ro2-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        db = tmp / "state.db"
        store = open_store(db)
        pid = commands.ensure_project(store, "ro-proj")["id"]
        commands.ensure_participant(store, project_id=pid, agent_id="agent-x")
        commands.create_task(store, project_id=pid, title="给 x 的活", assignee_agent_id="agent-x")
        store.close()

        conn = open_readonly_connection(db)
        self.addCleanup(conn.close)
        data = viewer.agent_stream(conn, "agent-x", 50)
        self.assertEqual([t["title"] for t in data["assigned_to_me"]], ["给 x 的活"])
        self.assertEqual(data["member_id"], commands.agent_member_id("agent-x"))


class ViewerAssetTests(unittest.TestCase):
    """静态资源本身的回归。

    踩过的坑：`const initial` 与 `function initial()` 同名 → 整段脚本语法错误 →
    页面完全白屏、控制台之外没有任何提示，而且 HTTP 依然 200。这种问题必须机械检查。
    """

    def test_viewer_js_is_syntactically_valid(self) -> None:
        html = (viewer.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        match = re.search(r"<script>(.*?)</script>", html, re.S)
        self.assertIsNotNone(match, "index.html 里没有 <script> 块")
        node = shutil.which("node")
        if not node:
            self.skipTest("node 不可用，跳过语法检查")
        tmp = Path(tempfile.mkdtemp(prefix="synapforge-js-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        target = tmp / "check.js"
        target.write_text(match.group(1), encoding="utf-8")
        result = subprocess.run([node, "--check", str(target)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_viewer_page_is_served(self) -> None:
        html = (viewer.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        for marker in ("/api/agents", "/api/group/stream", "__group__"):
            self.assertIn(marker, html, f"页面缺少 {marker} 相关逻辑")


if __name__ == "__main__":
    unittest.main()
