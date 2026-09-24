"""W-3 契约测试：auto 模式的自动调度器。

口径（按推荐执行并已写进交接）：**每个 tick 每个项目只推进一件事**——派一条任务，
或提示一次"没人能跑"。调度器只做低风险可逆的两件事（能力匹配派单、派不出去时提示），
**不做自动批准**；队长切回 manual 立刻停手。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from app.contracts import (
    AgentProjectGrant,
    AgentRegister,
    HumanMemberCreate,
    ProjectCreate,
    TaskCreate,
)
from app.store import DEV_ORG_ID, DEV_TEAM_ID, Store


class AutoFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(self.store.close)
        self.lead = self.store.get_member("member-001")
        self.project = self.store.create_project(
            ProjectCreate(
                name="W3 自动调度测试",
                competition_pack="cumcm-2026",
                problem_code="A",
                task_mode="auto",
                created_by=self.lead.id,
            )
        )
        self.peer = self.store.create_member(
            HumanMemberCreate(
                organization_id=UUID(DEV_ORG_ID), team_id=UUID(DEV_TEAM_ID), email="auto-peer@example.local", display_name="小林"
            )
        )
        self.store.add_project_member(self.project.id, self.peer.id, "contributor")

    def _agent(self, owner: str, agent_id: str, *, tools: list[str], online: bool = True) -> str:
        self.store.register_agent(
            AgentRegister(
                agent_id=agent_id,
                display_name=f"{agent_id} 执行体",
                owner_member_id=owner,
                model_provider="openai",
                model_name="gpt-test",
                supported_tools=tools,
            )
        )
        self.store.grant_agent_project(
            AgentProjectGrant(
                agent_id=agent_id,
                project_id=self.project.id,
                capabilities=["task.claim", "task.progress", "task.result"],
                granted_by=self.lead.id,
            )
        )
        if not online:
            self.store.db.execute("UPDATE agents SET status = 'offline' WHERE agent_id = ?", (agent_id,))
            self.store.db.commit()
        else:
            self.store.heartbeat(agent_id)
        return agent_id

    def _task(self, title: str, *, capabilities: list[str] | None = None, deadline: str | None = None, deps: list[UUID] | None = None):
        return self.store.create_task(
            self.project.id,
            TaskCreate(
                title=title,
                description="d",
                required_capabilities=capabilities or [],
                deadline=deadline,
                dependency_task_ids=deps or [],
            ),
        )


class AutoDispatchTests(AutoFixture):
    def test_assigns_to_member_with_matching_online_agent(self) -> None:
        self._agent(self.peer.id, "agent-python", tools=["python", "pandas"])
        task = self._task("建模求解", capabilities=["python"])

        events = self.store.auto_dispatch_tick(self.project.id)
        self.assertEqual([str(event.event_type) for event in events], ["task.auto_assigned"])
        self.assertEqual(self.store.get_task(task.id).assignee_member_id, self.peer.id)

        payload = events[0].payload
        self.assertEqual(payload["agent_id"], "agent-python")
        self.assertEqual(payload["matched_capabilities"], ["python"])
        contents = [message.content for message in self.store.list_project_messages(self.project.id, limit=10)]
        self.assertTrue(any("全自动：把任务「建模求解」派给了 小林" in item for item in contents))

    def test_only_one_task_per_tick(self) -> None:
        self._agent(self.peer.id, "agent-python", tools=["python"])
        self._task("任务一", capabilities=["python"])
        self._task("任务二", capabilities=["python"])

        first = self.store.auto_dispatch_tick(self.project.id)
        second = self.store.auto_dispatch_tick(self.project.id)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        assigned = [task.assignee_member_id for task in self.store.list_tasks(self.project.id)]
        self.assertEqual(assigned, [self.peer.id, self.peer.id])

    def test_skips_when_capability_unsatisfied_and_warns_once(self) -> None:
        self._agent(self.peer.id, "agent-python", tools=["python"])
        self._task("需要 GPU 的任务", capabilities=["cuda"])

        events = self.store.auto_dispatch_tick(self.project.id)
        self.assertEqual([str(event.event_type) for event in events], ["task.auto_unmatched"])
        task = self.store.list_tasks(self.project.id)[0]
        self.assertIsNone(task.assignee_member_id)

        # 只提示一次（幂等键），不会每 10 秒刷一条
        self.assertEqual(self.store.auto_dispatch_tick(self.project.id), [])
        contents = [message.content for message in self.store.list_project_messages(self.project.id, limit=20)]
        self.assertEqual(sum(1 for item in contents if "没有在线执行体能跑" in item), 1)

    def test_offline_agent_is_not_used(self) -> None:
        self._agent(self.peer.id, "agent-offline", tools=["python"], online=False)
        self._task("离线执行体的任务", capabilities=["python"])

        events = self.store.auto_dispatch_tick(self.project.id)
        self.assertEqual([str(event.event_type) for event in events], ["task.auto_unmatched"])
        self.assertIsNone(self.store.list_tasks(self.project.id)[0].assignee_member_id)

    def test_task_without_requirements_needs_no_warning(self) -> None:
        self._agent(self.peer.id, "agent-any", tools=[])
        self._task("无能力要求的任务")

        events = self.store.auto_dispatch_tick(self.project.id)
        # 无要求 → 任何在线执行体都能跑 → 直接派出去（不该走"没人能跑"分支）
        self.assertEqual([str(event.event_type) for event in events], ["task.auto_assigned"])

    def test_manual_and_hybrid_projects_are_untouched(self) -> None:
        self._agent(self.peer.id, "agent-python", tools=["python"])
        self._task("任务", capabilities=["python"])
        for mode in ("manual", "hybrid"):
            self.store.update_project_settings(self.project.id, task_mode=mode, set_team=False)
            self.assertEqual(self.store.auto_dispatch_tick(self.project.id), [])
            self.assertIsNone(self.store.list_tasks(self.project.id)[0].assignee_member_id)

    def test_switching_back_to_manual_stops_dispatch(self) -> None:
        self._agent(self.peer.id, "agent-python", tools=["python"])
        self._task("任务一", capabilities=["python"])
        self._task("任务二", capabilities=["python"])

        self.assertEqual(len(self.store.auto_dispatch_tick(self.project.id)), 1)
        self.store.update_project_settings(self.project.id, task_mode="manual", set_team=False)
        self.assertEqual(self.store.auto_dispatch_tick(self.project.id), [])
        assigned = [task.assignee_member_id for task in self.store.list_tasks(self.project.id)]
        self.assertEqual(assigned.count(self.peer.id), 1)

    def test_load_balancing_picks_least_busy_member(self) -> None:
        other = self.store.create_member(
            HumanMemberCreate(
                organization_id=UUID(DEV_ORG_ID), team_id=UUID(DEV_TEAM_ID), email="auto-other@example.local", display_name="小周"
            )
        )
        self.store.add_project_member(self.project.id, other.id, "contributor")
        self._agent(self.peer.id, "agent-a", tools=["python"])
        self._agent(other.id, "agent-b", tools=["python"])

        # 先给小林派两条（走队长手工派单，制造负载差）
        busy = [self._task(f"已派 {index}", capabilities=["python"]).id for index in range(2)]
        for task_id in busy:
            self.store.update_task(
                task_id, None, None, None, actor=self.lead.id, actor_kind="member", assignee_member_id=self.peer.id, dispatch_provided=True
            )
        self._task("新任务", capabilities=["python"])

        events = self.store.auto_dispatch_tick(self.project.id)
        self.assertEqual(events[0].payload["assignee_member_id"], other.id)

    def test_deadline_passed_is_skipped(self) -> None:
        self._agent(self.peer.id, "agent-python", tools=["python"])
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        self._task("过期任务", capabilities=["python"], deadline=past)

        self.assertEqual(self.store.auto_dispatch_tick(self.project.id), [])
        self.assertIsNone(self.store.list_tasks(self.project.id)[0].assignee_member_id)

    def test_dependency_not_ready_is_skipped(self) -> None:
        self._agent(self.peer.id, "agent-python", tools=["python"])
        upstream = self._task("上游", capabilities=["python"])
        downstream = self._task("下游", capabilities=["python"], deps=[upstream.id])

        # 上游先被派出去；下游依赖没满足，不该被提前派人
        self.store.auto_dispatch_tick(self.project.id)
        events = self.store.auto_dispatch_tick(self.project.id)
        self.assertEqual(events, [])
        tasks = {task.title: task for task in self.store.list_tasks(self.project.id)}
        self.assertEqual(tasks["上游"].assignee_member_id, self.peer.id)
        self.assertIsNone(tasks["下游"].assignee_member_id)

    def test_already_assigned_task_is_not_reassigned(self) -> None:
        self._agent(self.peer.id, "agent-python", tools=["python"])
        task = self._task("已派任务", capabilities=["python"])
        self.store.update_task(
            task.id, None, None, None, actor=self.lead.id, actor_kind="member", assignee_member_id=self.lead.id, dispatch_provided=True
        )
        self.assertEqual(self.store.auto_dispatch_tick(self.project.id), [])
        self.assertEqual(self.store.get_task(task.id).assignee_member_id, self.lead.id)

    def test_observer_owned_agent_is_not_a_target(self) -> None:
        watcher = self.store.create_member(
            HumanMemberCreate(
                organization_id=UUID(DEV_ORG_ID), team_id=UUID(DEV_TEAM_ID), email="auto-watch@example.local", display_name="旁观"
            )
        )
        self.store.add_project_member(self.project.id, watcher.id, "observer")
        self._agent(watcher.id, "agent-watch", tools=["python"])
        self._task("任务", capabilities=["python"])

        events = self.store.auto_dispatch_tick(self.project.id)
        self.assertEqual([str(event.event_type) for event in events], ["task.auto_unmatched"])
        self.assertIsNone(self.store.list_tasks(self.project.id)[0].assignee_member_id)

    def test_agent_without_project_grant_is_not_used(self) -> None:
        self.store.register_agent(
            AgentRegister(
                agent_id="agent-ungranted",
                display_name="未授权执行体",
                owner_member_id=self.peer.id,
                model_provider="openai",
                model_name="gpt-test",
                supported_tools=["python"],
            )
        )
        self.store.heartbeat("agent-ungranted")
        self._task("任务", capabilities=["python"])
        events = self.store.auto_dispatch_tick(self.project.id)
        self.assertEqual([str(event.event_type) for event in events], ["task.auto_unmatched"])


class AutoIntegrationTests(AutoFixture):
    def test_maintenance_pass_dispatches_for_auto_projects(self) -> None:
        """维护扫描（Demo 的 10 秒拍）会把 auto 项目的任务推出去——这是"自己动起来"的入口。"""

        from app import main

        previous = main.store
        main.store = self.store
        self.addCleanup(lambda: setattr(main, "store", previous))

        self._agent(self.peer.id, "agent-python", tools=["python"])
        self._task("维护拍任务", capabilities=["python"])
        main._maintenance_pass()
        self.assertEqual(self.store.list_tasks(self.project.id)[0].assignee_member_id, self.peer.id)

    def test_auto_assignment_card_is_in_chat_stream(self) -> None:
        self._agent(self.peer.id, "agent-python", tools=["python"])
        self._task("卡片任务", capabilities=["python"])
        self.store.auto_dispatch_tick(self.project.id)
        cards = [message for message in self.store.list_project_messages(self.project.id, limit=20) if message.message_type == "card"]
        self.assertTrue(any("全自动：把任务「卡片任务」派给了 小林（能力匹配：python）" == card.content for card in cards))
        self.assertTrue(all(card.sender_kind in {"system", "agent"} for card in cards))