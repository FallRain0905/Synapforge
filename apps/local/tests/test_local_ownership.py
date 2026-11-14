"""Agent 级归属回归测试（计划 §16.5 P6）。

背景：平台按**人**归属（`agents.owner_member_id` → `claim_next_task` 的过滤），
本地多个 Agent 若共用一个 owner，谁先调 `claim` 谁就领走任务——实测发生过
（后端 Agent 顺手领走了前端任务）。

修法：每个 Agent 一个独立归属成员 + 项目成员行；`create_task` 可指派给指定 Agent。
于是归属判断由 `claim_next_task` 既有的 SQL 过滤完成，**不新增第二套判断**。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from synapforge import commands
from synapforge.db import open_store


class AgentOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="synapforge-own-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = open_store(self.tmp / "state.db")
        self.pid = commands.ensure_project(self.store, "ownership")["id"]
        self.a, self.b = "agent-alpha", "agent-beta"
        for agent_id in (self.a, self.b):
            commands.ensure_participant(self.store, project_id=self.pid, agent_id=agent_id)

    def _owner_of(self, agent_id: str) -> str:
        row = self.store.db.execute(
            "SELECT owner_member_id FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        return str(row["owner_member_id"])

    def test_each_agent_has_its_own_member(self) -> None:
        self.assertNotEqual(self._owner_of(self.a), self._owner_of(self.b),
                            "两个 Agent 共用归属成员 = 归属过滤失效")
        self.assertEqual(self._owner_of(self.a), commands.agent_member_id(self.a))

    def test_owner_member_rows_exist(self) -> None:
        for agent_id in (self.a, self.b):
            mine = commands.agent_member_id(agent_id)
            self.assertIsNotNone(
                self.store.db.execute("SELECT 1 FROM human_members WHERE id = ?", (mine,)).fetchone(),
                f"{mine} 没有 human_members 行（悬空引用）")
            self.assertIsNotNone(
                self.store.db.execute(
                    "SELECT 1 FROM project_memberships WHERE project_id = ? AND member_id = ?",
                    (str(self.pid), mine)).fetchone(),
                f"{mine} 不是项目成员（create_task 的指派校验会失败）")

    def test_assigned_task_is_not_claimable_by_another_agent(self) -> None:
        """核心回归：beta 的任务，alpha 必须领不到。"""

        commands.create_task(self.store, project_id=self.pid, title="给 beta 的活",
                             assignee_agent_id=self.b)

        stolen = commands.claim(self.store, project_id=self.pid, agent_id=self.a)
        self.assertFalse(stolen.get("claimed"), f"alpha 不该领到 beta 的任务：{stolen}")
        self.assertEqual(stolen.get("reason"), "no_ready_task")

        mine = commands.claim(self.store, project_id=self.pid, agent_id=self.b)
        self.assertTrue(mine.get("claimed"), f"beta 应该能领到自己的任务：{mine}")
        self.assertEqual(mine["task"]["title"], "给 beta 的活")

    def test_assignment_is_recorded_on_the_task(self) -> None:
        task = commands.create_task(self.store, project_id=self.pid, title="有归属的任务",
                                    assignee_agent_id=self.b)
        row = self.store.db.execute(
            "SELECT assignee_member_id, assignee FROM tasks WHERE id = ?",
            (str(task["id"]),)).fetchone()
        self.assertEqual(str(row["assignee_member_id"]), commands.agent_member_id(self.b))
        self.assertEqual(str(row["assignee"]), self.b)

    def test_unassigned_task_stays_first_come_first_served(self) -> None:
        """未指派的任务仍是谁先到谁领——这条语义不能被本次修改破坏。"""

        commands.create_task(self.store, project_id=self.pid, title="无人认领的活")
        claimed = commands.claim(self.store, project_id=self.pid, agent_id=self.a)
        self.assertTrue(claimed.get("claimed"))

    def test_assigning_to_a_new_agent_auto_registers_it(self) -> None:
        newbie = "agent-newcomer"
        commands.create_task(self.store, project_id=self.pid, title="新人的活",
                             assignee_agent_id=newbie)
        self.assertIsNotNone(
            self.store.db.execute("SELECT 1 FROM agents WHERE agent_id = ?", (newbie,)).fetchone())
        claimed = commands.claim(self.store, project_id=self.pid, agent_id=newbie)
        self.assertTrue(claimed.get("claimed"))

    def test_legacy_shared_owner_is_corrected(self) -> None:
        """历史数据挂在 local-owner 上：再次 ensure 时补正，而不是继续共用。"""

        legacy = "agent-legacy"
        self.store.register_agent(__import__("app.contracts", fromlist=["AgentRegister"]).AgentRegister(
            agent_id=legacy, display_name="Legacy", owner_member_id="local-owner"))
        self.assertEqual(self._owner_of(legacy), "local-owner")

        commands.ensure_participant(self.store, project_id=self.pid, agent_id=legacy)
        self.assertEqual(self._owner_of(legacy), commands.agent_member_id(legacy),
                         "旧的共用归属必须被补正")

        commands.create_task(self.store, project_id=self.pid, title="给 legacy 的活",
                             assignee_agent_id=legacy)
        self.assertFalse(commands.claim(self.store, project_id=self.pid,
                                        agent_id=self.a).get("claimed"))
        self.assertTrue(commands.claim(self.store, project_id=self.pid,
                                       agent_id=legacy).get("claimed"))

    def test_stage_filter_still_works(self) -> None:
        """stage 过滤与归属过滤可以叠加。"""

        commands.create_task(self.store, project_id=self.pid, title="后端活", stage="backend",
                             assignee_agent_id=self.b)
        self.assertFalse(commands.claim(self.store, project_id=self.pid, agent_id=self.b,
                                        stages=["frontend"]).get("claimed"))
        self.assertTrue(commands.claim(self.store, project_id=self.pid, agent_id=self.b,
                                       stages=["backend"]).get("claimed"))


class AssignmentVisibilityTests(unittest.TestCase):
    """指派的另一半：被指派方必须能**发现**有活派给自己。

    光写 `assignee_member_id` 只保证别人领不到；没有 `inbox.assigned_to_me`，
    指派就等于把任务藏起来。
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="synapforge-inbox-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = open_store(self.tmp / "state.db")
        self.pid = commands.ensure_project(self.store, "inbox")["id"]
        self.a, self.b = "agent-alpha", "agent-beta"
        for agent_id in (self.a, self.b):
            commands.ensure_participant(self.store, project_id=self.pid, agent_id=agent_id)

    def test_assigned_task_appears_in_the_owners_inbox(self) -> None:
        commands.create_task(self.store, project_id=self.pid, title="给 beta 的活",
                             assignee_agent_id=self.b)

        beta = commands.inbox(self.store, agent_id=self.b, project_id=self.pid)
        self.assertEqual([t["title"] for t in beta["assigned_to_me"]], ["给 beta 的活"])
        self.assertTrue(beta["assigned_to_me"][0]["claimable_now"])
        self.assertEqual(beta["in_flight_tasks"], [])

        alpha = commands.inbox(self.store, agent_id=self.a, project_id=self.pid)
        self.assertEqual(alpha["assigned_to_me"], [],
                         "别人的指派不该出现在我的收件箱")

    def test_claiming_moves_it_from_assigned_to_in_flight(self) -> None:
        commands.create_task(self.store, project_id=self.pid, title="给 beta 的活",
                             assignee_agent_id=self.b)
        self.assertTrue(commands.claim(self.store, project_id=self.pid,
                                       agent_id=self.b).get("claimed"))

        beta = commands.inbox(self.store, agent_id=self.b, project_id=self.pid)
        self.assertEqual(beta["assigned_to_me"], [],
                         "领过之后不该还留在待领列表里")
        self.assertEqual(len(beta["in_flight_tasks"]), 1)
        self.assertTrue(beta["in_flight_tasks"][0]["lease_token"],
                        "在途任务必须带回租约令牌，否则新会话无法收尾")

    def test_unassigned_task_is_not_in_anyones_assigned_list(self) -> None:
        commands.create_task(self.store, project_id=self.pid, title="无人认领的活")
        for agent_id in (self.a, self.b):
            box = commands.inbox(self.store, agent_id=agent_id, project_id=self.pid)
            self.assertEqual(box["assigned_to_me"], [])

    def test_inbox_reports_the_agent_member(self) -> None:
        box = commands.inbox(self.store, agent_id=self.b, project_id=self.pid)
        self.assertEqual(box["member_id"], commands.agent_member_id(self.b))


if __name__ == "__main__":
    unittest.main()
