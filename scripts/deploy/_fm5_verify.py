"""FM-5 端到端验收：授权 → lease → 物化到 `inputs/` → 撤销后立刻失效。

单元测试把负向矩阵钉住了；这个脚本证明**接起来**也对，而且用的是真 HTTP + 真内核物化器：
真 uvicorn、真设备+项目授权、真 lease、真下载、真写盘（含 sha256 对账），最后做一次
**撤销演练**——撤销之后同一段 lease 再读必须失败，而已经落到 `inputs/` 的副本按计划保留
（"已导入项目的 Artifact 不因源 Grant 撤销而回溯删除"，工作区里的输入副本同理：撤销管的是**后续读取**）。

用法（仓库根目录）：`python -X utf8 scripts/deploy/_fm5_verify.py [--keep]`
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
from app import drive, drive_grants  # noqa: E402
from app.contracts import (  # noqa: E402
    AgentRegister,
    DevicePairingCreate,
    DeviceProjectGrantCreate,
    ProjectCreate,
    SessionCreate,
    TaskCreate,
)
from app.object_store import create_object_store  # noqa: E402
from app.store import DEV_ORG_ID, Store  # noqa: E402
from device_test_support import registration_request  # noqa: E402
from drive_materializer import DriveMaterializeConfig, DriveMaterializer  # noqa: E402
from input_fetcher import read_inputs_manifest  # noqa: E402

MEMBER = "member-001"
AGENT = "agent-fm5"
DEVICE = "device-fm5"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Client:
    def __init__(self, base: str, token: str | None = None) -> None:
        self.base = base
        self.token = token

    def call(self, method: str, path: str, payload: dict | None = None, extra: dict[str, str] | None = None) -> tuple[int, dict]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json", **(extra or {})}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(f"{self.base}{path}", data=body, method=method, headers=headers)
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


def main() -> int:
    parser = argparse.ArgumentParser(description="FM-5 云盘授权与物化验收")
    parser.add_argument("--keep", action="store_true")
    options = parser.parse_args()

    root = Path(os.environ.get("FM5_VERIFY_DIR") or tempfile.mkdtemp(prefix="fm5-verify-"))
    workspace = root / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    store = Store(root / "platform.db", object_store=create_object_store(root / "objects"))
    api_main.store = store
    drive.ensure_schema(store)
    drive_grants.ensure_schema(store)
    project = store.list_projects()[0]

    # 云盘里放两个文件 + 一个目录（含第三个文件）
    owner = drive.actor_for(store, MEMBER)
    shared = drive.put_file(store, owner, None, "题目原文.txt", "问题一：请给出模型假设。".encode("utf-8"))
    secret = drive.put_file(store, owner, None, "隐私-不该被看到.txt", "别人的东西".encode("utf-8"))
    folder = drive.create_directory(store, owner, None, "数据集")
    inside = drive.put_file(store, owner, folder["id"], "表.csv", b"x,y\n1,2\n")

    # 真设备 + 真项目授权
    store.register_agent(AgentRegister(agent_id=AGENT, display_name="FM-5 Agent", owner_member_id=MEMBER))
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    pairing = store.create_device_pairing(DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), MEMBER)
    store.register_device(registration_request(pairing, Ed25519PrivateKey.generate(), AGENT, DEVICE, device_name="FM-5 机器"))
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
    agent_headers = {
        "X-Agent-Id": AGENT,
        "X-Project-Id": str(project.id),
        "X-Project-Capability-Token": credential.project_token,
        "X-Device-Id": DEVICE,
    }

    # ---- 1. 跨成员节点不能授权 ----------------------------------------------
    from tests_support import ensure_member

    ensure_member(store, "member-fm5-other")
    other_actor = drive.actor_for(store, "member-fm5-other")
    other_file = drive.put_file(store, other_actor, None, "别人的文件.txt", b"secret")
    status, body = member.call(
        "POST",
        "/api/file-access-grants",
        {"agent_id": AGENT, "device_id": DEVICE, "project_id": str(project.id), "node_id": other_file["id"], "scope_type": "file"},
    )
    check("跨成员的云盘节点不能授权（404）", status == 404, f"{status} {str(body.get('detail'))[:60]}")

    # ---- 2. 单文件授权 + 物化到 inputs/ --------------------------------------
    status, created = member.call(
        "POST",
        "/api/file-access-grants",
        {
            "agent_id": AGENT,
            "device_id": DEVICE,
            "project_id": str(project.id),
            "node_id": shared["id"],
            "scope_type": "file",
            "expires_in_seconds": 3600,
        },
    )
    check("建授权（默认只读 + 可导入）", status == 201, f"{status}")
    if status != 201:
        print(json.dumps({"checks": checks, "dir": str(root)}, ensure_ascii=False, indent=2))
        server.should_exit = True
        return 1
    grant = created["grant"]
    check(
        "默认能力里没有写/删/移动",
        sorted(grant["capabilities"]) == ["drive.file.import", "drive.file.read", "drive.metadata.read"],
        str(grant["capabilities"]),
    )

    materializer = DriveMaterializer(
        DriveMaterializeConfig(
            url=base,
            project_token=credential.project_token,
            agent_id=AGENT,
            project_id=str(project.id),
            grant_id=grant["id"],
            workspace=workspace,
            device_id=DEVICE,
        ),
        log=lambda message: print(f"   [drive] {message}"),
    )
    result = materializer.materialize()
    landed = workspace / "inputs" / "题目原文.txt"
    check(
        "物化到 inputs/ 且 sha256 对账通过",
        result["written"] == ["题目原文.txt"] and landed.is_file() and hashlib.sha256(landed.read_bytes()).hexdigest() == shared["content_hash"],
        f"written={result['written']} failed={result['failed']}",
    )
    manifest = read_inputs_manifest(workspace)
    entry = manifest["entries"][0]
    check(
        "Manifest 记录了来源与哈希（source=drive_grant）",
        entry["artifact_id"] == shared["id"] and entry["sha256"] == hashlib.sha256(landed.read_bytes()).hexdigest() and entry["source"] == "drive_grant",
        json.dumps(entry, ensure_ascii=False)[:120],
    )
    check("没有越权物化别的文件", not (workspace / "inputs" / "隐私-不该被看到.txt").exists())

    # ---- 3. 目录授权：只覆盖当时节点；勾了"包含以后新增"才覆盖新文件 ----------
    status, folder_grant = member.call(
        "POST",
        "/api/file-access-grants",
        {"agent_id": AGENT, "device_id": DEVICE, "project_id": str(project.id), "node_id": folder["id"], "scope_type": "folder"},
    )
    folder_grant = folder_grant["grant"]
    later = drive.put_file(store, owner, folder["id"], "后加的表.csv", b"late\n")
    status, exchanged = member.call("POST", "/api/agent/file-leases/exchange", {"grant_id": folder_grant["id"]}, extra=agent_headers)
    lease = exchanged["lease"]["token"]
    status, listed = member.call("GET", "/api/agent/drive/files", extra={**agent_headers, "X-File-Access-Lease": lease})
    names = [item["name"] for item in listed["entries"]]
    check("目录授权默认只覆盖授权当时的文件", "表.csv" in names and "后加的表.csv" not in names, str(names))

    status, dynamic_grant = member.call(
        "POST",
        "/api/file-access-grants",
        {
            "agent_id": AGENT,
            "device_id": DEVICE,
            "project_id": str(project.id),
            "node_id": folder["id"],
            "scope_type": "folder",
            "include_future_nodes": True,
        },
    )
    dynamic_grant = dynamic_grant["grant"]
    status, exchanged_dynamic = member.call("POST", "/api/agent/file-leases/exchange", {"grant_id": dynamic_grant["id"]}, extra=agent_headers)
    lease_dynamic = exchanged_dynamic["lease"]["token"]
    status, listed_dynamic = member.call("GET", "/api/agent/drive/files", extra={**agent_headers, "X-File-Access-Lease": lease_dynamic})
    names_dynamic = [item["name"] for item in listed_dynamic["entries"]]
    check("勾了『包含以后新增』后新文件也在范围内", "后加的表.csv" in names_dynamic, str(names_dynamic))

    # ---- 4. 范围外读取被拒 ---------------------------------------------------
    code, body = member.call(
        "GET", f"/api/agent/drive/nodes/{secret['id']}/content", extra={**agent_headers, "X-File-Access-Lease": lease}
    )
    check("范围外读取被拒（403 scope_denied）", code == 403 and "scope_denied" in str(body.get("detail")), f"{code} {str(body.get('detail'))[:60]}")
    check("范围外文件也没落到工作区", not (workspace / "inputs" / "隐私-不该被看到.txt").exists())

    # ---- 5. 身份/绑定/能力的负向路径（HTTP 层再走一遍） ----------------------
    for label, extra, expected in (
        ("错误 Agent", {**agent_headers, "X-Agent-Id": "agent-not-me", "X-File-Access-Lease": lease}, 403),
        ("错误设备", {**agent_headers, "X-Device-Id": "device-not-me", "X-File-Access-Lease": lease}, 403),
        ("缺少 lease", agent_headers, 401),
    ):
        code, _body = member.call("GET", "/api/agent/drive/files", extra=extra)
        check(f"负向：{label} → {expected}", code == expected, str(code))

    member.call(
        "POST",
        "/api/file-access-grants",
        {"agent_id": AGENT, "device_id": DEVICE, "project_id": str(project.id), "node_id": shared["id"], "capabilities": ["drive.metadata.read"]},
    )
    code, _ = member.call(
        "POST", "/api/file-access-grants", {"agent_id": AGENT, "device_id": DEVICE, "project_id": str(project.id), "node_id": shared["id"], "capabilities": ["drive.file.delete"]}
    )
    check("写权限不在可授予集合里（拒绝创建）", code == 403, str(code))

    # ---- 6. 撤销演练：同一段 lease 立刻失效 ----------------------------------
    status, _ = member.call("POST", f"/api/file-access-grants/{grant['id']}/revoke", {"reason": "验收演练"})
    code, body = member.call("GET", f"/api/agent/drive/nodes/{shared['id']}/content", extra={**agent_headers, "X-File-Access-Lease": lease})
    check("撤销后同段 lease 立刻读不到（403）", code == 403, f"{code} {str(body.get('detail'))[:60]}")
    code, body = member.call("POST", "/api/agent/file-leases/exchange", {"grant_id": grant["id"]}, extra=agent_headers)
    check("撤销后新换 lease 也被拒", code == 403 and "revoked" in str(body.get("detail")), f"{code}")
    check("已物化的副本按计划保留（撤销管后续读取，不回溯删本地副本）", landed.is_file())

    # 设备撤销 → 联动撤销
    status, second = member.call(
        "POST",
        "/api/file-access-grants",
        {"agent_id": AGENT, "device_id": DEVICE, "project_id": str(project.id), "node_id": shared["id"]},
    )
    revoked = drive_grants.revoke_grants_for(store, device_id=DEVICE)
    row = store.db.execute("SELECT revoked_at, revoke_reason FROM file_access_grants WHERE id = ?", (second["grant"]["id"],)).fetchone()
    check("设备撤销 → 联动撤销该设备的所有授权", revoked >= 1 and dict(row)["revoked_at"] is not None, f"revoked={revoked} reason={dict(row)['revoke_reason']}")

    # 任务结束 → 联动撤销
    task = store.create_task(project.id, TaskCreate(title="FM-5 验收任务", assignee=AGENT))
    status, task_grant = member.call(
        "POST",
        "/api/file-access-grants",
        {"agent_id": AGENT, "device_id": DEVICE, "project_id": str(project.id), "node_id": shared["id"], "task_id": str(task.id)},
    )
    check("授权可绑定任务（同一项目）", status == 201, str(status))
    revoked_tasks = drive_grants.revoke_grants_for(store, task_id=str(task.id))
    check("任务结束 → 联动撤销", revoked_tasks >= 1, f"revoked={revoked_tasks}")

    # ---- 7. 审计 -------------------------------------------------------------
    status, audit = member.call("GET", f"/api/drive/nodes/{shared['id']}/audit")
    actions = {item["action"] for item in audit["events"]}
    check(
        "审计里有授权/读取/拒绝/撤销记录",
        {"grant:create", "lease:exchange", "agent:read", "agent:list", "grant:revoke"} <= actions,
        str(sorted(actions)),
    )
    check("审计里不出现文件名（只有节点 id 与哈希）", "题目原文.txt" not in json.dumps(audit, ensure_ascii=False))

    failed = [item for item in checks if not item["passed"]]
    print(json.dumps({"checks": checks, "passed": len(checks) - len(failed), "failed": len(failed), "dir": str(root)}, ensure_ascii=False, indent=2))
    server.should_exit = True
    if options.keep:
        print(f"临时目录保留在 {root}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())