"""「对话」轮询循环（MY-AGENT M-1）：取一条轮次 → 跑执行体 → 过程事件与结果回传。

为什么要独立于任务循环（用户拍板"对话就是单纯对话"）：对话不建任务、不进任务板、不走复核。
但它与任务循环共享**同一个执行体槽位**（外部传入的 `asyncio.Lock`）——一台机器同时只跑一个执行体进程：
两个 opencode 并发会互相抢 CPU/内存，而且输出混在一起后"这条是谁的"说不清。

形状与任务循环保持一致：空队列退避轮询；暂停/紧急停止/没有授权时不取活；身份每次从 provider 现读
（`/grant` 会在不重启内核的前提下替换身份）。

过程事件走 HTTP **逐条回传**（best-effort：丢一条进度不等于丢结果，失败只记日志）；
结果走 `complete`，带上正文、用量与 **opencode 会话句柄**（下一轮靠它续上下文）。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

try:  # 包内导入（正常运行）
    from .chat_outputs import ChatOutputCollector
    from .executor_events import ExecutorEventReporter
    from .input_fetcher import materialize_inputs, prompt_with_inputs
    from .result_uploader import AgentArtifactClient
    from .local_state import LocalAgentState
    from .machine_service import LocalProcessSupervisor, ProcessSpec
    from .opencode_executor import parse_opencode_jsonl
    from .opencode_server import OpenCodeServer, OpenCodeServerConfig, run_serve_turn
except ImportError:  # 直接脚本执行 / 测试以顶层模块导入（与 agentd 同一套兼容写法）
    from chat_outputs import ChatOutputCollector  # type: ignore
    from executor_events import ExecutorEventReporter  # type: ignore
    from input_fetcher import materialize_inputs, prompt_with_inputs  # type: ignore
    from result_uploader import AgentArtifactClient  # type: ignore
    from local_state import LocalAgentState  # type: ignore
    from machine_service import LocalProcessSupervisor, ProcessSpec  # type: ignore
    from opencode_executor import parse_opencode_jsonl  # type: ignore
    from opencode_server import OpenCodeServer, OpenCodeServerConfig, run_serve_turn  # type: ignore

# 平台没给模板时的兜底（与 deploy/cloud-agent 的部署模板一致）
DEFAULT_CHAT_TEMPLATE = ("opencode", "run", "--format", "json", "--auto", "{prompt}")
PROTOCOL_BY_NAME = {"opencode": "opencode", "codex": "codex"}

# 明确要求用户拍板时的 UI 协议：前端只解析消息末尾的这个 fence；普通编号列表不作猜测。
# 注入内核层而不是只写某个角色提示词，确保所有角色的选择都能从选项卡回传。
CHOICE_PROTOCOL_INSTRUCTION = r"""

[平台交互协议：结构化用户选择]
当你确实需要用户在多个互斥/可多选的选项间拍板、或需要同时回答多项确认问题时：
1. 先用普通 Markdown 简短解释为什么需要选择；不要只输出协议块。
2. 在回复**最后**追加且只追加一个 `synapforge-choice` fenced block，内容为合法 JSON，形状如下：
```synapforge-choice
{"id":"稳定的短标识","title":"选择卡标题","description":"可选说明","questions":[{"id":"topic","label":"主题范围","type":"single","required":true,"options":[{"value":"llm","label":"LLM 多智能体协作","description":"可选简述"},{"value":"marl","label":"多智能体强化学习 MARL"}]},{"id":"format","label":"产物格式","type":"multiple","required":true,"options":[{"value":"md","label":"Markdown 表格"},{"value":"bib","label":"BibTeX"}]},{"id":"extra","label":"其他要求","type":"text","required":false,"placeholder":"可选补充"}]}
```
3. `questions` 必须是数组；每项的 `id` 在卡片内唯一，`type` 只能是 `single`（单选）、`multiple`（多选）、`text`（补充输入）；单选/多选必须给至少一个 `options`，选项必须有稳定 `value` 与可读 `label`。必填项设 `required:true`。建议 2–8 个问题，每题 2–6 个选项。
4. 不要要求用户再回复“按默认”“回复 1–6”或把选项重新抄成自由文本；卡片底部会有「提交选择」按钮，提交后选择会作为下一条用户消息发回给你。
5. 仅在真正需要拍板时输出协议块。一般解释、建议、明确说“按默认即可”的情况不要强行出卡片。若信息缺失但用户无需作选择，正常直接提问。
6. JSON 必须严格有效：双引号、无注释、无尾逗号；协议块必须是回复最后内容。若不需要卡片，绝不输出 `synapforge-choice`。
"""

IdentityProvider = Callable[[], dict[str, Any]]
RunCommand = Callable[..., Awaitable[tuple[int | None, str, str]]]


class _TurnApprovals:
    """一轮的**审批通道**（M-5c S-3）：把执行体的权限请求变成平台上的一张"待批准"卡片，并等答案。

    为什么是轮询：执行体这一侧本来就在轮询（取活、取消都是），审批用同一个形状，
    平台不需要为此新增推送通道；等待有**上限**，超时就按「没人批 = 不执行」处理。

    `wait` 返回 opencode 认的三档之一（`once`/`always`/`reject`）；返回 None 表示"等不到了"。
    """

    def __init__(
        self,
        *,
        url: str,
        token: str,
        agent_id: str,
        turn_id: str,
        poll_seconds: float = 3.0,
        log: Callable[[str], None] = print,
    ) -> None:
        self.base = f"{url.rstrip('/')}/api/agents/{agent_id}/chat-turns/{turn_id}/approvals"
        self.token = token
        self.agent_id = agent_id
        self.poll_seconds = max(0.5, float(poll_seconds))
        self.log = log

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        request = urllib.request.Request(
            self.base + path,
            data=None if payload is None else json.dumps(payload).encode("utf-8"),
            method=method,
            headers={"Content-Type": "application/json", "X-Project-Capability-Token": self.token, "X-Agent-Id": self.agent_id},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
        return json.loads(raw) if raw.strip() else None

    def report(self, payload: dict[str, Any]) -> None:
        self._request("POST", "", payload)

    def wait(self, request_id: str, *, timeout: float, cancel_event: threading.Event | None = None) -> str | None:
        deadline = time.monotonic() + max(1.0, float(timeout))
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                return None
            state = self._request("GET", f"/{request_id}")
            status = str((state or {}).get("status") or "")
            if status and status != "PENDING":
                if status == "EXPIRED":
                    return None
                decision = str((state or {}).get("decision") or "")
                # 兼容"只有状态没有决定"的老数据：DENIED → reject，其余按"批准这一次"处理
                return decision or ("reject" if status == "DENIED" else "once")
            time.sleep(self.poll_seconds)
        return None

    def expire(self, request_id: str) -> None:
        self._request("POST", f"/{request_id}/expire", {})


def build_chat_command(
    template: Any,
    *,
    prompt: str,
    session_key: str | None = None,
    model: str | None = None,
    agent_role: str | None = None,
) -> list[str]:
    """把模板变成实际 argv：替换 `{prompt}`，并在它**前面**插入可选的 `--session` / `-m` / `--agent`。

    为什么插在 `{prompt}` 前面：提示词在模板里通常是"最后一个位置参数"，把开关插到它后面会被 CLI
    当成位置参数的一部分（`opencode run "..." --session x` 里 `--session` 会被吃进提示词）。
    """

    items = [str(part) for part in (template or DEFAULT_CHAT_TEMPLATE)]
    index = next((position for position, part in enumerate(items) if "{prompt}" in part), None)
    if index is None:
        raise ValueError("chat_command_template_requires_prompt_placeholder")
    extra: list[str] = []
    if session_key:
        extra += ["--session", str(session_key)]
    if model:
        extra += ["-m", str(model)]
    if agent_role:
        # 角色（M-6）：opencode 的 `--agent <名>` 选中执行体上的那个角色定义（一个 md 文件）。
        # 名字来自执行体探测（心跳上报），平台只存"选了谁"；名字不存在时 opencode 自己会报错，这里不猜。
        extra += ["--agent", str(agent_role)]
    items[index] = items[index].replace("{prompt}", prompt)
    return items[:index] + extra + items[index:]


@dataclass
class ChatLoopConfig:
    url: str
    workspace: Path
    state_path: Path
    device_id: str = ""
    idle_seconds: float = 3.0
    max_idle_seconds: float = 30.0
    turn_timeout_seconds: float = 900.0
    claim_lease_seconds: int = 1800
    # 跑的过程中多久查一次"这一轮被取消了吗"（M-4 真中断：查到就杀掉子进程）
    cancel_poll_seconds: float = 3.0
    # M-5c S-1：常驻 `opencode serve` 通道。端口为 0（默认）时**完全走原来的 CLI 通道**——
    # 不配就不启用，免得没有该二进制/端口的机器被动受影响。
    chat_server_port: int = 0
    chat_server_executable: str = "opencode"
    chat_server_startup_seconds: float = 25.0
    # serve 模式下对权限请求的默认策略：True = 与 CLI 通道的 `--auto` 等价（一律放行）。
    # S-3 起，常驻通道默认改走**真卡片**（`chat_approvals=True`），这条只作为"没接审批通道"时的兜底。
    auto_approve_permissions: bool = True
    # S-3：常驻通道是否把权限请求做成页面上的待批准卡片（CLI 回退通道没有权限事件，仍是 `--auto`）
    chat_approvals: bool = True
    # 等人批的上限：超时按「没人批 = 不执行」拒绝（有界等待，不会把轮次挂死）
    chat_approval_timeout_seconds: float = 300.0
    chat_approval_poll_seconds: float = 3.0


@dataclass
class ChatLoop:
    """一条一条地取「对话」轮次并执行；串行、可暂停、可被打断（协程取消）。"""

    config: ChatLoopConfig
    identity_provider: IdentityProvider
    enabled_provider: Callable[[], bool] = lambda: True
    environment_provider: Callable[[], dict[str, str]] = dict
    run_command: RunCommand | None = None
    log: Callable[[str], None] = print
    slot: asyncio.Lock | None = None
    _idle: float = field(default=0.0)
    _completed: int = field(default=0, init=False)
    _failed: int = field(default=0, init=False)
    _server: OpenCodeServer | None = field(default=None, init=False)
    # 对话产出采集器（懒建：要用到项目令牌与执行体身份）
    _outputs: ChatOutputCollector | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._idle = float(self.config.idle_seconds)

    # ---- 对外状态（壳/日志用）------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "chat_completed": self._completed,
            "chat_failed": self._failed,
            "chat_idle_seconds": round(self._idle, 1),
            **(self._server.snapshot() if self._server is not None else {}),
        }

    def close(self) -> None:
        """退场清理：常驻 server 是内核拉起来的子进程，内核不在了就不该留着。"""

        if self._server is not None:
            self._server.stop()

    # ---- HTTP ---------------------------------------------------------------

    def _call(self, path: str, payload: dict[str, Any], token: str, *, best_effort: bool = False) -> Any:
        request = urllib.request.Request(
            f"{self.config.url.rstrip('/')}{path}",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
        )
        request.add_header("Content-Type", "application/json")
        request.add_header("X-Project-Capability-Token", token)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:200]
            message = f"[chat] {path} -> http_{error.code}:{detail}"
            if best_effort:
                self.log(message)
                return None
            self.log(message)
            return None
        except (urllib.error.URLError, TimeoutError) as error:
            self.log(f"[chat] {path} 失败：{type(error).__name__}")
            return None
        return json.loads(raw) if raw.strip() else None

    # ---- 单轮 ---------------------------------------------------------------

    async def run_once(self) -> bool:
        """取一条待办轮次并跑完；没有待办返回 False（常量时间返回，便于退避）。"""

        identity = self.identity_provider() or {}
        project_id = str(identity.get("project_id") or "")
        token = str(identity.get("project_token") or "")
        agent_id = str(identity.get("agent_id") or "")
        if not project_id or not agent_id or len(token) < 16:
            return False

        claim = self._call(
            f"/api/agents/{agent_id}/chat-turns/claim",
            {"agent_id": agent_id, "project_id": project_id, "lease_seconds": int(self.config.claim_lease_seconds)},
            token,
        )
        if not claim or not isinstance(claim.get("turn"), dict):
            return False
        turn = claim["turn"]
        turn_id = str(turn.get("id") or "")
        if not turn_id:
            return False
        protocol = PROTOCOL_BY_NAME.get(str(claim.get("worker_events") or "opencode"), "none")
        # 输入文件：这一轮带的附件先下到 <workspace>/inputs/，再让执行体跑（M-3）
        prompt = str(turn.get("prompt") or "")
        input_files = claim.get("input_files") or []
        if input_files:
            written, failed = materialize_inputs(
                AgentArtifactClient(self.config.url, token, agent_id),
                input_files,
                workspace=Path(self.config.workspace),
                log=self.log,
            )
            prompt = prompt_with_inputs(prompt, written, failed)
        # 结构化选择协议：**每一轮**都在提示词末尾追加协议说明（不只限附件轮），
        # 这样任何角色在"需要用户拍板"时都能回一个能被页面渲染成选项卡的块。
        prompt = prompt + CHOICE_PROTOCOL_INSTRUCTION
        try:
            command = build_chat_command(
                claim.get("command_template"),
                prompt=prompt,
                session_key=turn.get("session_key"),
                model=turn.get("model"),
                agent_role=turn.get("role"),
            )
        except ValueError as error:
            self._finish(token, agent_id, turn_id, success=False, content="", usage={}, session_key=None, error=str(error))
            return True

        self.log(
            f"[chat] 取到轮次 {turn_id[:8]}（{protocol}，模型 {turn.get('model') or '默认'}，"
            f"角色 {turn.get('role') or '默认'}）"
        )
        # M-5c S-1：常驻 server 通道（只在执行体是 opencode、且配了端口时启用）。
        # **起得来就走它，起不来如实降级**到下面的 CLI 通道——两条通道的完成口径完全一致。
        if self._serve_enabled(protocol) and await self._run_via_server(
            token=token,
            agent_id=agent_id,
            turn_id=turn_id,
            turn=turn,
            prompt=prompt,
            identity=identity,
        ):
            return True

        # 把真正的 argv 记一行（**不含提示词正文**）：排查"角色到底传没传下去"时，
        # 这是唯一能一眼看到的证据——否则只能靠行为反推（`--agent` 插在提示词前，见 build_chat_command）。
        self.log("[chat] 命令（提示词已省略）：" + " ".join(str(part).replace(prompt, "{prompt}") for part in command))
        # CLI 通道也要采产出（两条通道口径一致）：执行前记快照，收尾时差分上传
        await self._collect_outputs(identity, before=True)
        sequence = iter(range(1, 1_000_000))

        def emit(event_type: str, payload: dict[str, Any]) -> None:
            # 逐条回传进度：失败只记日志，绝不影响这一轮的结果
            self._call(
                f"/api/agents/{agent_id}/chat-turns/{turn_id}/events",
                {
                    "event_type": event_type,
                    "sequence": next(sequence),
                    "payload": {key: value for key, value in payload.items() if value is not None},
                },
                token,
                best_effort=True,
            )

        reporter = ExecutorEventReporter(emit=emit, event_protocol=protocol)
        reporter.started(command, "chat", protocol=protocol)
        runner = self.run_command or self._default_run_command
        cancelled = False
        # 注意判据是"有没有注入自定义 runner"，**不是** `runner is self._default_run_command`：
        # 每次取绑定方法都是新对象，`is` 恒为假——线上就是这么漏掉取消轮询的（执行体照跑了 5 分钟）。
        if self.run_command is None:
            # 默认路径才有进程可杀；注入的 runner（测试）由它自己决定语义
            exit_code, stdout, stderr, cancelled = await runner(
                command,
                workspace=self.config.workspace,
                timeout_seconds=float(self.config.turn_timeout_seconds),
                on_stdout=reporter.feed,
                turn_id=turn_id,
                cancel_check=lambda: self._turn_cancelled(token, agent_id, turn_id),
                cancel_poll_seconds=float(self.config.cancel_poll_seconds),
            )
        else:
            exit_code, stdout, stderr = await runner(
                command,
                workspace=self.config.workspace,
                timeout_seconds=float(self.config.turn_timeout_seconds),
                on_stdout=reporter.feed,
                turn_id=turn_id,
            )
        reporter.flush()

        parsed = parse_opencode_jsonl(stdout) if protocol == "opencode" else {}
        content = str(parsed.get("final_message") or "").strip()
        if not content:
            content = stdout.strip()[-400:] if stdout.strip() else ""
        usage = reporter.usage() or parsed.get("usage") or {}
        if cancelled:
            # 已经被人停掉了：平台状态就是 CANCELLED，这里只记一笔，不去覆盖它（complete 也是幂等的）
            self._failed += 1
            self.log(f"[chat] 轮次 {turn_id[:8]} 被取消，已杀掉执行体进程")
            self._finish(
                token, agent_id, turn_id,
                success=False,
                content=content,
                usage=usage,
                session_key=None,
                error="cancelled_by_member",
                outputs=await self._collect_outputs(identity, before=False, turn_id=turn_id),
            )
            return True
        success = exit_code == 0
        error = "" if success else (stderr.strip()[-300:] or f"exit={exit_code}")
        self._finish(
            token,
            agent_id,
            turn_id,
            success=success,
            content=content,
            usage=usage,
            session_key=str(parsed.get("session_id") or "") or None,
            error=error,
            outputs=await self._collect_outputs(identity, before=False, turn_id=turn_id),
        )
        if success:
            self._completed += 1
            self.log(f"[chat] 轮次 {turn_id[:8]} 完成（{len(content)} 字）")
        else:
            self._failed += 1
            self.log(f"[chat] 轮次 {turn_id[:8]} 失败：{error[:120]}")
        return True

    def _outputs_for_turn(self, identity: dict[str, Any], *, before: bool, turn_id: str = "") -> list[dict[str, Any]]:
        """对话产出采集（2026-09-24 用户需求）：执行前记快照、执行后差分上传并返回给 `complete`。

        `before=True` 时只记快照；`before=False` 时采集。**任何失败都不改这一轮的成败**：
        采集失败最多是"这次没自动上传"，产出还在工作区里。
        """

        project_id = str(identity.get("project_id") or "")
        token = str(identity.get("project_token") or "")
        agent_id = str(identity.get("agent_id") or "")
        if not project_id or not agent_id or len(token) < 16:
            return []
        if self._outputs is None:
            self._outputs = ChatOutputCollector(
                workspace=Path(self.config.workspace),
                url=self.config.url,
                project_id=project_id,
                agent_id=agent_id,
                project_token=token,
                log=self.log,
            )
        if before:
            self._outputs.snapshot()
            return []
        try:
            result = self._outputs.collect(turn_id)
        except Exception as error:  # noqa: BLE001 - 采集绝不把这一轮搞崩（与 CL-1 的 I4 同一口径）
            self.log(f"[chat] 产出采集失败：{type(error).__name__}")
            return []
        if result.note() != "这一轮没有产出文件":
            self.log(f"[chat] {result.note()}")
        return [item.as_payload() for item in result.outputs]

    async def _collect_outputs(self, identity: dict[str, Any], *, before: bool, turn_id: str = "") -> list[dict[str, Any]]:
        """在线程里做（要扫目录、要上传，不能卡事件循环）。"""

        return await asyncio.to_thread(self._outputs_for_turn, identity, before=before, turn_id=turn_id)

    def _serve_enabled(self, protocol: str) -> bool:
        """要不要走常驻 server 通道：**必须两边都满足**——配了端口，且这一轮的执行体就是 opencode。

        （`protocol` 是平台按 `worker_events`/模板首词判出来的；executor 是 codex 之类时不能拿去喂 opencode 服务。）
        """

        return int(self.config.chat_server_port) > 0 and protocol == "opencode"

    def _ensure_server(self) -> OpenCodeServer | None:
        """确保常驻 server 可用；不可用返回 None（调用方降级，不抛异常）。"""

        if self._server is None:
            self._server = OpenCodeServer(
                OpenCodeServerConfig(
                    workspace=Path(self.config.workspace),
                    executable=self.config.chat_server_executable,
                    port=int(self.config.chat_server_port),
                    startup_timeout_seconds=float(self.config.chat_server_startup_seconds),
                ),
                log=self.log,
            )
        try:
            return self._server if self._server.ensure_running() else None
        except Exception as error:  # noqa: BLE001 - 起不来就降级，绝不把这一轮搞崩
            self.log(f"[chat] 常驻服务不可用：{type(error).__name__}（降级到 CLI 通道）")
            return None

    async def _run_via_server(
        self,
        *,
        token: str,
        agent_id: str,
        turn_id: str,
        turn: dict[str, Any],
        prompt: str,
        identity: dict[str, Any],
    ) -> bool:
        """用常驻 server 跑完这一轮；返回 True 表示"这一轮已经处理掉了"。"""

        # 产出采集：执行前记快照（后面差分出的新/改文件就是这一轮的产出）
        await self._collect_outputs(identity, before=True)

        server = await asyncio.to_thread(self._ensure_server)
        if server is None:
            return False
        sequence = iter(range(1, 1_000_000))

        def emit(event_type: str, payload: dict[str, Any]) -> None:
            self._call(
                f"/api/agents/{agent_id}/chat-turns/{turn_id}/events",
                {
                    "event_type": event_type,
                    "sequence": next(sequence),
                    "payload": {key: value for key, value in payload.items() if value is not None},
                },
                token,
                best_effort=True,
            )

        reporter = ExecutorEventReporter(emit=emit, event_protocol="opencode")
        self.log(f"[chat] 走常驻服务通道（端口 {self.config.chat_server_port}）")
        # S-3：权限请求走真卡片（页面上的"待批准"）；没开就退回 `--auto` 等价语义
        approvals = (
            _TurnApprovals(
                url=self.config.url,
                token=token,
                agent_id=agent_id,
                turn_id=turn_id,
                poll_seconds=float(self.config.chat_approval_poll_seconds),
                log=self.log,
            )
            if self.config.chat_approvals
            else None
        )
        outcome = await asyncio.to_thread(
            run_serve_turn,
            server.client,
            prompt=prompt,
            reporter=reporter,
            model=turn.get("model"),
            agent=turn.get("role"),
            session_key=turn.get("session_key"),
            # S-2：正文走 `delta` 事件（页面边生成边显示）。**故意绕开 reporter**——
            # 它有 1.5s 节流与 40 条上限，压掉一条增量就是丢正文；增量自己有节奏（见 opencode_server）。
            emit_delta=lambda payload: emit("delta", payload),
            approvals=approvals,
            approval_timeout_seconds=float(self.config.chat_approval_timeout_seconds),
            cancel_check=lambda: self._turn_cancelled(token, agent_id, turn_id),
            cancel_poll_seconds=float(self.config.cancel_poll_seconds),
            timeout=float(self.config.turn_timeout_seconds),
            auto_approve_permissions=bool(self.config.auto_approve_permissions),
            log=self.log,
        )
        if outcome.cancelled:
            # 与 CLI 通道同一口径：取消算失败、错误码固定 `cancelled_by_member`，不去覆盖平台的 CANCELLED 状态
            self._failed += 1
            self.log(f"[chat] 轮次 {turn_id[:8]} 被取消（已中止 server 上的会话）")
            self._finish(
                token, agent_id, turn_id,
                success=False,
                content=outcome.text,
                usage=outcome.usage,
                session_key=outcome.session_key or None,
                error="cancelled_by_member",
                outputs=await self._collect_outputs(identity, before=False, turn_id=turn_id),
            )
            return True
        self._finish(
            token, agent_id, turn_id,
            success=outcome.ok,
            content=outcome.text,
            usage=outcome.usage,
            session_key=outcome.session_key or None,
            error=outcome.error,
            outputs=await self._collect_outputs(identity, before=False, turn_id=turn_id),
        )
        if outcome.ok:
            self._completed += 1
            self.log(f"[chat] 轮次 {turn_id[:8]} 完成（{len(outcome.text)} 字，serve 通道）")
        else:
            self._failed += 1
            self.log(f"[chat] 轮次 {turn_id[:8]} 失败：{outcome.error[:120]}")
        return True

    def _turn_cancelled(self, token: str, agent_id: str, turn_id: str) -> bool:
        """这一轮被取消了吗（平台侧 /stop 会把状态改成 CANCELLED）。

        查不到（网络抖动）就当**没取消**：宁可多跑一会儿，也不因为一次查询失败误杀正在干的活。
        """

        request = urllib.request.Request(
            f"{self.config.url.rstrip('/')}/api/agents/{agent_id}/chat-turns/{turn_id}",
            method="GET",
            headers={"X-Project-Capability-Token": token, "X-Agent-Id": agent_id},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                state = json.loads(response.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 - 查询失败不误杀
            return False
        return str((state or {}).get("status") or "") == "CANCELLED"

    def _finish(
        self,
        token: str,
        agent_id: str,
        turn_id: str,
        *,
        success: bool,
        content: str,
        usage: dict[str, Any],
        session_key: str | None,
        error: str,
        outputs: list[dict[str, Any]] | None = None,
    ) -> None:
        self._call(
            f"/api/agents/{agent_id}/chat-turns/{turn_id}/complete",
            {
                "success": success,
                "content": content,
                "usage": usage,
                "session_key": session_key,
                "error": error,
                # 这一轮产出的文件（成果物，待审）：页面据此给出「下载 / 转入云盘」
                "outputs": outputs or [],
            },
            token,
        )

    async def _default_run_command(
        self,
        command: list[str],
        *,
        workspace: Path,
        timeout_seconds: float,
        on_stdout: Callable[[str], None],
        turn_id: str,
        cancel_check: Callable[[], bool] | None = None,
        cancel_poll_seconds: float = 3.0,
    ) -> tuple[int | None, str, str, bool]:
        """真正起进程。

        `stdin_enabled=False` 是有意的（内核会给 /dev/null）：opencode 在 stdin 不是 EOF 时会**挂住**
        ——这条是 CLOUD-1 实测踩出来的，别再改回去。
        """

        identity = self.identity_provider() or {}
        state = LocalAgentState(str(self.config.state_path))
        try:
            supervisor = LocalProcessSupervisor(
                state,
                5.0,
                output_callback=lambda _spec, stream, chunk: on_stdout(chunk) if stream == "stdout" else None,
            )
            spec = ProcessSpec(
                run_id=f"chat-{uuid4().hex[:12]}",
                command=tuple(command),
                project_id=str(identity.get("project_id") or "chat"),
                task_id=None,
                agent_id=str(identity.get("agent_id") or "chat"),
                device_id=str(self.config.device_id or identity.get("device_id") or "chat"),
                workspace_id="chat",
                workspace_path=str(workspace),
                cwd=str(workspace),
                env=dict(self.environment_provider() or {}),
                inherit_environment=False,
                timeout_seconds=timeout_seconds,
                stdin_enabled=False,
            )
            handle = await supervisor.start(spec)
            cancelled = False

            async def watch() -> None:
                """轮询"被取消了吗"：一旦取消就停掉这个进程（真中断，而不是等它自己跑完）。"""

                nonlocal cancelled
                while True:
                    await asyncio.sleep(max(1.0, float(cancel_poll_seconds)))
                    if cancel_check is not None and cancel_check():
                        cancelled = True
                        await supervisor.stop(handle.process_id)
                        return

            watcher = asyncio.create_task(watch()) if cancel_check is not None else None
            try:
                result = await supervisor.wait(handle.process_id)
            finally:
                if watcher is not None:
                    watcher.cancel()
                    await asyncio.gather(watcher, return_exceptions=True)
            return result.exit_code, result.stdout or "", result.stderr or "", cancelled
        finally:
            state.close()

    # ---- 循环 ---------------------------------------------------------------

    async def run_forever(
        self,
        stop: asyncio.Event | None = None,
        *,
        stop_when: Callable[[], bool] | None = None,
    ) -> None:
        while True:
            if (stop is not None and stop.is_set()) or (stop_when is not None and stop_when()):
                return
            if not self.enabled_provider():
                await self._pause(self.config.max_idle_seconds, stop)
                continue
            try:
                if self.slot is not None:
                    async with self.slot:  # 与任务循环共用一个执行体槽位：同时只跑一个进程
                        worked = await self.run_once()
                else:
                    worked = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - 轮询循环不能因为一次异常退出
                self.log(f"[chat] 轮询异常：{type(error).__name__}: {error}")
                worked = False
            if worked:
                self._idle = float(self.config.idle_seconds)
            await self._pause(float(self.config.idle_seconds if worked else self._idle), stop)
            if not worked:
                self._idle = min(self.config.max_idle_seconds, max(self.config.idle_seconds, self._idle * 2))

    async def _pause(self, seconds: float, stop: asyncio.Event | None = None) -> None:
        """退避等待；给了 stop 就让等待**可被提前唤醒**——否则关内核时要等满一次退避（最多 30 秒）。"""

        delay = max(0.0, float(seconds))
        if stop is None:
            await asyncio.sleep(delay)
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            pass


__all__ = ["ChatLoop", "ChatLoopConfig", "DEFAULT_CHAT_TEMPLATE", "build_chat_command"]


def _monotonic() -> float:  # pragma: no cover - 供将来观测用
    return time.monotonic()