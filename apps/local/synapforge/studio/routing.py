"""渠道路由：一个渠道可以挂多个 key，失败自动换下一个。

借鉴 magpie 的“额度用完悄悄换下一个账号”：
- 渠道本身保留一个主 key（兼容旧数据），额外 key 存在 `local_provider_keys`；
- 运行前按顺序挑一个「可用」的 key（enabled 且不是 failed/limited）；
- 运行失败且错误像认证/额度/限流时，把该 key 标记为 failed/limited 并换下一个重试；
- 全部失败时如实报错，不伪造成功。

标记会随时间自愈：`limited` 超过冷却时间（默认 10 分钟）重新可用，
`failed` 需要手动「恢复」或下一次深度检测通过。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from . import providers as studio_providers
from . import store as studio_store

LIMIT_COOLDOWN_SECONDS = 600
AUTH_PATTERNS = (
    "401", "403", "invalid token", "invalid api key", "unauthorized",
    "authentication", "auth error", "no api key", "permission",
)
LIMIT_PATTERNS = (
    "429", "rate limit", "rate_limit", "quota", "insufficient", "exceeded",
    "余额", "额度", "限流", "too many requests",
)


@dataclass(frozen=True)
class RouteKey:
    id: str
    label: str
    api_key: str
    source: str  # "provider" | "pool"


def _now() -> float:
    return time.time()


def _key_available(key: dict[str, Any]) -> bool:
    if not key["enabled"]:
        return False
    status = str(key.get("status") or "")
    if status == "failed":
        return False
    if status == "limited":
        checked = key.get("last_check") or {}
        until = float(checked.get("until") or 0)
        return until <= _now()
    return True


def classify_failure(text: str) -> str:
    """把错误文本归类成 `auth` / `limited` / `other`。"""

    raw = str(text or "").lower()
    if any(pattern in raw for pattern in AUTH_PATTERNS):
        return "auth"
    if any(pattern in raw for pattern in LIMIT_PATTERNS):
        return "limited"
    return "other"


def pool_keys(store: Any, provider: dict[str, Any]) -> list[dict[str, Any]]:
    """按顺序返回所有 key（含渠道主 key），带可用性。"""

    out: list[dict[str, Any]] = []
    if str(provider.get("api_key") or "").strip():
        out.append({"id": f"{provider['id']}:main", "label": "主 key",
                    "api_key": provider["api_key"], "enabled": True,
                    "status": "", "source": "provider",
                    "last_check": provider.get("last_check") or {}})
    for key in studio_store.list_provider_keys(store, provider["id"]):
        out.append({**key, "source": "pool"})
    return out


def usable_keys(store: Any, provider: dict[str, Any]) -> list[RouteKey]:
    keys = [item for item in pool_keys(store, provider) if _key_available(item)]
    return [RouteKey(id=item["id"], label=str(item.get("label") or "key"),
                     api_key=str(item["api_key"]), source=item["source"])
            for item in keys if str(item.get("api_key") or "").strip()]


def public_pool(store: Any, provider: dict[str, Any]) -> dict[str, Any]:
    keys = pool_keys(store, provider)
    return {
        "keys": [{
            "id": item["id"],
            "label": item.get("label") or "",
            "masked": studio_providers.mask_key(str(item.get("api_key") or "")),
            "enabled": bool(item.get("enabled")),
            "status": str(item.get("status") or ""),
            "available": _key_available(item),
            "source": item["source"],
            "last_check": item.get("last_check") or {},
        } for item in keys],
        "total": len(keys),
        "available": len([item for item in keys if _key_available(item)]),
    }


def mark_key(store: Any, provider_id: str, key_id: str, outcome: str,
             detail: str = "") -> None:
    """记录一次结果：ok 清状态；auth 标 failed；limited 标 limited + 冷却。"""

    if key_id.endswith(":main"):
        if outcome == "ok":
            studio_store.update_provider(store, provider_id, last_check={
                "ok": True, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        return
    if outcome == "ok":
        studio_store.update_provider_key(store, key_id, status="", last_check={
            "ok": True, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        return
    payload: dict[str, Any] = {"ok": False, "error": str(detail or "")[:300],
                               "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if outcome == "limited":
        payload["until"] = _now() + LIMIT_COOLDOWN_SECONDS
        studio_store.update_provider_key(store, key_id, status="limited",
                                         last_check=payload)
    else:
        studio_store.update_provider_key(store, key_id, status="failed",
                                         last_check=payload)


def restore_key(store: Any, provider_id: str, key_id: str) -> dict[str, Any]:
    if key_id.endswith(":main"):
        studio_store.update_provider(store, provider_id, last_check={})
        return public_pool(store, studio_store.get_provider(store, provider_id))
    studio_store.update_provider_key(store, key_id, status="", last_check={})
    return public_pool(store, studio_store.get_provider(store, provider_id))


def add_key(store: Any, provider_id: str, *, api_key: str, label: str = "",
            enabled: bool = True) -> dict[str, Any]:
    from .commands import StudioError

    provider = studio_store.get_provider(store, provider_id)
    if provider is None:
        raise StudioError("provider_not_found", [provider_id])
    value = str(api_key or "").strip()
    if not value:
        raise StudioError("provider_key_required", ["key 不能为空"])
    studio_store.add_provider_key(store, provider_id=provider_id, api_key=value,
                                  label=str(label or "").strip(), enabled=enabled)
    return public_pool(store, provider)


def delete_key(store: Any, provider_id: str, key_id: str) -> dict[str, Any]:
    from .commands import StudioError

    provider = studio_store.get_provider(store, provider_id)
    if provider is None:
        raise StudioError("provider_not_found", [provider_id])
    if key_id.endswith(":main"):
        raise StudioError("provider_key_main", ["主 key 请在编辑渠道里清空"])
    studio_store.delete_provider_key(store, key_id)
    return public_pool(store, provider)


def update_key(store: Any, provider_id: str, key_id: str, *,
               enabled: bool | None = None, label: str | None = None) -> dict[str, Any]:
    from .commands import StudioError

    provider = studio_store.get_provider(store, provider_id)
    if provider is None:
        raise StudioError("provider_not_found", [provider_id])
    if key_id.endswith(":main"):
        raise StudioError("provider_key_main", ["主 key 请在编辑渠道里修改"])
    fields: dict[str, Any] = {}
    if enabled is not None:
        fields["enabled"] = bool(enabled)
    if label is not None:
        fields["label"] = str(label)
    studio_store.update_provider_key(store, key_id, **fields)
    return public_pool(store, provider)


def test_key(store: Any, provider_id: str, key_id: str,
             timeout: float = 6.0) -> dict[str, Any]:
    """用某个 key 拉一次模型列表，结果写回该 key 的状态。"""

    from .commands import StudioError

    provider = studio_store.get_provider(store, provider_id)
    if provider is None:
        raise StudioError("provider_not_found", [provider_id])
    if key_id.endswith(":main"):
        api_key = provider["api_key"]
    else:
        key = studio_store.get_provider_key(store, key_id)
        if key is None:
            raise StudioError("provider_key_not_found", [key_id])
        api_key = key["api_key"]
    result = studio_providers.probe_endpoint(
        provider["base_url"], provider["kind"], api_key=api_key, timeout=timeout)
    if result["ok"]:
        mark_key(store, provider_id, key_id, "ok")
    else:
        kind = classify_failure(result.get("error") or "")
        mark_key(store, provider_id, key_id,
                 "limited" if kind == "limited" else "failed",
                 result.get("error") or "")
    result["key_id"] = key_id
    return result


def route_summary(store: Any, provider: dict[str, Any]) -> dict[str, Any]:
    pool = public_pool(store, provider)
    usable = usable_keys(store, provider)
    return {
        "provider_id": provider["id"],
        "provider_name": provider["name"],
        "total": pool["total"],
        "available": pool["available"],
        "next": usable[0].label if usable else "",
        "keys": pool["keys"],
    }
