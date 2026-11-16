"""桌面通知流：游标语义、终态过滤、limit 分页、Agent 名称。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from synapforge.db import open_store
from synapforge.studio import notifications
from synapforge.studio import store as studio_store

PAST = "2000-01-01T00:00:00+00:00"


class NotificationFeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sf-notify-")
        self.addCleanup(self.tmp.cleanup)
        self.store = open_store(Path(self.tmp.name) / "state.db")
        studio_store.ensure_schema(self.store)
        self.addCleanup(self.store.close)

    def _agent(self, name: str = "写手") -> None:
        self.store.db.execute(
            "INSERT INTO local_agents (id, name, workspace, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            ("a1", name, "w", PAST, PAST))
        self.store.db.commit()

    def _run(self, run_id: str, status: str, finished_at: str,
             text: str = "写一段说明") -> None:
        self.store.db.execute(
            "INSERT INTO local_agent_runs (id, agent_id, project_id, task_text,"
            " status, workspace, started_at, finished_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, "a1", "p1", text, status, "w", PAST, finished_at))
        self.store.db.commit()

    def _cluster(self, cluster_id: str, status: str, updated_at: str,
                 name: str = "演示集群") -> None:
        self.store.db.execute(
            "INSERT INTO local_clusters (id, project_id, name, objective,"
            " workspace_root, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (cluster_id, "p1", name, "", "w", status, PAST, updated_at))
        self.store.db.commit()

    def test_first_poll_is_only_a_cursor(self) -> None:
        payload = notifications.recent_events(self.store)
        self.assertTrue(payload["cursor"])
        self.assertEqual(payload["items"], [])

    def test_run_and_cluster_after_cursor(self) -> None:
        self._agent()
        self._run("r1", "DONE", "2026-11-16T04:00:00+00:00")
        self._cluster("c1", "DONE", "2026-11-16T04:00:01+00:00")
        payload = notifications.recent_events(self.store, since=PAST)
        self.assertEqual([i["kind"] for i in payload["items"]], ["run", "cluster"])
        self.assertEqual(payload["items"][0]["agent_name"], "写手")
        self.assertEqual(payload["cursor"], "2026-11-16T04:00:01+00:00")

    def test_cursor_is_exclusive(self) -> None:
        self._run("r1", "DONE", "2026-11-16T04:00:00+00:00")
        first = notifications.recent_events(self.store, since=PAST)
        self.assertEqual(len(first["items"]), 1)
        second = notifications.recent_events(self.store, since=first["cursor"])
        self.assertEqual(second["items"], [])

    def test_running_and_paused_are_ignored(self) -> None:
        self._run("r1", "RUNNING", "2026-11-16T04:00:00+00:00")
        self._cluster("c1", "PAUSED", "2026-11-16T04:00:00+00:00")
        payload = notifications.recent_events(self.store, since=PAST)
        self.assertEqual(payload["items"], [])

    def test_limit_and_next_page(self) -> None:
        for index in range(3):
            self._run(f"r{index}", "DONE", f"2026-11-16T04:00:0{index}+00:00",
                      text=f"任务 {index}")
        first = notifications.recent_events(self.store, since=PAST, limit=2)
        self.assertEqual([i["id"] for i in first["items"]], ["r0", "r1"])
        second = notifications.recent_events(self.store, since=first["cursor"], limit=2)
        self.assertEqual([i["id"] for i in second["items"]], ["r2"])

    def test_blank_title_falls_back(self) -> None:
        self._run("r1", "DONE", "2026-11-16T04:00:00+00:00", text="   ")
        payload = notifications.recent_events(self.store, since=PAST)
        self.assertEqual(payload["items"][0]["title"], "任务")


if __name__ == "__main__":
    unittest.main()
