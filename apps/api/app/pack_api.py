"""竞赛 pack 的 HTTP 服务层。

把 `packages.competition_packs` 的 loader/materializer/validation 暴露为
HTTP 端点所需的纯函数：pack 列表与详情、项目视角的 pack 与物化状态、
模板导出、以及基于项目真实成果物文本的校验。

规则本身只存在于 pack 内，这里不复制任何竞赛特有判断。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID

from packages.competition_packs import (
    CompetitionPack,
    CompetitionPackError,
    PackMaterializer,
    PackValidator,
    list_packs,
    load_pack,
    resolve_pack_id,
)


def pack_summaries() -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for pack in list_packs():
        summaries.append(
            {
                "pack_id": pack.pack_id,
                "display_name": pack.manifest.display_name,
                "version": pack.version,
                "description": pack.manifest.description,
                "competition_aliases": list(pack.manifest.competition_aliases),
                "problem_codes": list(pack.manifest.problem_codes),
                "default_problem_code": pack.manifest.default_problem_code,
                "questions": list(pack.questions()),
                "template_count": len(pack.manifest.templates),
                "dag_task_count": len(pack.dag()),
                "required_artifacts": list(pack.manifest.required_artifacts),
            }
        )
    return summaries


def pack_detail(pack_id: str) -> dict[str, Any]:
    return load_pack(pack_id).as_dict()


def pack_for_project(project: Any) -> CompetitionPack:
    return load_pack(resolve_pack_id(getattr(project, "competition_pack", None)))


def materialization_status(store: Any, project_id: UUID, pack: CompetitionPack) -> dict[str, Any]:
    """对比 pack 的 DAG/模板与项目现有任务/成果物，报告缺失项。"""

    tasks = {task.title for task in store.list_tasks(project_id)}
    artifacts = {artifact.name for artifact in store.list_artifacts(project_id)}
    missing_tasks = [task.title for task in pack.dag() if task.title not in tasks]
    missing_artifacts = [spec.filename for spec in pack.manifest.templates if spec.filename not in artifacts]
    total = len(pack.dag()) + len(pack.manifest.templates)
    present = total - len(missing_tasks) - len(missing_artifacts)
    return {
        "pack_id": pack.pack_id,
        "pack_version": pack.version,
        "materialized": not missing_tasks and not missing_artifacts,
        "task_total": len(pack.dag()),
        "task_present": len(pack.dag()) - len(missing_tasks),
        "artifact_total": len(pack.manifest.templates),
        "artifact_present": len(pack.manifest.templates) - len(missing_artifacts),
        "planned_total": total,
        "planned_present": present,
        "progress": round(present / total, 4) if total else 0.0,
        "missing_tasks": missing_tasks,
        "missing_artifacts": missing_artifacts,
    }


def project_documents(store: Any, project_id: UUID) -> dict[str, str]:
    """读取已存内容的成果物文本，供校验器判断章节与四问覆盖。"""

    documents: dict[str, str] = {}
    for artifact in store.list_artifacts(project_id):
        try:
            content = store.get_artifact_content(artifact.id)
        except (KeyError, ValueError):
            # 内容未落库或哈希不符：交给校验器按"文本不可读"处理。
            continue
        try:
            documents[artifact.name] = content.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return documents


def validate_project(store: Any, project_id: UUID, pack: CompetitionPack) -> dict[str, Any]:
    artifacts = [artifact.__dict__ for artifact in store.list_artifacts(project_id)]
    report = PackValidator(pack).validate(
        artifacts=artifacts,
        documents=project_documents(store, project_id),
        project_id=str(project_id),
    )
    return report.as_dict()


def render_template(
    store: Any,
    project: Any,
    template_id: str,
    *,
    problem_code: str | None = None,
    questions: list[int] | None = None,
    variables: Mapping[str, Any] | None = None,
) -> tuple[str, CompetitionPack, str]:
    """渲染模板用于导出；返回 (文本, pack, 文件名)。"""

    materializer = PackMaterializer(store)
    pack = materializer.resolve_pack(project)
    text = materializer.render_for_project(
        project.id,
        template_id,
        problem_code=problem_code,
        questions=questions,
        variables=variables,
    )
    return text, pack, pack.manifest.template(template_id).filename


def apply_pack(
    store: Any,
    project: Any,
    *,
    problem_code: str | None = None,
    questions: list[int] | None = None,
    created_by: str = "pack-materializer",
) -> dict[str, Any]:
    materializer = PackMaterializer(store)
    result = materializer.apply(
        project.id,
        problem_code=problem_code,
        questions=questions,
        created_by=created_by,
    )
    return result.as_dict()


def pack_error_status(error: CompetitionPackError) -> int:
    """pack 错误映射为 HTTP 状态码。"""

    not_found = {
        "pack_not_found",
        "pack_template_not_found",
        "pack_template_file_missing",
        "pack_dag_task_not_found",
    }
    return 404 if error.code in not_found else 400


# ---- 机器 Review 与信息边界 Gate ----------------------------------------

REVIEW_TARGET_ORDER = ("result_table", "audit_report")
REVIEW_TASK_TITLE = "独立复核与一致性检查"
# 平台合规严重度只有 fatal/major/minor，pack 的 info 需降级为 minor。
PLATFORM_SEVERITY = {"fatal": "fatal", "major": "major", "minor": "minor", "info": "minor"}
BOUNDARY_CODES = {"future_data_allowed", "information_boundary_missing"}


class MachineReviewError(RuntimeError):
    """稳定错误族：缺少可挂载的 Review 目标或非法审计范围。"""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code if not detail else f"{code}:{detail}")
        self.code = code
        self.code = code


@dataclass(frozen=True)
class ReviewTarget:
    """Review 挂载目标：``kind`` 是平台对象种类（artifact/task），``artifact_type`` 仅作说明。"""

    kind: str
    target_id: UUID
    artifact_type: str | None = None


def _review_target(store: Any, project_id: UUID, pack: CompetitionPack) -> ReviewTarget:
    """选择 Review 挂载目标：优先已填写的交付物，其次骨架槽位，最后复核任务。"""

    artifacts = store.list_artifacts(project_id)
    by_type: dict[str, list[Any]] = {}
    for artifact in artifacts:
        by_type.setdefault(str(artifact.artifact_type), []).append(artifact)

    def is_unmodified_scaffold(artifact: Any) -> bool:
        policy = artifact.data_policy if isinstance(artifact.data_policy, dict) else {}
        template_hash = policy.get("template_hash")
        return bool(policy.get("template_id")) and (not template_hash or template_hash == artifact.content_hash)

    for artifact_type in REVIEW_TARGET_ORDER:
        deliverables = [item for item in by_type.get(artifact_type, []) if not is_unmodified_scaffold(item)]
        if deliverables:
            return ReviewTarget("artifact", deliverables[0].id, artifact_type)
    for artifact_type in REVIEW_TARGET_ORDER:
        candidates = by_type.get(artifact_type) or []
        if candidates:
            return ReviewTarget("artifact", candidates[0].id, artifact_type)
    for task in store.list_tasks(project_id):
        if task.title == REVIEW_TASK_TITLE:
            return ReviewTarget("task", task.id)
    raise MachineReviewError("machine_review_target_missing")


def create_machine_review(
    store: Any,
    project: Any,
    pack: CompetitionPack,
    *,
    scope: str = "full",
    reviewer: str = "modular-audit",
    target_type: str | None = None,
    target_id: UUID | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """把 pack 校验结果落成平台 Review + Gate。

    - fatal → BLOCKED；major → NEEDS_REVISION；
    - 干净（PASS / PASS_WITH_ASSUMPTIONS）时不自动创建 Review：平台规则要求
      正式批准必须由人工提交，机器审计只能提出复核意见。
    - ``scope="information_boundary"`` 只把信息边界类发现写入 Review/Gate。
    """

    from app.contracts import ReviewCreate

    report = PackValidator(pack).validate(
        artifacts=[artifact.__dict__ for artifact in store.list_artifacts(project.id)],
        documents=project_documents(store, project.id),
        project_id=str(project.id),
    )
    if scope not in {"full", "information_boundary"}:
        raise MachineReviewError("machine_review_scope_invalid", scope)
    findings = list(report.findings)
    if scope == "information_boundary":
        findings = [item for item in findings if item.code in BOUNDARY_CODES]

    if any(item.severity == "fatal" for item in findings):
        verdict = "BLOCKED"
    elif any(item.severity == "major" for item in findings):
        verdict = "NEEDS_REVISION"
    else:
        return {
            "created": False,
            "reason": "machine_review_clean_requires_human_approval",
            "scope": scope,
            "status": report.status,
            "verdict": None,
            "finding_count": len(findings),
            "findings": [item.as_dict() for item in findings],
        }

    if target_type is not None and target_id is not None:
        resolved = ReviewTarget(target_type, target_id)
    else:
        resolved = _review_target(store, project.id, pack)

    payload = ReviewCreate(
        target_type=resolved.kind,
        target_id=resolved.target_id,
        verdict=verdict,
        summary=(
            f"pack 机器审计（{pack.pack_id} v{pack.version}，范围 {scope}）："
            f"{len(findings)} 项发现，{report.status}"
        ),
        findings=[
            {
                "severity": PLATFORM_SEVERITY.get(item.severity, "minor"),
                "code": item.code,
                "message": f"{item.subject}: {item.message}",
            }
            for item in findings
        ],
        reviewer=reviewer,
        idempotency_key=idempotency_key,
    )
    review = store.create_review(project.id, payload)
    return {
        "created": True,
        "scope": scope,
        "status": report.status,
        "verdict": verdict,
        "review_id": str(review.id),
        "target_type": resolved.kind,
        "target_id": str(resolved.target_id),
        "artifact_type": resolved.artifact_type,
        "finding_count": len(findings),
        "findings": [item.as_dict() for item in findings],
    }


__all__ = [
    "BOUNDARY_CODES",
    "MachineReviewError",
    "PLATFORM_SEVERITY",
    "REVIEW_TARGET_ORDER",
    "ReviewTarget",
    "apply_pack",
    "create_machine_review",
    "materialization_status",
    "pack_detail",
    "pack_error_status",
    "pack_for_project",
    "pack_summaries",
    "project_documents",
    "render_template",
    "validate_project",
]