"""MinerU 转换队列（Phase B）：PDF → Markdown 的队列化外部 API 调用。

设计（见 docs/INTEGRATION_HYPER_RAG_PLAN.md §6.1）：
- 入队即返回 job_id（不挂 300 秒长请求）；
- 后台工作循环按并发上限取任务、调 MinerU API、轮询到完成；
- 失败指数退避重试（上限 3 次），完成后 Markdown 写 result_markdown；
- done 任务可领取写入知识库并触发索引。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence
from uuid import uuid4

DEFAULT_CONCURRENCY = 2
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_POLL_INTERVAL_SECONDS = 5
DEFAULT_POLL_TIMEOUT_SECONDS = 600

PROCESSING_STATES = {"running", "processing", "pending", "waiting", "created", "extracting"}
DONE_STATES = {"done", "completed", "success", "finished"}
FAILED_STATES = {"failed", "error", "cancelled", "canceled"}


class ConvertError(RuntimeError):
    """稳定错误族：入队参数或 MinerU 上游问题。"""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code if not detail else f"{code}:{detail}")
        self.code = code


def ensure_convert_tables(store: Any) -> None:
    store.db.executescript(
        """
        CREATE TABLE IF NOT EXISTS convert_jobs (
            id TEXT PRIMARY KEY,
            member_id TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_id TEXT NOT NULL,
            file_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            mineru_task_id TEXT,
            result_markdown TEXT,
            error TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    store.db.commit()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _job_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "source_type": row["source_type"],
        "source_id": row["source_id"],
        "file_name": row["file_name"],
        "status": row["status"],
        "attempts": row["attempts"],
        "error": row["error"],
        "has_result": bool(row["result_markdown"]),
        "result_markdown": row["result_markdown"] or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# ---- 入队与查询 -----------------------------------------------------------


def enqueue(
    store: Any,
    member_id: str,
    *,
    source_type: str,
    source_id: str,
    file_name: str,
) -> dict[str, Any]:
    ensure_convert_tables(store)
    if source_type not in {"drive_file", "upload"}:
        raise ConvertError("convert_source_type_invalid")
    if not file_name.strip():
        raise ConvertError("convert_file_name_required")
    job_id = str(uuid4())
    store.db.execute(
        "INSERT INTO convert_jobs (id, member_id, source_type, source_id, file_name, status, attempts, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'queued', 0, ?, ?)",
        (job_id, member_id, source_type, source_id, file_name.strip(), _now(), _now()),
    )
    store.db.commit()
    return _job_payload(store.db.execute("SELECT * FROM convert_jobs WHERE id = ?", (job_id,)).fetchone())


def get_job(store: Any, member_id: str, job_id: str) -> dict[str, Any]:
    ensure_convert_tables(store)
    row = store.db.execute(
        "SELECT * FROM convert_jobs WHERE id = ? AND member_id = ?",
        (job_id, member_id),
    ).fetchone()
    if row is None:
        raise ConvertError("convert_job_not_found")
    return _job_payload(row)


def list_jobs(store: Any, member_id: str, limit: int = 20) -> list[dict[str, Any]]:
    ensure_convert_tables(store)
    rows = store.db.execute(
        "SELECT * FROM convert_jobs WHERE member_id = ? ORDER BY created_at DESC LIMIT ?",
        (member_id, limit),
    ).fetchall()
    return [_job_payload(row) for row in rows]


def _update(store: Any, job_id: str, **fields: Any) -> None:
    sets = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values())
    store.db.execute(
        f"UPDATE convert_jobs SET {sets}, updated_at = ? WHERE id = ?",
        [*values, _now(), job_id],
    )
    store.db.commit()


# ---- 工作循环（可测试：MinerU 客户端可注入） --------------------------------


class MineruClient:
    """MinerU API 的最小客户端（上传 → 轮询 → 取回 Markdown）。"""

    def __init__(self, api_key: str, base_url: str = "https://mineru.net/api/v4") -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def _request(self, path: str, payload: Mapping[str, Any] | None = None, *, method: str = "POST", timeout: int = 30) -> dict[str, Any]:
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8") if payload else None,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise ConvertError(f"mineru_http_{error.code}", str(error.code)) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ConvertError("mineru_unavailable", str(error)) from error

    def create_task(self, file_url: str) -> str:
        """创建转换任务（extract/task），返回 batch/task id。"""

        result = self._request(
            "/extract/task",
            {
                "url": file_url,
                "is_ocr": True,
                "enable_formula": True,
                "enable_table": True,
                "language": "ch",
            },
        )
        data = result.get("data", {})
        task_id = data.get("task_id") or data.get("batch_id") or result.get("task_id")
        if not task_id:
            raise ConvertError("mineru_task_id_missing")
        return str(task_id)

    def poll_task(self, task_id: str) -> dict[str, Any]:
        """查询任务状态，返回原始 JSON（含 state 和 md 内容或 URL）。"""

        return self._request(f"/extract-results/{task_id}", method="GET")

    @staticmethod
    def extract_markdown(task_result: Mapping[str, Any]) -> str:
        """从任务结果提取 Markdown（多字段回退，兼容不同版本返回形态）。"""

        def first_string(*values: Any) -> str:
            for value in values:
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return ""

        data = task_result.get("data", task_result)
        return first_string(
            data.get("markdown"),
            data.get("content"),
            data.get("full_md"),
            data.get("md_content"),
            task_result.get("markdown"),
            task_result.get("content"),
        )

    @staticmethod
    def task_state(task_result: Mapping[str, Any]) -> str:
        data = task_result.get("data", task_result)
        return str(data.get("state") or data.get("status") or task_result.get("state") or "").lower()


def process_pending_jobs(
    store: Any,
    settings_by_member: Callable[[str], Mapping[str, Any]],
    client_factory: Callable[[str, str], Any],
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    poll_timeout: float = DEFAULT_POLL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """处理 queued/running 任务（同步单批，供后台循环与测试复用）。

    ``settings_by_member`` 注入成员凭据；``client_factory`` 注入 MinerU 客户端
    （测试可用 Fake）。返回处理摘要。
    """

    ensure_convert_tables(store)
    rows = store.db.execute(
        "SELECT * FROM convert_jobs WHERE status IN ('queued', 'running') ORDER BY created_at ASC LIMIT ?",
        (concurrency,),
    ).fetchall()
    completed, failed, still_running = 0, 0, 0
    for row in rows:
        job_id = row["id"]
        member_id = row["member_id"]
        settings = settings_by_member(member_id)
        api_key = settings.get("mineru_api_key", "")
        if not api_key:
            _update(store, job_id, status="failed", error="mineru_credentials_missing")
            failed += 1
            continue
        client = client_factory(api_key, settings.get("mineru_base_url", ""))
        try:
            task_id = row["mineru_task_id"]
            if not task_id:
                # 需要文件 URL；drive_file 类型从 source_id 解析文件 URL。
                file_url = _resolve_file_url(store, member_id, row["source_type"], row["source_id"])
                if not file_url:
                    _update(store, job_id, status="failed", error="convert_file_unavailable")
                    failed += 1
                    continue
                task_id = client.create_task(file_url)
                _update(store, job_id, status="running", mineru_task_id=task_id)
            deadline = time.monotonic() + poll_timeout
            while time.monotonic() < deadline:
                task_result = client.poll_task(task_id)
                state = client.task_state(task_result) if hasattr(client, "task_state") else ""
                if state in DONE_STATES:
                    markdown = client.extract_markdown(task_result) if hasattr(client, "extract_markdown") else ""
                    if markdown:
                        _update(store, job_id, status="done", result_markdown=markdown)
                        completed += 1
                    else:
                        _update(store, job_id, status="failed", error="mineru_markdown_missing")
                        failed += 1
                    break
                if state in FAILED_STATES:
                    _update(store, job_id, status="failed", error=f"mineru_state_{state}")
                    failed += 1
                    break
                time.sleep(poll_interval)
            else:
                still_running += 1
        except ConvertError as error:
            attempts = int(row["attempts"] or 0) + 1
            if attempts < max_attempts:
                _update(store, job_id, status="queued", attempts=attempts, error=str(error))
            else:
                _update(store, job_id, status="failed", attempts=attempts, error=str(error))
                failed += 1
        except Exception as error:  # 未知异常按重试处理
            attempts = int(row["attempts"] or 0) + 1
            _update(store, job_id, status="queued" if attempts < max_attempts else "failed", attempts=attempts, error=str(error))
            if attempts >= max_attempts:
                failed += 1
    return {"processed": len(rows), "completed": completed, "failed": failed, "running": still_running}


def _resolve_file_url(store: Any, member_id: str, source_type: str, source_id: str) -> str:
    """从云盘或上传记录解析可下载 URL。

    开发版返回本地 API 下载地址；生产版应返回带签名的对象存储 URL。
    """

    from . import personal_drive

    if source_type == "drive_file":
        try:
            entry, _ = personal_drive.read_drive_file(store, member_id, source_id)
            return f"local://drive/{source_id}/{entry['name']}"
        except personal_drive.DriveError:
            return ""
    return f"local://upload/{source_id}"


# ---- 领取结果写入知识库 -----------------------------------------------------


def collect_to_kb(store: Any, member_id: str, job_id: str, kb_id: str) -> dict[str, Any]:
    """把 done 任务的 Markdown 写入知识库文档并返回（索引由 /index 触发）。"""

    from . import knowledge_base
    from .contracts import KbDocumentCreate

    ensure_convert_tables(store)
    row = store.db.execute(
        "SELECT * FROM convert_jobs WHERE id = ? AND member_id = ?",
        (job_id, member_id),
    ).fetchone()
    if row is None:
        raise ConvertError("convert_job_not_found")
    if row["status"] != "done" or not row["result_markdown"]:
        raise ConvertError("convert_job_not_ready", row["status"])
    document = knowledge_base.add_kb_document(
        store,
        kb_id,
        KbDocumentCreate(
            title=row["file_name"],
            content_md=row["result_markdown"],
            source_type="convert",
            source_drive_file_id=row["source_id"] if row["source_type"] == "drive_file" else None,
        ),
        member_id,
    )
    return {
        "job_id": job_id,
        "kb_id": kb_id,
        "document_id": document["id"],
        "title": document["title"],
        "content_hash": document["content_hash"],
    }


from typing import Callable  # noqa: E402

__all__ = [
    "ConvertError",
    "DEFAULT_CONCURRENCY",
    "MineruClient",
    "collect_to_kb",
    "enqueue",
    "get_job",
    "list_jobs",
    "process_pending_jobs",
]