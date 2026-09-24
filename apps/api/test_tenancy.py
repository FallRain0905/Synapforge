from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.contracts import (
    AgentProjectGrant,
    AgentRegister,
    HumanMemberCreate,
    InvitationCreate,
    OrganizationCreate,
    ProjectCreate,
    SessionCreate,
    TaskClaimRequest,
    TaskCreate,
    TeamCreate,
)
from app.store import Store


class TenancyContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_organizations_and_projects_are_member_scoped(self) -> None:
        organization = self.store.create_organization(OrganizationCreate(name="第二组织", slug="second-org"))
        team = self.store.create_team(TeamCreate(organization_id=organization.id, name="第二队伍"))
        member = self.store.create_member(HumanMemberCreate(organization_id=organization.id, team_id=team.id, email="second@example.local", display_name="第二负责人", role="owner"))
        project = self.store.create_project(ProjectCreate(name="第二组织项目", organization_id=organization.id, team_id=team.id, created_by=member.id))

        visible = self.store.list_projects_for_member(member.id)
        self.assertEqual([item.id for item in visible], [project.id])
        self.assertNotIn(project.id, {item.id for item in self.store.list_projects_for_member("member-001")})

    def test_agent_requires_explicit_project_grant(self) -> None:
        organization = self.store.create_organization(OrganizationCreate(name="隔离组织", slug="isolated-org"))
        team = self.store.create_team(TeamCreate(organization_id=organization.id, name="隔离队伍"))
        member = self.store.create_member(HumanMemberCreate(organization_id=organization.id, team_id=team.id, email="isolated@example.local", display_name="隔离负责人", role="owner"))
        project = self.store.create_project(ProjectCreate(name="隔离项目", organization_id=organization.id, team_id=team.id, created_by=member.id))
        task = self.store.create_task(project.id, TaskCreate(title="隔离任务"))
        agent = self.store.register_agent(AgentRegister(agent_id="isolated-agent", display_name="隔离 Agent", owner_member_id=member.id))

        with self.assertRaises(PermissionError):
            self.store.claim_task(task.id, TaskClaimRequest(agent_id=agent.agent_id, idempotency_key="isolated-claim-001"))

        self.store.grant_agent_project(AgentProjectGrant(agent_id=agent.agent_id, project_id=project.id, granted_by=member.id))
        claimed, _ = self.store.claim_task(task.id, TaskClaimRequest(agent_id=agent.agent_id, idempotency_key="isolated-claim-002"))
        self.assertEqual(claimed.id, task.id)

    def test_dev_session_and_invitation_lifecycle(self) -> None:
        organization = self.store.create_organization(OrganizationCreate(name="邀请组织", slug="invite-org"))
        team = self.store.create_team(TeamCreate(organization_id=organization.id, name="邀请队伍"))
        invitation = self.store.create_invitation(InvitationCreate(organization_id=organization.id, team_id=team.id, email="invited@example.local", role="reviewer"))
        member = self.store.accept_invitation(invitation.token, "受邀审核人")
        self.assertEqual(member.email, invitation.email)
        self.assertEqual(member.status, "active")

        session = self.store.create_session(SessionCreate(member_id=member.id))
        resolved = self.store.resolve_session(session.token)
        self.assertEqual(resolved.id, member.id)


if __name__ == "__main__":
    unittest.main()
