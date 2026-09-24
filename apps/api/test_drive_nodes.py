"""个人云盘正式数据模型与服务层（FM-1）契约测试。

这一层要证明的不是"页面能点"，而是六条会出事的性质：

1. **归属隔离**：跨成员、跨组织访问一律按"不存在"处理（不区分"别人的节点"与"没有这个节点"）；
2. **同目录同名**稳定 409，且**不覆盖**；
3. **配额**按去重后的物理占用算，并发上传不突破上限、不重复计费；
4. **删除两段式**：软删除进回收站 → 彻底清除才释放空间；被项目引用的一律不能清除；
5. **对象删除失败不能先丢追踪记录**：进 `drive_object_cleanup` 队列、可重试、`attempts` 与
   `last_error` 都留痕；
6. **老接口不回归**：`/api/drive` 系列仍是老形状，旧的平面文件按原 id/哈希/创建时间可见可下载。

另外把"目录环""修订冲突""同名恢复冲突""名字合法性"这些边界也钉住。
"""

from __future__ import annotations

import hashlib
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app import drive, main, personal_drive
from app.contracts import ArtifactCreate, DriveImportRequest, SessionCreate
from app.store import DEV_ORG_ID, Store

MAKE_REQUEST_MEMBER = "member-001"
LEGACY_CONTENT = "遗留内容".encode("utf-8")


def member_request(member_id: str):
    """构造带成员身份的 Request（开发模式用 member-001，其它成员走真实会话）。"""

    from starlette.requests import Request

    if member_id == MAKE_REQUEST_MEMBER:
        return Request({"type": "http", "method": "POST", "path": "/api/drive", "headers": []})
    token = main.store.create_session(SessionCreate(member_id=member_id, expires_in_seconds=600)).token
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/drive",
            "headers": [(b"authorization", f"Bearer {token}".encode("utf-8"))],
        }
    )


def make_member(store: Store, member_id: str, organization_id: str | None = None) -> str:
    """造一个成员（默认塞进开发组织，给了组织就造一个隔离组织）。"""

    if organization_id is None:
        organization_id = DEV_ORG_ID
        team = store.db.execute("SELECT id FROM teams WHERE organization_id = ? LIMIT 1", (organization_id,)).fetchone()
        team_id = str(team["id"])
    else:
        team_id = str(uuid4())
        store.db.execute(
            "INSERT INTO organizations (id, name, slug, created_at) VALUES (?, ?, ?, ?)",
            (organization_id, f"隔离组织 {member_id}", f"iso-{member_id}", datetime.now(UTC).isoformat()),
        )
        store.db.execute(
            "INSERT INTO teams (id, organization_id, name, created_at) VALUES (?, ?, ?, ?)",
            (team_id, organization_id, "隔离队伍", datetime.now(UTC).isoformat()),
        )
    store.db.execute(
        "INSERT INTO human_members (id, organization_id, team_id, email, display_name, status, created_at) VALUES (?, ?, ?, ?, ?, 'active', ?)",
        (member_id, organization_id, team_id, f"{member_id}@example.test", member_id, datetime.now(UTC).isoformat()),
    )
    store.db.commit()
    return member_id


class DriveTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp_dir.name)
        self.store = Store(self.root / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.client = TestClient(main.app)
        drive.ensure_schema(self.store)
        self.actor = drive.actor_for(self.store, MAKE_REQUEST_MEMBER)

    def tearDown(self) -> None:
        main.store = self.previous_store
        self.store.close()
        self.temp_dir.cleanup()

    def put(self, name: str, content: bytes, parent_id: str | None = None, mime: str = "text/plain"):
        return drive.put_file(self.store, self.actor, parent_id, name, content, mime)


class IsolationTests(DriveTestBase):
    def test_each_member_gets_exactly_one_root(self) -> None:
        first = drive.root_node(self.store, self.actor)
        second = drive.root_node(self.store, self.actor)
        self.assertEqual(first["id"], second["id"])
        rows = self.store.db.execute(
            "SELECT COUNT(*) AS c FROM drive_nodes WHERE owner_member_id = ? AND is_root = 1", (self.actor.member_id,)
        ).fetchone()
        self.assertEqual(int(rows["c"]), 1)

    def test_other_member_cannot_see_or_touch_the_node(self) -> None:
        make_member(self.store, "member-drive-other")
        other = drive.actor_for(self.store, "member-drive-other")
        node = self.put("mine.txt", b"secret")
        for action in (
            lambda: drive.get_node(self.store, other, node["id"]),
            lambda: drive.read_content(self.store, other, node["id"]),
            lambda: drive.rename(self.store, other, node["id"], "hijacked.txt"),
            lambda: drive.trash(self.store, other, node["id"]),
            lambda: drive.move(self.store, other, node["id"], None),
        ):
            with self.assertRaises(drive.DriveError) as caught:
                action()
            self.assertEqual(caught.exception.code, "file_node_not_found")
        self.assertEqual(drive.list_children(self.store, other, None)["nodes"], [])

    def test_other_organization_is_treated_as_missing(self) -> None:
        """同 id、别的组织：按不存在处理（不给出"存在但不是你的"这种可探测信号）。"""

        node = self.put("org.txt", b"x")
        make_member(self.store, "member-drive-foreign", organization_id=str(uuid4()))
        foreign = drive.actor_for(self.store, "member-drive-foreign")
        with self.assertRaises(drive.DriveError) as caught:
            drive.get_node(self.store, foreign, node["id"])
        self.assertEqual(caught.exception.code, "file_node_not_found")

    def test_unknown_actor_is_rejected_not_invented(self) -> None:
        with self.assertRaises(drive.DriveError) as caught:
            drive.actor_for(self.store, "member-does-not-exist")
        self.assertEqual(caught.exception.code, "drive_actor_unknown")


class TreeTests(DriveTestBase):
    def test_directory_tree_listing_and_breadcrumb(self) -> None:
        reports = drive.create_directory(self.store, self.actor, None, "报告")
        figures = drive.create_directory(self.store, self.actor, reports["id"], "图")
        self.put("趋势.png", b"png", figures["id"])
        listing = drive.list_children(self.store, self.actor, reports["id"])
        self.assertEqual([node["name"] for node in listing["nodes"]], ["图"])
        self.assertEqual([crumb["name"] for crumb in drive.breadcrumb(self.store, self.actor, figures["id"])], ["个人云盘", "报告", "图"])
        self.assertEqual(listing["total"], 1)

    def test_same_name_in_same_directory_conflicts(self) -> None:
        drive.create_directory(self.store, self.actor, None, "同名")
        with self.assertRaises(drive.DriveError) as caught:
            drive.create_directory(self.store, self.actor, None, "同名")
        self.assertEqual(caught.exception.code, "file_name_conflict")

    def test_same_name_different_directory_is_fine(self) -> None:
        one = drive.create_directory(self.store, self.actor, None, "甲")
        two = drive.create_directory(self.store, self.actor, None, "乙")
        self.put("data.csv", b"1", one["id"])
        self.put("data.csv", b"2", two["id"])
        self.assertEqual(len(drive.list_children(self.store, self.actor, one["id"])["nodes"]), 1)
        self.assertEqual(len(drive.list_children(self.store, self.actor, two["id"])["nodes"]), 1)

    def test_case_and_width_insensitive_names_collide(self) -> None:
        """大小写/全角差异在用户机器上是同一个名字：平台这边必须判冲突（否则导出落地才炸）。"""

        self.put("Data.csv", b"1")
        with self.assertRaises(drive.DriveError) as caught:
            self.put("data.csv", b"2")
        self.assertEqual(caught.exception.code, "file_name_conflict")
        with self.assertRaises(drive.DriveError):
            self.put("ＤＡＴＡ.csv", b"3")  # 全角

    def test_invalid_names_are_rejected(self) -> None:
        for name in ("", "  ", ".", "..", "a/b.txt", "a\\b.txt", "CON", "trailing."):
            with self.subTest(name=name):
                with self.assertRaises(drive.DriveError) as caught:
                    self.put(name, b"x")
                self.assertIn(caught.exception.code, {"file_name_required", "file_name_invalid"})

    def test_directory_cycle_is_refused(self) -> None:
        outer = drive.create_directory(self.store, self.actor, None, "外")
        inner = drive.create_directory(self.store, self.actor, outer["id"], "内")
        with self.assertRaises(drive.DriveError) as caught:
            drive.move(self.store, self.actor, outer["id"], inner["id"])
        self.assertEqual(caught.exception.code, "file_directory_cycle")

    def test_rename_and_revision_conflict(self) -> None:
        node = self.put("old.txt", b"x")
        renamed = drive.rename(self.store, self.actor, node["id"], "new.txt", expected_revision=node["revision"])
        self.assertEqual(renamed["name"], "new.txt")
        self.assertEqual(renamed["revision"], node["revision"] + 1)
        with self.assertRaises(drive.DriveError) as caught:
            drive.rename(self.store, self.actor, node["id"], "third.txt", expected_revision=node["revision"])
        self.assertEqual(caught.exception.code, "file_revision_conflict")

    def test_root_cannot_be_renamed_moved_trashed_or_copied(self) -> None:
        root = drive.root_node(self.store, self.actor)
        for action in (
            lambda: drive.rename(self.store, self.actor, root["id"], "我的盘"),
            lambda: drive.trash(self.store, self.actor, root["id"]),
            lambda: drive.copy(self.store, self.actor, root["id"]),
        ):
            with self.assertRaises(drive.DriveError) as caught:
                action()
            self.assertEqual(caught.exception.code, "file_root_immutable")

    def test_move_file_between_directories(self) -> None:
        target = drive.create_directory(self.store, self.actor, None, "归档")
        node = self.put("move-me.txt", b"x")
        moved = drive.move(self.store, self.actor, node["id"], target["id"])
        self.assertEqual(moved["parent_id"], target["id"])
        self.assertEqual([n["name"] for n in drive.list_children(self.store, self.actor, None)["nodes"]], ["归档"])

    def test_pagination_cursor_is_stable(self) -> None:
        for index in range(5):
            self.put(f"file-{index}.txt", b"x")
        first = drive.list_children(self.store, self.actor, None, limit=2)
        self.assertEqual(len(first["nodes"]), 2)
        self.assertTrue(first["truncated"])
        second = drive.list_children(self.store, self.actor, None, limit=2, cursor=first["next_cursor"])
        self.assertEqual(len(second["nodes"]), 2)
        self.assertEqual({n["id"] for n in first["nodes"]} & {n["id"] for n in second["nodes"]}, set())
        with self.assertRaises(drive.DriveError):
            drive.list_children(self.store, self.actor, None, limit=2, sort="size", cursor=first["next_cursor"])


class CopyTests(DriveTestBase):
    def test_copy_keeps_two_versions_and_adds_suffix(self) -> None:
        node = self.put("paper.md", "# 报告".encode("utf-8"))
        first = drive.copy(self.store, self.actor, node["id"])
        second = drive.copy(self.store, self.actor, node["id"])
        self.assertEqual(first["name"], "paper-副本.md")
        self.assertEqual(second["name"], "paper-副本2.md")
        self.assertEqual({first["content_hash"], second["content_hash"]}, {node["content_hash"]})

    def test_copy_shares_the_object_so_quota_counts_once(self) -> None:
        node = self.put("big.bin", b"x" * 1000)
        drive.copy(self.store, self.actor, node["id"])
        drive.copy(self.store, self.actor, node["id"])
        usage = drive.usage(self.store, self.actor)
        self.assertEqual(usage["file_count"], 3)
        self.assertEqual(usage["used_bytes"], 1000)  # 同内容共用对象：只算一份

    def test_copy_directory_copies_the_subtree(self) -> None:
        folder = drive.create_directory(self.store, self.actor, None, "数据集")
        drive.create_directory(self.store, self.actor, folder["id"], "子目录")
        self.put("train.csv", b"1,2", folder["id"])
        clone = drive.copy(self.store, self.actor, folder["id"])
        self.assertEqual(clone["name"], "数据集-副本")
        children = drive.list_children(self.store, self.actor, clone["id"])["nodes"]
        self.assertEqual({node["name"] for node in children}, {"子目录", "train.csv"})
        # 复制出来的子目录是**新节点**（不是同一个目录的两条引用）
        self.assertNotEqual(
            next(node["id"] for node in children if node["name"] == "子目录"),
            drive.list_children(self.store, self.actor, folder["id"])["nodes"][0]["id"],
        )


class TrashTests(DriveTestBase):
    def test_trash_then_restore(self) -> None:
        node = self.put("temp.txt", b"x")
        result = drive.trash(self.store, self.actor, node["id"])
        self.assertEqual(result["trashed_count"], 1)
        self.assertEqual([n["name"] for n in drive.trash_list(self.store, self.actor)], ["temp.txt"])
        self.assertEqual(drive.list_children(self.store, self.actor, None)["nodes"], [])
        # 回收站里的文件仍占配额（计划 §5.1/§7.3）
        self.assertEqual(drive.usage(self.store, self.actor)["used_bytes"], 1)
        drive.restore(self.store, self.actor, node["id"])
        self.assertEqual([n["name"] for n in drive.list_children(self.store, self.actor, None)["nodes"]], ["temp.txt"])

    def test_trashed_name_can_be_reused(self) -> None:
        node = self.put("name.txt", b"old")
        drive.trash(self.store, self.actor, node["id"])
        again = self.put("name.txt", b"new")
        self.assertNotEqual(again["id"], node["id"])
        with self.assertRaises(drive.DriveError) as caught:
            drive.restore(self.store, self.actor, node["id"])
        self.assertEqual(caught.exception.code, "file_name_conflict")

    def test_trash_directory_marks_whole_subtree(self) -> None:
        folder = drive.create_directory(self.store, self.actor, None, "整棵")
        drive.create_directory(self.store, self.actor, folder["id"], "内层")
        self.put("deep.txt", b"x", folder["id"])
        result = drive.trash(self.store, self.actor, folder["id"])
        self.assertEqual(result["trashed_count"], 3)
        self.assertEqual([n["name"] for n in drive.trash_list(self.store, self.actor)], ["整棵"])

    def test_purge_requires_trash_first(self) -> None:
        node = self.put("live.txt", b"x")
        with self.assertRaises(drive.DriveError) as caught:
            drive.purge(self.store, self.actor, node["id"])
        self.assertEqual(caught.exception.code, "file_not_trashed")

    def test_purge_directory_with_children_is_refused(self) -> None:
        folder = drive.create_directory(self.store, self.actor, None, "非空")
        self.put("child.txt", b"x", folder["id"])
        drive.trash(self.store, self.actor, folder["id"])
        with self.assertRaises(drive.DriveError) as caught:
            drive.purge(self.store, self.actor, folder["id"])
        self.assertEqual(caught.exception.code, "file_directory_not_empty")

    def test_purge_releases_quota_and_deletes_the_object(self) -> None:
        node = self.put("gone.txt", b"x" * 10)
        key = self.store.db.execute("SELECT storage_key FROM drive_nodes WHERE id = ?", (node["id"],)).fetchone()["storage_key"]
        drive.trash(self.store, self.actor, node["id"])
        result = drive.purge(self.store, self.actor, node["id"])
        self.assertEqual(result["objects_deleted"], 1)
        self.assertEqual(result["objects_pending"], 0)
        self.assertEqual(drive.usage(self.store, self.actor)["used_bytes"], 0)
        self.assertFalse((Path(self.store.object_store.root) / key).exists())
        status = drive.cleanup_queue(self.store, status="deleted")
        self.assertEqual(len(status), 1)
        self.assertEqual(int(status[0]["attempts"]), 1)

    def test_object_delete_failure_keeps_the_tracking_row(self) -> None:
        """删对象失败**不算**成功：队列里留 pending + attempts + last_error，可重试。"""

        node = self.put("stuck.txt", b"x")
        drive.trash(self.store, self.actor, node["id"])

        class BrokenStore:
            """只让 delete 抛错的替身对象存储。"""

            def __init__(self, inner):
                self.inner = inner

            def delete(self, key):
                raise OSError("storage_unavailable")

            def __getattr__(self, item):
                return getattr(self.inner, item)

        original = self.store.object_store
        self.store.object_store = BrokenStore(original)
        try:
            result = drive.purge(self.store, self.actor, node["id"])
        finally:
            self.store.object_store = original
        self.assertEqual(result["objects_deleted"], 0)
        self.assertEqual(result["objects_pending"], 1)
        pending = drive.cleanup_queue(self.store, status="pending")
        self.assertEqual(len(pending), 1)
        self.assertEqual(int(pending[0]["attempts"]), 1)
        self.assertIn("storage_unavailable", str(pending[0]["last_error"]))
        # 重试成功后才真正删掉
        again = drive.retry_cleanup(self.store)
        self.assertEqual(again["deleted"], 1)
        self.assertEqual(drive.cleanup_queue(self.store, status="pending"), [])

    def test_referenced_file_cannot_be_purged(self) -> None:
        project = self.store.list_projects()[0]
        node = self.put("used.csv", b"a,b\n1,2\n")
        imported = personal_drive.import_drive_file_to_project(self.store, MAKE_REQUEST_MEMBER, node["id"], project.id)
        drive.trash(self.store, self.actor, node["id"])
        with self.assertRaises(drive.DriveError) as caught:
            drive.purge(self.store, self.actor, node["id"])
        self.assertEqual(caught.exception.code, "file_referenced_by_project")
        self.assertTrue(imported["artifact_id"])


class QuotaTests(DriveTestBase):
    def test_quota_rejects_and_reports(self) -> None:
        original = personal_drive.DRIVE_QUOTA_BYTES
        personal_drive.DRIVE_QUOTA_BYTES = 10
        try:
            with self.assertRaises(drive.DriveError) as caught:
                self.put("big.bin", b"x" * 20)
            self.assertEqual(caught.exception.code, "file_quota_exceeded")
        finally:
            personal_drive.DRIVE_QUOTA_BYTES = original

    def _race(self, quota: int, payloads: list[bytes]) -> tuple[list[str], list[str], dict]:
        """并发跑一批上传，返回 `(接受的节点, 错误码, 用量)`。"""

        original = personal_drive.DRIVE_QUOTA_BYTES
        personal_drive.DRIVE_QUOTA_BYTES = quota
        accepted: list[str] = []
        errors: list[str] = []
        started = threading.Barrier(len(payloads))

        def upload(index: int) -> None:
            started.wait()
            try:
                node = drive.put_file(self.store, self.actor, None, f"part-{index}.bin", payloads[index])
                accepted.append(node["id"])
            except drive.DriveError as error:
                errors.append(error.code)
            except Exception as error:  # noqa: BLE001 - 任何异常都要如实记账
                errors.append(f"{type(error).__name__}:{error}")

        try:
            threads = [threading.Thread(target=upload, args=(index,)) for index in range(len(payloads))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finally:
            personal_drive.DRIVE_QUOTA_BYTES = original
        return accepted, errors, drive.usage(self.store, self.actor)

    def test_concurrent_uploads_do_not_exceed_the_quota(self) -> None:
        """并发上传不同内容：总量不许越过配额（先算后插必须在同一个事务里）。"""

        # 四条**互不相同**的内容：否则会被内容去重合并成一个对象，测的就不是配额了
        accepted, errors, usage = self._race(1000, [bytes([65 + index]) * 300 for index in range(4)])
        self.assertLessEqual(usage["used_bytes"], 1000)
        self.assertEqual(len(accepted), 3)  # 300×3=900 ≤ 1000，第 4 个必须被拒
        self.assertEqual(set(errors), {"file_quota_exceeded"})

    def test_concurrent_uploads_of_the_same_content_are_charged_once(self) -> None:
        """并发上传**同一份内容**：只该占一份空间（去重查询必须在写锁内）。"""

        accepted, errors, usage = self._race(1000, [b"z" * 300 for _ in range(4)])
        self.assertEqual(errors, [])
        self.assertEqual(len(accepted), 4)
        self.assertEqual(usage["used_bytes"], 300)
        keys = {
            str(drive._raw(self.store, node_id)["storage_key"])
            for node_id in accepted
        }
        self.assertEqual(len(keys), 1)

    def test_duplicate_content_is_charged_once(self) -> None:
        original = personal_drive.DRIVE_QUOTA_BYTES
        personal_drive.DRIVE_QUOTA_BYTES = 600
        try:
            self.put("a.bin", b"z" * 500)
            self.put("b.bin", b"z" * 500)  # 同内容：共用对象，不该被配额拦住
            usage = drive.usage(self.store, self.actor)
        finally:
            personal_drive.DRIVE_QUOTA_BYTES = original
        self.assertEqual(usage["file_count"], 2)
        self.assertEqual(usage["used_bytes"], 500)


class ContentIntegrityTests(DriveTestBase):
    def test_download_verifies_hash(self) -> None:
        node = self.put("check.txt", b"original")
        key = self.store.db.execute("SELECT storage_key FROM drive_nodes WHERE id = ?", (node["id"],)).fetchone()["storage_key"]
        (Path(self.store.object_store.root) / key).write_bytes(b"tampered")
        with self.assertRaises(drive.DriveError) as caught:
            drive.read_content(self.store, self.actor, node["id"])
        self.assertEqual(caught.exception.code, "file_content_hash_mismatch")
        audit = drive.audit_log(self.store, self.actor, node["id"])
        self.assertTrue(any(item["action"] == "download" and item["decision"] == "deny" for item in audit))

    def test_expected_hash_mismatch_is_rejected(self) -> None:
        with self.assertRaises(drive.DriveError) as caught:
            drive.put_file(
                self.store,
                self.actor,
                None,
                "hash.txt",
                b"content",
                expected_hash=hashlib.sha256(b"other").hexdigest(),
            )
        self.assertEqual(caught.exception.code, "file_upload_hash_mismatch")

    def test_audit_records_mutations_without_names(self) -> None:
        node = self.put("secret-name.txt", b"x")
        drive.rename(self.store, self.actor, node["id"], "renamed.txt")
        drive.trash(self.store, self.actor, node["id"])
        events = drive.audit_log(self.store, self.actor, node["id"])
        actions = {item["action"] for item in events}
        self.assertTrue({"upload", "rename", "trash"} <= actions)
        # 审计里不出现文件名本身，只有哈希（计划 §5.9）
        serialized = str(events)
        self.assertNotIn("secret-name.txt", serialized)
        self.assertNotIn("renamed.txt", serialized)


class LegacyCompatibilityTests(DriveTestBase):
    def test_backfill_preserves_ids_hashes_and_refs(self) -> None:
        """老平面表 → 节点树：同 id、同 storage_key、同 hash、同 created_at；幂等。"""

        legacy_id = str(uuid4())
        created = "2026-09-01T00:00:00+00:00"
        self.store.db.execute(
            "CREATE TABLE IF NOT EXISTS personal_drive_files (id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL, size_bytes INTEGER NOT NULL, content_hash TEXT NOT NULL, storage_key TEXT NOT NULL, mime_type TEXT, is_archive INTEGER NOT NULL DEFAULT 0, project_ids TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL)"
        )
        stored = self.store.object_store.put_bytes(f"drive/{MAKE_REQUEST_MEMBER}/{legacy_id}", LEGACY_CONTENT, "text/plain")
        self.store.db.execute(
            "INSERT INTO personal_drive_files (id, owner, name, size_bytes, content_hash, storage_key, mime_type, is_archive, project_ids, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (legacy_id, MAKE_REQUEST_MEMBER, "老文件.csv", 14, hashlib.sha256(LEGACY_CONTENT).hexdigest(), stored.key, "text/csv", '["%s"]' % self.store.list_projects()[0].id, created),
        )
        self.store.db.commit()

        first = drive.backfill_legacy(self.store)
        self.assertEqual(first["created"], 1)
        self.assertEqual(first["refs"], 1)
        node = drive.get_node(self.store, self.actor, legacy_id)
        self.assertEqual(node["created_at"], created)
        self.assertEqual(node["name"], "老文件.csv")
        raw = drive._raw(self.store, legacy_id)
        self.assertEqual(str(raw["storage_key"]), stored.key)
        self.assertEqual(str(raw["content_hash"]), hashlib.sha256(LEGACY_CONTENT).hexdigest())
        self.assertEqual(len(drive.refs_for(self.store, legacy_id)), 1)
        self.assertTrue(drive.refs_for(self.store, legacy_id)[0]["legacy_import"])

        again = drive.backfill_legacy(self.store)
        self.assertEqual(again["created"], 0)
        self.assertEqual(again["skipped"], 1)

    def test_legacy_endpoints_still_work_with_the_old_shape(self) -> None:
        uploaded = main.upload_personal_drive_file(member_request(MAKE_REQUEST_MEMBER), upload_file("data.csv", b"a,b\n"))
        self.assertEqual(uploaded["file"]["name"], "data.csv")
        self.assertIn("project_ids", uploaded["file"])
        listing = main.list_personal_drive(member_request(MAKE_REQUEST_MEMBER))
        self.assertEqual([item["name"] for item in listing["files"]], ["data.csv"])
        self.assertEqual(listing["usage"]["used_bytes"], 4)

        entry, content = personal_drive.read_drive_file(self.store, MAKE_REQUEST_MEMBER, uploaded["file"]["id"])
        self.assertEqual(content, b"a,b\n")
        self.assertEqual(entry["name"], "data.csv")

        deleted = main.delete_personal_drive_file(uploaded["file"]["id"], member_request(MAKE_REQUEST_MEMBER))
        self.assertTrue(deleted["deleted"])
        self.assertEqual(main.list_personal_drive(member_request(MAKE_REQUEST_MEMBER))["usage"]["used_bytes"], 0)

    def test_legacy_duplicate_content_no_longer_merges_two_files(self) -> None:
        """同名不同名两份相同内容：现在是**两个文件**（共用对象、只收一份费）。

        旧实现命中同 hash 就返回同一条记录——那会让"上传两个同名不同内容的文件"里的第二份
        在云盘里彻底看不见。文件管理器不能这么算（计划 §11.4 要求"同 hash 不重复计费"，
        而不是"同 hash 只留一个文件"）。
        """

        first = main.upload_personal_drive_file(member_request(MAKE_REQUEST_MEMBER), upload_file("a.csv", b"same content"))
        second = main.upload_personal_drive_file(member_request(MAKE_REQUEST_MEMBER), upload_file("b.csv", b"same content"))
        self.assertNotEqual(first["file"]["id"], second["file"]["id"])
        usage = main.list_personal_drive(member_request(MAKE_REQUEST_MEMBER))["usage"]
        self.assertEqual(usage["file_count"], 2)
        self.assertEqual(usage["used_bytes"], len(b"same content"))

    def test_legacy_import_still_creates_artifact_and_blocks_deletion(self) -> None:
        project = self.store.list_projects()[0]
        uploaded = main.upload_personal_drive_file(member_request(MAKE_REQUEST_MEMBER), upload_file("model.csv", b"q1,q2\n1,2\n", "text/csv"))
        result = main.import_personal_drive_file(
            project.id, DriveImportRequest(file_id=uploaded["file"]["id"]), member_request(MAKE_REQUEST_MEMBER)
        )
        self.assertEqual(result["artifact_type"], "result_table")
        self.assertEqual(result["content_hash"], uploaded["file"]["content_hash"])
        self.assertEqual([ref["project_id"] for ref in drive.refs_for(self.store, uploaded["file"]["id"])], [str(project.id)])
        again = main.import_personal_drive_file(
            project.id, DriveImportRequest(file_id=uploaded["file"]["id"]), member_request(MAKE_REQUEST_MEMBER)
        )
        # 同内容同名再导入一次：成果物按内容去重（返回同一份），引用也不会重复记
        self.assertEqual(again["artifact_id"], result["artifact_id"])
        self.assertEqual(len(drive.refs_for(self.store, uploaded["file"]["id"])), 1)


class HttpSurfaceTests(DriveTestBase):
    def test_download_headers_are_latin1_safe_and_rfc5987(self) -> None:
        node = self.put("题目原文.txt", "问题一：请给出假设。".encode("utf-8"))
        response = self.client.get(f"/api/drive/nodes/{node['id']}/content")
        self.assertEqual(response.status_code, 200)
        disposition = response.headers["content-disposition"]
        self.assertIn("filename*=UTF-8''", disposition)
        disposition.encode("latin-1")  # 头必须能按 latin-1 编码（否则 Starlette 直接 500）
        self.assertEqual(response.content.decode("utf-8"), "问题一：请给出假设。")

    def test_name_conflict_returns_409_over_http(self) -> None:
        self.client.post("/api/drive/files", files={"file": ("dup.txt", b"1", "text/plain")})
        response = self.client.post("/api/drive/files", files={"file": ("dup.txt", b"2", "text/plain")})
        self.assertEqual(response.status_code, 409)
        self.assertIn("file_name_conflict", response.json()["detail"])

    def test_directory_crud_over_http(self) -> None:
        folder = self.client.post("/api/drive/directories", json={"name": "论文"}).json()["node"]
        self.assertEqual(folder["kind"], "directory")
        listing = self.client.get("/api/drive/nodes", params={"parent_id": folder["id"]}).json()
        self.assertEqual(listing["parent"]["id"], folder["id"])
        renamed = self.client.patch(f"/api/drive/nodes/{folder['id']}", json={"name": "论文稿"}).json()["node"]
        self.assertEqual(renamed["name"], "论文稿")
        # 移动到"根"：根的 id 就是父节点，所以 parent_id 等于根 id（不是 null——null 只表示"根自己"）
        root_id = self.client.get("/api/drive/nodes").json()["parent"]["id"]
        moved = self.client.post(f"/api/drive/nodes/{folder['id']}/move", json={"parent_id": None}).json()["node"]
        self.assertEqual(moved["parent_id"], root_id)

    def test_required_mode_rejects_requests_without_a_token(self) -> None:
        """`PLATFORM_AUTH_MODE=required` 下，新接口没有 Bearer 必须被中间件挡住。"""

        import os

        original = os.environ.get("PLATFORM_AUTH_MODE")
        os.environ["PLATFORM_AUTH_MODE"] = "required"
        try:
            client = TestClient(main.app)
            self.assertEqual(client.get("/api/drive/nodes").status_code, 401)
            self.assertEqual(client.post("/api/drive/directories", json={"name": "x"}).status_code, 401)
            self.assertEqual(client.post("/api/drive/cleanup/retry").status_code, 401)
        finally:
            if original is None:
                os.environ.pop("PLATFORM_AUTH_MODE", None)
            else:
                os.environ["PLATFORM_AUTH_MODE"] = original


def upload_file(name: str, content: bytes, content_type: str | None = None):
    import io

    from fastapi import UploadFile

    return UploadFile(file=io.BytesIO(content), filename=name, headers={"content-type": content_type or "application/octet-stream"})


if __name__ == "__main__":
    unittest.main()