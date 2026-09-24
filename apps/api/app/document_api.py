"""文档模板的三层版本（草稿 / 提交 / 批准）与证据链。

阶段 7 的最小切片：把 pack 文档模板（论文、建模报告、复核报告…）纳入
平台既有的成果物版本链、Review/Gate 与 Evidence 原语，不另起一套文档模型。

三层语义（映射到平台成果物状态）：

- **草稿 draft**：`DRAFT`，可编辑、不进下游；
- **提交 submitted**：`PENDING_REVIEW`，冻结待审，提交必须带证据；
- **批准 approved**：`APPROVED`，不可变、允许下游引用，只能由人工 Review 产生；
- **退回/再起草**：从 `REJECTED` 或已批准版本**派生新 DRAFT 版本**，历史不丢。

验收对应：正式版本可追溯到成员/Agent/任务/Git commit；草稿无法绕过门禁。
"""

from __future__ import annotations

import difflib
from typing import Any, Mapping
from uuid import UUID

try:  # 独立加载路径兼容
    from packages.competition_packs import CompetitionPack, load_pack, resolve_pack_id
except ImportError:  # pragma: no cover
    CompetitionPack = Any  # type: ignore[assignment]

    def load_pack(pack_id: str):  # type: ignore[misc]
        raise RuntimeError("competition_packs_unavailable")

    def resolve_pack_id(value: str | None) -> str:  # type: ignore[misc]
        return "cumcm"


DRAFT = "draft"
SUBMITTED = "submitted"
APPROVED = "approved"

LAYER_BY_STATUS: dict[str, str] = {
    "DRAFT": DRAFT,
    "PENDING_REVIEW": SUBMITTED,
    "APPROVED": APPROVED,
    "REJECTED": DRAFT,
    "ARCHIVED": APPROVED,
}

EDITABLE_LAYERS = {DRAFT}
DOWNSTREAM_LAYERS = {APPROVED}


class DocumentLayerError(RuntimeError):
    """稳定错误族：文档层级不满足操作前提。"""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code if not detail else f"{code}:{detail}")
        self.code = code


def document_layer(artifact: Any) -> str:
    status = str(getattr(artifact, "status", "") or "")
    return LAYER_BY_STATUS.get(status, DRAFT)


def document_layers() -> dict[str, Any]:
    """对外暴露三层定义，便于前端与审计对齐口径。"""

    return {
        "layers": [
            {"layer": DRAFT, "artifact_status": "DRAFT", "editable": True, "downstream_allowed": False},
            {"layer": SUBMITTED, "artifact_status": "PENDING_REVIEW", "editable": False, "downstream_allowed": False},
            {"layer": APPROVED, "artifact_status": "APPROVED", "editable": False, "downstream_allowed": True},
        ],
        "rules": {
            "submit_requires_evidence": True,
            "approval_requires_human_review": True,
            "draft_cannot_be_downstream_input": True,
            "revision_creates_new_draft_version": True,
        },
        "status_to_layer": dict(LAYER_BY_STATUS),
    }


def _version_chain(store: Any, artifact_id: UUID) -> list[Any]:
    """沿 parent_artifact_id 回溯并返回从旧到新的版本链。"""

    chain: list[Any] = []
    seen: set[str] = set()
    current = artifact_id
    while current is not None:
        key = str(current)
        if key in seen:
            break
        seen.add(key)
        artifact = store.get_artifact(current)
        chain.append(artifact)
        current = getattr(artifact, "parent_artifact_id", None)
    chain.reverse()
    return chain


def document_timeline(store: Any, project_id: UUID, artifact_id: UUID) -> dict[str, Any]:
    """文档三层版本时间线 + 追溯信息（成员/Agent/任务/Git/审批/证据）。"""

    chain = _version_chain(store, artifact_id)
    if not chain:
        raise DocumentLayerError("document_not_found")
    if any(str(artifact.project_id) != str(project_id) for artifact in chain):
        raise DocumentLayerError("document_not_in_project")

    evidence_by_artifact: dict[str, list[dict[str, Any]]] = {}
    for evidence in store.list_evidence(project_id):
        if evidence.artifact_id is not None:
            evidence_by_artifact.setdefault(str(evidence.artifact_id), []).append(
                {
                    "id": str(evidence.id),
                    "claim": evidence.claim,
                    "evidence_type": str(evidence.evidence_type),
                    "run_id": str(evidence.run_id) if evidence.run_id else None,
                    "source_ref": evidence.source_ref,
                    "created_by": evidence.created_by,
                }
            )

    revisions: list[dict[str, Any]] = []
    for index, artifact in enumerate(chain, start=1):
        revisions.append(
            {
                "revision": index,
                "artifact_id": str(artifact.id),
                "layer": document_layer(artifact),
                "status": str(artifact.status),
                "version": artifact.version,
                "content_hash": artifact.content_hash,
                "editable": document_layer(artifact) in EDITABLE_LAYERS,
                "downstream_allowed": bool(artifact.downstream_allowed),
                "immutable": bool(artifact.immutable),
                "parent_artifact_id": str(artifact.parent_artifact_id) if artifact.parent_artifact_id else None,
                "traceability": {
                    "created_by": artifact.created_by,
                    "created_by_kind": str(artifact.created_by_kind),
                    "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
                    "task_id": str(artifact.task_id) if artifact.task_id else None,
                    "run_id": str(artifact.run_id) if artifact.run_id else None,
                    "git_commit": artifact.git_commit,
                    "snapshot_ref": artifact.snapshot_ref,
                    "approved_by": artifact.approved_by,
                    "approved_at": artifact.approved_at.isoformat() if artifact.approved_at else None,
                },
                "evidence": evidence_by_artifact.get(str(artifact.id), []),
            }
        )

    latest = revisions[-1]
    return {
        "project_id": str(project_id),
        "artifact_id": str(artifact_id),
        "root_artifact_id": revisions[0]["artifact_id"],
        "current_layer": latest["layer"],
        "current_revision": latest["revision"],
        "revision_count": len(revisions),
        "revisions": revisions,
        "layers": document_layers(),
    }


def document_evidence_chain(store: Any, project_id: UUID, artifact_id: UUID) -> dict[str, Any]:
    """证据链查询：文档及其历史版本的证据、来源运行与提交追溯。"""

    timeline = document_timeline(store, project_id, artifact_id)
    entries: list[dict[str, Any]] = []
    for revision in timeline["revisions"]:
        for evidence in revision["evidence"]:
            entries.append({**evidence, "artifact_id": revision["artifact_id"], "layer": revision["layer"]})
    return {
        "project_id": str(project_id),
        "artifact_id": str(artifact_id),
        "evidence": entries,
        "evidence_count": len(entries),
        "runs": sorted({entry["run_id"] for entry in entries if entry.get("run_id")}),
        "traceable_to": [revision["traceability"] for revision in timeline["revisions"]],
    }


def submit_document(
    store: Any,
    project_id: UUID,
    artifact_id: UUID,
    *,
    actor: str = "member-001",
    actor_kind: str = "member",
    evidence_ids: list[UUID] | None = None,
) -> dict[str, Any]:
    """草稿 → 提交（冻结待审）。提交必须带证据，草稿不能直接进下游。"""

    artifact = store.get_artifact(artifact_id)
    if str(artifact.project_id) != str(project_id):
        raise DocumentLayerError("document_not_in_project")
    layer = document_layer(artifact)
    if layer == SUBMITTED:
        return {"artifact_id": str(artifact_id), "layer": SUBMITTED, "changed": False}
    if layer != DRAFT:
        raise DocumentLayerError("document_not_editable", layer)
    try:
        submitted = store.submit_artifact_for_review(
            artifact_id, actor=actor, actor_kind=actor_kind, evidence_ids=evidence_ids
        )
    except ValueError as error:
        raise DocumentLayerError(str(error)) from error
    return {
        "artifact_id": str(artifact_id),
        "layer": document_layer(submitted),
        "status": str(submitted.status),
        "changed": True,
    }


def revise_document(
    store: Any,
    project_id: UUID,
    artifact_id: UUID,
    *,
    actor: str = "member-001",
    actor_kind: str = "member",
    description: str | None = None,
) -> dict[str, Any]:
    """退回/再起草：从被拒或已批准版本派生新的 DRAFT 版本。"""

    artifact = store.get_artifact(artifact_id)
    if str(artifact.project_id) != str(project_id):
        raise DocumentLayerError("document_not_in_project")
    from app.contracts import ArtifactCreate

    draft = ArtifactCreate(
        name=artifact.name,
        artifact_type=artifact.artifact_type,
        description=description if description is not None else artifact.description,
        task_id=artifact.task_id,
    )
    revised = store.revise_artifact(project_id, artifact_id, draft, actor=actor, actor_kind=actor_kind)
    return {
        "artifact_id": str(revised.id),
        "parent_artifact_id": str(artifact_id),
        "layer": document_layer(revised),
        "status": str(revised.status),
        "changed": True,
    }


def document_diff(
    store: Any,
    project_id: UUID,
    artifact_id: UUID,
    *,
    from_revision: int | None = None,
    to_revision: int | None = None,
) -> dict[str, Any]:
    """版本间 Diff：逐版本内容差异 + 追溯信息变化。

    默认比较「上一版 → 当前版」，也支持显式指定两端。两侧内容都必须已落库，
    否则 fail-closed（`document_content_unavailable`），不猜测差异。
    """

    timeline = document_timeline(store, project_id, artifact_id)
    revisions = timeline["revisions"]
    total = len(revisions)
    left_index = (from_revision or max(total - 1, 1)) - 1
    right_index = (to_revision or total) - 1
    if not (0 <= left_index < total) or not (0 <= right_index < total):
        raise DocumentLayerError("document_revision_out_of_range")
    left = revisions[left_index]
    right = revisions[right_index]

    left_content = _read_content(store, left["artifact_id"])
    right_content = _read_content(store, right["artifact_id"])
    left_lines = left_content.decode("utf-8", errors="replace").splitlines()
    right_lines = right_content.decode("utf-8", errors="replace").splitlines()
    unified = list(
        difflib.unified_diff(left_lines, right_lines, fromfile=f"revision-{left['revision']}", tofile=f"revision-{right['revision']}", lineterm="")
    )
    added = sum(1 for line in unified if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in unified if line.startswith("-") and not line.startswith("---"))
    hunks = sum(1 for line in unified if line.startswith("@@"))

    def trace_delta(key: str) -> dict[str, Any]:
        return {
            "from": left["traceability"].get(key),
            "to": right["traceability"].get(key),
            "changed": left["traceability"].get(key) != right["traceability"].get(key),
        }

    return {
        "project_id": str(project_id),
        "artifact_id": str(artifact_id),
        "from_revision": left["revision"],
        "to_revision": right["revision"],
        "from_artifact_id": left["artifact_id"],
        "to_artifact_id": right["artifact_id"],
        "content_changed": left["content_hash"] != right["content_hash"],
        "identical": left["content_hash"] == right["content_hash"],
        "stats": {"added_lines": added, "removed_lines": removed, "hunks": hunks},
        "unified_diff": unified,
        "layer_change": {"from": left["layer"], "to": right["layer"], "changed": left["layer"] != right["layer"]},
        "git_commit": trace_delta("git_commit"),
        "task_id": trace_delta("task_id"),
        "run_id": trace_delta("run_id"),
        "author": trace_delta("created_by"),
        "approver": trace_delta("approved_by"),
    }


def _read_content(store: Any, artifact_id: str) -> bytes:
    try:
        return store.get_artifact_content(UUID(artifact_id))
    except (KeyError, ValueError) as error:
        raise DocumentLayerError("document_content_unavailable", f"{artifact_id}:{error}") from error


def merge_confirm(
    store: Any,
    project_id: UUID,
    artifact_id: UUID,
    *,
    source_revision: int,
    note: str = "",
    git_commit: str | None = None,
    actor: str = "member-001",
    actor_kind: str = "member",
) -> dict[str, Any]:
    """合并确认：选定一方版本内容，派生新的草稿版本并留痕。

    合并只新增版本、不修改两端：已批准版本保持不可变，被合并的旧版本仍在链上。
    如项目登记了本地 Git 仓库且提供了 ``git_commit``，会校验该提交真实存在。
    """

    timeline = document_timeline(store, project_id, artifact_id)
    revisions = timeline["revisions"]
    if not (1 <= source_revision <= len(revisions)):
        raise DocumentLayerError("document_revision_out_of_range")
    target = revisions[-1]
    if target["immutable"]:
        raise DocumentLayerError("document_approved_is_immutable", target["artifact_id"])
    source = revisions[source_revision - 1]

    if git_commit:
        provider = _git_provider(store, project_id)
        if provider is not None and not provider.commit_exists(git_commit):
            raise DocumentLayerError("git_commit_not_found", git_commit)

    document = store.get_artifact(artifact_id)
    from app.contracts import ArtifactCreate

    draft = ArtifactCreate(
        name=document.name,
        artifact_type=document.artifact_type,
        description=document.description,
        git_commit=git_commit or document.git_commit,
    )
    merged = store.revise_artifact(project_id, UUID(target["artifact_id"]), draft, actor=actor, actor_kind=actor_kind)
    content = _read_content(store, source["artifact_id"])
    # 内容入库会回写 content_hash，必须用返回值为准，否则报告的是旧哈希。
    merged = store.store_artifact_content(merged.id, content)
    try:
        store.add_event(
            project_id,
            "document.merged",
            actor,
            {
                "artifact_id": str(merged.id),
                "source_artifact_id": source["artifact_id"],
                "target_artifact_id": target["artifact_id"],
                "source_revision": source_revision,
                "note": note,
                "git_commit": git_commit,
            },
        )
    except AttributeError:  # pragma: no cover - store 未提供 add_event 时降级
        pass
    return {
        "artifact_id": str(merged.id),
        "parent_artifact_id": target["artifact_id"],
        "source_artifact_id": source["artifact_id"],
        "source_revision": source_revision,
        "layer": document_layer(merged),
        "content_hash": merged.content_hash,
        "git_commit": merged.git_commit,
        "note": note,
        "changed": True,
    }


def _git_provider(store: Any, project_id: UUID) -> Any:
    """项目登记了本地 Git 仓库时返回 provider，用于提交校验。"""

    try:
        repository = store.get_git_repository(project_id)
    except (KeyError, ValueError):
        return None
    if str(getattr(repository, "provider", "")) != "local":
        return None
    try:
        from .git_adapter import LocalGitProvider

        return LocalGitProvider(repository.local_path)
    except Exception:  # pragma: no cover - Git 不可用时不做校验
        return None


__all__ = [
    "APPROVED",
    "COMMENT_EVENT",
    "RELATION_EVENT",
    "SNAPSHOT_EVENT",
    "add_comment",
    "create_snapshot",
    "impact_lookup",
    "link_relation",
    "list_comments",
    "list_relations",
    "list_snapshots",
    "DRAFT",
    "DocumentLayerError",
    "LAYER_BY_STATUS",
    "SUBMITTED",
    "document_evidence_chain",
    "document_layer",
    "document_diff",
    "document_layers",
    "document_timeline",
    "merge_confirm",
    "revise_document",
    "submit_document",
]

# ---- 评论 / 快照 / 结论-图表-段落关系 -----------------------------------
#
# 三者都落在平台既有的事件流上（typed events + 查询），不新增表：
# 评论与建议是人对文档的反馈，快照是"某一版此刻的检查点"，
# 关系是"结论/图表/运行 ↔ 文档段落"的可追溯连线。

COMMENT_EVENT = "document.comment"
SNAPSHOT_EVENT = "document.snapshot"
RELATION_EVENT = "document.relation"
RELATION_TARGETS = {"artifact", "run", "figure", "result_table", "task"}


def _document_events(store, project_id, artifact_id, event_type):
    """按文档读取指定类型的事件（事件流即账本，天然可审计）。"""

    entries = []
    for event in store.list_events(project_id, limit=2000):
        if event.event_type != event_type:
            continue
        payload = event.payload or {}
        if str(payload.get("artifact_id")) != str(artifact_id):
            continue
        entries.append(
            {
                "id": str(event.id),
                "sequence": event.sequence,
                "actor": event.actor,
                "actor_kind": str(event.actor_kind),
                "created_at": event.created_at.isoformat() if event.created_at else None,
                **payload,
            }
        )
    return entries


def add_comment(store, project_id, artifact_id, *, body, kind="comment", anchor=None, actor="member-001", actor_kind="member"):
    """评论或建议；可锚定到文档段落（anchor）。"""

    if kind not in {"comment", "suggestion"}:
        raise DocumentLayerError("document_comment_kind_invalid", kind)
    if not isinstance(body, str) or not body.strip():
        raise DocumentLayerError("document_comment_body_required")
    artifact = store.get_artifact(artifact_id)
    if str(artifact.project_id) != str(project_id):
        raise DocumentLayerError("document_not_in_project")
    store.add_event(
        project_id,
        COMMENT_EVENT,
        actor,
        {"artifact_id": str(artifact_id), "body": body.strip(), "kind": kind, "anchor": anchor, "layer": document_layer(artifact)},
        actor_kind=actor_kind,
        object_type="artifact",
        object_id=artifact_id,
    )
    return {"artifact_id": str(artifact_id), "kind": kind, "body": body.strip(), "anchor": anchor, "actor": actor}


def list_comments(store, project_id, artifact_id):
    entries = _document_events(store, project_id, artifact_id, COMMENT_EVENT)
    return {
        "artifact_id": str(artifact_id),
        "comments": entries,
        "comment_count": sum(1 for item in entries if item.get("kind") == "comment"),
        "suggestion_count": sum(1 for item in entries if item.get("kind") == "suggestion"),
    }


def create_snapshot(store, project_id, artifact_id, *, label="", actor="member-001", actor_kind="member"):
    """文档快照：记录某一版此刻的内容哈希作为检查点。"""

    timeline = document_timeline(store, project_id, artifact_id)
    current = timeline["revisions"][-1]
    store.add_event(
        project_id,
        SNAPSHOT_EVENT,
        actor,
        {"artifact_id": str(artifact_id), "revision": current["revision"], "content_hash": current["content_hash"], "layer": current["layer"], "label": label},
        actor_kind=actor_kind,
        object_type="artifact",
        object_id=UUID(current["artifact_id"]),
    )
    return {
        "artifact_id": str(artifact_id),
        "revision": current["revision"],
        "content_hash": current["content_hash"],
        "layer": current["layer"],
        "label": label,
    }


def list_snapshots(store, project_id, artifact_id):
    entries = _document_events(store, project_id, artifact_id, SNAPSHOT_EVENT)
    return {"artifact_id": str(artifact_id), "snapshots": entries, "snapshot_count": len(entries)}


def link_relation(store, project_id, artifact_id, *, target_type, target_id, paragraph, note="", actor="member-001", actor_kind="member"):
    """建立「结论/图表/运行 ↔ 文档段落」关系，用于影响面定位。"""

    if target_type not in RELATION_TARGETS:
        raise DocumentLayerError("document_relation_target_type_invalid", target_type)
    if not isinstance(paragraph, str) or not paragraph.strip():
        raise DocumentLayerError("document_relation_paragraph_required")
    artifact = store.get_artifact(artifact_id)
    if str(artifact.project_id) != str(project_id):
        raise DocumentLayerError("document_not_in_project")
    target_uuid = UUID(str(target_id))
    if target_type in {"artifact", "figure", "result_table"}:
        target = store.get_artifact(target_uuid)
    elif target_type == "run":
        target = store.get_run(target_uuid)
    else:
        target = store.get_task(target_uuid)
    if str(target.project_id) != str(project_id):
        raise DocumentLayerError("document_relation_target_not_in_project")
    store.add_event(
        project_id,
        RELATION_EVENT,
        actor,
        {"artifact_id": str(artifact_id), "target_type": target_type, "target_id": str(target_id), "paragraph": paragraph.strip(), "note": note},
        actor_kind=actor_kind,
        object_type="artifact",
        object_id=artifact_id,
    )
    return {"artifact_id": str(artifact_id), "target_type": target_type, "target_id": str(target_id), "paragraph": paragraph.strip(), "note": note}


def list_relations(store, project_id, artifact_id):
    entries = _document_events(store, project_id, artifact_id, RELATION_EVENT)
    return {"artifact_id": str(artifact_id), "relations": entries, "relation_count": len(entries)}


def impact_lookup(store, project_id, *, target_type, target_id):
    """影响面定位：某个结果/图表/运行变化后，受影响的是哪些文档与段落。"""

    if target_type not in RELATION_TARGETS:
        raise DocumentLayerError("document_relation_target_type_invalid", target_type)
    affected = []
    for event in store.list_events(project_id, limit=2000):
        if event.event_type != RELATION_EVENT:
            continue
        payload = event.payload or {}
        if str(payload.get("target_type")) != target_type or str(payload.get("target_id")) != str(target_id):
            continue
        affected.append(
            {
                "artifact_id": payload.get("artifact_id"),
                "paragraph": payload.get("paragraph"),
                "note": payload.get("note"),
                "linked_by": event.actor,
                "created_at": event.created_at.isoformat() if event.created_at else None,
            }
        )
    return {
        "project_id": str(project_id),
        "target_type": target_type,
        "target_id": str(target_id),
        "affected": affected,
        "affected_count": len(affected),
        "affected_artifacts": sorted({item["artifact_id"] for item in affected if item.get("artifact_id")}),
        "paragraphs": sorted({item["paragraph"] for item in affected if item.get("paragraph")}),
    }

