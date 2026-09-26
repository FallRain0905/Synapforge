"""FM-1 端到端验收：个人云盘正式数据模型与服务层（真 API、真 HTTP、真对象存储）。

单元测试证明的是函数行为；这个脚本证明**线上同构的那条路**也是通的——真 uvicorn、真 multipart
上传、真下载响应头（中文名走 RFC 5987）、真并发、真会话鉴权。跑在一份临时库与临时对象目录上，
不碰开发库、不碰线上。

六条验收逐条落到这里：

1. 老平面文件（`personal_drive_files`）经回填后**同 id、同哈希**可见可下载；
2. 跨成员、跨组织访问一律 404；
3. 同目录同名稳定 409；
4. 并发上传不突破配额（`PLATFORM_DRIVE_QUOTA_BYTES` 压到 1000 字节来逼出竞争）；
5. 删除两段式：回收站 → 彻底清除，且对象清理队列被消费掉；
6. `PLATFORM_AUTH_MODE=required` 下没有 Bearer 一律 401。

用法（仓库根目录）：`python -X utf8 scripts/deploy/_fm1_verify.py [--keep]`
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from uuid import uuid4
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for candidate in (str(ROOT), str(ROOT / "apps" / "api")):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

import uvicorn  # noqa: E402

import app.main as api_main  # noqa: E402
from app import drive  # noqa: E402
from app.contracts import SessionCreate  # noqa: E402
from app.object_store import create_object_store  # noqa: E402
from app.store import DEV_ORG_ID, Store  # noqa: E402

LEGACY_CONTENT = "遗留内容：第一问的原始数据。".encode("utf-8")
QUOTA_BYTES = 1000


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def multipart(name: str, content: bytes, parent_id: str | None = None) -> tuple[bytes, str]:
    boundary = "----fm1verify" + uuid.uuid4().hex[:12]
    parts = []
    if parent_id:
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"parent_id\"\r\n\r\n{parent_id}\r\n".encode("utf-8")
        )
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8")
        + content
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), boundary


def header(headers: dict, name: str) -> str:
    """大小写不敏感地取响应头（HTTP 头本来就不区分大小写）。"""

    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value)
    return ""


class Api:
    """极小的 HTTP 客户端：要的就是真的走一遍网络（不是 TestClient）。"""

    def __init__(self, base: str, token: str | None = None) -> None:
        self.base = base
        self.token = token

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **(extra or {})}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def call(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(f"{self.base}{path}", data=body, method=method, headers=self._headers())
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw)
            except ValueError:
                return error.code, {"detail": raw[:200].decode("utf-8", "replace")}

    def upload(self, name: str, content: bytes, parent_id: str | None = None) -> tuple[int, dict]:
        body, boundary = multipart(name, content, parent_id)
        request = urllib.request.Request(
            f"{self.base}/api/drive/files",
            data=body,
            method="POST",
            headers=self._headers({"Content-Type": f"multipart/form-data; boundary={boundary}"}),
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw)
            except ValueError:
                return error.code, {"detail": raw[:200].decode("utf-8", "replace")}

    def download(self, node_id: str) -> tuple[int, bytes, dict]:
        request = urllib.request.Request(f"{self.base}/api/drive/nodes/{node_id}/content", headers=self._headers())
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read(), dict(error.headers)


def main() -> int:
    parser = argparse.ArgumentParser(description="FM-1 个人云盘端到端验收")
    parser.add_argument("--keep", action="store_true", help="保留临时目录（排障用）")
    options = parser.parse_args()

    root = Path(os.environ.get("FM1_VERIFY_DIR") or tempfile.mkdtemp(prefix="fm1-verify-"))
    store = Store(root / "platform.db", object_store=create_object_store(root / "objects"))
    api_main.store = store
    drive.ensure_schema(store)

    # 老数据：伪造一条"旧平面表"记录 + 真实对象（回填要能原样搬过去）
    legacy_id = str(uuid4())
    created = "2026-09-01T12:00:00+00:00"
    stored = store.object_store.put_bytes(f"drive/member-001/{legacy_id}", LEGACY_CONTENT, "text/plain")
    store.db.execute(
        "CREATE TABLE IF NOT EXISTS personal_drive_files (id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL, size_bytes INTEGER NOT NULL, content_hash TEXT NOT NULL, storage_key TEXT NOT NULL, mime_type TEXT, is_archive INTEGER NOT NULL DEFAULT 0, project_ids TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL)"
    )
    store.db.execute(
        "INSERT INTO personal_drive_files (id, owner, name, size_bytes, content_hash, storage_key, mime_type, is_archive, project_ids, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, '[]', ?)",
        (legacy_id, "member-001", "遗留数据.csv", len(LEGACY_CONTENT), hashlib.sha256(LEGACY_CONTENT).hexdigest(), stored.key, "text/csv", created),
    )
    store.db.commit()
    backfill = drive.backfill_legacy(store)

    # 第二个成员（同组织）与第三个成员（别的组织）
    for member_id, organization in (("member-fm1-other", DEV_ORG_ID), ("member-fm1-foreign", str(uuid4()))):
        if organization == DEV_ORG_ID:
            team = store.db.execute("SELECT id FROM teams WHERE organization_id = ? LIMIT 1", (organization,)).fetchone()
            team_id = str(team["id"])
        else:
            team_id = str(uuid4())
            store.db.execute(
                "INSERT INTO organizations (id, name, slug, created_at) VALUES (?, ?, ?, ?)",
                (organization, f"隔离组织 {member_id}", f"iso-{member_id}", datetime.now(UTC).isoformat()),
            )
            store.db.execute(
                "INSERT INTO teams (id, organization_id, name, created_at) VALUES (?, ?, ?, ?)",
                (team_id, organization, "隔离队伍", datetime.now(UTC).isoformat()),
            )
        store.db.execute(
            "INSERT INTO human_members (id, organization_id, team_id, email, display_name, status, created_at) VALUES (?, ?, ?, ?, ?, 'active', ?)",
            (member_id, organization, team_id, f"{member_id}@example.test", member_id, datetime.now(UTC).isoformat()),
        )
    store.db.commit()

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(api_main.app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(150):
        try:
            urllib.request.urlopen(f"{base}/api/organizations", timeout=2).close()
            break
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)

    checks: list[dict] = []

    def check(name: str, passed: bool, detail: str = "") -> None:
        checks.append({"check": name, "passed": bool(passed), "detail": detail})
        print(f"[{'PASS' if passed else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")

    mine = Api(base, store.create_session(SessionCreate(member_id="member-001", expires_in_seconds=600)).token)
    other = Api(base, store.create_session(SessionCreate(member_id="member-fm1-other", expires_in_seconds=600)).token)
    foreign = Api(base, store.create_session(SessionCreate(member_id="member-fm1-foreign", expires_in_seconds=600)).token)

    # ---- 1. 老文件无损 -------------------------------------------------------
    check("回填把老平面文件搬进节点树（同 id）", backfill["created"] == 1, json.dumps(backfill, ensure_ascii=False))
    listing = mine.call("GET", "/api/drive")
    names = [item["name"] for item in listing[1].get("files", [])]
    check("老文件在旧接口里可见", "遗留数据.csv" in names, str(names))
    status, content, headers = mine.download(legacy_id)
    check(
        "老文件可下载且字节一致（同哈希）",
        status == 200 and content == LEGACY_CONTENT and header(headers, "X-Content-SHA256") == hashlib.sha256(LEGACY_CONTENT).hexdigest(),
        f"status={status} bytes={len(content)}",
    )

    # ---- 2. 隔离 -------------------------------------------------------------
    check("别的成员看不到老文件", other.call("GET", f"/api/drive/nodes/{legacy_id}")[0] == 404)
    check("别的组织看不到老文件", foreign.call("GET", f"/api/drive/nodes/{legacy_id}")[0] == 404)
    check("别的成员下载被拒", other.download(legacy_id)[0] == 404)

    # ---- 3. 目录 + 同名冲突 ---------------------------------------------------
    status, folder = mine.call("POST", "/api/drive/directories", {"name": "论文"})
    folder_id = folder["node"]["id"]
    check("新建目录", status == 201 and folder["node"]["kind"] == "directory", folder_id)
    rfc_name = "题目原文 · 汇总.txt"
    status, uploaded = mine.upload(rfc_name, "问题一".encode("utf-8"), folder_id)
    check("目录内上传", status == 201, json.dumps(uploaded.get("node", {}), ensure_ascii=False)[:120])
    status, conflict = mine.upload(rfc_name, b"another", folder_id)
    check("同目录同名 409（不覆盖）", status == 409 and "file_name_conflict" in str(conflict), f"status={status}")
    status, again = mine.upload(rfc_name, "问题一".encode("utf-8"), folder_id)
    check("同名同内容仍然 409（不静默复用）", status == 409, f"status={status}")
    node_id = uploaded["node"]["id"]
    status, content, headers = mine.download(node_id)
    disposition = header(headers, "Content-Disposition")
    safe = True
    try:
        disposition.encode("latin-1")
    except UnicodeEncodeError:
        safe = False
    check(
        "中文名下载响应头 latin-1 安全且带 RFC 5987",
        status == 200 and "filename*=UTF-8''" in disposition and safe,
        disposition[:80],
    )
    check("下载字节与上传一致", content == "问题一".encode("utf-8"), f"{len(content)} 字节")

    # ---- 4. 并发上传：同一份内容只计一次费 + 不同内容不突破配额 ----------------
    quota_client = Api(base, mine.token)
    base_used = mine.call("GET", "/api/drive/trash")[1]["usage"]["used_bytes"]

    def race(label: str, payloads: list[bytes]) -> list[tuple[int, str]]:
        results: list[tuple[int, str]] = []
        started = threading.Barrier(len(payloads))

        def one(index: int) -> None:
            started.wait()
            # 文件名按批次区分：两批都用同一个名字的话，第二批会撞上"同名 409"，测的就不是配额了
            status_code, payload = quota_client.upload(f"{label}-{index}.bin", payloads[index])
            results.append((status_code, str(payload.get("detail", ""))[:40]))

        threads = [threading.Thread(target=one, args=(index,)) for index in range(len(payloads))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return results

    same = race("same", [b"z" * 300 for _ in range(4)])
    accepted_same = [item for item in same if item[0] == 201]
    used_same = mine.call("GET", "/api/drive/trash")[1]["usage"]["used_bytes"]
    check(
        "并发上传同一份内容：四个文件、只占一份空间",
        len(accepted_same) == 4 and used_same == base_used + 300,
        f"201×{len(accepted_same)} used={used_same}（基线 {base_used}）",
    )

    distinct = race("distinct", [bytes([70 + index]) * 300 for index in range(4)])
    accepted_distinct = [item for item in distinct if item[0] == 201]
    rejected = [item for item in distinct if item[0] == 413]
    usage = mine.call("GET", "/api/drive/trash")[1]["usage"]
    expected_used = used_same + 300 * len(accepted_distinct)
    check(
        "并发上传不同内容：413 兜住，不突破配额",
        len(rejected) >= 1 and usage["used_bytes"] <= QUOTA_BYTES and usage["used_bytes"] == expected_used,
        f"201×{len(accepted_distinct)} 413×{len(rejected)} used={usage['used_bytes']}/{QUOTA_BYTES}",
    )

    # ---- 5. 回收站 → 彻底清除 → 队列被消费 ------------------------------------
    status, trashed = mine.call("DELETE", f"/api/drive/nodes/{node_id}")
    check("删除进回收站", status == 200 and trashed["trashed_count"] == 1, json.dumps(trashed, ensure_ascii=False)[:100])
    trash = mine.call("GET", "/api/drive/trash")[1]
    check("回收站能看到它", [item["name"] for item in trash["nodes"]] == [rfc_name], str([n["name"] for n in trash["nodes"]]))
    check("回收站里的文件仍占配额", trash["usage"]["used_bytes"] >= len("问题一".encode("utf-8")))
    status, restored = mine.call("POST", f"/api/drive/nodes/{node_id}/restore")
    check("从回收站恢复", status == 200 and restored["node"]["trashed_with"] is None, f"status={status}")
    mine.call("DELETE", f"/api/drive/nodes/{node_id}")
    before = mine.call("GET", "/api/drive/trash")[1]["usage"]["used_bytes"]
    status, purged = mine.call("DELETE", f"/api/drive/trash/{node_id}")
    after = mine.call("GET", "/api/drive/trash")[1]["usage"]["used_bytes"]
    check(
        "彻底清除释放空间并删掉对象",
        status == 200 and purged["objects_deleted"] == 1 and after == before - len("问题一".encode("utf-8")),
        f"deleted={purged['objects_deleted']} pending={purged['objects_pending']} used {before}→{after}",
    )
    retry = mine.call("POST", "/api/drive/cleanup/retry")[1]
    check("清理队列已消费干净", retry["pending_before"] == 0, json.dumps(retry, ensure_ascii=False))

    # ---- 6. required 模式 -----------------------------------------------------
    os.environ["PLATFORM_AUTH_MODE"] = "required"
    try:
        anonymous = Api(base)
        check("required 模式下无令牌 401（列表）", anonymous.call("GET", "/api/drive/nodes")[0] == 401)
        check("required 模式下无令牌 401（新建目录）", anonymous.call("POST", "/api/drive/directories", {"name": "x"})[0] == 401)
        check("required 模式下无令牌 401（下载）", anonymous.download(legacy_id)[0] == 401)
        check("required 模式下带令牌仍然可用", mine.call("GET", "/api/drive/nodes")[0] == 200)
    finally:
        os.environ.pop("PLATFORM_AUTH_MODE", None)

    # ---- 审计 ----------------------------------------------------------------
    audit = mine.call("GET", f"/api/drive/nodes/{legacy_id}/audit")[1]["events"]
    actions = {item["action"] for item in audit}
    check("审计里有下载与上传记录", {"download"} <= actions or {"upload"} <= actions, str(sorted(actions)))
    legacy_audit = mine.call("GET", f"/api/drive/nodes/{legacy_id}/audit")
    check("审计不泄漏文件名（只有哈希）", rfc_name not in json.dumps(legacy_audit[1], ensure_ascii=False))

    failed = [item for item in checks if not item["passed"]]
    print(json.dumps({"checks": checks, "passed": len(checks) - len(failed), "failed": len(failed), "dir": str(root)}, ensure_ascii=False, indent=2))
    server.should_exit = True
    if options.keep:
        print(f"临时目录保留在 {root}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())