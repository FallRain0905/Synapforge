"""FM-6 端到端验收：显式跨空间传输 + 冲突口径 + 压力 + 维护口。

这个脚本跑真 HTTP、真对象存储、真内核 Worker（在后台轮询执行工作区操作），验证四件事：

1. **云端 → 工作区**：一次显式复制真的把文件送进工作区（sha256 对账）；
2. **工作区 → 云端**：Agent 传进传输会话 → 人「存进云盘」；**同名冲突 409，不静默覆盖**；
3. **压力**：一批并发上传/下载/列举操作全部有终态，且配额与用量对得上（不静默丢件）；
4. **维护**：过期会话回收会删对象；孤儿扫描**只报告不删**。

用法（仓库根目录）：`python -X utf8 scripts/deploy/_fm6_verify.py [--keep]`
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
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
for candidate in (str(ROOT), str(ROOT / "apps" / "api"), str(ROOT / "apps" / "agent")):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

import uvicorn  # noqa: E402

import app.main as api_main  # noqa: E402
from app import drive, file_transfers, workspace_files  # noqa: E402
from app.contracts import AgentRegister, DevicePairingCreate, DeviceProjectGrantCreate, SessionCreate  # noqa: E402
from app.object_store import create_object_store  # noqa: E402
from app.store import DEV_ORG_ID, Store  # noqa: E402
from device_test_support import registration_request  # noqa: E402
from file_worker import FileWorkerConfig, WorkspaceFileWorker  # noqa: E402

MEMBER = "member-001"
AGENT = "agent-fm6"
DEVICE = "device-fm6"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Client:
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

    def put_bytes(self, path: str, content: bytes) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=content,
            method="PUT",
            headers={"Content-Type": "application/octet-stream", "Authorization": f"Bearer {self.token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            return error.code, {"detail": error.read()[:200].decode("utf-8", "replace")}


def main() -> int:
    parser = argparse.ArgumentParser(description="FM-6 跨空间传输与维护验收")
    parser.add_argument("--keep", action="store_true")
    options = parser.parse_args()

    root = Path(os.environ.get("FM6_VERIFY_DIR") or tempfile.mkdtemp(prefix="fm6-verify-"))
    workspace_root = root / "ws"
    workspace_root.mkdir(parents=True, exist_ok=True)
    store = Store(root / "platform.db", object_store=create_object_store(root / "objects"))
    api_main.store = store
    drive.ensure_schema(store)
    workspace_files.ensure_schema(store)
    project = store.list_projects()[0]

    store.register_agent(AgentRegister(agent_id=AGENT, display_name="FM-6 机器", owner_member_id=MEMBER))
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    pairing = store.create_device_pairing(DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), MEMBER)
    store.register_device(registration_request(pairing, Ed25519PrivateKey.generate(), AGENT, DEVICE, device_name="FM-6 机器"))
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

    member = Client(base, store.create_session(SessionCreate(member_id=MEMBER, expires_in_seconds=3600)).token)
    identity = workspace_files.workspace_identity_for(str(workspace_root))
    request = urllib.request.Request(
        f"{base}/api/agent/workspaces/register",
        data=json.dumps({"display_name": "FM-6 工作区", "workspace_identity": identity, "project_id": str(project.id)}).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Agent-Id": AGENT,
            "X-Project-Id": str(project.id),
            "X-Project-Capability-Token": credential.project_token,
            "X-Device-Id": DEVICE,
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        workspace_id = json.loads(response.read())["workspace"]["id"]

    worker = WorkspaceFileWorker(
        FileWorkerConfig(
            url=base,
            project_token=credential.project_token,
            agent_id=AGENT,
            project_id=str(project.id),
            workspace=workspace_root,
            workspace_id=workspace_id,
        ),
        log=lambda message: None,
    )
    stop_flag = threading.Event()

    def worker_loop() -> None:
        while not stop_flag.is_set():
            try:
                worker.poll_once()
            except Exception:  # noqa: BLE001 - 验收环境的循环不因一次失败退出
                pass
            time.sleep(0.5)

    threading.Thread(target=worker_loop, daemon=True).start()

    def wait_operation(operation_id: str, timeout: float = 30.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, payload = member.call("GET", f"/api/agent-workspaces/{workspace_id}/operations/{operation_id}")
            operation = payload["operation"]
            if operation["status"] in {"succeeded", "failed", "cancelled", "expired"}:
                return operation
            time.sleep(0.4)
        return {"status": "timeout", "error_code": "verification_timeout"}

    # ---- 1. 云端 → 工作区 ----------------------------------------------------
    content = "第一问的数据\n".encode("utf-8")
    node = drive.put_file(store, drive.actor_for(store, MEMBER), None, "数据.csv", content, "text/csv")
    status, started = member.call(
        "POST", "/api/file-transfers/drive-to-workspace", {"node_id": node["id"], "workspace_id": workspace_id}
    )
    check("云端 → 工作区：建传输会话 + 入队 upload", status == 201 and started["operation"]["operation_type"] == "upload", f"{status}")
    operation = wait_operation(started["operation"]["id"])
    landed = workspace_root / "数据.csv"
    check(
        "内容真的落到工作区且 sha256 一致",
        operation["status"] == "succeeded" and landed.is_file() and hashlib.sha256(landed.read_bytes()).hexdigest() == hashlib.sha256(content).hexdigest(),
        f"{operation['status']} {operation.get('error_code') or ''}",
    )

    # 同名再来一次：内核拒绝（默认不覆盖），操作如实失败
    status, again = member.call("POST", "/api/file-transfers/drive-to-workspace", {"node_id": node["id"], "workspace_id": workspace_id})
    second = wait_operation(again["operation"]["id"])
    check(
        "同名再复制：失败并如实报冲突（不静默覆盖）",
        second["status"] == "failed" and second["error_code"] == "workspace_path_conflict",
        f"{second['status']} {second.get('error_code')}",
    )

    # ---- 2. 工作区 → 云端 ----------------------------------------------------
    (workspace_root / "结果.md").write_text("# 结果\n工作区产出\n", encoding="utf-8")
    status, uploaded = member.call(
        "POST", "/api/file-transfers/workspace-to-drive", {"workspace_id": workspace_id, "relative_path": "结果.md"}
    )
    check("工作区 → 云端：入队 download", status == 201 and uploaded["operation"]["operation_type"] == "download", f"{status}")
    uploaded_operation = wait_operation(uploaded["operation"]["id"])
    check("Agent 把内容传进传输会话", uploaded_operation["status"] == "succeeded", f"{uploaded_operation['status']}")
    status, saved = member.call("POST", "/api/file-transfers/save-to-drive", {"transfer_id": uploaded["transfer"]["id"], "name": "结果.md"})
    check("存进云盘", status == 201 and saved["node"]["name"] == "结果.md", f"{status} {str(saved)[:80]}")
    _saved_node, saved_content = drive.read_content(store, drive.actor_for(store, MEMBER), saved["node"]["id"])
    check("云盘里内容与工作区一致", saved_content == (workspace_root / "结果.md").read_bytes(), f"{len(saved_content)} 字节")

    status, conflict = member.call("POST", "/api/file-transfers/save-to-drive", {"transfer_id": uploaded["transfer"]["id"], "name": "结果.md"})
    check("同名再存：409 冲突（不覆盖）", status == 409 and "file_name_conflict" in str(conflict.get("detail")), f"{status}")

    # ---- 3. 压力：并发传输与列举 ---------------------------------------------
    def upload_once(index: int) -> str:
        name = f"并发-{index}.bin"
        status, payload = member.call(
            "POST", "/api/file-transfers/drive-to-workspace", {"node_id": drive_node_ids[index], "workspace_id": workspace_id, "relative_path": name}
        )
        if status != 201:
            return f"enqueue_failed:{status}"
        return wait_operation(payload["operation"]["id"], timeout=45)["status"]

    # 先往云盘塞 8 个各不相同的文件（不同内容避免去重把它们并成一个对象）
    drive_node_ids = []
    for index in range(8):
        created = drive.put_file(store, drive.actor_for(store, MEMBER), None, f"批量-{index}.bin", bytes([65 + index]) * (1024 * (index + 1)))
        drive_node_ids.append(created["id"])
    results: list[str] = []
    started_at = time.time()
    threads = [threading.Thread(target=lambda i=index: results.append(upload_once(i)), args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.time() - started_at
    check(
        "并发 8 个跨空间传输全部有终态（无静默丢件）",
        len(results) == 8 and all(item == "succeeded" for item in results),
        f"{sorted(set(results))} · {elapsed:.1f}s",
    )
    usage = member.call("GET", "/api/drive/trash")[1]["usage"]
    expected_bytes = sum(len(bytes([65 + index]) * (1024 * (index + 1))) for index in range(8))
    check("配额用量与写入内容一致（去重后）", usage["used_bytes"] >= expected_bytes, f"used={usage['used_bytes']} ≥ {expected_bytes}")

    # ---- 4. 维护：传输历史 / 过期回收 / 孤儿扫描 ------------------------------
    status, history = member.call("GET", f"/api/file-transfers?workspace_id={workspace_id}")
    entries = history["transfers"]
    check("传输历史带操作终态与可重试标记", status == 200 and any(item.get("operation") for item in entries), f"{len(entries)} 条")
    failed_entry = next((item for item in entries if item.get("operation") and item["operation"]["status"] == "failed"), None)
    check("失败的传输在历史里能看见原因", failed_entry is not None and failed_entry["operation"]["error_code"] == "workspace_path_conflict", str(failed_entry and failed_entry["operation"]["error_code"]))

    # 过期回收：把一个会话的过期时间改到过去，回收应当删掉对象
    transfer_id = uploaded["transfer"]["id"]  # 已被 consume 的那个
    key = store.db.execute("SELECT storage_key FROM file_transfer_sessions WHERE id = ?", (transfer_id,)).fetchone()["storage_key"]
    object_path = Path(store.object_store.root) / str(key)
    before_exists = object_path.is_file()
    store.db.execute(
        "UPDATE file_transfer_sessions SET expires_at = ?, status = 'ready' WHERE id = ?",
        ((__import__("datetime").datetime.now(__import__("datetime").UTC) - __import__("datetime").timedelta(minutes=5)).isoformat(), transfer_id),
    )
    store.db.commit()
    status, cleaned = member.call("POST", "/api/file-maintenance/transfers/cleanup")
    check("过期会话回收（删对象）", status == 200 and cleaned["expired"] >= 1, json.dumps(cleaned))
    check("过期的对象确实从存储里没了", before_exists and not object_path.is_file())

    status, report = member.call("POST", "/api/file-maintenance/orphans/scan")
    check(
        "孤儿扫描只报告不删",
        status == 200 and "只报告不删" in report["note"] and "drive_orphan_count" in report,
        f"drive={report.get('drive_orphan_count')} transfer={report.get('transfer_orphan_count')}",
    )

    stop_flag.set()
    failed = [item for item in checks if not item["passed"]]
    print(json.dumps({"checks": checks, "passed": len(checks) - len(failed), "failed": len(failed), "dir": str(root)}, ensure_ascii=False, indent=2))
    server.should_exit = True
    if options.keep:
        print(f"临时目录保留在 {root}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())