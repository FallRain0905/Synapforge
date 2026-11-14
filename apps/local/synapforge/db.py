"""本地事实源：SQLite 上的 `Store`，开 WAL 以支持多进程并发写。

多个 Agent 进程同时读写同一个状态文件，是本地形态的前提。
`Store` 自己只设了 `PRAGMA foreign_keys`——WAL 与 busy_timeout 在这里补。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .paths import db_path

BUSY_TIMEOUT_MS = 15000


def _local_store_class() -> type:
    from app.store import DEFAULT_ORG_NAME, DEV_ORG_ID, DEV_TEAM_ID, Store, now

    class LocalStore(Store):
        """本地事实源：**不播种演示数据**。

        平台 `Store._seed()` 是开发态便利——预置演示项目、演示 Agent，并在
        `_seed_identity()` 里用 `CROSS JOIN agents × projects` 把**每个 Agent 授权到每个
        项目**。对本地单机事实源这是双重污染：会凭空出现 agent-nova/agent-mira/
        agent-aster，而且那个 cross join 会**静默绕过项目隔离**（谁能访问哪个项目本来是
        靠 `agent_project_grants` 行表达的，播种把它变成"人人有权"）。

        这里保留身份行（`create_project` 要求 team 属于 organization，没有团队行就建不了
        项目），去掉演示项目与那条 cross join 授权。
        """

        def _seed(self) -> None:
            # 演示项目/任务/成果物/Agent 一律不播——本地库只装真实产生的数据。
            self._seed_identity()

        def _seed_identity(self) -> None:
            created = now()
            self.db.execute(
                "INSERT OR IGNORE INTO organizations (id, name, slug, created_at) VALUES (?, ?, 'local', ?)",
                (DEV_ORG_ID, DEFAULT_ORG_NAME, created))
            self.db.execute(
                "INSERT OR IGNORE INTO teams (id, organization_id, name, created_at) VALUES (?, ?, '本地团队', ?)",
                (DEV_TEAM_ID, DEV_ORG_ID, created))
            self.db.execute(
                "INSERT OR IGNORE INTO human_members (id, organization_id, team_id, email, display_name, status, created_at)"
                " VALUES ('member-001', ?, ?, 'local@synapforge.local', '本地操作者', 'active', ?)",
                (DEV_ORG_ID, DEV_TEAM_ID, created))
            self.db.execute(
                "INSERT OR IGNORE INTO memberships (member_id, team_id, role, created_at)"
                " VALUES ('member-001', ?, 'owner', ?)",
                (DEV_TEAM_ID, created))
            self.db.execute(
                "UPDATE projects SET organization_id = COALESCE(organization_id, ?),"
                " team_id = COALESCE(team_id, ?), created_by = COALESCE(created_by, 'member-001')",
                (DEV_ORG_ID, DEV_TEAM_ID))
            self.db.commit()

    return LocalStore


def open_store(path: str | Path | None = None) -> Any:
    """打开（必要时创建）本地状态库。返回不播种的 `Store`，带 WAL 与完整协议表。"""

    from app import coordination_store, workflow_service

    target = Path(path) if path is not None else db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    store = _local_store_class()(target)
    # 协议表由各协议模块的 ensure_schema 建；在这里一次建齐，本地库一打开就是协议就绪的。
    coordination_store.ensure_schema(store)
    workflow_service.ensure_schema(store)
    # WAL：读不阻塞写，写不阻塞读。busy_timeout：并发写时给重试窗口而不是立刻 SQLITE_BUSY。
    store.db.execute("PRAGMA journal_mode=WAL")
    store.db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    store.db.commit()
    return store


def journal_mode(store: Any) -> str:
    return str(store.db.execute("PRAGMA journal_mode").fetchone()[0])


def open_readonly_connection(path: str | Path | None = None) -> Any:
    """只读连接——本地查看器专用（护栏一：机制上无法写，不是纪律上不写）。

    不走 `Store`：`Store.__init__` 会建表与播种，那本身就是写。
    """

    import sqlite3

    target = Path(path) if path is not None else db_path()
    conn = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn
