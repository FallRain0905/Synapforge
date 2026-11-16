"""交付页聚合：坏 project_id 不能打挂整个列表，共用项目要去重。"""

from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

from synapforge.db import open_store
from synapforge.studio import agents as studio_agents
from synapforge.studio import store as studio_store

PROJECT = str(uuid.uuid4())
OTHER_PROJECT = str(uuid.uuid4())


class AllArtifactsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sf-artifacts-")
        self.addCleanup(self.tmp.cleanup)
        self.store = open_store(Path(self.tmp.name) / "state.db")
        studio_store.ensure_schema(self.store)
        # 只测聚合逻辑：项目/团队的外键在这里没有语义，直接造数据更省事。
        self.store.db.execute("PRAGMA foreign_keys=OFF")
        self.addCleanup(self.store.close)

    def _agent(self, agent_id: str, project_id: str, name: str = "Agent") -> None:
        self.store.db.execute(
            "INSERT INTO local_agents (id, name, workspace, project_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (agent_id, name, "w", project_id, "2026-01-01T00:00:00+00:00",
             "2026-01-01T00:00:00+00:00"))
        self.store.db.commit()

    def _artifact(self, artifact_id: str, project_id: str, name: str = "a.md") -> None:
        self.store.db.execute(
            "INSERT INTO artifacts (id, project_id, name, artifact_type, description,"
            " content_hash, version, status, created_by, created_at, data_policy)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (artifact_id, project_id, name, "agent_answer", "", "hash-" + artifact_id,
             1, "PENDING_REVIEW", "member-001", "2026-01-01T00:00:00+00:00", "{}"))
        self.store.db.commit()

    def test_invalid_project_id_is_skipped(self) -> None:
        self._agent("agent-bad", "", "坏 Agent")
        self._agent("agent-good", PROJECT, "好 Agent")
        self._artifact(str(uuid.uuid4()), PROJECT, "a.md")
        items = studio_agents.all_artifacts(self.store)
        self.assertEqual([item["name"] for item in items], ["a.md"])
        self.assertEqual(items[0]["agent_id"], "agent-good")

    def test_shared_project_deduplicates(self) -> None:
        self._agent("agent-a", PROJECT, "A")
        self._agent("agent-b", PROJECT, "B")
        self._artifact(str(uuid.uuid4()), PROJECT)
        items = studio_agents.all_artifacts(self.store)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["agent_id"], "agent-a")

    def test_other_project_is_not_mixed_in(self) -> None:
        self._agent("agent-a", PROJECT, "A")
        self._artifact(str(uuid.uuid4()), OTHER_PROJECT)
        self.assertEqual(studio_agents.all_artifacts(self.store), [])

    def test_agent_state_tolerates_bad_project_id(self) -> None:
        self._agent("agent-bad", "", "坏 Agent")
        state = studio_agents.agent_state(self.store, "agent-bad")
        self.assertEqual(state["tasks"], [])
        self.assertEqual(state["artifacts"], [])
        self.assertEqual(state["name"], "坏 Agent")


if __name__ == "__main__":
    unittest.main()
