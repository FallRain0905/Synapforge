"""Studio 数据层：集群/角色/运行/进程的持久化。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from synapforge.db import open_store
from synapforge.studio import store as studio_store


class StudioStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sf-studio-store-")
        self.addCleanup(self.tmp.cleanup)
        self.store = open_store(Path(self.tmp.name) / "state.db")
        self.addCleanup(self.store.close)
        studio_store.ensure_schema(self.store)

    def test_cluster_and_roles_roundtrip(self) -> None:
        cluster = studio_store.create_cluster(
            self.store, project_id="project-1", name="数模集群",
            objective="完成四问", workspace_root=self.tmp.name,
            policy={"max_rounds": 10})
        self.assertEqual(cluster["status"], "READY")
        self.assertEqual(cluster["policy"]["max_rounds"], 10)

        lead = studio_store.add_role(
            self.store, cluster_id=cluster["id"], name="总编", agent_id="agent-lead",
            backend="claude", model="claude-sonnet-4-5", args=["--max-turns", "8"],
            system_prompt="你是 lead", workspace=self.tmp.name,
            capabilities=["planning"], budget={"timeout_seconds": 60}, sort_order=0)
        worker = studio_store.add_role(
            self.store, cluster_id=cluster["id"], name="写手", agent_id="agent-writer",
            backend="command", args=["python fake.py"], workspace=self.tmp.name,
            sort_order=1)
        studio_store.update_cluster(self.store, cluster["id"], lead_role_id=lead["id"])

        stored = studio_store.get_cluster(self.store, cluster["id"])
        self.assertEqual(stored["lead_role_id"], lead["id"])
        roles = studio_store.list_roles(self.store, cluster["id"])
        self.assertEqual([r["name"] for r in roles], ["总编", "写手"])
        self.assertEqual(roles[0]["args"], ["--max-turns", "8"])
        self.assertEqual(roles[0]["capabilities"], ["planning"])
        self.assertEqual(roles[1]["backend"], "command")
        self.assertFalse(roles[1]["requires_review"])

        updated = studio_store.update_role(self.store, worker["id"], name="写手B",
                                           requires_review=True)
        self.assertEqual(updated["name"], "写手B")
        self.assertTrue(updated["requires_review"])

    def test_run_and_process_lifecycle(self) -> None:
        cluster = studio_store.create_cluster(
            self.store, project_id="project-1", name="运行测试",
            objective="验证", workspace_root=self.tmp.name)
        role = studio_store.add_role(
            self.store, cluster_id=cluster["id"], name="Lead", agent_id="agent-lead",
            backend="claude", args=[], workspace=self.tmp.name)

        run = studio_store.create_run(self.store, cluster["id"])
        self.assertEqual(run["status"], "RUNNING")
        self.assertEqual(studio_store.latest_run(self.store, cluster["id"])["id"], run["id"])

        studio_store.update_run(self.store, run["id"], round=3,
                                checkpoint={"last_role": "Lead"},
                                budget_used={"invocations": {role["id"]: 2}})
        stored = studio_store.get_run(self.store, run["id"])
        self.assertEqual(stored["round"], 3)
        self.assertEqual(stored["checkpoint"]["last_role"], "Lead")
        self.assertEqual(stored["budget_used"]["invocations"][role["id"]], 2)

        process = studio_store.create_process(
            self.store, run_id=run["id"], role_id=role["id"], pid=None,
            command=["claude", "-p", "hi"], log_path=str(Path(self.tmp.name) / "a.log"))
        self.assertEqual(process["status"], "RUNNING")
        studio_store.update_process_pid(self.store, process["id"], 4321)
        self.assertEqual(studio_store.latest_process_for_role(self.store, role["id"])["pid"], 4321)
        studio_store.finish_process(self.store, process["id"], status="EXITED", exit_code=0)
        self.assertEqual(studio_store.list_processes(self.store, run["id"])[0]["exit_code"], 0)

        studio_store.delete_cluster(self.store, cluster["id"])
        self.assertIsNone(studio_store.get_cluster(self.store, cluster["id"]))
        self.assertEqual(studio_store.list_roles(self.store, cluster["id"]), [])


if __name__ == "__main__":
    unittest.main()
