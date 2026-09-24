"""FM-3 端到端验收：真平台 + 真内核 Worker，在工作区里真的动文件。

单元测试证明的是两侧各自的行为；这个脚本证明**接起来**也对：平台入队 → 内核领取 → 在本机执行 →
回报 → 平台上状态与结果都对；并且把"逃逸/保护/离线"这些必须拒绝的路径在真 HTTP 上再走一遍。

跑的东西与线上同构：真 uvicorn、真设备+项目授权（含 workspace.files.* 能力）、真 WorkspaceFileWorker
（走 HTTP，不是假客户端）。工作区是一个临时目录，不碰任何真实数据。

用法（仓库根目录）：`python -X utf8 scripts/deploy/_fm3_verify.py [--keep]`
"""

from __future__ import annotations

import argparse
import hashlib
import io
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
import zipfile
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
for candidate in (str(ROOT), str(ROOT / "apps" / "api"), str(ROOT / "apps" / "agent")):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

import uvicorn  # noqa: E402

import app.main as api_main  # noqa: E402
from app import workspace_files  # noqa: E402
from app.contracts import (  # noqa: E402
    AgentRegister,
    DevicePairingCreate,
    DeviceProjectGrantCreate,
    SessionCreate,
)
from app.object_store import create_object_store  # noqa: E402
from app.store import DEV_ORG_ID, Store  # noqa: E402
from device_test_support import registration_request  # noqa: E402
from file_worker import FileWorkerConfig, WorkspaceFileWorker  # noqa: E402

MEMBER = "member-001"
AGENT = "agent-fm3"
DEVICE = "device-fm3"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Member:
    """浏览器侧：真 HTTP + 人类会话令牌。"""

    def __init__(self, base: str, token: str) -> None:
        self.base = base
        self.token = token

    def call(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=body,
            method=method,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
        )
        return _send(request)

    def put_bytes(self, path: str, content: bytes) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=content,
            method="PUT",
            headers={"Content-Type": "application/octet-stream", "Authorization": f"Bearer {self.token}"},
        )
        return _send(request)

    def get_bytes(self, path: str) -> tuple[int, bytes]:
        request = urllib.request.Request(f"{self.base}{path}", headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()


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


def zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as handle:
        for name, content in entries.items():
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            handle.writestr(info, content)
    return buffer.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description="FM-3 工作区文件服务端到端验收")
    parser.add_argument("--keep", action="store_true", help="保留临时目录（排障用）")
    options = parser.parse_args()

    root = Path(os.environ.get("FM3_VERIFY_DIR") or tempfile.mkdtemp(prefix="fm3-verify-"))
    workspace_root = root / "ws"
    workspace_root.mkdir(parents=True, exist_ok=True)
    store = Store(root / "platform.db", object_store=create_object_store(root / "objects"))
    api_main.store = store
    workspace_files.ensure_schema(store)
    project = store.list_projects()[0]

    # 真设备 + 真项目授权（含 workspace.files.* 能力）+ 真 Agent 行
    store.register_agent(AgentRegister(agent_id=AGENT, display_name="FM-3 验收 Agent", owner_member_id=MEMBER))
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    pairing = store.create_device_pairing(DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), MEMBER)
    store.register_device(registration_request(pairing, Ed25519PrivateKey.generate(), AGENT, DEVICE, device_name="FM-3 验收机"))
    credential = store.create_device_project_grant(project.id, DeviceProjectGrantCreate(device_id=DEVICE), MEMBER)

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

    member = Member(base, store.create_session(SessionCreate(member_id=MEMBER, expires_in_seconds=3600)).token)

    # ---- 1. 工作区登记（Agent 上报"路径的哈希"，不是路径） --------------------
    identity = workspace_files.workspace_identity_for(str(workspace_root))
    payload = {
        "display_name": "FM-3 验收工作区",
        "workspace_identity": identity,
        "project_id": str(project.id),
        "protected_paths": ["secret-notes"],
    }
    # ① 浏览器/裸调用：**没有** X-Agent-Id 就不是 Agent，必须被拒（这条接口不是给人用的）
    naked = urllib.request.Request(
        f"{base}/api/agent/workspaces/register",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    naked_status, naked_body = _send(naked)
    check("登记接口只认 Agent（缺 X-Agent-Id 被拒）", naked_status in (400, 401), f"{naked_status} {str(naked_body)[:60]}")
    # ② 真 Agent 调用：X-Agent-Id + 项目能力令牌（授权里有 workspace.files.claim）
    request = urllib.request.Request(
        f"{base}/api/agent/workspaces/register",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Agent-Id": AGENT,
            "X-Project-Id": str(project.id),
            "X-Project-Capability-Token": credential.project_token,
        },
    )
    status, registered = _send(request)
    check("Agent 侧登记工作区", status == 200, f"{status} {str(registered)[:80]}")
    if status != 200:
        print(json.dumps({"checks": checks, "passed": 0, "failed": 1, "dir": str(root)}, ensure_ascii=False, indent=2))
        server.should_exit = True
        return 1
    workspace_id = registered["workspace"]["id"]
    listing = member.call("GET", "/api/agent-workspaces")[1]
    check(
        "浏览器侧看得到工作区且不含宿主机路径",
        any(item["id"] == workspace_id for item in listing["workspaces"])
        and str(workspace_root) not in json.dumps(listing, ensure_ascii=False),
        f"identity={listing['workspaces'][0]['workspace_identity'] if listing['workspaces'] else None}",
    )

    # ---- 2. 真内核 Worker：入队 → 领取 → 执行 → 回报 -------------------------
    worker = WorkspaceFileWorker(
        FileWorkerConfig(
            url=base,
            project_token=credential.project_token,
            agent_id=AGENT,
            project_id=str(project.id),
            workspace=workspace_root,
            workspace_id=workspace_id,
            protected_paths=["secret-notes"],
        ),
        log=lambda message: print(f"   [worker] {message}"),
    )

    def enqueue(kind: str, relative: str = "", arguments: dict | None = None, *, fail_when_offline: bool = False) -> dict:
        status, payload = member.call(
            "POST",
            f"/api/agent-workspaces/{workspace_id}/operations",
            {
                "operation_type": kind,
                "relative_path": relative,
                "arguments": arguments or {},
                "idempotency_key": f"op-{uuid.uuid4().hex[:10]}",
                "fail_when_offline": fail_when_offline,
            },
        )
        if status != 201:
            raise SystemExit(f"入队失败：{status} {payload}")
        return payload["operation"]

    def run_worker() -> None:
        worker.poll_once()

    def operation_state(operation_id: str) -> dict:
        status, payload = member.call("GET", f"/api/agent-workspaces/{workspace_id}/operations/{operation_id}")
        return payload["operation"]

    # 目录 + 列表
    enqueue("mkdir", "papers")
    run_worker()
    check("mkdir：真目录被创建", (workspace_root / "papers").is_dir())

    lookup = enqueue("list", "")
    run_worker()
    state = operation_state(lookup["id"])
    names = [entry["name"] for entry in (state["result"] or {}).get("entries", [])]
    check("list：内核回报真实目录内容", state["status"] == "succeeded" and "papers" in names, str(names))

    # 上传（大文件方向：内容经传输会话，不进任何帧）
    content = "第一问的模型假设与变量说明\n".encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    status, transfer = member.call(
        "POST",
        "/api/workspace-transfers",
        {"source_type": "drive", "target_type": "workspace", "workspace_id": workspace_id, "expected_hash": digest, "expected_size": len(content)},
    )
    transfer_id = transfer["transfer"]["id"]
    upload_status, _ = member.put_bytes(f"/api/workspace-transfers/{transfer_id}/content", content)
    check("浏览器把内容写进传输会话", upload_status == 200, f"status={upload_status}")
    enqueue("upload", "papers/假设.md", {"transfer_id": transfer_id})
    run_worker()
    landed = workspace_root / "papers" / "假设.md"
    check(
        "upload：内容按哈希落到工作区",
        landed.is_file() and landed.read_bytes() == content and hashlib.sha256(landed.read_bytes()).hexdigest() == digest,
        f"{landed.name} {landed.stat().st_size if landed.exists() else 0} 字节",
    )

    # 下载（另一个方向）
    status, transfer_out = member.call(
        "POST", "/api/workspace-transfers", {"source_type": "workspace", "target_type": "temp", "workspace_id": workspace_id}
    )
    download_transfer = transfer_out["transfer"]["id"]
    enqueue("download", "papers/假设.md", {"transfer_id": download_transfer})
    run_worker()
    fetch_status, fetched = member.get_bytes(f"/api/workspace-transfers/{download_transfer}/content")
    check(
        "download：内容经传输会话回到浏览器且字节一致",
        fetch_status == 200 and fetched == content,
        f"{fetch_status} {len(fetched)} 字节",
    )

    # 改名 / 移动 / 复制
    enqueue("rename", "papers/假设.md", {"name": "假设-v2.md"})
    run_worker()
    check("rename：文件被改名", (workspace_root / "papers" / "假设-v2.md").is_file())
    enqueue("mkdir", "archive")
    run_worker()
    enqueue("move", "papers/假设-v2.md", {"destination": "archive"})
    run_worker()
    check("move：文件到了新目录", (workspace_root / "archive" / "假设-v2.md").is_file())
    enqueue("copy", "archive/假设-v2.md", {"destination": "papers", "name": "副本.md"})
    run_worker()
    check("copy：两份都在", (workspace_root / "papers" / "副本.md").is_file() and (workspace_root / "archive" / "假设-v2.md").is_file())

    # 解压（真归档 + 攻击样本）
    (workspace_root / "材料.zip").write_bytes(zip_bytes({"报告/正文.md": "# 正文".encode("utf-8"), "报告/数据/表.csv": b"x,y\n"}))
    enqueue("extract", "材料.zip", {"destination": ""})
    run_worker()
    check("extract：解出目录树", (workspace_root / "材料" / "报告" / "正文.md").is_file())

    (workspace_root / "evil.zip").write_bytes(zip_bytes({"../escape.txt": b"x"}))
    evil = enqueue("extract", "evil.zip", {"destination": ""})
    run_worker()
    evil_state = operation_state(evil["id"])
    check(
        "extract：Zip Slip 被拒且不留东西",
        evil_state["status"] == "failed" and evil_state["error_code"] == "archive_path_unsafe" and not (root.parent / "escape.txt").exists(),
        f"{evil_state['status']} {evil_state.get('error_code')}",
    )

    # ---- 3. 逃逸与保护：必须拒绝，且拒绝的理由要能看见 ------------------------
    # 逃逸样本：**两道锁**都要拦——平台在入队时就拒（纵深防御），内核即使收到也拒（权威校验）
    from file_worker import FileWorkerError

    for relative, expected in (
        ("../outside.txt", "workspace_path_outside_root"),
        ("/etc/passwd", "workspace_path_invalid"),
        ("C:/Windows/evil.dll", "workspace_path_invalid"),
    ):
        status, body = member.call(
            "POST",
            f"/api/agent-workspaces/{workspace_id}/operations",
            {"operation_type": "delete", "relative_path": relative, "idempotency_key": f"bad-{uuid.uuid4().hex[:8]}"},
        )
        queued_rejected = status == 400 and "workspace_path" in str(body.get("detail", ""))
        worker_code = ""
        try:
            worker.execute("delete", relative, {})
        except FileWorkerError as error:
            worker_code = error.code
        check(
            f"逃逸被拒：{relative}",
            queued_rejected and worker_code == expected,
            f"入队 {status} / 内核 {worker_code}",
        )

    # 工作区根：平台上根本**表达不出来**（入队就要路径），内核也拒（受保护）
    status, body = member.call(
        "POST",
        f"/api/agent-workspaces/{workspace_id}/operations",
        {"operation_type": "delete", "relative_path": "", "idempotency_key": f"root-{uuid.uuid4().hex[:8]}"},
    )
    root_code = ""
    try:
        worker.execute("delete", "", {"recursive": True})
    except FileWorkerError as error:
        root_code = error.code
    check(
        "工作区根不可删（平台拒绝表达 + 内核受保护）",
        status == 400 and root_code == "workspace_path_protected",
        f"入队 {status} / 内核 {root_code}",
    )

    # 保护目录：平台**不拦**（它看不到真实文件系统，权威判定在内核），内核必须拒
    for relative in (".git", ".math-agent-platform", "secret-notes"):
        (workspace_root / relative).mkdir(exist_ok=True)
        operation = enqueue("delete", relative, {"recursive": True})
        run_worker()
        state = operation_state(operation["id"])
        check(
            f"保护路径不可删：{relative}",
            state["status"] == "failed" and state["error_code"] == "workspace_path_protected" and (workspace_root / relative).exists(),
            f"{state.get('error_code')}",
        )

    # ---- 4. 离线不假装成功 ---------------------------------------------------
    workspace_files.touch_workspace(store, AGENT, status="offline")
    queued = enqueue("list", "")
    check("Agent 离线：操作保持 queued（不假装执行）", queued["status"] == "queued", queued["status"])
    check("Agent 离线：Worker 领不到活", worker.poll_once() == 0)
    fast_fail = enqueue("list", "", fail_when_offline=True)
    check("Agent 离线 + 要求立刻失败：如实标 failed", fast_fail["status"] == "failed" and fast_fail["error_code"] == "workspace_offline", fast_fail.get("error_code") or "")
    workspace_files.touch_workspace(store, AGENT, status="online")
    run_worker()
    check("上线后那条 queued 被捡起来执行", operation_state(queued["id"])["status"] == "succeeded")

    # ---- 5. 幂等与审计 -------------------------------------------------------
    key = f"same-{uuid.uuid4().hex[:8]}"
    first = member.call(
        "POST", f"/api/agent-workspaces/{workspace_id}/operations", {"operation_type": "list", "idempotency_key": key}
    )[1]["operation"]
    second = member.call(
        "POST", f"/api/agent-workspaces/{workspace_id}/operations", {"operation_type": "list", "idempotency_key": key}
    )[1]["operation"]
    conflict = member.call(
        "POST",
        f"/api/agent-workspaces/{workspace_id}/operations",
        {"operation_type": "mkdir", "relative_path": "other", "idempotency_key": key},
    )
    check(
        "同 key 同内容 → 同一条；同 key 不同内容 → 409",
        first["id"] == second["id"] and conflict[0] == 409,
        f"{first['id'][:8]} / {conflict[0]}",
    )
    audit = member.call("GET", f"/api/agent-workspaces/{workspace_id}/audit")[1]["events"]
    actions = {item["action"] for item in audit}
    check("审计记下了入队与完成", any(item.startswith("queue:") for item in actions) and any(item.startswith("complete:") for item in actions), str(sorted(actions)[:6]))
    check("审计里没有路径明文", "假设.md" not in json.dumps(audit, ensure_ascii=False))

    failed = [item for item in checks if not item["passed"]]
    print(json.dumps({"checks": checks, "passed": len(checks) - len(failed), "failed": len(failed), "dir": str(root)}, ensure_ascii=False, indent=2))
    server.should_exit = True
    if options.keep:
        print(f"临时目录保留在 {root}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())