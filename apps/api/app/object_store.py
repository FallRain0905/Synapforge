"""Object storage boundary used by Artifact persistence.

LocalObjectStore is the deterministic development backend. S3ObjectStore uses
the same methods and is activated only when boto3 is installed and configured.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4


@dataclass(frozen=True)
class StoredObject:
    key: str
    size_bytes: int
    content_hash: str
    mime_type: str


class ObjectStore(Protocol):
    def put_bytes(self, key: str, content: bytes, mime_type: str | None = None) -> StoredObject: ...
    def get_bytes(self, key: str) -> bytes: ...
    def head(self, key: str) -> StoredObject: ...
    def delete(self, key: str) -> None: ...
    def initiate_multipart(self, key: str, mime_type: str | None = None) -> str: ...
    def upload_part(self, upload_id: str, part_number: int, content: bytes, key: str | None = None) -> str: ...
    def complete_multipart(self, upload_id: str, key: str | None = None, mime_type: str | None = None) -> StoredObject: ...
    def abort_multipart(self, upload_id: str, key: str | None = None) -> None: ...


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _mime(key: str, supplied: str | None) -> str:
    return supplied or mimetypes.guess_type(key)[0] or "application/octet-stream"


class LocalObjectStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ".multipart").mkdir(exist_ok=True)

    def _path(self, key: str) -> Path:
        normalized = key.replace("\\", "/").lstrip("/")
        path = (self.root / normalized).resolve()
        if self.root not in path.parents and path != self.root:
            raise ValueError("object_key_outside_store")
        return path

    def put_bytes(self, key: str, content: bytes, mime_type: str | None = None) -> StoredObject:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return StoredObject(key=key, size_bytes=len(content), content_hash=_hash(content), mime_type=_mime(key, mime_type))

    def get_bytes(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise KeyError("object_not_found")
        return path.read_bytes()

    def head(self, key: str) -> StoredObject:
        content = self.get_bytes(key)
        return StoredObject(key=key, size_bytes=len(content), content_hash=_hash(content), mime_type=_mime(key, None))

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    def initiate_multipart(self, key: str, mime_type: str | None = None) -> str:
        upload_id = uuid4().hex
        directory = self._path(f".multipart/{upload_id}")
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "manifest.json").write_text(json.dumps({"key": key, "mime_type": mime_type}), encoding="utf-8")
        return upload_id

    def upload_part(self, upload_id: str, part_number: int, content: bytes, key: str | None = None) -> str:
        if part_number < 1:
            raise ValueError("part_number_must_be_positive")
        path = self._path(f".multipart/{upload_id}/{part_number:012d}.part")
        if not path.parent.is_dir():
            raise KeyError("multipart_upload_not_found")
        path.write_bytes(content)
        return _hash(content)

    def complete_multipart(self, upload_id: str, key: str | None = None, mime_type: str | None = None) -> StoredObject:
        directory = self._path(f".multipart/{upload_id}")
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            raise KeyError("multipart_upload_not_found")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        parts = sorted(directory.glob("*.part"))
        if not parts:
            raise ValueError("multipart_upload_has_no_parts")
        part_numbers = [int(part.stem) for part in parts]
        if part_numbers != list(range(1, part_numbers[-1] + 1)):
            raise ValueError("multipart_parts_incomplete")
        content = b"".join(part.read_bytes() for part in parts)
        result = self.put_bytes(manifest["key"], content, manifest.get("mime_type"))
        shutil.rmtree(directory)
        return result

    def abort_multipart(self, upload_id: str, key: str | None = None) -> None:
        directory = self._path(f".multipart/{upload_id}")
        if directory.exists():
            shutil.rmtree(directory)


class S3ObjectStore:
    def __init__(self, bucket: str, *, endpoint_url: str | None = None, region_name: str | None = None, client: object | None = None) -> None:
        if client is None:
            try:
                import boto3
            except ImportError as error:  # pragma: no cover - optional production dependency
                raise RuntimeError("boto3_required_for_s3_object_store") from error
            client = boto3.client("s3", endpoint_url=endpoint_url, region_name=region_name)
        self.client = client
        self.bucket = bucket
        self._uploads: dict[str, tuple[str, str | None]] = {}

    def put_bytes(self, key: str, content: bytes, mime_type: str | None = None) -> StoredObject:
        resolved_mime = _mime(key, mime_type)
        content_hash = _hash(content)
        self.client.put_object(Bucket=self.bucket, Key=key, Body=content, ContentType=resolved_mime, Metadata={"sha256": content_hash})
        return StoredObject(key=key, size_bytes=len(content), content_hash=content_hash, mime_type=resolved_mime)

    def get_bytes(self, key: str) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except Exception as error:  # pragma: no cover - delegated SDK behavior
            raise KeyError("object_not_found") from error
        body = response["Body"]
        return body.read() if hasattr(body, "read") else bytes(body)

    def head(self, key: str) -> StoredObject:
        response = self.client.head_object(Bucket=self.bucket, Key=key)
        return StoredObject(key=key, size_bytes=int(response.get("ContentLength", 0)), content_hash=response.get("Metadata", {}).get("sha256", ""), mime_type=response.get("ContentType", _mime(key, None)))

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def initiate_multipart(self, key: str, mime_type: str | None = None) -> str:
        response = self.client.create_multipart_upload(Bucket=self.bucket, Key=key, ContentType=_mime(key, mime_type))
        upload_id = response["UploadId"]
        self._uploads[upload_id] = (key, mime_type)
        return upload_id

    def upload_part(self, upload_id: str, part_number: int, content: bytes, key: str | None = None) -> str:
        upload = self._uploads.get(upload_id)
        if upload:
            key = upload[0]
        if not key:
            raise KeyError("multipart_upload_not_found")
        response = self.client.upload_part(Bucket=self.bucket, Key=key, UploadId=upload_id, PartNumber=part_number, Body=content)
        return response["ETag"]

    def complete_multipart(self, upload_id: str, key: str | None = None, mime_type: str | None = None) -> StoredObject:
        upload = self._uploads.get(upload_id)
        if upload:
            key, stored_mime_type = upload
            mime_type = mime_type or stored_mime_type
        elif not key:
            raise KeyError("multipart_upload_not_found")
        parts: list[dict[str, object]] = []
        part_number_marker: str | None = None
        while True:
            request = {"Bucket": self.bucket, "Key": key, "UploadId": upload_id}
            if part_number_marker:
                request["PartNumberMarker"] = part_number_marker
            response = self.client.list_parts(**request)
            parts.extend({"PartNumber": part["PartNumber"], "ETag": part["ETag"]} for part in response.get("Parts", []))
            if not response.get("IsTruncated"):
                break
            part_number_marker = str(response.get("NextPartNumberMarker", ""))
            if not part_number_marker:
                raise ValueError("multipart_parts_pagination_invalid")
        parts = sorted(parts, key=lambda part: int(part["PartNumber"]))
        if not parts:
            raise ValueError("multipart_upload_has_no_parts")
        self.client.complete_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts})
        self._uploads.pop(upload_id, None)
        content = self.get_bytes(key)
        content_hash = _hash(content)
        resolved_mime = _mime(key, mime_type)
        self.client.copy_object(Bucket=self.bucket, Key=key, CopySource={"Bucket": self.bucket, "Key": key}, ContentType=resolved_mime, Metadata={"sha256": content_hash}, MetadataDirective="REPLACE")
        return StoredObject(key=key, size_bytes=len(content), content_hash=content_hash, mime_type=resolved_mime)

    def abort_multipart(self, upload_id: str, key: str | None = None) -> None:
        upload = self._uploads.pop(upload_id, None)
        if upload:
            key = upload[0]
        if not key:
            raise KeyError("multipart_upload_not_found")
        self.client.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id)


def create_object_store(default_root: str | Path, *, environ: dict[str, str] | None = None) -> ObjectStore:
    """Build the configured object store without changing the local default."""
    settings = os.environ if environ is None else environ
    backend = settings.get("OBJECT_STORE_BACKEND", "local").lower()
    if backend == "local":
        return LocalObjectStore(default_root)
    if backend == "s3":
        bucket = settings.get("S3_BUCKET")
        if not bucket:
            raise RuntimeError("S3_BUCKET_required_for_s3_object_store")
        return S3ObjectStore(
            bucket,
            endpoint_url=settings.get("S3_ENDPOINT_URL"),
            region_name=settings.get("S3_REGION", "us-east-1"),
        )
    raise ValueError("unsupported_object_store_backend")
