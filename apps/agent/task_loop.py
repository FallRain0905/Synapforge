"""常驻任务循环：claim → 执行 → 上报（DP-2-01）。

与 `agentd.worker_run` 的关系：`worker-run` 是**前台调试入口**（单项目、把过程打到终端），
本模块是同一套动作的可复用实现，供桌面端内核的 `daemon-run` 在连接就绪时驱动。
执行动作本身由调用方注入（`agentd._execute_task`），因此不存在"两份执行逻辑各自漂移"。

设计要点：

- **断线暂停**：`gate` 返回 False 时不领取任务（连接没恢复就领取，只会在上报阶段失败）；
- **退避**：空队列从 `idle_seconds` 指数退避到 `max_idle_seconds`，领到任务立刻复位；
- **单任务串行**：同一时刻只执行一个任务（决策 DE12：不做多任务并发）；
- **上报失败不吞**：Run 登记/完成上报失败只记录，任务结果仍要提交（平台侧不能永远停在 RUNNING）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence
from uuid import uuid4

HttpCallable = Callable[..., Any]
# 执行回调：(task, identity, run_id) -> (success, summary, stdout, stderr)。
# run_id 是平台侧登记的执行 id，执行体事件要挂在它上面平台才能把过程归到那次执行。
ExecuteCallable = Callable[[dict, dict, str | None], Awaitable[tuple[bool, str, str, str]]]
LogCallable = Callable[[str], None]
# 执行期事件出口：`(event_type, payload) -> None`，由常驻体接到 Gateway 的持久化事件队列上。
# 不传就只有状态变更（claim/progress/result），执行体内的过程不上报。
EventSink = Callable[[str, dict[str, Any]], None]


# 产出采集器（`output_collector.OutputCollector`）：`before` 记快照、`after` 上传并返回成果物 id。
# 用协议而不是直接依赖具体类：任务循环不该知道 HTTP、对象存储与本地上传队列的存在。
class OutputCollectorProtocol(Protocol):
    async def before(self, task: dict) -> None: ...

    async def after(self, task: dict, run_id: str | None, success: bool, summary: str) -> Any: ...


@dataclass(frozen=True)
class TaskLoopConfig:
    """任务循环的连接与身份参数。Token 只留在内存里，不落日志。"""

    url: str
    project_id: str
    agent_id: str
    device_id: str
    project_token: str
    stages: tuple[str, ...] = ()
    lease_seconds: int = 900
    idle_seconds: float = 5.0
    max_idle_seconds: float = 120.0

    def __post_init__(self) -> None:
        for field_name in ("url", "project_id", "agent_id", "device_id", "project_token"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"task_loop_{field_name}_required")
        if self.lease_seconds < 1:
            raise ValueError("task_loop_lease_seconds_invalid")
        if self.idle_seconds <= 0 or self.max_idle_seconds <= 0:
            raise ValueError("task_loop_idle_interval_invalid")
        if self.idle_seconds > self.max_idle_seconds:
            raise ValueError("task_loop_idle_range_invalid")


@dataclass
class TaskLoopStats:
    claimed: int = 0
    completed: int = 0
    failed: int = 0
    claim_errors: int = 0
    report_errors: int = 0
    last_result: dict[str, Any] | None = None
    last_claim_error: str | None = None
    last_outputs: dict[str, Any] | None = None


class TaskLoop:
    """单项目、串行的任务循环。`http` 与 `execute` 都从外部注入，便于测试。"""

    def __init__(
        self,
        config: TaskLoopConfig,
        *,
        http: HttpCallable,
        execute: ExecuteCallable,
        identity: Mapping[str, Any] | None = None,
        executor_stats: Callable[[], dict[str, int]] | None = None,
        usage_provider: Callable[[], dict[str, Any] | None] | None = None,
        output_collector: OutputCollectorProtocol | None = None,
        log: LogCallable | None = None,
        event_sink: EventSink | None = None,
        label: str = "task-loop",
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._http = http
        self._execute = execute
        # 执行层需要的完整身份（capabilities 等）由调用方给；HTTP 头只用 config 里的 Token。
        self._identity_extra = dict(identity or {})
        self._event_sink = event_sink
        self._executor_stats = executor_stats
        # 执行体侧的 token 用量（COST-1）：只有执行期解析了 JSONL 的执行体才有；
        # 耗时（seconds）不靠它——那是任务循环自己观测的，更可信。
        self._usage_provider = usage_provider
        self._outputs = output_collector
        self._label = label
        self._log = log or (lambda message: None)
        self._sleep = sleep
        self._monotonic = monotonic
        self._paused = False
        self._current: dict[str, Any] | None = None
        self._running_run_ids: set[str] = set()
        self._stats = TaskLoopStats()
        self._idle = config.idle_seconds

    # ---- 壳可见的状态 ---------------------------------------------------

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def busy(self) -> bool:
        return self._current is not None

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def snapshot(self) -> dict[str, Any]:
        """供心跳与本地状态页读取：在跑什么、排了多长、上次结果如何。"""

        return {
            "paused": self._paused,
            "claimed": self._stats.claimed,
            "completed": self._stats.completed,
            "failed": self._stats.failed,
            "claim_errors": self._stats.claim_errors,
            "report_errors": self._stats.report_errors,
            "current_task": self._current,
            "running_run_ids": sorted(self._running_run_ids),
            # 本机队列长度：串行执行下就是"当前在跑的那个"（0 或 1）。
            "local_queue_length": 1 if self._current is not None else 0,
            "last_result": self._stats.last_result,
            "last_claim_error": self._stats.last_claim_error,
            "last_outputs": self._stats.last_outputs,
        }

    # ---- 主循环 ---------------------------------------------------------

    async def run_forever(
        self,
        *,
        stop: asyncio.Event | None = None,
        gate: Callable[[], Awaitable[bool]] | None = None,
    ) -> TaskLoopStats:
        """一直领任务直到 `stop` 置位。

        `gate()` 为 False（例如平台连接未就绪）时只等待不领取；暂停由 `pause()` 控制，
        两者都不退出循环——常驻体不应因为一次断线就结束。
        """

        self._idle = self.config.idle_seconds
        while stop is None or not stop.is_set():
            if self._paused:
                await self._sleep(self.config.idle_seconds)
                continue
            if gate is not None and not await gate():
                await self._sleep(self.config.idle_seconds)
                continue
            try:
                result = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - 循环不能被单次异常终止
                self._stats.claim_errors += 1
                self._stats.last_claim_error = f"{type(error).__name__}:{error}"
                self._log(f"[{self._label}] 领取失败：{self._stats.last_claim_error}")
                result = None
            if result is None:
                await self._sleep(self._idle)
                self._idle = min(self._idle * 2, self.config.max_idle_seconds)
            else:
                self._idle = self.config.idle_seconds
                # 显式让出控制权：claim→执行→上报若是同步完成的（例如本地命令瞬间返回），
                # 这一轮里没有任何真正的挂起点，会把同一条事件循环上的心跳与控制任务饿死。
                await asyncio.sleep(0)
        return self._stats

    async def run_once(self) -> dict[str, Any] | None:
        """一轮 claim→执行→上报。队列为空返回 None（区分"没活"与"干完了"）。"""

        assignment = self._claim()
        if not assignment:
            return None
        task = assignment.get("task") or {}
        lease_token = str((assignment.get("lease") or {}).get("lease_token") or "")
        task_id = str(task.get("id") or "")
        self._current = {"id": task_id, "title": task.get("title", ""), "started_at": time.time()}
        try:
            result = await self._run_assignment(task, lease_token)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 任何意外都要变成一条失败结果，而不是打断循环
            summary = f"task_loop_error:{type(error).__name__}:{error}"
            self._log(f"[{self._label}] 执行过程异常：{summary}")
            # 兜底提交：客户端自己的 bug 不能让平台侧的任务永远停在 RUNNING
            self._report_failure(task_id, lease_token, summary)
            result = {"task_id": task_id, "run_id": None, "success": False, "summary": summary}
        finally:
            self._current = None
        self._stats.claimed += 1
        if result["success"]:
            self._stats.completed += 1
        else:
            self._stats.failed += 1
        self._stats.last_result = result
        return result

    # ---- 单轮的各步 -----------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"X-Project-Capability-Token": self.config.project_token}

    def _usage(self) -> dict[str, Any] | None:
        if self._usage_provider is None:
            return None
        try:
            return self._usage_provider()
        except Exception:  # noqa: BLE001 - 用量读取失败不能影响完成上报
            return None

    def _claim(self) -> dict[str, Any] | None:
        try:
            assignment = self._http(
                self.config.url,
                "POST",
                f"/api/agents/{self.config.agent_id}/tasks/claim",
                {
                    "project_id": self.config.project_id,
                    "stages": list(self.config.stages),
                    "lease_seconds": self.config.lease_seconds,
                    "idempotency_key": f"worker-claim:{self.config.agent_id}:{uuid4().hex}",
                },
                self._headers(),
            )
        except ValueError as error:
            self._stats.claim_errors += 1
            self._stats.last_claim_error = str(error)
            self._log(f"[{self._label}] claim 失败：{error}")
            return None
        if not assignment:
            return None
        return assignment

    async def _run_assignment(self, task: dict[str, Any], lease_token: str) -> dict[str, Any]:
        task_id = str(task.get("id") or "")
        self._log(f"[{self._label}] 领取任务 {task_id} · {task.get('title', '')}")
        self._progress(task_id, lease_token, "RUNNING", f"已领取，准备执行：{task.get('title', '')}"[:160])

        run_id = self._create_run(task)
        await self._collect_before(task)
        policy = task.get("resource_policy") if isinstance(task.get("resource_policy"), dict) else {}
        self._emit("run.started", {
            "task_id": task_id,
            "run_id": run_id,
            "title": task.get("title", ""),
            "executor": str(policy.get("worker_executor") or ("command" if policy.get("worker_command") else "unspecified")),
        })
        stdout, stderr = "", ""
        started_at = self._monotonic()
        try:
            success, summary, stdout, stderr = await self._execute(task, self._identity(), run_id)
        except Exception as error:  # noqa: BLE001 - 执行异常要作为结果上报，不能让循环退出
            success, summary = False, f"executor_error:{type(error).__name__}:{error}"
        elapsed_seconds = max(0.0, self._monotonic() - started_at)
        # 产出采集放在"完成 Run"之前：Run 需要带上它这次产出了什么（CL-1-04）
        artifacts, output_note = await self._collect_after(task, run_id, success, summary)
        if output_note:
            summary = _append_diagnostic(summary, output_note)
        if run_id:
            self._complete_run(run_id, success, summary, stdout, stderr, artifact_ids=artifacts, seconds=elapsed_seconds)
        # 终态**只走 HTTP 的 /complete 与 /result**：Gateway 的 run.completed 也会完成 Run，
        # 两条路径同时发会让同一个 Run 被完成两次。
        self._emit("process.exited", {
            "task_id": task_id,
            "run_id": run_id,
            "exit_status": "succeeded" if success else "failed",
            "summary": summary[:2000],
            # 统计必须在执行**之后**取：之前取永远是 0（"压掉了多少条"是这一轮的结论）
            **self._executor_reason(),
        })

        self._log(f"[{self._label}] 任务 {task_id} 结果：{'成功' if success else '失败'} · {summary[:160]}")
        try:
            self._http(
                self.config.url,
                "POST",
                f"/api/tasks/{task_id}/result",
                {
                    "agent_id": self.config.agent_id,
                    "lease_token": lease_token,
                    "success": success,
                    "summary": summary,
                    "output_artifact_ids": artifacts,
                    "idempotency_key": f"worker-result:{task_id}:{uuid4().hex}",
                },
                self._headers(),
            )
        except Exception as error:  # noqa: BLE001 - 结果提交失败必须留痕（任务会停在 RUNNING）
            self._stats.report_errors += 1
            self._log(f"[{self._label}] 结果提交失败：{error}")
        result = {
            "task_id": task_id,
            "run_id": run_id,
            "success": bool(success),
            "summary": summary,
            "output_artifact_ids": artifacts,
        }
        self._stats.last_outputs = {"artifact_ids": artifacts, "note": output_note} if (artifacts or output_note) else None
        return result

    async def _collect_before(self, task: dict) -> None:
        """执行前：让采集器记工作区快照。失败只记录（没快照就采不到产出，但任务照跑）。"""

        if self._outputs is None:
            return
        try:
            await self._outputs.before(task)
        except Exception as error:  # noqa: BLE001 - I4：内容采集不决定任务成败
            self._log(f"[{self._label}] 工作区快照失败：{type(error).__name__}:{error}")

    async def _collect_after(self, task: dict, run_id: str | None, success: bool, summary: str) -> tuple[list[str], str]:
        """执行后：上传产出，返回 `(成果物 id, 摘要补充说明)`。"""

        if self._outputs is None:
            return [], ""
        try:
            collected = await self._outputs.after(task, run_id, success, summary)
        except Exception as error:  # noqa: BLE001 - I4
            self._stats.report_errors += 1
            self._log(f"[{self._label}] 产出采集失败：{type(error).__name__}:{error}")
            return [], "产出采集失败（见本地日志），不影响任务结果"
        artifact_ids = [str(item) for item in getattr(collected, "artifact_ids", []) or []]
        note_function = getattr(collected, "note", None)
        note = str(note_function() if callable(note_function) else (note_function or ""))
        flushed = int(getattr(collected, "flushed_previous", 0) or 0)
        if flushed:
            note = f"{note} | 补传历史产出 {flushed} 项" if note else f"补传历史产出 {flushed} 项"
        return artifact_ids, note

    def _executor_reason(self) -> dict[str, Any]:
        """执行体自报的统计（节流条数、坏行数）：没有就不编。

        注意是"调用后取返回值"——统计是一个可调用对象（reporter.stats），
        直接 dict(callable) 会抛 TypeError，而那会把整轮执行打断在"Run 已登记、结果没提交"的中间态。
        """

        if self._executor_stats is None:
            return {}
        try:
            stats = self._executor_stats()
        except Exception:  # noqa: BLE001 - 统计拿不到不影响任务结果
            return {}
        return dict(stats) if isinstance(stats, dict) else {}

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        """把执行期事件交给常驻体（daemon 里接到 Gateway 的持久化队列）。

        没有 sink 时（例如 worker-run 前台调试）就什么都不做：
        本地 outbox 里堆积永远不会被投递的事件只会污染后续排查。
        """

        if self._event_sink is None:
            return
        try:
            self._event_sink(event_type, {**payload, "task_id": payload.get("task_id") or self._current_task_id()})
        except Exception as error:  # noqa: BLE001 - 上报失败不能影响任务执行
            self._log(f"[{self._label}] 事件上报失败：{type(error).__name__}:{error}")

    def _current_task_id(self) -> str | None:
        return str((self._current or {}).get("id") or "") or None

    def report_progress(self, event_type: str, payload: dict[str, Any]) -> None:
        """供执行层回调用：把执行体里发生的事报给平台（已做节流）。"""

        self._emit(event_type, payload)

    def _identity(self) -> dict[str, Any]:
        identity = {
            "project_id": self.config.project_id,
            "agent_id": self.config.agent_id,
            "device_id": self.config.device_id,
            "capabilities": [],
        }
        identity.update(self._identity_extra)
        return identity

    def _report_failure(self, task_id: str, lease_token: str, summary: str) -> None:
        """尽力提交一条失败结果（仅在执行过程本身炸掉时用）。"""

        if not task_id or not lease_token:
            return
        try:
            self._http(
                self.config.url,
                "POST",
                f"/api/tasks/{task_id}/result",
                {
                    "agent_id": self.config.agent_id,
                    "lease_token": lease_token,
                    "success": False,
                    "summary": summary[:4000],
                    "idempotency_key": f"worker-result-fallback:{task_id}:{uuid4().hex}",
                },
                self._headers(),
            )
        except Exception as error:  # noqa: BLE001 - 兜底失败只能留痕
            self._stats.report_errors += 1
            self._log(f"[{self._label}] 兜底结果提交失败：{error}")

    def _progress(self, task_id: str, lease_token: str, status: str, message: str) -> None:
        try:
            self._http(
                self.config.url,
                "POST",
                f"/api/tasks/{task_id}/progress",
                {
                    "agent_id": self.config.agent_id,
                    "lease_token": lease_token,
                    "status": status,
                    "message": message,
                    "idempotency_key": f"worker-progress:{task_id}:{uuid4().hex}",
                },
                self._headers(),
            )
        except Exception as error:  # noqa: BLE001 - 进度上报失败不阻塞执行
            self._log(f"[{self._label}] 进度上报失败：{error}")

    def _create_run(self, task: dict[str, Any]) -> str | None:
        try:
            created = self._http(
                self.config.url,
                "POST",
                f"/api/projects/{self.config.project_id}/runs",
                {
                    "task_id": task.get("id"),
                    "agent_id": self.config.agent_id,
                    # 执行归属：平台据此记录"哪台设备、谁的机器"（member_id 由平台推导，不在这里自报）
                    "device_id": self.config.device_id,
                    "parameters": {"task_title": task.get("title", ""), "worker": "daemon-run"},
                    "idempotency_key": f"worker-run-create:{task.get('id')}:{uuid4().hex}",
                },
                self._headers(),
            )
            run_id = str(created["id"])
        except Exception as error:  # noqa: BLE001 - Run 登记失败不阻塞任务执行，但要如实记录
            self._log(f"[{self._label}] Run 登记失败（继续执行）：{error}")
            return None
        self._running_run_ids.add(run_id)
        return run_id

    def _complete_run(
        self,
        run_id: str,
        success: bool,
        summary: str,
        stdout: str,
        stderr: str,
        *,
        artifact_ids: Sequence[str] = (),
        seconds: float | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "success": success,
            "summary": summary[:800],
            "stdout": (stdout or "")[-40000:],
            "stderr": (stderr or "")[-8000:],
            "output_artifact_ids": list(artifact_ids),
            "idempotency_key": f"worker-run-complete:{run_id}:{uuid4().hex}",
        }
        usage = self._usage()
        if usage is not None or seconds is not None:
            # 耗时由任务循环观测（永远有），token 只有执行体回报过才有——两者分开写，不互相冒充
            payload["usage"] = {**(usage or {}), "seconds": round(float(seconds or 0.0), 2)}
        try:
            self._http(
                self.config.url,
                "POST",
                f"/api/runs/{run_id}/complete",
                payload,
                self._headers(),
            )
        except Exception as error:  # noqa: BLE001 - 完成上报失败只记录，任务结果仍会提交
            self._stats.report_errors += 1
            self._log(f"[{self._label}] Run 完成上报失败：{error}")
        finally:
            self._running_run_ids.discard(run_id)


def _append_diagnostic(summary: str, note: str) -> str:
    """把采集说明追加到诊断段：回答在前、`---` 之后是诊断（见 docs/CODEX_EXECUTOR.md §3.3b）。"""

    text = (summary or "").strip()
    if not note:
        return text
    marker = chr(10) + "---" + chr(10)
    if marker in text:
        head, _, diagnostics = text.partition(marker)
        return f"{head}{marker}{diagnostics.strip()} | {note}".strip()
    return f"{text} | {note}".strip() if text else note


def task_loop_config_from_identity(
    identity: Mapping[str, Any],
    *,
    url: str,
    stages: Sequence[str] = (),
    lease_seconds: int = 900,
    idle_seconds: float = 5.0,
    max_idle_seconds: float = 120.0,
) -> TaskLoopConfig:
    """从 `_resolve_worker_identity` 的结果构造配置（单一来源，避免字段名漂移）。"""

    return TaskLoopConfig(
        url=url,
        project_id=str(identity["project_id"]),
        agent_id=str(identity["agent_id"]),
        device_id=str(identity["device_id"]),
        project_token=str(identity["project_token"]),
        stages=tuple(stages),
        lease_seconds=lease_seconds,
        idle_seconds=idle_seconds,
        max_idle_seconds=max_idle_seconds,
    )


__all__ = [
    "TaskLoop",
    "TaskLoopConfig",
    "TaskLoopStats",
    "task_loop_config_from_identity",
]