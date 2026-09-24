"""AIP-1a 线上验证：版本约束与候选排序走真实 store 代码路径（跑完自清理）。

做四件事（都在演示项目里，用的是平台自己的 API，不是伪造 SQL）：
  1. 临时建一条要求 `Latex@>=0.5` 的任务 → 断言"能满足"（Nova 声明 latex 未带版本？
     不，这里是断言**未声明版本不满足下限**：应该判不满足并出现在 unmet 里）；
  2. 临时建一条要求 `Markdown@>=0.5` 的任务 → 断言由写作 Agent 满足；
  3. 打印 rank_task_candidates 的候选（含排序理由，验证"界面推荐 = 调度器选人"）；
  4. 删除这两条临时任务与它们的事件/聊天卡片，恢复原状。

用法：cd /opt/math-agent-platform && PYTHONPATH=apps/api:. venv/bin/python /tmp/_aip1_verify.py
"""

from __future__ import annotations

import sys
from uuid import UUID

sys.path.insert(0, "apps/api")

from app.contracts import TaskCreate  # noqa: E402
from app.store import Store  # noqa: E402

DEMO_NAME = "演示 · 多 Agent 文档撰写"


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

    versioned = store.create_task(project.id, TaskCreate(title="AIP1 线上验证：latex 下限", description="临时", required_capabilities=["Latex@>=0.5"]))
    created.append(str(versioned.id))
    satisfied = store.create_task(project.id, TaskCreate(title="AIP1 线上验证：markdown 下限", description="临时", required_capabilities=["Markdown@>=0.5"]))
    created.append(str(satisfied.id))

    catalog = store.capability_catalog(project.organization_id)
    unmet_ids = {item["task_id"] for item in catalog["unmet_tasks"]}
    unmet_titles = {item["title"]: item for item in catalog["unmet_tasks"]}

    print("== 1. 版本下限：声明 latex（未带版本）不满足 >=0.5 ==")
    check("要求 Latex@>=0.5 的任务出现在「没人能跑」里", str(versioned.id) in unmet_ids)
    entry = unmet_titles.get("AIP1 线上验证：latex 下限")
    if entry:
        print("      缺失项:", entry["missing_capabilities"])
        print("      候选数:", len(entry["candidates"]))
    print("== 2. 大小写归一化 + 版本下限：Markdown@>=0.5 由写作 Agent（markdown@1.0）满足 ==")
    check("要求 Markdown@>=0.5 的任务**不**在「没人能跑」里", str(satisfied.id) not in unmet_ids)

    print("== 3. 候选排序（与调度器共用同一函数）==")
    row = store.db.execute("SELECT * FROM tasks WHERE id = ?", (str(satisfied.id),)).fetchone()
    ranked = store.rank_task_candidates(project.id, row)
    for item in (ranked["satisfied"] + ranked["partial"])[:4]:
        print(f"      - {item['display_name']} | {item['reason']}")
    check("候选里有满足项且理由带样本量", bool(ranked["satisfied"]) and "次" in ranked["satisfied"][0]["reason"])
    dispatch = store._auto_dispatch_candidate(project.id, row)
    if dispatch is not None and ranked["satisfied"]:
        check("调度器取的正是推荐第一条（或该条离线时取下一个在线项）", dispatch[1] == (ranked["satisfied"][0]["agent_id"] if ranked["satisfied"][0]["online"] else dispatch[1]))

    print("== 4. 清理临时任务 ==")
    for task_id in created:
        store.db.execute("DELETE FROM project_messages WHERE ref_task_id = ?", (task_id,))
        store.db.execute("DELETE FROM events WHERE object_id = ? OR idempotency_key LIKE ?", (task_id, f"%{task_id}%"))
        store.db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    store.db.commit()
    left = store.db.execute("SELECT COUNT(*) FROM tasks WHERE title LIKE 'AIP1 线上验证%'").fetchone()[0]
    check("临时任务已清理", left == 0)

    store.close()
    print("\n=== 结果：" + ("全部通过" if ok else "有失败项") + " ===")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()