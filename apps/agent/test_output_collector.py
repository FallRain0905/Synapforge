"""产出采集器的契约测试（CL-1，决策 D-CL-1/3/8/9）。

重点不是"能上传"，而是**边界**：
- 产出一律进「待审」，绝不自动批准（D-CL-1）；
- 回答写成文件并标成 `agent_answer`（D-CL-3），可用 `resource_policy` 关掉；
- 超限文件只登记不上传（D-CL-9）；
- 上传失败**不抛**、不改任务成败，只记录（D-CL-8、I4）；
- 扫描被剪枝：依赖目录不算产出。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from apps.agent.output_collector import CollectorConfig, OutputCollector
from apps.agent.result_uploader import UploadResult


class _FakeState:
    """内存版上传队列（`ResultUploader` 只需要这几个方法）。"""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def save_upload(self, local_path, **kwargs):
        upload_id = kwargs.get("upload_id") or f"upload-{len(self.rows)}"
        self.rows[upload_id] = {"upload_id": upload_id, "local_path": str(local_path), "status": "PENDING", **kwargs}
        return upload_id

    def list_uploads(self, statuses=None):
        items = list(self.rows.values())
        if statuses:
            items = [item for item in items if item.get("status") in set(statuses)]
        return items

    def mark_upload_attempt(self, upload_id):
        self.rows[upload_id]["attempts"] = self.rows[upload_id].get("attempts", 0) + 1

    def attach_upload_artifact(self, upload_id, artifact_id):
        self.rows[upload_id]["artifact_id"] = artifact_id

    def complete_upload(self, upload_id, artifact_id):
        self.rows[upload_id].update({"status": "SUCCEEDED", "artifact_id": artifact_id})

    def fail_upload(self, upload_id, error):
        self.rows[upload_id].update({"status": "FAILED", "last_error": error})


class _FakeClient:
    def __init__(self, *, fail: str | None = None) -> None:
        self.created: list[dict] = []
        self.uploaded: list[str] = []
        self.fail = fail
        self.next_id = 1

    def create(self, project_id, output, *, task_id=None, run_id=None, status="DRAFT"):
        if self.fail == "create":
            raise RuntimeError("artifact_http_503")
        artifact_id = f"artifact-{self.next_id:03d}"
        self.next_id += 1
        self.created.append(
            {"id": artifact_id, "name": output.name, "type": output.artifact_type, "status": status, "run_id": run_id}
        )
        return {"id": artifact_id}

    def upload_content(self, artifact_id, output):
        if self.fail == "upload":
            raise RuntimeError("artifact_upload_failed")
        self.uploaded.append(artifact_id)
        return {"id": artifact_id}


class _FakeUploader:
    """直接实现 ResultUploader 的两个方法，便于断言与制造失败。

    产出按**实例**记录（`queued_outputs`）：真实 uploader 是从本地队列行反查文件，
    用类级变量模拟会在"没走 _run 的用例"里悄悄返回空列表，把失败当成没产出。
    """

    def __init__(self, client: _FakeClient, state: _FakeState) -> None:
        self.client = client
        self.state = state
        self.queued: list[str] = []
        self.queued_outputs: dict[str, object] = {}

    def queue_outputs(self, project_id, run_id, request, outputs):
        for output in outputs:
            upload_id = f"output:{run_id}:{output.relative_path}"
            self.queued.append(output.relative_path)
            self.queued_outputs[output.relative_path] = output
            self.state.save_upload(
                output.path,
                upload_id=upload_id,
                content_hash=output.content_hash,
                status="PENDING",
                project_id=project_id,
                task_id=request.task_id,
                run_id=run_id,
                mime_type=output.mime_type,
                size_bytes=output.size_bytes,
                relative_path=output.relative_path,
                artifact_type=output.artifact_type,
            )
        return self.queued

    def upload_pending(self, *, task_id, run_id):
        results = []
        for item in self.state.list_uploads(["PENDING", "FAILED", "UPLOADING"]):
            if item.get("run_id") != run_id:
                continue
            output = self.queued_outputs.get(item["relative_path"])
            if output is None:
                continue
            artifact = self.client.create(
                item["project_id"], output, task_id=task_id, run_id=run_id, status="PENDING_REVIEW"
            )
            self.state.attach_upload_artifact(item["upload_id"], artifact["id"])
            self.client.upload_content(artifact["id"], output)
            self.state.complete_upload(item["upload_id"], artifact["id"])
            results.append(UploadResult(item["upload_id"], artifact["id"], output.path))
        return results


class CollectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.workspace = Path(self.temp.name)
        (self.workspace / "notes").mkdir()
        self.client = _FakeClient()
        self.state = _FakeState()
        self.uploader = _FakeUploader(self.client, self.state)
        self.collector = OutputCollector(
            CollectorConfig(
                workspace=self.workspace,
                url="http://127.0.0.1:8010",
                project_id="project-1",
                agent_id="agent-1",
                project_token="prj_token_value_1234567890",
            ),
            uploader=self.uploader,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def _run(self, summary: str = "回答正文\n\n---\ncodex exit=0", task: dict | None = None) -> object:
        task = task or {"id": "task-1", "title": "写个文件"}
        await self.collector.before(task)
        (self.workspace / "notes" / "result.md").write_text("# 结果\n", encoding="utf-8")
        return await self.collector.after(task, "run-1", True, summary)

    async def test_creates_pending_review_artifacts_for_changed_files(self) -> None:
        result = await self._run()
        self.assertEqual(len(result.artifact_ids), 2)  # 产出文件 + 回答
        statuses = {item["status"] for item in self.client.created}
        self.assertEqual(statuses, {"PENDING_REVIEW"}, "产出必须进待审，不能自动批准（D-CL-1）")
        types = {item["type"] for item in self.client.created}
        self.assertIn("agent_answer", types, "回答要作为内容入库（D-CL-3）")
        self.assertIn("paper_source", types, "notes/result.md 是 Markdown，按可交付文档归类")
        self.assertEqual({item["run_id"] for item in self.client.created}, {"run-1"})
        self.assertIn("产出 2 个成果物（待审）", result.note())

    async def test_answer_artifact_can_be_disabled_per_task(self) -> None:
        result = await self._run(task={"id": "task-1", "resource_policy": {"inline_answer_artifact": False}})
        self.assertEqual(len(result.artifact_ids), 1)
        self.assertNotIn("agent_answer", {item["type"] for item in self.client.created})

    async def test_file_collection_can_be_disabled_per_task(self) -> None:
        result = await self._run(task={"id": "task-1", "resource_policy": {"collect_outputs": False}})
        self.assertEqual([item["type"] for item in self.client.created], ["agent_answer"], result.note())

    async def test_dependency_directories_never_become_artifacts(self) -> None:
        await self.collector.before({"id": "task-1"})
        (self.workspace / "node_modules" / "pkg").mkdir(parents=True)
        (self.workspace / "node_modules" / "pkg" / "index.js").write_text("x", encoding="utf-8")
        (self.workspace / ".git").mkdir()
        (self.workspace / ".git" / "HEAD").write_text("ref", encoding="utf-8")
        (self.workspace / "figures").mkdir()
        (self.workspace / "figures" / "plot.png").write_bytes(b"\x89PNG")
        result = await self.collector.after({"id": "task-1"}, "run-9", True, "回答\n\n---\ncodex exit=0")
        self.assertEqual(sorted(self.uploader.queued), [".math-agent-platform/answers/run-9.md", "figures/plot.png"])

    async def test_oversized_files_are_recorded_but_not_uploaded(self) -> None:
        collector = OutputCollector(
            CollectorConfig(
                workspace=self.workspace,
                url="http://127.0.0.1:8010",
                project_id="project-1",
                agent_id="agent-1",
                project_token="prj_token_value_1234567890",
                max_upload_bytes=4,
            ),
            uploader=self.uploader,
        )
        await collector.before({"id": "task-1"})
        (self.workspace / "notes" / "big.md").write_text("1234567890", encoding="utf-8")
        result = await collector.after({"id": "task-1"}, "run-1", True, "回答\n\n---\ncodex exit=0")
        self.assertEqual(result.skipped_too_large, ["notes/big.md"])
        self.assertIn("超过上传上限", result.note())
        self.assertNotIn("notes/big.md", self.uploader.queued)

    async def test_upload_failure_is_recorded_and_never_raises(self) -> None:
        failing = OutputCollector(
            CollectorConfig(
                workspace=self.workspace,
                url="http://127.0.0.1:8010",
                project_id="project-1",
                agent_id="agent-1",
                project_token="prj_token_value_1234567890",
            ),
            uploader=_FakeUploader(_FakeClient(fail="create"), _FakeState()),
        )
        await failing.before({"id": "task-1"})
        (self.workspace / "notes" / "x.md").write_text("x", encoding="utf-8")
        result = await failing.after({"id": "task-1"}, "run-1", True, "回答\n\n---\ncodex exit=0")
        self.assertEqual(result.artifact_ids, [])
        self.assertTrue(result.errors)
        self.assertIn("上传失败", result.note())

    async def test_no_run_id_means_no_collection(self) -> None:
        result = await self.collector.after({"id": "task-1"}, None, True, "回答")
        self.assertEqual(result.artifact_ids, [])
        self.assertEqual(self.client.created, [])

    async def test_diagnostics_only_summary_is_not_an_answer(self) -> None:
        """声明式命令的摘要只有诊断（`exit=… | stdout: …`），不能当"回答"入库。"""

        result = await self._run(summary="exit=1 run=run-1 | stderr: boom")
        self.assertEqual([item["type"] for item in self.client.created], ["paper_source"])
        self.assertNotIn("Agent 回答", [item["name"] for item in self.client.created])


    async def test_placeholder_answer_is_not_content(self) -> None:
        """失败且没有回复时，摘要里的占位句不能变成"回答成果物"。"""

        result = await self._run(summary="（本次执行没有产出回复文本）\n\n---\ncodex exit=1 | errors: model_capacity")
        self.assertEqual([item["type"] for item in self.client.created], ["paper_source"], result.note())
        self.assertEqual(result.artifact_ids, self.client.created and [item["id"] for item in self.client.created])


if __name__ == "__main__":
    unittest.main()