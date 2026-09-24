"""W-3 部署后验证（服务器上就地跑，零副作用）：

1. 确认部署的代码里**有**调度器（`auto_dispatch_tick` 可调用）；
2. 确认它对线上项目**不动手**——线上项目若都不是 auto 模式，tick 必须返回空、且不新增事件/消息；
3. 顺带打印每个项目的 task_mode，便于人工确认（要试全自动时，在界面上切一下即可）。
"""
import sys

sys.path.insert(0, "/opt/math-agent-platform/apps/api")
from app.store import Store

store = Store("/opt/math-agent-platform/apps/api/data/platform.db")


def counts() -> tuple[int, int]:
    events = store.db.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]
    messages = store.db.execute("SELECT COUNT(*) AS c FROM project_messages").fetchone()["c"]
    return events, messages


before = counts()
print("调度器可用:", callable(getattr(store, "auto_dispatch_tick", None)))
projects = store.db.execute("SELECT id, name, task_mode FROM projects ORDER BY created_at").fetchall()
print("项目:", [(row["name"], row["task_mode"]) for row in projects])

acted = []
for row in projects:
    events = store.auto_dispatch_tick(row["id"])
    if events:
        acted.append((row["name"], [str(event.event_type) for event in events]))
print("本次 tick 的动作:", acted or "无（所有项目都不是 auto 模式）")

after = counts()
print(f"事件 {before[0]} → {after[0]}；消息 {before[1]} → {after[1]}（应完全相等：验证零副作用）")
print("零副作用:", before == after)
store.close()