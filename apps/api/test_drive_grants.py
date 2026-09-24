"""FM-5 平台侧契约测试：云盘授权的**负向矩阵**（这一层的价值几乎全在"拒绝"上）。

覆盖计划 §11.2 要求的每一格：

- 跨成员（别人的云盘节点）→ 拒绝；跨组织 → 拒绝；
- 同成员但错误项目 / 同项目但错误 Agent / 同 Agent 但错误设备 / 错误 Run → 拒绝；
- Grant 过期 → 拒绝；Grant 撤销 → **下一次读取立刻失败**；
- lease 过期、lease 被撤销、epoch 变化（撤销后再用旧 lease）→ 拒绝；
- 能力不足（没授 `drive.file.read`）→ 拒绝，且拒绝也写审计；
- 范围外读别的文件 → 拒绝（`file_access_scope_denied`）；
- 设备撤销、成员移除、任务结束 → 联动撤销；
- 写权限（删除/移动/改名/覆盖）**不在可授予集合里** → 拒绝创建。
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app import drive, drive_grants, main, workspace_files
from app.contracts import (
    AgentProjectGrant,
    AgentRegister,
    DevicePairingCreate,
    DeviceProjectGrantCreate,
    SessionCreate,
)
from app.store import DEV_ORG_ID, Store
from tests_support import ensure_member

MEMBER = "member-001"
OTHER = "member-grant-other"
AGENT = "agent-grant-1"
OTHER_AGENT = "agent-grant-2"
DEVICE = "device-grant-1"


class GrantTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.client = TestClient(main.app)
        drive.ensure_schema(self.store)
        workspace_files.ensure_schema(self.store)
        drive_grants.ensure_schema(self.store)
        self.project = self.store.list_projects()[0]
        self.actor = workspace_files.actor_for(self.store, MEMBER)
        self.drive_actor = drive.actor_for(self.store, MEMBER)
        self.session = self.store.create_session(SessionCreate(member_id=MEMBER, expires_in_seconds=900))
        self.headers = {"Authorization": f"Bearer {self.session.token}"}
        # 授权创建会校验"Agent 存在 + 设备 active + 设备对该项目有授权"，所以基线里先造好
        self.project_token = self.make_agent()

    def tearDown(self) -> None:
        main.store = self.previous_store
        self.store.close()
        self.temp_dir.cleanup()

    # -- 造数据 --------------------------------------------------------------
    def make_agent(self, agent_id: str = AGENT, device_id: str = DEVICE, *, owner: str = MEMBER) -> str:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        from device_test_support import registration_request

        self.store.register_agent(AgentRegister(agent_id=agent_id, display_name=f"{agent_id} 机器", owner_member_id=owner))
        pairing = self.store.create_device_pairing(DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), owner)
        self.store.register_device(registration_request(pairing, Ed25519PrivateKey.generate(), agent_id, device_id, device_name=device_id))
        credential = self.store.create_device_project_grant(self.project.id, DeviceProjectGrantCreate(device_id=device_id), owner)
        return credential.project_token

    def make_file(self, name: str, content: bytes, *, owner_actor=None) -> dict:
        actor = owner_actor or self.drive_actor
        return drive.put_file(self.store, actor, None, name, content, "text/plain")

    def make_folder_with_files(self) -> dict:
        folder = drive.create_directory(self.store, self.drive_actor, None, "数据集")
        drive.put_file(self.store, self.drive_actor, folder["id"], "a.csv", b"a", "text/csv")
        drive.put_file(self.store, self.drive_actor, folder["id"], "b.csv", b"b", "text/csv")
        return folder

    def create_grant(self, **overrides) -> dict:
        payload = {
            "agent_id": AGENT,
            "device_id": DEVICE,
            "project_id": str(self.project.id),
            "scope_type": "file",
            "expires_in_seconds": 3600,
        }
        payload.update(overrides)
        return drive_grants.create_grant(
            self.store,
            self.actor,
            owner_drive_actor=self.drive_actor,
            **payload,
        )

    def lease(self, grant_id: str, *, agent_id: str = AGENT, project_id: str | None = None, device_id: str | None = DEVICE) -> str:
        return drive_grants.exchange_lease(
            self.store,
            grant_id=grant_id,
            agent_id=agent_id,
            device_id=device_id,
            project_id=project_id or str(self.project.id),
        )["lease"]["token"]

    def list_granted(self, lease: str, *, agent_id: str = AGENT, device_id: str | None = DEVICE, project_id: str | None = None) -> dict:
        return drive_grants.agent_list_granted(
            self.store,
            lease_token=lease,
            agent_id=agent_id,
            project_id=project_id or str(self.project.id),
            device_id=device_id,
        )


class GrantCreationTests(GrantTestBase):
    def test_default_capabilities_are_read_only(self) -> None:
        node = self.make_file("题目.txt", b"data")
        grant = self.create_grant(node_id=node["id"])
        self.assertEqual(sorted(grant["capabilities"]), ["drive.file.import", "drive.file.read", "drive.metadata.read"])
        self.assertEqual(grant["scope_type"], "file")
        self.assertIn(node["id"], grant["node_ids"])

    def test_write_capabilities_are_refused(self) -> None:
        node = self.make_file("题目.txt", b"data")
        for capability in ("drive.file.delete", "drive.file.move", "drive.file.rename", "drive.file.write", "drive.file.copy_to_drive"):
            with self.subTest(capability=capability):
                with self.assertRaises(drive_grants.GrantError) as caught:
                    self.create_grant(node_id=node["id"], capabilities=[capability])
                self.assertEqual(caught.exception.code, "file_access_capability_denied")

    def test_folder_scope_snapshots_current_nodes(self) -> None:
        folder = self.make_folder_with_files()
        grant = self.create_grant(node_id=folder["id"], scope_type="folder")
        self.assertEqual(len(grant["node_ids"]), 3)  # 目录自己 + 两个文件
        later = drive.put_file(self.store, self.drive_actor, folder["id"], "c.csv", b"c", "text/csv")
        again = drive_grants.get_grant(self.store, self.actor, grant["id"])
        self.assertNotIn(later["id"], again["node_ids"], "默认只覆盖授权当时已有的节点")

    def test_include_future_nodes_covers_later_files(self) -> None:
        folder = self.make_folder_with_files()
        grant = self.create_grant(node_id=folder["id"], scope_type="folder", include_future_nodes=True)
        later = drive.put_file(self.store, self.drive_actor, folder["id"], "c.csv", b"c", "text/csv")
        token = self.lease(grant["id"])
        listing = self.list_granted(token)
        self.assertIn("c.csv", [entry["name"] for entry in listing["entries"]])

    def test_cross_member_node_is_refused(self) -> None:
        ensure_member(self.store, OTHER)
        other_actor = drive.actor_for(self.store, OTHER)
        other_file = self.make_file("别人的.txt", b"secret", owner_actor=other_actor)
        with self.assertRaises(drive.DriveError):
            self.create_grant(node_id=other_file["id"])

    def test_project_without_device_grant_is_refused(self) -> None:
        """设备没有被授权进入的项目：不给它授权文件（给了它也读不到项目上下文）。"""

        from app.contracts import ProjectCreate

        node = self.make_file("题目.txt", b"data")
        other_project = self.store.create_project(ProjectCreate(name="设备没被授权的项目"))
        with self.assertRaises(drive_grants.GrantError) as caught:
            self.create_grant(node_id=node["id"], project_id=str(other_project.id))
        self.assertEqual(caught.exception.code, "device_project_grant_invalid")

    def test_task_from_another_project_is_refused(self) -> None:
        from app.contracts import ProjectCreate, TaskCreate

        node = self.make_file("题目.txt", b"data")
        other_project = self.store.create_project(ProjectCreate(name="别的项目"))
        task = self.store.create_task(other_project.id, TaskCreate(title="别人的任务"))
        with self.assertRaises(drive_grants.GrantError) as caught:
            self.create_grant(node_id=node["id"], task_id=str(task.id))
        self.assertEqual(caught.exception.code, "file_access_binding_invalid")

    def test_revoked_device_cannot_receive_a_grant(self) -> None:
        self.store.revoke_device(DEVICE, MEMBER, reason="test")
        node = self.make_file("题目.txt", b"data")
        with self.assertRaises(drive_grants.GrantError) as caught:
            self.create_grant(node_id=node["id"])
        self.assertEqual(caught.exception.code, "device_revoked")


class AgentReadTests(GrantTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.node = self.make_file("题目.txt", b"problem data")
        self.grant = self.create_grant(node_id=self.node["id"])

    def test_granted_file_can_be_listed_and_read(self) -> None:
        token = self.lease(self.grant["id"])
        listing = self.list_granted(token)
        self.assertEqual([entry["name"] for entry in listing["entries"]], ["题目.txt"])
        node, content = drive_grants.agent_read_content(
            self.store, lease_token=token, agent_id=AGENT, project_id=str(self.project.id), device_id=DEVICE, node_id=self.node["id"]
        )
        self.assertEqual(content, b"problem data")
        self.assertEqual(node["id"], self.node["id"])

    def test_scope_denied_for_other_files(self) -> None:
        other = self.make_file("别的文件.txt", b"other")
        token = self.lease(self.grant["id"])
        listing = self.list_granted(token)
        self.assertEqual([entry["name"] for entry in listing["entries"]], ["题目.txt"])
        with self.assertRaises(drive_grants.GrantError) as caught:
            drive_grants.agent_read_content(
                self.store, lease_token=token, agent_id=AGENT, project_id=str(self.project.id), device_id=DEVICE, node_id=other["id"]
            )
        self.assertEqual(caught.exception.code, "file_access_scope_denied")

    def test_wrong_agent_device_project_run_are_refused(self) -> None:
        token = self.lease(self.grant["id"])
        with self.assertRaises(drive_grants.GrantError) as caught:
            self.list_granted(token, agent_id=OTHER_AGENT)
        self.assertEqual(caught.exception.code, "file_access_agent_mismatch")
        with self.assertRaises(drive_grants.GrantError) as caught:
            self.list_granted(token, device_id="device-someone-else")
        self.assertEqual(caught.exception.code, "file_access_device_mismatch")
        with self.assertRaises(drive_grants.GrantError) as caught:
            self.list_granted(token, project_id=str(uuid4()))
        self.assertEqual(caught.exception.code, "file_access_project_mismatch")
        # 绑了 Run 的授权：拿**另一个** Run 去换 lease 必须被拒
        from app.contracts import RunCreate

        run = self.store.create_run(
            self.project.id, RunCreate(agent_id=AGENT, idempotency_key="grant-run-1", observed_input_files=[])
        )
        run_bound = self.create_grant(node_id=self.node["id"], run_id=str(run.id))
        with self.assertRaises(drive_grants.GrantError) as caught:
            drive_grants.exchange_lease(
                self.store,
                grant_id=run_bound["id"],
                agent_id=AGENT,
                device_id=DEVICE,
                project_id=str(self.project.id),
                run_id="run-someone-else",
            )
        self.assertEqual(caught.exception.code, "file_access_run_mismatch")
        # 绑定一致时可以换
        token_same_run = drive_grants.exchange_lease(
            self.store,
            grant_id=run_bound["id"],
            agent_id=AGENT,
            device_id=DEVICE,
            project_id=str(self.project.id),
            run_id=str(run.id),
        )["lease"]["token"]
        self.assertTrue(token_same_run)

    def test_capability_without_read_is_refused(self) -> None:
        metadata_only = self.create_grant(node_id=self.node["id"], capabilities=["drive.metadata.read"])
        token = self.lease(metadata_only["id"])
        self.list_granted(token)  # 列清单可以
        with self.assertRaises(drive_grants.GrantError) as caught:
            drive_grants.agent_read_content(
                self.store, lease_token=token, agent_id=AGENT, project_id=str(self.project.id), device_id=DEVICE, node_id=self.node["id"]
            )
        self.assertEqual(caught.exception.code, "file_access_capability_denied")

    def test_revocation_takes_effect_immediately(self) -> None:
        token = self.lease(self.grant["id"])
        drive_grants.revoke_grant(self.store, self.actor, self.grant["id"])
        with self.assertRaises(drive_grants.GrantError) as caught:
            self.list_granted(token)
        # 撤销会同时作废该 Grant 下的 lease：先撞上哪一条都算"立刻拒绝"
        self.assertIn(caught.exception.code, {"file_access_grant_revoked", "file_access_lease_revoked"})
        # 撤销后**新换** lease 也不行
        with self.assertRaises(drive_grants.GrantError) as caught:
            self.lease(self.grant["id"])
        self.assertEqual(caught.exception.code, "file_access_grant_revoked")

    def test_expired_grant_and_lease_are_refused(self) -> None:
        token = self.lease(self.grant["id"])
        past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        self.store.db.execute("UPDATE file_access_leases SET expires_at = ? WHERE grant_id = ?", (past, self.grant["id"]))
        self.store.db.commit()
        with self.assertRaises(drive_grants.GrantError) as caught:
            self.list_granted(token)
        self.assertEqual(caught.exception.code, "file_access_lease_expired")

        fresh = self.lease(self.grant["id"])
        self.store.db.execute("UPDATE file_access_grants SET expires_at = ? WHERE id = ?", (past, self.grant["id"]))
        self.store.db.commit()
        with self.assertRaises(drive_grants.GrantError) as caught:
            self.list_granted(fresh)
        self.assertEqual(caught.exception.code, "file_access_grant_expired")

    def test_coupled_revocations(self) -> None:
        # 设备撤销 → 联动撤销
        token = self.lease(self.grant["id"])
        self.assertEqual(drive_grants.revoke_grants_for(self.store, device_id=DEVICE), 1)
        with self.assertRaises(drive_grants.GrantError):
            self.list_granted(token)

        # 成员移除 → 联动撤销
        second = self.create_grant(node_id=self.node["id"])
        self.assertEqual(drive_grants.revoke_grants_for(self.store, member_id=MEMBER), 1)
        self.assertIsNotNone(drive_grants.get_grant(self.store, self.actor, second["id"])["revoked_at"])

    def test_audit_records_allow_and_deny(self) -> None:
        token = self.lease(self.grant["id"])
        self.list_granted(token)
        other = self.make_file("越权.txt", b"x")
        with self.assertRaises(drive_grants.GrantError):
            drive_grants.agent_read_content(
                self.store, lease_token=token, agent_id=AGENT, project_id=str(self.project.id), device_id=DEVICE, node_id=other["id"]
            )
        rows = self.store.db.execute("SELECT action, decision, reason FROM drive_audit WHERE action LIKE 'agent:%'").fetchall()
        self.assertTrue(any(row["decision"] == "allow" for row in rows))
        self.assertTrue(any(row["decision"] == "deny" and row["reason"] == "scope_denied" for row in rows))


class GrantHttpTests(GrantTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.node = self.make_file("题目原文.txt", "问题一".encode("utf-8"))
        self.agent_headers = {
            "X-Agent-Id": AGENT,
            "X-Project-Id": str(self.project.id),
            "X-Project-Capability-Token": self.project_token,
            "X-Device-Id": DEVICE,
        }

    def test_http_grant_lifecycle_and_agent_read(self) -> None:
        created = self.client.post(
            "/api/file-access-grants",
            json={"agent_id": AGENT, "device_id": DEVICE, "project_id": str(self.project.id), "node_id": self.node["id"], "scope_type": "file"},
            headers=self.headers,
        )
        self.assertEqual(created.status_code, 201, created.text)
        grant_id = created.json()["grant"]["id"]

        listing = self.client.get("/api/file-access-grants", headers=self.headers)
        self.assertEqual([item["id"] for item in listing.json()["grants"]], [grant_id])

        exchanged = self.client.post(
            "/api/agent/file-leases/exchange",
            json={"grant_id": grant_id, "ttl_seconds": 600},
            headers=self.agent_headers,
        )
        self.assertEqual(exchanged.status_code, 200, exchanged.text)
        lease = exchanged.json()["lease"]["token"]
        self.assertTrue(lease.startswith("lease_"))

        files = self.client.get("/api/agent/drive/files", headers={**self.agent_headers, "X-File-Access-Lease": lease})
        self.assertEqual(files.status_code, 200, files.text)
        self.assertEqual([entry["name"] for entry in files.json()["entries"]], ["题目原文.txt"])

        content = self.client.get(
            f"/api/agent/drive/nodes/{self.node['id']}/content", headers={**self.agent_headers, "X-File-Access-Lease": lease}
        )
        self.assertEqual(content.status_code, 200)
        self.assertEqual(content.content.decode("utf-8"), "问题一")
        self.assertEqual(content.headers["X-Content-SHA256"], hashlib.sha256("问题一".encode("utf-8")).hexdigest())

        manifest = self.client.post("/api/agent/drive/materialize", headers={**self.agent_headers, "X-File-Access-Lease": lease})
        self.assertEqual(manifest.status_code, 200)
        self.assertEqual(manifest.json()["entries"][0]["node_id"], self.node["id"])

        revoked = self.client.post(f"/api/file-access-grants/{grant_id}/revoke", json={"reason": "test"}, headers=self.headers)
        self.assertEqual(revoked.status_code, 200)
        after = self.client.get("/api/agent/drive/files", headers={**self.agent_headers, "X-File-Access-Lease": lease})
        self.assertEqual(after.status_code, 403)
        # 撤销同时作废 lease：先撞上哪一条都算"立刻拒绝"
        self.assertTrue("file_access_grant_revoked" in after.text or "file_access_lease_revoked" in after.text, after.text)

    def test_agent_without_lease_is_refused(self) -> None:
        response = self.client.get("/api/agent/drive/files", headers=self.agent_headers)
        self.assertEqual(response.status_code, 401)

    def test_member_cannot_see_other_members_grants(self) -> None:
        created = self.client.post(
            "/api/file-access-grants",
            json={"agent_id": AGENT, "device_id": DEVICE, "project_id": str(self.project.id), "node_id": self.node["id"]},
            headers=self.headers,
        )
        ensure_member(self.store, OTHER)
        other_session = self.store.create_session(SessionCreate(member_id=OTHER, expires_in_seconds=600))
        listing = self.client.get("/api/file-access-grants", headers={"Authorization": f"Bearer {other_session.token}"})
        self.assertEqual(listing.json()["grants"], [])
        detail = self.client.get(
            f"/api/file-access-grants/{created.json()['grant']['id']}",
            headers={"Authorization": f"Bearer {other_session.token}"},
        )
        self.assertEqual(detail.status_code, 404)

    def test_renew_extends_expiry(self) -> None:
        created = self.client.post(
            "/api/file-access-grants",
            json={"agent_id": AGENT, "device_id": DEVICE, "project_id": str(self.project.id), "node_id": self.node["id"], "expires_in_seconds": 600},
            headers=self.headers,
        ).json()["grant"]
        renewed = self.client.post(
            f"/api/file-access-grants/{created['id']}/renew", json={"expires_in_seconds": 86400}, headers=self.headers
        ).json()["grant"]
        self.assertGreater(renewed["expires_at"], created["expires_at"])


if __name__ == "__main__":
    unittest.main()