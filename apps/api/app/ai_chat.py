"""内置 AI 普通对话：**渠道优先**，成员自配凭据兜底。

- 平台渠道（`llm_channels`）：管理员录入的 OpenAI 兼容上游，成员消耗平台免费额度，
  不需要自己配任何凭据；
- 成员自配（`ai_settings`）：原有行为——`llm_api_key/base_url/model` 走成员自己的第三方模型。

路由口径（`chat_routed`）：请求模型命中某条**启用**渠道 → 走渠道；没有渠道接得住 →
回退成员自配凭据。一旦渠道接住了请求，渠道自身的失败**不**静默回落到成员自配 key
（避免悄悄花成员自己的钱），以 `AiChatError(原 llm_* 错误码)` 抛出。
支持 SSE 流式（``stream=True``）与非流式两种模式。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Generator, Mapping, Sequence

from . import llm_channels


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




# ---- 渠道优先路由（/ask 的入口） -------------------------------------------


def chat_routed(
    store: Any,
    *,
    member_id: str,
    organization_id: str,
    messages: Sequence[Mapping[str, str]],
    settings: Mapping[str, Any],
    model: str | None = None,
    temperature: float = 0.7,
    stream: bool = False,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[dict[str, Any] | Generator[dict[str, Any], None, None], str]:
    """/ask 的统一入口：**渠道优先，成员自配凭据兜底**。

    - 请求模型（显式指定 or 成员 ``llm_model``；都没配则用优先级最高的启用渠道的第一个模型）
      命中某条**启用**渠道 → 走平台渠道：扣成员免费额度、记用量流水，``via="channel"``；
    - 没有渠道接得住（没配渠道 / 模型不在任何渠道上）→ 回退成员自配凭据（``chat``，原有行为），
      ``via="own_key"``；
    - **渠道一旦接住，自身失败不静默回落**到成员自配 key——回退只发生在"路由"阶段，
      避免渠道坏了/额度耗尽时悄悄花成员自己的付费 key。

    返回 ``(结果, via)``：非流式为 ``({"content", "model", "usage"}, via)``；
    流式为 ``(逐块生成器, via)``，每块形如 ``{"delta": ...}``（首块带 ``via``）。
    """

    requested = str(model or settings.get("llm_model") or "").strip()
    if not requested:
        available = llm_channels.available_models(store, organization_id=organization_id)
        requested = available[0] if available else ""

    if requested:
        payload = {
            "model": requested,
            "messages": [dict(message) for message in messages],
            "temperature": temperature,
        }
        try:
            if stream:
                inner = llm_channels.proxy_chat_completions_stream(
                    store, member_id, payload, organization_id=organization_id
                )
                return _channel_stream_chunks(inner, requested), "channel"
            upstream = llm_channels.proxy_chat_completions(
                store, member_id, payload, organization_id=organization_id
            )
        except llm_channels.LlmChannelError as error:
            if error.code == "llm_channel_no_route":
                pass  # 没有渠道接得住这个模型 → 回退成员自配凭据
            else:
                # 渠道已接住：额度耗尽 / 上游故障都如实抛出，不烧成员自己的 key
                raise AiChatError(error.code, error.detail or None) from error
        else:
            choices = upstream.get("choices") or [{}]
            first = choices[0] if isinstance(choices[0], dict) else {}
            content = (first.get("message") or {}).get("content", "")
            return {
                "content": content,
                "model": upstream.get("model", requested),
                "usage": upstream.get("usage", {}),
            }, "channel"

    # ---- 没有渠道接得住：成员自配凭据（原有行为，凭据缺失由 chat 抛 ai_credentials_missing）----
    return chat(messages, settings, model=model, temperature=temperature, stream=stream, timeout=timeout), "own_key"


def _channel_stream_chunks(
    raw: Generator[bytes, None, None], model_name: str
) -> Generator[dict[str, Any], None, None]:
    """把渠道流式代理的原始 SSE 字节流转成 /ask 的 delta 块。

    首个内容块带 ``via: "channel"``；渠道中途出错时透传 ``{"error": ...}``（代理侧已保证
    出错后跟 [DONE]）。
    """

    newline = chr(10)
    buffer = ""
    first = True
    try:
        for chunk in raw:
            buffer += chunk.decode("utf-8", errors="replace")
            while newline in buffer:
                line, buffer = buffer.split(newline, 1)
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
                if not isinstance(parsed, dict):
                    continue
                if parsed.get("error"):
                    yield {"error": parsed["error"], "via": "channel"}
                    continue
                choices = parsed.get("choices") or []
                delta = ""
                if choices and isinstance(choices[0], dict):
                    delta = (choices[0].get("delta") or {}).get("content", "")
                if delta:
                    block = {"delta": delta, "model": parsed.get("model", model_name)}
                    if first:
                        block["via"] = "channel"
                        first = False
                    yield block
    finally:
        close = getattr(raw, "close", None)
        if close is not None:
            close()


__all__ = ["AiChatError", "chat", "chat_routed"]
