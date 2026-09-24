"""FM-6 端到端验收：断点续传（真 API + 真内核），含"某片先失败再重试"的演练。

三件事必须真的发生，不接受"接口返回 200"当证据：

1. **平台侧分片**：按片上传、跳过一片、读续传游标看到缺哪片、补上、收口 → **内容逐字节一致**；
2. **内核侧分片 + 重试**：工作区里放一个大文件，让内核分片上传，**人为让第 2 片的第一次失败**，
   内核应当自动重试成功并把内容原样传上去；随后"存进云盘"并对账云盘那份的哈希；
3. **失败要如实**：故意让最后一片一直失败 → 操作必须报 `workspace_transfer_part_failed`，
   **不能**收口成"成功"。

用法（仓库根目录）：`python -X utf8 scripts/deploy/_fm6_resume_verify.py [--keep]`
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
AGENT = "agent-resume"
DEVICE = "device-resume"
PART_BYTES = 256 * 1024  # 分片大小：验收用小片，别真造几十 MB 数据


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


class Client:
    def __init__(self, base: str, token: str) -> None:
        self.base = base
        self.token = token

    def call(self, method: str, path: str, payload: dict | None = None, extra: dict[str, str] | None = None) -> tuple[int, dict]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json", **(extra or {})}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return self._send(urllib.request.Request(f"{self.base}{path}", data=body, method=method, headers=headers))

    def put_part(self, transfer_id: str, number: int, content: bytes) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"{self.base}/api/workspace-transfers/{transfer_id}/parts/{number}",
            data=content,
            method="PUT",
            headers={
                "Content-Type": "application/octet-stream",
                "X-Part-SHA256": hashlib.sha256(content).hexdigest(),
                **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
            },
        )
        return self._send(request)

    def get_bytes(self, path: str) -> tuple[int, bytes, dict]:
        request = urllib.request.Request(f"{self.base}{path}", headers={"Authorization": f"Bearer {self.token}"})
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


def main() -> int:
    parser = argparse.ArgumentParser(description="FM-6 断点续传验收")
    parser.add_argument("--keep", action="store_true")
    options = parser.parse_args()

    root = Path(os.environ.get("FM6R_VERIFY_DIR") or tempfile.mkdtemp(prefix="fm6-resume-"))
    workspace_root = root / "ws"
    workspace_root.mkdir(parents=True, exist_ok=True)
    store = Store(root / "platform.db", object_store=create_object_store(root / "objects"))
    api_main.store = store
    drive.ensure_schema(store)
    workspace_files.ensure_schema(store)
    project = store.list_projects()[0]

    store.register_agent(AgentRegister(agent_id=AGENT, display_name="续传验收机", owner_member_id=MEMBER))
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    pairing = store.create_device_pairing(DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), MEMBER)
    store.register_device(registration_request(pairing, Ed25519PrivateKey.generate(), AGENT, DEVICE, device_name="续传验收机"))
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
        data=json.dumps({"display_name": "续传验收工作区", "workspace_identity": identity, "project_id": str(project.id)}).encode("utf-8"),
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

    # ---- 1. 平台侧分片 + 续传游标 -------------------------------------------
    payload = bytes(range(256)) * (3 * 1024)  # 768KB → 3 片（256KB 一片）
    digest = hashlib.sha256(payload).hexdigest()
    status, created = member.call(
        "POST",
        "/api/workspace-transfers",
        {
            "source_type": "workspace",
            "target_type": "drive",
            "workspace_id": workspace_id,
            "expected_size": len(payload),
            "expected_hash": digest,
        },
    )
    transfer_id = created["transfer"]["id"]
    chunks = [payload[offset : offset + PART_BYTES] for offset in range(0, len(payload), PART_BYTES)]
    status, _ = member.put_part(transfer_id, 1, chunks[0])
    first_ok = status == 200
    status, _ = member.put_part(transfer_id, 3, chunks[2])  # 故意跳过第 2 片
    third_ok = status == 200
    status, cursor = member.call("GET", f"/api/workspace-transfers/{transfer_id}/parts")
    check(
        "跳过一片上传：游标指出还缺第 2 片",
        first_ok and third_ok and cursor["received_parts"] == [1, 3] and cursor["missing_parts"] == [2],
        f"received={cursor.get('received_parts')} missing={cursor.get('missing_parts')}",
    )
    status, blocked = member.call("POST", f"/api/workspace-transfers/{transfer_id}/complete")
    check("片不齐不许收口（明确报缺哪片）", status == 400 and "parts_missing" in str(blocked.get("detail")), f"{status} {str(blocked.get('detail'))[:60]}")
    member.put_part(transfer_id, 2, chunks[1])
    status, completed = member.call("POST", f"/api/workspace-transfers/{transfer_id}/complete")
    check("补上缺片后收口成功", status == 200 and completed["transfer"]["status"] == "ready", f"{status} {completed.get('transfer', {}).get('status')}")
    status, served, headers = member.get_bytes(f"/api/workspace-transfers/{transfer_id}/content")
    check(
        "拼出来的对象逐字节等于原内容（含 sha256）",
        status == 200 and served == payload and header(headers, "X-Transfer-SHA256") == digest,
        f"{len(served)} 字节",
    )

    # ---- 2. 内核侧分片 + 某片先失败再重试 ------------------------------------
    big_content = bytes([7]) * (PART_BYTES * 3 + 4096)  # 3 片多一点
    (workspace_root / "大文件.bin").write_bytes(big_content)
    flaky_state = {"failed_once": False}

    def flaky_http(method: str, url: str, *, headers: dict[str, str], body: bytes | None = None, timeout: float = 60.0):
        """模拟网络抖动：第 2 片的**第一次** PUT 直接失败，之后正常。"""

        if method == "PUT" and "/parts/2" in url and not flaky_state["failed_once"]:
            flaky_state["failed_once"] = True
            return 500, {"detail": "flaky_network (验收注入)"}
        return WorkspaceFileWorker._default_http(method, url, headers=headers, body=body, timeout=timeout)

    worker = WorkspaceFileWorker(
        FileWorkerConfig(
            url=base,
            project_token=credential.project_token,
            agent_id=AGENT,
            project_id=str(project.id),
            workspace=workspace_root,
            workspace_id=workspace_id,
            device_id=DEVICE,
            chunk_bytes=PART_BYTES,
            chunk_retries=3,
        ),
        http=flaky_http,
        log=lambda message: print(f"   [worker] {message}"),
    )

    status, started = member.call(
        "POST", "/api/file-transfers/workspace-to-drive", {"workspace_id": workspace_id, "relative_path": "大文件.bin"}
    )
    operation_id = started["operation"]["id"]
    deadline = time.time() + 30
    operation = started["operation"]
    while time.time() < deadline:
        operation = member.call("GET", f"/api/agent-workspaces/{workspace_id}/operations/{operation_id}")[1]["operation"]
        if operation["status"] in {"succeeded", "failed"}:
            break
        worker.poll_once()
        time.sleep(0.4)
    result = operation.get("result") or {}
    check(
        "内核分片上传（第 2 片先失败一次 → 自动重试成功）",
        operation["status"] == "succeeded" and result.get("transfer_mode") == "chunked" and result.get("parts") == 4,
        f"{operation['status']} mode={result.get('transfer_mode')} parts={result.get('parts')} resumed={result.get('resumed_parts')}",
    )
    check("注入的失败真的发生过（否则这条验收没意义）", flaky_state["failed_once"], str(flaky_state))
    status, saved = member.call(
        "POST", "/api/file-transfers/save-to-drive", {"transfer_id": started["transfer"]["id"], "name": "大文件.bin"}
    )
    check("存进云盘", status == 201, f"{status} {str(saved.get('detail'))[:60]}")
    if status == 201:
        _node, saved_content = drive.read_content(store, drive.actor_for(store, MEMBER), saved["node"]["id"])
        check(
            "云盘那份与工作区原文件逐字节一致",
            saved_content == big_content and hashlib.sha256(saved_content).hexdigest() == hashlib.sha256(big_content).hexdigest(),
            f"{len(saved_content)} 字节",
        )
        parts_rows = store.db.execute(
            "SELECT part_number, attempts FROM file_transfer_parts WHERE transfer_id = ? ORDER BY part_number",
            (started["transfer"]["id"],),
        ).fetchall()
        check(
            "四片都落在平台上（1..4 连续）",
            [int(row["part_number"]) for row in parts_rows] == [1, 2, 3, 4],
            str([int(row["part_number"]) for row in parts_rows]),
        )
        check(
            "客户端侧重试计数留痕（第 2 片重试过）",
            int((result.get("client_retries") or {}).get("2", 0)) >= 1,
            json.dumps(result.get("client_retries") or {}, ensure_ascii=False),
        )

    # ---- 3. 一直失败的片：必须如实报失败，不许收口 ---------------------------
    (workspace_root / "传不上去.bin").write_bytes(bytes([9]) * (PART_BYTES * 2))
    status, failing = member.call(
        "POST", "/api/file-transfers/workspace-to-drive", {"workspace_id": workspace_id, "relative_path": "传不上去.bin"}
    )

    def always_fail(method: str, url: str, *, headers: dict[str, str], body: bytes | None = None, timeout: float = 60.0):
        if method == "PUT" and "/parts/2" in url:
            return 500, {"detail": "always_broken (验收注入)"}
        return WorkspaceFileWorker._default_http(method, url, headers=headers, body=body, timeout=timeout)

    broken_worker = WorkspaceFileWorker(
        FileWorkerConfig(
            url=base,
            project_token=credential.project_token,
            agent_id=AGENT,
            project_id=str(project.id),
            workspace=workspace_root,
            workspace_id=workspace_id,
            device_id=DEVICE,
            chunk_bytes=PART_BYTES,
            chunk_retries=2,
        ),
        http=always_fail,
        log=lambda message: print(f"   [worker] {message}"),
    )
    deadline = time.time() + 30
    failed_operation = failing["operation"]
    while time.time() < deadline:
        failed_operation = member.call("GET", f"/api/agent-workspaces/{workspace_id}/operations/{failed_operation['id']}")[1]["operation"]
        if failed_operation["status"] in {"succeeded", "failed"}:
            break
        broken_worker.poll_once()
        time.sleep(0.4)
    check(
        "某片一直失败 → 操作如实失败（不收口、不假装成功）",
        failed_operation["status"] == "failed" and failed_operation["error_code"] == "workspace_transfer_part_failed",
        f"{failed_operation['status']} {failed_operation.get('error_code')}",
    )
    failure_rows = store.db.execute(
        "SELECT part_number, attempts FROM file_transfer_parts WHERE transfer_id = ? ORDER BY part_number",
        (failing["transfer"]["id"],),
    ).fetchall()
    # 平台侧只该有第 1 片（第 2 片从未到达平台），重试次数由内核如实回报
    check(
        "一直失败时平台只有第 1 片（没半成品被当成成功）",
        [int(row["part_number"]) for row in failure_rows] == [1],
        str([int(row["part_number"]) for row in failure_rows]),
    )
    check(
        "失败详情里写清重试了几次（== 配置的重试上限）",
        "retries=2" in str(failed_operation.get("error_message") or ""),
        str(failed_operation.get("error_message"))[:80],
    )

    failed = [item for item in checks if not item["passed"]]
    print(json.dumps({"checks": checks, "passed": len(checks) - len(failed), "failed": len(failed), "dir": str(root)}, ensure_ascii=False, indent=2))
    server.should_exit = True
    if options.keep:
        print(f"临时目录保留在 {root}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())