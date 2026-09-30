"""产物溯源 receipt（W2.4，docs/RECEIPT_FORMAT.md）契约测试。

口径：
- B（agentd）上传时随 ArtifactCreate.receipt 提交；平台校验结构后落库溯源列，
  并在 project.artifact.uploaded 事件的 payload.receipt 里原样携带；
- 哈希必须是 SHA-256 前 16 位小写 hex（结构不符 → 422，结构性问题在契约层拒）；
- 未知 receipt_version → **软拒收**：artifact 照常收下但不带溯源，拒收原因进事件
  （不静默丢弃）；
- production-path 节点带 receipt 展示位（B 期三预留的槽）；人工上传没有 receipt 是常态。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient

from app import main
from app.contracts import ArtifactCreate
from app.store import Store

MEMBER = "member-001"
VALID_HASH = "0123456789abcdef"


class ArtifactReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.client = TestClient(main.app)
        self.project = self.store.list_projects()[0]

    def tearDown(self) -> None:
        main.store = self.previous_store
        self.client.close()  # Windows：不先关连接，tempdir 清理会撞文件锁
        self.store.close()
        self.temp_dir.cleanup()

    def upload_events(self, artifact_id: str) -> list[dict]:
        rows = self.store.db.execute(
            "SELECT payload FROM events WHERE project_id = ? AND event_type = 'project.artifact.uploaded' ORDER BY sequence",
            (str(self.project.id),),
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows if json.loads(row["payload"])["artifact_id"] == artifact_id]

    # ---- 正路径 ----------------------------------------------------------

    def test_upload_with_receipt_persists_and_event_carries_it(self) -> None:
        response = self.client.post(
            f"/api/projects/{self.project.id}/artifacts",
            json={
                "name": "figure-1.png",
                "artifact_type": "figure",
                "receipt": {
                    "receipt_version": 1,
                    "tool_name": "workspace_diff",
                    "tool_call_id": "turn-abc",
                    "args_hash": VALID_HASH,
                    "output_hash": "fedcba9876543210",
                    "output_bytes": 2048,
                    "truncated": False,
                },
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        artifact_id = response.json()["id"]
        stored = self.store.get_artifact(UUID(artifact_id))
        self.assertIsNotNone(stored.receipt)
        self.assertEqual(stored.receipt.tool_name, "workspace_diff")
        self.assertEqual(stored.receipt.output_hash, "fedcba9876543210")
        self.assertEqual(stored.receipt.output_bytes, 2048)
        event_payloads = self.upload_events(artifact_id)
        self.assertEqual(len(event_payloads), 1)
        self.assertEqual(event_payloads[0]["receipt"]["tool_call_id"], "turn-abc")
        self.assertNotIn("receipt_rejected", event_payloads[0])

    def test_upload_without_receipt_is_unchanged(self) -> None:
        """人工上传没有 receipt：历史行为零变化（receipt_version=0、事件不带 receipt 键）。"""

        response = self.client.post(
            f"/api/projects/{self.project.id}/artifacts", json={"name": "manual.txt", "artifact_type": "figure"}
        )
        self.assertEqual(response.status_code, 201, response.text)
        artifact_id = response.json()["id"]
        stored = self.store.get_artifact(UUID(artifact_id))
        self.assertIsNone(stored.receipt)
        payloads = self.upload_events(artifact_id)
        self.assertEqual(len(payloads), 1)
        self.assertNotIn("receipt", payloads[0])

    def test_production_path_node_carries_receipt(self) -> None:
        artifact = self.store.create_artifact(
            self.project.id,
            ArtifactCreate(
                name="result.csv",
                artifact_type="result_table",
                receipt={
                    "receipt_version": 1,
                    "tool_name": "workspace_diff",
                    "tool_call_id": "turn-xyz",
                    "args_hash": VALID_HASH,
                    "output_hash": VALID_HASH,
                    "output_bytes": 10,
                },
            ),
            created_by=MEMBER,
        )
        response = self.client.get(f"/api/projects/{self.project.id}/production-path")
        self.assertEqual(response.status_code, 200, response.text)
        node = next(item for item in response.json()["nodes"] if item["artifact_id"] == str(artifact.id))
        self.assertEqual(node["receipt"]["tool_name"], "workspace_diff")
        self.assertEqual(node["receipt"]["output_hash"], VALID_HASH)

    # ---- 边界 ------------------------------------------------------------

    def test_malformed_hash_rejected_by_contract(self) -> None:
        """哈希结构不符（不是 16 位小写 hex）→ 422：结构性问题在契约层就拒。"""

        response = self.client.post(
            f"/api/projects/{self.project.id}/artifacts",
            json={
                "name": "bad-hash.png",
                "artifact_type": "figure",
                "receipt": {"tool_name": "write", "args_hash": "NOTAHASH", "output_hash": VALID_HASH},
            },
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_unknown_receipt_version_soft_rejected(self) -> None:
        """未知 receipt_version：artifact 照常收下但不带溯源，拒收原因进事件（不静默丢弃）。"""

        response = self.client.post(
            f"/api/projects/{self.project.id}/artifacts",
            json={
                "name": "future-version.bin",
                "artifact_type": "result_table",
                "receipt": {
                    "receipt_version": 99,
                    "tool_name": "write",
                    "args_hash": VALID_HASH,
                    "output_hash": VALID_HASH,
                },
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        artifact_id = response.json()["id"]
        stored = self.store.get_artifact(UUID(artifact_id))
        self.assertIsNone(stored.receipt)  # 不带溯源
        payloads = self.upload_events(artifact_id)
        self.assertEqual(payloads[0].get("receipt_rejected"), "receipt_version_unknown:99")
        self.assertNotIn("receipt", payloads[0])


if __name__ == "__main__":
    unittest.main()
