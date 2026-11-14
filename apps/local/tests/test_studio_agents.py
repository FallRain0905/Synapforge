"""单 Agent 工作台：注册、角色卡、任务下发、状态。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from synapforge import commands as base
from synapforge.db import open_store
from synapforge.studio import agents as studio_agents
from synapforge.studio import store as studio_store


class StudioAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sf-studio-agent-")
        self.addCleanup(self.tmp.cleanup)
        self.store = open_store(Path(self.tmp.name) / "state.db")
        self.addCleanup(self.store.close)
        studio_store.ensure_schema(self.store)

    def test_create_agent_bootstraps_project_workspace_and_grants(self) -> None:
        agent = studio_agents.create_agent(
            self.store, name="Writer", model="glm-5.3",
            system_prompt="你是技术写手", workspace=str(Path(self.tmp.name) / "ws"))
        self.assertEqual(agent["name"], "Writer")
        self.assertTrue(Path(agent["workspace"]).exists())
        self.assertIn("Writer", agent["project_name"])
        project = self.store.get_project(agent["project_id"])
        self.assertEqual(project.name, agent["project_name"])
        grants = self.store.db.execute(
            "SELECT COUNT(*) AS c FROM agent_project_grants WHERE agent_id = ? AND project_id = ?",
            (agent["id"], agent["project_id"])).fetchone()
        self.assertEqual(grants["c"], 1)

    def test_duplicate_agent_name_rejected(self) -> None:
        studio_agents.create_agent(self.store, name="Writer", workspace=str(Path(self.tmp.name) / "dup-a"))
        with self.assertRaises(studio_agents.StudioError) as ctx:
            studio_agents.create_agent(self.store, name="Writer", workspace=str(Path(self.tmp.name) / "dup-b"))
        self.assertEqual(ctx.exception.code, "agent_name_exists")

    def test_start_task_creates_run_and_assigned_task(self) -> None:
        agent = studio_agents.create_agent(self.store, name="Writer", model="glm-5.3", workspace=str(Path(self.tmp.name) / "task-ws"))
        result = studio_agents.start_task(self.store, agent["id"], "写一个 README.md")
        self.assertEqual(result["run"]["status"], "QUEUED")
        self.assertEqual(result["queue_position"], 1)
        self.assertEqual(result["run"]["agent_id"], agent["id"])
        self.assertEqual(result["task"]["assignee"], agent["id"])
        self.assertEqual(result["task"]["status"], "READY")
        run = studio_store.latest_agent_run(self.store, agent["id"])
        self.assertEqual(run["task_text"], "写一个 README.md")

        state = studio_agents.agent_state(self.store, agent["id"])
        self.assertEqual(state["id"], agent["id"])
        self.assertEqual(state["steps"], [])
        self.assertEqual(state["tasks"][0]["id"], result["task"]["id"])
        self.assertEqual(len(state["queue"]), 1)
        self.assertIn("models", state)

    def test_second_task_is_queued(self) -> None:
        agent = studio_agents.create_agent(self.store, name="Writer", workspace=str(Path(self.tmp.name) / "busy-ws"))
        first = studio_agents.start_task(self.store, agent["id"], "第一个任务")
        second = studio_agents.start_task(self.store, agent["id"], "第二个任务")
        self.assertEqual(first["run"]["status"], "QUEUED")
        self.assertEqual(second["run"]["status"], "QUEUED")
        self.assertEqual(studio_store.queue_position(self.store, agent["id"]), 2)

    def test_retry_creates_new_queued_run(self) -> None:
        agent = studio_agents.create_agent(self.store, name="Writer", workspace=str(Path(self.tmp.name) / "retry-ws"))
        first = studio_agents.start_task(self.store, agent["id"], "写一个文件")
        studio_store.update_agent_run(self.store, first["run"]["id"], status="FAILED")
        retried = studio_agents.retry_run(self.store, first["run"]["id"])
        self.assertEqual(retried["run"]["status"], "QUEUED")
        self.assertEqual(retried["run"]["task_text"], "写一个文件")
        self.assertNotEqual(retried["run"]["id"], first["run"]["id"])

    def test_new_session_clears_agent_session(self) -> None:
        agent = studio_agents.create_agent(self.store, name="Writer", workspace=str(Path(self.tmp.name) / "sess-ws"))
        studio_agents.update_agent(self.store, agent["id"], session_id="session-1")
        cleared = studio_agents.new_session(self.store, agent["id"])
        self.assertEqual(cleared["session_id"], "")

    def test_worker_can_be_reused_after_finishing(self) -> None:
        """Thread._stop 是 Thread 自己的方法；worker 不能把它覆盖成 Event。"""

        import threading

        from synapforge.studio.agent_runtime import AgentWorker, RunBroker

        agent = studio_agents.create_agent(
            self.store, name="Reuse", workspace=str(Path(self.tmp.name) / "reuse-ws"))
        worker = AgentWorker(agent["id"], Path(self.tmp.name) / "state.db", RunBroker())
        self.assertTrue(callable(getattr(threading.Thread, "_stop")))
        self.assertIsInstance(worker._halt, threading.Event)
        worker.start()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())

    def test_update_agent_and_model_options(self) -> None:
        agent = studio_agents.create_agent(self.store, name="Writer", workspace=str(Path(self.tmp.name) / "update-ws"))
        self.assertTrue(agent["allow_web"])
        updated = studio_agents.update_agent(self.store, agent["id"],
                                             model="glm-5.3-flash",
                                             system_prompt="新角色卡",
                                             allow_web=False)
        self.assertEqual(updated["model"], "glm-5.3-flash")
        self.assertEqual(updated["system_prompt"], "新角色卡")
        self.assertFalse(updated["allow_web"])
        models = studio_agents.model_options()
        self.assertTrue(models["models"])
        self.assertTrue(models["default"])


if __name__ == "__main__":
    unittest.main()
