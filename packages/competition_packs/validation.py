"""pack 校验：四问覆盖、模板章节、官方结果表与信息边界规则。

校验只读已登记成果物与其文本，不做任何写入；结论用固定严重度口径
（fatal / major / minor / info）映射为 Review 可用的状态，便于生成机器 Review。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .loader import CompetitionPack


SECTION_PATTERN = re.compile(r"^#{1,6}\s*(.+?)\s*$", re.MULTILINE)
WORD_PATTERN = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9]+")

# 与 app.contracts.ArtifactType 保持一致；pack 不得声明平台未知的成果物类型。
PLATFORM_ARTIFACT_TYPES = {
    "problem_source",
    "problem_facts",
    "problem_analysis",
    "data_profile",
    "model_spec",
    "code",
    "experiment_plan",
    "run_manifest",
    "result_table",
    "figure",
    "audit_report",
    "review_report",
    "paper_source",
    "compiled_pdf",
    "submission_bundle",
}

BLOCKING_TYPES = {"result_table", "paper_source", "review_report"}
SEVERITY_ORDER = {"fatal": 0, "major": 1, "minor": 2, "info": 3}


@dataclass(frozen=True)
class ValidationFinding:
    severity: str
    code: str
    subject: str
    message: str

    def __post_init__(self) -> None:
        if self.severity not in SEVERITY_ORDER:
            raise ValueError(f"validation_severity_invalid:{self.severity}")

    def as_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "code": self.code, "subject": self.subject, "message": self.message}


@dataclass(frozen=True)
class ValidationReport:
    project_id: str
    pack_id: str
    pack_version: str
    status: str
    findings: tuple[ValidationFinding, ...] = ()
    coverage: Mapping[str, Any] = field(default_factory=dict)
    missing_artifacts: tuple[str, ...] = ()
    checked_artifacts: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return not any(item.severity in {"fatal", "major"} for item in self.findings)

    def worst_severity(self) -> str | None:
        if not self.findings:
            return None
        return min(self.findings, key=lambda item: SEVERITY_ORDER[item.severity]).severity

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "pack_id": self.pack_id,
            "pack_version": self.pack_version,
            "status": self.status,
            "allowed": self.allowed,
            "worst_severity": self.worst_severity(),
            "missing_artifacts": list(self.missing_artifacts),
            "checked_artifacts": list(self.checked_artifacts),
            "coverage": dict(self.coverage),
            "findings": [item.as_dict() for item in self.findings],
        }


class PackValidator:
    """按 pack manifest 的规则校验项目成果物。"""

    def __init__(self, pack: CompetitionPack) -> None:
        self.pack = pack
        self.rules = pack.validation_rules().get("required_artifacts", {})
        coverage = pack.validation_rules().get("question_coverage", {})
        self.expected_questions = tuple(int(value) for value in coverage.get("expected_questions", ()) or pack.questions())
        self.question_markers = tuple(
            str(value) for value in coverage.get("question_markers", ()) or _default_markers(self.expected_questions)
        )
        self.official_files = {
            int(key): str(value)
            for key, value in (pack.validation_rules().get("official_result_files") or {}).items()
        }
        # 模板脚手架（例如 RESULT_TABLE_TEMPLATE.md）不是交付结果表，官方文件名校验不适用。
        self.scaffold_filenames = {spec.filename for spec in pack.manifest.templates}

    # ---- 公共入口 ---------------------------------------------------------

    def validate(
        self,
        *,
        artifacts: Sequence[Mapping[str, Any]] = (),
        documents: Mapping[str, str] | None = None,
        project_id: str = "",
    ) -> ValidationReport:
        """校验一批已登记成果物；``documents`` 按成果物名提供文本内容。"""

        findings: list[ValidationFinding] = []
        documents = dict(documents or {})
        by_type: dict[str, list[Mapping[str, Any]]] = {}
        for artifact in artifacts:
            by_type.setdefault(str(artifact.get("artifact_type") or ""), []).append(artifact)

        missing = tuple(
            artifact_type
            for artifact_type in self.pack.manifest.required_artifacts
            if not by_type.get(artifact_type)
        )
        for artifact_type in missing:
            severity = "fatal" if artifact_type in BLOCKING_TYPES else "major"
            findings.append(
                ValidationFinding(severity, f"missing_required_artifact", artifact_type, f"缺少必需成果物：{artifact_type}")
            )

        checked: list[str] = []
        for artifact_type, items in by_type.items():
            rules = self.rules.get(artifact_type)
            for artifact in items:
                name = str(artifact.get("name") or "")
                checked.append(name or artifact_type)
                if rules is None:
                    continue
                text = documents.get(name)
                findings.extend(self._check_artifact(artifact, artifact_type, rules, text))

        coverage = self._check_question_coverage(by_type, documents, findings)
        findings.extend(self._check_information_boundary(by_type))

        report_findings = tuple(sorted(findings, key=lambda item: (SEVERITY_ORDER[item.severity], item.code, item.subject)))
        return ValidationReport(
            project_id=project_id,
            pack_id=self.pack.pack_id,
            pack_version=self.pack.version,
            status=_status_for(report_findings),
            findings=report_findings,
            coverage=coverage,
            missing_artifacts=missing,
            checked_artifacts=tuple(sorted(set(checked))),
        )

    def validate_pack(self) -> ValidationReport:
        """自检 pack 自身：模板成果物类型必须被平台识别。"""

        findings: list[ValidationFinding] = []
        for spec in self.pack.manifest.templates:
            if spec.artifact_type not in PLATFORM_ARTIFACT_TYPES:
                findings.append(
                    ValidationFinding("fatal", "pack_template_artifact_type_unknown", spec.template_id, f"未知成果物类型：{spec.artifact_type}")
                )
        declared = set(self.pack.manifest.required_artifacts)
        for artifact_type in declared:
            if artifact_type not in PLATFORM_ARTIFACT_TYPES:
                findings.append(
                    ValidationFinding("fatal", "pack_required_artifact_type_unknown", artifact_type, "必需成果物类型未被平台识别")
                )
        report_findings = tuple(sorted(findings, key=lambda item: (SEVERITY_ORDER[item.severity], item.code, item.subject)))
        return ValidationReport(
            project_id="",
            pack_id=self.pack.pack_id,
            pack_version=self.pack.version,
            status=_status_for(report_findings),
            findings=report_findings,
        )

    # ---- 单项检查 ---------------------------------------------------------

    def _check_artifact(
        self,
        artifact: Mapping[str, Any],
        artifact_type: str,
        rules: Mapping[str, Any],
        text: str | None,
    ) -> list[ValidationFinding]:
        findings: list[ValidationFinding] = []
        name = str(artifact.get("name") or artifact_type)
        if rules.get("requires_hash") and not artifact.get("content_hash"):
            findings.append(ValidationFinding("major", "artifact_content_hash_missing", name, "成果物缺少内容哈希，无法回溯"))
        if rules.get("requires_official_filename"):
            findings.extend(self._check_official_filename(artifact, artifact_type))
        if text is None:
            findings.append(ValidationFinding("minor", "artifact_text_unavailable", name, "未能读取成果物文本，章节与字数检查跳过"))
            return findings
        if rules.get("requires_entrypoint") and "code/" not in text:
            findings.append(ValidationFinding("major", "code_entrypoint_missing", name, "代码说明未声明 code/ 入口"))
        if rules.get("requires_output_paths") and "output/" not in text:
            findings.append(ValidationFinding("major", "code_output_paths_missing", name, "代码说明未声明 output/ 输出路径"))
        required_sections = [str(value) for value in rules.get("required_sections", ()) or ()]
        if required_sections:
            headings = _headings(text)
            for section in required_sections:
                if not _section_present(section, headings):
                    findings.append(ValidationFinding("major", "required_section_missing", f"{name}#{section}", f"缺少必需章节：{section}"))
        min_words = rules.get("min_words")
        if isinstance(min_words, int) and min_words > 0:
            words = _count_words(text)
            if words < min_words:
                findings.append(
                    ValidationFinding("major", "artifact_too_short", name, f"正文仅 {words} 字，少于要求 {min_words} 字")
                )
        required_keys = [str(value) for value in rules.get("required_keys", ()) or ()]
        forbidden_keys = [str(value) for value in rules.get("forbidden_keys", ()) or ()]
        if required_keys or forbidden_keys:
            findings.extend(self._check_json_keys(name, text, required_keys, forbidden_keys))
        if rules.get("requires_declared_time_range") and "information_boundary" not in text:
            findings.append(ValidationFinding("major", "information_boundary_missing", name, "数据画像缺少信息边界口径"))
        return findings

    def _is_scaffold(self, artifact: Mapping[str, Any]) -> bool:
        """判断成果物是否只是**尚未填写**的模板骨架。

        判定依据是登记时写入的 ``data_policy.template_id`` 与
        ``data_policy.template_hash``：只有内容哈希仍等于下发骨架的哈希时才算
        空骨架。团队填写内容后哈希改变，成果物立刻按真实交付物参与校验——
        否则一份已写完的建模报告会永远被当成空模板，四问覆盖永远判不出来。

        不用文件名判断：真实交付物本来就沿用模板文件名（MODELING_REPORT.md）。
        """

        policy = artifact.get("data_policy") or {}
        if not isinstance(policy, Mapping) or not policy.get("template_id"):
            return False
        template_hash = policy.get("template_hash")
        if not template_hash:
            return True
        return str(artifact.get("content_hash") or "") == str(template_hash)

    def _check_official_filename(self, artifact: Mapping[str, Any], artifact_type: str) -> list[ValidationFinding]:
        """官方结果表文件名规则只对 pack 自己交付的结果表强制。

        模板脚手架是待填写骨架（跳过）；如果项目里还有导入的旧结果表，
        它们不是本次 pack 交付物，只提示 minor，避免把历史文件当成阻塞项。
        """

        if not self.official_files:
            return []
        name = str(artifact.get("name") or artifact_type)
        if name in self.scaffold_filenames:
            return []
        allowed = set(self.official_files.values())
        if name in allowed:
            return []
        policy = artifact.get("data_policy") or {}
        managed = isinstance(policy, Mapping) and policy.get("source") == "competition_pack"
        return [
            ValidationFinding(
                "major" if managed else "minor",
                "official_result_filename_mismatch",
                name,
                f"官方结果表文件名不符合要求，期望之一：{', '.join(sorted(allowed))}",
            )
        ]

    def _markers_for(self, question: int) -> tuple[str, ...]:
        """第 N 问的可接受标记：中文数字与阿拉伯数字写法都算命中。

        模板的 questions_block 由 materializer 渲染为 `### 问题1`（阿拉伯数字），
        而 pack manifest 声明的标记是 `问题一`（中文数字）；人工撰写时两种写法
        都会出现，因此两种都接受，避免把已覆盖的问题判成缺失。
        """

        candidates: list[str] = []
        index = self.expected_questions.index(question) if question in self.expected_questions else -1
        if index >= 0 and index < len(self.question_markers):
            candidates.append(self.question_markers[index])
        candidates.extend(
            (
                f"问题{question}",
                f"问题 {question}",
                f"Q{question}",
                f"Question {question}",
            )
        )
        deduped: list[str] = []
        for value in candidates:
            if value and value not in deduped:
                deduped.append(value)
        return tuple(deduped)

    def _check_json_keys(
        self,
        name: str,
        text: str,
        required_keys: Sequence[str],
        forbidden_keys: Sequence[str],
    ) -> list[ValidationFinding]:
        findings: list[ValidationFinding] = []
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            return [ValidationFinding("major", "artifact_json_invalid", name, "成果物声明为 JSON 但无法解析")]
        for key in required_keys:
            if _lookup(payload, key) is None:
                findings.append(ValidationFinding("major", "required_key_missing", f"{name}#{key}", f"缺少必需字段：{key}"))
        for key in forbidden_keys:
            if _lookup(payload, key) is not None:
                findings.append(ValidationFinding("major", "forbidden_key_present", f"{name}#{key}", f"出现禁止字段：{key}"))
        return findings

    def _check_question_coverage(
        self,
        by_type: Mapping[str, list[Mapping[str, Any]]],
        documents: Mapping[str, str],
        findings: list[ValidationFinding],
    ) -> dict[str, Any]:
        coverage: dict[str, Any] = {"expected_questions": list(self.expected_questions), "per_artifact": {}}
        tracked = ("model_spec", "audit_report", "result_table")
        for artifact_type in tracked:
            items = by_type.get(artifact_type) or []
            covered: set[int] = set()
            readable = 0
            for artifact in items:
                if self._is_scaffold(artifact):
                    # pack 自己下发的模板骨架不是交付内容，不能拿它判断
                    # "这一问有没有写"——否则空模板会把已完成的问题判成缺失。
                    continue
                text = documents.get(str(artifact.get("name") or ""))
                if not text:
                    continue
                readable += 1
                for question in self.expected_questions:
                    if any(marker in text for marker in self._markers_for(question)):
                        covered.add(question)
            coverage["per_artifact"][artifact_type] = sorted(covered)
            # 只有真正读到文本时才判断覆盖：文本缺失说明调用方没提供内容，
            # 不能据此把该问题判成"未覆盖"（那会把可读性问题伪装成内容缺陷）。
            if readable and self.pack.validation_rules().get("question_coverage", {}).get("require_each_question_modeling"):
                for question in self.expected_questions:
                    if question not in covered:
                        markers = "、".join(self._markers_for(question))
                        findings.append(
                            ValidationFinding(
                                "major",
                                "question_coverage_incomplete",
                                f"{artifact_type}#问题{question}",
                                f"未发现第 {question} 问（可接受标记：{markers}）的内容",
                            )
                        )
        coverage["questions_covered"] = sorted(
            {value for values in coverage["per_artifact"].values() for value in values}
        )
        return coverage

    def _check_information_boundary(self, by_type: Mapping[str, list[Mapping[str, Any]]]) -> list[ValidationFinding]:
        findings: list[ValidationFinding] = []
        rules = self.pack.validation_rules().get("information_boundary", {})
        if rules.get("deny_future_data"):
            for items in by_type.values():
                for artifact in items:
                    policy = artifact.get("data_policy") or {}
                    boundary = policy.get("information_boundary") if isinstance(policy, Mapping) else None
                    if isinstance(boundary, Mapping) and boundary.get("future_data") == "allow":
                        findings.append(
                            ValidationFinding("fatal", "future_data_allowed", str(artifact.get("name") or ""), "成果物声明允许使用未来数据")
                        )
        return findings


def _default_markers(questions: Iterable[int]) -> tuple[str, ...]:
    names = {1: "问题一", 2: "问题二", 3: "问题三", 4: "问题四", 5: "问题五"}
    return tuple(names.get(int(value), f"问题{int(value)}") for value in questions)


def _headings(text: str) -> tuple[str, ...]:
    return tuple(match.group(1).strip() for match in SECTION_PATTERN.finditer(text))


def _section_present(section: str, headings: Sequence[str]) -> bool:
    target = re.sub(r"\s+", "", section)
    return any(target in re.sub(r"\s+", "", heading) for heading in headings)


def _count_words(text: str) -> int:
    return len(WORD_PATTERN.findall(text))


def _lookup(payload: Any, dotted: str) -> Any:
    current = payload
    for part in dotted.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    if current in (None, "", [], {}):
        return None
    return current


def _status_for(findings: Sequence[ValidationFinding]) -> str:
    severities = {item.severity for item in findings}
    if "fatal" in severities:
        return "BLOCKED"
    if "major" in severities:
        return "NEEDS_REVISION"
    if "minor" in severities:
        return "PASS_WITH_ASSUMPTIONS"
    return "PASS"


__all__ = [
    "PLATFORM_ARTIFACT_TYPES",
    "PackValidator",
    "ValidationFinding",
    "ValidationReport",
]
