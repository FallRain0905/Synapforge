"""DP-2 端到端验收（第二轮）：用平台默认能力集重新授权，验证全自动闭环。

与第一轮的区别：授权能力用**平台默认值**（设备页同样如此），因此
`/api/runs/{id}/complete` 与 `/api/tasks/{id}/result` 不会再 403。
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

INFO = json.load(open(os.path.join(ROOT, "dp2_acceptance.json"), encoding="utf-8"))
API = INFO["api"]


def call(method: str, path: str, payload: dict | None = None) -> dict | list:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(f"{API}{path}", data=body, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}


def main() -> int:
    grant = call("POST", f"/api/projects/{INFO['project_id']}/device-grants", {"device_id": INFO["device_id"]})
    blob = base64.urlsafe_b64encode(json.dumps({
        "project_id": str(grant["grant"]["project_id"]),
        "project_token": grant["project_token"],
        "capabilities": grant["grant"]["capabilities"],
        "agent_id": grant["grant"]["agent_id"],
        "device_id": grant["grant"]["device_id"],
        "expires_at": grant["grant"]["expires_at"],
    }, ensure_ascii=False).encode()).decode().rstrip("=")
    task = call("POST", f"/api/projects/{INFO['project_id']}/tasks", {
        "title": "DP2 全自动闭环验收（Codex）",
        "description": "用 Codex CLI 回答一句话即可。",
        "stage": "modeling",
        "assignee": INFO["agent_id"],
        "priority": "high",
        "acceptance_criteria": ["运行台账里能看到 Codex 回复"],
        "resource_policy": {"worker_executor": "codex", "worker_prompt": "只回答一句话：DP2 全自动闭环验收通过。不要修改任何文件。"},
    })
    INFO["grant_blob"] = blob
    INFO["task_id"] = task["id"]
    INFO["grant_capabilities"] = grant["grant"]["capabilities"]
    json.dump(INFO, open(os.path.join(ROOT, "dp2_acceptance.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(json.dumps({"task_id": task["id"], "capabilities": grant["grant"]["capabilities"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())