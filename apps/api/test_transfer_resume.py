"""断点续传（FM-6）平台侧契约测试：分片、续传游标、缺片、整份哈希与放弃。

钉住的性质：

1. **续传游标**：`GET .../parts` 要能回答"已收到哪些、还缺哪些、每片多大"，客户端据此只补缺的；
2. **重传一片 = 覆盖**（重试的常态），并写入新的 sha256、attempts +1；
3. **片不齐不许收口**：缺片时 `complete` 明确报缺哪些（不拼出半个文件）；
4. **整份哈希**：所有片到齐但整体 sha256 与声明不符 → 删掉刚拼的对象、标 `failed` 并报冲突；
5. **放弃续传**：作废分片会话、清掉已收记录、状态标 failed；
6. **过期回收连带清分片**（`.multipart/` 不涨）；
7. 片号越界（0 / 10001）与空片被拒。
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app import main, workspace_files
from app.contracts import SessionCreate
from app.store import Store

MEMBER = "member-001"
PART = 1024 * 1024  # 测试里用 1MB 分片，别真造 8MB 数据


class ResumeTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.client = TestClient(main.app)
        workspace_files.ensure_schema(self.store)
        self.actor = workspace_files.actor_for(self.store, MEMBER)
        self.session = self.store.create_session(SessionCreate(member_id=MEMBER, expires_in_seconds=900))
        self.headers = {"Authorization": f"Bearer {self.session.token}"}
        self.workspace = workspace_files.register_workspace(
            self.store,
            agent_id="agent-resume",
            organization_id=self.actor.organization_id,
            display_name="续传测试工作区",
            workspace_identity="ws-resume000001",
        )
        self.content = bytes(range(256)) * (3 * 4096)  # 3MB 且内容可复现

    def tearDown(self) -> None:
        main.store = self.previous_store
        self.store.close()
        self.temp_dir.cleanup()

    def new_transfer(self, *, expected_hash: str | None = None, expected_size: int | None = None) -> dict:
        digest = hashlib.sha256(self.content).hexdigest()
        return workspace_files.create_transfer(
            self.store,
            self.actor,
            source_type="workspace",
            target_type="drive",
            workspace_id=self.workspace["id"],
            expected_hash=digest if expected_hash is None else expected_hash,
            expected_size=len(self.content) if expected_size is None else expected_size,
        )

    def chunks(self, size: int = PART) -> list[bytes]:
        return [self.content[offset : offset + size] for offset in range(0, len(self.content), size)]

    def multipart_leftovers(self) -> int:
        root = Path(self.store.object_store.root) / ".multipart"
        return len([path for path in root.rglob("*") if path.is_file()]) if root.exists() else 0


class PartUploadTests(ResumeTestBase):
    def test_upload_parts_and_complete(self) -> None:
        transfer = self.new_transfer()
        parts = self.chunks()
        for number, chunk in enumerate(parts, start=1):
            result = workspace_files.upload_transfer_part(self.store, self.actor, transfer["id"], number, chunk)
            self.assertEqual(result["cursor"]["received_parts"], list(range(1, number + 1)))
        cursor = workspace_files.list_transfer_parts(self.store, self.actor, transfer["id"])
        self.assertEqual(cursor["total_parts"], len(parts))
        self.assertEqual(cursor["missing_parts"], [])
        self.assertEqual(cursor["received_bytes"], len(self.content))
        completed = workspace_files.complete_transfer_upload(self.store, self.actor, transfer["id"])
        self.assertEqual(completed["transfer"]["status"], "ready")
        self.assertEqual(completed["transfer"]["source_hash"], hashlib.sha256(self.content).hexdigest())
        _row, served = workspace_files.read_transfer_content(self.store, self.actor, transfer["id"])
        self.assertEqual(served, self.content, "拼出来的对象必须逐字节等于原内容")

    def test_resume_cursor_only_asks_for_the_missing_parts(self) -> None:
        transfer = self.new_transfer()
        parts = self.chunks()
        workspace_files.upload_transfer_part(self.store, self.actor, transfer["id"], 1, parts[0])
        workspace_files.upload_transfer_part(self.store, self.actor, transfer["id"], 3, parts[2])
        cursor = workspace_files.list_transfer_parts(self.store, self.actor, transfer["id"])
        self.assertEqual(cursor["received_parts"], [1, 3])
        self.assertEqual(cursor["missing_parts"], [2], "只该要第 2 片")
        with self.assertRaises(workspace_files.WorkspaceError) as caught:
            workspace_files.complete_transfer_upload(self.store, self.actor, transfer["id"])
        self.assertEqual(caught.exception.code, "workspace_transfer_parts_missing")
        self.assertIn("2", caught.exception.detail)
        # 补上缺的那片就能收口
        workspace_files.upload_transfer_part(self.store, self.actor, transfer["id"], 2, parts[1])
        workspace_files.complete_transfer_upload(self.store, self.actor, transfer["id"])
        _row, served = workspace_files.read_transfer_content(self.store, self.actor, transfer["id"])
        self.assertEqual(served, self.content)

    def test_reuploading_a_part_overwrites_and_bumps_attempts(self) -> None:
        transfer = self.new_transfer()
        parts = self.chunks()
        workspace_files.upload_transfer_part(self.store, self.actor, transfer["id"], 1, parts[0])
        again = workspace_files.upload_transfer_part(self.store, self.actor, transfer["id"], 1, parts[0])
        self.assertEqual(again["part"]["attempts"], 2)
        self.assertEqual(again["cursor"]["received_parts"], [1])
        # 重传时给了错的 sha256 → 拒绝，且不覆盖已存的那片
        with self.assertRaises(workspace_files.WorkspaceError) as caught:
            workspace_files.upload_transfer_part(self.store, self.actor, transfer["id"], 1, b"tampered", part_hash="0" * 64)
        self.assertEqual(caught.exception.code, "workspace_transfer_part_hash_mismatch")
        cursor = workspace_files.list_transfer_parts(self.store, self.actor, transfer["id"])
        self.assertEqual(cursor["parts"][0]["content_hash"], hashlib.sha256(parts[0]).hexdigest())

    def test_part_number_and_empty_part_are_rejected(self) -> None:
        transfer = self.new_transfer()
        for number in (0, -1, 10001):
            with self.subTest(number=number):
                with self.assertRaises(workspace_files.WorkspaceError) as caught:
                    workspace_files.upload_transfer_part(self.store, self.actor, transfer["id"], number, b"x")
                self.assertEqual(caught.exception.code, "workspace_transfer_part_invalid")
        with self.assertRaises(workspace_files.WorkspaceError) as caught:
            workspace_files.upload_transfer_part(self.store, self.actor, transfer["id"], 1, b"")
        self.assertEqual(caught.exception.code, "workspace_transfer_part_empty")

    def test_whole_hash_mismatch_deletes_the_assembled_object(self) -> None:
        transfer = self.new_transfer(expected_hash="1" * 64)  # 声明一个永远对不上的哈希
        for number, chunk in enumerate(self.chunks(), start=1):
            workspace_files.upload_transfer_part(self.store, self.actor, transfer["id"], number, chunk)
        with self.assertRaises(workspace_files.WorkspaceError) as caught:
            workspace_files.complete_transfer_upload(self.store, self.actor, transfer["id"])
        self.assertEqual(caught.exception.code, "workspace_transfer_hash_mismatch")
        after = workspace_files.get_transfer(self.store, self.actor, transfer["id"])
        self.assertEqual(after["status"], "failed")
        object_path = Path(self.store.object_store.root) / str(transfer["storage_key"])
        self.assertFalse(object_path.exists(), "整体哈希对不上时不能把对象留在存储里")

    def test_abort_clears_parts_and_multipart_session(self) -> None:
        transfer = self.new_transfer()
        workspace_files.upload_transfer_part(self.store, self.actor, transfer["id"], 1, self.chunks()[0])
        self.assertGreater(self.multipart_leftovers(), 0)
        aborted = workspace_files.abort_transfer_upload(self.store, self.actor, transfer["id"])
        self.assertTrue(aborted["aborted"])
        self.assertEqual(aborted["transfer"]["status"], "failed")
        self.assertEqual(workspace_files.list_transfer_parts(self.store, self.actor, transfer["id"])["parts"], [])
        self.assertEqual(self.multipart_leftovers(), 0, "放弃后不该留半成品分片")

    def test_expiry_cleanup_also_clears_parts(self) -> None:
        transfer = self.new_transfer()
        workspace_files.upload_transfer_part(self.store, self.actor, transfer["id"], 1, self.chunks()[0])
        past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        self.store.db.execute("UPDATE file_transfer_sessions SET expires_at = ? WHERE id = ?", (past, transfer["id"]))
        self.store.db.commit()
        result = workspace_files.cleanup_expired_transfers(self.store, self.actor)
        self.assertGreaterEqual(result["expired"], 1)
        self.assertEqual(workspace_files.list_transfer_parts(self.store, self.actor, transfer["id"])["parts"], [])
        self.assertEqual(self.multipart_leftovers(), 0)

    def test_upload_part_after_expiry_is_refused(self) -> None:
        transfer = self.new_transfer()
        self.store.db.execute("UPDATE file_transfer_sessions SET status = 'expired' WHERE id = ?", (transfer["id"],))
        self.store.db.commit()
        with self.assertRaises(workspace_files.WorkspaceError) as caught:
            workspace_files.upload_transfer_part(self.store, self.actor, transfer["id"], 1, b"x")
        self.assertIn(caught.exception.code, {"workspace_transfer_expired", "workspace_transfer_not_found"})


class ResumeHttpTests(ResumeTestBase):
    def test_http_parts_flow(self) -> None:
        transfer = self.new_transfer()
        parts = self.chunks()
        cursor = self.client.get(f"/api/workspace-transfers/{transfer['id']}/parts", headers=self.headers)
        self.assertEqual(cursor.status_code, 200)
        # 还没传过任何一片：分片大小**如实报未知（0）**，切片大小由客户端定
        self.assertEqual(cursor.json()["part_size_bytes"], 0)
        self.assertEqual(cursor.json()["total_parts"], 0)
        self.assertEqual(cursor.json()["missing_parts"], [])
        self.assertEqual(cursor.json()["parts"], [])

        first = self.client.put(
            f"/api/workspace-transfers/{transfer['id']}/parts/1", content=parts[0], headers=self.headers
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["cursor"]["received_parts"], [1])
        # 首片（1MB）落地后，游标按它推出还缺 2、3 片
        self.assertEqual(first.json()["cursor"]["part_size_bytes"], len(parts[0]))
        self.assertEqual(first.json()["cursor"]["missing_parts"], [2, 3])

        bad_hash = self.client.put(
            f"/api/workspace-transfers/{transfer['id']}/parts/2",
            content=parts[1],
            headers={**self.headers, "X-Part-SHA256": "0" * 64},
        )
        self.assertEqual(bad_hash.status_code, 400)
        self.assertIn("part_hash_mismatch", bad_hash.text)

        missing = self.client.post(f"/api/workspace-transfers/{transfer['id']}/complete", headers=self.headers)
        self.assertEqual(missing.status_code, 400)
        self.assertIn("parts_missing", missing.text)

        for number, chunk in ((2, parts[1]), (3, parts[2])):
            self.client.put(f"/api/workspace-transfers/{transfer['id']}/parts/{number}", content=chunk, headers=self.headers)
        done = self.client.post(f"/api/workspace-transfers/{transfer['id']}/complete", headers=self.headers)
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(done.json()["transfer"]["status"], "ready")
        fetched = self.client.get(f"/api/workspace-transfers/{transfer['id']}/content", headers=self.headers)
        self.assertEqual(fetched.content, self.content)

    def test_http_abort(self) -> None:
        transfer = self.new_transfer()
        self.client.put(f"/api/workspace-transfers/{transfer['id']}/parts/1", content=self.chunks()[0], headers=self.headers)
        aborted = self.client.post(f"/api/workspace-transfers/{transfer['id']}/abort", headers=self.headers)
        self.assertEqual(aborted.status_code, 200)
        self.assertEqual(aborted.json()["transfer"]["status"], "failed")

    def test_required_mode_blocks_part_routes(self) -> None:
        import os

        transfer = self.new_transfer()
        original = os.environ.get("PLATFORM_AUTH_MODE")
        os.environ["PLATFORM_AUTH_MODE"] = "required"
        try:
            client = TestClient(main.app)
            self.assertEqual(client.get(f"/api/workspace-transfers/{transfer['id']}/parts").status_code, 401)
            self.assertEqual(
                client.put(f"/api/workspace-transfers/{transfer['id']}/parts/1", content=b"x").status_code, 401
            )
        finally:
            if original is None:
                os.environ.pop("PLATFORM_AUTH_MODE", None)
            else:
                os.environ["PLATFORM_AUTH_MODE"] = original

    def test_other_member_cannot_touch_parts(self) -> None:
        from tests_support import ensure_member

        transfer = self.new_transfer()
        ensure_member(self.store, "member-resume-other")
        other = self.store.create_session(SessionCreate(member_id="member-resume-other", expires_in_seconds=600))
        response = self.client.get(
            f"/api/workspace-transfers/{transfer['id']}/parts", headers={"Authorization": f"Bearer {other.token}"}
        )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()