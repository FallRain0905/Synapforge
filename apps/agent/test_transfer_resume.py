"""FM-6 内核侧断点续传测试：分片上传、每片重试、续传只补缺的。

要证明的性质：

1. 小文件仍走一次 PUT（省一次往返）；
2. 大文件分片；**某片失败会自动重试**，重试成功则整体成功；
3. **续传**：服务端已收到且哈希一致的片不重传，只补缺的；
4. 片一直传不上去 → 明确失败（不假装成功、不静默丢一片）；
5. 收口失败（服务端说缺片/哈希不符）要如实报出来。
"""

from __future__ import annotations

import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from file_worker import FileWorkerError, upload_transfer_content  # noqa: E402


class FakeTransferServer:
    """假的传输服务端：记账分片、可按片注入失败（模拟网络抖动）。"""

    def __init__(self, *, fail_parts: dict[int, int] | None = None, preloaded: dict[int, bytes] | None = None, part_size: int = 0) -> None:
        self.parts: dict[int, bytes] = dict(preloaded or {})
        self.fail_parts = dict(fail_parts or {})  # part_number -> 还要失败几次
        self.attempts: dict[int, int] = {}
        self.calls: list[str] = []
        self.completed = False
        self.part_size = part_size or (len(next(iter(self.parts.values()))) if self.parts else 4)

    def __call__(self, method: str, url: str, *, headers: dict[str, str], body: bytes | None = None, timeout: float = 60.0):
        path = "/api" + url.split("/api", 1)[1] if "/api" in url else url
        self.calls.append(f"{method} {path}")
        if method == "GET" and path.endswith("/parts"):
            return 200, {
                "part_size_bytes": self.part_size,
                "parts": [{"part_number": number, "content_hash": hashlib.sha256(content).hexdigest()} for number, content in sorted(self.parts.items())],
            }
        if method == "PUT" and path.endswith("/content"):
            self.parts[1] = body or b""
            self.completed = True
            return 200, {"transfer": {"id": "t1"}}
        if method == "PUT" and "/parts/" in path:
            number = int(path.rsplit("/", 1)[1])
            self.attempts[number] = self.attempts.get(number, 0) + 1
            if self.fail_parts.get(number, 0) > 0:
                self.fail_parts[number] -= 1
                return 500, {"detail": "flaky_network"}
            self.parts[number] = body or b""
            return 200, {"part": {"part_number": number}}
        if method == "POST" and path.endswith("/complete"):
            numbers = sorted(self.parts)
            if numbers != list(range(1, len(numbers) + 1)):
                return 400, {"detail": "workspace_transfer_parts_missing"}
            self.completed = True
            return 200, {"transfer": {"id": "t1", "status": "ready"}}
        return 404, {"detail": "unexpected"}


class ChunkedUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.content = bytes(range(256)) * 64  # 16KB，够分 4 片
        self.headers = {"X-Agent-Id": "agent-1"}

    def upload(self, server: FakeTransferServer, *, chunk_bytes: int = 4096, retries: int = 3):
        return upload_transfer_content(
            server,
            url="http://fake",
            headers=self.headers,
            transfer_id="t1",
            content=self.content,
            chunk_bytes=chunk_bytes,
            retries=retries,
            log=lambda _message: None,
        )

    def test_small_content_uses_one_put(self) -> None:
        server = FakeTransferServer()
        result = self.upload(server, chunk_bytes=1024 * 1024)
        self.assertEqual(result["mode"], "single")
        self.assertEqual(server.parts[1], self.content)
        self.assertEqual([call for call in server.calls if "/parts" in call], [])

    def test_large_content_is_chunked_and_reassembled(self) -> None:
        server = FakeTransferServer(part_size=4096)
        result = self.upload(server)
        self.assertEqual(result["mode"], "chunked")
        self.assertEqual(result["parts"], 4)
        self.assertEqual(result["uploaded_parts"], [1, 2, 3, 4])
        self.assertTrue(server.completed)
        joined = b"".join(server.parts[number] for number in sorted(server.parts))
        self.assertEqual(joined, self.content, "按片拼回来必须逐字节一致")
        self.assertEqual(result["sha256"], hashlib.sha256(self.content).hexdigest())

    def test_a_flaky_part_is_retried(self) -> None:
        server = FakeTransferServer(part_size=4096, fail_parts={2: 2})  # 第 2 片先失败两次
        result = self.upload(server)
        self.assertEqual(result["uploaded_parts"], [1, 2, 3, 4])
        self.assertEqual(server.attempts[2], 3, "失败两次后第三次应当成功")
        self.assertTrue(server.completed)

    def test_resume_only_uploads_the_missing_parts(self) -> None:
        # 服务端已经有第 1、3 片（内容一致），只该补 2、4
        parts = [self.content[offset : offset + 4096] for offset in range(0, len(self.content), 4096)]
        server = FakeTransferServer(part_size=4096, preloaded={1: parts[0], 3: parts[2]})
        result = self.upload(server)
        self.assertEqual(result["resumed_parts"], [1, 3])
        self.assertEqual(sorted(result["uploaded_parts"]), [2, 4])
        self.assertEqual(server.attempts, {2: 1, 4: 1}, "已收到且一致的片不该重传")

    def test_changed_part_is_reuploaded_even_if_number_exists(self) -> None:
        # 服务端有一片但内容与本地不符（上一轮传坏了）：必须重传
        server = FakeTransferServer(part_size=4096, preloaded={1: b"garbage", 2: self.content[4096:8192]})
        result = self.upload(server)
        self.assertEqual(result["resumed_parts"], [2])
        self.assertIn(1, result["uploaded_parts"])
        self.assertEqual(server.parts[1], self.content[:4096])

    def test_persistent_failure_raises_instead_of_faking_success(self) -> None:
        server = FakeTransferServer(part_size=4096, fail_parts={3: 99})
        with self.assertRaises(FileWorkerError) as caught:
            self.upload(server, retries=2)
        self.assertEqual(caught.exception.code, "workspace_transfer_part_failed")
        self.assertIn("part=3", caught.exception.detail)
        self.assertFalse(server.completed, "有一片没传上去就不能收口")

    def test_complete_failure_is_reported(self) -> None:
        class BadComplete(FakeTransferServer):
            def __call__(self, method, url, *, headers, body=None, timeout=60.0):
                if method == "POST" and url.endswith("/complete"):
                    self.calls.append("POST complete")
                    return 400, {"detail": "workspace_transfer_hash_mismatch:abc!=def"}
                return super().__call__(method, url, headers=headers, body=body, timeout=timeout)

        with self.assertRaises(FileWorkerError) as caught:
            self.upload(BadComplete(part_size=4096))
        self.assertEqual(caught.exception.code, "workspace_transfer_complete_failed")
        self.assertIn("hash_mismatch", caught.exception.detail)


if __name__ == "__main__":
    unittest.main()