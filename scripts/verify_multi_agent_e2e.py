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
    check_gate_evaluated_payload,
    check_ledger_snapshot,
    check_stall_series,
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

        # manual 模式：关闭维护线程的自动派发，引擎场景由脚本显式驱动每个节点
        # （auto 模式的受约束派发由 auto_dispatch_tick 承担，与本剧本的显式领取竞争）
        status, raw = call(base, "/api/projects", "POST", {
            "name": "双智能体协作验收", "task_mode": "manual",
        }, token=member_token)
        check("创建协作项目", status in {200, 201}, f"HTTP {status} {raw[:160]}")
        project_id = json.loads(raw)["id"]

        # ---- ② 编制：两个 Agent、两台设备、两份能力授权 --------------------
        def bring_up_agent(agent_id: str, device_id: str) -> str:
            status, raw = call(base, "/api/agents/register", "POST", {
                "agent_id": agent_id, "display_name": agent_id, "owner_member_id": member_id,
                # 工作流任务按角色绑定带 required_capabilities（files/shell），领取处硬校验
                "supported_tools": ["files.read", "files.write", "shell.run"],
            })
            assert status == 200, raw  # 该路由未声明 201，默认 200
            status, raw = call(base, "/api/devices/pairings", "POST", {"organization_id": organization_id}, token=member_token)
            assert status == 201, raw
            pairing = SimpleNamespace(**json.loads(raw))
            payload = registration_request(
                pairing, Ed25519PrivateKey.generate(), agent_id, device_id,
                device_name=f"e2e-{device_id}",
                # 角色绑定的能力要求（mm-*：files/shell）必须齐——工作流任务的
                # required_capabilities 在 claim 处硬校验，缺了就是三次重试撞同一堵墙
                capabilities=["task.claim", "task.progress", "task.result", "task.lease",
                              "artifact.write", "artifact.read", "files.read", "files.write", "shell.run"],
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

        # ---- ⑦ 工作流引擎 + 运行时协作 + 阶段 8/9（D2）-------------------
        # 诚实剧本：七个节点全部走 claim → 进度 → 产物 → 结果 → 人工审核批准
        # （产物批准 ≠ 任务批准）；compile/bundle 的交付适配器在节点批准后由推进器执行。
        status, raw = call(base, "/api/workflows/builtin", "POST", {}, token=member_token)
        check("安装内置工作流包", status == 200 and "cumcm-main" in raw, f"HTTP {status}")
        status, raw = call(base, "/api/workflows", token=member_token)
        wf = next((w for w in json.loads(raw) if w["key"] == "cumcm-main"), None)
        check("cumcm-main 包存在", wf is not None)
        status, raw = call(base, f"/api/projects/{project_id}/workflow-runs", "POST", {
            "workflow_id": wf["id"], "inputs": {"problem_code": "C", "questions": "1,2,3,4"},
        }, token=member_token)
        check("应用 cumcm-main → 七节点任务骨架", status == 201 and len(json.loads(raw)["tasks"]) == 7,
              f"HTTP {status} {raw[:160]}")
        wf_run = json.loads(raw)
        wf_tasks = {item["node_id"]: item["task_id"] for item in wf_run["tasks"]}
        wf_stalls: list[int] = []

        def approve_artifact_and_task(artifact_id: str | None, task_id: str, idem: str) -> None:
            if artifact_id:
                call(base, f"/api/projects/{project_id}/reviews", "POST", {
                    "target_type": "artifact", "target_id": artifact_id, "verdict": "APPROVED",
                    "summary": "产物合格", "idempotency_key": f"e2e-arp-{idem}",
                }, token=member_token)
            call(base, f"/api/projects/{project_id}/reviews", "POST", {
                "target_type": "task", "target_id": task_id, "verdict": "APPROVED",
                "summary": "任务收口", "idempotency_key": f"e2e-trp-{idem}",
            }, token=member_token)

        def drive_node(node_id: str, agent_id: str, token: str, artifact_name: str | None,
                       artifact_type: str, idem: str, with_report: bool = True) -> str | None:
            task_id = wf_tasks[node_id]
            status, raw = call(base, f"/api/tasks/{task_id}/claim", "POST", {
                "agent_id": agent_id, "lease_seconds": 900, "idempotency_key": f"e2e-{idem}-claim",
            }, token=member_token, agent=as_agent(agent_id, token))
            if status != 200:
                ev = call(base, f"/api/projects/{project_id}/events", token=member_token)
                claims = [item.get("payload") for item in json.loads(ev[1])
                          if item.get("event_type") == "task.claimed" and json.dumps(item.get("payload", {})).find(node_id) >= 0]
                raise AssertionError(f"claim {node_id}: {raw[:200]} claims={claims}")
            lease = json.loads(raw)["lease"]
            progress = call(base, f"/api/tasks/{task_id}/progress", "POST", {
                "agent_id": agent_id, "lease_token": lease["lease_token"], "status": "RUNNING",
                "idempotency_key": f"e2e-{idem}-prog",
            }, token=member_token, agent=as_agent(agent_id, token))
            assert progress[0] == 200, f"progress {node_id}: {progress[1][:200]}"
            if with_report:
                rep = call(base, f"/api/agents/{agent_id}/tasks/{task_id}/stage-report", "POST", {
                    "report": {"status": "partial", "summary": f"{node_id} 进行中",
                               "completed_items": ["一半"], "incomplete_items": ["另一半"],
                               "output_artifacts": [], "evidence_refs": [],
                               "tests": {"passed": 1, "failed": 0, "skipped": 0, "commands": []},
                               "blockers": [], "can_continue_safely": True,
                               "continued_under_assumption": False, "assumptions": []},
                    "run_id": wf_run["run_id"], "attempt": 1,
                }, token=member_token, agent=as_agent(agent_id, token))
                check(f"{node_id} 阶段报告被接收（不是批准）", rep[0] == 201, f"HTTP {rep[0]}")
            artifact_id = None
            if artifact_name:
                art = call(base, f"/api/projects/{project_id}/artifacts", "POST", {
                    "name": artifact_name, "artifact_type": artifact_type, "task_id": task_id,
                    "receipt": {"receipt_version": 1, "tool_name": "workspace_diff",
                                "tool_call_id": f"turn-{idem}", "args_hash": "0123456789abcdef",
                                "output_hash": "fedcba9876543210", "output_bytes": 256},
                }, token=member_token, agent=as_agent(agent_id, token))
                check(f"{node_id} 产物带 receipt", art[0] == 201, f"HTTP {art[0]}")
                artifact_id = json.loads(art[1])["id"]
            result = call(base, f"/api/tasks/{task_id}/result", "POST", {
                "agent_id": agent_id, "lease_token": lease["lease_token"], "idempotency_key": f"e2e-{idem}-result",
                "success": True, "output_artifact_ids": [artifact_id] if artifact_id else [],
                "summary": f"{node_id} 完成",
            }, token=member_token, agent=as_agent(agent_id, token))
            assert result[0] == 200, f"result {node_id}: {result[1][:200]}"
            return artifact_id

        def advance(_run=None) -> tuple[int, str]:
            return call(base,
                        f"/api/projects/{project_id}/workflow-runs/{wf_run['run_id']}/advance",
                        "POST", {}, token=member_token)

        facts_art = drive_node("problem_facts", "e2e-agent-a", token_a, "problem-facts.md", "problem_facts", "facts")
        approve_artifact_and_task(facts_art, wf_tasks["problem_facts"], "facts")

        model_art = drive_node("model", "e2e-agent-a", token_a, "model-spec.md", "model_spec", "model")
        # 门禁周期：交付待审时 NOT_HOLDS（未批准）→ 批准后 HOLDS
        status, raw = advance(wf_run)
        gate_round1 = json.loads(raw)["gates"]
        check("model 门禁评估为 NOT_HOLDS（fail-closed）",
              bool(gate_round1) and gate_round1[0]["verdict"] == "NOT_HOLDS", raw[:160])
        status, raw = call(base, f"/api/projects/{project_id}/events", token=member_token)
        gate_events = [item["payload"] for item in json.loads(raw)
                       if item.get("event_type") == "project.gate.evaluated"]
        ok, extra = check_gate_evaluated_payload(gate_events[0])
        check("gate.evaluated 叶子三值一致（契约断言）", ok, extra)
        # 先批产物（门禁在任务仍 WAITING_REVIEW 时评估 → HOLDS），再批任务解锁下游
        call(base, f"/api/projects/{project_id}/reviews", "POST", {
            "target_type": "artifact", "target_id": model_art, "verdict": "APPROVED",
            "summary": "模型规格合格", "idempotency_key": "e2e-review-model-art",
        }, token=member_token)
        status, raw = advance(wf_run)
        gate_round2 = json.loads(raw)["gates"]
        check("model 门禁批准后 HOLDS", bool(gate_round2) and gate_round2[0]["verdict"] == "HOLDS", raw[:160])
        call(base, f"/api/projects/{project_id}/reviews", "POST", {
            "target_type": "task", "target_id": wf_tasks["model"], "verdict": "APPROVED",
            "summary": "模型任务收口", "idempotency_key": "e2e-review-model-task",
        }, token=member_token)

        # code 节点：领取 → RUNNING → 运行时信息请求（required）→ 回复 → 消费（RUNNING）
        # → 产物 → 结果（信息请求发生在执行中，不是执行前后）
        status, raw = call(base, f"/api/tasks/{wf_tasks['code']}/claim", "POST", {
            "agent_id": "e2e-agent-b", "lease_seconds": 900, "idempotency_key": "e2e-claim-code",
        }, token=member_token, agent=as_agent("e2e-agent-b", token_b))
        assert status == 200, f"claim code: {raw[:200]}"
        code_lease = json.loads(raw)["lease"]
        status, raw = call(base, f"/api/agents/e2e-agent-b/information-requests?project_id={project_id}",
                           "POST", {
            "request": {"request_type": "context", "question": "模型约束第 2 条是什么？",
                        "required_information": ["约束 2"], "blocking": "required",
                        "reason": "没有约束 2 无法安全编码",
                        "response_deadline": "2026-10-02T23:00:00+00:00"},
            "run_id": wf_run["run_id"], "requester_node_id": "code",
            "requester_task_id": wf_tasks["code"], "provider_agent_id": "e2e-agent-a",
            "idempotency_key": "e2e-ireq-1",
        }, token=member_token, agent=as_agent("e2e-agent-b", token_b))
        check("B 创建 required 信息请求", status == 201, f"HTTP {status} {raw[:160]}")
        ireq = json.loads(raw)
        check("required 请求节点效果 WAITING", "WAITING" in ireq["node_effect"], ireq["node_effect"])
        call(base, f"/api/agents/e2e-agent-a/information-requests/{ireq['request_id']}/ack",
             "POST", {"outcome": "received"}, token=member_token, agent=as_agent("e2e-agent-a", token_a))
        call(base, f"/api/agents/e2e-agent-a/information-requests/{ireq['request_id']}/respond",
             "POST", {"response": {"status": "answered", "answer_summary": "约束 2：求解步长 0.1",
                                   "facts": ["步长 0.1"], "artifact_refs": [model_art],
                                   "evidence_refs": [], "assumptions": []}},
             token=member_token, agent=as_agent("e2e-agent-a", token_a))
        consumed = call(base, f"/api/agents/e2e-agent-b/information-requests/{ireq['request_id']}/consume",
                        "POST", {}, token=member_token, agent=as_agent("e2e-agent-b", token_b))
        check("B 消费回复后继续（RUNNING）",
              consumed[0] == 200 and json.loads(consumed[1])["node_effect_result"]["effect"] == "RUNNING",
              consumed[1][:160])

        # code 节点收尾：两个产物（code + result_table，C 对照清单②1）→ 结果
        for art_name, art_type in (("code-result.zip", "code"), ("result-table.csv", "result_table")):
            art = call(base, f"/api/projects/{project_id}/artifacts", "POST", {
                "name": art_name, "artifact_type": art_type, "task_id": wf_tasks["code"],
                "receipt": {"receipt_version": 1, "tool_name": "workspace_diff",
                            "tool_call_id": f"turn-{art_type}", "args_hash": "0123456789abcdef",
                            "output_hash": "fedcba9876543210", "output_bytes": 256},
            }, token=member_token, agent=as_agent("e2e-agent-b", token_b))
            check(f"code 产物 {art_type} 带 receipt", art[0] == 201, f"HTTP {art[0]}")
        result = call(base, f"/api/tasks/{wf_tasks['code']}/result", "POST", {
            "agent_id": "e2e-agent-b", "lease_token": code_lease["lease_token"],
            "idempotency_key": "e2e-result-code", "success": True,
            "output_artifact_ids": [], "summary": "代码与结果表完成",
        }, token=member_token, agent=as_agent("e2e-agent-b", token_b))
        assert result[0] == 200, f"result code: {result[1][:200]}"
        approve_artifact_and_task(None, wf_tasks["code"], "code-task")  # 任务级收口（产物审批在上方）
        review_art = drive_node("review", "e2e-agent-b", token_b, "audit-report.md", "audit_report", "review")
        approve_artifact_and_task(review_art, wf_tasks["review"], "review")
        paper_art = drive_node("paper", "e2e-agent-a", token_a, "paper-source.tex", "paper_source", "paper")
        approve_artifact_and_task(paper_art, wf_tasks["paper"], "paper")
        compile_art = drive_node("compile", "e2e-agent-a", token_a, "main.pdf", "compiled_pdf", "compile")
        approve_artifact_and_task(compile_art, wf_tasks["compile"], "compile")
        bundle_art = drive_node("bundle", "e2e-agent-a", token_a, "bundle.zip", "submission_bundle", "bundle")
        approve_artifact_and_task(bundle_art, wf_tasks["bundle"], "bundle")

        # 阶段 8/9：全部节点批准 → COMPLETED；交付适配器已执行并如实记账
        status, raw = advance(wf_run)
        final = json.loads(raw)
        check("全部节点批准 → 运行 COMPLETED（阶段 8/9）", final["status"] == "COMPLETED", raw[:160])
        check("交付适配器被执行并如实记账（deliveries）",
              any(d["node_id"] == "compile" for d in final["deliveries"]), str(final["deliveries"])[:160])
        wf_stalls.append(final["stall_count"])

        # 决策可回放 + 账本/停滞断言（C 的断言库）
        status, raw = call(base, f"/api/projects/{project_id}/orchestration-decisions", token=member_token)
        decisions = json.loads(raw)
        check("编排决策已持久化可回放", status == 200 and len(decisions) >= 1, raw[:120])
        status, raw = call(base, f"/api/projects/{project_id}/workflow-runs/{wf_run['run_id']}", token=member_token)
        detail = json.loads(raw)
        ok, extra = check_ledger_snapshot(detail["ledger"])
        check("账本快照结构（契约断言）", ok, extra)
        ok, extra = check_stall_series(wf_stalls)
        check("stall 记账合规（契约断言）", ok, extra)
        status, raw = call(base, f"/api/projects/{project_id}/workflow-view", token=member_token)
        view = json.loads(raw)
        check("workflow-view 聚合（D1 DTO）", view["summary"].get("APPROVED", 0) >= 1, str(view["summary"])[:120])

        # ---- 状态机总断言（fail-closed 白名单）---------------------------
        ok, extra = check_task_lifecycle(t1_steps)        # ---- 状态机总断言（fail-closed 白名单）---------------------------
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
        if os.environ.get("E2E_DUMP_SERVER_LOG"):
            out = process.stdout.read().decode("utf-8", "replace") if process.stdout else ""
            Path("/tmp/e2e_server.log").write_text(out, encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
