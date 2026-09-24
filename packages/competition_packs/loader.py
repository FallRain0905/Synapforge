"""Competition domain packs (CUMCM 等) 的加载与版本规则。

一个 pack 是自包含、带版本的竞赛领域工作流描述：

- 项目必须产出的成果物清单（required artifacts）；
- 为每类成果物提供脚手架模板（templates）；
- 把题面四问拆成任务 DAG（four-question task DAG）；
- 判定"交付是否完整"的校验规则（validation rules）；
- 版本升级规则（upgrade rules），保证 pack 升级不破坏既有项目。

设计约束（PROJECT_EXECUTION_PLAN §13.3）：竞赛特有字段只存在于 pack 内，
平台通用内核不得感知 CUMCM 细节。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


PACKS_ROOT = Path(__file__).resolve().parent
PLACEHOLDER_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class CompetitionPackError(RuntimeError):
    """稳定错误族：pack 缺失、格式非法或渲染失败。"""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code if not detail else f"{code}:{detail}")
        self.code = code


def parse_version(value: str) -> tuple[int, int, int]:
    """把 ``major.minor.patch`` 解析为可比较的元组。"""

    if not isinstance(value, str) or not value.strip():
        raise CompetitionPackError("pack_version_invalid")
    parts = value.strip().lstrip("v").split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise CompetitionPackError("pack_version_invalid", value)
    return tuple(int(part) for part in parts)  # type: ignore[return-value]


@dataclass(frozen=True)
class TemplateSpec:
    """一个成果物模板。"""

    template_id: str
    name: str
    artifact_type: str
    filename: str
    stage: str
    description: str = ""
    questions: tuple[int, ...] = ()
    official_format: bool = False

    def __post_init__(self) -> None:
        for field_name in ("template_id", "name", "artifact_type", "filename", "stage"):
            if not isinstance(getattr(self, field_name), str) or not getattr(self, field_name).strip():
                raise CompetitionPackError(f"pack_template_{field_name}_required")
        if any(not isinstance(item, int) or isinstance(item, bool) or item < 1 for item in self.questions):
            raise CompetitionPackError("pack_template_question_invalid")

    @property
    def relative_path(self) -> str:
        return f"templates/{self.filename}"


@dataclass(frozen=True)
class DagTask:
    """四问 DAG 中的一个任务节点。"""

    task_id: str
    title: str
    stage: str
    question: int | None = None
    depends_on: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    description: str = ""
    role: str = "agent"

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id.strip():
            raise CompetitionPackError("pack_dag_task_id_required")
        if not isinstance(self.title, str) or not self.title.strip():
            raise CompetitionPackError(f"pack_dag_task_title_required:{self.task_id}")
        if self.question is not None and (not isinstance(self.question, int) or isinstance(self.question, bool) or self.question < 1):
            raise CompetitionPackError(f"pack_dag_question_invalid:{self.task_id}")


@dataclass(frozen=True)
class UpgradeRule:
    """pack 版本升级规则。"""

    from_version: str
    to_version: str
    compatibility: str  # "compatible" | "requires_review" | "breaking"
    added_artifacts: tuple[str, ...] = ()
    renamed_artifacts: Mapping[str, str] = field(default_factory=dict)
    removed_artifacts: tuple[str, ...] = ()
    notes: str = ""

    def __post_init__(self) -> None:
        parse_version(self.from_version)
        parse_version(self.to_version)
        if self.compatibility not in {"compatible", "requires_review", "breaking"}:
            raise CompetitionPackError(f"pack_upgrade_compatibility_invalid:{self.from_version}->{self.to_version}")


@dataclass(frozen=True)
class PackManifest:
    """pack 元数据与规则集合。"""

    pack_id: str
    display_name: str
    version: str
    schema_version: str
    description: str
    competition_aliases: tuple[str, ...]
    problem_codes: tuple[str, ...]
    default_problem_code: str
    stage_sequence: tuple[str, ...]
    templates: tuple[TemplateSpec, ...]
    required_artifacts: tuple[str, ...]
    optional_artifacts: tuple[str, ...] = ()
    upgrade_rules: tuple[UpgradeRule, ...] = ()
    validation_rules: Mapping[str, Any] = field(default_factory=dict)
    boundary_rules: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.pack_id, str) or not self.pack_id.strip():
            raise CompetitionPackError("pack_id_required")
        parse_version(self.version)
        if self.default_problem_code not in self.problem_codes:
            raise CompetitionPackError("pack_default_problem_code_not_in_codes")
        for spec in self.templates:
            if spec.template_id in {other.template_id for other in self.templates if other is not spec}:
                raise CompetitionPackError(f"pack_template_id_duplicate:{spec.template_id}")

    def template(self, template_id: str) -> TemplateSpec:
        for spec in self.templates:
            if spec.template_id == template_id:
                return spec
        raise CompetitionPackError("pack_template_not_found", template_id)

    def templates_for_stage(self, stage: str) -> tuple[TemplateSpec, ...]:
        return tuple(spec for spec in self.templates if spec.stage == stage)

    def templates_for_question(self, question: int) -> tuple[TemplateSpec, ...]:
        return tuple(spec for spec in self.templates if not spec.questions or question in spec.questions)

    def required_validation(self) -> Mapping[str, Any]:
        return dict(self.validation_rules.get("required_artifacts", {}))

    def plan_upgrade(self, from_version: str) -> "UpgradePlan":
        """计算从 ``from_version`` 升级到本版本需要执行的规则与迁移。"""

        target = parse_version(from_version)
        current = parse_version(self.version)
        if target > current:
            raise CompetitionPackError("pack_downgrade_not_supported", from_version)
        if target == current:
            return UpgradePlan(from_version, self.version, "noop", (), (), (), ())
        applicable = [
            rule
            for rule in self.upgrade_rules
            if parse_version(rule.from_version) >= target and parse_version(rule.to_version) <= current
        ]
        applicable.sort(key=lambda rule: parse_version(rule.to_version))
        added: list[str] = []
        removed: list[str] = []
        renamed: list[tuple[str, str]] = []
        compatibility = "compatible"
        for rule in applicable:
            added.extend(rule.added_artifacts)
            removed.extend(rule.removed_artifacts)
            renamed.extend(rule.renamed_artifacts.items())
            if rule.compatibility == "breaking" or (rule.compatibility == "requires_review" and compatibility == "compatible"):
                compatibility = rule.compatibility
        return UpgradePlan(from_version, self.version, compatibility, tuple(added), tuple(removed), tuple(renamed), tuple(rule.notes for rule in applicable))


@dataclass(frozen=True)
class UpgradePlan:
    """一次 pack 升级的可执行计划。"""

    from_version: str
    to_version: str
    compatibility: str
    added_artifacts: tuple[str, ...]
    removed_artifacts: tuple[str, ...]
    renamed_artifacts: tuple[tuple[str, str], ...]
    notes: tuple[str, ...] = ()

    @property
    def requires_review(self) -> bool:
        return self.compatibility in {"requires_review", "breaking"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "from_version": self.from_version,
            "to_version": self.to_version,
            "compatibility": self.compatibility,
            "requires_review": self.requires_review,
            "added_artifacts": list(self.added_artifacts),
            "removed_artifacts": list(self.removed_artifacts),
            "renamed_artifacts": [{"from": source, "to": target} for source, target in self.renamed_artifacts],
            "notes": list(self.notes),
        }


class CompetitionPack:
    """已加载的 pack：manifest + DAG + 模板文件。"""

    def __init__(self, manifest: PackManifest, dag: Sequence[DagTask], root: Path) -> None:
        self.manifest = manifest
        self._dag = tuple(dag)
        self.root = root
        self._dag_by_id = {task.task_id: task for task in self._dag}
        if len(self._dag_by_id) != len(self._dag):
            raise CompetitionPackError("pack_dag_task_id_duplicate")
        self._validate_dag()

    def _validate_dag(self) -> None:
        for task in self._dag:
            for dependency in task.depends_on:
                if dependency not in self._dag_by_id:
                    raise CompetitionPackError("pack_dag_dependency_missing", f"{task.task_id}->{dependency}")
        # Cycle detection keeps the DAG safe to materialize as task dependencies.
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visited:
                return
            if task_id in visiting:
                raise CompetitionPackError("pack_dag_cycle_detected", task_id)
            visiting.add(task_id)
            for dependency in self._dag_by_id[task_id].depends_on:
                visit(dependency)
            visiting.discard(task_id)
            visited.add(task_id)

        for task in self._dag:
            visit(task.task_id)

    @property
    def pack_id(self) -> str:
        return self.manifest.pack_id

    @property
    def version(self) -> str:
        return self.manifest.version

    def dag(self) -> tuple[DagTask, ...]:
        return self._dag

    def dag_task(self, task_id: str) -> DagTask:
        try:
            return self._dag_by_id[task_id]
        except KeyError as error:
            raise CompetitionPackError("pack_dag_task_not_found", task_id) from error

    def questions(self) -> tuple[int, ...]:
        return tuple(sorted({task.question for task in self._dag if task.question is not None}))

    def tasks_for_question(self, question: int) -> tuple[DagTask, ...]:
        return tuple(task for task in self._dag if task.question == question)

    def template_text(self, template_id: str) -> str:
        spec = self.manifest.template(template_id)
        path = self.root / spec.relative_path
        if not path.is_file():
            raise CompetitionPackError("pack_template_file_missing", spec.relative_path)
        return path.read_text(encoding="utf-8")

    def render_template(self, template_id: str, variables: Mapping[str, Any]) -> str:
        """渲染模板占位符；未提供变量时 fail-closed。"""

        spec = self.manifest.template(template_id)
        text = self.template_text(template_id)
        missing: list[str] = []
        provided = {key: "" if value is None else str(value) for key, value in variables.items()}

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in provided:
                missing.append(name)
                return match.group(0)
            return provided[name]

        rendered = PLACEHOLDER_PATTERN.sub(replace, text)
        if missing:
            raise CompetitionPackError("pack_template_variables_missing", f"{spec.template_id}:{','.join(sorted(set(missing)))}")
        return rendered

    def placeholders(self, template_id: str) -> tuple[str, ...]:
        return tuple(sorted(set(PLACEHOLDER_PATTERN.findall(self.template_text(template_id)))))

    def validation_rules(self) -> Mapping[str, Any]:
        return dict(self.manifest.validation_rules)

    def boundary_rules(self) -> Mapping[str, Any]:
        return dict(self.manifest.boundary_rules)

    def as_dict(self) -> dict[str, Any]:
        return {
            "pack_id": self.manifest.pack_id,
            "display_name": self.manifest.display_name,
            "version": self.manifest.version,
            "schema_version": self.manifest.schema_version,
            "description": self.manifest.description,
            "competition_aliases": list(self.manifest.competition_aliases),
            "problem_codes": list(self.manifest.problem_codes),
            "default_problem_code": self.manifest.default_problem_code,
            "stage_sequence": list(self.manifest.stage_sequence),
            "questions": list(self.questions()),
            "required_artifacts": list(self.manifest.required_artifacts),
            "optional_artifacts": list(self.manifest.optional_artifacts),
            "templates": [
                {
                    "template_id": spec.template_id,
                    "name": spec.name,
                    "artifact_type": spec.artifact_type,
                    "filename": spec.filename,
                    "stage": spec.stage,
                    "description": spec.description,
                    "questions": list(spec.questions),
                    "official_format": spec.official_format,
                    "placeholders": list(self.placeholders(spec.template_id)),
                }
                for spec in self.manifest.templates
            ],
            "dag": [
                {
                    "task_id": task.task_id,
                    "title": task.title,
                    "stage": task.stage,
                    "question": task.question,
                    "depends_on": list(task.depends_on),
                    "produces": list(task.produces),
                    "role": task.role,
                    "description": task.description,
                }
                for task in self._dag
            ],
            "validation_rules": dict(self.manifest.validation_rules),
            "boundary_rules": dict(self.manifest.boundary_rules),
            "upgrade_rules": [
                {
                    "from_version": rule.from_version,
                    "to_version": rule.to_version,
                    "compatibility": rule.compatibility,
                    "added_artifacts": list(rule.added_artifacts),
                    "renamed_artifacts": dict(rule.renamed_artifacts),
                    "removed_artifacts": list(rule.removed_artifacts),
                    "notes": rule.notes,
                }
                for rule in self.manifest.upgrade_rules
            ],
        }

    def schemas(self) -> dict[str, Any]:
        """返回 pack 内的 JSON Schema（题面事实、数据画像等）。"""

        result: dict[str, Any] = {}
        schema_dir = self.root / "schemas"
        if schema_dir.is_dir():
            for path in sorted(schema_dir.glob("*.json")):
                result[path.stem] = json.loads(path.read_text(encoding="utf-8"))
        return result


def available_pack_ids() -> list[str]:
    return sorted(
        path.name
        for path in PACKS_ROOT.iterdir()
        if path.is_dir() and (path / "manifest.json").is_file()
    )


def load_pack(pack_id: str) -> CompetitionPack:
    """从磁盘加载一个 pack；结构非法时 fail-closed。"""

    if not isinstance(pack_id, str) or not pack_id.strip():
        raise CompetitionPackError("pack_id_required")
    root = PACKS_ROOT / pack_id
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise CompetitionPackError("pack_not_found", pack_id)
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = _manifest_from_dict(raw)
    dag_path = root / "four_question_dag.json"
    dag: list[DagTask] = []
    if dag_path.is_file():
        dag = _dag_from_list(json.loads(dag_path.read_text(encoding="utf-8")))
    return CompetitionPack(manifest, dag, root)


def list_packs() -> list[CompetitionPack]:
    return [load_pack(pack_id) for pack_id in available_pack_ids()]


def resolve_pack_id(competition_pack: str | None) -> str:
    """把项目上的 ``competition_pack`` 值解析为包 id。"""

    if not competition_pack:
        return "cumcm"
    value = competition_pack.strip().lower()
    for pack_id in available_pack_ids():
        if value == pack_id.lower():
            return pack_id
        pack = load_pack(pack_id)
        for alias in pack.manifest.competition_aliases:
            if value == alias.lower() or value.startswith(f"{alias.lower()}-"):
                return pack_id
    raise CompetitionPackError("pack_not_found", competition_pack)


def _manifest_from_dict(raw: Mapping[str, Any]) -> PackManifest:
    templates = tuple(
        TemplateSpec(
            template_id=str(item["template_id"]),
            name=str(item["name"]),
            artifact_type=str(item["artifact_type"]),
            filename=str(item["filename"]),
            stage=str(item["stage"]),
            description=str(item.get("description", "")),
            questions=tuple(int(value) for value in item.get("questions", ()) or ()),
            official_format=bool(item.get("official_format", False)),
        )
        for item in raw.get("templates", ())
    )
    upgrade_rules = tuple(
        UpgradeRule(
            from_version=str(item["from_version"]),
            to_version=str(item["to_version"]),
            compatibility=str(item.get("compatibility", "compatible")),
            added_artifacts=tuple(str(value) for value in item.get("added_artifacts", ()) or ()),
            renamed_artifacts={str(key): str(value) for key, value in (item.get("renamed_artifacts") or {}).items()},
            removed_artifacts=tuple(str(value) for value in item.get("removed_artifacts", ()) or ()),
            notes=str(item.get("notes", "")),
        )
        for item in raw.get("upgrade_rules", ())
    )
    return PackManifest(
        pack_id=str(raw["pack_id"]),
        display_name=str(raw["display_name"]),
        version=str(raw["version"]),
        schema_version=str(raw.get("schema_version", "1.0")),
        description=str(raw.get("description", "")),
        competition_aliases=tuple(str(value) for value in raw.get("competition_aliases", ()) or ()),
        problem_codes=tuple(str(value) for value in raw.get("problem_codes", ()) or ("A", "B", "C", "D", "E")),
        default_problem_code=str(raw.get("default_problem_code", "C")),
        stage_sequence=tuple(str(value) for value in raw.get("stage_sequence", ()) or ()),
        templates=templates,
        required_artifacts=tuple(str(value) for value in raw.get("required_artifacts", ()) or ()),
        optional_artifacts=tuple(str(value) for value in raw.get("optional_artifacts", ()) or ()),
        upgrade_rules=upgrade_rules,
        validation_rules=dict(raw.get("validation_rules", {}) or {}),
        boundary_rules=dict(raw.get("boundary_rules", {}) or {}),
    )


def _dag_from_list(raw: Sequence[Mapping[str, Any]]) -> list[DagTask]:
    tasks: list[DagTask] = []
    for item in raw:
        question = item.get("question")
        tasks.append(
            DagTask(
                task_id=str(item["task_id"]),
                title=str(item["title"]),
                stage=str(item["stage"]),
                question=None if question is None else int(question),
                depends_on=tuple(str(value) for value in item.get("depends_on", ()) or ()),
                produces=tuple(str(value) for value in item.get("produces", ()) or ()),
                description=str(item.get("description", "")),
                role=str(item.get("role", "agent")),
            )
        )
    return tasks


__all__ = [
    "CompetitionPack",
    "CompetitionPackError",
    "DagTask",
    "PACKS_ROOT",
    "PackManifest",
    "TemplateSpec",
    "UpgradePlan",
    "UpgradeRule",
    "available_pack_ids",
    "list_packs",
    "load_pack",
    "parse_version",
    "resolve_pack_id",
]
