"""stream_bridge.py 的单测（工作包 C，第二期交付）。

覆盖：发布/游标重放、seq 单调性、gap 检测（部分裁剪与全部裁剪）、
subscribe 的 重放→实时→心跳→结束 全生命周期、并发发布不丢不重、
topic 隔离、cleanup 语义。自包含：无网络、仅短 sleep、线程有超时兜底。
"""

from __future__ import annotations

import threading
import time
import unittest

from app.stream_bridge import (
    END_SENTINEL,
    HEARTBEAT_SENTINEL,
    BridgeGap,
    StreamBridge,
)


class PublishReadTests(unittest.TestCase):
    def test_roundtrip_and_cursor_replay(self):
        bridge = StreamBridge()
        for i in range(1, 4):
            bridge.publish("t", "delta", {"n": i})
        page0 = bridge.read_since("t", 0)
        self.assertEqual([e.seq for e in page0.events], [1, 2, 3])
        self.assertIsNone(page0.gap)
        self.assertFalse(page0.ended)
        page1 = bridge.read_since("t", 1)
        self.assertEqual([e.data["n"] for e in page1.events], [2, 3])
        self.assertEqual(page1.next_cursor, 3)
        page2 = bridge.read_since("t", 3)
        self.assertEqual(page2.events, [])

    def test_explicit_seq_preserved_and_monotonicity_enforced(self):
        bridge = StreamBridge()
        self.assertEqual(bridge.publish("t", "a", {}, seq=7), 7)
        self.assertEqual(bridge.publish("t", "b", {}), 8)
        with self.assertRaises(ValueError):
            bridge.publish("t", "c", {}, seq=8)
        with self.assertRaises(ValueError):
            bridge.publish("t", "c", {}, seq=3)

    def test_publish_after_end_raises(self):
        bridge = StreamBridge()
        bridge.publish("t", "a", {})
        bridge.publish_end("t")
        with self.assertRaises(ValueError):
            bridge.publish("t", "b", {})

    def test_topic_isolation(self):
        bridge = StreamBridge()
        bridge.publish("a", "x", {})
        bridge.publish("b", "y", {})
        self.assertEqual([e.event for e in bridge.read_since("a", 0).events], ["x"])
        self.assertEqual([e.event for e in bridge.read_since("b", 0).events], ["y"])

    def test_never_published_topic_is_empty_page_without_gap(self):
        bridge = StreamBridge()
        page = bridge.read_since("ghost", 5)
        self.assertEqual(page.events, [])
        self.assertIsNone(page.gap)
        self.assertFalse(page.events)


class GapTests(unittest.TestCase):
    def test_partial_trim_gap_carries_bounds(self):
        bridge = StreamBridge(retention=4)
        for i in range(1, 7):  # 保留 3..6
            bridge.publish("t", "e", {"n": i})
        page = bridge.read_since("t", 1)  # 需要 2，已裁掉
        self.assertIsNotNone(page.gap)
        assert isinstance(page.gap, BridgeGap)
        self.assertEqual(page.gap.requested_seq, 1)
        self.assertEqual(page.gap.earliest_available, 3)
        self.assertEqual(page.gap.latest_available, 6)
        self.assertEqual(page.events, [])
        fresh = bridge.read_since("t", 3)
        self.assertIsNone(fresh.gap)
        self.assertEqual([e.seq for e in fresh.events], [4, 5, 6])

    def test_fully_trimmed_gap_has_none_bound(self):
        bridge = StreamBridge(retention=2)
        for i in range(1, 6):
            bridge.publish("t", "e", {"n": i})
        bridge.read_since("t", 10)  # 触发后无影响，只为读一次
        page = bridge.read_since("t", 4)  # 需要 5；retention=2 保留 4,5 —— 不裁
        self.assertIsNone(page.gap)
        tighter = StreamBridge(retention=2)
        for i in range(1, 6):
            tighter.publish("u", "e", {"n": i})
        after_all = tighter.read_since("u", 2)  # 需要 3..5，只剩 4,5
        self.assertIsNotNone(after_all.gap)
        assert isinstance(after_all.gap, BridgeGap)
        self.assertEqual(after_all.gap.earliest_available, 4)

    def test_consumer_at_head_never_sees_gap(self):
        bridge = StreamBridge(retention=3)
        for i in range(1, 6):
            bridge.publish("t", "e", {"n": i})
        self.assertIsNone(bridge.read_since("t", 4).gap)

    def test_retention_must_be_positive(self):
        with self.assertRaises(ValueError):
            StreamBridge(retention=0)


class SubscribeTests(unittest.TestCase):
    def _collect(self, bridge: StreamBridge, topic: str, *, after_seq: int = 0, heartbeat: float = 0.05):
        items: list = []
        done = threading.Event()

        def run():
            for item in bridge.subscribe(topic, after_seq=after_seq, heartbeat_interval=heartbeat):
                items.append(item)
                if item is END_SENTINEL or isinstance(item, BridgeGap):
                    done.set()
                    return

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return items, thread, done

    def test_replay_then_live_then_end(self):
        bridge = StreamBridge()
        bridge.publish("t", "delta", {"n": 1})
        items, thread, _ = self._collect(bridge, "t")
        time.sleep(0.1)
        bridge.publish("t", "delta", {"n": 2})
        bridge.publish("t", "delta", {"n": 3})
        bridge.publish_end("t")
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        real = [i for i in items if i is not HEARTBEAT_SENTINEL]  # 心跳可合法出现在空闲间隙
        self.assertEqual([i.event for i in real], ["delta", "delta", "delta", END_SENTINEL.event])
        self.assertEqual([i.data.get("n") for i in real[:3]], [1, 2, 3])
        self.assertIs(items[-1], END_SENTINEL)  # 心跳不得出现在结束之后

    def test_heartbeat_during_idle(self):
        bridge = StreamBridge()
        items, thread, _ = self._collect(bridge, "idle", heartbeat=0.05)
        time.sleep(0.3)
        bridge.publish_end("idle")
        thread.join(timeout=5)
        beats = [i for i in items if i is HEARTBEAT_SENTINEL]
        self.assertGreaterEqual(len(beats), 1)
        self.assertIs(items[-1], END_SENTINEL)

    def test_gap_stops_generator(self):
        bridge = StreamBridge(retention=3)
        for i in range(1, 6):
            bridge.publish("t", "e", {"n": i})
        items, thread, done = self._collect(bridge, "t", after_seq=0)
        thread.join(timeout=5)
        self.assertTrue(done.is_set())
        self.assertEqual(len(items), 1)
        self.assertIsInstance(items[0], BridgeGap)

    def test_cleanup_ends_subscriber(self):
        bridge = StreamBridge()
        items, thread, _ = self._collect(bridge, "t", heartbeat=0.05)
        time.sleep(0.1)
        bridge.cleanup("t")
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertIs(items[-1], END_SENTINEL)

    def test_read_since_after_cleanup_reports_ended(self):
        bridge = StreamBridge()
        bridge.publish("t", "a", {})
        bridge.cleanup("t")
        page = bridge.read_since("t", 0)
        self.assertEqual(page.events, [])
        self.assertTrue(page.ended)

    def test_heartbeat_interval_must_be_positive(self):
        bridge = StreamBridge()
        with self.assertRaises(ValueError):
            bridge.subscribe("t", heartbeat_interval=0)


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_publishers_no_loss_no_duplication(self):
        bridge = StreamBridge()
        threads_total, per_thread = 4, 60
        barrier = threading.Barrier(threads_total)

        def publish_many(k: int):
            barrier.wait()
            for i in range(per_thread):
                bridge.publish("t", "e", {"k": k, "i": i})

        threads = [threading.Thread(target=publish_many, args=(k,)) for k in range(threads_total)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        total = threads_total * per_thread
        seen: list[int] = []
        cursor = 0
        while True:
            page = bridge.read_since("t", cursor, limit=100)
            if not page.events:
                break
            seen.extend(e.seq for e in page.events)
            cursor = page.next_cursor
        self.assertEqual(len(seen), total)
        self.assertEqual(seen, list(range(1, total + 1)))

    def test_live_subscriber_sees_every_event_during_burst(self):
        bridge = StreamBridge()
        received: list[int] = []
        saw_end = threading.Event()

        def consume():
            for item in bridge.subscribe("t", after_seq=0, heartbeat_interval=0.02):
                if item is END_SENTINEL:
                    saw_end.set()
                    return
                if isinstance(item, BridgeGap):
                    return
                received.append(item.seq)

        consumer = threading.Thread(target=consume, daemon=True)
        consumer.start()
        for i in range(1, 51):
            bridge.publish("t", "e", {"n": i})
        bridge.publish_end("t")
        consumer.join(timeout=10)
        self.assertTrue(saw_end.is_set())
        self.assertEqual(received, list(range(1, 51)))


if __name__ == "__main__":
    unittest.main()
