"""把**被授权的云盘文件**物化到工作区 `inputs/`（FM-5）。

流程与边界：

1. 用项目能力令牌 + Agent/设备身份换一个**短期 lease**（明文只在响应里出现一次，内核不落盘）；
2. 拉"物化清单"（平台按授权范围算出来的文件列表，含名字/大小/sha256）；
3. 逐个下载到 `<workspace>/inputs/`，**每份都校验大小与 sha256**，先写临时文件再 `os.replace`
   （不留半个文件）；重名加序号，不覆盖已有输入；
4. 把结果合并进 `inputs-manifest.json`（复用 `input_fetcher` 的台账，来源标 `drive_grant`），
   失败项如实写进 `failures`——**不假装拿到了文件**。

撤销与过期是平台侧判的：lease 过期、Grant 被撤、epoch 变了，下载会直接 403；
这里如实把错误记进 failures，而不是重试到天荒地老。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from input_fetcher import INPUT_DIR_NAME, _safe_name, write_inputs_manifest


class DriveMaterializeError(RuntimeError):
    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code if not detail else f"{code}:{detail}")
        self.code = code
        self.detail = detail or ""


@dataclass
class DriveMaterializeConfig:
    url: str
    project_token: str
    agent_id: str
    project_id: str
    grant_id: str
    workspace: Path
    device_id: str | None = None
    run_id: str | None = None
    lease_ttl_seconds: int = 900
    max_bytes: int = 64 * 1024 * 1024


class DriveMaterializer:
    """物化器：`http` 可注入（测试用假平台，不联网）。"""

    def __init__(self, config: DriveMaterializeConfig, *, http: Callable[..., Any] | None = None, log: Callable[[str], None] = print) -> None:
        self.config = config
        self.log = log
        self._http = http or self._default_http

    def _headers(self, *, lease: str | None = None) -> dict[str, str]:
        headers = {
            "X-Project-Capability-Token": self.config.project_token,
            "X-Agent-Id": self.config.agent_id,
            "X-Project-Id": self.config.project_id,
        }
        if self.config.device_id:
            headers["X-Device-Id"] = self.config.device_id
        if lease:
            headers["X-File-Access-Lease"] = lease
        return headers

    @staticmethod
    def _default_http(method: str, url: str, *, headers: dict[str, str], body: bytes | None = None, timeout: float = 60.0) -> tuple[int, Any]:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(url, data=body, method=method, headers={**headers, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                content_type = response.headers.get("Content-Type", "")
                payload: Any = json.loads(raw) if raw and content_type.startswith("application/json") else raw
                return response.status, payload
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw)
            except ValueError:
                return error.code, {"detail": raw[:200].decode("utf-8", "replace")}

    def _call(self, method: str, path: str, payload: dict | None = None, *, lease: str | None = None) -> tuple[int, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        return self._http(method, f"{self.config.url.rstrip('/')}{path}", headers=self._headers(lease=lease), body=body)

    # -- 主流程 --------------------------------------------------------------
    def materialize(self) -> dict[str, Any]:
        exchange_status, exchange = self._call(
            "POST",
            "/api/agent/file-leases/exchange",
            {"grant_id": self.config.grant_id, "run_id": self.config.run_id, "ttl_seconds": self.config.lease_ttl_seconds},
        )
        if exchange_status != 200 or not isinstance(exchange, dict):
            raise DriveMaterializeError("drive_lease_exchange_failed", f"{exchange_status}:{str(exchange)[:160]}")
        lease = str((exchange.get("lease") or {}).get("token") or "")
        if not lease:
            raise DriveMaterializeError("drive_lease_exchange_failed", "no_token")

        manifest_status, manifest = self._call("POST", "/api/agent/drive/materialize", lease=lease)
        if manifest_status != 200 or not isinstance(manifest, dict):
            raise DriveMaterializeError("drive_manifest_failed", f"{manifest_status}:{str(manifest)[:160]}")
        entries = manifest.get("entries") or []
        if not entries:
            self.log("[drive] 授权范围内没有文件可物化")
            return {"written": [], "failed": [], "grant_id": self.config.grant_id}

        directory = Path(self.config.workspace).expanduser() / INPUT_DIR_NAME
        written: list[str] = []
        failed: list[str] = []
        manifest_entries: list[dict[str, Any]] = []
        for entry in entries:
            node_id = str(entry.get("node_id") or "")
            name = str(entry.get("name") or "")
            expected_hash = str(entry.get("content_hash") or "")
            expected_size = int(entry.get("size_bytes") or 0)
            target_name = _safe_name(name, node_id)
            try:
                content = self._download(node_id, lease)
                if expected_size and len(content) != expected_size:
                    raise DriveMaterializeError("drive_input_size_mismatch", f"{len(content)}!={expected_size}")
                actual = hashlib.sha256(content).hexdigest()
                if expected_hash and actual != expected_hash:
                    raise DriveMaterializeError("drive_input_hash_mismatch", f"{actual}!={expected_hash}")
            except DriveMaterializeError as error:
                failed.append(f"{target_name}: {error.code}")
                self.log(f"[drive] {target_name} 取不到：{error.code}")
                continue
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / target_name
            suffix = 2
            while target.exists():
                target = directory / f"{Path(target_name).stem}-{suffix}{Path(target_name).suffix}"
                suffix += 1
            temporary = target.with_name(f".{target.name}.part")
            temporary.write_bytes(content)
            temporary.replace(target)
            written.append(target.name)
            manifest_entries.append(
                {
                    "artifact_id": node_id,
                    "name": target.name,
                    "relative_path": f"{INPUT_DIR_NAME}/{target.name}",
                    "sha256": actual,
                    "size_bytes": len(content),
                    "source": "drive_grant",
                    "grant_id": self.config.grant_id,
                }
            )
            self.log(f"[drive] 已物化 {INPUT_DIR_NAME}/{target.name}（{len(content)} 字节）")

        if manifest_entries or failed:
            write_inputs_manifest(self.config.workspace, manifest_entries, failures=failed, log=self.log)
        return {
            "grant_id": self.config.grant_id,
            "expires_at": manifest.get("expires_at"),
            "written": written,
            "failed": failed,
        }

    def _download(self, node_id: str, lease: str) -> bytes:
        status, payload = self._http(
            "GET",
            f"{self.config.url.rstrip('/')}/api/agent/drive/nodes/{node_id}/content",
            headers=self._headers(lease=lease),
            body=None,
        )
        if status == 403:
            detail = str(payload.get("detail") if isinstance(payload, dict) else payload)[:120]
            raise DriveMaterializeError("file_access_scope_denied" if "scope" in detail else "file_access_revoked", detail)
        if status != 200:
            raise DriveMaterializeError("drive_download_failed", f"{status}")
        if isinstance(payload, (bytes, bytearray)):
            content = bytes(payload)
        elif isinstance(payload, dict) and payload.get("content_base64"):
            import base64

            content = base64.b64decode(str(payload["content_base64"]))
        else:
            raise DriveMaterializeError("drive_download_failed", "unexpected_payload")
        if len(content) > self.config.max_bytes:
            raise DriveMaterializeError("drive_input_too_large", str(len(content)))
        return content


__all__ = ["DriveMaterializeConfig", "DriveMaterializeError", "DriveMaterializer"]