"""DP-2 端到端验收：配对 → 授权 → 建 codex 任务，然后由 daemon-run 自动领取执行。

只用公开 API 与内核自带的配对逻辑（不手工编造 ID），并把关键返回值写到
`dp2_acceptance.json` 供后续步骤读取。
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from sidecar_api import default_pair_handler, default_state_dir  # noqa: E402
from sidecar_api import write_platform_info  # noqa: E402

API = os.environ.get("DP2_API", "http://127.0.0.1:8010")
OUT = ROOT / "dp2_acceptance.json"


def call(method: str, path: str, payload: dict | None = None) -> dict | list:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(f"{API}{path}", data=body, method=method,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}


def main() -> int:
    organizations = call("GET", "/api/organizations")
    organization_id = organizations[0]["id"]
    pairing = call("POST", "/api/devices/pairings", {"organization_id": organization_id, "expires_in_seconds": 900})
    blob = base64.urlsafe_b64encode(
        json.dumps({
            "pairing_id": pairing["id"],
            "pairing_code": pairing["pairing_code"],
            "challenge": pairing["challenge"],
            "expires_at": pairing["expires_at"],
        }).encode()
    ).decode().rstrip("=")

    state_dir = default_state_dir()
    paired = default_pair_handler({"platform_url": API, "pairing_blob": blob, "agent_name": "DP2 验收机"})
    print(json.dumps({"pair": paired}, ensure_ascii=False))
    write_platform_info(state_dir, API, paired["device_id"], paired["agent_id"])

    projects = call("GET", "/api/projects")
    project = next((item for item in projects if "DP2" in item["name"]), None)
    if project is None:
        project = call("POST", "/api/projects", {"name": "DP2 验收项目（自动领取）", "competition_pack": "cumcm-2026"})
    grant = call("POST", f"/api/projects/{project['id']}/device-grants", {
        "device_id": paired["device_id"],
        "capabilities": ["task.claim", "task.progress", "artifact.read", "artifact.write", "run.create", "run.event"],
        "expires_in_seconds": 86400,
    })
    grant_blob = base64.urlsafe_b64encode(
        json.dumps({
            "project_id": str(grant["grant"]["project_id"]),
            "project_token": grant["project_token"],
            "capabilities": grant["grant"]["capabilities"],
            "agent_id": grant["grant"]["agent_id"],
            "device_id": grant["grant"]["device_id"],
            "expires_at": grant["grant"]["expires_at"],
        }, ensure_ascii=False).encode()
    ).decode().rstrip("=")

    task = call("POST", f"/api/projects/{project['id']}/tasks", {
        "title": "DP2 自动领取验收（Codex 执行体）",
        "description": "用 Codex CLI 回答一句话即可：本次执行是否成功。",
        "stage": "modeling",
        "assignee": paired["agent_id"],
        "priority": "high",
        "acceptance_criteria": ["有 Codex 回复"],
        "resource_policy": {
            "worker_executor": "codex",
            "worker_prompt": "请只回答一句话：DP2 自动领取验收成功。不要修改任何文件。",
        },
    })

    payload = {
        "api": API,
        "organization_id": organization_id,
        "state_dir": str(state_dir),
        "device_id": paired["device_id"],
        "agent_id": paired["agent_id"],
        "project_id": project["id"],
        "task_id": task["id"],
        "grant_blob": grant_blob,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"project": project["id"], "task": task["id"], "device": paired["device_id"], "grant_blob": grant_blob}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())