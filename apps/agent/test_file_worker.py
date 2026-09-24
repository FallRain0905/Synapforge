"""FM-3 内核侧测试：路径防护（逃逸样本）与工作区文件 Worker 的行为。

三类要证明的事：

1. **路径逃逸必须被拒**：`..`、绝对路径、盘符、UNC、控制字符、超深、符号链接、Windows junction
   （`os.path.islink()` 认不出 junction，得看 reparse 位——这条单独测）；
2. **危险操作必须被拒**：删工作区根、删 `.git`、删 `.math-agent-platform`、把目录移进自己的子树；
3. **Worker 的失败口径**：执行失败必须回报 `success=false` + 稳定错误码，**绝不**报成功；
   传输缺失、同名冲突、非空目录递归删除都要有明确错误码。
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest
import zipfile
import io
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import file_worker  # noqa: E402
from file_worker import FileWorkerConfig, FileWorkerError, WorkspaceFileWorker, is_protected, safe_path, validate_relative  # noqa: E402


class FakeHttp:
    """假的平台：按 URL 返回预设响应，并记录每一次调用（用来断言"回报了什么"）。"""

    def __init__(self, *, transfers: dict[str, bytes] | None = None, claim: list[dict] | None = None) -> None:
        self.transfers = transfers or {}
        self.claim_payload = claim or []
        self.calls: list[dict] = []
        self.uploaded: dict[str, bytes] = {}

    def __call__(self, method: str, url: str, *, headers: dict[str, str], body: bytes | None = None, timeout: float = 60.0):
        path = "/api" + url.split("/api", 1)[1] if "/api" in url else url
        self.calls.append({"method": method, "path": path, "body": body, "headers": dict(headers)})
        if path.startswith("/api/agent/workspace-operations/claim"):
            return 200, {"operations": self.claim_payload, "large_file_bytes": file_worker.LARGE_FILE_BYTES}
        if path.endswith("/start") or path.endswith("/progress") or path.endswith("/complete"):
            return 200, {"operation": {"id": path.split("/")[-2]}}
        if "/workspace-transfers/" in path and path.endswith("/content"):
            transfer_id = path.split("/workspace-transfers/")[1].split("/")[0]
            if method == "GET":
                content = self.transfers.get(transfer_id)
                if content is None:
                    return 404, {"detail": "workspace_transfer_not_found"}
                return 200, content
            self.uploaded[transfer_id] = body or b""
            return 200, {"transfer": {"id": transfer_id}}
        return 200, {}


class PathSafetyTests(unittest.TestCase):
    def test_syntax_escapes_are_rejected(self) -> None:
        for value in (
            "../escape.txt",
            "a/../../b.txt",
            "/etc/passwd",
            "C:/Windows/system32/evil.dll",
            "//server/share/x.txt",
            "a/\x00b",
            "a/" + "x" * 300 + ".txt",
            "/".join(["d"] * 40),
            "trailing.",
            "a /b.txt",  # 段内尾随空格（整串两端的空白是用户输入习惯，会被裁剪——这里测的是段内）
        ):
            with self.subTest(value=value):
                with self.assertRaises(FileWorkerError) as caught:
                    validate_relative(value, allow_empty=False)
                self.assertIn(caught.exception.code, {"workspace_path_invalid", "workspace_path_outside_root"})

    def test_safe_path_stays_inside_the_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(safe_path(root, "a/b.txt"), root / "a" / "b.txt")
            self.assertEqual(safe_path(root, ""), root.resolve())

    def test_symlinked_directory_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "ws"
            outside = Path(temp) / "outside"
            root.mkdir()
            outside.mkdir()
            (outside / "secret.txt").write_text("secret", encoding="utf-8")
            link = root / "escape"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("这个环境不允许建符号链接（Windows 需要开发者模式）")
            with self.assertRaises(FileWorkerError) as caught:
                safe_path(root, "escape/secret.txt")
            self.assertEqual(caught.exception.code, "workspace_symlink_denied")

    def test_windows_junction_is_refused(self) -> None:
        """Windows 的 junction：`os.path.islink()` 认不出来，必须看 reparse 位。"""

        if os.name != "nt":
            self.skipTest("只有 Windows 有 junction")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "ws"
            target = Path(temp) / "target"
            root.mkdir()
            target.mkdir()
            junction = root / "junction"
            import subprocess

            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
                capture_output=True,
                text=True,
            )
            if created.returncode != 0:
                self.skipTest(f"建不了 junction：{created.stderr or created.stdout}")
            self.assertFalse(os.path.islink(junction))  # junction 不是 symlink：这就是为什么要单测
            with self.assertRaises(FileWorkerError) as caught:
                safe_path(root, "junction/anything.txt")
            self.assertEqual(caught.exception.code, "workspace_symlink_denied")

    def test_protected_paths(self) -> None:
        self.assertTrue(is_protected(""))
        self.assertTrue(is_protected(".git/config"))
        self.assertTrue(is_protected(".math-agent-platform/run-manifests/x.json"))
        self.assertTrue(is_protected("papers", ["papers"]))
        self.assertFalse(is_protected("papers/main.tex", ["papers-other"]))


class WorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name) / "ws"
        self.root.mkdir(parents=True)
        self.http = FakeHttp()
        self.worker = WorkspaceFileWorker(
            FileWorkerConfig(url="http://fake", project_token="t", agent_id="agent-1", workspace=self.root, project_id="p1"),
            http=self.http,
            log=lambda _message: None,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def completion(self) -> dict:
        for call in reversed(self.http.calls):
            if call["path"].endswith("/complete"):
                return json.loads((call["body"] or b"{}").decode("utf-8"))
        raise AssertionError("没有回报 complete")

    def run_operation(self, kind: str, relative: str = "", arguments: dict | None = None) -> None:
        self.worker._run(
            {
                "id": "op-1",
                "operation_type": kind,
                "relative_path": relative,
                "arguments": arguments or {},
            }
        )

    def test_list_reports_the_effective_protected_paths(self) -> None:
        """页面靠这个清单决定"哪些操作不给入口"：必须包含内置默认（`.git` 等）与自报项。"""

        worker = WorkspaceFileWorker(
            FileWorkerConfig(
                url="http://fake",
                project_token="t",
                agent_id="agent-1",
                workspace=self.root,
                protected_paths=["secret-notes"],
            ),
            http=FakeHttp(),
            log=lambda _message: None,
        )
        worker._run({"id": "op-p", "operation_type": "list", "relative_path": "", "arguments": {}})
        protected = worker._op_list("", {})["protected"]
        self.assertIn(".git", protected)
        self.assertIn(".math-agent-platform", protected)
        self.assertIn("secret-notes", protected)

    def test_list_and_stat(self) -> None:
        (self.root / "notes").mkdir()
        (self.root / "notes" / "a.md").write_text("# hi", encoding="utf-8")
        self.run_operation("list")
        payload = self.completion()
        self.assertTrue(payload["success"])
        names = [entry["name"] for entry in payload["result"]["entries"]]
        self.assertEqual(names, ["notes"])

        self.run_operation("stat", "notes/a.md")
        payload = self.completion()
        self.assertEqual(payload["result"]["entry"]["size_bytes"], 4)

    def test_mkdir_and_conflict(self) -> None:
        self.run_operation("mkdir", "deep/nested")
        self.assertTrue(self.completion()["success"])
        self.assertTrue((self.root / "deep" / "nested").is_dir())
        (self.root / "deep" / "nested" / "file.txt").write_text("x", encoding="utf-8")
        (self.root / "conflict.txt").write_text("x", encoding="utf-8")
        self.run_operation("mkdir", "conflict.txt")
        payload = self.completion()
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error_code"], "workspace_path_conflict")

    def test_upload_via_transfer_is_atomic_and_hashed(self) -> None:
        content = "第一问的模型假设".encode("utf-8")
        self.http.transfers["tr-1"] = content
        self.run_operation("upload", "papers/假设.md", {"transfer_id": "tr-1"})
        payload = self.completion()
        self.assertTrue(payload["success"], payload)
        self.assertEqual((self.root / "papers" / "假设.md").read_bytes(), content)
        self.assertEqual(payload["result"]["size_bytes"], len(content))
        # 不留半成品临时文件
        self.assertEqual([p.name for p in (self.root / "papers").iterdir()], ["假设.md"])

    def test_upload_refuses_overwrite_by_default(self) -> None:
        (self.root / "exists.txt").write_text("old", encoding="utf-8")
        self.http.transfers["tr-2"] = b"new"
        self.run_operation("upload", "exists.txt", {"transfer_id": "tr-2"})
        payload = self.completion()
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error_code"], "workspace_path_conflict")
        self.assertEqual((self.root / "exists.txt").read_text(encoding="utf-8"), "old")

    def test_download_pushes_bytes_to_the_transfer(self) -> None:
        (self.root / "out.csv").write_bytes(b"x,y\n1,2\n")
        self.run_operation("download", "out.csv", {"transfer_id": "tr-out"})
        payload = self.completion()
        self.assertTrue(payload["success"], payload)
        self.assertEqual(self.http.uploaded["tr-out"], b"x,y\n1,2\n")
        self.assertEqual(payload["result"]["sha256"], file_worker._sha256(b"x,y\n1,2\n"))

    def test_download_without_transfer_fails_honestly(self) -> None:
        (self.root / "out.csv").write_bytes(b"data")
        self.run_operation("download", "out.csv", {})
        payload = self.completion()
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error_code"], "workspace_transfer_required")

    def test_rename_move_copy(self) -> None:
        (self.root / "a.txt").write_text("a", encoding="utf-8")
        (self.root / "dir").mkdir()
        self.run_operation("rename", "a.txt", {"name": "b.txt"})
        self.assertTrue(self.completion()["success"])
        self.assertTrue((self.root / "b.txt").exists())
        self.run_operation("move", "b.txt", {"destination": "dir"})
        self.assertTrue(self.completion()["success"])
        self.assertTrue((self.root / "dir" / "b.txt").exists())
        self.run_operation("copy", "dir/b.txt", {"destination": "dir", "name": "c.txt"})
        self.assertTrue(self.completion()["success"])
        self.assertEqual((self.root / "dir" / "c.txt").read_text(encoding="utf-8"), "a")

    def test_move_directory_into_its_own_subtree_is_refused(self) -> None:
        (self.root / "outer").mkdir()
        (self.root / "outer" / "inner").mkdir()
        self.run_operation("move", "outer", {"destination": "outer/inner"})
        payload = self.completion()
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error_code"], "workspace_directory_cycle")

    def test_protected_paths_cannot_be_deleted_or_moved(self) -> None:
        (self.root / ".git").mkdir()
        (self.root / ".math-agent-platform").mkdir()
        (self.root / "keep").mkdir()
        for relative in ("", ".git", ".math-agent-platform", "keep"):
            with self.subTest(relative=relative):
                arguments = {"recursive": True} if relative else {}
                self.run_operation("delete", relative, arguments)
                payload = self.completion()
                if relative == "keep":
                    self.assertTrue(payload["success"])  # 普通目录可以删
                    self.assertFalse((self.root / "keep").exists())
                else:
                    self.assertFalse(payload["success"], relative)
                    self.assertEqual(payload["error_code"], "workspace_path_protected")

    def test_delete_directory_requires_recursive(self) -> None:
        (self.root / "some").mkdir()
        (self.root / "some" / "x.txt").write_text("x", encoding="utf-8")
        self.run_operation("delete", "some", {})
        payload = self.completion()
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error_code"], "workspace_path_invalid")

    def test_escape_attempt_is_reported_not_silently_ignored(self) -> None:
        self.run_operation("stat", "../../secret.txt")
        payload = self.completion()
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error_code"], "workspace_path_outside_root")

    def test_extract_zip_into_workspace(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as handle:
            handle.writestr("报告/正文.md", "# 正文".encode("utf-8"))
            handle.writestr("报告/数据/表.csv", b"x,y\n")
        (self.root / "材料.zip").write_bytes(buffer.getvalue())
        self.run_operation("extract", "材料.zip", {"destination": ""})
        payload = self.completion()
        self.assertTrue(payload["success"], payload)
        self.assertEqual(payload["result"]["files"], 2)
        self.assertTrue((self.root / "材料" / "报告" / "正文.md").is_file())
        # 临时目录被清掉（不留 staging）
        temp_dir = self.root / ".math-agent-platform" / "tmp"
        self.assertFalse(temp_dir.exists() and any(temp_dir.iterdir()))

    def test_extract_attack_is_refused_and_leaves_nothing(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as handle:
            handle.writestr("../escape.txt", b"x")
        (self.root / "evil.zip").write_bytes(buffer.getvalue())
        before = sorted(p.name for p in self.root.iterdir())
        self.run_operation("extract", "evil.zip", {})
        payload = self.completion()
        self.assertFalse(payload["success"])
        self.assertEqual(payload["error_code"], "archive_path_unsafe")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), before, "被拒的解压不该留下东西")

    def test_poll_once_claims_and_executes(self) -> None:
        (self.root / "hello.txt").write_text("hi", encoding="utf-8")
        self.http.claim_payload = [
            {"id": "op-a", "operation_type": "stat", "relative_path": "hello.txt", "arguments": {}},
            {"id": "op-b", "operation_type": "delete", "relative_path": "hello.txt", "arguments": {}},
        ]
        count = self.worker.poll_once()
        self.assertEqual(count, 2)
        self.assertEqual(self.worker.stats["succeeded"], 2)
        self.assertFalse((self.root / "hello.txt").exists())
        claim_call = self.http.calls[0]
        self.assertEqual(claim_call["headers"]["X-Agent-Id"], "agent-1")
        self.assertEqual(claim_call["headers"]["X-Project-Id"], "p1")

    def test_claim_failure_is_recorded_not_faked(self) -> None:
        def broken(method: str, url: str, *, headers: dict[str, str], body: bytes | None = None, timeout: float = 60.0):
            return 403, {"detail": "device_capability_denied"}

        worker = WorkspaceFileWorker(
            FileWorkerConfig(url="http://fake", project_token="t", agent_id="agent-1", workspace=self.root),
            http=broken,
            log=lambda _message: None,
        )
        self.assertEqual(worker.poll_once(), 0)
        self.assertIn("claim_failed:403", str(worker.stats["last_error"]))


if __name__ == "__main__":
    unittest.main()