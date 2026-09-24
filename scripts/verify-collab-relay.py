"""阶段 7 协作：真实 WebSocket 服务上的双客户端中继端到端验证。

启动 API 后运行：两个客户端连接同一项目 WS，一方发送 document.update 帧，
另一方必须收到；发送者不应收到回声；非法帧返回 collaboration.error。
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import urllib.request

import websockets


async def main() -> int:
    with urllib.request.urlopen("http://127.0.0.1:8000/api/projects", timeout=10) as response:
        projects = json.loads(response.read().decode("utf-8"))
    project_id = projects[0]["id"]
    url = f"ws://127.0.0.1:8000/ws/projects/{project_id}"

    alice = await websockets.connect(url)
    bob = await websockets.connect(url)
    try:
        await alice.recv()  # connected 帧
        await bob.recv()

        update = base64.b64encode(b"crdt-update-payload").decode("ascii")
        await alice.send(json.dumps({"type": "document.update", "document_id": "doc-1", "update": update}))

        received = json.loads(await asyncio.wait_for(bob.recv(), timeout=5))
        relayed = received.get("type") == "document.update" and received.get("update") == update
        print("bob received relayed update:", relayed, "| document:", received.get("document_id"))

        try:
            echo = await asyncio.wait_for(alice.recv(), timeout=1)
            echoed = True
            print("alice unexpectedly received:", echo[:80])
        except asyncio.TimeoutError:
            echoed = False
            print("no echo to sender: True")

        await alice.send(json.dumps({"type": "chat.message", "document_id": "doc-1"}))
        error_frame = json.loads(await asyncio.wait_for(alice.recv(), timeout=5))
        print("invalid frame rejected:", error_frame.get("type") == "collaboration.error", error_frame.get("code"))

        await bob.send(json.dumps({"type": "document.presence", "document_id": "doc-1", "peer": "member-002", "state": "active"}))
        presence = json.loads(await asyncio.wait_for(alice.recv(), timeout=5))
        print("presence relayed:", presence.get("type") == "document.presence", presence.get("peer"))

        ok = relayed and not echoed and error_frame.get("type") == "collaboration.error" and presence.get("type") == "document.presence"
        print("RELAY_E2E_OK" if ok else "RELAY_E2E_FAILED")
        return 0 if ok else 1
    finally:
        await alice.close()
        await bob.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))