"""端到端验收：双 Agent 协作生产主线（W2.6，实施计划第四期）。

与单测的区别：真 uvicorn 进程 + 真 HTTP + 两套真实注册的设备/Agent/能力令牌——
验证的是部署形态下的协作协议（领取、进度、结果、审核、重试、溯源、聚合视图），
不依赖静态数据库剧本。执行体侧的"行为"由 HTTP 调用模拟（与 opencode 无关，
协作语义才是本剧本的对象）。

剧本（规划 §6 阶段 2 的验收场景）：
  Agent A 领取任务并执行（进度 → 产物+receipt → 结果）
  → 人工批准产物（下游可用）
  → Agent B 领取下游任务（读批准输入）→ 一次失败 → 重试 → 成功交付
  → 人工退回一次产生新版本 → 修订后再批准
  → 团队/生产聚合视图与 project.* 事件目录全程可追溯
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "apps" / "api"
sys.path.insert(0, str(API_DIR))
sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from device_test_support import registration_request  # noqa: E402

from multi_agent_assertions import (  # noqa: E402
    check_artifact_feeds_task,
    check_artifact_version_bump,
    check_event_stream,
    check_production_path,
    check_receipt_shape,
    check_task_lifecycle,
    check_team_view,
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def call(base: str, path: str, method: str = "GET", body: dict | None = None, token: str | None = None,
         agent: tuple[str, str] | None = None) -> tuple[int, str]:
    request = urllib.request.Request(f"{base}{path}", method=method)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if agent:
        agent_id, project_token = agent
        request.add_header("X-Agent-Id", agent_id)
        request.add_header("X-Project-Capability-Token", project_token)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    try:
        with urllib.request.urlopen(request, data=data, timeout=30) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


def main() -> int:
    failures: list[str] = []

    def check(label: str, condition: bool, extra: str = "") -> None:
        print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' — ' + extra) if extra else ''}")
        if not condition:
            failures.append(label)

    workdir = Path(tempfile.mkdtemp(prefix="ma-e2e-"))
    port = free_port()
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(API_DIR), str(ROOT)]),
        "PLATFORM_AUTH_MODE": "required",
        "MULTI_AGENT_E2E_DATA_DIR": str(workdir),
        "MULTI_AGENT_E2E_PORT": str(port),
    }
    print(f"启动 API（端口 {port}，临时库 {workdir}）")
    process = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(ROOT / "scripts" / "_multi_agent_e2e_launcher.py")],
        cwd=str(API_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        ready = False
        for _ in range(60):
            try:
                status, _ = call(base, "/api/auth/me")
                if status in {200, 401}:
                    ready = True
                    break
            except Exception:
                time.sleep(0.5)
        if not ready:
            output = process.stdout.read().decode("utf-8", "replace") if process.stdout else ""
            print("服务没起来，输出如下：\n", output[-3000:])
            return 1
        print("服务已就绪。\n")

        # ---- ① 账号与项目 ------------------------------------------------
        status, raw = call(base, "/api/auth/register", "POST", {
            "email": "owner@e2e.test", "password": "owner-pass-12345", "display_name": "项目负责人",
        })
        check("注册项目负责人的账号", status == 201, f"HTTP {status}")
        if status != 201:
            return 1
        member_token = json.loads(raw)["token"]
        member_id = json.loads(raw)["account"]["member"]["id"]
        organization_id = json.loads(raw)["account"]["member"]["organization_id"]

        status, raw = call(base, "/api/projects", "POST", {"name": "双智能体协作验收"}, token=member_token)
        check("创建协作项目", status in {200, 201}, f"HTTP {status} {raw[:160]}")
        project_id = json.loads(raw)["id"]

        # ---- ② 编制：两个 Agent、两台设备、两份能力授权 --------------------
        def bring_up_agent(agent_id: str, device_id: str) -> str:
            status, raw = call(base, "/api/agents/register", "POST", {
                "agent_id": agent_id, "display_name": agent_id, "owner_member_id": member_id,
            })
            assert status == 200, raw  # 该路由未声明 201，默认 200
            status, raw = call(base, "/api/devices/pairings", "POST", {"organization_id": organization_id}, token=member_token)
            assert status == 201, raw
            pairing = SimpleNamespace(**json.loads(raw))
            payload = registration_request(
                pairing, Ed25519PrivateKey.generate(), agent_id, device_id,
                device_name=f"e2e-{device_id}", capabilities=["task.claim", "task.progress", "task.result", "task.lease", "artifact.write", "artifact.read"],
            )
            status, raw = call(base, "/api/devices/register", "POST", payload.model_dump(mode="json"))
            assert status == 201, raw
            status, raw = call(base, f"/api/projects/{project_id}/device-grants", "POST", {"device_id": device_id}, token=member_token)
            assert status == 201, raw
            return json.loads(raw)["project_token"]

        token_a = bring_up_agent("e2e-agent-a", "e2e-device-a")
        token_b = bring_up_agent("e2e-agent-b", "e2e-device-b")
        check("两套 Agent/设备/能力令牌就绪", bool(token_a) and bool(token_b))
        # 能力令牌之外还要 X-Agent-Id（服务端核对令牌属于这台设备的 Agent）
        as_agent = lambda aid, tok: (aid, tok)  # noqa: E731

        # ---- ③ 主线：A 执行 T1（进度 → 产物+receipt → 结果）--------------
        status, raw = call(base, f"/api/projects/{project_id}/tasks", "POST", {
            "title": "整理题面与数据画像", "stage": "problem_analysis", "output_types": ["problem_facts"],
        }, token=member_token)
        check("创建任务 T1", status == 201, f"HTTP {status}")
        t1 = json.loads(raw)["id"]
        t1_steps = ["READY"]

        status, raw = call(base, f"/api/tasks/{t1}/claim", "POST", {
            "agent_id": "e2e-agent-a", "lease_seconds": 900, "idempotency_key": "e2e-claim-t1",
        }, token=member_token, agent=as_agent("e2e-agent-a", token_a))
        t1_steps.append(json.loads(raw)["task"]["status"] if status == 200 else f"http-{status}")
        check("A 领取 T1（能力令牌）", status == 200 and json.loads(raw)["task"]["status"] == "CLAIMED", f"HTTP {status}")
        lease_a = json.loads(raw)["lease"]

        status, raw = call(base, f"/api/tasks/{t1}/progress", "POST", {
            "agent_id": "e2e-agent-a", "lease_token": lease_a["lease_token"], "status": "RUNNING",
            "idempotency_key": "e2e-prog-t1",
        }, token=member_token, agent=as_agent("e2e-agent-a", token_a))
        t1_steps.append(json.loads(raw)["status"] if status == 200 else f"http-{status}")
        check("A 推进 T1 → RUNNING", status == 200, f"HTTP {status}")

        status, raw = call(base, f"/api/projects/{project_id}/artifacts", "POST", {
            "name": "problem-facts.md", "artifact_type": "problem_facts", "task_id": t1,
            "receipt": {"receipt_version": 1, "tool_name": "workspace_diff", "tool_call_id": "turn-t1",
                        "args_hash": "0123456789abcdef", "output_hash": "fedcba9876543210", "output_bytes": 512},
        }, token=member_token, agent=as_agent("e2e-agent-a", token_a))
        check("A 上传产物（带 receipt）", status == 201, f"HTTP {status}")
        artifact_a = json.loads(raw)
        check("产物带溯源 receipt", artifact_a["receipt"]["tool_name"] == "workspace_diff")
        ok, extra = check_receipt_shape(artifact_a["receipt"])
        check("receipt 形状符合 RECEIPT_FORMAT §2（契约断言）", ok, extra)

        status, raw = call(base, f"/api/tasks/{t1}/result", "POST", {
            "agent_id": "e2e-agent-a", "lease_token": lease_a["lease_token"], "idempotency_key": "e2e-result-t1",
            "success": True, "output_artifact_ids": [artifact_a["id"]], "summary": "题面事实已整理",
        }, token=member_token, agent=as_agent("e2e-agent-a", token_a))
        t1_steps.append(json.loads(raw)["task"]["status"] if status == 200 else f"http-{status}")
        check("A 交付 T1 → 待审", status == 200 and json.loads(raw)["task"]["status"] == "WAITING_REVIEW", f"HTTP {status}")

        status, raw = call(base, f"/api/projects/{project_id}/reviews", "POST", {
            "target_type": "artifact", "target_id": artifact_a["id"], "verdict": "APPROVED",
            "summary": "事实清单完整", "idempotency_key": "e2e-review-a1",
        }, token=member_token)
        check("人工批准 A 的产物", status == 201, f"HTTP {status} {raw[:160]}")

        # ---- ④ 下游：B 执行 T2，一次失败 → 重试 → 成功 --------------------
        status, raw = call(base, f"/api/projects/{project_id}/tasks", "POST", {
            "title": "建立模型并求解", "stage": "modeling", "input_artifacts": [artifact_a["id"]],
        }, token=member_token)
        t2 = json.loads(raw)["id"]
        t2_steps = ["READY"]

        status, raw = call(base, f"/api/tasks/{t2}/claim", "POST", {
            "agent_id": "e2e-agent-b", "lease_seconds": 900, "idempotency_key": "e2e-claim-t2",
        }, token=member_token, agent=as_agent("e2e-agent-b", token_b))
        t2_steps.append(json.loads(raw)["task"]["status"] if status == 200 else f"http-{status}")
        check("B 领取下游任务 T2", status == 200, f"HTTP {status} {raw[:160]}")
        lease_b = json.loads(raw)["lease"]

        status, raw = call(base, f"/api/tasks/{t2}/result", "POST", {
            "agent_id": "e2e-agent-b", "lease_token": lease_b["lease_token"], "idempotency_key": "e2e-result-t2-fail",
            "success": False, "output_artifact_ids": [], "summary": "求解器崩溃（第一次）",
        }, token=member_token, agent=as_agent("e2e-agent-b", token_b))
        t2_steps.append(json.loads(raw)["task"]["status"] if status == 200 else f"http-{status}")
        check("B 第一次交付失败 → FAILED", status == 200 and json.loads(raw)["task"]["status"] == "FAILED", f"HTTP {status}")

        status, raw = call(base, f"/api/tasks/{t2}?status=READY", "PATCH", token=member_token)
        t2_steps.append(json.loads(raw)["status"] if status == 200 else f"http-{status}")
        check("负责人把 T2 放回 READY（重试）", status == 200, f"HTTP {status} {raw[:160]}")
        status, raw = call(base, f"/api/tasks/{t2}/claim", "POST", {
            "agent_id": "e2e-agent-b", "lease_seconds": 900, "idempotency_key": "e2e-claim-t2-retry",
        }, token=member_token, agent=as_agent("e2e-agent-b", token_b))
        t2_steps.append(json.loads(raw)["task"]["status"] if status == 200 else f"http-{status}")
        check("B 重试领取 T2", status == 200, f"HTTP {status} {raw[:160]}")
        lease_b2 = json.loads(raw)["lease"]

        status, raw = call(base, f"/api/projects/{project_id}/artifacts", "POST", {
            "name": "model-spec.md", "artifact_type": "model_spec", "task_id": t2,
            "input_artifact_ids": [artifact_a["id"]],
        }, token=member_token, agent=as_agent("e2e-agent-b", token_b))
        artifact_b = json.loads(raw)

        status, raw = call(base, f"/api/tasks/{t2}/result", "POST", {
            "agent_id": "e2e-agent-b", "lease_token": lease_b2["lease_token"], "idempotency_key": "e2e-result-t2-ok",
            "success": True, "output_artifact_ids": [artifact_b["id"]], "summary": "模型与求解完成",
        }, token=member_token, agent=as_agent("e2e-agent-b", token_b))
        t2_steps.append(json.loads(raw)["task"]["status"] if status == 200 else f"http-{status}")
        check("B 重试后交付成功", status == 200 and json.loads(raw)["task"]["status"] == "WAITING_REVIEW", f"HTTP {status}")

        # ---- ⑤ 退回 → 修订新版本 → 再批准 --------------------------------
        status, raw = call(base, f"/api/projects/{project_id}/reviews", "POST", {
            "target_type": "artifact", "target_id": artifact_b["id"], "verdict": "NEEDS_REVISION",
            "summary": "缺少灵敏度说明", "idempotency_key": "e2e-review-b1",
        }, token=member_token)
        check("人工退回 B 的产物", status == 201, f"HTTP {status}")

        status, raw = call(base, f"/api/projects/{project_id}/artifacts/{artifact_b['id']}/versions", "POST", {
            "name": "model-spec.md", "artifact_type": "model_spec", "task_id": t2,
            "input_artifact_ids": [artifact_a["id"]], "description": "补灵敏度说明",
        }, token=member_token)
        check("修订产生新版本", status == 201 and json.loads(raw)["version"] == artifact_b["version"] + 1, f"HTTP {status} {raw[:160]}")
        artifact_b2 = json.loads(raw)

        status, raw = call(base, f"/api/projects/{project_id}/reviews", "POST", {
            "target_type": "artifact", "target_id": artifact_b2["id"], "verdict": "APPROVED",
            "summary": "灵敏度已补", "idempotency_key": "e2e-review-b2",
        }, token=member_token)
        check("人工批准修订版", status == 201, f"HTTP {status}")

        # 产物批准 ≠ 任务批准（规划 §7 红线的另一半）：T2 任务本身也要人工收口
        status, raw = call(base, f"/api/projects/{project_id}/reviews", "POST", {
            "target_type": "task", "target_id": t2, "verdict": "APPROVED",
            "summary": "模型求解与产出完整", "idempotency_key": "e2e-review-t2",
        }, token=member_token)
        check("人工批准 T2 任务本身", status == 201, f"HTTP {status} {raw[:160]}")
        status, raw = call(base, f"/api/tasks/{t2}", token=member_token)
        observed = json.loads(raw)["task"]["status"]  # TaskDetail 包装（task + 预算执行态）
        t2_steps.append(observed)
        check("任务级收口推进 T2 → APPROVED", observed == "APPROVED", f"HTTP {status}")

        # ---- ⑥ 聚合视图与事件可追溯 --------------------------------------
        status, raw = call(base, f"/api/projects/{project_id}/team", token=member_token)
        team = json.loads(raw)
        ok, extra = check_team_view(team, expect_agent_ids=["e2e-agent-a", "e2e-agent-b"])
        check("团队视图结构与计数一致（契约断言）", ok, extra)
        check("团队视图任务状态全景含已批准", team["task_status_counts"].get("APPROVED", 0) >= 1, str(team["task_status_counts"]))

        status, raw = call(base, f"/api/projects/{project_id}/production-path", token=member_token)
        production = json.loads(raw)
        nodes = production["nodes"]
        ok, extra = check_production_path(
            production, expect_artifact_ids=[artifact_a["id"], artifact_b["id"], artifact_b2["id"]]
        )
        check("生产路径结构契约断言", ok, extra)
        ok, extra = check_artifact_version_bump(artifact_b["version"], artifact_b2["version"])
        check("退回修订恰好 +1（契约断言）", ok, extra)
        ok, extra = check_artifact_feeds_task(production, artifact_a["id"], t2)
        check("因果链断言：A 产物喂给 T2", ok, extra)
        node_a = next((n for n in nodes if n["artifact_id"] == artifact_a["id"]), None)
        check("生产路径：A 产物带 receipt 溯源", bool(node_a and node_a["receipt"] and node_a["receipt"]["tool_call_id"] == "turn-t1"))
        check("生产路径：A 产物喂给了 B 的任务", any(item["task_id"] == t2 for item in (node_a or {}).get("downstream_tasks", [])))
        node_b2 = next((n for n in nodes if n["artifact_id"] == artifact_b2["id"]), None)
        check("生产路径：修订版已批准且版本+1", bool(node_b2 and node_b2["status"] == "APPROVED" and node_b2["version"] == artifact_b["version"] + 1))

        status, raw = call(base, f"/api/projects/{project_id}/events", token=member_token)
        # /events 返回 Event 契约（event_type/sequence），映射成契约信封形状再校验
        envelopes = [
            {"event": item.get("event_type"), "seq": item.get("sequence")}
            for item in json.loads(raw)
            if isinstance(item, dict)
        ]
        ok, extra = check_event_stream(envelopes)
        check("事件目录与 seq 严格递增（契约断言）", ok, extra)
        check("事件流含 receipt 溯源字段", "workspace_diff" in raw)

        # ---- 状态机总断言（fail-closed 白名单）---------------------------
        ok, extra = check_task_lifecycle(t1_steps)
        check("T1 状态机全程合法", ok, extra)
        ok, extra = check_task_lifecycle(t2_steps)
        check("T2 状态机全程合法（含失败重试）", ok, extra)

        # ---- 收尾 --------------------------------------------------------
        print()
        if failures:
            print(f"验收未通过：{len(failures)} 项失败 — {'; '.join(failures)}")
            return 1
        print("多智能体协作 e2e 验收全部通过。")
        return 0
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except Exception:
            process.kill()


if __name__ == "__main__":
    sys.exit(main())
