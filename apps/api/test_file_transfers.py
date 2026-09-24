"""FM-6 契约测试：显式跨空间传输、冲突口径与维护口。

这一层要证明的是**没有静默覆盖、没有假装成功**：

1. 云盘 → 工作区：内容按 sha256 搬进传输会话，入队一个 upload 操作；目标同名默认不覆盖（由内核拦）；
2. 工作区 → 云盘：Agent 传完再"存进云盘"，同名冲突 → 409（让人选改名/换目录/跳过），**不覆盖**；
3. 冲突口径：`file_name_conflict` / `file_revision_conflict` 原样上报（不吞掉）；
4. 哈希不符的会话拒绝存（`file_upload_hash_mismatch`）；
5. 传输历史带失败原因与"可重试"标记；
6. 过期会话回收会删对象；孤儿扫描**只报告不删**。
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app import drive, file_transfers, main, workspace_files
from app.contracts import SessionCreate
from app.store import Store

MEMBER = "member-001"


class TransferTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.client = TestClient(main.app)
        drive.ensure_schema(self.store)
        workspace_files.ensure_schema(self.store)
        self.actor = workspace_files.actor_for(self.store, MEMBER)
        self.drive_actor = drive.actor_for(self.store, MEMBER)
        self.session = self.store.create_session(SessionCreate(member_id=MEMBER, expires_in_seconds=900))
        self.headers = {"Authorization": f"Bearer {self.session.token}"}
        self.workspace = workspace_files.register_workspace(
            self.store,
            agent_id="agent-transfer",
            organization_id=self.actor.organization_id,
            display_name="传输测试工作区",
            workspace_identity="ws-transfer0001",
        )

    def tearDown(self) -> None:
        main.store = self.previous_store
        self.store.close()
        self.temp_dir.cleanup()

    def put_drive_file(self, name: str, content: bytes) -> dict:
        return drive.put_file(self.store, self.drive_actor, None, name, content, "text/plain")


class DriveToWorkspaceTests(TransferTestBase):
    def test_drive_to_workspace_creates_transfer_and_upload_operation(self) -> None:
        content = "第一问的数据".encode("utf-8")
        node = self.put_drive_file("数据.csv", content)
        result = file_transfers.drive_to_workspace(self.store, self.actor, node_id=node["id"], workspace_id=self.workspace["id"])
        self.assertEqual(result["transfer"]["status"], "ready")
        self.assertEqual(result["transfer"]["source_type"], "drive")
        self.assertEqual(result["operation"]["operation_type"], "upload")
        self.assertEqual(result["operation"]["status"], "queued")
        # 内容已经搬进传输会话（Agent 领到就能直接落盘）
        _transfer, served = workspace_files.read_transfer_content(self.store, self.actor, result["transfer"]["id"])
        self.assertEqual(served, content)
        self.assertEqual(result["transfer"]["expected_hash"], hashlib.sha256(content).hexdigest())

    def test_drive_to_workspace_rejects_directories(self) -> None:
        folder = drive.create_directory(self.store, self.drive_actor, None, "目录")
        with self.assertRaises(file_transfers.TransferError) as caught:
            file_transfers.drive_to_workspace(self.store, self.actor, node_id=folder["id"], workspace_id=self.workspace["id"])
        # 目录不是文件：云盘那层就报"这个节点读不到"（不是"传输格式不对"）
        self.assertIn(caught.exception.code, {"file_node_not_found", "file_transfer_source_invalid"})

    def test_each_call_is_its_own_explicit_copy(self) -> None:
        """显式动作语义：再点一次就是再复制一次（两条独立操作、两个独立会话）。

        同名不覆盖由**内核**拦（`workspace_path_conflict`），所以第二次会失败并如实显示——
        这正是"显式复制"该有的样子；要幂等请重试同一条操作（FM-3 的幂等键覆盖那条路径）。
        """

        node = self.put_drive_file("幂等.csv", b"same")
        first = file_transfers.drive_to_workspace(self.store, self.actor, node_id=node["id"], workspace_id=self.workspace["id"])
        second = file_transfers.drive_to_workspace(self.store, self.actor, node_id=node["id"], workspace_id=self.workspace["id"])
        self.assertNotEqual(first["operation"]["id"], second["operation"]["id"])
        self.assertNotEqual(first["transfer"]["id"], second["transfer"]["id"])
        self.assertEqual(first["operation"]["status"], "queued")


class WorkspaceToDriveTests(TransferTestBase):
    def test_workspace_to_drive_then_save(self) -> None:
        started = file_transfers.workspace_to_drive(self.store, self.actor, workspace_id=self.workspace["id"], relative_path="out/结果.md")
        transfer_id = started["transfer"]["id"]
        self.assertEqual(started["operation"]["operation_type"], "download")
        content = "# 结果\n来自工作区".encode("utf-8")
        workspace_files.write_transfer_content(self.store, self.actor, transfer_id, content)
        saved = file_transfers.save_transfer_to_drive(self.store, self.actor, transfer_id=transfer_id, name="结果.md")
        self.assertEqual(saved["node"]["name"], "结果.md")
        _node, stored = drive.read_content(self.store, self.drive_actor, saved["node"]["id"])
        self.assertEqual(stored, content)

    def test_save_refuses_to_overwrite_a_same_name_file(self) -> None:
        self.put_drive_file("结果.md", "已有内容".encode("utf-8"))
        started = file_transfers.workspace_to_drive(self.store, self.actor, workspace_id=self.workspace["id"], relative_path="结果.md")
        workspace_files.write_transfer_content(self.store, self.actor, started["transfer"]["id"], "新内容".encode("utf-8"))
        with self.assertRaises(file_transfers.TransferError) as caught:
            file_transfers.save_transfer_to_drive(self.store, self.actor, transfer_id=started["transfer"]["id"], name="结果.md")
        self.assertEqual(caught.exception.code, "file_name_conflict")
        # 原文件没有被改
        listing = drive.list_children(self.store, self.drive_actor, None)["nodes"]
        existing = next(item for item in listing if item["name"] == "结果.md")
        _node, content = drive.read_content(self.store, self.drive_actor, existing["id"])
        self.assertEqual(content, "已有内容".encode("utf-8"))

    def test_save_refuses_hash_mismatch(self) -> None:
        started = file_transfers.workspace_to_drive(self.store, self.actor, workspace_id=self.workspace["id"], relative_path="x.bin")
        transfer_id = started["transfer"]["id"]
        workspace_files.write_transfer_content(self.store, self.actor, transfer_id, b"right")
        # 偷偷改掉声明哈希（模拟"传上来的东西跟声明的不一样"）
        self.store.db.execute("UPDATE file_transfer_sessions SET expected_hash = ? WHERE id = ?", ("0" * 64, transfer_id))
        self.store.db.commit()
        with self.assertRaises(file_transfers.TransferError) as caught:
            file_transfers.save_transfer_to_drive(self.store, self.actor, transfer_id=transfer_id)
        # 读取时就发现内容与声明不符（同一个错误族，名字取决于是哪一层先看见）
        self.assertIn(caught.exception.code, {"file_upload_hash_mismatch", "workspace_transfer_hash_mismatch"})

    def test_expired_transfer_cannot_be_saved(self) -> None:
        started = file_transfers.workspace_to_drive(self.store, self.actor, workspace_id=self.workspace["id"], relative_path="x.bin")
        workspace_files.write_transfer_content(self.store, self.actor, started["transfer"]["id"], b"data")
        past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        self.store.db.execute("UPDATE file_transfer_sessions SET expires_at = ? WHERE id = ?", (past, started["transfer"]["id"]))
        self.store.db.commit()
        with self.assertRaises(file_transfers.TransferError) as caught:
            file_transfers.save_transfer_to_drive(self.store, self.actor, transfer_id=started["transfer"]["id"])
        self.assertIn(caught.exception.code, {"workspace_transfer_expired", "file_transfer_expired"})


class HistoryAndMaintenanceTests(TransferTestBase):
    def test_history_reports_failures_with_reason(self) -> None:
        started = file_transfers.workspace_to_drive(self.store, self.actor, workspace_id=self.workspace["id"], relative_path="x.bin")
        # 手动把操作挂到会话上并标失败（模拟 Agent 回报失败）
        self.store.db.execute(
            "UPDATE file_transfer_sessions SET operation_id = ? WHERE id = ?",
            (started["operation"]["id"], started["transfer"]["id"]),
        )
        workspace_files.complete_operation(
            self.store, started["operation"]["id"], agent_id="agent-transfer", success=False, error_code="workspace_path_conflict"
        )
        self.store.db.commit()
        history = file_transfers.transfer_history(self.store, self.actor, workspace_id=self.workspace["id"])
        entry = next(item for item in history if item["id"] == started["transfer"]["id"])
        self.assertEqual(entry["operation"]["status"], "failed")
        self.assertEqual(entry["operation"]["error_code"], "workspace_path_conflict")
        self.assertTrue(entry["retryable"])

    def test_cleanup_expired_transfers_deletes_objects(self) -> None:
        started = file_transfers.workspace_to_drive(self.store, self.actor, workspace_id=self.workspace["id"], relative_path="x.bin")
        workspace_files.write_transfer_content(self.store, self.actor, started["transfer"]["id"], b"data")
        store_roots = list((Path(self.store.object_store.root) / "transfers").rglob("*"))
        files_before = [path for path in store_roots if path.is_file()]
        past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        self.store.db.execute("UPDATE file_transfer_sessions SET expires_at = ? WHERE id = ?", (past, started["transfer"]["id"]))
        self.store.db.commit()
        result = file_transfers.cleanup_transfers(self.store, self.actor)
        self.assertGreaterEqual(result["expired"], 1)
        after = [path for path in (Path(self.store.object_store.root) / "transfers").rglob("*") if path.is_file()]
        self.assertLess(len(after), len(files_before), "过期会话的对象应当被删掉")

    def test_orphan_scan_reports_without_deleting(self) -> None:
        node = self.put_drive_file("会被删的.txt", b"orphan")
        raw = drive._raw(self.store, node["id"])
        key = str(raw["storage_key"])
        self.store.db.execute("UPDATE drive_nodes SET purged_at = ?, storage_key = storage_key WHERE id = ?", ("2026-01-01T00:00:00+00:00", node["id"]))
        self.store.db.commit()
        before = (Path(self.store.object_store.root) / key).is_file()
        report = file_transfers.scan_orphans(self.store, self.actor)
        self.assertEqual(report["note"].startswith("只报告不删"), True)
        self.assertIn(key, report["drive_orphan_keys"])
        if before:
            self.assertTrue((Path(self.store.object_store.root) / key).is_file(), "扫描不许删对象")


class TransferHttpTests(TransferTestBase):
    def test_transfer_endpoints_over_http(self) -> None:
        node = self.put_drive_file("题目.txt", "内容".encode("utf-8"))
        created = self.client.post(
            "/api/file-transfers/drive-to-workspace",
            json={"node_id": node["id"], "workspace_id": self.workspace["id"]},
            headers=self.headers,
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["operation"]["operation_type"], "upload")

        started = self.client.post(
            "/api/file-transfers/workspace-to-drive",
            json={"workspace_id": self.workspace["id"], "relative_path": "out/结果.md"},
            headers=self.headers,
        )
        self.assertEqual(started.status_code, 201, started.text)
        transfer_id = started.json()["transfer"]["id"]
        put = self.client.put(f"/api/workspace-transfers/{transfer_id}/content", content='结果'.encode("utf-8"), headers=self.headers)
        self.assertEqual(put.status_code, 200)
        saved = self.client.post(
            "/api/file-transfers/save-to-drive", json={"transfer_id": transfer_id, "name": "结果.md"}, headers=self.headers
        )
        self.assertEqual(saved.status_code, 201, saved.text)

        conflict = self.client.post(
            "/api/file-transfers/save-to-drive", json={"transfer_id": transfer_id, "name": "结果.md"}, headers=self.headers
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)

        history = self.client.get("/api/file-transfers", headers=self.headers)
        self.assertEqual(history.status_code, 200)
        self.assertTrue(history.json()["transfers"])

        orphans = self.client.post("/api/file-maintenance/orphans/scan", headers=self.headers)
        self.assertEqual(orphans.status_code, 200)
        self.assertIn("drive_orphan_count", orphans.json())

    def test_required_mode_blocks_transfer_routes(self) -> None:
        import os

        original = os.environ.get("PLATFORM_AUTH_MODE")
        os.environ["PLATFORM_AUTH_MODE"] = "required"
        try:
            client = TestClient(main.app)
            self.assertEqual(client.get("/api/file-transfers").status_code, 401)
            self.assertEqual(client.post("/api/file-maintenance/orphans/scan").status_code, 401)
        finally:
            if original is None:
                os.environ.pop("PLATFORM_AUTH_MODE", None)
            else:
                os.environ["PLATFORM_AUTH_MODE"] = original


if __name__ == "__main__":
    unittest.main()