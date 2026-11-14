"""契约成果物 + 版本绑定 + 收尾闸门。

覆盖上一轮实测踩到的失败：两个 Agent 各自"觉得确认过了"，其中一方后来改了契约，
另一方毫不知情，产物合不起来。这里锁住的是：

- 契约有**稳定身份 + 版本 + 哈希**，不是一条聊天消息；
- 发布新版本时**当场报出谁被甩下了**；
- 任务声明了必需契约后，**绑不上或不同步就收不了尾**（硬闸门，不是警告）；
- 只能绑当前版本（绑旧版等于明知不同步还继续）。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from synapforge import commands
from synapforge.db import open_store

CONTRACT = {
    "auth": {"login": "POST /api/auth/login", "register": "POST /api/auth/register"},
    "files": {"list": "GET /api/files", "upload": "POST /api/files"},
}


class ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="synapforge-contract-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = open_store(self.tmp / "state.db")
        self.pid = commands.ensure_project(self.store, "contract-proj")["id"]
        self.a, self.b = "agent-front", "agent-back"
        for agent_id in (self.a, self.b):
            commands.ensure_participant(self.store, project_id=self.pid, agent_id=agent_id)

    def _task_for(self, agent_id: str, *, required: bool = True) -> tuple[str, str]:
        task = commands.create_task(self.store, project_id=self.pid, title="联调任务",
                                    assignee_agent_id=agent_id,
                                    required_contracts=["api"] if required else None)
        lease = commands.claim(self.store, project_id=self.pid, agent_id=agent_id)
        token = lease["lease"]["lease_token"]
        commands.mark_running(self.store, task_id=task["id"], agent_id=agent_id, lease_token=token)
        return str(task["id"]), token

    # ---- 发布 ------------------------------------------------------------

    def test_first_publish_is_version_one(self) -> None:
        result = commands.publish_contract(self.store, project_id=self.pid, key="api",
                                           body=CONTRACT, agent_id=self.b)
        self.assertEqual(result["version"], 1)
        self.assertRegex(result["content_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(result["stale_bindings"], [])

    def test_republishing_same_body_does_not_bump_version(self) -> None:
        first = commands.publish_contract(self.store, project_id=self.pid, key="api",
                                          body=CONTRACT, agent_id=self.b)
        again = commands.publish_contract(self.store, project_id=self.pid, key="api",
                                         body=CONTRACT, agent_id=self.b)
        self.assertEqual(again["version"], first["version"],
                         "正文没变就不该造新版本，否则版本号会失去意义")

    def test_republishing_changed_body_bumps_version_and_names_stale_bindings(self) -> None:
        commands.publish_contract(self.store, project_id=self.pid, key="api",
                                  body=CONTRACT, agent_id=self.b)
        commands.bind_contract(self.store, project_id=self.pid, key="api", agent_id=self.a)

        changed = dict(CONTRACT)
        changed["files"] = {"list": "GET /api/files", "upload": "POST /api/upload"}
        second = commands.publish_contract(self.store, project_id=self.pid, key="api",
                                           body=changed, agent_id=self.b)

        self.assertEqual(second["version"], 2)
        self.assertEqual([item["agent_id"] for item in second["stale_bindings"]], [self.a],
                         "发布新版本必须当场报出谁被甩下了")

    # ---- 绑定 ------------------------------------------------------------

    def test_bind_marks_in_sync(self) -> None:
        commands.publish_contract(self.store, project_id=self.pid, key="api",
                                  body=CONTRACT, agent_id=self.b)
        bound = commands.bind_contract(self.store, project_id=self.pid, key="api",
                                       agent_id=self.a)
        self.assertTrue(bound["in_sync"])
        status = commands.contract_status(self.store, project_id=self.pid, agent_id=self.a)
        self.assertTrue(status[0]["in_sync"])
        self.assertEqual(status[0]["my_version"], 1)

    def test_binding_an_old_version_is_refused(self) -> None:
        commands.publish_contract(self.store, project_id=self.pid, key="api",
                                  body=CONTRACT, agent_id=self.b)
        changed = dict(CONTRACT)
        changed["files"] = {"list": "GET /api/files"}
        commands.publish_contract(self.store, project_id=self.pid, key="api",
                                  body=changed, agent_id=self.b)
        with self.assertRaises(Exception) as ctx:
            commands.bind_contract(self.store, project_id=self.pid, key="api",
                                   agent_id=self.a, version=1)
        self.assertIn("contract_version_not_current", str(ctx.exception))

    def test_show_returns_the_body(self) -> None:
        commands.publish_contract(self.store, project_id=self.pid, key="api",
                                  body=CONTRACT, agent_id=self.b)
        shown = commands.show_contract(self.store, project_id=self.pid, key="api")
        self.assertEqual(shown["body"], CONTRACT)
        self.assertEqual(shown["version"], 1)

    # ---- 收尾闸门 ---------------------------------------------------------

    def test_finish_without_binding_is_refused(self) -> None:
        task_id, token = self._task_for(self.a)
        commands.publish_contract(self.store, project_id=self.pid, key="api",
                                  body=CONTRACT, agent_id=self.b)
        with self.assertRaises(Exception) as ctx:
            commands.finish_task(self.store, task_id=task_id, agent_id=self.a,
                                 lease_token=token, success=True, summary="done")
        self.assertIn("contract_drift", str(ctx.exception))
        self.assertIn("not_bound", str(ctx.exception))

    def test_finish_after_binding_succeeds(self) -> None:
        task_id, token = self._task_for(self.a)
        commands.publish_contract(self.store, project_id=self.pid, key="api",
                                  body=CONTRACT, agent_id=self.b)
        commands.bind_contract(self.store, project_id=self.pid, key="api", agent_id=self.a,
                               task_id=task_id)
        result = commands.finish_task(self.store, task_id=task_id, agent_id=self.a,
                                      lease_token=token, success=True, summary="done")
        self.assertTrue(result["accepted"])

    def test_contract_change_after_binding_blocks_finish_until_rebind(self) -> None:
        """这是上一轮真实翻车的场景：A 绑 v1 开工，B 改成 v2，A 必须重绑才能收尾。"""

        task_id, token = self._task_for(self.a)
        commands.publish_contract(self.store, project_id=self.pid, key="api",
                                  body=CONTRACT, agent_id=self.b)
        commands.bind_contract(self.store, project_id=self.pid, key="api", agent_id=self.a,
                               task_id=task_id)

        changed = dict(CONTRACT)
        changed["files"] = {"list": "GET /api/files", "upload": "POST /api/upload"}
        commands.publish_contract(self.store, project_id=self.pid, key="api",
                                  body=changed, agent_id=self.b)

        with self.assertRaises(Exception) as ctx:
            commands.finish_task(self.store, task_id=task_id, agent_id=self.a,
                                 lease_token=token, success=True, summary="done")
        self.assertIn("drifted", str(ctx.exception))

        # 重新读、重新绑——这一步就是"我重新看过了"的证明
        commands.bind_contract(self.store, project_id=self.pid, key="api", agent_id=self.a,
                               task_id=task_id)
        result = commands.finish_task(self.store, task_id=task_id, agent_id=self.a,
                                      lease_token=token, success=True, summary="done")
        self.assertTrue(result["accepted"])

    def test_task_without_required_contracts_is_unaffected(self) -> None:
        task_id, token = self._task_for(self.a, required=False)
        commands.publish_contract(self.store, project_id=self.pid, key="api",
                                  body=CONTRACT, agent_id=self.b)
        result = commands.finish_task(self.store, task_id=task_id, agent_id=self.a,
                                      lease_token=token, success=True, summary="done")
        self.assertTrue(result["accepted"], "契约是 opt-in，没声明就不该被拦")

    # ---- 留痕 ------------------------------------------------------------

    def test_contract_events_are_recorded(self) -> None:
        commands.publish_contract(self.store, project_id=self.pid, key="api",
                                  body=CONTRACT, agent_id=self.b)
        commands.bind_contract(self.store, project_id=self.pid, key="api", agent_id=self.a)
        types = [e["event_type"] for e in commands.recent_events(self.store,
                                                                 project_id=self.pid, limit=50)]
        self.assertIn("project.contract.published", types)
        self.assertIn("project.contract.bound", types)

    def test_inbox_surfaces_contract_sync_state(self) -> None:
        commands.publish_contract(self.store, project_id=self.pid, key="api",
                                  body=CONTRACT, agent_id=self.b)
        box = commands.inbox(self.store, agent_id=self.a, project_id=self.pid)
        self.assertEqual(len(box["contracts"]), 1)
        self.assertFalse(box["contracts"][0]["in_sync"])
        self.assertIsNone(box["contracts"][0]["my_version"])

        commands.bind_contract(self.store, project_id=self.pid, key="api", agent_id=self.a)
        box = commands.inbox(self.store, agent_id=self.a, project_id=self.pid)
        self.assertTrue(box["contracts"][0]["in_sync"])

    def test_contract_body_is_stored_as_an_artifact_with_receipt(self) -> None:
        result = commands.publish_contract(self.store, project_id=self.pid, key="api",
                                           body=CONTRACT, agent_id=self.b)
        artifacts = commands.list_artifacts(self.store, project_id=self.pid)
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]["receipt_version"], 1, "契约正文必须带溯源回执")
        self.assertEqual(artifacts[0]["content_hash"], result["content_hash"],
                         "artifact 的 content_hash 必须等于契约登记的 hash")


if __name__ == "__main__":
    unittest.main()
