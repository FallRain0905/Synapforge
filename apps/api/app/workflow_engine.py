"""工作流推进器（W3.2，实施计划第五期）。

把 workflow run 从"任务骨架"推进到"受约束的自动流转"。每轮 ``advance_run``：
1. **有界重试**：``on_fail=retry`` 的失败节点放回 READY（发 ``project.task.retried``），
   尝试次数有硬上限——预算的 ``max_attempts`` 在 claim 处强制，超限不重试（失败可见，
   不静默循环）；``max_attempts`` 用尽或 ``on_fail=escalate_human`` 保持 FAILED 由人处置。
2. **门禁评估**：带 ``gate_policy`` 的节点在交付待审时评估（W3.3 的
   ``evaluate_workflow_gate``）；结果按 ``on_block`` 处置——``blocked`` 停下等输入、
   ``escalate_human`` 升级人工（发 ``project.gate.evaluated/passed/blocked/escalated``）。
   **门禁不替代人工审核**："任务成功 ≠ 成果物已批准"红线原样保留。
3. **推进账本**：Magentic-One 语义的确定性子集（``progress_ledger.ProgressLedger``）——
   每轮以确定性信号记账（本轮是否有状态迁移=进步、重复签名=在循环），stall 计数达标
   置 ``needs_replan``，运行状态转 ``STALLED``。不用 LLM 判定：编排信号全部可确定性导出
   时，让 LLM 参与只会引入不必要的不确定性（deer-flow 的判停哲学：可证明的进展才算进展）。
4. **完成判定**：全部节点任务 APPROVED → 运行 ``COMPLETED``。

防误判与防空转的借鉴来源（实施计划 §1）：deer-flow ``runtime/goal.py``（类型化 blocker
+ 最新可见产出 SHA-256 判停滞——本模块以"任务状态集合签名"为等价物）与 autogen
Magentic-One（``_magentic_one_orchestrator.py`` 的账本字段与 stall 规则，逐字对齐）。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any
from uuid import UUID

from . import workflow_delivery, workflow_gate
from .progress_ledger import ProgressLedger

__all__ = ["advance_run", "WorkflowEngineError"]

_MAX_STALLS = 3
_RETRY_EVENT = "project.task.retried"


class WorkflowEngineError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _enum(value: Any) -> str:
    return str(value.value if hasattr(value, "value") else value)


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _load(store: Any, project_id: UUID, run_id: UUID) -> tuple[Any, dict, dict[str, str], ProgressLedger, dict]:
    from . import workflow_service

    workflow_service.ensure_schema(store)  # 引擎可能先于 service 被打到（404 也要有表可查）
    row = store.db.execute(
        "SELECT * FROM project_workflow_runs WHERE id = ? AND project_id = ?", (str(run_id), str(project_id))
    ).fetchone()
    if row is None:
        raise WorkflowEngineError("workflow_run_not_found")
    version = store.db.execute(
        "SELECT definition FROM workflow_versions WHERE id = ?", (row["workflow_version_id"],)
    ).fetchone()
    if version is None:
        raise WorkflowEngineError("workflow_version_not_found")
    definition = json.loads(version["definition"])
    node_tasks: dict[str, str] = json.loads(row["node_tasks"] or "{}")
    # 持久化结构：{ledger: <ProgressLedger 字段>, last_signature, attempts}——外层键是引擎的，
    # 内层才是账本；直接 ProgressLedger(**raw) 会被引擎私钥炸掉。
    state = json.loads(row["ledger"] or "{}") or {}
    ledger_raw = state.get("ledger") or {}
    ledger = (
        ProgressLedger(**{**ledger_raw, "facts": tuple(ledger_raw.get("facts", ())), "plan": tuple(ledger_raw.get("plan", ()))})
        if ledger_raw
        else ProgressLedger.new(str(definition.get("workflow", {}).get("name") or "workflow"))
    )
    return row, definition, node_tasks, ledger, state


def _node_gate(store: Any, project_id: UUID, definition: dict, node_tasks: dict[str, str], actor: str) -> list[dict[str, Any]]:
    """门禁评估（W3.3）：交付待审且带 gate_policy 的节点。"""

    outcomes: list[dict[str, Any]] = []
    policies = {str(item.get("id")): item for item in definition.get("gate_policies", []) if isinstance(item, dict)}
    for node in definition.get("nodes", []):
        gate_ref = node.get("gate_policy")
        if not gate_ref or str(node.get("id")) not in node_tasks:
            continue
        policy = policies.get(str(gate_ref))
        if not policy:
            continue
        task_id = node_tasks[str(node["id"])]
        task_row = store.db.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task_row is None or _enum(task_row["status"]) != "WAITING_REVIEW":
            continue
        result = workflow_gate.evaluate_workflow_gate(store, project_id, policy.get("spec"), node_task_id=task_id)
        leaves = [
            {"criterion": leaf.criterion, "family": leaf.family, "verdict": leaf.verdict, "detail": leaf.detail}
            for leaf in result.leaves
        ]
        outcome = {
            "node_id": str(node["id"]),
            "task_id": task_id,
            "gate_policy": str(gate_ref),
            "verdict": result.verdict,
            "all_hold": result.all_hold,
            "leaves": leaves,
            "unchecked": list(result.unchecked),
            "on_block": str(policy.get("on_block") or "blocked"),
        }
        store.add_event(
            UUID(str(project_id)), "project.gate.evaluated", actor,
            {"task_id": task_id, "node_id": str(node["id"]), "verdict": result.verdict, "leaves": leaves},
            actor_kind="system", object_type="task", object_id=UUID(task_id),
        )
        if result.holds:
            store.add_event(
                UUID(str(project_id)), "project.gate.passed", actor,
                {"task_id": task_id, "node_id": str(node["id"])},
                actor_kind="system", object_type="task", object_id=UUID(task_id),
            )
        else:
            store.add_event(
                UUID(str(project_id)), "project.gate.blocked", actor,
                {"task_id": task_id, "node_id": str(node["id"]), "on_block": outcome["on_block"],
                 "unchecked": list(result.unchecked)},
                actor_kind="system", object_type="task", object_id=UUID(task_id),
            )
            if outcome["on_block"] == "escalate_human":
                store.add_event(
                    UUID(str(project_id)), "project.gate.escalated", actor,
                    {"task_id": task_id, "node_id": str(node["id"]), "reason": "gate_blocked_escalate_human"},
                    actor_kind="system", object_type="task", object_id=UUID(task_id),
                )
        outcomes.append(outcome)
    return outcomes


def _bounded_retry(store: Any, project_id: UUID, definition: dict, node_tasks: dict[str, str], engine_state: dict, actor: str) -> list[str]:
    """有界重试：on_fail=retry 的失败节点放回 READY；预算 max_attempts 是硬顶。"""

    retried: list[str] = []
    attempts: dict[str, int] = engine_state.setdefault("attempts", {})
    store_now = _now()
    for node in definition.get("nodes", []):
        node_id = str(node.get("id") or "")
        retry_policy = node.get("retry_policy") or {}
        on_fail = str(node.get("on_fail") or retry_policy.get("on_fail") or "escalate_human")
        if on_fail != "retry" or node_id not in node_tasks:
            continue
        task_id = node_tasks[node_id]
        task_row = store.db.execute("SELECT status, budget FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task_row is None or _enum(task_row["status"]) != "FAILED":
            continue
        budget = json.loads(task_row["budget"] or "{}") if task_row["budget"] else {}
        max_attempts = int(budget.get("max_attempts") or 1)
        used = int(attempts.get(node_id, 1))  # 第一次领取已算一次尝试
        if used >= max_attempts:
            continue  # 预算到顶：保持 FAILED（claim 侧同样强制），升级人工看得到
        backoff = int((node.get("retry_policy") or {}).get("backoff_seconds") or 0)
        store.db.execute("UPDATE tasks SET status = 'READY', updated_at = ? WHERE id = ?", (store_now, task_id))
        attempts[node_id] = used + 1
        store.add_event(
            UUID(str(project_id)), _RETRY_EVENT, actor,
            {"task_id": task_id, "node_id": node_id, "attempt": used + 1, "max_attempts": max_attempts,
             "backoff_seconds": backoff},
            actor_kind="system", object_type="task", object_id=UUID(task_id),
        )
        retried.append(node_id)
    return retried


def _run_deliveries(
    store: Any, project_id: UUID, definition: dict, node_tasks: dict[str, str], engine_state: dict, actor: str, statuses: dict[str, str]
) -> list[dict[str, Any]]:
    """节点 APPROVED 后执行交付适配器（W3.6）。每个节点只交付一次；结果入账本，失败如实记。"""

    deliveries: list[dict[str, Any]] = []
    delivered: list[str] = engine_state.setdefault("delivered", [])
    recorded: dict[str, Any] = engine_state.setdefault("deliveries", {})
    # 两层解析：node.delivery_adapter 引用的是定义里 delivery_adapters[].id，
    # 注册表认的是它的 kind——不解析就变成"拿 id 查注册表"的假失败（测试抓过）。
    adapters_by_id = {
        str(item.get("id")): item for item in definition.get("delivery_adapters", []) if isinstance(item, dict)
    }
    for node in definition.get("nodes", []):
        node_id = str(node.get("id") or "")
        adapter_ref = str(node.get("delivery_adapter") or "")
        if not adapter_ref or node_id not in node_tasks or node_id in delivered:
            continue
        if statuses.get(node_id) != "APPROVED":
            continue
        kind = str((adapters_by_id.get(adapter_ref) or {}).get("kind") or adapter_ref)
        result = workflow_delivery.run_adapter(
            store, project_id, kind, actor=actor, node_id=node_id, task_id=node_tasks[node_id]
        )
        recorded[node_id] = {"kind": kind, **result}
        if result.get("status") not in {"failed", "unknown"}:
            delivered.append(node_id)
        deliveries.append({"node_id": node_id, "kind": kind, **result})
    return deliveries


def _signature(node_tasks: dict[str, str], statuses: dict[str, str]) -> str:
    import hashlib

    joined = json.dumps({node: statuses.get(task, "?") for node, task in sorted(node_tasks.items())}, sort_keys=True)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def advance_run(store: Any, project_id: UUID, run_id: UUID, actor: str) -> dict[str, Any]:
    """推进一轮：重试 → 门禁 → 账本 → 完成判定。幂等安全：无变化 = 纯记账。"""

    actor = str(actor)
    row, definition, node_tasks, ledger, engine_state = _load(store, project_id, run_id)
    if row["status"] != "RUNNING":
        raise WorkflowEngineError("workflow_run_not_running")

    gates = _node_gate(store, project_id, definition, node_tasks, actor)
    retried = _bounded_retry(store, project_id, definition, node_tasks, engine_state, actor)

    statuses: dict[str, str] = {}
    for node_id, task_id in node_tasks.items():
        task_row = store.db.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        statuses[node_id] = _enum(task_row["status"]) if task_row else "?"
    approved_all = bool(node_tasks) and all(status == "APPROVED" for status in statuses.values())
    deliveries = _run_deliveries(store, project_id, definition, node_tasks, engine_state, actor, statuses)

    previous_signature = (json.loads(row["ledger"] or "{}") or {}).get("last_signature")
    current_signature = _signature(node_tasks, statuses)
    progressed = current_signature != previous_signature
    update = {
        "is_request_satisfied": {"answer": approved_all, "reason": "all node tasks APPROVED" if approved_all else "pending"},
        "is_progress_being_made": {"answer": progressed, "reason": "state signature changed" if progressed else "no transition"},
        "is_in_loop": {"answer": bool(retried) and not progressed, "reason": "retry without transition"},
        "instruction_or_question": "advance workflow run",
        "next_speaker": "engine",
    }
    ledger = ledger.apply_round(update, participant_names=["engine"], max_stalls=_MAX_STALLS)

    status = row["status"]
    if approved_all:
        status = "COMPLETED"
    elif ledger.needs_replan:
        status = "STALLED"

    store.db.execute(
        "UPDATE project_workflow_runs SET ledger = ?, status = ?, node_tasks = ?, updated_at = ? WHERE id = ?",
        (
            json.dumps(
                {
                    "ledger": asdict(ledger),
                    "last_signature": current_signature,
                    "attempts": engine_state.get("attempts", {}),
                    "delivered": engine_state.get("delivered", []),
                    "deliveries": engine_state.get("deliveries", {}),
                },
                ensure_ascii=False,
            ),
            status,
            json.dumps(node_tasks, ensure_ascii=False),
            _now(),
            str(run_id),
        ),
    )
    store.db.commit()
    return {
        "run_id": str(run_id),
        "status": status,
        "node_statuses": statuses,
        "gates": gates,
        "retried": retried,
        "deliveries": deliveries,
        "round": ledger.round,
        "stall_count": ledger.stall_count,
        "needs_replan": ledger.needs_replan,
    }
