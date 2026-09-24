"""CUMCM 工作区导入适配器。

导入器只做登记：保留原始文件路径和 SHA-256，不修改用户目录，也不把
文件内容偷偷送入模型。后续接入对象存储时可以沿用这些哈希和相对路径。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from .contracts import HandoffImportReport, ImportSummary, TaskCreate
from .store import Store


SINGLETON_TYPES = {
    "PROBLEM_ANALYSIS.md": ("problem_analysis", "赛题分析与任务拆解"),
    "PROBLEM_FACTS.json": ("problem_facts", "题面事实台账"),
    "DATA_PROFILE.json": ("data_profile", "数据画像与字段审计"),
    "MODELING_REPORT.md": ("model_spec", "数学模型与假设报告"),
    "AUDIT_REPORT.md": ("audit_report", "约束和结果审计报告"),
    "COMP_REVIEW.md": ("review_report", "独立复核报告"),
    "INFORMATION_BOUNDARY_REVIEW.md": ("audit_report", "信息边界审计报告"),
}

IGNORED_PARTS = {".git", ".venv", "node_modules", "__pycache__", "vendor", ".next", "dist", "build"}
CODE_SUFFIXES = {".py", ".mjs", ".js", ".ts", ".tsx", ".r", ".m", ".jl", ".java", ".cpp", ".c", ".h"}
FIGURE_SUFFIXES = {".png", ".jpg", ".jpeg", ".svg", ".pdf"}
RESULT_SUFFIXES = {".json", ".csv", ".xlsx", ".xls", ".tsv"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _classify(path: Path, relative: str) -> tuple[str, str] | None:
    name = path.name
    if name in SINGLETON_TYPES:
        return SINGLETON_TYPES[name]
    lower = relative.lower()
    suffix = path.suffix.lower()
    if "/code/" in f"/{lower}" or lower.startswith("code/") or suffix in CODE_SUFFIXES:
        return "code", "代码与计算脚本"
    if "/paper/" in f"/{lower}" or lower.startswith("paper/") or suffix in {".tex", ".bib", ".sty"}:
        return ("compiled_pdf", "编译后的论文文件") if suffix == ".pdf" else ("paper_source", "论文源文件")
    if "/figures/" in f"/{lower}" or lower.startswith("figures/"):
        return ("figure", "图表文件") if suffix in FIGURE_SUFFIXES else ("result_table", "实验结果或指标文件")
    if "/output/" in f"/{lower}" or lower.startswith("output/"):
        return ("figure", "输出图表") if suffix in FIGURE_SUFFIXES else ("result_table", "结果模板或输出表")
    if "/user_data/" in f"/{lower}" or lower.startswith("user_data/") or name.lower() in {"c题.pdf", "c_problem.pdf"}:
        return "problem_source", "题面或官方附件"
    if suffix in RESULT_SUFFIXES:
        return "result_table", "数据、结果或审计输入"
    return None


def _iter_candidates(root: Path) -> list[tuple[Path, str, str, str]]:
    candidates: list[tuple[Path, str, str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in IGNORED_PARTS for part in path.parts):
            continue
        relative = _safe_relative(path, root)
        classification = _classify(path, relative)
        if classification:
            artifact_type, description = classification
            candidates.append((path, relative, artifact_type, description))
    return candidates


class CumcmImporter:
    def __init__(self, store: Store) -> None:
        self.store = store

    def import_project(self, project_id: UUID, source_path: str, created_by: str = "importer", dry_run: bool = False) -> ImportSummary:
        root = Path(source_path).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise ValueError("source_path_must_be_existing_directory")
        candidates = _iter_candidates(root)
        warnings: list[str] = []
        if not candidates:
            warnings.append("没有发现可识别的 CUMCM 工作区文件")
        artifact_ids: list[UUID] = []
        imported = 0
        skipped = 0
        if not dry_run:
            self.store.get_project(project_id)
        for path, relative, artifact_type, description in candidates:
            try:
                content_hash = sha256_file(path)
            except OSError as error:
                warnings.append(f"无法读取 {relative}: {error}")
                continue
            if dry_run:
                imported += 1
                continue
            data_policy: dict[str, Any] = {
                "future_data": "deny",
                "source_relative_path": relative,
                "source_modified_at": path.stat().st_mtime,
            }
            if path.name in {"PROBLEM_FACTS.json", "DATA_PROFILE.json"}:
                try:
                    parsed = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(parsed, dict) and "information_boundary" in parsed:
                        data_policy["information_boundary"] = parsed["information_boundary"]
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                    warnings.append(f"{relative} 未能解析为 JSON: {error}")
            artifact, created = self.store.import_artifact(
                project_id,
                name=relative,
                artifact_type=artifact_type,
                description=description,
                source_path=str(path),
                content_hash=content_hash,
                data_policy=data_policy,
                created_by=created_by,
            )
            if created:
                try:
                    artifact = self.store.store_artifact_content(artifact.id, path.read_bytes(), expected_hash=content_hash)
                except (OSError, ValueError) as error:
                    warnings.append(f"{relative} 内容未能写入对象存储: {error}")
            artifact_ids.append(artifact.id)
            if created:
                imported += 1
            else:
                skipped += 1
        created_tasks = 0
        if not dry_run and not self.store.list_tasks(project_id):
            task_specs = [
                ("题面事实与数据画像", "整理题面、附件、字段和信息边界。", "problem_analysis", ["problem_facts", "data_profile"]),
                ("问题拆解与模型建立", "将四问转为变量、假设、约束和目标。", "modeling", ["problem_analysis", "model_spec"]),
                ("代码实现与实验", "执行可复现计算并登记参数、种子和结果。", "coding", ["code", "run_manifest", "result_table"]),
                ("独立复核与信息边界审计", "检查重复计量、未来信息和跨问一致性。", "review", ["audit_report", "review_report"]),
                ("论文编译与提交交付", "生成论文、编译 PDF 并完成提交包检查。", "delivery", ["paper_source", "compiled_pdf", "submission_bundle"]),
            ]
            for title, description, stage, output_types in task_specs:
                self.store.create_task(project_id, TaskCreate(title=title, description=description, stage=stage, output_types=output_types, allow_future_data=False))
                created_tasks += 1
        return ImportSummary(project_id=project_id, source_path=str(root), discovered_files=len(candidates), imported_artifacts=imported, skipped_artifacts=skipped, created_tasks=created_tasks, artifact_ids=artifact_ids, warnings=warnings)


# ---- 现有 C 题交接包的可解释导入 ----------------------------------------

# 交接包的目录布局 → 成果物类型；None 表示该目录是真实工作区，沿用既有分类。
HANDOFF_LAYOUT: dict[str, tuple[str, str] | None] = {
    "00_复审入口": ("review_report", "交接包复审入口文档"),
    "01_项目工作区": None,
    "02_外部参考": ("problem_source", "外部参考材料"),
    "03_工作流插件": ("code", "工作流插件与脚本"),
}


def _handoff_layout(root: Path) -> dict[str, Path]:
    """按前缀匹配交接包目录。

    真实交接包的目录名常带后缀（例如 ``02_外部参考_其他模型``），
    因此用前缀匹配而不是精确相等，否则整目录会被当成"未识别"。
    """

    layout: dict[str, Path] = {}
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        for name in HANDOFF_LAYOUT:
            if child.name == name or child.name.startswith(name):
                layout[name] = child
                break
    return layout


class CumcmHandoffImporter:
    """导入「C 题四问完整交接包」这类目录：工作区 + 复审入口 + 外部参考 + 插件。

    与纯工作区导入的差别是**可解释性**：报告会说明识别到的布局、每类文件的
    去向、未识别文件清单，以及是否无损（声明文件全部入库且哈希一致）。
    """

    def __init__(self, store: Store) -> None:
        self.store = store
        self._workspace_importer = CumcmImporter(store)

    def import_handoff(
        self,
        project_id: UUID,
        source_path: str,
        created_by: str = "handoff-importer",
        dry_run: bool = False,
    ) -> HandoffImportReport:
        root = Path(source_path).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            raise ValueError("source_path_must_be_existing_directory")
        layout = _handoff_layout(root)
        workspace = layout.get("01_项目工作区", root)

        warnings: list[str] = []
        classification: dict[str, int] = {}
        artifact_ids: list[UUID] = []
        unknown_files: list[str] = []
        hash_mismatches: list[str] = []
        imported = 0
        skipped = 0
        declared = 0

        candidates: list[tuple[Path, str, str, str]] = []
        if workspace.is_dir():
            for path, relative, artifact_type, description in _iter_candidates(workspace):
                candidates.append((path, f"01_项目工作区/{relative}" if workspace != root else relative, artifact_type, description))
        for directory, mapping in HANDOFF_LAYOUT.items():
            if mapping is None or directory not in layout:
                continue
            artifact_type, description = mapping
            for path in sorted(layout[directory].rglob("*")):
                if not path.is_file() or any(part in IGNORED_PARTS for part in path.parts):
                    continue
                relative = f"{directory}/{_safe_relative(path, layout[directory])}"
                candidates.append((path, relative, artifact_type, description))

        # 未识别文件：既不属于工作区、也不落在任何已识别的交接包目录下。
        # 注意比较的是**实际目录名**（可能带后缀），不是规则名。这些文件仍然
        # 以可解释的方式回退登记为 `problem_source`，绝不静默丢弃。
        known_dirs = {matched.name for matched in layout.values()}
        for path in sorted(root.rglob("*")):
            if not path.is_file() or any(part in IGNORED_PARTS for part in path.parts):
                continue
            relative = _safe_relative(path, root)
            if relative.split("/")[0] in known_dirs:
                continue
            unknown_files.append(relative)
            candidates.append((path, relative, "problem_source", "未分类文件（回退登记）"))

        declared = len(candidates)

        if dry_run:
            return HandoffImportReport(
                project_id=project_id,
                source_path=str(root),
                workspace_path=str(workspace),
                layout_detected=bool(layout),
                declared_files=declared,
                imported_artifacts=0,
                skipped_artifacts=0,
                unknown_files=unknown_files,
                hash_mismatches=[],
                lossless=False,
                created_tasks=0,
                artifact_ids=[],
                classification={},
                warnings=["dry_run：未写入任何成果物"],
            )

        self.store.get_project(project_id)
        for path, relative, artifact_type, description in candidates:
            try:
                content_hash = sha256_file(path)
            except OSError as error:
                warnings.append(f"无法读取 {relative}: {error}")
                continue
            data_policy: dict[str, Any] = {
                "future_data": "deny",
                "source_relative_path": relative,
                "source_modified_at": path.stat().st_mtime,
                "import_source": "cumcm_handoff",
            }
            artifact, created = self.store.import_artifact(
                project_id,
                name=relative,
                artifact_type=artifact_type,
                description=description,
                source_path=str(path),
                content_hash=content_hash,
                data_policy=data_policy,
                created_by=created_by,
            )
            if created:
                try:
                    self.store.store_artifact_content(artifact.id, path.read_bytes(), expected_hash=content_hash)
                except (OSError, ValueError) as error:
                    hash_mismatches.append(f"{relative}: {error}")
                    warnings.append(f"{relative} 内容未能无损入库: {error}")
            artifact_ids.append(artifact.id)
            classification[artifact_type] = classification.get(artifact_type, 0) + 1
            if created:
                imported += 1
            else:
                skipped += 1

        created_tasks = 0
        if not self.store.list_tasks(project_id):
            task_specs = [
                ("题面事实与数据画像", "整理题面、附件、字段和信息边界。", "problem_analysis", ["problem_facts", "data_profile"]),
                ("问题拆解与模型建立", "将四问转为变量、假设、约束和目标。", "modeling", ["problem_analysis", "model_spec"]),
                ("代码实现与实验", "执行可复现计算并登记参数、种子和结果。", "coding", ["code", "run_manifest", "result_table"]),
                ("独立复核与信息边界审计", "检查重复计量、未来信息和跨问一致性。", "review", ["audit_report", "review_report"]),
                ("论文编译与提交交付", "生成论文、编译 PDF 并完成提交包检查。", "delivery", ["paper_source", "compiled_pdf", "submission_bundle"]),
            ]
            for title, description, stage, output_types in task_specs:
                self.store.create_task(project_id, TaskCreate(title=title, description=description, stage=stage, output_types=output_types, allow_future_data=False))
                created_tasks += 1

        # 无损 = 参与导入的每个文件都已入库且哈希一致（未分类文件走回退登记，
        # 仍计入 declared，因此这里不必再单独要求 unknown_files 为空）。
        lossless = declared > 0 and imported + skipped == declared and not hash_mismatches
        return HandoffImportReport(
            project_id=project_id,
            source_path=str(root),
            workspace_path=str(workspace),
            layout_detected=bool(layout),
            declared_files=declared,
            imported_artifacts=imported,
            skipped_artifacts=skipped,
            unknown_files=unknown_files,
            hash_mismatches=hash_mismatches,
            lossless=lossless,
            created_tasks=created_tasks,
            artifact_ids=artifact_ids,
            classification=classification,
            warnings=warnings,
        )
