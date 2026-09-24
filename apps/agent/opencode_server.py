"""对话通道的常驻 opencode server（M-5c S-1）。

**为什么换通道**：`opencode run --format json` 是"一步一条事件"——纯问答要等到最后才有正文（M-5c §0 实测：
一条问答跑 45 秒，平台只收到 `process.started`）。opencode 的**服务端接口**有真正的增量事件，而且不用每轮起进程。

**协议事实（都来自 S-0 在真机上采到的样本，不猜）**：
- 用 **v1 通道**：`POST /session` → `POST /session/{id}/message`（**阻塞到这一轮结束**）→ `GET /event`（SSE）。
  注释里特别标一句：v2 的 `/api/*` 那套**认不出配置里的自定义 provider**（实测 `ModelUnavailableError`），别换回去。
- 增量在 `message.part.delta`（`properties.field` + `properties.delta`）；整段在 `message.part.updated`；
  工具状态在 `part.type == "tool"` 的 `state.status`（pending → running → completed）；`session.idle` 是收尾信号；
  权限请求是 `permission.asked`，**不回就不继续**（实测卡住）。
- 设了 `OPENCODE_SERVER_PASSWORD` 之后，认证是 **HTTP Basic、用户名固定 `opencode`**（实测：Bearer/自定义头都是 401）。
- 就绪探针用 `GET /api/health`（带 Basic 认证返回 `{"healthy":true}`）。

这一层只管三件事：**起停与保活**、**把一轮对话跑完**、**把过程变成平台事件**。
提示词/角色/模型都从平台那一轮带下来，这里不发明概念。
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# 实测：Basic 认证的用户名固定是 opencode，不是任意用户名（user/admin 都是 401）
SERVER_AUTH_USER = "opencode"
HEALTH_PATH = "/api/health"
SERVER_PASSWORD_BYTES = 24
DEFAULT_PORT = 4199
# 一轮对话的默认上限：与 CLI 通道的 turn_timeout（900s）同量级
DEFAULT_TURN_TIMEOUT_SECONDS = 900.0


def split_model_ref(model: str | None) -> dict[str, str] | None:
    """`deepseek/deepseek-v4.1-flash` → `{providerID, modelID}`。

    opencode 的模型串是 `<provider 别名>/<模型 id>`，模型 id 本身可能带 `/`，所以只按**第一个**斜杠切。
    拿不到合法串就返回 None——让 server 用它自己的默认模型，而不是我们编一个（不假装）。
    """

    text = str(model or "").strip()
    if not text or "/" not in text:
        return None
    provider, _, model_id = text.partition("/")
    if not provider or not model_id:
        return None
    return {"providerID": provider, "modelID": model_id}


@dataclass
class ServeTurnOutcome:
    """一轮的结果。`cancelled` 与 `ok` 分开：被取消不是失败，也不是成功。"""

    ok: bool
    text: str = ""
    session_key: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    cancelled: bool = False


class ServerClient:
    """v1 通道的薄封装（urllib，与内核其余部分一致，不引第三方依赖）。"""

    def __init__(
        self,
        base_url: str,
        password: str,
        *,
        log: Callable[[str], None] = print,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.log = log
        self.timeout = timeout
        # 事件流句柄：`read_events` 在**另一个线程**里阻塞读，需要有人能把它叫醒（见 `close_stream`）
        self._stream: Any = None
        self._stream_lock = threading.Lock()

    # ---- 底层 ---------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> tuple[int, Any]:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=None if payload is None else json.dumps(payload).encode("utf-8"),
            method=method,
        )
        request.add_header("Content-Type", "application/json")
        if self.password:
            token = base64.b64encode(f"{SERVER_AUTH_USER}:{self.password}".encode("utf-8")).decode("ascii")
            request.add_header("Authorization", f"Basic {token}")
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", "replace")
            return error.code, raw[:400]
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            return 0, f"{type(error).__name__}"
        try:
            return 200, json.loads(raw) if raw.strip() else None
        except json.JSONDecodeError:
            return 200, raw[:400]

    def health(self) -> bool:
        code, body = self._request("GET", HEALTH_PATH, timeout=5.0)
        return code == 200 and isinstance(body, dict) and bool(body.get("healthy"))

    # ---- 会话 ---------------------------------------------------------------

    def create_session(self, *, model: dict[str, str] | None = None, agent: str | None = None, title: str = "") -> str:
        """建会话。

        **实测**：v1 的 `POST /session` **不接受 `model` 字段**——带上它就整条 400（`{"_tag":"BadRequest"}`，
        而同样的请求去掉 `model` 就是 200；`agent` 是接受的）。模型在**发消息**那一步带（`send_message` 的
        `model` 字段，S-0 实测可用），所以这里显式忽略 `model`，别让它把会话建崩。
        """

        payload: dict[str, Any] = {"title": title or "平台对话"}
        if agent:
            payload["agent"] = agent
        code, body = self._request("POST", "/session", payload)
        if code != 200 or not isinstance(body, dict):
            self.log(f"[chat] 建会话失败：http_{code}:{str(body)[:160]}")
            return ""
        return str(body.get("id") or "")

    def send_message(
        self,
        session_id: str,
        *,
        prompt: str,
        model: dict[str, str] | None = None,
        agent: str | None = None,
        timeout: float = DEFAULT_TURN_TIMEOUT_SECONDS,
    ) -> tuple[int, Any]:
        """`POST /session/{id}/message`：**阻塞**到这一轮结束（实测）。返回 (状态码, 响应体)。"""

        payload: dict[str, Any] = {"parts": [{"type": "text", "text": prompt}]}
        if model:
            payload["model"] = model
        if agent:
            payload["agent"] = agent
        return self._request("POST", f"/session/{session_id}/message", payload, timeout=timeout)

    def reply_permission(self, session_id: str, request_id: str, response: str = "always") -> bool:
        code, _body = self._request(
            "POST",
            f"/session/{session_id}/permissions/{request_id}",
            {"response": response},
        )
        return code == 200

    def abort(self, session_id: str) -> bool:
        code, _body = self._request("POST", f"/session/{session_id}/abort", {})
        return code == 200

    # ---- 事件流 -------------------------------------------------------------

    def read_events(self, stop: threading.Event, on_event: Callable[[dict[str, Any]], None]) -> None:
        """读 `/event` 的 SSE，按 `data:` 行解析成 dict 交给回调。

        **为什么要 `close_stream`**：urllib 的阻塞读只有"来数据"或"对端关闭"才会醒。
        一轮结束后事件可能不再来，读线程就会一直挂在 socket 上（每轮挂一个连接+一个服务端线程，攒起来很难看）。
        所以退出用两条路：置位 `stop` 让它在**读到下一条事件时**自然退出；`close_stream()` 直接关掉底层连接把它叫醒。
        """

        request = urllib.request.Request(f"{self.base_url}/event", headers={"Accept": "text/event-stream"})
        if self.password:
            token = base64.b64encode(f"{SERVER_AUTH_USER}:{self.password}".encode("utf-8")).decode("ascii")
            request.add_header("Authorization", f"Basic {token}")
        try:
            with urllib.request.urlopen(request, timeout=DEFAULT_TURN_TIMEOUT_SECONDS + 60) as response:
                with self._stream_lock:
                    self._stream = response
                for raw_line in response:
                    if stop.is_set():
                        return
                    line = raw_line.decode("utf-8", "replace").rstrip("\n")
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    try:
                        data = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(data, dict):
                        on_event(data)
        except Exception as error:  # noqa: BLE001 - 关流是**从别的线程**做的，收尾时会冒出 AttributeError 之类的噪声
            if not stop.is_set():
                self.log(f"[chat] 事件流断开：{type(error).__name__}")
        finally:
            with self._stream_lock:
                self._stream = None

    def close_stream(self) -> None:
        """从别的线程关掉事件流连接，把阻塞中的读线程叫醒（会被当成"事件流断开"，属预期）。"""

        with self._stream_lock:
            stream = self._stream
        if stream is None:
            return
        try:
            stream.close()
        except Exception:  # noqa: BLE001 - 关闭路径上的异常不影响这一轮的结果
            pass


@dataclass
class OpenCodeServerConfig:
    workspace: Path
    executable: str = "opencode"
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    startup_timeout_seconds: float = 25.0
    health_interval_seconds: float = 0.5


class OpenCodeServer:
    """常驻 `opencode serve` 的生命周期：起、等就绪、发现挂掉就重启、退出清理。

    一个工作目录一个实例（对话通道当前就是单工作目录）。`popen` 可注入——测试用假进程，
    不需要真装 opencode。
    """

    def __init__(
        self,
        config: OpenCodeServerConfig,
        *,
        log: Callable[[str], None] = print,
        popen: Callable[..., Any] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        client: ServerClient | None = None,
    ) -> None:
        self.config = config
        self.log = log
        self._popen = popen
        self._clock = clock
        self._sleep = sleep
        # 每次启动生成一个随机口令；只在内存里流转（进子进程靠环境变量）
        self.password = secrets.token_urlsafe(SERVER_PASSWORD_BYTES)
        # 客户端可注入：测试拿假 HTTP 服务 + 假探活替掉它，不需要真装 opencode
        self.client = client or ServerClient(self.base_url, self.password, log=log)
        self._process: Any = None
        self._started_at: float = 0.0
        self._restarts = 0
        self._last_error = ""

    # ---- 状态 ---------------------------------------------------------------

    @property
    def base_url(self) -> str:
        return f"http://{self.config.host}:{self.config.port}"

    def snapshot(self) -> dict[str, Any]:
        return {
            "chat_server_alive": self.alive,
            "chat_server_port": self.config.port,
            "chat_server_restarts": self._restarts,
            "chat_server_error": self._last_error,
        }

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    # ---- 生命周期 -----------------------------------------------------------

    def _spawn(self) -> None:
        environment = dict(os.environ)
        environment["OPENCODE_SERVER_PASSWORD"] = self.password
        command = [
            str(self.config.executable),
            "serve",
            "--hostname",
            self.config.host,
            "--port",
            str(self.config.port),
        ]
        self._process = self._popen(
            command,
            cwd=str(self.config.workspace),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._started_at = self._clock()
        self.log(f"[chat] 常驻执行体服务已启动（端口 {self.config.port}，口令只在内核内存里）")

    def _wait_ready(self) -> bool:
        client = self.client
        deadline = self._clock() + float(self.config.startup_timeout_seconds)
        while self._clock() < deadline:
            if client.health():
                return True
            if self._process is not None and self._process.poll() is not None:
                self._last_error = f"serve 退出码 {self._process.returncode}"
                return False
            self._sleep(self.config.health_interval_seconds)
        self._last_error = "serve 就绪超时"
        return False

    def ensure_running(self) -> bool:
        """确保有一个健康的 server；不可用返回 False（调用方**降级到 CLI 通道**，不假装）。"""

        if self.alive and self.client.health():
            return True
        if self.alive and not self.client.health():
            # 进程还在但探活失败：先杀再起（僵尸进程/端口占用都走这条路）
            self.log("[chat] 常驻服务探活失败，重启中")
            self.stop()
        try:
            self._spawn()
        except (OSError, ValueError) as error:
            self._last_error = f"启动失败：{type(error).__name__}"
            self.log(f"[chat] 常驻服务启动失败：{type(error).__name__}（这一轮降级到 CLI 通道）")
            return False
        if self._wait_ready():
            self._last_error = ""
            return True
        self.log(f"[chat] 常驻服务未就绪：{self._last_error}（这一轮降级到 CLI 通道）")
        self.stop()
        return False

    def restart(self) -> bool:
        self._restarts += 1
        self.stop()
        return self.ensure_running()

    def stop(self) -> None:
        if self._process is None:
            return
        try:
            if self._process.poll() is None:
                self._process.terminate()
                for _ in range(20):
                    if self._process.poll() is not None:
                        break
                    self._sleep(0.25)
                if self._process.poll() is None:
                    self._process.kill()
        except (OSError, ValueError) as error:  # 退出路径上任何异常都只记录，不影响内核退场
            self.log(f"[chat] 停止常驻服务时出错：{type(error).__name__}")
        finally:
            self._process = None


def _message_text(response: Any) -> str:
    """从 `POST /session/{id}/message` 的响应里取正文：按顺序拼 `parts` 里的 text 段。"""

    parts = (response or {}).get("parts") if isinstance(response, dict) else None
    if not isinstance(parts, list):
        return ""
    chunks = [
        str(part.get("text") or "")
        for part in parts
        if isinstance(part, dict) and part.get("type") == "text" and str(part.get("text") or "").strip()
    ]
    return "\n\n".join(chunks)


def _message_usage(response: Any, *, steps: int) -> dict[str, Any]:
    """把 serve 的 token 账转成平台口径（与 CLI 通道同一批字段名，页面读 `total_tokens`）。"""

    info = (response or {}).get("info") if isinstance(response, dict) else None
    tokens = info.get("tokens") if isinstance(info, dict) else None
    if not isinstance(tokens, dict):
        return {}
    input_tokens = int(tokens.get("input") or 0)
    output_tokens = int(tokens.get("output") or 0)
    usage: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": int(tokens.get("total") or (input_tokens + output_tokens)),
        "turns": max(1, steps),
        "source": "opencode-serve",
    }
    reasoning = int(tokens.get("reasoning") or 0)
    if reasoning:
        usage["reasoning_tokens"] = reasoning
    cache = tokens.get("cache")
    if isinstance(cache, dict):
        read = int(cache.get("read") or 0)
        write = int(cache.get("write") or 0)
        if read:
            usage["cache_read_tokens"] = read
        if write:
            usage["cache_write_tokens"] = write
    return usage


def _message_error(response: Any) -> str:
    info = (response or {}).get("info") if isinstance(response, dict) else None
    error = info.get("error") if isinstance(info, dict) else None
    if not error:
        return ""
    if isinstance(error, dict):
        return str(error.get("message") or error.get("type") or "provider_error")[:300]
    return str(error)[:300]


def _permission_summary(properties: dict[str, Any], tool_name: str = "") -> str:
    """把 opencode 的 metadata 拼成一句人看得懂的话（拼不出来就退回 permission + patterns）。"""

    metadata = properties.get("metadata") if isinstance(properties.get("metadata"), dict) else {}
    permission = str(properties.get("permission") or "")
    patterns = [str(item) for item in (properties.get("patterns") or []) if str(item)]
    command = str(metadata.get("command") or "")
    filepath = str(metadata.get("filepath") or metadata.get("path") or "")
    prefix = f"{tool_name} " if tool_name else ""
    if command:
        return f"{prefix}执行命令：{command[:180]}"
    if filepath:
        return f"{prefix}动工作区外的文件：{filepath[:180]}"
    if patterns:
        return f"{prefix}{permission or '权限请求'}：" + "、".join(patterns[:3])
    return f"{prefix}{permission or '权限请求'}"


def _decide_permission(
    *,
    payload: dict[str, Any],
    approvals: Any | None,
    auto_approve_permissions: bool,
    approval_timeout_seconds: float,
    reporter: Any,
    cancel_event: threading.Event,
    log: Callable[[str], None],
) -> str:
    """决定一条权限请求怎么回（`once`/`always`/`reject`），并把过程记进平台事件。

    有审批通道（S-3 真卡片）：上报 → 等人批（有界等待）→ 按决定回复；**等不到就 reject + 标 EXPIRED**。
    没通道（模块被独立使用/测试）：沿用 `auto_approve_permissions`（`--auto` 等价语义）。
    上报/轮询本身失败时**不把这一轮挂死**：退回自动策略并如实记日志。
    """

    request_id = str(payload["request_id"])
    if reporter is not None:
        reporter.progress(
            "approval.requested",
            {"request_id": request_id, "permission": payload.get("permission"), "summary": payload.get("summary")},
        )
    if approvals is None:
        decision, source = ("always", "auto") if auto_approve_permissions else ("reject", "auto")
    else:
        try:
            approvals.report(payload)
        except Exception as error:  # noqa: BLE001 - 上报失败不能让这一轮卡住
            log(f"[chat] 权限请求上报失败（{type(error).__name__}）：退回自动策略")
            decision, source = ("always", "auto") if auto_approve_permissions else ("reject", "auto")
            return _note_decision(reporter, request_id, decision, source)
        try:
            decided = approvals.wait(request_id, timeout=approval_timeout_seconds, cancel_event=cancel_event)
        except Exception as error:  # noqa: BLE001 - 轮询失败当"没人批"处理（宁可拒绝也不擅自放行）
            log(f"[chat] 权限决策查询失败（{type(error).__name__}）：按无人批准处理")
            decided = None
        if decided:
            decision, source = str(decided), "member"
        else:
            try:
                approvals.expire(request_id)
            except Exception as error:  # noqa: BLE001 - 标记失败只影响台账，不影响这一轮
                log(f"[chat] 权限请求过期标记失败：{type(error).__name__}")
            decision, source = "reject", "timeout"
            log("[chat] 权限请求等到超时：按「没人批 = 不执行」拒绝")
    return _note_decision(reporter, request_id, decision, source)


def _note_decision(reporter: Any, request_id: str, decision: str, source: str) -> str:
    if reporter is not None:
        reporter.progress("approval.decided", {"request_id": request_id, "decision": decision, "by": source})
    return decision


def run_serve_turn(
    client: ServerClient,
    *,
    prompt: str,
    reporter: Any,
    model: str | None = None,
    agent: str | None = None,
    session_key: str | None = None,
    emit_delta: Callable[[dict[str, Any]], None] | None = None,
    delta_interval_seconds: float = 0.8,
    max_delta_events: int = 240,
    approvals: Any | None = None,
    approval_timeout_seconds: float = 300.0,
    cancel: threading.Event | None = None,
    cancel_check: Callable[[], bool] | None = None,
    cancel_poll_seconds: float = 3.0,
    timeout: float = DEFAULT_TURN_TIMEOUT_SECONDS,
    auto_approve_permissions: bool = True,
    clock: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = print,
) -> ServeTurnOutcome:
    """跑完一轮：订阅事件 → 发消息（阻塞）→ 汇总结果。

    **正文怎么出去（S-2）**：给了 `emit_delta` 就把逐字增量按 `delta` 事件发出去（页面边生成边显示）；
    没给就退回"每段一条 `agent.message`"的老口径。delta **不走 reporter**——reporter 有 1.5s 节流与
    40 条上限，用来压"过程事件"是对的，但**压掉一条增量就是丢正文**，所以增量有自己的节奏：
    攒够 `delta_interval_seconds` 或攒够一小段文本才发一条，且**最后一定把剩下的补发**（不丢字）。

    **权限（S-3）**：给了 `approvals` 通道就走真卡片——上报请求 → 等人批（有界等待）→ 按决定回复 opencode；
    **等不到人批就 `reject` 并标 EXPIRED**（没人批 = 不执行，不假装有人同意）。没给通道（独立使用/测试）时
    沿用 `auto_approve_permissions` 的 `--auto` 等价语义。
    实测的回复取值：`once` / `always` 会执行，`reject` **确实不执行**。

    过程事件（`tool.completed` / `file.changed`）照旧走 reporter。
    """

    cancel_event = cancel or threading.Event()
    stop = threading.Event()
    state: dict[str, Any] = {
        "session_id": str(session_key or ""),
        "part_id": "",
        "part_text": "",
        "pending": "",
        "delta_events": 0,
        "last_delta_at": float("-inf"),
        # **实测教训**：思考（reasoning）与正文（text）都是 `message.part.delta`、`field` 都是 `text`，
        # 光看 `field` 分不开（第一版就是把模型的英文思考混进了气泡）。类型只能从
        # `message.part.updated` 的 `part.id` + `part.type` 学，所以按 partID 记一张类型表。
        "part_types": {},
        "unknown": {},
        "unknown_deltas": 0,
        # 工具名按 callID 记一份：权限请求里只给 callID，卡片上要显示"是哪个工具在要权限"
        "tool_calls": {},
        "permission_replies": [],
        "steps": 0,
        "permissions": 0,
        "files": 0,
        "response": None,
    }

    def accept_delta(part_id: str, chunk: str) -> None:
        """把一段增量并入正文缓冲（只认已知的 text 段；思考段丢弃；类型未知先攒着不猜）。"""

        kind = state["part_types"].get(part_id)
        if kind == "reasoning":
            return
        if kind is None:
            state["unknown"][part_id] = state["unknown"].get(part_id, "") + chunk
            state["unknown_deltas"] += 1
            return
        state["part_text"] += chunk
        state["pending"] += chunk
        maybe_emit_delta()

    def learn_part(part: dict[str, Any]) -> None:
        """`message.part.updated` 是**部分的权威形状**：记住类型；类型一到就把攒着的增量定性。"""

        part_id = str(part.get("id") or "")
        part_type = str(part.get("type") or "")
        if not part_id or not part_type:
            return
        if part_type == "tool":
            call_id = str(part.get("callID") or "")
            if call_id:
                state["tool_calls"][call_id] = str(part.get("tool") or "")
        state["part_types"][part_id] = part_type
        buffered = state["unknown"].pop(part_id, "")
        if not buffered:
            return
        if part_type == "text":
            state["part_text"] += buffered
            state["pending"] += buffered
            maybe_emit_delta()
        # reasoning（或其它类型）：攒着的那段不该出现在正文里——直接丢掉，不猜也不展示

    def maybe_emit_delta(force: bool = False) -> None:
        """把攒下的正文增量发一条 `delta`。`force` 用于收尾：**剩下的必须发出去**，不设上限。"""

        pending = state["pending"]
        if not pending or emit_delta is None:
            return
        now = clock()
        if not force:
            if state["delta_events"] >= max_delta_events:
                return
            if now - state["last_delta_at"] < delta_interval_seconds and len(pending) < 32:
                return
        state["pending"] = ""
        state["last_delta_at"] = now
        state["delta_events"] += 1
        emit_delta({"text": pending, "part_id": state["part_id"]})

    def flush_part() -> None:
        if emit_delta is not None:
            # 常驻通道：正文已经按 delta 出去了，收尾只补最后没发完的那一点
            maybe_emit_delta(force=True)
            state["part_text"] = ""
            state["part_id"] = ""
            return
        text = state["part_text"].strip()
        if text and reporter is not None:
            reporter.progress("agent.message", {"text": text[:500]})
        state["part_text"] = ""
        state["part_id"] = ""

    def handle(event: dict[str, Any]) -> None:
        kind = str(event.get("type") or "")
        properties = event.get("properties") if isinstance(event.get("properties"), dict) else {}
        if kind == "message.part.delta":
            if str(properties.get("field") or "") != "text":
                return
            part_id = str(properties.get("partID") or "")
            if part_id and part_id != state["part_id"]:
                flush_part()
                state["part_id"] = part_id
            accept_delta(part_id, str(properties.get("delta") or ""))
            return
        if kind == "message.part.updated":
            part = properties.get("part") if isinstance(properties.get("part"), dict) else {}
            learn_part(part)
            part_type = str(part.get("type") or "")
            if part_type == "tool":
                tool_state = part.get("state") if isinstance(part.get("state"), dict) else {}
                if str(tool_state.get("status") or "") == "completed":
                    tool = str(part.get("tool") or "tool")
                    if reporter is not None:
                        reporter.progress("tool.completed", {"tool": tool[:80]})
            elif part_type == "step-finish":
                state["steps"] += 1
            return
        if kind == "file.edited":
            state["files"] += 1
            if reporter is not None:
                reporter.progress("file.changed", {"path": str(properties.get("file") or "")[:200]})
            return
        if kind == "permission.asked":
            session_id = str(properties.get("sessionID") or state["session_id"])
            request_id = str(properties.get("id") or "")
            state["permissions"] += 1
            if not request_id:
                return
            tool = properties.get("tool") if isinstance(properties.get("tool"), dict) else {}
            call_id = str(tool.get("callID") or "")
            payload = {
                "request_id": request_id,
                "permission": str(properties.get("permission") or ""),
                "patterns": [str(item) for item in (properties.get("patterns") or []) if str(item)],
                "summary": _permission_summary(properties, str(state["tool_calls"].get(call_id, ""))),
                "tool": str(state["tool_calls"].get(call_id, "")),
                "call_id": call_id,
            }
            reply = _decide_permission(
                payload=payload,
                approvals=approvals,
                auto_approve_permissions=auto_approve_permissions,
                approval_timeout_seconds=approval_timeout_seconds,
                reporter=reporter,
                cancel_event=cancel_event,
                log=log,
            )
            state["permission_replies"].append((request_id, reply))
            if session_id and reply:
                allowed = client.reply_permission(session_id, request_id, reply)
                log(f"[chat] 权限请求 {request_id[:12]} → {reply}（已回复：{allowed}）")
            return
        if kind == "session.idle":
            # 这一轮的自然终点（S-0 采到的收尾信号）：读线程该收手了。
            # 只认**本会话**的 idle——别的会话的 idle 不能把我们的流关掉。
            if str(properties.get("sessionID") or "") == str(state["session_id"]):
                stop.set()
            return

    reader = threading.Thread(target=client.read_events, args=(stop, handle), daemon=True)
    reader.start()

    watcher_stop = threading.Event()

    def watch_cancel() -> None:
        """取消语义：serve 模式下不是杀进程，而是 `POST /session/{id}/abort`（M-5c 计划 §3）。"""

        while not watcher_stop.wait(cancel_poll_seconds):
            if not state["session_id"] or cancel_check is None:
                continue
            try:
                if cancel_check() and not cancel_event.is_set():
                    cancel_event.set()
                    log(f"[chat] 轮次被取消，正在中止会话 {state['session_id'][:12]}")
                    client.abort(state["session_id"])
            except Exception as error:  # noqa: BLE001 - 取消轮询绝不能把这一轮搞崩
                log(f"[chat] 取消轮询失败：{type(error).__name__}")

    watcher = threading.Thread(target=watch_cancel, daemon=True)
    if cancel_check is not None:
        watcher.start()

    model_ref = split_model_ref(model)
    created_now = ""
    if not state["session_id"]:
        created_now = client.create_session(model=model_ref, agent=agent)
        state["session_id"] = created_now
    if reporter is not None:
        reporter.started(["opencode", "serve", "message"], "chat", protocol="opencode")

    code, body = (0, "no_session")
    if state["session_id"]:
        code, body = client.send_message(
            state["session_id"],
            prompt=prompt,
            model=model_ref,
            agent=agent,
            timeout=timeout,
        )
        if code == 404:
            # 平台带来的会话句柄在 server 上不存在（例如上一轮走的是 CLI 通道）：新建一个再来一次
            log("[chat] 会话不存在，改用新会话重发")
            fallback_id = client.create_session(model=model_ref, agent=agent)
            if fallback_id:
                state["session_id"] = fallback_id
                code, body = client.send_message(
                    fallback_id,
                    prompt=prompt,
                    model=model_ref,
                    agent=agent,
                    timeout=timeout,
                )

    # 收尾：事件流以 `session.idle` 为自然终点（读线程读到它自己退出，见 handle）。
    # **不能在这里提前置位 stop**——那会把"已经到 socket 但还没读"的事件（工具、文件改动）丢掉。
    # 等它一会儿；没等到（例如这一轮压根没有 idle）就关连接把它叫醒，免得每轮都挂一个 SSE 连接。
    reader.join(timeout=1.0)
    if reader.is_alive():
        # 先置位 stop 再关流：否则读线程会把"关流"当成真断线记一条日志（关流是正常收尾）
        stop.set()
        client.close_stream()
        reader.join(timeout=0.8)
    flush_part()
    stop.set()
    watcher_stop.set()
    state["response"] = body

    error = _message_error(body)
    text = _message_text(body)
    # 取消不算成功（与 CLI 通道同一口径：取消走 success=false + `cancelled_by_member`）
    ok = code == 200 and not error and not cancel_event.is_set()
    if code != 200:
        error = f"http_{code}:{str(body)[:200]}"
    if reporter is not None:
        reporter.progress("process.exited", {
            "exit_code": 0 if ok else 1,
            "cancelled": cancel_event.is_set(),
            "delta_events": int(state["delta_events"]),
            # 类型未知而没敢当正文的增量条数（正常应为 0；不为 0 说明事件形状变了，查这里）
            "unknown_deltas": int(state["unknown_deltas"]),
            "permissions": int(state["permissions"]),
            **reporter.stats(),
        })
    return ServeTurnOutcome(
        ok=ok,
        text=text,
        session_key=state["session_id"],
        usage=_message_usage(body, steps=int(state["steps"])),
        error=error,
        cancelled=cancel_event.is_set(),
    )


__all__ = [
    "DEFAULT_PORT",
    "OpenCodeServer",
    "OpenCodeServerConfig",
    "ServeTurnOutcome",
    "ServerClient",
    "run_serve_turn",
    "split_model_ref",
]