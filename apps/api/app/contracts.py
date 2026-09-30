from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from packages.agent_protocol import (
    AgentHeartbeat,
    GatewayCommandResult,
    GatewayEnvelope,
    GatewayEventAck,
    GatewayReplayRequest,
)


class TaskStatus(str, Enum):
    DRAFT = "DRAFT"
    READY = "READY"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    WAITING_REVIEW = "WAITING_REVIEW"
    APPROVED = "APPROVED"
    BLOCKED = "BLOCKED"
    NEEDS_REVISION = "NEEDS_REVISION"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class HandoffStatus(str, Enum):
    PASS = "PASS"
    PASS_WITH_ASSUMPTIONS = "PASS_WITH_ASSUMPTIONS"
    NEEDS_REVISION = "NEEDS_REVISION"
    BLOCKED = "BLOCKED"


class Severity(str, Enum):
    FATAL = "fatal"
    MAJOR = "major"
    MINOR = "minor"


class ReviewerKind(str, Enum):
    MEMBER = "member"
    AGENT = "agent"
    SYSTEM = "system"


class GateStatus(str, Enum):
    OPEN = "OPEN"
    PASSED = "PASSED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    INVALIDATED = "INVALIDATED"


class HandoffType(str, Enum):
    RELAY = "RELAY"
    FANOUT = "FANOUT"
    AGGREGATE = "AGGREGATE"


class HandoffReceiptStatus(str, Enum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class HandoffRecipientType(str, Enum):
    AGENT = "agent"
    MEMBER = "member"
    TEAM = "team"
    AGENT_GROUP = "agent_group"


class DeviceStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"


class DevicePairingStatus(str, Enum):
    PENDING = "PENDING"
    CONSUMED = "CONSUMED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class AgentConnectionStatus(str, Enum):
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    REVOKED = "REVOKED"


class ExecutionMode(str, Enum):
    HEADLESS = "HEADLESS"
    USER_SESSION = "USER_SESSION"
    INTERACTIVE_DESKTOP = "INTERACTIVE_DESKTOP"


ArtifactType = Literal[
    "problem_source",
    "problem_facts",
    "problem_analysis",
    "data_profile",
    "model_spec",
    "code",
    "experiment_plan",
    "run_manifest",
    "result_table",
    "figure",
    "audit_report",
    "review_report",
    "paper_source",
    "compiled_pdf",
    "submission_bundle",
    # CL-1（D-CL-3）：Agent 的回答也是内容——有版本、可审核、能进交付。
    # 追加新值不改变既有类型的语义；交付装配按类型白名单取内容，未知类型自然被忽略。
    "agent_answer",
]


class APIModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)


class ExecutionProfile(APIModel):
    mode: ExecutionMode = ExecutionMode.HEADLESS
    requires_user_session: bool = False
    allow_remote_terminal: bool = False
    allow_desktop_control: bool = False
    network_policy: Literal["deny-by-default", "allow-listed", "unrestricted"] = "deny-by-default"
    auto_retry: bool = False

    @model_validator(mode="after")
    def validate_mode_permissions(self) -> "ExecutionProfile":
        if self.mode == ExecutionMode.HEADLESS and self.requires_user_session:
            raise ValueError("headless_cannot_require_user_session")
        if self.mode == ExecutionMode.HEADLESS and self.allow_desktop_control:
            raise ValueError("headless_cannot_allow_desktop_control")
        if self.mode != ExecutionMode.INTERACTIVE_DESKTOP and self.allow_desktop_control:
            raise ValueError("desktop_control_requires_interactive_profile")
        if self.mode in {ExecutionMode.USER_SESSION, ExecutionMode.INTERACTIVE_DESKTOP} and not self.requires_user_session:
            raise ValueError("interactive_profile_requires_user_session")
        return self


class ProjectCreate(APIModel):
    name: str = Field(min_length=2, max_length=120)
    competition_pack: str = "cumcm-2026"
    problem_code: str | None = None
    description: str = ""
    # 工作区（W-1）：立项目标与目标人数（仅作建队参考，不做加入拦截）
    goal: str = ""
    target_member_count: int | None = Field(default=None, ge=1, le=200)
    task_mode: Literal["manual", "hybrid", "auto"] = "manual"
    organization_id: UUID | None = None
    team_id: UUID | None = None
    created_by: str = "member-001"


class Project(APIModel):
    id: UUID
    organization_id: UUID | None = None
    team_id: UUID | None = None
    created_by: str = "member-001"
    name: str
    competition_pack: str
    problem_code: str | None
    description: str
    goal: str | None = None
    target_member_count: int | None = None
    # 任务推进模式：manual=队长派单（默认）/ hybrid=允许成员认领 / auto=模板全自动（opt-in）
    task_mode: Literal["manual", "hybrid", "auto"] = "manual"
    stage: str
    progress: int
    created_at: datetime
    updated_at: datetime


class TaskBudget(APIModel):
    """任务的执行预算（AIP-1d，见 docs/AIP_1_PLAN.md §6.2）。

    `max_seconds`（单次执行墙钟上限，领取时压租约）与 `max_attempts`（整条任务允许的领取次数）
    是**强制**的；`max_tokens` **只记录不强制**——平台没有 token 计量，界面会明确标注"未强制"，
    要真强制得先让执行体在完成时回报用量。
    """

    max_seconds: int | None = Field(default=None, ge=30, le=86400)
    max_attempts: int | None = Field(default=None, ge=1, le=50)
    max_tokens: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def require_declared_dimension(self) -> "TaskBudget":
        if self.max_seconds is None and self.max_attempts is None and self.max_tokens is None:
            raise ValueError("budget_requires_a_dimension")
        return self


class EvidenceRequirement(APIModel):
    """显式证据要求（AIP-1d）：必须附哪类证据才算完成。

    `evidence_type` 复用既有 Evidence 词表；`min_count` 是这类证据的最少条数。
    空列表（默认）= 不做任何要求，门禁规则不产生 finding。
    """

    evidence_type: Literal["artifact", "run", "event", "external_source"]
    min_count: int = Field(default=1, ge=1, le=20)
    note: str = Field(default="", max_length=200)


class TaskEvidenceGap(APIModel):
    """证据缺口（读时计算，用于展示与门禁）。"""

    evidence_type: Literal["artifact", "run", "event", "external_source"]
    required: int = 1
    present: int = 0
    missing: int = 0
    note: str = ""


class TaskBudgetState(APIModel):
    """预算执行态：领取次数、是否用尽、上限回显，以及**已回报的累计用量**。

    `usage_reported_runs = 0` 表示"没有任何执行体回报过用量"——这时 `tokens_used = 0` 不代表"没花"，
    而是"平台不知道"，界面必须按无数据展示（COST-1）。
    """

    attempts: int = 0
    max_attempts: int | None = None
    exhausted: bool = False
    max_seconds: int | None = None
    max_tokens: int | None = None
    tokens_used: int = 0
    usage_reported_runs: int = 0


class TaskFlags(APIModel):
    """任务列表用的轻量标记（读时聚合，一次查询出全部任务）：缺口与超预算角标。

    为什么不塞进 `Task`：那会让每一条任务在 `_task()` 里各查一次证据与用量（N+1）。
    列表页只想知道"哪些行需要人看一眼"，所以单开一个批量端点。
    """

    task_id: UUID
    evidence_missing: int = 0
    usage_overrun: bool = False
    usage_reported: bool = False


class TaskDetail(APIModel):
    """任务详情（AIP-1d）：任务本体 + 预算执行态 + 证据缺口（后两者都是读时算）。"""

    task: "Task"
    budget_state: TaskBudgetState = Field(default_factory=TaskBudgetState)
    evidence_gaps: list[TaskEvidenceGap] = Field(default_factory=list)


class TaskCreate(APIModel):
    title: str = Field(min_length=2, max_length=180)
    description: str = ""
    stage: str = "modeling"
    assignee: str = "Unassigned"
    # 派单（P1-1）：指派给某个成员，只有其名下设备能领取；None = 谁先轮到谁跑
    assignee_member_id: str | None = Field(default=None, max_length=120)
    priority: Literal["low", "medium", "high", "critical"] = "medium"
    requires_review: bool = True
    allow_future_data: bool = False
    input_artifacts: list[str] = Field(default_factory=list)
    input_handoff_ids: list[UUID] = Field(default_factory=list)
    output_types: list[str] = Field(default_factory=list)
    parent_task_id: UUID | None = None
    dependency_task_ids: list[UUID] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    # 意图对象（AIP-1d）：预算与显式证据要求；默认空 = 不设限、零行为变化
    budget: "TaskBudget | None" = None
    evidence_requirements: list["EvidenceRequirement"] = Field(default_factory=list)
    deadline: datetime | None = None
    information_boundary: dict[str, Any] = Field(default_factory=dict)
    resource_policy: dict[str, Any] = Field(default_factory=dict)
    requires_human_approval: bool | None = None


class ProjectMemberView(APIModel):
    """项目成员目录（派单选择器与成员展示用）。"""

    member_id: str
    role: str
    display_name: str
    email: str
    status: str


class TeamMemberUpsert(APIModel):
    role: str = Field(default="contributor", min_length=3, max_length=40)


class TeamMemberChange(APIModel):
    """加入/移出团队的结果：顺带带入了多少个项目。"""

    member_id: str
    team_id: str
    role: str | None = None
    joined_projects: int = 0
    left_projects: int = 0


class ProjectTeamUpdate(APIModel):
    """项目设置更新：改归团队（team_id=None 表示脱离）、目标/人数、任务推进模式。

    三个新字段都是可选的——老调用只发 team_id 时行为不变（PATCH 语义）。
    """

    team_id: UUID | None = None
    goal: str | None = None
    target_member_count: int | None = Field(default=None, ge=1, le=200)
    task_mode: Literal["manual", "hybrid", "auto"] | None = None


class ProjectMessage(APIModel):
    """项目聊天流的一条：人类消息（message_type=text）或从事件派生的卡片（card）。

    卡片不复刻内容——`content` 是服务端渲染的人话摘要，`ref_*` 指向可跳转的对象。
    """

    id: UUID
    project_id: UUID
    seq: int
    sender_kind: Literal["human", "agent", "system"]
    sender_member_id: str | None = None
    sender_agent_id: str | None = None
    sender_name: str = ""
    content: str
    message_type: Literal["text", "card"] = "text"
    ref_event_id: int | None = None
    ref_artifact_id: UUID | None = None
    ref_task_id: UUID | None = None
    created_at: datetime


class ProjectMessageCreate(APIModel):
    content: str = Field(min_length=1, max_length=4000)
    # 可选：把一条本项目成果物挂在这条消息上（上传文件后"文件已入成果物库"的引用）
    ref_artifact_id: UUID | None = None
    # 可选：把一条本项目任务挂在这条消息上（聊天区 /task 直接建任务后的引用）
    ref_task_id: UUID | None = None


class WorkspaceMember(APIModel):
    """工作区右侧成员概览的一行（人类成员）。"""

    member_id: str
    display_name: str
    email: str
    role: str
    status: str
    open_tasks: int = 0
    running_tasks: int = 0
    agents: int = 0
    agents_online: int = 0
    last_activity: datetime | None = None


class WorkspaceAgent(APIModel):
    """工作区右侧成员概览的一行（Agent 执行体）。"""

    agent_id: str
    display_name: str
    owner_member_id: str
    owner_name: str = ""
    status: str
    connected: bool = False
    last_seen: datetime | None = None
    current_task_id: UUID | None = None
    current_task_title: str | None = None


class WorkspaceTaskBrief(APIModel):
    id: UUID
    title: str
    status: str
    stage: str
    priority: str
    assignee_member_id: str | None = None
    assignee_name: str | None = None
    deadline: datetime | None = None


class WorkspaceTaskSummary(APIModel):
    total: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    open_items: list[WorkspaceTaskBrief] = Field(default_factory=list)


class WorkspaceArtifactBrief(APIModel):
    id: UUID
    name: str
    status: str
    version: int = 1
    kind: str = "deliverable"
    created_at: datetime


class WorkspaceArtifactSummary(APIModel):
    total: int = 0
    approved: int = 0
    pending_review: int = 0
    recent: list[WorkspaceArtifactBrief] = Field(default_factory=list)


class TaskBulkAssign(APIModel):
    """批量派单请求：把一组任务派给同一个成员（空串 = 全部取消指派）。"""

    task_ids: list[UUID] = Field(min_length=1, max_length=200)
    assignee_member_id: str = Field(default="", max_length=120)


class TaskBulkAssignFailure(APIModel):
    task_id: UUID
    reason: str


class TaskBulkAssignResult(APIModel):
    updated: int
    task_ids: list[UUID] = Field(default_factory=list)
    failures: list[TaskBulkAssignFailure] = Field(default_factory=list)


class DeliverableArtifact(APIModel):
    id: UUID
    name: str
    artifact_type: str
    version: int
    status: str
    downstream_allowed: bool = False
    task_id: UUID | None = None
    run_id: UUID | None = None
    created_by: str = ""
    created_by_kind: str = "member"
    created_at: datetime


class DeliverableDocument(DeliverableArtifact):
    """文本类成果物（草稿 / 提交 / 批准三层版本那套）。"""

    layer: Literal["draft", "submitted", "approved"] = "draft"
    has_draft: bool = False


class DeliverableHandoff(APIModel):
    id: UUID
    task_id: UUID | None = None
    sender_agent_id: str = ""
    status: str
    objective: str = ""
    key_conclusions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    created_at: datetime


class DeliverableGate(APIModel):
    id: UUID
    target_type: str
    target_id: str
    status: str
    blocking_count: int = 0
    approved_by: str | None = None
    approved_at: datetime | None = None


class DeliverableReview(APIModel):
    id: UUID
    target_type: str
    target_id: str
    verdict: str
    reviewer: str = ""
    reviewer_kind: str = "member"
    summary: str = ""
    created_at: datetime


class ArtifactSummary(APIModel):
    total: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    recent: list[DeliverableArtifact] = Field(default_factory=list)


class HandoffSummary(APIModel):
    total: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    recent: list[DeliverableHandoff] = Field(default_factory=list)


class DocumentSummary(APIModel):
    total: int = 0
    by_layer: dict[str, int] = Field(default_factory=dict)
    recent: list[DeliverableDocument] = Field(default_factory=list)


class GateSummary(APIModel):
    total: int = 0
    open: int = 0
    items: list[DeliverableGate] = Field(default_factory=list)


class ReviewSummary(APIModel):
    total: int = 0
    recent: list[DeliverableReview] = Field(default_factory=list)


class RiskSummary(APIModel):
    total: int = 0
    open: int = 0


class ProjectDeliverables(APIModel):
    """成果空间聚合：成果物 / 交接 / 文档 / 门禁与复核 / 风险（读时聚合，不落新表）。"""

    artifacts: ArtifactSummary
    handoffs: HandoffSummary
    documents: DocumentSummary
    gates: GateSummary
    reviews: ReviewSummary
    risks: RiskSummary


class WorkspaceViewer(APIModel):
    """当前访问者在该项目里的身份与能做/不能做的事（界面据此隐藏发言框、模式开关）。"""

    member_id: str | None = None
    role: str | None = None
    can_chat: bool = False
    can_manage: bool = False


class ProjectWorkspaceOverview(APIModel):
    """工作区首屏聚合：一个请求拿到右侧成员栏 + 任务/成果摘要 + 最近聊天。"""

    project: Project
    viewer: WorkspaceViewer = WorkspaceViewer()
    members: list[WorkspaceMember] = Field(default_factory=list)
    agents: list[WorkspaceAgent] = Field(default_factory=list)
    tasks: WorkspaceTaskSummary
    artifacts: WorkspaceArtifactSummary
    messages: list[ProjectMessage] = Field(default_factory=list)


class CapabilityCard(APIModel):
    """能力卡（AIP-1a，见 docs/AIP_1_PLAN.md §3.1）：机读的技能描述。

    `inputs` / `outputs` 建议用既有 `ArtifactType` 词表（`data_profile`/`model_spec`/`code`…），
    也允许自由串——不认识的取值不拒绝，界面标灰即可（避免再造一套需要维护的枚举）。
    """

    skill: str = Field(min_length=2, max_length=80)
    version: str = Field(default="", max_length=40)
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    description: str = Field(default="", max_length=400)


class AgentExecutor(APIModel):
    """执行体程序包自报（AIP-1c）：服务端据此补 `package_id`（跨机器相同的那一段身份）。"""

    kind: str = Field(default="", max_length=40)
    version: str = Field(default="", max_length=40)
    package: str = Field(default="", max_length=120)


class TaskCandidate(APIModel):
    """一条任务的候选执行体（AIP-1b）：能跑 / 差一点，以及排序理由。"""

    agent_id: str
    display_name: str = ""
    member_id: str
    online: bool = False
    load: int = 0
    runs_total: int = 0
    succeeded: int = 0
    failed: int = 0
    success_rate: float = 0.5
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    reason: str = ""


class TaskCandidates(APIModel):
    task_id: UUID
    required_capabilities: list[str] = Field(default_factory=list)
    satisfied: list[TaskCandidate] = Field(default_factory=list)
    partial: list[TaskCandidate] = Field(default_factory=list)
    satisfied_total: int = 0
    partial_total: int = 0


class CapabilityPackage(APIModel):
    """同一执行体程序包的多实例聚合（AIP-1c）：`codex@0.9.3` 在三台机器上跑。"""

    package_id: str
    source: Literal["reported", "inferred"] = "inferred"
    instances: int = 0
    instances_online: int = 0
    skills: list[str] = Field(default_factory=list)
    succeeded: int = 0
    failed: int = 0
    total: int = 0
    success_rate: float | None = None
    members: list[str] = Field(default_factory=list)


class CapabilityAgent(APIModel):
    agent_id: str
    display_name: str
    owner_member_id: str | None = None
    owner_name: str = ""
    agent_status: str
    last_seen: datetime | None = None
    capabilities: list[str] = Field(default_factory=list)
    # AIP-1a：技能名 → 版本（未声明版本的技能值为空串）
    skill_versions: dict[str, str] = Field(default_factory=dict)
    cards: list[CapabilityCard] = Field(default_factory=list)
    # 授权范围（`task.claim`/`artifact.write`…）与技能是两套词表，分栏展示
    scope_capabilities: list[str] = Field(default_factory=list)
    # AIP-1c：包 / 实例两段身份；`inferred` 表示平台推断（不是内核上报）
    package_id: str | None = None
    instance_id: str | None = None
    package_source: str = "inferred"
    runs_total: int = 0
    success_rate: float | None = None
    devices: list[dict[str, Any]] = Field(default_factory=list)
    devices_online: int = 0


class UnmetCapabilityTask(APIModel):
    """声明了能力要求、但当前没有任何执行体满足的任务（没人能跑的活）。"""

    task_id: UUID
    title: str
    project_id: UUID
    project_name: str
    required_capabilities: list[str] = Field(default_factory=list)
    missing_capabilities: list[str] = Field(default_factory=list)
    # AIP-1b：不再只给"没人能跑"，同时给"这几台差哪一项"（候选列表）
    candidates: list[TaskCandidate] = Field(default_factory=list)


class CapabilityCatalog(APIModel):
    agents: list[CapabilityAgent] = Field(default_factory=list)
    packages: list[CapabilityPackage] = Field(default_factory=list)
    unmet_tasks: list[UnmetCapabilityTask] = Field(default_factory=list)


class ThroughputDay(APIModel):
    day: str
    total: int = 0
    succeeded: int = 0
    failed: int = 0


class ThroughputMember(APIModel):
    member_id: str
    display_name: str
    total: int = 0
    succeeded: int = 0
    failed: int = 0


class TeamThroughput(APIModel):
    days: int
    daily: list[ThroughputDay] = Field(default_factory=list)
    members: list[ThroughputMember] = Field(default_factory=list)


class MemberWorkload(APIModel):
    """成员工作量视图的一行。"""

    member_id: str
    display_name: str
    email: str
    status: str
    is_admin: bool = False
    assigned_open: int = 0
    running: int = 0
    completed: int = 0
    agents: int = 0
    devices: int = 0
    devices_active: int = 0
    projects: int = 0
    teams: list[str] = Field(default_factory=list)


class ProjectMemberRemoval(APIModel):
    """移除项目成员的结果：连带撤销了多少授权、释放了多少派单。"""

    member_id: str
    released_tasks: int = 0
    revoked_grants: int = 0
    revoked_devices: list[str] = Field(default_factory=list)


class ProjectMemberUpdate(APIModel):
    """改项目内角色。"""

    role: str = Field(min_length=3, max_length=40)


class TaskBoardItem(APIModel):
    """个人任务中心的一行：任务本体 + 展示所需的上下文。"""

    task: Task
    project_name: str = ""
    assignee_member_name: str | None = None
    # 正在执行时是 agant_id（沿用 tasks.assignee 的执行者标注）
    executor_agent_id: str | None = None
    lease_active: bool = False
    # 已过截止时间：界面要给出明确提示（这类任务不会被自动领取）
    deadline_passed: bool = False


class MyTasks(APIModel):
    """个人任务中心：三组视图，全部按"与我有关"聚合。"""

    assigned: list[TaskBoardItem] = Field(default_factory=list)
    running: list[TaskBoardItem] = Field(default_factory=list)
    recent: list[TaskBoardItem] = Field(default_factory=list)


class AttentionItem(APIModel):
    """"需要我关注"的一条（DESKTOP-NOTIFY）：只带通知与跳转需要的最小字段。"""

    task_id: UUID
    title: str
    project_id: UUID
    project_name: str = ""
    status: str
    kind: Literal["assigned", "review"] = "assigned"
    # 指回我时的补充信息（可选）：执行体、是否过期
    executor_agent_id: str | None = None
    deadline_passed: bool = False


class MyAttention(APIModel):
    """桌面通知用的"需要我关注"清单：派给我的任务 + 我该复核的任务。

    与 `/api/my-tasks` 的区别：那个是"我的任务中心"（含我的 Agent 在跑的、最近完成的），
    这个是**给通知用的最小增量视图**（只有未结束的、我该动手的），客户端按 id 去重即可。
    """

    assigned: list[AttentionItem] = Field(default_factory=list)
    review_pending: list[AttentionItem] = Field(default_factory=list)
    assigned_total: int = 0
    review_total: int = 0
    generated_at: datetime


class TaskUpdateRequest(APIModel):
    """`PATCH /api/tasks/{id}` 的 JSON 体。

    状态与负责人仍走查询参数（既有契约不动）；这里只承载**执行方式**——
    没有它，界面就无法把"模板包生成的任务"变成"Agent 能真正跑的任务"，
    用户看到的就是"任务摆在那里、Agent 也不动"。
    """

    resource_policy: dict[str, Any] | None = None
    # 派单：出现在请求体里才生效（含空串=取消指派），未出现则不动
    assignee_member_id: str = Field(default="", max_length=120)
    # 截止时间：同样"出现才生效"，空串=清除
    deadline: str = Field(default="", max_length=64)
    # 意图对象（AIP-1d）：同样"出现才生效"；budget=null 或空 evidence 列表 = 清除
    budget: "TaskBudget | None" = None
    evidence_requirements: list["EvidenceRequirement"] | None = None


class Task(APIModel):
    id: UUID
    project_id: UUID
    title: str
    description: str
    stage: str
    status: TaskStatus
    # 执行者标注：领取时平台写成 agent_id（历史行为，保留）
    assignee: str
    # 派单目标成员（P1-1）：None = 未指派，谁先轮到谁跑
    assignee_member_id: str | None = None
    priority: str
    requires_review: bool
    allow_future_data: bool
    input_artifacts: list[str]
    input_handoff_ids: list[UUID] = Field(default_factory=list)
    output_types: list[str]
    parent_task_id: UUID | None = None
    dependency_task_ids: list[UUID] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    # 意图对象（AIP-1d）：预算（max_seconds/max_attempts 强制，max_tokens 只记录）与显式证据要求
    budget: "TaskBudget | None" = None
    evidence_requirements: list["EvidenceRequirement"] = Field(default_factory=list)
    deadline: datetime | None = None
    information_boundary: dict[str, Any] = Field(default_factory=dict)
    resource_policy: dict[str, Any] = Field(default_factory=dict)
    requires_human_approval: bool = True
    blocked_reason: str | None = None
    updated_at: datetime


class TaskClaimRequest(APIModel):
    agent_id: str = Field(min_length=2, max_length=80)
    lease_seconds: int = Field(default=900, ge=30, le=86400)
    idempotency_key: str = Field(min_length=8, max_length=160)


class AgentTaskClaimRequest(APIModel):
    project_id: UUID | None = None
    stages: list[str] = Field(default_factory=list)
    lease_seconds: int = Field(default=900, ge=30, le=86400)
    idempotency_key: str = Field(min_length=8, max_length=160)


class TaskLease(APIModel):
    lease_id: UUID
    task_id: UUID
    project_id: UUID
    agent_id: str
    lease_token: str
    status: Literal["ACTIVE", "RELEASED", "EXPIRED"]
    issued_at: datetime
    expires_at: datetime


class TaskAssignment(APIModel):
    task: Task
    lease: TaskLease


class TaskLeaseHeartbeat(APIModel):
    agent_id: str = Field(min_length=2, max_length=80)
    project_id: UUID
    extend_seconds: int = Field(default=900, ge=30, le=86400)


class TaskProgressRequest(APIModel):
    agent_id: str = Field(min_length=2, max_length=80)
    lease_token: str = Field(min_length=16, max_length=160)
    status: Literal["RUNNING", "WAITING_REVIEW", "BLOCKED", "FAILED"]
    message: str = ""
    idempotency_key: str = Field(min_length=8, max_length=160)


class TaskResultSubmit(APIModel):
    agent_id: str = Field(min_length=2, max_length=80)
    lease_token: str = Field(min_length=16, max_length=160)
    success: bool
    summary: str = ""
    output_artifact_ids: list[UUID] = Field(default_factory=list)
    handoff_id: UUID | None = None
    idempotency_key: str = Field(min_length=8, max_length=160)


class TaskResult(APIModel):
    task: Task
    lease: TaskLease
    accepted: bool
    message: str = ""


class RunCreate(APIModel):
    task_id: UUID | None = None
    agent_id: str = Field(min_length=2, max_length=80)
    source_commit: str | None = None
    input_artifact_ids: list[UUID] = Field(default_factory=list)
    environment_image_digest: str | None = None
    dependency_lock: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    random_seed: int | None = None
    model_provider: str | None = None
    model_name: str | None = None
    tool_versions: dict[str, str] = Field(default_factory=dict)
    network_policy: Literal["deny-by-default", "allow-listed", "unrestricted"] = "deny-by-default"
    execution_profile: ExecutionProfile = Field(default_factory=ExecutionProfile)
    data_access_policy: dict[str, Any] = Field(default_factory=dict)
    observed_input_files: list[str] = Field(default_factory=list)
    idempotency_key: str = Field(min_length=8, max_length=160)

    @model_validator(mode="after")
    def align_network_policy(self) -> "RunCreate":
        network_explicit = "network_policy" in self.model_fields_set
        profile_explicit = "execution_profile" in self.model_fields_set
        if network_explicit and profile_explicit and self.network_policy != self.execution_profile.network_policy:
            raise ValueError("run_network_policy_mismatch")
        if self.network_policy == "deny-by-default" and self.execution_profile.network_policy != "deny-by-default":
            object.__setattr__(self, "network_policy", self.execution_profile.network_policy)
        elif self.execution_profile.network_policy == "deny-by-default" and self.network_policy != "deny-by-default":
            # Preserve the legacy flat field for callers that predate execution_profile.
            object.__setattr__(self.execution_profile, "network_policy", self.network_policy)
        return self
    # 执行归属：执行体自报的设备 id（服务端会校验它与 Agent 对得上，member_id 一律服务端推导）
    device_id: str | None = Field(default=None, max_length=120)


class RunUsage(APIModel):
    """执行用量回报（COST-1）：token 由执行体回报（有则填），耗时优先自报、缺省由平台观测。

    为什么分这么细：平台**没有**独立的 token 计量能力，只有执行体自己知道它花了多少；
    所以 `source` 记录出处（`codex-jsonl` / `platform-observed`），让人能分辨"回报"与"平台观测"。
    """

    input_tokens: int | None = Field(default=None, ge=0, le=100_000_000)
    output_tokens: int | None = Field(default=None, ge=0, le=100_000_000)
    total_tokens: int | None = Field(default=None, ge=0, le=100_000_000)
    turns: int | None = Field(default=None, ge=0, le=100_000)
    # 执行耗时（秒）：执行体自报优先，缺省由 started_at/completed_at 兜底
    seconds: float | None = Field(default=None, ge=0, le=604800)
    # token 的出处（`codex-jsonl` / `agent-reported` / `platform-observed`）
    source: str = Field(default="", max_length=60)
    # 耗时的出处：`agent`（自报）或 `platform`（平台按开始/完成时间观测）——与 token 出处分开记
    seconds_source: str = Field(default="", max_length=20)

    @model_validator(mode="after")
    def fill_total(self) -> "RunUsage":
        if self.total_tokens is None and (self.input_tokens is not None or self.output_tokens is not None):
            object.__setattr__(self, "total_tokens", int(self.input_tokens or 0) + int(self.output_tokens or 0))
        return self


class RunComplete(APIModel):
    success: bool
    stdout: str = ""
    stderr: str = ""
    summary: str = ""
    output_artifact_ids: list[UUID] = Field(default_factory=list)
    observed_input_files: list[str] = Field(default_factory=list)
    # 执行用量（COST-1，可选）：报了就参与 token/耗时预算判定，不报就只按平台观测计时
    usage: "RunUsage | None" = None
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=160)


class Run(APIModel):
    id: UUID
    project_id: UUID
    task_id: UUID | None
    agent_id: str
    # 执行归属（迁移 019）
    device_id: str | None = None
    member_id: str | None = None
    status: Literal["CREATED", "RUNNING", "SUCCEEDED", "FAILED", "BLOCKED"]
    source_commit: str | None
    input_artifact_ids: list[UUID]
    environment_image_digest: str | None
    dependency_lock: str | None
    parameters: dict[str, Any]
    random_seed: int | None
    model_provider: str | None
    model_name: str | None
    tool_versions: dict[str, str]
    network_policy: Literal["deny-by-default", "allow-listed", "unrestricted"]
    execution_profile: ExecutionProfile
    data_access_policy: dict[str, Any]
    observed_input_files: list[str]
    output_artifact_ids: list[UUID]
    stdout: str
    stderr: str
    summary: str
    information_boundary: dict[str, Any]
    # 执行用量（COST-1）：空对象 = 未回报（通用 CLI 执行体没有用量可报）
    usage: "RunUsage" = Field(default_factory=lambda: RunUsage())
    started_at: datetime
    completed_at: datetime | None


class ImportRequest(APIModel):
    source_path: str = Field(min_length=1, max_length=1000)
    created_by: str = "importer"
    dry_run: bool = False


class DeliveryChecklistRequest(APIModel):
    """交付检查；未提供正文时使用最新已批准论文源文件。"""

    paper_text: str | None = None


class DriveImportRequest(APIModel):
    """把个人云盘文件导入项目空间。"""

    file_id: str = Field(min_length=1, max_length=64)
    task_id: UUID | None = None


class DriveDirectoryCreate(APIModel):
    """新建目录（FM-1）。`parent_id` 为空 = 建在根目录下。"""

    parent_id: str | None = None
    name: str = Field(min_length=1, max_length=240)


class DriveNodePatch(APIModel):
    """改名。`expected_revision` 给了就做并发校验（对不上返回冲突，不盲目覆盖）。"""

    name: str = Field(min_length=1, max_length=240)
    expected_revision: int | None = None


class DriveNodeMove(APIModel):
    parent_id: str | None = None
    expected_revision: int | None = None


class FileTransferDriveToWorkspace(APIModel):
    """云盘 → Agent 工作区（显式复制）。"""

    node_id: str = Field(min_length=4, max_length=80)
    workspace_id: str = Field(min_length=4, max_length=80)
    relative_path: str | None = None
    overwrite: bool = False


class FileTransferWorkspaceToDrive(APIModel):
    """Agent 工作区 → 云盘（Agent 先传到传输会话，再由人存进云盘）。"""

    workspace_id: str = Field(min_length=4, max_length=80)
    relative_path: str = Field(min_length=1, max_length=1024)


class FileTransferSave(APIModel):
    transfer_id: str = Field(min_length=4, max_length=80)
    parent_id: str | None = None
    name: str | None = Field(default=None, max_length=240)


class FileAccessGrantCreate(APIModel):
    """给一个 Agent 授权云盘里的某些文件（FM-5）。默认只读 + 可导入项目。"""

    agent_id: str = Field(min_length=2, max_length=80)
    device_id: str | None = None
    project_id: UUID
    node_id: str | None = None
    scope_type: Literal["file", "folder", "drive"] = "file"
    capabilities: list[str] = Field(default_factory=list)
    include_future_nodes: bool = False
    task_id: str | None = None
    run_id: str | None = None
    conversation_id: str | None = None
    turn_id: str | None = None
    expires_in_seconds: int = Field(default=7 * 24 * 3600, ge=300, le=30 * 24 * 3600)


class FileAccessGrantDecision(APIModel):
    """撤销/续期共用的请求体。"""

    reason: str = Field(default="revoked_by_member", max_length=200)
    expires_in_seconds: int = Field(default=7 * 24 * 3600, ge=300, le=30 * 24 * 3600)


class FileLeaseExchange(APIModel):
    """Agent 用项目令牌换一个短期 lease（明文只在这次响应里出现一次）。"""

    grant_id: str = Field(min_length=4, max_length=80)
    run_id: str | None = None
    ttl_seconds: int = Field(default=900, ge=60, le=3600)


class WorkspaceRegisterRequest(APIModel):
    """Agent 上报自己的工作区（心跳时带；`workspace_identity` 是**路径的哈希**，不是路径本身）。"""

    display_name: str = Field(min_length=1, max_length=120)
    workspace_identity: str = Field(min_length=4, max_length=80)
    device_id: str | None = None
    project_id: UUID | None = None
    kind: Literal["cloud", "desktop"] = "desktop"
    protected_paths: list[str] = Field(default_factory=list)
    policy_version: str = "1"


class WorkspaceOperationCreate(APIModel):
    """浏览器入队一个工作区操作（Agent 领取后执行）。"""

    operation_type: Literal["list", "stat", "mkdir", "upload", "download", "rename", "move", "copy", "delete", "extract"]
    relative_path: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=6, max_length=160)
    expected_revision: str | None = None
    fail_when_offline: bool = False


class WorkspaceOperationClaim(APIModel):
    workspace_id: str | None = None
    limit: int = Field(default=4, ge=1, le=16)
    lease_seconds: int = Field(default=120, ge=10, le=3600)


class WorkspaceOperationProgress(APIModel):
    progress: dict[str, Any] = Field(default_factory=dict)


class WorkspaceOperationComplete(APIModel):
    success: bool
    result: dict[str, Any] | None = None
    error_code: str | None = Field(default=None, max_length=120)
    error_message: str | None = Field(default=None, max_length=500)


class WorkspaceTransferCreate(APIModel):
    source_type: Literal["drive", "workspace", "temp"]
    target_type: Literal["drive", "workspace", "temp"]
    operation_id: str | None = None
    workspace_id: str | None = None
    source_id: str | None = None
    source_hash: str | None = None
    target_id: str | None = None
    expected_size: int = Field(default=0, ge=0)
    expected_hash: str | None = None
    ttl_seconds: int = Field(default=3600, ge=60, le=86400)


class DriveExtractionCreate(APIModel):
    """解压：把云盘里的归档解成一个新目录（FM-2）。"""

    node_id: str = Field(min_length=1, max_length=64)
    target_parent_id: str | None = None
    directory_name: str | None = Field(default=None, max_length=240)


class DriveNodeCopy(APIModel):
    """复制到某目录；`name` 不填就沿用原名（同名时自动加「-副本」后缀，不覆盖）。"""

    parent_id: str | None = None
    name: str | None = Field(default=None, max_length=240)


class KbCreate(APIModel):
    """创建知识库：project_id 为空即个人知识库。"""

    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    project_id: UUID | None = None
    shared: bool = False


class KbShareCreate(APIModel):
    """把个人知识库分享给其他成员。"""

    member_id: str = Field(min_length=2, max_length=120)
    permission: Literal["read", "write"] = "read"


class KbDocumentCreate(APIModel):
    """向知识库登记文档（Markdown 文本）。"""

    title: str = Field(min_length=1, max_length=300)
    content_md: str = Field(min_length=1)
    source_type: Literal["manual", "drive", "artifact", "convert"] = "manual"
    source_artifact_id: UUID | None = None
    source_drive_file_id: str | None = Field(default=None, max_length=64)


class AiSettingsUpdate(APIModel):
    """成员级 AI 凭据（LLM/Embedding/MinerU）。"""

    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    embedding_api_key: str = ""
    embedding_base_url: str = ""
    embedding_model: str = ""
    embedding_dimensions: int = Field(default=1024, ge=64, le=8192)
    mineru_api_key: str = ""


class KbQueryRequest(APIModel):
    """知识库检索问答。"""

    question: str = Field(min_length=1, max_length=4000)
    mode: Literal["hyper", "hyper-lite", "graph", "naive", "llm"] = "hyper"


class KbIndexRequest(APIModel):
    """触发文档索引。"""

    doc_ids: list[str] = Field(default_factory=list)


class ConversationCreate(APIModel):
    """创建会话（普通对话或知识库检索）。"""

    title: str = Field(min_length=1, max_length=200)
    mode: Literal["chat", "rag"] = "chat"
    kb_id: str | None = None


class MessageCreate(APIModel):
    """向会话追加消息。"""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=20000)
    sources: list[dict[str, Any]] = Field(default_factory=list)


class ConvertEnqueueRequest(APIModel):
    """入队 MinerU 转换任务。"""

    source_type: Literal["drive_file", "upload"]
    source_id: str = Field(min_length=1, max_length=64)
    file_name: str = Field(min_length=1, max_length=300)


class AiChatRequest(APIModel):
    """普通对话请求：直连成员配置的 LLM。"""

    messages: list[dict[str, str]] = Field(min_length=1)
    system_prompt: str | None = None
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    stream: bool = False


class ConvertToKbRequest(APIModel):
    """把转换结果写入知识库。"""

    kb_id: str = Field(min_length=1, max_length=64)


class DeliveryCompileRequest(APIModel):
    """编译论文源码为 PDF；未提供 source 时使用最新已批准 paper_source。"""

    source: str | None = None
    artifact_name: str = "main.pdf"
    actor: str | None = None
    actor_kind: ReviewerKind = ReviewerKind.MEMBER


class DeliveryBundleRequest(APIModel):
    """生成提交包（装配 + 检查 + 清单），并提交待审。"""

    label: str = ""
    actor: str | None = None
    actor_kind: ReviewerKind = ReviewerKind.MEMBER
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=160)


class BoundaryGateRequest(APIModel):
    """信息边界审计 Gate：按 pack 规则审计运行事实。"""

    task_id: UUID | None = None
    actor: str | None = None
    actor_kind: ReviewerKind = ReviewerKind.AGENT
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=160)


class CompetitionPackApplyRequest(APIModel):
    """把竞赛 pack 的四问 DAG 与模板物化到项目。"""

    problem_code: str | None = Field(default=None, max_length=8)
    questions: list[int] | None = None
    # 已弃用、服务端忽略（W2.2 可信归属）：物化者身份由登录会话注入，
    # 保留字段只为兼容既有客户端，传什么都不改变归属。
    created_by: str = "pack-materializer"
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=160)


class DocumentSubmitRequest(APIModel):
    """草稿 → 提交（冻结待审）。"""

    actor: str | None = None
    actor_kind: ReviewerKind = ReviewerKind.MEMBER
    evidence_ids: list[UUID] = Field(default_factory=list)


class DocumentCommentRequest(APIModel):
    """文档评论/建议。"""

    body: str = Field(min_length=1, max_length=2000)
    kind: Literal["comment", "suggestion"] = "comment"
    anchor: str | None = Field(default=None, max_length=200)
    actor: str | None = None
    actor_kind: ReviewerKind = ReviewerKind.MEMBER


class DocumentSnapshotRequest(APIModel):
    """文档快照（当前版本的检查点）。"""

    label: str = ""
    actor: str | None = None
    actor_kind: ReviewerKind = ReviewerKind.MEMBER


class DocumentRelationRequest(APIModel):
    """结论/图表/运行/任务 ↔ 文档段落关系。"""

    target_type: Literal["artifact", "run", "figure", "result_table", "task"]
    target_id: UUID
    paragraph: str = Field(min_length=1, max_length=300)
    note: str = ""
    actor: str | None = None
    actor_kind: ReviewerKind = ReviewerKind.MEMBER


class DocumentMergeRequest(APIModel):
    """合并确认：选定一方版本内容派生出新的草稿版本。"""

    source_revision: int = Field(ge=1)
    note: str = ""
    git_commit: str | None = None
    actor: str | None = None
    actor_kind: ReviewerKind = ReviewerKind.MEMBER


class DocumentReviseRequest(APIModel):
    """从已有版本派生新的草稿版本。"""

    actor: str | None = None
    actor_kind: ReviewerKind = ReviewerKind.MEMBER
    description: str | None = None


class CompetitionPackReviewRequest(APIModel):
    """把 pack 校验结果落成平台 Review + Gate。"""

    scope: Literal["full", "information_boundary"] = "full"
    reviewer: str = "modular-audit"
    target_type: Literal["task", "artifact"] | None = None
    target_id: UUID | None = None
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=160)


class ImportSummary(APIModel):
    project_id: UUID
    source_path: str
    discovered_files: int
    imported_artifacts: int
    skipped_artifacts: int
    created_tasks: int
    artifact_ids: list[UUID]
    warnings: list[str] = Field(default_factory=list)


class HandoffImportReport(APIModel):
    """现有 C 题交接包的可解释导入报告。"""

    project_id: UUID
    source_path: str
    workspace_path: str
    layout_detected: bool
    declared_files: int
    imported_artifacts: int
    skipped_artifacts: int
    unknown_files: list[str] = Field(default_factory=list)
    hash_mismatches: list[str] = Field(default_factory=list)
    lossless: bool = False
    created_tasks: int = 0
    artifact_ids: list[UUID] = Field(default_factory=list)
    classification: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class HandoffCreate(APIModel):
    task_id: UUID
    receiver: str | dict[str, Any] | list[dict[str, Any]] = "Team"
    status: HandoffStatus = HandoffStatus.PASS_WITH_ASSUMPTIONS
    objective: str
    completed: list[str] = Field(default_factory=list)
    input_artifacts: list[str] = Field(default_factory=list)
    output_artifacts: list[str] = Field(default_factory=list)
    key_conclusions: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    risks: list[dict[str, Any]] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    requires_human_approval: bool = True
    schema_version: str = "1.0"
    handoff_type: HandoffType = HandoffType.RELAY
    input_handoff_ids: list[UUID] = Field(default_factory=list)
    revision_of_handoff_id: UUID | None = None
    idempotency_key: str | None = None


class Handoff(APIModel):
    id: UUID
    project_id: UUID
    task_id: UUID
    sender_agent_id: str
    receiver: str | dict[str, Any] | list[dict[str, Any]]
    status: HandoffStatus
    objective: str
    completed: list[str]
    input_artifacts: list[str]
    output_artifacts: list[str]
    key_conclusions: list[str]
    assumptions: list[str]
    evidence_refs: list[str]
    open_questions: list[str]
    risks: list[dict[str, Any]]
    next_actions: list[str]
    requires_human_approval: bool
    schema_version: str = "1.0"
    handoff_type: HandoffType = HandoffType.RELAY
    input_handoff_ids: list[UUID] = Field(default_factory=list)
    revision_of_handoff_id: UUID | None = None
    revision_number: int = Field(default=1, ge=1)
    receipt_status: HandoffReceiptStatus = HandoffReceiptStatus.PENDING
    received_by: str | None = None
    received_at: datetime | None = None
    decision_reason: str | None = None
    decision_findings: list[dict[str, Any]] = Field(default_factory=list)
    receipts: list["HandoffReceipt"] = Field(default_factory=list)
    idempotency_key: str | None = None
    created_at: datetime


class HandoffReceipt(APIModel):
    id: UUID
    handoff_id: UUID
    receiver_type: HandoffRecipientType
    receiver_id: str
    status: HandoffReceiptStatus = HandoffReceiptStatus.PENDING
    received_by: str | None = None
    received_at: datetime | None = None
    decision_reason: str | None = None
    decision_findings: list[dict[str, Any]] = Field(default_factory=list)
    idempotency_key: str | None = None
    created_at: datetime


Handoff.model_rebuild()


class HandoffAcceptRequest(APIModel):
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=160)


class HandoffDecisionRequest(APIModel):
    reason: str = Field(min_length=1, max_length=2000)
    findings: list[dict[str, Any]] = Field(default_factory=list)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=160)


class EvidenceCreate(APIModel):
    claim: str = Field(min_length=1, max_length=1000)
    evidence_type: Literal["artifact", "run", "event", "external_source"]
    artifact_id: UUID | None = None
    run_id: UUID | None = None
    source_ref: str | None = None
    created_by: str = "member-001"


class WorkflowUpsert(APIModel):
    """创建/追加工作流包版本：definition 形状由 docs/WORKFLOW_SCHEMA.md v1 权威定义，
    服务端按其 §4 八条校验（错误逐条列出，绝不静默丢弃）。"""

    definition: dict[str, Any]


class WorkflowRunStart(APIModel):
    """应用工作流：绑定冻结版本（缺省 = 当前已发布版本）并物化节点为任务骨架。"""

    workflow_id: UUID
    version_id: UUID | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)


class ArtifactReceipt(APIModel):
    """产物溯源 receipt（W2.4，docs/RECEIPT_FORMAT.md §2，receipt_version=1）。

    回答"这个成果物是哪次工具调用产出的"：B（agentd）上传时随 ArtifactCreate 提交，
    平台校验结构后落库并在 `project.artifact.uploaded` 事件的 payload.receipt 里携带。
    哈希是 SHA-256 前 16 位小写 hex——字段名如实叫 `*_hash`（纠正 deer-flow 的
    `*_sha256` 名实不符），长度进文档不进名字。
    """

    receipt_version: int = 1
    tool_name: str = Field(min_length=1, max_length=128)
    tool_call_id: str = Field(default="", max_length=128)
    args_hash: str = Field(pattern=r"^[0-9a-f]{16}$")
    output_hash: str = Field(pattern=r"^[0-9a-f]{16}$")
    output_bytes: int = Field(default=0, ge=0)
    truncated: bool = False


class ArtifactCreate(APIModel):
    name: str = Field(min_length=1, max_length=240)
    artifact_type: ArtifactType
    description: str = ""
    content_hash: str | None = None
    source_path: str | None = None
    task_id: UUID | None = None
    run_id: UUID | None = None
    status: Literal["DRAFT", "PENDING_REVIEW", "APPROVED", "REJECTED"] = "DRAFT"
    data_policy: dict[str, Any] = Field(default_factory=dict)
    input_artifact_ids: list[UUID] = Field(default_factory=list)
    git_commit: str | None = None
    snapshot_ref: str | None = None
    # 溯源 receipt（W2.4）：执行体产出时带上；人工上传没有就留空（历史行为零变化）。
    receipt: ArtifactReceipt | None = None
    mime_type: str | None = None


class Artifact(APIModel):
    id: UUID
    project_id: UUID
    name: str
    artifact_type: ArtifactType
    description: str
    content_hash: str
    version: int
    status: str
    source_path: str | None
    task_id: UUID | None
    run_id: UUID | None
    created_by: str
    created_at: datetime
    data_policy: dict[str, Any]
    input_artifact_ids: list[UUID] = Field(default_factory=list)
    git_commit: str | None = None
    snapshot_ref: str | None = None
    created_by_kind: ReviewerKind = ReviewerKind.MEMBER
    approved_by: str | None = None
    approved_at: datetime | None = None
    downstream_allowed: bool = False
    storage_key: str | None = None
    size_bytes: int | None = None
    mime_type: str | None = None
    immutable: bool = False
    # 溯源（W2.4）：receipt_version=0/None 表示没有 receipt（历史行、人工上传、receipt 被拒收）
    receipt: ArtifactReceipt | None = None
    parent_artifact_id: UUID | None = None
    archived_at: datetime | None = None


class AgentRegister(APIModel):
    agent_id: str = Field(min_length=2, max_length=80)
    display_name: str = Field(min_length=2, max_length=120)
    owner_member_id: str = "member-001"
    model_provider: str = "local"
    model_name: str = "unspecified"
    supported_tools: list[str] = Field(default_factory=list)
    supported_languages: list[str] = Field(default_factory=lambda: ["python"])
    # AIP-1a：能力卡（技能名 + 版本 + 输入 + 输出）。老内核不发这个字段照旧工作，
    # 平台会把卡片技能与 supported_tools 合并成技能集合。
    capability_cards: list["CapabilityCard"] = Field(default_factory=list)
    # AIP-1c：执行体程序包自报（kind/version），服务端据此填 package_id；不发则平台推断
    executor: "AgentExecutor | None" = None
    max_concurrency: int = Field(default=1, ge=1, le=32)
    local_workspace: str | None = None
    network_policy: str = "deny-by-default"

    @field_validator("capability_cards")
    @classmethod
    def validate_cards(cls, cards: list["CapabilityCard"]) -> list["CapabilityCard"]:
        """非法技能名直接 422（`capability_skill_invalid`），不让脏词表进库。"""

        from . import skill_match

        for card in cards:
            if skill_match.normalize_skill_id(card.skill) is None:
                raise ValueError("capability_skill_invalid")
        if len(cards) > 32:
            raise ValueError("capability_cards_too_many")
        return cards

    @field_validator("supported_tools")
    @classmethod
    def validate_tools(cls, tools: list[str]) -> list[str]:
        from . import skill_match

        for item in tools:
            if skill_match.normalize_skill_id(skill_match.split_requirement(item)[0]) is None:
                raise ValueError("capability_skill_invalid")
        return tools


class Agent(APIModel):
    agent_id: str
    display_name: str
    owner_member_id: str
    model_provider: str
    model_name: str
    supported_tools: list[str]
    supported_languages: list[str]
    max_concurrency: int
    # 注册与心跳的响应里是执行体自报的**绝对路径**（设备侧要回读对账）；成员列表里恒为 None，
    # 只给 `workspace_identity`（路径的 sha256 前 12 位）。见 `app/path_privacy.py`。
    local_workspace: str | None
    workspace_identity: str | None = None
    network_policy: str
    status: Literal["online", "idle", "offline"]
    last_seen: datetime
    # AIP-1a/1c：能力卡与包/实例两段身份（老行由平台推断，`package_source` 标注来源）
    capability_cards: list["CapabilityCard"] = Field(default_factory=list)
    package_id: str | None = None
    instance_id: str | None = None
    package_source: str = "inferred"


class DevicePairingCreate(APIModel):
    organization_id: UUID
    expires_in_seconds: int = Field(default=900, ge=60, le=3600)


class DevicePairing(APIModel):
    id: UUID
    organization_id: UUID
    created_by: str
    status: DevicePairingStatus
    expires_at: datetime
    device_id: str | None = None
    consumed_at: datetime | None = None
    created_at: datetime
    # The raw code is returned only by the creation response and is never persisted.
    pairing_code: str | None = None
    # The raw challenge is returned only by the creation response and is never persisted.
    challenge: str | None = None


class DeviceRegisterRequest(APIModel):
    pairing_code: str = Field(min_length=16, max_length=240)
    pairing_id: UUID
    challenge: str = Field(min_length=32, max_length=240)
    challenge_signature: str = Field(min_length=80, max_length=2048)
    agent_id: str = Field(min_length=2, max_length=80)
    device_id: str = Field(min_length=2, max_length=80)
    device_name: str = Field(min_length=2, max_length=120)
    public_key: str = Field(min_length=16, max_length=8192)
    platform: str = Field(default="unknown", min_length=1, max_length=80)
    agent_version: str = Field(default="unknown", min_length=1, max_length=80)
    capabilities: list[str] = Field(default_factory=list)


class DocumentDraft(APIModel):
    """协作文档的服务端草稿（CL-6）：刷新/换设备后编辑不丢，冲突由 revision 判定。"""

    artifact_id: UUID
    project_id: UUID
    content: str
    revision: int = Field(ge=1)
    updated_by: str
    updated_at: datetime
    created_at: datetime


class DocumentDraftUpdate(APIModel):
    content: str
    # 上次看到的修订号；与服务端不一致即冲突（首次保存传 0）
    base_revision: int = Field(default=0, ge=0)


class ArtifactConsumer(APIModel):
    """把这份成果物当输入的任务（领取校验会检查它们是否已批准）。"""

    task_id: UUID
    title: str
    status: str


class ArtifactLineageEntry(APIModel):
    artifact_id: UUID
    version: int
    status: str
    content_hash: str | None = None
    created_at: datetime
    is_current: bool = False


class ArtifactDetail(APIModel):
    """成果物全貌（CL-3）：从哪来、有几版、谁批的、被谁用、边界与孤儿状态。"""

    artifact: Artifact
    lineage: list[ArtifactLineageEntry] = Field(default_factory=list)
    consumers: list[ArtifactConsumer] = Field(default_factory=list)
    reviews: list[Review] = Field(default_factory=list)
    gate: Gate | None = None
    source_task: dict[str, Any] | None = None
    source_run: dict[str, Any] | None = None
    events: list[Event] = Field(default_factory=list)
    orphan: bool = False
    orphan_reason: str | None = None


class DeviceRuntimeState(APIModel):
    """设备最近一次心跳上报的运行态（B1）。

    字段与 `packages.agent_protocol.AgentHeartbeat` 对齐：心跳是设备自报的，
    平台只落库并展示，不用它做任何授权判定。
    """

    device_id: str
    connection_id: str | None = None
    agent_version: str | None = None
    adapter_versions: dict[str, str] = Field(default_factory=dict)
    capabilities: list[str] = Field(default_factory=list)
    running_run_ids: list[str] = Field(default_factory=list)
    local_queue_length: int = Field(default=0, ge=0)
    user_session_state: str = "unknown"
    resource_summary: dict[str, Any] = Field(default_factory=dict)
    reported_at: datetime


class Device(APIModel):
    device_id: str
    organization_id: UUID
    agent_id: str
    owner_member_id: str
    device_name: str
    public_key_fingerprint: str
    platform: str
    agent_version: str
    capabilities: list[str]
    status: DeviceStatus
    token_version: int = Field(default=1, ge=1)
    created_at: datetime
    last_seen: datetime | None = None
    revoked_at: datetime | None = None
    token_rotated_at: datetime | None = None
    # B4：最近一次心跳运行态。只有列表端填充，单设备读取路径保持 None（省一次查询）。
    runtime: DeviceRuntimeState | None = None


class DeviceCredential(APIModel):
    device: Device
    device_token: str


class AgentSelfView(APIModel):
    """B3：设备 Token 换自身配置（`GET /api/agent/me`）。

    只返回该设备自己的信息与其被授权项目的清单；不含任何 Token 明文。
    """

    device: Device
    runtime: DeviceRuntimeState | None = None
    grants: list["DeviceProjectGrant"] = Field(default_factory=list)
    runtime_policy: dict[str, Any] = Field(default_factory=dict)


class DeviceRevokeRequest(APIModel):
    reason: str = Field(default="revoked_by_member", min_length=1, max_length=500)


class DeviceTokenRotateRequest(APIModel):
    reason: str = Field(default="rotated_by_member", min_length=1, max_length=500)


class DeviceProjectGrantCreate(APIModel):
    device_id: str = Field(min_length=2, max_length=80)
    capabilities: list[str] = Field(default_factory=lambda: [
        # 「对话」（MY-AGENT）：单纯对话轮次的领取/回传，与任务领取分开授权
        "chat.run",
        # 「LLM 渠道」（方案 A）：执行体上的 opencode 经平台代理消费渠道模型，
        # 额度记到 agent 的 owner 名下。旧授权串没有它 ⇒ 需要重签授权
        "llm.invoke",
        "task.claim",
        "task.lease",
        "task.progress",
        "task.result",
        "artifact.read",
        "artifact.write",
        "run.create",
        "run.complete",
        "run.event",
        "handoff.create",
        "handoff.accept",
        "handoff.reject",
        "review.submit",
        # 工作区文件服务（FM-3）：读/写/领取分开授权。**旧授权串里没有这三项**，
        # 已配对设备的授权需要重新签发才会拿到（见 FM-3 交接的部署注意事项）。
        "workspace.files.read",
        "workspace.files.write",
        "workspace.files.claim",
    ])
    expires_in_seconds: int = Field(default=86400, ge=60, le=2592000)


class DeviceProjectGrant(APIModel):
    id: UUID
    device_id: str
    agent_id: str
    project_id: UUID
    capabilities: list[str]
    granted_by: str
    expires_at: datetime
    revoked_at: datetime | None = None
    created_at: datetime


class DeviceProjectCredential(APIModel):
    grant: DeviceProjectGrant
    project_token: str


class AgentConnection(APIModel):
    connection_id: str
    device_id: str
    agent_id: str
    session_id: str
    transport: Literal["websocket", "long_poll"]
    status: AgentConnectionStatus
    last_received_sequence: int = Field(default=0, ge=0)
    last_sent_sequence: int = Field(default=0, ge=0)
    connected_at: datetime
    last_heartbeat_at: datetime
    disconnected_at: datetime | None = None


class Event(APIModel):
    id: UUID
    project_id: UUID
    sequence: int
    event_type: str
    actor: str
    payload: dict[str, Any]
    created_at: datetime
    actor_kind: ReviewerKind = ReviewerKind.SYSTEM
    object_type: str | None = None
    object_id: UUID | None = None
    idempotency_key: str | None = None
    schema_version: str = "1.0"


class EventOutbox(APIModel):
    id: UUID
    event_id: UUID
    project_id: UUID
    status: Literal["PENDING", "PROCESSING", "DELIVERED", "FAILED"] = "PENDING"
    attempts: int = Field(default=0, ge=0)
    available_at: datetime
    locked_at: datetime | None = None
    lock_expires_at: datetime | None = None
    delivered_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime


class ReviewCreate(APIModel):
    target_type: Literal["task", "artifact", "handoff"]
    target_id: UUID
    verdict: Literal["APPROVED", "NEEDS_REVISION", "BLOCKED"]
    summary: str
    findings: list[dict[str, Any]] = Field(default_factory=list)
    evidence_ids: list[UUID] = Field(default_factory=list)
    reviewer: str = "team-reviewer"
    reviewer_kind: ReviewerKind = ReviewerKind.AGENT
    idempotency_key: str | None = None


class Review(APIModel):
    id: UUID
    project_id: UUID
    target_type: str
    target_id: UUID
    verdict: str
    summary: str
    findings: list[dict[str, Any]]
    evidence_ids: list[UUID] = Field(default_factory=list)
    risk_summary: dict[str, Any] = Field(default_factory=dict)
    reviewer: str
    reviewer_kind: ReviewerKind
    created_at: datetime


class InformationBoundary(APIModel):
    allowed: bool = True
    data_time_start: datetime | None = None
    data_time_end: datetime | None = None
    accessible_at: datetime | None = None
    future_data_policy: Literal["deny", "allow", "unknown"] = "deny"
    source_types: list[Literal["observation", "forecast", "plan", "actual", "synthetic", "unknown"]] = Field(default_factory=list)
    declared_input_files: list[str] = Field(default_factory=list)
    observed_input_files: list[str] = Field(default_factory=list)
    violations: list[dict[str, Any]] = Field(default_factory=list)


class Evidence(APIModel):
    id: UUID
    project_id: UUID
    claim: str
    evidence_type: Literal["artifact", "run", "event", "external_source"]
    artifact_id: UUID | None = None
    run_id: UUID | None = None
    source_ref: str | None = None
    created_by: str
    created_at: datetime


class Gate(APIModel):
    id: UUID
    project_id: UUID
    target_type: Literal["task", "artifact", "handoff", "project"]
    target_id: UUID | None = None
    status: GateStatus = GateStatus.OPEN
    required_human_approval: bool = True
    blocking_findings: list[dict[str, Any]] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)
    review_ids: list[UUID] = Field(default_factory=list)
    evidence_ids: list[UUID] = Field(default_factory=list)
    risk_summary: dict[str, Any] = Field(default_factory=dict)
    input_snapshot: dict[str, Any] = Field(default_factory=dict)
    invalidated_at: datetime | None = None
    invalidation_reason: str | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None


class RiskRegistryEntry(APIModel):
    id: UUID
    project_id: UUID
    review_id: UUID
    target_type: str
    target_id: UUID
    code: str
    severity: Severity
    message: str
    resolved: bool = False
    evidence_refs: list[str] = Field(default_factory=list)
    owner: str | None = None
    resolution_reason: str | None = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    closure_evidence_ids: list[UUID] = Field(default_factory=list)
    created_at: datetime


class RiskDecisionRequest(APIModel):
    action: Literal["ASSIGN", "RESOLVE", "REOPEN"]
    owner: str | None = Field(default=None, max_length=200)
    reason: str | None = Field(default=None, max_length=2000)
    evidence_ids: list[UUID] = Field(default_factory=list)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=160)


class ReviewCenter(APIModel):
    gates: list[Gate]
    reviews: list[Review]
    evidence: list[Evidence]
    risks: list[RiskRegistryEntry]
    handoffs: list[Handoff] = Field(default_factory=list)


class IdempotencyEnvelope(APIModel):
    key: str = Field(min_length=8, max_length=160)
    operation: str = Field(min_length=1, max_length=120)
    request_hash: str = Field(min_length=1, max_length=128)
    schema_version: str = "1.0"


MemberRole = Literal["owner", "project_lead", "contributor", "reviewer", "observer"]


class OrganizationCreate(APIModel):
    name: str = Field(min_length=2, max_length=160)
    slug: str = Field(min_length=2, max_length=80)


class Organization(APIModel):
    id: UUID
    name: str
    slug: str
    created_at: datetime


class TeamCreate(APIModel):
    organization_id: UUID
    name: str = Field(min_length=2, max_length=160)


class Team(APIModel):
    id: UUID
    organization_id: UUID
    name: str
    created_at: datetime


class HumanMemberCreate(APIModel):
    organization_id: UUID
    team_id: UUID | None = None
    email: str = Field(min_length=3, max_length=240)
    display_name: str = Field(min_length=2, max_length=120)
    role: MemberRole = "contributor"


class HumanMember(APIModel):
    id: str
    organization_id: UUID
    team_id: UUID | None
    email: str
    display_name: str
    status: Literal["active", "invited", "suspended"]
    created_at: datetime


class Membership(APIModel):
    member_id: str
    team_id: UUID
    role: MemberRole
    created_at: datetime


class AgentProjectGrant(APIModel):
    agent_id: str
    project_id: UUID
    capabilities: list[str] = Field(default_factory=list)
    granted_by: str
    created_at: datetime | None = None


class Session(APIModel):
    token: str
    member_id: str
    expires_at: datetime


class Invitation(APIModel):
    id: UUID
    organization_id: UUID
    team_id: UUID | None
    email: str
    role: MemberRole
    token: str
    status: Literal["PENDING", "ACCEPTED", "EXPIRED", "REVOKED"]
    expires_at: datetime
    created_at: datetime


class InvitationCreate(APIModel):
    organization_id: UUID
    team_id: UUID | None = None
    email: str = Field(min_length=3, max_length=240)
    role: MemberRole = "contributor"
    expires_in_seconds: int = Field(default=86400, ge=300, le=604800)


class SessionCreate(APIModel):
    member_id: str = Field(min_length=2, max_length=120)
    expires_in_seconds: int = Field(default=86400, ge=300, le=2592000)


# ---------- 账号系统（AUTH-1） ----------


class RegisterRequest(APIModel):
    """邀请码注册。首个注册者免码并自动成为管理员（见 store.register_account）。"""

    email: str = Field(min_length=3, max_length=240)
    password: str = Field(min_length=10, max_length=200)
    display_name: str = Field(min_length=2, max_length=120)
    invite_code: str | None = Field(default=None, max_length=240)


class LoginRequest(APIModel):
    email: str = Field(min_length=3, max_length=240)
    password: str = Field(min_length=1, max_length=200)


class PasswordChangeRequest(APIModel):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=10, max_length=200)


class AccountView(APIModel):
    """账号视角：成员本体 + 账号属性。口令哈希绝不出现在任何响应里。"""

    member: HumanMember
    has_password: bool
    is_admin: bool
    last_login_at: datetime | None = None


class AuthSession(APIModel):
    """登录/注册的响应：令牌只在这一次出现，服务端只存哈希。"""

    token: str
    expires_at: datetime
    account: AccountView


class AccountUpdateRequest(APIModel):
    """管理员改账号：停用/启用、授予/取消管理员。两项都可省。"""

    status: Literal["active", "suspended"] | None = None
    is_admin: bool | None = None


class PasswordResetResult(APIModel):
    """管理员重置：一次性临时口令（明文只在这次响应里），成员登录后应自行修改。"""

    temporary_password: str
    account: AccountView


class GitRepositoryCreate(APIModel):
    project_id: UUID
    provider: Literal["local", "github", "gitlab", "gitea"] = "local"
    remote_url: str | None = None
    local_path: str = Field(min_length=1, max_length=1000)


class GitRepository(APIModel):
    project_id: UUID
    provider: str
    remote_url: str | None
    local_path: str
    head_commit: str | None = None


class GitFileIndex(APIModel):
    project_id: UUID
    commit_sha: str
    path: str
    content_hash: str
    size_bytes: int
    indexed_at: datetime


class Dashboard(APIModel):
    project: Project
    tasks: list[Task]
    handoffs: list[Handoff]
    artifacts: list[Artifact]
    agents: list[Agent]
    events: list[Event]
    runs: list[Run] = Field(default_factory=list)
    metrics: dict[str, int | float]


class AgentChatConversationCreate(APIModel):
    """建一个「对话」——单纯对话，不建任务、不进任务板（与「项目工作」分开）。

    `device_id` 决定谁来跑（这台设备的执行体按会话轮询取活）；`model` 为空就用设备探测到的默认模型。
    `role` 是执行体上的角色名（如 `mm-review`）：空 = 默认（不传 `--agent`）。名字以执行体心跳
    上报的 `roles` 为准，平台**不校验也不编**（名字在那边不存在时，由 opencode 自己报错并如实回传）。
    """

    title: str = Field(default="", max_length=120)
    project_id: UUID
    device_id: str = Field(min_length=2, max_length=80)
    model: str = Field(default="", max_length=160)
    role: str = Field(default="", max_length=80)
    variant: Literal["", "minimal", "high", "max"] = ""
    command_template: list[str] = Field(default_factory=list, max_length=24)


class AgentChatConversationUpdate(APIModel):
    """改会话设置（M-6）：只传要改的字段，**只影响下一轮**——历史轮次记着它当时用的模型与角色。"""

    role: str | None = Field(default=None, max_length=80)
    model: str | None = Field(default=None, max_length=160)
    variant: Literal["", "minimal", "high", "max"] | None = None
    title: str | None = Field(default=None, max_length=120)


class AgentChatConversation(APIModel):
    id: UUID
    member_id: str
    project_id: UUID
    device_id: str
    agent_id: str | None = None
    title: str
    model: str = ""
    # 会话上选的角色（空 = 默认）。执行体按它插 `--agent`；探不到角色时这里只会是空。
    role: str = ""
    # 推理强度：非空时内核走已验证支持 `--variant` 的 CLI 通道；serve 端目前会静默忽略它。
    variant: str = ""
    session_key: str | None = None
    turn_count: int = 0
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None


# 一轮对话的**终止原因**（stop_reason，W1.1）：`status` 只说"怎么结束"（DONE/FAILED/CANCELLED），
# 这一列说"为什么"。平台侧能自己判定的：cancelled（成员停止）与 completed/failed（按回报的
# success 兜底）；其余靠执行体回报（token_capped/turn_capped 等执行体信号映射，B 侧后补）。
# 认不出的值平台一律归一化为 `unknown`——宁可诚实说"不知道为什么结束"，也不编一个像模像样的值。
AGENT_TURN_STOP_REASONS: tuple[str, ...] = (
    "completed",
    "failed",
    "cancelled",
    "token_capped",
    "turn_capped",
    "timeout",
    "permission_timeout",
    "unknown",
)


class AgentChatTurn(APIModel):
    """一轮对话：用户消息 + 执行体的回复（同一行，回复由执行体回填）。"""

    id: UUID
    conversation_id: UUID
    seq: int
    status: Literal["PENDING", "CLAIMED", "DONE", "FAILED", "CANCELLED"]
    prompt: str
    content: str = ""
    model: str = ""
    # 这一轮**实际**用的角色（建轮次时从会话抄下来）：中途换角色时，历史里"这轮谁跑的"仍然如实
    role: str = ""
    # 这一轮实际使用的 `--variant`，与角色一样在建轮次时冻结，避免改会话后污染历史。
    variant: str = ""
    session_key: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    claimed_by: str | None = None
    # 这一轮带的输入文件（id + 真名）：写进轮次时就一并存下，读列表不用再逐条查成果物
    artifacts: list["AgentChatInputFile"] = Field(default_factory=list)
    # 这一轮产出的文件（成果物，待审）：由执行体在 complete 时上报（2026-09-24）
    outputs: list["AgentChatTurnOutput"] = Field(default_factory=list)
    # 为什么结束（W1.1）：AGENT_TURN_STOP_REASONS 之一；历史行为空串（读取层显示"未记录"）
    stop_reason: str = ""
    created_at: datetime
    completed_at: datetime | None = None


class AgentChatTurnEvent(APIModel):
    """对话轮次的过程事件（执行中一行一行回传；与任务的 Run 事件分开存）。"""

    id: UUID
    turn_id: UUID
    sequence: int
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class AgentChatMessageCreate(APIModel):
    content: str = Field(min_length=1, max_length=20000)
    # 随这轮一起给执行体的文件（项目成果物 id）。平台只存引用与名字；
    # 执行体在跑之前把它们下到工作目录（见 apps/agent/input_fetcher.py）。
    artifact_ids: list[UUID] = Field(default_factory=list, max_length=10)


class AgentChatTurnClaim(APIModel):
    agent_id: str = Field(min_length=2, max_length=80)
    project_id: UUID
    lease_seconds: int = Field(default=1800, ge=60, le=86400)


class AgentChatInputFile(APIModel):
    """执行体要下到工作目录的输入文件（名字给执行体看，id 用来下载）。"""

    artifact_id: UUID
    name: str
    size_bytes: int | None = None


class AgentChatTurnClaimResult(APIModel):
    """执行体取到的一轮：它需要知道给谁跑、用什么模型、续哪个会话、带哪些输入文件。"""

    turn: AgentChatTurn
    conversation_id: UUID
    project_id: UUID
    workspace_hint: str = ""
    command_template: list[str] = Field(default_factory=list)
    worker_events: str = "opencode"
    input_files: list[AgentChatInputFile] = Field(default_factory=list)


class AgentChatTurnComplete(APIModel):
    success: bool
    content: str = ""
    session_key: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    exit_code: int | None = None
    # 为什么这轮结束了（W1.1）：执行体知道自己怎么停的就如实报（token_capped/permission_timeout…）。
    # 留空 = 老执行体，平台按 success 兜底（成功 completed / 失败 failed）；集合外的值归一化为 unknown。
    stop_reason: str = ""
    # 这一轮**产出**的文件（成果物，待审）：页面据此给出「下载 / 转入云盘」。
    # 与 `artifacts`（输入附件）分开：两者语义相反，混在一起历史就分不清谁进谁出。
    outputs: list["AgentChatTurnOutput"] = Field(default_factory=list)


class AgentChatTurnOutput(APIModel):
    """对话产出的一条：成果物 id + 名字 + 大小（内容在成果物里，列表不重复搬）。"""

    artifact_id: UUID
    name: str = ""
    size_bytes: int = 0
    artifact_type: str = ""
    mime_type: str = ""
    relative_path: str = ""


class AgentConversationPromote(APIModel):
    """把一次对话转入正式项目生产（W1.2）：会话产出 → 任务输入附件 + 新任务。

    产物只能从**本会话**轮次的 outputs 里挑（服务端校验），防止跨会话/跨项目夹带输入。
    """

    title: str = Field(min_length=2, max_length=180)
    description: str = Field(default="", max_length=8000)
    output_artifact_ids: list[UUID] = Field(default_factory=list, max_length=10)
    # 交给另一个 Agent：当前任务系统按能力匹配领取、不支持把任务硬性钉给某个 Agent，
    # 所以这里如实做成**结构化交接块**写进任务描述（目标 + 上下文）；真正的派发钉定
    # 属多智能体编排期（实施计划 W3.2 的派发策略），不在这里假装已经能钉。
    target_agent_id: str | None = Field(default=None, max_length=80)
    handoff_context: str | None = Field(default=None, max_length=4000)


class AgentConversationPromoteResult(APIModel):
    task: "Task"
    attached_artifact_ids: list[UUID] = Field(default_factory=list)
    # 非空 = 有交接意图；如实说明"是描述里的交接块，不是硬性钉定"，页面照原文展示。
    handoff_note: str = ""


class AgentChatTurnApprovalRequest(APIModel):
    """执行体上报一条权限请求（M-5c S-3）。

    字段名照 opencode 的 `permission.asked` 原样收：`permission`（如 `external_directory`）、
    `patterns`（如 `["/tmp/*"]`）、`summary`（人看的一句话，由内核从 metadata 拼）、`tool`/`call_id`。
    """

    request_id: str = Field(min_length=2, max_length=120)
    permission: str = Field(default="", max_length=120)
    patterns: list[str] = Field(default_factory=list)
    summary: str = Field(default="", max_length=400)
    tool: str = Field(default="", max_length=80)
    call_id: str = Field(default="", max_length=120)


class AgentChatTurnApprovalDecision(APIModel):
    """人对一张待批准卡片的决定。

    三档与 opencode 的回复一一对应（真机实测：`reject` 之后文件**确实没被写出来**）：
    `once` 批准这一次、`always` 本次会话都允许、`reject` 拒绝。
    """

    decision: Literal["once", "always", "reject"]


class AgentChatTurnApproval(APIModel):
    """一张待批准卡片（或它的结局）。"""

    id: str
    turn_id: UUID
    conversation_id: UUID
    status: Literal["PENDING", "APPROVED", "DENIED", "EXPIRED"]
    permission: str = ""
    patterns: list[str] = Field(default_factory=list)
    summary: str = ""
    tool: str = ""
    call_id: str = ""
    decision: str = ""
    decided_by: str | None = None
    created_at: datetime
    decided_at: datetime | None = None


class AgentChatTurnApprovalState(APIModel):
    """执行体轮询用：只看状态与决定（内核据此回复 opencode）。"""

    status: str
    decision: str = ""


class AgentChatTurnEventReport(APIModel):
    event_type: str = Field(min_length=2, max_length=80)
    sequence: int = Field(ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime | None = None


class MyAgentRole(APIModel):
    """执行体上报的一个角色（opencode 的 agent）：名字 + 说明 + 能不能动手 + 定义指纹。

    名字是 `opencode agent list` 里真实存在的（执行体探测），说明取自角色定义文件自己的 frontmatter——
    所以页面上看到的选项**一定在那边能用**，不会出现点不动的假选项。
    `executes` 来自角色文件的 `tools` 开关：真则这个角色**能执行命令/写文件**（页面要如实标注"会改动工作目录"）。
    R-4 追加的四个字段来自角色文件本身：`sha256`（内容哈希前 12 位，页面标"定义版本"）、
    `rules`（"硬规则（禁令）"一节的前三条摘要）、`drifted`（与**部署清单**不一致 = 执行体上被手工改过）、
    `installed_at`（清单写下的时间）。**平台只做展示，不改写**这些值——它们必须反映执行体上的真实文件。
    """

    name: str
    description: str = ""
    executes: bool = False
    sha256: str = ""
    bytes: int = 0
    modified_at: str = ""
    rules: list[str] = Field(default_factory=list)
    drifted: bool = False
    installed_at: str = ""


class MyAgentEndpoint(APIModel):
    """「我的智能体」页面用的执行体条目：设备 + Agent + 可用模型 + 可用角色 + 会话数。"""

    device_id: str
    device_name: str = ""
    agent_id: str | None = None
    platform: str = "unknown"
    status: str = "unknown"
    online: bool = False
    project_id: UUID
    project_name: str = ""
    executor: str = ""
    models: list[str] = Field(default_factory=list)
    default_model: str = ""
    # 空列表 = 这台执行体没有可选角色：页面只显示「默认」，不显示空下拉
    roles: list[MyAgentRole] = Field(default_factory=list)
    conversation_count: int = 0


class LlmChannelCreate(APIModel):
    """管理员录入 OpenAI 兼容渠道：api_key 只落库，响应一律只给末 4 位提示。"""

    name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str = ""
    models: list[str] = Field(default_factory=list)
    priority: int = 100
    enabled: bool = True


class LlmChannelUpdate(APIModel):
    """只改传入字段；api_key 缺省/null = 不变、空串 = 清空。"""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    base_url: str | None = Field(default=None, min_length=1, max_length=500)
    api_key: str | None = None
    models: list[str] | None = None
    priority: int | None = None
    enabled: bool | None = None


class LlmQuotaSet(APIModel):
    """成员免费额度（token 上限）；负数 = 不限量。"""

    token_limit: int
