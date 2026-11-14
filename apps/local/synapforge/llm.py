"""最小 OpenAI 兼容客户端。

只有**执行体**用它；协议层一行都不碰网络（Stage 1 验收第 10 条）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Protocol

# 推理模型的 max_tokens 会被思考吃掉：实测 max_tokens=16 时 13 个 token 落在
# reasoning 上、正文被截断。默认给足。
DEFAULT_MAX_TOKENS = 4096
REQUEST_TIMEOUT_SECONDS = 180


class LlmError(RuntimeError):
    pass


class LlmClient(Protocol):
    def complete(self, *, system: str, user: str, max_tokens: int = DEFAULT_MAX_TOKENS) -> str: ...


class OpenAICompatLlm:
    def __init__(self, base_url: str, api_key: str, model: str, *, timeout: int = REQUEST_TIMEOUT_SECONDS) -> None:
        if not base_url or not api_key or not model:
            raise LlmError("llm_credentials_missing")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def complete(self, *, system: str, user: str, max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": 0,
        }
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.api_key,
                "User-Agent": "synapforge-local/0.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise LlmError(f"llm_http_{exc.code}") from exc
        except Exception as exc:  # noqa: BLE001 — 传输层任何异常都归一到可读码
            raise LlmError(f"llm_transport_{type(exc).__name__}") from exc
        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError("llm_response_shape_unexpected") from exc


class ScriptedLlm:
    """测试用：按顺序返回预设回复；用尽后重复最后一条。"""

    def __init__(self, replies: list[str]) -> None:
        if not replies:
            raise ValueError("scripted_llm_needs_at_least_one_reply")
        self.replies = list(replies)
        self.calls: list[dict[str, str]] = []

    def complete(self, *, system: str, user: str, max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
        self.calls.append({"system": system, "user": user})
        index = min(len(self.calls) - 1, len(self.replies) - 1)
        return self.replies[index]
