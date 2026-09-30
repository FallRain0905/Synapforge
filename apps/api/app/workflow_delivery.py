"""交付适配器（W3.6，实施计划第六期）。

工作流节点的 ``delivery_adapter`` 声明"产出之后怎么交付"；这里定义适配器协议与注册表，
推进器在节点任务 APPROVED 后调用——**交付是批准后的副作用**，失败如实记账，不掩盖、
不阻塞运行完成（"失败、阻塞状态如实呈现"纪律）。

v1 注册两个真实适配器（背靠既有 `delivery.py`，不是空壳）：
- ``paper_compile`` → ``delivery.compile_paper``（论文源码 → PDF 成果物，缺引擎时
  fail-closed 返回 not_compiled 诊断，不登记假成果）；
- ``submission_bundle`` → ``delivery.create_submission_bundle``（装配+检查+清单，进待审）。

扩展方式：新适配器实现 ``DeliveryAdapter`` 协议并注册进 ``ADAPTERS``——
定义侧只引 ``kind`` 字符串（schema §3 的 ``delivery_adapters[].kind``）。
"""

from __future__ import annotations

from typing import Any, Callable, Protocol
from uuid import UUID

from . import delivery

__all__ = ["DeliveryAdapter", "ADAPTERS", "run_adapter", "DeliveryAdapterError"]


class DeliveryAdapterError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}:{detail}")
        self.code = code


class DeliveryAdapter(Protocol):
    """``kind`` 是定义侧引用名；``deliver`` 返回如实的结果字典（含 status）。"""

    kind: str

    def deliver(self, store: Any, project_id: UUID, *, actor: str, node_id: str, task_id: str) -> dict[str, Any]: ...


def _paper_compile(store: Any, project_id: UUID, *, actor: str, node_id: str, task_id: str) -> dict[str, Any]:
    result = delivery.compile_paper(store, project_id, actor=actor, actor_kind="member")
    return {"adapter": "paper_compile", "status": str(result.get("status") or "unknown"), "detail": result}


def _submission_bundle(store: Any, project_id: UUID, *, actor: str, node_id: str, task_id: str) -> dict[str, Any]:
    result = delivery.create_submission_bundle(store, project_id, actor=actor, actor_kind="member")
    return {"adapter": "submission_bundle", "status": "created", "detail": result}


ADAPTERS: dict[str, Callable[..., dict[str, Any]]] = {
    "paper_compile": _paper_compile,
    "submission_bundle": _submission_bundle,
}


def run_adapter(
    store: Any, project_id: UUID, kind: str, *, actor: str, node_id: str, task_id: str
) -> dict[str, Any]:
    """调用适配器并**如实**返回结果；异常在此收口成 failed 记账，绝不炸掉推进器。

    未知 kind → ``unknown_adapter``（不猜、不静默跳过——定义校验期就应挡住，
    运行期再遇到说明定义被绕过，如实报）。
    """

    adapter = ADAPTERS.get(str(kind))
    if adapter is None:
        return {"adapter": str(kind), "status": "failed", "error": f"unknown_adapter:{kind}"}
    try:
        return adapter(store, project_id, actor=actor, node_id=node_id, task_id=task_id)
    except Exception as error:  # noqa: BLE001 - 交付失败是记账事实，不是崩溃理由
        return {"adapter": str(kind), "status": "failed", "error": f"{error.__class__.__name__}: {error}"}
