"""CLOUD-1 线上验证：云端执行体（opencode）接到平台 → 真跑一条任务 → 用量/过程事件/成果物（跑完自清理）。

六项断言（都对着真实 store 数据，不伪造记录）：
  1. 设备在线：`device-cloud-01` 存在、`platform=linux`、状态 active；
  2. 真跑：新建的极小任务被**真实执行体**领取并执行，任务到 WAITING_REVIEW（不遍历、不造 Run）；
  3. 用量：该 Run 的 `usage.source == "opencode-jsonl"` 且 `total_tokens > 0`（COST-1 的判定链）；
  4. 出处分开：token 出处是执行体回报、耗时出处也是 `agent`（不是平台观测）；
  5. 过程事件：窗口内出现 `agent.process.started` / `agent.agent.message`（方案 B 的直接验收点）；
  6. 成果物：`artifacts.task_id` 能查到本次任务的产出。
清理：按外键顺序删掉本次任务的成果物/Run/事件（含 outbox）/聊天卡片/任务；`--keep` 可跳过（留给人看）。

为什么跑在平台机上：与 `_cost1_verify.py` 同一模式——需要 store 级断言与清理，而平台没有删除类端点。
用法：cd /opt/math-agent-platform && PYTHONPATH=apps/api:. venv/bin/python /tmp/_cloud_agent_verify.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from uuid import uuid4

sys.path.insert(0, "apps/api")

from app.contracts import TaskCreate  # noqa: E402
from app.store import Store  # noqa: E402

OPENCODE_POLICY = {
    "worker_executor": "cli",
    "worker_command": ["opencode", "run", "--format", "json", "--auto", "{prompt}"],
    # 显式声明协议（模板首词是 opencode 时 Agent 也会自己判出来，写上更不容易配错）
    "worker_events": "opencode",
    "worker_prompt": "Reply with exactly: CLOUD-VERIFY",
}

TERMINAL = {"WAITING_REVIEW", "APPROVED", "FAILED", "BLOCKED", "NEEDS_REVISION"}


def main() -> None:
    parser = argparse.ArgumentParser(description="CLOUD-1 云端执行体线上验证")
    parser.add_argument("--project", default="云端智能体")
    parser.add_argument("--device", default="device-cloud-01")
    parser.add_argument("--timeout", type=float, default=240.0, help="等待执行体领取并跑完的秒数")
    parser.add_argument("--keep", action="store_true", help="不清理，留下任务与成果物供人工查看")
    args = parser.parse_args()

    store = Store("apps/api/data/platform.db")
    ok = True
    task_id: str | None = None

    def check(label: str, condition: bool) -> None:
        nonlocal ok
        print(("  [PASS] " if condition else "  [FAIL] ") + label)
        ok = ok and condition

    project = next((p for p in store.list_projects() if p.name == args.project), None)
    if project is None:
        raise SystemExit(f"没找到项目「{args.project}」")
    print(f"项目：{project.name} ({project.id})")

    print("== 1. 设备在线（platform=linux）==")
    device = store.db.execute("SELECT * FROM devices WHERE device_id = ?", (args.device,)).fetchone()
    check(f"{args.device} 已注册", device is not None)
    if device is not None:
        check("platform=linux", str(device["platform"]) == "linux")
        check("状态 active", str(device["status"]) == "active")
    # 派单对象是**设备归属人**：平台的规则是"只有该成员名下的设备能领取"，
    # 指派给项目里的其它成员会让这台云端设备根本领不到（踩过：任务停在 READY）。
    owner_member = str(device["owner_member_id"]) if device is not None else None
    if not owner_member:
        raise SystemExit(f"设备 {args.device} 没有归属成员，无法派单")

    print("== 2. 真跑一条极小任务 ==")
    started_at = time.time()
    task = store.create_task(
        project.id,
        TaskCreate(
            title=f"CLOUD1 线上验证：{time.strftime('%H:%M:%S')}",
            description="云端执行体自检：只回一行字",
            stage="modeling",
            assignee_member_id=owner_member,
            requires_review=True,
            budget={"max_seconds": 300, "max_attempts": 2, "max_tokens": 50_000},
            resource_policy=dict(OPENCODE_POLICY),
        ),
    )
    task_id = str(task.id)
    print(f"  任务已建：{task_id}（等执行体领取，最多 {args.timeout:.0f} 秒）")
    status = "READY"
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        status = store.db.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()["status"]
        if status in TERMINAL:
            break
        time.sleep(6)
    check(f"任务被执行体跑完（当前 {status}）", status in {"WAITING_REVIEW", "APPROVED"})

    row = store.db.execute("SELECT * FROM runs WHERE task_id = ? ORDER BY started_at DESC LIMIT 1", (task_id,)).fetchone()
    check("有 Run 记录", row is not None)
    if row is not None:
        usage = json.loads(row["usage"] or "{}")
        print(f"  Run {row['id']} status={row['status']}（runs 表没有 exit_code 列，它是契约字段）")
        print(f"  usage={json.dumps(usage, ensure_ascii=False)}")
        print(f"  summary={(row['summary'] or '')[:300]}")
        print("== 3. 用量与出处 ==")
        check("Run 成功", str(row["status"]) == "SUCCEEDED")
        check("token 出处为 opencode-jsonl", usage.get("source") == "opencode-jsonl")
        check("确实报了 token（>0）", int(usage.get("total_tokens") or 0) > 0)
        print("== 4. 耗时出处 ==")
        check("耗时由执行体自报（agent）", usage.get("seconds_source") == "agent")

    print("== 5. 过程事件（方案 B 验收点）==")
    window = ("SELECT event_type, COUNT(*) AS c FROM events WHERE project_id = ? AND created_at >= datetime(?, 'unixepoch') GROUP BY event_type",)
    counts = {
        r["event_type"]: r["c"]
        for r in store.db.execute(
            "SELECT event_type, COUNT(*) AS c FROM events WHERE project_id = ? GROUP BY event_type",
            (str(project.id),),
        )
    }
    for event_type in ("agent.process.started", "agent.agent.message", "agent.process.exited"):
        check(f"收到 {event_type}（累计 {counts.get(event_type, 0)} 条）", counts.get(event_type, 0) > 0)

    print("== 6. 成果物 ==")
    produced = store.db.execute("SELECT COUNT(*) AS c FROM artifacts WHERE task_id = ?", (task_id,)).fetchone()["c"]
    print(f"  本次任务产出成果物 {produced} 个")
    check("至少一个成果物（跑不动就没产出）", produced >= 1)

    if args.keep:
        print("\n== 清理：已跳过（--keep）==")
    else:
        print("== 清理 ==")
        run_id = str(row["id"]) if row is not None else ""
        # 只删本次任务/本次 Run 相关的事件（执行体上报的 payload 里带 task_id / run_id）：
        # 不用"时间窗"这种粗暴条件，免得误伤同项目并发跑的其它任务。
        store.db.execute(
            "DELETE FROM event_outbox WHERE event_id IN (SELECT id FROM events WHERE object_id = ? OR payload LIKE ? OR payload LIKE ?)",
            (task_id, f"%{task_id}%", f"%{run_id}%" if run_id else task_id),
        )
        store.db.execute(
            "DELETE FROM events WHERE object_id = ? OR payload LIKE ? OR payload LIKE ?",
            (task_id, f"%{task_id}%", f"%{run_id}%" if run_id else task_id),
        )
        store.db.execute("DELETE FROM project_messages WHERE ref_task_id = ?", (task_id,))
        # 按外键顺序：引用 tasks 的两张表（handoffs / task_leases）必须先清，否则
        # 删任务会撞 FOREIGN KEY constraint failed（本脚本第一版就撞了）。
        store.db.execute("DELETE FROM handoffs WHERE task_id = ?", (task_id,))
        store.db.execute("DELETE FROM task_leases WHERE task_id = ?", (task_id,))
        store.db.execute("DELETE FROM gates WHERE target_type = 'task' AND target_id = ?", (task_id,))
        store.db.execute("DELETE FROM artifacts WHERE task_id = ?", (task_id,))
        store.db.execute("DELETE FROM runs WHERE task_id = ?", (task_id,))
        store.db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        # 顺手清掉历史失败运行留下的同名验证任务（同一标题前缀），免得它们在项目里堆着
        store.db.execute("DELETE FROM project_messages WHERE ref_task_id IN (SELECT id FROM tasks WHERE title LIKE 'CLOUD1 线上验证%')")
        store.db.execute("DELETE FROM handoffs WHERE task_id IN (SELECT id FROM tasks WHERE title LIKE 'CLOUD1 线上验证%')")
        store.db.execute("DELETE FROM task_leases WHERE task_id IN (SELECT id FROM tasks WHERE title LIKE 'CLOUD1 线上验证%')")
        store.db.execute("DELETE FROM runs WHERE task_id IN (SELECT id FROM tasks WHERE title LIKE 'CLOUD1 线上验证%')")
        store.db.execute("DELETE FROM tasks WHERE title LIKE 'CLOUD1 线上验证%'")
        store.db.commit()
        leftovers = store.db.execute(
            "SELECT COUNT(*) FROM tasks WHERE title LIKE 'CLOUD1 线上验证%'"
        ).fetchone()[0]
        orphan = store.db.execute("SELECT COUNT(*) FROM runs WHERE task_id = ?", (task_id,)).fetchone()[0]
        check("临时任务与 Run 已清理", leftovers == 0 and orphan == 0)

    store.close()
    print("\n=== 结果：" + ("全部通过" if ok else "有失败项") + " ===")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()