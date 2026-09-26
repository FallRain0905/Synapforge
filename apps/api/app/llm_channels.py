"""LLM 渠道与全员免费额度（管理员「渠道」页 + OpenAI 兼容代理端点）。

## 定位

平台把一个 OpenAI 兼容上游包装成「渠道」：管理员录入 base_url + api_key + 模型列表，
所有成员即获得平台提供的免费 LLM 额度——调用走平台代理端点
``POST /api/llm/v1/chat/completions``，按 token 扣成员额度，渠道对成员不可见。

## 安全边界（硬约束）

- api_key 只落库（与成员 ai_settings 同一先例：密钥存库、响应一律脱敏）；
  任何响应都不回传 key，只给 ``key_hint``（末 4 位）供管理员辨认；
- 渠道清单与 key 只有管理员能看；成员只能读自己的额度；
- 仓库与测试里**不出现任何真实 key / 真实渠道地址**，测试一律用本机假上游。

## 渠道检测与测速

- 检测（check）：对渠道发一次最小 chat 请求（max_tokens=1），回填 ``last_check_*``；
- 测速（speed-test）：连续 N 轮同样最小请求，记录每轮延迟与最小/平均/最大，
  渠道上记住最近一次平均延迟（``last_latency_ms``）。失败轮如实记录，不假装通过。

## 路由与配额

- 路由：请求模型必须命中某条**启用**渠道的 models（大小写不敏感），命中多条时
  priority 小者优先；没有可用渠道 → 明确报错（不猜、不回落）；
- 配额：``llm_member_quotas`` 懒创建（默认额度来自环境变量，管理员可按成员调整，
  负数上限 = 不限量）。调用前检查剩余额度，调用后按上游回报的 usage 扣减
  （上游不给 usage 就按字符数粗估）；每次调用记一条 ``llm_usage_log``。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Iterator, Mapping
from uuid import uuid4

# 成员默认免费额度（token）。管理员可按成员覆盖；负数 = 不限量。
DEFAULT_QUOTA_TOKENS = int(os.getenv("PLATFORM_LLM_FREE_TOKENS_PER_MEMBER", "200000"))
PROBE_TIMEOUT = 30.0
CHAT_TIMEOUT = 120.0
SPEED_TEST_ROUNDS = 3
MAX_SPEED_ROUNDS = 10

# 出站请求头：**必须**给 User-Agent。
# 实测教训（2026-09-26 真实渠道验收）：云/AI 中转普遍挂在 Cloudflare 后面，它按 UA 拦
# "看起来像脚本"的客户端——urllib 默认的 `Python-urllib/3.x` 会被直接 403（CF error 1010），
# 换成 SDK 形态的 UA 立刻 200。这里按 OpenAI 兼容客户端形态发请求（中转的允许清单就按这个认），
# 不是伪装浏览器。
UPSTREAM_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "OpenAI/Python 1.0.0",
}

# 稳定错误码 → HTTP 状态（main.py 的 _llm_error 用）。
ERROR_STATUS = {
    "llm_channel_not_found": 404,
    "llm_channel_no_route": 404,
    "llm_model_required": 400,
    "llm_name_required": 400,
    "llm_name_taken": 409,
    "llm_base_url_invalid": 400,
    "llm_models_required": 400,
    "llm_quota_exceeded": 429,
    "llm_messages_required": 400,
    "llm_upstream_error": 502,
    "llm_upstream_unreachable": 502,
}


class LlmChannelError(RuntimeError):
    """稳定错误族：code 进 HTTP detail，前端按 code 展示。"""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code if not detail else f"{code}:{detail}")
        self.code = code
        self.detail = detail or ""


@contextmanager
def _transaction(store: Any) -> Iterator[None]:
    try:
        yield
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _schema_ready(store: Any) -> bool:
    try:
        return store.db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'llm_channels'"
        ).fetchone() is not None
    except Exception:  # noqa: BLE001 - 生产 Postgres 由迁移建表，sqlite_master 探测会异常
        return True


def ensure_schema(store: Any) -> None:
    """SQLite 侧镜像 033 迁移（生产 Postgres 走 migrations/033_llm_channels.sql）。"""

    if _schema_ready(store):
        return
    store.db.executescript(
        """
        CREATE TABLE IF NOT EXISTS llm_channels (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL,
            base_url TEXT NOT NULL,
            api_key TEXT NOT NULL DEFAULT '',
            models TEXT NOT NULL DEFAULT '[]',
            priority INTEGER NOT NULL DEFAULT 100,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_check_at TEXT,
            last_check_ok INTEGER,
            last_check_detail TEXT NOT NULL DEFAULT '',
            last_latency_ms INTEGER,
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS llm_channels_name_idx ON llm_channels(name);

        CREATE TABLE IF NOT EXISTS llm_member_quotas (
            member_id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL DEFAULT '',
            token_limit INTEGER NOT NULL,
            tokens_used INTEGER NOT NULL DEFAULT 0,
            updated_by TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS llm_usage_log (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL DEFAULT '',
            member_id TEXT NOT NULL,
            channel_id TEXT,
            model TEXT NOT NULL DEFAULT '',
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            latency_ms INTEGER,
            status TEXT NOT NULL,
            error_code TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS llm_usage_log_member_idx ON llm_usage_log(member_id);
        CREATE INDEX IF NOT EXISTS llm_usage_log_created_idx ON llm_usage_log(created_at);
        """
    )
    store.db.commit()


# ---- 视图与脱敏 -----------------------------------------------------------


def _clean_models(models: Any) -> list[str]:
    if not isinstance(models, list):
        return []
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in models:
        name = str(raw).strip()
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            cleaned.append(name)
    return cleaned


def _channel_view(row: Any) -> dict[str, Any]:
    """对外视图：api_key 永不出现在响应里（key_hint 只有末 4 位）。"""

    try:
        models = json.loads(row["models"] or "[]")
    except (TypeError, ValueError):
        models = []
    key = str(row["api_key"] or "")
    checked_ok = row["last_check_ok"]
    return {
        "id": row["id"],
        "name": row["name"],
        "base_url": row["base_url"],
        "models": models,
        "priority": row["priority"],
        "enabled": bool(row["enabled"]),
        "key_hint": key[-4:] if key else "",
        "last_check_at": row["last_check_at"],
        "last_check_ok": None if checked_ok is None else bool(checked_ok),
        "last_check_detail": row["last_check_detail"] or "",
        "last_latency_ms": row["last_latency_ms"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _raw_channel(store: Any, channel_id: str, organization_id: str) -> Any:
    row = store.db.execute(
        "SELECT * FROM llm_channels WHERE id = ? AND organization_id = ?",
        (channel_id, str(organization_id or "")),
    ).fetchone()
    if row is None:
        raise LlmChannelError("llm_channel_not_found", channel_id)
    return row


# ---- 渠道 CRUD（管理员） ---------------------------------------------------


def list_channels(store: Any, organization_id: str) -> list[dict[str, Any]]:
    rows = store.db.execute(
        "SELECT * FROM llm_channels WHERE organization_id = ? ORDER BY priority ASC, created_at ASC",
        (str(organization_id or ""),),
    ).fetchall()
    return [_channel_view(row) for row in rows]


def get_channel(store: Any, channel_id: str, organization_id: str) -> dict[str, Any]:
    return _channel_view(_raw_channel(store, channel_id, organization_id))


def create_channel(
    store: Any,
    *,
    name: str,
    base_url: str,
    api_key: str = "",
    models: list[str] | None = None,
    priority: int = 100,
    enabled: bool = True,
    actor_member_id: str = "",
    organization_id: str = "",
) -> dict[str, Any]:
    name = str(name or "").strip()
    base = str(base_url or "").strip().rstrip("/")
    if not name:
        raise LlmChannelError("llm_name_required")
    if not base.startswith(("http://", "https://")):
        raise LlmChannelError("llm_base_url_invalid", base)
    cleaned = _clean_models(models)
    if not cleaned:
        raise LlmChannelError("llm_models_required")
    now = _now()
    channel_id = f"llmch-{uuid4().hex[:12]}"
    with _transaction(store):
        dup = store.db.execute("SELECT id FROM llm_channels WHERE name = ?", (name,)).fetchone()
        if dup is not None:
            raise LlmChannelError("llm_name_taken", name)
        store.db.execute(
            "INSERT INTO llm_channels (id, organization_id, name, base_url, api_key, models,"
            " priority, enabled, created_by, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                channel_id,
                str(organization_id or ""),
                name,
                base,
                str(api_key or "").strip(),
                json.dumps(cleaned, ensure_ascii=False),
                int(priority),
                1 if enabled else 0,
                str(actor_member_id or ""),
                now,
                now,
            ),
        )
    return get_channel(store, channel_id, organization_id)


def update_channel(
    store: Any,
    channel_id: str,
    *,
    organization_id: str = "",
    name: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    models: list[str] | None = None,
    priority: int | None = None,
    enabled: bool | None = None,
    actor_member_id: str = "",
) -> dict[str, Any]:
    """只改传了的字段；``api_key=None`` 表示保持不变，``""`` 表示清空。"""

    _raw_channel(store, channel_id, organization_id)
    updates: list[tuple[str, Any]] = []
    if name is not None:
        value = str(name).strip()
        if not value:
            raise LlmChannelError("llm_name_required")
        dup = store.db.execute(
            "SELECT id FROM llm_channels WHERE name = ? AND id != ?", (value, channel_id)
        ).fetchone()
        if dup is not None:
            raise LlmChannelError("llm_name_taken", value)
        updates.append(("name", value))
    if base_url is not None:
        value = str(base_url).strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise LlmChannelError("llm_base_url_invalid", value)
        updates.append(("base_url", value))
    if api_key is not None:
        updates.append(("api_key", str(api_key).strip()))
    if models is not None:
        cleaned = _clean_models(models)
        if not cleaned:
            raise LlmChannelError("llm_models_required")
        updates.append(("models", json.dumps(cleaned, ensure_ascii=False)))
    if priority is not None:
        updates.append(("priority", int(priority)))
    if enabled is not None:
        updates.append(("enabled", 1 if enabled else 0))
    if updates:
        updates.append(("updated_at", _now()))
        assignments = ", ".join(f"{column} = ?" for column, _ in updates)
        with _transaction(store):
            store.db.execute(
                f"UPDATE llm_channels SET {assignments} WHERE id = ? AND organization_id = ?",
                (*(value for _, value in updates), channel_id, str(organization_id or "")),
            )
    return get_channel(store, channel_id, organization_id)


def delete_channel(store: Any, channel_id: str, *, organization_id: str = "") -> None:
    """渠道删除后用量流水保留（channel_id 变成悬空引用，展示为「已删渠道」）。"""

    _raw_channel(store, channel_id, organization_id)
    with _transaction(store):
        store.db.execute(
            "DELETE FROM llm_channels WHERE id = ? AND organization_id = ?",
            (channel_id, str(organization_id or "")),
        )


# ---- 上游 HTTP（urllib，与 ai_probe 同一套先例） ---------------------------


def _normalized_base(base_url: str) -> str:
    """OpenAI 兼容约定：``…/v1/chat/completions``。没写 ``/v1`` 的自动补。"""

    base = str(base_url or "").strip().rstrip("/")
    return base if base.endswith("/v1") else f"{base}/v1"


def _open_upstream(base_url: str, api_key: str, payload: dict[str, Any], *, timeout: float) -> Any:
    """打开上游 chat/completions；HTTPError / 网络错误统一转 LlmChannelError。"""

    url = _normalized_base(base_url) + "/chat/completions"
    headers = dict(UPSTREAM_HEADERS)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        try:
            detail = error.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001
            detail = str(error.code)
        raise LlmChannelError("llm_upstream_error", f"HTTP {error.code} {detail}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise LlmChannelError("llm_upstream_unreachable", str(error)) from error


def _minimal_payload(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
    }


def _read_json(response: Any) -> Any:
    raw = response.read().decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except ValueError:
        return {"_raw": raw[:300]}


def _first_error_detail(body: Any) -> str:
    if isinstance(body, dict):
        message = body.get("error")
        if isinstance(message, dict):
            return str(message.get("message") or message)[:200]
        if message:
            return str(message)[:200]
        if body.get("_raw"):
            return str(body["_raw"])[:200]
    return ""


# ---- 检测与测速 ------------------------------------------------------------


def _record_check(
    store: Any, channel_id: str, *, ok: bool, detail: str, latency_ms: int
) -> None:
    with _transaction(store):
        store.db.execute(
            "UPDATE llm_channels SET last_check_at = ?, last_check_ok = ?, last_check_detail = ?,"
            " last_latency_ms = COALESCE(?, last_latency_ms), updated_at = ? WHERE id = ?",
            (_now(), 1 if ok else 0, detail[:300], latency_ms if ok else None, _now(), channel_id),
        )


def check_channel(
    store: Any, channel_id: str, *, model: str | None = None, organization_id: str = ""
) -> dict[str, Any]:
    """渠道检测：一次最小真实请求，成功与否都如实回填。"""

    row = _raw_channel(store, channel_id, organization_id)
    try:
        models = json.loads(row["models"] or "[]")
    except (TypeError, ValueError):
        models = []
    target = str(model or "").strip() or (models[0] if models else "")
    if not target:
        raise LlmChannelError("llm_model_required")
    started = time.perf_counter()
    ok = False
    detail = ""
    latency_ms = 0
    try:
        with _open_upstream(row["base_url"], row["api_key"], _minimal_payload(target), timeout=PROBE_TIMEOUT) as response:
            latency_ms = int((time.perf_counter() - started) * 1000)
            body = _read_json(response)
        choices = body.get("choices") if isinstance(body, dict) else None
        ok = bool(choices)
        detail = (
            f"模型 {target} 可用（{latency_ms} ms）"
            if ok
            else f"HTTP 200 但响应异常：{_first_error_detail(body) or '无 choices'}"
        )
    except LlmChannelError as error:
        latency_ms = int((time.perf_counter() - started) * 1000)
        detail = str(error)
    _record_check(store, channel_id, ok=ok, detail=detail, latency_ms=latency_ms)
    return {"ok": ok, "detail": detail, "latency_ms": latency_ms, "model": target, "checked_at": _now()}


def speed_test_channel(
    store: Any,
    channel_id: str,
    *,
    model: str | None = None,
    rounds: int = SPEED_TEST_ROUNDS,
    organization_id: str = "",
) -> dict[str, Any]:
    """渠道测速：连续 N 轮最小请求；每轮成败与延迟都如实汇报。"""

    row = _raw_channel(store, channel_id, organization_id)
    try:
        models = json.loads(row["models"] or "[]")
    except (TypeError, ValueError):
        models = []
    target = str(model or "").strip() or (models[0] if models else "")
    if not target:
        raise LlmChannelError("llm_model_required")
    count = max(1, min(int(rounds), MAX_SPEED_ROUNDS))
    results: list[dict[str, Any]] = []
    for index in range(1, count + 1):
        started = time.perf_counter()
        ok = False
        detail = ""
        latency_ms = 0
        try:
            with _open_upstream(row["base_url"], row["api_key"], _minimal_payload(target), timeout=PROBE_TIMEOUT) as response:
                latency_ms = int((time.perf_counter() - started) * 1000)
                body = _read_json(response)
            ok = bool(body.get("choices")) if isinstance(body, dict) else False
            detail = "ok" if ok else f"HTTP 200 但响应异常：{_first_error_detail(body) or '无 choices'}"
        except LlmChannelError as error:
            latency_ms = int((time.perf_counter() - started) * 1000)
            detail = str(error)
        results.append({"round": index, "ok": ok, "latency_ms": latency_ms, "detail": detail})
    good = [item["latency_ms"] for item in results if item["ok"]]
    summary = {
        "model": target,
        "rounds": results,
        "ok_rounds": len(good),
        "avg_ms": round(sum(good) / len(good)) if good else None,
        "min_ms": min(good) if good else None,
        "max_ms": max(good) if good else None,
    }
    if good:
        _record_check(store, channel_id, ok=True, detail=summary["model"] and f"测速 {len(good)}/{count} 轮通过，均值 {summary['avg_ms']} ms", latency_ms=int(summary["avg_ms"]))
    return summary


# ---- 路由与配额 ------------------------------------------------------------


def resolve_channel(store: Any, model: str, *, organization_id: str = "") -> Any:
    """按模型选渠道：本组织 + 启用 + 模型命中（大小写不敏感），priority 小者优先。"""

    key = str(model or "").strip().casefold()
    if not key:
        raise LlmChannelError("llm_model_required")
    rows = store.db.execute(
        "SELECT * FROM llm_channels WHERE enabled = 1 AND organization_id = ?"
        " ORDER BY priority ASC, created_at ASC",
        (str(organization_id or ""),),
    ).fetchall()
    for row in rows:
        try:
            models = json.loads(row["models"] or "[]")
        except (TypeError, ValueError):
            continue
        if any(str(name).strip().casefold() == key for name in models):
            return row
    raise LlmChannelError("llm_channel_no_route", model)


def available_models(store: Any, *, organization_id: str = "") -> list[str]:
    """所有启用渠道的模型合集（``GET /api/llm/v1/models`` 用，按优先级排序去重）。"""

    seen: set[str] = set()
    models: list[str] = []
    rows = store.db.execute(
        "SELECT * FROM llm_channels WHERE enabled = 1 AND organization_id = ?"
        " ORDER BY priority ASC, created_at ASC",
        (str(organization_id or ""),),
    ).fetchall()
    for row in rows:
        try:
            channel_models = json.loads(row["models"] or "[]")
        except (TypeError, ValueError):
            continue
        for name in channel_models:
            key = str(name).strip().casefold()
            if name and key not in seen:
                seen.add(key)
                models.append(str(name).strip())
    return models


def get_quota(store: Any, member_id: str, *, organization_id: str = "") -> dict[str, Any]:
    """成员额度（懒创建：首次查询即按默认值建档）。负数上限 = 不限量。"""

    row = store.db.execute(
        "SELECT * FROM llm_member_quotas WHERE member_id = ?", (member_id,)
    ).fetchone()
    if row is None:
        with _transaction(store):
            store.db.execute(
                "INSERT OR IGNORE INTO llm_member_quotas (member_id, organization_id, token_limit,"
                " tokens_used, updated_by, updated_at) VALUES (?, ?, ?, 0, 'default', ?)",
                (member_id, str(organization_id or ""), DEFAULT_QUOTA_TOKENS, _now()),
            )
        row = store.db.execute(
            "SELECT * FROM llm_member_quotas WHERE member_id = ?", (member_id,)
        ).fetchone()
    limit = int(row["token_limit"])
    used = int(row["tokens_used"])
    unlimited = limit < 0
    return {
        "member_id": member_id,
        "organization_id": str(organization_id or ""),
        "token_limit": limit,
        "tokens_used": used,
        "tokens_remaining": None if unlimited else max(0, limit - used),
        "unlimited": unlimited,
    }


def set_quota(
    store: Any,
    member_id: str,
    token_limit: int,
    *,
    actor_member_id: str = "",
    organization_id: str = "",
) -> dict[str, Any]:
    get_quota(store, member_id, organization_id=organization_id)  # 确保行存在
    with _transaction(store):
        store.db.execute(
            "UPDATE llm_member_quotas SET token_limit = ?, updated_by = ?, updated_at = ? WHERE member_id = ?",
            (int(token_limit), str(actor_member_id or ""), _now(), member_id),
        )
    return get_quota(store, member_id, organization_id=organization_id)


def _deduct_quota(store: Any, member_id: str, tokens: int) -> None:
    if tokens <= 0:
        return
    with _transaction(store):
        store.db.execute(
            "UPDATE llm_member_quotas SET tokens_used = tokens_used + ?, updated_at = ? WHERE member_id = ?",
            (int(tokens), _now(), member_id),
        )


def _estimate_tokens(messages: Any, text: str) -> tuple[int, int]:
    """上游不给 usage 时的粗估（约 4 字符 = 1 token），只作记账用。"""

    prompt = 0
    if isinstance(messages, list):
        for item in messages:
            content = item.get("content") if isinstance(item, dict) else None
            prompt += max(1, len(str(content or "")) // 4)
    completion = max(1, len(text or "") // 4)
    return max(1, prompt), completion


def _record_usage(
    store: Any,
    *,
    member_id: str,
    organization_id: str = "",
    channel_id: str | None,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    status: str,
    error_code: str = "",
) -> None:
    with _transaction(store):
        store.db.execute(
            "INSERT INTO llm_usage_log (id, organization_id, member_id, channel_id, model, prompt_tokens,"
            " completion_tokens, total_tokens, latency_ms, status, error_code, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"llmu-{uuid4().hex[:16]}",
                str(organization_id or ""),
                member_id,
                channel_id,
                model,
                int(prompt_tokens),
                int(completion_tokens),
                int(prompt_tokens + completion_tokens),
                int(latency_ms) if latency_ms else None,
                status,
                str(error_code or "")[:120],
                _now(),
            ),
        )


def usage_overview(store: Any, *, limit: int = 20, organization_id: str = "") -> dict[str, Any]:
    """管理员侧总览：总量 + 按成员聚合（top N，额度行存在才带上限）。"""

    org = str(organization_id or "")
    total = store.db.execute(
        "SELECT COUNT(*) AS requests,"
        " COALESCE(SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END), 0) AS ok_requests,"
        " COALESCE(SUM(total_tokens), 0) AS total_tokens FROM llm_usage_log WHERE organization_id = ?",
        (org,),
    ).fetchone()
    rows = store.db.execute(
        "SELECT log.member_id AS member_id, COALESCE(SUM(log.total_tokens), 0) AS log_tokens,"
        " COUNT(*) AS requests, quota.tokens_used AS tokens_used, quota.token_limit AS token_limit"
        " FROM llm_usage_log log"
        " LEFT JOIN llm_member_quotas quota ON quota.member_id = log.member_id"
        " WHERE log.organization_id = ?"
        " GROUP BY log.member_id ORDER BY log_tokens DESC, requests DESC LIMIT ?",
        (org, int(limit)),
    ).fetchall()
    members: list[dict[str, Any]] = []
    for row in rows:
        token_limit = row["token_limit"]
        members.append(
            {
                "member_id": row["member_id"],
                "requests": int(row["requests"]),
                "log_tokens": int(row["log_tokens"]),
                "tokens_used": None if row["tokens_used"] is None else int(row["tokens_used"]),
                "token_limit": None if token_limit is None else int(token_limit),
                "unlimited": token_limit is not None and int(token_limit) < 0,
            }
        )
    return {
        "totals": {
            "requests": int(total["requests"]),
            "ok_requests": int(total["ok_requests"]),
            "total_tokens": int(total["total_tokens"]),
        },
        "members": members,
    }


# ---- 代理端点 --------------------------------------------------------------


def validate_chat_payload(payload: Any) -> dict[str, Any]:
    """OpenAI 兼容体校验：model + messages 必须有；透传其余字段。"""

    body = dict(payload or {})
    if not str(body.get("model") or "").strip():
        raise LlmChannelError("llm_model_required")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise LlmChannelError("llm_messages_required")
    body["stream"] = bool(body.get("stream"))
    return body


def _quota_gate(store: Any, member_id: str, *, organization_id: str = "") -> None:
    """调用前的额度闸门：没有剩余就拒绝（调用上游之前，不白花）。"""

    quota = get_quota(store, member_id, organization_id=organization_id)
    if not quota["unlimited"] and int(quota["tokens_remaining"] or 0) <= 0:
        raise LlmChannelError("llm_quota_exceeded", str(quota["tokens_used"]))


def _extract_usage(body: Any) -> tuple[int, int]:
    if isinstance(body, dict) and isinstance(body.get("usage"), dict):
        usage = body["usage"]
        return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
    return 0, 0


def _completion_text(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    parts: list[str] = []
    for choice in body.get("choices") or []:
        message = choice.get("message") if isinstance(choice, dict) else None
        if isinstance(message, dict) and message.get("content"):
            parts.append(str(message["content"]))
    return "".join(parts)


def proxy_chat_completions(
    store: Any, member_id: str, payload: Mapping[str, Any], *, organization_id: str = ""
) -> dict[str, Any]:
    """非流式代理：额度闸门 → 选渠道 → 转发 → 扣减 + 记账 → 原样回传上游响应。"""

    body = validate_chat_payload(payload)
    model = str(body["model"]).strip()
    _quota_gate(store, member_id, organization_id=organization_id)
    row = resolve_channel(store, model, organization_id=organization_id)
    started = time.perf_counter()
    try:
        with _open_upstream(row["base_url"], row["api_key"], body, timeout=CHAT_TIMEOUT) as response:
            upstream = _read_json(response)
            status_ok = True
    except LlmChannelError as error:
        latency_ms = int((time.perf_counter() - started) * 1000)
        _record_usage(
            store, member_id=member_id, organization_id=organization_id, channel_id=row["id"], model=model,
            prompt_tokens=0, completion_tokens=0,
            latency_ms=latency_ms, status="error", error_code=error.code,
        )
        raise
    latency_ms = int((time.perf_counter() - started) * 1000)
    prompt_tokens, completion_tokens = _extract_usage(upstream)
    if prompt_tokens == 0 and completion_tokens == 0:
        prompt_tokens, completion_tokens = _estimate_tokens(body.get("messages"), _completion_text(upstream))
    _deduct_quota(store, member_id, prompt_tokens + completion_tokens)
    _record_usage(
        store, member_id=member_id, organization_id=organization_id, channel_id=row["id"], model=model,
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        latency_ms=latency_ms, status="ok" if status_ok else "error",
    )
    return upstream if isinstance(upstream, dict) else {"_raw": upstream}


def proxy_chat_completions_stream(
    store: Any, member_id: str, payload: Mapping[str, Any], *, organization_id: str = ""
) -> Iterator[bytes]:
    """流式代理（SSE 逐行透传）。同步段先完成额度闸门与选渠（错误以 HTTP 状态返回），
    流结束后按最后一个带 usage 的 chunk 扣减；上游没给 usage 就按收到的正文粗估。"""

    body = validate_chat_payload(payload)
    model = str(body["model"]).strip()
    _quota_gate(store, member_id, organization_id=organization_id)
    row = resolve_channel(store, model, organization_id=organization_id)
    stream_body = {**body, "stream": True}

    def generate() -> Iterator[bytes]:
        prompt_tokens = 0
        completion_tokens = 0
        text_parts: list[str] = []
        status = "ok"
        error_code = ""
        started = time.perf_counter()
        response = None
        try:
            response = _open_upstream(row["base_url"], row["api_key"], stream_body, timeout=CHAT_TIMEOUT)
            for raw in response:
                line = raw.decode("utf-8", errors="replace").strip()
                if line.startswith("data:"):
                    data = line[len("data:"):].strip()
                    if data and data != "[DONE]":
                        try:
                            chunk = json.loads(data)
                        except ValueError:
                            chunk = None
                        if isinstance(chunk, dict):
                            usage = chunk.get("usage")
                            if isinstance(usage, dict):
                                prompt_tokens = int(usage.get("prompt_tokens") or 0)
                                completion_tokens = int(usage.get("completion_tokens") or 0)
                            choices = chunk.get("choices")
                            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                                delta = choices[0].get("delta")
                                if isinstance(delta, dict) and delta.get("content"):
                                    text_parts.append(str(delta["content"]))
                yield raw
        except (LlmChannelError, urllib.error.URLError, TimeoutError, OSError) as error:
            status = "error"
            error_code = error.code if isinstance(error, LlmChannelError) else "llm_upstream_unreachable"
            message = str(error).replace('"', "'").encode("utf-8")
            yield b'data: {"error": {"message": "' + message + b'"}}\n\n'
            yield b"data: [DONE]\n\n"
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:  # noqa: BLE001
                    pass
            if prompt_tokens == 0 and completion_tokens == 0 and status == "ok":
                prompt_tokens, completion_tokens = _estimate_tokens(body.get("messages"), "".join(text_parts))
            latency_ms = int((time.perf_counter() - started) * 1000)
            _deduct_quota(store, member_id, prompt_tokens + completion_tokens)
            _record_usage(
                store, member_id=member_id, organization_id=organization_id, channel_id=row["id"], model=model,
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                latency_ms=latency_ms, status=status, error_code=error_code,
            )

    return generate()
