"""删除「演示 · 多 Agent 文档撰写」项目与演示 Agent（按外键顺序），供重新种入带正文的演示。

只动演示数据：项目名以「演示 ·」开头 + 两个 demo agent。其余一律不碰。
用法：python scripts/deploy/_demo_reseed.py [db路径]
"""
import sqlite3
import sys

DB = sys.argv[1] if len(sys.argv) > 1 else "apps/api/data/platform.db"
db = sqlite3.connect(DB)
db.row_factory = sqlite3.Row

project = db.execute("SELECT id, name FROM projects WHERE name LIKE '演示 ·%'").fetchone()
if not project:
    print("没有演示项目，无需清理")
    db.close()
    raise SystemExit(0)

pid = project["id"]
tables = [row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
touched: list[str] = []
for name in tables:
    columns = [column["name"] for column in db.execute(f"PRAGMA table_info({name})")]
    if "project_id" in columns:
        cursor = db.execute(f"DELETE FROM {name} WHERE project_id = ?", (pid,))
        if cursor.rowcount:
            touched.append(f"{name} x{cursor.rowcount}")
db.execute("DELETE FROM projects WHERE id = ?", (pid,))

# 演示 Agent（全局表，按 id 精确删）
deleted_agents = 0
for agent_id in ("agent-demo-writer", "agent-demo-checker"):
    deleted_agents += db.execute("DELETE FROM agents WHERE agent_id = ?", (agent_id,)).rowcount
db.commit()
print("已清理项目:", project["name"])
print("清理的表:", touched)
print("删除演示 Agent:", deleted_agents)
print("剩余项目数:", db.execute("SELECT COUNT(*) AS c FROM projects").fetchone()["c"])
db.close()