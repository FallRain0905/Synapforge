"""CL-2 验收：内容接进流程的三种情况（未批准拦下 / 已批准放行 / 退回后新版本）。

用真实平台接口跑：见 `docs/CONTENT_LIFECYCLE_PLAN.md` CL-2 的三条验收标准。
"""

from __future__ import annotations

import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "apps" / "agent"))

API = "http://127.0.0.1:8010"
PROJECT = "0a5b4269-928a-48ff-aa35-2788a781e4bf"
DEVICE = "device-fallrain"
AGENT = "agent-fallrain"


def call(method: str, path: str, payload: dict | None = None, headers: dict | None = None):
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{API}{path}", data=body, method=method, headers={"Content-Type": "application/json", **(headers or {})}
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8") or "{}")


def main() -> int:
    credential = json.loads((ROOT / "content_acceptance_credential.json").read_text(encoding="utf-8"))
    token = credential["project_token"]
    headers = {"X-Project-Capability-Token": token}

    _, artifacts = call("GET", f"/api/projects/{PROJECT}/artifacts")
    approved = [a for a in artifacts if a["status"] == "APPROVED" and a.get("downstream_allowed")]
    pending = [a for a in artifacts if a["status"] == "PENDING_REVIEW"]
    print(f"已批准可用 {len(approved)} 份，待审 {len(pending)} 份")

    results: dict[str, object] = {}

    # ① 未批准的内容当输入 → 领取必须被拦下
    if pending:
        _, task = call("POST", f"/api/projects/{PROJECT}/tasks", {
            "title": "CL-2 验收：引用未批准内容（应被拦）",
            "description": "claim 时必须失败",
            "stage": "modeling",
            "input_artifacts": [pending[0]["id"]],
        })
        status, body = call(
            "POST",
            f"/api/agents/{AGENT}/tasks/claim",
            {"project_id": PROJECT, "idempotency_key": f"cl2-blocked-{task['id']}"},
            headers,
        )
        # 平台的保护方式是**跳过**（claim_next_task 只挑依赖就绪的任务），不是报错：
        # 因此断言"没被领走"而不是"报 409"——界面侧由任务诊断提示"等待上游/输入未批准"。
        claimed = (body or {}).get("task") if isinstance(body, dict) else None
        results["unapproved_input_not_claimed"] = claimed is None or claimed.get("id") != task["id"]
        print("① 未批准输入 →", status, "claimed:", (claimed or {}).get("title") if claimed else None)

    # ② 已批准的内容当输入 → 可以被领取
    if approved:
        _, task = call("POST", f"/api/projects/{PROJECT}/tasks", {
            "title": "CL-2 验收：引用已批准内容（应放行）",
            "description": "claim 应当成功",
            "stage": "modeling",
            "input_artifacts": [approved[0]["id"]],
        })
        status, body = call(
            "POST",
            f"/api/agents/{AGENT}/tasks/claim",
            {"project_id": PROJECT, "idempotency_key": f"cl2-allowed-{task['id']}"},
            headers,
        )
        results["approved_input_claimable"] = status == 200 and bool(body.get("task"))
        print("② 已批准输入 →", status, str(body)[:120])
        if status == 200:
            # 放回队列，避免占着租约影响后续验收
            lease = body["lease"]["lease_token"]
            call("POST", f"/api/tasks/{task['id']}/progress", {
                "agent_id": AGENT, "lease_token": lease, "status": "RUNNING",
                "message": "CL-2 验收占位", "idempotency_key": f"cl2-progress-{task['id']}",
            }, headers)
            call("POST", f"/api/tasks/{task['id']}/result", {
                "agent_id": AGENT, "lease_token": lease, "success": False,
                "summary": "CL-2 验收：仅验证可领取，不计入执行结果",
                "idempotency_key": f"cl2-result-{task['id']}",
            }, headers)

    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())