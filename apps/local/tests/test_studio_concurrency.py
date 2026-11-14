"""集群并发：多个 worker 同时跑、并发上限、单角色失败隔离。"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from synapforge import commands as base
from synapforge.db import open_store
from synapforge.studio import commands as studio_commands
from synapforge.studio import store as studio_store
from synapforge.studio.supervisor import SupervisorManager


class ClusterConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sf-cluster-conc-")
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "state.db"
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.seen: list[str] = []
        self.fail_role = ""
        self.project_id = ""

    def _cluster(self, workers: int, *, parallel: int) -> dict:
        store = open_store(self.db)
        try:
            studio_store.ensure_schema(store)
            specs = [{"name": f"Worker{i}", "backend": "command", "args": ["fake"],
                      "system_prompt": "worker"} for i in range(workers)]
            cluster = studio_commands.create_cluster(
                store, name=f"并发{workers}-{parallel}", objective="验证并发派发",
                workspace_root=str(Path(self.tmp.name) / f"ws-{workers}-{parallel}"),
                lead={"name": "Lead", "backend": "command", "args": ["fake"],
                      "system_prompt": "lead"},
                roles=specs,
                policy={"auto_approve": True, "max_rounds": 200,
                        "role_timeout_seconds": 20,
                        "max_parallel_workers": parallel})
            self.project_id = cluster["project_id"]
            return cluster
        finally:
            store.close()

    def _runner(self, role, invocation, log_path, store):
        """每个 worker 处理自己的任务；用 sleep 把并发窗口撑开。"""

        name = str(role.get("name") or "")
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.seen.append(name)
        try:
            if name == self.fail_role:
                time.sleep(0.4)
                return 1, "intentional failure"
            time.sleep(0.5)
            box = base.inbox(store, agent_id=role["agent_id"],
                             project_id=self.project_id)
            for item in box.get("to_answer") or []:
                base.answer(store, request_id=item["request_id"],
                            agent_id=role["agent_id"],
                            response={"status": "answered", "answer_summary": "ok",
                                      "facts": [], "artifact_refs": [],
                                      "evidence_refs": [], "assumptions": []})
                return 0, "answered"
            for item in box.get("to_consume") or []:
                base.consume(store, request_id=item["request_id"],
                             agent_id=role["agent_id"])
                return 0, "consumed"
            if box.get("assigned_to_me"):
                claimed = base.claim(store, project_id=self.project_id,
                                     agent_id=role["agent_id"])
                if claimed.get("claimed"):
                    task, lease = claimed["task"], claimed["lease"]["lease_token"]
                    base.mark_running(store, task_id=task["id"],
                                      agent_id=role["agent_id"], lease_token=lease)
                    base.finish_task(store, task_id=task["id"],
                                     agent_id=role["agent_id"], lease_token=lease,
                                     success=True, summary=f"{name} done")
            return 0, "ok"
        finally:
            with self.lock:
                self.active -= 1

    def _seed_worker_tasks(self, cluster: dict, count: int) -> None:
        store = open_store(self.db)
        try:
            roles = studio_store.list_roles(store, cluster["id"])
            for index, role in enumerate(roles[1:1 + count]):
                base.create_task(
                    store, project_id=cluster["project_id"],
                    title=f"任务 {index}", description=f"worker {index} 的活",
                    assignee_agent_id=role["agent_id"], requires_review=False)
        finally:
            store.close()

    def _drive(self, cluster: dict, seconds: float = 6.0) -> None:
        manager = SupervisorManager(self.db, interval=0.2, runner=self._runner)
        self.addCleanup(manager.shutdown)
        manager.start(cluster["id"])
        deadline = time.time() + seconds
        while time.time() < deadline:
            store = open_store(self.db)
            try:
                run = studio_store.latest_run(store, cluster["id"])
            finally:
                store.close()
            if run is None or run["status"] != "RUNNING":
                break
            time.sleep(0.1)
        manager.stop(cluster["id"])

    def test_workers_run_concurrently(self) -> None:
        cluster = self._cluster(3, parallel=3)
        self._seed_worker_tasks(cluster, 3)
        self._drive(cluster, seconds=8.0)
        self.assertGreaterEqual(self.peak, 2, f"peak={self.peak} seen={self.seen}")
        self.assertGreaterEqual(len(set(self.seen)), 3, self.seen)

    def test_parallel_limit_one_is_serial(self) -> None:
        cluster = self._cluster(3, parallel=1)
        self._seed_worker_tasks(cluster, 3)
        self._drive(cluster, seconds=10.0)
        self.assertEqual(self.peak, 1, f"peak={self.peak} seen={self.seen}")

    def test_single_role_failure_does_not_pause_cluster(self) -> None:
        cluster = self._cluster(2, parallel=2)
        self.fail_role = "Worker0"
        self._seed_worker_tasks(cluster, 2)
        self._drive(cluster, seconds=8.0)
        store = open_store(self.db)
        try:
            run = studio_store.latest_run(store, cluster["id"])
            roles = studio_store.list_roles(store, cluster["id"])
        finally:
            store.close()
        self.assertIn("Worker1", self.seen)
        checkpoint = (run or {}).get("checkpoint") or {}
        blocked = checkpoint.get("blocked_roles") or {}
        self.assertIn(roles[1]["id"], blocked)
        self.assertIsNone(checkpoint.get("paused_reason"),
                          f"不该因单角色失败暂停：{checkpoint.get('paused_reason')}")


if __name__ == "__main__":
    unittest.main()
