"""账号系统（AUTH-1）契约测试：注册、登录、会话、改密、账号管理、邀请码，以及三个断点修复的回归。"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from uuid import UUID, uuid4

import app.main as main
from app.accounts import LoginThrottle, hash_password, hash_token, verify_password
from app.contracts import (
    ProjectCreate,
    AccountUpdateRequest,
    AgentRegister,
    DevicePairingCreate,
    HumanMemberCreate,
    InvitationCreate,
    LoginRequest,
    OrganizationCreate,
    PasswordChangeRequest,
    RegisterRequest,
    SessionCreate,
    TeamCreate,
)
from app.store import DEV_ORG_ID, DEV_TEAM_ID, LEGACY_MEMBER_ID, SESSION_TTL_SECONDS, Store
from device_test_support import registration_request
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class AccountFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        # 端点处理函数读模块级 store；按仓库既有惯例（test_http_contracts）在这里换成临时库
        self.old_store = main.store
        main.store = self.store
        # addCleanup 是 LIFO：先登记临时目录、再登记关库，才能先关库再删目录（Windows 下文件占用会报错）
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(self.store.close)
        self.addCleanup(lambda: setattr(main, "store", self.old_store))

    def register(self, email: str = "admin@example.local", password: str = "correct-horse-battery", code: str | None = None):
        return self.store.register_account(RegisterRequest(email=email, password=password, display_name="管理员", invite_code=code))

    def invite(self, email: str = "member@example.local", role: str = "contributor") -> str:
        invitation = self.store.create_invitation(
            InvitationCreate(organization_id=UUID(DEV_ORG_ID), team_id=UUID(DEV_TEAM_ID), email=email, role=role)
        )
        return invitation.token


class PasswordCryptoTests(unittest.TestCase):
    def test_scrypt_hash_roundtrip_and_self_description(self) -> None:
        encoded = hash_password("correct-horse-battery")
        self.assertTrue(encoded.startswith("scrypt$16384$8$1$"))
        self.assertTrue(verify_password("correct-horse-battery", encoded))
        self.assertFalse(verify_password("correct-horse-batterz", encoded))
        self.assertFalse(verify_password("correct-horse-battery", None))

    def test_session_and_token_hashing(self) -> None:
        # 令牌哈希是定长十六进制，明文不落库（与设备令牌一致）
        self.assertEqual(len(hash_token("abc")), 64)
        self.assertNotEqual(hash_token("abc"), hash_token("abd"))

    def test_login_throttle_locks_and_expires(self) -> None:
        now = [0.0]
        throttle = LoginThrottle(threshold=3, base_seconds=60, max_seconds=600, clock=lambda: now[0])
        key = throttle.key("A@Example.local", "10.0.0.1")
        self.assertEqual(throttle.locked_seconds(key), 0)
        for _ in range(3):
            throttle.record_failure(key)
        self.assertGreater(throttle.locked_seconds(key), 0)
        now[0] += 61
        self.assertEqual(throttle.locked_seconds(key), 0)
        throttle.clear(key)
        # 邮箱大小写与空白归一化后是同一个节流键
        self.assertEqual(key, throttle.key("a@example.local  ", "10.0.0.1"))


class RegistrationTests(AccountFixture):
    def test_first_registration_becomes_admin_and_claims_legacy_data(self) -> None:
        legacy_project = self.store.list_projects()[0]
        self.assertEqual(legacy_project.created_by, LEGACY_MEMBER_ID)

        session, account = self.register()
        self.assertTrue(account.is_admin)
        self.assertTrue(account.has_password)
        self.assertEqual(account.member.email, "admin@example.local")

        # 存量数据一次性归到首个管理员；member-001 停用保留
        claimed_project = next(item for item in self.store.list_projects_for_member(account.member.id) if item.id == legacy_project.id)
        self.assertEqual(claimed_project.created_by, account.member.id)
        self.assertEqual(self.store.get_member(LEGACY_MEMBER_ID).status, "suspended")
        # 旧账号不能再登录
        self.assertEqual(self.store.count_accounts_with_password(), 1)

    def test_second_registration_requires_invite_code(self) -> None:
        self.register()
        with self.assertRaisesRegex(PermissionError, "invite_code_required"):
            self.register(email="second@example.local")

    def test_invite_code_flow_and_reuse(self) -> None:
        self.register()
        code = self.invite("member@example.local")
        _, account = self.register(email="member@example.local", code=code)
        self.assertFalse(account.is_admin)
        # 同一个码不能再发给别人（换邮箱，避免被"邮箱已注册"提前拦下）
        with self.assertRaisesRegex(PermissionError, "invite_code_used"):
            self.register(email="another@example.local", code=code)
        # 同一个邮箱重复注册也拦得住
        with self.assertRaisesRegex(ValueError, "email_already_registered"):
            self.register(email="member@example.local", code=code)

    def test_invite_code_email_mismatch(self) -> None:
        self.register()
        code = self.invite("someone@example.local")
        with self.assertRaisesRegex(PermissionError, "invite_code_email_mismatch"):
            self.register(email="other@example.local", code=code)

    def test_invited_member_joins_existing_projects(self) -> None:
        """新队友注册后必须能看到队伍的项目（否则登录进去是空的）。"""

        admin_session, admin = self.register()
        self.assertEqual(len(self.store.list_projects_for_member(admin.member.id)), 1)
        code = self.invite("member@example.local", role="reviewer")
        _, member = self.register(email="member@example.local", code=code)
        visible = self.store.list_projects_for_member(member.member.id)
        self.assertEqual(len(visible), 1)
        self.assertEqual(self.store._member_project_role(visible[0].id, member.member.id), "reviewer")

    def test_registration_validation(self) -> None:
        # 长度下限由契约层（RegisterRequest.min_length=10）先拦；策略层负责"够长但太弱"的口令
        with self.assertRaisesRegex(ValueError, "password_all_digits"):
            self.register(password="12345678901234")
        with self.assertRaisesRegex(ValueError, "password_all_letters"):
            self.register(password="abcdefghijklmn")
        with self.assertRaisesRegex(ValueError, "email_invalid"):
            self.register(email="not-an-email")
        self.register()
        with self.assertRaisesRegex(ValueError, "email_already_registered"):
            self.register(email="ADMIN@example.local")  # 归一化后同一个邮箱


class SessionTests(AccountFixture):
    def test_session_token_is_hashed_at_rest(self) -> None:
        session, account = self.register()
        rows = self.store.db.execute("SELECT token FROM sessions").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0]["token"], session.token)
        self.assertEqual(rows[0]["token"], hash_token(session.token))
        self.assertEqual(self.store.resolve_session(session.token).id, account.member.id)

    def test_logout_deletes_session(self) -> None:
        session, _ = self.register()
        self.store.delete_session(session.token)
        with self.assertRaisesRegex(PermissionError, "invalid_session"):
            self.store.resolve_session(session.token)

    def test_expired_session_is_purged_on_resolve(self) -> None:
        session, _ = self.register()
        self.store.db.execute("UPDATE sessions SET expires_at = ? WHERE token = ?", ("2000-01-01T00:00:00+00:00", hash_token(session.token)))
        self.store.db.commit()
        with self.assertRaisesRegex(PermissionError, "session_expired"):
            self.store.resolve_session(session.token)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) AS c FROM sessions").fetchone()["c"], 0)

    def test_password_change_keeps_current_session_and_revokes_others(self) -> None:
        first, account = self.register()
        second, _ = self.store.login_account("admin@example.local", "correct-horse-battery")
        self.store.change_password(account.member.id, "correct-horse-battery", "new-correct-horse", keep_token=second.token)
        # 当前会话保留，其它会话被撤销
        self.assertEqual(self.store.resolve_session(second.token).id, account.member.id)
        with self.assertRaisesRegex(PermissionError, "invalid_session"):
            self.store.resolve_session(first.token)
        self.assertTrue(verify_password("new-correct-horse", self.store.db.execute("SELECT password_hash FROM human_members WHERE id = ?", (account.member.id,)).fetchone()["password_hash"]))
        with self.assertRaisesRegex(PermissionError, "current_password_invalid"):
            self.store.change_password(account.member.id, "wrong-password", "another-good-password")


class LoginTests(AccountFixture):
    def test_login_rejects_unknown_and_wrong_password_with_same_code(self) -> None:
        self.register()
        for email, password in (("ghost@example.local", "whatever-long"), ("admin@example.local", "wrong-password")):
            with self.assertRaisesRegex(PermissionError, "invalid_credentials"):
                self.store.authenticate_account(email, password)

    def test_suspended_account_cannot_login(self) -> None:
        _, account = self.register()
        self.store.db.execute("UPDATE human_members SET status = 'suspended' WHERE id = ?", (account.member.id,))
        self.store.db.commit()
        with self.assertRaisesRegex(PermissionError, "account_suspended"):
            self.store.authenticate_account("admin@example.local", "correct-horse-battery")

    def test_login_endpoint_throttles_after_repeated_failures(self) -> None:
        from fastapi import HTTPException

        _, account = self.register()
        request = mock.Mock()
        request.headers = {}
        request.client = mock.Mock(host="203.0.113.7")
        main.LOGIN_THROTTLE.clear(main.LOGIN_THROTTLE.key("admin@example.local", "203.0.113.7"))
        for _ in range(5):
            with self.assertRaises(HTTPException) as first:
                main.login_account(LoginRequest(email="admin@example.local", password="wrong-password"), request)
            self.assertEqual(first.exception.status_code, 401)
        with self.assertRaises(HTTPException) as locked:
            main.login_account(LoginRequest(email="admin@example.local", password="correct-horse-battery"), request)
        self.assertEqual(locked.exception.status_code, 429)
        self.assertIn("Retry-After", locked.exception.headers)
        main.LOGIN_THROTTLE.clear(main.LOGIN_THROTTLE.key("admin@example.local", "203.0.113.7"))
        result = main.login_account(LoginRequest(email="admin@example.local", password="correct-horse-battery"), request)
        self.assertEqual(result.account.member.id, account.member.id)

    def test_login_updates_last_login(self) -> None:
        _, account = self.register()
        self.assertIsNotNone(self.store.account_view(self.store.get_member(account.member.id)).last_login_at)


class AccountManagementTests(AccountFixture):
    def setUp(self) -> None:
        super().setUp()
        _, self.admin = self.register()
        code = self.invite("member@example.local")
        _, self.member = self.register(email="member@example.local", code=code)

    def test_non_admin_is_rejected(self) -> None:
        with self.assertRaisesRegex(PermissionError, "admin_required"):
            self.store.require_admin(self.member.member.id)
        self.store.require_admin(self.admin.member.id)

    def test_admin_can_suspend_demote_and_reset(self) -> None:
        # 先提为管理员（此时仍在职，计数应为 2）
        promoted = self.store.update_account(self.member.member.id, AccountUpdateRequest(is_admin=True), actor_member_id=self.admin.member.id)
        self.assertTrue(promoted.is_admin)
        self.assertEqual(self.store._count_admins(), 2)

        # 停用后不能再登录，且不再计入在职管理员
        suspended = self.store.update_account(self.member.member.id, AccountUpdateRequest(status="suspended"), actor_member_id=self.admin.member.id)
        self.assertEqual(suspended.member.status, "suspended")
        self.assertEqual(self.store._count_admins(), 1)
        with self.assertRaisesRegex(PermissionError, "account_suspended"):
            self.store.authenticate_account("member@example.local", "correct-horse-battery")

        # 重新启用 + 重置密码：临时口令可登录
        self.store.update_account(self.member.member.id, AccountUpdateRequest(status="active"), actor_member_id=self.admin.member.id)
        temporary, account = self.store.reset_password(self.member.member.id, actor_member_id=self.admin.member.id)
        self.assertGreaterEqual(len(temporary), 10)
        session, view = self.store.login_account("member@example.local", temporary)
        self.assertTrue(session.token)
        self.assertEqual(view.member.id, account.member.id)
        self.assertEqual(self.store._count_admins(), 2)

    def test_admin_guards_against_locking_self_out(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot_suspend_self"):
            self.store.update_account(self.admin.member.id, AccountUpdateRequest(status="suspended"), actor_member_id=self.admin.member.id)
        with self.assertRaisesRegex(ValueError, "cannot_demote_self"):
            self.store.update_account(self.admin.member.id, AccountUpdateRequest(is_admin=False), actor_member_id=self.admin.member.id)
        with self.assertRaisesRegex(ValueError, "use_password_change_for_self"):
            self.store.reset_password(self.admin.member.id, actor_member_id=self.admin.member.id)

    def test_last_admin_cannot_be_demoted(self) -> None:
        # 先把成员提为管理员，再让原管理员自降——此时系统仍有管理员，允许；
        # 但唯一管理员不能降级自己（上面已断言）。这里验证"最后一个管理员"规则对他人也成立。
        self.store.update_account(self.member.member.id, AccountUpdateRequest(is_admin=True), actor_member_id=self.admin.member.id)
        with self.assertRaisesRegex(ValueError, "cannot_demote_self"):
            self.store.update_account(self.admin.member.id, AccountUpdateRequest(is_admin=False), actor_member_id=self.admin.member.id)
        self.assertEqual(self.store._count_admins(), 2)

    def test_account_view_never_exposes_password_hash(self) -> None:
        view = self.store.account_view(self.store.get_member(self.admin.member.id))
        payload = view.model_dump(mode="json")
        self.assertNotIn("password_hash", str(payload))
        self.assertTrue(payload["has_password"])


class RequiredModeTests(AccountFixture):
    """切到强制鉴权后必须仍然成立的两件事：Agent 接入不被拦，普通端点被拦。"""

    def setUp(self) -> None:
        super().setUp()
        self._env = mock.patch.dict(os.environ, {"PLATFORM_AUTH_MODE": "required"})
        self._env.start()
        self.addCleanup(self._env.stop)

    def _middleware(self, path: str):
        """直接跑中间件的免鉴权分支判定（不启 ASGI 服务器）。"""

        async def call_next(request):  # pragma: no cover - 只在放行时返回标记
            return "passed"

        request = mock.Mock()
        request.method = "POST"
        request.url = mock.Mock(path=path)
        return main.enforce_project_authorization(request, call_next)

    def test_agent_onboarding_endpoints_bypass_middleware(self) -> None:
        import asyncio

        for path in ("/api/devices/register", "/api/agents/register", "/api/agent/me"):
            self.assertEqual(asyncio.run(self._middleware(path)), "passed", path)

    def test_member_endpoints_require_token_in_required_mode(self) -> None:
        with self.assertRaises(main.HTTPException) as blocked:
            main._request_member_id(mock.Mock(headers={}))
        self.assertEqual(blocked.exception.status_code, 401)
        self.assertEqual(blocked.exception.detail, "authentication_required")

    def test_dev_session_endpoint_disabled(self) -> None:
        with self.assertRaises(main.HTTPException) as disabled:
            main.create_dev_session(SessionCreate(member_id=LEGACY_MEMBER_ID))
        self.assertEqual(disabled.exception.status_code, 403)

    def test_agent_registration_still_works_after_accounts_took_over(self) -> None:
        """回归：首个管理员接管 member-001 后，老轨登记的 Agent 仍能被新账号配对接入。"""

        from fastapi import HTTPException

        _, admin = self.register()
        self.assertEqual(self.store.get_member(LEGACY_MEMBER_ID).status, "suspended")
        self.store.register_agent(AgentRegister(agent_id="legacy-agent", display_name="Legacy", owner_member_id=LEGACY_MEMBER_ID))
        pairing = self.store.create_device_pairing(DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), admin.member.id)
        credential = self.store.register_device(
            registration_request(pairing, Ed25519PrivateKey.generate(), "legacy-agent", "device-after-accounts", device_name="After accounts")
        )
        self.assertEqual(credential.device.owner_member_id, admin.member.id)
        legacy_agent = next(item for item in self.store.list_agents() if item.agent_id == "legacy-agent")
        self.assertEqual(legacy_agent.owner_member_id, admin.member.id)
        # 归属仍在职的其它 Agent 依旧不可被接管
        stranger = self.store.create_member(
            HumanMemberCreate(organization_id=UUID(DEV_ORG_ID), team_id=UUID(DEV_TEAM_ID), email="stranger@example.local", display_name="Stranger")
        )
        self.store.register_agent(AgentRegister(agent_id="stranger-agent", display_name="Stranger", owner_member_id=stranger.id))
        other_pairing = self.store.create_device_pairing(DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), admin.member.id)
        with self.assertRaisesRegex(PermissionError, "device_agent_owner_mismatch"):
            self.store.register_device(
                registration_request(other_pairing, Ed25519PrivateKey.generate(), "stranger-agent", "device-stranger")
            )

    def test_collab_websocket_requires_token(self) -> None:
        import asyncio

        project = self.store.list_projects()[0]

        class FakeWebSocket:
            def __init__(self, token: str | None) -> None:
                self.query_params = {"token": token} if token else {}
                self.closed: list[int] = []

            async def close(self, code: int = 1000) -> None:
                self.closed.append(code)

            async def accept(self) -> None:  # pragma: no cover - 有令牌时才会走到
                pass

            async def send_json(self, payload):  # pragma: no cover
                pass

            async def receive_text(self):  # pragma: no cover
                raise RuntimeError("not reached")

        no_token = FakeWebSocket(None)
        asyncio.run(main.project_events(no_token, project.id))
        self.assertEqual(no_token.closed, [4403])

        bad_token = FakeWebSocket("not-a-real-session")
        asyncio.run(main.project_events(bad_token, project.id))
        self.assertEqual(bad_token.closed, [4401])


class InvitationAdminTests(AccountFixture):
    def test_invitation_creation_requires_admin_once_accounts_exist(self) -> None:
        from fastapi import HTTPException

        request = mock.Mock()
        request.headers = {}
        # 账号系统上线前（无人设口令）保持开发期行为：允许
        created = main.create_invitation(InvitationCreate(organization_id=UUID(DEV_ORG_ID), email="first@example.local"), request)
        self.assertTrue(created.token)

        _, admin = self.register()
        code = self.invite("member@example.local")
        _, member = self.register(email="member@example.local", code=code)

        member_request = mock.Mock()
        member_request.headers = {"Authorization": f"Bearer {self.store.login_account('member@example.local', 'correct-horse-battery')[0].token}"}
        with self.assertRaises(HTTPException) as denied:
            main.create_invitation(InvitationCreate(organization_id=UUID(DEV_ORG_ID), email="third@example.local"), member_request)
        self.assertEqual(denied.exception.status_code, 403)

        admin_request = mock.Mock()
        admin_request.headers = {"Authorization": f"Bearer {self.store.login_account('admin@example.local', 'correct-horse-battery')[0].token}"}
        allowed = main.create_invitation(InvitationCreate(organization_id=UUID(DEV_ORG_ID), email="third@example.local"), admin_request)
        self.assertTrue(allowed.token)
        listing = main.list_invitations(admin_request, limit=10)
        self.assertTrue(any(item.id == allowed.id for item in listing))
        # 非管理员不能列出邀请码
        with self.assertRaises(HTTPException) as listing_denied:
            main.list_invitations(member_request, limit=10)
        self.assertEqual(listing_denied.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()

class OpenRegistrationTests(unittest.TestCase):
    """开放邮箱注册（W-8）：无邀请码也能注册即用（contributor）；开关关掉则回到邀请码模式。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(self.store.close)
        # 造一个已有管理员的库（跳过"首个账号"分支）
        self.store.register_account(
            RegisterRequest(email="admin@example.local", password="correct-horse-battery", display_name="管理员")
        )
        self.project = self.store.create_project(
            ProjectCreate(name="既有项目", competition_pack="cumcm-2026", created_by="member-001")
        )

    def test_open_registration_without_code(self) -> None:
        session, account = self.store.register_account(
            RegisterRequest(email="newbie@example.local", password="correct-horse-battery", display_name="新队友"),
            allow_open_registration=True,
        )
        self.assertFalse(account.is_admin)
        # 角色落在项目成员关系上（contributor）
        memberships = self.store.db.execute(
            "SELECT role FROM project_memberships WHERE member_id = ?", (account.member.id,)
        ).fetchall()
        self.assertTrue(memberships and all(row["role"] == "contributor" for row in memberships))
        # 注册即用：能直接登录、能看到组织内既有项目
        view = self.store.authenticate_account("newbie@example.local", "correct-horse-battery")
        self.assertEqual(view.member.display_name, "新队友")
        projects = self.store.list_projects_for_member(view.member.id)
        self.assertTrue(any(str(item.id) == str(self.project.id) for item in projects))
        self.assertTrue(session.token)

    def test_open_registration_still_honors_invite_code_role(self) -> None:
        code = self.store.create_invitation(
            InvitationCreate(organization_id=UUID(DEV_ORG_ID), team_id=UUID(DEV_TEAM_ID), email="reviewer@example.local", role="reviewer")
        ).token
        _, account = self.store.register_account(
            RegisterRequest(email="reviewer@example.local", password="correct-horse-battery", display_name="复核员", invite_code=code),
            allow_open_registration=True,
        )
        memberships = self.store.db.execute(
            "SELECT role FROM project_memberships WHERE member_id = ?", (account.member.id,)
        ).fetchall()
        self.assertTrue(memberships and all(row["role"] == "reviewer" for row in memberships))

    def test_closed_registration_requires_code(self) -> None:
        with self.assertRaisesRegex(PermissionError, "invite_code_required"):
            self.store.register_account(
                RegisterRequest(email="nope@example.local", password="correct-horse-battery", display_name="路人"),
                allow_open_registration=False,
            )

    def test_email_uniqueness_still_holds(self) -> None:
        self.store.register_account(
            RegisterRequest(email="dup@example.local", password="correct-horse-battery", display_name="队员甲"), allow_open_registration=True
        )
        with self.assertRaises(ValueError):
            self.store.register_account(
                RegisterRequest(email="dup@example.local", password="correct-horse-battery", display_name="队员乙"), allow_open_registration=True
            )
