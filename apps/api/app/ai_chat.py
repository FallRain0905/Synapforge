"""内置 AI 普通对话：平台代理直连用户配置的 OpenAI 兼容 LLM。

凭据从成员 ``ai_settings`` 读取（用户自配第三方模型），平台不持有全局密钥。
支持 SSE 流式（``stream=True``）与非流式两种模式。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Generator, Mapping, Sequence


class AiChatError(RuntimeError):
    """稳定错误族：凭据缺失或上游 LLM 失败。"""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code if not detail else f"{code}:{detail}")
        self.code = code


DEFAULT_TIMEOUT = 60


def chat(
    messages: Sequence[Mapping[str, str]],
    settings: Mapping[str, Any],
    *,
    model: str | None = None,
    temperature: float = 0.7,
    stream: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any] | Generator[dict[str, Any], None, None]:
    """调用用户配置的 OpenAI 兼容接口。

    ``stream=True`` 时返回逐块生成器（每块含 ``delta`` 文本增量）。
    ``stream=False`` 时返回完整响应（含 ``content`` 与 ``usage``）。
    """

    api_key = settings.get("llm_api_key", "")
    base_url = str(settings.get("llm_base_url", "")).rstrip("/")
    model_name = model or settings.get("llm_model", "")
    if not api_key or not base_url or not model_name:
        raise AiChatError("ai_credentials_missing")
    if not base_url.endswith("/chat/completions"):
        base_url = base_url.rstrip("/") + "/chat/completions"

    payload = {
        "model": model_name,
        "messages": [dict(message) for message in messages],
        "temperature": temperature,
        "stream": stream,
    }
    if stream:
        payload["stream_options"] = {"include_usage": False}

    request = urllib.request.Request(
        base_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Accept": "text/event-stream" if stream else "application/json",
        },
        method="POST",
    )

    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        try:
            detail = error.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            detail = str(error.code)
        raise AiChatError(f"llm_http_{error.code}", detail) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise AiChatError("llm_unavailable", str(error)) from error

    if not stream:
        body = json.loads(response.read().decode("utf-8"))
        choice = body.get("choices", [{}])[0]
        content = choice.get("message", {}).get("content", "")
        return {
            "content": content,
            "model": body.get("model", model_name),
            "usage": body.get("usage", {}),
        }

    return _stream_chunks(response, model_name)


def _stream_chunks(response: Any, model_name: str) -> Generator[dict[str, Any], None, None]:
    """解析 SSE 流，逐块 yield ``{"delta": "文本增量"}``。"""

    buffer = ""
    try:
        while True:
            chunk = response.read(4096)
            if not chunk:
                break
            buffer += chunk.decode("utf-8", errors="replace")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    parsed = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = parsed.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {}).get("content", "")
                    if delta:
                        yield {"delta": delta, "model": parsed.get("model", model_name)}
    finally:
        response.close()


__all__ = ["AiChatError", "chat"]