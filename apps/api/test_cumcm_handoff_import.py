"""现有 C 题交接包的可解释导入契约测试。

用合成夹具锁定：布局识别（含带后缀目录名）、可解释分类、无损判定、
未分类文件回退登记、重复导入幂等。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from starlette.requests import Request

from app import main
from app.contracts import ImportRequest
from app.store import Store


def make_request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/x", "headers": []})


class CumcmHandoffImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.store = Store(self.root / "platform.db")
        self.previous_store = main.store
        self.previous_importer_store = main.handoff_importer.store
        main.store = self.store
        main.handoff_importer.store = self.store
        self.project = self.store.create_project(
            main.ProjectCreate(
                name="交接包导入项目",
                competition_pack="cumcm-2026",
                problem_code="C",
                description="handoff import fixture",
            )
        )
        self.package = self._build_package()

    def tearDown(self) -> None:
        main.store = self.previous_store
        main.handoff_importer.store = self.previous_importer_store
        self.store.close()
        self.temp_dir.cleanup()

    def _build_package(self) -> Path:
        package = self.root / "C题四问完整交接包"
        workspace = package / "01_项目工作区"
        (workspace / "code").mkdir(parents=True)
        (workspace / "paper").mkdir(parents=True)
        (workspace / "input").mkdir(parents=True)
        (workspace / "PROBLEM_ANALYSIS.md").write_text("# 赛题分析\n## 子问题拆解\n", encoding="utf-8")
        (workspace / "PROBLEM_FACTS.json").write_text('{"_meta": {"problem_id": "2026-C"}}', encoding="utf-8")
        (workspace / "MODELING_REPORT.md").write_text("# 模型\n## 模型建立\n", encoding="utf-8")
        (workspace / "code" / "q1_solve.py").write_text("print('q1')\n", encoding="utf-8")
        (workspace / "input" / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        (workspace / "paper" / "main.tex").write_text("\documentclass{article}\n", encoding="utf-8")
        (package / "00_复审入口").mkdir()
        (package / "00_复审入口" / "复审清单.md").write_text("# 复审清单\n", encoding="utf-8")
        # 真实包目录名带后缀，用于锁定前缀匹配。
        reference = package / "02_外部参考_其他模型"
        reference.mkdir()
        (reference / "other_model.md").write_text("# 其他模型\n", encoding="utf-8")
        (package / "03_工作流插件").mkdir()
        (package / "03_工作流插件" / "tool.py").write_text("print('tool')\n", encoding="utf-8")
        # 顶层未分类文件：走可解释回退。
        (package / "READ_ME_FIRST.txt").write_text("handoff notes\n", encoding="utf-8")
        return package

    def _import(self, *, dry_run: bool = False):
        return main.import_cumcm_handoff_package(
            self.project.id, ImportRequest(source_path=str(self.package), dry_run=dry_run), make_request()
        )

    def test_dry_run_reports_layout_without_writing(self) -> None:
        report = self._import(dry_run=True)
        self.assertTrue(report.layout_detected)
        self.assertGreater(report.declared_files, 0)
        self.assertEqual(report.imported_artifacts, 0)
        self.assertFalse(report.lossless)
        self.assertEqual(self.store.list_artifacts(self.project.id), [])

    def test_import_is_lossless_and_explainable(self) -> None:
        report = self._import()
        self.assertTrue(report.layout_detected)
        self.assertEqual(report.imported_artifacts, report.declared_files)
        self.assertEqual(report.skipped_artifacts, 0)
        self.assertTrue(report.lossless)
        self.assertEqual(report.hash_mismatches, [])
        # 带后缀的目录被前缀识别，不再算未识别。
        # problem_source = 02_外部参考 的 1 个 + 顶层未分类回退的 1 个。
        self.assertEqual(report.classification.get("problem_source"), 2)
        self.assertEqual(report.classification.get("code"), 2)
        self.assertEqual(report.classification.get("review_report"), 1)
        # 顶层未分类文件仍被登记，并在报告中解释。
        self.assertEqual(report.unknown_files, ["READ_ME_FIRST.txt"])

    def test_imported_artifacts_keep_source_path_and_hash(self) -> None:
        self._import()
        artifacts = {artifact.name: artifact for artifact in self.store.list_artifacts(self.project.id)}
        self.assertIn("01_项目工作区/code/q1_solve.py", artifacts)
        code = artifacts["01_项目工作区/code/q1_solve.py"]
        self.assertEqual(code.data_policy["source_relative_path"], "01_项目工作区/code/q1_solve.py")
        self.assertEqual(code.data_policy["import_source"], "cumcm_handoff")
        content = self.store.get_artifact_content(code.id)
        # 与源文件逐字节一致（Windows 写入带 CRLF，因此直接对比源文件字节）。
        self.assertEqual(content, (self.package / "01_项目工作区" / "code" / "q1_solve.py").read_bytes())

    def test_reimport_is_idempotent(self) -> None:
        first = self._import()
        second = self._import()
        self.assertEqual(second.imported_artifacts, 0)
        self.assertEqual(second.skipped_artifacts, first.imported_artifacts)
        self.assertTrue(second.lossless)

    def test_import_creates_five_stage_tasks_once(self) -> None:
        first = self._import()
        self.assertEqual(first.created_tasks, 5)
        second = self._import()
        self.assertEqual(second.created_tasks, 0)

    def test_imported_project_can_continue_with_pack_validation(self) -> None:
        self._import()
        report = main.validate_project_competition_pack(self.project.id)
        # 导入后可以继续执行：校验给出真实缺口而不是报错。
        self.assertIn(report["status"], {"NEEDS_REVISION", "BLOCKED"})
        self.assertIn("missing_required_artifact", {finding["code"] for finding in report["findings"]})

    def test_missing_source_directory_is_rejected(self) -> None:
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as caught:
            main.import_cumcm_handoff_package(
                self.project.id,
                ImportRequest(source_path=str(self.root / "nope")),
                make_request(),
            )
        self.assertEqual(caught.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
