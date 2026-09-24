"""派单模式（P1-1）与两条口径锁定的契约测试。

覆盖：
  * 任务指派给成员后，只有该成员名下 Agent 能领取；未指派仍是先到先得
  * 指派目标必须是项目成员
  * PATCH 的"出现才生效"语义（不动 / 改派 / 取消）
  * 个人任务中心三组聚合（指派给我 / 我的 Agent 在跑 / 最近完成）
  * 口径锁定：**允许**批准自己 Agent 的产出；项目内草稿/待审内容对**所有**项目成员可见
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import UUID, uuid4

from app.contracts import (
    AgentRegister,
    TaskStatus,
    ArtifactCreate,
    HumanMemberCreate,
    ProjectCreate,
    ReviewCreate,
    TaskClaimRequest,
    TaskCreate,
    TaskUpdateRequest,
)
from app.store import DEV_ORG_ID, DEV_TEAM_ID, Store


class DispatchFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(self.store.close)

        self.lead = self.store.get_member("member-001")
        # 用新建的空项目：种子项目里本来就有 READY 任务，会让"谁领到了什么"的断言失去意义
        self.project = self.store.create_project(
            ProjectCreate(name="派单测试项目", competition_pack="cumcm-2026", problem_code="A", created_by=self.lead.id)
        )
        # 两位真实成员 + 各自的 Agent，都授权到同一个项目（模拟"不同用户的 Agent 在同一团队协作"）
        self.alice = self.store.create_member(
            HumanMemberCreate(organization_id=UUID(DEV_ORG_ID), team_id=UUID(DEV_TEAM_ID), email="alice@example.local", display_name="Alice", role="contributor")
        )
        self.bob = self.store.create_member(
            HumanMemberCreate(organization_id=UUID(DEV_ORG_ID), team_id=UUID(DEV_TEAM_ID), email="bob@example.local", display_name="Bob", role="contributor")
        )
        for member in (self.alice, self.bob):
            self.store.add_project_member(self.project.id, member.id, "contributor")
        self.alice_agent = self.store.register_agent(AgentRegister(agent_id="agent-alice", display_name="Alice 的机器", owner_member_id=self.alice.id))
        self.bob_agent = self.store.register_agent(AgentRegister(agent_id="agent-bob", display_name="Bob 的机器", owner_member_id=self.bob.id))
        for agent_id in ("agent-alice", "agent-bob"):
            self.store.db.execute(
                "INSERT OR REPLACE INTO agent_project_grants (agent_id, project_id, capabilities, granted_by, created_at) VALUES (?, ?, ?, ?, ?)",
                (agent_id, str(self.project.id), "[]", self.lead.id, "2026-01-01T00:00:00+00:00"),
            )
        self.store.db.commit()

    def claim_request(self, agent_id: str) -> TaskClaimRequest:
        return TaskClaimRequest(agent_id=agent_id, lease_seconds=600, idempotency_key=uuid4().hex)

    def make_task(self, title: str, assignee_member_id: str | None = None):
        from uuid import UUID as _UUID

        return self.store.create_task(
            self.project.id,
            TaskCreate(title=title, description="派单测试", assignee_member_id=assignee_member_id),
        )


class DispatchClaimTests(DispatchFixture):
    def test_assigned_task_only_claimable_by_target_member_agent(self) -> None:
        task = self.make_task("给 Alice 的活", assignee_member_id=self.alice.id)
        self.assertEqual(task.assignee_member_id, self.alice.id)

        # Bob 的机器轮询：拿不到（不是派给他的）
        self.assertIsNone(self.store.claim_next_task(self.claim_request("agent-bob"), self.project.id))
        # Bob 直接点名领取：明确拒绝
        with self.assertRaisesRegex(PermissionError, "task_assigned_to_another_member"):
            self.store.claim_task(task.id, self.claim_request("agent-bob"))

        # Alice 的机器能领到，并且只有一台能领（租约仍然生效）
        claimed, lease = self.store.claim_next_task(self.claim_request("agent-alice"), self.project.id)
        self.assertEqual(claimed.id, task.id)
        self.assertEqual(lease.agent_id, "agent-alice")
        self.assertEqual(self.store.get_task(task.id).status, "CLAIMED")

    def test_unassigned_task_stays_first_come_first_served(self) -> None:
        task = self.make_task("谁先轮到谁跑")
        self.assertIsNone(task.assignee_member_id)
        claimed, _ = self.store.claim_next_task(self.claim_request("agent-bob"), self.project.id)
        self.assertEqual(claimed.id, task.id)
        # 已被领走后，另一台机器不再拿到同一个任务
        self.assertIsNone(self.store.claim_next_task(self.claim_request("agent-alice"), self.project.id))

    def test_dispatch_target_must_be_project_member(self) -> None:
        outsider = self.store.create_member(
            HumanMemberCreate(organization_id=UUID(DEV_ORG_ID), team_id=UUID(DEV_TEAM_ID), email="outsider@example.local", display_name="Outsider")
        )
        with self.assertRaisesRegex(ValueError, "assignee_not_project_member"):
            self.make_task("给外部人的活", assignee_member_id=outsider.id)

    def test_patch_semantics_unchanged_reassigned_cleared(self) -> None:
        task = self.make_task("可改派的任务", assignee_member_id=self.alice.id)

        # 未出现该字段 → 不动
        self.store.update_task(task.id, None, None, None, dispatch_provided=False)
        self.assertEqual(self.store.get_task(task.id).assignee_member_id, self.alice.id)

        # 改派给 Bob
        self.store.update_task(task.id, None, None, None, assignee_member_id=self.bob.id, dispatch_provided=True)
        self.assertEqual(self.store.get_task(task.id).assignee_member_id, self.bob.id)
        self.assertIsNone(self.store.claim_next_task(self.claim_request("agent-alice"), self.project.id))

        # 空串 = 取消指派，回到先到先得
        self.store.update_task(task.id, None, None, None, assignee_member_id="", dispatch_provided=True)
        self.assertIsNone(self.store.get_task(task.id).assignee_member_id)
        self.assertIsNotNone(self.store.claim_next_task(self.claim_request("agent-alice"), self.project.id))

    def test_dispatch_survives_lease_expiry_recycle(self) -> None:
        """任务被领走又因租约过期回收后，派单关系仍在（不会被回收流程清掉）。"""

        task = self.make_task("过期回收后仍派给 Alice", assignee_member_id=self.alice.id)
        claimed, _ = self.store.claim_next_task(self.claim_request("agent-alice"), self.project.id)
        self.assertEqual(claimed.id, task.id)
        self.store.db.execute("UPDATE task_leases SET expires_at = '2000-01-01T00:00:00+00:00' WHERE task_id = ?", (str(task.id),))
        self.store.db.execute("UPDATE tasks SET status = 'READY' WHERE id = ?", (str(task.id),))
        self.store.db.commit()
        self.assertEqual(self.store.get_task(task.id).assignee_member_id, self.alice.id)
        self.assertIsNone(self.store.claim_next_task(self.claim_request("agent-bob"), self.project.id))


class ProjectMemberDirectoryTests(DispatchFixture):
    def test_members_listing_returns_roles_and_names(self) -> None:
        members = {item["member_id"]: item for item in self.store.list_project_members(self.project.id)}
        self.assertIn(self.alice.id, members)
        self.assertIn(self.bob.id, members)
        self.assertEqual(members[self.alice.id]["display_name"], "Alice")
        self.assertEqual(members[self.alice.id]["role"], "contributor")
        self.assertEqual(members[self.lead.id]["role"], "project_lead")


class MyTasksTests(DispatchFixture):
    def test_board_groups_assigned_running_and_recent(self) -> None:
        task = self.make_task("Alice 的待办", assignee_member_id=self.alice.id)
        board = self.store.my_tasks(self.alice.id)
        self.assertEqual([item["task"].id for item in board["assigned"]], [task.id])
        self.assertEqual(board["assigned"][0]["assignee_member_name"], "Alice")
        self.assertEqual(board["assigned"][0]["project_name"], self.project.name)
        self.assertEqual(board["running"], [])
        # Bob 的任务中心里没有 Alice 的派单
        self.assertEqual(self.store.my_tasks(self.bob.id)["assigned"], [])

        # Alice 的机器领走 → 出现在"我的 Agent 正在跑"
        self.store.claim_next_task(self.claim_request("agent-alice"), self.project.id)
        board = self.store.my_tasks(self.alice.id)
        self.assertEqual([item["task"].id for item in board["running"]], [task.id])
        self.assertEqual(board["running"][0]["executor_agent_id"], "agent-alice")
        self.assertTrue(board["running"][0]["lease_active"])

    def test_recent_lists_finished_work_of_my_agents(self) -> None:
        task = self.make_task("已完成的任务", assignee_member_id=self.alice.id)
        self.store.claim_next_task(self.claim_request("agent-alice"), self.project.id)
        # 走合法路径：CLAIMED → RUNNING → FAILED（APPROVED 只能由人工复核产生，此处不适用）
        self.store.update_task(task.id, TaskStatus.RUNNING, None, None, actor="agent-alice", actor_kind="agent")
        self.store.update_task(task.id, TaskStatus.FAILED, None, None, actor="agent-alice", actor_kind="agent")
        board = self.store.my_tasks(self.alice.id)
        self.assertEqual([item["task"].id for item in board["recent"]], [task.id])
        self.assertEqual(board["running"], [])


class DecisionLockTests(DispatchFixture):
    """把用户拍板的两条口径固化成测试：允许自批、内容全部可见。"""

    def test_member_may_approve_own_agents_output(self) -> None:
        # 批准权仍按项目角色判定（contributor 不能批）；这里给 Alice reviewer 角色，
        # 验证的是"允许批准自己 Agent 的产出"——即不做职责分离约束。
        self.store.add_project_member(self.project.id, self.alice.id, "reviewer")
        artifact = self.store.create_artifact(
            self.project.id,
            ArtifactCreate(name="alice-agent-output.md", artifact_type="paper_source", created_by="agent-alice", created_by_kind="agent"),
        )
        # Alice 批准自己 Agent 的产出：按拍板口径这是允许的（不做职责分离约束）
        review = self.store.create_review(
            self.project.id,
            ReviewCreate(target_type="artifact", target_id=artifact.id, verdict="APPROVED", summary="自批（口径允许）", reviewer=self.alice.id, reviewer_kind="member"),
        )
        self.assertEqual(review.verdict, "APPROVED")
        self.assertEqual(self.store.get_artifact(artifact.id).status, "APPROVED")

    def test_pending_content_visible_to_all_project_members(self) -> None:
        artifact = self.store.create_artifact(
            self.project.id,
            ArtifactCreate(name="alice-draft.md", artifact_type="paper_source", created_by="agent-alice", created_by_kind="agent"),
        )
        self.assertEqual(self.store.get_artifact(artifact.id).status, "DRAFT")
        # 任何项目成员（这里用 Bob）都能在项目成果物列表里看到草稿/待审内容
        visible = {item.id for item in self.store.list_artifacts(self.project.id)}
        self.assertIn(artifact.id, visible)
        # 但下游使用仍要求已批准（这条没有被口径改动）
        self.assertFalse(self.store.get_artifact(artifact.id).downstream_allowed)


if __name__ == "__main__":
    unittest.main()