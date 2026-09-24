"""P3 契约测试：成员变更审计与恢复、项目归队、能力目录、吞吐、Agent 列表组织收口。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from app.contracts import (
    AgentRegister,
    HumanMemberCreate,
    OrganizationCreate,
    ProjectCreate,
    RunCreate,
    RunComplete,
    TaskCreate,
    TeamCreate,
)
from app.store import DEV_ORG_ID, DEV_TEAM_ID, Store


class P3Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(self.store.close)
        self.lead = self.store.get_member("member-001")
        self.alice = self.store.create_member(
            HumanMemberCreate(organization_id=UUID(DEV_ORG_ID), team_id=UUID(DEV_TEAM_ID), email="alice@example.local", display_name="Alice")
        )
        self.project = self.store.create_project(
            ProjectCreate(name="P3 测试项目", competition_pack="cumcm-2026", problem_code="A", created_by=self.lead.id)
        )


class MembershipAuditTests(P3Fixture):
    def test_membership_changes_are_events_with_roles(self) -> None:
        # 用"项目创建之后才加入组织"的成员：建项目会自动带上当时的在职成员，
        # 用 Alice 的话第一次"加入"会被算成改角色（这正是 auto-carry 的预期行为）
        carol = self.store.create_member(
            HumanMemberCreate(organization_id=UUID(DEV_ORG_ID), team_id=UUID(DEV_TEAM_ID), email="carol@example.local", display_name="Carol")
        )
        self.store.add_project_member(self.project.id, carol.id, "contributor")
        self.store.add_project_member(self.project.id, carol.id, "reviewer")
        self.store.remove_project_member(self.project.id, carol.id)

        events = self.store.list_latest_events(self.project.id, limit=10)
        types = [str(event.event_type) for event in events]
        self.assertIn("project.member_added", types)
        self.assertIn("project.member_role_changed", types)
        self.assertIn("project.member_removed", types)

        added = next(event for event in events if str(event.event_type) == "project.member_added")
        self.assertEqual(added.payload["role"], "contributor")
        changed = next(event for event in events if str(event.event_type) == "project.member_role_changed")
        self.assertEqual(changed.payload["previous_role"], "contributor")
        self.assertEqual(changed.payload["role"], "reviewer")
        # 移除事件带上被移除时的角色——界面据此提供"恢复"
        removed = next(event for event in events if str(event.event_type) == "project.member_removed")
        self.assertEqual(removed.payload["role"], "reviewer")
        self.assertEqual(str(removed.actor_kind), "member")

    def test_restore_after_removal_uses_recorded_role(self) -> None:
        self.store.add_project_member(self.project.id, self.alice.id, "reviewer")
        self.store.remove_project_member(self.project.id, self.alice.id)
        removed = next(event for event in self.store.list_latest_events(self.project.id, limit=5) if str(event.event_type) == "project.member_removed")

        # 界面上的"恢复"就是：读事件里的角色 → 重新加入
        self.store.add_project_member(self.project.id, self.alice.id, removed.payload["role"])
        roles = {item["member_id"]: item["role"] for item in self.store.list_project_members(self.project.id)}
        self.assertEqual(roles[self.alice.id], "reviewer")


class ProjectTeamTests(P3Fixture):
    def test_project_can_change_team(self) -> None:
        team = self.store.create_team(TeamCreate(organization_id=UUID(DEV_ORG_ID), name="B 队"))
        updated = self.store.update_project_team(self.project.id, team.id)
        self.assertEqual(updated.team_id, team.id)
        events = [str(event.event_type) for event in self.store.list_latest_events(self.project.id, limit=3)]
        self.assertIn("project.team_changed", events)
        # 脱离团队
        detached = self.store.update_project_team(self.project.id, None)
        self.assertIsNone(detached.team_id)

    def test_team_must_belong_to_same_organization(self) -> None:
        other = self.store.create_organization(OrganizationCreate(name="别的组织", slug="p3-other"))
        foreign_team = self.store.create_team(TeamCreate(organization_id=other.id, name="别队的队"))
        with self.assertRaisesRegex(ValueError, "team_not_in_project_organization"):
            self.store.update_project_team(self.project.id, foreign_team.id)


class CapabilityCatalogTests(P3Fixture):
    def test_catalog_lists_capabilities_and_unmet_tasks(self) -> None:
        self.store.register_agent(AgentRegister(agent_id="agent-alice", display_name="Alice 的机器", owner_member_id=self.alice.id, supported_tools=["codex", "python"]))
        self.store.create_task(self.project.id, TaskCreate(title="需要 R 的任务", description="", required_capabilities=["r-language"]))
        self.store.create_task(self.project.id, TaskCreate(title="需要 python 的任务", description="", required_capabilities=["python"]))

        catalog = self.store.capability_catalog(UUID(DEV_ORG_ID))
        entry = next(item for item in catalog["agents"] if item["agent_id"] == "agent-alice")
        self.assertIn("codex", entry["capabilities"])
        self.assertIn("python", entry["capabilities"])
        self.assertEqual(entry["owner_name"], "Alice")
        # 只有"没人满足"的任务会被列出来
        unmet = {item["title"]: item for item in catalog["unmet_tasks"]}
        self.assertIn("需要 R 的任务", unmet)
        self.assertNotIn("需要 python 的任务", unmet)
        self.assertEqual(unmet["需要 R 的任务"]["missing_capabilities"], ["r-language"])


class ThroughputTests(P3Fixture):
    def _run_for(self, agent_id: str, *, days_ago: int, success: bool) -> None:
        self.store.register_agent(AgentRegister(agent_id=agent_id, display_name=agent_id, owner_member_id=self.alice.id))
        self.store.db.execute(
            "INSERT OR REPLACE INTO agent_project_grants (agent_id, project_id, capabilities, granted_by, created_at) VALUES (?, ?, '[]', ?, ?)",
            (agent_id, str(self.project.id), self.lead.id, "2026-01-01T00:00:00+00:00"),
        )
        self.store.db.commit()
        run = self.store.create_run(self.project.id, RunCreate(agent_id=agent_id, idempotency_key=uuid4().hex))
        stamp = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
        self.store.db.execute(
            "UPDATE runs SET started_at = ?, completed_at = ?, status = ? WHERE id = ?",
            (stamp, stamp, "SUCCEEDED" if success else "FAILED", str(run.id)),
        )
        self.store.db.commit()

    def test_throughput_counts_by_day_and_member(self) -> None:
        self._run_for("agent-1", days_ago=1, success=True)
        self._run_for("agent-2", days_ago=1, success=False)
        self._run_for("agent-3", days_ago=3, success=True)

        data = self.store.throughput(UUID(DEV_ORG_ID), days=14)
        self.assertEqual(sum(day["total"] for day in data["daily"]), 3)
        self.assertEqual(sum(day["succeeded"] for day in data["daily"]), 2)
        self.assertEqual(sum(day["failed"] for day in data["daily"]), 1)
        self.assertEqual(len(data["members"]), 1)
        member_row = data["members"][0]
        self.assertEqual(member_row["member_id"], self.alice.id)
        self.assertEqual(member_row["display_name"], "Alice")
        self.assertEqual((member_row["total"], member_row["succeeded"], member_row["failed"]), (3, 2, 1))
        # 窗口之外的不计入
        self._run_for("agent-4", days_ago=40, success=True)
        self.assertEqual(sum(day["total"] for day in self.store.throughput(UUID(DEV_ORG_ID), days=14)["daily"]), 3)


class AgentScopeTests(P3Fixture):
    def test_agent_list_is_scoped_to_organization(self) -> None:
        self.store.register_agent(AgentRegister(agent_id="agent-alice", display_name="Alice 的机器", owner_member_id=self.alice.id))
        other = self.store.create_organization(OrganizationCreate(name="别的组织", slug="p3-other-2"))
        outsider = self.store.create_member(
            HumanMemberCreate(organization_id=other.id, email="outsider@example.local", display_name="Outsider")
        )
        self.store.register_agent(AgentRegister(agent_id="agent-outsider", display_name="别人的机器", owner_member_id=outsider.id))

        mine = {item.agent_id for item in self.store.list_agents(UUID(DEV_ORG_ID))}
        self.assertIn("agent-alice", mine)
        self.assertNotIn("agent-outsider", mine)
        theirs = {item.agent_id for item in self.store.list_agents(other.id)}
        self.assertEqual(theirs, {"agent-outsider"})
        # 不带组织参数（内部调用/测试）保持原有全量行为
        self.assertGreaterEqual(len(self.store.list_agents()), 2)


if __name__ == "__main__":
    unittest.main()