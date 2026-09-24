"""安全解压（FM-2）契约测试：攻击样本必须被拒绝，且**失败不留半个目录**。

这一层的价值全在"拒绝"上：路径穿越、绝对路径、盘符、UNC、symlink/hardlink、设备文件、
zip bomb、条目数、嵌套归档、同名冲突——每一条都得有真样本打进来。另外还要证明两件事：

1. 校验阶段**不写任何东西**（被拒的归档在云盘里既没有新节点、对象存储里也没有新对象）；
2. 写入阶段中途失败会**补偿清理**（用户看到的要么是完整结果，要么什么都没有）。
"""

from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from app import archive, drive, main, personal_drive
from app.store import Store

MEMBER = "member-001"


def zip_bytes(entries: dict[str, bytes], *, mode: dict[str, int] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as handle:
        for name, content in entries.items():
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED  # 手搓的 ZipInfo 默认是 STORED，压不出来就测不了压缩比
            info.external_attr = ((mode or {}).get(name, 0o100644) & 0xFFFF) << 16
            handle.writestr(info, content)
    return buffer.getvalue()


def tar_bytes(entries: list[tuple[str, bytes]], *, links: list[tuple[str, str, str]] | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as handle:
        for name, content in entries:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            handle.addfile(info, io.BytesIO(content))
        for kind, name, target in links or []:
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE if kind == "sym" else tarfile.LNKTYPE if kind == "hard" else tarfile.CHRTYPE
            info.linkname = target
            handle.addfile(info)
    return buffer.getvalue()


class ExtractionTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.client = TestClient(main.app)
        drive.ensure_schema(self.store)
        self.actor = drive.actor_for(self.store, MEMBER)
        self.quota = personal_drive.DRIVE_QUOTA_BYTES

    def tearDown(self) -> None:
        main.store = self.previous_store
        self.store.close()
        self.temp_dir.cleanup()

    def upload(self, name: str, content: bytes) -> dict:
        return drive.put_file(self.store, self.actor, None, name, content)

    def object_count(self) -> int:
        return len(list((Path(self.store.object_store.root) / "drive").rglob("*")))

    def node_count(self) -> int:
        row = self.store.db.execute("SELECT COUNT(*) AS c FROM drive_nodes WHERE purged_at IS NULL").fetchone()
        return int(row["c"])

    def assert_rejected(self, name: str, payload: bytes, code: str) -> None:
        node = self.upload(name, payload)
        nodes_before = self.node_count()
        objects_before = self.object_count()
        with self.assertRaises(drive.DriveError) as caught:
            archive.extract_archive(self.store, self.actor, node["id"])
        self.assertEqual(caught.exception.code, code)
        # 被拒 = 什么都没写（节点与对象都不多）
        self.assertEqual(self.node_count(), nodes_before, "被拒的归档不该留下节点")
        self.assertEqual(self.object_count(), objects_before, "被拒的归档不该往对象存储里多写东西")


class ScanAttackTests(ExtractionTestBase):
    def test_zip_slip_is_rejected(self) -> None:
        self.assert_rejected("slip.zip", zip_bytes({"../escape.txt": b"x"}), "archive_path_unsafe")

    def test_absolute_path_is_rejected(self) -> None:
        self.assert_rejected("abs.zip", zip_bytes({"/etc/passwd": b"x"}), "archive_path_unsafe")

    def test_windows_drive_and_unc_are_rejected(self) -> None:
        self.assert_rejected("drive.zip", zip_bytes({"C:/Windows/system32/evil.dll": b"x"}), "archive_path_unsafe")
        self.assert_rejected("unc.zip", zip_bytes({"//server/share/evil.txt": b"x"}), "archive_path_unsafe")

    def test_zip_symlink_is_rejected(self) -> None:
        payload = zip_bytes({"link": b"../target"}, mode={"link": 0o120777})
        self.assert_rejected("symlink.zip", payload, "archive_path_unsafe")

    def test_tar_symlink_and_hardlink_are_rejected(self) -> None:
        self.assert_rejected("sym.tar.gz", tar_bytes([("ok.txt", b"x")], links=[("sym", "link", "/etc/passwd")]), "archive_path_unsafe")
        self.assert_rejected("hard.tar.gz", tar_bytes([("ok.txt", b"x")], links=[("hard", "link", "ok.txt")]), "archive_path_unsafe")

    def test_tar_device_file_is_rejected(self) -> None:
        self.assert_rejected("dev.tar.gz", tar_bytes([("ok.txt", b"x")], links=[("chr", "dev/null", "")]), "archive_path_unsafe")

    def test_reserved_and_trailing_names_are_rejected(self) -> None:
        self.assert_rejected("entries.zip", zip_bytes({"CON": b"x"}), "archive_path_unsafe")
        self.assert_rejected("entries2.zip", zip_bytes({"file.": b"x"}), "archive_path_unsafe")

    def test_too_many_entries_is_rejected(self) -> None:
        entries = {f"f-{index}.txt": b"x" for index in range(archive.MAX_ENTRIES + 5)}
        self.assert_rejected("many.zip", zip_bytes(entries), "archive_too_many_entries")

    def test_single_file_limit_is_enforced(self) -> None:
        original = archive.MAX_SINGLE_BYTES
        archive.MAX_SINGLE_BYTES = 16
        try:
            self.assert_rejected("big.zip", zip_bytes({"big.txt": b"x" * 64}), "archive_uncompressed_size_exceeded")
        finally:
            archive.MAX_SINGLE_BYTES = original

    def test_ratio_limit_catches_zip_bomb(self) -> None:
        original = archive.MAX_RATIO
        archive.MAX_RATIO = 2.0
        try:
            # 4MB 的零字节压成几 KB：压缩比远超阈值
            self.assert_rejected("bomb.zip", zip_bytes({"zeros.bin": b"\x00" * (4 * 1024 * 1024)}), "archive_ratio_exceeded")
        finally:
            archive.MAX_RATIO = original

    def test_unsupported_formats_are_refused(self) -> None:
        for name in ("archive.7z", "archive.rar", "archive.bz2", "archive.xz"):
            with self.subTest(name=name):
                self.assert_rejected(name, b"not really an archive", "archive_format_unsupported")

    def test_corrupt_zip_is_refused(self) -> None:
        self.assert_rejected("broken.zip", b"PK\x03\x04 not a real zip", "archive_format_unsupported")

    def test_empty_archive_is_refused(self) -> None:
        self.assert_rejected("empty.zip", zip_bytes({}), "archive_empty")


class ExtractionResultTests(ExtractionTestBase):
    def test_zip_extracts_into_a_new_directory(self) -> None:
        payload = zip_bytes({"报告/正文.md": "# 正文".encode("utf-8"), "报告/数据/表.csv": b"a,b\n1,2\n"})
        node = self.upload("材料.zip", payload)
        result = archive.extract_archive(self.store, self.actor, node["id"])
        self.assertEqual(result["node"]["name"], "材料")
        self.assertEqual(result["files"], 2)
        # 目录数按实际结果数：解压出来的根 +「报告」+「数据」（zip 不写显式目录条目，靠路径推）
        self.assertEqual(result["directories"], 3)
        listing = drive.list_children(self.store, self.actor, result["node"]["id"])["nodes"]
        self.assertEqual([item["name"] for item in listing], ["报告"])
        inner = drive.list_children(self.store, self.actor, listing[0]["id"])["nodes"]
        self.assertEqual({item["name"] for item in inner}, {"正文.md", "数据"})

    def test_extracted_directory_name_gets_a_suffix_when_taken(self) -> None:
        payload = zip_bytes({"a.txt": b"x"})
        node = self.upload("同名.zip", payload)
        archive.extract_archive(self.store, self.actor, node["id"])
        second = archive.extract_archive(self.store, self.actor, node["id"])
        self.assertEqual(second["node"]["name"], "同名-2")

    def test_tar_gz_extracts(self) -> None:
        payload = tar_bytes([("code/solve.py", b"print('ok')\n"), ("README.md", "# 说明".encode("utf-8"))])
        node = self.upload("code.tar.gz", payload)
        result = archive.extract_archive(self.store, self.actor, node["id"])
        self.assertEqual(result["node"]["name"], "code")
        listing = drive.list_children(self.store, self.actor, result["node"]["id"])["nodes"]
        self.assertEqual({item["name"] for item in listing}, {"code", "README.md"})

    def test_junk_and_nested_archives_are_skipped_with_a_count(self) -> None:
        payload = zip_bytes(
            {
                "__MACOSX/._x": b"junk",
                ".DS_Store": b"junk",
                "inner.zip": zip_bytes({"deep.txt": b"x"}),
                "real.txt": b"ok",
            }
        )
        node = self.upload("mixed.zip", payload)
        result = archive.extract_archive(self.store, self.actor, node["id"])
        self.assertEqual(result["files"], 1)
        self.assertEqual(result["skipped_junk"], 2)
        self.assertEqual(result["skipped_nested"], ["inner.zip"])

    def test_quota_blocks_the_whole_extraction(self) -> None:
        """配额闸在**整份解压**上：一次过不了就整份拒绝，不会解到一半说空间不够。"""

        node = self.upload("big.zip", zip_bytes({"f.bin": b"x" * 64}))
        used = drive.usage(self.store, self.actor)["used_bytes"]
        nodes_before = self.node_count()  # 含成员根目录与归档本身
        personal_drive.DRIVE_QUOTA_BYTES = used + 10  # 只剩 10 字节，而解压要 64
        try:
            with self.assertRaises(drive.DriveError) as caught:
                archive.extract_archive(self.store, self.actor, node["id"])
        finally:
            personal_drive.DRIVE_QUOTA_BYTES = self.quota
        self.assertEqual(caught.exception.code, "file_quota_exceeded")
        self.assertEqual(self.node_count(), nodes_before, "配额闸拦下时不该建出任何目录")

    def test_mid_write_failure_leaves_nothing_behind(self) -> None:
        """写入阶段中途失败 → 补偿清理：不留半个目录、对象也不留孤儿。"""

        payload = zip_bytes({"a.txt": b"a" * 10, "b.txt": b"b" * 10, "c.txt": b"c" * 10})
        node = self.upload("半途.zip", payload)
        nodes_before = self.node_count()
        objects_before = self.object_count()

        original_put = drive.put_file
        calls = {"count": 0}

        def flaky(store, actor, parent_id, name, content, *args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 3:  # 第三个文件开始失败
                raise drive.DriveError("file_quota_exceeded", "injected")
            return original_put(store, actor, parent_id, name, content, *args, **kwargs)

        drive.put_file = flaky
        try:
            with self.assertRaises(drive.DriveError):
                archive.extract_archive(self.store, self.actor, node["id"])
        finally:
            drive.put_file = original_put

        self.assertEqual(self.node_count(), nodes_before, "失败的解压不该留下节点")
        self.assertEqual(self.object_count(), objects_before, "失败的解压不该留下对象")

    def test_extraction_is_audited(self) -> None:
        node = self.upload("审计.zip", zip_bytes({"a.txt": b"x"}))
        result = archive.extract_archive(self.store, self.actor, node["id"])
        events = drive.audit_log(self.store, self.actor, result["node"]["id"])
        self.assertIn("extract", {item["action"] for item in events})


class ExtractionHttpTests(ExtractionTestBase):
    def test_extract_over_http(self) -> None:
        node = self.upload("包.zip", zip_bytes({"doc/readme.md": b"# hi"}))
        response = self.client.post("/api/drive/extractions", json={"node_id": node["id"]})
        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["node"]["name"], "包")
        self.assertEqual(payload["files"], 1)

    def test_attack_over_http_is_400_and_writes_nothing(self) -> None:
        node = self.upload("evil.zip", zip_bytes({"../evil.txt": b"x"}))
        nodes_before = self.node_count()
        response = self.client.post("/api/drive/extractions", json={"node_id": node["id"]})
        self.assertEqual(response.status_code, 400)
        self.assertIn("archive_path_unsafe", response.json()["detail"])
        self.assertEqual(self.node_count(), nodes_before)

    def test_ratio_attack_over_http_is_413(self) -> None:
        original = archive.MAX_RATIO
        archive.MAX_RATIO = 2.0
        try:
            node = self.upload("bomb.zip", zip_bytes({"zeros.bin": b"\x00" * (1024 * 1024)}))
            response = self.client.post("/api/drive/extractions", json={"node_id": node["id"]})
        finally:
            archive.MAX_RATIO = original
        self.assertEqual(response.status_code, 413)
        self.assertIn("archive_ratio_exceeded", response.json()["detail"])

    def test_required_mode_blocks_extraction(self) -> None:
        import os

        node = self.upload("x.zip", zip_bytes({"a.txt": b"x"}))
        original = os.environ.get("PLATFORM_AUTH_MODE")
        os.environ["PLATFORM_AUTH_MODE"] = "required"
        try:
            client = TestClient(main.app)
            response = client.post("/api/drive/extractions", json={"node_id": node["id"]})
        finally:
            if original is None:
                os.environ.pop("PLATFORM_AUTH_MODE", None)
            else:
                os.environ["PLATFORM_AUTH_MODE"] = original
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()