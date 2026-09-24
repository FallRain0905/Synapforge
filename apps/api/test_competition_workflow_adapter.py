"""competition-workflow 脚本适配契约测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.competition_workflow_adapter import (
    CHECK_SCRIPT_MAP,
    CompetitionWorkflowAdapterError,
    adapt_competition_workflow,
    write_adapter_report,
)


class CompetitionWorkflowAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.plugin = Path(self.temp_dir.name) / "competition-workflow"
        (self.plugin / "skills" / "comp-modeling").mkdir(parents=True)
        (self.plugin / "skills" / "comp-modeling" / "SKILL.md").write_text("# modeling\n", encoding="utf-8")
        (self.plugin / "skills" / "comp-prob-analysis").mkdir(parents=True)
        (self.plugin / "skills" / "comp-prob-analysis" / "SKILL.md").write_text("# analysis\n", encoding="utf-8")
        (self.plugin / "skills" / "feishu-notify").mkdir(parents=True)
        (self.plugin / "skills" / "feishu-notify" / "SKILL.md").write_text("# notify\n", encoding="utf-8")
        (self.plugin / "templates").mkdir()
        (self.plugin / "templates" / "PAPER_PLAN_TEMPLATE.md").write_text("# paper plan\n", encoding="utf-8")
        (self.plugin / "templates" / "README.md").write_text("# readme\n", encoding="utf-8")
        (self.plugin / "scripts").mkdir()
        (self.plugin / "scripts" / "capability_check.py").write_text("print('cap')\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_maps_known_skills_templates_and_checks(self) -> None:
        report = adapt_competition_workflow(self.plugin)
        skills = {item["skill"]: item for item in report.skills}
        self.assertEqual(skills["comp-modeling"]["stage"], "modeling")
        self.assertEqual(skills["comp-modeling"]["produces"], ["model_spec"])
        self.assertEqual(skills["comp-prob-analysis"]["stage"], "problem_analysis")
        self.assertIsNone(skills["feishu-notify"]["stage"])

        templates = {item["source"]: item for item in report.templates}
        self.assertEqual(templates["templates/PAPER_PLAN_TEMPLATE.md"]["pack_template_id"], "paper_outline")
        self.assertEqual(templates["templates/PAPER_PLAN_TEMPLATE.md"]["relationship"], "equivalent")
        self.assertIsNone(templates["templates/README.md"]["pack_template_id"])

        checks = {item["source"]: item for item in report.checks}
        self.assertEqual(checks["scripts/capability_check.py"]["platform_entry"], "pack:capability_checklist")

        # 未识别资产显式列出，不静默丢弃。
        self.assertIn("skills/feishu-notify", report.unmapped)
        self.assertIn("templates/README.md", report.unmapped)

    def test_report_summary_counts(self) -> None:
        report = adapt_competition_workflow(self.plugin)
        payload = report.as_dict()
        self.assertEqual(payload["skill_count"], 3)
        self.assertEqual(payload["mapped_skill_count"], 2)
        self.assertEqual(payload["template_count"], 2)
        self.assertEqual(payload["check_count"], 1)
        self.assertEqual(payload["pack_id"], "cumcm")

    def test_missing_plugin_directory_fails_closed(self) -> None:
        with self.assertRaises(CompetitionWorkflowAdapterError) as caught:
            adapt_competition_workflow(Path(self.temp_dir.name) / "nope")
        self.assertEqual(caught.exception.code, "competition_workflow_plugin_missing")

    def test_empty_plugin_fails_closed(self) -> None:
        empty = Path(self.temp_dir.name) / "empty-plugin"
        empty.mkdir()
        with self.assertRaises(CompetitionWorkflowAdapterError) as caught:
            adapt_competition_workflow(empty)
        self.assertEqual(caught.exception.code, "competition_workflow_assets_missing")

    def test_write_report_persists_json(self) -> None:
        report = adapt_competition_workflow(self.plugin)
        destination = Path(self.temp_dir.name) / "out" / "adapter.json"
        path = write_adapter_report(report, destination)
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["pack_id"], "cumcm")
        self.assertEqual(len(payload["skills"]), 3)
        self.assertIn("checks", payload)

    def test_check_script_map_covers_capability_and_disclosure(self) -> None:
        # 适配表本身要覆盖能力清单与 AI 使用披露两类审计入口。
        self.assertEqual(CHECK_SCRIPT_MAP["capability_check.py"], "pack:capability_checklist")
        self.assertIn("ai_disclosure_rules.md", CHECK_SCRIPT_MAP)


if __name__ == "__main__":
    unittest.main()