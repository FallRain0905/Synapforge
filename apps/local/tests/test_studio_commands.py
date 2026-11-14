"""Studio 命令层：建集群、启停、发指令、复核。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import UUID

from synapforge import commands as base
from synapforge.db import open_store
from synapforge.studio import commands as studio_commands
from synapforge.studio import store as studio_store


class StudioCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sf-studio-cmd-")
        self.addCleanup(self.tmp.cleanup)
        self.store = open_store(Path(self.tmp.name) / "state.db")
        self.addCleanup(self.store.close)
        studio_store.ensure_schema(self.store)

    def _cluster(self, name: str = "测试集群") -> dict:
        return studio_commands.create_cluster(
            self.store, name=name, objective="验证命令层",
            workspace_root=str(Path(self.tmp.name) / "ws"),
            lead={"name": "Lead", "backend": "command", "args": ["python", "fake.py"],
                  "system_prompt": "lead"},
            roles=[{"name": "Worker", "backend": "command", "args": ["python", "fake.py"],
                    "system_prompt": "worker"}],
            policy={"max_rounds": 12})

    def test_create_cluster_bootstraps_project_and_roles(self) -> None:
        detail = self._cluster()
        self.assertEqual(detail["status"], "READY")
        self.assertEqual(len(detail["roles"]), 2)
        project = self.store.get_project(UUID(str(detail["project_id"])))
        self.assertIn("测试集群", project.name)

        agents = {row["agent_id"] for row in
                  self.store.db.execute("SELECT agent_id FROM agents").fetchall()}
        self.assertEqual(agents, {r["agent_id"] for r in detail["roles"]})
        grants = {row["agent_id"] for row in self.store.db.execute(
            "SELECT agent_id FROM agent_project_grants WHERE project_id = ?",
            (str(detail["project_id"]),)).fetchall()}
        self.assertEqual(grants, agents)
        for role in detail["roles"]:
            self.assertTrue(Path(role["workspace"]).exists())

    def test_start_pause_resume_stop(self) -> None:
        detail = self._cluster()
        started = studio_commands.start_cluster(self.store, detail["id"])
        self.assertTrue(started["started"])
        self.assertEqual(started["run"]["status"], "RUNNING")
        self.assertEqual(started["task"]["status"], "READY")
        self.assertEqual(started["task"]["assignee"], detail["lead_role_id"] and
                         detail["roles"][0]["agent_id"])

        paused = studio_commands.pause_cluster(self.store, detail["id"])
        self.assertTrue(paused["paused"])
        self.assertEqual(studio_commands.cluster_detail(
            self.store, detail["id"])["run"]["status"], "PAUSED")

        resumed = studio_commands.resume_cluster(self.store, detail["id"])
        self.assertTrue(resumed["resumed"])
        self.assertEqual(studio_commands.cluster_detail(
            self.store, detail["id"])["run"]["status"], "RUNNING")

        stopped = studio_commands.stop_cluster(self.store, detail["id"])
        self.assertTrue(stopped["stopped"])
        self.assertEqual(studio_commands.cluster_detail(
            self.store, detail["id"])["run"]["status"], "STOPPED")

    def test_instruction_creates_tasks_and_message(self) -> None:
        detail = self._cluster()
        result = studio_commands.send_instruction(self.store, detail["id"], "先写提纲")
        self.assertEqual(len(result["broadcast"]["tasks"]), 2)
        tasks = base.status(self.store, project_id=detail["project_id"])["tasks"]
        self.assertEqual(len(tasks), 2)
        self.assertTrue(all(t["status"] == "READY" for t in tasks))
        events = base.recent_events(self.store, project_id=detail["project_id"], limit=20)
        self.assertTrue(any(e["event_type"] == "project.message.posted" for e in events))

    def test_review_task_closes_waiting_review(self) -> None:
        detail = self._cluster()
        worker = detail["roles"][1]
        task = base.create_task(self.store, project_id=detail["project_id"],
                                title="写一个文件", description="x",
                                assignee_agent_id=worker["agent_id"],
                                requires_review=True)
        claimed = base.claim(self.store, project_id=detail["project_id"],
                             agent_id=worker["agent_id"])
        lease = claimed["lease"]["lease_token"]
        base.mark_running(self.store, task_id=task["id"], agent_id=worker["agent_id"],
                          lease_token=lease)
        finished = base.finish_task(self.store, task_id=task["id"],
                                    agent_id=worker["agent_id"], lease_token=lease,
                                    success=True, summary="done")
        self.assertEqual(finished["task"]["status"], "WAITING_REVIEW")

        reviewed = studio_commands.review_task(self.store, task["id"], "APPROVED", "通过")
        self.assertEqual(reviewed["review"]["verdict"], "APPROVED")
        self.assertEqual(reviewed["task"]["status"], "APPROVED")
        self.assertEqual(studio_commands.auto_approve_pending(self.store, detail["id"]), 0)

    def test_review_artifact_marks_approved(self) -> None:
        detail = self._cluster()
        worker = detail["roles"][1]
        task = base.create_task(self.store, project_id=detail["project_id"],
                                title="写成果物", assignee_agent_id=worker["agent_id"])
        claimed = base.claim(self.store, project_id=detail["project_id"],
                             agent_id=worker["agent_id"])
        lease = claimed["lease"]["lease_token"]
        base.mark_running(self.store, task_id=task["id"], agent_id=worker["agent_id"],
                          lease_token=lease)
        path = Path(worker["workspace"]) / "result.md"
        path.write_text("done\n", encoding="utf-8")
        artifact = base.publish_artifact(self.store, project_id=detail["project_id"],
                                         workspace=worker["workspace"], relative_path="result.md",
                                         agent_id=worker["agent_id"], task_id=task["id"])
        base.finish_task(self.store, task_id=task["id"], agent_id=worker["agent_id"],
                         lease_token=lease, success=True, summary="done",
                         output_artifact_ids=[artifact["id"]])

        reviewed = studio_commands.review_artifact(self.store, artifact["id"],
                                                   "APPROVED", "通过")
        self.assertEqual(reviewed["review"]["verdict"], "APPROVED")
        stored = self.store.get_artifact(UUID(artifact["id"]))
        self.assertEqual(stored.status, "APPROVED")

    def test_duplicate_cluster_name_rejected(self) -> None:
        self._cluster("唯一名字")
        with self.assertRaises(studio_commands.StudioError) as ctx:
            self._cluster("唯一名字")
        self.assertEqual(ctx.exception.code, "cluster_name_exists")


if __name__ == "__main__":
    unittest.main()
