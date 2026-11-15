"""集群任务视角：跨集群的任务看板、任务细节编辑、成果本地路径。

和 `commands.cluster_detail` 的区别：
- `cluster_detail` 是**单个集群**的控制台（角色 / 进程 / 预算 / 事件）。
- 这里是**所有集群**的任务汇总，给"集群任务"页用：一眼看到哪些任务在跑、
  谁在做、出了什么成果、成果在磁盘上的哪个路径。

任务正文（标题/描述）在平台内核的 `tasks` 表里，内核没有"改标题"的 API
（`Store.update_task` 只改状态/指派人/预算这些协议字段）。这里只做**文字层**
的编辑，并且刻意不碰状态机——改状态仍然必须走内核的状态迁移，否则会绕过
审批门禁。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import commands as base
from . import agents as studio_agents
from . import store as studio_store
from .commands import StudioError

EDITABLE = ("title", "description")


def _task_view(task: dict[str, Any], *, cluster: dict[str, Any],
               role_by_agent: dict[str, dict[str, Any]],
               artifacts_by_task: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    assignee = str(task.get("assignee") or "")
    role = role_by_agent.get(assignee) or {}
    task_id = str(task.get("id") or "")
    return {
        "id": task_id,
        "cluster_id": str(cluster.get("id") or ""),
        "cluster_name": str(cluster.get("name") or ""),
        "title": str(task.get("title") or ""),
        "description": str(task.get("description") or ""),
        "status": str(task.get("status") or ""),
        "stage": str(task.get("stage") or ""),
        "assignee": assignee,
        "role_name": str(role.get("name") or ""),
        "role_id": str(role.get("id") or ""),
        "agent_id": str(role.get("local_agent_id") or ""),
        "workspace": str(role.get("workspace") or cluster.get("workspace_root") or ""),
        "requires_review": bool(task.get("requires_review")),
        "updated_at": str(task.get("updated_at") or task.get("created_at") or ""),
        "artifacts": artifacts_by_task.get(task_id, []),
    }


def list_cluster_tasks(store: Any, *, cluster_id: str = "",
                       status: str = "", query: str = "") -> dict[str, Any]:
    """所有集群的任务平铺。带成果物 + 成果在磁盘上的路径。"""

    clusters = studio_store.list_clusters(store)
    needle = str(query or "").strip().lower()
    wanted = str(status or "").strip().upper()
    rows: list[dict[str, Any]] = []
    summary = {"clusters": len(clusters), "tasks": 0, "running": 0,
               "waiting_review": 0, "artifacts": 0}
    # 老数据没有 source_path，靠扫工作区找同名文件；同一根目录只扫一次
    lookup_cache: dict[tuple[str, str], str] = {}

    for cluster in clusters:
        cid = str(cluster.get("id") or "")
        if cluster_id and cid != str(cluster_id):
            continue
        project_id = str(cluster.get("project_id") or "")
        if not project_id:
            continue
        roles = studio_store.list_roles(store, cid)
        role_by_agent = {str(r.get("agent_id")): r for r in roles}
        artifacts = base.list_artifacts(store, project_id=project_id) or []
        by_task: dict[str, list[dict[str, Any]]] = {}
        for artifact in artifacts:
            task_id = str(artifact.get("task_id") or "")
            if not task_id:
                continue
            local_path = str(artifact.get("source_path") or "")
            if not local_path:
                name = str(artifact.get("name") or "")
                key = (str(cluster.get("workspace_root") or ""), name)
                if key in lookup_cache:
                    local_path = lookup_cache[key]
                else:
                    local_path = _find_by_name(Path(key[0]), name) if key[0] and name else ""
                    lookup_cache[key] = local_path
            by_task.setdefault(task_id, []).append({
                "id": str(artifact.get("id") or ""),
                "name": str(artifact.get("name") or ""),
                "artifact_type": str(artifact.get("artifact_type") or ""),
                "status": str(artifact.get("status") or ""),
                "size_bytes": int(artifact.get("size_bytes") or 0),
                "output_hash": str(artifact.get("output_hash") or ""),
                "local_path": local_path,
            })
        for task in store.list_tasks(project_id) or []:
            data = task.model_dump(mode="json") if hasattr(task, "model_dump") else dict(task)
            view = _task_view(data, cluster=cluster, role_by_agent=role_by_agent,
                              artifacts_by_task=by_task)
            if wanted and view["status"].upper() != wanted:
                continue
            if needle:
                haystack = " ".join([view["title"], view["description"],
                                     view["role_name"], view["cluster_name"]]).lower()
                if needle not in haystack:
                    continue
            rows.append(view)

    rows.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    summary["tasks"] = len(rows)
    summary["running"] = sum(1 for r in rows if r["status"].upper() in ("RUNNING", "CLAIMED"))
    summary["waiting_review"] = sum(1 for r in rows if r["status"].upper() == "WAITING_REVIEW")
    summary["artifacts"] = sum(len(r["artifacts"]) for r in rows)
    return {
        "tasks": rows,
        "summary": summary,
        "clusters": [{"id": str(c.get("id") or ""), "name": str(c.get("name") or ""),
                      "status": str(c.get("status") or "")} for c in clusters],
    }


def _workspace_roots(store: Any) -> list[Path]:
    roots: list[Path] = []
    for agent in studio_store.list_agents(store):
        raw = str(agent.get("workspace") or "").strip()
        if raw:
            roots.append(Path(raw))
    for cluster in studio_store.list_clusters(store):
        raw = str(cluster.get("workspace_root") or "").strip()
        if raw:
            roots.append(Path(raw))
    return roots


def _find_by_name(root: Path, name: str, *, limit: int = 4000) -> str:
    """在 root 下面找同名文件（老数据没有 source_path 时的兜底）。

    不做无界递归：`rglob` 撞上 node_modules 这种东西会卡很久，所以数到
    `limit` 个候选就放弃，宁可返回空（界面显示"未记录"）也不拖垮接口。
    """

    if not name or not root.exists():
        return ""
    seen = 0
    try:
        for candidate in root.rglob(name):
            seen += 1
            if seen > limit:
                return ""
            if candidate.is_file():
                return str(candidate)
    except OSError:
        return ""
    return ""


def _resolve_artifact_path(store: Any, artifact_id: str) -> Path:
    """定位成果的本地文件。

    优先用登记时记下的 `source_path`（新数据都有）；老数据没有这一列，就按
    「工作区根 + 文件名」在已知工作区里找同名文件——找不到就如实报错，
    不猜一个看起来像的路径。
    """

    import uuid as _uuid

    try:
        artifact = store.get_artifact(_uuid.UUID(str(artifact_id)))
    except (KeyError, ValueError) as error:
        raise StudioError("artifact_not_found", [str(artifact_id)]) from error
    data = artifact.model_dump(mode="json") if hasattr(artifact, "model_dump") else dict(artifact)
    raw = str(data.get("source_path") or "").strip()
    if raw:
        path = Path(raw)
        if path.exists():
            return path
    name = str(data.get("name") or "").strip()
    if name:
        for root in _workspace_roots(store):
            found = _find_by_name(root, name)
            if found:
                return Path(found)
    raise StudioError("artifact_path_unknown",
                      ["这个成果没有记录本地路径（老数据），也没在工作区里找到同名文件"])


def artifact_local_path(store: Any, artifact_id: str) -> dict[str, Any]:
    path = _resolve_artifact_path(store, artifact_id)
    return {"artifact_id": str(artifact_id), "local_path": str(path),
            "directory": str(path.parent), "exists": True,
            "size_bytes": path.stat().st_size}


def update_task_text(store: Any, task_id: str, **fields: Any) -> dict[str, Any]:
    """只改任务的文字层（标题 / 描述）。

    状态、指派人、预算这些**故意不在这里**：它们受内核状态机与派单策略约束，
    绕过去等于关掉审批门禁。要改状态请用 `POST /tasks/{id}/review`。
    """

    unknown = [key for key in fields if key not in EDITABLE]
    if unknown:
        raise StudioError("task_field_not_editable",
                          [f"{key}: 只支持改 {'/'.join(EDITABLE)}" for key in unknown])
    updates: dict[str, Any] = {}
    if "title" in fields:
        title = str(fields["title"] or "").strip()
        if not title:
            raise StudioError("task_title_required", ["标题不能为空"])
        if len(title) > 200:
            raise StudioError("task_title_too_long", ["标题不能超过 200 字符"])
        updates["title"] = title
    if "description" in fields:
        updates["description"] = str(fields["description"] or "").strip()[:4000]
    if not updates:
        raise StudioError("task_nothing_to_update", ["没有要改的字段"])

    # 内核的 update_task_text 只动文字层，并自己写 task.text_edited 事件
    updated = base.update_task_text(store, task_id=task_id, **updates)
    data = updated.model_dump(mode="json") if hasattr(updated, "model_dump") else dict(updated)
    return {"task": data, "fields": sorted(updates)}


def cluster_task_agents(store: Any, cluster_id: str) -> dict[str, Any]:
    """某个集群里参与的 Agent（含状态、模型、路由），给「集群 Agent」页用。"""

    cluster = studio_store.get_cluster(store, cluster_id)
    if cluster is None:
        raise StudioError("cluster_not_found", [str(cluster_id)])
    roles = studio_store.list_roles(store, cluster_id)
    providers = {item["id"]: item for item in studio_store.list_providers(store)}
    out: list[dict[str, Any]] = []
    for role in roles:
        local = (studio_store.get_agent(store, role["local_agent_id"])
                 if role.get("local_agent_id") else None)
        run = studio_store.latest_run(store, cluster_id) or {}
        out.append({
            "role_id": str(role.get("id") or ""),
            "role_name": str(role.get("name") or ""),
            "is_lead": str(role.get("id")) == str(cluster.get("lead_role_id")),
            "agent_id": str(role.get("local_agent_id") or ""),
            "agent_name": str((local or {}).get("name") or ""),
            "agent_missing": bool(role.get("local_agent_id") and local is None),
            "backend": str(role.get("backend") or ""),
            "model": str(role.get("model") or ""),
            "provider_id": str(role.get("provider_id")
                               or (local or {}).get("provider_id") or ""),
            "provider_name": str((providers.get(
                str(role.get("provider_id") or (local or {}).get("provider_id") or "")) or {}
            ).get("name") or "本机默认"),
            "workspace": str(role.get("workspace") or ""),
            "status": str(role.get("role_status") or "IDLE"),
            "run_status": str(run.get("status") or ""),
            "role_prompt": str(role.get("role_prompt") or ""),
            "system_prompt": str(role.get("system_prompt") or ""),
        })
    return {"cluster": {"id": cluster["id"], "name": cluster.get("name"),
                        "status": cluster.get("status")},
            "agents": out}


def list_agents_with_runs(store: Any) -> list[dict[str, Any]]:
    """平台上的所有 Agent + 正在跑什么（「集群 Agent」页的列表用）。"""

    agents = studio_agents.list_agents(store)
    out: list[dict[str, Any]] = []
    for agent in agents:
        run = studio_store.latest_agent_run(store, agent["id"]) or {}
        cluster_roles: list[dict[str, Any]] = []
        for cluster in studio_store.list_clusters(store):
            for role in studio_store.list_roles(store, cluster["id"]):
                if str(role.get("local_agent_id") or "") == str(agent["id"]):
                    cluster_roles.append({
                        "cluster_id": cluster["id"], "cluster_name": cluster.get("name"),
                        "role_id": role["id"], "role_name": role.get("name"),
                        "is_lead": str(role["id"]) == str(cluster.get("lead_role_id")),
                    })
        # 导入的 Agent 用工作区里的上下文文件做标记（不额外加数据库列：
        # 这个信息本来就是"文件在不在"，多存一份就会不同步）
        workspace = str(agent.get("workspace") or "")
        imported = False
        if workspace:
            try:
                imported = (Path(workspace) / "IMPORTED-SESSION.md").exists()
            except OSError:
                imported = False
        out.append({
            **agent,
            "imported": imported,
            "running": str(run.get("status") or "") in ("RUNNING", "QUEUED"),
            "current_task": str(run.get("task_text") or "")[:160],
            "current_run_status": str(run.get("status") or ""),
            "clusters": cluster_roles,
        })
    return out
