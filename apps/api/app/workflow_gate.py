"""工作流门禁运行器（W3.3，实施计划第五期）。

把工作包 C 的确定性验收判定器（``acceptance.py``）接进平台：
- **文件探针**：以成果物库为平台侧的"文件现实"——agent 产出只有成为 artifact
  平台才见得到字节，所以 ``file:<path>`` 按成果物的 name/source_path 匹配，
  stat/read 走成果物行与内容存储。工作区里的中间文件平台看不到，如实 UNVERIFIED
  （等执行体侧上报工作区快照再扩展——fail-closed，不猜）。
- **决策查询**：``artifact:<type> approved`` 查项目内该类型已批准成果物；
  ``review:<role> concluded`` 在给了节点任务时查"该任务及其产物上的、由该角色
  给出的复核结论"，没给节点就查项目内 reviewer 名字匹配的复核。
- **tests_passed**：v1 平台没有测试输出记录，一律 UNVERIFIED（显式口径，
  不假装能判定）。
- 门禁条件必须可判定（schema §1 规则 4）；本模块只评估、不自动批准——
  人工审核与"任务成功 ≠ 成果物已批准"的红线原样保留。
"""

from __future__ import annotations

from typing import Any

from . import acceptance

__all__ = ["evaluate_workflow_gate", "ArtifactFileProbe", "ProjectDecisions"]


def _artifact_file_stat(row: Any) -> acceptance.FileStat:
    size = row["size_bytes"]
    return acceptance.FileStat(exists=True, size=int(size) if size is not None else None)


class ArtifactFileProbe:
    """acceptance.FileProbe 的成果物库实现（相对路径 → 成果物匹配）。"""

    def __init__(self, store: Any, project_id: Any) -> None:
        self._store = store
        self._project_id = str(project_id)

    def _match(self, path: str):
        normalized = str(path or "").strip().replace("\\", "/").lstrip("/")
        if not normalized or normalized.startswith("..") or "/../" in f"/{normalized}":
            return None
        row = self._store.db.execute(
            "SELECT id, name, size_bytes, source_path, status FROM artifacts"
            " WHERE project_id = ? AND status != 'ARCHIVED' AND (source_path = ? OR name = ?)"
            " ORDER BY created_at DESC LIMIT 1",
            (self._project_id, normalized, normalized.rsplit("/", 1)[-1]),
        ).fetchone()
        return row

    def stat(self, path: str) -> acceptance.FileStat | None:
        row = self._match(path)
        return _artifact_file_stat(row) if row is not None else None

    def read(self, path: str, max_bytes: int) -> bytes | None:
        row = self._match(path)
        if row is None:
            return None
        try:
            content = self._store.get_artifact_content(row["id"])
        except Exception:  # noqa: BLE001 - 探针语义：读不到 = None，不抛
            return None
        if content is None:
            return None
        return bytes(content[: max(1, int(max_bytes))])


class ProjectDecisions:
    """acceptance.DecisionQuery 的平台实现（成果物批准 / 复核结论）。"""

    def __init__(self, store: Any, project_id: Any, node_task_id: str | None = None) -> None:
        self._store = store
        self._project_id = str(project_id)
        self._node_task_id = str(node_task_id) if node_task_id else None

    def artifact_approved(self, artifact_type: str) -> bool | None:
        # 节点上下文内判定（引擎传入 node_task_id）："本节点产出了已批准的该类型成果物"
        # ——项目级全局查询会被同类型的历史产物污染（e2e 实测抓到）。无上下文时退回项目级。
        if self._node_task_id:
            row = self._store.db.execute(
                "SELECT 1 FROM artifacts WHERE project_id = ? AND artifact_type = ? AND task_id = ?"
                " AND status = 'APPROVED' LIMIT 1",
                (self._project_id, str(artifact_type), self._node_task_id),
            ).fetchone()
            return row is not None
        row = self._store.db.execute(
            "SELECT 1 FROM artifacts WHERE project_id = ? AND artifact_type = ? AND status = 'APPROVED' LIMIT 1",
            (self._project_id, str(artifact_type)),
        ).fetchone()
        return row is not None

    def review_concluded(self, role: str) -> bool | None:
        role = str(role or "").strip()
        if not role:
            return None
        if self._node_task_id:
            row = self._store.db.execute(
                "SELECT 1 FROM reviews WHERE project_id = ? AND target_id IN"
                " (SELECT id FROM artifacts WHERE task_id = ? UNION SELECT ?)"
                " AND verdict IN ('APPROVED', 'NEEDS_REVISION', 'BLOCKED') LIMIT 1",
                (self._project_id, self._node_task_id, self._node_task_id),
            ).fetchone()
            return row is not None
        row = self._store.db.execute(
            "SELECT 1 FROM reviews WHERE project_id = ? AND reviewer = ?"
            " AND verdict IN ('APPROVED', 'NEEDS_REVISION', 'BLOCKED') LIMIT 1",
            (self._project_id, role),
        ).fetchone()
        return row is not None


class _NoTestRecords:
    """v1 口径：平台没有测试输出记录，tests_passed 一律 None → UNVERIFIED。"""

    def lookup(self, command: str) -> str | None:
        return None


def evaluate_workflow_gate(
    store: Any,
    project_id: Any,
    spec: Any,
    *,
    node_task_id: str | None = None,
) -> acceptance.GateResult:
    """评估一个门禁 spec（顶层列表 = 隐式 all；组合语义交给 acceptance）。"""

    criteria = spec if isinstance(spec, list) else [spec]
    return acceptance.evaluate_gate(
        list(criteria),
        probe=ArtifactFileProbe(store, project_id),
        tests=_NoTestRecords(),
        decisions=ProjectDecisions(store, project_id, node_task_id),
    )
