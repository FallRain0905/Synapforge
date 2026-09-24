"""知识库 Gateway：平台对 hyper-rag-service 的 HTTP 代理（服务独立部署）。

服务地址由 ``HYPERRAG_SERVICE_URL`` 或默认 ``http://127.0.0.1:8100`` 决定，
凭据随请求传入（调用方持有模型凭据，服务无状态——沿用 question-bank 的协议）。

代理层职责：
- 鉴权与 KB 归属校验（在路由层完成，这里只转发）；
- 服务不可达时返回稳定错误码 ``hyper_rag_unavailable``，不伪装成功；
- 索引结果与文档哈希映射回写（``mark_index_status``）；
- 检索结果的 ``text_units`` 溯源：用平台的 hash→doc 映射替代 MD5 事后反查。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Mapping
from urllib.parse import quote

from . import knowledge_base

DEFAULT_SERVICE_URL = "http://127.0.0.1:8100"
HEALTH_TIMEOUT = 5
QUERY_TIMEOUT = 120
INDEX_TIMEOUT = 300


class HyperRagError(RuntimeError):
    """稳定错误族：服务不可达或上游失败。"""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code if not detail else f"{code}:{detail}")
        self.code = code


def service_url() -> str:
    return os.environ.get("HYPERRAG_SERVICE_URL", DEFAULT_SERVICE_URL).rstrip("/")


def _post(path: str, payload: Mapping[str, Any], timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(
        service_url() + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read().decode("utf-8")).get("detail", str(error.code))
        except Exception:
            detail = str(error.code)
        raise HyperRagError(f"hyper_rag_upstream_{error.code}", str(detail)) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise HyperRagError("hyper_rag_unavailable", str(error)) from error


def _get(path: str, timeout: int = 15) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(service_url() + path, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read().decode("utf-8")).get("detail", str(error.code))
        except Exception:
            detail = str(error.code)
        raise HyperRagError(f"hyper_rag_upstream_{error.code}", str(detail)) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise HyperRagError("hyper_rag_unavailable", str(error)) from error


def _config_payload(settings: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "llm": {
            "api_key": settings.get("llm_api_key", ""),
            "model_name": settings.get("llm_model", ""),
            "base_url": settings.get("llm_base_url", ""),
        },
        "embedding": {
            "api_key": settings.get("embedding_api_key", ""),
            "model_name": settings.get("embedding_model", ""),
            "base_url": settings.get("embedding_base_url", ""),
            "dimensions": settings.get("embedding_dimensions", 1024),
        },
    }


def health() -> dict[str, Any]:
    return _get("/health", HEALTH_TIMEOUT)


def kb_status(kb_id: str) -> dict[str, Any]:
    return _get(f"/api/status/{quote(kb_id)}")


def query(kb_id: str, question: str, mode: str, settings: Mapping[str, Any]) -> dict[str, Any]:
    payload = {
        "kb_id": kb_id,
        "user_id": settings.get("member_id", ""),
        "question": question,
        "mode": mode,
        "config": _config_payload(settings),
    }
    return _post("/api/query", payload, QUERY_TIMEOUT)


def index_documents(
    store: Any,
    kb_id: str,
    member_id: str,
    doc_ids: list[str],
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    """把知识库文档送入 hyper-rag-service 索引，并回写文档索引状态。"""

    documents_payload = []
    by_id = {}
    for doc_id in doc_ids:
        row = knowledge_base.get_kb_row_doc(store, doc_id)
        if row is None or row["kb_id"] != kb_id:
            raise HyperRagError(f"kb_document_not_in_kb:{doc_id}")
        documents_payload.append(
            {
                "doc_id": row["id"],
                "title": row["title"],
                "content_md": row["content_md"],
            }
        )
        by_id[row["id"]] = row
    if not documents_payload:
        raise HyperRagError("kb_index_no_documents")
    payload = {
        "kb_id": kb_id,
        "user_id": member_id,
        "documents": documents_payload,
        "config": _config_payload(settings),
    }
    result = _post("/api/sync-batch", payload, INDEX_TIMEOUT)
    outcomes = {item["doc_id"]: item for item in result.get("results", []) if isinstance(item, dict)}
    for doc_id, outcome in outcomes.items():
        if outcome.get("success"):
            knowledge_base.mark_index_status(store, doc_id, "indexed")
        else:
            knowledge_base.mark_index_status(store, doc_id, "failed", str(outcome.get("error", "")))
    return {
        "kb_id": kb_id,
        "submitted": len(documents_payload),
        "indexed": sum(1 for item in outcomes.values() if item.get("success")),
        "failed": sum(1 for item in outcomes.values() if not item.get("success")),
        "results": [dict(item) for item in result.get("results", [])],
    }


def entities(kb_id: str, page: int = 1, page_size: int = 20) -> dict[str, Any]:
    return _get(f"/api/entities/{quote(kb_id)}?page={page}&page_size={page_size}")


def relationships(kb_id: str, page: int = 1, page_size: int = 20) -> dict[str, Any]:
    return _get(f"/api/relationships/{quote(kb_id)}?page={page}&page_size={page_size}")


def entity_names(kb_id: str, page: int = 1, page_size: int = 50) -> dict[str, Any]:
    return _get(f"/api/entity-names/{quote(kb_id)}?page={page}&page_size={page_size}")


def vertex_neighbor(kb_id: str, vertex_id: str) -> dict[str, Any]:
    return _get(f"/api/vertex-neighbor/{quote(kb_id)}/{quote(vertex_id)}")


def sync_progress(kb_id: str) -> dict[str, Any]:
    return _get(f"/api/sync-progress/{quote(kb_id)}")


__all__ = [
    "DEFAULT_SERVICE_URL",
    "HyperRagError",
    "entities",
    "entity_names",
    "health",
    "index_documents",
    "kb_status",
    "query",
    "relationships",
    "service_url",
    "sync_progress",
    "vertex_neighbor",
]