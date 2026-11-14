"""Studio 调度器：假后端跑通 lead-worker 全流程与暂停。"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from synapforge import commands as base
from synapforge.db import open_store
from synapforge.studio import commands as studio_commands
from synapforge.studio import store as studio_store
from synapforge.studio.supervisor import SupervisorManager


class StudioSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sf-studio-sup-")
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "state.db"
        store = open_store(self.db)
        studio_store.ensure_schema(store)
        self.cluster = studio_commands.create_cluster(
            store, name="假后端集群", objective="验证 lead-worker 全流程",
            workspace_root=str(Path(self.tmp.name) / "ws"),
            lead={"name": "Lead", "backend": "command", "args": ["fake"],
                  "system_prompt": "lead"},
            roles=[{"name": "Worker", "backend": "command", "args": ["fake"],
                    "system_prompt": "worker"}],
            policy={"auto_approve": True, "max_rounds": 40, "role_timeout_seconds": 30})
        self.cid = self.cluster["id"]
        self.pid = self.cluster["project_id"]
        roles = studio_store.list_roles(store, self.cid)
        self.lead, self.worker = roles[0], roles[1]
        store.close()

    def test_fake_backend_full_collaboration(self) -> None:
        def runner(role, invocation, log_path, store):
            box = base.inbox(store, agent_id=role["agent_id"], project_id=self.pid)
            for item in box.get("to_answer") or []:
                base.answer(store, request_id=item["request_id"], agent_id=role["agent_id"],
                            response={"status": "answered", "answer_summary": "字段名用 title",
                                      "facts": [{"key": "field", "value": "title"}],
                                      "artifact_refs": [], "evidence_refs": [],
                                      "assumptions": []})
                return 0, "answered"
            for item in box.get("to_consume") or []:
                base.consume(store, request_id=item["request_id"], agent_id=role["agent_id"])
                return 0, "consumed"

            if role["id"] == self.lead["id"]:
                if not box.get("assigned_to_me"):
                    return 0, "lead idle"
                claimed = base.claim(store, project_id=self.pid, agent_id=role["agent_id"])
                if not claimed.get("claimed"):
                    return 0, "lead claim failed"
                task, lease = claimed["task"], claimed["lease"]["lease_token"]
                base.mark_running(store, task_id=task["id"], agent_id=role["agent_id"],
                                  lease_token=lease)
                if str(task.get("stage")) == "kickoff":
                    base.create_task(store, project_id=self.pid, title="写一句简介",
                                     description="写一句简介并落成 intro.md",
                                     assignee_agent_id=self.worker["agent_id"],
                                     requires_review=True)
                base.finish_task(store, task_id=task["id"], agent_id=role["agent_id"],
                                 lease_token=lease, success=True, summary="lead done")
                return 0, "lead done"

            if box.get("assigned_to_me"):
                claimed = base.claim(store, project_id=self.pid, agent_id=role["agent_id"])
                if not claimed.get("claimed"):
                    return 0, "worker claim failed"
                task, lease = claimed["task"], claimed["lease"]["lease_token"]
                base.mark_running(store, task_id=task["id"], agent_id=role["agent_id"],
                                  lease_token=lease)
                base.ask(store, project_id=self.pid, agent_id=role["agent_id"],
                         provider_agent_id=self.lead["agent_id"], request_type="fact",
                         question="字段名用什么？", reason="没有字段名无法写文件",
                         task_id=task["id"])
                return 0, "worker asked"
            if box.get("in_flight_tasks"):
                task = box["in_flight_tasks"][0]
                path = Path(role["workspace"]) / "intro.md"
                path.write_text("synapforge studio 是一个本地 Agent 集群工具。\n",
                                encoding="utf-8")
                artifact = base.publish_artifact(
                    store, project_id=self.pid, workspace=role["workspace"],
                    relative_path="intro.md", agent_id=role["agent_id"],
                    task_id=task["id"])
                base.finish_task(store, task_id=task["id"], agent_id=role["agent_id"],
                                 lease_token=task["lease_token"], success=True,
                                 summary="worker done", output_artifact_ids=[artifact["id"]])
                return 0, "worker done"
            return 0, "worker idle"

        manager = SupervisorManager(self.db, interval=0.05, runner=runner)
        self.addCleanup(manager.shutdown)
        self.assertTrue(manager.start(self.cid).get("started"))

        status = ""
        for _ in range(400):
            store = open_store(self.db)
            run = studio_store.latest_run(store, self.cid)
            status = run["status"] if run else ""
            store.close()
            if status in ("DONE", "PAUSED", "STOPPED"):
                break
            time.sleep(0.05)

        store = open_store(self.db)
        self.addCleanup(store.close)
        run = studio_store.latest_run(store, self.cid)
        self.assertEqual(status, "DONE", f"run checkpoint={run.get('checkpoint') if run else None}")
        tasks = {t.title: t.status for t in store.list_tasks(self.pid)}
        self.assertEqual(tasks["拆解目标并派活"], "APPROVED")
        self.assertEqual(tasks["写一句简介"], "APPROVED")
        self.assertEqual(tasks["集群终审"], "APPROVED")
        artifacts = base.list_artifacts(store, project_id=self.pid)
        self.assertEqual([a["name"] for a in artifacts], ["intro.md"])
        events = [e["event_type"] for e in
                  base.recent_events(store, project_id=self.pid, limit=80)]
        self.assertIn("project.information_request.created", events)
        self.assertIn("project.information_request.answered", events)
        self.assertIn("project.information_request.consumed", events)

    def test_pause_stops_scheduling(self) -> None:
        def slow_runner(role, invocation, log_path, store):
            time.sleep(0.3)
            return 0, "slow"

        manager = SupervisorManager(self.db, interval=0.05, runner=slow_runner)
        self.addCleanup(manager.shutdown)
        manager.start(self.cid)
        time.sleep(0.1)
        manager.pause(self.cid)

        for _ in range(100):
            store = open_store(self.db)
            run = studio_store.latest_run(store, self.cid)
            status = run["status"] if run else ""
            store.close()
            if status == "PAUSED":
                break
            time.sleep(0.05)
        self.assertEqual(status, "PAUSED")


if __name__ == "__main__":
    unittest.main()
