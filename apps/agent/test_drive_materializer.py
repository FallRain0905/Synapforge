"""FM-5 内核侧测试：被授权云盘文件物化到 `inputs/`。

要证明的性质：
1. **哈希/大小对账**：平台声明的 sha256 与拿到的字节不一致 → 拒收（不落半个文件）；
2. **失败如实记账**：下载失败/被撤销的文件进 `failures`，`inputs-manifest.json` 里能看到，
   而且**不会**在 `entries` 里出现（不假装拿到了）；
3. **不覆盖已有输入**：同名文件加序号（平台下发的输入可能和别的来源重名）；
4. **lease 与撤销**：换不到 lease 直接失败；下载被拒（403 scope/revoked）如实标错误码。
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from drive_materializer import DriveMaterializeConfig, DriveMaterializeError, DriveMaterializer  # noqa: E402
from input_fetcher import manifest_path, read_inputs_manifest  # noqa: E402


class FakePlatform:
    """假平台：按 node_id 给内容，可注入 403/断网/错误哈希。"""

    def __init__(self, files: dict[str, bytes], *, lease_token: str = "lease_test", deny: set[str] | None = None, wrong_hash: set[str] | None = None) -> None:
        self.files = files
        self.lease_token = lease_token
        self.deny = deny or set()
        self.wrong_hash = wrong_hash or set()
        self.calls: list[str] = []

    def __call__(self, method: str, url: str, *, headers: dict[str, str], body: bytes | None = None, timeout: float = 60.0):
        path = "/api" + url.split("/api", 1)[1] if "/api" in url else url
        self.calls.append(f"{method} {path}")
        if path == "/api/agent/file-leases/exchange":
            return 200, {"lease": {"token": self.lease_token, "grant_id": "g-1", "expires_at": "2030-01-01T00:00:00+00:00"}}
        if path == "/api/agent/drive/materialize":
            entries = [
                {
                    "node_id": node_id,
                    "name": name.decode("utf-8") if isinstance(name, bytes) else str(name),
                    "size_bytes": len(content),
                    "content_hash": ("0" * 64) if node_id in self.wrong_hash else hashlib.sha256(content).hexdigest(),
                    "revision": 1,
                    "source": "drive_grant",
                }
                for node_id, (name, content) in self.files.items()
            ]
            return 200, {"grant_id": "g-1", "expires_at": "2030-01-01T00:00:00+00:00", "entries": entries}
        if "/api/agent/drive/nodes/" in path and path.endswith("/content"):
            node_id = path.split("/api/agent/drive/nodes/")[1].split("/")[0]
            if node_id in self.deny:
                return 403, {"detail": "file_access_scope_denied:" + node_id}
            content = self.files.get(node_id)
            if content is None:
                return 404, {"detail": "file_node_not_found"}
            return 200, content[1]
        return 200, {}


class DriveMaterializeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.workspace = Path(self.temp.name) / "ws"
        self.workspace.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def materializer(self, platform: FakePlatform) -> DriveMaterializer:
        return DriveMaterializer(
            DriveMaterializeConfig(
                url="http://fake",
                project_token="pt",
                agent_id="agent-1",
                project_id="project-1",
                grant_id="g-1",
                workspace=self.workspace,
            ),
            http=platform,
            log=lambda _message: None,
        )

    def test_materializes_with_hash_check_and_manifest(self) -> None:
        content = "第一问的数据".encode("utf-8")
        platform = FakePlatform({"node-1": ("数据.csv", content)})
        result = self.materializer(platform).materialize()
        self.assertEqual(result["written"], ["数据.csv"])
        self.assertEqual(result["failed"], [])
        landed = self.workspace / "inputs" / "数据.csv"
        self.assertEqual(landed.read_bytes(), content)
        manifest = read_inputs_manifest(self.workspace)
        entry = manifest["entries"][0]
        self.assertEqual(entry["artifact_id"], "node-1")
        self.assertEqual(entry["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(entry["source"], "drive_grant")
        # 不留半成品临时文件
        self.assertFalse(list((self.workspace / "inputs").glob("*.part")))

    def test_wrong_hash_is_rejected_and_reported(self) -> None:
        platform = FakePlatform({"node-bad": ("坏文件.csv", b"content")}, wrong_hash={"node-bad"})
        result = self.materializer(platform).materialize()
        self.assertEqual(result["written"], [])
        self.assertEqual(len(result["failed"]), 1)
        self.assertIn("hash_mismatch", result["failed"][0])
        self.assertFalse((self.workspace / "inputs" / "坏文件.csv").exists())
        manifest = read_inputs_manifest(self.workspace)
        self.assertEqual(manifest["entries"], [])
        self.assertIn("hash_mismatch", manifest["failures"][0])

    def test_denied_file_is_reported_not_faked(self) -> None:
        platform = FakePlatform(
            {"node-ok": ("好的.txt", b"ok"), "node-denied": ("越权的.txt", b"secret")},
            deny={"node-denied"},
        )
        result = self.materializer(platform).materialize()
        self.assertEqual(result["written"], ["好的.txt"])
        self.assertEqual(len(result["failed"]), 1)
        self.assertIn("scope_denied", result["failed"][0])
        self.assertFalse((self.workspace / "inputs" / "越权的.txt").exists())

    def test_existing_input_is_not_overwritten(self) -> None:
        (self.workspace / "inputs").mkdir(parents=True)
        (self.workspace / "inputs" / "同名.csv").write_text("旧内容", encoding="utf-8")
        platform = FakePlatform({"node-1": ("同名.csv", b"new content")})
        result = self.materializer(platform).materialize()
        self.assertEqual(result["written"], ["同名-2.csv"])
        self.assertEqual((self.workspace / "inputs" / "同名.csv").read_text(encoding="utf-8"), "旧内容")

    def test_lease_exchange_failure_raises(self) -> None:
        class NoLease(FakePlatform):
            def __call__(self, method: str, url: str, *, headers: dict[str, str], body: bytes | None = None, timeout: float = 60.0):
                if "/file-leases/exchange" in url:
                    return 403, {"detail": "file_access_grant_revoked"}
                return super().__call__(method, url, headers=headers, body=body, timeout=timeout)

        with self.assertRaises(DriveMaterializeError) as caught:
            self.materializer(NoLease({})).materialize()
        self.assertEqual(caught.exception.code, "drive_lease_exchange_failed")

    def test_second_run_keeps_previous_entries(self) -> None:
        first = FakePlatform({"node-1": ("a.txt", b"a")})
        self.materializer(first).materialize()
        second = FakePlatform({"node-2": ("b.txt", b"b")})
        self.materializer(second).materialize()
        manifest = read_inputs_manifest(self.workspace)
        self.assertEqual({entry["name"] for entry in manifest["entries"]}, {"a.txt", "b.txt"})


if __name__ == "__main__":
    unittest.main()