"""本地 Agent CLI and durable Gateway client.

Legacy HTTP task commands remain available while the durable Gateway path is
introduced. The local state database stores recovery metadata, never device
secrets.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import getpass
import json
import os
import platform
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from uuid import UUID, uuid4

# Keep direct ``python apps/agent/agentd.py`` usage able to import shared packages.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from packages.agent_protocol import AgentHeartbeat
from packages.device_identity import parse_public_key, sign_registration

try:
    from .agent_inventory import AgentInventory
    from .executor_events import ExecutorEventReporter
    from .output_collector import CollectorConfig, OutputCollector
    from .codex_executor import build_codex_command, detect_codex_cli, parse_codex_jsonl, summarize_codex_result
    from .chat_loop import ChatLoop, ChatLoopConfig
    from .input_fetcher import materialize_inputs, prompt_with_inputs
    from .opencode_executor import parse_opencode_jsonl, summarize_opencode_result
    from .sidecar_api import CONTRACT_VERSION, SIDECAR_VERSION, SidecarService, default_state_dir, read_platform_info, write_platform_info
    from .gateway_client import DurableGatewayClient, GatewayIdentity
    from .local_state import LocalAgentState
    from .machine_service import LocalProcessSupervisor, MachineAgentService, MachineServiceConfig
    from .named_pipe_transport import NamedPipeServer, PipePeerPolicy, current_user_sid
    from .conpty_runner import ConPtyProcessSupervisor, HybridProcessSupervisor, conpty_capability
    from .result_uploader import AgentArtifactClient, ResultUploader
    from .runner import AdapterDescriptor, CommandAdapter, ConPtyAdapter, LocalRunner, RunnerRequest, WorkspacePolicy
    from .container_runner import ContainerPolicy, ContainerRunner, ContainerRuntime
    from .windows_etw_observer import WindowsEtwAccessObservationProvider, WindowsEtwConfig
    from .cli_adapters import ClaudeCodeAdapter, CliAdapterError, CodexAdapter
    from .credential_store import WindowsCredentialManager, create_credential_store, device_token_target, project_token_target
    from .session_runtime import SessionRuntimeConfig, SessionWorkerRuntime
    from .task_loop import TaskLoop, task_loop_config_from_identity
    from .windows_service_adapter import (
        CtypesServiceControlBackend,
        FailureRecoveryPolicy,
        ServiceInstallSpec,
        SessionWorkerCoordinator,
        WindowsServiceHost,
        WindowsSessionProcessBackend,
        WindowsServiceInstaller,
        make_session_worker_command,
    )
except ImportError:  # Support direct script execution on Windows.
    from agent_inventory import AgentInventory
    from executor_events import ExecutorEventReporter
    from output_collector import CollectorConfig, OutputCollector
    from codex_executor import build_codex_command, detect_codex_cli, parse_codex_jsonl, summarize_codex_result
    from chat_loop import ChatLoop, ChatLoopConfig
    from input_fetcher import materialize_inputs, prompt_with_inputs  # type: ignore
    from opencode_executor import parse_opencode_jsonl, summarize_opencode_result
    from sidecar_api import CONTRACT_VERSION, SIDECAR_VERSION, SidecarService, default_state_dir, read_platform_info, write_platform_info
    from gateway_client import DurableGatewayClient, GatewayIdentity
    from local_state import LocalAgentState
    from machine_service import LocalProcessSupervisor, MachineAgentService, MachineServiceConfig
    from named_pipe_transport import NamedPipeServer, PipePeerPolicy, current_user_sid
    from conpty_runner import ConPtyProcessSupervisor, HybridProcessSupervisor, conpty_capability
    from result_uploader import AgentArtifactClient, ResultUploader
    from runner import AdapterDescriptor, CommandAdapter, ConPtyAdapter, LocalRunner, RunnerRequest, WorkspacePolicy
    from container_runner import ContainerPolicy, ContainerRunner, ContainerRuntime
    from windows_etw_observer import WindowsEtwAccessObservationProvider, WindowsEtwConfig
    from cli_adapters import ClaudeCodeAdapter, CliAdapterError, CodexAdapter
    from credential_store import WindowsCredentialManager, create_credential_store, device_token_target, project_token_target
    from session_runtime import SessionRuntimeConfig, SessionWorkerRuntime
    from task_loop import TaskLoop, task_loop_config_from_identity
    from windows_service_adapter import (
        CtypesServiceControlBackend,
        FailureRecoveryPolicy,
        ServiceInstallSpec,
        SessionWorkerCoordinator,
        WindowsServiceHost,
        WindowsSessionProcessBackend,
        WindowsServiceInstaller,
        make_session_worker_command,
    )


def request(base_url: str, method: str, path: str, payload: dict | None = None, headers: dict[str, str] | None = None) -> dict | list:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(f"{base_url.rstrip('/')}{path}", data=body, method=method, headers=request_headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        # 把服务端的稳定错误码带出来：只抛 HTTPError 会让接入失败时看不到任何原因。
        detail = ""
        try:
            detail = error.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 读不到响应体时退回状态码
            detail = ""
        raise ValueError(f"http_{error.code}:{detail.strip() or error.reason}") from error


def capability_cards_from_args(args: argparse.Namespace) -> list[dict]:
    """把 `--capability-card` 的取值解析成能力卡列表（AIP-1a）。

    取值可以是内联 JSON、`@文件路径`，或 `技能@版本` 简写（版本可省）。
    解析不出来就**不猜**：直接报错让使用者看见，而不是静默丢掉声明。
    """

    raw = getattr(args, "capability_card", None)
    if not raw:
        return []
    cards: list[dict] = []
    for item in raw:
        text = str(item).strip()
        if not text:
            continue
        if text.startswith("@"):
            path = Path(text[1:]).expanduser()
            if not path.is_file():
                raise ValueError(f"capability_card_file_not_found:{path}")
            text = path.read_text(encoding="utf-8").strip()
        if text.startswith("{") or text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as error:
                raise ValueError(f"capability_card_json_invalid:{error}") from error
            for entry in parsed if isinstance(parsed, list) else [parsed]:
                if not isinstance(entry, dict) or not str(entry.get("skill") or "").strip():
                    raise ValueError("capability_card_skill_required")
                cards.append(
                    {
                        "skill": str(entry["skill"]).strip(),
                        "version": str(entry.get("version") or ""),
                        "inputs": [str(value) for value in (entry.get("inputs") or [])],
                        "outputs": [str(value) for value in (entry.get("outputs") or [])],
                        "description": str(entry.get("description") or ""),
                    }
                )
            continue
        skill, _, version = text.partition("@")
        cards.append({"skill": skill.strip(), "version": version.strip()})
    return cards


def register(args: argparse.Namespace) -> None:
    payload = {"agent_id": args.agent_id, "display_name": args.display_name, "owner_member_id": args.owner, "model_provider": args.provider, "model_name": args.model, "supported_tools": args.tools, "supported_languages": args.languages, "local_workspace": str(Path(args.workspace).resolve()), "network_policy": "deny-by-default"}
    cards = capability_cards_from_args(args)
    if cards:
        payload["capability_cards"] = cards
    executor_kind = str(getattr(args, "executor_kind", "") or "").strip().lower()
    if executor_kind:
        # 执行体程序包自报（AIP-1c）：不发的话平台会按设备探测值推断
        payload["executor"] = {"kind": executor_kind, "version": str(getattr(args, "executor_version", "") or "").strip()}
    result = request(args.url, "POST", "/api/agents/register", payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def heartbeat(args: argparse.Namespace) -> None:
    while True:
        try:
            result = request(args.url, "POST", f"/api/agents/{args.agent_id}/heartbeat")
            print(f"[{time.strftime('%H:%M:%S')}] online: {result['display_name']}")
        except (urllib.error.URLError, TimeoutError) as error:
            print(f"[{time.strftime('%H:%M:%S')}] offline: {error}")
        time.sleep(args.interval)


def claim(args: argparse.Namespace) -> None:
    payload = {
        "project_id": args.project_id,
        "stages": args.stages,
        "lease_seconds": args.lease_seconds,
        "idempotency_key": args.key or f"claim:{args.agent_id}:{uuid4().hex}",
    }
    result = request(args.url, "POST", f"/api/agents/{args.agent_id}/tasks/claim", payload, {"X-Project-Capability-Token": args.project_token})
    print(json.dumps(result, ensure_ascii=False, indent=2))


def progress(args: argparse.Namespace) -> None:
    payload = {
        "agent_id": args.agent_id,
        "lease_token": args.lease_token,
        "status": args.status,
        "message": args.message,
        "idempotency_key": args.key or f"progress:{args.task_id}:{uuid4().hex}",
    }
    result = request(args.url, "POST", f"/api/tasks/{args.task_id}/progress", payload, {"X-Project-Capability-Token": args.project_token})
    print(json.dumps(result, ensure_ascii=False, indent=2))


def complete(args: argparse.Namespace) -> None:
    payload = {
        "agent_id": args.agent_id,
        "lease_token": args.lease_token,
        "success": not args.failed,
        "summary": args.summary,
        "output_artifact_ids": args.artifact_ids,
        "handoff_id": args.handoff_id,
        "idempotency_key": args.key or f"result:{args.task_id}:{uuid4().hex}",
    }
    result = request(args.url, "POST", f"/api/tasks/{args.task_id}/result", payload, {"X-Project-Capability-Token": args.project_token})
    print(json.dumps(result, ensure_ascii=False, indent=2))


def lease_heartbeat(args: argparse.Namespace) -> None:
    payload = {
        "agent_id": args.agent_id,
        "project_id": args.project_id,
        "extend_seconds": args.extend_seconds,
    }
    result = request(
        args.url,
        "POST",
        f"/api/task-leases/{args.lease_token}/heartbeat",
        payload,
        {"X-Project-Capability-Token": args.project_token},
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def device_register(args: argparse.Namespace) -> None:
    public_key = args.public_key
    challenge_signature = args.challenge_signature
    if args.private_key:
        try:
            private_key = serialization.load_pem_private_key(
                Path(args.private_key).read_bytes(), password=None
            )
        except (OSError, ValueError, TypeError) as error:
            raise ValueError("device_private_key_invalid") from error
        if not isinstance(private_key, Ed25519PrivateKey):
            raise ValueError("device_private_key_algorithm_unsupported")
        derived_public_key = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")
        if public_key is not None:
            try:
                _, supplied_fingerprint = parse_public_key(public_key)
                _, derived_fingerprint = parse_public_key(derived_public_key)
            except ValueError as error:
                raise ValueError("device_public_key_invalid") from error
            if supplied_fingerprint != derived_fingerprint:
                raise ValueError("device_public_key_private_key_mismatch")
        else:
            public_key = derived_public_key
        _, fingerprint = parse_public_key(public_key)
        generated_signature = sign_registration(
            private_key,
            args.pairing_id,
            args.challenge,
            args.agent_id,
            args.device_id,
            fingerprint,
        )
        if challenge_signature is not None and challenge_signature != generated_signature:
            raise ValueError("device_signature_private_key_mismatch")
        challenge_signature = generated_signature
    if not public_key or not challenge_signature:
        raise ValueError("device_private_key_or_signature_required")
    result = request(
        args.url,
        "POST",
        "/api/devices/register",
        {
            "pairing_code": args.pairing_code,
            "pairing_id": args.pairing_id,
            "challenge": args.challenge,
            "challenge_signature": challenge_signature,
            "agent_id": args.agent_id,
            "device_id": args.device_id,
            "device_name": args.device_name,
            "public_key": public_key,
            "platform": args.platform,
            "agent_version": args.agent_version,
            "capabilities": args.capabilities,
        },
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


WORKER_CONFIG_NAME = "worker.json"


def _decode_grant(blob: str) -> dict:
    """解析授权串（base64url(JSON)），与 Web 向导页的 projectGrantBlob 一致。"""

    padded = blob.strip().replace("-", "+").replace("_", "/")
    padded += "=" * ((4 - len(padded) % 4) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception as error:  # noqa: BLE001 - 统一报成稳定错误
        raise ValueError("worker_grant_invalid") from error
    for field in ("project_id", "project_token"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise ValueError(f"worker_grant_missing_{field}")
    return payload


def _worker_config_path(args: argparse.Namespace) -> Path:
    state_path = Path(getattr(args, "state_path", "") or (Path.home() / ".math-agent-platform" / "agentd.db"))
    return state_path.parent / WORKER_CONFIG_NAME


def _load_worker_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_worker_identity(args: argparse.Namespace) -> dict:
    """解析 worker 身份：--grant（首次）或本地配置 + 凭据管理器（后续）。"""

    config_path = _worker_config_path(args)
    config = _load_worker_config(config_path)

    grant_blob = getattr(args, "grant", None)
    if grant_blob:
        payload = _decode_grant(grant_blob)
        # 项目 Token 只进凭据后端：与设备 Token 一样不落本地文件。
        if not getattr(args, "skip_credential", False):
            _credential_store(args).put(project_token_target(payload["project_id"]), payload["project_token"])
        # 授权串里的 device_id **优先**：平台按"这台设备是否可以替这个项目干活"校验，
        # 而 --device-id / platform.json 只说明"我是谁"。两者不一致时必须以授权串为准，
        # 否则会出现"能领任务、能上报结果，但 Gateway 的过程事件被判
        # gateway_project_token_identity_mismatch"这种半通不通的状态。
        config.update({
            "project_id": payload["project_id"],
            "agent_id": payload.get("agent_id") or getattr(args, "agent_id", None) or config.get("agent_id"),
            "device_id": payload.get("device_id") or getattr(args, "device_id", None) or config.get("device_id"),
            "capabilities": payload.get("capabilities") or config.get("capabilities") or [],
            "grant_expires_at": payload.get("expires_at") or config.get("grant_expires_at"),
        })
        discovered = detect_codex_cli(getattr(args, "codex_path", None))
        if discovered:
            config["codex_path"] = discovered
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    project_id = getattr(args, "project_id", None) or config.get("project_id")
    agent_id = getattr(args, "agent_id", None) or config.get("agent_id")
    if not project_id:
        raise ValueError("worker_project_required:先执行带 --grant 的首次接入，或显式提供 --project-id")
    if not agent_id:
        raise ValueError("worker_agent_required:配置里没有 agent_id，请显式提供 --agent-id")

    token = getattr(args, "project_token", None)
    if not token:
        try:
            token = _credential_store(args).get(project_token_target(project_id))
        except Exception as error:  # noqa: BLE001 - 凭据缺失要给出可执行的提示
            raise ValueError(
                f"worker_project_token_missing:{project_id}（用带 --grant 的首次接入写入凭据，或显式传 --project-token）"
            ) from error
    if not getattr(args, "codex_path", None) and config.get("codex_path"):
        args.codex_path = config["codex_path"]
    device_id = getattr(args, "device_id", None) or config.get("device_id")
    if not device_id:
        # LocalRunner 会把 device_id 写进进程规范与运行清单，缺了它执行层直接拒绝。
        raise ValueError("worker_device_required:授权串里没有 device_id，请显式提供 --device-id")
    return {
        "project_id": project_id,
        "agent_id": agent_id,
        "device_id": device_id,
        "project_token": token,
        "capabilities": config.get("capabilities") or [],
        "config_path": config_path,
    }


def _task_command(task: dict, fallback: list[str] | None) -> list[str] | None:
    """任务级命令（resource_policy.worker_command）优先，其次 worker 级默认命令。"""

    policy = task.get("resource_policy") or {}
    declared = policy.get("worker_command") if isinstance(policy, dict) else None
    if isinstance(declared, str) and declared.strip():
        return [declared.strip()]
    if isinstance(declared, (list, tuple)) and declared:
        return [str(item) for item in declared]
    return fallback


def _executor_kind(task: dict) -> str:
    """任务的执行体类型：codex（协议适配）/ cli（通用提示词驱动）/ command（声明式 argv）/ 未声明。"""

    policy = task.get("resource_policy") or {}
    if not isinstance(policy, dict):
        return "unspecified"
    executor = str(policy.get("worker_executor") or "").strip().lower()
    if executor in {"codex", "cli"}:
        return executor
    return "command" if policy.get("worker_command") else "unspecified"


def _is_codex_executor(task: dict) -> bool:
    """任务是否要求用 Codex CLI 执行（resource_policy.worker_executor == "codex"）。"""

    return _executor_kind(task) == "codex"


# 通用 CLI 里"已知事件协议"的执行体：命令模板首词命中它，就按该协议解析 stdout。
# 只认我们**采过真实样本、有测试**的执行体，不做通用猜测（其余一律按 stdout 即结果处理）。
_KNOWN_CLI_PROTOCOLS = {"opencode": "opencode"}


def _executor_label(inventory: Any) -> str | None:
    """这台机器"主力执行体"的标签（心跳里的展示字段）：opencode 优先，其次 codex。"""

    versions = inventory.adapter_versions()
    if versions.get("opencode-cli"):
        return "opencode"
    if versions.get("codex-cli"):
        return "codex"
    return None


def _event_protocol(task: dict) -> str:
    """这次执行按哪种事件协议解析 stdout：`codex` / `opencode` / `none`。

    优先级：显式声明 `resource_policy.worker_events` > 命令模板首词命中已知执行体 > `none`。
    声明与模板不一致时**以声明为准**（人写下来的比推断可信）。
    """

    policy = task.get("resource_policy") or {}
    if not isinstance(policy, dict):
        return "none"
    kind = _executor_kind(task)
    if kind == "codex":
        return "codex"
    if kind != "cli":
        return "none"
    declared = str(policy.get("worker_events") or "").strip().lower()
    if declared in {"codex", "opencode", "none"}:
        return declared
    template = _task_command(task, None) or []
    first = str(template[0]).strip().rsplit("/", 1)[-1].lower() if template else ""
    return _KNOWN_CLI_PROTOCOLS.get(first, "none")


def _cli_command(task: dict, fallback: list[str] | None) -> list[str] | None:
    """通用 CLI 执行（worker_executor == "cli"）：命令模板的 {prompt} 换成任务提示词。

    模板由任务声明（例如 ["workbuddy", "exec", "{prompt}"] 或 ["zcode", "run", "{prompt}"]）——
    平台不猜各家 CLI 的参数语义：stdout 即结果、退出码即成败，与声明式命令同一套边界。
    """

    policy = task.get("resource_policy") or {}
    template = _task_command(task, fallback)
    if not template:
        return None
    prompt = _codex_prompt(task)
    substituted = [str(item).replace("{prompt}", prompt) for item in template]
    return substituted


def _codex_prompt(task: dict) -> str:
    """Codex 的提示词：优先 worker_prompt，其次任务说明+完成标准，最后退回标题。"""

    policy = task.get("resource_policy") or {}
    declared = str(policy.get("worker_prompt") or "").strip() if isinstance(policy, dict) else ""
    if declared:
        return declared
    parts = [str(task.get("title") or "").strip(), str(task.get("description") or "").strip()]
    criteria = task.get("acceptance_criteria") or []
    if criteria:
        parts.append("完成标准：\n" + "\n".join(f"- {item}" for item in criteria))
    prompt = "\n\n".join(part for part in parts if part)
    return prompt or "请完成这个任务并给出结论。"


# 执行子进程需要的环境变量白名单：定位用户配置/凭据、临时目录与可执行搜索路径。
# 平台禁止继承整个父环境，所以这里显式列出——不给的变量子进程就看不到。
_EXECUTOR_ENVIRONMENT_KEYS = (
    "USERPROFILE",   # Codex 靠它找 ~/.codex（配置与凭据）
    "HOME",
    "APPDATA",
    "LOCALAPPDATA",  # 桌面端解包目录（codex.exe 自身位置）
    "CODEX_HOME",
    "PATH",
    "PATHEXT",
    "ComSpec",
    "TEMP",
    "TMP",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PROCESSOR_ARCHITECTURE",
)


def _executor_environment() -> dict[str, str]:
    """按白名单收集当前进程里存在的变量（缺的就不传，不编造空值）。"""

    return {key: os.environ[key] for key in _EXECUTOR_ENVIRONMENT_KEYS if os.environ.get(key)}


async def _execute_task(
    task: dict,
    identity: dict,
    args: argparse.Namespace,
    reporter: Any | None = None,
) -> tuple[bool, str, str, str]:
    """执行一个任务。

    - ``resource_policy.worker_executor = "codex"``：用 Codex CLI 执行（JSONL 事件解析成摘要）；
    - 否则按声明式命令（``worker_command``）走 LocalRunner；
    - 两者都没有就显式失败，不假装成功。

    ``reporter``（`executor_events.ExecutorEventReporter`）让调用方在执行**过程中**就看到 stdout；
    不传就是"跑完才知道结果"的老行为。终态事件由任务循环发（它知道成功与否与 run_id）。
    """

    kind = _executor_kind(task)
    codex_mode = kind == "codex"
    protocol = _event_protocol(task)
    # M-3：任务的输入成果物先下到 <workspace>/inputs/。
    # 平台在任务里只给 id（不给名字），文件名从下载响应的 Content-Disposition 取（见 AgentArtifactClient.download）。
    input_note = ""
    input_ids = [str(item) for item in (task.get("input_artifacts") or [])]
    if input_ids:
        identity_for_inputs = loop_identity or {}
        try:
            written, failed = materialize_inputs(
                AgentArtifactClient(
                    args.url,
                    str(identity_for_inputs.get("project_token") or ""),
                    str(identity_for_inputs.get("agent_id") or ""),
                ),
                [{"artifact_id": item, "name": ""} for item in input_ids],
                workspace=Path(args.workspace).expanduser().resolve(),
                log=print,
            )
            input_note = prompt_with_inputs("", written, failed)
        except Exception as error:  # noqa: BLE001 - 输入取不到也要照常跑，但如实写进提示词
            input_note = f"[输入文件处理失败：{type(error).__name__}，本轮没有可用的输入文件]{chr(10)}"
            print(f"[inputs] 处理失败：{type(error).__name__}: {error}")
    if input_note:
        # 把"有哪些输入文件"并进提示词：写成 worker_prompt，后面组命令时自然会用上
        task = {**task, "resource_policy": {**(task.get("resource_policy") or {}), "worker_prompt": input_note + _codex_prompt(task)}}
    command = _cli_command(task, args.executor_command) if kind == "cli" else _task_command(task, args.executor_command)
    if kind == "cli" and command:
        print(f"[worker] 通用 CLI 执行体：{command[0]}（提示词已注入命令模板，事件协议 {protocol}）")
    if codex_mode:
        codex_path = detect_codex_cli(getattr(args, "codex_path", None))
        if not codex_path:
            return (
                False,
                "codex_cli_not_found:未找到 codex CLI。装好 ChatGPT 桌面端/Codex CLI 后，"
                "或用 --codex-path 指定；也可在 connect-agent.ps1 加 -Codex 自动探测。",
                "",
                "",
            )
        try:
            command = build_codex_command(
                codex_path,
                _codex_prompt(task),
                sandbox=getattr(args, "codex_sandbox", "read-only") or "read-only",
            )
        except CliAdapterError as error:
            return False, f"codex_command_rejected:{error}", "", ""
        print(f"[worker] Codex 执行体：{codex_path}")
    if not command:
        return (
            False,
            "executor_not_configured:任务没有声明 resource_policy.worker_command 或 worker_executor=codex，"
            "worker 也没有 --executor-command",
            "",
            "",
        )

    run_id = f"run-{uuid4().hex[:12]}"
    if reporter is not None:
        reporter.started(command, kind if kind in {"codex", "cli"} else "command", protocol=protocol)
    workspace = Path(args.workspace).expanduser().resolve()
    state = LocalAgentState(args.state_path)
    process_output = None
    if reporter is not None:
        # 只喂 stdout：Codex 的 JSONL 在 stdout 上，stderr 是它自己的诊断信息
        def process_output(_spec: Any, stream_name: str, chunk: str) -> None:  # noqa: ANN401 - 回调签名由 supervisor 决定
            if stream_name == "stdout":
                reporter.feed(chunk)
    supervisor = LocalProcessSupervisor(state, args.process_stop_timeout, output_callback=process_output)
    descriptor = AdapterDescriptor(
        adapter_id="worker-command",
        agent_name="worker-command",
        agent_version="0.1.0",
        adapter_version="0.1.0",
        supported_os=("Windows", "Linux", "Darwin"),
        supported_execution_profiles=("HEADLESS",),
        supported_capabilities=tuple(identity.get("capabilities") or ()),
    )
    environment = _executor_environment()
    runner = LocalRunner(
        supervisor,
        WorkspacePolicy(
            allowed_roots=(str(workspace),),
            allowed_executables=tuple(command[:1]),
            # env 的键必须显式允许，否则 runner 会拒绝（不是继承，而是白名单）
            allowed_environment_keys=tuple(environment.keys()),
        ),
        [CommandAdapter(descriptor)],
        current_os=platform.system(),
    )
    request = RunnerRequest(
        project_id=identity["project_id"],
        task_id=str(task.get("id") or ""),
        run_id=run_id,
        agent_id=identity["agent_id"],
        device_id=identity["device_id"],
        workspace_id="worker",
        workspace_path=str(workspace),
        command=tuple(command),
        environment=environment,
        timeout_seconds=args.task_timeout,
        parameters={"task_title": task.get("title", "")},
    )
    result = await runner.run("worker-command", request)
    if reporter is not None:
        reporter.flush()

    if codex_mode:
        # 判定与摘要都走 codex_executor 的单一实现（成功规则要能被单独测到）
        success, summary = summarize_codex_result(result.exit_code, parse_codex_jsonl(result.stdout or ""), run_id)
        # stderr 与原始 stdout 分开返回：Run 台账里 stdout 保留 JSONL 便于排查
        return success, summary, result.stdout or "", result.stderr or ""

    if protocol == "opencode":
        # 同理：`--format json` 的 stdout 是事件流，直接当结果会把一串 JSON 塞进摘要与成果物
        success, summary = summarize_opencode_result(
            result.exit_code, parse_opencode_jsonl(result.stdout or ""), run_id
        )
        return success, summary, result.stdout or "", result.stderr or ""

    stdout_tail = (result.stdout or "").strip()[-400:]
    stderr_tail = (result.stderr or "").strip()[-300:]
    summary = f"exit={result.exit_code} run={run_id}"
    if stdout_tail:
        summary += f" | stdout: {stdout_tail}"
    if stderr_tail:
        summary += f" | stderr: {stderr_tail}"
    return result.exit_code == 0, summary, result.stdout or "", result.stderr or ""


def _build_task_loop(
    identity: dict,
    args: argparse.Namespace,
    label: str = "worker",
    event_sink: Callable[[str, dict], None] | None = None,
    collect_outputs: bool = False,
    executor_slot: Any | None = None,
) -> TaskLoop:
    """把 CLI/配置解析结果拼成任务循环（`worker-run` 与 `daemon-run` 共用）。

    `event_sink` 只有常驻体（daemon）会给：执行期事件走 Gateway 的持久化队列，
    由常驻体负责投递；前台调试（worker-run）不给 sink，免得本地 outbox 堆积永远发不出去的事件。

    `collect_outputs` 同理默认关：`demo-1.0.ps1` 用 `worker-run --once` 跑演示链路，
    默认采集会给演示项目塞进一堆产出。常驻体（daemon-run）默认开——那才是"装完就干活"的路径。
    """

    config = task_loop_config_from_identity(
        identity,
        url=args.url,
        stages=args.stages,
        lease_seconds=args.lease_seconds,
        idle_seconds=args.idle_seconds,
        max_idle_seconds=args.max_idle_seconds,
    )
    # reporter 总是建：emit=None 时它不发过程事件，但仍会解析输出里的**用量**（COST-1），
    # 否则前台调试路径（worker-run）永远报不出 token。
    reporter = ExecutorEventReporter(emit=event_sink)
    collector = _build_output_collector(identity, args) if collect_outputs else None

    async def execute(task: dict, loop_identity: dict, run_id: str | None = None) -> tuple[bool, str, str, str]:
        if reporter is not None:
            reporter.task_id = str(task.get("id") or "") or None
            reporter.run_id = run_id
        if executor_slot is None:
            return await _execute_task(task, loop_identity, args, reporter=reporter)
        # 与「对话」循环共用一把锁：一台机器同时只跑一个执行体进程（两个 opencode 会互相抢资源，输出也分不清）
        async with executor_slot:
            return await _execute_task(task, loop_identity, args, reporter=reporter)

    return TaskLoop(
        config,
        http=request,
        execute=execute,
        identity=identity,
        log=print,
        label=label,
        event_sink=event_sink,
        executor_stats=reporter.stats,
        usage_provider=reporter.usage,
        output_collector=collector,
    )


def _build_output_collector(identity: dict, args: argparse.Namespace) -> Any | None:
    """构造产出采集器：需要项目能力 Token（上传成果物要 `artifact.write`）。

    状态库单独一个文件（`uploads.db`，与执行体的 agentd.db 分开）：
    上传队列是"内容侧"的账本，不该和运行状态互相争 SQLite 锁。
    """

    project_id = str(identity.get("project_id") or "")
    token = str(identity.get("project_token") or "")
    agent_id = str(identity.get("agent_id") or "")
    if not project_id or not token or not agent_id:
        return None
    state_path = Path(args.state_path).expanduser().parent / "uploads.db"
    return OutputCollector(
        CollectorConfig(
            workspace=Path(args.workspace).expanduser(),
            url=args.url,
            project_id=project_id,
            agent_id=agent_id,
            project_token=token,
            state_path=state_path,
        ),
        log=print,
    )


def worker_run(args: argparse.Namespace) -> None:
    """前台调试入口：claim → 执行 → 上报结果（UX-5-01，DP-2-02 保留）。

    - 授权：首次用 ``--grant <授权串>``（向导页复制），之后从凭据管理器与 worker.json 读取；
    - 空队列：退避轮询（``--idle-seconds`` 起步，指数增长到 ``--max-idle-seconds``）；
    - 执行：任务声明 ``resource_policy.worker_command`` 或用 ``worker_executor=codex`` 时用 LocalRunner/Codex 跑；
      没有声明则显式记为失败，不做"假装成功"的上报；
    - ``--once`` 只跑一轮（测试与演示用）。

    常驻版本见 ``daemon-run``（同一套循环 + 平台连接监督）。
    """

    identity = _resolve_worker_identity(args)
    print(json.dumps({
        "worker": "started",
        "agent_id": identity["agent_id"],
        "device_id": identity["device_id"],
        "project_id": identity["project_id"],
        "capabilities": identity["capabilities"],
        "config": str(identity["config_path"]),
        "mode": "once" if args.once else "loop",
    }, ensure_ascii=False, indent=2))

    loop = _build_task_loop(identity, args, collect_outputs=bool(getattr(args, "collect_outputs", False)))
    if args.once:
        asyncio.run(loop.run_once())
        # `--once` 是调试入口：领取失败必须让调用方看见（非零退出），不能静默当作"没活干"。
        snapshot = loop.snapshot()
        if snapshot["claim_errors"]:
            raise ValueError(f"worker_claim_failed:{snapshot['last_claim_error']}")
        return
    try:
        asyncio.run(loop.run_forever())
    except KeyboardInterrupt:
        print(json.dumps({"worker": "stopped", "stats": loop.snapshot()}, ensure_ascii=False))


def keygen(args: argparse.Namespace) -> None:
    """生成 Ed25519 设备身份密钥对（UX-4-01）。

    - 私钥写成**未加密 PKCS8 PEM**——`device-register --private-key` 正是以 ``password=None``
      加载，加密私钥会让配对在运行时才失败；
    - 公钥写成 SubjectPublicKeyInfo PEM，可直接作为 ``--public-key``；
    - 指纹与平台一致：DER SPKI 的 SHA-256（`packages.device_identity.parse_public_key`），
      同时落一个 ``.fingerprint`` 文件供接入脚本读取；
    - 已存在同名密钥时默认拒绝覆盖，需要 ``--force`` 显式覆盖（避免把已在平台注册过的
      设备身份悄悄换掉）。

    Windows 下不做 chmod（无 POSIX 权限位），私钥的访问控制依赖用户目录 ACL。
    """

    directory = Path(args.directory).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    private_path = directory / f"{args.name}.key"
    public_path = directory / f"{args.name}.pub"
    fingerprint_path = directory / f"{args.name}.fingerprint"

    existing = [path for path in (private_path, public_path) if path.exists()]
    if existing and not args.force:
        raise ValueError(f"keygen_refused_existing_key:{existing[0]}")

    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    _, fingerprint = parse_public_key(public_pem.decode("ascii"))

    private_path.write_bytes(private_pem)
    public_path.write_bytes(public_pem)
    fingerprint_path.write_text(fingerprint + "\n", encoding="ascii")
    if os.name != "nt":
        os.chmod(private_path, 0o600)

    print(
        json.dumps(
            {
                "algorithm": "ed25519",
                "private_key_path": str(private_path),
                "public_key_path": str(public_path),
                "fingerprint_path": str(fingerprint_path),
                "public_key_fingerprint": fingerprint,
                "next_step": (
                    "python apps/agent/agentd.py device-register "
                    "--pairing-code <CODE> --pairing-id <ID> --challenge <CHALLENGE> "
                    "--agent-id <AGENT_ID> --device-id <DEVICE_ID> --device-name <NAME> "
                    f'--private-key "{private_path}"'
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def gateway_queue(args: argparse.Namespace) -> None:
    state = LocalAgentState(args.state_path)
    try:
        payload = json.loads(args.payload)
        if not isinstance(payload, dict):
            raise ValueError("payload_must_be_object")
        client = DurableGatewayClient(
            state,
            GatewayIdentity(
                device_id=args.device_id,
                agent_id=args.agent_id,
                session_id=args.session_id,
                connection_id=args.connection_id,
            ),
        )
        event = client.queue_event(
            args.message_type,
            payload,
            idempotency_key=args.idempotency_key or f"agent-event:{uuid4().hex}",
            message_id=args.message_id,
        )
        print(json.dumps(event, ensure_ascii=False, indent=2))
    finally:
        state.close()


def gateway_recover(args: argparse.Namespace) -> None:
    state = LocalAgentState(args.state_path)
    try:
        recovered = state.recover_unacked()
        print(json.dumps({"recovered": recovered, "pending": state.pending_events()}, ensure_ascii=False, indent=2))
    finally:
        state.close()


def gateway_run(args: argparse.Namespace) -> None:
    state = LocalAgentState(args.state_path)
    try:
        client = DurableGatewayClient(
            state,
            GatewayIdentity(
                device_id=args.device_id,
                agent_id=args.agent_id,
                session_id=args.session_id,
                connection_id=args.connection_id,
            ),
        )
        asyncio.run(client.run_once(args.uri, resolve_device_token(args)))
    finally:
        state.close()


def service_run(args: argparse.Namespace) -> None:
    state = LocalAgentState(args.state_path)
    try:
        client = DurableGatewayClient(
            state,
            GatewayIdentity(
                device_id=args.device_id,
                agent_id=args.agent_id,
                session_id=args.session_id,
                connection_id=args.connection_id,
            ),
        )
        config = MachineServiceConfig(
            gateway_uri=args.uri,
            agent_version=args.agent_version,
            capabilities=tuple(args.capabilities),
            heartbeat_interval_seconds=args.heartbeat_interval,
            send_poll_interval_seconds=args.send_poll_interval,
            retry_after_seconds=args.retry_after_seconds,
            reconnect_base_seconds=args.reconnect_base,
            reconnect_max_seconds=args.reconnect_max,
            control_poll_interval_seconds=args.control_poll_interval,
            process_stop_timeout_seconds=args.process_stop_timeout,
        )
        service = MachineAgentService(client, lambda: resolve_device_token(args), config)
        status = asyncio.run(service.run_forever())
        print(json.dumps({"status": status, "snapshot": service.snapshot()}, ensure_ascii=False, indent=2))
    finally:
        state.close()


def service_emergency_stop(args: argparse.Namespace) -> None:
    state = LocalAgentState(args.state_path)
    try:
        state.set_emergency_stop(args.reason)
        print(json.dumps(state.emergency_stop_state(), ensure_ascii=False, indent=2))
    finally:
        state.close()


def service_clear_emergency_stop(args: argparse.Namespace) -> None:
    state = LocalAgentState(args.state_path)
    try:
        state.clear_emergency_stop()
        print(json.dumps(state.emergency_stop_state(), ensure_ascii=False, indent=2))
    finally:
        state.close()


def resolve_device_token(args: argparse.Namespace) -> str:
    """Resolve a token without ever putting the keyring value in local state."""

    explicit_token = getattr(args, "device_token", None)
    if explicit_token:
        return explicit_token
    target = getattr(args, "credential_target", None) or device_token_target(args.device_id)
    return _credential_store(args).get(target)


def _credential_store(args: argparse.Namespace) -> Any:
    """这次运行的凭据后端。

    默认 `auto`：**Windows 走凭据管理器**（既有行为不变），**POSIX 走 0600 文件**——后者是云端
    执行体（headless）需要的等价物，目录固定在状态目录旁边 `<state_dir>/credentials/`。
    文件后端是对"桌面端不得把设备/项目令牌持久化到 Windows 凭据管理器之外"这条硬约束的
    **限定例外**（意图是"别在 Windows 上到处撒密钥"），详见 `credential_store` 模块 docstring。
    """

    backend = str(getattr(args, "credential_backend", "auto") or "auto").strip().lower()
    if backend == "auto":
        backend = "windows" if os.name == "nt" else "file"
    if backend == "windows":
        # 保持模块级名字引用：既有测试用 patch("agentd.WindowsCredentialManager") 注入假实现
        return WindowsCredentialManager()
    state_path = getattr(args, "state_path", None)
    # 没有 --state-path 时用约定的状态目录（POSIX: ~/.math-agent-platform）、**不要**退回当前目录：
    # 否则凭据会悄悄落在 cwd 的 credentials/ 里，找都找不到（云端部署踩过一次）。
    base = Path(state_path).expanduser() if state_path else default_state_dir() / "agentd.db"
    return create_credential_store(backend=backend, directory=base.parent / "credentials")


def credential_save(args: argparse.Namespace) -> None:
    target = args.target or device_token_target(args.device_id)
    token = sys.stdin.read().strip() if args.token_stdin else getpass.getpass("Device token (input hidden): ")
    _credential_store(args).put(target, token)
    print(json.dumps({"status": "STORED", "target": target}, ensure_ascii=False, indent=2))


def credential_delete(args: argparse.Namespace) -> None:
    target = args.target or device_token_target(args.device_id)
    deleted = _credential_store(args).delete(target)
    print(json.dumps({"status": "DELETED" if deleted else "NOT_FOUND", "target": target}, ensure_ascii=False, indent=2))


def conpty_capability_command(args: argparse.Namespace) -> None:
    del args
    print(json.dumps(conpty_capability(), ensure_ascii=False, indent=2))


def cli_capability(args: argparse.Namespace) -> None:
    """Report an external CLI capability without claiming unsupported support."""
    adapter = CodexAdapter(args.executable or "codex") if args.adapter == "codex" else ClaudeCodeAdapter(args.executable)
    print(json.dumps(adapter.probe(args.timeout).as_dict(), ensure_ascii=False, indent=2))


def service_install(args: argparse.Namespace) -> None:
    backend = CtypesServiceControlBackend()
    spec = ServiceInstallSpec(
        service_name=args.service_name,
        display_name=args.display_name,
        command=tuple(args.command),
        description=args.description,
        start_type=args.start_type,
        service_account=args.service_account,
        dependencies=tuple(args.dependencies),
        failure_recovery=FailureRecoveryPolicy(tuple(args.restart_delays), args.reset_period),
    )
    result = WindowsServiceInstaller(backend).install_or_update(spec)
    print(json.dumps({"status": result, "service_name": spec.service_name}, ensure_ascii=False, indent=2))


def service_control(args: argparse.Namespace) -> None:
    backend = CtypesServiceControlBackend()
    if args.service_action == "start":
        backend.start(args.service_name)
    elif args.service_action == "stop":
        backend.stop(args.service_name, args.timeout)
    elif args.service_action == "uninstall":
        backend.uninstall(args.service_name)
    elif args.service_action == "status":
        print(json.dumps({"service_name": args.service_name, "status": backend.status(args.service_name)}, ensure_ascii=False, indent=2))
        return
    print(json.dumps({"status": "OK", "service_name": args.service_name, "action": args.service_action}, ensure_ascii=False, indent=2))


def service_host(args: argparse.Namespace) -> None:
    backend = WindowsSessionProcessBackend()
    workspace = str(Path(args.workspace).resolve())
    state_path = str(Path(args.state_path).resolve())

    def pipe_name(session: object) -> str:
        return rf"\\.\pipe\math-agent-{session.windows_session_id}"

    def worker_command(session: object, pipe: str) -> tuple[str, ...]:
        return make_session_worker_command(
            tuple(args.agentd_command),
            pipe_name=pipe,
            worker_id=f"worker-{session.session_id}",
            user_session_id=session.session_id,
            user_sid=session.user_sid,
            workspace=workspace,
            allowed_peer_id=args.allowed_peer_id,
            allowed_sids=tuple(args.allowed_sids),
            allowed_session_ids=(0,),
            state_path=state_path,
            adapter_id=args.adapter_id,
            conpty_adapter_id=args.conpty_adapter_id,
            python_executable=args.python_executable,
        )

    coordinator = SessionWorkerCoordinator(
        backend,
        worker_command_factory=worker_command,
        worker_cwd=workspace,
        pipe_name_factory=pipe_name,
        machine_peer_id=args.allowed_peer_id,
        machine_process_id=os.getpid(),
        machine_windows_session_id=0,
    )
    WindowsServiceHost(
        coordinator,
        reconcile_interval_seconds=args.reconcile_interval,
    ).run_as_windows_service(args.service_name)


def session_worker_run(args: argparse.Namespace) -> None:
    workspace = Path(args.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    state = LocalAgentState(args.state_path)
    runtime = None
    server = None
    uploader = None
    stop_event = threading.Event()
    try:
        descriptor = AdapterDescriptor(
            adapter_id=args.adapter_id,
            agent_name="Python User Session Runner",
            agent_version=args.agent_version,
            adapter_version="0.1.0",
            supported_os=("Windows",),
            supported_execution_profiles=("USER_SESSION",),
            supported_capabilities=tuple(args.capabilities),
            input_mode="stdin",
            output_mode="stdout-stderr",
        )
        conpty_adapter_id = args.conpty_adapter_id or f"{args.adapter_id}-conpty"
        conpty_descriptor = AdapterDescriptor(
            adapter_id=conpty_adapter_id,
            agent_name="Python User Session ConPTY Runner",
            agent_version=args.agent_version,
            adapter_version="0.1.0",
            supported_os=("Windows",),
            supported_execution_profiles=("USER_SESSION",),
            supported_capabilities=tuple(args.capabilities),
            input_mode="conpty",
            output_mode="conpty",
        )
        pipe_supervisor = LocalProcessSupervisor(state, args.process_stop_timeout)
        conpty_supervisor = ConPtyProcessSupervisor(state, args.process_stop_timeout)
        access_observation_provider = None
        if getattr(args, "access_observer", "none") == "windows-etw":
            access_observation_provider = WindowsEtwAccessObservationProvider(
                WindowsEtwConfig(output_directory=getattr(args, "etw_output_directory", None)),
            )
        runner = LocalRunner(
            HybridProcessSupervisor(pipe_supervisor, conpty_supervisor),
            WorkspacePolicy(
                allowed_roots=(str(workspace),),
                allowed_executables=(args.python_executable,),
                allowed_environment_keys=tuple(args.allowed_environment_keys),
            ),
            [CommandAdapter(descriptor), ConPtyAdapter(conpty_descriptor)],
            access_observation_provider=access_observation_provider,
            current_os="Windows",
        )
        container_runners = {}
        if args.container_runtime:
            container_runtime = ContainerRuntime(args.container_runtime)
            container_runners[args.container_runtime] = ContainerRunner(
                runner.supervisor,
                ContainerPolicy(allowed_roots=(str(workspace),)),
                container_runtime,
            )
        if args.project_token and args.agent_id:
            uploader = ResultUploader(
                state,
                AgentArtifactClient(args.url, args.project_token, args.agent_id),
            )

        def persist_event(event: object) -> None:
            if not args.project_token:
                return
            state.enqueue_event(
                "agent.event",
                {
                    "project_id": event.project_id,
                    "project_token": args.project_token,
                    "event": event.model_dump(mode="json"),
                },
                idempotency_key=f"session-event:{args.worker_id}:{event.sequence}",
                message_id=event.event_id,
            )

        def upload_run_outputs(request: object, result: object) -> list[str]:
            if uploader is None:
                return []
            return uploader.upload_run_outputs(request, result)

        def persist_transition(transition: object) -> None:
            state.save_lifecycle_transition(transition)

        runtime = SessionWorkerRuntime(
            SessionRuntimeConfig(
                worker_id=args.worker_id,
                user_session_id=args.user_session_id,
                user_sid=args.user_sid or current_user_sid(),
                supported_execution_modes=("USER_SESSION", "HEADLESS") if container_runners else ("USER_SESSION",),
                capabilities=tuple(args.capabilities),
                current_os="Windows",
            ),
            runner,
            container_runners=container_runners,
            on_event=persist_event,
            on_transition=persist_transition,
            on_run_complete=upload_run_outputs,
        )
        server = NamedPipeServer(
            args.pipe_name,
            PipePeerPolicy(
                peer_id=args.allowed_peer_id,
                peer_kind=args.allowed_peer_kind,
                allowed_sids=tuple(args.allowed_sids),
                allowed_session_ids=tuple(args.allowed_session_ids),
            ),
        )
        print(json.dumps({"status": "LISTENING", "pipe": server.name, "worker_id": args.worker_id}, ensure_ascii=False))
        server.serve_forever(
            runtime.handle,
            stop_event=stop_event,
            on_error=lambda error: print(json.dumps({"status": "CLIENT_REJECTED", "error": str(error)}, ensure_ascii=False)),
        )
    except KeyboardInterrupt:
        print(json.dumps({"status": "STOPPING", "worker_id": args.worker_id}, ensure_ascii=False))
    finally:
        stop_event.set()
        if server is not None:
            server.close()
        if runtime is not None:
            runtime.close()
        state.close()



# 全局 `--url` 的开发默认值。它只对"随手调试"有意义：对常驻体来说，**"没显式指定"必须能区分出来**，
# 否则它会盖住配对时落盘的 platform.json（云端部署踩过：daemon 拿这个默认值去连 127.0.0.1）。
DEV_PLATFORM_URL = "http://localhost:8000"


def _resolve_platform_url(args: argparse.Namespace, platform_info: dict, fallback: str) -> str:
    """平台地址优先级：命令行 > 配对时落盘的 platform.json > 本地默认。"""

    cli_url = getattr(args, "url", None)
    if cli_url == DEV_PLATFORM_URL:
        cli_url = None  # 等于全局默认值 = 用户没指定
    return str(cli_url or platform_info.get("url") or fallback)


def _gateway_uri(url: str, device_id: str, session_id: str, connection_id: str) -> str:
    """把平台 HTTP 地址换成 Gateway WebSocket 地址（http→ws / https→wss）。"""

    base = url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    elif not base.startswith(("ws://", "wss://")):
        raise ValueError(f"daemon_platform_url_invalid:{url}")
    return f"{base}/ws/agents/{device_id}?session_id={session_id}&connection_id={connection_id}"


def user_session_state() -> str:
    """当前用户会话状态（尽力而为）。

    Windows 上交互会话一定有 `SESSIONNAME`（Console / RDP-Tcp#N）；服务或计划任务里为空。
    首版只区分"有交互会话 / 未知"：锁屏与注销不做检测，因此**不谎报** locked/logged_out。
    """

    if os.name != "nt":
        return "logged_in" if os.environ.get("DISPLAY") or os.environ.get("SSH_TTY") else "unknown"
    return "logged_in" if (os.environ.get("SESSIONNAME") or "").strip() else "unknown"


def _resolve_daemon_identity(args: argparse.Namespace) -> dict | None:
    """常驻体的项目身份：有授权就用，没有就只连接不领任务（不是错误）。"""

    try:
        return _resolve_worker_identity(args)
    except ValueError as error:
        print(json.dumps({
            "daemon": "no_project_grant",
            "detail": str(error),
            "hint": "平台设备页 → 授权到项目 → 把授权串交给内核（深链或 POST /grant）；未授权期间只连接并上报本机清单。",
        }, ensure_ascii=False))
        return None


def apply_project_grant(args: argparse.Namespace, grant_blob: str) -> dict:
    """应用一个项目授权串：Token 入凭据管理器、身份写 worker.json，返回解析出的身份。

    与 `_resolve_worker_identity(--grant ...)` 是同一套语义（单一来源），
    区别只是"运行期被调用"——桌面端内核拿到 `map://grant` 深链后走这条路径，
    因此不需要为了换项目重启内核。
    """

    return _resolve_worker_identity(argparse.Namespace(**{**vars(args), "grant": grant_blob}))


# 任务循环真正需要的项目能力（授权串缺一项就会在某个上报端点吃 403，
# 表现为"任务干完了但平台永远停在 RUNNING"）。内核启动时据此给出可执行的提醒。
_TASK_LOOP_REQUIRED_CAPABILITIES = (
    "task.claim",
    "task.progress",
    "task.result",
    "run.create",
    "run.complete",
)


def missing_task_loop_capabilities(capabilities) -> list[str]:
    granted = {str(item) for item in (capabilities or ())}
    return [item for item in _TASK_LOOP_REQUIRED_CAPABILITIES if item not in granted]


def _verify_project_grant_scope(args: argparse.Namespace, device_id: str, project_id: str | None, sidecar: SidecarService) -> bool:
    """用设备 Token 查 `GET /api/agent/me`，确认"这个项目授权确实属于本机设备"。

    为什么要查：项目授权串里带 device_id，而"我是谁"由设备 Token 决定。两者不一致时，
    领任务与上报结果**都能成功**（那些端点只看项目 Token），但 Gateway 的过程事件会被判
    `gateway_project_token_identity_mismatch` 静默丢弃——表现为"任务在跑，平台看不到过程"。
    这种半通不通的状态必须在启动时说出来。
    """

    if not project_id:
        return True
    try:
        device_token = resolve_device_token(argparse.Namespace(**{**vars(args), "device_id": device_id}))
        view = request(args.url, "GET", "/api/agent/me", None, {"Authorization": f"Bearer {device_token}"})
    except Exception as error:  # noqa: BLE001 - 校验失败不阻断启动，但要留痕
        sidecar.logs.append(f"[{datetime.now(UTC).isoformat()}] 未能校验项目授权归属（{type(error).__name__}）")
        return True
    grants = view.get("grants") if isinstance(view, dict) else None
    granted_projects = {str(item.get("project_id")) for item in (grants or []) if isinstance(item, dict)}
    if str(project_id) in granted_projects:
        return True
    message = (
        f"项目 {project_id} 的授权不属于本机设备 {device_id}：任务能领能报，但过程事件会被平台拒收。"
        "请在平台「设备与接入」里对这一台设备重新「授权到项目」，再点「把授权交给本机桌面端」。"
    )
    print(json.dumps({"daemon": "grant_device_mismatch", "project_id": project_id, "device_id": device_id}, ensure_ascii=False))
    sidecar.logs.append(f"[{datetime.now(UTC).isoformat()}] {message}")
    return False


def _await_device_credentials(
    args: argparse.Namespace,
    sidecar_state: Any,
    sidecar: SidecarService,
    *,
    preferred_device_id: str = "",
    poll_seconds: float = 1.0,
    timeout_seconds: float | None = None,
) -> tuple[str, str]:
    """等待"已配对且设备 Token 在凭据管理器里"，返回 `(device_id, device_token)`。

    内核会在配对**之前**就被壳拉起（DP-1 的启动顺序），所以这里不能直接报错退出：
    未配对期间只提供契约服务，用户网页点「接入这台电脑」后本函数自然返回，连接随即开始，
    不需要重启内核。`timeout_seconds` 仅给测试与排障用，默认无限等。
    """

    waited = 0.0
    announced = False
    while True:
        device_id = str(sidecar_state.device_id or preferred_device_id or getattr(args, "device_id", "") or "").strip()
        if device_id:
            try:
                token = resolve_device_token(argparse.Namespace(**{**vars(args), "device_id": device_id}))
            except Exception as error:  # noqa: BLE001 - 凭据缺失/后端不可用都要如实展示并继续等
                sidecar_state.update(connection_state="unpaired", last_error=f"device_token_unavailable:{error}")
                sidecar.logs.append(f"[{datetime.now(UTC).isoformat()}] 设备 Token 不可用：{error}")
            else:
                sidecar_state.update(connection_state="starting", last_error=None)
                return device_id, token
        else:
            sidecar_state.update(connection_state="unpaired", last_error=None)
            if not announced:
                print(json.dumps({
                    "daemon": "waiting_for_pairing",
                    "hint": "平台设备页 → 生成配对 → 点「接入这台电脑」或用配对串调用 POST /pair",
                }, ensure_ascii=False))
                announced = True
        if timeout_seconds is not None and waited >= timeout_seconds:
            raise ValueError("daemon_pairing_timeout:未在限定时间内完成配对")
        time.sleep(poll_seconds)
        waited += poll_seconds


def daemon_run(args: argparse.Namespace) -> None:
    """常驻体（DP-2-02）：本地契约服务 + 平台连接监督 + 任务循环，同进程。

    与 `sidecar-run`（只提供契约、不连平台）和 `worker-run`（前台调试、不保持连接）的区别：
    这是交付给桌面端内核的形态——装完就待在那里，平台有任务就干，断线自己重连。

    - 设备 Token 从凭据管理器读取（`--device-token` 可显式覆盖，仅供调试）；
    - **未配对时先只提供契约服务**（壳照常可用），配对成功后自动开始连接，不必重启内核；
    - 项目授权缺失时**仍然连接**（心跳、清单照常上报），只是不领取任务；
    - 暂停/紧急停止来自本地契约（托盘菜单），通过 `paused_provider` 与本地紧急停止标志生效。
    """

    state_dir = Path(args.state_dir).expanduser() if args.state_dir else default_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = Path(args.state_path).expanduser() if args.state_path else state_dir / "agentd.db"
    args.state_path = str(state_path)
    # 平台地址与设备标识：命令行 > 配对时落盘的 platform.json > 本地默认
    platform_info = read_platform_info(state_dir)
    args.url = _resolve_platform_url(args, platform_info, "http://127.0.0.1:8010")
    if not getattr(args, "device_id", None):
        args.device_id = platform_info.get("device_id")
    if not getattr(args, "agent_id", None):
        args.agent_id = platform_info.get("agent_id")
    if not getattr(args, "workspace", None):
        args.workspace = platform_info.get("workspace") or os.getcwd()

    inventory = AgentInventory(ttl_seconds=args.inventory_ttl)
    # 后台预热清单（含 opencode models）：别让第一次心跳等在探测上
    inventory.warm()
    local_state = LocalAgentState(str(state_path))
    sidecar = SidecarService(
        state_dir=state_dir,
        agents_probe=lambda: inventory.entries(),
        agents_rescan=lambda: inventory.entries(refresh=True),
        log_buffer=[],
    )
    sidecar_state = sidecar.state
    sidecar_state.update(
        platform_url=args.url,
        paused=bool(getattr(args, "start_paused", False)),
        connection_state="starting",
    )

    def on_pause(paused: bool) -> None:
        sidecar_state.update(paused=paused)

    def on_emergency_stop(active: bool) -> None:
        # 本地紧急停止是**共享状态**（SQLite），连接监督与任务循环都读它。
        if active:
            local_state.set_emergency_stop("sidecar_emergency_stop")
        else:
            local_state.clear_emergency_stop()

    sidecar.on_pause = on_pause
    sidecar.on_emergency_stop = on_emergency_stop

    # 先把契约服务起起来：没配对时壳也要能读到状态、拿到配对码对应的命令，
    # 所以"没有设备 Token"不能当成启动失败（DP-1 的壳会在配对前就拉起内核）。
    sidecar.start(args.port)
    print(json.dumps({"daemon": "contract_ready", "contract": CONTRACT_VERSION, **sidecar.info()}, ensure_ascii=False))

    identity = _resolve_daemon_identity(args)
    device_id, device_token = _await_device_credentials(
        args,
        sidecar_state,
        sidecar,
        preferred_device_id=str((identity or {}).get("device_id") or ""),
    )

    recorded_device = str(platform_info.get("device_id") or "")
    if recorded_device and recorded_device != device_id:
        # 授权串里的设备与配对时记录的不是同一台：以授权串为准，并把记录改写掉，
        # 否则下次开机又会走回同一个不一致状态（过程事件会被平台拒收）。
        warning = f"项目授权属于 {device_id}，与配对记录的 {recorded_device} 不一致：按授权串的设备运行"
        print(json.dumps({"daemon": "device_identity_adjusted", "runtime_device": device_id, "paired_device": recorded_device}, ensure_ascii=False))
        sidecar.logs.append(f"[{datetime.now(UTC).isoformat()}] {warning}")
        write_platform_info(state_dir, args.url, device_id, (identity or {}).get("agent_id") or args.agent_id)

    _verify_project_grant_scope(args, device_id, (identity or {}).get("project_id"), sidecar)

    session_id = str(getattr(args, "session_id", "") or f"session-{device_id}")
    # 把本次实际使用的平台地址与标识落盘：下次开机内核自己就能连回来
    write_platform_info(state_dir, args.url, device_id, (identity or {}).get("agent_id") or args.agent_id)
    missing = missing_task_loop_capabilities((identity or {}).get("capabilities"))
    if identity and missing:
        # 授权串缺能力时"任务干完了但平台停在 RUNNING"很难排查，这里提前说清楚。
        sidecar.logs.append(
            f"[{datetime.now(UTC).isoformat()}] 授权串缺少能力：{', '.join(missing)}（结果/运行上报会 403）"
        )
        print(json.dumps({"daemon": "grant_capabilities_incomplete", "missing": missing}, ensure_ascii=False))
    client = DurableGatewayClient(
        local_state,
        GatewayIdentity(device_id=device_id, agent_id=(identity or {}).get("agent_id") or f"agent-{device_id}", session_id=session_id, connection_id=f"connection-{uuid4().hex[:12]}"),
    )
    capabilities = tuple((identity or {}).get("capabilities") or ()) or ("task.claim",)
    def runtime_event_sink(event_type: str, payload: dict) -> None:
        """执行期事件 → Gateway 持久化队列（agent.event）。

        走队列而不是直接 HTTP：连不上平台时事件会在本地补传，不会因为一次断网
        就丢掉"这一轮到底做了什么"。终态（run.completed/run.failed）不走这里——
        它会让 Gateway 去完成 Run，而完成 Run 是 HTTP /complete 的职责。
        """

        project = holder["identity"] or {}
        project_id = str(project.get("project_id") or "")
        token = str(project.get("project_token") or "")
        if not project_id or len(token) < 16:
            return
        queue_event = getattr(client, "queue_event", None)
        if not callable(queue_event):
            return
        event = {
            "schema_version": "1.0",
            "event_id": f"event-{uuid4().hex}",
            "worker_id": f"daemon-{client.identity.device_id}",
            "event_type": event_type,
            "sequence": next(event_sequence),
            "project_id": project_id,
            "run_id": payload.get("run_id"),
            "occurred_at": datetime.now(UTC).isoformat(),
            "payload": {key: value for key, value in payload.items() if value is not None},
        }
        queue_event(
            "agent.event",
            {"project_id": project_id, "project_token": token, "event": event},
            idempotency_key=f"exec-event:{client.identity.device_id}:{event['event_id']}",
        )

    event_sequence = iter(range(1, 1_000_000))
    # 执行体槽位：任务循环与对话循环**共用**一把锁 —— 一台机器同时只跑一个执行体进程
    executor_slot = asyncio.Lock()
    task_loop = (
        _build_task_loop(
            identity, args, label="daemon", event_sink=runtime_event_sink, collect_outputs=True, executor_slot=executor_slot
        )
        if identity
        else None
    )
    # 运行期可变的身份/循环（`/grant` 会在不重启内核的前提下替换它们）
    holder: dict[str, Any] = {"identity": identity, "task_loop": task_loop, "capabilities": capabilities}

    def chat_log(message: str) -> None:
        print(message)
        sidecar.logs.append(f"[{datetime.now(UTC).isoformat()}] {message}")

    # 「对话」轮询循环（MY-AGENT）：与任务循环共用执行体槽位；暂停/紧急停止/没有授权时不取活。
    chat_loop: ChatLoop | None = None
    if getattr(args, "chat_turns", True):
        chat_loop = ChatLoop(
            ChatLoopConfig(
                url=args.url,
                workspace=Path(args.workspace).expanduser().resolve(),
                state_path=state_path,
                device_id=device_id,
                idle_seconds=float(getattr(args, "chat_idle_seconds", 3.0)),
                # 对话是**人等着看**的：空转退避不许拉长（默认 30 秒会让"发出去半天没反应"），封顶 5 秒
                max_idle_seconds=5.0,
                turn_timeout_seconds=float(args.task_timeout),
                # M-5c S-1：常驻 serve 通道；0（默认）= 不启用，继续走原来的 `opencode run` CLI 通道
                chat_server_port=int(getattr(args, "chat_server_port", 0) or 0),
            ),
            identity_provider=lambda: holder["identity"] or {},
            enabled_provider=lambda: bool(holder["identity"])
            and not bool(sidecar_state.paused)
            and not local_state.is_emergency_stopped(),
            environment_provider=_executor_environment,
            log=chat_log,
            slot=executor_slot,
        )

    def current_loop_snapshot() -> dict[str, Any]:
        loop = holder["task_loop"]
        return loop.snapshot() if loop is not None and hasattr(loop, "snapshot") else {}

    def heartbeat_provider():
        loop_snapshot = current_loop_snapshot()
        chat_models = inventory.models()  # 带 TTL 缓存：不会每跳心跳都去跑 `opencode models`
        chat_roles = inventory.roles()  # 同上：角色（agent）探测也走缓存，探不到就是空列表
        return AgentHeartbeat(
            device_id=device_id,
            agent_id=client.identity.agent_id,
            session_id=session_id,
            connection_id=client.identity.connection_id,
            agent_version=SIDECAR_VERSION,
            adapter_versions=inventory.adapter_versions(),
            capabilities=list(holder["capabilities"]),
            running_run_ids=[run_id for run_id in (loop_snapshot.get("running_run_ids") or []) if _is_uuid_string(run_id)],
            local_queue_length=int(loop_snapshot.get("local_queue_length") or 0),
            user_session_state=user_session_state(),
            resource_summary={
                "active_processes": 0,
                "pending_gateway_events": len(local_state.pending_events(limit=1000)),
                "executor": _executor_label(inventory),
                # 「我的智能体」的模型下拉读这两项（执行体真探测到的；探不到就是空列表，不编）
                "models": chat_models[0],
                "default_model": chat_models[1],
                # 角色下拉读这一项：`[{name, description}]`，名字以 `opencode agent list` 为准
                "roles": chat_roles,
                "os": platform.system(),
                "cpu_count": os.cpu_count(),
                **(chat_loop.snapshot() if chat_loop is not None else {}),
            },
            sent_at=datetime.now(UTC),
        )

    config = MachineServiceConfig(
        gateway_uri=_gateway_uri(args.url, device_id, session_id, client.identity.connection_id),
        agent_version=SIDECAR_VERSION,
        capabilities=capabilities,
        heartbeat_interval_seconds=args.heartbeat_interval,
        send_poll_interval_seconds=args.send_poll_interval,
        retry_after_seconds=args.retry_after_seconds,
        reconnect_base_seconds=args.reconnect_base,
        reconnect_max_seconds=args.reconnect_max,
        control_poll_interval_seconds=args.control_poll_interval,
        process_stop_timeout_seconds=args.process_stop_timeout,
    )
    service = MachineAgentService(
        client,
        lambda: device_token,
        config,
        heartbeat_provider=heartbeat_provider,
        task_loop=task_loop,
        paused_provider=lambda: bool(sidecar_state.paused),
        gateway_uri_provider=lambda connection_id: _gateway_uri(args.url, device_id, session_id, connection_id),
        connection_id_provider=lambda: f"connection-{uuid4().hex[:12]}",
    )
    loop_holder: dict[str, Any] = {"loop": None}

    def grant_handler(payload: dict) -> dict:
        """契约 `/grant`：应用项目授权串并立刻开始领任务（无需重启内核）。

        授权串是短期凭证：调用方（壳）只把它交给本机回环端点，不落盘到契约状态里。
        """

        granted = apply_project_grant(args, str(payload["grant_blob"]))
        if granted["device_id"] != device_id:
            raise ValueError(f"grant_device_mismatch:{granted['device_id']}!={device_id}")
        # 授权串里的平台地址必须与本内核连接的一致，否则任务会被派到另一个平台地址上。
        requested_url = str(payload.get("platform_url") or "").rstrip("/")
        if requested_url and requested_url != str(args.url).rstrip("/"):
            raise ValueError(f"grant_platform_mismatch:{requested_url}!={args.url}")
        new_loop = _build_task_loop(
            granted, args, label="daemon", event_sink=runtime_event_sink, collect_outputs=True, executor_slot=executor_slot
        )
        holder["identity"] = granted
        holder["task_loop"] = new_loop
        holder["capabilities"] = tuple(granted.get("capabilities") or ()) or ("task.claim",)
        scheduled = False
        loop = loop_holder["loop"]
        if loop is not None and not loop.is_closed():
            asyncio.run_coroutine_threadsafe(service.install_task_loop(new_loop), loop)
            scheduled = True
        return {
            "granted": True,
            "project_id": granted["project_id"],
            "agent_id": granted["agent_id"],
            "device_id": granted["device_id"],
            "capabilities": list(holder["capabilities"]),
            "task_loop": "running" if scheduled else "pending",
            "message": None if scheduled else "内核尚未开始连接，授权已保存，启动后生效",
        }

    sidecar._grant_handler = grant_handler  # noqa: SLF001 - 内核自己装配依赖（同进程）

    print(json.dumps({
        "daemon": "started",
        "contract": CONTRACT_VERSION,
        "device_id": device_id,
        "agent_id": client.identity.agent_id,
        "project_id": (identity or {}).get("project_id"),
        "task_loop": bool(task_loop),
        "gateway_uri": config.gateway_uri,
        "port": sidecar.port,
    }, ensure_ascii=False))

    # 状态镜像：把连接监督/任务循环的实时状态抄进契约快照，供壳与本地状态页读取。
    def mirror() -> None:
        snapshot = service.snapshot()
        loop_snapshot = snapshot.get("task_loop") or {} or current_loop_snapshot()
        sidecar_state.update(
            connection_state=_contract_connection_state(snapshot["status"]),
            reconnect_attempt=snapshot["reconnect_attempt"],
            last_error=snapshot["last_error"],
            device_id=device_id,
            agent_id=client.identity.agent_id,
            project_id=(holder["identity"] or {}).get("project_id"),
            current_task=loop_snapshot.get("current_task"),
            completed_since_start=int(loop_snapshot.get("completed") or 0),
            local_queue_length=int(loop_snapshot.get("local_queue_length") or 0),
            emergency_stop=bool(local_state.is_emergency_stopped()),
        )
        sidecar.logs.append(
            f"[{datetime.now(UTC).isoformat()}] connection={snapshot['status']} "
            f"claimed={loop_snapshot.get('claimed', 0)} completed={loop_snapshot.get('completed', 0)}"
        )

    async def run_daemon() -> str:
        """跑连接监督；紧急停止后**保持契约服务**，等托盘解除再自动恢复。

        紧急停止的语义是"停机"（停领取 + 平台看到掉线），不是"退出内核"：
        内核退出就没法从托盘解除，壳还会当成崩溃反复重启（最多 3 次后彻底放弃）。
        """

        loop_holder["loop"] = asyncio.get_running_loop()
        mirror_task = asyncio.create_task(_mirror_loop(mirror, sidecar, service))
        # 对话循环与连接监督并行跑；契约服务被 /shutdown 关掉时它自己退出（stop_when）
        chat_task = (
            asyncio.create_task(chat_loop.run_forever(stop_when=lambda: not sidecar.running))
            if chat_loop is not None
            else None
        )
        try:
            while True:
                status = await service.run_forever()
                if status != "EMERGENCY_STOPPED":
                    return status
                while local_state.is_emergency_stopped() and sidecar.running:
                    await asyncio.sleep(1.0)
                if not sidecar.running:
                    return status
                service.clear_emergency_stop()
                sidecar.logs.append(f"[{datetime.now(UTC).isoformat()}] 紧急停止已解除，重新连接平台")
        finally:
            mirror_task.cancel()
            if chat_task is not None:
                chat_task.cancel()
            if chat_loop is not None:
                # 常驻 serve 是内核拉起来的子进程：内核退场要把它收掉，别留孤儿进程在机器上
                chat_loop.close()
            await asyncio.gather(mirror_task, *([chat_task] if chat_task is not None else []), return_exceptions=True)

    try:
        status = asyncio.run(run_daemon())
        print(json.dumps({"daemon": "stopped", "status": status, "snapshot": service.snapshot()}, ensure_ascii=False))
    except KeyboardInterrupt:
        print(json.dumps({"daemon": "stopped", "reason": "keyboard_interrupt"}, ensure_ascii=False))
    finally:
        # 契约服务活着等壳来收尸：/shutdown 只停 HTTP，真正的进程退出由这里统一负责。
        sidecar.stop()
        local_state.close()


async def _mirror_loop(
    mirror: Callable[[], None],
    sidecar: SidecarService,
    service: MachineAgentService,
    interval: float = 1.0,
) -> None:
    """周期把实时状态写进契约快照；契约服务被 `/shutdown` 关掉后，常驻体也随之退出。"""

    while sidecar.running:
        try:
            mirror()
        except Exception as error:  # noqa: BLE001 - 镜像失败不能影响常驻体
            sidecar.logs.append(f"mirror failed: {type(error).__name__}:{error}")
        await asyncio.sleep(interval)
    sidecar.logs.append(f"[{datetime.now(UTC).isoformat()}] contract stopped, shutting the daemon down")
    await service.stop()


def _contract_connection_state(status: str) -> str:
    """机器服务状态 → 契约里的 connection.state（壳按它点灯）。"""

    return {
        "CONNECTED": "connected",
        "CONNECTING": "connecting",
        "RECONNECT_WAIT": "reconnecting",
        "DISCONNECTED": "disconnected",
        "STOPPED": "stopped",
        "EMERGENCY_STOPPED": "emergency_stopped",
    }.get(status, status.lower() or "unknown")


def _is_uuid_string(value: object) -> bool:
    try:
        UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return False
    return True


def sidecar_run(args: argparse.Namespace) -> None:
    """只提供本地契约服务的形态（不连平台；DP-1 的壳启动方式，保留给测试与排障）。

    桌面端交付形态是 `daemon-run`（契约 + 连接监督 + 任务循环）。
    """

    state_dir = Path(args.state_dir).expanduser() if args.state_dir else default_state_dir()
    service = SidecarService(state_dir=state_dir)
    if args.port:
        service.port = args.port
    service.start(args.port)
    info = service.info()
    print(json.dumps({"sidecar": "started", "contract": CONTRACT_VERSION, **info}, ensure_ascii=False))
    try:
        # serve_forever 跑在守护线程里，主线程等它停下来（/shutdown 或 Ctrl+C）
        while service._server is not None:  # noqa: SLF001 - 同模块内的生命周期检查
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
        print(json.dumps({"sidecar": "stopped"}, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="Math Agent Platform local agent")
    parser.add_argument("--url", default="http://localhost:8000")
    sub = parser.add_subparsers(dest="command", required=True)
    register_parser = sub.add_parser("register")
    register_parser.add_argument("--agent-id", required=True)
    register_parser.add_argument("--display-name", required=True)
    register_parser.add_argument("--owner", default="member-001")
    register_parser.add_argument("--provider", default="local")
    register_parser.add_argument("--model", default="unspecified")
    register_parser.add_argument("--workspace", default=".")
    register_parser.add_argument("--tools", nargs="*", default=[])
    register_parser.add_argument("--languages", nargs="*", default=["python"])
    # AIP-1a/1c：可选的能力卡与执行体程序包自报（不发就照旧，平台按设备探测值推断）
    register_parser.add_argument(
        "--capability-card",
        action="append",
        default=[],
        help="能力卡：内联 JSON、@文件 或 技能@版本 简写；可重复",
    )
    register_parser.add_argument("--executor-kind", default="", help="执行体种类（codex/cli/…）")
    register_parser.add_argument("--executor-version", default="", help="执行体程序包版本")
    register_parser.set_defaults(func=register)
    heartbeat_parser = sub.add_parser("heartbeat")
    heartbeat_parser.add_argument("--agent-id", required=True)
    heartbeat_parser.add_argument("--interval", type=int, default=15)
    heartbeat_parser.set_defaults(func=heartbeat)
    claim_parser = sub.add_parser("claim", help="claim one executable task and receive a lease")
    claim_parser.add_argument("--agent-id", required=True)
    claim_parser.add_argument("--project-id", required=True)
    claim_parser.add_argument("--project-token", required=True)
    claim_parser.add_argument("--stages", nargs="*", default=[])
    claim_parser.add_argument("--lease-seconds", type=int, default=900)
    claim_parser.add_argument("--key")
    claim_parser.set_defaults(func=claim)
    progress_parser = sub.add_parser("progress", help="send task progress")
    progress_parser.add_argument("--agent-id", required=True)
    progress_parser.add_argument("--task-id", required=True)
    progress_parser.add_argument("--lease-token", required=True)
    progress_parser.add_argument("--project-token", required=True)
    progress_parser.add_argument("--status", choices=["RUNNING", "WAITING_REVIEW", "BLOCKED", "FAILED"], required=True)
    progress_parser.add_argument("--message", default="")
    progress_parser.add_argument("--key")
    progress_parser.set_defaults(func=progress)
    complete_parser = sub.add_parser("complete", help="submit task result and release lease")
    complete_parser.add_argument("--agent-id", required=True)
    complete_parser.add_argument("--task-id", required=True)
    complete_parser.add_argument("--lease-token", required=True)
    complete_parser.add_argument("--project-token", required=True)
    complete_parser.add_argument("--summary", default="")
    complete_parser.add_argument("--artifact-ids", nargs="*", default=[])
    complete_parser.add_argument("--handoff-id")
    complete_parser.add_argument("--failed", action="store_true")
    complete_parser.add_argument("--key")
    complete_parser.set_defaults(func=complete)
    lease_parser = sub.add_parser("lease-heartbeat", help="extend an active task lease")
    lease_parser.add_argument("--agent-id", required=True)
    lease_parser.add_argument("--project-id", required=True)
    lease_parser.add_argument("--lease-token", required=True)
    lease_parser.add_argument("--project-token", required=True)
    lease_parser.add_argument("--extend-seconds", type=int, default=900)
    lease_parser.set_defaults(func=lease_heartbeat)
    device_register_parser = sub.add_parser("device-register", help="register this device with pairing code and Ed25519 proof")
    device_register_parser.add_argument("--pairing-code", required=True)
    device_register_parser.add_argument("--pairing-id", required=True)
    device_register_parser.add_argument("--challenge", required=True)
    device_register_parser.add_argument("--challenge-signature", help="base64url Ed25519 signature; optional with --private-key")
    device_register_parser.add_argument("--agent-id", required=True)
    device_register_parser.add_argument("--device-id", required=True)
    device_register_parser.add_argument("--device-name", required=True)
    device_register_parser.add_argument("--public-key", help="PEM or OpenSSH Ed25519 public key")
    device_register_parser.add_argument("--private-key", help="PEM Ed25519 private key; used only to sign this request")
    device_register_parser.add_argument("--platform", default="windows")
    device_register_parser.add_argument("--agent-version", default="0.1.0")
    device_register_parser.add_argument("--capabilities", nargs="*", default=["task.claim"])
    device_register_parser.set_defaults(func=device_register)
    keygen_parser = sub.add_parser("keygen", help="generate an Ed25519 device identity key pair")
    keygen_parser.add_argument("--directory", default=str(Path.home() / ".math-agent-platform" / "keys"))
    keygen_parser.add_argument("--name", default="device", help="file stem for the key pair")
    keygen_parser.add_argument("--force", action="store_true", help="overwrite an existing key pair")
    keygen_parser.set_defaults(func=keygen)
    worker_parser = sub.add_parser("worker-run", help="run the claim-execute-report task loop")
    worker_parser.add_argument("--grant", help="base64url project grant blob copied from the device wizard page")
    worker_parser.add_argument("--project-id")
    worker_parser.add_argument("--project-token")
    worker_parser.add_argument("--agent-id")
    worker_parser.add_argument("--device-id")
    worker_parser.add_argument("--workspace", default=".")
    worker_parser.add_argument("--stages", nargs="*", default=[])
    worker_parser.add_argument("--lease-seconds", type=int, default=900)
    worker_parser.add_argument("--idle-seconds", type=float, default=5.0)
    worker_parser.add_argument("--max-idle-seconds", type=float, default=30.0)
    worker_parser.add_argument("--task-timeout", type=float, default=300.0)
    worker_parser.add_argument("--executor-command", nargs="*", default=None, help="fallback argv when the task declares no worker_command")
    worker_parser.add_argument("--codex-path", help="codex CLI path; auto-detected when omitted")
    worker_parser.add_argument("--codex-sandbox", choices=["read-only", "workspace-write"], default="read-only",
                               help="sandbox mode for codex tasks (danger-full-access is never allowed)")
    worker_parser.add_argument("--process-stop-timeout", type=float, default=5.0)
    worker_parser.add_argument("--state-path", default=str(Path.home() / ".math-agent-platform" / "agentd.db"))
    worker_parser.add_argument("--skip-credential", action="store_true", help="do not persist the project token (debug only)")
    worker_parser.add_argument(
        "--credential-backend",
        choices=("auto", "windows", "file", "memory"),
        default="auto",
        help="credential backend: auto = Windows Credential Manager on Windows, 0600 file on POSIX (headless)",
    )
    worker_parser.add_argument("--once", action="store_true", help="run a single claim-execute-report round")
    worker_parser.add_argument("--collect-outputs", action="store_true",
                               help="upload workspace changes as artifacts (daemon-run does this by default)")
    worker_parser.set_defaults(func=worker_run)
    sidecar_parser = sub.add_parser("sidecar-run", help="run the local sidecar HTTP contract for the desktop shell")
    sidecar_parser.add_argument("--port", type=int, help="fixed port (default: pick a free one)")
    sidecar_parser.add_argument("--state-dir", help="writable state directory (default: %%LOCALAPPDATA%%\\MathAgentPlatform)")
    sidecar_parser.set_defaults(func=sidecar_run)
    daemon_parser = sub.add_parser("daemon-run", help="resident body: local contract + platform connection supervision + task loop")
    daemon_parser.add_argument(
        "--url",
        default=argparse.SUPPRESS,  # 别用 None 覆盖全局 --url：子命令默认值会遮蔽命令行上写在子命令前面的那个
        help="platform base URL (default: platform.json written at pairing time)",
    )
    daemon_parser.add_argument("--grant", help="base64url project grant blob (first run); afterwards read from worker.json + credential vault")
    daemon_parser.add_argument("--project-id")
    daemon_parser.add_argument("--project-token")
    daemon_parser.add_argument("--agent-id")
    daemon_parser.add_argument("--device-id")
    daemon_parser.add_argument("--device-token", help="debug only; production reads the token from the OS credential store")
    daemon_parser.add_argument("--credential-target")
    daemon_parser.add_argument("--session-id", help="defaults to session-<device_id>")
    daemon_parser.add_argument("--workspace", default=".")
    daemon_parser.add_argument("--stages", nargs="*", default=[])
    daemon_parser.add_argument("--lease-seconds", type=int, default=900)
    daemon_parser.add_argument("--idle-seconds", type=float, default=5.0)
    daemon_parser.add_argument("--max-idle-seconds", type=float, default=60.0)
    daemon_parser.add_argument("--task-timeout", type=float, default=300.0)
    daemon_parser.add_argument("--executor-command", nargs="*", default=None, help="fallback argv when the task declares no worker_command")
    daemon_parser.add_argument("--codex-path", help="codex CLI path; auto-detected when omitted")
    daemon_parser.add_argument("--codex-sandbox", choices=["read-only", "workspace-write"], default="read-only",
                               help="sandbox mode for codex tasks (danger-full-access is never allowed)")
    daemon_parser.add_argument("--inventory-ttl", type=float, default=300.0, help="seconds to cache the local agent inventory")
    daemon_parser.add_argument("--heartbeat-interval", type=float, default=15.0)
    daemon_parser.add_argument("--send-poll-interval", type=float, default=0.25)
    daemon_parser.add_argument("--retry-after-seconds", type=int, default=5)
    daemon_parser.add_argument("--reconnect-base", type=float, default=1.0)
    daemon_parser.add_argument("--reconnect-max", type=float, default=60.0)
    daemon_parser.add_argument("--control-poll-interval", type=float, default=0.25)
    daemon_parser.add_argument("--process-stop-timeout", type=float, default=5.0)
    daemon_parser.add_argument("--port", type=int, help="fixed contract port (default: pick a free one)")
    daemon_parser.add_argument("--state-dir", help="writable state directory (default: %%LOCALAPPDATA%%\\MathAgentPlatform)")
    daemon_parser.add_argument("--state-path", default=None, help="local state DB (default: <state-dir>\\agentd.db)")
    daemon_parser.add_argument("--skip-credential", action="store_true", help="do not persist tokens from --grant (debug only)")
    daemon_parser.add_argument(
        "--credential-backend",
        choices=("auto", "windows", "file", "memory"),
        default="auto",
        help="credential backend: auto = Windows Credential Manager on Windows, 0600 file on POSIX (headless)",
    )
    daemon_parser.add_argument("--start-paused", action="store_true", help="start without claiming tasks")
    daemon_parser.add_argument(
        "--chat-turns",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="also poll 「对话」turns (MY-AGENT); --no-chat-turns disables it",
    )
    daemon_parser.add_argument("--chat-idle-seconds", type=float, default=3.0, help="chat turn poll interval")
    daemon_parser.add_argument(
        "--chat-server-port",
        type=int,
        default=0,
        help="M-5c S-1: reuse one long-lived `opencode serve` on this port for chat turns (0 = disabled, use the CLI channel)",
    )
    daemon_parser.set_defaults(func=daemon_run)
    queue_parser = sub.add_parser("gateway-queue", help="enqueue a durable Gateway event")
    queue_parser.add_argument("--state-path", default=str(Path.home() / ".math-agent-platform" / "agentd.db"))
    queue_parser.add_argument("--device-id", required=True)
    queue_parser.add_argument("--agent-id", required=True)
    queue_parser.add_argument("--session-id", required=True)
    queue_parser.add_argument("--connection-id", required=True)
    queue_parser.add_argument("--message-type", default="agent.event")
    queue_parser.add_argument("--payload", default="{}")
    queue_parser.add_argument("--idempotency-key")
    queue_parser.add_argument("--message-id")
    queue_parser.set_defaults(func=gateway_queue)
    recover_parser = sub.add_parser("gateway-recover", help="recover unacknowledged Gateway events")
    recover_parser.add_argument("--state-path", default=str(Path.home() / ".math-agent-platform" / "agentd.db"))
    recover_parser.set_defaults(func=gateway_recover)
    run_parser = sub.add_parser("gateway-run", help="run one durable Agent Gateway WebSocket session")
    run_parser.add_argument("--uri", required=True, help="example ws://localhost:8000/ws/agents/<device_id>?session_id=...&connection_id=...")
    run_parser.add_argument("--device-token", help="development compatibility path; production should use the OS credential store")
    run_parser.add_argument("--credential-target", help="Windows Credential Manager target; defaults from device ID")
    run_parser.add_argument("--state-path", default=str(Path.home() / ".math-agent-platform" / "agentd.db"))
    run_parser.add_argument("--device-id", required=True)
    run_parser.add_argument("--agent-id", required=True)
    run_parser.add_argument("--session-id", required=True)
    run_parser.add_argument("--connection-id", required=True)
    run_parser.set_defaults(func=gateway_run)
    service_parser = sub.add_parser("service-run", help="run the reconnecting machine Agent service")
    service_parser.add_argument("--uri", required=True, help="Gateway WebSocket URI")
    service_parser.add_argument("--device-token", help="development compatibility path; production should use the OS credential store")
    service_parser.add_argument("--credential-target", help="Windows Credential Manager target; defaults from device ID")
    service_parser.add_argument("--state-path", default=str(Path.home() / ".math-agent-platform" / "agentd.db"))
    service_parser.add_argument("--device-id", required=True)
    service_parser.add_argument("--agent-id", required=True)
    service_parser.add_argument("--session-id", required=True)
    service_parser.add_argument("--connection-id", required=True)
    service_parser.add_argument("--agent-version", default="unknown")
    service_parser.add_argument("--capabilities", nargs="*", default=[])
    service_parser.add_argument("--heartbeat-interval", type=float, default=15.0)
    service_parser.add_argument("--send-poll-interval", type=float, default=0.25)
    service_parser.add_argument("--retry-after-seconds", type=int, default=5)
    service_parser.add_argument("--reconnect-base", type=float, default=1.0)
    service_parser.add_argument("--reconnect-max", type=float, default=60.0)
    service_parser.add_argument("--control-poll-interval", type=float, default=0.25)
    service_parser.add_argument("--process-stop-timeout", type=float, default=5.0)
    service_parser.set_defaults(func=service_run)
    credential_save_parser = sub.add_parser("credential-save", help="save a device token in Windows Credential Manager")
    credential_save_target = credential_save_parser.add_mutually_exclusive_group(required=True)
    credential_save_target.add_argument("--target", help="Credential Manager target")
    credential_save_target.add_argument("--device-id", help="derive the default target from device ID")
    credential_save_parser.add_argument("--token-stdin", action="store_true", help="read the token from stdin without echo")
    credential_save_parser.add_argument("--state-path", default=None, help="state DB path; the 0600 credential directory sits next to it")
    credential_save_parser.add_argument(
        "--credential-backend",
        choices=("auto", "windows", "file", "memory"),
        default="auto",
        help="credential backend: auto = Windows Credential Manager on Windows, 0600 file on POSIX (headless)",
    )
    credential_save_parser.set_defaults(func=credential_save)
    credential_delete_parser = sub.add_parser("credential-delete", help="delete a device token from Windows Credential Manager")
    credential_delete_target = credential_delete_parser.add_mutually_exclusive_group(required=True)
    credential_delete_target.add_argument("--target", help="Credential Manager target")
    credential_delete_target.add_argument("--device-id", help="derive the default target from device ID")
    credential_delete_parser.set_defaults(func=credential_delete)
    credential_delete_parser.add_argument("--state-path", default=None, help="state DB path; the 0600 credential directory sits next to it")
    credential_delete_parser.add_argument(
        "--credential-backend",
        choices=("auto", "windows", "file", "memory"),
        default="auto",
        help="credential backend: auto = Windows Credential Manager on Windows, 0600 file on POSIX (headless)",
    )
    emergency_parser = sub.add_parser("service-emergency-stop", help="set the shared local emergency-stop flag")
    emergency_parser.add_argument("--state-path", default=str(Path.home() / ".math-agent-platform" / "agentd.db"))
    emergency_parser.add_argument("--reason", default="local_emergency_stop")
    emergency_parser.set_defaults(func=service_emergency_stop)
    clear_emergency_parser = sub.add_parser("service-clear-emergency-stop", help="clear the shared local emergency-stop flag")
    clear_emergency_parser.add_argument("--state-path", default=str(Path.home() / ".math-agent-platform" / "agentd.db"))
    clear_emergency_parser.set_defaults(func=service_clear_emergency_stop)
    conpty_parser = sub.add_parser("conpty-capability", help="show real Windows ConPTY capability")
    conpty_parser.set_defaults(func=conpty_capability_command)
    cli_capability_parser = sub.add_parser("cli-capability", help="probe a supported external CLI adapter")
    cli_capability_parser.add_argument("--adapter", choices=["codex", "claude"], required=True)
    cli_capability_parser.add_argument("--executable", help="optional executable path or name")
    cli_capability_parser.add_argument("--timeout", type=float, default=5.0)
    cli_capability_parser.set_defaults(func=cli_capability)
    service_install_parser = sub.add_parser("service-install", help="install or update the Windows Machine Agent service")
    service_install_parser.add_argument("--service-name", default="MathAgentMachineService")
    service_install_parser.add_argument("--display-name", default="Math Agent Machine Service")
    service_install_parser.add_argument("--description", default="Math Agent Platform machine service")
    service_install_parser.add_argument("--command", nargs="+", required=True, help="service executable and arguments")
    service_install_parser.add_argument("--start-type", choices=["auto", "demand", "disabled"], default="auto")
    service_install_parser.add_argument("--service-account", default="LocalSystem")
    service_install_parser.add_argument("--dependencies", nargs="*", default=[])
    service_install_parser.add_argument("--restart-delays", nargs="+", type=int, default=[60, 300, 900])
    service_install_parser.add_argument("--reset-period", type=int, default=86400)
    service_install_parser.set_defaults(func=service_install)
    service_control_parser = sub.add_parser("service-control", help="control an installed Windows Machine Agent service")
    service_control_parser.add_argument("service_action", choices=["start", "stop", "status", "uninstall"])
    service_control_parser.add_argument("--service-name", default="MathAgentMachineService")
    service_control_parser.add_argument("--timeout", type=float, default=10.0)
    service_control_parser.set_defaults(func=service_control)
    service_host_parser = sub.add_parser("service-host", help="run the Session 0 Windows Service host")
    service_host_parser.add_argument("--service-name", default="MathAgentMachineService")
    service_host_parser.add_argument("--agentd-command", nargs="+", required=True, help="agentd launcher prefix, e.g. python.exe apps/agent/agentd.py")
    service_host_parser.add_argument("--workspace", required=True)
    service_host_parser.add_argument("--state-path", default=str(Path.home() / ".math-agent-platform" / "session-worker.db"))
    service_host_parser.add_argument("--allowed-peer-id", required=True)
    service_host_parser.add_argument("--allowed-sids", nargs="+", required=True)
    service_host_parser.add_argument("--adapter-id", default="python-user-session")
    service_host_parser.add_argument("--conpty-adapter-id")
    service_host_parser.add_argument("--python-executable")
    service_host_parser.add_argument("--reconcile-interval", type=float, default=5.0)
    service_host_parser.set_defaults(func=service_host)
    session_worker_parser = sub.add_parser("session-worker-run", help="run a Windows User Session Worker Named Pipe endpoint")
    session_worker_parser.add_argument("--pipe-name", required=True)
    session_worker_parser.add_argument("--worker-id", required=True)
    session_worker_parser.add_argument("--user-session-id", required=True)
    session_worker_parser.add_argument("--user-sid")
    session_worker_parser.add_argument("--allowed-peer-id", required=True)
    session_worker_parser.add_argument("--allowed-peer-kind", choices=["machine_service", "desktop_ui"], default="machine_service")
    session_worker_parser.add_argument("--allowed-sids", nargs="+", required=True)
    session_worker_parser.add_argument("--allowed-session-ids", nargs="+", type=int, default=[0])
    session_worker_parser.add_argument("--workspace", required=True)
    session_worker_parser.add_argument(
        "--url",
        default=argparse.SUPPRESS,  # 同上：不要让子命令默认值遮蔽全局 --url
    )
    session_worker_parser.add_argument("--agent-id")
    session_worker_parser.add_argument("--project-token")
    session_worker_parser.add_argument("--state-path", default=str(Path.home() / ".math-agent-platform" / "session-worker.db"))
    session_worker_parser.add_argument("--python-executable", default=sys.executable)
    session_worker_parser.add_argument("--adapter-id", default="python-user-session")
    session_worker_parser.add_argument("--conpty-adapter-id")
    session_worker_parser.add_argument("--agent-version", default=platform.python_version())
    session_worker_parser.add_argument("--capabilities", nargs="*", default=["session.run", "terminal.input", "terminal.resize"])
    session_worker_parser.add_argument("--allowed-environment-keys", nargs="*", default=[])
    session_worker_parser.add_argument("--process-stop-timeout", type=float, default=5.0)
    session_worker_parser.add_argument("--container-runtime", choices=["docker", "podman"], help="enable a container execution backend")
    session_worker_parser.add_argument("--access-observer", choices=["none", "windows-etw"], default="none", help="enable an explicit local access observer")
    session_worker_parser.add_argument("--etw-output-directory", help="optional directory for retained ETL/CSV diagnostics")
    session_worker_parser.set_defaults(func=session_worker_run)
    args = parser.parse_args()
    try:
        args.func(args)
    except (ValueError, KeyError) as error:
        # 稳定错误（含服务端返回的 http_<code>:<detail>）打一行干净信息即可，
        # 不打 traceback：接入失败时用户需要看到的是原因，不是调用栈。
        sys.stderr.write(f"错误：{error}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
