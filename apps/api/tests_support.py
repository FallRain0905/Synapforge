"""测试用的小工具：造一个"另一个成员"（隔离组织需要自己建）。

为什么单独一个模块：多个测试文件都要造别的成员，把建法抄来抄去容易把组织关系抄错
（组织不一致会让"跨组织"用例变成"同组织"用例，测试就失去意义）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.store import DEV_ORG_ID


def ensure_member(store, member_id: str, *, organization_id: str | None = None, team_id: str | None = None) -> str:
    """确保成员存在（幂等）：默认塞进开发组织；给了组织就自建一套组织/队伍。"""

    row = store.db.execute("SELECT id FROM human_members WHERE id = ?", (member_id,)).fetchone()
    if row is not None:
        return member_id
    if organization_id is None:
        organization_id = DEV_ORG_ID
        team = store.db.execute("SELECT id FROM teams WHERE organization_id = ? LIMIT 1", (organization_id,)).fetchone()
        team_id = str(team["id"])
    else:
        team_id = team_id or str(uuid4())
        store.db.execute(
            "INSERT OR IGNORE INTO organizations (id, name, slug, created_at) VALUES (?, ?, ?, ?)",
            (organization_id, f"隔离组织 {member_id}", f"iso-{member_id}", datetime.now(UTC).isoformat()),
        )
        store.db.execute(
            "INSERT OR IGNORE INTO teams (id, organization_id, name, created_at) VALUES (?, ?, ?, ?)",
            (team_id, organization_id, "隔离队伍", datetime.now(UTC).isoformat()),
        )
    store.db.execute(
        "INSERT INTO human_members (id, organization_id, team_id, email, display_name, status, created_at) VALUES (?, ?, ?, ?, ?, 'active', ?)",
        (member_id, organization_id, team_id, f"{member_id}@example.test", member_id, datetime.now(UTC).isoformat()),
    )
    store.db.commit()
    return member_id


__all__ = ["ensure_member"]
