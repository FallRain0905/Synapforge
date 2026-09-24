"""DP-2 验收 3/4 与 B3：暂停/恢复领取、紧急停止后的掉线、设备自视图。

依赖正在运行的 daemon-run 与平台；所有断言都通过真实端点完成。
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "apps" / "agent"))

from credential_store import WindowsCredentialManager, device_token_target  # noqa: E402
from sidecar_api import default_state_dir  # noqa: E402

INFO = json.loads((ROOT / "dp2_acceptance.json").read_text(encoding="utf-8"))
API = INFO["api"]
STATE = Path(INFO["state_dir"])


def call(method: str, path: str, payload: dict | None = None, headers: dict | None = None) -> dict | list:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(f"{API}{path}", data=body, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}


def sidecar(method: str, path: str) -> dict:
    info = json.loads((STATE / "sidecar.json").read_text(encoding="utf-8"))
    request = urllib.request.Request(f"http://127.0.0.1:{info['port']}{path}", data=b"{}" if method == "POST" else None,
                                     method=method, headers={"Authorization": f"Bearer {info['token']}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read())


def task_status(task_id: str) -> str:
    dashboard = call("GET", f"/api/projects/{INFO['project_id']}/dashboard")
    task = next(item for item in dashboard["tasks"] if item["id"] == task_id)
    return task["status"]


def new_task(title: str) -> str:
    created = call("POST", f"/api/projects/{INFO['project_id']}/tasks", {
        "title": title, "description": "暂停/恢复验收用。", "stage": "modeling",
        "assignee": INFO["agent_id"], "priority": "medium",
        "resource_policy": {"worker_executor": "codex", "worker_prompt": "只回答一个字：好"},
    })
    return created["id"]


def wait_for(predicate, seconds: float, step: float = 2.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return None


def main() -> int:
    results: dict[str, object] = {}

    # --- 验收 3：暂停领取 ---
    paused_task = new_task("DP2 验收：暂停领取期间不该被领走")
    sidecar("POST", "/tasks/pause")
    time.sleep(8)
    results["paused_task_status"] = task_status(paused_task)
    results["paused_before_resume_is_ready"] = results["paused_task_status"] == "READY"

    sidecar("POST", "/tasks/resume")
    claimed = wait_for(lambda: task_status(paused_task) in {"CLAIMED", "RUNNING", "WAITING_REVIEW", "APPROVED"}, 90)
    results["claimed_after_resume"] = claimed

    # --- B3：设备 Token 换自身配置 ---
    token = WindowsCredentialManager().get(device_token_target(INFO["device_id"]))
    self_view = call("GET", "/api/agent/me", headers={"Authorization": f"Bearer {token}"})
    results["self_view_project"] = self_view["grants"][0]["project_id"] if self_view.get("grants") else None
    results["self_view_capabilities"] = len(self_view["grants"][0]["capabilities"]) if self_view.get("grants") else 0
    results["self_view_policy"] = self_view["runtime_policy"]
    results["self_view_adapter_versions"] = (self_view.get("runtime") or {}).get("adapter_versions")
    results["self_view_has_no_plaintext_token"] = "device_token" not in json.dumps(self_view)

    # --- 验收 4：紧急停止 → 停止领取 + 平台看到掉线 ---
    sidecar("POST", "/emergency-stop")
    time.sleep(12)
    stopped_task = new_task("DP2 验收：紧急停止后不该被领走")
    time.sleep(15)
    results["emergency_task_status"] = task_status(stopped_task)
    results["emergency_task_not_claimed"] = results["emergency_task_status"] == "READY"
    agents = call("GET", "/api/agents")
    agent = next((item for item in agents if item["agent_id"] == INFO["agent_id"]), None)
    results["agent_status_after_emergency"] = (agent or {}).get("status")

    sidecar("POST", "/clear-emergency-stop")
    results["emergency_flag_cleared"] = sidecar("GET", "/status")["emergency_stop"] is False

    out = ROOT / "dp2_verify.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())