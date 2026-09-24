"""阶段 8 交付：大纲装配、交付检查、提交包生成与冻结、跨部署恢复验证。

核心约束（对应计划 §15.3 验收）：

- **只装配已批准成果物**：`assemble_paper` 只读取 `APPROVED` 状态成果物，
  并把因未批准而被排除的成果物显式列出——未批准结果无法进入正式论文/PPT；
- **可追溯**：每个章节记录来源成果物、内容哈希、批准人，关键结论可回溯到
  数据/代码/运行/审核；
- **可重新生成**：装配是纯函数式读取（无缓存），素材更新后重跑即得新稿；
- **可跨部署恢复**：提交包附带校验清单，并可用 `verify_submission_bundle`
  在另一套 Store 上比对哈希。
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import UUID

from .pack_api import pack_for_project

APPROVED = "APPROVED"

# 章节 → 允许的来源成果物类型（按 pack 的论文结构）。
PAPER_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("摘要", ("paper_source", "problem_analysis")),
    ("问题重述与分析", ("problem_analysis",)),
    ("模型建立与求解", ("model_spec",)),
    ("实验与检验", ("audit_report", "experiment_plan")),
    ("独立复核", ("review_report",)),
    ("结果与结论", ("result_table",)),
)

REQUIRED_DELIVERABLES = ("paper_source", "result_table", "compiled_pdf")

ANONYMITY_PATTERNS = (
    r"学校[:：]",
    r"学院[:：]",
    r"姓名[:：]",
    r"队号[:：]",
    r"参赛队号",
    r"指导教师",
)

CITE_PATTERN = re.compile(r"\\cite\{([^}]*)\}")
BIBITEM_PATTERN = re.compile(r"\\bibitem\{([^}]*)\}")
LABEL_PATTERN = re.compile(r"\\label\{([^}]*)\}")
REF_PATTERN = re.compile(r"\\(?:eqref|ref)\{([^}]*)\}")


class DeliveryError(RuntimeError):
    """稳定错误族：交付装配前提不满足。"""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code if not detail else f"{code}:{detail}")
        self.code = code


def _artifact_text(store: Any, artifact: Any, limit: int = 4000) -> str:
    try:
        content = store.get_artifact_content(artifact.id)
    except (KeyError, ValueError):
        return ""
    text = content.decode("utf-8", errors="replace")
    return text[:limit]


def _manifest_entry(artifact: Any) -> dict[str, Any]:
    return {
        "artifact_id": str(artifact.id),
        "name": artifact.name,
        "artifact_type": str(artifact.artifact_type),
        "content_hash": artifact.content_hash,
        "status": str(artifact.status),
        "approved_by": artifact.approved_by,
        "git_commit": artifact.git_commit,
        "task_id": str(artifact.task_id) if artifact.task_id else None,
    }


def assemble_paper(store: Any, project_id: UUID, *, excerpt_limit: int = 400) -> dict[str, Any]:
    """按批准素材装配论文大纲与内容片段（未批准素材一律排除）。"""

    project = store.get_project(project_id)
    pack = pack_for_project(project)
    artifacts = list(store.list_artifacts(project_id))
    approved = [item for item in artifacts if str(item.status) == APPROVED]
    approved_by_type: dict[str, list[Any]] = {}
    for item in approved:
        approved_by_type.setdefault(str(item.artifact_type), []).append(item)

    sections: list[dict[str, Any]] = []
    blocked_reasons: list[str] = []
    sources: list[dict[str, Any]] = []
    for title, source_types in PAPER_SECTIONS:
        used: list[Any] = []
        for artifact_type in source_types:
            candidates = approved_by_type.get(artifact_type) or []
            if candidates:
                used.append(sorted(candidates, key=lambda item: item.created_at)[-1])
                break
        if used:
            artifact = used[0]
            section = {
                "title": title,
                "source_artifact_id": str(artifact.id),
                "source_name": artifact.name,
                "artifact_type": str(artifact.artifact_type),
                "content_hash": artifact.content_hash,
                "approved_by": artifact.approved_by,
                "traceability": {
                    "task_id": str(artifact.task_id) if artifact.task_id else None,
                    "git_commit": artifact.git_commit,
                },
                "content": _artifact_text(store, artifact),
            }
            sections.append(section)
            sources.append(_manifest_entry(artifact))
        else:
            blocked_reasons.append(f"missing_approved_source:{title}:{'/'.join(source_types)}")

    excluded = [_manifest_entry(item) for item in artifacts if str(item.status) != APPROVED]
    return {
        "project_id": str(project_id),
        "pack_id": pack.pack_id,
        "pack_version": pack.version,
        "sections": sections,
        "section_count": len(sections),
        "sources": sources,
        "excluded_unapproved": excluded,
        "excluded_count": len(excluded),
        "blocked": bool(blocked_reasons),
        "blocked_reasons": blocked_reasons,
        "generatable": bool(sections),
    }


def assemble_slides(store: Any, project_id: UUID) -> str:
    """从批准素材生成 Marp 兼容的演示大纲（每个来源一页）。"""

    assembly = assemble_paper(store, project_id)
    lines = ["---", "marp: true", "paginate: true", "---", ""]
    lines.append("# 数模竞赛答辩提纲")
    lines.append("")
    for section in assembly["sections"]:
        lines.append("---")
        lines.append("")
        lines.append(f"## {section['title']}")
        lines.append("")
        excerpt = (section["content"] or "").strip().splitlines()
        for line in excerpt[:6]:
            if line.strip():
                lines.append(f"- {line.strip()[:120]}")
        lines.append("")
        lines.append(f"> 来源：{section['source_name']} · {section['content_hash'][:12]} · 批准人 {section['approved_by'] or '—'}")
        lines.append("")
    return "\n".join(lines)


def build_delivery_checklist(
    store: Any,
    project_id: UUID,
    *,
    paper_text: str | None = None,
) -> dict[str, Any]:
    """交付检查清单：图表引用、引用与公式、匿名、附件、页数。"""

    artifacts = list(store.list_artifacts(project_id))
    approved = [item for item in artifacts if str(item.status) == APPROVED]
    by_type: dict[str, list[Any]] = {}
    for item in artifacts:
        by_type.setdefault(str(item.artifact_type), []).append(item)

    if paper_text is None:
        approved_papers = [item for item in approved if str(item.artifact_type) == "paper_source"]
        paper_artifact = sorted(approved_papers, key=lambda item: item.created_at)[-1] if approved_papers else None
        paper_text = _artifact_text(store, paper_artifact, limit=200000) if paper_artifact else ""

    checks: list[dict[str, Any]] = []

    figures = by_type.get("figure", [])
    unreferenced = [item.name for item in figures if item.name not in paper_text]
    checks.append(
        {
            "code": "figure_references",
            "status": "warn" if unreferenced else "pass",
            "detail": f"图表 {len(figures)} 个，未被正文引用 {len(unreferenced)} 个" + (f"：{', '.join(unreferenced[:5])}" if unreferenced else ""),
        }
    )

    citations = {key.strip() for match in CITE_PATTERN.findall(paper_text) for key in match.split(",") if key.strip()}
    bibliography = {key.strip() for key in BIBITEM_PATTERN.findall(paper_text)}
    missing_citations = sorted(citations - bibliography)
    checks.append(
        {
            "code": "citation_keys",
            "status": "fail" if missing_citations else "pass",
            "detail": f"引用 {len(citations)} 处，缺少文献条目 {missing_citations}" if missing_citations else f"引用 {len(citations)} 处均有文献条目",
        }
    )

    labels = {value.strip() for value in LABEL_PATTERN.findall(paper_text)}
    refs = {value.strip() for value in REF_PATTERN.findall(paper_text)}
    dangling_refs = sorted(refs - labels)
    checks.append(
        {
            "code": "equation_labels",
            "status": "fail" if dangling_refs else "pass",
            "detail": f"公式标签 {len(labels)} 个，悬空引用 {dangling_refs}" if dangling_refs else f"公式标签 {len(labels)} 个，引用闭合",
        }
    )

    anonymity_hits = [pattern for pattern in ANONYMITY_PATTERNS if re.search(pattern, paper_text)]
    checks.append(
        {
            "code": "anonymity",
            "status": "fail" if anonymity_hits else "pass",
            "detail": f"命中需匿名化字段：{anonymity_hits}" if anonymity_hits else "未发现学校/姓名/队号等需匿名字段",
        }
    )

    missing_deliverables = [artifact_type for artifact_type in REQUIRED_DELIVERABLES if not by_type.get(artifact_type)]
    checks.append(
        {
            "code": "attachments",
            "status": "fail" if missing_deliverables else "pass",
            "detail": f"缺少交付附件：{missing_deliverables}" if missing_deliverables else "论文、结果表、编译 PDF 齐备",
        }
    )

    compiled = by_type.get("compiled_pdf") or []
    if compiled:
        from .latex_compile import pdf_page_count

        latest_pdf = sorted(compiled, key=lambda item: item.created_at)[-1]
        pages: int | None = None
        try:
            pages = pdf_page_count(store.get_artifact_content(latest_pdf.id))
        except (KeyError, ValueError):
            pages = None
        checks.append(
            {
                "code": "page_count",
                "status": "pass" if pages else "warn",
                "detail": f"编译 PDF 共 {pages} 页（{latest_pdf.name}）" if pages else "已登记编译 PDF，但内容未落库或页数无法解析",
            }
        )
    else:
        checks.append({"code": "page_count", "status": "fail", "detail": "尚无编译 PDF"})

    blocking = [item for item in checks if item["status"] == "fail"]
    return {
        "project_id": str(project_id),
        "checks": checks,
        "blocking_count": len(blocking),
        "warn_count": sum(1 for item in checks if item["status"] == "warn"),
        "passed": not blocking,
        "approved_artifact_count": len(approved),
        "paper_text_hash": hashlib.sha256(paper_text.encode("utf-8")).hexdigest(),
    }


def create_submission_bundle(
    store: Any,
    project_id: UUID,
    *,
    actor: str = "member-001",
    actor_kind: str = "member",
    label: str = "",
) -> dict[str, Any]:
    """生成提交包（装配 + 检查 + 校验清单），并提交待审以进入冻结流程。"""

    from .contracts import ArtifactCreate, EvidenceCreate

    assembly = assemble_paper(store, project_id)
    if not assembly["generatable"]:
        raise DeliveryError("submission_bundle_no_approved_sources")
    checklist = build_delivery_checklist(store, project_id)
    slides = assemble_slides(store, project_id)

    manifest = {
        "schema_version": "1.0",
        "project_id": str(project_id),
        "pack_id": assembly["pack_id"],
        "pack_version": assembly["pack_version"],
        "label": label,
        "sources": assembly["sources"],
        "excluded_unapproved": assembly["excluded_unapproved"],
        "sections": [
            {
                "title": section["title"],
                "source_name": section["source_name"],
                "content_hash": section["content_hash"],
                "approved_by": section["approved_by"],
            }
            for section in assembly["sections"]
        ],
        "checklist": checklist["checks"],
        "blocking_count": checklist["blocking_count"],
        "paper_text_hash": checklist["paper_text_hash"],
    }
    manifest["manifest_hash"] = hashlib.sha256(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    payload = {
        "manifest": manifest,
        "slides": slides,
        "paper_sections": [
            {"title": section["title"], "content": section["content"]} for section in assembly["sections"]
        ],
    }
    content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    content_hash = hashlib.sha256(content).hexdigest()

    artifact = store.create_artifact(
        project_id,
        ArtifactCreate(
            name="SUBMISSION_BUNDLE.json" if not label else f"SUBMISSION_BUNDLE-{label}.json",
            artifact_type="submission_bundle",
            description=f"提交包：{assembly['section_count']} 个章节、{len(assembly['sources'])} 个已批准来源",
            content_hash=content_hash,
            mime_type="application/json",
        ),
        created_by=actor,
        created_by_kind=actor_kind,
    )
    store.store_artifact_content(artifact.id, content)
    store.create_evidence(
        project_id,
        EvidenceCreate(
            claim=f"提交包由 {len(assembly['sources'])} 个已批准成果物装配（pack {manifest['pack_id']} v{manifest['pack_version']}）",
            evidence_type="artifact",
            artifact_id=artifact.id,
            created_by=actor,
        ),
    )
    submit_error: str | None = None
    try:
        artifact = store.submit_artifact_for_review(artifact.id, actor=actor, actor_kind=actor_kind)
    except ValueError as error:  # pragma: no cover - 证据已建，理论不会触发
        submit_error = str(error)

    return {
        "artifact_id": str(artifact.id),
        "content_hash": content_hash,
        "manifest_hash": manifest["manifest_hash"],
        "layer": "submitted" if str(artifact.status) == "PENDING_REVIEW" else "draft",
        "status": str(artifact.status),
        "section_count": assembly["section_count"],
        "source_count": len(assembly["sources"]),
        "excluded_unapproved_count": assembly["excluded_count"],
        "blocking_count": checklist["blocking_count"],
        "checks": checklist["checks"],
        "submit_error": submit_error,
    }



def compile_paper(
    store: Any,
    project_id: UUID,
    *,
    source: str | None = None,
    artifact_name: str = "main.pdf",
    actor: str = "member-001",
    actor_kind: str = "member",
) -> dict[str, Any]:
    """把批准论文源码编译为 PDF 并登记为 `compiled_pdf` 成果物。

    源码优先取参数，其次取最新的**已批准** `paper_source`；引擎缺失或编译失败时
    返回 `not_compiled` 诊断，不登记任何成果物（fail-closed）。
    """

    from .contracts import ArtifactCreate
    from .latex_compile import compile_latex

    artifacts = list(store.list_artifacts(project_id))
    approved_papers = [
        item for item in artifacts if str(item.artifact_type) == "paper_source" and str(item.status) == APPROVED
    ]
    if source is None:
        if not approved_papers:
            raise DeliveryError("compile_requires_approved_paper_source")
        paper = sorted(approved_papers, key=lambda item: item.created_at)[-1]
        source = _artifact_text(store, paper, limit=500000)
        if not source.strip():
            raise DeliveryError("compile_source_unreadable")
    else:
        paper = None

    result = compile_latex(source)
    if not result.succeeded:
        return {
            "project_id": str(project_id),
            "status": result.status,
            "reason": result.reason,
            "engine": result.engine,
            "source_artifact_id": str(paper.id) if paper else None,
            "log_tail": result.log_tail[-500:],
        }

    artifact = store.create_artifact(
        project_id,
        ArtifactCreate(
            name=artifact_name,
            artifact_type="compiled_pdf",
            description=f"LaTeX 编译产物（{result.engine}，{result.pages or '未知'} 页）",
            content_hash=result.pdf_sha256,
            mime_type="application/pdf",
            task_id=paper.task_id if paper else None,
        ),
        created_by=actor,
        created_by_kind=actor_kind,
    )
    store.store_artifact_content(artifact.id, result.pdf_bytes)
    return {
        "project_id": str(project_id),
        "status": "compiled",
        "engine": result.engine,
        "pages": result.pages,
        "artifact_id": str(artifact.id),
        "artifact_name": artifact.name,
        "pdf_sha256": result.pdf_sha256,
        "pdf_bytes": len(result.pdf_bytes),
        "source_artifact_id": str(paper.id) if paper else None,
        "layer": "draft",
    }


def verify_submission_bundle(
    store: Any,
    project_id: UUID,
    artifact_id: UUID,
    *,
    restore_store: Any = None,
) -> dict[str, Any]:
    """校验提交包：自身一致性 + 可选跨部署恢复比对。"""

    artifact = store.get_artifact(artifact_id)
    if str(artifact.project_id) != str(project_id):
        raise DeliveryError("submission_bundle_not_in_project")
    payload = json.loads(store.get_artifact_content(artifact_id).decode("utf-8"))
    manifest = payload.get("manifest", {})
    entries = list(manifest.get("sources", []))

    # 同名成果物在项目里可能不止一个（例如种子项目自带 MODELING_REPORT.md），
    # 因此优先按 artifact_id 解析，只有 id 不可用时才退回文件名匹配。
    by_id = {str(item.id): item for item in store.list_artifacts(project_id)}
    by_name: dict[str, list[Any]] = {}
    for item in store.list_artifacts(project_id):
        by_name.setdefault(item.name, []).append(item)

    def resolve(entry: Mapping[str, Any]) -> Any:
        current = by_id.get(str(entry.get("artifact_id")))
        if current is not None:
            return current
        candidates = by_name.get(str(entry.get("name")), [])
        return candidates[-1] if candidates else None

    mismatches: list[str] = []
    checked = 0
    for entry in entries:
        current = resolve(entry)
        if current is None:
            mismatches.append(f"{entry.get('name')}:missing")
            continue
        if str(current.content_hash) != str(entry.get("content_hash")):
            mismatches.append(f"{entry.get('name')}:hash_changed")
        checked += 1

    restore_report: dict[str, Any] | None = None
    if restore_store is not None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_path = store.export_project_bundle(project_id, Path(tmp) / "project-bundle")
            restored_project = restore_store.restore_project_bundle(bundle_path)
            restored_artifacts = list(restore_store.list_artifacts(restored_project.id))
            restored_by_id = {str(item.id): item for item in restored_artifacts}
            restored_by_name: dict[str, list[Any]] = {}
            for item in restored_artifacts:
                restored_by_name.setdefault(item.name, []).append(item)
            restore_mismatches: list[str] = []
            for entry in entries:
                current = restored_by_id.get(str(entry.get("artifact_id")))
                if current is None:
                    candidates = restored_by_name.get(str(entry.get("name")), [])
                    current = candidates[-1] if candidates else None
                if current is None:
                    restore_mismatches.append(f"{entry.get('name')}:missing_after_restore")
                elif str(current.content_hash) != str(entry.get("content_hash")):
                    restore_mismatches.append(f"{entry.get('name')}:hash_mismatch_after_restore")
            restore_report = {
                "restored_project_id": str(restored_project.id),
                "restored_artifact_count": len(restored_artifacts),
                "checked": len(entries),
                "mismatches": restore_mismatches,
                "restored": not restore_mismatches,
            }
            mismatches.extend(restore_mismatches)

    return {
        "artifact_id": str(artifact_id),
        "manifest_hash": manifest.get("manifest_hash"),
        "checked_sources": checked,
        "expected_sources": len(entries),
        "mismatches": mismatches,
        "verified": checked == len(entries) and not mismatches,
        "restore": restore_report,
    }


__all__ = [
    "DeliveryError",
    "PAPER_SECTIONS",
    "REQUIRED_DELIVERABLES",
    "assemble_paper",
    "assemble_slides",
    "build_delivery_checklist",
    "compile_paper",
    "create_submission_bundle",
    "verify_submission_bundle",
]