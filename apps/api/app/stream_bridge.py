"""内存实时桥（实施计划 W1.3/W2.1 支撑件，工作包 C）。

StreamBridge 把"事件生产者（服务/执行体回报）"与"SSE 消费端"解耦：
事件照旧以 `events` / `event_outbox` / `agent_turn_events` 为权威，
桥只是**加速器**——断线重连走 `after=<seq>` 从桥重放，桥里已经不在了
（被保留上限裁掉）就给 ``BridgeGap``，消费方回权威存储重同步。
**绝不把部分回放冒充完整回放**（deer-flow ``StreamGap`` 语义）。

抽象对齐 deer-flow ``runtime/stream_bridge/base.py``：publish / publish_end /
subscribe(游标, 心跳, 结束哨兵, gap)。差异：本实现是**单进程同步**版
（threading.Condition，FastAPI 线程池直接可用），不做 Redis、不做跨进程；
seq 游标与事件契约一致（``docs/AGENT_EVENT_CONTRACT.md`` 规则 4）。

线程安全：所有方法可从任意线程调用；``publish`` 不阻塞（满则裁最旧，
消费方以 gap 察觉）。多进程部署时换成 Redis 实现再谈，接口不变。

topic 约定（软约束，纯字符串）::

    turn:{turn_id}          单轮流式（W1.3）
    conversation:{id}       会话全量流
    project:{id}            项目协作流（W2.3 团队视图）
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Iterator, Mapping

__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL",
    "DEFAULT_RETENTION",
    "BridgeEvent",
    "BridgeGap",
    "HEARTBEAT_SENTINEL",
    "END_SENTINEL",
    "StreamItem",
    "BridgePage",
    "StreamBridge",
]

DEFAULT_HEARTBEAT_INTERVAL = 15.0
DEFAULT_RETENTION = 10_000

HEARTBEAT_EVENT = "__heartbeat__"
END_EVENT = "__end__"


@dataclass(frozen=True)
class BridgeEvent:
    """单条事件。``seq`` 即 SSE 的 ``id:`` 帧（契约规则 4：单调递增）。"""

    seq: int
    event: str
    data: Mapping


@dataclass(frozen=True)
class BridgeGap:
    """订阅游标已无法完整重放——消费方必须回权威存储重同步。"""

    requested_seq: int
    earliest_available: int | None  # 桥里还留着的最早 seq；桥空时为 None
    latest_available: int | None


HEARTBEAT_SENTINEL = BridgeEvent(seq=0, event=HEARTBEAT_EVENT, data={})
END_SENTINEL = BridgeEvent(seq=0, event=END_EVENT, data={})

#: subscribe() 可能产出的项：真事件 / gap / 心跳 / 结束。
StreamItem = BridgeEvent

#: BridgeGap 用独立类型承载（subscribe 产出 BridgeGap 后立即停止）。
GapItem = BridgeGap


@dataclass(frozen=True)
class BridgePage:
    """``read_since`` 的一页结果（非阻塞、一次性）。"""

    events: list[BridgeEvent]
    next_cursor: int  # 消费成功后应保存的游标
    gap: BridgeGap | None
    ended: bool  # 生产者已宣布结束或 topic 已被清理


@dataclass
class _Topic:
    events: deque[BridgeEvent]
    last_seq: int = 0
    ever_published: bool = False
    ended: bool = False
    trimmed_any: bool = False


class StreamBridge:
    """单进程内存桥。所有方法线程安全。"""

    def __init__(self, *, retention: int = DEFAULT_RETENTION) -> None:
        if retention < 1:
            raise ValueError("retention must be >= 1")
        self._retention = retention
        self._cond = threading.Condition()
        self._topics: dict[str, _Topic] = {}

    # ---- 生产侧 ----

    def publish(self, topic: str, event: str, data: Mapping, *, seq: int | None = None) -> int:
        """发布一条事件，返回其 seq。

        ``seq=None`` 由桥按 topic 自增分配；显式传 seq 时必须严格大于该 topic
        已有最大 seq（契约规定权威流无空洞，桥镜像调用方的 seq）。
        topic 已 ``publish_end`` 后再 publish 是调用方 bug，直接抛错。
        """
        if not topic:
            raise ValueError("topic must be non-empty")
        with self._cond:
            t = self._topics.setdefault(topic, _Topic(events=deque()))
            if t.ended:
                raise ValueError(f"topic {topic!r} already ended")
            if seq is None:
                seq = t.last_seq + 1
            elif seq <= t.last_seq:
                raise ValueError(f"seq must be strictly increasing on topic {topic!r}: got {seq}, last {t.last_seq}")
            t.events.append(BridgeEvent(seq=seq, event=event, data=data))
            # 手工裁剪（不用 deque(maxlen=)：自动裁剪会让 trimmed_any 记账失效，
            # 消费方就永远看不到 gap、把残缺回放当完整回放）。
            while len(t.events) > self._retention:
                t.events.popleft()
                t.trimmed_any = True
            t.last_seq = max(t.last_seq, seq)
            t.ever_published = True
            self._cond.notify_all()
            return seq

    def publish_end(self, topic: str) -> None:
        """宣布不再有事件。此后 subscribe 会收到 END_SENTINEL 并停止。"""
        with self._cond:
            t = self._topics.setdefault(topic, _Topic(events=deque()))
            t.ended = True
            self._cond.notify_all()

    def cleanup(self, topic: str) -> None:
        """清理 topic：事件内存立即释放，topic 留下 ended 墓碑——
        阻塞中的订阅者收到 END_SENTINEL，需要历史的消费方回权威存储
        （桥不装它不知道的事）。墓碑与"从未发布"是两回事：后者是
        还没开始（继续等），前者是已经被清走（别再等了）。"""
        with self._cond:
            self._topics[topic] = _Topic(events=deque(), ended=True)
            self._cond.notify_all()

    # ---- 消费侧 ----

    def read_since(self, topic: str, after_seq: int, *, limit: int = 500) -> BridgePage:
        """非阻塞读一页：seq 严格大于 after_seq 的事件（最多 limit 条）。

        游标落后于保留窗口时返回 ``gap``（含可回放边界），事件照常返回
        已保留部分——消费方拿 gap 后必须重同步，不得当完整回放用。
        topic 从未发布 → 空页、无 gap（是"还没开始"，不是"错过了"）。
        topic 已被 cleanup → 空页、ended=True。
        """
        with self._cond:
            return self._read_locked(topic, after_seq, limit=limit)

    def subscribe(
        self,
        topic: str,
        *,
        after_seq: int = 0,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
        limit: int = 200,
    ) -> Iterator[StreamItem]:
        """阻塞式订阅生成器：先重放，再实时，空闲发心跳，结束发哨兵。

        产出顺序：重放事件 → 实时事件 →（空闲超过 heartbeat_interval 时）
        HEARTBEAT_SENTINEL → publish_end/cleanup 后 END_SENTINEL 并停止；
        落后到无法完整重放时产出 BridgeGap 并立即停止。
        SSE 端可直接在 threadpool 里迭代本生成器；async 处理器用
        ``anyio.to_thread`` 包一层即可。
        生成器挂起（yield）时**绝不持锁**——锁只在取页快照和等待时持有。
        """
        if heartbeat_interval <= 0:
            raise ValueError("heartbeat_interval must be positive")
        if limit < 1:
            raise ValueError("limit must be >= 1")
        return self._subscribe_iter(topic, after_seq=after_seq, heartbeat_interval=heartbeat_interval, limit=limit)

    def _subscribe_iter(
        self,
        topic: str,
        *,
        after_seq: int,
        heartbeat_interval: float,
        limit: int,
    ) -> Iterator[StreamItem]:
        cursor = after_seq
        while True:
            idle = False
            woke = False
            with self._cond:
                page = self._read_locked(topic, cursor, limit=limit)
                idle = not page.events and page.gap is None and not page.ended
                if idle:
                    woke = self._cond.wait(timeout=heartbeat_interval)
            # —— 以下全部在锁外 ——
            if idle:
                if woke:
                    continue  # 有新事件/结束信号/清理，重读一页
                yield HEARTBEAT_SENTINEL
                continue
            if page.gap is not None:
                yield page.gap
                return
            for item in page.events:
                cursor = item.seq
                yield item
            if page.ended:
                yield END_SENTINEL
                return

    # ---- 内部 ----

    def _read_locked(self, topic: str, after_seq: int, *, limit: int) -> BridgePage:
        """锁内版 read_since（调用方必须已持有 self._cond）。"""
        t = self._topics.get(topic)
        if t is None:
            # topic 从未发布：空页、无 gap、未结束——是"还没开始"，
            # 订阅者应当继续等实时事件，而不是被当成结束。
            return BridgePage(events=[], next_cursor=after_seq, gap=None, ended=False)
        earliest = t.events[0].seq if t.events else None
        latest = t.events[-1].seq if t.events else None
        gap: BridgeGap | None = None
        needed = after_seq + 1
        if t.trimmed_any and earliest is not None and needed < earliest:
            gap = BridgeGap(requested_seq=after_seq, earliest_available=earliest, latest_available=latest)
        elif t.trimmed_any and earliest is None and needed <= t.last_seq:
            gap = BridgeGap(requested_seq=after_seq, earliest_available=None, latest_available=t.last_seq)
        events = [e for e in t.events if e.seq > after_seq][:limit] if gap is None else []
        next_cursor = events[-1].seq if events else after_seq
        return BridgePage(events=events, next_cursor=next_cursor, gap=gap, ended=t.ended)
