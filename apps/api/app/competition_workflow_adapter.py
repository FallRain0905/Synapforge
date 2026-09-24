"""现有 competition-workflow 插件适配器（阶段 6 工作包）。

把外部竞赛工作流插件（skills/templates/scripts）映射到平台 pack 的
模板、阶段与校验入口，输出可供接收方执行的适配清单：

- 每个插件技能 → 对应的 pack 阶段与产出成果物类型；
- 每个插件模板 → 与 pack 模板的对应关系（可直接替换/作为参考）；
- 插件内置检查脚本 → 平台可调用的审计入口（例如能力清单校验）；
- 未识别的资产单列，绝不静默丢弃。

只读取插件，不修改插件；输出 JSON 适配清单供平台侧登记使用。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

SKILL_STAGE_MAP: dict[str, dict[str, Any]] = {
    "comp-prob-analysis": {"stage": "problem_analysis", "produces": ["problem_analysis", "problem_facts"]},
    "comp-modeling": {"stage": "modeling", "produces": ["model_spec"]},
    "comp-code": {"stage": "coding", "produces": ["code", "result_table"]},
    "comp-review": {"stage": "review", "produces": ["review_report", "audit_report"]},
    "comp-paper-zh": {"stage": "paper", "produces": ["paper_source"]},
    "comp-paper-en": {"stage": "paper", "produces": ["paper_source"]},
    "comp-paper-zh-docx": {"stage": "paper", "produces": ["paper_source", "compiled_pdf"]},
    "comp-paper-en-docx": {"stage": "paper", "produces": ["paper_source", "compiled_pdf"]},
    "comp-compile-zh": {"stage": "delivery", "produces": ["compiled_pdf", "submission_bundle"]},
    "comp-compile-en": {"stage": "delivery", "produces": ["compiled_pdf", "submission_bundle"]},
    "comp-stats-topic": {"stage": "problem_analysis", "produces": ["problem_analysis"]},
}

# 插件模板 → pack 模板 id 的对照（同名或语义等价）。
TEMPLATE_MAP: dict[str, str] = {
    "EXPERIMENT_PLAN_TEMPLATE.md": "experiment_plan",
    "EXPERIMENT_LOG_TEMPLATE.md": "experiment_plan",
    "PAPER_PLAN_TEMPLATE.md": "paper_outline",
    "NARRATIVE_REPORT_TEMPLATE.md": "paper_outline",
    "RESEARCH_BRIEF_TEMPLATE.md": "problem_analysis",
    "RESEARCH_CONTRACT_TEMPLATE.md": "problem_analysis",
    "FINDINGS_TEMPLATE.md": "audit_report",
    "IDEA_CANDIDATES_TEMPLATE.md": "model_spec",
}

# 插件检查脚本 → 平台审计入口。
CHECK_SCRIPT_MAP: dict[str, str] = {
    "capability_check.py": "pack:capability_checklist",
    "capability_audit.py": "pack:capability_checklist",
    "bib_authenticity_check.py": "pack:paper_source.references",
    "ai_disclosure_rules.md": "pack:paper_outline.compliance",
}


class CompetitionWorkflowAdapterError(RuntimeError):
    """稳定错误族：插件目录不可用。"""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code if not detail else f"{code}:{detail}")
        self.code = code


@dataclass
class AdapterReport:
    plugin_root: str
    pack_id: str
    skills: list[dict[str, Any]] = field(default_factory=list)
    templates: list[dict[str, Any]] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)

    @property
    def mapped_skill_count(self) -> int:
        return sum(1 for item in self.skills if item.get("stage"))

    def as_dict(self) -> dict[str, Any]:
        return {
            "plugin_root": self.plugin_root,
            "pack_id": self.pack_id,
            "skill_count": len(self.skills),
            "mapped_skill_count": self.mapped_skill_count,
            "template_count": len(self.templates),
            "check_count": len(self.checks),
            "unmapped": list(self.unmapped),
            "skills": self.skills,
            "templates": self.templates,
            "checks": self.checks,
        }


def adapt_competition_workflow(plugin_root: str | Path, *, pack_id: str = "cumcm") -> AdapterReport:
    """扫描插件并输出适配清单；缺目录时 fail-closed。"""

    root = Path(plugin_root).expanduser().resolve()
    if not root.is_dir():
        raise CompetitionWorkflowAdapterError("competition_workflow_plugin_missing", str(root))

    report = AdapterReport(plugin_root=str(root), pack_id=pack_id)

    skills_dir = root / "skills"
    if skills_dir.is_dir():
        for entry in sorted(skills_dir.iterdir()):
            if not entry.is_dir():
                continue
            mapping = SKILL_STAGE_MAP.get(entry.name)
            report.skills.append(
                {
                    "skill": entry.name,
                    "has_skill_md": (entry / "SKILL.md").is_file(),
                    "stage": mapping["stage"] if mapping else None,
                    "produces": list(mapping["produces"]) if mapping else [],
                }
            )
            if mapping is None:
                report.unmapped.append(f"skills/{entry.name}")

    templates_dir = root / "templates"
    if templates_dir.is_dir():
        for path in sorted(templates_dir.glob("*.md")):
            target = TEMPLATE_MAP.get(path.name)
            report.templates.append(
                {
                    "source": f"templates/{path.name}",
                    "pack_template_id": target,
                    "relationship": "equivalent" if target else None,
                }
            )
            if target is None:
                report.unmapped.append(f"templates/{path.name}")

    tools_dir = root / "tools"
    for name, target in CHECK_SCRIPT_MAP.items():
        candidates = list(root.rglob(name))
        if not candidates:
            continue
        report.checks.append(
            {
                "source": str(candidates[0].relative_to(root)).replace("\\", "/"),
                "platform_entry": target,
            }
        )

    if not report.skills and not report.templates:
        raise CompetitionWorkflowAdapterError("competition_workflow_assets_missing", str(root))
    return report


def write_adapter_report(report: AdapterReport, destination: str | Path) -> Path:
    path = Path(destination).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


__all__ = [
    "AdapterReport",
    "CHECK_SCRIPT_MAP",
    "CompetitionWorkflowAdapterError",
    "SKILL_STAGE_MAP",
    "TEMPLATE_MAP",
    "adapt_competition_workflow",
    "write_adapter_report",
]