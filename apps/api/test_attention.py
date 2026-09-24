"""桌面通知的关注清单（DESKTOP-NOTIFY）：派给我的任务 + 我该复核的任务。

覆盖：
  * assigned：只含"未结束 + 派给我"的任务；终止态（APPROVED/CANCELLED）不出现；
  * NEEDS_REVISION 也算"要你动手"（退回给你），不会因为状态不是 READY 就漏掉；
  * review_pending：只在**我有 review.approve 权限**的项目里（owner/project_lead/reviewer），
    contributor 不该收到复核通知；
  * 只覆盖我是成员的项目（不跨组织/不跨项目泄露）；
  * HTTP：会话缺失 401、正常 200 且结构完整。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app import main
from app.contracts import HumanMemberCreate, ProjectCreate, SessionCreate, TaskCreate
from app.store import DEV_ORG_ID, DEV_TEAM_ID, Store


class AttentionFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.addCleanup(self.store.close)
        self.lead = self.store.get_member("member-001")
        self.me = self.store.create_member(
            HumanMemberCreate(
                organization_id=UUID(DEV_ORG_ID), team_id=UUID(DEV_TEAM_ID), email="attention@example.local", display_name="关注者"
            )
        )
        self.other = self.store.create_member(
            HumanMemberCreate(
                organization_id=UUID(DEV_ORG_ID), team_id=UUID(DEV_TEAM_ID), email="outsider@example.local", display_name="别人"
            )
        )
        self.project = self.store.create_project(
            ProjectCreate(name="通知测试", competition_pack="cumcm-2026", problem_code="A", created_by=self.lead.id)
        )
        self.foreign = self.store.create_project(
            ProjectCreate(name="别人项目", competition_pack="cumcm-2026", problem_code="B", created_by=self.other.id)
        )
        self.store.add_project_member(self.project.id, self.me.id, "contributor")
        self.store.add_project_member(self.project.id, self.other.id, "reviewer")
        self.store.add_project_member(self.foreign.id, self.me.id, "observer")

    def task(self, title: str, **overrides):
        return self.store.create_task(self.project.id, TaskCreate(title=title, description="测试", **overrides))

    def set_status(self, task_id, status: str) -> None:
        self.store.db.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, str(task_id)))
        self.store.db.commit()


class AttentionTests(AttentionFixture):
    def test_assigned_only_covers_open_tasks_for_me(self) -> None:
        mine = self.task("派给我的", assignee_member_id=self.me.id)
        self.task("派给别人的", assignee_member_id=self.other.id)
        self.task("没人认领")
        payload = self.store.my_attention(self.me.id)
        self.assertEqual([item["title"] for item in payload["assigned"]], ["派给我的"])
        self.assertEqual(payload["assigned_total"], 1)
        # 结束的任务不再出现在通知清单里
        self.set_status(mine.id, "APPROVED")
        self.assertEqual(self.store.my_attention(self.me.id)["assigned"], [])

    def test_needs_revision_still_counts_as_action_for_me(self) -> None:
        task = self.task("退回给我改", assignee_member_id=self.me.id)
        self.set_status(task.id, "NEEDS_REVISION")
        payload = self.store.my_attention(self.me.id)
        self.assertEqual(len(payload["assigned"]), 1)
        self.assertEqual(payload["assigned"][0]["status"], "NEEDS_REVISION")

    def test_review_pending_only_for_approvers(self) -> None:
        waiting = self.task("等我复核")
        self.set_status(waiting.id, "WAITING_REVIEW")
        # 我是 contributor：不是复核人 → 不通知
        self.assertEqual(self.store.my_attention(self.me.id)["review_pending"], [])
        # 别人是 reviewer → 应该收到
        self.assertEqual([item["title"] for item in self.store.my_attention(self.other.id)["review_pending"]], ["等我复核"])
        # 升级成 reviewer 后我也该收到
        self.store.db.execute(
            "UPDATE project_memberships SET role = 'reviewer' WHERE project_id = ? AND member_id = ?",
            (str(self.project.id), self.me.id),
        )
        self.store.db.commit()
        self.assertEqual([item["title"] for item in self.store.my_attention(self.me.id)["review_pending"]], ["等我复核"])

    def test_attention_is_scoped_to_my_projects(self) -> None:
        foreign_task = self.store.create_task(self.foreign.id, TaskCreate(title="别人的待复核", description=""))
        self.set_status(foreign_task.id, "WAITING_REVIEW")
        payload = self.store.my_attention(self.me.id)
        self.assertEqual(payload["review_pending"], [])
        self.assertEqual(payload["assigned"], [])

    def test_items_carry_project_and_kind(self) -> None:
        task = self.task("带项目名", assignee_member_id=self.me.id)
        item = self.store.my_attention(self.me.id)["assigned"][0]
        self.assertEqual(item["kind"], "assigned")
        self.assertEqual(item["project_name"], "通知测试")
        self.assertEqual(item["task_id"], task.id)


class AttentionHttpTests(AttentionFixture):
    def setUp(self) -> None:
        super().setUp()
        self.previous_store = main.store
        main.store = self.store
        self.addCleanup(self._restore)
        self.client = TestClient(main.app)
        self.headers = {"Authorization": f"Bearer {self.store.create_session(SessionCreate(member_id=self.me.id)).token}"}

    def _restore(self) -> None:
        main.store = self.previous_store
        self.client.close()

    def test_endpoint_returns_both_groups(self) -> None:
        self.task("派给我", assignee_member_id=self.me.id)
        waiting = self.task("等复核")
        self.set_status(waiting.id, "WAITING_REVIEW")
        self.store.db.execute(
            "UPDATE project_memberships SET role = 'project_lead' WHERE project_id = ? AND member_id = ?",
            (str(self.project.id), self.me.id),
        )
        self.store.db.commit()
        response = self.client.get("/api/my-attention", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["assigned_total"], 1)
        self.assertEqual(body["review_total"], 1)
        self.assertEqual(body["assigned"][0]["title"], "派给我")
        self.assertEqual(body["review_pending"][0]["kind"], "review")
        self.assertIn("generated_at", body)

    def test_endpoint_requires_a_session_in_required_mode(self) -> None:
        """强制鉴权模式下没有会话就是 401。

        开发模式（默认）里 `_request_member_id` 会退化成 member-001，所以这里显式切模式再断言——
        否则测出来的是"开发模式的宽容"，不是"鉴权生效"。
        """

        import os

        original = os.environ.get("PLATFORM_AUTH_MODE")
        os.environ["PLATFORM_AUTH_MODE"] = "required"
        try:
            response = self.client.get("/api/my-attention")
            self.assertEqual(response.status_code, 401)
            authenticated = self.client.get("/api/my-attention", headers=self.headers)
            self.assertEqual(authenticated.status_code, 200)
        finally:
            if original is None:
                os.environ.pop("PLATFORM_AUTH_MODE", None)
            else:
                os.environ["PLATFORM_AUTH_MODE"] = original


if __name__ == "__main__":
    unittest.main()