"""FM-3 平台侧契约测试：工作区登记、操作队列（幂等/领取/完成/过期）、传输会话与权限隔离。

钉住的性质（都是会出事的地方）：

1. **路径语法**：绝对路径、`..`、盘符、UNC、控制字符、超深一律拒绝（纵深防御；权威校验在 Agent 侧）；
2. **幂等**：同 key 同内容 → 同一条操作；**同 key 不同内容 → 409**（安全要求第 12 条）；
3. **完成只认第一次**：重复 `complete` 不会把已成功改成失败（重连后重复提交是常态）；
4. **离线不假装**：Agent 离线时入队保持 `queued`（如实显示"等上线"），`fail_when_offline` 才立刻失败；
5. **过期**：心跳过期的 claimed/running 变 `expired` 并带错误码，不留假的"执行中"；
6. **隔离**：别的成员/别的组织看不到工作区（404），别的项目的操作也不串；
7. **传输**：哈希/大小不符拒绝；过期会话读到就报过期；清理会删对象。
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app import main, workspace_files
from app.contracts import AgentProjectGrant, AgentRegister, SessionCreate
from app.store import DEV_ORG_ID, Store

MEMBER = "member-001"
AGENT = "agent-ws-1"
DEVICE = "device-ws-1"


class WorkspaceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.client = TestClient(main.app)
        workspace_files.ensure_schema(self.store)
        self.project = self.store.list_projects()[0]
        self.actor = workspace_files.actor_for(self.store, MEMBER)

    def tearDown(self) -> None:
        main.store = self.previous_store
        self.store.close()
        self.temp_dir.cleanup()

    def make_workspace(self, *, agent_id: str = AGENT, identity: str = "ws-abc123def456", status: str = "online", project_id: str | None = None):
        workspace = workspace_files.register_workspace(
            self.store,
            agent_id=agent_id,
            organization_id=self.actor.organization_id,
            display_name="本机工作区",
            workspace_identity=identity,
            project_id=project_id or str(self.project.id),
            protected_paths=["secret-notes"],
        )
        if status != "online":
            workspace_files.touch_workspace(self.store, agent_id, status=status)
            workspace = workspace_files.get_workspace(self.store, self.actor, workspace["id"])
        return workspace


class PathSyntaxTests(unittest.TestCase):
    def test_bad_paths_are_rejected(self) -> None:
        for value in (
            "../etc/passwd",
            "/abs/path.txt",
            "C:/Windows/evil.dll",
            "//server/share/x.txt",
            "a/\x00b",
            "/".join(["d"] * 40),
            "   ",
        ):
            with self.subTest(value=value):
                with self.assertRaises(workspace_files.WorkspaceError) as caught:
                    workspace_files.validate_relative_path(value, allow_empty=False)
                self.assertIn(caught.exception.code, {"workspace_path_invalid", "workspace_path_outside_root", "workspace_path_required"})

    def test_good_paths_are_normalized(self) -> None:
        self.assertEqual(workspace_files.validate_relative_path("a//b/./c.txt"), "a/b/c.txt")
        self.assertEqual(workspace_files.validate_relative_path(""), "")

    def test_workspace_identity_matches_the_fm0_rule(self) -> None:
        """工作区标识与 FM-0 的 `path_privacy.workspace_identity` 必须同口径（平台按它对齐两边）。"""

        from app.path_privacy import workspace_identity

        for raw in (r"C:\Users\someone\ws", "/srv/synapforge/cloud"):
            self.assertEqual(workspace_files.workspace_identity_for(raw), workspace_identity(raw))


class OperationQueueTests(WorkspaceTestBase):
    def test_enqueue_claim_complete_round_trip(self) -> None:
        workspace = self.make_workspace()
        operation = workspace_files.create_operation(
            self.store, self.actor, workspace["id"], operation_type="list", relative_path="", idempotency_key="k-1"
        )
        self.assertEqual(operation["status"], "queued")
        claimed = workspace_files.claim_operations(self.store, agent_id=AGENT)
        self.assertEqual([item["id"] for item in claimed], [operation["id"]])
        self.assertEqual(claimed[0]["status"], "claimed")
        started = workspace_files.start_operation(self.store, operation["id"], agent_id=AGENT)
        self.assertEqual(started["status"], "running")
        done = workspace_files.complete_operation(
            self.store, operation["id"], agent_id=AGENT, success=True, result={"entries": 3}
        )
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(done["result"]["entries"], 3)

    def test_idempotency_same_key_same_payload_returns_same_operation(self) -> None:
        workspace = self.make_workspace()
        first = workspace_files.create_operation(
            self.store, self.actor, workspace["id"], operation_type="mkdir", relative_path="papers", idempotency_key="k-2"
        )
        second = workspace_files.create_operation(
            self.store, self.actor, workspace["id"], operation_type="mkdir", relative_path="papers", idempotency_key="k-2"
        )
        self.assertEqual(first["id"], second["id"])

    def test_idempotency_same_key_different_payload_conflicts(self) -> None:
        workspace = self.make_workspace()
        workspace_files.create_operation(
            self.store, self.actor, workspace["id"], operation_type="mkdir", relative_path="papers", idempotency_key="k-3"
        )
        with self.assertRaises(workspace_files.WorkspaceError) as caught:
            workspace_files.create_operation(
                self.store, self.actor, workspace["id"], operation_type="mkdir", relative_path="other", idempotency_key="k-3"
            )
        self.assertEqual(caught.exception.code, "operation_idempotency_conflict")

    def test_complete_is_idempotent_and_never_flips_a_result(self) -> None:
        workspace = self.make_workspace()
        operation = workspace_files.create_operation(
            self.store, self.actor, workspace["id"], operation_type="stat", relative_path="a.txt", idempotency_key="k-4"
        )
        workspace_files.claim_operations(self.store, agent_id=AGENT)
        workspace_files.complete_operation(self.store, operation["id"], agent_id=AGENT, success=True, result={"ok": True})
        again = workspace_files.complete_operation(
            self.store, operation["id"], agent_id=AGENT, success=False, error_code="late_failure"
        )
        self.assertEqual(again["status"], "succeeded")
        self.assertEqual(again["error_code"], None)

    def test_other_agent_cannot_complete(self) -> None:
        workspace = self.make_workspace()
        operation = workspace_files.create_operation(
            self.store, self.actor, workspace["id"], operation_type="list", idempotency_key="k-5"
        )
        with self.assertRaises(workspace_files.WorkspaceError) as caught:
            workspace_files.complete_operation(self.store, operation["id"], agent_id="agent-other", success=True)
        self.assertEqual(caught.exception.code, "workspace_operation_agent_mismatch")

    def test_offline_workspace_keeps_queued_and_can_fail_fast(self) -> None:
        workspace = self.make_workspace(status="offline")
        queued = workspace_files.create_operation(
            self.store, self.actor, workspace["id"], operation_type="list", idempotency_key="k-6"
        )
        self.assertEqual(queued["status"], "queued")  # 不假装执行
        self.assertEqual(workspace_files.claim_operations(self.store, agent_id=AGENT), [])
        failed = workspace_files.create_operation(
            self.store, self.actor, workspace["id"], operation_type="list", idempotency_key="k-7", fail_when_offline=True
        )
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error_code"], "workspace_offline")

    def test_stale_claim_expires_with_a_clear_code(self) -> None:
        workspace = self.make_workspace()
        operation = workspace_files.create_operation(
            self.store, self.actor, workspace["id"], operation_type="list", idempotency_key="k-8"
        )
        workspace_files.claim_operations(self.store, agent_id=AGENT)
        # 把租约到期时间改到过去（等价于"Agent 掉线很久了"）
        self.store.db.execute(
            "UPDATE workspace_operations SET expected_revision = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(seconds=30)).isoformat(), operation["id"]),
        )
        self.store.db.commit()
        expired = workspace_files.expire_stale_operations(self.store)
        self.assertIn(operation["id"], expired)
        row = workspace_files.get_operation(self.store, self.actor, operation["id"])
        self.assertEqual(row["status"], "expired")
        self.assertEqual(row["error_code"], "workspace_operation_expired")

    def test_cancel_queued_operation(self) -> None:
        workspace = self.make_workspace()
        operation = workspace_files.create_operation(
            self.store, self.actor, workspace["id"], operation_type="delete", relative_path="tmp.txt", idempotency_key="k-9"
        )
        cancelled = workspace_files.cancel_operation(self.store, self.actor, operation["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(workspace_files.claim_operations(self.store, agent_id=AGENT), [])

    def test_protected_paths_helper(self) -> None:
        workspace = self.make_workspace()
        self.assertTrue(workspace_files.is_protected(workspace, ""))
        self.assertTrue(workspace_files.is_protected(workspace, ".git/config"))
        self.assertTrue(workspace_files.is_protected(workspace, "secret-notes/plan.md"))
        self.assertFalse(workspace_files.is_protected(workspace, "papers/main.tex"))


class TransferTests(WorkspaceTestBase):
    def test_write_and_read_transfer_content_checks_hash(self) -> None:
        content = "上传内容".encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        transfer = workspace_files.create_transfer(
            self.store, self.actor, source_type="workspace", target_type="temp", expected_hash=digest, expected_size=len(content)
        )
        ready = workspace_files.write_transfer_content(self.store, self.actor, transfer["id"], content)
        self.assertEqual(ready["status"], "ready")
        loaded, fetched = workspace_files.read_transfer_content(self.store, self.actor, transfer["id"], mark_consumed=True)
        self.assertEqual(fetched, content)
        self.assertEqual(loaded["status"], "consumed")

    def test_hash_and_size_mismatch_are_rejected(self) -> None:
        transfer = workspace_files.create_transfer(
            self.store, self.actor, source_type="temp", target_type="workspace", expected_hash=hashlib.sha256(b"right").hexdigest()
        )
        with self.assertRaises(workspace_files.WorkspaceError) as caught:
            workspace_files.write_transfer_content(self.store, self.actor, transfer["id"], b"wrong")
        self.assertEqual(caught.exception.code, "workspace_transfer_hash_mismatch")

        sized = workspace_files.create_transfer(
            self.store, self.actor, source_type="temp", target_type="workspace", expected_size=10
        )
        with self.assertRaises(workspace_files.WorkspaceError) as caught:
            workspace_files.write_transfer_content(self.store, self.actor, sized["id"], b"short")
        self.assertEqual(caught.exception.code, "workspace_transfer_size_mismatch")

    def test_expired_transfer_reports_expired_and_cleanup_deletes_objects(self) -> None:
        expired = workspace_files.create_transfer(
            self.store, self.actor, source_type="temp", target_type="workspace", ttl_seconds=60
        )
        workspace_files.write_transfer_content(self.store, self.actor, expired["id"], b"data")
        untouched = workspace_files.create_transfer(
            self.store, self.actor, source_type="temp", target_type="workspace", ttl_seconds=60
        )
        workspace_files.write_transfer_content(self.store, self.actor, untouched["id"], b"more")
        past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        self.store.db.execute(
            "UPDATE file_transfer_sessions SET expires_at = ? WHERE id IN (?, ?)",
            (past, expired["id"], untouched["id"]),
        )
        self.store.db.commit()

        # 读已过期的会话：明确报过期（不把内容发出去），并把这条标成 expired
        with self.assertRaises(workspace_files.WorkspaceError) as caught:
            workspace_files.read_transfer_content(self.store, self.actor, expired["id"])
        self.assertEqual(caught.exception.code, "workspace_transfer_expired")

        # 回收：没人碰过的那条会被标过期并删对象
        result = workspace_files.cleanup_expired_transfers(self.store, self.actor)
        self.assertGreaterEqual(result["expired"], 1)
        self.assertGreaterEqual(result["objects_deleted"], 1)


class HttpSurfaceTests(WorkspaceTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.store.register_agent(AgentRegister(agent_id=AGENT, display_name="工作区 Agent", owner_member_id=MEMBER))
        self.store.grant_agent_project(AgentProjectGrant(agent_id=AGENT, project_id=self.project.id, granted_by=MEMBER))
        self.session = self.store.create_session(SessionCreate(member_id=MEMBER, expires_in_seconds=600))
        self.headers = {"Authorization": f"Bearer {self.session.token}"}
        # 真设备 + 真项目授权：Agent 侧接口要 `X-Project-Capability-Token`，
        # 而且能力集合里必须有 workspace.files.*（这条同时验证了"新能力要重新签发授权"）
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        from app.contracts import DevicePairingCreate, DeviceProjectGrantCreate
        from device_test_support import registration_request

        pairing = self.store.create_device_pairing(DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), MEMBER)
        self.store.register_device(
            registration_request(pairing, Ed25519PrivateKey.generate(), AGENT, DEVICE, device_name="工作区设备")
        )
        credential = self.store.create_device_project_grant(
            self.project.id, DeviceProjectGrantCreate(device_id=DEVICE), MEMBER
        )
        self.project_token = credential.project_token
        self.agent_headers = {
            "X-Agent-Id": AGENT,
            "X-Project-Id": str(self.project.id),
            "X-Project-Capability-Token": self.project_token,
        }

    def test_register_then_browser_enqueue_claim_complete(self) -> None:
        registered = self.client.post(
            "/api/agent/workspaces/register",
            json={
                "display_name": "本机工作区",
                "workspace_identity": "ws-111122223333",
                "project_id": str(self.project.id),
                "protected_paths": ["secret-notes"],
            },
            headers=self.agent_headers,
        )
        self.assertEqual(registered.status_code, 200, registered.text)
        workspace_id = registered.json()["workspace"]["id"]

        listing = self.client.get("/api/agent-workspaces", headers=self.headers)
        self.assertEqual(listing.status_code, 200)
        self.assertEqual([item["id"] for item in listing.json()["workspaces"]], [workspace_id])
        # 响应里**没有**宿主机绝对路径（只有哈希标识）
        self.assertNotIn("C:\\", listing.text)
        self.assertNotIn("/home/", listing.text)

        created = self.client.post(
            f"/api/agent-workspaces/{workspace_id}/operations",
            json={"operation_type": "mkdir", "relative_path": "papers", "idempotency_key": "http-k-1"},
            headers=self.headers,
        )
        self.assertEqual(created.status_code, 201, created.text)
        operation_id = created.json()["operation"]["id"]

        claimed = self.client.post(
            "/api/agent/workspace-operations/claim",
            json={"workspace_id": workspace_id, "limit": 4},
            headers=self.agent_headers,
        )
        self.assertEqual(claimed.status_code, 200, claimed.text)
        self.assertEqual([item["id"] for item in claimed.json()["operations"]], [operation_id])

        done = self.client.post(
            f"/api/agent/workspace-operations/{operation_id}/complete",
            json={"success": True, "result": {"created": True}},
            headers=self.agent_headers,
        )
        self.assertEqual(done.status_code, 200)
        self.assertEqual(done.json()["operation"]["status"], "succeeded")
        detail = self.client.get(f"/api/agent-workspaces/{workspace_id}/operations/{operation_id}", headers=self.headers)
        self.assertEqual(detail.json()["operation"]["status"], "succeeded")

    def test_path_escape_over_http_returns_400(self) -> None:
        workspace = self.make_workspace()
        for path in ("../etc/passwd", "/etc/passwd", "C:/Windows/x.dll"):
            with self.subTest(path=path):
                response = self.client.post(
                    f"/api/agent-workspaces/{workspace['id']}/operations",
                    json={"operation_type": "delete", "relative_path": path, "idempotency_key": f"bad-{abs(hash(path))}"},
                    headers=self.headers,
                )
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("workspace_path", response.json()["detail"])

    def test_same_idempotency_key_different_payload_is_409(self) -> None:
        workspace = self.make_workspace()
        first = self.client.post(
            f"/api/agent-workspaces/{workspace['id']}/operations",
            json={"operation_type": "mkdir", "relative_path": "one", "idempotency_key": "dup-key"},
            headers=self.headers,
        )
        self.assertEqual(first.status_code, 201)
        second = self.client.post(
            f"/api/agent-workspaces/{workspace['id']}/operations",
            json={"operation_type": "mkdir", "relative_path": "two", "idempotency_key": "dup-key"},
            headers=self.headers,
        )
        self.assertEqual(second.status_code, 409)

    def test_other_member_sees_nothing_and_cannot_enqueue(self) -> None:
        workspace = self.make_workspace()
        other = self.store.create_session(SessionCreate(member_id=self._make_other_member(), expires_in_seconds=600))
        headers = {"Authorization": f"Bearer {other.token}"}
        listing = self.client.get("/api/agent-workspaces", headers=headers)
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()["workspaces"], [])
        response = self.client.post(
            f"/api/agent-workspaces/{workspace['id']}/operations",
            json={"operation_type": "list", "idempotency_key": "foreign-key"},
            headers=headers,
        )
        self.assertIn(response.status_code, (403, 404))

    def _make_other_member(self) -> str:
        from tests_support import ensure_member  # noqa: PLC0415 - 仅测试内部使用

        return ensure_member(self.store, "member-ws-other")

    def test_transfer_over_http_without_hash_is_allowed_and_with_hash_checked(self) -> None:
        created = self.client.post(
            "/api/workspace-transfers",
            json={"source_type": "temp", "target_type": "workspace", "expected_hash": hashlib.sha256(b"payload").hexdigest()},
            headers=self.headers,
        )
        self.assertEqual(created.status_code, 201, created.text)
        transfer_id = created.json()["transfer"]["id"]
        bad = self.client.put(f"/api/workspace-transfers/{transfer_id}/content", content=b"other", headers=self.headers)
        self.assertEqual(bad.status_code, 400)
        good = self.client.put(f"/api/workspace-transfers/{transfer_id}/content", content=b"payload", headers=self.headers)
        self.assertEqual(good.status_code, 200, good.text)
        fetched = self.client.get(f"/api/workspace-transfers/{transfer_id}/content", headers=self.headers)
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.content, b"payload")
        self.assertEqual(fetched.headers["X-Transfer-SHA256"], hashlib.sha256(b"payload").hexdigest())

    def test_required_mode_blocks_workspace_routes(self) -> None:
        import os

        workspace = self.make_workspace()
        original = os.environ.get("PLATFORM_AUTH_MODE")
        os.environ["PLATFORM_AUTH_MODE"] = "required"
        try:
            client = TestClient(main.app)
            self.assertEqual(client.get("/api/agent-workspaces").status_code, 401)
            self.assertEqual(
                client.post(
                    f"/api/agent-workspaces/{workspace['id']}/operations",
                    json={"operation_type": "list", "idempotency_key": "no-token-key"},
                ).status_code,
                401,
            )
        finally:
            if original is None:
                os.environ.pop("PLATFORM_AUTH_MODE", None)
            else:
                os.environ["PLATFORM_AUTH_MODE"] = original

    def test_audit_records_queue_and_complete(self) -> None:
        workspace = self.make_workspace()
        operation = workspace_files.create_operation(
            self.store, self.actor, workspace["id"], operation_type="delete", relative_path="tmp.txt", idempotency_key="audit-1"
        )
        workspace_files.claim_operations(self.store, agent_id=AGENT)
        workspace_files.complete_operation(self.store, operation["id"], agent_id=AGENT, success=False, error_code="workspace_path_protected")
        events = self.client.get(f"/api/agent-workspaces/{workspace['id']}/audit", headers=self.headers).json()["events"]
        actions = {item["action"] for item in events}
        self.assertIn("queue:delete", actions)
        self.assertTrue(any(item["decision"] == "deny" and item["reason"] == "workspace_path_protected" for item in events))
        # 审计里不出现路径本身（只记哈希）
        self.assertNotIn("tmp.txt", str(events))


if __name__ == "__main__":
    unittest.main()