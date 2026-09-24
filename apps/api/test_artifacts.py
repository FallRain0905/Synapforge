from __future__ import annotations

import tempfile
import unittest
import hashlib
import io
from pathlib import Path

from app.contracts import ArtifactCreate, ReviewCreate
from app.object_store import LocalObjectStore, S3ObjectStore, create_object_store
from app.store import Store


class ArtifactStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.project = self.store.list_projects()[0]

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_artifact_content_hash_version_and_immutability(self) -> None:
        artifact = self.store.create_artifact(
            self.project.id,
            ArtifactCreate(name="versioned-result.json", artifact_type="result_table", mime_type="application/json"),
        )
        stored = self.store.store_artifact_content(artifact.id, b'{"value": 1}', expected_hash=None)
        self.assertEqual(stored.size_bytes, 12)
        self.assertEqual(len(stored.content_hash), 64)
        self.assertEqual(self.store.get_artifact_content(artifact.id), b'{"value": 1}')

        self.store.create_review(
            self.project.id,
            ReviewCreate(target_type="artifact", target_id=artifact.id, verdict="APPROVED", summary="确认结果", reviewer="member-001", reviewer_kind="member"),
        )
        approved = next(item for item in self.store.list_artifacts(self.project.id) if item.id == artifact.id)
        self.assertTrue(approved.immutable)
        self.assertTrue(approved.downstream_allowed)
        with self.assertRaises(ValueError):
            self.store.store_artifact_content(artifact.id, b'{"value": 2}')

        version = self.store.create_artifact_version(
            self.project.id,
            artifact.id,
            ArtifactCreate(name="versioned-result.json", artifact_type="result_table"),
        )
        self.assertEqual(version.version, 2)
        self.assertEqual(version.parent_artifact_id, artifact.id)
        archived = self.store.archive_artifact(artifact.id)
        self.assertEqual(archived.status, "ARCHIVED")
        self.assertFalse(archived.downstream_allowed)
        self.assertEqual(self.store.archive_artifact(artifact.id).status, "ARCHIVED")

    def test_local_object_store_multipart_is_resumable(self) -> None:
        root = Path(self.temp_dir.name) / "objects"
        objects = LocalObjectStore(root)
        upload_id = objects.initiate_multipart("projects/p1/large.bin", "application/octet-stream")
        objects.upload_part(upload_id, 2, b"world")
        objects.upload_part(upload_id, 1, b"hello ")
        completed = objects.complete_multipart(upload_id)
        self.assertEqual(completed.size_bytes, 11)
        self.assertEqual(objects.get_bytes("projects/p1/large.bin"), b"hello world")

    def test_local_multipart_rejects_missing_part(self) -> None:
        objects = LocalObjectStore(Path(self.temp_dir.name) / "incomplete-objects")
        upload_id = objects.initiate_multipart("projects/p1/incomplete.bin")
        objects.upload_part(upload_id, 2, b"world")
        with self.assertRaisesRegex(ValueError, "multipart_parts_incomplete"):
            objects.complete_multipart(upload_id)

    def test_artifact_version_is_not_collapsed_by_content_hash_deduplication(self) -> None:
        content_hash = hashlib.sha256(b"same-content").hexdigest()
        first = self.store.create_artifact(self.project.id, ArtifactCreate(name="versioned-same.json", artifact_type="result_table", content_hash=content_hash))
        second = self.store.create_artifact_version(self.project.id, first.id, ArtifactCreate(name="versioned-same.json", artifact_type="result_table", content_hash=content_hash))
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(second.version, 2)
        self.assertEqual(second.parent_artifact_id, first.id)

    def test_store_multipart_is_bound_to_artifact_and_updates_metadata(self) -> None:
        artifact = self.store.create_artifact(self.project.id, ArtifactCreate(name="multipart.json", artifact_type="result_table", mime_type="application/json"))
        upload = self.store.initiate_artifact_multipart(artifact.id, "application/json")
        self.store.upload_artifact_part(artifact.id, upload["upload_id"], 2, b"world")
        self.store.upload_artifact_part(artifact.id, upload["upload_id"], 1, b"hello ")
        completed = self.store.complete_artifact_multipart(artifact.id, upload["upload_id"])
        self.assertEqual(completed.size_bytes, 11)
        self.assertEqual(completed.mime_type, "application/json")
        self.assertEqual(self.store.get_artifact_content(artifact.id), b"hello world")
        retried = self.store.complete_artifact_multipart(artifact.id, upload["upload_id"])
        self.assertEqual(retried.id, artifact.id)
        with self.assertRaises(KeyError):
            self.store.upload_artifact_part(artifact.id, upload["upload_id"], 1, b"again")

    def test_stale_multipart_upload_can_be_cleaned(self) -> None:
        artifact = self.store.create_artifact(self.project.id, ArtifactCreate(name="stale.json", artifact_type="result_table"))
        upload = self.store.initiate_artifact_multipart(artifact.id)
        self.store.db.execute("UPDATE artifact_multipart_uploads SET created_at = ? WHERE upload_id = ?", ("2000-01-01T00:00:00+00:00", upload["upload_id"]))
        self.store.db.commit()
        self.assertEqual(self.store.cleanup_stale_artifact_multipart_uploads(), 1)
        with self.assertRaises(KeyError):
            self.store.abort_artifact_multipart(artifact.id, upload["upload_id"])

    def test_content_hash_deduplicates_non_archived_artifact_creation(self) -> None:
        content_hash = hashlib.sha256(b"same-content").hexdigest()
        baseline = len(self.store.list_artifacts(self.project.id))
        first = self.store.create_artifact(self.project.id, ArtifactCreate(name="duplicate.json", artifact_type="result_table", content_hash=content_hash))
        second = self.store.create_artifact(self.project.id, ArtifactCreate(name="duplicate.json", artifact_type="result_table", content_hash=content_hash))
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.store.list_artifacts(self.project.id)), baseline + 1)

    def test_read_detects_corrupted_object_store_content(self) -> None:
        artifact = self.store.create_artifact(self.project.id, ArtifactCreate(name="integrity.json", artifact_type="result_table"))
        stored = self.store.store_artifact_content(artifact.id, b"original")
        object_path = self.store.object_store.root / stored.storage_key
        object_path.write_bytes(b"corrupted")
        with self.assertRaisesRegex(ValueError, "artifact_stored_content_hash_mismatch"):
            self.store.get_artifact_content(artifact.id)
        with self.assertRaisesRegex(ValueError, "artifact_hash_mismatch"):
            self.store.export_project_bundle(self.project.id, self.temp_dir.name + "/corrupt.zip")


class MemoryS3Client:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, object]] = {}
        self.uploads: dict[str, dict[str, object]] = {}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str, Metadata: dict[str, str]) -> None:
        self.objects[Key] = {"body": bytes(Body), "content_type": ContentType, "metadata": Metadata}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        obj = self.objects[Key]
        return {"Body": io.BytesIO(obj["body"]), "ContentType": obj["content_type"]}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        obj = self.objects[Key]
        return {"ContentLength": len(obj["body"]), "ContentType": obj["content_type"], "Metadata": obj["metadata"]}

    def create_multipart_upload(self, *, Bucket: str, Key: str, ContentType: str) -> dict[str, str]:
        upload_id = f"upload-{len(self.uploads) + 1}"
        self.uploads[upload_id] = {"key": Key, "content_type": ContentType, "parts": {}}
        return {"UploadId": upload_id}

    def upload_part(self, *, Bucket: str, Key: str, UploadId: str, PartNumber: int, Body: bytes) -> dict[str, str]:
        upload = self.uploads[UploadId]
        upload["parts"][PartNumber] = bytes(Body)
        return {"ETag": f"etag-{PartNumber}"}

    def list_parts(self, *, Bucket: str, Key: str, UploadId: str) -> dict[str, list[dict[str, object]]]:
        upload = self.uploads[UploadId]
        return {"Parts": [{"PartNumber": number, "ETag": f"etag-{number}"} for number in upload["parts"]]}

    def complete_multipart_upload(self, *, Bucket: str, Key: str, UploadId: str, MultipartUpload: dict[str, object]) -> None:
        upload = self.uploads.pop(UploadId)
        content = b"".join(upload["parts"][part["PartNumber"]] for part in MultipartUpload["Parts"])
        self.objects[Key] = {"body": content, "content_type": upload["content_type"], "metadata": {}}

    def copy_object(self, *, Bucket: str, Key: str, CopySource: dict[str, str], ContentType: str, Metadata: dict[str, str], MetadataDirective: str) -> None:
        obj = self.objects[CopySource["Key"]]
        self.objects[Key] = {"body": obj["body"], "content_type": ContentType, "metadata": Metadata}

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self.objects.pop(Key, None)

    def abort_multipart_upload(self, *, Bucket: str, Key: str, UploadId: str) -> None:
        self.uploads.pop(UploadId, None)


class S3ObjectStoreTests(unittest.TestCase):
    def test_s3_hash_metadata_and_cross_instance_multipart_completion(self) -> None:
        client = MemoryS3Client()
        objects = S3ObjectStore("bucket", client=client)
        stored = objects.put_bytes("projects/p1/result.json", b"hello", "application/json")
        self.assertEqual(objects.head("projects/p1/result.json").content_hash, stored.content_hash)
        upload_id = objects.initiate_multipart("projects/p1/large.bin", "application/octet-stream")
        objects.upload_part(upload_id, 2, b"world")
        objects.upload_part(upload_id, 1, b"hello ")
        restarted = S3ObjectStore("bucket", client=client)
        restarted.upload_part(upload_id, 2, b"world", "projects/p1/large.bin")
        completed = restarted.complete_multipart(upload_id, "projects/p1/large.bin", "application/octet-stream")
        self.assertEqual(completed.content_hash, hashlib.sha256(b"hello world").hexdigest())
        self.assertEqual(restarted.head("projects/p1/large.bin").content_hash, completed.content_hash)


class ObjectStoreFactoryTests(unittest.TestCase):
    def test_factory_keeps_local_default_and_requires_s3_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            local = create_object_store(Path(root) / "factory", environ={})
            self.assertIsInstance(local, LocalObjectStore)
            with self.assertRaisesRegex(RuntimeError, "S3_BUCKET_required"):
                create_object_store(Path(root) / "factory", environ={"OBJECT_STORE_BACKEND": "s3"})


if __name__ == "__main__":
    unittest.main()
