"""AIP-1d 线上验证：预算与显式证据要求走真实 store 代码路径（跑完自清理）。

四件事：
  1. 临时任务设 `max_seconds=60` → 领取时租约被压到 ≤60 秒；续租也不能越过上限；
  2. 临时任务设 `max_attempts=1` → 第一次领取成功；释放后再领被拒 `task_budget_exhausted`，
     且 `task.budget_exhausted` 事件只写一条（幂等）；
  3. 临时任务设 `evidence_requirements=[{run,1}]` → 读时缺口为 1；批准被拒且门禁留下 blocking finding；
  4. 清理：删掉临时任务与它们的租赁/事件/门禁，恢复原状。

用法：cd /opt/math-agent-platform && PYTHONPATH=apps/api:. venv/bin/python /tmp/_aip1d_verify.py
"""

from __future__ import annotations

import sys
from uuid import UUID, uuid4

sys.path.insert(0, "apps/api")

from app.contracts import EvidenceRequirement, ReviewCreate, ReviewerKind, TaskBudget, TaskClaimRequest, TaskCreate  # noqa: E402
from app.store import Store  # noqa: E402

DEMO_NAME = "演示 · 多 Agent 文档撰写"
AGENT_ID = "agent-demo-writer"


def main() -> None:
    store = Store("apps/api/data/platform.db")
    project = next((p for p in store.list_projects() if p.name == DEMO_NAME), None)
    if project is None:
        raise SystemExit("没找到演示项目")
    created: list[str] = []
    ok = True

    def check(label: str, condition: bool) -> None:
        nonlocal ok
        print(("  [PASS] " if condition else "  [FAIL] ") + label)
        ok = ok and condition

    def claim(task_id, *, seconds: int = 3600):
        return store.claim_task(
            task_id,
            TaskClaimRequest(agent_id=AGENT_ID, lease_seconds=seconds, idempotency_key=uuid4().hex),
        )

    print("== 1. max_seconds：租约被压到预算以内，续租不越界 ==")
    timed = store.create_task(
        project.id,
        TaskCreate(title="AIP1d 线上验证：限时", description="临时", budget=TaskBudget(max_seconds=60)),
    )
    created.append(str(timed.id))
    _, lease = claim(timed.id)
    span = (lease.expires_at - lease.issued_at).total_seconds()
    check(f"租约 {span:.0f}s ≤ 60s", span <= 61)
    extended = store.heartbeat_lease(lease.lease_token, AGENT_ID, 3600)
    over = (extended.expires_at - lease.issued_at).total_seconds()
    check(f"续租后 {over:.0f}s 仍未越过上限", over <= 61)
    store.db.execute("UPDATE task_leases SET status = 'RELEASED' WHERE task_id = ?", (str(timed.id),))
    store.db.commit()

    print("== 2. max_attempts：用尽后拒绝领取，且事件只写一条 ==")
    once = store.create_task(
        project.id,
        TaskCreate(title="AIP1d 线上验证：一次机会", description="临时", budget=TaskBudget(max_attempts=1)),
    )
    created.append(str(once.id))
    claim(once.id)
    store.db.execute("UPDATE task_leases SET status = 'RELEASED' WHERE task_id = ?", (str(once.id),))
    store.db.execute("UPDATE tasks SET status = 'READY' WHERE id = ?", (str(once.id),))
    store.db.commit()
    raised = ""
    try:
        claim(once.id)
    except ValueError as error:
        raised = str(error)
    check("第二次领取被拒（task_budget_exhausted）", raised == "task_budget_exhausted")
    try:
        claim(once.id)
    except ValueError:
        pass
    events = store.db.execute(
        "SELECT COUNT(*) AS c FROM events WHERE event_type = 'task.budget_exhausted' AND idempotency_key = ?",
        (f"budget-exhausted:{once.id}",),
    ).fetchone()["c"]
    check("task.budget_exhausted 事件恰好一条（幂等）", events == 1)

    print("== 3. evidence_requirements：读时缺口 + 批准被拒且门禁留痕 ==")
    evidenced = store.create_task(
        project.id,
        TaskCreate(
            title="AIP1d 线上验证：要运行证据",
            description="临时",
            evidence_requirements=[EvidenceRequirement(evidence_type="run", min_count=1, note="必须有运行记录")],
        ),
    )
    created.append(str(evidenced.id))
    gaps = store.task_evidence_gaps(evidenced.id)
    check("缺口为 1 条 run 证据", bool(gaps) and gaps[0]["missing"] == 1)
    store.db.execute("UPDATE tasks SET status = 'WAITING_REVIEW' WHERE id = ?", (str(evidenced.id),))
    store.db.commit()
    # 复核人必须是项目内有 review.approve 权限的成员（用项目里的 project_lead，别用平台种子成员）
    reviewer_row = store.db.execute(
        "SELECT member_id FROM project_memberships WHERE project_id = ? AND role IN ('owner', 'project_lead') LIMIT 1",
        (str(project.id),),
    ).fetchone()
    reviewer = reviewer_row["member_id"] if reviewer_row else "member-001"
    detail = ""
    try:
        store.create_review(
            project.id,
            ReviewCreate(
                target_type="task",
                target_id=evidenced.id,
                reviewer=reviewer,
                reviewer_kind=ReviewerKind.MEMBER,
                verdict="APPROVED",
                summary="线上验证",
            ),
        )
        check("批准被拒", False)
    except (ValueError, PermissionError, KeyError) as error:
        detail = str(error)
        check("批准被拒（缺证据）", "task_evidence_requirements_unmet" in detail)
    gate = store.db.execute(
        "SELECT status, blocking_findings FROM gates WHERE project_id = ? AND target_type = 'task' AND target_id = ?",
        (str(project.id), str(evidenced.id)),
    ).fetchone()
    check(
        "门禁留下 blocking finding（status=FAILED）",
        bool(gate) and gate["status"] == "FAILED" and "task:evidence_requirements" in (gate["blocking_findings"] or ""),
    )

    print("== 4. 清理临时数据 ==")
    # 按外键顺序删：租约/门禁/outbox → 事件 → 任务。
    # `event_outbox.event_id REFERENCES events(id)`——不先删 outbox，删事件会 FK 失败（这次踩到了）。
    sweep = [row["id"] for row in store.db.execute("SELECT id FROM tasks WHERE title LIKE 'AIP1d 线上验证%'")]
    for task_id in sweep:
        store.db.execute("DELETE FROM task_leases WHERE task_id = ?", (task_id,))
        store.db.execute("DELETE FROM project_messages WHERE ref_task_id = ?", (task_id,))
        store.db.execute("DELETE FROM gates WHERE target_type = 'task' AND target_id = ?", (task_id,))
        store.db.execute(
            "DELETE FROM event_outbox WHERE event_id IN ("
            "SELECT id FROM events WHERE object_id = ? OR idempotency_key LIKE ? OR payload LIKE ?)",
            (task_id, f"%{task_id}%", f"%{task_id}%"),
        )
        store.db.execute(
            "DELETE FROM events WHERE object_id = ? OR idempotency_key LIKE ? OR payload LIKE ?",
            (task_id, f"%{task_id}%", f"%{task_id}%"),
        )
        store.db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    store.db.commit()
    left = store.db.execute("SELECT COUNT(*) FROM tasks WHERE title LIKE 'AIP1d 线上验证%'").fetchone()[0]
    stray = store.db.execute(
        "SELECT COUNT(*) FROM task_leases WHERE task_id NOT IN (SELECT id FROM tasks)"
    ).fetchone()[0]
    check("临时任务、租赁与孤儿行已清理", left == 0 and stray == 0)

    store.close()
    print("\n=== 结果：" + ("全部通过" if ok else "有失败项") + " ===")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()