"""会话聚合、重命名、激活与导出。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from synapforge.db import open_store
from synapforge.studio import agents as studio_agents
from synapforge.studio import sessions as studio_sessions
from synapforge.studio import store as studio_store


class SessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sf-studio-session-")
        self.addCleanup(self.tmp.cleanup)
        self.store = open_store(Path(self.tmp.name) / "state.db")
        self.addCleanup(self.store.close)
        studio_store.ensure_schema(self.store)
        self.agent = studio_agents.create_agent(
            self.store, name="Writer",
            workspace=str(Path(self.tmp.name) / "ws"))
        self.task = None

    def _run(self, text: str, session_id: str, status: str = "DONE",
             cost: float = 0.1, tokens: int = 100) -> dict:
        project_id = self.agent["project_id"]
        run = studio_store.create_agent_run(
            self.store, agent_id=self.agent["id"], project_id=project_id,
            task_id="", task_text=text, model="glm-5.3",
            workspace=self.agent["workspace"], status="QUEUED")
        studio_store.update_agent_run(
            self.store, run["id"], status=status, session_id=session_id,
            finished_at=studio_store.now_iso(),
            usage={"input_tokens": tokens, "total_cost_usd": cost})
        studio_store.append_agent_step(
            self.store, run_id=run["id"], kind="assistant", title="Assistant",
            detail=f"完成 {text}")
        return studio_store.get_agent_run(self.store, run["id"])

    def test_list_groups_runs_by_session(self) -> None:
        self._run("第一件事", "sess-a")
        self._run("第二件事", "sess-a")
        self._run("另一段", "sess-b")
        items = studio_sessions.list_sessions(self.store)
        self.assertEqual(len(items), 2)
        by_id = {item["id"]: item for item in items}
        self.assertEqual(by_id["sess-a"]["runs"], 2)
        self.assertEqual(by_id["sess-a"]["tokens"], 200)
        self.assertAlmostEqual(by_id["sess-a"]["cost"], 0.2)
        self.assertEqual(by_id["sess-a"]["agent_name"], "Writer")
        self.assertEqual(by_id["sess-b"]["runs"], 1)

    def test_search_rename_and_archive(self) -> None:
        self._run("写一份报告", "sess-c")
        found = studio_sessions.list_sessions(self.store, query="报告")
        self.assertEqual(len(found), 1)
        renamed = studio_sessions.rename_session(self.store, "sess-c", "报告会话")
        self.assertEqual(renamed["title"], "报告会话")
        found = studio_sessions.list_sessions(self.store, query="报告会话")
        self.assertEqual(found[0]["id"], "sess-c")
        studio_sessions.toggle_archive(self.store, "sess-c", True)
        self.assertEqual(studio_sessions.list_sessions(self.store), [])
        self.assertEqual(len(studio_sessions.list_sessions(
            self.store, include_archived=True)), 1)

    def test_detail_and_export(self) -> None:
        self._run("写 README", "sess-d")
        detail = studio_sessions.session_detail(self.store, "sess-d")
        self.assertEqual(detail["session"]["runs"], 1)
        self.assertEqual(detail["runs"][0]["steps"][0]["title"], "Assistant")
        filename, markdown = studio_sessions.export_markdown(self.store, "sess-d")
        self.assertTrue(filename.endswith(".md"))
        self.assertIn("写 README", markdown)
        self.assertIn("完成 写 README", markdown)

    def test_activate_sets_agent_session(self) -> None:
        self._run("继续之前的事", "sess-e")
        agent = studio_sessions.activate_session(self.store, "sess-e")
        self.assertEqual(agent["session_id"], "sess-e")

    def test_empty_session_runs_are_ignored(self) -> None:
        project_id = self.agent["project_id"]
        studio_store.create_agent_run(
            self.store, agent_id=self.agent["id"], project_id=project_id,
            task_id="", task_text="命令行任务", model="",
            workspace=self.agent["workspace"], status="DONE")
        self.assertEqual(studio_sessions.list_sessions(self.store), [])


if __name__ == "__main__":
    unittest.main()
