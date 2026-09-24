"""个人云盘契约测试：配额、上传、删除、导入项目。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException, UploadFile
from starlette.requests import Request

import io
from app import main
from app.contracts import DriveImportRequest
from app.store import Store


def make_request(member: str = "member-001") -> Request:
    """构造带成员身份的请求：member-001 走开发默认，其他成员用真实 Session。"""

    if member == "member-001":
        return Request({"type": "http", "method": "POST", "path": "/api/drive", "headers": []})
    session = _session_for(member)
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/drive",
            "headers": [(b"authorization", f"Bearer {session.token}".encode("utf-8"))],
        }
    )


def _session_for(member_id: str):
    from app.contracts import SessionCreate

    # 创建另一个成员（若不存在）并签发会话，使隔离测试走真实认证路径。
    store = main.store
    try:
        store.get_member(member_id)
    except KeyError:
        from uuid import uuid4

        organization_id = uuid4()
        team_id = uuid4()
        store.db.execute(
            "INSERT INTO organizations (id, name, slug, created_at) VALUES (?, ?, ?, ?)",
            (str(organization_id), f"隔离组织 {member_id}", f"iso-{member_id}", datetime.now(UTC).isoformat()),
        )
        store.db.execute(
            "INSERT INTO teams (id, organization_id, name, created_at) VALUES (?, ?, ?, ?)",
            (str(team_id), str(organization_id), "隔离队伍", datetime.now(UTC).isoformat()),
        )
        store.db.execute(
            "INSERT INTO human_members (id, organization_id, team_id, email, display_name, status, created_at) VALUES (?, ?, ?, ?, ?, 'active', ?)",
            (member_id, str(organization_id), str(team_id), f"{member_id}@example.test", member_id, datetime.now(UTC).isoformat()),
        )
        store.db.execute(
            "INSERT INTO memberships (member_id, team_id, role, created_at) VALUES (?, ?, 'owner', ?)",
            (member_id, str(team_id), datetime.now(UTC).isoformat()),
        )
        store.db.commit()
    return store.create_session(SessionCreate(member_id=member_id, expires_in_seconds=600))


def upload_file(name: str, content: bytes, content_type: str | None = None) -> UploadFile:
    return UploadFile(file=io.BytesIO(content), filename=name, headers={"content-type": content_type or "application/octet-stream"})


class PersonalDriveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.project = self.store.list_projects()[0]

    def tearDown(self) -> None:
        main.store = self.previous_store
        self.store.close()
        self.temp_dir.cleanup()

    def _upload(self, name: str, content: bytes, content_type: str | None = None):
        return main.upload_personal_drive_file(make_request(), upload_file(name, content, content_type))

    def test_upload_and_list_with_usage(self) -> None:
        first = self._upload("data.csv", b"problem,one\n")
        self.assertEqual(first["file"]["name"], "data.csv")
        self.assertEqual(first["file"]["size_bytes"], 12)
        self.assertFalse(first["file"]["is_archive"])

        second = self._upload("figures.zip", b"PK-zip-content")
        self.assertTrue(second["file"]["is_archive"])

        listing = main.list_personal_drive(make_request())
        self.assertEqual(len(listing["files"]), 2)
        self.assertEqual(listing["usage"]["file_count"], 2)
        self.assertEqual(listing["usage"]["used_bytes"], 12 + 14)

    def test_duplicate_content_is_reused(self) -> None:
        first = self._upload("a.csv", b"same content")
        second = self._upload("b.csv", b"same content")
        self.assertEqual(first["file"]["id"], second["file"]["id"])
        usage = main.list_personal_drive(make_request())["usage"]
        self.assertEqual(usage["file_count"], 1)
        self.assertEqual(usage["used_bytes"], len(b"same content"))

    def test_quota_rejects_oversize(self) -> None:
        import app.personal_drive as drive_module

        original = drive_module.DRIVE_QUOTA_BYTES
        drive_module.DRIVE_QUOTA_BYTES = 10
        try:
            with self.assertRaises(HTTPException) as caught:
                self._upload("big.bin", b"x" * 20)
            self.assertEqual(caught.exception.status_code, 413)
            self.assertIn("drive_quota_exceeded", str(caught.exception.detail))
        finally:
            drive_module.DRIVE_QUOTA_BYTES = original

    def test_empty_and_missing_name_rejected(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            self._upload("empty.bin", b"")
        self.assertEqual(caught.exception.status_code, 400)

    def test_delete_removes_file_and_usage(self) -> None:
        result = self._upload("to-delete.txt", b"delete me")
        file_id = result["file"]["id"]
        deleted = main.delete_personal_drive_file(file_id, make_request())
        self.assertTrue(deleted["deleted"])
        listing = main.list_personal_drive(make_request())
        self.assertEqual(listing["usage"]["file_count"], 0)
        self.assertEqual(listing["usage"]["used_bytes"], 0)

    def test_delete_unknown_file_returns_404(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            main.delete_personal_drive_file("no-such-id", make_request())
        self.assertEqual(caught.exception.status_code, 404)

    def test_import_creates_project_artifact_and_links(self) -> None:
        uploaded = self._upload("model.csv", b"q1,q2\n1,2\n", "text/csv")
        result = main.import_personal_drive_file(
            self.project.id,
            DriveImportRequest(file_id=uploaded["file"]["id"]),
            make_request(),
        )
        self.assertEqual(result["artifact_type"], "result_table")
        self.assertEqual(result["content_hash"], uploaded["file"]["content_hash"])

        artifact = next(
            item for item in self.store.list_artifacts(self.project.id) if str(item.id) == result["artifact_id"]
        )
        self.assertEqual(artifact.name, "model.csv")
        self.assertEqual(self.store.get_artifact_content(artifact.id), b"q1,q2\n1,2\n")

        # 云盘记录被项目引用后不可删除。
        with self.assertRaises(HTTPException) as caught:
            main.delete_personal_drive_file(uploaded["file"]["id"], make_request())
        self.assertEqual(caught.exception.status_code, 409)

    def test_import_unknown_file_returns_404(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            main.import_personal_drive_file(self.project.id, DriveImportRequest(file_id="missing"), make_request())
        self.assertEqual(caught.exception.status_code, 404)

    def test_members_are_isolated(self) -> None:
        first = self._upload("mine.txt", b"owner one file")
        listing = main.list_personal_drive(make_request("member-002"))
        self.assertEqual(listing["files"], [])
        self.assertEqual(listing["usage"]["used_bytes"], 0)
        # 另一个成员看不到也删不了第一个成员的文件。
        with self.assertRaises(HTTPException) as caught:
            main.delete_personal_drive_file(first["file"]["id"], make_request("member-002"))
        self.assertEqual(caught.exception.status_code, 404)

    def test_archive_upload_maps_to_problem_source_on_import(self) -> None:
        uploaded = self._upload("backup.zip", b"PK-zip-content")
        result = main.import_personal_drive_file(
            self.project.id,
            DriveImportRequest(file_id=uploaded["file"]["id"]),
            make_request(),
        )
        self.assertTrue(result["is_archive"])
        self.assertEqual(result["artifact_type"], "problem_source")


if __name__ == "__main__":
    unittest.main()