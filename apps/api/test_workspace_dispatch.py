"""W-2 契约测试：派单口径（队长可派 / 成员 hybrid 认领）、批量派单、派单事件与卡片、成果空间聚合。

口径来自用户拍板："不应该默认进行全自动化设计，任务需要队长派遣，同时成员自己可以看到自己的任务，
同时也支持按照模板进行任务全自动化推进。" 因此：

  * manual（默认）：只有队长能改负责人；
  * hybrid/auto：成员可以认领**还没有负责人**的任务（把负责人设成自己），也能释放自己认领的；
  * 成员不能把任务派给别人，也不能动别人负责的任务。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import UUID, uuid4

from app.contracts import (
    ArtifactCreate,
    EvidenceCreate,
    HandoffCreate,
    HumanMemberCreate,
    ProjectCreate,
    ReviewCreate,
    ReviewerKind,
    SessionCreate,
    TaskCreate,
)
from app.store import DEV_ORG_ID, DEV_TEAM_ID, Store


class DispatchFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(self.store.close)
        self.lead = self.store.get_member("member-001")
        self.project = self.store.create_project(
            ProjectCreate(
                name="W2 派单测试",
                competition_pack="cumcm-2026",
                problem_code="A",
                goal="验证派单口径",
                task_mode="manual",
                created_by=self.lead.id,
            )
        )
        self.peer = self.store.create_member(
            HumanMemberCreate(
                organization_id=UUID(DEV_ORG_ID), team_id=UUID(DEV_TEAM_ID), email="peer@example.local", display_name="小王"
            )
        )
        self.store.add_project_member(self.project.id, self.peer.id, "contributor")

    def _tasks(self, count: int = 3) -> list[UUID]:
        return [
            self.store.create_task(self.project.id, TaskCreate(title=f"任务 {index}", description="d")).id
            for index in range(count)
        ]


class DispatchPolicyTests(DispatchFixture):
    def test_lead_can_assign_and_reassign_and_release(self) -> None:
        task_id = self._tasks(1)[0]
        assigned = self.store.update_task(
            task_id, None, None, None, actor=self.lead.id, actor_kind="member", assignee_member_id=self.peer.id, dispatch_provided=True
        )
        self.assertEqual(assigned.assignee_member_id, self.peer.id)

        other = self.store.create_member(
            HumanMemberCreate(
                organization_id=UUID(DEV_ORG_ID), team_id=UUID(DEV_TEAM_ID), email="other@example.local", display_name="小李"
            )
        )
        self.store.add_project_member(self.project.id, other.id, "contributor")
        reassigned = self.store.update_task(
            task_id, None, None, None, actor=self.lead.id, actor_kind="member", assignee_member_id=other.id, dispatch_provided=True
        )
        self.assertEqual(reassigned.assignee_member_id, other.id)

        released = self.store.update_task(
            task_id, None, None, None, actor=self.lead.id, actor_kind="member", assignee_member_id="", dispatch_provided=True
        )
        self.assertIsNone(released.assignee_member_id)

    def test_member_cannot_assign_in_manual_mode(self) -> None:
        task_id = self._tasks(1)[0]
        with self.assertRaises(PermissionError) as caught:
            self.store.update_task(
                task_id, None, None, None, actor=self.peer.id, actor_kind="member", assignee_member_id=self.peer.id, dispatch_provided=True
            )
        self.assertEqual(str(caught.exception), "task_self_claim_disabled_in_manual_mode")

    def test_member_cannot_assign_others(self) -> None:
        task_id = self._tasks(1)[0]
        self.store.update_project_settings(self.project.id, task_mode="hybrid", set_team=False)
        with self.assertRaises(PermissionError) as caught:
            self.store.update_task(
                task_id, None, None, None, actor=self.peer.id, actor_kind="member", assignee_member_id=self.lead.id, dispatch_provided=True
            )
        self.assertEqual(str(caught.exception), "task_dispatch_requires_lead")

    def test_member_self_claim_in_hybrid_and_release(self) -> None:
        task_id = self._tasks(1)[0]
        self.store.update_project_settings(self.project.id, task_mode="hybrid", set_team=False)
        claimed = self.store.update_task(
            task_id, None, None, None, actor=self.peer.id, actor_kind="member", assignee_member_id=self.peer.id, dispatch_provided=True
        )
        self.assertEqual(claimed.assignee_member_id, self.peer.id)

        # 认领过的任务可以自己释放
        released = self.store.update_task(
            task_id, None, None, None, actor=self.peer.id, actor_kind="member", assignee_member_id="", dispatch_provided=True
        )
        self.assertIsNone(released.assignee_member_id)

    def test_member_cannot_steal_assigned_task(self) -> None:
        task_id = self._tasks(1)[0]
        self.store.update_project_settings(self.project.id, task_mode="hybrid", set_team=False)
        self.store.update_task(
            task_id, None, None, None, actor=self.lead.id, actor_kind="member", assignee_member_id=self.lead.id, dispatch_provided=True
        )
        with self.assertRaises(PermissionError) as caught:
            self.store.update_task(
                task_id, None, None, None, actor=self.peer.id, actor_kind="member", assignee_member_id=self.peer.id, dispatch_provided=True
            )
        self.assertEqual(str(caught.exception), "task_assigned_to_another_member")

    def test_member_cannot_release_someone_elses_task(self) -> None:
        task_id = self._tasks(1)[0]
        self.store.update_task(
            task_id, None, None, None, actor=self.lead.id, actor_kind="member", assignee_member_id=self.lead.id, dispatch_provided=True
        )
        with self.assertRaises(PermissionError):
            self.store.update_task(
                task_id, None, None, None, actor=self.peer.id, actor_kind="member", assignee_member_id="", dispatch_provided=True
            )

    def test_auto_mode_also_allows_self_claim(self) -> None:
        task_id = self._tasks(1)[0]
        self.store.update_project_settings(self.project.id, task_mode="auto", set_team=False)
        claimed = self.store.update_task(
            task_id, None, None, None, actor=self.peer.id, actor_kind="member", assignee_member_id=self.peer.id, dispatch_provided=True
        )
        self.assertEqual(claimed.assignee_member_id, self.peer.id)

    def test_assignee_must_be_project_member(self) -> None:
        outsider = self.store.create_member(
            HumanMemberCreate(
                organization_id=UUID(DEV_ORG_ID), team_id=UUID(DEV_TEAM_ID), email="outsider@example.local", display_name="外人"
            )
        )
        task_id = self._tasks(1)[0]
        with self.assertRaises(ValueError) as caught:
            self.store.update_task(
                task_id, None, None, None, actor=self.lead.id, actor_kind="member", assignee_member_id=outsider.id, dispatch_provided=True
            )
        self.assertEqual(str(caught.exception), "assignee_not_project_member")

    def test_dispatch_writes_event_and_chat_card(self) -> None:
        task_id = self._tasks(1)[0]
        self.store.update_task(
            task_id, None, None, None, actor=self.lead.id, actor_kind="member", assignee_member_id=self.peer.id, dispatch_provided=True
        )
        events = self.store.list_latest_events(self.project.id, limit=5)
        dispatched = next(event for event in events if str(event.event_type) == "task.dispatched")
        self.assertEqual(dispatched.payload["assignee_member_id"], self.peer.id)
        self.assertEqual(dispatched.payload["action"], "dispatch")

        contents = [message.content for message in self.store.list_project_messages(self.project.id, limit=10)]
        self.assertTrue(any("派给了 小王" in item for item in contents))

        # hybrid 认领的卡片文案不同（"认领了任务"）
        self.store.update_project_settings(self.project.id, task_mode="hybrid", set_team=False)
        second = self._tasks(2)[1]
        self.store.update_task(
            second, None, None, None, actor=self.peer.id, actor_kind="member", assignee_member_id=self.peer.id, dispatch_provided=True
        )
        contents = [message.content for message in self.store.list_project_messages(self.project.id, limit=10)]
        self.assertTrue(any("小王 认领了任务" in item for item in contents))


class BulkAssignTests(DispatchFixture):
    def test_lead_bulk_assigns_and_releases(self) -> None:
        task_ids = self._tasks(3)
        result = self.store.assign_tasks(self.project.id, task_ids, self.peer.id, self.lead.id)
        self.assertEqual(result["updated"], 3)
        self.assertEqual(result["failures"], [])
        self.assertEqual({task.assignee_member_id for task in self.store.list_tasks(self.project.id)}, {self.peer.id})

        cleared = self.store.assign_tasks(self.project.id, task_ids, "", self.lead.id)
        self.assertEqual(cleared["updated"], 3)
        self.assertEqual({task.assignee_member_id for task in self.store.list_tasks(self.project.id)}, {None})

    def test_bulk_reports_partial_failures(self) -> None:
        task_ids = self._tasks(2)
        # 一条已被队长派给别人：hybrid 下成员批量认领时，这条应失败、另一条成功
        self.store.update_task(
            task_ids[0], None, None, None, actor=self.lead.id, actor_kind="member", assignee_member_id=self.lead.id, dispatch_provided=True
        )
        self.store.update_project_settings(self.project.id, task_mode="hybrid", set_team=False)
        result = self.store.assign_tasks(self.project.id, task_ids, self.peer.id, self.peer.id)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(len(result["failures"]), 1)
        self.assertEqual(result["failures"][0]["task_id"], str(task_ids[0]))
        self.assertIn("task_assigned_to_another_member", result["failures"][0]["reason"])

        tasks = {str(task.id): task for task in self.store.list_tasks(self.project.id)}
        self.assertEqual(tasks[str(task_ids[1])].assignee_member_id, self.peer.id)
        self.assertEqual(tasks[str(task_ids[0])].assignee_member_id, self.lead.id)

    def test_bulk_rejects_unknown_task(self) -> None:
        result = self.store.assign_tasks(self.project.id, [uuid4()], self.peer.id, self.lead.id)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["failures"][0]["reason"], "task_not_found")

    def test_bulk_requires_project_member_target(self) -> None:
        outsider = self.store.create_member(
            HumanMemberCreate(
                organization_id=UUID(DEV_ORG_ID), team_id=UUID(DEV_TEAM_ID), email="outsider2@example.local", display_name="外人2"
            )
        )
        with self.assertRaises(ValueError):
            self.store.assign_tasks(self.project.id, self._tasks(1), outsider.id, self.lead.id)


class DeliverablesTests(DispatchFixture):
    def test_deliverables_aggregate_all_five_sections(self) -> None:
        paper = self.store.create_artifact(
            self.project.id,
            ArtifactCreate(name="论文草稿", artifact_type="paper_source", description="d"),
            created_by=self.lead.id,
        )
        figure = self.store.create_artifact(
            self.project.id,
            ArtifactCreate(name="图 1", artifact_type="figure", description="d"),
            created_by=self.lead.id,
        )
        review = self.store.create_review(
            self.project.id,
            ReviewCreate(
                target_type="artifact",
                target_id=paper.id,
                verdict="APPROVED",
                summary="批准",
                reviewer=self.lead.id,
                reviewer_kind=ReviewerKind.MEMBER,
            ),
        )
        self.store.create_evidence(
            self.project.id,
            EvidenceCreate(claim="结论", evidence_type="artifact", artifact_id=paper.id, created_by=self.lead.id),
        )
        self.store.create_handoff(
            self.project.id,
            HandoffCreate(
                task_id=self._tasks(1)[0],
                sender_agent_id="agent-demo",
                receiver={"type": "member", "id": self.peer.id},
                objective="把结论交出去",
                key_conclusions=["结论一"],
            ),
        )

        payload = self.store.project_deliverables(self.project.id)
        self.assertEqual(payload["artifacts"]["total"], 2)
        self.assertIn(payload["artifacts"]["recent"][0]["status"], {"DRAFT", "SUBMITTED", "PENDING_REVIEW", "APPROVED"})
        # 文档面板只收文本类成果物（figure 不算文档）
        self.assertEqual(payload["documents"]["total"], 1)
        self.assertEqual(payload["documents"]["recent"][0]["name"], "论文草稿")
        self.assertEqual(payload["handoffs"]["total"], 1)
        self.assertEqual(payload["handoffs"]["recent"][0]["objective"], "把结论交出去")
        self.assertTrue(payload["gates"]["total"] >= 1)
        self.assertEqual(payload["reviews"]["total"], 1)
        self.assertEqual(payload["reviews"]["recent"][0]["verdict"], "APPROVED")
        self.assertEqual(payload["risks"]["total"], 0)
        self.assertTrue(review.id)

    def test_document_layer_follows_artifact_status(self) -> None:
        paper = self.store.create_artifact(
            self.project.id,
            ArtifactCreate(name="论文", artifact_type="paper_source", description="d"),
            created_by=self.lead.id,
        )
        payload = self.store.project_deliverables(self.project.id)
        document = next(item for item in payload["documents"]["recent"] if item["id"] == paper.id)
        self.assertEqual(document["layer"], "draft")

        evidence = self.store.create_evidence(
            self.project.id,
            EvidenceCreate(claim="论文证据", evidence_type="artifact", artifact_id=paper.id, created_by=self.lead.id),
        )
        self.store.submit_artifact_for_review(paper.id, actor=self.lead.id, evidence_ids=[evidence.id])
        payload = self.store.project_deliverables(self.project.id)
        document = next(item for item in payload["documents"]["recent"] if item["id"] == paper.id)
        self.assertEqual(document["layer"], "submitted")

    def test_deliverables_on_empty_project(self) -> None:
        payload = self.store.project_deliverables(self.project.id)
        self.assertEqual(payload["artifacts"]["total"], 0)
        self.assertEqual(payload["documents"]["by_layer"], {"draft": 0, "submitted": 0, "approved": 0})
        self.assertEqual(payload["handoffs"]["total"], 0)
        self.assertEqual(payload["reviews"]["total"], 0)

    def test_risk_counts(self) -> None:
        artifact = self.store.create_artifact(
            self.project.id,
            ArtifactCreate(name="待审", artifact_type="paper_source", description="d"),
            created_by=self.lead.id,
        )
        review = self.store.create_review(
            self.project.id,
            ReviewCreate(
                target_type="artifact",
                target_id=artifact.id,
                verdict="NEEDS_REVISION",
                summary="有问题",
                reviewer=self.lead.id,
                reviewer_kind=ReviewerKind.MEMBER,
                findings=[{"code": "content_mismatch", "severity": "major", "message": "结论与图不符"}],
            ),
        )
        payload = self.store.project_deliverables(self.project.id)
        self.assertGreaterEqual(payload["risks"]["total"], 1)
        self.assertGreaterEqual(payload["risks"]["open"], 1)
        self.assertTrue(review.id)

class HttpDispatchTests(unittest.TestCase):
    """HTTP 层回归：派单链路的 500 是"store 测试全绿、路由却少了个 Request 参数"造成的。

    这一组专门走 FastAPI 路由 + 中间件（含 required 模式的会话解析），
    保证"面板上点一下派单"和"成员认领"在 HTTP 上真的成立。
    """

    @classmethod
    def setUpClass(cls) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: F401
        except Exception:  # pragma: no cover - 依赖缺失时跳过
            raise unittest.SkipTest("需要 httpx 才能跑 HTTP 层断言")

    def setUp(self) -> None:
        from app import main

        self.main = main
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(self.store.close)
        self.previous_store = main.store
        main.store = self.store
        self.addCleanup(lambda: setattr(main, "store", self.previous_store))

        from fastapi.testclient import TestClient

        # 强制鉴权模式：生产就是这个档位（开发模式下匿名请求会被当成 member-001，测不出收口）
        import os
        from unittest import mock

        patches = mock.patch.dict(os.environ, {"PLATFORM_AUTH_MODE": "required"})
        patches.start()
        self.addCleanup(patches.stop)

        self.client = TestClient(main.app, raise_server_exceptions=False)
        self.lead = self.store.get_member("member-001")
        self.peer = self.store.create_member(
            HumanMemberCreate(
                organization_id=UUID(DEV_ORG_ID), team_id=UUID(DEV_TEAM_ID), email="http-peer@example.local", display_name="小王"
            )
        )
        self.project = self.store.create_project(
            ProjectCreate(name="W2 HTTP 测试", competition_pack="cumcm-2026", problem_code="A", created_by=self.lead.id, task_mode="hybrid")
        )
        self.store.add_project_member(self.project.id, self.peer.id, "contributor")
        self.task = self.store.create_task(self.project.id, TaskCreate(title="HTTP 任务", description="d"))
        self.lead_token = self.store.create_session(SessionCreate(member_id=self.lead.id)).token
        self.peer_token = self.store.create_session(SessionCreate(member_id=self.peer.id)).token

    def _headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def test_leader_can_assign_through_http(self) -> None:
        response = self.client.patch(
            f"/api/tasks/{self.task.id}",
            headers=self._headers(self.lead_token),
            json={"assignee_member_id": self.peer.id},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["assignee_member_id"], self.peer.id)

    def test_member_claim_and_reject_paths_through_http(self) -> None:
        # hybrid：成员认领无人任务
        response = self.client.patch(
            f"/api/tasks/{self.task.id}", headers=self._headers(self.peer_token), json={"assignee_member_id": self.peer.id}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["assignee_member_id"], self.peer.id)

        # 成员不能把别人的任务改派给自己
        second = self.store.create_task(self.project.id, TaskCreate(title="别人的任务", description="d"))
        self.store.update_task(
            second.id, None, None, None, actor=self.lead.id, actor_kind="member", assignee_member_id=self.lead.id, dispatch_provided=True
        )
        response = self.client.patch(
            f"/api/tasks/{second.id}", headers=self._headers(self.peer_token), json={"assignee_member_id": self.peer.id}
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn("task_assigned_to_another_member", response.text)

        # manual 模式下成员连无人任务也不能认领
        self.store.update_project_settings(self.project.id, task_mode="manual", set_team=False)
        third = self.store.create_task(self.project.id, TaskCreate(title="manual 任务", description="d"))
        response = self.client.patch(
            f"/api/tasks/{third.id}", headers=self._headers(self.peer_token), json={"assignee_member_id": self.peer.id}
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertIn("task_self_claim_disabled_in_manual_mode", response.text)

    def test_bulk_assign_endpoint(self) -> None:
        task_ids = [
            self.store.create_task(self.project.id, TaskCreate(title=f"批量 {index}", description="d")).id for index in range(2)
        ]
        response = self.client.post(
            f"/api/projects/{self.project.id}/tasks/assign",
            headers=self._headers(self.lead_token),
            json={"task_ids": [str(value) for value in task_ids], "assignee_member_id": self.peer.id},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["updated"], 2)
        self.assertEqual(body["failures"], [])

    def test_deliverables_endpoint_requires_session(self) -> None:
        anonymous = self.client.get(f"/api/projects/{self.project.id}/deliverables")
        self.assertEqual(anonymous.status_code, 401)
        response = self.client.get(f"/api/projects/{self.project.id}/deliverables", headers=self._headers(self.lead_token))
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertIn("artifacts", payload)
        self.assertIn("documents", payload)
        self.assertIn("gates", payload)

    def test_observer_cannot_dispatch_through_http(self) -> None:
        observer = self.store.create_member(
            HumanMemberCreate(
                organization_id=UUID(DEV_ORG_ID), team_id=UUID(DEV_TEAM_ID), email="watcher@example.local", display_name="观察者"
            )
        )
        self.store.add_project_member(self.project.id, observer.id, "observer")
        token = self.store.create_session(SessionCreate(member_id=observer.id)).token
        response = self.client.patch(
            f"/api/tasks/{self.task.id}", headers=self._headers(token), json={"assignee_member_id": observer.id}
        )
        self.assertIn(response.status_code, {401, 403}, response.text)
