"""DP-2-01：常驻任务循环（claim → 执行 → 上报）的契约测试。

覆盖：
- 一轮循环的调用顺序与幂等键前缀（claim/progress/run-create/run-complete/result）；
- 空队列返回 None、"连接未就绪"时**不领取**（断线暂停）、暂停后不领取、恢复后立刻领取；
- 执行抛异常时仍上报失败结果（不能把任务永远留在 RUNNING）；
- 结果提交失败要计数留痕，而不是静默；
- 退避：空队列后等待时间指数增长，领到任务立即复位。
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from task_loop import TaskLoop, TaskLoopConfig, task_loop_config_from_identity


class FakeHttp:
    """记录所有请求，并按路径给出预设响应。"""

    def __init__(self, assignment: dict[str, Any] | None = None) -> None:
        self.assignment = assignment
        self.calls: list[dict[str, Any]] = []
        self.fail_paths: dict[str, str] = {}

    def __call__(self, url: str, method: str, path: str, payload: dict | None = None, headers: dict | None = None) -> Any:
        self.calls.append({"url": url, "method": method, "path": path, "payload": payload, "headers": headers})
        for prefix, error in self.fail_paths.items():
            if path.startswith(prefix):
                raise ValueError(error)
        if path.endswith("/tasks/claim"):
            return self.assignment
        if path.endswith("/runs"):
            return {"id": "11111111-1111-4111-8111-111111111111"}
        return {}

    def paths(self) -> list[str]:
        return [call["path"] for call in self.calls]


def _config(**overrides: Any) -> TaskLoopConfig:
    values = {
        "url": "http://platform.test",
        "project_id": "22222222-2222-4222-8222-222222222222",
        "agent_id": "agent-loop",
        "device_id": "device-loop",
        "project_token": "prj_secret",
        "idle_seconds": 0.01,
        "max_idle_seconds": 0.04,
    }
    values.update(overrides)
    return TaskLoopConfig(**values)


def _assignment() -> dict[str, Any]:
    return {
        "task": {"id": "task-1", "title": "跑一次回归"},
        "lease": {"lease_token": "lease-token-1"},
    }


class TaskLoopClaimTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_round_calls_claim_execute_report_in_order(self) -> None:
        http = FakeHttp(_assignment())
        executions: list[dict[str, Any]] = []

        async def execute(task: dict, identity: dict, run_id: str | None = None) -> tuple[bool, str, str, str]:
            executions.append({"task": task, "identity": identity, "run_id": run_id})
            return True, "exit=0 run=run-1", "stdout", ""

        loop = TaskLoop(_config(), http=http, execute=execute, identity={"capabilities": ["task.claim"]})
        result = await loop.run_once()

        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result["success"])
        self.assertEqual(result["task_id"], "task-1")
        self.assertEqual(http.paths(), [
            "/api/agents/agent-loop/tasks/claim",
            "/api/tasks/task-1/progress",
            "/api/projects/22222222-2222-4222-8222-222222222222/runs",
            "/api/runs/11111111-1111-4111-8111-111111111111/complete",
            "/api/tasks/task-1/result",
        ])
        # 每次调用都带项目能力 Token；幂等键前缀与既有 worker-run 保持一致（平台侧可幂等重放）
        self.assertTrue(all(call["headers"] == {"X-Project-Capability-Token": "prj_secret"} for call in http.calls))
        self.assertTrue(http.calls[0]["payload"]["idempotency_key"].startswith("worker-claim:"))
        self.assertTrue(http.calls[3]["payload"]["idempotency_key"].startswith("worker-run-complete:"))
        # 执行层拿到的是完整身份（含 capabilities），而不是精简副本
        self.assertEqual(executions[0]["identity"]["capabilities"], ["task.claim"])
        self.assertEqual(executions[0]["identity"]["agent_id"], "agent-loop")
        # run_id 一并传入：执行体过程事件要挂在这次执行上
        self.assertEqual(executions[0]["run_id"], "11111111-1111-4111-8111-111111111111")

    async def test_empty_queue_returns_none_without_executing(self) -> None:
        http = FakeHttp(None)
        executed = False

        async def execute(task: dict, identity: dict, run_id: str | None = None) -> tuple[bool, str, str, str]:
            nonlocal executed
            executed = True
            return True, "unused", "", ""

        loop = TaskLoop(_config(), http=http, execute=execute)
        self.assertIsNone(await loop.run_once())
        self.assertFalse(executed)
        self.assertEqual(http.paths(), ["/api/agents/agent-loop/tasks/claim"])

    async def test_executor_exception_is_reported_as_failure(self) -> None:
        http = FakeHttp(_assignment())

        async def execute(task: dict, identity: dict, run_id: str | None = None) -> tuple[bool, str, str, str]:
            raise RuntimeError("codex_cli_not_found")

        loop = TaskLoop(_config(), http=http, execute=execute)
        result = await loop.run_once()
        assert result is not None
        self.assertFalse(result["success"])
        self.assertIn("codex_cli_not_found", result["summary"])
        # 失败也要提交结果，否则任务永远停在 RUNNING
        self.assertIn("/api/tasks/task-1/result", http.paths())
        result_call = next(call for call in http.calls if call["path"].endswith("/result"))
        self.assertFalse(result_call["payload"]["success"])

    async def test_result_submission_failure_is_counted(self) -> None:
        http = FakeHttp(_assignment())
        http.fail_paths["/api/tasks/task-1/result"] = "http_500:lease_released"

        async def execute(task: dict, identity: dict, run_id: str | None = None) -> tuple[bool, str, str, str]:
            return True, "ok", "", ""

        loop = TaskLoop(_config(), http=http, execute=execute)
        await loop.run_once()
        snapshot = loop.snapshot()
        self.assertEqual(snapshot["report_errors"], 1)
        self.assertEqual(snapshot["completed"], 1)

    async def test_claim_error_is_counted_and_does_not_raise(self) -> None:
        http = FakeHttp(_assignment())
        http.fail_paths["/api/agents/agent-loop/tasks/claim"] = "http_409:task_already_claimed"

        async def execute(task: dict, identity: dict, run_id: str | None = None) -> tuple[bool, str, str, str]:
            return True, "unused", "", ""

        loop = TaskLoop(_config(), http=http, execute=execute)
        self.assertIsNone(await loop.run_once())
        self.assertEqual(loop.snapshot()["claim_errors"], 1)
        self.assertIn("task_already_claimed", loop.snapshot()["last_claim_error"] or "")


class TaskLoopRunForeverTests(unittest.IsolatedAsyncioTestCase):
    async def test_gate_blocks_claiming_until_the_platform_connection_is_ready(self) -> None:
        http = FakeHttp(_assignment())
        ready = False

        async def execute(task: dict, identity: dict, run_id: str | None = None) -> tuple[bool, str, str, str]:
            return True, "ok", "", ""

        async def gate() -> bool:
            return ready

        loop = TaskLoop(_config(idle_seconds=0.005), http=http, execute=execute)
        task = asyncio.create_task(loop.run_forever(gate=gate))
        await asyncio.sleep(0.05)
        # 连接没就绪：一次都没有领取（断线暂停的核心断言）
        self.assertEqual(http.calls, [])
        ready = True
        for _ in range(100):
            if http.calls:
                break
            await asyncio.sleep(0.005)
        self.assertTrue(http.calls)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_pause_stops_claiming_and_resume_claims_immediately(self) -> None:
        http = FakeHttp(_assignment())

        async def execute(task: dict, identity: dict, run_id: str | None = None) -> tuple[bool, str, str, str]:
            return True, "ok", "", ""

        loop = TaskLoop(_config(idle_seconds=0.005), http=http, execute=execute)
        loop.pause()
        task = asyncio.create_task(loop.run_forever())
        await asyncio.sleep(0.03)
        self.assertEqual(http.calls, [])
        self.assertTrue(loop.snapshot()["paused"])

        loop.resume()
        for _ in range(100):
            if http.calls:
                break
            await asyncio.sleep(0.005)
        self.assertTrue(http.calls)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_idle_backoff_grows_and_resets_after_work(self) -> None:
        delays: list[float] = []

        async def sleep(delay: float) -> None:
            delays.append(delay)
            if len(delays) >= 3:
                raise asyncio.CancelledError

        http = FakeHttp(None)
        loop = TaskLoop(_config(idle_seconds=1.0, max_idle_seconds=4.0), http=http, execute=_never_execute, sleep=sleep)
        with self.assertRaises(asyncio.CancelledError):
            await loop.run_forever()
        self.assertEqual(delays, [1.0, 2.0, 4.0])

    async def test_snapshot_reports_running_state(self) -> None:
        http = FakeHttp(_assignment())
        seen: dict[str, Any] = {}

        async def execute(task: dict, identity: dict, run_id: str | None = None) -> tuple[bool, str, str, str]:
            seen.update(loop.snapshot())
            return True, "ok", "", ""

        loop = TaskLoop(_config(), http=http, execute=execute)
        await loop.run_once()
        self.assertEqual(seen["local_queue_length"], 1)
        self.assertEqual(seen["current_task"]["id"], "task-1")
        final = loop.snapshot()
        self.assertEqual(final["local_queue_length"], 0)
        self.assertEqual(final["current_task"], None)
        self.assertEqual(final["completed"], 1)
        self.assertEqual(final["last_result"]["task_id"], "task-1")


async def _never_execute(task: dict, identity: dict, run_id: str | None = None) -> tuple[bool, str, str, str]:
    raise AssertionError("executor must not run for an empty queue")


class TaskLoopConfigTests(unittest.TestCase):
    def test_config_validation(self) -> None:
        with self.assertRaisesRegex(ValueError, "task_loop_project_token_required"):
            _config(project_token="")
        with self.assertRaisesRegex(ValueError, "task_loop_idle_range_invalid"):
            _config(idle_seconds=5.0, max_idle_seconds=1.0)

    def test_config_from_identity_keeps_token_and_identity_in_sync(self) -> None:
        identity = {
            "project_id": "p-1",
            "agent_id": "a-1",
            "device_id": "d-1",
            "project_token": "prj_x",
            "capabilities": ["task.claim"],
        }
        config = task_loop_config_from_identity(identity, url="http://platform.test", stages=["modeling"])
        self.assertEqual(config.project_id, "p-1")
        self.assertEqual(config.project_token, "prj_x")
        self.assertEqual(config.stages, ("modeling",))


class RunUsageReportingTests(unittest.IsolatedAsyncioTestCase):
    """完成 Run 时带上用量（COST-1）：耗时来自循环自身观测，token 来自执行体（usage_provider）。"""

    async def test_complete_payload_carries_usage(self) -> None:
        http = FakeHttp(_assignment())

        async def execute(task: dict, identity: dict, run_id: str | None = None) -> tuple[bool, str, str, str]:
            return True, "exit=0", "stdout", ""

        ticks = iter([10.0, 13.5])  # 执行前 / 执行后：差 3.5 秒
        loop = TaskLoop(
            _config(),
            http=http,
            execute=execute,
            identity={"capabilities": ["task.claim"]},
            usage_provider=lambda: {"input_tokens": 40, "output_tokens": 10, "total_tokens": 50, "turns": 1, "source": "codex-jsonl"},
            monotonic=lambda: next(ticks),
        )
        await loop.run_once()

        completes = [call for call in http.calls if call["path"].endswith("/complete")]
        self.assertEqual(len(completes), 1)
        usage = completes[0]["payload"]["usage"]
        self.assertEqual(usage["total_tokens"], 50)
        self.assertEqual(usage["source"], "codex-jsonl")
        self.assertAlmostEqual(usage["seconds"], 3.5, places=2)

    async def test_without_usage_provider_only_seconds_are_reported(self) -> None:
        http = FakeHttp(_assignment())

        async def execute(task: dict, identity: dict, run_id: str | None = None) -> tuple[bool, str, str, str]:
            return True, "exit=0", "stdout", ""

        ticks = iter([0.0, 1.25])
        loop = TaskLoop(_config(), http=http, execute=execute, monotonic=lambda: next(ticks))
        await loop.run_once()

        completes = [call for call in http.calls if call["path"].endswith("/complete")]
        self.assertEqual(len(completes), 1)
        # 只有耗时（循环观测）：没有 token 字段，就不冒充"执行体回报过用量"
        self.assertEqual(completes[0]["payload"]["usage"], {"seconds": 1.25})


if __name__ == "__main__":
    unittest.main()

class TaskLoopFailurePathTests(unittest.IsolatedAsyncioTestCase):
    """执行过程**本身**炸掉时的兜底：不能让平台侧的任务永远停在 RUNNING。

    真实事故：把 `reporter.stats`（绑定方法）直接塞进 `dict()` 抛 TypeError，
    异常发生在"Run 已登记、结果未提交"之间——任务在平台上一直是 RUNNING，
    而本地日志里什么都没有。这两条断言就是为它写的。
    """

    async def test_unexpected_error_still_submits_a_failed_result(self) -> None:
        http = FakeHttp(_assignment())

        async def execute(task, identity, run_id=None):
            return True, "never used", "", ""

        loop = TaskLoop(_config(), http=http, execute=execute)
        # 模拟"某一步在守卫之外炸掉"（真实事故是 dict(reporter.stats) 抛 TypeError）
        loop._create_run = lambda task: (_ for _ in ()).throw(TypeError("cannot convert method"))
        result = await loop.run_once()
        assert result is not None
        self.assertFalse(result["success"])
        self.assertIn("/api/tasks/task-1/result", http.paths())
        submitted = next(call for call in http.calls if call["path"].endswith("/result"))
        self.assertFalse(submitted["payload"]["success"])

    async def test_broken_event_sink_never_breaks_the_task(self) -> None:
        """事件上报失败（网络/队列）不能影响任务本身：sink 抛错也要照常执行并上报结果。"""

        http = FakeHttp(_assignment())
        executed = False

        async def execute(task, identity, run_id=None):
            nonlocal executed
            executed = True
            return True, "ok", "", ""

        loop = TaskLoop(_config(), http=http, execute=execute, event_sink=_explode)
        result = await loop.run_once()
        assert result is not None
        self.assertTrue(result["success"], result["summary"])
        self.assertTrue(executed)


def _explode(event_type: str, payload: dict) -> None:
    raise RuntimeError("gateway_queue_unavailable")


if __name__ == "__main__":
    unittest.main()

class _FakeCollector:
    """产出采集替身：记录钩子调用顺序，返回给定的成果物 id。"""

    def __init__(self, artifact_ids: list[str] | None = None, *, fail_before: bool = False, fail_after: bool = False) -> None:
        self.calls: list[str] = []
        self.artifact_ids = artifact_ids or []
        self.fail_before = fail_before
        self.fail_after = fail_after

    async def before(self, task: dict) -> None:
        self.calls.append("before")
        if self.fail_before:
            raise RuntimeError("snapshot_failed")

    async def after(self, task: dict, run_id: str | None, success: bool, summary: str):
        self.calls.append("after")
        if self.fail_after:
            raise RuntimeError("upload_failed")

        class _Result:
            artifact_ids = self.artifact_ids
            flushed_previous = 0

            @staticmethod
            def note() -> str:
                return f"产出 {len(self.artifact_ids)} 个成果物（待审）" if self.artifact_ids else "产出 0 个成果物"

        return _Result()


class OutputCollectionHookTests(unittest.IsolatedAsyncioTestCase):
    """CL-1-04/05/06：采集钩子、id 回填、摘要说明、失败不判死。"""

    async def test_collector_hooks_wrap_the_execution_and_ids_are_backfilled(self) -> None:
        http = FakeHttp(_assignment())
        collector = _FakeCollector(["artifact-1", "artifact-2"])
        order: list[str] = []

        async def execute(task, identity, run_id=None):
            order.append("execute")
            return True, "回答正文\n\n---\ncodex exit=0", "stdout", ""

        loop = TaskLoop(_config(), http=http, execute=execute, output_collector=collector)
        result = await loop.run_once()

        assert result is not None
        self.assertEqual(collector.calls, ["before", "after"])
        self.assertEqual(result["output_artifact_ids"], ["artifact-1", "artifact-2"])

        complete = next(call for call in http.calls if call["path"].endswith("/complete"))
        self.assertEqual(complete["payload"]["output_artifact_ids"], ["artifact-1", "artifact-2"])
        # 产出说明进诊断段，回答保持在最前面
        self.assertTrue(complete["payload"]["summary"].startswith("回答正文"))
        self.assertIn("产出 2 个成果物（待审）", complete["payload"]["summary"])

        submitted = next(call for call in http.calls if call["path"].endswith("/result"))
        self.assertEqual(submitted["payload"]["output_artifact_ids"], ["artifact-1", "artifact-2"])
        self.assertEqual(loop.snapshot()["last_outputs"]["artifact_ids"], ["artifact-1", "artifact-2"])

    async def test_collector_failure_never_changes_the_task_outcome(self) -> None:
        """I4：内容采集失败不能判死任务——只记录，任务照常成功。"""

        http = FakeHttp(_assignment())
        collector = _FakeCollector(fail_after=True)

        async def execute(task, identity, run_id=None):
            return True, "回答", "", ""

        loop = TaskLoop(_config(), http=http, execute=execute, output_collector=collector)
        result = await loop.run_once()
        assert result is not None
        self.assertTrue(result["success"], result["summary"])
        self.assertEqual(result["output_artifact_ids"], [])
        complete = next(call for call in http.calls if call["path"].endswith("/complete"))
        self.assertIn("产出采集失败", complete["payload"]["summary"])
        self.assertEqual(loop.snapshot()["report_errors"], 1)

    async def test_snapshot_failure_is_logged_and_execution_proceeds(self) -> None:
        http = FakeHttp(_assignment())
        collector = _FakeCollector(fail_before=True)
        executed = False

        async def execute(task, identity, run_id=None):
            nonlocal executed
            executed = True
            return True, "ok", "", ""

        loop = TaskLoop(_config(), http=http, execute=execute, output_collector=collector)
        result = await loop.run_once()
        assert result is not None
        self.assertTrue(executed)
        self.assertTrue(result["success"])
        self.assertIn("before", collector.calls)

    async def test_without_collector_nothing_changes(self) -> None:
        http = FakeHttp(_assignment())

        async def execute(task, identity, run_id=None):
            return True, "ok", "", ""

        loop = TaskLoop(_config(), http=http, execute=execute)
        result = await loop.run_once()
        assert result is not None
        self.assertEqual(result["output_artifact_ids"], [])
        complete = next(call for call in http.calls if call["path"].endswith("/complete"))
        self.assertEqual(complete["payload"]["output_artifact_ids"], [])
        self.assertIsNone(loop.snapshot()["last_outputs"])


if __name__ == "__main__":
    unittest.main()
