"""S3/MinIO 对象存储路径的契约测试（用注入的假客户端，不需要真 MinIO）。

为什么值得单独测：生产要切 S3/MinIO 时，"云盘/成果物/传输"三条链路都要能在 S3 上跑。
本机没有 Docker（跑不了真 MinIO），所以这里验证的是**协议形状**——调用哪个 API、带哪些元数据、
分片上传怎么走、删对象删的是什么键。真 MinIO 的端到端验证仍需要基础设施（见 FM-6 交接的偏差）。
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from app import drive
from app.object_store import S3ObjectStore, create_object_store
from app.store import Store


class FakeS3Client:
    """最小假客户端：只实现 S3ObjectStore 用到的几个调用，并记录调用形状。"""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.calls: list[tuple[str, dict]] = []
        self.metadata: dict[str, dict] = {}

    def put_object(self, **kwargs):
        self.calls.append(("put_object", kwargs))
        self.objects[kwargs["Key"]] = kwargs["Body"]
        self.metadata[kwargs["Key"]] = kwargs.get("Metadata", {})
        return {}

    def get_object(self, **kwargs):
        self.calls.append(("get_object", kwargs))
        key = kwargs["Key"]
        if key not in self.objects:
            raise KeyError("NoSuchKey")
        payload = self.objects[key]

        class Body:
            def read(inner_self) -> bytes:
                return payload

        return {"Body": Body()}

    def delete_object(self, **kwargs):
        self.calls.append(("delete_object", kwargs))
        self.objects.pop(kwargs["Key"], None)
        return {}

    def copy_object(self, **kwargs):
        self.calls.append(("copy_object", kwargs))
        return {}

    def create_multipart_upload(self, **kwargs):
        self.calls.append(("create_multipart_upload", kwargs))
        return {"UploadId": "upload-1"}

    def upload_part(self, **kwargs):
        self.calls.append(("upload_part", kwargs))
        return {"ETag": f"etag-{kwargs['PartNumber']}"}

    def complete_multipart_upload(self, **kwargs):
        self.calls.append(("complete_multipart_upload", kwargs))
        self.objects[kwargs["Key"]] = b"multipart-content"
        return {}

    def abort_multipart_upload(self, **kwargs):
        self.calls.append(("abort_multipart_upload", kwargs))
        return {}

    def list_parts(self, **kwargs):
        self.calls.append(("list_parts", kwargs))
        return {"Parts": [{"PartNumber": 1, "ETag": "etag-1"}]}


class S3ObjectStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeS3Client()
        self.store = S3ObjectStore("platform-bucket", client=self.client)

    def test_put_bytes_records_sha256_metadata(self) -> None:
        content = "内容".encode("utf-8")
        stored = self.store.put_bytes("drive/member/x.bin", content, "application/octet-stream")
        self.assertEqual(stored.key, "drive/member/x.bin")
        self.assertEqual(stored.content_hash, hashlib.sha256(content).hexdigest())
        self.assertEqual(stored.size_bytes, len(content))
        put = next(call for call in self.client.calls if call[0] == "put_object")
        self.assertEqual(put[1]["Bucket"], "platform-bucket")
        self.assertEqual(put[1]["Metadata"]["sha256"], stored.content_hash)

    def test_get_and_delete(self) -> None:
        self.store.put_bytes("k", b"payload")
        self.assertEqual(self.store.get_bytes("k"), b"payload")
        self.store.delete("k")
        self.assertNotIn("k", self.client.objects)
        self.assertTrue(any(call[0] == "delete_object" for call in self.client.calls))

    def test_multipart_upload_flow(self) -> None:
        upload_id = self.store.initiate_multipart("big.bin", "application/octet-stream")
        etag = self.store.upload_part(upload_id, 1, b"part-one")
        self.assertEqual(etag, "etag-1")
        completed = self.store.complete_multipart(upload_id)
        self.assertEqual(completed.key, "big.bin")
        kinds = [call[0] for call in self.client.calls]
        # 分片上传的真实形状：建会话 → 传分片 → 列分片 → 完成 → 回读校验 → 改回正确的 ContentType
        self.assertEqual(
            kinds,
            ["create_multipart_upload", "upload_part", "list_parts", "complete_multipart_upload", "get_object", "copy_object"],
        )
        self.store.abort_multipart(upload_id, key="big.bin")
        self.assertGreaterEqual(len([call for call in self.client.calls if call[0] == "abort_multipart_upload"]), 1)

    def test_factory_builds_s3_backend_from_settings(self) -> None:
        store = create_object_store(
            Path(tempfile.mkdtemp()) / "objects",
            environ={"OBJECT_STORE_BACKEND": "s3", "S3_BUCKET": "bucket-x", "S3_REGION": "cn-hangzhou"},
        )
        self.assertIsInstance(store, S3ObjectStore)
        self.assertEqual(store.bucket, "bucket-x")
        with self.assertRaises(RuntimeError):
            create_object_store(Path(tempfile.mkdtemp()), environ={"OBJECT_STORE_BACKEND": "s3"})


class DriveOnS3Tests(unittest.TestCase):
    """云盘服务在 S3 后端上必须照样跑：它只依赖 `put_bytes/get_bytes/delete` 这三个调用。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.client = FakeS3Client()
        self.store = Store(Path(self.temp.name) / "platform.db", object_store=S3ObjectStore("bucket", client=self.client))
        drive.ensure_schema(self.store)
        self.actor = drive.actor_for(self.store, "member-001")

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_upload_download_and_purge_on_s3(self) -> None:
        node = drive.put_file(self.store, self.actor, None, "s3.txt", b"hello s3", "text/plain")
        self.assertEqual(len(self.client.objects), 1)
        _row, content = drive.read_content(self.store, self.actor, node["id"])
        self.assertEqual(content, b"hello s3")
        drive.trash(self.store, self.actor, node["id"])
        result = drive.purge(self.store, self.actor, node["id"])
        self.assertEqual(result["objects_deleted"], 1)
        self.assertEqual(self.client.objects, {}, "对象确实从 S3 删掉了")
        self.assertEqual(drive.usage(self.store, self.actor)["used_bytes"], 0)


if __name__ == "__main__":
    unittest.main()