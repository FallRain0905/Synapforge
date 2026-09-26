"""FM-2 端到端验收：文件管理器（真 HTTP）+ 安全解压（攻击样本全部被拒）。

单元测试已经把 22 类攻击样本钉住了；这个脚本证明**线上同构的这条路**也拦得住，并且正常流程
（上传 → 解压 → 目录树 → 搜索/排序/分页 → 改名/移动/复制 → 回收站 → 彻底清除）整条走得通。

用法（仓库根目录）：`PLATFORM_DRIVE_QUOTA_BYTES=2000000 python -X utf8 scripts/deploy/_fm2_verify.py [--keep]`
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import socket
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for candidate in (str(ROOT), str(ROOT / "apps" / "api")):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

import uvicorn  # noqa: E402

import app.main as api_main  # noqa: E402
from app import archive, drive  # noqa: E402
from app.contracts import SessionCreate  # noqa: E402
from app.object_store import create_object_store  # noqa: E402
from app.store import Store  # noqa: E402

GOOD_ZIP = {
    "报告/正文.md": "# 正文\n第一问的模型假设。".encode("utf-8"),
    "报告/数据/表.csv": b"x,y\n1,2\n",
    "报告/图/趋势.txt": b"trend",
}


def zip_bytes(entries: dict[str, bytes], *, mode: dict[str, int] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as handle:
        for name, content in entries.items():
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = ((mode or {}).get(name, 0o100644) & 0xFFFF) << 16
            handle.writestr(info, content)
    return buffer.getvalue()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def header(headers: dict, name: str) -> str:
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value)
    return ""


class Api:
    def __init__(self, base: str, token: str | None = None) -> None:
        self.base = base
        self.token = token

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **(extra or {})}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def call(self, method: str, path: str, payload: dict | list | None = None) -> tuple[int, dict]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(f"{self.base}{path}", data=body, method=method, headers=self._headers())
        return self._send(request)

    def upload(self, name: str, content: bytes, parent_id: str | None = None) -> tuple[int, dict]:
        boundary = "----fm2verify" + uuid.uuid4().hex[:12]
        parts = []
        if parent_id:
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"parent_id\"\r\n\r\n{parent_id}\r\n".encode())
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n".encode()
            + content
            + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())
        request = urllib.request.Request(
            f"{self.base}/api/drive/files",
            data=b"".join(parts),
            method="POST",
            headers=self._headers({"Content-Type": f"multipart/form-data; boundary={boundary}"}),
        )
        return self._send(request)

    def download(self, node_id: str) -> tuple[int, bytes, dict]:
        request = urllib.request.Request(f"{self.base}/api/drive/nodes/{node_id}/content", headers=self._headers())
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, error.read(), dict(error.headers)

    @staticmethod
    def _send(request: urllib.request.Request) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw)
            except ValueError:
                return error.code, {"detail": raw[:200].decode("utf-8", "replace")}


def flatten(api: Api, node_id: str, prefix: str = "") -> dict[str, bytes]:
    """把一棵子树读成 `相对路径 → 内容`（用来对账解压结果）。"""

    out: dict[str, bytes] = {}
    status, payload = api.call("GET", f"/api/drive/nodes?parent_id={node_id}&limit=500")
    if status != 200:
        return out
    for node in payload["nodes"]:
        path = f"{prefix}{node['name']}"
        if node["kind"] == "directory":
            out.update(flatten(api, node["id"], f"{path}/"))
        else:
            code, content, _ = api.download(node["id"])
            if code == 200:
                out[path] = content
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="FM-2 文件管理器与安全解压验收")
    parser.add_argument("--keep", action="store_true", help="保留临时目录（排障用）")
    options = parser.parse_args()

    root = Path(os.environ.get("FM2_VERIFY_DIR") or tempfile.mkdtemp(prefix="fm2-verify-"))
    store = Store(root / "platform.db", object_store=create_object_store(root / "objects"))
    api_main.store = store
    drive.ensure_schema(store)

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

    api = Api(base, store.create_session(SessionCreate(member_id="member-001", expires_in_seconds=600)).token)

    def node_count() -> int:
        return int(store.db.execute("SELECT COUNT(*) AS c FROM drive_nodes WHERE purged_at IS NULL").fetchone()["c"])

    # ---- 1. 攻击样本：一律拒绝，且什么都不写 ---------------------------------
    attacks = {
        "zip slip": ("slip.zip", zip_bytes({"../escape.txt": b"x"}), 400, "archive_path_unsafe"),
        "绝对路径": ("abs.zip", zip_bytes({"/etc/passwd": b"x"}), 400, "archive_path_unsafe"),
        "盘符": ("drive.zip", zip_bytes({"C:/Windows/evil.dll": b"x"}), 400, "archive_path_unsafe"),
        "UNC": ("unc.zip", zip_bytes({"//server/share/evil.txt": b"x"}), 400, "archive_path_unsafe"),
        "zip symlink": ("symlink.zip", zip_bytes({"link": b"../t"}, mode={"link": 0o120777}), 400, "archive_path_unsafe"),
        "保留名": ("reserved.zip", zip_bytes({"CON": b"x"}), 400, "archive_path_unsafe"),
        "不支持格式": ("archive.7z", b"not an archive", 400, "archive_format_unsupported"),
        "坏 zip": ("broken.zip", b"PK\x03\x04 garbage", 400, "archive_format_unsupported"),
        "空归档": ("empty.zip", zip_bytes({}), 400, "archive_empty"),
    }
    for label, (name, payload, expected_status, expected_code) in attacks.items():
        status, uploaded = api.upload(name, payload)
        if status != 201:
            check(f"攻击样本被拒：{label}", False, f"上传就失败了 {status} {uploaded}")
            continue
        # 基线取在**上传之后**：成员根目录是懒创建的（第一次请求才会多出一个节点）
        before = node_count()
        status, result = api.call("POST", "/api/drive/extractions", {"node_id": uploaded["node"]["id"]})
        check(
            f"攻击样本被拒：{label}",
            status == expected_status and expected_code in str(result.get("detail", "")),
            f"{status} {str(result.get('detail'))[:60]}",
        )
        check(f"攻击样本不留痕迹：{label}", node_count() == before, f"节点 {before}→{node_count()}（被拒的解压不该多出任何节点）")

    # tar 的链接类攻击（symlink/hardlink/设备文件）
    for label, kind, target in (("tar symlink", "sym", "/etc/passwd"), ("tar hardlink", "hard", "ok.txt"), ("tar 设备文件", "chr", "")):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as handle:
            info = tarfile.TarInfo("ok.txt")
            info.size = 2
            handle.addfile(info, io.BytesIO(b"ok"))
            link = tarfile.TarInfo(f"{kind}-link")
            link.type = tarfile.SYMTYPE if kind == "sym" else tarfile.LNKTYPE if kind == "hard" else tarfile.CHRTYPE
            link.linkname = target
            handle.addfile(link)
        status, uploaded = api.upload(f"{kind}.tar.gz", buffer.getvalue())
        status, result = api.call("POST", "/api/drive/extractions", {"node_id": uploaded["node"]["id"]})
        check(f"攻击样本被拒：{label}", status == 400 and "archive_path_unsafe" in str(result.get("detail")), f"{status}")

    # 压缩炸弹（压缩比）：阈值临时压低到 2.0 才能用几 MB 的数据逼出来
    original_ratio = archive.MAX_RATIO
    archive.MAX_RATIO = 2.0
    try:
        status, uploaded = api.upload("bomb.zip", zip_bytes({"zeros.bin": b"\x00" * (2 * 1024 * 1024)}))
        nodes_before = node_count()
        status, result = api.call("POST", "/api/drive/extractions", {"node_id": uploaded["node"]["id"]})
    finally:
        archive.MAX_RATIO = original_ratio
    check("攻击样本被拒：压缩炸弹", status == 413 and "archive_ratio_exceeded" in str(result.get("detail")), f"{status}")
    check("压缩炸弹不留痕迹", node_count() == nodes_before, f"节点 {nodes_before}→{node_count()}")

    # ---- 2. 正常解压：目录树、内容、哈希都对得上 -----------------------------
    status, uploaded = api.upload("材料.zip", zip_bytes(GOOD_ZIP))
    check("上传归档", status == 201, str(uploaded.get("node", {}).get("name")))
    archive_id = uploaded["node"]["id"]
    status, extraction = api.call("POST", "/api/drive/extractions", {"node_id": archive_id})
    check(
        "解压成新目录（文件数/目录数如实）",
        status == 201 and extraction["files"] == 3 and extraction["directories"] == 4,
        f"{status} files={extraction.get('files')} dirs={extraction.get('directories')}",
    )
    tree = flatten(api, extraction["node"]["id"])
    check("解压出来的路径与归档一致", set(tree) == set(GOOD_ZIP), str(sorted(tree)))
    check(
        "解压出来的字节与归档一致（逐份对哈希）",
        all(tree.get(path) == GOOD_ZIP[path] for path in GOOD_ZIP),
        f"{len(tree)} 份",
    )

    # ---- 3. 管理操作：搜索/排序/分页、改名、移动、复制 -------------------------
    listing = api.call("GET", f"/api/drive/nodes?parent_id={extraction['node']['id']}")[1]
    check("列目录带面包屑与用量", listing["breadcrumb"][-1]["name"] == "材料" and "usage" in listing, str([c["name"] for c in listing["breadcrumb"]]))

    status, mkdir = api.call("POST", "/api/drive/directories", {"name": "归档区"})
    archive_dir = mkdir["node"]["id"]
    status, conflict = api.call("POST", "/api/drive/directories", {"name": "归档区"})
    check("同名目录冲突 409", status == 409 and "file_name_conflict" in str(conflict.get("detail")), f"{status}")

    status, renamed = api.call("PATCH", f"/api/drive/nodes/{archive_dir}", {"name": "归档区-2026"})
    check("改名", status == 200 and renamed["node"]["name"] == "归档区-2026", f"{status}")
    revision = renamed["node"]["revision"]
    status, stale = api.call("PATCH", f"/api/drive/nodes/{archive_dir}", {"name": "又改", "expected_revision": revision - 1})
    check("修订冲突 409（不盲目覆盖）", status == 409 and "file_revision_conflict" in str(stale.get("detail")), f"{status}")

    status, moved = api.call("POST", f"/api/drive/nodes/{extraction['node']['id']}/move", {"parent_id": archive_dir})
    check("移动到另一个目录", status == 200 and moved["node"]["parent_id"] == archive_dir, f"{status}")
    status, cycle = api.call("POST", f"/api/drive/nodes/{archive_dir}/move", {"parent_id": extraction["node"]["id"]})
    check("目录环被拒 409", status == 409 and "file_directory_cycle" in str(cycle.get("detail")), f"{status}")

    used_before_copy = api.call("GET", "/api/drive/trash")[1]["usage"]["used_bytes"]
    status, copied = api.call("POST", f"/api/drive/nodes/{archive_id}/copy", {"parent_id": archive_dir})
    check(
        "复制到别的目录（无同名则沿用原名）",
        status == 201 and copied["node"]["name"] == "材料.zip" and copied["node"]["parent_id"] == archive_dir,
        str(copied.get("node", {}).get("name")),
    )
    status, second_copy = api.call("POST", f"/api/drive/nodes/{archive_id}/copy", {"parent_id": archive_dir})
    check(
        "同目录再复制一次：自动加「-副本」，不覆盖",
        status == 201 and "副本" in second_copy["node"]["name"],
        str(second_copy.get("node", {}).get("name")),
    )
    used_after_copy = api.call("GET", "/api/drive/trash")[1]["usage"]["used_bytes"]
    check(
        "复制共生对象：配额不增加（同内容只算一份）",
        used_after_copy == used_before_copy,
        f"used {used_before_copy}→{used_after_copy}",
    )

    # 搜索与排序
    search_term = urllib.parse.quote("报告")  # URL 里的中文必须编码（否则 urllib 直接抛 ascii 错）
    status, searched = api.call("GET", f"/api/drive/nodes?parent_id={extraction['node']['id']}&query={search_term}")
    check("搜索命中直接子目录", status == 200 and [n["name"] for n in searched["nodes"]] == ["报告"], str([n["name"] for n in searched["nodes"]]))
    status, sized = api.call("GET", f"/api/drive/nodes?sort=size&limit=2")
    check("按大小排序 + 分页", status == 200 and len(sized["nodes"]) <= 2, f"truncated={sized['truncated']}")

    # ---- 4. 回收站：软删 → 恢复 → 再删 → 彻底清除 -----------------------------
    status, trashed = api.call("DELETE", f"/api/drive/nodes/{archive_id}")
    check("删除进回收站", status == 200 and trashed["trashed_count"] == 1, f"{status}")
    trash = api.call("GET", "/api/drive/trash")[1]["nodes"]
    check("回收站列出它", any(node["id"] == archive_id for node in trash), str([n["name"] for n in trash]))
    status, _ = api.call("POST", f"/api/drive/nodes/{archive_id}/restore")
    check("恢复", status == 200, f"{status}")
    api.call("DELETE", f"/api/drive/nodes/{archive_id}")
    status, purged = api.call("DELETE", f"/api/drive/trash/{archive_id}")
    # 这个对象被刚复制出来的两份共用：**不能**删（删了副本就坏了）
    check(
        "共用对象不被误删（副本仍可下载）",
        status == 200 and purged["objects_deleted"] == 0 and purged["objects_pending"] == 0,
        json.dumps({k: purged[k] for k in ("objects_deleted", "objects_pending")}),
    )
    copy_code, copy_bytes, _ = api.download(copied["node"]["id"])
    check("副本内容仍完整（同哈希）", copy_code == 200 and copy_bytes == zip_bytes(GOOD_ZIP), f"{copy_code} {len(copy_bytes)} 字节")
    check("清理队列已消费", api.call("POST", "/api/drive/cleanup/retry")[1]["pending_before"] == 0)

    # 独占对象的彻底清除：这份没被复制过，删对象必须成功
    status, unique_upload = api.upload("独占.txt", b"only one reference")
    unique_id = unique_upload["node"]["id"]
    api.call("DELETE", f"/api/drive/nodes/{unique_id}")
    status, purged_unique = api.call("DELETE", f"/api/drive/trash/{unique_id}")
    check(
        "彻底清除独占对象：真删了",
        status == 200 and purged_unique["objects_deleted"] == 1 and purged_unique["objects_pending"] == 0,
        json.dumps({k: purged_unique[k] for k in ("objects_deleted", "objects_pending")}),
    )

    # ---- 5. 旧接口不回归 ------------------------------------------------------
    legacy = api.call("GET", "/api/drive")[1]
    check("旧接口仍能用（根目录列表 + usage）", "files" in legacy and "usage" in legacy, f"{len(legacy.get('files', []))} 个文件")

    # ---- 6. 中文名下载头 ------------------------------------------------------
    status, uploaded = api.upload("题目原文 · 汇总.txt", "问题一".encode("utf-8"))
    status, content, headers = api.download(uploaded["node"]["id"])
    disposition = header(headers, "Content-Disposition")
    check(
        "中文名下载头 latin-1 安全 + RFC 5987",
        status == 200 and "filename*=UTF-8''" in disposition and content == "问题一".encode("utf-8"),
        disposition[:70],
    )

    failed = [item for item in checks if not item["passed"]]
    print(json.dumps({"checks": checks, "passed": len(checks) - len(failed), "failed": len(failed), "dir": str(root)}, ensure_ascii=False, indent=2))
    server.should_exit = True
    if options.keep:
        print(f"临时目录保留在 {root}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())