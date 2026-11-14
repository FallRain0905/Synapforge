"""集群调度器：把"谁有活"变成"拉起一次 Agent CLI"。

**不是常驻 Agent。** 每个角色是一次性 CLI 会话：先读 `inbox`，处理一项，退出。
调度器只负责轮次、进程、预算、暂停/恢复；协议事实仍在 SQLite。
"""

from __future__ import annotations

import collections
import json
import os
import queue
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

from .. import commands as base
from ..db import open_store
from ..paths import state_dir
from . import backends
from . import capabilities as studio_capabilities
from . import commands as studio_commands
from . import plugins as studio_plugins
from . import routing as studio_routing
from . import store as studio_store
from .agent_runtime import RunBroker
from .step_parser import StepParser

Runner = Callable[[dict[str, Any], backends.Invocation, Path, Any], tuple[int, str]]


def _dump(obj: Any) -> Any:
    return obj.model_dump(mode="json") if hasattr(obj, "model_dump") else obj


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _tail(path: Path, limit: int = 4000) -> str:
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return data[-limit:]


class ClusterWorker:
    """一个集群一个线程；顺序执行，lead 优先。"""

    def __init__(self, cluster_id: str, state_path: str | Path | None, *,
                 interval: float = 2.0, runner: Runner | None = None,
                 broker: RunBroker | None = None) -> None:
        self.cluster_id = str(cluster_id)
        self.state_path = state_path
        self.interval = max(0.5, float(interval))
        self._runner = runner or self._run_process
        self._stop = threading.Event()
        self._current_lock = threading.Lock()
        self._current: dict[str, subprocess.Popen] = {}
        self._process_ids: dict[str, str] = {}
        self._killed: set[str] = set()
        self._nudges: set[str] = set()
        self._role_locks: dict[str, threading.Lock] = {}
        self._inflight: dict[str, threading.Thread] = {}
        self._inflight_lock = threading.Lock()
        self._bump_lock = threading.Lock()
        self._broker = broker or RunBroker()
        self._active_run_id = ""
        self._thread = threading.Thread(target=self._main, name=f"cluster-{cluster_id[-8:]}",
                                        daemon=True)
        self.error = ""

    # ---- 生命周期 ----------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def request_stop(self) -> None:
        self._stop.set()
        self.kill_current()

    def nudge(self, role_id: str) -> None:
        self._nudges.add(str(role_id))

    def kill_current(self, role_id: str | None = None) -> None:
        with self._current_lock:
            targets = list(self._current.items())
            for key, _ in targets:
                if role_id is None or key == str(role_id):
                    self._killed.add(key)
        for key, proc in targets:
            if role_id is not None and key != str(role_id):
                continue
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    # ---- 主循环 ------------------------------------------------------------

    def _main(self) -> None:
        store = open_store(self.state_path)
        try:
            studio_store.ensure_schema(store)
            cluster = studio_store.get_cluster(store, self.cluster_id)
            if cluster is None:
                return
            idle = 0
            while not self._stop.is_set():
                run = studio_store.latest_run(store, self.cluster_id)
                if run is None or run["status"] != "RUNNING":
                    break
                if not self._check_budget(store, cluster, run):
                    break
                did = self._dispatch(store, cluster, run)
                if did:
                    idle = 0
                    continue
                if self._auto_approve(store, cluster):
                    continue
                if self._maybe_finalize(store, cluster):
                    continue
                idle = min(idle + 1, 6)
                time.sleep(self.interval * idle)
        except Exception as error:  # noqa: BLE001 — 调度器崩溃要留下可见原因
            self.error = f"{type(error).__name__}: {error}"
            try:
                run = studio_store.latest_run(store, self.cluster_id)
                if run and run["status"] == "RUNNING":
                    studio_store.update_run(store, run["id"], status="PAUSED",
                                            checkpoint={**run.get("checkpoint", {}),
                                                        "error": self.error})
                    studio_store.update_cluster(store, self.cluster_id, status="PAUSED")
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._wait_inflight(timeout=5.0)
            store.close()

    def _wait_inflight(self, timeout: float) -> None:
        with self._inflight_lock:
            threads = list(self._inflight.values())
        deadline = time.time() + timeout
        for thread in threads:
            remaining = max(0.1, deadline - time.time())
            thread.join(remaining)

    # ---- 并发派发 ----------------------------------------------------------

    def _dispatch(self, store: Any, cluster: dict[str, Any], run: dict[str, Any]) -> bool:
        """有空位就把角色派到独立线程；lead 有活时只派 lead。"""

        roles = studio_store.list_roles(store, self.cluster_id)
        lead = next((r for r in roles if r["id"] == cluster.get("lead_role_id")), None)
        self._reap()

        if lead and self._has_work(store, cluster, lead):
            return self._spawn(store, cluster, run, lead, roles)

        policy = cluster.get("policy") or {}
        limit = int(policy.get("max_parallel_workers") or 3)
        limit = max(1, limit)
        busy = self._inflight_count()
        if busy >= limit:
            return False
        dispatched = False
        for role in roles:
            if lead and role["id"] == lead["id"]:
                continue
            if self._inflight_count() >= limit:
                break
            if self._is_inflight(role["id"]):
                continue
            if role["id"] in self._nudges or self._has_work(store, cluster, role):
                self._nudges.discard(role["id"])
                if self._spawn(store, cluster, run, role, roles):
                    dispatched = True
        return dispatched

    def _reap(self) -> None:
        with self._inflight_lock:
            done = [key for key, thread in self._inflight.items() if not thread.is_alive()]
            for key in done:
                self._inflight.pop(key, None)

    def _inflight_count(self) -> int:
        with self._inflight_lock:
            return sum(1 for thread in self._inflight.values() if thread.is_alive())

    def _is_inflight(self, role_id: str) -> bool:
        with self._inflight_lock:
            thread = self._inflight.get(str(role_id))
            return bool(thread and thread.is_alive())

    def _spawn(self, store: Any, cluster: dict[str, Any], run: dict[str, Any],
               role: dict[str, Any], roles: list[dict[str, Any]]) -> bool:
        if self._is_inflight(role["id"]):
            return False

        def body() -> None:
            own = open_store(self.state_path)
            try:
                current = studio_store.get_run(own, run["id"]) or run
                self._invoke(own, cluster, current, role, roles)
            finally:
                own.close()

        thread = threading.Thread(target=body,
                                  name=f"role-{role['id'][-6:]}", daemon=True)
        with self._inflight_lock:
            self._inflight[role["id"]] = thread
        thread.start()
        return True

    def _step(self, store: Any, cluster: dict[str, Any], run: dict[str, Any]) -> bool:
        return self._dispatch(store, cluster, run)

    def _has_work(self, store: Any, cluster: dict[str, Any], role: dict[str, Any]) -> bool:
        try:
            box = base.inbox(store, agent_id=role["agent_id"],
                             project_id=cluster["project_id"])
        except Exception:  # noqa: BLE001
            return False
        if box.get("assigned_to_me") or box.get("to_answer") or box.get("to_consume"):
            return True
        for task in box.get("in_flight_tasks") or []:
            if str(task.get("status") or "").upper() in ("CLAIMED", "RUNNING"):
                return True
        return False

    def _invoke(self, store: Any, cluster: dict[str, Any], run: dict[str, Any],
                role: dict[str, Any], roles: list[dict[str, Any]]) -> bool:
        lock = self._role_locks.setdefault(role["id"], threading.Lock())
        if not lock.acquire(blocking=False):
            return False
        try:
            tasks = [_dump(t) for t in store.list_tasks(cluster["project_id"])]
            events = base.recent_events(store, project_id=cluster["project_id"], limit=40)
            prompt = backends.build_prompt(cluster=cluster, role=role, roles=roles,
                                           tasks=tasks, events=events)
            run_dir = (state_dir() / "studio" / self.cluster_id / run["id"])
            run_dir.mkdir(parents=True, exist_ok=True)
            extra_servers = self._role_servers(store, role)
            provider, api_key = self._role_channel(store, role)
            mcp_path = backends.write_mcp_config(
                run_dir / f"mcp-{role['id']}.json",
                agent_id=role["agent_id"], project_id=cluster["project_id"],
                state_path=str(self.state_path) if self.state_path else None,
                extra_servers=extra_servers)
            invocation = backends.build_invocation(
                {**role, "_provider": provider, "_api_key": api_key,
                 "_run_dir": str(run_dir)},
                prompt=prompt, mcp_config=mcp_path,
                workspace=role["workspace"], agent_id=role["agent_id"],
                project_id=cluster["project_id"], policy=cluster.get("policy") or {},
                state_path=str(self.state_path) if self.state_path else None)
            stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            log_path = run_dir / f"{role['id']}-{stamp}.log"
            process = studio_store.create_process(
                store, run_id=run["id"], role_id=role["id"], pid=None,
                command=invocation.command, log_path=str(log_path), status="STARTING")
            log_path.write_text(
                f"$ {' '.join(invocation.command)}\n"
                f"# role={role['name']} backend={invocation.backend} cwd={invocation.cwd}\n",
                encoding="utf-8")

            _event(store, cluster["project_id"], "studio.role.invoked", role["agent_id"], {
                "cluster_id": self.cluster_id, "run_id": run["id"], "role": role["name"],
                "process_id": process["id"], "backend": invocation.backend,
            })
            self._bump(store, run, role)
            self._process_ids[role["id"]] = process["id"]
            self._active_run_id = str(run["id"])
            # 角色开始跑：先把「收到任务」写进步骤流，前端面板立刻有内容
            studio_store.append_cluster_step(
                store, run_id=str(run["id"]), role_id=role["id"], kind="task",
                title="开始执行", detail=str(role.get("current_prompt") or role["name"]),
                process_id=str(process["id"]))
            self._broker.publish(role["id"], {
                "type": "role", "role_id": role["id"], "status": "RUNNING",
                "name": role["name"], "model": role.get("model") or "",
            })

            code, tail = self._runner(role, invocation, log_path, store)
            if code == 0:
                status = "EXITED"
            elif code == -1:
                status = "STOPPED"
            elif code == -2:
                status = "TIMEOUT"
            else:
                status = "FAILED"
            studio_store.finish_process(store, process["id"], status=status, exit_code=code)
            self._broker.publish(role["id"], {
                "type": "role", "role_id": role["id"],
                "status": "EXITED" if code == 0 else status,
                "name": role["name"], "model": role.get("model") or "",
                "exit_code": code,
            })

            current = studio_store.get_run(store, run["id"]) or run
            checkpoint = {**(current.get("checkpoint") or {}),
                          "last_role": role["name"],
                          "last_role_id": role["id"],
                          "last_exit": code,
                          "last_finished_at": _now(),
                          "last_output_tail": tail[-1200:]}
            if code not in (0, -1):
                failures = dict(checkpoint.get("failures") or {})
                failures[role["id"]] = int(failures.get(role["id"], 0)) + 1
                checkpoint["failures"] = failures
                if failures[role["id"]] >= 2:
                    # 并发下单个角色失败不该拖垮整个集群：标成 blocked，继续跑别的角色
                    blocked = dict(checkpoint.get("blocked_roles") or {})
                    blocked[role["id"]] = role["name"]
                    checkpoint["blocked_roles"] = blocked
                    checkpoint["last_error"] = tail[-400:]
            else:
                checkpoint["failures"] = {}
                blocked = dict(checkpoint.get("blocked_roles") or {})
                if blocked.pop(role["id"], None) is not None:
                    checkpoint["blocked_roles"] = blocked
            studio_store.update_run(store, run["id"], checkpoint=checkpoint)
            return True
        except backends.BackendError as error:
            blocked = dict((studio_store.get_run(store, run["id"]) or run)
                           .get("checkpoint", {}).get("blocked_roles") or {})
            blocked[role["id"]] = role["name"]
            studio_store.update_run(store, run["id"],
                                    checkpoint={**(run.get("checkpoint") or {}),
                                                "paused_reason": error.code,
                                                "blocked_roles": blocked,
                                                "error": str(error)})
            studio_store.update_cluster(store, self.cluster_id, status="PAUSED")
            return False
        except Exception as error:  # noqa: BLE001
            studio_store.update_run(store, run["id"], status="PAUSED",
                                    checkpoint={**(run.get("checkpoint") or {}),
                                                "paused_reason": "invoke_failed",
                                                "error": f"{type(error).__name__}: {error}"})
            studio_store.update_cluster(store, self.cluster_id, status="PAUSED")
            return False
        finally:
            lock.release()

    def _role_channel(self, store: Any, role: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """复用 Agent 的角色沿用它的渠道与 key；手填角色走本机默认。"""

        provider_id = str((role.get("budget") or {}).get("provider_id") or "")
        if not provider_id:
            return None, ""
        provider = studio_store.get_provider(store, provider_id)
        if provider is None or not provider["enabled"]:
            return None, ""
        keys = studio_routing.usable_keys(store, provider)
        return provider, (keys[0].api_key if keys else provider["api_key"])

    def _role_servers(self, store: Any, role: dict[str, Any]) -> dict[str, Any]:
        """复用 Agent 的角色：把它自己的 MCP + 插件 MCP 合进本次配置，并挂载技能/子代理。"""

        budget = role.get("budget") or {}
        agent_id = str(role.get("local_agent_id") or "")
        workspace = Path(str(role.get("workspace") or ""))
        skills = list(budget.get("skills") or [])
        mcp_ids = list(budget.get("mcp") or [])
        plugin_ids = list(budget.get("plugins") or [])
        servers: dict[str, Any] = {}
        if not agent_id and not skills and not mcp_ids and not plugin_ids:
            return servers
        agent = studio_store.get_agent(store, agent_id) if agent_id else None
        try:
            if skills or plugin_ids:
                extra: list[dict[str, Any]] = []
                if plugin_ids and agent:
                    parts = studio_plugins.agent_plugin_parts(
                        store, {**agent, "plugins": plugin_ids}, self.state_path)
                    extra = parts["skills"]
                    servers.update(parts["mcp"])
                    if parts["subagents"]:
                        studio_plugins.sync_plugin_subagents(workspace, parts["subagents"])
                studio_capabilities.sync_workspace_skills(
                    workspace, Path.home(), skills, extra_skills=extra)
            if mcp_ids:
                payload = studio_capabilities.mcp_payload(
                    Path.home(), mcp_ids, [agent] if agent else [])
                servers.update(payload.get("mcpServers") or {})
        except Exception:  # noqa: BLE001 — 扩展挂载失败不该拖垮这次调用
            pass
        return servers

    def _bump(self, store: Any, run: dict[str, Any], role: dict[str, Any]) -> None:
        with self._bump_lock:
            current = studio_store.get_run(store, run["id"]) or run
            used = dict(current.get("budget_used") or {})
            invocations = dict(used.get("invocations") or {})
            invocations[role["id"]] = int(invocations.get(role["id"], 0)) + 1
            used["invocations"] = invocations
            studio_store.update_run(store, run["id"],
                                    round=int(current.get("round") or 0) + 1,
                                    budget_used=used)

    # ---- 预算 / 自动复核 / 收尾 -------------------------------------------

    def _check_budget(self, store: Any, cluster: dict[str, Any],
                      run: dict[str, Any]) -> bool:
        policy = cluster.get("policy") or {}
        max_rounds = int(policy.get("max_rounds") or 200)
        if int(run.get("round") or 0) >= max_rounds:
            self._pause(store, run, "max_rounds")
            return False
        max_wall = int(policy.get("max_wall_minutes") or 480)
        try:
            started = datetime.fromisoformat(str(run["started_at"]).replace("Z", "+00:00"))
            elapsed = (datetime.now(UTC) - started).total_seconds()
        except (TypeError, ValueError):
            elapsed = 0
        if max_wall > 0 and elapsed > max_wall * 60:
            self._pause(store, run, "max_wall_time")
            return False
        used = run.get("budget_used") or {}
        max_inv = int(policy.get("max_invocations_per_role") or 30)
        for role_id, count in (used.get("invocations") or {}).items():
            if max_inv > 0 and int(count) >= max_inv:
                self._pause(store, run, f"max_invocations:{role_id}")
                return False
        return True

    def _pause(self, store: Any, run: dict[str, Any], reason: str) -> None:
        studio_store.update_run(store, run["id"], status="PAUSED",
                                checkpoint={**(run.get("checkpoint") or {}),
                                            "paused_reason": reason})
        studio_store.update_cluster(store, self.cluster_id, status="PAUSED")

    def _auto_approve(self, store: Any, cluster: dict[str, Any]) -> bool:
        policy = cluster.get("policy") or {}
        pending = [t for t in store.list_tasks(cluster["project_id"])
                   if str(getattr(t, "status", "")).upper() == "WAITING_REVIEW"]
        if not pending:
            return False
        if policy.get("human_review"):
            run = studio_store.latest_run(store, self.cluster_id)
            if run:
                self._pause(store, run, "waiting_human_review")
            return True
        if not policy.get("auto_approve", True):
            return False
        return studio_commands.auto_approve_pending(store, self.cluster_id) > 0

    def _maybe_finalize(self, store: Any, cluster: dict[str, Any]) -> bool:
        tasks = store.list_tasks(cluster["project_id"])
        statuses = {str(getattr(t, "status", "")).upper() for t in tasks}
        blocked = [t for t in tasks
                   if str(getattr(t, "status", "")).upper() in ("BLOCKED", "FAILED")]
        if blocked:
            # 只有"正常任务"被阻塞才算失败；终审被阻塞说明 Agent 提出了异议，
            # 那是给人看的结论，不该把整个集群停掉。
            blocking = [t for t in blocked
                        if str(getattr(t, "stage", "")) != "final_review"]
            if blocking:
                run = studio_store.latest_run(store, self.cluster_id)
                if run:
                    self._pause(store, run, "task_blocked_or_failed")
                return True
        if statuses & {"READY", "CLAIMED", "RUNNING", "WAITING", "NEEDS_REVISION",
                       "WAITING_REVIEW"}:
            return False
        artifacts = base.list_artifacts(store, project_id=cluster["project_id"])
        if not artifacts:
            return False
        final = next((t for t in tasks if str(getattr(t, "stage", "")) == "final_review"), None)
        if final is None:
            return studio_commands.ensure_final_review_task(store, self.cluster_id) is not None
        status = str(getattr(final, "status", "")).upper()
        if status == "APPROVED":
            studio_commands.mark_done(store, self.cluster_id)
            return True
        if status in ("FAILED", "BLOCKED"):
            run = studio_store.latest_run(store, self.cluster_id)
            if run:
                self._pause(store, run, "final_review_needs_human")
            return True
        return False

    # ---- 默认进程执行 ------------------------------------------------------

    def _make_parser(self, role: dict[str, Any], process_id: str) -> StepParser:
        """给这次角色调用造一个解析器：step 落 local_cluster_steps，增量推 broker。"""

        run_id = self._active_run_id
        role_id = str(role["id"])
        broker = self._broker

        def emit(kind: str, title: str, detail: str,
                 payload: dict[str, Any] | None) -> dict[str, Any]:
            store = open_store(self.state_path)
            try:
                step = studio_store.append_cluster_step(
                    store, run_id=run_id, role_id=role_id, kind=kind, title=title,
                    detail=detail or "", payload=payload, process_id=process_id)
            finally:
                store.close()
            broker.publish(role_id, {"type": "step", "step": step})
            return step

        def update(step_id: str, title: str, detail: str) -> None:
            store = open_store(self.state_path)
            try:
                studio_store.update_cluster_step(store, step_id, title=title, detail=detail)
            finally:
                store.close()

        def publish(event: dict[str, Any]) -> None:
            step = dict(event.get("step") or {})
            step["role_id"] = role_id
            broker.publish(role_id, {**event, "step": step})

        return StepParser(emit=emit, update=update, publish=publish)

    def _run_process(self, role: dict[str, Any], invocation: backends.Invocation,
                     log_path: Path, store: Any) -> tuple[int, str]:
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(
                invocation.command, cwd=invocation.cwd, env=invocation.env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", shell=False, creationflags=creationflags)
        except FileNotFoundError:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"\n[studio] executable not found: {invocation.command[:1]}\n")
            return 127, "executable_not_found"
        except OSError as error:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"\n[studio] start failed: {error}\n")
            return 126, f"start_failed:{error}"

        with self._current_lock:
            self._current[role["id"]] = proc
        process_id = self._process_ids.get(role["id"]) or ""
        if process_id:
            studio_store.update_process_pid(store, process_id, proc.pid)
        code = -3
        parser = self._make_parser(role, process_id)
        try:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"# pid={proc.pid}\n")
                handle.flush()

                def pump() -> None:
                    try:
                        assert proc.stdout is not None
                        for line in proc.stdout:
                            handle.write(line)
                            handle.flush()
                            try:
                                parser.feed(line, backend=invocation.backend)
                            except Exception:  # noqa: BLE001 — 解析失败不能中断进程
                                pass
                    except (OSError, ValueError):
                        pass

                reader = threading.Thread(target=pump, daemon=True)
                reader.start()
                try:
                    code = proc.wait(timeout=invocation.timeout_seconds)
                    status_note = "exit"
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        pass
                    code = -2
                    status_note = "timeout"
                reader.join(timeout=5)
                handle.write(f"\n# {status_note} code={code}\n")
        finally:
            with self._current_lock:
                self._current.pop(role["id"], None)
            try:
                if proc.stdout:
                    proc.stdout.close()
            except (OSError, ValueError):
                pass
        with self._current_lock:
            killed = role["id"] in self._killed
            self._killed.discard(role["id"])
        if killed or self._stop.is_set():
            return -1, _tail(log_path)
        return int(code), _tail(log_path)


def _event(store: Any, project_id: str, event_type: str, actor: str,
           payload: dict[str, Any]) -> None:
    try:
        store.add_event(UUID(str(project_id)), event_type, actor, payload, actor_kind="agent")
    except Exception:  # noqa: BLE001
        pass


class SupervisorManager:
    """一个 studio 进程内的集群线程表；pause/resume 与 DB 状态一致。"""

    def __init__(self, state_path: str | Path | None = None, *,
                 interval: float = 2.0, runner: Runner | None = None) -> None:
        self.state_path = state_path
        self.interval = interval
        self._runner = runner
        self._lock = threading.Lock()
        self._workers: dict[str, ClusterWorker] = {}
        # manager 持有共享 broker：SSE 订阅时 worker 可能还没创建
        self._broker = RunBroker()
        self._recover()

    def _recover(self) -> None:
        store = open_store(self.state_path)
        try:
            studio_store.ensure_schema(store)
            for process in studio_store.running_processes(store):
                studio_store.finish_process(store, process["id"], status="ORPHANED",
                                            exit_code=None)
            for cluster in studio_store.list_clusters(store):
                run = studio_store.latest_run(store, cluster["id"])
                if run and run["status"] == "RUNNING":
                    studio_store.update_run(store, run["id"], status="PAUSED",
                                            checkpoint={**(run.get("checkpoint") or {}),
                                                        "paused_reason": "studio_restarted"})
                    studio_store.update_cluster(store, cluster["id"], status="PAUSED")
        finally:
            store.close()

    def _store(self) -> Any:
        return open_store(self.state_path)

    def start(self, cluster_id: str, *, kickoff: bool = True) -> dict[str, Any]:
        with self._lock:
            worker = self._workers.get(cluster_id)
            if worker and worker.is_alive():
                return {"started": False, "reason": "worker_alive"}
            store = self._store()
            try:
                studio_store.ensure_schema(store)
                cluster = studio_store.get_cluster(store, cluster_id)
                if cluster is None:
                    raise studio_commands.StudioError("cluster_not_found", [cluster_id])
                if cluster["status"] == "RUNNING":
                    run = studio_store.latest_run(store, cluster_id)
                    if run is None:
                        result = studio_commands.start_cluster(
                            store, cluster_id, kickoff=kickoff)
                    else:
                        result = {"started": False, "reason": "already_running", "run": run}
                elif cluster["status"] == "PAUSED":
                    studio_commands.resume_cluster(store, cluster_id)
                    result = {"started": True, "resumed": True,
                              "run": studio_store.latest_run(store, cluster_id)}
                else:
                    result = studio_commands.start_cluster(
                        store, cluster_id, kickoff=kickoff)
            finally:
                store.close()
            worker = ClusterWorker(cluster_id, self.state_path, interval=self.interval,
                                   broker=self._broker,
                                   runner=self._runner)
            self._workers[cluster_id] = worker
            worker.start()
            return result

    def pause(self, cluster_id: str, *, stop_processes: bool = False) -> dict[str, Any]:
        store = self._store()
        try:
            result = studio_commands.pause_cluster(store, cluster_id)
        finally:
            store.close()
        if stop_processes:
            worker = self._workers.get(cluster_id)
            if worker:
                worker.kill_current()
        return result

    def resume(self, cluster_id: str) -> dict[str, Any]:
        store = self._store()
        try:
            result = studio_commands.resume_cluster(store, cluster_id)
        finally:
            store.close()
        with self._lock:
            worker = self._workers.get(cluster_id)
            if not (worker and worker.is_alive()):
                worker = ClusterWorker(cluster_id, self.state_path, interval=self.interval,
                                   broker=self._broker,
                                       runner=self._runner)
                self._workers[cluster_id] = worker
                worker.start()
        return result

    def stop(self, cluster_id: str) -> dict[str, Any]:
        store = self._store()
        try:
            result = studio_commands.stop_cluster(store, cluster_id)
        finally:
            store.close()
        with self._lock:
            worker = self._workers.get(cluster_id)
        if worker:
            worker.request_stop()
        return result

    def nudge(self, role_id: str) -> dict[str, Any]:
        store = self._store()
        try:
            role = studio_store.get_role(store, role_id)
            if role is None:
                raise studio_commands.StudioError("role_not_found", [role_id])
            cluster_id = role["cluster_id"]
        finally:
            store.close()
        worker = self._workers.get(cluster_id)
        if worker is None or not worker.is_alive():
            self.start(cluster_id)
            worker = self._workers.get(cluster_id)
        if worker:
            worker.nudge(role_id)
        return {"nudged": True, "role_id": role_id, "cluster_id": cluster_id}

    def stop_role(self, role_id: str) -> dict[str, Any]:
        store = self._store()
        try:
            role = studio_store.get_role(store, role_id)
            if role is None:
                raise studio_commands.StudioError("role_not_found", [role_id])
            cluster_id = role["cluster_id"]
        finally:
            store.close()
        worker = self._workers.get(cluster_id)
        if worker:
            worker.kill_current(role_id)
        return {"stopped": True, "role_id": role_id}

    def active(self) -> dict[str, Any]:
        with self._lock:
            return {cid: {"alive": w.is_alive(), "error": w.error}
                    for cid, w in self._workers.items()}

    def subscribe(self, role_id: str) -> queue.Queue:
        return self._broker.subscribe(str(role_id))

    def unsubscribe(self, role_id: str, box: queue.Queue) -> None:
        self._broker.unsubscribe(str(role_id), box)

    def log_tail(self, role_id: str, lines: int = 200) -> str:
        store = self._store()
        try:
            process = studio_store.latest_process_for_role(store, role_id)
        finally:
            store.close()
        if not process or not process.get("log_path"):
            return ""
        path = Path(process["log_path"])
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                data = collections.deque(handle, maxlen=max(1, min(2000, int(lines))))
            return "".join(data)
        except OSError:
            return ""

    def shutdown(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
        for worker in workers:
            worker.request_stop()
        for worker in workers:
            worker.join(timeout=3)
