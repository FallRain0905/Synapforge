"""MinerU 转换队列契约测试：入队/查询/工作循环/写入知识库。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException
from starlette.requests import Request

from app import convert_queue, knowledge_base, main
from app.contracts import ConvertEnqueueRequest, ConvertToKbRequest, KbCreate
from app.store import Store


def make_request(member: str = "member-001") -> Request:
    return Request({"type": "http", "method": "POST", "path": "/api/convert", "headers": []})


class FakeMineruClient:
    """可控的 MinerU 客户端替身。"""

    def __init__(self, states: list[str], markdown: str = "# 转换结果\n\n这是内容。") -> None:
        self.states = states
        self.markdown = markdown
        self.poll_count = 0
        self.created_tasks: list[str] = []

    def create_task(self, file_url: str) -> str:
        task_id = f"fake-task-{len(self.created_tasks) + 1}"
        self.created_tasks.append(task_id)
        return task_id

    def poll_task(self, task_id: str) -> dict:
        state = self.states[min(self.poll_count, len(self.states) - 1)]
        self.poll_count += 1
        return {"data": {"state": state, "markdown": self.markdown if state in {"done", "completed", "success"} else ""}}

    def task_state(self, task_result: dict) -> str:
        return str(task_result.get("data", {}).get("state", "")).lower()

    @staticmethod
    def extract_markdown(task_result: dict) -> str:
        return str(task_result.get("data", {}).get("markdown", ""))


class ConvertQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store

    def tearDown(self) -> None:
        main.store = self.previous_store
        self.store.close()
        self.temp_dir.cleanup()

    def _enqueue(self, name: str = "论文.pdf", source_type: str = "upload") -> dict:
        return main.enqueue_convert_job(
            ConvertEnqueueRequest(source_type=source_type, source_id="src-001", file_name=name),
            make_request(),
        )

    def test_enqueue_returns_queued_immediately(self) -> None:
        job = self._enqueue()
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["attempts"], 0)
        self.assertFalse(job["has_result"])

        listing = main.list_convert_jobs(make_request())
        self.assertEqual(len(listing), 1)

    def test_get_job_and_not_found(self) -> None:
        job = self._enqueue()
        fetched = main.get_convert_job(job["id"], make_request())
        self.assertEqual(fetched["id"], job["id"])

        with self.assertRaises(HTTPException) as caught:
            main.get_convert_job("no-such-job", make_request())
        self.assertEqual(caught.exception.status_code, 404)

    def test_worker_completes_job_and_writes_markdown(self) -> None:
        job = self._enqueue()
        client = FakeMineruClient(states=["pending", "running", "done"])

        def settings_for(member: str) -> dict:
            return {"mineru_api_key": "test-key"}

        summary = convert_queue.process_pending_jobs(
            self.store, settings_for, lambda key, base: client, poll_interval=0.01,
        )
        self.assertEqual(summary["completed"], 1)

        done = main.get_convert_job(job["id"], make_request())
        self.assertEqual(done["status"], "done")
        self.assertTrue(done["has_result"])
        self.assertIn("转换结果", done["result_markdown"])

    def test_worker_marks_failed_on_error_state(self) -> None:
        job = self._enqueue()
        client = FakeMineruClient(states=["running", "failed"])

        summary = convert_queue.process_pending_jobs(
            self.store, lambda m: {"mineru_api_key": "k"}, lambda key, base: client, poll_interval=0.01,
        )
        self.assertEqual(summary["failed"], 1)
        failed = main.get_convert_job(job["id"], make_request())
        self.assertEqual(failed["status"], "failed")
        self.assertIn("mineru_state_failed", failed["error"])

    def test_worker_marks_failed_on_missing_credentials(self) -> None:
        self._enqueue()
        summary = convert_queue.process_pending_jobs(
            self.store, lambda m: {"mineru_api_key": ""}, lambda key, base: None,
        )
        self.assertEqual(summary["failed"], 1)

    def test_worker_retries_on_convert_error(self) -> None:
        job = self._enqueue()

        class FlakyClient:
            calls = 0

            def create_task(self, file_url: str) -> str:
                FlakyClient.calls += 1
                if FlakyClient.calls == 1:
                    raise convert_queue.ConvertError("mineru_unavailable")
                return "task-ok"

            def poll_task(self, task_id: str) -> dict:
                return {"data": {"state": "done", "markdown": "# 重试成功"}}

            def task_state(self, result: dict) -> str:
                return "done"

            @staticmethod
            def extract_markdown(result: dict) -> str:
                return "# 重试成功"

        client = FlakyClient()
        # 第一次失败 → 回 queued（attempts=1）
        convert_queue.process_pending_jobs(self.store, lambda m: {"mineru_api_key": "k"}, lambda key, base: client, poll_interval=0.01)
        retry = main.get_convert_job(job["id"], make_request())
        self.assertEqual(retry["status"], "queued")
        self.assertEqual(retry["attempts"], 1)
        # 第二次成功
        convert_queue.process_pending_jobs(self.store, lambda m: {"mineru_api_key": "k"}, lambda key, base: client, poll_interval=0.01)
        done = main.get_convert_job(job["id"], make_request())
        self.assertEqual(done["status"], "done")

    def test_collect_to_kb_creates_document(self) -> None:
        job = self._enqueue()
        # 直接把任务标记为 done（模拟 MinerU 已完成）
        convert_queue.ensure_convert_tables(self.store)
        convert_queue._update(self.store, job["id"], status="done", result_markdown="# PDF 转的 Markdown\n内容。")
        kb = knowledge_base.create_kb(self.store, KbCreate(name="收集库"), "member-001")

        result = main.collect_convert_to_kb(job["id"], ConvertToKbRequest(kb_id=kb["id"]), make_request())
        self.assertEqual(result["title"], "论文.pdf")
        self.assertTrue(result["content_hash"])

        docs = knowledge_base.list_kb_documents(self.store, kb["id"], "member-001", include_content=True)
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["source_type"], "convert")
        self.assertIn("PDF 转的", docs[0]["content_md"])

    def test_collect_not_ready_is_rejected(self) -> None:
        job = self._enqueue()
        kb = knowledge_base.create_kb(self.store, KbCreate(name="收集库 2"), "member-001")
        with self.assertRaises(HTTPException) as caught:
            main.collect_convert_to_kb(job["id"], ConvertToKbRequest(kb_id=kb["id"]), make_request())
        self.assertEqual(caught.exception.status_code, 400)

    def test_queue_concurrency_limit(self) -> None:
        for index in range(4):
            self._enqueue(name=f"文档{index}.pdf")
        client = FakeMineruClient(states=["done"])
        summary = convert_queue.process_pending_jobs(
            self.store, lambda m: {"mineru_api_key": "k"}, lambda key, base: client,
            concurrency=2, poll_interval=0.01,
        )
        # 只处理前 2 个（并发上限）
        self.assertEqual(summary["processed"], 2)


if __name__ == "__main__":
    unittest.main()