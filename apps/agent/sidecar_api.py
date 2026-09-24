"""桌面端内核（sidecar）的本地 HTTP 契约实现。

契约见 `docs/SIDECAR_CONTRACT.md`（v1，冻结）。设计要点：

- **只监听 127.0.0.1**，端口由内核自选，启动信息（端口 + 令牌 + pid）写入
  ``%LOCALAPPDATA%\\MathAgentPlatform\\sidecar.json``，壳读该文件后连接；
- 除 ``/health`` 外所有端点要求 ``Authorization: Bearer <启动令牌>``；
- 凭据与平台连接都留在内核，壳拿不到设备/项目 Token（DE6）；
- 不提供"执行任意命令"的端点：执行只由平台派发的任务触发。

本模块只做"壳要的那层皮"，真正的连接/任务/执行逻辑复用现有 `machine_service`、
`worker-run` 与 `credential_store`，不重复实现。
"""

from __future__ import annotations

import json
import os
import platform
import secrets
import socket
import sys
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

CONTRACT_VERSION = "v1"
SIDECAR_VERSION = "0.1.0"

# 状态文件放在可写状态目录（DE8），不污染用户家目录
STATE_DIR_ENV = "MAP_STATE_DIR"


def default_state_dir() -> Path:
    """可写状态目录：%LOCALAPPDATA%\\MathAgentPlatform（非 Windows 退回 ~/.math-agent-platform）。"""

    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "MathAgentPlatform"
    return Path.home() / ".math-agent-platform"


def sidecar_info_path(state_dir: Path | None = None) -> Path:
    return (state_dir or default_state_dir()) / "sidecar.json"


def platform_info_path(state_dir: Path | None = None) -> Path:
    """配对结果落盘（平台地址 + 设备标识）。

    内核重启后要能自己找回"连哪个平台、我是谁"——否则每次开机都得重新配对。
    这里**只放地址与标识**：设备 Token 永远只在 Windows 凭据管理器里（DE6）。
    """

    return (state_dir or default_state_dir()) / "platform.json"


def write_platform_info(state_dir: Path | None, url: str, device_id: str, agent_id: str | None) -> None:
    directory = state_dir or default_state_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = platform_info_path(directory)
        path.write_text(
            json.dumps({"url": url, "device_id": device_id, "agent_id": agent_id, "paired_at": _now()}, ensure_ascii=False),
            encoding="utf-8",
        )
        if os.name != "nt":
            os.chmod(path, 0o600)
    except OSError:
        # 落盘失败不影响本次配对：下次启动顶多是找不到默认平台地址。
        pass


def read_platform_info(state_dir: Path | None = None) -> dict[str, Any]:
    path = platform_info_path(state_dir)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def host_slug() -> str:
    """与 connect-agent.ps1 一致的标识派生：主机名小写、非字母数字转 '-'。"""

    raw = os.environ.get("COMPUTERNAME") or platform.node() or "host"
    return "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in raw.lower()).strip("-") or "host"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SidecarError(Exception):
    """稳定错误族：带 HTTP 状态与错误码。"""

    def __init__(self, status: int, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}:{detail}")
        self.status = status
        self.code = code
        self.detail = detail


def decode_pairing_blob(blob: str) -> dict[str, Any]:
    """解析网页/脚本给出的配对串：base64url(JSON)，字段 pairing_id/pairing_code/challenge[/expires_at]。

    与 `scripts/connect-agent.ps1` 的 `ConvertFrom-Base64Url` 保持同一格式；这里额外做过期校验。
    """

    import base64

    padded = blob.strip().replace("-", "+").replace("_", "/")
    padded += "=" * ((4 - len(padded) % 4) % 4)
    try:
        payload = json.loads(base64.b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception as error:  # noqa: BLE001 - 统一映射为契约错误码
        raise SidecarError(400, "sidecar_pairing_blob_invalid", "base64_or_json") from error
    if not isinstance(payload, dict):
        raise SidecarError(400, "sidecar_pairing_blob_invalid", "not_object")
    for field in ("pairing_id", "pairing_code", "challenge"):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise SidecarError(400, "sidecar_pairing_blob_invalid", f"missing_{field}")
    expires_at = payload.get("expires_at")
    if isinstance(expires_at, str) and expires_at.strip():
        try:
            parsed = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise SidecarError(400, "sidecar_pairing_blob_invalid", "expires_at_unparseable") from error
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        if parsed < datetime.now(UTC):
            raise SidecarError(400, "sidecar_pairing_expired", expires_at)
    return payload


class SidecarState:
    """壳可见的状态快照。运行期由机器服务/任务循环更新，这里只负责聚合与线程安全读写。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.connection_state = "disconnected"
        self.connection_since: str | None = None
        self.reconnect_attempt = 0
        self.last_error: str | None = None
        self.device_id: str | None = None
        self.agent_id: str | None = None
        self.project_id: str | None = None
        self.platform_url: str | None = None
        self.paired_at: str | None = None
        self.last_pair_error: str | None = None
        self.paused = False
        self.emergency_stop = False
        self.current_task: dict[str, Any] | None = None
        self.completed_since_start = 0
        self.local_queue_length = 0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "connection": {
                    "state": self.connection_state,
                    "since": self.connection_since,
                    "reconnect_attempt": self.reconnect_attempt,
                    "last_error": self.last_error,
                },
                "identity": {
                    "device_id": self.device_id,
                    "agent_id": self.agent_id,
                    "project_id": self.project_id,
                    "platform_url": self.platform_url,
                    "paired_at": self.paired_at,
                    # 契约 v1 追加的可选字段：最近一次配对失败原因（否则该信息只活在壳的内存里）
                    "last_pair_error": self.last_pair_error,
                },
                "tasks": {
                    "running": [],
                    "claimed_current": self.current_task,
                    "paused": self.paused,
                    "completed_since_start": self.completed_since_start,
                },
                "queue": {"local_pending_events": 0, "local_queue_length": self.local_queue_length},
                "emergency_stop": self.emergency_stop,
            }

    def update(self, **fields: Any) -> None:
        with self._lock:
            for key, value in fields.items():
                if not hasattr(self, key):
                    raise ValueError(f"sidecar_unknown_state_field:{key}")
                setattr(self, key, value)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class SidecarService:
    """契约端点的业务实现。``agents_probe`` / ``pair`` 可注入，便于测试与替换。"""

    def __init__(
        self,
        *,
        state_dir: Path | None = None,
        state: SidecarState | None = None,
        agents_probe: Callable[[], list[dict[str, Any]]] | None = None,
        agents_rescan: Callable[[], list[dict[str, Any]]] | None = None,
        pair_handler: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        grant_handler: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        log_buffer: list[str] | None = None,
    ) -> None:
        self.state_dir = state_dir or default_state_dir()
        self.state = state or SidecarState()
        self._agents_probe = agents_probe or default_agents_probe
        # 重扫与普通读取分开：普通读取可以走缓存，重扫必须真探测（DP-2-03）。
        self._agents_rescan = agents_rescan or self._agents_probe
        self._pair_handler = pair_handler or default_pair_handler
        # 项目授权只在常驻体（daemon-run）里有处理者：sidecar-run 没接平台，必须如实报 503。
        self._grant_handler = grant_handler
        # 常驻体（daemon-run）挂上来的钩子：契约端只改自己的状态，真正生效交给连接监督。
        self.on_pause: Callable[[bool], None] | None = None
        self.on_emergency_stop: Callable[[bool], None] | None = None
        self.logs = log_buffer if log_buffer is not None else []
        self.token = secrets.token_urlsafe(32)
        self.port: int | None = None
        self.started_at = _now()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        """契约服务是否还在监听（`/shutdown` 之后为 False）。"""

        return self._server is not None

    # ---- 生命周期 -------------------------------------------------------

    def start(self, port: int | None = None) -> dict[str, Any]:
        self.port = port or free_port()
        handler = _make_handler(self)
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="sidecar-http", daemon=True)
        self._thread.start()
        self._write_info_file()
        return self.info()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        info = sidecar_info_path(self.state_dir)
        if info.exists():
            try:
                info.unlink()
            except OSError:
                pass

    def info(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "token": self.token,
            "pid": os.getpid(),
            "started_at": self.started_at,
            "contract": CONTRACT_VERSION,
        }

    def _write_info_file(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = sidecar_info_path(self.state_dir)
        path.write_text(json.dumps(self.info(), ensure_ascii=False), encoding="utf-8")
        if os.name != "nt":
            os.chmod(path, 0o600)

    # ---- 端点实现 -------------------------------------------------------

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "contract": CONTRACT_VERSION,
            "version": SIDECAR_VERSION,
            "started_at": self.started_at,
            "pid": os.getpid(),
        }

    def status(self) -> dict[str, Any]:
        payload = self.state.snapshot()
        payload["local_agents"] = self._agents_probe()
        payload["contract"] = CONTRACT_VERSION
        return payload

    def pair(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise SidecarError(400, "sidecar_pairing_payload_invalid")
        url = str(payload.get("platform_url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise SidecarError(400, "sidecar_platform_url_invalid", url)
        blob = str(payload.get("pairing_blob") or "").strip()
        if not blob:
            raise SidecarError(400, "sidecar_pairing_blob_invalid", "missing")

        try:
            result = self._pair_handler({**payload, "platform_url": url, "pairing_blob": blob})
        except SidecarError as error:
            self.state.update(last_pair_error=f"{error.code}: {error.detail}".strip())
            self.logs.append(f"[{_now()}] pair failed: {error.code}")
            raise
        except Exception as error:  # noqa: BLE001 - 上游/未知错误也要留痕
            self.state.update(last_pair_error=f"{type(error).__name__}: {error}")
            self.logs.append(f"[{_now()}] pair failed: {type(error).__name__}")
            raise
        self.state.update(
            device_id=result.get("device_id"),
            agent_id=result.get("agent_id"),
            platform_url=url,
            paired_at=_now(),
            last_pair_error=None,
        )
        # 重启后能自己找回平台地址与设备标识（Token 仍在凭据管理器，不落这里）
        write_platform_info(
            self.state_dir,
            url,
            str(result.get("device_id") or ""),
            result.get("agent_id"),
        )
        self.logs.append(f"[{_now()}] paired device={result.get('device_id')} agent={result.get('agent_id')}")
        return result

    def rescan_agents(self) -> dict[str, Any]:
        return {"local_agents": self._agents_rescan()}

    def grant(self, payload: dict[str, Any]) -> dict[str, Any]:
        """应用项目授权串（契约 v1 追加端点，DP-2-08）。

        与 `/pair` 分开：配对解决"这台机器是谁"，授权解决"能替哪个项目干活"。
        授权串等同短期凭证，只经本机回环 + 启动令牌传输，不写日志。
        """

        if not isinstance(payload, dict):
            raise SidecarError(400, "sidecar_grant_payload_invalid")
        url = str(payload.get("platform_url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise SidecarError(400, "sidecar_platform_url_invalid", url)
        blob = str(payload.get("grant_blob") or "").strip()
        if not blob:
            raise SidecarError(400, "sidecar_grant_blob_invalid", "missing")
        if self._grant_handler is None:
            raise SidecarError(503, "sidecar_grant_unavailable", "该内核未连接平台，无法应用项目授权")
        try:
            result = self._grant_handler({**payload, "platform_url": url, "grant_blob": blob})
        except SidecarError:
            raise
        except ValueError as error:
            self.logs.append(f"[{_now()}] grant rejected: {error}")
            raise SidecarError(400, "sidecar_grant_blob_invalid", str(error)) from error
        except Exception as error:  # noqa: BLE001 - 未知异常也要给壳稳定形状
            self.logs.append(f"[{_now()}] grant failed: {type(error).__name__}")
            raise SidecarError(500, "sidecar_grant_failed", f"{type(error).__name__}:{error}") from error
        self.logs.append(f"[{_now()}] granted project={result.get('project_id')} device={result.get('device_id')}")
        return result

    def set_paused(self, paused: bool) -> dict[str, Any]:
        self.state.update(paused=bool(paused))
        if self.on_pause is not None:
            self.on_pause(bool(paused))
        self.logs.append(f"[{_now()}] tasks {'paused' if paused else 'resumed'}")
        return {"paused": bool(paused)}

    def set_emergency_stop(self, active: bool) -> dict[str, Any]:
        self.state.update(emergency_stop=bool(active))
        if self.on_emergency_stop is not None:
            self.on_emergency_stop(bool(active))
        self.logs.append(f"[{_now()}] emergency_stop={bool(active)}")
        return {"emergency_stop": bool(active)}

    def tail_logs(self, tail: int) -> dict[str, Any]:
        size = max(1, min(int(tail), 2000))
        return {"lines": self.logs[-size:], "truncated": len(self.logs) > size}


# ---- 默认依赖：复用既有能力，不重复实现 ---------------------------------


def default_agents_probe() -> list[dict[str, Any]]:
    """本机可用 Agent 探测：复用 agent_inventory（同一份探测与缓存，DP-2-03）。"""

    try:  # 延迟导入：sidecar 在无桌面端也能启动，探测失败不影响存活
        from agent_inventory import AgentInventory  # type: ignore
    except ImportError:
        from .agent_inventory import AgentInventory  # type: ignore

    return AgentInventory().entries()


def default_pair_handler(payload: dict[str, Any]) -> dict[str, Any]:
    """默认配对：复用 agentd 的 keygen/device-register 与凭据入库。

    壳只需给平台地址 + 配对串；其余标识由主机名派生，与 `connect-agent.ps1` 保持一致。
    """

    import argparse

    import agentd  # type: ignore  # 同目录模块

    platform_url = payload["platform_url"]
    blob = payload["pairing_blob"]
    # 配对串与"项目授权串"是不同的东西：前者含 pairing_id/pairing_code/challenge，
    # 后者含 project_id/project_token。这里必须用配对串解码器。
    pairing = decode_pairing_blob(blob)
    slug = host_slug()
    agent_id = str(payload.get("agent_id") or f"agent-{slug}")
    device_id = str(payload.get("device_id") or f"device-{slug}")
    agent_name = str(payload.get("agent_name") or platform.node() or "workstation")

    state_dir = default_state_dir()
    key_dir = state_dir / "keys"
    key_dir.mkdir(parents=True, exist_ok=True)
    key_path = key_dir / f"{device_id}.key"

    # keygen（复用 CLI 子命令的实现，保持单一来源）
    if not key_path.exists():
        agentd.keygen(argparse.Namespace(directory=str(key_dir), name=device_id, force=False))

    # Agent 登记（平台侧 upsert，可重复）
    agentd.request(
        platform_url,
        "POST",
        "/api/agents/register",
        {"agent_id": agent_id, "display_name": agent_name, "owner_member_id": "member-001"},
    )

    # 设备注册（私钥签名）——沿用 device-register 的语义（--opt=value 形式避免 challenge 以 '-' 开头被当选项）
    def register_once(current_device_id: str, current_key_path: Path):
        register_args = [
            "--pairing-code={0}".format(pairing["pairing_code"]),
            "--pairing-id={0}".format(pairing["pairing_id"]),
            "--challenge={0}".format(pairing["challenge"]),
            "--agent-id", agent_id,
            "--device-id", current_device_id,
            "--device-name", agent_name,
            "--private-key", str(current_key_path),
            "--platform", "windows" if os.name == "nt" else sys.platform,
            "--agent-version", SIDECAR_VERSION,
            "--capabilities", "task.claim",
        ]
        if not current_key_path.exists():
            agentd.keygen(argparse.Namespace(directory=str(current_key_path.parent), name=current_key_path.stem, force=False))
        return agentd.request(platform_url, "POST", "/api/devices/register",
                             _device_register_payload(register_args, current_key_path))

    try:
        register = register_once(device_id, key_path)
    except ValueError as error:
        # 一个 device_id 只能注册一次（还可能撞公钥唯一）。重装/重置后同主机名必然遇到，
        # 这里自动换标识重试一次，用户不必懂这个约束；换后的标识会回报给用户与平台。
        if "device_id_already_registered" not in str(error):
            raise
        suffix = secrets.token_hex(2)
        device_id = f"{device_id}-{suffix}"
        key_path = key_dir / f"{device_id}.key"
        register = register_once(device_id, key_path)

    # 设备 Token 入凭据管理器（壳拿不到明文，DE6）
    token = register.get("device_token")
    credential_stored = False
    if isinstance(token, str) and token:
        try:
            agentd.WindowsCredentialManager().put(agentd.device_token_target(device_id), token)
            credential_stored = True
        except Exception:  # noqa: BLE001 - 凭据后端不可用时如实回报，不谎报
            credential_stored = False

    # worker.json 里记 Codex 路径（顺带探测）
    codex_path = None
    if payload.get("codex", True):
        try:
            codex_path = agentd.detect_codex_cli()
        except Exception:  # noqa: BLE001
            codex_path = None

    return {
        "paired": True,
        "device_id": device_id,
        "agent_id": agent_id,
        "public_key_fingerprint": (register.get("device") or {}).get("public_key_fingerprint"),
        "credential_stored": credential_stored,
        "codex_path": codex_path,
        "message": None if credential_stored else "凭据未入库：设备 Token 无法持久化，请检查凭据管理器",
    }


def _device_register_payload(register_args: list[str], key_path: Path) -> dict[str, Any]:
    """把 CLI 参数形态转成平台请求体（与 device_register 子命令同构）。"""

    from cryptography.hazmat.primitives import serialization
    from packages.device_identity import parse_public_key, sign_registration

    values: dict[str, Any] = {"capabilities": ["task.claim"]}
    index = 0
    while index < len(register_args):
        token = register_args[index]
        if token.startswith("--") and "=" in token:
            key, _, value = token.partition("=")
            values[key.lstrip("-").replace("-", "_")] = value
            index += 1
            continue
        if token.startswith("--"):
            key = token.lstrip("-").replace("-", "_")
            value = register_args[index + 1] if index + 1 < len(register_args) else ""
            values[key] = value
            index += 2
            continue
        index += 1

    private_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    _, fingerprint = parse_public_key(public_pem)
    signature = sign_registration(
        private_key,
        values["pairing_id"],
        values["challenge"],
        values["agent_id"],
        values["device_id"],
        fingerprint,
    )
    # --capabilities 在 CLI 上是 nargs="*"：可能解析成字符串（单个值）或列表。
    # 平台契约要求 list[str]，这里统一成列表，避免 422 list_type。
    raw_capabilities = values.get("capabilities") or ["task.claim"]
    if isinstance(raw_capabilities, str):
        capabilities = raw_capabilities.split()
    else:
        capabilities = [str(item) for item in raw_capabilities]
    if not capabilities:
        capabilities = ["task.claim"]

    return {
        "pairing_code": values["pairing_code"],
        "pairing_id": values["pairing_id"],
        "challenge": values["challenge"],
        "challenge_signature": signature,
        "agent_id": values["agent_id"],
        "device_id": values["device_id"],
        "device_name": values["device_name"],
        "public_key": public_pem,
        "platform": values.get("platform", "windows"),
        "agent_version": values.get("agent_version", SIDECAR_VERSION),
        "capabilities": capabilities,
    }


# ---- HTTP 层 ------------------------------------------------------------


def _make_handler(service: SidecarService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"MathAgentSidecar/{SIDECAR_VERSION}"

        def log_message(self, *args: Any) -> None:  # 静音：日志走 service.logs
            return

        # 工具
        def _send(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            scheme, _, token = header.partition(" ")
            return scheme.lower() == "bearer" and secrets.compare_digest(token, service.token)

        def _raw_body(self) -> bytes:
            """读一次并缓存。

            必须**无条件**把请求体读掉：调用方（尤其 Windows 上的 urllib）在服务端
            提前关连接时会对未发送完的请求体收到 RST，表现为 ConnectionAborted/WinError 10053。
            """

            cached = getattr(self, "_raw_body_cache", None)
            if cached is not None:
                return cached
            length = int(self.headers.get("Content-Length") or 0)
            try:
                cached = self.rfile.read(length) if length > 0 else b""
            except OSError:
                cached = b""
            self._raw_body_cache = cached
            return cached

        def _body(self) -> dict[str, Any]:
            raw = self._raw_body()
            if not raw:
                return {}
            try:
                payload = json.loads(raw.decode("utf-8"))
            except ValueError as error:
                raise SidecarError(400, "sidecar_body_invalid", str(error)) from error
            return payload if isinstance(payload, dict) else {}

        # 路由
        def do_GET(self) -> None:  # noqa: N802 - http.server 接口
            path = self.path.split("?", 1)[0]
            try:
                if path == "/health":
                    return self._send(200, service.health())
                if not self._authorized():
                    return self._send(401, {"error": "sidecar_unauthorized"})
                if path == "/status":
                    return self._send(200, service.status())
                if path == "/logs":
                    query = self.path.split("?", 1)[1] if "?" in self.path else ""
                    tail = 200
                    for pair in query.split("&"):
                        key, _, value = pair.partition("=")
                        if key == "tail" and value.isdigit():
                            tail = int(value)
                    return self._send(200, service.tail_logs(tail))
                return self._send(404, {"error": "sidecar_not_found", "path": path})
            except SidecarError as error:
                return self._send(error.status, {"error": error.code, "detail": error.detail})

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            try:
                self._raw_body()  # 先排空请求体，避免提前关连接导致客户端 RST
                if not self._authorized():
                    return self._send(401, {"error": "sidecar_unauthorized"})
                if path == "/pair":
                    return self._send(200, service.pair(self._body()))
                if path == "/agents/rescan":
                    return self._send(200, service.rescan_agents())
                if path == "/grant":
                    return self._send(200, service.grant(self._body()))
                if path == "/tasks/pause":
                    return self._send(200, service.set_paused(True))
                if path == "/tasks/resume":
                    return self._send(200, service.set_paused(False))
                if path == "/emergency-stop":
                    return self._send(200, service.set_emergency_stop(True))
                if path == "/clear-emergency-stop":
                    return self._send(200, service.set_emergency_stop(False))
                if path == "/shutdown":
                    self._send(200, {"shutting_down": True})
                    threading.Thread(target=_delayed_stop, args=(service,), daemon=True).start()
                    return
                return self._send(404, {"error": "sidecar_not_found", "path": path})
            except SidecarError as error:
                return self._send(error.status, {"error": error.code, "detail": error.detail})
            except Exception as error:  # noqa: BLE001 - 未知异常也要给壳一个稳定形状
                service.logs.append(f"[{_now()}] error {type(error).__name__}: {error}")
                return self._send(500, {"error": "sidecar_internal_error", "detail": f"{type(error).__name__}:{error}"})

    return Handler


def _delayed_stop(service: SidecarService, delay: float = 0.2) -> None:
    time.sleep(delay)
    service.stop()
    # 容器进程由入口点决定退出；此处仅停止 HTTP，留给入口点清理平台连接


def run_sidecar(port: int | None = None, service: SidecarService | None = None) -> SidecarService:
    """入口：起 HTTP 服务并阻塞（供 `agentd sidecar-run` 与测试共用）。"""

    instance = service or SidecarService()
    instance.start(port)
    info = instance.info()
    print(json.dumps({"sidecar": "started", **info}, ensure_ascii=False))
    return instance