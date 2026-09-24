"""能力与截止时间在领取时被真正消费（P2 第 5 条）。

覆盖：
  * required_capabilities 不满足的任务：轮询跳过、点名领取也拒绝
  * 声明了能力之后即可领取（能力来自 Agent 自报 tools ∪ 设备声明 ∪ 心跳上报）
  * 已过截止时间的任务不再自动领取，点名领取也拒绝
  * 排序：有截止时间的优先（最早在前）
  * PATCH 改期与清除截止时间
  * 个人任务中心标记"已过期"
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from app.contracts import AgentRegister, HumanMemberCreate, ProjectCreate, TaskClaimRequest, TaskCreate
from app.store import DEV_ORG_ID, DEV_TEAM_ID, Store


class CapabilityAndDeadlineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(self.store.close)

        self.lead = self.store.get_member("member-001")
        self.project = self.store.create_project(
            ProjectCreate(name="能力与截止时间测试", competition_pack="cumcm-2026", problem_code="A", created_by=self.lead.id)
        )
        self.member = self.store.create_member(
            HumanMemberCreate(organization_id=UUID(DEV_ORG_ID), team_id=UUID(DEV_TEAM_ID), email="cap@example.local", display_name="Capability Owner")
        )
        self.store.add_project_member(self.project.id, self.member.id, "contributor")
        # 一个"什么能力都没声明"的 Agent
        self.store.register_agent(AgentRegister(agent_id="agent-plain", display_name="无能力声明", owner_member_id=self.member.id))
        self.store.db.execute(
            "INSERT OR REPLACE INTO agent_project_grants (agent_id, project_id, capabilities, granted_by, created_at) VALUES (?, ?, ?, ?, ?)",
            ("agent-plain", str(self.project.id), "[]", self.lead.id, "2026-01-01T00:00:00+00:00"),
        )
        self.store.db.commit()

    def claim_request(self) -> TaskClaimRequest:
        return TaskClaimRequest(agent_id="agent-plain", lease_seconds=600, idempotency_key=uuid4().hex)

    def declare_capabilities(self, tools: list[str]) -> None:
        self.store.db.execute("UPDATE agents SET supported_tools = ? WHERE agent_id = 'agent-plain'", (json.dumps(tools),))
        self.store.db.commit()

    def make_task(self, title: str, **overrides):
        return self.store.create_task(self.project.id, TaskCreate(title=title, description="测试", **overrides))

    def test_missing_capability_blocks_claim_until_declared(self) -> None:
        from app.contracts import TaskStatus

        task = self.make_task("需要 codex 的活", required_capabilities=["codex"])
        # 没声明能力：轮询跳过
        self.assertIsNone(self.store.claim_next_task(self.claim_request(), self.project.id))
        # 点名领取：明确拒绝（而不是领下来再失败）
        with self.assertRaisesRegex(PermissionError, "task_capabilities_not_satisfied"):
            self.store.claim_task(task.id, self.claim_request())

        # 声明能力后可以领取
        self.declare_capabilities(["codex"])
        claimed, _ = self.store.claim_next_task(self.claim_request(), self.project.id)
        self.assertEqual(claimed.id, task.id)
        self.assertEqual(self.store.get_task(task.id).status, "CLAIMED")

    def test_task_without_requirements_is_unaffected(self) -> None:
        task = self.make_task("没有能力要求的活")
        claimed, _ = self.store.claim_next_task(self.claim_request(), self.project.id)
        self.assertEqual(claimed.id, task.id)

    def test_device_declared_capabilities_count(self) -> None:
        # 设备声明的能力同样算数（Agent 自己没声明时）
        self.store.db.execute(
            "INSERT INTO devices (device_id, organization_id, agent_id, owner_member_id, device_name, public_key, public_key_fingerprint, device_token_hash, platform, agent_version, capabilities, status, created_at, last_seen, revoked_at) "
            "VALUES ('device-cap', ?, 'agent-plain', ?, '能力设备', 'pk', 'fp-cap', 'hash-cap', 'windows', '0.1.0', ?, 'active', ?, ?, NULL)",
            (DEV_ORG_ID, self.member.id, json.dumps(["python"]), "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        self.store.db.execute(
            "INSERT INTO agent_connections (connection_id, device_id, agent_id, session_id, transport, status, last_received_sequence, last_sent_sequence, connected_at, last_heartbeat_at, disconnected_at) "
            "VALUES ('conn-cap', 'device-cap', 'agent-plain', 's1', 'websocket', 'connected', 0, 0, ?, ?, NULL)",
            ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        self.store.db.commit()
        task = self.make_task("需要 python 的活", required_capabilities=["python"])
        claimed, _ = self.store.claim_next_task(self.claim_request(), self.project.id)
        self.assertEqual(claimed.id, task.id)

    def test_expired_deadline_is_not_claimed(self) -> None:
        past = datetime.now(UTC) - timedelta(hours=2)
        task = self.make_task("已经过期", deadline=past)
        self.assertIsNone(self.store.claim_next_task(self.claim_request(), self.project.id))
        with self.assertRaisesRegex(ValueError, "task_deadline_passed"):
            self.store.claim_task(task.id, self.claim_request())

    def test_deadline_ordering_prefers_earliest(self) -> None:
        now = datetime.now(UTC)
        late = self.make_task("晚的截止", deadline=now + timedelta(days=7))
        soon = self.make_task("早的截止", deadline=now + timedelta(hours=3))
        none = self.make_task("没有截止时间")
        claimed, _ = self.store.claim_next_task(self.claim_request(), self.project.id)
        self.assertEqual(claimed.id, soon.id)
        # 再领一次应拿到次早（无截止时间的排最后）
        claimed2, _ = self.store.claim_next_task(self.claim_request(), self.project.id)
        self.assertEqual(claimed2.id, late.id)
        claimed3, _ = self.store.claim_next_task(self.claim_request(), self.project.id)
        self.assertEqual(claimed3.id, none.id)

    def test_patch_can_set_and_clear_deadline(self) -> None:
        task = self.make_task("可改期")
        future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        self.store.update_task(task.id, None, None, None, deadline=future, deadline_provided=True)
        self.assertEqual(self.store.get_task(task.id).deadline.isoformat(), future)
        # 空串 = 清除
        self.store.update_task(task.id, None, None, None, deadline="", deadline_provided=True)
        self.assertIsNone(self.store.get_task(task.id).deadline)
        # 未提供 = 不动
        self.store.update_task(task.id, None, None, None, deadline_provided=False)
        self.assertIsNone(self.store.get_task(task.id).deadline)

    def test_my_tasks_flags_passed_deadline(self) -> None:
        past = datetime.now(UTC) - timedelta(minutes=5)
        self.make_task("派给我但过期了", assignee_member_id=self.member.id, deadline=past)
        board = self.store.my_tasks(self.member.id)
        self.assertEqual(len(board["assigned"]), 1)
        self.assertTrue(board["assigned"][0]["deadline_passed"])


if __name__ == "__main__":
    unittest.main()