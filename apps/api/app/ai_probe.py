"""UX-7-03：「测试连接」探针。

用成员已保存的凭据各发一次**最小**请求：
- LLM：``/chat/completions``，max_tokens=1；
- Embedding：``/embeddings``，输入一个词，顺带回报向量维度；
- MinerU：官方 API 没有免费的探活接口，因此这里只判断"是否已配置"，
  并明确告诉用户要用一次真实转换来验证——不假装测过。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Mapping

from .ai_chat import AiChatError

PROBE_TIMEOUT = 20


def _post_json(url: str, payload: dict[str, Any], api_key: str) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            detail = error.read().decode("utf-8", errors="replace")[:200]
        except Exception:  # noqa: BLE001
            detail = str(error.code)
        return error.code, {"_error": detail}
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return 0, {"_error": str(error)}


def _endpoint(base_url: str, suffix: str) -> str:
    base = base_url.rstrip("/")
    return base if base.endswith(suffix) else f"{base}{suffix}"


def probe_llm(settings: Mapping[str, Any]) -> dict[str, Any]:
    api_key = str(settings.get("llm_api_key") or "")
    base_url = str(settings.get("llm_base_url") or "")
    model = str(settings.get("llm_model") or "")
    if not api_key or not base_url or not model:
        return {"ok": False, "detail": "未配置完整（需要 Base URL、模型名与 API Key）"}
    status, body = _post_json(
        _endpoint(base_url, "/chat/completions"),
        {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1, "temperature": 0},
        api_key,
    )
    if status == 200:
        return {"ok": True, "detail": f"模型 {model} 可用", "model": model}
    if status == 0:
        return {"ok": False, "detail": f"网络不可达：{body.get('_error', '')}"}
    return {"ok": False, "detail": f"HTTP {status}：{body.get('_error', '')}"}


def probe_embedding(settings: Mapping[str, Any]) -> dict[str, Any]:
    api_key = str(settings.get("embedding_api_key") or "")
    base_url = str(settings.get("embedding_base_url") or "")
    model = str(settings.get("embedding_model") or "")
    if not api_key or not base_url or not model:
        return {"ok": False, "detail": "未配置完整（需要 Base URL、模型名与 API Key）"}
    status, body = _post_json(_endpoint(base_url, "/embeddings"), {"model": model, "input": ["ping"]}, api_key)
    if status == 200:
        vector = (body.get("data") or [{}])[0].get("embedding")
        dimensions = len(vector) if isinstance(vector, list) else None
        configured = settings.get("embedding_dimensions")
        mismatch = ""
        if dimensions and configured and int(configured) != dimensions:
            mismatch = f"（注意：设置里写的是 {configured} 维，实际返回 {dimensions} 维）"
        return {"ok": True, "detail": f"模型 {model} 可用，维度 {dimensions}{mismatch}", "dimensions": dimensions}
    if status == 0:
        return {"ok": False, "detail": f"网络不可达：{body.get('_error', '')}"}
    return {"ok": False, "detail": f"HTTP {status}：{body.get('_error', '')}"}


def probe_mineru(settings: Mapping[str, Any]) -> dict[str, Any]:
    configured = bool(str(settings.get("mineru_api_key") or ""))
    if not configured:
        return {"ok": False, "detail": "未配置 Token"}
    return {
        "ok": True,
        "detail": "Token 已配置；MinerU 没有免费探活接口，请用一次真实转换验证（云盘 → 转换入知识库）",
        "verified": False,
    }


def test_all(settings: Mapping[str, Any]) -> dict[str, Any]:
    """按组件返回探测结果；单个组件失败不影响其它组件。"""

    results: dict[str, Any] = {}
    for name, probe in (("llm", probe_llm), ("embedding", probe_embedding), ("mineru", probe_mineru)):
        try:
            results[name] = probe(settings)
        except AiChatError as error:
            results[name] = {"ok": False, "detail": str(error)}
        except Exception as error:  # noqa: BLE001 - 探针自身异常也要如实回报
            results[name] = {"ok": False, "detail": f"{type(error).__name__}:{error}"}
    return results