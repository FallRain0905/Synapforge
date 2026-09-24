"""把竞赛 pack 落地到项目。

- 四问 DAG → 平台任务（含 `dependency_task_ids` 依赖关系）；
- pack 模板 → 渲染后的成果物骨架（写入工作区并登记 Artifact + 内容哈希）；
- 全流程幂等：重复执行不会重复建任务或成果物。

只写入模板骨架与任务结构，不伪造任何结果数据。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import UUID

from .loader import CompetitionPack, CompetitionPackError, DagTask, TemplateSpec, load_pack, resolve_pack_id

try:  # 平台契约；packages 独立加载时允许缺失。
    from app.contracts import ArtifactCreate, TaskCreate
except ImportError:  # pragma: no cover - 独立加载路径
    ArtifactCreate = None  # type: ignore[assignment]
    TaskCreate = None  # type: ignore[assignment]


DEFAULT_CREATED_BY = "pack-materializer"


class PackMaterializationError(RuntimeError):
    """稳定错误族：项目不可用、模板缺失或写入失败。"""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code if not detail else f"{code}:{detail}")
        self.code = code


@dataclass(frozen=True)
class MaterializedTask:
    dag_task_id: str
    task_id: str
    title: str
    stage: str
    question: int | None
    created: bool
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class MaterializedArtifact:
    template_id: str
    artifact_type: str
    name: str
    artifact_id: str
    created: bool
    content_hash: str
    relative_path: str | None = None


@dataclass(frozen=True)
class PackMaterialization:
    project_id: str
    pack_id: str
    pack_version: str
    problem_code: str
    questions: tuple[int, ...]
    tasks: tuple[MaterializedTask, ...] = ()
    artifacts: tuple[MaterializedArtifact, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def created_task_count(self) -> int:
        return sum(1 for item in self.tasks if item.created)

    @property
    def created_artifact_count(self) -> int:
        return sum(1 for item in self.artifacts if item.created)

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "pack_id": self.pack_id,
            "pack_version": self.pack_version,
            "problem_code": self.problem_code,
            "questions": list(self.questions),
            "created_task_count": self.created_task_count,
            "created_artifact_count": self.created_artifact_count,
            "tasks": [
                {
                    "dag_task_id": item.dag_task_id,
                    "task_id": item.task_id,
                    "title": item.title,
                    "stage": item.stage,
                    "question": item.question,
                    "created": item.created,
                    "depends_on": list(item.depends_on),
                }
                for item in self.tasks
            ],
            "artifacts": [
                {
                    "template_id": item.template_id,
                    "artifact_type": item.artifact_type,
                    "name": item.name,
                    "artifact_id": item.artifact_id,
                    "created": item.created,
                    "content_hash": item.content_hash,
                    "relative_path": item.relative_path,
                }
                for item in self.artifacts
            ],
            "warnings": list(self.warnings),
        }


class PackMaterializer:
    """把 pack 的结构写入平台项目。"""

    def __init__(self, store: Any, pack: CompetitionPack | None = None) -> None:
        self.store = store
        self._pack = pack

    def resolve_pack(self, project: Any) -> CompetitionPack:
        if self._pack is not None:
            return self._pack
        return load_pack(resolve_pack_id(getattr(project, "competition_pack", None)))

    def apply(
        self,
        project_id: UUID | str,
        *,
        problem_code: str | None = None,
        questions: Sequence[int] | None = None,
        variables: Mapping[str, Any] | None = None,
        output_dir: Path | str | None = None,
        created_by: str = DEFAULT_CREATED_BY,
        create_tasks: bool = True,
        create_artifacts: bool = True,
    ) -> PackMaterialization:
        project = self.store.get_project(project_id if isinstance(project_id, UUID) else UUID(str(project_id)))
        pack = self.resolve_pack(project)
        code = problem_code or getattr(project, "problem_code", None) or pack.manifest.default_problem_code
        expected = tuple(self._expected_questions(pack))
        selected = tuple(sorted({int(value) for value in (questions or expected)}))
        if not selected:
            raise PackMaterializationError("pack_materialization_questions_required")
        unknown = [value for value in selected if value not in expected]
        if unknown:
            raise PackMaterializationError("pack_materialization_question_out_of_range", str(unknown))

        warnings: list[str] = []
        context = self._template_variables(project, pack, code, selected, variables)
        dag_tasks = self._selected_dag_tasks(pack, selected)

        task_map: dict[str, str] = {}
        materialized_tasks: list[MaterializedTask] = []
        if create_tasks:
            existing = {task.title: task for task in self.store.list_tasks(project.id)}
            for dag_task in dag_tasks:
                previous = existing.get(dag_task.title)
                if previous is not None:
                    task_map[dag_task.task_id] = str(previous.id)
                    materialized_tasks.append(
                        MaterializedTask(dag_task.task_id, str(previous.id), dag_task.title, dag_task.stage, dag_task.question, False, dag_task.depends_on)
                    )
                    continue
                created = self.store.create_task(
                    project.id,
                    TaskCreate(
                        title=dag_task.title,
                        description=dag_task.description or dag_task.title,
                        stage=dag_task.stage,
                        priority="high" if dag_task.stage in {"modeling", "coding"} else "medium",
                        requires_review=dag_task.stage in {"review", "paper", "delivery"},
                        output_types=list(dag_task.produces),
                        dependency_task_ids=[UUID(task_map[value]) for value in dag_task.depends_on if value in task_map],
                        acceptance_criteria=self._acceptance_criteria(pack, dag_task),
                        information_boundary=dict(pack.boundary_rules()),
                    ),
                )
                task_map[dag_task.task_id] = str(created.id)
                materialized_tasks.append(
                    MaterializedTask(dag_task.task_id, str(created.id), dag_task.title, dag_task.stage, dag_task.question, True, dag_task.depends_on)
                )

        materialized_artifacts: list[MaterializedArtifact] = []
        if create_artifacts:
            workspace = Path(output_dir).expanduser().resolve() if output_dir is not None else None
            if workspace is not None:
                workspace.mkdir(parents=True, exist_ok=True)
            existing_artifacts = {
                (artifact.name, str(artifact.artifact_type)): artifact for artifact in self.store.list_artifacts(project.id)
            }
            for spec in pack.manifest.templates:
                if spec.questions and not set(spec.questions).intersection(selected):
                    continue
                try:
                    content = pack.render_template(spec.template_id, context).encode("utf-8")
                except CompetitionPackError as error:
                    warnings.append(f"{spec.template_id}:{error.code}")
                    continue
                digest = hashlib.sha256(content).hexdigest()
                previous = existing_artifacts.get((spec.filename, spec.artifact_type))
                if previous is not None:
                    # The artifact is already registered (for example a seeded
                    # project file).  Keep the record as-is, but still drop the
                    # rendered scaffold into the workspace when it is missing,
                    # so every template is visible to the team.
                    if workspace is not None:
                        target = workspace / spec.filename
                        if not target.exists():
                            target.write_bytes(content)
                    materialized_artifacts.append(
                        MaterializedArtifact(spec.template_id, spec.artifact_type, spec.filename, str(previous.id), False, str(previous.content_hash or digest), spec.relative_path)
                    )
                    continue
                artifact = self.store.create_artifact(
                    project.id,
                    ArtifactCreate(
                        name=spec.filename,
                        artifact_type=spec.artifact_type,
                        description=spec.description,
                        content_hash=digest,
                        mime_type="application/json" if spec.filename.endswith(".json") else "text/markdown",
                        data_policy={
                            "source": "competition_pack",
                            "pack_id": pack.pack_id,
                            "pack_version": pack.version,
                            "template_id": spec.template_id,
                            "template_hash": digest,
                            "official_format": spec.official_format,
                            "information_boundary": dict(pack.boundary_rules()),
                        },
                    ),
                    created_by=created_by,
                    created_by_kind="agent",
                )
                try:
                    artifact = self.store.store_artifact_content(artifact.id, content, expected_hash=digest)
                except (OSError, ValueError) as error:
                    warnings.append(f"{spec.filename}:content_not_stored:{error}")
                if workspace is not None:
                    target = workspace / spec.filename
                    target.write_bytes(content)
                materialized_artifacts.append(
                    MaterializedArtifact(spec.template_id, spec.artifact_type, spec.filename, str(artifact.id), True, digest, spec.relative_path)
                )

        return PackMaterialization(
            project_id=str(project.id),
            pack_id=pack.pack_id,
            pack_version=pack.version,
            problem_code=code,
            questions=selected,
            tasks=tuple(materialized_tasks),
            artifacts=tuple(materialized_artifacts),
            warnings=tuple(warnings),
        )

    def render_for_project(
        self,
        project_id: UUID | str,
        template_id: str,
        *,
        problem_code: str | None = None,
        questions: Sequence[int] | None = None,
        variables: Mapping[str, Any] | None = None,
    ) -> str:
        """按项目上下文渲染单个模板（用于模板导出）。"""

        project = self.store.get_project(project_id if isinstance(project_id, UUID) else UUID(str(project_id)))
        pack = self.resolve_pack(project)
        code = problem_code or getattr(project, "problem_code", None) or pack.manifest.default_problem_code
        selected = tuple(sorted({int(value) for value in (questions or self._expected_questions(pack))}))
        context = self._template_variables(project, pack, code, selected, variables)
        return pack.render_template(template_id, context)

    @staticmethod
    def _expected_questions(pack: CompetitionPack) -> tuple[int, ...]:
        coverage = pack.validation_rules().get("question_coverage", {})
        expected = coverage.get("expected_questions") or pack.questions()
        return tuple(int(value) for value in expected)

    @staticmethod
    def _selected_dag_tasks(pack: CompetitionPack, questions: Sequence[int]) -> tuple[DagTask, ...]:
        """返回与所选问题相关的 DAG 任务，并保证依赖顺序。"""

        selected = set(questions)
        chosen = [task for task in pack.dag() if task.question is None or task.question in selected]
        ordered: list[DagTask] = []
        placed: set[str] = set()
        pending = list(chosen)
        while pending:
            progressed = False
            for task in list(pending):
                if all(dependency in placed or dependency not in {item.task_id for item in chosen} for dependency in task.depends_on):
                    ordered.append(task)
                    placed.add(task.task_id)
                    pending.remove(task)
                    progressed = True
            if not progressed:  # 理论上不会发生：loader 已检测环
                raise PackMaterializationError("pack_dag_order_failed", ",".join(task.task_id for task in pending))
        return tuple(ordered)

    @staticmethod
    def _acceptance_criteria(pack: CompetitionPack, task: DagTask) -> list[str]:
        criteria = [f"产出：{', '.join(task.produces)}"] if task.produces else []
        for artifact_type in task.produces:
            rules = pack.validation_rules().get("required_artifacts", {}).get(artifact_type, {})
            sections = rules.get("required_sections") or []
            if sections:
                criteria.append("章节完整：" + "、".join(sections))
            if rules.get("min_words"):
                criteria.append(f"正文不少于 {rules['min_words']} 字")
        if task.question is not None:
            criteria.append(f"覆盖第 {task.question} 问且结果可复算")
        boundary = pack.boundary_rules()
        if boundary.get("future_data_policy") == "deny":
            criteria.append("不得使用决策时间之后的数据")
        return criteria

    def _template_variables(
        self,
        project: Any,
        pack: CompetitionPack,
        problem_code: str,
        questions: Sequence[int],
        overrides: Mapping[str, Any] | None,
    ) -> dict[str, str]:
        competition = "CUMCM" if pack.pack_id == "cumcm" else pack.pack_id.upper()
        values: dict[str, Any] = {
            "project_name": getattr(project, "name", "") or str(project.id),
            "competition": competition,
            "problem_code": problem_code,
            "problem_id": getattr(project, "problem_code", None) and f"{competition}-{getattr(project, 'problem_code')}" or f"{competition}-{problem_code}",
            "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            "pack_version": pack.version,
            "n_questions": len(questions),
            "questions_block": self._questions_block(pack, questions),
        }
        for key, value in (overrides or {}).items():
            values[str(key)] = value
        return {key: "" if value is None else str(value) for key, value in values.items()}

    @staticmethod
    def _questions_block(pack: CompetitionPack, questions: Sequence[int]) -> str:
        lines: list[str] = []
        for question in questions:
            tasks = pack.tasks_for_question(question)
            lines.append(f"### 问题{question}")
            lines.append("")
            if tasks:
                for task in tasks:
                    lines.append(f"- **{task.title}**（{task.stage}）：{task.description or task.title}")
                    if task.produces:
                        lines.append(f"  - 产出：{', '.join(task.produces)}")
            else:
                lines.append("- 按 DAG 补全本问的建模、代码与实验步骤。")
            lines.append("")
        return "\n".join(lines).strip()


__all__ = [
    "MaterializedArtifact",
    "MaterializedTask",
    "PackMaterialization",
    "PackMaterializationError",
    "PackMaterializer",
]
