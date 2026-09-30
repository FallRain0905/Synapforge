from __future__ import annotations

import asyncio
from urllib.parse import quote
import hashlib
import logging
import json
import os
import tempfile
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Generator, TypeVar
from uuid import UUID

from fastapi import Body, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from .contracts import (
    AgentChatConversation,
    AgentChatConversationCreate,
    AgentChatConversationUpdate,
    AgentChatMessageCreate,
    AgentConversationPromote,
    AgentConversationPromoteResult,
    WorkflowRunStart,
    WorkflowUpsert,
    AgentChatTurn,
    AgentChatTurnApproval,
    AgentChatTurnApprovalDecision,
    AgentChatTurnApprovalRequest,
    AgentChatTurnApprovalState,
    AgentChatTurnClaim,
    AgentChatTurnClaimResult,
    AgentChatTurnComplete,
    AgentChatTurnEvent,
    AgentChatTurnEventReport,
    AgentConnection,
    MyAgentEndpoint,
    Agent,
    AgentProjectGrant,
    AgentTaskClaimRequest,
    AgentRegister,
    Artifact,
    ArtifactCreate,
    BoundaryGateRequest,
    CompetitionPackApplyRequest,
    DeliveryBundleRequest,
    DeliveryChecklistRequest,
    DeliveryCompileRequest,
    DriveDirectoryCreate,
    DriveExtractionCreate,
    DriveImportRequest,
    DriveNodeCopy,
    DriveNodeMove,
    DriveNodePatch,
    AiSettingsUpdate,
    CompetitionPackReviewRequest,
    ConversationCreate,
    KbCreate,
    KbDocumentCreate,
    KbIndexRequest,
    KbQueryRequest,
    KbShareCreate,
    MessageCreate,
    ConvertEnqueueRequest,
    ConvertToKbRequest,
    Dashboard,
    DocumentCommentRequest,
    DocumentMergeRequest,
    DocumentRelationRequest,
    DocumentReviseRequest,
    DocumentSnapshotRequest,
    DocumentSubmitRequest,
    HandoffImportReport,
    Device,
    DeviceCredential,
    DevicePairing,
    DevicePairingCreate,
    DeviceProjectCredential,
    DeviceProjectGrant,
    DeviceProjectGrantCreate,
    DeviceRegisterRequest,
    DeviceTokenRotateRequest,
    DeviceRevokeRequest,
    AgentSelfView,
    ArtifactConsumer,
    ArtifactDetail,
    ArtifactLineageEntry,
    DocumentDraft,
    DocumentDraftUpdate,
    TaskUpdateRequest,
    Evidence,
    EvidenceCreate,
    Event,
    Gate,
    GitFileIndex,
    GitRepository,
    GitRepositoryCreate,
    HandoffAcceptRequest,
    HandoffDecisionRequest,
    Handoff,
    HandoffCreate,
    HumanMember,
    HumanMemberCreate,
    ImportRequest,
    ImportSummary,
    Invitation,
    InvitationCreate,
    # 账号系统（AUTH-1）
    AccountUpdateRequest,
    AccountView,
    AuthSession,
    LoginRequest,
    CapabilityAgent,
    CapabilityCatalog,
    CapabilityPackage,
    MemberWorkload,
    MyTasks,
    MyAttention,
    ProjectDeliverables,
    ProjectMessage,
    ProjectMessageCreate,
    TaskBulkAssign,
    TaskBulkAssignResult,
    ProjectTeamUpdate,
    ProjectWorkspaceOverview,
    TeamThroughput,
    ThroughputDay,
    ThroughputMember,
    UnmetCapabilityTask,
    ProjectMemberRemoval,
    ProjectMemberUpdate,
    ProjectMemberView,
    TeamMemberChange,
    TeamMemberUpsert,
    TaskBoardItem,
    PasswordChangeRequest,
    PasswordResetResult,
    RegisterRequest,
    Organization,
    OrganizationCreate,
    Project,
    ProjectCreate,
    Review,
    ReviewCreate,
    ReviewCenter,
    RiskDecisionRequest,
    RiskRegistryEntry,
    Run,
    RunComplete,
    RunCreate,
    Session,
    SessionCreate,
    Team,
    TeamCreate,
    Task,
    TaskAssignment,
    TaskClaimRequest,
    TaskLease,
    TaskLeaseHeartbeat,
    TaskProgressRequest,
    TaskResult,
    TaskResultSubmit,
    TaskCreate,
    TaskCandidates,
    TaskBudgetState,
    TaskDetail,
    TaskEvidenceGap,
    AttentionItem,
    TaskFlags,
    TaskStatus,
    WorkspaceOperationClaim,
    WorkspaceOperationComplete,
    WorkspaceOperationCreate,
    WorkspaceOperationProgress,
    WorkspaceRegisterRequest,
    WorkspaceTransferCreate,
    FileAccessGrantCreate,
    FileAccessGrantDecision,
    FileLeaseExchange,
    FileTransferDriveToWorkspace,
    FileTransferSave,
    FileTransferWorkspaceToDrive,
)
from .store import Store
from .accounts import LOGIN_THROTTLE
from .object_store import create_object_store
from .path_privacy import public_agent, public_artifact, public_run
from . import archive, drive, drive_grants, file_transfers, llm_channels, workspace_files
from .cumcm_importer import CumcmHandoffImporter, CumcmImporter
from .gateway import GatewayProtocolError, GatewayService
from . import agent_chat, ai_chat, ai_probe, boundary_gate, collaboration, convert_queue, delivery, document_api, kb_gateway, knowledge_base, observability, pack_api, personal_drive, project_team, stream_bridge, workflow_service
from .contracts import LlmChannelCreate, LlmChannelUpdate, LlmQuotaSet
from packages.competition_packs import CompetitionPackError


BASE_DIR = Path(__file__).resolve().parents[1]
store = Store(BASE_DIR / "data" / "platform.db", object_store=create_object_store(BASE_DIR / "data" / "objects"))
# 「我的智能体」对话表（agent_conversations / agent_turns / agent_turn_events）：启动时建好
agent_chat.ensure_schema(store)
# 个人云盘（FM-1）：节点树 + 引用 + 对象清理队列 + 审计；老表数据一次性回填（幂等）
drive.ensure_schema(store)
DRIVE_BACKFILL = drive.backfill_legacy(store)
# LLM 渠道与全员免费额度（管理员「渠道」页 + 成员代理端点）：启动建表（幂等）
llm_channels.ensure_schema(store)
importer = CumcmImporter(store)
handoff_importer = CumcmHandoffImporter(store)
gateway = GatewayService(store)

AGENT_PROJECT_TOKEN_HEADER = "X-Project-Capability-Token"
_INVALID_AGENT_TOKEN_ERRORS = {
    "device_project_token_invalid",
    "device_project_token_revoked",
    "device_project_token_expired",
}


class ConnectionManager:
    def __init__(self) -> None:
        self.connections: dict[str, set[WebSocket]] = {}

    async def connect(self, project_id: UUID, websocket: WebSocket) -> None:
        await websocket.accept()
        self.connections.setdefault(str(project_id), set()).add(websocket)

    def disconnect(self, project_id: UUID, websocket: WebSocket) -> None:
        self.connections.get(str(project_id), set()).discard(websocket)

    async def broadcast(self, event: Event) -> None:
        await self.relay(str(event.project_id), event.model_dump(mode="json"))

    async def relay(self, project_id: str, message: dict, *, exclude: Any = None) -> int:
        """把消息转发给同项目的其他连接（协作帧不回声给发送者）。"""

        delivered = 0
        for websocket in list(self.connections.get(str(project_id), set())):
            if websocket is exclude:
                continue
            try:
                await websocket.send_json(message)
                delivered += 1
            except Exception:
                self.connections.get(str(project_id), set()).discard(websocket)
        return delivered


manager = ConnectionManager()
T = TypeVar("T")


# 维护扫描周期：Demo 1.0 期间固定 10s（计划 §3 D3）。
MAINTENANCE_INTERVAL_SECONDS = 10


def _maintenance_pass() -> list[Event]:
    """一次维护扫描（同步，跑在工作线程里）：心跳超时置离线 + 租约回收。

    这里只做数据库工作，不广播：`broadcast_event` 依赖 `asyncio.get_running_loop()`，
    在工作线程里调用会抛 RuntimeError 并被静默吞掉（界面于是不会自动更新）。
    广播统一由 `_maintenance_tick` 在事件循环线程里完成。
    """

    events: list[Event] = []
    events.extend(store.expire_stale_agents())
    events.extend(store.recycle_expired_leases())
    # W-3：auto 模式的项目由调度器推进（每个 tick 每项目最多派一条；不用事件循环，纯库操作）
    for project_id in store.list_project_ids():
        try:
            events.extend(store.auto_dispatch_tick(project_id, limit=1))
        except Exception:  # 单个项目推进失败不能让整轮扫描挂掉
            logging.getLogger("app.workspace").exception("auto dispatch failed for %s", project_id)
    return events


async def _maintenance_tick() -> list[Event]:
    """跑一次扫描，并把新事件推给同项目的 WebSocket 订阅者。

    这是 `broadcast_event` 的真实调用方（此前是死代码）：界面因此不需要手动刷新
    就能看到 Agent 掉线与任务回收。同一拍里也兜底推进工作区聊天桥：任何漏了即时
    桥接/推送的路径（工作线程、维护扫描自身产生的事件）都在这补齐。
    """

    events = await asyncio.to_thread(_maintenance_pass)
    await asyncio.to_thread(store.catch_up_project_messages)
    for event in events:
        broadcast_event(event)
    for project_id in await asyncio.to_thread(store.list_project_ids):
        publish_project_chat(project_id)
    return events


async def _maintenance_loop() -> None:
    while True:
        await asyncio.sleep(MAINTENANCE_INTERVAL_SECONDS)
        try:
            await _maintenance_tick()
        except asyncio.CancelledError:
            raise
        except Exception:  # 单次扫描失败不能让维护循环退出
            logging.getLogger("app.maintenance").exception("maintenance pass failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 启动时：补历史（老项目的事件一次性进聊天流）+ 对齐推送基线（历史不回放给 WS）
    global _MAIN_LOOP
    _MAIN_LOOP = asyncio.get_running_loop()
    await asyncio.to_thread(store.catch_up_project_messages)
    prime_chat_broadcast()
    maintenance = asyncio.create_task(_maintenance_loop())
    try:
        yield
    finally:
        maintenance.cancel()
        with suppress(asyncio.CancelledError):
            await maintenance


def _cors_origins() -> list[str]:
    """允许的前端来源。

    默认是本机 Demo 的两个地址（`localhost:3000` / `127.0.0.1:3000`）；
    自托管部署时用 `PLATFORM_CORS_ORIGINS` 覆盖（逗号分隔，例如
    `http://10.0.0.5:3000,https://map.example.com`），否则浏览器会因为
    "工作台来源不在白名单"而看不到任何数据（典型症状是页面显示 Failed to fetch）。
    """

    raw = os.getenv("PLATFORM_CORS_ORIGINS")
    if raw is None or not raw.strip():
        return ["http://localhost:3000", "http://127.0.0.1:3000"]
    origins = [item.strip().rstrip("/") for item in raw.split(",")]
    return [item for item in origins if item] or ["http://localhost:3000", "http://127.0.0.1:3000"]


app = FastAPI(title="Math Agent Platform API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=_cors_origins(), allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


def _auth_mode() -> str:
    return os.getenv("PLATFORM_AUTH_MODE", "development").strip().lower()


def _request_member_id(request: Request) -> str:
    authorization = request.headers.get("Authorization", "")
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(status_code=401, detail="invalid_authorization_header")
        try:
            return store.resolve_session(token).id
        except PermissionError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
    if _auth_mode() in {"required", "production"}:
        raise HTTPException(status_code=401, detail="authentication_required")
    return "member-001"


def _request_member(request: Request) -> HumanMember:
    """当前会话成员（带 organization_id）；开发模式回落到内置 member-001。"""

    authorization = request.headers.get("Authorization", "")
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(status_code=401, detail="invalid_authorization_header")
        try:
            return store.resolve_session(token)
        except PermissionError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
    if _auth_mode() in {"required", "production"}:
        raise HTTPException(status_code=401, detail="authentication_required")
    return store.get_member("member-001")


def _require_admin_member(request: Request) -> HumanMember:
    """管理员动作的成员对象（organization_id 用于渠道/额度的组织隔离）。"""

    member = _request_member(request)
    store.require_admin(member.id)
    return member


def _project_id_from_path(request: Request) -> UUID | None:
    parts = request.url.path.strip("/").split("/")
    try:
        if len(parts) >= 3 and parts[:2] == ["api", "projects"]:
            if parts[2] == "restore":
                return None
            return UUID(parts[2])
        if len(parts) >= 3 and parts[:2] in (["api", "tasks"], ["api", "handoffs"], ["api", "artifacts"], ["api", "runs"]):
            resource_id = UUID(parts[2])
            if parts[1] == "tasks":
                return store.get_task(resource_id).project_id
            if parts[1] == "handoffs":
                return store.get_handoff(resource_id).project_id
            if parts[1] == "artifacts":
                return store.get_artifact(resource_id).project_id
            return store.get_run(resource_id).project_id
    except (ValueError, KeyError):
        return None
    return None


def _project_permission(request: Request) -> str:
    path = request.url.path
    if path.endswith("/export"):
        return "project.export"
    # 成员目录是"读"权限：派单选择器（contributor 建任务时要用）依赖它；
    # 增删改成员与授权管理才是 admin。
    if "/members" in path:
        return "project.admin" if request.method != "GET" else "project.view"
    # 项目聊天：发言不是"改项目"——reviewer 也要能说话，observer 只读。
    # 限定 /api/projects/ 前缀：AI 会话的 /api/ai/conversations/{id}/messages 也以 /messages 结尾，
    # 不能共用这套语义（那条路径本就没有项目 id，但判定要写准）。
    if path.startswith("/api/projects/") and path.endswith("/messages"):
        return "project.chat" if request.method == "POST" else "project.view"
    if "/agent-grants" in path or "/device-grants" in path:
        return "project.admin"
    if request.method == "GET":
        return "project.view"
    if "/reviews" in path and request.method == "POST":
        return "review.submit"
    return "project.write"


@app.middleware("http")
async def enforce_project_authorization(request: Request, call_next: Any) -> Any:
    path = request.url.path
    if request.method == "OPTIONS" or not path.startswith("/api/") or path.startswith("/api/auth/"):
        return await call_next(request)
    # Agent-only routes authenticate with a project capability token in the
    # route handler; they do not carry a human session bearer.
    if path.startswith("/api/agent/"):
        return await call_next(request)
    # 同理：任何带项目能力令牌的请求都交给 handler 按能力校验。
    # 少了这条，Agent 的 HTTP 领取链路（/api/agents/{id}/tasks/claim 等，worker-run 走的就是它）
    # 会在 PLATFORM_AUTH_MODE=required 下被当成"缺少人类会话"而 401——每个相关 handler
    # 都调用了 _require_agent_capability，所以放行不等于开洞。
    if request.headers.get("X-Project-Capability-Token"):
        return await call_next(request)
    # 接入流程的两个端点本来就不带人类令牌（凭据是配对码 + 私钥签名 / 幂等登记），
    # 强制鉴权（PLATFORM_AUTH_MODE=required）下也必须放行，否则新设备接不进来。
    if path in {"/api/devices/register", "/api/agents/register"}:
        return await call_next(request)
    try:
        member_id = _request_member_id(request)
        project_id = _project_id_from_path(request)
        if project_id:
            store.authorize_member(project_id, member_id, _project_permission(request))
        return await call_next(request)
    except HTTPException as error:
        return JSONResponse(status_code=error.status_code, content={"detail": error.detail})
    except PermissionError as error:
        return JSONResponse(status_code=403, content={"detail": str(error)})


def _enforce_quota(project_id: UUID, resource: str) -> None:
    """写路径配额判定：超限返回 429，错误码稳定可断言。"""

    try:
        observability.enforce_quota(store, project_id, resource=resource)
    except observability.QuotaExceeded as error:
        raise HTTPException(status_code=429, detail=str(error)) from error


def content_disposition(filename: str) -> str:
    """下载头：中文等非 ASCII 文件名必须走 RFC 5987 的 filename*，并给一个 ASCII 兜底。

    Starlette 把响应头按 latin-1 编码——直接塞中文名会抛 UnicodeEncodeError 变成 500
    （成果物名是中文时"下载/就地查看"全挂，这个坑就是这么踩出来的）。
    """

    safe_name = (filename or "artifact").replace(chr(13), " ").replace(chr(10), " ").strip() or "artifact"
    ascii_fallback = safe_name.encode("ascii", "ignore").decode("ascii").strip() or "artifact"
    ascii_fallback = ascii_fallback.replace('"', "")
    if ascii_fallback.startswith("."):
        # 纯中文名去掉非 ASCII 后只剩扩展名，兜底补个 artifact（浏览器走 filename*，这是给 curl 之类看的）
        ascii_fallback = f"artifact{ascii_fallback}"
    encoded = quote(safe_name, safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


def project_or_404(project_id: UUID) -> Project:
    try:
        return store.get_project(project_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="project_not_found") from error


def broadcast_event(event: Event) -> None:
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(manager.broadcast(event))
    except RuntimeError:
        pass


# ---- 项目工作区聊天推送（W-1）----
# 说明：绝大多数路由是同步 def，跑在 FastAPI 的线程池里——那里没有运行中的事件循环，
# 所以不能用 get_running_loop。启动时抓住主循环，用 run_coroutine_threadsafe 调度。
_MAIN_LOOP: asyncio.AbstractEventLoop | None = None

# 每个项目"已经推送给 WS 的最大消息序号"。单进程部署（Store 本身也是单进程串行），
# 进程内字典足够；多实例时 WS 推送本就跨实例不共享，这里不额外引入复杂度。
_CHAT_PUSHED: dict[str, int] = {}


def prime_chat_broadcast() -> None:
    """对齐推送基线：当前已有的消息视为"已经看到过"，不回放。"""

    for project in store.list_projects():
        _CHAT_PUSHED[str(project.id)] = store.max_project_message_seq(project.id)


def publish_project_chat(project_id: UUID) -> int:
    """把项目新产生的聊天消息推给 WS 订阅者，返回推送条数。

    调用点：人类发言之后（立即回显给其他成员）、Agent 执行链路的关键端点之后
    （领任务/进度/结果/运行），以及维护循环每拍兜底。首次见到某个项目时只对齐
    基线——页面加载时的历史由 GET /messages 负责，不靠 WS 回放。
    """

    key = str(project_id)
    if key not in _CHAT_PUSHED:
        _CHAT_PUSHED[key] = store.max_project_message_seq(project_id)
        return 0
    delivered = 0
    while True:
        batch = store.list_project_messages(project_id, after_seq=_CHAT_PUSHED[key], limit=100)
        if not batch:
            break
        for message in batch:
            _CHAT_PUSHED[key] = message.seq
            frame = {"type": "project.message", "message": message.model_dump(mode="json")}
            loop = _MAIN_LOOP
            if loop is None or loop.is_closed():
                continue
            try:
                asyncio.run_coroutine_threadsafe(manager.relay(key, frame), loop)
                delivered += 1
            except RuntimeError:
                continue
        if len(batch) < 100:
            break
    return delivered


def publish_chat_after(project_id: UUID | None) -> None:
    """端点收尾调用：swallow 掉推送失败——数据已经落库，推送只是"更快看到"。"""

    if project_id is None:
        return
    try:
        publish_project_chat(project_id)
    except Exception:  # pragma: no cover - 推送失败不影响业务结果
        logging.getLogger("app.workspace").exception("chat publish failed")


def require_idempotency_key(value: str | None) -> str:
    if not isinstance(value, str) or len(value) < 8 or len(value) > 160:
        raise HTTPException(status_code=400, detail="idempotency_key_required")
    return value


def _require_agent_capability(
    request: Request,
    project_id: UUID,
    capability: str,
    agent_id: str,
) -> DeviceProjectGrant:
    """Authenticate a machine request against one project-scoped grant.

    Human sessions and device transport tokens intentionally do not satisfy
    this check. A task or Run mutation must carry the narrower capability
    token so an authenticated device cannot cross project boundaries.
    """
    project_token = request.headers.get(AGENT_PROJECT_TOKEN_HEADER)
    if not project_token:
        raise HTTPException(status_code=401, detail="agent_project_token_required")
    try:
        grant = store.resolve_device_project_token(project_token, project_id, capability)
    except PermissionError as error:
        detail = str(error)
        status_code = 401 if detail in _INVALID_AGENT_TOKEN_ERRORS else 403
        raise HTTPException(status_code=status_code, detail=detail) from error
    if grant.agent_id != agent_id:
        raise HTTPException(status_code=403, detail="agent_project_agent_mismatch")
    return grant


def _require_agent_header(request: Request) -> str:
    agent_id = request.headers.get("X-Agent-Id", "")
    if len(agent_id) < 2 or len(agent_id) > 80:
        raise HTTPException(status_code=400, detail="agent_id_required")
    return agent_id


def _require_device_token(request: Request) -> Device:
    """设备 Token（`Authorization: Bearer dvc_...`）认证，与 Gateway 同一套凭据。

    人类会话与项目能力 Token 都不能替代它：这是"设备看自己"的路径。
    """

    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="agent_device_token_required")
    try:
        return store.resolve_device_token(token)
    except PermissionError as error:
        detail = str(error)
        status_code = 401 if detail == "device_token_invalid" else 403
        raise HTTPException(status_code=status_code, detail=detail) from error


def _positive_env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def agent_runtime_policy() -> dict[str, Any]:
    """B3：平台下发给设备的运行参数。

    这些是**平台默认值**，可用环境变量覆盖（`MAP_AGENT_HEARTBEAT_SECONDS` 等）；
    首版不做按设备个性化配置，避免出现"平台里配了但设备没读"的假象。
    """

    sandboxes = [item.strip() for item in (os.getenv("MAP_AGENT_ALLOWED_SANDBOXES") or "read-only,workspace-write").split(",")]
    return {
        "heartbeat_interval_seconds": _positive_env_int("MAP_AGENT_HEARTBEAT_SECONDS", 15),
        "executor": (os.getenv("MAP_AGENT_EXECUTOR") or "codex").strip(),
        "allowed_sandboxes": [item for item in sandboxes if item] or ["read-only"],
        "max_concurrent_tasks": _positive_env_int("MAP_AGENT_MAX_CONCURRENT_TASKS", 1),
        "lease_seconds": _positive_env_int("MAP_AGENT_LEASE_SECONDS", 900),
    }


def request_hash(payload: Any) -> str:
    if isinstance(payload, bytes):
        encoded = payload
    else:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _upload_limit(environment_name: str, default_bytes: int) -> int:
    raw = os.getenv(environment_name)
    if raw is None:
        return default_bytes
    try:
        value = int(raw)
    except ValueError as error:
        raise HTTPException(status_code=500, detail=f"invalid_{environment_name.lower()}") from error
    if value <= 0:
        raise HTTPException(status_code=500, detail=f"invalid_{environment_name.lower()}")
    return value


def read_upload_limited(file: UploadFile, *, environment_name: str, default_bytes: int, error_code: str) -> bytes:
    limit = _upload_limit(environment_name, default_bytes)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = file.file.read(min(1024 * 1024, limit - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail=error_code)
        chunks.append(chunk)
    return b"".join(chunks)


def idempotent_model_response(
    key: str,
    operation: str,
    fingerprint: str,
    model_type: type[T],
    action: Callable[[], T],
) -> T:
    previous = store.get_idempotent_response(key, operation, fingerprint)
    if previous is not None:
        return model_type.model_validate(previous)
    result = action()
    payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
    store.save_idempotent_response(key, operation, payload, fingerprint)
    return result


def idempotent_json_response(
    key: str,
    operation: str,
    fingerprint: str,
    action: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    previous = store.get_idempotent_response(key, operation, fingerprint)
    if previous is not None:
        return dict(previous)
    result = action()
    store.save_idempotent_response(key, operation, result, fingerprint)
    return result


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "math-agent-platform-api", "mode": _auth_mode()}


@app.get("/api/projects", response_model=list[Project])
def list_projects(request: Request) -> list[Project]:
    return store.list_projects_for_member(_request_member_id(request))


@app.post("/api/projects", response_model=Project, status_code=201)
def create_project(data: ProjectCreate, request: Request) -> Project:
    try:
        member_id = _request_member_id(request)
        return store.create_project(data.model_copy(update={"created_by": member_id}))
    except Exception as error:
        raise task_protocol_error(error) from error


# ---- 组织 / 团队 / 成员目录 ----
# 这一组端点此前被中间件白名单豁免且 handler 不校验身份 → 公网匿名可建组织/团队/成员
# （还能用 POST /api/members 抢注邮箱，阻断真人注册）。现在：读取需要会话，写入需要管理员。


@app.get("/api/organizations", response_model=list[Organization])
def list_organizations(request: Request) -> list[Organization]:
    _request_member_id(request)
    return store.list_organizations()


@app.post("/api/organizations", response_model=Organization, status_code=201)
def create_organization(data: OrganizationCreate, request: Request) -> Organization:
    try:
        _require_admin(request)
        return store.create_organization(data)
    except Exception as error:
        raise account_error(error) from error


@app.get("/api/team/capabilities", response_model=CapabilityCatalog)
def capability_catalog(request: Request) -> CapabilityCatalog:
    """能力目录：谁能跑什么（技能卡 + 授权范围 + 包/实例身份）、哪些任务没人能跑、差一点的是哪几台。"""

    try:
        member = store.resolve_session(_bearer_token(request))
        payload = store.capability_catalog(member.organization_id)
        return CapabilityCatalog(
            agents=[CapabilityAgent(**item) for item in payload["agents"]],
            packages=[CapabilityPackage(**item) for item in payload.get("packages", [])],
            unmet_tasks=[UnmetCapabilityTask(**item) for item in payload["unmet_tasks"]],
        )
    except Exception as error:
        raise account_error(error) from error


@app.get("/api/team/throughput", response_model=TeamThroughput)
def team_throughput(request: Request, days: int = Query(default=14, ge=1, le=90)) -> TeamThroughput:
    """近 N 天吞吐：按天与按成员的执行完成数（数据来自 runs 的归属与完成时间）。"""

    try:
        member = store.resolve_session(_bearer_token(request))
        payload = store.throughput(member.organization_id, days=days)
        return TeamThroughput(
            days=payload["days"],
            daily=[ThroughputDay(**item) for item in payload["daily"]],
            members=[ThroughputMember(**item) for item in payload["members"]],
        )
    except Exception as error:
        raise account_error(error) from error


@app.patch("/api/projects/{project_id}", response_model=Project)
def update_project(project_id: UUID, data: ProjectTeamUpdate, request: Request) -> Project:
    """项目设置更新（需 project.admin）：归属团队、立项目标/人数、任务推进模式。

    PATCH 语义：只改请求里显式给出的字段。team_id 用 `model_fields_set` 区分
    "显式置空（脱离团队）"与"本次不动团队"。
    """

    try:
        store.authorize_member(project_id, _request_member_id(request), "project.admin")
        payload_keys = data.model_fields_set
        updated = store.update_project_settings(
            project_id,
            team_id=data.team_id,
            set_team="team_id" in payload_keys,
            goal=data.goal if "goal" in payload_keys else None,
            target_member_count=data.target_member_count if "target_member_count" in payload_keys else None,
            task_mode=data.task_mode if "task_mode" in payload_keys else None,
        )
        if updated.task_mode == "auto":
            # 切到全自动后立刻推进一件（其余交给维护循环），队长马上能看到调度器在干活
            store.auto_dispatch_tick(project_id, limit=1)
            publish_chat_after(project_id)
        return updated
    except Exception as error:
        raise account_error(error) from error


@app.get("/api/projects/{project_id}/workspace", response_model=ProjectWorkspaceOverview)
def project_workspace(project_id: UUID, request: Request) -> ProjectWorkspaceOverview:
    """项目工作区首屏聚合（需 project.view）：成员概览、Agent 状态、任务/成果摘要、最近聊天。

    权限由中间件按路径判定（GET → project.view），这里只负责取数与组装。
    """

    project_or_404(project_id)
    return ProjectWorkspaceOverview(**store.project_workspace(project_id, member_id=_request_member_id(request)))


@app.get("/api/projects/{project_id}/messages", response_model=list[ProjectMessage])
def list_project_messages(
    project_id: UUID,
    before: int | None = Query(default=None, description="向后翻历史页：取 seq 小于它的最近 N 条"),
    after: int | None = Query(default=None, description="向前增量：取 seq 大于它的 N 条"),
    limit: int = Query(default=60, ge=1, le=200),
) -> list[ProjectMessage]:
    """项目聊天流（需 project.view）：人类消息 + 从事件派生的 Agent/系统卡片，按 seq 升序。"""

    project_or_404(project_id)
    return store.list_project_messages(project_id, before_seq=before, after_seq=after, limit=limit)


@app.post("/api/projects/{project_id}/messages", response_model=ProjectMessage, status_code=201)
def post_project_message(project_id: UUID, data: ProjectMessageCreate, request: Request) -> ProjectMessage:
    """在项目聊天流里发言（需 project.chat：owner/project_lead/contributor/reviewer）。

    落库后立刻推给同项目的 WS 订阅者：不用等下一次维护扫描。
    """

    project_or_404(project_id)
    try:
        member_id = _request_member_id(request)
        message = store.post_project_message(project_id, member_id, data.content, data.ref_artifact_id, data.ref_task_id)
        publish_chat_after(project_id)
        return message
    except Exception as error:
        raise account_error(error) from error


@app.get("/api/team/workload", response_model=list[MemberWorkload])
def team_workload(request: Request) -> list[MemberWorkload]:
    """组织内成员工作量（谁在忙什么）。任何登录成员可见——团队透明度按用户口径"所有内容可见"。"""

    try:
        member_id = _request_member_id(request)
        member = store.get_member(member_id)
        if member.status != "active":
            raise PermissionError("account_suspended")
        return [MemberWorkload(**item) for item in store.member_workload(member.organization_id)]
    except Exception as error:
        raise account_error(error) from error


@app.get("/api/teams/{team_id}/members", response_model=list[ProjectMemberView])
def list_team_members(team_id: UUID, request: Request) -> list[ProjectMemberView]:
    try:
        _request_member_id(request)
        return [ProjectMemberView(**item) for item in store.list_team_members(team_id)]
    except Exception as error:
        raise account_error(error) from error


@app.post("/api/teams/{team_id}/members/{member_id}", response_model=TeamMemberChange)
def add_team_member(team_id: UUID, member_id: str, request: Request, data: TeamMemberUpsert | None = Body(default=None)) -> TeamMemberChange:
    """把成员加入团队（需管理员）：同时自动加入该团队的所有项目。"""

    try:
        actor = _require_admin(request)
        result = store.add_team_member(team_id, member_id, data.role if data else "contributor", actor_member_id=actor)
        return TeamMemberChange(**result)
    except Exception as error:
        raise account_error(error) from error


@app.delete("/api/teams/{team_id}/members/{member_id}", response_model=TeamMemberChange)
def remove_team_member(team_id: UUID, member_id: str, request: Request) -> TeamMemberChange:
    try:
        _require_admin(request)
        return TeamMemberChange(**store.remove_team_member(team_id, member_id))
    except Exception as error:
        raise account_error(error) from error


@app.get("/api/organizations/{organization_id}/teams", response_model=list[Team])
def list_teams(organization_id: UUID, request: Request) -> list[Team]:
    try:
        _request_member_id(request)
        return store.list_teams(organization_id)
    except Exception as error:
        raise task_protocol_error(error) from error


@app.post("/api/teams", response_model=Team, status_code=201)
def create_team(data: TeamCreate, request: Request) -> Team:
    try:
        _require_admin(request)
        return store.create_team(data)
    except Exception as error:
        raise account_error(error) from error


@app.post("/api/members", response_model=HumanMember, status_code=201)
def create_member(data: HumanMemberCreate, request: Request) -> HumanMember:
    """直接建成员（无口令、不可登录）。团队成员的正规入口是邀请码注册，这里只留给管理员补录。"""

    try:
        _require_admin(request)
        return store.create_member(data)
    except Exception as error:
        raise account_error(error) from error


@app.get("/api/members/{member_id}/projects", response_model=list[Project])
def list_member_projects(member_id: str, request: Request) -> list[Project]:
    """本人或管理员可查：否则任何登录成员都能枚举别人的项目列表。"""

    try:
        actor = _request_member_id(request)
        if actor != member_id:
            store.require_admin(actor)
        return store.list_projects_for_member(member_id)
    except Exception as error:
        raise account_error(error) from error


@app.post("/api/projects/{project_id}/members/{member_id}", status_code=204)
def add_project_member(project_id: UUID, member_id: str, role: str = "contributor") -> None:
    try:
        store.add_project_member(project_id, member_id, role)
    except Exception as error:
        raise task_protocol_error(error) from error


@app.post("/api/projects/{project_id}/agent-grants", response_model=AgentProjectGrant)
def grant_agent_project(project_id: UUID, data: AgentProjectGrant, request: Request) -> AgentProjectGrant:
    if data.project_id != project_id:
        raise HTTPException(status_code=400, detail="project_id_mismatch")
    try:
        return store.grant_agent_project(data.model_copy(update={"granted_by": _request_member_id(request)}))
    except Exception as error:
        raise task_protocol_error(error) from error


@app.post("/api/devices/pairings", response_model=DevicePairing, status_code=201)
def create_device_pairing(data: DevicePairingCreate, request: Request) -> DevicePairing:
    try:
        return store.create_device_pairing(data, _request_member_id(request))
    except Exception as error:
        raise task_protocol_error(error) from error


@app.post("/api/devices/register", response_model=DeviceCredential, status_code=201)
def register_device(data: DeviceRegisterRequest) -> DeviceCredential:
    try:
        return store.register_device(data)
    except Exception as error:
        raise task_protocol_error(error) from error


@app.get("/api/devices", response_model=list[Device])
def list_devices(request: Request) -> list[Device]:
    try:
        return store.list_devices_for_member(_request_member_id(request))
    except Exception as error:
        raise task_protocol_error(error) from error


@app.post("/api/devices/{device_id}/revoke", response_model=Device)
def revoke_device(device_id: str, data: DeviceRevokeRequest, request: Request) -> Device:
    try:
        return store.revoke_device(device_id, _request_member_id(request), data.reason)
    except Exception as error:
        raise task_protocol_error(error) from error


@app.post("/api/devices/{device_id}/rotate-token", response_model=DeviceCredential)
def rotate_device_token(device_id: str, data: DeviceTokenRotateRequest, request: Request) -> DeviceCredential:
    try:
        return store.rotate_device_token(device_id, _request_member_id(request), data.reason)
    except Exception as error:
        raise task_protocol_error(error) from error


@app.get("/api/agent/me", response_model=AgentSelfView)
def agent_self_view(request: Request) -> AgentSelfView:
    """B3：设备 Token 换自身配置。

    设备在没有项目授权时也能调用：拿到自己的身份/能力、最近心跳运行态、
    已授权项目清单与平台下发的运行参数。返回体不含任何 Token 明文。
    """

    device = _require_device_token(request)
    try:
        grants = store.list_device_project_grants_for_device(device.device_id)
        runtime = store.get_device_runtime_state(device.device_id)
    except Exception as error:
        raise task_protocol_error(error) from error
    return AgentSelfView(
        device=device,
        runtime=runtime,
        grants=grants,
        runtime_policy=agent_runtime_policy(),
    )


@app.post("/api/projects/{project_id}/device-grants", response_model=DeviceProjectCredential, status_code=201)
def create_device_project_grant(project_id: UUID, data: DeviceProjectGrantCreate, request: Request) -> DeviceProjectCredential:
    try:
        return store.create_device_project_grant(project_id, data, _request_member_id(request))
    except Exception as error:
        raise task_protocol_error(error) from error


@app.get("/api/projects/{project_id}/device-grants", response_model=list[DeviceProjectGrant])
def list_device_project_grants(project_id: UUID, request: Request) -> list[DeviceProjectGrant]:
    try:
        return store.list_device_project_grants(project_id, _request_member_id(request))
    except Exception as error:
        raise task_protocol_error(error) from error


@app.post("/api/device-grants/{grant_id}/revoke", response_model=DeviceProjectGrant)
def revoke_device_project_grant(grant_id: UUID, request: Request) -> DeviceProjectGrant:
    try:
        return store.revoke_device_project_grant(grant_id, _request_member_id(request))
    except Exception as error:
        raise task_protocol_error(error) from error


@app.post("/api/auth/dev/session", response_model=Session)
def create_dev_session(data: SessionCreate) -> Session:
    # 开发入口：只在非强制鉴权模式下可用，避免线上绕过账号体系直接签发会话
    if _auth_mode() in {"required", "production"}:
        raise HTTPException(status_code=403, detail="dev_session_disabled_in_required_mode")
    try:
        return store.create_session(data)
    except Exception as error:
        raise task_protocol_error(error) from error


# ---------- 账号系统（AUTH-1） ----------


def _client_ip(request: Request) -> str:
    """登入节流的维度之一。nginx 会带 X-Forwarded-For，取第一段。"""

    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "-"


def _bearer_token(request: Request) -> str:
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="authentication_required")
    return token


# 凭据类失败 401；权限与邀请码类 403；参数与状态冲突 409；找不到 404
_UNAUTHORIZED_CODES = {"invalid_credentials", "invalid_session", "session_expired", "current_password_invalid"}


def account_error(error: Exception) -> HTTPException:
    if isinstance(error, HTTPException):
        return error
    if isinstance(error, KeyError):
        detail = str(error.args[0]) if error.args else "not_found"
        return HTTPException(status_code=404, detail=detail)
    if isinstance(error, PermissionError):
        detail = str(error)
        status = 401 if detail in _UNAUTHORIZED_CODES else 403
        return HTTPException(status_code=status, detail=detail)
    if isinstance(error, ValueError):
        return HTTPException(status_code=409, detail=str(error))
    return HTTPException(status_code=400, detail=str(error))


def _auth_session(session: Session, account: AccountView) -> AuthSession:
    return AuthSession(token=session.token, expires_at=session.expires_at, account=account)


def _open_registration() -> bool:
    """开放邮箱注册（默认开）：填邮箱+密码即可注册即用，暂不验证邮箱。

    想关掉（回到"必须邀请码"）：PLATFORM_OPEN_REGISTRATION=0。
    注意口径：开放注册进来的成员是 contributor，且会自动加入组织内现有项目——
    这是"小团队开箱即用"的选择，公网部署又不想公开内容时就把它关掉。
    """

    return os.getenv("PLATFORM_OPEN_REGISTRATION", "1").strip().lower() not in {"0", "false", "no", "off"}


@app.post("/api/auth/register", response_model=AuthSession, status_code=201)
def register_account(data: RegisterRequest) -> AuthSession:
    """注册。三种情况：首个账号免码成为管理员；带邀请码按码上的角色；开放注册直接成为 contributor。"""

    try:
        session, account = store.register_account(data, allow_open_registration=_open_registration())
        return _auth_session(session, account)
    except Exception as error:
        raise account_error(error) from error


@app.post("/api/auth/login", response_model=AuthSession)
def login_account(data: LoginRequest, request: Request) -> AuthSession:
    key = LOGIN_THROTTLE.key(data.email, _client_ip(request))
    locked = LOGIN_THROTTLE.locked_seconds(key)
    if locked:
        raise HTTPException(status_code=429, detail="too_many_attempts", headers={"Retry-After": str(locked)})
    try:
        session, account = store.login_account(data.email, data.password)
    except Exception as error:
        mapped = account_error(error)
        if mapped.status_code == 401:
            LOGIN_THROTTLE.record_failure(key)
        raise mapped from error
    LOGIN_THROTTLE.clear(key)
    return _auth_session(session, account)


@app.post("/api/auth/logout", status_code=204)
def logout_account(request: Request) -> Response:
    try:
        store.delete_session(_bearer_token(request))
    except Exception as error:
        raise account_error(error) from error
    return Response(status_code=204)


@app.get("/api/auth/me", response_model=AccountView)
def current_account(request: Request) -> AccountView:
    try:
        return store.account_view(store.resolve_session(_bearer_token(request)))
    except Exception as error:
        raise account_error(error) from error


@app.post("/api/auth/password", response_model=AccountView)
def change_account_password(data: PasswordChangeRequest, request: Request) -> AccountView:
    """改自己的密码：保留当前会话，撤销该成员的其它会话。"""

    token = _bearer_token(request)
    try:
        member = store.resolve_session(token)
        return store.change_password(member.id, data.current_password, data.new_password, keep_token=token)
    except Exception as error:
        raise account_error(error) from error


# ---- 账号管理（仅管理员） ----


def _require_admin(request: Request) -> str:
    actor = _request_member_id(request)
    store.require_admin(actor)
    return actor


@app.get("/api/accounts", response_model=list[AccountView])
def list_accounts(request: Request) -> list[AccountView]:
    try:
        _require_admin(request)
        return store.list_accounts()
    except Exception as error:
        raise account_error(error) from error


@app.patch("/api/accounts/{member_id}", response_model=AccountView)
def update_account(member_id: str, data: AccountUpdateRequest, request: Request) -> AccountView:
    try:
        actor = _require_admin(request)
        return store.update_account(member_id, data, actor_member_id=actor)
    except Exception as error:
        raise account_error(error) from error


@app.post("/api/accounts/{member_id}/reset-password", response_model=PasswordResetResult)
def reset_account_password(member_id: str, request: Request) -> PasswordResetResult:
    try:
        actor = _require_admin(request)
        temporary, account = store.reset_password(member_id, actor_member_id=actor)
        return PasswordResetResult(temporary_password=temporary, account=account)
    except Exception as error:
        raise account_error(error) from error


@app.get("/api/invitations", response_model=list[Invitation])
def list_invitations(request: Request, limit: int = Query(default=50, ge=1, le=200)) -> list[Invitation]:
    try:
        _require_admin(request)
        return store.list_invitations(limit=limit)
    except Exception as error:
        raise account_error(error) from error


@app.post("/api/invitations", response_model=Invitation, status_code=201)
def create_invitation(data: InvitationCreate, request: Request) -> Invitation:
    try:
        # 已有真实账号时，邀请码只能由管理员发；账号系统上线前（无人设口令）保持开发期行为
        if store.count_accounts_with_password() > 0:
            store.require_admin(_request_member_id(request))
        return store.create_invitation(data)
    except Exception as error:
        raise account_error(error) from error


@app.get("/api/projects/{project_id}", response_model=Project)
def get_project(project_id: UUID) -> Project:
    return project_or_404(project_id)


@app.get("/api/projects/{project_id}/dashboard", response_model=Dashboard)
def dashboard(project_id: UUID) -> Dashboard:
    project = project_or_404(project_id)
    tasks = store.list_tasks(project_id)
    handoffs = store.list_handoffs(project_id)
    artifacts = store.list_artifacts(project_id)
    agents = store.list_agents()
    events = store.list_latest_events(project_id, limit=20)
    runs = store.list_runs(project_id)
    metrics = {
        "active_tasks": sum(task.status in {TaskStatus.CLAIMED, TaskStatus.RUNNING, TaskStatus.WAITING_REVIEW} for task in tasks),
        "pending_gates": sum(task.status == TaskStatus.WAITING_REVIEW for task in tasks),
        "approved_artifacts": sum(artifact.status == "APPROVED" for artifact in artifacts),
        "online_agents": sum(agent.status == "online" for agent in agents),
        "active_runs": sum(run.status in {"CREATED", "RUNNING"} for run in runs),
        "blocked_runs": sum(run.status == "BLOCKED" for run in runs),
        "evidence_coverage": 82,
    }
    return Dashboard(project=project, tasks=tasks, handoffs=handoffs, artifacts=artifacts, agents=agents, events=events, runs=runs, metrics=metrics)


@app.get("/api/projects/{project_id}/tasks", response_model=list[Task])
def list_tasks(project_id: UUID) -> list[Task]:
    project_or_404(project_id)
    return store.list_tasks(project_id)


@app.get("/api/projects/{project_id}/task-flags", response_model=list[TaskFlags])
def project_task_flags(project_id: UUID) -> list[TaskFlags]:
    """项目内任务的轻量标记（COST-1/AIP-1d）：证据缺口条数与用量超预算——给列表页画角标。

    读时聚合（两次分组查询），不落库；只返回"需要人看一眼"的任务。
    """

    project_or_404(project_id)
    return [TaskFlags(**item) for item in store.project_task_flags(project_id)]


@app.post("/api/projects/{project_id}/tasks/assign", response_model=TaskBulkAssignResult)
def bulk_assign_tasks(project_id: UUID, data: TaskBulkAssign, request: Request) -> TaskBulkAssignResult:
    """批量派单（W-2）：队长一键把任务分给某个成员，或全部收回未指派。

    逐条走同一套口径：成员只能认领无人任务（且需非 manual 模式），队长不受限；
    失败项按 task_id + 原因逐条返回，不做"全成功或全失败"。
    """

    project_or_404(project_id)
    try:
        actor = _request_member_id(request)
        result = store.assign_tasks(project_id, data.task_ids, data.assignee_member_id, actor)
        if result["task_ids"]:
            # 派单卡片立刻进群聊（不依赖下一次维护扫描）；一次批量 = 一次推送
            publish_chat_after(project_id)
        return TaskBulkAssignResult(**result)
    except Exception as error:
        raise task_protocol_error(error) from error


@app.get("/api/projects/{project_id}/deliverables", response_model=ProjectDeliverables)
def project_deliverables(project_id: UUID) -> ProjectDeliverables:
    """成果空间聚合（需 project.view）：成果物 / 交接 / 文档 / 门禁与复核 / 风险。"""

    project_or_404(project_id)
    return ProjectDeliverables(**store.project_deliverables(project_id))


@app.get("/api/projects/{project_id}/team", response_model=dict[str, Any])
def project_team_overview(project_id: UUID) -> dict[str, Any]:
    """团队视图（W2.3）：每个 Agent 的身份/能力/在线/当前任务/负载/在等什么/下一件。"""

    project_or_404(project_id)
    return project_team.project_team(store, project_id)


@app.get("/api/projects/{project_id}/production-path", response_model=dict[str, Any])
def project_production_path(project_id: UUID) -> dict[str, Any]:
    """生产路径（W2.3）：成果物 → 交接 → 下游任务的因果链（最近 50 个成果物）。"""

    project_or_404(project_id)
    return project_team.project_production_path(store, project_id)


# ---- 通用垂直工作流包（W3.1；定义形状权威 docs/WORKFLOW_SCHEMA.md v1）----------


def _workflow_http_error(error: workflow_service.WorkflowError) -> HTTPException:
    status = {
        "workflow_definition_invalid": 422,
        "workflow_key_exists": 409,
        "workflow_not_found": 404,
        "workflow_version_not_found": 404,
        "workflow_nodes_unresolvable": 422,
    }.get(error.code, 400)
    return HTTPException(status_code=status, detail={"code": error.code, "errors": error.errors})


@app.post("/api/workflows", response_model=dict[str, Any], status_code=201)
def create_workflow_package(data: WorkflowUpsert, request: Request) -> dict[str, Any]:
    """创建工作流包（定义存 JSON 列，服务端按 schema §4 八条校验，错误逐条列出）。"""

    member = _request_member(request)
    try:
        return workflow_service.create_workflow(store, member.organization_id, member.id, data.definition)
    except workflow_service.WorkflowError as error:
        raise _workflow_http_error(error) from error


@app.get("/api/workflows", response_model=list[dict[str, Any]])
def list_workflow_packages(request: Request) -> list[dict[str, Any]]:
    return workflow_service.list_workflows(store, _request_member(request).organization_id)


@app.get("/api/workflows/{workflow_id}", response_model=dict[str, Any])
def get_workflow_package(workflow_id: UUID, request: Request) -> dict[str, Any]:
    try:
        return workflow_service.get_workflow(store, workflow_id, _request_member(request).organization_id)
    except workflow_service.WorkflowError as error:
        raise _workflow_http_error(error) from error


@app.post("/api/workflows/{workflow_id}/versions", response_model=dict[str, Any], status_code=201)
def add_workflow_package_version(workflow_id: UUID, data: WorkflowUpsert, request: Request) -> dict[str, Any]:
    """追加新版本（旧版本只读，永不改写——schema §1 规则 2）。"""

    try:
        return workflow_service.add_workflow_version(store, workflow_id, _request_member_id(request), data.definition)
    except workflow_service.WorkflowError as error:
        raise _workflow_http_error(error) from error


@app.post("/api/projects/{project_id}/workflow-runs", response_model=dict[str, Any], status_code=201)
def start_workflow_run(project_id: UUID, data: WorkflowRunStart, request: Request) -> dict[str, Any]:
    """应用工作流：绑定冻结版本并把节点物化为任务骨架（骨架 ≠ 结果）。"""

    project_or_404(project_id)
    member = _request_member(request)
    try:
        return workflow_service.start_workflow_run(
            store, project_id, member.organization_id, member.id, data.workflow_id, data.inputs, data.version_id
        )
    except workflow_service.WorkflowError as error:
        raise _workflow_http_error(error) from error


@app.get("/api/projects/{project_id}/workflow-runs", response_model=list[dict[str, Any]])
def list_workflow_runs(project_id: UUID) -> list[dict[str, Any]]:
    project_or_404(project_id)
    return workflow_service.list_project_workflow_runs(store, project_id)


@app.post("/api/projects/{project_id}/tasks", response_model=Task, status_code=201)
def create_task(project_id: UUID, data: TaskCreate, request: Request) -> Task:
    project_or_404(project_id)
    # actor 服务端注入（W2.2 口径）：任务是谁建的由会话决定，不信请求体里的自报字段。
    task = store.create_task(project_id, data, actor=_request_member_id(request))
    publish_chat_after(project_id)
    return task


@app.post("/api/projects/{project_id}/imports/cumcm", response_model=ImportSummary)
def import_cumcm_workspace(project_id: UUID, data: ImportRequest, request: Request) -> ImportSummary:
    project_or_404(project_id)
    try:
        return importer.import_project(project_id, data.source_path, _request_member_id(request), data.dry_run)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


# ---- 竞赛领域包（阶段 6 数学建模模板） ----------------------------------


# ---- 文档三层版本（阶段 7 最小切片） -----------------------------------


# ---- 知识库与内置 AI（Hyper-RAG Phase A） -------------------------------


def _mask_ai_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """凭据一律只回显"configured / 空"：密钥明文不出服务器（GET 与 PUT 共用）。"""

    masked = dict(settings)
    for key in ("llm_api_key", "embedding_api_key", "mineru_api_key"):
        masked[key] = "configured" if settings.get(key) else ""
    return masked


@app.get("/api/settings/ai", response_model=dict[str, Any])
def get_member_ai_settings(request: Request) -> dict[str, Any]:
    """读取当前成员的 AI 凭据（脱敏返回）。"""

    member_id = _request_member_id(request)
    return _mask_ai_settings(knowledge_base.get_ai_settings(store, member_id))


@app.put("/api/settings/ai", response_model=dict[str, Any])
def update_member_ai_settings(data: AiSettingsUpdate, request: Request) -> dict[str, Any]:
    """保存成员 AI 凭据；空字段表示清空。响应同样脱敏。"""

    member_id = _request_member_id(request)
    return _mask_ai_settings(knowledge_base.save_ai_settings(store, member_id, data.model_dump()))


@app.post("/api/settings/ai/test", response_model=dict[str, Any])
def test_ai_settings(request: Request) -> dict[str, Any]:
    """用当前成员已保存的凭据各发一次最小请求，返回逐组件结果（UX-7-03）。

    浏览器不能持有密钥直连模型，所以探针必须在服务端跑；单个组件失败不影响其他组件。
    """

    member_id = _request_member_id(request)
    settings = knowledge_base.get_ai_settings(store, member_id)
    return ai_probe.test_all(settings)


@app.get("/api/projects/{project_id}/kb", response_model=list[dict[str, Any]])
def list_project_kbs(project_id: UUID, request: Request) -> list[dict[str, Any]]:
    """项目知识库列表。"""

    project_or_404(project_id)
    return knowledge_base.list_kbs(store, _request_member_id(request), project_id)


@app.get("/api/kb", response_model=list[dict[str, Any]])
def list_member_kbs(request: Request) -> list[dict[str, Any]]:
    """个人知识库列表（含被分享的）。"""

    return knowledge_base.list_kbs(store, _request_member_id(request))


@app.post("/api/kb", response_model=dict[str, Any], status_code=201)
def create_kb(data: KbCreate, request: Request) -> dict[str, Any]:
    """创建知识库：带 project_id 即项目库，否则个人库。"""

    member_id = _request_member_id(request)
    try:
        return knowledge_base.create_kb(store, data, member_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.post("/api/kb/{kb_id}/share", response_model=dict[str, Any])
def share_kb(kb_id: str, data: KbShareCreate, request: Request) -> dict[str, Any]:
    """把个人知识库分享给其他成员。"""

    try:
        return knowledge_base.share_kb(store, kb_id, data, _request_member_id(request))
    except knowledge_base.KnowledgeBaseError as error:
        status = 404 if error.code == "kb_not_found" else 403 if error.code == "kb_share_only_owner" else 400
        raise HTTPException(status_code=status, detail=str(error)) from error


@app.get("/api/kb/{kb_id}/documents", response_model=list[dict[str, Any]])
def get_kb_documents(kb_id: str, request: Request, include_content: bool = False) -> list[dict[str, Any]]:
    """知识库文档列表（默认不含正文）。"""

    try:
        return knowledge_base.list_kb_documents(store, kb_id, _request_member_id(request), include_content=include_content)
    except knowledge_base.KnowledgeBaseError as error:
        status = 404 if error.code == "kb_not_found" else 403
        raise HTTPException(status_code=status, detail=str(error)) from error


@app.post("/api/kb/{kb_id}/documents", response_model=dict[str, Any], status_code=201)
def add_kb_document(kb_id: str, data: KbDocumentCreate, request: Request) -> dict[str, Any]:
    """登记文档（索引由 /index 端点显式触发）。"""

    try:
        return knowledge_base.add_kb_document(store, kb_id, data, _request_member_id(request))
    except knowledge_base.KnowledgeBaseError as error:
        status = 404 if error.code == "kb_not_found" else 403
        raise HTTPException(status_code=status, detail=str(error)) from error


@app.post("/api/kb/{kb_id}/index", response_model=dict[str, Any])
def index_kb_documents(kb_id: str, data: KbIndexRequest, request: Request) -> dict[str, Any]:
    """把文档送入 hyper-rag-service 索引并回写状态。"""

    member_id = _request_member_id(request)
    try:
        knowledge_base.require_kb_access(store, kb_id, member_id)
    except knowledge_base.KnowledgeBaseError as error:
        status = 404 if error.code == "kb_not_found" else 403
        raise HTTPException(status_code=status, detail=str(error)) from error
    settings = knowledge_base.get_ai_settings(store, member_id)
    if not knowledge_base.ai_credentials_ready(settings):
        raise HTTPException(status_code=400, detail="ai_credentials_missing")
    try:
        return kb_gateway.index_documents(store, kb_id, member_id, list(data.doc_ids), settings)
    except kb_gateway.HyperRagError as error:
        status = 502 if error.code == "hyper_rag_unavailable" else 400
        raise HTTPException(status_code=status, detail=str(error)) from error


@app.post("/api/kb/{kb_id}/query", response_model=dict[str, Any])
def query_kb(kb_id: str, data: KbQueryRequest, request: Request) -> dict[str, Any]:
    """知识库检索问答（代理 hyper-rag-service）。"""

    member_id = _request_member_id(request)
    try:
        knowledge_base.require_kb_access(store, kb_id, member_id)
    except knowledge_base.KnowledgeBaseError as error:
        status = 404 if error.code == "kb_not_found" else 403
        raise HTTPException(status_code=status, detail=str(error)) from error
    settings = knowledge_base.get_ai_settings(store, member_id)
    if not knowledge_base.ai_credentials_ready(settings):
        raise HTTPException(status_code=400, detail="ai_credentials_missing")
    try:
        return kb_gateway.query(kb_id, data.question, data.mode, settings)
    except kb_gateway.HyperRagError as error:
        status = 502 if error.code == "hyper_rag_unavailable" else 400
        raise HTTPException(status_code=status, detail=str(error)) from error


@app.get("/api/kb/{kb_id}/graph/entities", response_model=dict[str, Any])
def get_kb_graph_entities(kb_id: str, request: Request, page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """超图实体分页（代理 hyper-rag-service）。"""

    _require_kb_graph_access(kb_id, request)
    try:
        return kb_gateway.entities(kb_id, page, page_size)
    except kb_gateway.HyperRagError as error:
        raise HTTPException(status_code=502 if error.code == "hyper_rag_unavailable" else 400, detail=str(error)) from error


@app.get("/api/kb/{kb_id}/graph/relationships", response_model=dict[str, Any])
def get_kb_graph_relationships(kb_id: str, request: Request, page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """超图关系分页（代理 hyper-rag-service）。"""

    _require_kb_graph_access(kb_id, request)
    try:
        return kb_gateway.relationships(kb_id, page, page_size)
    except kb_gateway.HyperRagError as error:
        raise HTTPException(status_code=502 if error.code == "hyper_rag_unavailable" else 400, detail=str(error)) from error


@app.get("/api/kb/{kb_id}/graph/entity-names", response_model=dict[str, Any])
def get_kb_graph_entity_names(kb_id: str, request: Request, page: int = 1, page_size: int = 50) -> dict[str, Any]:
    """超图实体名分页（代理 hyper-rag-service）。"""

    _require_kb_graph_access(kb_id, request)
    try:
        return kb_gateway.entity_names(kb_id, page, page_size)
    except kb_gateway.HyperRagError as error:
        raise HTTPException(status_code=502 if error.code == "hyper_rag_unavailable" else 400, detail=str(error)) from error


@app.get("/api/kb/{kb_id}/graph/vertex-neighbor/{vertex_id}", response_model=dict[str, Any])
def get_kb_graph_vertex_neighbor(kb_id: str, vertex_id: str, request: Request) -> dict[str, Any]:
    """顶点一阶邻居子图（代理 hyper-rag-service）。"""

    _require_kb_graph_access(kb_id, request)
    try:
        return kb_gateway.vertex_neighbor(kb_id, vertex_id)
    except kb_gateway.HyperRagError as error:
        raise HTTPException(status_code=502 if error.code == "hyper_rag_unavailable" else 400, detail=str(error)) from error


def _require_kb_graph_access(kb_id: str, request: Request) -> None:
    try:
        knowledge_base.require_kb_access(store, kb_id, _request_member_id(request))
    except knowledge_base.KnowledgeBaseError as error:
        status = 404 if error.code == "kb_not_found" else 403
        raise HTTPException(status_code=status, detail=str(error)) from error


@app.get("/api/ai/conversations", response_model=list[dict[str, Any]])
def list_ai_conversations(request: Request) -> list[dict[str, Any]]:
    """多会话列表（普通对话 + 检索会话）。"""

    return knowledge_base.list_conversations(store, _request_member_id(request))


@app.post("/api/ai/conversations", response_model=dict[str, Any], status_code=201)
def create_ai_conversation(data: ConversationCreate, request: Request) -> dict[str, Any]:
    """创建会话。"""

    try:
        return knowledge_base.create_conversation(store, _request_member_id(request), kb_id=data.kb_id, title=data.title, mode=data.mode)
    except knowledge_base.KnowledgeBaseError as error:
        status = 404 if error.code == "kb_not_found" else 403 if error.code == "kb_access_denied" else 400
        raise HTTPException(status_code=status, detail=str(error)) from error


@app.delete("/api/ai/conversations/{conversation_id}", status_code=204)
def delete_ai_conversation(conversation_id: str, request: Request) -> Response:
    member_id = _request_member_id(request)
    if not knowledge_base.delete_conversation(store, member_id, conversation_id):
        raise HTTPException(status_code=404, detail="conversation_not_found")
    return Response(status_code=204)


@app.post("/api/ai/conversations/{conversation_id}/messages", response_model=dict[str, Any], status_code=201)
def append_ai_message(conversation_id: str, data: MessageCreate, request: Request) -> dict[str, Any]:
    """向会话追加消息。"""

    try:
        return knowledge_base.add_message(store, _request_member_id(request), conversation_id, role=data.role, content=data.content, sources=data.sources)
    except knowledge_base.KnowledgeBaseError as error:
        status = 404 if error.code == "conversation_not_found" else 400
        raise HTTPException(status_code=status, detail=str(error)) from error


# ---- 内置 AI 普通对话（直连用户配置的 LLM） ------------------------------

from .contracts import AiChatRequest


@app.post("/api/ai/chat")
def chat_with_user_llm(data: AiChatRequest, request: Request) -> Response:
    """普通对话：**渠道优先**——模型命中启用渠道就走平台渠道（扣成员免费额度、记用量流水）；
    没有渠道接得住再回退成员自配凭据。响应带 ``via``（channel / own_key）说明走了哪条。

    ``stream=true`` 时返回 SSE（每行 ``data: {"delta": "..."}``），
    ``stream=false`` 时返回完整 JSON ``{"content": "...", "usage": {}, "via": "..."}``。
    """

    member = _request_member(request)
    settings = knowledge_base.get_ai_settings(store, member.id)

    messages = [{"role": str(item["role"]), "content": str(item["content"])} for item in data.messages]
    if data.system_prompt:
        messages.insert(0, {"role": "system", "content": str(data.system_prompt)})

    try:
        # 渠道接住后的失败不静默回落到成员自配 key（口径见 ai_chat.chat_routed）
        outcome, via = ai_chat.chat_routed(
            store,
            member_id=member.id,
            organization_id=str(member.organization_id),
            messages=messages,
            settings=settings,
            temperature=data.temperature,
            stream=data.stream,
        )
    except ai_chat.AiChatError as error:
        status = 400 if error.code == "ai_credentials_missing" else 429 if error.code == "llm_quota_exceeded" else 502
        raise HTTPException(status_code=status, detail=str(error)) from error

    if not data.stream:
        return JSONResponse({**outcome, "via": via})

    def generate() -> Generator[str, None, None]:
        for chunk in outcome:
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})



# ---- MinerU 转换队列（Phase B） ------------------------------------------


@app.post("/api/convert", response_model=dict[str, Any], status_code=202)
def enqueue_convert_job(data: ConvertEnqueueRequest, request: Request) -> dict[str, Any]:
    """入队 MinerU 转换任务（立即返回 job_id，不挂长请求）。"""

    member_id = _request_member_id(request)
    try:
        return convert_queue.enqueue(
            store,
            member_id,
            source_type=data.source_type,
            source_id=data.source_id,
            file_name=data.file_name,
        )
    except convert_queue.ConvertError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/convert", response_model=list[dict[str, Any]])
def list_convert_jobs(request: Request, limit: int = 20) -> list[dict[str, Any]]:
    """列出当前成员的转换任务。"""

    return convert_queue.list_jobs(store, _request_member_id(request), limit)


@app.get("/api/convert/{job_id}", response_model=dict[str, Any])
def get_convert_job(job_id: str, request: Request) -> dict[str, Any]:
    """查询转换任务状态与结果。"""

    try:
        return convert_queue.get_job(store, _request_member_id(request), job_id)
    except convert_queue.ConvertError as error:
        status = 404 if error.code == "convert_job_not_found" else 400
        raise HTTPException(status_code=status, detail=str(error)) from error


@app.post("/api/convert/{job_id}/to-kb", response_model=dict[str, Any])
def collect_convert_to_kb(job_id: str, data: ConvertToKbRequest, request: Request) -> dict[str, Any]:
    """把 done 任务的 Markdown 写入知识库文档。"""

    member_id = _request_member_id(request)
    try:
        knowledge_base.require_kb_access(store, data.kb_id, member_id)
        return convert_queue.collect_to_kb(store, member_id, job_id, data.kb_id)
    except convert_queue.ConvertError as error:
        status = 404 if error.code == "convert_job_not_found" else 400 if error.code == "convert_job_not_ready" else 400
        raise HTTPException(status_code=status, detail=str(error)) from error
    except knowledge_base.KnowledgeBaseError as error:
        status = 404 if error.code == "kb_not_found" else 403
        raise HTTPException(status_code=status, detail=str(error)) from error


@app.post("/api/convert/process", response_model=dict[str, Any])
def process_convert_jobs(request: Request) -> dict[str, Any]:
    """手动触发一轮队列处理（后台循环的补充入口）。"""

    member_id = _request_member_id(request)

    def settings_for(member: str) -> dict[str, Any]:
        return knowledge_base.get_ai_settings(store, member)

    def client_factory(api_key: str, base_url: str) -> convert_queue.MineruClient:
        return convert_queue.MineruClient(api_key, base_url or "https://mineru.net/api/v4")

    return convert_queue.process_pending_jobs(store, settings_for, client_factory)


# ---- 个人云盘（成员级文件暂存与项目导入） --------------------------------


@app.get("/api/drive", response_model=dict[str, Any])
def list_personal_drive(request: Request) -> dict[str, Any]:
    """列出当前成员的个人云盘文件与用量。"""

    owner = _request_member_id(request)
    return {
        "usage": personal_drive.drive_usage(store, owner),
        "files": personal_drive.list_drive_files(store, owner),
    }


@app.post("/api/drive/upload", response_model=dict[str, Any])
def upload_personal_drive_file(
    request: Request,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """上传文件到个人云盘（200MB 配额，同内容复用不重复占用）。"""

    owner = _request_member_id(request)
    content = read_upload_limited(file, environment_name="MAX_DRIVE_UPLOAD_BYTES", default_bytes=200 * 1024 * 1024, error_code="drive_upload_too_large")
    try:
        entry = personal_drive.upload_drive_file(
            store,
            owner,
            name=file.filename or "unnamed",
            content=content,
            mime_type=file.content_type,
        )
    except personal_drive.DriveError as error:
        raise HTTPException(status_code=413 if error.code == "drive_quota_exceeded" else 400, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"file": entry, "usage": personal_drive.drive_usage(store, owner)}


@app.delete("/api/drive/{file_id}", response_model=dict[str, Any])
def delete_personal_drive_file(file_id: str, request: Request) -> dict[str, Any]:
    """删除个人云盘文件；已被项目引用的文件需先处理引用。"""

    owner = _request_member_id(request)
    try:
        return personal_drive.delete_drive_file(store, owner, file_id)
    except personal_drive.DriveError as error:
        status = 404 if error.code == "drive_file_not_found" else 409 if error.code == "drive_file_referenced_by_project" else 400
        raise HTTPException(status_code=status, detail=str(error)) from error


@app.post("/api/projects/{project_id}/drive/import", response_model=dict[str, Any])
def import_personal_drive_file(
    project_id: UUID,
    data: DriveImportRequest,
    request: Request,
) -> dict[str, Any]:
    """把个人云盘文件导入项目空间，登记为项目成果物。"""

    project_or_404(project_id)
    owner = _request_member_id(request)
    try:
        return personal_drive.import_drive_file_to_project(
            store,
            owner,
            str(data.file_id),
            project_id,
            task_id=data.task_id,
            created_by=owner,
        )
    except personal_drive.DriveError as error:
        status = 404 if error.code in {"drive_file_not_found", "drive_project_not_found"} else 400
        raise HTTPException(status_code=status, detail=str(error)) from error
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/drive/from-artifact/{artifact_id}", response_model=dict[str, Any])
def copy_artifact_into_drive(artifact_id: UUID, request: Request) -> dict[str, Any]:
    """把一份成果物复制进自己的个人云盘（对话产出的「转入云盘」）。

    权限：只有**该项目的成员**能取——成果物属于项目，不是谁拿到 id 都能搬走。
    内容寻址去重与配额在 `personal_drive.upload_drive_file` 里（同内容不会重复占空间）。
    """

    member_id = _request_member_id(request)
    try:
        artifact = store.get_artifact(artifact_id)
    except Exception as error:  # noqa: BLE001 - 取不到就是 404（不把内部细节透出去）
        raise HTTPException(status_code=404, detail="artifact_not_found") from error
    try:
        allowed = {str(item.id) for item in store.list_projects_for_member(member_id)}
    except KeyError:
        # 开发模式下没有 Bearer 时会被当成合成的 member-001 —— 库里没有这个成员行、查不到成员关系。
        # 这与平台其余部分的开发模式口径一致（开发模式本身就不做访问控制）；账号模式下成员一定存在，
        # 所以这条兜底不会把生产环境的口子开大。
        allowed = {str(artifact.project_id)}
    if str(artifact.project_id) not in allowed:
        raise HTTPException(status_code=403, detail="project_membership_required")
    try:
        return personal_drive.copy_artifact_to_drive(store, member_id, artifact_id)
    except personal_drive.DriveError as error:
        status = 404 if error.code == "artifact_not_found" else 413 if error.code == "drive_quota_exceeded" else 400
        raise HTTPException(status_code=status, detail=str(error)) from error


# ---- 个人云盘文件服务（FM-1：目录树、下载、改名/移动/复制、回收站、审计） ----
#
# 旧的四条路由（上面那一段）继续可用，内部已经改走同一套服务；这一段是**新接口**，
# 路径与错误码按文件管理计划 §6.1/§6.5 定，UI（FM-2）与后续的跨空间传输（FM-6）都用它。


def _drive_actor(request: Request) -> drive.DriveActor:
    """操作者上下文：从会话解析成员，并带出他所属的组织（服务层每一步都校验）。

    成员行缺失时**拒绝**（403）而不是编一个组织——云盘是私有资产，宁可不服务也不猜。
    """

    member_id = _request_member_id(request)
    try:
        return drive.actor_for(store, member_id)
    except drive.DriveError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error


def _drive_error(error: drive.DriveError) -> HTTPException:
    """稳定错误码 → HTTP 状态：找不到 404、冲突 409、配额 413、其余 400。"""

    code = error.code
    if code in {"file_node_not_found", "drive_actor_unknown"}:
        return HTTPException(status_code=404, detail=str(error))
    if code in {
        "file_name_conflict",
        "file_revision_conflict",
        "file_directory_cycle",
        "file_referenced_by_project",
        "file_directory_not_empty",
        "file_not_trashed",
    }:
        return HTTPException(status_code=409, detail=str(error))
    if code in {"file_quota_exceeded", "file_copy_too_large"}:
        return HTTPException(status_code=413, detail=str(error))
    # 解压：条目数/总大小/压缩比超限按"太大"处理（413），路径不安全与格式不支持是 400，
    # 目标冲突是 409——前端据此决定是提示改参数还是直接报错
    if code in {"archive_too_many_entries", "archive_uncompressed_size_exceeded", "archive_ratio_exceeded"}:
        return HTTPException(status_code=413, detail=str(error))
    if code == "archive_target_conflict":
        return HTTPException(status_code=409, detail=str(error))
    return HTTPException(status_code=400, detail=str(error))


@app.get("/api/drive/nodes", response_model=dict[str, Any])
def list_drive_nodes(
    request: Request,
    parent_id: str | None = Query(default=None, description="空 = 根目录"),
    query: str = Query(default=""),
    sort: str = Query(default="name"),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
) -> dict[str, Any]:
    """列一个目录（分页 + 搜索 + 排序）。"""

    actor = _drive_actor(request)
    try:
        listing = drive.list_children(store, actor, parent_id, query=query, sort=sort, cursor=cursor, limit=limit)
    except drive.DriveError as error:
        raise _drive_error(error) from error
    return {
        "parent": listing["parent"],
        "nodes": listing["nodes"],
        "total": listing["total"],
        "next_cursor": listing["next_cursor"],
        "truncated": listing["truncated"],
        "usage": drive.usage(store, actor),
        "breadcrumb": drive.breadcrumb(store, actor, str(listing["parent"]["id"])),
    }


@app.get("/api/drive/nodes/{node_id}", response_model=dict[str, Any])
def get_drive_node(node_id: str, request: Request) -> dict[str, Any]:
    """节点详情：元数据 + 面包屑 + 项目引用（详情抽屉的数据源）。"""

    actor = _drive_actor(request)
    try:
        node = drive.get_node(store, actor, node_id)
        return {
            "node": node,
            "breadcrumb": drive.breadcrumb(store, actor, node_id),
            "refs": drive.refs_for(store, node_id),
            "audit": drive.audit_log(store, actor, node_id, limit=20),
        }
    except drive.DriveError as error:
        raise _drive_error(error) from error


@app.get("/api/drive/nodes/{node_id}/content")
def download_drive_node(node_id: str, request: Request) -> Response:
    """下载文件正文：响应头必须 latin-1 安全（中文名走 RFC 5987 `filename*`）。"""

    actor = _drive_actor(request)
    try:
        node, content = drive.read_content(store, actor, node_id)
    except drive.DriveError as error:
        raise _drive_error(error) from error
    headers = {
        "Content-Disposition": content_disposition(node["name"]),
        "Content-Type": node.get("mime_type") or "application/octet-stream",
        "X-Drive-Revision": str(node.get("revision", 1)),
        "X-Content-SHA256": str(node.get("content_hash") or ""),
    }
    return Response(content=content, media_type=headers["Content-Type"], headers=headers)


@app.post("/api/drive/directories", response_model=dict[str, Any], status_code=201)
def create_drive_directory(data: DriveDirectoryCreate, request: Request) -> dict[str, Any]:
    """新建目录。"""

    actor = _drive_actor(request)
    try:
        node = drive.create_directory(store, actor, data.parent_id, data.name)
    except drive.DriveError as error:
        raise _drive_error(error) from error
    return {"node": node, "usage": drive.usage(store, actor)}


@app.post("/api/drive/files", response_model=dict[str, Any], status_code=201)
def upload_drive_node(
    request: Request,
    file: UploadFile = File(...),
    parent_id: str | None = Form(default=None),
) -> dict[str, Any]:
    """上传文件到指定目录（默认根目录）。同名**不覆盖**，返回 409 让前端去问用户。"""

    actor = _drive_actor(request)
    content = read_upload_limited(
        file,
        environment_name="MAX_DRIVE_UPLOAD_BYTES",
        default_bytes=drive.quota_bytes(store),
        error_code="drive_upload_too_large",
    )
    try:
        node = drive.put_file(store, actor, parent_id, file.filename or "unnamed", content, file.content_type)
    except drive.DriveError as error:
        raise _drive_error(error) from error
    return {"node": node, "usage": drive.usage(store, actor)}


@app.patch("/api/drive/nodes/{node_id}", response_model=dict[str, Any])
def rename_drive_node(node_id: str, data: DriveNodePatch, request: Request) -> dict[str, Any]:
    """改名（可带 `expected_revision` 做并发校验）。"""

    actor = _drive_actor(request)
    try:
        node = drive.rename(store, actor, node_id, data.name, expected_revision=data.expected_revision)
    except drive.DriveError as error:
        raise _drive_error(error) from error
    return {"node": node}


@app.post("/api/drive/nodes/{node_id}/move", response_model=dict[str, Any])
def move_drive_node(node_id: str, data: DriveNodeMove, request: Request) -> dict[str, Any]:
    """移动到另一个目录（拒绝把目录移进自己的子树）。"""

    actor = _drive_actor(request)
    try:
        node = drive.move(store, actor, node_id, data.parent_id, expected_revision=data.expected_revision)
    except drive.DriveError as error:
        raise _drive_error(error) from error
    return {"node": node, "breadcrumb": drive.breadcrumb(store, actor, node["id"])}


@app.post("/api/drive/nodes/{node_id}/copy", response_model=dict[str, Any], status_code=201)
def copy_drive_node(node_id: str, data: DriveNodeCopy, request: Request) -> dict[str, Any]:
    """复制节点（同名自动加「-副本」后缀，默认保留两份）。"""

    actor = _drive_actor(request)
    try:
        node = drive.copy(store, actor, node_id, data.parent_id, name=data.name)
    except drive.DriveError as error:
        raise _drive_error(error) from error
    return {"node": node, "usage": drive.usage(store, actor)}


@app.delete("/api/drive/nodes/{node_id}", response_model=dict[str, Any])
def trash_drive_node(node_id: str, request: Request) -> dict[str, Any]:
    """删除 → **进回收站**（软删除）。彻底清除是 `DELETE /api/drive/trash/{node_id}`。"""

    actor = _drive_actor(request)
    try:
        result = drive.trash(store, actor, node_id)
    except drive.DriveError as error:
        raise _drive_error(error) from error
    return {**result, "usage": drive.usage(store, actor)}


@app.get("/api/drive/trash", response_model=dict[str, Any])
def list_drive_trash(request: Request) -> dict[str, Any]:
    """回收站：只列"这次删除的根"，并给出这次删了多少个节点。"""

    actor = _drive_actor(request)
    return {"nodes": drive.trash_list(store, actor), "usage": drive.usage(store, actor)}


@app.post("/api/drive/nodes/{node_id}/restore", response_model=dict[str, Any])
def restore_drive_node(node_id: str, request: Request) -> dict[str, Any]:
    """从回收站恢复（名字被占用时明确报冲突，不覆盖）。"""

    actor = _drive_actor(request)
    try:
        node = drive.restore(store, actor, node_id)
    except drive.DriveError as error:
        raise _drive_error(error) from error
    return {"node": node, "usage": drive.usage(store, actor)}


@app.delete("/api/drive/trash/{node_id}", response_model=dict[str, Any])
def purge_drive_node(node_id: str, request: Request) -> dict[str, Any]:
    """彻底清除（只有在回收站里的节点能清除；被项目引用的一律拒绝）。

    对象删除**失败不算失败**：记录进 `drive_object_cleanup` 队列并如实返回 `objects_pending`，
    由 `retry_cleanup` 重试——数据库追踪记录绝不能先丢（计划 §7.3）。
    """

    actor = _drive_actor(request)
    try:
        result = drive.purge(store, actor, node_id)
    except drive.DriveError as error:
        raise _drive_error(error) from error
    return {**result, "usage": drive.usage(store, actor)}


@app.post("/api/drive/extractions", response_model=dict[str, Any], status_code=201)
def extract_drive_archive(data: DriveExtractionCreate, request: Request) -> dict[str, Any]:
    """安全解压（.zip/.tar/.tar.gz/.tgz）：先全量校验、再落库；失败时不留半个目录。

    攻击面（Zip Slip、绝对路径、盘符、UNC、symlink/hardlink、设备文件、zip bomb、条目数、
    嵌套归档、同名冲突）在 `app/archive.py` 的**扫描阶段**一律拦掉，那里不写任何东西。
    """

    actor = _drive_actor(request)
    try:
        return archive.extract_archive(
            store,
            actor,
            data.node_id,
            target_parent_id=data.target_parent_id,
            directory_name=data.directory_name,
        )
    except drive.DriveError as error:
        raise _drive_error(error) from error


@app.get("/api/drive/nodes/{node_id}/audit", response_model=dict[str, Any])
def drive_node_audit(node_id: str, request: Request, limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    """节点审计：谁、什么时候、做了什么、允许还是拒绝。"""

    actor = _drive_actor(request)
    try:
        drive.get_node(store, actor, node_id)  # 归属校验（别人的节点不给看审计）
    except drive.DriveError as error:
        raise _drive_error(error) from error
    # 节点的完整审计（含 Agent 通过授权读取的记录）——按节点收口，不按成员收口
    return {"node_id": node_id, "events": drive.node_audit(store, node_id, actor.organization_id, limit=limit)}


@app.post("/api/drive/cleanup/retry", response_model=dict[str, Any])
def retry_drive_cleanup(request: Request, limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
    """重试对象清理队列（运维/定时任务用）。只处理**自己组织**的待删对象。"""

    actor = _drive_actor(request)
    pending = store.db.execute(
        "SELECT COUNT(*) AS c FROM drive_object_cleanup WHERE organization_id = ? AND status = 'pending'",
        (actor.organization_id,),
    ).fetchone()["c"]
    result = drive.retry_cleanup(store, limit=limit)
    return {**result, "pending_before": int(pending)}


# ---- Agent 工作区文件服务（FM-3） ------------------------------------------
#
# 两条通道**分开**（计划 §4 的核心边界）：
#   * **Agent 侧**（设备/项目令牌）：登记工作区、领取操作、回报进度与结果、读写传输内容；
#   * **浏览器侧**（人类会话）：看工作区、入队操作、看操作状态、取消。
# 平台**不**入站连接 Agent 机器，也不持有宿主机绝对路径（工作区只有 `workspace_identity`）。
# Gateway 的命令列表零改动——这是一套独立的、版本化的轮询契约。


def _workspace_actor(request: Request) -> workspace_files.WorkspaceActor:
    member_id = _request_member_id(request)
    try:
        return workspace_files.actor_for(store, member_id)
    except workspace_files.WorkspaceError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error


def _workspace_error(error: workspace_files.WorkspaceError) -> HTTPException:
    """稳定错误码 → HTTP：找不到 404、冲突/离线/过期 409、路径与参数问题 400。"""

    code = error.code
    if code in {"workspace_not_found", "workspace_operation_not_found", "workspace_transfer_not_found", "workspace_actor_unknown"}:
        return HTTPException(status_code=404, detail=str(error))
    if code in {"operation_idempotency_conflict", "workspace_operation_already_finished", "workspace_offline", "workspace_operation_expired"}:
        return HTTPException(status_code=409, detail=str(error))
    return HTTPException(status_code=400, detail=str(error))


def _authorize_workspace_member(actor: workspace_files.WorkspaceActor, workspace: dict[str, Any]) -> None:
    """工作区可见性：挂了项目的按项目成员判定，没挂的按同组织。"""

    project_id = workspace.get("project_id")
    if not project_id:
        return
    try:
        store.authorize_member(UUID(str(project_id)), actor.member_id, "project.view")
    except (PermissionError, ValueError) as error:
        raise HTTPException(status_code=403, detail="project_membership_required") from error


@app.post("/api/agent/workspaces/register", response_model=dict[str, Any])
def register_agent_workspace(data: WorkspaceRegisterRequest, request: Request) -> dict[str, Any]:
    """Agent 上报工作区（心跳时带）。凭据是项目能力令牌；`workspace_identity` 由内核算好。"""

    agent_id = request.headers.get("X-Agent-Id", "")
    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id_required")
    project_id = str(data.project_id) if data.project_id else None
    if project_id:
        _require_agent_capability(request, data.project_id, "workspace.files.claim", agent_id)
    agent_row = store.db.execute("SELECT owner_member_id FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
    if agent_row is None:
        raise HTTPException(status_code=404, detail="agent_not_found")
    actor = workspace_files.actor_for(store, str(agent_row["owner_member_id"]))
    try:
        workspace = workspace_files.register_workspace(
            store,
            agent_id=agent_id,
            organization_id=actor.organization_id,
            display_name=data.display_name,
            workspace_identity=data.workspace_identity,
            device_id=data.device_id,
            project_id=project_id,
            kind=data.kind,
            protected_paths=data.protected_paths,
            policy_version=data.policy_version,
        )
    except workspace_files.WorkspaceError as error:
        raise _workspace_error(error) from error
    return {"workspace": workspace}


@app.get("/api/agent-workspaces", response_model=dict[str, Any])
def list_agent_workspaces(request: Request) -> dict[str, Any]:
    """工作区列表（浏览器侧）：带在线状态与最近同步时间；**不返回宿主机路径**。"""

    actor = _workspace_actor(request)
    workspaces = []
    for workspace in workspace_files.list_workspaces(store, actor):
        try:
            _authorize_workspace_member(actor, workspace)
        except HTTPException:
            continue  # 不是我能看的项目工作区：不出现在列表里（不给出"存在但无权"的信号）
        workspaces.append(workspace)
    return {
        "workspaces": workspaces,
        "operations": workspace_files.list_operations(store, actor, limit=50),
        "large_file_bytes": workspace_files.LARGE_FILE_BYTES,
    }


@app.get("/api/agent-workspaces/{workspace_id}", response_model=dict[str, Any])
def get_agent_workspace(workspace_id: str, request: Request) -> dict[str, Any]:
    actor = _workspace_actor(request)
    try:
        workspace = workspace_files.get_workspace(store, actor, workspace_id)
    except workspace_files.WorkspaceError as error:
        raise _workspace_error(error) from error
    _authorize_workspace_member(actor, workspace)
    return {
        "workspace": workspace,
        "operations": workspace_files.list_operations(store, actor, workspace_id, limit=100),
        "audit": workspace_files.audit_log(store, actor, workspace_id=workspace_id, limit=50),
    }


@app.post("/api/agent-workspaces/{workspace_id}/operations", response_model=dict[str, Any], status_code=201)
def create_workspace_operation(workspace_id: str, data: WorkspaceOperationCreate, request: Request) -> dict[str, Any]:
    """入队一个文件操作（人点的）。Agent 离线时默认保持 queued，不假装执行。"""

    actor = _workspace_actor(request)
    try:
        workspace = workspace_files.get_workspace(store, actor, workspace_id)
        _authorize_workspace_member(actor, workspace)
        operation = workspace_files.create_operation(
            store,
            actor,
            workspace_id,
            operation_type=data.operation_type,
            relative_path=data.relative_path,
            arguments=data.arguments,
            idempotency_key=data.idempotency_key,
            expected_revision=data.expected_revision,
            fail_when_offline=data.fail_when_offline,
        )
    except workspace_files.WorkspaceError as error:
        raise _workspace_error(error) from error
    return {"operation": operation, "workspace": workspace}


@app.get("/api/agent-workspaces/{workspace_id}/operations/{operation_id}", response_model=dict[str, Any])
def get_workspace_operation(workspace_id: str, operation_id: str, request: Request) -> dict[str, Any]:
    actor = _workspace_actor(request)
    try:
        workspace_files.get_workspace(store, actor, workspace_id)
        operation = workspace_files.get_operation(store, actor, operation_id)
    except workspace_files.WorkspaceError as error:
        raise _workspace_error(error) from error
    if str(operation["workspace_id"]) != str(workspace_id):
        raise HTTPException(status_code=404, detail="workspace_operation_not_found")
    return {"operation": operation}


@app.post("/api/agent-workspaces/{workspace_id}/operations/{operation_id}/cancel", response_model=dict[str, Any])
def cancel_workspace_operation(workspace_id: str, operation_id: str, request: Request) -> dict[str, Any]:
    actor = _workspace_actor(request)
    try:
        workspace_files.get_workspace(store, actor, workspace_id)
        operation = workspace_files.cancel_operation(store, actor, operation_id)
    except workspace_files.WorkspaceError as error:
        raise _workspace_error(error) from error
    if str(operation["workspace_id"]) != str(workspace_id):
        raise HTTPException(status_code=404, detail="workspace_operation_not_found")
    return {"operation": operation}


@app.post("/api/agent/workspace-operations/claim", response_model=dict[str, Any])
def claim_workspace_operations(data: WorkspaceOperationClaim, request: Request) -> dict[str, Any]:
    """Agent 领活（轮询）。带项目能力令牌与 X-Agent-Id；离线工作区不发活。"""

    agent_id = request.headers.get("X-Agent-Id", "")
    project_id = request.headers.get("X-Project-Id", "")
    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id_required")
    if project_id:
        _require_agent_capability(request, UUID(project_id), "workspace.files.claim", agent_id)
    workspace_files.expire_stale_operations(store)
    operations = workspace_files.claim_operations(
        store,
        agent_id=agent_id,
        workspace_id=data.workspace_id,
        limit=data.limit,
        lease_seconds=data.lease_seconds,
    )
    return {"operations": operations, "large_file_bytes": workspace_files.LARGE_FILE_BYTES}


@app.post("/api/agent/workspace-operations/{operation_id}/start", response_model=dict[str, Any])
def start_workspace_operation(operation_id: str, request: Request) -> dict[str, Any]:
    agent_id = request.headers.get("X-Agent-Id", "")
    try:
        operation = workspace_files.start_operation(store, operation_id, agent_id=agent_id)
    except workspace_files.WorkspaceError as error:
        raise _workspace_error(error) from error
    return {"operation": operation}


@app.post("/api/agent/workspace-operations/{operation_id}/progress", response_model=dict[str, Any])
def progress_workspace_operation(operation_id: str, data: WorkspaceOperationProgress, request: Request) -> dict[str, Any]:
    agent_id = request.headers.get("X-Agent-Id", "")
    try:
        operation = workspace_files.progress_operation(store, operation_id, agent_id=agent_id, progress=data.progress)
    except workspace_files.WorkspaceError as error:
        raise _workspace_error(error) from error
    return {"operation": operation}


@app.post("/api/agent/workspace-operations/{operation_id}/complete", response_model=dict[str, Any])
def complete_workspace_operation(operation_id: str, data: WorkspaceOperationComplete, request: Request) -> dict[str, Any]:
    agent_id = request.headers.get("X-Agent-Id", "")
    try:
        operation = workspace_files.complete_operation(
            store,
            operation_id,
            agent_id=agent_id,
            success=data.success,
            result=data.result,
            error_code=data.error_code,
            error_message=data.error_message,
        )
    except workspace_files.WorkspaceError as error:
        raise _workspace_error(error) from error
    return {"operation": operation}


def _agent_transfer_actor(request: Request, agent_id: str, capability: str) -> workspace_files.WorkspaceActor:
    """Agent 侧读写传输内容：用项目能力令牌 + X-Agent-Id 认证，映射到该 Agent 所属成员的组织。"""

    project_id = request.headers.get("X-Project-Id", "")
    if project_id:
        _require_agent_capability(request, UUID(project_id), capability, agent_id)
    row = store.db.execute("SELECT owner_member_id FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="agent_not_found")
    try:
        return workspace_files.actor_for(store, str(row["owner_member_id"]))
    except workspace_files.WorkspaceError as error:
        raise _workspace_error(error) from error


@app.put("/api/agent/workspace-transfers/{transfer_id}/content", response_model=dict[str, Any])
async def agent_write_workspace_transfer(transfer_id: str, request: Request) -> dict[str, Any]:
    """Agent 把工作区里的字节写进传输会话（下载方向：Agent → 平台对象存储）。"""

    agent_id = request.headers.get("X-Agent-Id", "")
    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id_required")
    actor = _agent_transfer_actor(request, agent_id, "workspace.files.write")
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="transfer_body_required")
    try:
        transfer = workspace_files.write_transfer_content(store, actor, transfer_id, body)
    except workspace_files.WorkspaceError as error:
        raise _workspace_error(error) from error
    return {"transfer": transfer}


@app.get("/api/agent/workspace-transfers/{transfer_id}/content")
def agent_read_workspace_transfer(transfer_id: str, request: Request) -> Response:
    """Agent 读取传输会话里的字节（上传方向：平台对象存储 → 工作区）。"""

    agent_id = request.headers.get("X-Agent-Id", "")
    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id_required")
    actor = _agent_transfer_actor(request, agent_id, "workspace.files.read")
    try:
        transfer, content = workspace_files.read_transfer_content(store, actor, transfer_id, mark_consumed=True)
    except workspace_files.WorkspaceError as error:
        raise _workspace_error(error) from error
    headers = {
        "Content-Type": "application/octet-stream",
        "X-Transfer-SHA256": str(transfer.get("source_hash") or ""),
        "X-Transfer-Size": str(len(content)),
    }
    return Response(content=content, media_type="application/octet-stream", headers=headers)


@app.post("/api/workspace-transfers", response_model=dict[str, Any], status_code=201)
def create_workspace_transfer(data: WorkspaceTransferCreate, request: Request) -> dict[str, Any]:
    """开传输会话（大文件走对象存储，两边只交换会话 id 与哈希）。"""

    actor = _workspace_actor(request)
    try:
        transfer = workspace_files.create_transfer(
            store,
            actor,
            source_type=data.source_type,
            target_type=data.target_type,
            operation_id=data.operation_id,
            workspace_id=data.workspace_id,
            source_id=data.source_id,
            source_hash=data.source_hash,
            target_id=data.target_id,
            expected_size=data.expected_size,
            expected_hash=data.expected_hash,
            ttl_seconds=data.ttl_seconds,
        )
    except workspace_files.WorkspaceError as error:
        raise _workspace_error(error) from error
    return {"transfer": transfer}


@app.put("/api/workspace-transfers/{transfer_id}/content", response_model=dict[str, Any])
async def write_workspace_transfer(transfer_id: str, request: Request) -> dict[str, Any]:
    """上传内容（原样字节）：声明了哈希/大小就必须对得上，否则拒绝。"""

    actor = _workspace_actor(request)
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="transfer_body_required")
    try:
        transfer = workspace_files.write_transfer_content(
            store, actor, transfer_id, body, mime_type=request.headers.get("Content-Type")
        )
    except workspace_files.WorkspaceError as error:
        raise _workspace_error(error) from error
    return {"transfer": transfer}


@app.get("/api/workspace-transfers/{transfer_id}/content")
def read_workspace_transfer(transfer_id: str, request: Request) -> Response:
    actor = _workspace_actor(request)
    try:
        transfer, content = workspace_files.read_transfer_content(store, actor, transfer_id, mark_consumed=True)
    except workspace_files.WorkspaceError as error:
        raise _workspace_error(error) from error
    headers = {
        "Content-Type": "application/octet-stream",
        "X-Transfer-SHA256": str(transfer.get("source_hash") or ""),
        "X-Transfer-Size": str(len(content)),
    }
    return Response(content=content, media_type="application/octet-stream", headers=headers)


# ---- 断点续传（FM-6）：分片上传与续传游标 -------------------------------------
#
# 人类侧与 Agent 侧各有一套（与 `/content` 的两套同源）：
#   ① 传一片：`PUT .../parts/{n}`（同一片重传=覆盖，重试的常态）
#   ② 看游标：`GET .../parts` → 已收到哪些、还缺哪些（只补缺的，不重传已传的）
#   ③ 收口：  `POST .../complete` → 片不齐明确报缺哪些；拼完再比对整体 sha256
#   ④ 放弃：  `POST .../abort` → 作废分片会话、清半成品


@app.get("/api/workspace-transfers/{transfer_id}/parts", response_model=dict[str, Any])
def list_workspace_transfer_parts(transfer_id: str, request: Request) -> dict[str, Any]:
    actor = _workspace_actor(request)
    try:
        return workspace_files.list_transfer_parts(store, actor, transfer_id)
    except workspace_files.WorkspaceError as error:
        raise _workspace_error(error) from error


@app.put("/api/workspace-transfers/{transfer_id}/parts/{part_number}", response_model=dict[str, Any])
async def put_workspace_transfer_part(transfer_id: str, part_number: int, request: Request) -> dict[str, Any]:
    actor = _workspace_actor(request)
    body = await request.body()
    try:
        return workspace_files.upload_transfer_part(
            store, actor, transfer_id, part_number, body, part_hash=request.headers.get("X-Part-SHA256")
        )
    except workspace_files.WorkspaceError as error:
        raise _workspace_error(error) from error


@app.post("/api/workspace-transfers/{transfer_id}/complete", response_model=dict[str, Any])
def complete_workspace_transfer(transfer_id: str, request: Request) -> dict[str, Any]:
    actor = _workspace_actor(request)
    try:
        return workspace_files.complete_transfer_upload(store, actor, transfer_id)
    except workspace_files.WorkspaceError as error:
        raise _workspace_error(error) from error


@app.post("/api/workspace-transfers/{transfer_id}/abort", response_model=dict[str, Any])
def abort_workspace_transfer(transfer_id: str, request: Request) -> dict[str, Any]:
    actor = _workspace_actor(request)
    try:
        return workspace_files.abort_transfer_upload(store, actor, transfer_id)
    except workspace_files.WorkspaceError as error:
        raise _workspace_error(error) from error


@app.get("/api/agent/workspace-transfers/{transfer_id}/parts", response_model=dict[str, Any])
def agent_list_workspace_transfer_parts(transfer_id: str, request: Request) -> dict[str, Any]:
    agent_id = request.headers.get("X-Agent-Id", "")
    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id_required")
    actor = _agent_transfer_actor(request, agent_id, "workspace.files.read")
    try:
        return workspace_files.list_transfer_parts(store, actor, transfer_id)
    except workspace_files.WorkspaceError as error:
        raise _workspace_error(error) from error


@app.put("/api/agent/workspace-transfers/{transfer_id}/parts/{part_number}", response_model=dict[str, Any])
async def agent_put_workspace_transfer_part(transfer_id: str, part_number: int, request: Request) -> dict[str, Any]:
    agent_id = request.headers.get("X-Agent-Id", "")
    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id_required")
    actor = _agent_transfer_actor(request, agent_id, "workspace.files.write")
    body = await request.body()
    try:
        return workspace_files.upload_transfer_part(
            store, actor, transfer_id, part_number, body, part_hash=request.headers.get("X-Part-SHA256")
        )
    except workspace_files.WorkspaceError as error:
        raise _workspace_error(error) from error


@app.post("/api/agent/workspace-transfers/{transfer_id}/complete", response_model=dict[str, Any])
def agent_complete_workspace_transfer(transfer_id: str, request: Request) -> dict[str, Any]:
    agent_id = request.headers.get("X-Agent-Id", "")
    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id_required")
    actor = _agent_transfer_actor(request, agent_id, "workspace.files.write")
    try:
        return workspace_files.complete_transfer_upload(store, actor, transfer_id)
    except workspace_files.WorkspaceError as error:
        raise _workspace_error(error) from error


@app.get("/api/agent-workspaces/{workspace_id}/audit", response_model=dict[str, Any])
def workspace_audit_log(workspace_id: str, request: Request, limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    actor = _workspace_actor(request)
    try:
        workspace_files.get_workspace(store, actor, workspace_id)
    except workspace_files.WorkspaceError as error:
        raise _workspace_error(error) from error
    return {"workspace_id": workspace_id, "events": workspace_files.audit_log(store, actor, workspace_id=workspace_id, limit=limit)}


@app.post("/api/workspace-transfers/cleanup", response_model=dict[str, Any])
def cleanup_workspace_transfers(request: Request) -> dict[str, Any]:
    """过期传输会话回收（对象删掉、状态置 expired）。"""

    actor = _workspace_actor(request)
    return workspace_files.cleanup_expired_transfers(store, actor)

# ---- 云盘文件访问授权（FM-5） -----------------------------------------------
#
# 两条通道：**人类侧**建/看/续/撤授权；**Agent 侧**换短期 lease 并只读被授权的那部分文件。
# 平台始终不入站连接 Agent 机器，Agent 也**不能**用成员令牌访问云盘——它只有 lease。
# 与 S-3 的权限卡片无关：那是"工具执行批准"，这里是"数据访问范围"，两者不能互相替代。


def _grant_error(error: drive_grants.GrantError) -> HTTPException:
    code = error.code
    if code in {"file_access_grant_not_found", "file_node_not_found", "agent_not_found", "device_not_found"}:
        return HTTPException(status_code=404, detail=str(error))
    if code in {
        "file_access_grant_revoked",
        "file_access_grant_expired",
        "file_access_lease_revoked",
        "file_access_lease_expired",
        "file_access_scope_denied",
        "file_access_capability_denied",
        # 身份/绑定不符（错的 Agent/设备/项目/Run）：这是**授权失败**，不是"请求写错了"，
        # 所以按 403 返回（400 会让人以为改改参数就能过）
        "file_access_agent_mismatch",
        "file_access_device_mismatch",
        "file_access_project_mismatch",
        "file_access_run_mismatch",
    }:
        return HTTPException(status_code=403, detail=str(error))
    return HTTPException(status_code=400, detail=str(error))


@app.post("/api/file-access-grants", response_model=dict[str, Any], status_code=201)
def create_file_access_grant(data: FileAccessGrantCreate, request: Request) -> dict[str, Any]:
    """给某个 Agent 授权云盘里的文件/文件夹/整盘（默认只读 + 可导入项目）。"""

    member_id = _request_member_id(request)
    try:
        owner_actor = drive.actor_for(store, member_id)
        actor = workspace_files.actor_for(store, member_id)
        grant = drive_grants.create_grant(
            store,
            actor,
            owner_drive_actor=owner_actor,
            node_id=data.node_id,
            agent_id=data.agent_id,
            device_id=data.device_id,
            project_id=str(data.project_id),
            scope_type=data.scope_type,
            capabilities=data.capabilities or None,
            include_future_nodes=data.include_future_nodes,
            task_id=data.task_id,
            run_id=data.run_id,
            conversation_id=data.conversation_id,
            turn_id=data.turn_id,
            expires_in_seconds=data.expires_in_seconds,
        )
    except drive.DriveError as error:
        raise HTTPException(status_code=404 if error.code == "file_node_not_found" else 400, detail=str(error)) from error
    except drive_grants.GrantError as error:
        raise _grant_error(error) from error
    return {"grant": grant}


@app.get("/api/file-access-grants", response_model=dict[str, Any])
def list_file_access_grants(request: Request, node_id: str | None = None, agent_id: str | None = None) -> dict[str, Any]:
    """列出我发出的授权（可按节点或 Agent 过滤）。"""

    try:
        actor = workspace_files.actor_for(store, _request_member_id(request))
        grants = drive_grants.list_grants(store, actor, node_id=node_id, agent_id=agent_id)
    except drive_grants.GrantError as error:
        raise _grant_error(error) from error
    return {"grants": grants}


@app.get("/api/file-access-grants/{grant_id}", response_model=dict[str, Any])
def get_file_access_grant(grant_id: str, request: Request) -> dict[str, Any]:
    try:
        actor = workspace_files.actor_for(store, _request_member_id(request))
        return {"grant": drive_grants.get_grant(store, actor, grant_id)}
    except drive_grants.GrantError as error:
        raise _grant_error(error) from error


@app.post("/api/file-access-grants/{grant_id}/revoke", response_model=dict[str, Any])
def revoke_file_access_grant(grant_id: str, data: FileAccessGrantDecision, request: Request) -> dict[str, Any]:
    """撤销：epoch +1 并作废该 Grant 下的所有 lease（Agent 下一次读取立刻失败）。"""

    try:
        actor = workspace_files.actor_for(store, _request_member_id(request))
        return {"grant": drive_grants.revoke_grant(store, actor, grant_id, reason=data.reason)}
    except drive_grants.GrantError as error:
        raise _grant_error(error) from error


@app.post("/api/file-access-grants/{grant_id}/renew", response_model=dict[str, Any])
def renew_file_access_grant(grant_id: str, data: FileAccessGrantDecision, request: Request) -> dict[str, Any]:
    try:
        actor = workspace_files.actor_for(store, _request_member_id(request))
        return {"grant": drive_grants.renew_grant(store, actor, grant_id, expires_in_seconds=data.expires_in_seconds)}
    except drive_grants.GrantError as error:
        raise _grant_error(error) from error


@app.post("/api/agent/file-leases/exchange", response_model=dict[str, Any])
def exchange_file_lease(data: FileLeaseExchange, request: Request) -> dict[str, Any]:
    """Agent 换短期 lease：要项目能力令牌 + X-Agent-Id（+ 设备 id）。"""

    agent_id = request.headers.get("X-Agent-Id", "")
    project_id = request.headers.get("X-Project-Id", "")
    device_id = request.headers.get("X-Device-Id") or None
    if not agent_id or not project_id:
        raise HTTPException(status_code=400, detail="agent_and_project_required")
    _require_agent_capability(request, UUID(project_id), "artifact.read", agent_id)
    try:
        return drive_grants.exchange_lease(
            store,
            grant_id=data.grant_id,
            agent_id=agent_id,
            device_id=device_id,
            project_id=project_id,
            run_id=data.run_id,
            ttl_seconds=data.ttl_seconds,
        )
    except drive_grants.GrantError as error:
        raise _grant_error(error) from error


def _agent_lease_headers(request: Request) -> tuple[str, str, str | None, str]:
    agent_id = request.headers.get("X-Agent-Id", "")
    project_id = request.headers.get("X-Project-Id", "")
    device_id = request.headers.get("X-Device-Id") or None
    lease = request.headers.get("X-File-Access-Lease", "")
    if not agent_id or not project_id:
        raise HTTPException(status_code=400, detail="agent_and_project_required")
    if not lease:
        raise HTTPException(status_code=401, detail="file_access_lease_required")
    _require_agent_capability(request, UUID(project_id), "artifact.read", agent_id)
    return agent_id, project_id, device_id, lease


@app.get("/api/agent/drive/files", response_model=dict[str, Any])
def agent_list_granted_files(request: Request) -> dict[str, Any]:
    """Agent 列出被授权的文件（只有这一小块；跨范围一律看不到）。"""

    agent_id, project_id, device_id, lease = _agent_lease_headers(request)
    try:
        return drive_grants.agent_list_granted(
            store, lease_token=lease, agent_id=agent_id, project_id=project_id, device_id=device_id
        )
    except drive_grants.GrantError as error:
        raise _grant_error(error) from error


@app.get("/api/agent/drive/nodes/{node_id}", response_model=dict[str, Any])
def agent_get_granted_node(node_id: str, request: Request) -> dict[str, Any]:
    agent_id, project_id, device_id, lease = _agent_lease_headers(request)
    try:
        loaded = drive_grants._load_lease(store, lease, agent_id=agent_id, project_id=project_id, device_id=device_id)
        if not drive_grants._covered(store, loaded["grant"], node_id):
            raise drive_grants.GrantError("file_access_scope_denied", node_id)
        owner_actor = drive.DriveActor(
            member_id=str(loaded["grant"]["owner_member_id"]), organization_id=str(loaded["grant"]["organization_id"])
        )
        return {"node": drive.get_node(store, owner_actor, node_id)}
    except (drive_grants.GrantError, drive.DriveError) as error:
        raise _grant_error(error) if isinstance(error, drive_grants.GrantError) else HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/agent/drive/nodes/{node_id}/content")
def agent_get_granted_content(node_id: str, request: Request) -> Response:
    """读正文：范围/能力/epoch/设备状态逐条校验（每次读都重判，撤销立刻生效）。"""

    agent_id, project_id, device_id, lease = _agent_lease_headers(request)
    try:
        node, content = drive_grants.agent_read_content(
            store, lease_token=lease, agent_id=agent_id, project_id=project_id, device_id=device_id, node_id=node_id
        )
    except drive_grants.GrantError as error:
        raise _grant_error(error) from error
    headers = {
        "Content-Disposition": content_disposition(node["name"]),
        "Content-Type": node.get("mime_type") or "application/octet-stream",
        "X-Content-SHA256": str(node.get("content_hash") or ""),
    }
    return Response(content=content, media_type=headers["Content-Type"], headers=headers)


@app.post("/api/agent/drive/materialize", response_model=dict[str, Any])
def agent_materialize_drive(request: Request) -> dict[str, Any]:
    """物化清单：Agent 据此把文件落到 `<workspace>/inputs/`，并写来源 Manifest。"""

    agent_id, project_id, device_id, lease = _agent_lease_headers(request)
    try:
        return drive_grants.agent_materialize_manifest(
            store, lease_token=lease, agent_id=agent_id, project_id=project_id, device_id=device_id
        )
    except drive_grants.GrantError as error:
        raise _grant_error(error) from error

# ---- 显式跨空间传输与维护口（FM-6） -----------------------------------------


def _transfer_error(error: Exception) -> HTTPException:
    if isinstance(error, file_transfers.TransferError):
        code = error.code
    elif isinstance(error, workspace_files.WorkspaceError):
        code = error.code
    else:
        code = "file_transfer_failed"
    if code in {"file_node_not_found", "workspace_not_found", "workspace_transfer_not_found"}:
        return HTTPException(status_code=404, detail=str(error))
    if code in {"file_name_conflict", "file_revision_conflict", "file_transfer_expired"}:
        return HTTPException(status_code=409, detail=str(error))
    if code in {"file_quota_exceeded", "file_upload_hash_mismatch"}:
        return HTTPException(status_code=413 if code == "file_quota_exceeded" else 400, detail=str(error))
    if code.startswith("file_access_"):
        return HTTPException(status_code=403, detail=str(error))
    return HTTPException(status_code=400, detail=str(error))


@app.post("/api/file-transfers/drive-to-workspace", response_model=dict[str, Any], status_code=201)
def transfer_drive_to_workspace(data: FileTransferDriveToWorkspace, request: Request) -> dict[str, Any]:
    """云盘 → Agent 工作区：显式复制一次（不做后台同步）。同名默认不覆盖。"""

    actor = _workspace_actor(request)
    try:
        return file_transfers.drive_to_workspace(
            store,
            actor,
            node_id=data.node_id,
            workspace_id=data.workspace_id,
            relative_path=data.relative_path,
            overwrite=data.overwrite,
        )
    except (file_transfers.TransferError, workspace_files.WorkspaceError) as error:
        raise _transfer_error(error) from error


@app.post("/api/file-transfers/workspace-to-drive", response_model=dict[str, Any], status_code=201)
def transfer_workspace_to_drive(data: FileTransferWorkspaceToDrive, request: Request) -> dict[str, Any]:
    """Agent 工作区 → 云盘：先让 Agent 传进传输会话。"""

    actor = _workspace_actor(request)
    try:
        return file_transfers.workspace_to_drive(store, actor, workspace_id=data.workspace_id, relative_path=data.relative_path)
    except (file_transfers.TransferError, workspace_files.WorkspaceError) as error:
        raise _transfer_error(error) from error


@app.post("/api/file-transfers/save-to-drive", response_model=dict[str, Any], status_code=201)
def save_transfer_into_drive(data: FileTransferSave, request: Request) -> dict[str, Any]:
    """把人选定的传输内容存进云盘（同名冲突 409，不静默覆盖）。"""

    actor = _workspace_actor(request)
    try:
        return file_transfers.save_transfer_to_drive(store, actor, transfer_id=data.transfer_id, parent_id=data.parent_id, name=data.name)
    except (file_transfers.TransferError, workspace_files.WorkspaceError) as error:
        raise _transfer_error(error) from error


@app.get("/api/file-transfers", response_model=dict[str, Any])
def list_file_transfers(request: Request, workspace_id: str | None = None, limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    """传输历史：会话 + 关联操作的终态与失败原因（失败可重试）。"""

    actor = _workspace_actor(request)
    return {"transfers": file_transfers.transfer_history(store, actor, workspace_id=workspace_id, limit=limit)}


@app.post("/api/file-maintenance/transfers/cleanup", response_model=dict[str, Any])
def cleanup_file_transfers(request: Request) -> dict[str, Any]:
    """过期传输会话回收。"""

    actor = _workspace_actor(request)
    return file_transfers.cleanup_transfers(store, actor)


@app.post("/api/file-maintenance/orphans/scan", response_model=dict[str, Any])
def scan_file_orphans(request: Request) -> dict[str, Any]:
    """孤儿对象扫描：只报告不删（生产加固的"先能看见"那一步）。"""

    actor = _workspace_actor(request)
    return file_transfers.scan_orphans(store, actor)


@app.get("/api/documents/layers", response_model=dict[str, Any])
def get_document_layers() -> dict[str, Any]:
    """文档三层（草稿/提交/批准）定义与门禁规则。"""

    return document_api.document_layers()


@app.get("/api/projects/{project_id}/documents/{artifact_id}/timeline", response_model=dict[str, Any])
def get_document_timeline(project_id: UUID, artifact_id: UUID) -> dict[str, Any]:
    """版本时间线：三层状态、版本链与成员/Agent/任务/Git/审批追溯。"""

    project_or_404(project_id)
    try:
        return document_api.document_timeline(store, project_id, artifact_id)
    except document_api.DocumentLayerError as error:
        raise HTTPException(status_code=_document_error_status(error), detail=str(error)) from error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/projects/{project_id}/documents/{artifact_id}/evidence", response_model=dict[str, Any])
def get_document_evidence(project_id: UUID, artifact_id: UUID) -> dict[str, Any]:
    """证据链查询：文档与历史版本的证据、来源运行与提交追溯。"""

    project_or_404(project_id)
    try:
        return document_api.document_evidence_chain(store, project_id, artifact_id)
    except document_api.DocumentLayerError as error:
        raise HTTPException(status_code=_document_error_status(error), detail=str(error)) from error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.post("/api/projects/{project_id}/documents/{artifact_id}/submit", response_model=dict[str, Any])
def submit_document_revision(
    project_id: UUID,
    artifact_id: UUID,
    data: DocumentSubmitRequest,
    request: Request,
) -> dict[str, Any]:
    """草稿 → 提交：冻结待审；必须带证据，草稿无法绕过正式版本门禁。"""

    project_or_404(project_id)
    try:
        return document_api.submit_document(
            store,
            project_id,
            artifact_id,
            actor=data.actor or _request_member_id(request),
            actor_kind=data.actor_kind,
            evidence_ids=data.evidence_ids,
        )
    except document_api.DocumentLayerError as error:
        raise HTTPException(status_code=_document_error_status(error), detail=str(error)) from error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/projects/{project_id}/documents/{artifact_id}/diff", response_model=dict[str, Any])
def get_document_diff(
    project_id: UUID,
    artifact_id: UUID,
    from_revision: int | None = None,
    to_revision: int | None = None,
) -> dict[str, Any]:
    """版本间 Diff：内容差异（unified diff + 统计）与追溯信息变化。"""

    project_or_404(project_id)
    try:
        return document_api.document_diff(
            store, project_id, artifact_id, from_revision=from_revision, to_revision=to_revision
        )
    except document_api.DocumentLayerError as error:
        raise HTTPException(status_code=_document_error_status(error), detail=str(error)) from error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.post("/api/projects/{project_id}/documents/{artifact_id}/merge", response_model=dict[str, Any])
def merge_document_revision(
    project_id: UUID,
    artifact_id: UUID,
    data: DocumentMergeRequest,
    request: Request,
) -> dict[str, Any]:
    """合并确认：选定一方版本内容派生新草稿版本并留痕（不改动两端）。"""

    project_or_404(project_id)
    try:
        return document_api.merge_confirm(
            store,
            project_id,
            artifact_id,
            source_revision=data.source_revision,
            note=data.note,
            git_commit=data.git_commit,
            actor=data.actor or _request_member_id(request),
            actor_kind=data.actor_kind,
        )
    except document_api.DocumentLayerError as error:
        raise HTTPException(status_code=_document_error_status(error), detail=str(error)) from error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/projects/{project_id}/documents/{artifact_id}/revise", response_model=dict[str, Any])
def revise_document_revision(
    project_id: UUID,
    artifact_id: UUID,
    data: DocumentReviseRequest,
    request: Request,
) -> dict[str, Any]:
    """退回/再起草：从被拒或已批准版本派生新的草稿版本，历史保留。"""

    project_or_404(project_id)
    try:
        return document_api.revise_document(
            store,
            project_id,
            artifact_id,
            actor=data.actor or _request_member_id(request),
            actor_kind=data.actor_kind,
            description=data.description,
        )
    except document_api.DocumentLayerError as error:
        raise HTTPException(status_code=_document_error_status(error), detail=str(error)) from error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def _document_error_status(error: "document_api.DocumentLayerError") -> int:
    not_found = {
        "document_not_found",
        "document_not_in_project",
        "document_relation_target_not_in_project",
    }
    return 404 if error.code in not_found else 400


# ---- 阶段 9 最小切片：健康 / 可观测 / 配额 ----------------------------


@app.get("/api/platform/health", response_model=dict[str, Any])
def platform_health() -> dict[str, Any]:
    """平台健康与关键计数（自托管部署的探活入口）。"""

    metrics = observability.platform_metrics(store)
    return {
        "status": "ok",
        "version": observability.PLATFORM_VERSION,
        "metrics": metrics["counts"],
        "storage_bytes": metrics["storage_bytes"],
        "pending_outbox": metrics["pending_outbox"],
    }


@app.get("/api/platform/metrics", response_model=dict[str, Any])
def platform_metrics_endpoint() -> dict[str, Any]:
    """平台级事实汇总与配额限额。"""

    return observability.platform_metrics(store)


@app.get("/api/platform/quota", response_model=dict[str, Any])
def platform_quota() -> dict[str, Any]:
    """配额限额与成本单价口径。"""

    return {
        "limits": observability.quota_limits(),
        "cost_rates": observability.cost_rates(),
        "resources": sorted(observability.RESOURCE_QUOTA_KEYS),
    }


@app.get("/api/projects/{project_id}/usage", response_model=dict[str, Any])
def project_usage_endpoint(project_id: UUID) -> dict[str, Any]:
    """单项目用量与成本估算（运行/存储/Token/事件）。"""

    project_or_404(project_id)
    return observability.project_usage(store, project_id)


@app.get("/api/competition-packs", response_model=list[dict[str, Any]])
def list_competition_packs() -> list[dict[str, Any]]:
    """列出可用的竞赛领域包及其模板/DAG 概要。"""

    return pack_api.pack_summaries()


@app.get("/api/competition-packs/{pack_id}", response_model=dict[str, Any])
def get_competition_pack(pack_id: str) -> dict[str, Any]:
    try:
        return pack_api.pack_detail(pack_id)
    except CompetitionPackError as error:
        raise HTTPException(status_code=pack_api.pack_error_status(error), detail=str(error)) from error


@app.get("/api/competition-packs/{pack_id}/templates/{template_id}")
def get_competition_pack_template(pack_id: str, template_id: str) -> Response:
    """导出未渲染的模板骨架。"""

    try:
        pack = load_competition_pack(pack_id)
        text = pack.template_text(template_id)
    except CompetitionPackError as error:
        raise HTTPException(status_code=pack_api.pack_error_status(error), detail=str(error)) from error
    filename = pack.manifest.template(template_id).filename
    return Response(
        content=text,
        media_type="application/json" if filename.endswith(".json") else "text/markdown; charset=utf-8",
        headers={"Content-Disposition": content_disposition(filename)},
    )


# ---- 评论 / 快照 / 关系（阶段 7） ---------------------------------------


@app.post("/api/projects/{project_id}/documents/{artifact_id}/comments", response_model=dict[str, Any])
def add_document_comment(
    project_id: UUID,
    artifact_id: UUID,
    data: DocumentCommentRequest,
    request: Request,
) -> dict[str, Any]:
    """对文档发表评论或建议（可锚定段落）。"""

    project_or_404(project_id)
    try:
        return document_api.add_comment(
            store,
            project_id,
            artifact_id,
            body=data.body,
            kind=data.kind,
            anchor=data.anchor,
            actor=data.actor or _request_member_id(request),
            actor_kind=data.actor_kind,
        )
    except document_api.DocumentLayerError as error:
        raise HTTPException(status_code=_document_error_status(error), detail=str(error)) from error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/projects/{project_id}/documents/{artifact_id}/comments", response_model=dict[str, Any])
def list_document_comments(project_id: UUID, artifact_id: UUID) -> dict[str, Any]:
    project_or_404(project_id)
    try:
        return document_api.list_comments(store, project_id, artifact_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.post("/api/projects/{project_id}/documents/{artifact_id}/snapshots", response_model=dict[str, Any])
def create_document_snapshot(
    project_id: UUID,
    artifact_id: UUID,
    data: DocumentSnapshotRequest,
    request: Request,
) -> dict[str, Any]:
    """文档快照：记录当前版本的内容哈希作为检查点。"""

    project_or_404(project_id)
    try:
        return document_api.create_snapshot(
            store,
            project_id,
            artifact_id,
            label=data.label,
            actor=data.actor or _request_member_id(request),
            actor_kind=data.actor_kind,
        )
    except document_api.DocumentLayerError as error:
        raise HTTPException(status_code=_document_error_status(error), detail=str(error)) from error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/projects/{project_id}/documents/{artifact_id}/snapshots", response_model=dict[str, Any])
def list_document_snapshots(project_id: UUID, artifact_id: UUID) -> dict[str, Any]:
    project_or_404(project_id)
    try:
        return document_api.list_snapshots(store, project_id, artifact_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.post("/api/projects/{project_id}/documents/{artifact_id}/relations", response_model=dict[str, Any])
def link_document_relation(
    project_id: UUID,
    artifact_id: UUID,
    data: DocumentRelationRequest,
    request: Request,
) -> dict[str, Any]:
    """建立「结论/图表/运行/任务 ↔ 文档段落」关系。"""

    project_or_404(project_id)
    try:
        return document_api.link_relation(
            store,
            project_id,
            artifact_id,
            target_type=data.target_type,
            target_id=str(data.target_id),
            paragraph=data.paragraph,
            note=data.note,
            actor=data.actor or _request_member_id(request),
            actor_kind=data.actor_kind,
        )
    except document_api.DocumentLayerError as error:
        raise HTTPException(status_code=_document_error_status(error), detail=str(error)) from error
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/projects/{project_id}/documents/{artifact_id}/relations", response_model=dict[str, Any])
def list_document_relations(project_id: UUID, artifact_id: UUID) -> dict[str, Any]:
    project_or_404(project_id)
    try:
        return document_api.list_relations(store, project_id, artifact_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/projects/{project_id}/impact", response_model=dict[str, Any])
def get_impact_lookup(
    project_id: UUID,
    target_type: str = "artifact",
    target_id: str = "",
) -> dict[str, Any]:
    """影响面定位：某个结果/图表/运行变化后，受影响的文档与段落。"""

    project_or_404(project_id)
    if not target_id:
        raise HTTPException(status_code=400, detail="impact_target_id_required")
    try:
        return document_api.impact_lookup(store, project_id, target_type=target_type, target_id=target_id)
    except document_api.DocumentLayerError as error:
        raise HTTPException(status_code=_document_error_status(error), detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


# ---- 阶段 8 交付：装配 / 检查 / 提交包 / 恢复校验 ----------------------


@app.post("/api/projects/{project_id}/delivery/compile", response_model=dict[str, Any])
def compile_delivery_paper(
    project_id: UUID,
    data: DeliveryCompileRequest,
    request: Request,
) -> dict[str, Any]:
    """把已批准论文源码编译为 PDF 并登记为 compiled_pdf（引擎缺失时 fail-closed）。"""

    project_or_404(project_id)
    try:
        return delivery.compile_paper(
            store,
            project_id,
            source=data.source,
            artifact_name=data.artifact_name,
            actor=data.actor or _request_member_id(request),
            actor_kind=data.actor_kind,
        )
    except delivery.DeliveryError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except CompetitionPackError as error:
        raise HTTPException(status_code=pack_api.pack_error_status(error), detail=str(error)) from error
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/projects/{project_id}/delivery/assembly", response_model=dict[str, Any])
def get_delivery_assembly(project_id: UUID) -> dict[str, Any]:
    """按批准素材装配论文大纲与内容片段（未批准素材一律排除并列出）。"""

    project_or_404(project_id)
    try:
        return delivery.assemble_paper(store, project_id)
    except CompetitionPackError as error:
        raise HTTPException(status_code=pack_api.pack_error_status(error), detail=str(error)) from error


@app.get("/api/projects/{project_id}/delivery/slides")
def get_delivery_slides(project_id: UUID) -> Response:
    """生成 Marp 兼容的答辩提纲（Markdown）。"""

    project_or_404(project_id)
    text = delivery.assemble_slides(store, project_id)
    return Response(content=text, media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="SLIDES.md"'})


@app.post("/api/projects/{project_id}/delivery/checklist", response_model=dict[str, Any])
def run_delivery_checklist(project_id: UUID, data: DeliveryChecklistRequest) -> dict[str, Any]:
    """交付检查：图表引用、引用与公式、匿名、附件、页数。"""

    project_or_404(project_id)
    return delivery.build_delivery_checklist(store, project_id, paper_text=data.paper_text)


@app.post("/api/projects/{project_id}/delivery/submission-bundle", response_model=dict[str, Any])
def create_delivery_submission_bundle(
    project_id: UUID,
    data: DeliveryBundleRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """生成提交包并提交待审（冻结需人工批准）。"""

    project_or_404(project_id)
    key = require_idempotency_key(data.idempotency_key or idempotency_key)
    fingerprint = request_hash({"project_id": str(project_id), "label": data.label})
    try:
        return idempotent_json_response(
            key,
            "delivery.submission-bundle",
            fingerprint,
            lambda: delivery.create_submission_bundle(
                store,
                project_id,
                actor=data.actor or _request_member_id(request),
                actor_kind=data.actor_kind,
                label=data.label,
            ),
        )
    except delivery.DeliveryError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except CompetitionPackError as error:
        raise HTTPException(status_code=pack_api.pack_error_status(error), detail=str(error)) from error
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/projects/{project_id}/delivery/submission-bundle/{artifact_id}/verify", response_model=dict[str, Any])
def verify_delivery_submission_bundle(
    project_id: UUID,
    artifact_id: UUID,
    restore_check: bool = False,
) -> dict[str, Any]:
    """校验提交包一致性；`restore_check=true` 时在隔离 Store 上做恢复比对。"""

    project_or_404(project_id)
    restore_store = None
    if restore_check:
        restore_store = Store(BASE_DIR / "data" / "restore-check.db", object_store=create_object_store(BASE_DIR / "data" / "restore-check-objects"))
    try:
        return delivery.verify_submission_bundle(store, project_id, artifact_id, restore_store=restore_store)
    except delivery.DeliveryError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    finally:
        if restore_store is not None:
            restore_store.close()


@app.get("/api/projects/{project_id}/competition-pack", response_model=dict[str, Any])
def get_project_competition_pack(project_id: UUID) -> dict[str, Any]:
    """项目视角的 pack：解析结果 + 物化进度。"""

    project = project_or_404(project_id)
    try:
        pack = pack_api.pack_for_project(project)
    except CompetitionPackError as error:
        raise HTTPException(status_code=pack_api.pack_error_status(error), detail=str(error)) from error
    return {
        "project_id": str(project.id),
        "competition_pack": project.competition_pack,
        "pack": pack.as_dict(),
        "materialization": pack_api.materialization_status(store, project.id, pack),
    }


@app.post("/api/projects/{project_id}/competition-pack/apply", response_model=dict[str, Any])
def apply_project_competition_pack(
    project_id: UUID,
    data: CompetitionPackApplyRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """把四问 DAG 与模板物化到项目（任务 + 成果物骨架），天然幂等。"""

    project = project_or_404(project_id)
    key = require_idempotency_key(data.idempotency_key or idempotency_key)
    fingerprint = request_hash(
        {
            "project_id": str(project.id),
            "problem_code": data.problem_code,
            "questions": data.questions,
        }
    )
    try:
        result = idempotent_json_response(
            key,
            "competition-pack.apply",
            fingerprint,
            lambda: pack_api.apply_pack(
                store,
                project,
                problem_code=data.problem_code,
                questions=data.questions,
                # 身份服务端注入（W2.2）：请求体里的 created_by 一律忽略，不信任自报字段。
                created_by=_request_member_id(request),
            ),
        )
        if project.task_mode == "auto":
            # 任务刚物化出来：auto 模式顺手推进一件，别让队长等下一拍
            store.auto_dispatch_tick(project.id, limit=1)
            publish_chat_after(project.id)
        return result
    except CompetitionPackError as error:
        raise HTTPException(status_code=pack_api.pack_error_status(error), detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/projects/{project_id}/competition-pack/validate", response_model=dict[str, Any])
def validate_project_competition_pack(project_id: UUID) -> dict[str, Any]:
    """按 pack 规则校验项目成果物：四问覆盖、章节字数、官方结果表与信息边界。"""

    project = project_or_404(project_id)
    try:
        return pack_api.validate_project(store, project.id, pack_api.pack_for_project(project))
    except CompetitionPackError as error:
        raise HTTPException(status_code=pack_api.pack_error_status(error), detail=str(error)) from error


@app.post("/api/projects/{project_id}/imports/cumcm-handoff", response_model=HandoffImportReport)
def import_cumcm_handoff_package(project_id: UUID, data: ImportRequest, request: Request) -> HandoffImportReport:
    """导入「C 题四问完整交接包」并返回可解释报告（布局识别、分类计数、未识别文件、无损判定）。"""

    project_or_404(project_id)
    try:
        return handoff_importer.import_handoff(project_id, data.source_path, _request_member_id(request), data.dry_run)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/projects/{project_id}/competition-pack/boundary-gate", response_model=dict[str, Any])
def create_project_boundary_gate(
    project_id: UUID,
    data: BoundaryGateRequest,
    request: Request,
) -> dict[str, Any]:
    """按 pack 的信息边界规则审计运行事实并落库 Review + Gate。

    干净结论不会自动批准；违规→BLOCKED、观察缺失等→NEEDS_REVISION。
    """

    project_or_404(project_id)
    try:
        return boundary_gate.create_information_boundary_gate(
            store,
            project_id,
            task_id=data.task_id,
            actor=data.actor or _request_member_id(request),
            actor_kind=data.actor_kind,
            idempotency_key=data.idempotency_key,
        )
    except boundary_gate.BoundaryGateError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except pack_api.MachineReviewError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except CompetitionPackError as error:
        raise HTTPException(status_code=pack_api.pack_error_status(error), detail=str(error)) from error
    except (KeyError, ValueError, PermissionError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/projects/{project_id}/competition-pack/boundary-audit", response_model=dict[str, Any])
def get_project_boundary_audit(project_id: UUID, task_id: UUID | None = None) -> dict[str, Any]:
    """只读的信息边界审计结果（不落库），便于预览与排查。"""

    project_or_404(project_id)
    try:
        return boundary_gate.evaluate_information_boundary(store, project_id, task_id=task_id)
    except boundary_gate.BoundaryGateError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except CompetitionPackError as error:
        raise HTTPException(status_code=pack_api.pack_error_status(error), detail=str(error)) from error


@app.post("/api/projects/{project_id}/competition-pack/review", response_model=dict[str, Any])
def review_project_competition_pack(
    project_id: UUID,
    data: CompetitionPackReviewRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    """机器审计：把 pack 校验结果写成 Review + Gate（fatal→BLOCKED，major→NEEDS_REVISION）。

    干净结果不会自动批准：平台要求正式批准必须由人工提交。
    """

    project = project_or_404(project_id)
    try:
        return pack_api.create_machine_review(
            store,
            project,
            pack_api.pack_for_project(project),
            scope=data.scope,
            reviewer=data.reviewer,
            target_type=data.target_type,
            target_id=data.target_id,
            idempotency_key=data.idempotency_key or idempotency_key,
        )
    except pack_api.MachineReviewError as error:
        raise HTTPException(status_code=404 if error.args[0] == "machine_review_target_missing" else 400, detail=str(error)) from error
    except CompetitionPackError as error:
        raise HTTPException(status_code=pack_api.pack_error_status(error), detail=str(error)) from error
    except (KeyError, ValueError, PermissionError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.get("/api/projects/{project_id}/competition-pack/templates/{template_id}")
def export_project_competition_pack_template(
    project_id: UUID,
    template_id: str,
    problem_code: str | None = Query(default=None),
    questions: str | None = Query(default=None, description="逗号分隔的问题号，例如 1,2"),
) -> Response:
    """按项目上下文渲染并导出模板（模板导出）。"""

    project = project_or_404(project_id)
    parsed_questions: list[int] | None = None
    if questions:
        try:
            parsed_questions = [int(value) for value in questions.split(",") if value.strip()]
        except ValueError as error:
            raise HTTPException(status_code=400, detail="questions_invalid") from error
    try:
        text, pack, filename = pack_api.render_template(
            store, project, template_id, problem_code=problem_code, questions=parsed_questions
        )
    except CompetitionPackError as error:
        raise HTTPException(status_code=pack_api.pack_error_status(error), detail=str(error)) from error
    return Response(
        content=text,
        media_type="application/json" if filename.endswith(".json") else "text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": content_disposition(filename),
            "X-Pack-Id": pack.pack_id,
            "X-Pack-Version": pack.version,
        },
    )


def load_competition_pack(pack_id: str):
    """测试可替换的 pack 加载入口。"""

    from packages.competition_packs import load_pack

    return load_pack(pack_id)


@app.get("/api/tasks/mine", response_model=MyTasks)
def my_tasks(request: Request) -> MyTasks:
    """个人任务中心：指派给我的 / 我的 Agent 正在跑的 / 我的 Agent 最近完成的。"""

    try:
        member_id = _request_member_id(request)
        board = store.my_tasks(member_id)
        return MyTasks(
            assigned=[TaskBoardItem(**item) for item in board["assigned"]],
            running=[TaskBoardItem(**item) for item in board["running"]],
            recent=[TaskBoardItem(**item) for item in board["recent"]],
        )
    except Exception as error:
        raise account_error(error) from error


@app.delete("/api/projects/{project_id}/members/{member_id}", response_model=ProjectMemberRemoval)
def remove_project_member(project_id: UUID, member_id: str) -> ProjectMemberRemoval:
    """把成员移出项目（需 project.admin）。会连带撤销其设备/Agent 在本项目的授权并释放派单。"""

    try:
        project_or_404(project_id)
        return ProjectMemberRemoval(**store.remove_project_member(project_id, member_id))
    except Exception as error:
        raise account_error(error) from error


@app.patch("/api/projects/{project_id}/members/{member_id}", response_model=ProjectMemberView)
def update_project_member(project_id: UUID, member_id: str, data: ProjectMemberUpdate) -> ProjectMemberView:
    try:
        project_or_404(project_id)
        store.add_project_member(project_id, member_id, data.role)
        member = next(item for item in store.list_project_members(project_id) if item["member_id"] == member_id)
        return ProjectMemberView(**member)
    except Exception as error:
        raise account_error(error) from error


@app.get("/api/projects/{project_id}/members", response_model=list[ProjectMemberView])
def list_project_members(project_id: UUID) -> list[ProjectMemberView]:
    """项目成员目录（派单选择器用）。走中间件的 project.view 鉴权。"""

    project_or_404(project_id)
    return [ProjectMemberView(**item) for item in store.list_project_members(project_id)]


@app.patch("/api/tasks/{task_id}", response_model=Task)
def update_task(
    task_id: UUID,
    request: Request,
    status: TaskStatus | None = None,
    assignee: str | None = None,
    blocked_reason: str | None = None,
    data: TaskUpdateRequest | None = Body(default=None),
) -> Task:
    """更新任务。状态/负责人走查询参数（既有契约），执行方式、派单与意图对象走 JSON 体。"""

    try:
        dispatch_provided = data is not None and "assignee_member_id" in data.model_fields_set
        deadline_provided = data is not None and "deadline" in data.model_fields_set
        # 意图对象（AIP-1d）：同样"出现才生效"（body 里给了 budget / evidence_requirements 才写）
        budget_provided = data is not None and "budget" in data.model_fields_set
        evidence_provided = data is not None and "evidence_requirements" in data.model_fields_set
        return store.update_task(
            task_id,
            status,
            assignee,
            blocked_reason,
            actor=_request_member_id(request),
            actor_kind="member",
            resource_policy=data.resource_policy if data is not None else None,
            assignee_member_id=data.assignee_member_id if data is not None else None,
            dispatch_provided=dispatch_provided,
            deadline=data.deadline if data is not None else None,
            deadline_provided=deadline_provided,
            budget=data.budget if data is not None else None,
            budget_provided=budget_provided,
            evidence_requirements=data.evidence_requirements if data is not None else None,
            evidence_provided=evidence_provided,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="task_not_found") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.get("/api/tasks/{task_id}", response_model=TaskDetail)
def task_detail(task_id: UUID) -> TaskDetail:
    """任务详情（AIP-1d）：任务本体 + 预算执行态 + 证据缺口。

    预算执行态与证据缺口都是**读时算**（数租约行、数证据条数），不落库——避免两份真相。
    """

    try:
        task = store.get_task(task_id)
        row = store.db.execute("SELECT * FROM tasks WHERE id = ?", (str(task_id),)).fetchone()
        state = store._task_budget_state(row)
        gaps = store.task_evidence_gaps(task_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="task_not_found") from error
    return TaskDetail(task=task, budget_state=TaskBudgetState(**state), evidence_gaps=[TaskEvidenceGap(**gap) for gap in gaps])


@app.get("/api/tasks/{task_id}/candidates", response_model=TaskCandidates)
def task_candidates(task_id: UUID) -> TaskCandidates:
    """一条任务的候选执行体（AIP-1b）：能在哪几台跑、差一点的是缺哪一项、按什么排序。

    与 auto 调度器共用 `store.rank_task_candidates`，所以"界面推荐的第一个"就是"调度器会派的那一个"。
    离线机器也会列出（标注出来），因为"开机就能接"往往比"重新找机器"快。
    """

    try:
        task = store.get_task(task_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="task_not_found") from error
    row = store.db.execute("SELECT * FROM tasks WHERE id = ?", (str(task_id),)).fetchone()
    ranked = store.rank_task_candidates(task.project_id, row)
    return TaskCandidates(task_id=task_id, **ranked)


def task_protocol_error(error: Exception) -> HTTPException:
    if isinstance(error, HTTPException):
        return error
    if isinstance(error, KeyError):
        detail = str(error.args[0]) if error.args else "not_found"
        return HTTPException(status_code=404, detail=detail)
    if isinstance(error, PermissionError):
        return HTTPException(status_code=403, detail=str(error))
    if isinstance(error, ValueError):
        return HTTPException(status_code=409, detail=str(error))
    return HTTPException(status_code=400, detail=str(error))


@app.post("/api/tasks/{task_id}/claim", response_model=TaskAssignment)
def claim_task(task_id: UUID, data: TaskClaimRequest, request: Request) -> TaskAssignment:
    try:
        task = store.get_task(task_id)
        _require_agent_capability(request, task.project_id, "task.claim", data.agent_id)
        task, lease = store.claim_task(task_id, data)
        publish_chat_after(task.project_id)
        return TaskAssignment(task=task, lease=lease)
    except Exception as error:
        raise task_protocol_error(error) from error


@app.post("/api/agents/{agent_id}/tasks/claim", response_model=TaskAssignment | None)
def claim_next_task(agent_id: str, data: AgentTaskClaimRequest, request: Request) -> TaskAssignment | None:
    if not data.project_id:
        raise HTTPException(status_code=400, detail="project_id_required_for_agent_claim")
    project_or_404(data.project_id)
    _require_agent_capability(request, data.project_id, "task.claim", agent_id)
    try:
        claim = store.claim_next_task(TaskClaimRequest(agent_id=agent_id, lease_seconds=data.lease_seconds, idempotency_key=data.idempotency_key), data.project_id, data.stages)
    except Exception as error:
        raise task_protocol_error(error) from error
    if claim is None:
        return None
    task, lease = claim
    publish_chat_after(task.project_id)
    return TaskAssignment(task=task, lease=lease)


@app.post("/api/task-leases/{lease_token}/heartbeat", response_model=TaskLease)
def heartbeat_task_lease(lease_token: str, data: TaskLeaseHeartbeat, request: Request) -> TaskLease:
    try:
        _require_agent_capability(request, data.project_id, "task.lease", data.agent_id)
        return store.heartbeat_lease(lease_token, data.agent_id, data.extend_seconds, data.project_id)
    except Exception as error:
        raise task_protocol_error(error) from error


@app.post("/api/tasks/{task_id}/progress", response_model=Task)
def progress_task(task_id: UUID, data: TaskProgressRequest, request: Request) -> Task:
    try:
        task = store.get_task(task_id)
        _require_agent_capability(request, task.project_id, "task.progress", data.agent_id)
        updated = store.update_task_progress(task_id, data)
        publish_chat_after(task.project_id)
        return updated
    except Exception as error:
        raise task_protocol_error(error) from error


@app.post("/api/tasks/{task_id}/result", response_model=TaskResult)
def submit_task_result(task_id: UUID, data: TaskResultSubmit, request: Request) -> TaskResult:
    try:
        task = store.get_task(task_id)
        _require_agent_capability(request, task.project_id, "task.result", data.agent_id)
        result = store.submit_task_result(task_id, data)
        publish_chat_after(task.project_id)
        return result
    except Exception as error:
        raise task_protocol_error(error) from error


@app.get("/api/projects/{project_id}/handoffs", response_model=list[Handoff])
def list_handoffs(project_id: UUID) -> list[Handoff]:
    project_or_404(project_id)
    return store.list_handoffs(project_id)


@app.post("/api/projects/{project_id}/handoffs", response_model=Handoff, status_code=201)
def create_handoff(project_id: UUID, data: HandoffCreate) -> Handoff:
    project_or_404(project_id)
    try:
        handoff = store.create_handoff(project_id, data)
    except Exception as error:
        raise task_protocol_error(error) from error
    return handoff


@app.post("/api/handoffs/{handoff_id}/accept", response_model=Handoff)
def accept_handoff(handoff_id: UUID, data: HandoffAcceptRequest, request: Request) -> Handoff:
    try:
        actor = _request_member_id(request)
        return store.accept_handoff(handoff_id, actor, "member")
    except Exception as error:
        raise task_protocol_error(error) from error


@app.post("/api/handoffs/{handoff_id}/reject", response_model=Handoff)
def reject_handoff(handoff_id: UUID, data: HandoffDecisionRequest, request: Request) -> Handoff:
    try:
        actor = _request_member_id(request)
        return store.reject_handoff(handoff_id, actor, data.reason, data.findings, "member")
    except Exception as error:
        raise task_protocol_error(error) from error


@app.post("/api/agent/handoffs/{handoff_id}/accept", response_model=Handoff)
def agent_accept_handoff(handoff_id: UUID, data: HandoffAcceptRequest, request: Request) -> Handoff:
    try:
        handoff = store.get_handoff(handoff_id)
        agent_id = _require_agent_header(request)
        _require_agent_capability(request, handoff.project_id, "handoff.accept", agent_id)
        if not any(item.receiver_type == "agent" and item.receiver_id == agent_id for item in handoff.receipts):
            raise HTTPException(status_code=403, detail="handoff_receiver_mismatch")
        return store.accept_handoff(handoff_id, agent_id, "agent")
    except Exception as error:
        raise task_protocol_error(error) from error


@app.post("/api/agent/handoffs/{handoff_id}/reject", response_model=Handoff)
def agent_reject_handoff(handoff_id: UUID, data: HandoffDecisionRequest, request: Request) -> Handoff:
    try:
        handoff = store.get_handoff(handoff_id)
        agent_id = _require_agent_header(request)
        _require_agent_capability(request, handoff.project_id, "handoff.reject", agent_id)
        if not any(item.receiver_type == "agent" and item.receiver_id == agent_id for item in handoff.receipts):
            raise HTTPException(status_code=403, detail="handoff_receiver_mismatch")
        return store.reject_handoff(handoff_id, agent_id, data.reason, data.findings, "agent")
    except Exception as error:
        raise task_protocol_error(error) from error


@app.get("/api/projects/{project_id}/artifacts", response_model=list[Artifact])
def list_artifacts(project_id: UUID) -> list[Artifact]:
    project_or_404(project_id)
    # 来源路径是执行体机器上的绝对路径：成员侧只留文件名（身份靠 id + 内容 hash + 版本）
    return [public_artifact(artifact) for artifact in store.list_artifacts(project_id)]


@app.get("/api/projects/{project_id}/gates", response_model=list[Gate])
def list_gates(project_id: UUID) -> list[Gate]:
    project_or_404(project_id)
    return store.list_gates(project_id)


@app.get("/api/my-attention", response_model=MyAttention)
def my_attention(request: Request) -> MyAttention:
    """桌面通知用：派给我的任务 + 我该复核的任务（DESKTOP-NOTIFY）。

    客户端按 `task_id + status` 去重；这里不做"已读"状态（那是客户端的事，避免服务端多一份真相）。
    """

    try:
        payload = store.my_attention(_request_member_id(request))
        return MyAttention(
            assigned=[AttentionItem(**item) for item in payload["assigned"]],
            review_pending=[AttentionItem(**item) for item in payload["review_pending"]],
            assigned_total=payload["assigned_total"],
            review_total=payload["review_total"],
            generated_at=datetime.fromisoformat(str(payload["generated_at"])),
        )
    except Exception as error:  # noqa: BLE001 - 统一走账号错误映射（401/403）
        raise account_error(error) from error


@app.get("/api/projects/{project_id}/review-center", response_model=ReviewCenter)
def review_center(project_id: UUID) -> ReviewCenter:
    project_or_404(project_id)
    return ReviewCenter(
        gates=store.list_gates(project_id),
        reviews=store.list_reviews(project_id),
        evidence=store.list_evidence(project_id),
        risks=store.list_risks(project_id),
        handoffs=store.list_handoffs(project_id),
    )


@app.patch("/api/projects/{project_id}/risks/{risk_id}", response_model=RiskRegistryEntry)
def update_risk(project_id: UUID, risk_id: UUID, data: RiskDecisionRequest, request: Request) -> RiskRegistryEntry:
    project_or_404(project_id)
    try:
        return store.update_risk(project_id, risk_id, data, actor=_request_member_id(request))
    except Exception as error:
        raise task_protocol_error(error) from error


@app.get("/api/projects/{project_id}/evidence", response_model=list[Evidence])
def list_evidence(project_id: UUID) -> list[Evidence]:
    project_or_404(project_id)
    return store.list_evidence(project_id)


@app.post("/api/projects/{project_id}/evidence", response_model=Evidence, status_code=201)
def create_evidence(project_id: UUID, data: EvidenceCreate, request: Request) -> Evidence:
    project_or_404(project_id)
    try:
        evidence = store.create_evidence(project_id, data.model_copy(update={"created_by": _request_member_id(request)}))
        publish_chat_after(project_id)
        return evidence
    except Exception as error:
        raise task_protocol_error(error) from error


@app.post("/api/projects/{project_id}/artifacts", response_model=Artifact, status_code=201)
def create_artifact(project_id: UUID, data: ArtifactCreate, request: Request) -> Artifact:
    _enforce_quota(project_id, "artifacts")
    project_or_404(project_id)
    try:
        artifact = store.create_artifact(project_id, data, created_by=_request_member_id(request), created_by_kind="member")
        publish_chat_after(project_id)
        return artifact
    except Exception as error:
        raise task_protocol_error(error) from error


@app.post("/api/agent/projects/{project_id}/artifacts", response_model=Artifact, status_code=201)
def create_agent_artifact(project_id: UUID, data: ArtifactCreate, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> Artifact:
    """Create an output Artifact using a project-scoped Agent capability."""
    try:
        agent_id = _require_agent_header(request)
        _require_agent_capability(request, project_id, "artifact.write", agent_id)
        if data.run_id:
            run = store.get_run(data.run_id)
            if run.project_id != project_id or run.agent_id != agent_id:
                raise PermissionError("agent_artifact_run_mismatch")
        key = require_idempotency_key(idempotency_key)
        fingerprint = request_hash({"project_id": str(project_id), "agent_id": agent_id, "artifact": data.model_dump(mode="json")})
        artifact = idempotent_model_response(key, "agent.artifact.create", fingerprint, Artifact, lambda: store.create_artifact(project_id, data, created_by=agent_id, created_by_kind="agent"))
        publish_chat_after(project_id)
        return artifact
    except Exception as error:
        raise task_protocol_error(error) from error


@app.post("/api/projects/{project_id}/artifacts/{artifact_id}/versions", response_model=Artifact, status_code=201)
def create_artifact_version(project_id: UUID, artifact_id: UUID, data: ArtifactCreate, request: Request) -> Artifact:
    project_or_404(project_id)
    try:
        return store.create_artifact_version(project_id, artifact_id, data, created_by=_request_member_id(request), created_by_kind="member")
    except Exception as error:
        raise task_protocol_error(error) from error


@app.get("/api/projects/{project_id}/documents/{artifact_id}/draft", response_model=DocumentDraft | None)
def get_document_draft(project_id: UUID, artifact_id: UUID) -> DocumentDraft | None:
    """协作草稿（CL-6）：刷新/换设备后未保存的编辑还在。没有草稿时返回 null。"""

    project_or_404(project_id)
    try:
        return store.get_document_draft(project_id, artifact_id)
    except Exception as error:
        raise task_protocol_error(error) from error


@app.put("/api/projects/{project_id}/documents/{artifact_id}/draft", response_model=DocumentDraft)
def save_document_draft(project_id: UUID, artifact_id: UUID, data: DocumentDraftUpdate, request: Request) -> DocumentDraft:
    """保存协作草稿（节流调用）。

    `base_revision` 与服务端不一致 → 409 `document_draft_conflict`，由界面让用户选"以谁为准"，
    绝不静默覆盖别人的编辑（CL-6-02）。已批准内容不可再存草稿。
    """

    project_or_404(project_id)
    try:
        return store.save_document_draft(
            project_id,
            artifact_id,
            data.content,
            base_revision=data.base_revision,
            updated_by=_request_member_id(request),
        )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except Exception as error:
        raise task_protocol_error(error) from error


@app.get("/api/projects/{project_id}/artifacts/{artifact_id}/detail", response_model=ArtifactDetail)
def get_artifact_detail(project_id: UUID, artifact_id: UUID) -> ArtifactDetail:
    """成果物的全貌（CL-3）：来源、版本谱系、复核记录、被谁引用与孤儿状态。

    只读拼装，不新增存储方法：SQLite 与 PostgreSQL 两条实现因此天然一致。
    """

    project_or_404(project_id)
    try:
        artifact = store.get_artifact(artifact_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="artifact_not_found") from error
    if artifact.project_id != project_id:
        raise HTTPException(status_code=404, detail="artifact_not_in_project")

    artifacts = store.list_artifacts(project_id)
    tasks = store.list_tasks(project_id)
    reviews = [review for review in store.list_reviews(project_id) if str(review.target_id) == str(artifact_id)]

    # 版本谱系：同名称的所有版本（含自己），按版本号升序
    lineage = [
        ArtifactLineageEntry(
            artifact_id=item.id,
            version=item.version,
            status=str(item.status),
            content_hash=item.content_hash,
            created_at=item.created_at,
            is_current=item.id == artifact.id,
        )
        for item in sorted((entry for entry in artifacts if entry.name == artifact.name), key=lambda entry: entry.version)
    ]

    consumers = [
        ArtifactConsumer(task_id=task.id, title=task.title, status=str(task.status))
        for task in tasks
        if str(artifact_id) in {str(value) for value in (task.input_artifacts or [])}
    ]

    gate = next(
        (item for item in store.list_gates(project_id) if item.target_type == "artifact" and str(item.target_id) == str(artifact_id)),
        None,
    )

    source_task = next((task for task in tasks if task.id == artifact.task_id), None)
    source_run = None
    if artifact.run_id:
        try:
            run = store.get_run(artifact.run_id)
            source_run = {"id": str(run.id), "status": str(run.status), "agent_id": run.agent_id, "started_at": run.started_at.isoformat()}
        except KeyError:
            source_run = None

    # 孤儿：来源任务已不存在（任务删除/项目恢复等），只标注不清理（D-CL-7）
    orphan_reason = None
    if artifact.task_id and source_task is None:
        orphan_reason = "来源任务已不存在"
    elif artifact.run_id and source_run is None:
        orphan_reason = "来源运行记录已不存在"

    events = [
        event
        for event in store.list_latest_events(project_id, limit=200)
        if str(event.object_id or "") == str(artifact_id)
        or str((event.payload or {}).get("artifact_id") or "") == str(artifact_id)
    ][-20:]

    return ArtifactDetail(
        artifact=public_artifact(artifact),
        lineage=lineage,
        consumers=consumers,
        reviews=reviews,
        gate=gate,
        source_task={"id": str(source_task.id), "title": source_task.title, "status": str(source_task.status)} if source_task else None,
        source_run=source_run,
        events=events,
        orphan=bool(orphan_reason),
        orphan_reason=orphan_reason,
    )


@app.post("/api/artifacts/{artifact_id}/archive", response_model=Artifact)
def archive_artifact(artifact_id: UUID) -> Artifact:
    try:
        return store.archive_artifact(artifact_id)
    except Exception as error:
        raise task_protocol_error(error) from error


@app.post("/api/artifacts/{artifact_id}/content", response_model=Artifact)
def upload_artifact_content(
    artifact_id: UUID,
    file: UploadFile = File(...),
    expected_hash: str | None = Header(default=None, alias="X-Content-SHA256"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Artifact:
    try:
        key = require_idempotency_key(idempotency_key)
        content = read_upload_limited(file, environment_name="MAX_ARTIFACT_UPLOAD_BYTES", default_bytes=64 * 1024 * 1024, error_code="artifact_upload_too_large")
        expected_hash = expected_hash if isinstance(expected_hash, str) else None
        fingerprint = request_hash({"artifact_id": str(artifact_id), "mime_type": file.content_type, "expected_hash": expected_hash, "content_hash": hashlib.sha256(content).hexdigest(), "size": len(content)})
        return idempotent_model_response(key, "artifact.content.upload", fingerprint, Artifact, lambda: store.store_artifact_content(artifact_id, content, file.content_type, expected_hash))
    except Exception as error:
        raise task_protocol_error(error) from error


@app.post("/api/agent/artifacts/{artifact_id}/content", response_model=Artifact)
def upload_agent_artifact_content(
    artifact_id: UUID,
    request: Request,
    file: UploadFile = File(...),
    expected_hash: str | None = Header(default=None, alias="X-Content-SHA256"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Artifact:
    try:
        agent_id = _require_agent_header(request)
        artifact = store.get_artifact(artifact_id)
        if artifact.created_by != agent_id or artifact.created_by_kind != "agent":
            raise PermissionError("agent_artifact_owner_mismatch")
        _require_agent_capability(request, artifact.project_id, "artifact.write", agent_id)
        key = require_idempotency_key(idempotency_key)
        content = read_upload_limited(file, environment_name="MAX_ARTIFACT_UPLOAD_BYTES", default_bytes=64 * 1024 * 1024, error_code="artifact_upload_too_large")
        expected_hash = expected_hash if isinstance(expected_hash, str) else None
        fingerprint = request_hash({"artifact_id": str(artifact_id), "agent_id": agent_id, "mime_type": file.content_type, "expected_hash": expected_hash, "content_hash": hashlib.sha256(content).hexdigest(), "size": len(content)})
        return idempotent_model_response(key, "agent.artifact.content.upload", fingerprint, Artifact, lambda: store.store_artifact_content(artifact_id, content, file.content_type, expected_hash))
    except Exception as error:
        raise task_protocol_error(error) from error


def _authorize_agent_artifact_request(request: Request, artifact_id: UUID) -> tuple[str, Artifact]:
    agent_id = _require_agent_header(request)
    artifact = store.get_artifact(artifact_id)
    if artifact.created_by != agent_id or artifact.created_by_kind != "agent":
        raise PermissionError("agent_artifact_owner_mismatch")
    _require_agent_capability(request, artifact.project_id, "artifact.write", agent_id)
    return agent_id, artifact


@app.post("/api/agent/artifacts/{artifact_id}/multipart", response_model=dict[str, str])
def initiate_agent_artifact_multipart(
    artifact_id: UUID,
    request: Request,
    mime_type: str | None = Query(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, str]:
    try:
        _authorize_agent_artifact_request(request, artifact_id)
        key = require_idempotency_key(idempotency_key)
        fingerprint = request_hash({"artifact_id": str(artifact_id), "mime_type": mime_type})
        return idempotent_json_response(key, "agent.artifact.multipart.initiate", fingerprint, lambda: store.initiate_artifact_multipart(artifact_id, mime_type))
    except Exception as error:
        raise task_protocol_error(error) from error


@app.put("/api/agent/artifacts/{artifact_id}/multipart/{upload_id}/parts/{part_number}")
def upload_agent_artifact_part(
    artifact_id: UUID,
    upload_id: str,
    part_number: int,
    request: Request,
    file: UploadFile = File(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, str | int]:
    try:
        _authorize_agent_artifact_request(request, artifact_id)
        key = require_idempotency_key(idempotency_key)
        content = read_upload_limited(file, environment_name="MAX_ARTIFACT_UPLOAD_BYTES", default_bytes=64 * 1024 * 1024, error_code="artifact_part_too_large")
        fingerprint = request_hash({"artifact_id": str(artifact_id), "upload_id": upload_id, "part_number": part_number, "content_hash": hashlib.sha256(content).hexdigest(), "size": len(content)})
        return idempotent_json_response(
            key,
            "agent.artifact.multipart.part",
            fingerprint,
            lambda: {"upload_id": upload_id, "part_number": part_number, "part_hash": store.upload_artifact_part(artifact_id, upload_id, part_number, content)},
        )
    except Exception as error:
        raise task_protocol_error(error) from error


@app.post("/api/agent/artifacts/{artifact_id}/multipart/{upload_id}/complete", response_model=Artifact)
def complete_agent_artifact_multipart(
    artifact_id: UUID,
    upload_id: str,
    request: Request,
    expected_hash: str | None = Header(default=None, alias="X-Content-SHA256"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Artifact:
    try:
        _authorize_agent_artifact_request(request, artifact_id)
        key = require_idempotency_key(idempotency_key)
        fingerprint = request_hash({"artifact_id": str(artifact_id), "upload_id": upload_id, "expected_hash": expected_hash})
        return idempotent_model_response(key, "agent.artifact.multipart.complete", fingerprint, Artifact, lambda: store.complete_artifact_multipart(artifact_id, upload_id, expected_hash))
    except Exception as error:
        raise task_protocol_error(error) from error


@app.get("/api/artifacts/{artifact_id}/content")
def download_artifact_content(artifact_id: UUID) -> Response:
    try:
        artifact = store.get_artifact(artifact_id)
        content = store.get_artifact_content(artifact_id)
    except Exception as error:
        raise task_protocol_error(error) from error
    filename = artifact.name.replace("\"", "_").replace("\r", "_").replace("\n", "_")
    return Response(
        content=content,
        media_type=artifact.mime_type or "application/octet-stream",
        headers={"Content-Disposition": content_disposition(filename), "X-Content-SHA256": artifact.content_hash},
    )


@app.post("/api/artifacts/{artifact_id}/multipart", response_model=dict[str, str])
def initiate_artifact_multipart(artifact_id: UUID, mime_type: str | None = Query(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, str]:
    try:
        key = require_idempotency_key(idempotency_key)
        mime_type = mime_type if isinstance(mime_type, str) else None
        fingerprint = request_hash({"artifact_id": str(artifact_id), "mime_type": mime_type})
        return idempotent_json_response(key, "artifact.multipart.initiate", fingerprint, lambda: store.initiate_artifact_multipart(artifact_id, mime_type))
    except Exception as error:
        raise task_protocol_error(error) from error


@app.put("/api/artifacts/{artifact_id}/multipart/{upload_id}/parts/{part_number}")
def upload_artifact_part(
    artifact_id: UUID,
    upload_id: str,
    part_number: int,
    file: UploadFile = File(...),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, str | int]:
    try:
        key = require_idempotency_key(idempotency_key)
        content = read_upload_limited(file, environment_name="MAX_ARTIFACT_UPLOAD_BYTES", default_bytes=64 * 1024 * 1024, error_code="artifact_part_too_large")
        fingerprint = request_hash({"artifact_id": str(artifact_id), "upload_id": upload_id, "part_number": part_number, "content_hash": hashlib.sha256(content).hexdigest(), "size": len(content)})

        def upload() -> dict[str, str | int]:
            part_hash = store.upload_artifact_part(artifact_id, upload_id, part_number, content)
            return {"upload_id": upload_id, "part_number": part_number, "part_hash": part_hash}

        return idempotent_json_response(key, "artifact.multipart.part", fingerprint, upload)
    except Exception as error:
        raise task_protocol_error(error) from error


@app.post("/api/artifacts/{artifact_id}/multipart/{upload_id}/complete", response_model=Artifact)
def complete_artifact_multipart(
    artifact_id: UUID,
    upload_id: str,
    expected_hash: str | None = Header(default=None, alias="X-Content-SHA256"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Artifact:
    try:
        key = require_idempotency_key(idempotency_key)
        expected_hash = expected_hash if isinstance(expected_hash, str) else None
        fingerprint = request_hash({"artifact_id": str(artifact_id), "upload_id": upload_id, "expected_hash": expected_hash})
        return idempotent_model_response(key, "artifact.multipart.complete", fingerprint, Artifact, lambda: store.complete_artifact_multipart(artifact_id, upload_id, expected_hash))
    except Exception as error:
        raise task_protocol_error(error) from error


@app.delete("/api/artifacts/{artifact_id}/multipart/{upload_id}", status_code=204)
def abort_artifact_multipart(artifact_id: UUID, upload_id: str, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> None:
    try:
        key = require_idempotency_key(idempotency_key)
        fingerprint = request_hash({"artifact_id": str(artifact_id), "upload_id": upload_id})
        previous = store.get_idempotent_response(key, "artifact.multipart.abort", fingerprint)
        if previous is None:
            store.abort_artifact_multipart(artifact_id, upload_id)
            store.save_idempotent_response(key, "artifact.multipart.abort", {"status": "ABORTED"}, fingerprint)
    except Exception as error:
        raise task_protocol_error(error) from error


@app.get("/api/projects/{project_id}/git", response_model=GitRepository)
def get_git_repository(project_id: UUID) -> GitRepository:
    project_or_404(project_id)
    try:
        return store.get_git_repository(project_id)
    except Exception as error:
        raise task_protocol_error(error) from error


@app.post("/api/projects/{project_id}/git", response_model=GitRepository)
def register_git_repository(project_id: UUID, data: GitRepositoryCreate, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> GitRepository:
    if data.project_id != project_id:
        raise HTTPException(status_code=400, detail="project_id_mismatch")
    project_or_404(project_id)
    try:
        key = require_idempotency_key(idempotency_key)
        fingerprint = request_hash(data.model_dump(mode="json"))
        return idempotent_model_response(key, "git.repository.register", fingerprint, GitRepository, lambda: store.register_git_repository(data))
    except Exception as error:
        raise task_protocol_error(error) from error


@app.post("/api/projects/{project_id}/git/index", response_model=list[GitFileIndex])
def index_git_repository(project_id: UUID, commit_sha: str | None = Query(default=None), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> list[GitFileIndex]:
    project_or_404(project_id)
    try:
        key = require_idempotency_key(idempotency_key)
        fingerprint = request_hash({"project_id": str(project_id), "commit_sha": commit_sha})
        previous = store.get_idempotent_response(key, "git.index", fingerprint)
        if previous is not None:
            return [GitFileIndex.model_validate(item) for item in previous]
        result = store.index_git_repository(project_id, commit_sha)
        store.save_idempotent_response(key, "git.index", [item.model_dump(mode="json") for item in result], fingerprint)
        return result
    except Exception as error:
        raise task_protocol_error(error) from error


@app.get("/api/projects/{project_id}/git/index", response_model=list[GitFileIndex])
def list_git_index(project_id: UUID, commit_sha: str | None = Query(default=None)) -> list[GitFileIndex]:
    project_or_404(project_id)
    return store.list_git_index(project_id, commit_sha)


@app.get("/api/projects/{project_id}/export")
def export_project_bundle(project_id: UUID) -> FileResponse:
    project_or_404(project_id)
    export_dir = BASE_DIR / "data" / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    file_descriptor, temp_path = tempfile.mkstemp(prefix=f"project-{project_id}-", suffix=".zip", dir=export_dir)
    os.close(file_descriptor)
    target = Path(temp_path)
    try:
        store.export_project_bundle(project_id, target)
    except Exception as error:
        target.unlink(missing_ok=True)
        raise task_protocol_error(error) from error
    return FileResponse(target, media_type="application/zip", filename=f"project-{project_id}.zip", background=BackgroundTask(target.unlink, missing_ok=True))


@app.post("/api/projects/restore", response_model=Project)
def restore_project_bundle(request: Request, file: UploadFile = File(...), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> Project:
    restore_dir = BASE_DIR / "data" / "restore"
    restore_dir.mkdir(parents=True, exist_ok=True)
    file_descriptor, temp_path = tempfile.mkstemp(prefix="project-", suffix=".zip", dir=restore_dir)
    os.close(file_descriptor)
    target = Path(temp_path)
    try:
        key = require_idempotency_key(idempotency_key)
        content = read_upload_limited(file, environment_name="MAX_BUNDLE_UPLOAD_BYTES", default_bytes=256 * 1024 * 1024, error_code="bundle_upload_too_large")
        fingerprint = request_hash(content)
        target.write_bytes(content)
        member_id = _request_member_id(request)
        identity = store.inspect_project_bundle(target)
        organization_id = identity.get("organization_id")
        if organization_id is None:
            raise HTTPException(status_code=400, detail="bundle_organization_required")
        store.authorize_organization_member(UUID(organization_id), member_id, "project.restore")
        previous = store.get_idempotent_response(key, "project.restore", fingerprint)
        if previous is not None:
            return Project.model_validate(previous)
        result = store.restore_project_bundle(target)
        store.save_idempotent_response(key, "project.restore", result.model_dump(mode="json"), fingerprint)
        return result
    except Exception as error:
        raise task_protocol_error(error) from error
    finally:
        target.unlink(missing_ok=True)


@app.get("/api/projects/{project_id}/events", response_model=list[Event])
def list_events(
    project_id: UUID,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    latest: bool = Query(default=False, description="true = 取最近 N 条（时间线/审计要的是最近，不是最早）"),
) -> list[Event]:
    project_or_404(project_id)
    if latest:
        return store.list_latest_events(project_id, limit=limit)
    return store.list_events(project_id, after=after, limit=limit)


@app.get("/api/agents", response_model=list[Agent])
def list_agents(request: Request) -> list[Agent]:
    """Agent 列表按组织收口（此前全局可见，任何登录成员都能看到别人的机器与工作区路径）。

    响应再过一道 `path_privacy`：成员看到的是工作区**标识**，不是那台机器的绝对路径。
    """

    try:
        member = store.resolve_session(_bearer_token(request))
        return [public_agent(agent) for agent in store.list_agents(member.organization_id)]
    except Exception as error:
        raise account_error(error) from error


@app.delete("/api/agents/{agent_id}", status_code=204)
def unregister_agent(agent_id: str, request: Request) -> Response:
    """注销 Agent：本人（其所有者）或管理员可操作。"""

    try:
        actor = _request_member_id(request)
        agent = next((item for item in store.list_agents() if item.agent_id == agent_id), None)
        if agent and agent.owner_member_id != actor:
            store.require_admin(actor)
        store.unregister_agent(agent_id)
    except Exception as error:
        raise account_error(error) from error
    return Response(status_code=204)


@app.post("/api/agents/register", response_model=Agent)
def register_agent(data: AgentRegister) -> Agent:
    return store.register_agent(data)


@app.post("/api/agents/{agent_id}/heartbeat", response_model=Agent)
def heartbeat(agent_id: str) -> Agent:
    try:
        return store.heartbeat(agent_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="agent_not_found") from error


@app.post("/api/projects/{project_id}/reviews", response_model=Review, status_code=201)
def create_review(project_id: UUID, data: ReviewCreate, request: Request) -> Review:
    project_or_404(project_id)
    try:
        review = store.create_review(project_id, data.model_copy(update={"reviewer": _request_member_id(request), "reviewer_kind": "member"}))
        publish_chat_after(project_id)
        return review
    except Exception as error:
        raise task_protocol_error(error) from error


@app.get("/api/projects/{project_id}/runs", response_model=list[Run])
def list_runs(project_id: UUID) -> list[Run]:
    project_or_404(project_id)
    # 观察到输入文件在执行体机器上是绝对路径：成员侧只留文件名（真值仍在库里与边界结论里）
    return [public_run(run) for run in store.list_runs(project_id)]


@app.get("/api/runs/{run_id}", response_model=Run)
def get_run(run_id: UUID, request: Request) -> Run:
    """单次执行的详情：回答、原始输出与边界结论。

    `/api/runs/{id}` 不在 `/api/projects/...` 路径下，中间件拿不到项目，所以这里显式按
    项目做成员授权（越权读别人的执行记录必须是 403，而不是"碰巧能读到"）。
    """

    try:
        run = store.get_run(run_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="run_not_found") from error
    try:
        store.authorize_member(run.project_id, _request_member_id(request), "project.view")
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    return public_run(run)


@app.post("/api/projects/{project_id}/runs", response_model=Run, status_code=201)
def create_run(project_id: UUID, data: RunCreate, request: Request) -> Run:
    _enforce_quota(project_id, "runs")
    project_or_404(project_id)
    try:
        _require_agent_capability(request, project_id, "run.create", data.agent_id)
        run = store.create_run(project_id, data)
        publish_chat_after(project_id)
        return run
    except Exception as error:
        raise task_protocol_error(error) from error


@app.post("/api/runs/{run_id}/complete", response_model=Run)
def complete_run(run_id: UUID, data: RunComplete, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> Run:
    try:
        run = store.get_run(run_id)
        _require_agent_capability(request, run.project_id, "run.complete", run.agent_id)
        header_key = idempotency_key if isinstance(idempotency_key, str) else None
        key = data.idempotency_key or header_key or f"run-complete:{run_id}:{request_hash(data.model_dump(mode='json'))[:32]}"
        data = data.model_copy(update={"idempotency_key": key})
        fingerprint = request_hash({"run_id": str(run_id), "data": data.model_dump(mode="json")})
        result = idempotent_model_response(key, "run.complete", fingerprint, Run, lambda: store.complete_run(run_id, data))
        publish_chat_after(run.project_id)
        return result
    except Exception as error:
        raise task_protocol_error(error) from error


@app.websocket("/ws/projects/{project_id}")
async def project_events(websocket: WebSocket, project_id: UUID) -> None:
    project_or_404(project_id)
    # 浏览器无法给 WebSocket 设请求头，所以会话令牌走查询参数（nginx 对该路径关访问日志，避免令牌落盘）。
    # development 模式保持免令牌（本机开发/演示），required/production 下必须有有效会话且对项目有查看权。
    if _auth_mode() in {"required", "production"}:
        token = websocket.query_params.get("token", "")
        try:
            member = store.resolve_session(token)
            store.authorize_member(project_id, member.id, "project.view")
        except (PermissionError, KeyError):
            await websocket.close(code=4401 if token else 4403)
            return
    await manager.connect(project_id, websocket)
    try:
        await websocket.send_json(
            {
                "type": "connected",
                "project_id": str(project_id),
                "events": [event.model_dump(mode="json") for event in store.list_latest_events(project_id, limit=20)],
                # 工作区聊天流历史（最近 50 条）：页面连上即有内容，不用再单独拉一次
                "messages": [message.model_dump(mode="json") for message in store.list_project_messages(project_id, limit=50)],
                # 这条连接已"看到"到哪个序号：之后的新消息由 project.message 帧推进
                "message_seq": store.max_project_message_seq(project_id),
            }
        )
        while True:
            raw = await websocket.receive_text()
            try:
                frame = collaboration.parse_frame(raw)
            except collaboration.CollaborationError as error:
                await websocket.send_json({"type": "collaboration.error", "code": error.code, "detail": str(error)})
                continue
            await manager.relay(
                str(project_id),
                frame.as_message(sender=str(project_id)),
                exclude=websocket,
            )
    except WebSocketDisconnect:
        manager.disconnect(project_id, websocket)


def _websocket_bearer(websocket: WebSocket) -> str:
    authorization = websocket.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise GatewayProtocolError("gateway_device_token_required")
    return token


@app.websocket("/ws/agents/{device_id}")
async def agent_gateway(websocket: WebSocket, device_id: str) -> None:
    context = None
    try:
        session_id = websocket.query_params.get("session_id", "")
        connection_id = websocket.query_params.get("connection_id", "")
        if len(session_id) < 2 or len(connection_id) < 2:
            raise GatewayProtocolError("gateway_connection_identifiers_required")
        context = gateway.authenticate(
            _websocket_bearer(websocket),
            device_id=device_id,
            session_id=session_id,
            connection_id=connection_id,
            transport="websocket",
        )
        await websocket.accept()
        await websocket.send_json(gateway.connected_message(context).model_dump(mode="json"))
        while True:
            raw_message = await websocket.receive_json()
            response = gateway.receive(context, raw_message)
            await websocket.send_json(response.model_dump(mode="json"))
    except WebSocketDisconnect:
        if context is not None:
            try:
                gateway.close(context)
            except Exception:
                pass
    except GatewayProtocolError:
        await websocket.close(code=4401)
    except (KeyError, PermissionError, ValueError):
        await websocket.close(code=4403)
    finally:
        if context is not None:
            try:
                connection = store.get_agent_connection(context.connection.connection_id)
                if connection.status == "CONNECTED":
                    gateway.close(context)
            except Exception:
                pass


# ================= 「我的智能体」：对话（MY-AGENT M-1，见 docs/MY_AGENT_CONSOLE_PLAN.md） =================
# 与任务体系分开：对话不建 Task、不进任务板、不走复核——用户拍板"对话就是单纯对话"。
# 执行体侧用"项目能力令牌 + chat.run 能力"取活（与任务领取同一套鉴权），平台不新增推送通道。


def _agent_chat_http_error(error: agent_chat.AgentChatError) -> HTTPException:
    """把对话模块的稳定错误码映射成合适的状态码（别一律 500，界面上要能分辨原因）。"""

    code = str(error).split(":", 1)[0]
    if code.endswith("_not_found"):
        return HTTPException(status_code=404, detail=code)
    if "not_owned" in code or code == "device_grant_missing_chat_capability":
        return HTTPException(status_code=403, detail=code)
    if code == "conversation_busy" or code.endswith("_already_finished"):
        return HTTPException(status_code=409, detail=code)
    return HTTPException(status_code=400, detail=code)


@app.get("/api/my-agent/agents", response_model=list[MyAgentEndpoint])
def my_agent_agents(request: Request) -> list[MyAgentEndpoint]:
    return agent_chat.list_my_agents(store, _request_member_id(request))


@app.get("/api/my-agent/conversations", response_model=list[AgentChatConversation])
def my_agent_conversations(request: Request) -> list[AgentChatConversation]:
    return agent_chat.list_conversations(store, _request_member_id(request))


@app.post("/api/my-agent/conversations", response_model=AgentChatConversation, status_code=201)
def create_my_agent_conversation(data: AgentChatConversationCreate, request: Request) -> AgentChatConversation:
    try:
        return agent_chat.create_conversation(store, _request_member_id(request), data)
    except agent_chat.AgentChatError as error:
        raise _agent_chat_http_error(error) from error


@app.patch("/api/my-agent/conversations/{conversation_id}", response_model=AgentChatConversation)
def update_my_agent_conversation(
    conversation_id: UUID, data: AgentChatConversationUpdate, request: Request
) -> AgentChatConversation:
    """改会话设置（角色 / 模型 / 标题）：只影响**下一轮**（历史轮次记着它当时用的是什么）。"""

    try:
        return agent_chat.update_conversation(store, conversation_id, _request_member_id(request), data)
    except agent_chat.AgentChatError as error:
        raise _agent_chat_http_error(error) from error


@app.delete("/api/my-agent/conversations/{conversation_id}", status_code=204)
def delete_my_agent_conversation(conversation_id: UUID, request: Request) -> Response:
    try:
        agent_chat.delete_conversation(store, conversation_id, _request_member_id(request))
    except agent_chat.AgentChatError as error:
        raise _agent_chat_http_error(error) from error
    return Response(status_code=204)


@app.get("/api/my-agent/conversations/{conversation_id}/turns", response_model=list[AgentChatTurn])
def my_agent_turns(conversation_id: UUID, request: Request) -> list[AgentChatTurn]:
    try:
        return agent_chat.list_turns(store, conversation_id, _request_member_id(request))
    except agent_chat.AgentChatError as error:
        raise _agent_chat_http_error(error) from error


@app.post("/api/my-agent/conversations/{conversation_id}/messages", response_model=AgentChatTurn, status_code=201)
def send_my_agent_message(conversation_id: UUID, data: AgentChatMessageCreate, request: Request) -> AgentChatTurn:
    try:
        return agent_chat.append_message(store, conversation_id, _request_member_id(request), data)
    except agent_chat.AgentChatError as error:
        raise _agent_chat_http_error(error) from error


@app.get("/api/my-agent/turns/{turn_id}/events", response_model=list[AgentChatTurnEvent])
def my_agent_turn_events(turn_id: UUID, request: Request) -> list[AgentChatTurnEvent]:
    try:
        return agent_chat.list_turn_events(store, turn_id, _request_member_id(request))
    except agent_chat.AgentChatError as error:
        raise _agent_chat_http_error(error) from error


# ---- my-agent 实时流（W1.3，消费工作包 C 的 stream_bridge）-----------------------
#
# 帧格式 event/data/id：`id:` 帧即游标（turn 流 = agent_turn_events.sequence；会话流 = 桥自增），
# EventSource 断线重连自动带 Last-Event-ID，服务端从桥重放增量。
# 两个控制帧：`__gap__`（桥被裁剪、无法完整重放——客户端必须回权威接口全量重同步后重连，
# 绝不把残缺回放当完整流用）；`__end__`（正常结束；最终 content/stop_reason 以轮次行为准）。
# 心跳用 SSE 注释帧（`: heartbeat`），不打扰 EventSource 的游标状态。


def _agent_sse_frame(event: str, data: str, *, seq: int | None = None) -> str:
    lines = []
    if seq is not None:
        lines.append(f"id: {seq}")
    lines.append(f"event: {event}")
    lines.append(f"data: {data}")
    return chr(10).join(lines) + chr(10) * 2


def _agent_stream_after_seq(request: Request, after: int | None) -> int:
    """续传游标：显式 `?after=` 优先，其次 SSE 标准的 Last-Event-ID 头，都没有从头开始。"""

    if after is not None:
        return int(after)
    last_event_id = request.headers.get("Last-Event-ID", "").strip()
    return int(last_event_id) if last_event_id.isdigit() else 0


def _agent_sse_stream(topic: str, after_seq: int) -> StreamingResponse:
    """把桥订阅包装成 SSE 响应（C 的 subscribe 是同步生成器，线程池直接迭代）。"""

    def generate():
        for item in agent_chat.bridge().subscribe(topic, after_seq=after_seq):
            if isinstance(item, stream_bridge.BridgeGap):
                yield _agent_sse_frame(
                    "__gap__",
                    json.dumps(
                        {
                            "requested_seq": item.requested_seq,
                            "earliest_available": item.earliest_available,
                            "latest_available": item.latest_available,
                        }
                    ),
                )
                return
            if item is stream_bridge.END_SENTINEL:
                yield _agent_sse_frame("__end__", "{}")
                return
            if item is stream_bridge.HEARTBEAT_SENTINEL:
                yield ": heartbeat" + chr(10) + chr(10)
                continue
            yield _agent_sse_frame(item.event, json.dumps(item.data, ensure_ascii=False), seq=item.seq)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/my-agent/turns/{turn_id}/events/stream", response_class=StreamingResponse)
def stream_my_agent_turn_events(
    turn_id: UUID, request: Request, after: int | None = Query(default=None, ge=0)
) -> StreamingResponse:
    """一轮的过程事件流（SSE，增量）。实时层是加速器，权威仍是 `agent_turn_events`。"""

    try:
        agent_chat.ensure_turn_owned(store, turn_id, _request_member_id(request))
    except agent_chat.AgentChatError as error:
        raise _agent_chat_http_error(error) from error
    return _agent_sse_stream(agent_chat.turn_topic(turn_id), _agent_stream_after_seq(request, after))


@app.get("/api/my-agent/conversations/{conversation_id}/stream", response_class=StreamingResponse)
def stream_my_agent_conversation(
    conversation_id: UUID, request: Request, after: int | None = Query(default=None, ge=0)
) -> StreamingResponse:
    """会话流（SSE）：轮次生命周期信号 `turn.created / turn.finished / turn.cancelled`。

    只承载生命周期，不镜像轮次内容——内容走 `turns/{id}/events/stream`；
    这里 gap 后客户端全量重取轮次列表即可（会话流的权威就是轮次行本身）。
    """

    try:
        agent_chat.get_conversation(store, conversation_id, _request_member_id(request))
    except agent_chat.AgentChatError as error:
        raise _agent_chat_http_error(error) from error
    return _agent_sse_stream(
        agent_chat.conversation_topic(conversation_id), _agent_stream_after_seq(request, after)
    )


@app.post("/api/my-agent/turns/{turn_id}/stop", response_model=AgentChatTurn)
def stop_my_agent_turn(turn_id: UUID, request: Request) -> AgentChatTurn:
    """停止一轮：M-1 只改状态，执行体仍会把这一轮跑完（平台还没有到 Agent 的中断通道）。"""

    try:
        return agent_chat.cancel_turn(store, turn_id, _request_member_id(request))
    except agent_chat.AgentChatError as error:
        raise _agent_chat_http_error(error) from error


@app.get("/api/my-agent/turns/{turn_id}/approvals", response_model=list[AgentChatTurnApproval])
def my_agent_turn_approvals(turn_id: UUID, request: Request) -> list[AgentChatTurnApproval]:
    """这一轮的权限请求（页面轮询它来显示"待批准"卡片）。"""

    try:
        return agent_chat.list_turn_approvals(store, turn_id, _request_member_id(request))
    except agent_chat.AgentChatError as error:
        raise _agent_chat_http_error(error) from error


@app.post("/api/my-agent/turns/{turn_id}/approvals/{request_id}", response_model=AgentChatTurnApproval)
def decide_my_agent_turn_approval(
    turn_id: UUID, request_id: str, data: AgentChatTurnApprovalDecision, request: Request
) -> AgentChatTurnApproval:
    """对一张待批准卡片做决定：`once` 批准这一次 / `always` 本次会话都允许 / `reject` 拒绝。

    实测语义（真机三遍）：`reject` 之后执行体**确实没有**做那件事——所以这个按钮是真闸门，不是摆设。
    """

    try:
        return agent_chat.decide_approval(store, turn_id, request_id, _request_member_id(request), data.decision)
    except agent_chat.AgentChatError as error:
        raise _agent_chat_http_error(error) from error


@app.post("/api/agents/{agent_id}/chat-turns/{turn_id}/approvals", response_model=AgentChatTurnApproval, status_code=201)
def report_chat_turn_approval(
    agent_id: str, turn_id: UUID, data: AgentChatTurnApprovalRequest, request: Request
) -> AgentChatTurnApproval:
    """执行体上报一条权限请求（内核从 opencode 的 `permission.asked` 翻译过来）。"""

    try:
        project_id = UUID(agent_chat.turn_project_id(store, turn_id))
    except agent_chat.AgentChatError as error:
        raise _agent_chat_http_error(error) from error
    _require_agent_capability(request, project_id, "chat.run", agent_id)
    try:
        return agent_chat.request_approval(store, turn_id, agent_id, data)
    except agent_chat.AgentChatError as error:
        raise _agent_chat_http_error(error) from error


@app.get("/api/agents/{agent_id}/chat-turns/{turn_id}/approvals/{request_id}", response_model=AgentChatTurnApprovalState)
def read_chat_turn_approval(agent_id: str, turn_id: UUID, request_id: str, request: Request) -> AgentChatTurnApprovalState:
    """执行体轮询：人批了没有（它是阻塞等待的那一方，只能靠轮询）。"""

    try:
        project_id = UUID(agent_chat.turn_project_id(store, turn_id))
    except agent_chat.AgentChatError as error:
        raise _agent_chat_http_error(error) from error
    _require_agent_capability(request, project_id, "chat.run", agent_id)
    try:
        return agent_chat.approval_state(store, turn_id, request_id, agent_id)
    except agent_chat.AgentChatError as error:
        raise _agent_chat_http_error(error) from error


@app.post("/api/agents/{agent_id}/chat-turns/{turn_id}/approvals/{request_id}/expire", response_model=AgentChatTurnApproval)
def expire_chat_turn_approval(agent_id: str, turn_id: UUID, request_id: str, request: Request) -> AgentChatTurnApproval:
    """执行体不再等了（超时）→ 标 EXPIRED，并按 `reject` 回复 opencode（没人批 = 不执行）。"""

    try:
        project_id = UUID(agent_chat.turn_project_id(store, turn_id))
    except agent_chat.AgentChatError as error:
        raise _agent_chat_http_error(error) from error
    _require_agent_capability(request, project_id, "chat.run", agent_id)
    try:
        return agent_chat.expire_approval(store, turn_id, request_id, agent_id)
    except agent_chat.AgentChatError as error:
        raise _agent_chat_http_error(error) from error


@app.post("/api/agents/{agent_id}/chat-turns/claim", response_model=AgentChatTurnClaimResult | None)
def claim_chat_turn(agent_id: str, data: AgentChatTurnClaim, request: Request) -> AgentChatTurnClaimResult | None:
    """执行体取走一条待办轮次；没有待办返回 204/None（轮询空手是常态，不是错误）。"""

    _require_agent_capability(request, data.project_id, "chat.run", agent_id)
    try:
        return agent_chat.claim_turn(store, agent_id, data.project_id, data.lease_seconds)
    except agent_chat.AgentChatError as error:
        raise _agent_chat_http_error(error) from error


@app.post("/api/agents/{agent_id}/chat-turns/{turn_id}/complete", response_model=AgentChatTurn)
def complete_chat_turn(
    agent_id: str, turn_id: UUID, data: AgentChatTurnComplete, request: Request
) -> AgentChatTurn:
    try:
        project_id = UUID(agent_chat.turn_project_id(store, turn_id))
    except agent_chat.AgentChatError as error:
        raise _agent_chat_http_error(error) from error
    _require_agent_capability(request, project_id, "chat.run", agent_id)
    try:
        return agent_chat.complete_turn(store, turn_id, agent_id, data)
    except agent_chat.AgentChatError as error:
        raise _agent_chat_http_error(error) from error


@app.post("/api/agents/{agent_id}/chat-turns/{turn_id}/events", response_model=AgentChatTurnEvent, status_code=201)
def report_chat_turn_event(
    agent_id: str, turn_id: UUID, data: AgentChatTurnEventReport, request: Request
) -> AgentChatTurnEvent:
    try:
        project_id = UUID(agent_chat.turn_project_id(store, turn_id))
    except agent_chat.AgentChatError as error:
        raise _agent_chat_http_error(error) from error
    _require_agent_capability(request, project_id, "chat.run", agent_id)
    try:
        return agent_chat.record_turn_event(store, turn_id, agent_id, data)
    except agent_chat.AgentChatError as error:
        raise _agent_chat_http_error(error) from error


@app.post("/api/my-agent/conversations/{conversation_id}/promote", response_model=AgentConversationPromoteResult, status_code=201)
def promote_my_agent_conversation(
    conversation_id: UUID, data: AgentConversationPromote, request: Request
) -> AgentConversationPromoteResult:
    """转入项目生产（W1.2）：会话产出挂为任务输入附件 + 创建正式任务。

    幂等键服务端派生、**内容寻址**（会话 + 请求指纹）：同一会话同一载荷重复提交
    返回同一任务；载荷变了就是新的转产意图，得到自己的键、允许再建。
    """

    try:
        member_id = _request_member_id(request)
        fingerprint = request_hash(
            {"conversation_id": str(conversation_id), "member_id": member_id, "payload": data.model_dump(mode="json")}
        )
        result = idempotent_model_response(
            f"myagent:promote:{conversation_id}:{fingerprint[:16]}",
            "myagent.conversation.promote",
            fingerprint,
            AgentConversationPromoteResult,
            lambda: agent_chat.promote_conversation(store, conversation_id, member_id, data),
        )
    except agent_chat.AgentChatError as error:
        raise _agent_chat_http_error(error) from error
    except Exception as error:
        raise task_protocol_error(error) from error
    publish_chat_after(result.task.project_id)
    return result


@app.get("/api/agent/artifacts/{artifact_id}/content")
def download_agent_artifact_content(artifact_id: UUID, request: Request) -> Response:
    """Agent 专用下载：执行体把「这一轮的输入文件」拉到本地工作目录。

    与上传那条**刻意不同**：不要求 Artifact 由这个 Agent 创建（输入通常是人上传的），
    只要求该 Agent 对这个项目持有 `artifact.read` —— 项目边界仍然守住。
    """

    try:
        agent_id = _require_agent_header(request)
        artifact = store.get_artifact(artifact_id)
        _require_agent_capability(request, artifact.project_id, "artifact.read", agent_id)
        content = store.get_artifact_content(artifact_id)
    except Exception as error:
        raise task_protocol_error(error) from error
    filename = artifact.name.replace('"', "_").replace(chr(13), "_").replace(chr(10), "_")
    return Response(
        content=content if content is not None else b"",
        media_type="application/octet-stream",
        headers={"Content-Disposition": content_disposition(filename)},
    )


@app.get("/api/agents/{agent_id}/chat-turns/{turn_id}", response_model=AgentChatTurn)
def read_chat_turn(agent_id: str, turn_id: UUID, request: Request) -> AgentChatTurn:
    """执行体读某一轮的状态：**跑的过程中靠它发现"被取消了"**，从而杀掉自己的子进程。

    没有这条路由，"停止"就只能是平台侧改个状态、执行体照跑到底（M-4 之前的口径）。
    """

    try:
        project_id = UUID(agent_chat.turn_project_id(store, turn_id))
    except agent_chat.AgentChatError as error:
        raise _agent_chat_http_error(error) from error
    _require_agent_capability(request, project_id, "chat.run", agent_id)
    try:
        return agent_chat.get_turn(store, turn_id)
    except agent_chat.AgentChatError as error:
        raise _agent_chat_http_error(error) from error


# ---- LLM 渠道与全员免费额度（管理员「渠道」页 + 成员代理端点） ----------------
# 设计口径（交接计划 §LLM）：
#   * api_key 只落库，任何响应只回 key_hint（末 4 位）；
#   * 代理端点 OpenAI 兼容：非流式原样回传上游 JSON，stream=true 走 SSE 逐行透传；
#   * 额度调用前检查（耗尽 429）、调用后按上游 usage 扣减（无 usage 按字符数粗估）；
#   * 检测/测速对上游发最小真实请求（max_tokens=1），成功失败都如实回填。


def _llm_channel_http_error(error: Exception) -> HTTPException:
    """LlmChannelError 稳定码 → HTTP 状态（ERROR_STATUS）；其余错误走 account_error。"""

    if isinstance(error, llm_channels.LlmChannelError):
        return HTTPException(
            status_code=llm_channels.ERROR_STATUS.get(error.code, 400), detail=str(error)
        )
    return account_error(error)


async def _optional_json_body(request: Request) -> dict[str, Any]:
    """可选 JSON 体（检测/测速的 {"model": ...} / {"rounds": N}）；空体 = {}，坏 JSON = 400。"""

    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="invalid_json") from error
    return payload if isinstance(payload, dict) else {}


# ---- 成员端点：平台代理（渠道对成员不可见，成员只见模型与自己的额度） ----


@app.get("/api/llm/quota")
def get_my_llm_quota(request: Request) -> dict[str, Any]:
    """成员看自己的免费额度（懒创建：首次查看即按平台默认额度建档）。"""

    try:
        member = _request_member(request)
        return llm_channels.get_quota(store, member.id, organization_id=str(member.organization_id))
    except Exception as error:
        raise _llm_channel_http_error(error) from error


@app.get("/api/llm/v1/models")
def list_llm_models(request: Request) -> dict[str, Any]:
    """可用模型列表：所有启用渠道的模型合集（按优先级排序去重）。"""

    try:
        member = _request_member(request)
        models = llm_channels.available_models(store, organization_id=str(member.organization_id))
    except Exception as error:
        raise _llm_channel_http_error(error) from error
    return {
        "object": "list",
        "models": models,
        "data": [{"id": name, "object": "model"} for name in models],
    }


@app.post("/api/llm/v1/chat/completions")
async def llm_chat_completions(request: Request) -> Any:
    """OpenAI 兼容代理：调用前查额度（耗尽 429），调用后按上游 usage 扣减并记流水。"""

    member = _request_member(request)
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="invalid_json") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="llm_messages_required")
    try:
        if payload.get("stream"):
            generator = llm_channels.proxy_chat_completions_stream(
                store, member.id, payload, organization_id=str(member.organization_id)
            )
            return StreamingResponse(
                generator,
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        return llm_channels.proxy_chat_completions(
            store, member.id, payload, organization_id=str(member.organization_id)
        )
    except Exception as error:
        raise _llm_channel_http_error(error) from error


# ---- 管理员端点：渠道 CRUD + 检测/测速 + 用量总览 + 成员额度 ----


@app.get("/api/admin/llm-channels")
def list_llm_channels(request: Request) -> dict[str, Any]:
    try:
        actor = _require_admin_member(request)
        return {
            "channels": llm_channels.list_channels(store, str(actor.organization_id)),
            "default_quota_tokens": llm_channels.DEFAULT_QUOTA_TOKENS,
        }
    except Exception as error:
        raise _llm_channel_http_error(error) from error


@app.post("/api/admin/llm-channels", status_code=201)
def create_llm_channel(data: LlmChannelCreate, request: Request) -> dict[str, Any]:
    try:
        actor = _require_admin_member(request)
        return llm_channels.create_channel(
            store,
            name=data.name,
            base_url=data.base_url,
            api_key=data.api_key,
            models=data.models or None,
            priority=data.priority,
            enabled=data.enabled,
            actor_member_id=actor.id,
            organization_id=str(actor.organization_id),
        )
    except Exception as error:
        raise _llm_channel_http_error(error) from error


@app.get("/api/admin/llm-channels/{channel_id}")
def get_llm_channel(channel_id: str, request: Request) -> dict[str, Any]:
    try:
        actor = _require_admin_member(request)
        return llm_channels.get_channel(store, channel_id, str(actor.organization_id))
    except Exception as error:
        raise _llm_channel_http_error(error) from error


@app.patch("/api/admin/llm-channels/{channel_id}")
def update_llm_channel(channel_id: str, data: LlmChannelUpdate, request: Request) -> dict[str, Any]:
    """只改传入字段；api_key 缺省/null = 不变、空串 = 清空（响应只回 key_hint）。"""

    try:
        actor = _require_admin_member(request)
        return llm_channels.update_channel(
            store,
            channel_id,
            organization_id=str(actor.organization_id),
            name=data.name,
            base_url=data.base_url,
            api_key=data.api_key,
            models=data.models,
            priority=data.priority,
            enabled=data.enabled,
            actor_member_id=actor.id,
        )
    except Exception as error:
        raise _llm_channel_http_error(error) from error


@app.delete("/api/admin/llm-channels/{channel_id}", status_code=204)
def delete_llm_channel(channel_id: str, request: Request) -> Response:
    try:
        actor = _require_admin_member(request)
        llm_channels.delete_channel(store, channel_id, organization_id=str(actor.organization_id))
    except Exception as error:
        raise _llm_channel_http_error(error) from error
    return Response(status_code=204)


@app.post("/api/admin/llm-channels/{channel_id}/check")
async def check_llm_channel(channel_id: str, request: Request) -> dict[str, Any]:
    """渠道检测：对上游发一次最小真实请求，成功与否都如实回填 last_check_*。"""

    try:
        actor = _require_admin_member(request)
        payload = await _optional_json_body(request)
        return llm_channels.check_channel(
            store,
            channel_id,
            model=str(payload.get("model") or "") or None,
            organization_id=str(actor.organization_id),
        )
    except Exception as error:
        raise _llm_channel_http_error(error) from error


@app.post("/api/admin/llm-channels/{channel_id}/speed-test")
async def speed_test_llm_channel(channel_id: str, request: Request) -> dict[str, Any]:
    """渠道测速：连续 N 轮最小请求（默认 3，上限 10），逐轮如实汇报并给汇总。"""

    try:
        actor = _require_admin_member(request)
        payload = await _optional_json_body(request)
        rounds = payload.get("rounds")
        return llm_channels.speed_test_channel(
            store,
            channel_id,
            model=str(payload.get("model") or "") or None,
            rounds=int(rounds) if rounds is not None else llm_channels.SPEED_TEST_ROUNDS,
            organization_id=str(actor.organization_id),
        )
    except Exception as error:
        raise _llm_channel_http_error(error) from error


@app.get("/api/admin/llm-usage")
def llm_usage_overview(request: Request) -> dict[str, Any]:
    """用量总览：总量 + 按成员聚合（top N）。"""

    try:
        actor = _require_admin_member(request)
        return llm_channels.usage_overview(store, organization_id=str(actor.organization_id))
    except Exception as error:
        raise _llm_channel_http_error(error) from error


@app.get("/api/admin/llm-quotas/{member_id}")
def get_llm_member_quota(member_id: str, request: Request) -> dict[str, Any]:
    try:
        actor = _require_admin_member(request)
        return llm_channels.get_quota(store, member_id, organization_id=str(actor.organization_id))
    except Exception as error:
        raise _llm_channel_http_error(error) from error


@app.put("/api/admin/llm-quotas/{member_id}")
def set_llm_member_quota(member_id: str, data: LlmQuotaSet, request: Request) -> dict[str, Any]:
    """管理员设置成员免费额度（token 上限；负数 = 不限量）。"""

    try:
        actor = _require_admin_member(request)
        return llm_channels.set_quota(
            store,
            member_id,
            data.token_limit,
            actor_member_id=actor.id,
            organization_id=str(actor.organization_id),
        )
    except Exception as error:
        raise _llm_channel_http_error(error) from error

# ---- 执行体侧 LLM 代理（方案 A：我的智能体对话走平台渠道） ----------------
# opencode 的 provider 指到这里：baseURL = <平台>/api/agent/llm/v1/<agent_id>/<project_id>，
# apiKey = **项目能力令牌**（Bearer）。鉴权复用项目授权机器：令牌必须带 ``llm.invoke``
# 能力、属于该 agent——额度与用量记到 agent 的 ``owner_member_id`` 名下。
# 令牌不走 URL（会进访问日志），opencode 恰好只会发 Authorization 头，正好吻合。


def _agent_llm_context(agent_id: str, project_id: str, request: Request) -> tuple[str, str]:
    """校验执行体身份与 ``llm.invoke`` 能力，返回（额度归属成员, 其组织）。"""

    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="agent_project_token_required")
    try:
        project_uuid = UUID(project_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="invalid_project_id") from error
    try:
        grant = store.resolve_device_project_token(token, project_uuid, "llm.invoke")
    except PermissionError as error:
        detail = str(error)
        status_code = 401 if detail in _INVALID_AGENT_TOKEN_ERRORS else 403
        raise HTTPException(status_code=status_code, detail=detail) from error
    if grant.agent_id != agent_id:
        raise HTTPException(status_code=403, detail="agent_project_agent_mismatch")
    agent = store.db.execute("SELECT owner_member_id FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
    if agent is None:
        raise HTTPException(status_code=404, detail="agent_not_found")
    owner = store.get_member(str(agent["owner_member_id"]))
    return str(owner.id), str(owner.organization_id)


@app.get("/api/agent/llm/v1/{agent_id}/{project_id}/models")
def agent_llm_models(agent_id: str, project_id: str, request: Request) -> dict[str, Any]:
    """执行体侧可见的渠道模型清单（opencode provider 的 models 配置来源）。"""

    _, organization_id = _agent_llm_context(agent_id, project_id, request)
    models = llm_channels.available_models(store, organization_id=organization_id)
    return {"object": "list", "models": models, "data": [{"id": name, "object": "model"} for name in models]}


@app.post("/api/agent/llm/v1/{agent_id}/{project_id}/chat/completions")
async def agent_llm_chat_completions(agent_id: str, project_id: str, request: Request) -> Response:
    """OpenAI 兼容代理（执行体身份）：额度记到 agent 的 owner 名下，口径与成员代理一致。"""

    member_id, organization_id = _agent_llm_context(agent_id, project_id, request)
    try:
        payload = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="invalid_json") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="llm_messages_required")
    try:
        if payload.get("stream"):
            generator = llm_channels.proxy_chat_completions_stream(
                store, member_id, payload, organization_id=organization_id
            )
            return StreamingResponse(
                generator,
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        return JSONResponse(
            llm_channels.proxy_chat_completions(store, member_id, payload, organization_id=organization_id)
        )
    except llm_channels.LlmChannelError as error:
        raise _llm_channel_http_error(error) from error
