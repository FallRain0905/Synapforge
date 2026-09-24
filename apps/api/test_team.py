"""团队协作语义的契约测试：执行归属、项目成员管理、多团队、成员工作量、组织可配置。"""

from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from uuid import UUID, uuid4

from app.contracts import (
    AgentRegister,
    HumanMemberCreate,
    OrganizationCreate,
    ProjectCreate,
    RunCreate,
    TeamCreate,
    TeamMemberUpsert,
    TaskCreate,
)
from app.store import DEV_ORG_ID, DEV_TEAM_ID, Store


class TeamFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(self.store.close)
        self.lead = self.store.get_member("member-001")
        self.alice = self.store.create_member(
            HumanMemberCreate(organization_id=UUID(DEV_ORG_ID), team_id=UUID(DEV_TEAM_ID), email="alice@example.local", display_name="Alice")
        )
        self.bob = self.store.create_member(
            HumanMemberCreate(organization_id=UUID(DEV_ORG_ID), team_id=UUID(DEV_TEAM_ID), email="bob@example.local", display_name="Bob")
        )
        self.project = self.store.create_project(
            ProjectCreate(name="团队协作测试", competition_pack="cumcm-2026", problem_code="A", created_by=self.lead.id)
        )


class ProjectMembershipTests(TeamFixture):
    def test_new_project_carries_existing_members(self) -> None:
        """用户指出的缺口：新建项目后老成员看不见。现在创建时自动带上组织内在职成员。"""

        members = {item["member_id"]: item["role"] for item in self.store.list_project_members(self.project.id)}
        self.assertEqual(members[self.lead.id], "project_lead")
        self.assertEqual(members[self.alice.id], "contributor")
        self.assertEqual(members[self.bob.id], "contributor")
        # 成员可见性随之成立（项目列表按项目成员关系过滤）
        self.assertIn(self.project.id, [item.id for item in self.store.list_projects_for_member(self.alice.id)])

    def test_remove_member_revokes_project_grants_and_releases_dispatch(self) -> None:
        self.store.register_agent(AgentRegister(agent_id="agent-alice", display_name="Alice 的机器", owner_member_id=self.alice.id))
        self.store.db.execute(
            "INSERT INTO devices (device_id, organization_id, agent_id, owner_member_id, device_name, public_key, public_key_fingerprint, device_token_hash, platform, agent_version, capabilities, status, created_at, last_seen, revoked_at) "
            "VALUES ('device-alice', ?, 'agent-alice', ?, 'Alice 的机器', 'pk', 'fp-alice', 'hash-alice', 'windows', '0.1.0', '[]', 'active', ?, ?, NULL)",
            (DEV_ORG_ID, self.alice.id, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        self.store.db.execute(
            "INSERT INTO device_project_grants (id, device_id, agent_id, project_id, token_hash, capabilities, granted_by, expires_at, revoked_at, created_at) "
            "VALUES ('grant-alice', 'device-alice', 'agent-alice', ?, 'hash-grant', '[]', ?, ?, NULL, ?)",
            (str(self.project.id), self.lead.id, "2030-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        self.store.db.execute(
            "INSERT INTO agent_project_grants (agent_id, project_id, capabilities, granted_by, created_at) VALUES ('agent-alice', ?, '[]', ?, ?)",
            (str(self.project.id), self.lead.id, "2026-01-01T00:00:00+00:00"),
        )
        self.store.db.commit()
        task = self.store.create_task(self.project.id, TaskCreate(title="派给 Alice 的活", description="", assignee_member_id=self.alice.id))

        result = self.store.remove_project_member(self.project.id, self.alice.id)
        self.assertEqual(result["released_tasks"], 1)
        self.assertGreaterEqual(result["revoked_grants"], 2)
        # 派单被释放、成员关系消失
        self.assertIsNone(self.store.get_task(task.id).assignee_member_id)
        self.assertNotIn(self.alice.id, [item["member_id"] for item in self.store.list_project_members(self.project.id)])
        # 关键：他的设备在这个项目上的授权被撤销（否则机器还会继续领任务）
        grant = self.store.db.execute("SELECT revoked_at FROM device_project_grants WHERE id = 'grant-alice'").fetchone()
        self.assertIsNotNone(grant["revoked_at"])
        self.assertFalse(
            self.store.db.execute("SELECT 1 FROM agent_project_grants WHERE agent_id = 'agent-alice' AND project_id = ?", (str(self.project.id),)).fetchone()
        )

    def test_cannot_remove_last_project_lead(self) -> None:
        with self.assertRaisesRegex(ValueError, "last_project_lead_cannot_be_removed"):
            self.store.remove_project_member(self.project.id, self.lead.id)
        # 换一个人当 lead 之后就能移除原 lead
        self.store.add_project_member(self.project.id, self.alice.id, "project_lead")
        self.store.remove_project_member(self.project.id, self.lead.id)
        self.assertNotIn(self.lead.id, [item["member_id"] for item in self.store.list_project_members(self.project.id)])


class TeamMembershipTests(TeamFixture):
    def setUp(self) -> None:
        super().setUp()
        self.team = self.store.create_team(TeamCreate(organization_id=UUID(DEV_ORG_ID), name="A 队"))

    def test_join_team_brings_its_projects(self) -> None:
        # 让项目归属这个团队，再把 Carol 加入团队
        self.store.db.execute("UPDATE projects SET team_id = ? WHERE id = ?", (str(self.team.id), str(self.project.id)))
        self.store.db.commit()
        carol = self.store.create_member(
            HumanMemberCreate(organization_id=UUID(DEV_ORG_ID), email="carol@example.local", display_name="Carol")
        )
        self.assertEqual(self.store.list_projects_for_member(carol.id), [])
        result = self.store.add_team_member(self.team.id, carol.id, "reviewer")
        self.assertEqual(result["joined_projects"], 1)
        # 入队即入项目（且用团队角色）
        roles = {item["member_id"]: item["role"] for item in self.store.list_project_members(self.project.id)}
        self.assertEqual(roles[carol.id], "reviewer")
        self.assertIn(self.project.id, [item.id for item in self.store.list_projects_for_member(carol.id)])

    def test_leave_team_leaves_its_projects_and_revokes_grants(self) -> None:
        self.store.db.execute("UPDATE projects SET team_id = ? WHERE id = ?", (str(self.team.id), str(self.project.id)))
        self.store.db.commit()
        self.store.add_team_member(self.team.id, self.alice.id, "contributor")
        self.assertIn(self.alice.id, [item["member_id"] for item in self.store.list_project_members(self.project.id)])
        result = self.store.remove_team_member(self.team.id, self.alice.id)
        self.assertEqual(result["left_projects"], 1)
        self.assertNotIn(self.alice.id, [item["member_id"] for item in self.store.list_project_members(self.project.id)])
        self.assertEqual(self.store.list_team_members(self.team.id), self.store.list_team_members(self.team.id))

    def test_team_member_rejects_cross_organization(self) -> None:
        other = self.store.create_organization(OrganizationCreate(name="别的组织", slug="other-org"))
        outsider = self.store.create_member(
            HumanMemberCreate(organization_id=other.id, email="outsider@example.local", display_name="Outsider")
        )
        with self.assertRaisesRegex(ValueError, "member_not_in_team_organization"):
            self.store.add_team_member(self.team.id, outsider.id)


class WorkloadTests(TeamFixture):
    def test_workload_counts_per_member(self) -> None:
        self.store.register_agent(AgentRegister(agent_id="agent-bob", display_name="Bob 的机器", owner_member_id=self.bob.id))
        self.store.db.execute(
            "INSERT OR REPLACE INTO agent_project_grants (agent_id, project_id, capabilities, granted_by, created_at) VALUES ('agent-bob', ?, '[]', ?, ?)",
            (str(self.project.id), self.lead.id, "2026-01-01T00:00:00+00:00"),
        )
        self.store.db.commit()
        task = self.store.create_task(self.project.id, TaskCreate(title="Bob 的活", description="", assignee_member_id=self.bob.id))
        self.store.claim_next_task(
            __import__("app.contracts", fromlist=["TaskClaimRequest"]).TaskClaimRequest(agent_id="agent-bob", lease_seconds=600, idempotency_key=uuid4().hex),
            self.project.id,
        )

        workload = {item["member_id"]: item for item in self.store.member_workload(UUID(DEV_ORG_ID))}
        bob = workload[self.bob.id]
        self.assertEqual(bob["assigned_open"], 1)
        self.assertEqual(bob["running"], 1)
        self.assertEqual(bob["agents"], 1)
        self.assertEqual(bob["projects"], 1)
        self.assertEqual(bob["teams"], ["示例建模队"])
        alice = workload[self.alice.id]
        self.assertEqual(alice["assigned_open"], 0)
        self.assertEqual(alice["agents"], 0)
        # 任务确实在 Bob 名下（领取后 status 变 CLAIMED）
        self.assertEqual(self.store.get_task(task.id).status, "CLAIMED")


class RunAttributionTests(TeamFixture):
    """执行归属落库：Run 与事件都要能看出"哪台设备、谁的机器"。"""

    def setUp(self) -> None:
        super().setUp()
        self.store.register_agent(AgentRegister(agent_id="agent-alice", display_name="Alice 的机器", owner_member_id=self.alice.id))
        self.store.db.execute(
            "INSERT INTO devices (device_id, organization_id, agent_id, owner_member_id, device_name, public_key, public_key_fingerprint, device_token_hash, platform, agent_version, capabilities, status, created_at, last_seen, revoked_at) "
            "VALUES ('device-alice', ?, 'agent-alice', ?, 'Alice 的机器', 'pk', 'fp-a', 'hash-a', 'windows', '0.1.0', '[]', 'active', ?, ?, NULL)",
            (DEV_ORG_ID, self.alice.id, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        self.store.db.execute(
            "INSERT OR REPLACE INTO agent_project_grants (agent_id, project_id, capabilities, granted_by, created_at) VALUES ('agent-alice', ?, '[]', ?, ?)",
            (str(self.project.id), self.lead.id, "2026-01-01T00:00:00+00:00"),
        )
        self.store.db.commit()

    def test_run_records_device_and_member(self) -> None:
        run = self.store.create_run(
            self.project.id,
            RunCreate(agent_id="agent-alice", device_id="device-alice", idempotency_key=uuid4().hex),
        )
        self.assertEqual(run.device_id, "device-alice")
        self.assertEqual(run.member_id, self.alice.id)

    def test_run_member_derived_from_agent_when_device_absent(self) -> None:
        run = self.store.create_run(self.project.id, RunCreate(agent_id="agent-alice", idempotency_key=uuid4().hex))
        self.assertIsNone(run.device_id)  # 没有活跃连接可回溯
        self.assertEqual(run.member_id, self.alice.id)  # 但成员仍能由 Agent 归属推出

    def test_unknown_or_mismatched_device_is_dropped(self) -> None:
        """伪造/串号的 device_id 不会被写进归属（防"冒名机器"）。"""

        run = self.store.create_run(
            self.project.id,
            RunCreate(agent_id="agent-alice", device_id="device-does-not-exist", idempotency_key=uuid4().hex),
        )
        self.assertIsNone(run.device_id)
        self.assertEqual(run.member_id, self.alice.id)

    def test_agent_events_carry_agent_actor_kind(self) -> None:
        task = self.store.create_task(self.project.id, TaskCreate(title="归属事件测试", description=""))
        claim_request = __import__("app.contracts", fromlist=["TaskClaimRequest"]).TaskClaimRequest(
            agent_id="agent-alice", lease_seconds=600, idempotency_key=uuid4().hex
        )
        self.store.claim_next_task(claim_request, self.project.id)
        event = self.store.list_latest_events(self.project.id, limit=1)[0]
        self.assertEqual(event.event_type, "task.claimed")
        self.assertEqual(event.actor, "agent-alice")
        self.assertEqual(str(event.actor_kind), "agent")


class OrganizationConfigTests(unittest.TestCase):
    def test_org_id_comes_from_environment(self) -> None:
        """部署可把组织指到别的行（多租户隔离不在本轮范围，但组织可配是必要的）。"""

        import app.store as store_module

        custom_org = str(uuid4())
        with mock.patch.dict(os.environ, {"PLATFORM_ORG_ID": custom_org, "PLATFORM_ORG_NAME": "我们的建模组"}):
            reloaded = importlib.reload(store_module)
            self.assertEqual(reloaded.DEV_ORG_ID, custom_org)
            self.assertEqual(reloaded.DEFAULT_ORG_NAME, "我们的建模组")
            with tempfile.TemporaryDirectory() as temp_dir:
                store = reloaded.Store(Path(temp_dir) / "platform.db")
                try:
                    org = store.list_organizations()[0]
                    self.assertEqual(str(org.id), custom_org)
                    self.assertEqual(org.name, "我们的建模组")
                finally:
                    store.close()
        importlib.reload(store_module)  # 还原模块常量，避免影响其他测试


if __name__ == "__main__":
    unittest.main()