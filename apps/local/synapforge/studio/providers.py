"""渠道（provider）与本地路由探测。

一个渠道 = 协议 + base URL + key + 模型列表。Claude Code 走 Anthropic 协议，
自定义命令执行体走 OpenAI 兼容协议。除了手工新建，还可以：

- 从 ``~/.claude/settings.json`` 导入当前 Claude Code 路由；
- 扫描本机常见端口（magpie 3425、Ollama 11434、LM Studio 1234 …）；
- 扫描 Codex / opencode 配置里的 base URL。

密钥只留在本机 SQLite：所有 HTTP 响应只返回掩码和 ``has_key``，探测结果不落 key。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import store as studio_store
from .commands import StudioError

KINDS = ("anthropic", "openai")
DEFAULT_MODELS = ["glm-5.3", "glm-5.3-flash", "Qwen3.8-Max"]
USER_AGENT = "synapforge-studio/0.2"
MAX_MODELS = 500
MODEL_SUFFIX = re.compile(r"\[[^\]]*\]$")

# 本机常见 AI 网关。只连 127.0.0.1，不扫局域网。
LOCAL_PORTS: tuple[tuple[int, str, str, str], ...] = (
    (3425, "magpie 本地网关", "anthropic", "magpie"),
    (11434, "Ollama", "openai", ""),
    (1234, "LM Studio", "openai", ""),
    (4000, "LiteLLM", "openai", ""),
    (3000, "one-api / New API", "openai", ""),
    (8000, "vLLM / 通用 OpenAI", "openai", ""),
    (8080, "通用 OpenAI 网关", "openai", ""),
    (8081, "通用 OpenAI 网关", "openai", ""),
    (5000, "通用 OpenAI 网关", "openai", ""),
    (8888, "通用 OpenAI 网关", "openai", ""),
    (9000, "通用 OpenAI 网关", "openai", ""),
    (7860, "Gradio / 通用网关", "openai", ""),
)


# ---- 基础工具 ---------------------------------------------------------------


def _settings_env(home: Path) -> dict[str, Any]:
    try:
        data = json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        env = data.get("env") or {}
        return env if isinstance(env, dict) else {}
    except (OSError, ValueError):
        return {}


def clean_model(value: Any) -> str:
    return MODEL_SUFFIX.sub("", str(value or "").strip()).strip()


def settings_models(home: Path | None = None) -> tuple[list[str], str]:
    """`~/.claude/settings.json` 里的模型；作为“本机默认”渠道。"""

    env = _settings_env(home or Path.home())
    default = clean_model(env.get("ANTHROPIC_MODEL"))
    found: list[str] = []
    for key in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                "ANTHROPIC_DEFAULT_FABLE_MODEL"):
        value = clean_model(env.get(key))
        if value and value not in found:
            found.append(value)
    return found or list(DEFAULT_MODELS), default


def mask_key(key: str) -> str:
    raw = str(key or "")
    if not raw:
        return ""
    if len(raw) <= 8:
        return "••••"
    return f"{raw[:3]}…{raw[-4:]}"


def normalize_kind(kind: str) -> str:
    value = str(kind or "").strip().lower()
    if value in ("claude", "anthropic", "messages"):
        return "anthropic"
    if value in ("openai", "ollama", "lmstudio", "gateway", "compatible"):
        return "openai"
    raise StudioError("provider_kind_invalid", [f"未知协议：{kind}（可选 anthropic / openai）"])


def normalize_base(url: str, kind: str = "anthropic") -> str:
    raw = str(url or "").strip().rstrip("/")
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise StudioError("provider_url_invalid", [f"地址必须是 http(s)://：{raw}"])
    if parsed.username or parsed.password:
        raise StudioError("provider_url_invalid", ["地址里不要带用户名/密码"])
    return raw


def api_root(base_url: str) -> str:
    """Claude Code / OpenAI SDK 的 base 有时带 /v1；拼接口前先去掉。"""

    base = str(base_url or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base


def models_url(base_url: str) -> str:
    return api_root(base_url) + "/v1/models"


def is_local_url(url: str) -> bool:
    host = (urlparse(str(url or "")).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def _clean_models(values: Any) -> list[str]:
    out: list[str] = []
    if isinstance(values, str):
        values = re.split(r"[\n,]+", values)
    if not isinstance(values, (list, tuple, set)):
        return out
    for item in values:
        if isinstance(item, dict):
            item = item.get("id") or item.get("name") or item.get("model") or ""
        value = clean_model(item)
        if value and value not in out and len(value) <= 160:
            out.append(value)
        if len(out) >= MAX_MODELS:
            break
    return out


def _parse_models(data: Any) -> list[str]:
    if isinstance(data, dict):
        for key in ("data", "models", "result"):
            if isinstance(data.get(key), (list, tuple)):
                return _clean_models(data[key])
        return []
    return _clean_models(data)


def _request(url: str, *, headers: dict[str, str] | None = None,
             method: str = "GET", body: dict[str, Any] | None = None,
             timeout: float = 5.0) -> dict[str, Any]:
    """一次 HTTP 调用；永远返回结构化结果，不抛网络异常。"""

    data = None
    request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    request_headers.update(headers or {})
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=data, method=method, headers=request_headers)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(2_000_000)
            elapsed = int((time.monotonic() - started) * 1000)
            text = raw.decode("utf-8", errors="replace")
            try:
                parsed = json.loads(text) if text else None
            except ValueError:
                parsed = None
            return {"ok": True, "status": int(response.status), "data": parsed,
                    "text": text[:800], "latency_ms": elapsed, "url": url,
                    "content_type": response.headers.get("Content-Type") or ""}
    except urllib.error.HTTPError as error:
        elapsed = int((time.monotonic() - started) * 1000)
        try:
            text = error.read(200_000).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            text = ""
        try:
            parsed = json.loads(text) if text else None
        except ValueError:
            parsed = None
        return {"ok": False, "status": int(error.code), "data": parsed,
                "text": text[:800], "latency_ms": elapsed, "url": url,
                "content_type": error.headers.get("Content-Type") or "" if error.headers else ""}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        elapsed = int((time.monotonic() - started) * 1000)
        reason = getattr(error, "reason", error)
        return {"ok": False, "status": 0, "data": None, "text": "", "error": str(reason),
                "latency_ms": elapsed, "url": url, "content_type": ""}


def _auth_headers(kind: str, api_key: str) -> dict[str, str]:
    key = str(api_key or "").strip()
    if not key:
        return {}
    if kind == "anthropic":
        return {"x-api-key": key, "Authorization": f"Bearer {key}",
                "anthropic-version": "2023-06-01"}
    return {"Authorization": f"Bearer {key}"}


def _error_hint(result: dict[str, Any]) -> tuple[str, bool]:
    """把 HTTP 结果翻译成一句人话；第二个返回值表示是否认证问题。"""

    status = int(result.get("status") or 0)
    if status in (401, 403):
        return "认证失败（key 无效或没有权限）", True
    if status == 404:
        return "接口不存在（检查 base URL；Claude Code 的地址不要带 /v1）", False
    if status == 429:
        return "被限流（429），但地址和 key 是通的", False
    if status >= 500:
        return f"上游错误 {status}", False
    if status:
        text = str(result.get("text") or "")[:200]
        return f"HTTP {status}" + (f"：{text}" if text else ""), False
    return str(result.get("error") or "连接失败"), False


# ---- 探测 -------------------------------------------------------------------


def probe_endpoint(base_url: str, kind: str = "anthropic", *, api_key: str = "",
                   timeout: float = 5.0, include_ollama: bool = True) -> dict[str, Any]:
    """免费探测：先拉 /v1/models，失败再尝试 Ollama /api/tags。"""

    kind = normalize_kind(kind)
    url = models_url(base_url)
    result = _request(url, headers=_auth_headers(kind, api_key), timeout=timeout)
    models = _parse_models(result.get("data"))
    payload: dict[str, Any] = {
        "ok": bool(result["ok"]),
        "base_url": api_root(base_url),
        "kind": kind,
        "models": models,
        "latency_ms": result["latency_ms"],
        "status": result["status"],
        "endpoint": url,
        "error": "",
        "auth_error": False,
        "needs_deep": False,
    }
    if result["ok"]:
        if not models:
            payload["needs_deep"] = True
            payload["error"] = "接口可达，但没有返回模型列表；可以发一条最小消息验证"
        return payload
    hint, auth_error = _error_hint(result)
    payload["auth_error"] = auth_error
    if include_ollama and not auth_error and int(result.get("status") or 0) in (0, 404, 405):
        tags = _request(api_root(base_url) + "/api/tags",
                        headers=_auth_headers(kind, api_key), timeout=timeout)
        tag_models = _parse_models(tags.get("data"))
        if tags["ok"] and tag_models:
            payload.update({"ok": True, "models": tag_models,
                            "latency_ms": tags["latency_ms"], "status": tags["status"],
                            "endpoint": tags["url"], "error": "", "needs_deep": False})
            return payload
    payload["error"] = hint
    payload["needs_deep"] = _looks_reachable(result)
    return payload


def _looks_reachable(result: dict[str, Any]) -> bool:
    """404/405/HTML 说明 TCP+TLS 通了，只是没有 models 接口。"""

    status = int(result.get("status") or 0)
    return status in (404, 405) or (status == 200 and not result.get("data"))


def deep_check(base_url: str, kind: str, *, api_key: str = "", model: str = "",
               timeout: float = 25.0) -> dict[str, Any]:
    """发一条 max_tokens=1 的真实消息；会花极少 token，由用户显式触发。"""

    kind = normalize_kind(kind)
    if not str(model or "").strip():
        raise StudioError("model_required", ["深度检测需要一个模型名"])
    if kind == "anthropic":
        url = api_root(base_url) + "/v1/messages"
    else:
        url = api_root(base_url) + "/v1/chat/completions"
    body = {"model": clean_model(model), "max_tokens": 1,
            "messages": [{"role": "user", "content": "ping"}]}
    result = _request(url, headers=_auth_headers(kind, api_key), method="POST",
                      body=body, timeout=timeout)
    ok = bool(result["ok"]) and int(result.get("status") or 0) == 200
    return {
        "ok": ok,
        "status": result["status"],
        "latency_ms": result["latency_ms"],
        "endpoint": url,
        "model": clean_model(model),
        "error": "" if ok else _error_hint(result)[0],
        "reply": _reply_text(result.get("data")),
    }


def _reply_text(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    content = data.get("content")
    if isinstance(content, list):
        parts = [str(item.get("text") or "") for item in content if isinstance(item, dict)]
        return "".join(parts)[:200]
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict):
            return str(message.get("content") or "")[:200]
    return ""


def test_provider(store: Any, provider_id: str, *, deep: bool = False,
                  model: str = "", timeout: float = 6.0) -> dict[str, Any]:
    provider = studio_store.get_provider(store, provider_id)
    if provider is None:
        raise StudioError("provider_not_found", [provider_id])
    result = probe_endpoint(provider["base_url"], provider["kind"],
                            api_key=provider["api_key"], timeout=timeout)
    result["provider_id"] = provider["id"]
    result["provider_name"] = provider["name"]
    if deep:
        target = clean_model(model) or (provider["models"][0] if provider["models"] else "")
        if target:
            result["deep"] = deep_check(provider["base_url"], provider["kind"],
                                        api_key=provider["api_key"], model=target)
    checked = {key: value for key, value in result.items() if key != "deep"}
    checked["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    updates: dict[str, Any] = {"last_check": checked}
    if result.get("models"):
        updates["models"] = list(dict.fromkeys(list(provider["models"]) + result["models"]))
    studio_store.update_provider(store, provider_id, **updates)
    return result


def test_ambient(*, model: str = "", deep: bool = False, timeout: float = 6.0,
                 home: Path | None = None) -> dict[str, Any]:
    """没有渠道时，检测 `~/.claude/settings.json` 当前路由。"""

    env = _settings_env(home or Path.home())
    base = str(env.get("ANTHROPIC_BASE_URL") or "").strip()
    if not base:
        return {"ok": False, "error": "~/.claude/settings.json 没有 ANTHROPIC_BASE_URL",
                "status": 0, "latency_ms": 0, "models": [], "needs_deep": False,
                "auth_error": False}
    key = str(env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or "")
    result = probe_endpoint(base, "anthropic", api_key=key, timeout=timeout)
    result["provider_name"] = "本机默认"
    if deep:
        target = clean_model(model) or clean_model(env.get("ANTHROPIC_MODEL"))
        if target:
            result["deep"] = deep_check(base, "anthropic", api_key=key, model=target)
    return result


def test_agent(store: Any, agent_id: str, *, deep: bool = False,
               model: str = "", home: Path | None = None) -> dict[str, Any]:
    agent = studio_store.get_agent(store, agent_id)
    if agent is None:
        raise StudioError("agent_not_found", [agent_id])
    if agent["provider_id"]:
        return test_provider(store, agent["provider_id"], deep=deep,
                             model=model or agent["model"])
    result = test_ambient(model=model or agent["model"], deep=deep, home=home)
    result["agent_id"] = agent_id
    return result


# ---- 渠道 CRUD --------------------------------------------------------------


def public_provider(provider: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": provider["id"],
        "name": provider["name"],
        "kind": provider["kind"],
        "base_url": provider["base_url"],
        "models": provider["models"],
        "note": provider["note"],
        "enabled": provider["enabled"],
        "is_default": provider["is_default"],
        "last_check": provider["last_check"],
        "has_key": bool(provider["api_key"]),
        "key_masked": mask_key(provider["api_key"]),
        "created_at": provider["created_at"],
        "updated_at": provider["updated_at"],
    }


def list_providers(store: Any) -> list[dict[str, Any]]:
    return [public_provider(item) for item in studio_store.list_providers(store)]


def create_provider(store: Any, *, name: str, kind: str = "anthropic",
                    base_url: str = "", api_key: str = "",
                    models: Any = None, note: str = "", enabled: bool = True,
                    is_default: bool = False) -> dict[str, Any]:
    label = str(name or "").strip()
    if len(label) < 2:
        raise StudioError("provider_name_required", ["渠道名至少 2 个字符"])
    if len(label) > 60:
        raise StudioError("provider_name_too_long", ["渠道名不能超过 60 个字符"])
    if studio_store.get_provider_by_name(store, label):
        raise StudioError("provider_name_exists", [f"{label}: 已有同名渠道"])
    protocol = normalize_kind(kind)
    base = normalize_base(base_url, protocol)
    if not base:
        raise StudioError("provider_url_required", ["base URL 必填"])
    provider = studio_store.create_provider(
        store, name=label, kind=protocol, base_url=base,
        api_key=str(api_key or "").strip(), models=_clean_models(models or []),
        note=str(note or "").strip(), enabled=bool(enabled),
        is_default=bool(is_default))
    return public_provider(provider)


def update_provider(store: Any, provider_id: str, **fields: Any) -> dict[str, Any]:
    provider = studio_store.get_provider(store, provider_id)
    if provider is None:
        raise StudioError("provider_not_found", [provider_id])
    updates: dict[str, Any] = {}
    if "name" in fields:
        label = str(fields.get("name") or "").strip()
        if len(label) < 2:
            raise StudioError("provider_name_required", ["渠道名至少 2 个字符"])
        other = studio_store.get_provider_by_name(store, label)
        if other and other["id"] != provider_id:
            raise StudioError("provider_name_exists", [f"{label}: 已有同名渠道"])
        updates["name"] = label
    if "kind" in fields:
        updates["kind"] = normalize_kind(str(fields.get("kind") or ""))
    if "base_url" in fields:
        updates["base_url"] = normalize_base(str(fields.get("base_url") or ""),
                                             updates.get("kind") or provider["kind"])
        if not updates["base_url"]:
            raise StudioError("provider_url_required", ["base URL 必填"])
    api_key = fields.get("api_key")
    if api_key is not None and str(api_key).strip():
        updates["api_key"] = str(api_key).strip()
    if fields.get("clear_key"):
        updates["api_key"] = ""
    if "models" in fields:
        updates["models"] = _clean_models(fields.get("models") or [])
    if "note" in fields:
        updates["note"] = str(fields.get("note") or "").strip()
    if "enabled" in fields:
        updates["enabled"] = bool(fields.get("enabled"))
    if "is_default" in fields:
        updates["is_default"] = bool(fields.get("is_default"))
    updated = studio_store.update_provider(store, provider_id, **updates)
    return public_provider(updated)  # type: ignore[arg-type]


def delete_provider(store: Any, provider_id: str) -> dict[str, Any]:
    provider = studio_store.get_provider(store, provider_id)
    if provider is None:
        raise StudioError("provider_not_found", [provider_id])
    users = [a for a in studio_store.list_agents(store)
             if a.get("provider_id") == provider_id]
    studio_store.delete_provider(store, provider_id)
    return {"deleted": True, "provider_id": provider_id, "name": provider["name"],
            "detached_agents": [a["name"] for a in users]}


# ---- 模型目录 ---------------------------------------------------------------


def catalog(store: Any | None = None) -> dict[str, Any]:
    """统一模型目录：本机默认渠道 + 用户渠道。

    `models` 扁平列表兼容旧前端/datalist；`channels` 给带渠道选择的界面用。
    渠道的模型 = 手工保存的 + 上次检测发现的（去重，保存的排前面）。
    """

    found, settings_default = settings_models()
    channels: list[dict[str, Any]] = [{
        "id": "", "name": "本机默认", "kind": "anthropic",
        "base_url": "~/.claude/settings.json", "models": found,
        "has_key": True, "enabled": True, "is_default": False,
        "note": "沿用 Claude Code 当前配置", "last_check": {}, "key_masked": "",
    }]
    providers = studio_store.list_providers(store) if store is not None else []
    for provider in providers:
        if not provider["enabled"]:
            continue
        discovered = (provider.get("last_check") or {}).get("models") or []
        models = list(dict.fromkeys(
            [str(item) for item in provider["models"]]
            + [str(item) for item in discovered if item]))
        channels.append({
            "id": provider["id"], "name": provider["name"], "kind": provider["kind"],
            "base_url": provider["base_url"], "models": models,
            "has_key": bool(provider["api_key"]), "enabled": True,
            "is_default": provider["is_default"], "note": provider["note"],
            "last_check": provider["last_check"],
            "key_masked": mask_key(provider["api_key"]),
            "saved_count": len(provider["models"]),
        })

    models: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for channel in channels:
        for model in channel["models"]:
            key = (channel["id"], model)
            if key in seen:
                continue
            seen.add(key)
            models.append({"id": model, "label": model,
                           "provider_id": channel["id"],
                           "provider_name": channel["name"]})
    default = ""
    for channel in channels:
        if channel["is_default"] and channel["models"]:
            default = channel["models"][0]
            break
    if not default:
        for channel in channels:
            if channel["id"] == "" and settings_default in channel["models"]:
                default = settings_default
                break
    if not default and models:
        default = models[0]["id"]
    return {"models": models, "channels": channels, "default": default}


def channel_models(store: Any | None, provider_id: str) -> list[str]:
    for channel in catalog(store)["channels"]:
        if channel["id"] == str(provider_id or ""):
            return list(channel["models"])
    return []


def resolve_for_agent(store: Any, agent: dict[str, Any],
                      run: dict[str, Any] | None = None) -> dict[str, Any] | None:
    provider_id = str((run or {}).get("provider_id") or agent.get("provider_id") or "")
    if not provider_id:
        return None
    provider = studio_store.get_provider(store, provider_id)
    if provider is None:
        raise StudioError("provider_not_found", [provider_id])
    if not provider["enabled"]:
        raise StudioError("provider_disabled", [f"{provider['name']}: 渠道已停用"])
    backend = str((run or {}).get("backend") or agent.get("backend") or "claude")
    if backend == "claude" and provider["kind"] != "anthropic":
        raise StudioError(
            "provider_incompatible",
            [f"{provider['name']} 是 OpenAI 协议，Claude Code 不能直接用；"
             "请把执行体换成自定义命令，或改用 Anthropic 协议网关"])
    return provider


def env_for(provider: dict[str, Any] | None, model: str = "",
            api_key: str = "") -> dict[str, str]:
    """渠道 env；api_key 为空时用渠道自己的 key（路由会传入池里选中的 key）。"""

    if not provider:
        return {}
    kind = provider["kind"]
    base = api_root(provider["base_url"])
    key = str(api_key or provider.get("api_key") or "")
    env: dict[str, str] = {
        "SYNAPFORGE_PROVIDER": provider["name"],
        "SYNAPFORGE_PROVIDER_KIND": kind,
        "SYNAPFORGE_BASE_URL": base,
    }
    if model:
        env["SYNAPFORGE_MODEL"] = clean_model(model)
    if kind == "anthropic":
        env["ANTHROPIC_BASE_URL"] = base
        if key:
            env["ANTHROPIC_AUTH_TOKEN"] = key
            env["ANTHROPIC_API_KEY"] = key
        if model:
            env["ANTHROPIC_MODEL"] = clean_model(model)
    else:
        env["OPENAI_BASE_URL"] = base + "/v1"
        if key:
            env["OPENAI_API_KEY"] = key
        if model:
            env["OPENAI_MODEL"] = clean_model(model)
    return env


# ---- 本机路由扫描 -----------------------------------------------------------


def _route_id(base_url: str, kind: str) -> str:
    raw = f"{kind}|{api_root(base_url)}".encode("utf-8")
    return "route-" + hashlib.sha1(raw).hexdigest()[:12]


def _candidate(*, label: str, base_url: str, kind: str, source: str,
               api_key: str = "", models: Any = None, is_current: bool = False,
               port: int | None = None) -> dict[str, Any]:
    return {"label": label, "base_url": api_root(base_url), "kind": normalize_kind(kind),
            "source": source, "api_key": str(api_key or ""),
            "models": _clean_models(models or []), "is_current": is_current,
            "port": port}


def _claude_candidates(home: Path) -> list[dict[str, Any]]:
    env = _settings_env(home)
    base = str(env.get("ANTHROPIC_BASE_URL") or "").strip()
    if not base:
        return []
    key = str(env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or "")
    found, default = settings_models(home)
    return [_candidate(label="Claude Code 当前路由", base_url=base, kind="anthropic",
                       source="Claude Code 设置", api_key=key, models=found,
                       is_current=True)]


def _codex_candidates(home: Path) -> list[dict[str, Any]]:
    path = home / ".codex" / "config.toml"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    bases = re.findall(r"(?m)^\s*base_url\s*=\s*[\"']([^\"']+)[\"']", text)
    models = re.findall(r"(?m)^\s*model\s*=\s*[\"']([^\"']+)[\"']", text)
    key = ""
    try:
        auth = json.loads((home / ".codex" / "auth.json").read_text(encoding="utf-8"))
        key = str(auth.get("OPENAI_API_KEY") or "")
    except (OSError, ValueError):
        key = ""
    return [_candidate(label="Codex 配置", base_url=base, kind="openai",
                       source="Codex config.toml", api_key=key, models=models)
            for base in dict.fromkeys(bases[:4])]


def _opencode_paths(home: Path) -> list[Path]:
    paths = [home / ".config" / "opencode" / "opencode.json",
             home / ".config" / "opencode" / "config.json",
             home / ".opencode.json"]
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths.append(Path(appdata) / "opencode" / "opencode.json")
    return paths


def _opencode_candidates(home: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in _opencode_paths(home):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        providers = data.get("provider") if isinstance(data, dict) else None
        if not isinstance(providers, dict):
            continue
        for name, config in providers.items():
            if not isinstance(config, dict):
                continue
            options = config.get("options") or {}
            base = str(options.get("baseURL") or options.get("baseUrl")
                       or options.get("base_url") or "").strip()
            if not base:
                continue
            key = str(options.get("apiKey") or options.get("api_key") or "")
            models = list((config.get("models") or {}).keys()) \
                if isinstance(config.get("models"), dict) else []
            kind = "anthropic" if "anthropic" in str(name).lower() else "openai"
            out.append(_candidate(label=f"opencode · {name}", base_url=base, kind=kind,
                                  source=f"opencode {path.name}", api_key=key,
                                  models=models))
    return out


def _env_candidates() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    anthropic = str(os.environ.get("ANTHROPIC_BASE_URL") or "").strip()
    if anthropic:
        key = str(os.environ.get("ANTHROPIC_AUTH_TOKEN")
                  or os.environ.get("ANTHROPIC_API_KEY") or "")
        out.append(_candidate(label="环境变量 ANTHROPIC_BASE_URL", base_url=anthropic,
                              kind="anthropic", source="环境变量", api_key=key))
    openai = str(os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_BASE")
                 or "").strip()
    if openai:
        out.append(_candidate(label="环境变量 OPENAI_BASE_URL", base_url=openai,
                              kind="openai", source="环境变量",
                              api_key=str(os.environ.get("OPENAI_API_KEY") or "")))
    for env_name, label in (("OLLAMA_HOST", "环境变量 OLLAMA_HOST"),
                            ("LMSTUDIO_HOST", "环境变量 LMSTUDIO_HOST")):
        value = str(os.environ.get(env_name) or "").strip()
        if not value:
            continue
        base = value if value.startswith(("http://", "https://")) else f"http://{value}"
        out.append(_candidate(label=label, base_url=base, kind="openai", source="环境变量"))
    return out


def _port_open(port: int, timeout: float = 0.18) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex(("127.0.0.1", int(port))) == 0
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _port_candidates(ports: tuple[tuple[int, str, str, str], ...] | None = None,
                     timeout: float = 0.18) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for port, label, kind, key in (LOCAL_PORTS if ports is None else ports):
        if not _port_open(port, timeout):
            continue
        out.append(_candidate(label=f"{label} · :{port}",
                              base_url=f"http://127.0.0.1:{port}", kind=kind,
                              source="本机端口扫描", api_key=key, port=port))
    return out


def _probe_candidate(candidate: dict[str, Any], timeout: float) -> dict[str, Any]:
    probe = probe_endpoint(candidate["base_url"], candidate["kind"],
                           api_key=candidate["api_key"], timeout=timeout)
    models = probe["models"] or candidate["models"]
    return {
        "id": _route_id(candidate["base_url"], candidate["kind"]),
        "label": candidate["label"],
        "base_url": candidate["base_url"],
        "kind": candidate["kind"],
        "source": candidate["source"],
        "models": models,
        "model_count": len(models),
        "reachable": bool(probe["ok"]),
        "status": probe["status"],
        "latency_ms": probe["latency_ms"],
        "error": probe["error"],
        "auth_error": probe["auth_error"],
        "needs_deep": probe["needs_deep"],
        "has_key": bool(candidate["api_key"]),
        "key_masked": mask_key(candidate["api_key"]),
        "is_current": bool(candidate["is_current"]),
        "local": is_local_url(candidate["base_url"]),
        "port": candidate.get("port"),
    }


def scan_local_routes(*, home: Path | None = None,
                      ports: tuple[tuple[int, str, str, str], ...] | None = None,
                      timeout: float = 2.0, include_remote: bool = True,
                      store: Any | None = None) -> list[dict[str, Any]]:
    """扫描配置文件和本机端口；配置里的远端地址也会探测（只发给它自己的 host）。"""

    base_home = home or Path.home()
    candidates: list[dict[str, Any]] = []
    for finder in (_claude_candidates, _codex_candidates, _opencode_candidates):
        try:
            candidates.extend(finder(base_home))
        except Exception:  # noqa: BLE001 — 单个配置文件坏了不影响整体扫描
            continue
    candidates.extend(_env_candidates())
    candidates.extend(_port_candidates(ports))

    if not include_remote:
        candidates = [c for c in candidates if is_local_url(c["base_url"])]
    candidates = [c for c in candidates
                  if str(c["base_url"]).startswith(("http://", "https://"))]

    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for candidate in candidates:
        key = (candidate["kind"], api_root(candidate["base_url"]))
        if key in unique:
            existing = unique[key]
            existing["is_current"] = existing["is_current"] or candidate["is_current"]
            existing["api_key"] = existing["api_key"] or candidate["api_key"]
            merged = list(existing["models"]) + [m for m in candidate["models"]
                                                 if m not in existing["models"]]
            existing["models"] = merged
            if candidate["label"] not in existing["label"]:
                existing["label"] += " / " + candidate["label"]
            continue
        unique[key] = candidate

    routes: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_probe_candidate, candidate, timeout): candidate
                   for candidate in unique.values()}
        for future in futures:
            try:
                routes.append(future.result())
            except Exception:  # noqa: BLE001
                continue

    providers = studio_store.list_providers(store) if store is not None else []
    by_endpoint = {(p["kind"], api_root(p["base_url"])): p for p in providers}
    for route in routes:
        provider = by_endpoint.get((route["kind"], api_root(route["base_url"])))
        route["provider_id"] = provider["id"] if provider else ""
        route["added"] = bool(provider)

    routes.sort(key=lambda item: (not item["is_current"], not item["reachable"],
                                  not item["local"], item["label"]))
    return routes


def import_route(store: Any, *, url: str, kind: str = "anthropic",
                 name: str = "", home: Path | None = None,
                 ports: tuple[tuple[int, str, str, str], ...] | None = None) -> dict[str, Any]:
    """把扫描结果变成渠道；key 由服务端从本机配置重新读取，不接受前端传 key。"""

    target = api_root(url)
    protocol = normalize_kind(kind)
    routes = scan_local_routes(home=home, ports=ports, store=store, timeout=2.5)
    match = next((r for r in routes
                  if api_root(r["base_url"]) == target and r["kind"] == protocol), None)
    if match is None:
        raise StudioError("route_not_found", [f"{url}（{protocol}）"])
    existing = next((p for p in studio_store.list_providers(store)
                     if p["kind"] == protocol and api_root(p["base_url"]) == target), None)
    if existing:
        updates: dict[str, Any] = {
            "models": list(dict.fromkeys(list(existing["models"]) + list(match["models"]))),
            "last_check": {"ok": match["reachable"], "status": match["status"],
                           "latency_ms": match["latency_ms"],
                           "at": time.strftime("%Y-%m-%dT%H:%M:%S")},
        }
        key = _route_key(match, home=home)
        if key and not existing["api_key"]:
            updates["api_key"] = key
        updated = studio_store.update_provider(store, existing["id"], **updates)
        return public_provider(updated)  # type: ignore[arg-type]
    label = str(name or "").strip() or match["label"].split(" · ")[0]
    base_label = label
    index = 2
    while studio_store.get_provider_by_name(store, label):
        label = f"{base_label} {index}"
        index += 1
    provider = studio_store.create_provider(
        store, name=label, kind=protocol, base_url=match["base_url"],
        api_key=_route_key(match, home=home), models=match["models"],
        note=f"来自{match['source']}", is_default=False)
    checked = {"ok": match["reachable"], "status": match["status"],
               "latency_ms": match["latency_ms"], "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    updated = studio_store.update_provider(store, provider["id"], last_check=checked)
    return public_provider(updated)  # type: ignore[arg-type]


def _route_key(route: dict[str, Any], *, home: Path | None = None) -> str:
    """重新读配置拿 key；只给该路由自己的 host。"""

    if route.get("port") == 3425:
        return "magpie"
    base_home = home or Path.home()
    try:
        for candidate in (_claude_candidates(base_home) + _codex_candidates(base_home)
                          + _opencode_candidates(base_home) + _env_candidates()):
            if (candidate["kind"] == route["kind"]
                    and api_root(candidate["base_url"]) == api_root(route["base_url"])):
                return candidate["api_key"]
    except Exception:  # noqa: BLE001
        pass
    return ""


def import_claude_settings(store: Any, *, home: Path | None = None) -> dict[str, Any]:
    candidates = _claude_candidates(home or Path.home())
    if not candidates:
        raise StudioError("claude_settings_missing",
                          ["~/.claude/settings.json 里没有 ANTHROPIC_BASE_URL"])
    candidate = candidates[0]
    existing = next((p for p in studio_store.list_providers(store)
                     if p["kind"] == "anthropic"
                     and api_root(p["base_url"]) == candidate["base_url"]), None)
    models = _clean_models(candidate["models"])
    if existing:
        updated = studio_store.update_provider(
            store, existing["id"], base_url=candidate["base_url"],
            api_key=candidate["api_key"] or existing["api_key"],
            models=list(dict.fromkeys(list(existing["models"]) + models)))
        return public_provider(updated)  # type: ignore[arg-type]
    label = "Claude Code 设置"
    index = 2
    while studio_store.get_provider_by_name(store, label):
        label = f"Claude Code 设置 {index}"
        index += 1
    has_default = any(p["is_default"] for p in studio_store.list_providers(store))
    provider = studio_store.create_provider(
        store, name=label, kind="anthropic", base_url=candidate["base_url"],
        api_key=candidate["api_key"], models=models,
        note="从 ~/.claude/settings.json 导入", is_default=not has_default)
    return public_provider(provider)
