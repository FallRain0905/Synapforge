"""诊断三：把 outbox 里真实的 agent.event 重放给平台，打印错误码。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from agentd import _gateway_uri  # noqa: E402
from credential_store import WindowsCredentialManager, device_token_target  # noqa: E402

API = "http://127.0.0.1:8010"
DEVICE_ID = "device-fallrain"
AGENT_ID = "agent-fallrain"
SESSION_ID = f"session-{DEVICE_ID}"


async def main() -> int:
    source = Path(os.environ["LOCALAPPDATA"]) / "MathAgentPlatform" / "agentd.db"
    temp = Path(tempfile.mkdtemp()) / "agentd.db"
    shutil.copy2(source, temp)
    connection = sqlite3.connect(f"file:{temp}?mode=ro", uri=True)
    row = connection.execute(
        "SELECT sequence, payload FROM gateway_outbox WHERE message_type='agent.event' ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    connection.close()
    if row is None:
        print("no agent.event in outbox")
        return 1
    sequence, payload = row
    body = json.loads(payload)
    print("重放 payload：", json.dumps(body, ensure_ascii=False)[:400])

    token = WindowsCredentialManager().get(device_token_target(DEVICE_ID))
    connection_id = f"connection-diag3-{uuid4().hex[:6]}"
    import websockets

    async with websockets.connect(_gateway_uri(API, DEVICE_ID, SESSION_ID, connection_id),
                                  additional_headers={"Authorization": f"Bearer {token}"}) as socket:
        json.loads(await socket.recv())
        envelope = {
            "device_id": DEVICE_ID, "agent_id": AGENT_ID, "session_id": SESSION_ID, "connection_id": connection_id,
            "sequence": 1, "message_id": f"message-diag3-{uuid4().hex}", "idempotency_key": f"idempotency-diag3-{uuid4().hex}",
            "message_type": "agent.event", "schema_version": "1.0", "sent_at": "2026-09-16T04:00:00+00:00",
            "payload": body,
        }
        await socket.send(json.dumps(envelope))
        response = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
        print("响应：", response.get("message_type"), json.dumps(response.get("payload"), ensure_ascii=False)[:400])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))