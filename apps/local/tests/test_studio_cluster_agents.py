"""集群角色复用已有 Agent：继承配置、本轮职责、同一 Agent 进多个集群。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from synapforge.db import open_store
from synapforge.studio import agents as studio_agents
from synapforge.studio import commands as studio_commands
from synapforge.studio import store as studio_store


class ClusterAgentReuseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sf-cluster-agent-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = open_store(self.root / "state.db")
        self.addCleanup(self.store.close)
        studio_store.ensure_schema(self.store)
        self.writer = studio_agents.create_agent(
            self.store, name="Writer", model="auto/glm-5.3-flash",
            system_prompt="你是技术写手，先给结论。",
            workspace=str(self.root / "writer-ws"))
        studio_store.update_agent(
            self.store, self.writer["id"], skills=["agents:image-gen"],
            mcp=["mcp-abc"], mcp_managed=True, plugins=["plugin-x"])
        self.worker = studio_agents.create_agent(
            self.store, name="Researcher", model="glm-5.3",
            workspace=str(self.root / "researcher-ws"))

    def _cluster(self, name: str) -> dict:
        return studio_commands.create_cluster(
            self.store, name=name, objective="验证角色复用",
            workspace_root=str(self.root / f"cluster-{name}"),
            lead={"agent_id": self.writer["id"]},
            roles=[{"agent_id": self.worker["id"],
                    "role_prompt": "只负责检索与事实核对"}])

    def test_role_inherits_agent_config(self) -> None:
        detail = self._cluster("复用")
        lead, worker = detail["roles"][0], detail["roles"][1]
        self.assertEqual(lead["name"], "Writer")
        self.assertEqual(lead["model"], "auto/glm-5.3-flash")
        self.assertEqual(lead["local_agent_id"], self.writer["id"])
        self.assertEqual(lead["agent_name"], "Writer")
        self.assertEqual(lead["backend"], "claude")
        self.assertIn("先给结论", lead["system_prompt"])
        budget = lead["budget"]
        self.assertEqual(budget["skills"], ["agents:image-gen"])
        self.assertEqual(budget["mcp"], ["mcp-abc"])
        self.assertEqual(budget["plugins"], ["plugin-x"])
        self.assertTrue(budget["mcp_managed"])

    def test_role_prompt_appended_to_role_card(self) -> None:
        detail = self._cluster("职责")
        worker = detail["roles"][1]
        self.assertEqual(worker["name"], "Researcher")
        self.assertEqual(worker["role_prompt"], "只负责检索与事实核对")
        self.assertIn("本轮职责：只负责检索与事实核对", worker["system_prompt"])

    def test_same_agent_in_two_clusters(self) -> None:
        first = self._cluster("集群一")
        second = self._cluster("集群二")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["roles"][0]["local_agent_id"], self.writer["id"])
        self.assertEqual(second["roles"][0]["local_agent_id"], self.writer["id"])
        # 协议身份仍然唯一
        self.assertNotEqual(first["roles"][0]["agent_id"], second["roles"][0]["agent_id"])

    def test_role_workspace_stays_inside_cluster_root(self) -> None:
        detail = self._cluster("工作区")
        root = Path(detail["workspace_root"]).resolve()
        for role in detail["roles"]:
            workspace = Path(role["workspace"]).resolve()
            self.assertEqual(workspace.parent, root)
            self.assertNotEqual(workspace, Path(self.writer["workspace"]).resolve())

    def test_add_role_from_agent(self) -> None:
        detail = self._cluster("追加")
        third = studio_agents.create_agent(
            self.store, name="Reviewer", workspace=str(self.root / "reviewer-ws"))
        role = studio_commands.add_role(
            self.store, detail["id"], agent_id=third["id"],
            role_prompt="只做终审")
        self.assertEqual(role["name"], "Reviewer")
        self.assertEqual(role["local_agent_id"], third["id"])
        refreshed = studio_commands.cluster_detail(self.store, detail["id"])
        self.assertEqual(len(refreshed["roles"]), 3)
        self.assertEqual(refreshed["roles"][2]["role_prompt"], "只做终审")

    def test_missing_agent_rejected(self) -> None:
        with self.assertRaises(studio_commands.StudioError) as ctx:
            studio_commands.create_cluster(
                self.store, name="缺 Agent", objective="验证错误",
                workspace_root=str(self.root / "cluster-missing"),
                lead={"agent_id": "agent-does-not-exist"},
                roles=[{"agent_id": self.worker["id"]}])
        self.assertEqual(ctx.exception.code, "role_agent_not_found")

    def test_duplicate_role_name_rejected(self) -> None:
        with self.assertRaises(studio_commands.StudioError) as ctx:
            studio_commands.create_cluster(
                self.store, name="重名", objective="验证重名",
                workspace_root=str(self.root / "cluster-dup"),
                lead={"agent_id": self.writer["id"]},
                roles=[{"agent_id": self.writer["id"]}])
        self.assertEqual(ctx.exception.code, "role_name_duplicate")

    def test_delete_role_keeps_lead(self) -> None:
        detail = self._cluster("删角色")
        worker_id = detail["roles"][1]["id"]
        result = studio_commands.delete_role(self.store, worker_id)
        self.assertTrue(result["deleted"])
        with self.assertRaises(studio_commands.StudioError) as ctx:
            studio_commands.delete_role(self.store, detail["roles"][0]["id"])
        self.assertEqual(ctx.exception.code, "role_is_lead")

    def test_invocations_surface_in_detail(self) -> None:
        detail = self._cluster("计数")
        run = studio_store.create_run(self.store, detail["id"])
        role = detail["roles"][1]
        studio_store.update_run(self.store, run["id"],
                                budget_used={"invocations": {role["id"]: 3}})
        refreshed = studio_commands.cluster_detail(self.store, detail["id"])
        worker = refreshed["roles"][1]
        self.assertEqual(worker["invocations"], 3)
        self.assertEqual(worker["max_invocations"], 30)


if __name__ == "__main__":
    unittest.main()
