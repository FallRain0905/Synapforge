"""COST-1 线上验证：用量回报 → 预算判定 → 门禁留痕（跑完自清理）。

四件事（都走真实 store 代码路径，不伪造 SQL）：
  1. 建一条 `max_tokens=100` 的任务，造一次 `total_tokens=250` 的执行 → 完成 Run 时写
     `task.budget_exceeded` 事件（幂等一次）+ 聊天卡片；
  2. 任务详情（`task_usage_summary` / `_task_budget_state`）能看到累计用量与"有数的执行次数"；
  3. 批准被拒（`task_usage_budget_exceeded`），门禁留下 `task:usage_budget_exceeded` 的 blocking finding；
  4. 不报用量的 Run：只有平台观测的耗时，token 字段为空（不把"没数"当成 0）。
清理：按外键顺序删掉临时任务、Run、事件（含 outbox）、聊天卡片与门禁。

用法：cd /opt/math-agent-platform && PYTHONPATH=apps/api:. venv/bin/python /tmp/_cost1_verify.py
"""

from __future__ import annotations

import sys
from uuid import uuid4

sys.path.insert(0, "apps/api")

from app.contracts import (  # noqa: E402
    ProjectCreate,
    ReviewCreate,
    ReviewerKind,
    RunComplete,
    RunCreate,
    RunUsage,
    TaskCreate,
)
from app.store import Store  # noqa: E402

DEMO_NAME = "演示 · 多 Agent 文档撰写"


def main() -> None:
    store = Store("apps/api/data/platform.db")
    project = next((p for p in store.list_projects() if p.name == DEMO_NAME), None)
    if project is None:
        raise SystemExit("没找到演示项目")
    ok = True
    created_tasks: list[str] = []
    created_runs: list[str] = []

    def check(label: str, condition: bool) -> None:
        nonlocal ok
        print(("  [PASS] " if condition else "  [FAIL] ") + label)
        ok = ok and condition

    def run_for(task_id, *, usage: RunUsage | None):
        run = store.create_run(
            project.id,
            RunCreate(agent_id="agent-demo-writer", task_id=task_id, idempotency_key=uuid4().hex),
        )
        created_runs.append(str(run.id))
        done = store.complete_run(
            run.id, RunComplete(success=True, summary="线上验证：用量回报", usage=usage, idempotency_key=uuid4().hex)
        )
        return done

    print("== 1. 超 token 预算：完成时留痕（一次性事件）==")
    over = store.create_task(
        project.id,
        TaskCreate(title="COST1 线上验证：超预算", description="临时", budget={"max_tokens": 100}),
    )
    created_tasks.append(str(over.id))
    done = run_for(over.id, usage=RunUsage(input_tokens=200, output_tokens=50, turns=2, seconds=12.0, source="codex-jsonl"))
    check("用量按 in+out 推导为 250", done.usage.total_tokens == 250)
    check("token 出处保留（codex-jsonl）", done.usage.source == "codex-jsonl")
    check("耗时来源标为执行体", done.usage.seconds_source == "agent")
    events = store.db.execute(
        "SELECT COUNT(*) AS c FROM events WHERE event_type = 'task.budget_exceeded' AND idempotency_key = ?",
        (f"budget-exceeded:{done.id}",),
    ).fetchone()["c"]
    check("task.budget_exceeded 事件恰好一条", events == 1)
    cards = store.db.execute(
        "SELECT COUNT(*) AS c FROM project_messages WHERE content LIKE '%超出预算%' AND ref_task_id = ?", (str(over.id),)
    ).fetchone()["c"]
    check("聊天卡片已出（超出预算）", cards == 1)

    print("== 2. 任务详情能看到累计用量 ==")
    summary = store.task_usage_summary(over.id)
    check("累计 250 tokens、1 次有数", summary["tokens_used"] == 250 and summary["usage_reported_runs"] == 1)
    state = store._task_budget_state(store.db.execute("SELECT * FROM tasks WHERE id = ?", (str(over.id),)).fetchone())
    check("预算态回显 tokens_used 与上限", state["tokens_used"] == 250 and state["max_tokens"] == 100)

    print("== 3. 批准被门禁拦下并留痕 ==")
    store.db.execute("UPDATE tasks SET status = 'WAITING_REVIEW' WHERE id = ?", (str(over.id),))
    store.db.commit()
    reviewer = store.db.execute(
        "SELECT member_id FROM project_memberships WHERE project_id = ? AND role IN ('owner', 'project_lead') LIMIT 1",
        (str(project.id),),
    ).fetchone()["member_id"]
    detail = ""
    try:
        store.create_review(
            project.id,
            ReviewCreate(
                target_type="task",
                target_id=over.id,
                reviewer=reviewer,
                reviewer_kind=ReviewerKind.MEMBER,
                verdict="APPROVED",
                summary="线上验证",
            ),
        )
        check("批准被拒", False)
    except (ValueError, PermissionError, KeyError) as error:
        detail = str(error)
        check("批准被拒（超预算）", "task_usage_budget_exceeded" in detail)
    gate = store.db.execute(
        "SELECT status, blocking_findings FROM gates WHERE project_id = ? AND target_type = 'task' AND target_id = ?",
        (str(project.id), str(over.id)),
    ).fetchone()
    check(
        "门禁 FAILED 且含 task:usage_budget_exceeded",
        bool(gate) and gate["status"] == "FAILED" and "task:usage_budget_exceeded" in (gate["blocking_findings"] or ""),
    )

    print("== 4. 不报用量 = 没数据（不是 0）==")
    silent = store.create_task(project.id, TaskCreate(title="COST1 线上验证：无回报", description="临时", budget={"max_tokens": 100}))
    created_tasks.append(str(silent.id))
    done_silent = run_for(silent.id, usage=None)
    check("token 字段为空", done_silent.usage.total_tokens is None)
    check("耗时来源标为平台观测", done_silent.usage.seconds_source == "platform")
    check(
        "没有超预算事件",
        store.db.execute(
            "SELECT COUNT(*) AS c FROM events WHERE event_type = 'task.budget_exceeded' AND payload LIKE ?", (f"%{silent.id}%",)
        ).fetchone()["c"]
        == 0,
    )

    print("== 5. 清理 ==")
    for run_id in created_runs:
        store.db.execute("DELETE FROM event_outbox WHERE event_id IN (SELECT id FROM events WHERE payload LIKE ?)", (f"%{run_id}%",))
        store.db.execute("DELETE FROM events WHERE payload LIKE ?", (f"%{run_id}%",))
        store.db.execute("DELETE FROM runs WHERE id = ?", (run_id,))
    for task_id in created_tasks:
        store.db.execute("DELETE FROM project_messages WHERE ref_task_id = ?", (task_id,))
        store.db.execute("DELETE FROM gates WHERE target_type = 'task' AND target_id = ?", (task_id,))
        store.db.execute(
            "DELETE FROM event_outbox WHERE event_id IN (SELECT id FROM events WHERE object_id = ? OR payload LIKE ?)",
            (task_id, f"%{task_id}%"),
        )
        store.db.execute("DELETE FROM events WHERE object_id = ? OR payload LIKE ?", (task_id, f"%{task_id}%"))
        store.db.execute("DELETE FROM runs WHERE task_id = ?", (task_id,))
        store.db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    store.db.commit()
    leftovers = store.db.execute("SELECT COUNT(*) FROM tasks WHERE title LIKE 'COST1 线上验证%'").fetchone()[0]
    orphan_runs = store.db.execute(
        "SELECT COUNT(*) FROM runs WHERE id IN (%s)" % ",".join("?" * len(created_runs)), created_runs
    ).fetchone()[0] if created_runs else 0
    check("临时任务与 Run 已清理", leftovers == 0 and orphan_runs == 0)

    store.close()
    print("\n=== 结果：" + ("全部通过" if ok else "有失败项") + " ===")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()