"""阶段 9 最小切片：健康/可观测、配额与成本统计。

刻意保持“只读统计 + 明确口径”：

- `platform_metrics` 汇总平台级事实（项目、任务、运行、成果物、事件、outbox 待投递）；
- `project_usage` 汇总单个项目的用量（运行、存储、Token 计数、事件）并给出**标注为估算**的成本；
- `enforce_quota` 在写路径上限制运行数、成果物数与存储字节，超限抛 `QuotaExceeded`
  （路由层转为 429），限额可由 `PLATFORM_QUOTA_*` 环境变量覆盖。

统计口径不猜测模型 token：平台只掌握自身事实（能力 Token 数量、运行与存储），
成本按可配置单价换算，字段名与说明都写成 `estimated_*`。
"""

from __future__ import annotations

import os
from typing import Any, Mapping
from uuid import UUID

PLATFORM_VERSION = "0.1.0"

DEFAULT_QUOTAS: dict[str, int] = {
    "max_runs_per_project": 2000,
    "max_artifacts_per_project": 5000,
    "max_storage_bytes_per_project": 5 * 1024 ** 3,
    "max_projects_per_organization": 50,
}

DEFAULT_COST_RATES: dict[str, float] = {
    # 单价仅用于演示口径，可按部署环境覆盖。
    "cost_per_run": 0.0,
    "cost_per_gb_month": 0.0,
}

COST_ENV_KEYS = {
    "cost_per_run": "PLATFORM_COST_PER_RUN",
    "cost_per_gb_month": "PLATFORM_COST_PER_GB_MONTH",
}

RESOURCE_QUOTA_KEYS = {
    "runs": "max_runs_per_project",
    "artifacts": "max_artifacts_per_project",
    "storage": "max_storage_bytes_per_project",
    "projects": "max_projects_per_organization",
}


class QuotaExceeded(RuntimeError):
    """稳定错误族：超出配额。"""

    def __init__(self, resource: str, limit: int, used: int) -> None:
        super().__init__(f"quota_exceeded_{resource}:{used}/{limit}")
        self.resource = resource
        self.limit = limit
        self.used = used
        self.code = f"quota_exceeded_{resource}"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        return default
    return value if value >= 0 else default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(str(raw).strip())
    except ValueError:
        return default
    return value if value >= 0 else default


def quota_limits() -> dict[str, int]:
    """限额：默认值 + `PLATFORM_QUOTA_<大写键名>` 覆盖。"""

    limits: dict[str, int] = {}
    for key, default in DEFAULT_QUOTAS.items():
        limits[key] = _env_int(f"PLATFORM_QUOTA_{key.upper()}", default)
    return limits


def cost_rates() -> dict[str, float]:
    rates: dict[str, float] = {}
    for key, default in DEFAULT_COST_RATES.items():
        # 显式列环境变量名，避免把 cost_per_run 拼成 PLATFORM_COST_COST_PER_RUN。
        rates[key] = _env_float(COST_ENV_KEYS[key], default)
    return rates


def _count(store: Any, sql: str, params: tuple = ()) -> int:
    row = store.db.execute(sql, params).fetchone()
    if row is None:
        return 0
    return int(row[0] if not isinstance(row, Mapping) else list(row.values())[0])


def platform_metrics(store: Any) -> dict[str, Any]:
    """平台级事实汇总（不包含任何项目内容）。"""

    tasks_by_status = {
        row["status"]: row["count"]
        for row in store.db.execute("SELECT status, COUNT(*) AS count FROM tasks GROUP BY status")
    }
    runs_by_status = {
        row["status"]: row["count"]
        for row in store.db.execute("SELECT status, COUNT(*) AS count FROM runs GROUP BY status")
    }
    storage_bytes = _count(store, "SELECT COALESCE(SUM(size_bytes), 0) FROM artifacts")
    pending_outbox = _count(store, "SELECT COUNT(*) FROM event_outbox WHERE status = 'PENDING'")
    active_devices = _count(store, "SELECT COUNT(*) FROM devices WHERE status = 'active'")
    return {
        "platform_version": PLATFORM_VERSION,
        "storage_backend": "sqlite",
        "counts": {
            "organizations": _count(store, "SELECT COUNT(*) FROM organizations"),
            "projects": _count(store, "SELECT COUNT(*) FROM projects"),
            "tasks": _count(store, "SELECT COUNT(*) FROM tasks"),
            "runs": _count(store, "SELECT COUNT(*) FROM runs"),
            "artifacts": _count(store, "SELECT COUNT(*) FROM artifacts"),
            "approved_artifacts": _count(store, "SELECT COUNT(*) FROM artifacts WHERE status = 'APPROVED'"),
            "events": _count(store, "SELECT COUNT(*) FROM events"),
            "reviews": _count(store, "SELECT COUNT(*) FROM reviews"),
            "gates": _count(store, "SELECT COUNT(*) FROM gates"),
            "devices": _count(store, "SELECT COUNT(*) FROM devices"),
            "active_devices": active_devices,
        },
        "tasks_by_status": tasks_by_status,
        "runs_by_status": runs_by_status,
        "storage_bytes": storage_bytes,
        "pending_outbox": pending_outbox,
        "quotas": quota_limits(),
    }


def project_usage(store: Any, project_id: UUID) -> dict[str, Any]:
    """单项目用量与成本估算（运行、存储、Token、事件）。"""

    project = store.get_project(project_id)
    runs = list(store.list_runs(project_id))
    artifacts = list(store.list_artifacts(project_id))
    storage_bytes = sum(int(item.size_bytes or 0) for item in artifacts)
    stream_bytes = 0
    for run in runs:
        stream_bytes += len((run.stdout or "").encode("utf-8")) + len((run.stderr or "").encode("utf-8"))

    capability_tokens = _count(
        store,
        """
        SELECT (SELECT COUNT(*) FROM agent_project_grants WHERE project_id = ?)
             + (SELECT COUNT(*) FROM device_project_grants WHERE project_id = ?)
        """,
        (str(project_id), str(project_id)),
    )
    events = _count(store, "SELECT COUNT(*) FROM events WHERE project_id = ?", (str(project_id),))
    tasks = _count(store, "SELECT COUNT(*) FROM tasks WHERE project_id = ?", (str(project_id),))

    rates = cost_rates()
    gb = storage_bytes / (1024 ** 3)
    estimated_cost = round(len(runs) * rates["cost_per_run"] + gb * rates["cost_per_gb_month"], 6)

    return {
        "project_id": str(project_id),
        "project_name": project.name,
        "competition_pack": project.competition_pack,
        "counts": {
            "tasks": tasks,
            "runs": len(runs),
            "artifacts": len(artifacts),
            "approved_artifacts": sum(1 for item in artifacts if str(item.status) == "APPROVED"),
            "events": events,
            "capability_tokens": capability_tokens,
        },
        "runs_by_status": {
            status: sum(1 for run in runs if str(run.status) == status)
            for status in {str(run.status) for run in runs}
        },
        "storage": {
            "artifact_bytes": storage_bytes,
            "stream_bytes": stream_bytes,
            "total_bytes": storage_bytes + stream_bytes,
        },
        "estimated_cost": {
            "amount": estimated_cost,
            "currency": "CNY",
            "basis": dict(rates),
            "note": "按可配置单价换算的估算值；平台不采集模型 token 明细",
        },
        "quotas": quota_status(store, project_id),
    }


def quota_status(store: Any, project_id: UUID) -> dict[str, Any]:
    """当前用量与限额对照（供前端提示与写路径判定复用）。"""

    limits = quota_limits()
    runs = _count(store, "SELECT COUNT(*) FROM runs WHERE project_id = ?", (str(project_id),))
    artifacts = _count(store, "SELECT COUNT(*) FROM artifacts WHERE project_id = ?", (str(project_id),))
    storage = _count(
        store, "SELECT COALESCE(SUM(size_bytes), 0) FROM artifacts WHERE project_id = ?", (str(project_id),)
    )
    return {
        "limits": limits,
        "used": {"runs": runs, "artifacts": artifacts, "storage_bytes": storage},
        # 与 enforce_quota 同口径：used >= limit 表示已无余量（再加一条即被拒）。
        "exceeded": {
            "runs": runs >= limits["max_runs_per_project"],
            "artifacts": artifacts >= limits["max_artifacts_per_project"],
            "storage": storage >= limits["max_storage_bytes_per_project"],
        },
    }


def enforce_quota(store: Any, project_id: UUID, *, resource: str) -> None:
    """写路径配额判定；超限抛 `QuotaExceeded`。"""

    if resource not in RESOURCE_QUOTA_KEYS:
        raise ValueError(f"quota_resource_unknown:{resource}")
    limits = quota_limits()
    limit = limits[RESOURCE_QUOTA_KEYS[resource]]
    if resource == "runs":
        used = _count(store, "SELECT COUNT(*) FROM runs WHERE project_id = ?", (str(project_id),))
    elif resource == "artifacts":
        used = _count(store, "SELECT COUNT(*) FROM artifacts WHERE project_id = ?", (str(project_id),))
    elif resource == "storage":
        used = _count(
            store, "SELECT COALESCE(SUM(size_bytes), 0) FROM artifacts WHERE project_id = ?", (str(project_id),)
        )
    else:  # projects
        used = _count(store, "SELECT COUNT(*) FROM projects")
    if used >= limit:
        raise QuotaExceeded(resource, limit, used)


__all__ = [
    "DEFAULT_COST_RATES",
    "DEFAULT_QUOTAS",
    "PLATFORM_VERSION",
    "QuotaExceeded",
    "RESOURCE_QUOTA_KEYS",
    "cost_rates",
    "enforce_quota",
    "platform_metrics",
    "project_usage",
    "quota_limits",
    "quota_status",
]