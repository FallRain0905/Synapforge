"""W-1 项目工作区契约测试：立项目标/人数、任务推进模式、聊天流（人类 + 事件卡片）、概览聚合。

覆盖的边界：
  * 事件 → 卡片是服务端渲染（Agent 协议零改动），非卡事件（agent.offline 等运维噪声）不进流；
  * 水位线保证"定时兜底"不会重复写卡片，老项目的历史事件能被补进流；
  * 分页游标 before/after 各自语义正确；
  * project.chat 权限：reviewer 能发言、observer 只读。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import UUID, uuid4

from app.contracts import (
    AgentProjectGrant,
    AgentRegister,
    EvidenceCreate,
    HumanMemberCreate,
    ProjectCreate,
    TaskClaimRequest,
    TaskCreate,
    TaskProgressRequest,
)
from app.store import DEV_ORG_ID, DEV_TEAM_ID, Store


class WorkspaceFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(self.store.close)
        self.lead = self.store.get_member("member-001")
        self.project = self.store.create_project(
            ProjectCreate(
                name="W1 工作区测试",
                competition_pack="cumcm-2026",
                problem_code="A",
                description="测试项目",
                goal="把项目工作区跑通",
                target_member_count=4,
                task_mode="manual",
                created_by=self.lead.id,
            )
        )

    def _agent(self, agent_id: str = "agent-w1") -> str:
        self.store.register_agent(
            AgentRegister(
                agent_id=agent_id,
                display_name="工作区测试号",
                owner_member_id=self.lead.id,
                model_provider="openai",
                model_name="gpt-test",
            )
        )
        self.store.grant_agent_project(
            AgentProjectGrant(
                agent_id=agent_id,
                project_id=self.project.id,
                capabilities=["task.claim", "task.progress", "task.result", "run.create", "run.complete"],
                granted_by=self.lead.id,
            )
        )
        return agent_id

    def _member(self, name: str, role: str) -> str:
        member = self.store.create_member(
            HumanMemberCreate(
                organization_id=UUID(DEV_ORG_ID),
                team_id=UUID(DEV_TEAM_ID),
                email=f"{name.lower()}@example.local",
                display_name=name,
            )
        )
        self.store.add_project_member(self.project.id, member.id, role)
        return member.id


class ProjectBootstrapTests(WorkspaceFixture):
    def test_create_project_carries_goal_count_and_mode(self) -> None:
        project = self.store.get_project(self.project.id)
        self.assertEqual(project.goal, "把项目工作区跑通")
        self.assertEqual(project.target_member_count, 4)
        self.assertEqual(project.task_mode, "manual")

        # 创建者自动是队长（沿用既有 project_lead 角色，不新增枚举值）
        roles = {item["member_id"]: item["role"] for item in self.store.list_project_members(self.project.id)}
        self.assertEqual(roles[self.lead.id], "project_lead")

    def test_project_created_event_becomes_first_card(self) -> None:
        messages = self.store.list_project_messages(self.project.id, limit=10)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].message_type, "card")
        self.assertEqual(messages[0].sender_kind, "system")
        self.assertIn("W1 工作区测试", messages[0].content)


class ProjectSettingsTests(WorkspaceFixture):
    def test_patch_semantics_only_touch_given_fields(self) -> None:
        self.store.update_project_settings(self.project.id, goal="新的目标", set_team=False)
        project = self.store.get_project(self.project.id)
        self.assertEqual(project.goal, "新的目标")
        # 没给 task_mode 就不该动
        self.assertEqual(project.task_mode, "manual")

        self.store.update_project_settings(self.project.id, task_mode="hybrid", set_team=False)
        self.assertEqual(self.store.get_project(self.project.id).task_mode, "hybrid")
        messages = self.store.list_project_messages(self.project.id, limit=5)
        self.assertTrue(any("任务推进模式" in message.content for message in messages))

    def test_unknown_task_mode_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.store.update_project_settings(self.project.id, task_mode="turbo", set_team=False)

    def test_team_and_settings_can_change_together(self) -> None:
        other_org = self.store.create_organization(type("D", (), {"name": "外部组织", "slug": f"other-{uuid4().hex[:8]}"})())
        other_team = self.store.create_team(type("T", (), {"organization_id": other_org.id, "name": "外部队"})())
        with self.assertRaises(ValueError):
            self.store.update_project_settings(self.project.id, team_id=other_team.id, set_team=True)
        # 失败的团队校验不该顺手改掉别的字段
        self.assertEqual(self.store.get_project(self.project.id).goal, "把项目工作区跑通")


class ChatStreamTests(WorkspaceFixture):
    def test_human_messages_and_cursors(self) -> None:
        first = self.store.post_project_message(self.project.id, self.lead.id, "先分工")
        second = self.store.post_project_message(self.project.id, self.lead.id, "今晚建模")
        third = self.store.post_project_message(self.project.id, self.lead.id, "明早验收")

        self.assertEqual(first.sender_kind, "human")
        self.assertEqual(first.sender_name, self.lead.display_name)
        self.assertLess(first.seq, second.seq)
        self.assertLess(second.seq, third.seq)

        after = self.store.list_project_messages(self.project.id, after_seq=second.seq, limit=10)
        self.assertEqual([message.seq for message in after], [third.seq])

        before = self.store.list_project_messages(self.project.id, before_seq=third.seq, limit=10)
        spins = [message.seq for message in before]
        # 翻历史页拿的是"比游标更早"的最近 N 条（含项目创建卡片），按升序返回
        self.assertEqual(spins, sorted(spins))
        self.assertIn(first.seq, spins)
        self.assertEqual(spins[-1], second.seq)
        self.assertNotIn(third.seq, spins)

    def test_empty_content_is_rejected_by_contract(self) -> None:
        from pydantic import ValidationError

        from app.contracts import ProjectMessageCreate

        with self.assertRaises(ValidationError):
            ProjectMessageCreate(content="")

    def test_message_can_reference_a_project_artifact(self) -> None:
        """上传文件后那条消息要能挂成果物引用（点开跳到成果物库）——W-6。"""

        from app.contracts import ArtifactCreate

        artifact = self.store.create_artifact(
            self.project.id,
            ArtifactCreate(name="附件.txt", artifact_type="problem_source", description="d"),
            created_by=self.lead.id,
        )
        message = self.store.post_project_message(
            self.project.id, self.lead.id, "已上传「附件.txt」", ref_artifact_id=artifact.id
        )
        self.assertEqual(message.ref_artifact_id, artifact.id)
        self.assertEqual(message.message_type, "text")

    def test_message_rejects_foreign_artifact_reference(self) -> None:
        from app.contracts import ArtifactCreate

        other = self.store.create_project(
            ProjectCreate(name="另一个项目", competition_pack="cumcm-2026", created_by=self.lead.id)
        )
        foreign = self.store.create_artifact(
            other.id,
            ArtifactCreate(name="别人的成果物", artifact_type="problem_source", description="d"),
            created_by=self.lead.id,
        )
        with self.assertRaises(ValueError) as caught:
            self.store.post_project_message(self.project.id, self.lead.id, "越权引用", ref_artifact_id=foreign.id)
        self.assertEqual(str(caught.exception), "message_artifact_not_in_project")

    def test_reviewer_can_chat_observer_cannot(self) -> None:
        reviewer = self._member("Reviewer", "reviewer")
        observer = self._member("Observer", "observer")
        self.store.authorize_member(self.project.id, reviewer, "project.chat")
        with self.assertRaises(PermissionError):
            self.store.authorize_member(self.project.id, observer, "project.chat")

    def test_agent_actions_become_cards_with_refs(self) -> None:
        agent_id = self._agent()
        task = self.store.create_task(self.project.id, TaskCreate(title="灵敏度分析", description="d", assignee="Unassigned"))
        claimed, lease = self.store.claim_task(
            task.id, TaskClaimRequest(agent_id=agent_id, lease_seconds=600, idempotency_key="w1-claim-key")
        )
        self.store.update_task_progress(
            task.id,
            TaskProgressRequest(
                agent_id=agent_id, lease_token=lease.lease_token, status="RUNNING", message="开始枚举参数", idempotency_key="w1-progress-key"
            ),
        )

        messages = self.store.list_project_messages(self.project.id, limit=50)
        contents = [message.content for message in messages]
        self.assertTrue(any("领取了任务「灵敏度分析」" in item for item in contents))
        self.assertTrue(any("执行中：开始枚举参数" in item for item in contents))

        claimed_card = next(message for message in messages if "领取了任务" in message.content)
        self.assertEqual(claimed_card.sender_kind, "agent")
        self.assertEqual(claimed_card.sender_name, "工作区测试号")
        self.assertEqual(claimed_card.ref_task_id, task.id)
        self.assertIsNotNone(claimed_card.ref_event_id)

    def test_system_cards_and_noise_filter(self) -> None:
        self.store.create_task(self.project.id, TaskCreate(title="审题", description="d"))
        messages = self.store.list_project_messages(self.project.id, limit=20)
        self.assertTrue(any("创建了任务「审题」" in message.content for message in messages))
        self.assertFalse(any("agent.offline" in message.content for message in messages))

    def test_catch_up_backfills_legacy_events_and_is_idempotent(self) -> None:
        # 直接插一行事件（绕过桥），模拟"迁移前就存在的老事件"
        import json as jsonlib
        from datetime import UTC, datetime

        from uuid import uuid4 as uuid4_

        self.store.db.execute(
            "INSERT INTO events (id, project_id, sequence, event_type, actor, payload, created_at, actor_kind, object_type, object_id, idempotency_key, schema_version) "
            "VALUES (?, ?, ?, 'task.created', 'member-001', ?, ?, 'member', NULL, NULL, NULL, '1.0')",
            (
                str(uuid4_()),
                str(self.project.id),
                self.store.db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM events WHERE project_id = ?", (str(self.project.id),)).fetchone()[0],
                jsonlib.dumps({"task_id": str(uuid4()), "title": "历史任务"}, ensure_ascii=False),
                datetime.now(UTC).isoformat(),
            ),
        )
        self.store.db.commit()
        created = self.store.catch_up_project_messages(self.project.id)
        self.assertEqual([message.content for message in created], ["创建了任务「历史任务」"])
        # 幂等：再跑一次不会重复
        self.assertEqual(self.store.catch_up_project_messages(self.project.id), [])
        contents = [message.content for message in self.store.list_project_messages(self.project.id, limit=50)]
        self.assertEqual(contents.count("创建了任务「历史任务」"), 1)

    def test_non_card_events_do_not_block_later_cards(self) -> None:
        # 一串非卡事件之后紧跟一张卡：水位线按"已检查序号"推进，不能漏掉后面的卡
        self.store.create_evidence(
            self.project.id,
            EvidenceCreate(claim="一条非卡的边注", evidence_type="external_source", source_ref="note", created_by=self.lead.id),
        )
        self.store.create_task(self.project.id, TaskCreate(title="后置任务", description="d"))
        contents = [message.content for message in self.store.list_project_messages(self.project.id, limit=50)]
        self.assertTrue(any("后置任务" in item for item in contents))


class MiddlewarePermissionTests(unittest.TestCase):
    """工作区路径的中间件权限映射（此前踩过"路径全判成 admin"的坑，这里钉住语义）。"""

    @staticmethod
    def _permission(method: str, path: str) -> str:
        from app import main

        class _Url:
            def __init__(self, value: str) -> None:
                self.path = value

        class _Request:
            def __init__(self, method: str, path: str) -> None:
                self.method = method
                self.url = _Url(path)

        return main._project_permission(_Request(method, path))

    def test_message_and_workspace_paths(self) -> None:
        self.assertEqual(self._permission("POST", f"/api/projects/{uuid4()}/messages"), "project.chat")
        self.assertEqual(self._permission("GET", f"/api/projects/{uuid4()}/messages"), "project.view")
        self.assertEqual(self._permission("GET", f"/api/projects/{uuid4()}/workspace"), "project.view")
        self.assertEqual(self._permission("POST", f"/api/projects/{uuid4()}/tasks"), "project.write")
        self.assertEqual(self._permission("PATCH", f"/api/projects/{uuid4()}"), "project.write")
        # AI 会话的消息路径不能被当成项目聊天（那条路径没有项目 id，中间件本就不会走项目授权）
        self.assertNotEqual(self._permission("POST", "/api/ai/conversations/abc/messages"), "project.chat")


class OverviewTests(WorkspaceFixture):
    def test_overview_aggregates_members_agents_tasks_artifacts(self) -> None:
        agent_id = self._agent()
        task = self.store.create_task(self.project.id, TaskCreate(title="汇总", description="d", assignee="Unassigned"))
        self.store.claim_task(task.id, TaskClaimRequest(agent_id=agent_id, lease_seconds=600, idempotency_key="w1-overview-key"))
        self.store.create_evidence(
            self.project.id,
            EvidenceCreate(claim="证据", evidence_type="external_source", source_ref="note", created_by=self.lead.id),
        )

        overview = self.store.project_workspace(self.project.id)
        self.assertEqual(overview["project"].id, self.project.id)
        self.assertEqual(overview["project"].goal, "把项目工作区跑通")
        self.assertTrue(overview["members"])
        self.assertEqual(overview["members"][0]["role"], "project_lead")

        self.assertEqual(len(overview["agents"]), 1)
        agent_row = overview["agents"][0]
        self.assertEqual(agent_row["agent_id"], agent_id)
        self.assertEqual(agent_row["owner_name"], self.lead.display_name)
        self.assertEqual(agent_row["current_task_title"], "汇总")

        self.assertEqual(overview["tasks"]["total"], 1)
        self.assertEqual(overview["tasks"]["by_status"].get("CLAIMED"), 1)
        self.assertEqual([item["title"] for item in overview["tasks"]["open_items"]], ["汇总"])
        self.assertIn("total", overview["artifacts"])
        self.assertTrue(overview["messages"])

    def test_viewer_identity_is_the_requesting_member(self) -> None:
        # 回归：成员列表循环曾经覆盖了参数 member_id，导致"我是谁"变成列表最后一个人，
        # 队长因此拿不到模式开关、普通成员反而可能有管理权。
        other = self._member("Other", "contributor")
        viewer = self.store.project_workspace(self.project.id, member_id=self.lead.id)["viewer"]
        self.assertEqual(viewer["member_id"], self.lead.id)
        self.assertEqual(viewer["role"], "project_lead")
        self.assertTrue(viewer["can_manage"])

        viewer = self.store.project_workspace(self.project.id, member_id=other)["viewer"]
        self.assertEqual(viewer["member_id"], other)
        self.assertEqual(viewer["role"], "contributor")
        self.assertTrue(viewer["can_chat"])
        self.assertFalse(viewer["can_manage"])

        # observer 只读：不能发言、不能管理
        observer = self._member("Watcher", "observer")
        viewer = self.store.project_workspace(self.project.id, member_id=observer)["viewer"]
        self.assertFalse(viewer["can_chat"])
        self.assertFalse(viewer["can_manage"])

    def test_overview_artifact_counts(self) -> None:
        from app.contracts import ArtifactCreate

        self.store.create_artifact(self.project.id, ArtifactCreate(name="论文草稿", artifact_type="paper_source", description="d"), created_by=self.lead.id)
        overview = self.store.project_workspace(self.project.id)
        self.assertEqual(overview["artifacts"]["total"], 1)
        self.assertEqual(overview["artifacts"]["recent"][0]["name"], "论文草稿")

class MessageTaskReferenceTests(WorkspaceFixture):
    """聊天区 /task 直接建任务（W-10）：消息可挂任务引用，跨项目引用必须被拒。"""

    def test_message_can_reference_a_project_task(self) -> None:
        task = self.store.create_task(self.project.id, TaskCreate(title="从聊天区建的任务", description="d"))
        message = self.store.post_project_message(
            self.project.id, self.lead.id, "已新建任务「从聊天区建的任务」", ref_task_id=task.id
        )
        self.assertEqual(message.ref_task_id, task.id)
        self.assertIsNone(message.ref_artifact_id)

    def test_message_rejects_foreign_task_reference(self) -> None:
        other = self.store.create_project(
            ProjectCreate(name="另一个项目", competition_pack="cumcm-2026", created_by=self.lead.id)
        )
        foreign = self.store.create_task(other.id, TaskCreate(title="别人项目的任务", description="d"))
        with self.assertRaises(ValueError) as caught:
            self.store.post_project_message(self.project.id, self.lead.id, "越权引用", ref_task_id=foreign.id)
        self.assertEqual(str(caught.exception), "message_task_not_in_project")
