"""单 Agent 运行器：把一次任务跑成可实时观看的会话。

Claude Code 走 `--output-format stream-json`：每个 JSON 行转成一条 step
（system / assistant / tool / tool_result / result），写进 SQLite 并通过
`RunBroker` 推给 SSE 订阅者。任务完成后扫工作区差分，登记成果物。
"""

from __future__ import annotations

import json
import os
import queue
import shlex
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from .. import commands as base
from ..artifacts import diff, snapshot
from ..db import open_store
from . import capabilities as studio_capabilities
from . import plugins as studio_plugins
from . import providers as studio_providers
from . import routing as studio_routing
from . import store as studio_store
from .backends import _base_env, find_claude
from .commands import StudioError
from .step_parser import StepParser

DEFAULT_TIMEOUT = 900
STALL_TIMEOUT = 180
MAX_FILES = 30


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _text(value: Any, limit: int = 6000) -> str:
    text = str(value if value is not None else "")
    return text if len(text) <= limit else text[:limit] + "…"


class RunBroker:
    """run_id → 订阅者队列；SSE handler 从这里取事件。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: dict[str, set[queue.Queue]] = {}

    def subscribe(self, run_id: str) -> queue.Queue:
        box: queue.Queue = queue.Queue(maxsize=2000)
        with self._lock:
            self._subs.setdefault(run_id, set()).add(box)
        return box

    def unsubscribe(self, run_id: str, box: queue.Queue) -> None:
        with self._lock:
            self._subs.get(run_id, set()).discard(box)

    def publish(self, run_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            boxes = list(self._subs.get(run_id, set()))
        for box in boxes:
            try:
                box.put_nowait(event)
            except queue.Full:
                pass

    def close(self, run_id: str) -> None:
        self.publish(run_id, {"type": "done"})


class AgentRunWorker:
    def __init__(self, run_id: str, state_path: str | Path | None,
                 broker: RunBroker) -> None:
        self.run_id = str(run_id)
        self.state_path = state_path
        self.broker = broker
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()
        self._result_text = ""
        self._ok = False
        self._usage: dict[str, Any] = {}
        self._session_id = ""
        self._model = ""
        self._backend = "claude"
        self._provider: dict[str, Any] | None = None
        self._settings_path: Path | None = None
        self._mcp_path: Path | None = None
        self._session_announced = False
        self._new_session_id = ""
        self._stalled = False
        self._output_lines: list[str] = []
        self._session_recorded = ""
        self._plugin_parts: dict[str, Any] = {"skills": [], "mcp": {},
                                              "subagents": [], "plugins": [],
                                              "missing": []}
        self._route_key_list: list[studio_routing.RouteKey] = []
        self._key_index = 0
        self._key_id = ""
        self._key_label = ""
        self._partial = False
        self._blocks: dict[int, dict[str, Any]] = {}
        self._streamed_blocks = 0
        self._thread = threading.Thread(target=self._run, name=f"agent-{run_id[-6:]}",
                                        daemon=True)
        self.error = ""

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def stop(self) -> None:
        self._stop.set()
        with self._proc_lock:
            proc = self._proc
        if proc:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    # ---- 主流程 ------------------------------------------------------------

    def _run(self) -> None:
        store = open_store(self.state_path)
        try:
            studio_store.ensure_schema(store)
            run = studio_store.get_agent_run(store, self.run_id)
            if run is None:
                return
            agent = studio_store.get_agent(store, run["agent_id"])
            if agent is None:
                self._fail(store, run, "agent_not_found")
                return
            self._model = run.get("model") or agent.get("model") or ""
            self._backend = str(run.get("backend") or agent.get("backend") or "claude")
            try:
                self._provider = studio_providers.resolve_for_agent(store, agent, run)
            except StudioError as error:
                self._step(store, "error", "渠道不可用", "; ".join(error.errors) or error.code)
                self._fail(store, run, f"{error.code}: {'; '.join(error.errors)}")
                return
            self._step(store, "task", "收到任务", run.get("task_text") or "")

            lease_token = ""
            try:
                from app.contracts import TaskClaimRequest

                task, lease = store.claim_task(
                    UUID(str(run["task_id"])),
                    TaskClaimRequest(agent_id=run["agent_id"],
                                     idempotency_key=f"agent-claim-{self.run_id}"))
                lease_token = str(lease.lease_token)
                base.mark_running(store, task_id=task.id, agent_id=run["agent_id"],
                                  lease_token=lease_token, message="agent task")
            except Exception as error:  # noqa: BLE001 — claim 失败如实记录
                self._step(store, "error", "领取任务失败", f"{type(error).__name__}: {error}")
                self._fail(store, run, f"claim_failed: {error}")
                return

            workspace = Path(run["workspace"])
            workspace.mkdir(parents=True, exist_ok=True)
            try:
                self._plugin_parts = studio_plugins.agent_plugin_parts(
                    store, agent, self.state_path)
                if self._plugin_parts["missing"]:
                    self._step(store, "error", "插件缺失",
                               "已卸载或不存在：" + ", ".join(self._plugin_parts["missing"]))
            except Exception as error:  # noqa: BLE001 — 插件解析失败不阻断任务
                self._step(store, "error", "插件解析失败", f"{type(error).__name__}: {error}")
            skill_ids = agent.get("skills") or []
            try:
                report = studio_capabilities.sync_workspace_skills(
                    workspace, Path.home(), skill_ids,
                    extra_skills=self._plugin_parts["skills"])
                if skill_ids or self._plugin_parts["skills"] or report["mounted"] \
                        or report["missing"] or report["skipped"]:
                    self._step(
                        store, "system", "挂载技能",
                        "已挂载：" + (", ".join(report["mounted"]) or "无")
                        + ("\n缺失：" + ", ".join(report["missing"]) if report["missing"] else "")
                        + ("\n跳过：" + ", ".join(
                            f"{item['name']}({item['reason']})" for item in report["skipped"])
                           if report["skipped"] else ""))
            except Exception as error:  # noqa: BLE001 — 技能挂载失败不阻断任务
                self._step(store, "error", "技能挂载失败", f"{type(error).__name__}: {error}")
            try:
                subagents = studio_plugins.sync_plugin_subagents(
                    workspace, self._plugin_parts["subagents"])
                if subagents["mounted"]:
                    self._step(store, "system", "挂载子代理",
                               "已挂载：" + ", ".join(subagents["mounted"]))
            except Exception as error:  # noqa: BLE001
                self._step(store, "error", "子代理挂载失败", f"{type(error).__name__}: {error}")
            before, before_truncated = snapshot(workspace)
            provider_label = (self._provider["name"] if self._provider else "本机默认")
            skill_count = len(agent.get("skills") or []) + len(self._plugin_parts.get("skills") or [])
            plugin_count = len(self._plugin_parts.get("plugins") or [])
            mcp_label = (f"strict · {len(agent.get('mcp') or []) + len(self._plugin_parts.get('mcp') or {})} 个"
                         if agent.get("mcp_managed")
                         else f"本机全部 + 插件 {len(self._plugin_parts.get('mcp') or {})} 个")

            keys = self._route_keys(store)
            if keys:
                self._step(store, "system", "选择 key",
                           f"渠道：{provider_label}\n可用 key："
                           + ", ".join(key.label for key in keys)
                           + (f"\n本次使用：{keys[0].label}" if keys else ""))
            code = 0
            attempt = 0
            while True:
                attempt += 1
                command = self._command(agent, run)
                if not command:
                    reason = ("command_template_missing" if self._backend == "command"
                              else "claude_not_found")
                    self._fail(store, run, reason)
                    return
                self._step(store, "system", "启动 Agent",
                           f"backend={self._backend}\nmodel={self._model or '默认'}\n"
                           f"channel={provider_label}\nkey={self._key_label or '默认'}\n"
                           f"skills={skill_count}\n"
                           f"plugins={plugin_count}\nmcp={mcp_label}\n"
                           f"workspace={workspace}\n"
                           f"command={' '.join(command[:4])} …")
                session_id = self._session_id or self._new_session_id
                if session_id:
                    self._remember_session(store, run, session_id)
                code = self._stream(command, workspace)
                failure = "" if (code == 0 and self._ok) else self._failure_text(code)
                outcome = studio_routing.classify_failure(failure) if failure else ""
                if not outcome or outcome == "other":
                    if failure and self._key_id:
                        studio_routing.mark_key(store, self._provider["id"], self._key_id,
                                                "failed", failure)
                    elif not failure and self._key_id:
                        studio_routing.mark_key(store, self._provider["id"], self._key_id, "ok")
                    break
                if self._key_id:
                    studio_routing.mark_key(
                        store, self._provider["id"], self._key_id,
                        "auth" if outcome == "auth" else "limited", failure)
                remaining = [key for key in self._route_keys(store)
                             if key.id != self._key_id]
                if not remaining:
                    self._step(store, "error", "所有 key 都失败",
                               f"{self._key_label or '默认'}：{failure[:400]}\n"
                               "渠道已没有可用 key；到「渠道」里补 key 或恢复状态。")
                    break
                self._step(store, "system", "换 key 重试",
                           f"{self._key_label or '默认'} 失败（{outcome}），"
                           f"改用 {remaining[0].label} 重试")
                self._reset_attempt_state()
            if self._backend == "command":
                if not self._result_text:
                    tail = self._output_lines[-20:]
                    self._result_text = "\n".join(tail)[-6000:]
                self._ok = code == 0
            after, after_truncated = snapshot(workspace)
            changed = diff(before, after, before_truncated=before_truncated,
                           after_truncated=after_truncated)

            artifact_ids: list[str] = []
            for relative in list(changed.changed)[:MAX_FILES]:
                try:
                    artifact = base.publish_artifact(
                        store, project_id=run["project_id"], workspace=workspace,
                        relative_path=relative, agent_id=run["agent_id"],
                        task_id=run["task_id"])
                    artifact_ids.append(str(artifact["id"]))
                    self._step(store, "artifact", f"登记成果物 · {relative}",
                               f"{artifact.get('size_bytes', 0)} B")
                except Exception as error:  # noqa: BLE001
                    self._step(store, "error", f"登记失败 · {relative}",
                               f"{type(error).__name__}: {error}")

            ok = code == 0 and self._ok and not self._stop.is_set() and not self._stalled
            status = "STOPPED" if self._stop.is_set() else ("DONE" if ok else "FAILED")
            summary = self._result_text or ("任务已停止" if status == "STOPPED" else "任务结束")
            if self._stalled:
                self._step(store, "error", "运行停滞",
                           f"{STALL_TIMEOUT}s 没有新输出，已终止进程；已写出的文件仍会登记。")
            try:
                base.finish_task(store, task_id=run["task_id"], agent_id=run["agent_id"],
                                 lease_token=lease_token, success=ok,
                                 summary=_text(summary, 4000),
                                 output_artifact_ids=artifact_ids)
            except Exception as error:  # noqa: BLE001
                self._step(store, "error", "任务收尾失败", f"{type(error).__name__}: {error}")

            studio_store.update_agent_run(
                store, self.run_id, status=status, finished_at=_now(),
                session_id=self._session_id or self._new_session_id, usage=self._usage,
                artifact_ids=artifact_ids,
                error="" if ok else ("stalled_no_output" if self._stalled
                                     else _text(self._result_text or "agent exited with error", 2000)))
            session_id = self._session_id or self._new_session_id
            if session_id:
                studio_store.update_agent(store, run["agent_id"], session_id=session_id)
            final = studio_store.get_agent_run(store, self.run_id)
            if not self._result_text:
                self._step(store, "result", "结束", _text(summary, 4000),
                           payload={"run": final})
            self.broker.publish(self.run_id, {"type": "run", "run": final})
        except Exception as error:  # noqa: BLE001
            self.error = f"{type(error).__name__}: {error}"
            try:
                run = studio_store.get_agent_run(store, self.run_id)
                if run:
                    self._step(store, "error", "运行异常", self.error)
                    self._fail(store, run, self.error)
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._cleanup_settings()
            self.broker.close(self.run_id)
            store.close()

    def _cleanup_settings(self) -> None:
        paths = [path for path in (self._settings_path, self._mcp_path) if path]
        for path in paths:
            try:
                path.unlink()
            except OSError:
                pass
            try:
                path.parent.rmdir()
            except OSError:
                pass
        self._settings_path = None
        self._mcp_path = None

    def _route_keys(self, store: Any) -> list[studio_routing.RouteKey]:
        """渠道 key 池里当前可用的 key，按顺序；每次换 key 都重新读，跳过刚失败的。"""

        if not self._provider:
            self._key_id = ""
            self._key_label = "本机默认"
            self._route_key_list = []
            return []
        if not self._route_key_list:
            self._route_key_list = studio_routing.usable_keys(store, self._provider)
        remaining = [key for key in self._route_key_list if key.id != self._key_id]
        if self._key_id and remaining:
            self._route_key_list = remaining
        current = self._route_key_list[0] if self._route_key_list else None
        if current:
            self._key_id = current.id
            self._key_label = current.label
        else:
            self._key_id = ""
            self._key_label = "无可用 key"
        return list(self._route_key_list)

    def _current_api_key(self) -> str:
        for key in self._route_key_list:
            if key.id == self._key_id:
                return key.api_key
        return str((self._provider or {}).get("api_key") or "")

    def _failure_text(self, code: int) -> str:
        parts = [self._result_text or "", self.error or "",
                 "\n".join(self._output_lines[-6:])]
        parts.append(f"exit={code}")
        return "\n".join(part for part in parts if part)[:4000]

    def _reset_attempt_state(self) -> None:
        self._result_text = ""
        self._ok = False
        self._usage = {}
        self._session_announced = False
        self._stalled = False
        self._output_lines = []
        self._cleanup_settings()

    def _remember_session(self, store: Any, run: dict[str, Any], session_id: str) -> None:
        sid = str(session_id or "").strip()
        if not sid or self._session_recorded == sid:
            return
        self._session_recorded = sid
        try:
            studio_store.update_agent_run(store, self.run_id, session_id=sid)
            studio_store.ensure_agent_session(
                store, session_id=sid, agent_id=str(run["agent_id"]),
                title=str(run.get("task_text") or "")[:80])
            studio_store.update_agent(store, str(run["agent_id"]), session_id=sid)
        except Exception:  # noqa: BLE001 — 会话索引失败不影响主流程
            pass

    def _fail(self, store: Any, run: dict[str, Any], error: str) -> None:
        studio_store.update_agent_run(store, self.run_id, status="FAILED",
                                      finished_at=_now(), error=_text(error, 2000))
        final = studio_store.get_agent_run(store, self.run_id)
        self.broker.publish(self.run_id, {"type": "run", "run": final})

    # ---- 命令与流 ----------------------------------------------------------

    def _command(self, agent: dict[str, Any], run: dict[str, Any]) -> list[str] | None:
        if self._backend == "command":
            return self._command_custom(agent, run)
        executable = find_claude()
        if not executable:
            return None
        tools = "Write,Read,Edit" + (",Bash" if agent.get("allow_bash") else "") + (
            ",WebSearch,WebFetch" if agent.get("allow_web", True) else "")
        command = [executable]
        # Claude Code 会把 ~/.claude/settings.json 的 env 合并进来（优先级高于进程环境）。
        # 选了渠道时必须用 --settings 重新声明 ANTHROPIC_*，否则会继续打到旧地址。
        if self._provider:
            self._settings_path = self._write_claude_settings()
            command += ["--settings", str(self._settings_path)]
        if agent.get("mcp_managed") or self._plugin_parts.get("mcp"):
            self._mcp_path = self._write_mcp_config(agent)
            command += ["--mcp-config", str(self._mcp_path)]
            if agent.get("mcp_managed"):
                command += ["--strict-mcp-config"]
        resume = str(agent.get("session_id") or "").strip()
        if resume:
            # 续接同一会话；模型仍显式传，保证换了渠道/模型后下一次任务生效
            self._session_id = resume
            command += ["--resume", resume]
            prompt = (
                f"继续同一个会话。新任务：{run.get('task_text')}\n\n"
                f"工作区：{run.get('workspace')}\n\n"
                "要求：直接做，不要反问；产出写成工作区文件；完成后一段话总结做了什么、产出哪些文件。\n"
            )
        else:
            self._new_session_id = str(uuid4())
            command += ["--session-id", self._new_session_id]
            prompt = (
                f"你是本地工作台里的 Agent「{agent['name']}」。\n"
                f"角色设定：{agent.get('system_prompt') or '你是一个可靠的执行型 Agent。'}\n\n"
                f"任务：{run.get('task_text')}\n\n"
                f"工作区：{run.get('workspace')}\n\n"
                "要求：\n"
                "1. 直接开始做，不要反问；需要信息就基于工作区现有文件判断。\n"
                "2. 产出必须写成工作区里的文件（Markdown/代码/数据都行），文件名清晰。\n"
                "3. 只读写工作区内的文件；可以使用 Read/Write/Edit 工具。\n"
                "4. 完成后用一段话总结：做了什么、产出哪些文件、还有什么没做。\n"
            )
        if self._model:
            command += ["--model", self._model]
        command += [
            "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--permission-mode", "acceptEdits",
            "--allowedTools", tools,
        ]
        return command

    def _command_custom(self, agent: dict[str, Any], run: dict[str, Any]) -> list[str] | None:
        template = str(agent.get("command") or "").strip()
        if not template:
            return None
        prompt = (
            f"你是本地工作台里的 Agent「{agent['name']}」。\n"
            f"角色设定：{agent.get('system_prompt') or '你是一个可靠的执行型 Agent。'}\n\n"
            f"任务：{run.get('task_text')}\n\n"
            f"工作区：{run.get('workspace')}\n"
            f"模型：{self._model or '默认'}\n"
            f"渠道：{self._provider['name'] if self._provider else '本机默认'}\n\n"
            "要求：直接做，不要反问；产出写成工作区文件；完成后一段话总结做了什么、产出哪些文件。\n"
        )
        memory = studio_capabilities.memory_context(str(run.get("workspace") or ""))
        if memory:
            prompt += f"\n长期记忆（工作区 CLAUDE.md/AGENTS.md）：\n{memory}\n"
        values = {
            "prompt": prompt,
            "workspace": str(run.get("workspace") or ""),
            "model": self._model,
            "agent_id": str(agent.get("id") or ""),
            "state": str(self.state_path or ""),
        }
        try:
            tokens = shlex.split(template, posix=(os.name != "nt"))
        except ValueError as error:
            self._step_store("error", "命令模板无法解析", str(error))
            return None
        if os.name == "nt":
            tokens = [token.strip('"') for token in tokens]
        command = [_fill_token(token, values) for token in tokens]
        if not command:
            return None
        if "{prompt}" not in template:
            command.append(prompt)
        return command

    def _write_claude_settings(self) -> Path:
        """每次运行一份 0600 的 settings，把渠道 env 固定到这次调用。"""

        base = (Path(self.state_path).expanduser().resolve().parent
                if self.state_path else Path.home() / ".synapforge")
        path = base / "studio" / "agent-runs" / self.run_id / "claude-settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        env = studio_providers.env_for(self._provider, self._model,
                                       api_key=self._current_api_key())
        payload = {"env": {key: value for key, value in env.items()
                           if key.startswith("ANTHROPIC_")}}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path

    def _write_mcp_config(self, agent: dict[str, Any]) -> Path:
        base = (Path(self.state_path).expanduser().resolve().parent
                if self.state_path else Path.home() / ".synapforge")
        path = base / "studio" / "agent-runs" / self.run_id / "mcp-config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = studio_capabilities.mcp_payload(
            Path.home(), agent.get("mcp") or [], [agent])
        servers = dict(payload["mcpServers"])
        servers.update(self._plugin_parts.get("mcp") or {})
        path.write_text(json.dumps({"mcpServers": servers},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path

    def _stream(self, command: list[str], cwd: Path) -> int:
        env = _base_env(studio_providers.env_for(self._provider, self._model,
                                                api_key=self._current_api_key()))
        env.setdefault("CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT", "1")
        env.setdefault("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "1000000")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            proc = subprocess.Popen(
                command, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", shell=False,
                creationflags=creationflags)
        except OSError as error:
            self._step_store("error", "启动失败", f"{type(error).__name__}: {error}")
            return 126
        with self._proc_lock:
            self._proc = proc
        timer = threading.Timer(DEFAULT_TIMEOUT, lambda: self._kill(proc))
        timer.daemon = True
        timer.start()
        last_activity = [time.time()]

        def watch() -> None:
            while proc.poll() is None:
                if self._stop.is_set():
                    return
                if time.time() - last_activity[0] > STALL_TIMEOUT:
                    self._stalled = True
                    self._kill(proc)
                    return
                time.sleep(5)

        watchdog = threading.Thread(target=watch, daemon=True)
        watchdog.start()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                last_activity[0] = time.time()
                self._handle_line(line)
                if self._stop.is_set():
                    break
            code = proc.wait()
        finally:
            timer.cancel()
            with self._proc_lock:
                self._proc = None
        if self._stalled:
            return -3
        return int(code)

    def _kill(self, proc: subprocess.Popen) -> None:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass

    def _parser(self) -> StepParser:
        if getattr(self, "_step_parser", None) is None:
            self._step_parser = StepParser(
                emit=lambda kind, title, detail, payload: self._step_store(
                    kind, title, detail, payload),
                update=lambda step_id, title, detail: self._update_step(
                    step_id, title, detail),
                publish=lambda event: self.broker.publish(self.run_id, event),
            )
        return self._step_parser

    def _update_step(self, step_id: str, title: str, detail: str) -> None:
        store = self._store()
        try:
            studio_store.update_agent_step(store, step_id, title=title, detail=detail)
        finally:
            store.close()

    def _handle_line(self, line: str) -> None:
        raw = (line or "").strip()
        if not raw:
            return
        parser = self._parser()
        parser.on_session = self._remember_session_id
        parser.feed(raw, backend=self._backend)
        self._session_id = parser.session_id or self._session_id
        self._model = parser.model or self._model
        self._result_text = parser.result_text or self._result_text
        if parser.result_text:
            self._ok = parser.ok
        if parser.usage:
            self._usage = dict(parser.usage)
            if self._model:
                store = self._store()
                try:
                    studio_store.update_agent_run(store, self.run_id, usage=self._usage)
                finally:
                    store.close()

    def _remember_session_id(self, session_id: str) -> None:
        if not session_id or session_id == self._session_recorded:
            return
        store = self._store()
        try:
            run = studio_store.get_agent_run(store, self.run_id)
            if run:
                self._remember_session(store, run, session_id)
        finally:
            store.close()

    def _merge_usage(self, usage: dict[str, Any], result: dict[str, Any] | None = None) -> None:
        merged = dict(self._usage)
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                    "cache_creation_input_tokens"):
            value = usage.get(key)
            if value is not None:
                merged[key] = int(value)
        if result:
            model_usage = result.get("modelUsage") or {}
            if isinstance(model_usage, dict) and model_usage:
                first = next(iter(model_usage.values()))
                if isinstance(first, dict):
                    merged["context_window"] = first.get("contextWindow")
                    merged["canonical_model"] = first.get("canonicalModel")
            if result.get("total_cost_usd") is not None:
                merged["total_cost_usd"] = result.get("total_cost_usd")
            if result.get("duration_ms") is not None:
                merged["duration_ms"] = result.get("duration_ms")
        merged["model"] = self._model
        self._usage = merged
        if self._model:
            store = self._store()
            try:
                studio_store.update_agent_run(store, self.run_id, usage=merged)
            finally:
                store.close()

    def _store(self) -> Any:
        return open_store(self.state_path)

    def _step(self, store: Any, kind: str, title: str, detail: str = "",
              payload: dict[str, Any] | None = None) -> dict[str, Any]:
        step = studio_store.append_agent_step(
            store, run_id=self.run_id, kind=kind, title=title,
            detail=detail or "", payload=payload)
        self.broker.publish(self.run_id, {"type": "step", "step": step})
        return step

    def _step_store(self, kind: str, title: str, detail: str = "",
                    payload: dict[str, Any] | None = None) -> dict[str, Any]:
        store = self._store()
        try:
            return self._step(store, kind, title, detail, payload)
        finally:
            store.close()


def _fill_token(template: str, values: dict[str, str]) -> str:
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", str(value or ""))
    return out


def _tool_input(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("file_path", "path", "command", "description", "pattern", "query"):
            if value.get(key):
                return str(value[key])
        return json.dumps(value, ensure_ascii=False)[:2000]
    return str(value or "")


def _tool_result(value: Any) -> str:
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts) or json.dumps(value, ensure_ascii=False)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value or "")


class AgentWorker(threading.Thread):
    """一个 Agent 的串行队列：按顺序把 QUEUED 的 run 跑完。"""

    def __init__(self, agent_id: str, state_path: str | Path | None,
                 broker: RunBroker) -> None:
        super().__init__(name=f"agent-worker-{agent_id[-6:]}", daemon=True)
        self.agent_id = str(agent_id)
        self.state_path = state_path
        self.broker = broker
        self._halt = threading.Event()
        self._lock = threading.Lock()
        self._current: AgentRunWorker | None = None

    def stop(self) -> None:
        self._halt.set()
        with self._lock:
            current = self._current
        if current:
            current.stop()

    def stop_run(self, run_id: str) -> bool:
        with self._lock:
            current = self._current
        if current and current.run_id == str(run_id):
            current.stop()
            return True
        return False

    def run(self) -> None:  # noqa: D401 — Thread.run
        while not self._halt.is_set():
            store = open_store(self.state_path)
            try:
                queued = studio_store.next_queued_run(store, self.agent_id)
            finally:
                store.close()
            if queued is None:
                break
            store = open_store(self.state_path)
            try:
                studio_store.update_agent_run(store, queued["id"], status="RUNNING")
                running = studio_store.get_agent_run(store, queued["id"])
            finally:
                store.close()
            self.broker.publish(queued["id"], {"type": "run", "run": running})
            runner = AgentRunWorker(queued["id"], self.state_path, self.broker)
            with self._lock:
                self._current = runner
            try:
                runner._run()  # noqa: SLF001 — 同一个模块内的执行核心
            except Exception:  # noqa: BLE001 — 单个 run 失败不能拖垮队列
                pass
            finally:
                with self._lock:
                    self._current = None


class AgentRuntimeManager:
    """studio 进程内的 Agent 队列线程表 + SSE 订阅表。"""

    def __init__(self, state_path: str | Path | None = None) -> None:
        self.state_path = state_path
        self.broker = RunBroker()
        self._lock = threading.Lock()
        self._workers: dict[str, AgentWorker] = {}
        self._recover()

    def _recover(self) -> None:
        store = open_store(self.state_path)
        queued_agents: list[str] = []
        try:
            studio_store.ensure_schema(store)
            rows = store.db.execute(
                "SELECT id FROM local_agent_runs WHERE status = 'RUNNING'").fetchall()
            for row in rows:
                studio_store.update_agent_run(
                    store, row["id"], status="FAILED", finished_at=_now(),
                    error="studio_restarted")
            queued = store.db.execute(
                "SELECT DISTINCT agent_id FROM local_agent_runs WHERE status = 'QUEUED'").fetchall()
            queued_agents = [row["agent_id"] for row in queued]
        finally:
            store.close()
        for agent_id in queued_agents:
            self.start_agent(agent_id)

    def start_agent(self, agent_id: str) -> None:
        with self._lock:
            worker = self._workers.get(agent_id)
            if worker and worker.is_alive():
                return
            worker = AgentWorker(agent_id, self.state_path, self.broker)
            self._workers[agent_id] = worker
            worker.start()

    def stop_run(self, run_id: str) -> None:
        run = None
        store = open_store(self.state_path)
        try:
            run = studio_store.get_agent_run(store, run_id)
            if run and run["status"] in ("QUEUED", "RUNNING"):
                studio_store.update_agent_run(
                    store, run_id, status="STOPPED", finished_at=_now(),
                    error="stopped_by_user")
        finally:
            store.close()
        if run:
            worker = self._workers.get(run["agent_id"])
            if worker:
                worker.stop_run(run_id)

    def stop_agent(self, agent_id: str) -> None:
        worker = self._workers.get(agent_id)
        if worker:
            worker.stop()

    def subscribe(self, run_id: str) -> queue.Queue:
        return self.broker.subscribe(run_id)

    def unsubscribe(self, run_id: str, box: queue.Queue) -> None:
        self.broker.unsubscribe(run_id, box)

    def shutdown(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
        for worker in workers:
            worker.stop()
        for worker in workers:
            worker.join(timeout=2)

