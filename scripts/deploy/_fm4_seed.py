"""FM-4 浏览器验收用的临时环境：真 API + 真工作区 + 真 Worker，供页面连。

不碰开发库也不碰线上：临时库 + 临时工作区目录，跑完由调用方清理。
用途只有一个——让 `/drive` 页面在浏览器里能真的看到一个 Agent 工作区，并且操作真的被执行。

用法：`python -X utf8 scripts/deploy/_fm4_seed.py --port 8010 [--workspace DIR] [--worker-seconds 600]`
"""

from __future__ import annotations

import argparse
import json
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
from app import workspace_files  # noqa: E402
from app.contracts import AgentRegister, DevicePairingCreate, DeviceProjectGrantCreate, SessionCreate  # noqa: E402
from app.object_store import create_object_store  # noqa: E402
from app.store import DEV_ORG_ID, Store  # noqa: E402
from device_test_support import registration_request  # noqa: E402
from file_worker import FileWorkerConfig, WorkspaceFileWorker  # noqa: E402


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def main() -> int:
    parser = argparse.ArgumentParser(description="FM-4 浏览器验收环境")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--root", default=None, help="临时库/对象目录（默认系统临时目录）")
    parser.add_argument("--worker-seconds", type=float, default=900.0)
    options = parser.parse_args()

    root = Path(options.root or tempfile.mkdtemp(prefix="fm4-seed-"))
    workspace_root = Path(options.workspace) if options.workspace else root / "ws"
    workspace_root.mkdir(parents=True, exist_ok=True)
    (workspace_root / "papers").mkdir(exist_ok=True)
    (workspace_root / "papers" / "假设.md").write_text("# 模型假设\n- 风光互补\n", encoding="utf-8")
    (workspace_root / "README.md").write_text("# 工作区\n这是 FM-4 验收用的临时工作区。\n", encoding="utf-8")
    (workspace_root / ".git").mkdir(exist_ok=True)

    store = Store(root / "platform.db", object_store=create_object_store(root / "objects"))
    api_main.store = store
    workspace_files.ensure_schema(store)
    project = store.list_projects()[0]

    store.register_agent(AgentRegister(agent_id="agent-fm4", display_name="FM-4 桌面机", owner_member_id="member-001"))
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    pairing = store.create_device_pairing(DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), "member-001")
    store.register_device(registration_request(pairing, Ed25519PrivateKey.generate(), "agent-fm4", "device-fm4", device_name="FM-4 机器"))
    credential = store.create_device_project_grant(project.id, DeviceProjectGrantCreate(device_id="device-fm4"), "member-001")

    api = f"http://127.0.0.1:{options.port}"
    server = uvicorn.Server(uvicorn.Config(api_main.app, host="127.0.0.1", port=options.port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(150):
        try:
            urllib.request.urlopen(f"{api}/api/organizations", timeout=2).close()
            break
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)

    identity = workspace_files.workspace_identity_for(str(workspace_root))
    request = urllib.request.Request(
        f"{api}/api/agent/workspaces/register",
        data=json.dumps(
            {
                "display_name": "本机工作区（FM-4）",
                "workspace_identity": identity,
                "project_id": str(project.id),
                "kind": "desktop",
                "protected_paths": ["secret-notes"],
            }
        ).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Agent-Id": "agent-fm4",
            "X-Project-Id": str(project.id),
            "X-Project-Capability-Token": credential.project_token,
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        workspace = json.loads(response.read())["workspace"]

    worker = WorkspaceFileWorker(
        FileWorkerConfig(
            url=api,
            project_token=credential.project_token,
            agent_id="agent-fm4",
            project_id=str(project.id),
            workspace=workspace_root,
            workspace_id=workspace["id"],
            protected_paths=["secret-notes"],
        ),
        log=lambda message: None,
    )

    def loop() -> None:
        deadline = time.time() + options.worker_seconds
        while time.time() < deadline:
            try:
                worker.poll_once()
            except Exception:  # noqa: BLE001 - 验收环境里循环不能因一次失败退出
                pass
            time.sleep(1.0)

    threading.Thread(target=loop, daemon=True).start()
    token = store.create_session(SessionCreate(member_id="member-001", expires_in_seconds=7200)).token
    print(
        json.dumps(
            {
                "api": api,
                "workspace_id": workspace["id"],
                "workspace_root": str(workspace_root),
                "token": token,
                "agent_id": "agent-fm4",
                "tmp_dir": str(root),
            },
            ensure_ascii=False,
        )
    )
    print("环境就绪（Ctrl+C 结束）", flush=True)
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())