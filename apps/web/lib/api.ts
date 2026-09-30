export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

/* ---------- 会话令牌（AUTH-1） ----------
   令牌由 lib/auth.tsx 在登录/恢复会话时写入这里；本模块所有请求与 WS 地址都从这里取，
   这样"哪一处忘了带令牌"不会成为漏网的 bug。 */

const TOKEN_STORAGE_KEY = "map.sessionToken";
let sessionTokenValue: string | null = null;
let unauthorizedHandler: (() => void) | null = null;

/** auth.tsx 在挂载后调用：从 localStorage 恢复令牌（首帧不读，保证静态 HTML 一致）。 */
export function restoreSessionToken(): string | null {
  if (typeof window === "undefined") return null;
  sessionTokenValue = window.localStorage.getItem(TOKEN_STORAGE_KEY);
  return sessionTokenValue;
}

export function getSessionToken(): string | null {
  return sessionTokenValue;
}

export function setSessionToken(token: string | null): void {
  sessionTokenValue = token;
  if (typeof window === "undefined") return;
  if (token) window.localStorage.setItem(TOKEN_STORAGE_KEY, token);
  else window.localStorage.removeItem(TOKEN_STORAGE_KEY);
}

/** 401 的统一出口：auth.tsx 注册回调（清会话 + 跳登录），避免每个页面各写一遍。 */
export function onUnauthorized(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

/** 带会话令牌的 fetch。所有 API 调用都应走它（含流式响应）。 */
export async function apiFetch(input: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers ?? {});
  const token = getSessionToken();
  if (token && !headers.has("Authorization")) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(input, { ...init, headers });
  if (response.status === 401) {
    setSessionToken(null);
    unauthorizedHandler?.();
  }
  return response;
}

/** WebSocket 地址：浏览器无法给 WS 设请求头，令牌只能走查询参数（nginx 对该路径关访问日志）。 */
export function projectSocketUrl(projectId: string): string {
  const base = API_URL.replace(/^http/, "ws");
  const token = getSessionToken();
  return `${base}/ws/projects/${projectId}${token ? `?token=${encodeURIComponent(token)}` : ""}`;
}

/** 服务端稳定错误码形态（如 ai_credentials_missing、hyper_rag_unavailable）。 */
const ERROR_CODE_PATTERN = /^[a-z][a-z0-9_]{2,}$/;

/** 统一的 API 失败：带上 HTTP 状态、稳定错误码与服务端原始 detail。 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: string;

  constructor(status: number, message: string, code = "", detail = "") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

/** 把响应体解析成一句人话：优先 detail，其次按错误码映射，最后退回调用方给的兜底文案。 */
export async function apiError(
  response: Response,
  fallback: string,
  codeMessages: Record<string, string> = {},
): Promise<ApiError> {
  let raw = "";
  try {
    raw = await response.text();
  } catch {
    return new ApiError(response.status, fallback);
  }
  let detail = raw.trim();
  try {
    const parsed = JSON.parse(raw) as { detail?: unknown };
    if (typeof parsed?.detail === "string") {
      detail = parsed.detail;
    } else if (Array.isArray(parsed?.detail)) {
      // FastAPI 参数校验失败：detail 是 [{loc, msg}] 列表，取字段名 + 原因才有意义。
      detail = parsed.detail
        .map((item) => {
          if (item && typeof item === "object" && "msg" in item) {
            const entry = item as { msg?: unknown; loc?: unknown };
            const path = Array.isArray(entry.loc) ? entry.loc.slice(1).join(".") : "";
            return path ? `${path}：${String(entry.msg)}` : String(entry.msg);
          }
          return JSON.stringify(item);
        })
        .join("；");
    } else if (parsed?.detail !== undefined) {
      detail = JSON.stringify(parsed.detail);
    }
  } catch {
    // 非 JSON 响应体按原文处理
  }
  const code = extractErrorCode(detail);
  return new ApiError(response.status, (code && codeMessages[code]) || detail || fallback, code, detail);
}

/** 从 detail 取稳定错误码：整体匹配优先，否则取冒号前首段（如 drive_quota_exceeded:123/456）。 */
function extractErrorCode(detail: string): string {
  if (ERROR_CODE_PATTERN.test(detail)) return detail;
  const head = detail.split(":", 1)[0].trim();
  return ERROR_CODE_PATTERN.test(head) ? head : "";
}

export type TaskStatus = "DRAFT" | "READY" | "CLAIMED" | "RUNNING" | "WAITING_REVIEW" | "APPROVED" | "BLOCKED" | "NEEDS_REVISION" | "FAILED" | "CANCELLED";

export type Project = {
  id: string;
  organization_id?: string;
  /** 归属团队（null = 未归属）；团队成员会自动加入该团队的项目 */
  team_id?: string | null;
  name: string;
  competition_pack: string;
  problem_code: string | null;
  description: string;
  /** 立项目标（工作区，W-1） */
  goal?: string | null;
  /** 目标人数：只作建队参考，不做加入拦截 */
  target_member_count?: number | null;
  /** 任务推进模式：manual=队长派单（默认）/ hybrid=允许成员认领 / auto=模板全自动（opt-in） */
  task_mode?: TaskMode;
  stage: string;
  progress: number;
  created_at: string;
  updated_at: string;
};

export type TaskMode = "manual" | "hybrid" | "auto";

export type Task = {
  id: string;
  project_id: string;
  title: string;
  description: string;
  stage: string;
  status: TaskStatus;
  /** 执行者标注：领取时平台写成 agent_id */
  assignee: string;
  /** 派单目标成员（null = 未指派，谁先轮到谁跑） */
  assignee_member_id?: string | null;
  priority: string;
  requires_review: boolean;
  allow_future_data: boolean;
  input_artifacts: string[];
  output_types: string[];
  blocked_reason: string | null;
  updated_at: string;
  /** 执行方式（worker_executor=codex / worker_command）：任务能否被 Agent 跑起来的依据。 */
  resource_policy?: Record<string, unknown>;
  /** 意图对象（AIP-1d）：预算与显式证据要求（默认空 = 不设限）。 */
  budget?: TaskBudget | null;
  evidence_requirements?: EvidenceRequirement[];
  // 后端 Task 契约里已有、此前前端类型漏掉的字段（任务详情要用）。
  parent_task_id?: string | null;
  dependency_task_ids?: string[];
  acceptance_criteria?: string[];
  required_capabilities?: string[];
  input_handoff_ids?: string[];
  requires_human_approval?: boolean;
  deadline?: string | null;
};

export type Handoff = {
  id: string;
  project_id: string;
  task_id: string;
  sender_agent_id: string;
  receiver: string | Record<string, unknown> | Array<Record<string, unknown>>;
  status: "PASS" | "PASS_WITH_ASSUMPTIONS" | "NEEDS_REVISION" | "BLOCKED";
  objective: string;
  completed: string[];
  input_artifacts: string[];
  output_artifacts: string[];
  key_conclusions: string[];
  assumptions: string[];
  evidence_refs: string[];
  open_questions: string[];
  risks: { severity: string; text: string }[];
  next_actions: string[];
  requires_human_approval: boolean;
  handoff_type: "RELAY" | "FANOUT" | "AGGREGATE";
  input_handoff_ids: string[];
  revision_number: number;
  receipt_status: "PENDING" | "ACCEPTED" | "REJECTED";
  decision_reason: string | null;
  decision_findings: Array<Record<string, unknown>>;
  receipts: HandoffReceipt[];
  created_at: string;
};

export type HandoffReceipt = {
  id: string;
  handoff_id: string;
  receiver_type: "agent" | "member" | "team" | "agent_group";
  receiver_id: string;
  status: "PENDING" | "ACCEPTED" | "REJECTED";
  received_by: string | null;
  received_at: string | null;
  decision_reason: string | null;
  decision_findings: Array<Record<string, unknown>>;
  created_at: string;
};

export type Artifact = {
  id: string;
  project_id: string;
  name: string;
  artifact_type: string;
  description: string;
  content_hash: string;
  version: number;
  // ARCHIVED 是回收态（CL-4 起有归档入口）：保留审计与引用，但不再作为新任务输入
  status: "DRAFT" | "PENDING_REVIEW" | "APPROVED" | "REJECTED" | "ARCHIVED";
  source_path: string | null;
  task_id: string | null;
  run_id: string | null;
  created_by: string;
  /** 创建者身份：`agent` 表示由内核执行产出自动入库。 */
  created_by_kind?: string;
  created_at: string;
  data_policy: Record<string, string>;
  /** 是否允许下游任务引用（领取校验会检查）。 */
  downstream_allowed?: boolean;
  /** 修订谱系：新版本的父版本 id（非修订版本为 null）。 */
  parent_artifact_id?: string | null;
};

export type Agent = {
  agent_id: string;
  display_name: string;
  owner_member_id: string;
  model_provider: string;
  model_name: string;
  supported_tools: string[];
  supported_languages: string[];
  max_concurrency: number;
  local_workspace: string | null;
  network_policy: string;
  status: "online" | "idle" | "offline";
  last_seen: string;
};

export type Event = {
  id: string;
  project_id: string;
  sequence: number;
  event_type: string;
  actor: string;
  /** 事件载荷：形态随事件类型不同（执行体过程事件是 payload.event.payload）。 */
  payload: Record<string, any>;
  created_at: string;
  actor_kind?: string;
  object_type?: string | null;
  object_id?: string | null;
};

export type Run = {
  id: string;
  project_id: string;
  task_id: string | null;
  agent_id: string;
  /** 执行归属（迁移 019）：哪台设备、谁的机器；member_id 由平台推导 */
  device_id?: string | null;
  member_id?: string | null;
  status: "CREATED" | "RUNNING" | "SUCCEEDED" | "FAILED" | "BLOCKED";
  source_commit: string | null;
  input_artifact_ids: string[];
  environment_image_digest: string | null;
  dependency_lock: string | null;
  parameters: Record<string, unknown>;
  random_seed: number | null;
  model_provider: string | null;
  model_name: string | null;
  tool_versions: Record<string, string>;
  network_policy: string;
  data_access_policy: Record<string, unknown>;
  observed_input_files: string[];
  output_artifact_ids: string[];
  stdout: string;
  stderr: string;
  summary: string;
  information_boundary: { allowed?: boolean; violations?: { severity: string; code: string }[] };
  /** 执行用量（COST-1）：空对象 = 未回报（通用 CLI 执行体没有用量可报） */
  usage?: RunUsage;
  started_at: string;
  completed_at: string | null;
};

export type GateStatus = "OPEN" | "PASSED" | "FAILED" | "BLOCKED" | "INVALIDATED";

export type Gate = {
  id: string;
  project_id: string;
  target_type: "task" | "artifact" | "handoff" | "project";
  target_id: string | null;
  status: GateStatus;
  required_human_approval: boolean;
  blocking_findings: Array<Record<string, unknown>>;
  rules: string[];
  review_ids: string[];
  evidence_ids: string[];
  risk_summary: Record<string, unknown>;
  input_snapshot: Record<string, unknown>;
  invalidated_at: string | null;
  invalidation_reason: string | null;
  approved_by: string | null;
  approved_at: string | null;
};

export type Review = {
  id: string;
  project_id: string;
  target_type: string;
  target_id: string;
  verdict: "APPROVED" | "NEEDS_REVISION" | "BLOCKED" | string;
  summary: string;
  findings: Array<Record<string, unknown>>;
  evidence_ids: string[];
  risk_summary: Record<string, unknown>;
  reviewer: string;
  reviewer_kind: "member" | "agent" | "system";
  created_at: string;
};

export type Evidence = {
  id: string;
  project_id: string;
  claim: string;
  evidence_type: "artifact" | "run" | "event" | "external_source";
  artifact_id: string | null;
  run_id: string | null;
  source_ref: string | null;
  created_by: string;
  created_at: string;
};

export type RiskRegistryEntry = {
  id: string;
  project_id: string;
  review_id: string;
  target_type: string;
  target_id: string;
  code: string;
  severity: "fatal" | "major" | "minor" | string;
  message: string;
  resolved: boolean;
  evidence_refs: string[];
  owner: string | null;
  resolution_reason: string | null;
  resolved_by: string | null;
  resolved_at: string | null;
  closure_evidence_ids: string[];
  created_at: string;
};

export type ReviewCenter = {
  gates: Gate[];
  reviews: Review[];
  evidence: Evidence[];
  risks: RiskRegistryEntry[];
  handoffs: Handoff[];
};

export type Dashboard = {
  project: Project;
  tasks: Task[];
  handoffs: Handoff[];
  artifacts: Artifact[];
  agents: Agent[];
  events: Event[];
  runs: Run[];
  metrics: Record<string, number>;
};

export type ImportSummary = {
  project_id: string;
  source_path: string;
  discovered_files: number;
  imported_artifacts: number;
  skipped_artifacts: number;
  created_tasks: number;
  artifact_ids: string[];
  warnings: string[];
};

export async function getProjects(): Promise<Project[]> {
  const response = await apiFetch(`${API_URL}/api/projects`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "项目列表读取失败");
  return response.json();
}

export type ProjectCreateInput = {
  name: string;
  competition_pack?: string;
  problem_code?: string | null;
  description?: string;
  /** 立项目标：工作区右侧概览与项目叙事的第一句 */
  goal?: string;
  /** 目标人数（建队参考） */
  target_member_count?: number | null;
  /** 任务推进模式：默认 manual（队长派单） */
  task_mode?: TaskMode;
  /** 归属团队（可选）：团队成员会自动加入该团队的项目 */
  team_id?: string | null;
};

export async function createProject(data: ProjectCreateInput): Promise<Project> {
  const response = await apiFetch(`${API_URL}/api/projects`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!response.ok) throw await apiError(response, "项目创建失败");
  return response.json();
}

/** 页面侧统一取错误文案：ApiError 用服务端 detail，其余退回兜底，避免只弹"…失败"。 */
export function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError && error.message) return error.message;
  if (error instanceof Error && error.message) return error.message;
  return fallback;
}

export async function getDashboard(projectId: string): Promise<Dashboard> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/dashboard`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "项目数据读取失败");
  return response.json();
}

/**
 * 提交人工复核（门禁批准的**唯一**入口）。
 *
 * 门禁状态由 Review 派生：verdict=APPROVED → PascalCase 的 PASSED，
 * NEEDS_REVISION → FAILED，BLOCKED → BLOCKED（见 store.create_review）。
 * 服务端守卫：存在未关闭的 fatal/major 风险会拒绝、任务必须处于可复核状态、
 * APPROVED 必须是 member 身份——这些都会以稳定错误码回传，界面要如实展示。
 */
export async function submitReview(
  projectId: string,
  payload: {
    target_type: "task" | "artifact" | "handoff";
    target_id: string;
    verdict: "APPROVED" | "NEEDS_REVISION" | "BLOCKED";
    summary: string;
    findings?: { severity: string; code: string; message: string }[];
    reviewer?: string;
  },
): Promise<unknown> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/reviews`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      reviewer: payload.reviewer ?? "member-001",
      reviewer_kind: "member",
      findings: payload.findings ?? [],
      ...payload,
    }),
  });
  if (!response.ok) {
    throw await apiError(response, "复核提交失败", {
      review_blocked_by_open_risks: "存在未关闭的严重风险，先关闭或降级后才能批准",
      task_not_waiting_for_review: "任务不在待复核状态（需先提交复核或标记返工）",
      approved_task_is_immutable: "该任务已批准，不能再改结论",
      human_approval_required: "批准必须由人工成员提交（Agent 不能代批）",
      rejected_handoff_requires_revision: "该交接已被拒绝，需要先修订再批准",
      review_target_not_found: "审批目标不存在",
    });
  }
  return response.json();
}

/**
 * 交接收据：接受/拒绝由**接收方**提交（服务端按收据行校验身份）。
 *
 * 稳定错误码含义：
 *   handoff_receiver_mismatch        —— 这份交接不是发给当前成员的
 *   handoff_rejected_requires_revision —— 已被拒绝，必须先修订
 *   handoff_not_acceptible           —— 交接状态不是 PASS/PASS_WITH_ASSUMPTIONS
 */
const HANDOFF_ERROR_MESSAGES: Record<string, string> = {
  handoff_receiver_mismatch: "这份交接不是发给你的（接收方不匹配）",
  handoff_rejected_requires_revision: "该交接已被拒绝，需要先修订再接受",
  handoff_not_acceptible: "交接尚未通过（需为 PASS 或 PASS_WITH_ASSUMPTIONS）",
  handoff_not_found: "交接不存在",
};

export async function acceptHandoff(handoffId: string): Promise<Handoff> {
  const response = await apiFetch(`${API_URL}/api/handoffs/${handoffId}/accept`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ idempotency_key: `handoff-accept:${handoffId}:${crypto.randomUUID()}` }),
  });
  if (!response.ok) throw await apiError(response, "交接接受失败", HANDOFF_ERROR_MESSAGES);
  return response.json();
}

export async function rejectHandoff(handoffId: string, reason: string): Promise<Handoff> {
  const response = await apiFetch(`${API_URL}/api/handoffs/${handoffId}/reject`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      reason,
      findings: [],
      idempotency_key: `handoff-reject:${handoffId}:${crypto.randomUUID()}`,
    }),
  });
  if (!response.ok) throw await apiError(response, "交接拒绝失败", HANDOFF_ERROR_MESSAGES);
  return response.json();
}

export async function getReviewCenter(projectId: string): Promise<ReviewCenter> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/review-center`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "审核中心读取失败");
  return response.json();
}

export async function updateRisk(projectId: string, riskId: string, payload: { action: "ASSIGN" | "RESOLVE" | "REOPEN"; owner?: string; reason?: string; evidence_ids?: string[] }): Promise<RiskRegistryEntry> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/risks/${riskId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...payload, idempotency_key: crypto.randomUUID() }),
  });
  if (!response.ok) throw await apiError(response, "风险状态更新失败");
  return response.json();
}

export type TaskBudget = {
  /** 单次执行墙钟上限（秒）——领取时把租约压到这个值以内，强制。 */
  max_seconds?: number | null;
  /** 整条任务允许的领取次数——用尽后不再接受领取，强制。 */
  max_attempts?: number | null;
  /** 只记录不强制（平台没有 token 计量），界面会标注"未强制"。 */
  max_tokens?: number | null;
};

export type EvidenceRequirement = {
  evidence_type: "artifact" | "run" | "event" | "external_source";
  min_count: number;
  note?: string;
};

/** 执行用量（COST-1）：token 只有执行体回报过才有数；seconds 优先自报、缺省平台观测。 */
export type RunUsage = {
  input_tokens?: number | null;
  output_tokens?: number | null;
  total_tokens?: number | null;
  turns?: number | null;
  seconds?: number | null;
  /** token 的出处：codex-jsonl / agent-reported / platform-observed */
  source?: string;
  /** 耗时的出处：agent（自报）或 platform（平台观测） */
  seconds_source?: string;
};

export type TaskBudgetState = {
  attempts: number;
  max_attempts: number | null;
  exhausted: boolean;
  max_seconds: number | null;
  max_tokens: number | null;
  /** 已回报的累计 token；`usage_reported_runs = 0` 时它不代表"没花"，而是"平台不知道" */
  tokens_used: number;
  usage_reported_runs: number;
};

export type TaskEvidenceGap = {
  evidence_type: string;
  required: number;
  present: number;
  missing: number;
  note: string;
};

export type TaskFlags = {
  task_id: string;
  evidence_missing: number;
  usage_overrun: boolean;
  usage_reported: boolean;
};

/** 项目内任务的轻量标记（缺口/超预算），一次取回给列表画角标。 */
export async function getProjectTaskFlags(projectId: string): Promise<TaskFlags[]> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/task-flags`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "任务标记读取失败");
  return response.json();
}

export type TaskDetail = {
  task: Task;
  budget_state: TaskBudgetState;
  evidence_gaps: TaskEvidenceGap[];
};

export type TaskPatch = {
  status?: TaskStatus;
  assignee?: string;
  blocked_reason?: string;
  /** 执行方式（codex 或声明式命令）：走请求体，是"把任务变成 Agent 能真正跑的"唯一入口。 */
  resource_policy?: Record<string, unknown>;
  /** 派单：成员 id（改派）或空串（取消指派）；不传则不动 */
  assignee_member_id?: string;
  /** 截止时间（ISO 字符串）；空串 = 清除 */
  deadline?: string;
  /** 意图对象（AIP-1d）：预算；不传则不动，null = 清除 */
  budget?: TaskBudget | null;
  /** 意图对象（AIP-1d）：显式证据要求；不传则不动，[] = 清除 */
  evidence_requirements?: EvidenceRequirement[];
};

export async function updateTask(taskId: string, patch: TaskPatch): Promise<Task> {
  // 后端 PATCH /api/tasks/{id}：状态/负责人走查询参数（既有契约），执行方式与派单走 JSON 体。
  const query = new URLSearchParams();
  if (patch.status) query.set("status", patch.status);
  if (patch.assignee !== undefined) query.set("assignee", patch.assignee);
  if (patch.blocked_reason !== undefined) query.set("blocked_reason", patch.blocked_reason);
  const init: RequestInit = { method: "PATCH" };
  if (
    patch.resource_policy !== undefined ||
    patch.assignee_member_id !== undefined ||
    patch.deadline !== undefined ||
    patch.budget !== undefined ||
    patch.evidence_requirements !== undefined
  ) {
    init.headers = { "Content-Type": "application/json" };
    const body: Record<string, unknown> = {};
    if (patch.resource_policy !== undefined) body.resource_policy = patch.resource_policy;
    // 空串 = 取消指派 / 清除截止时间；只要字段出现在 body 里服务端就会处理
    if (patch.assignee_member_id !== undefined) body.assignee_member_id = patch.assignee_member_id;
    if (patch.deadline !== undefined) body.deadline = patch.deadline;
    // 意图对象（AIP-1d）：null / [] = 清除；"出现才生效"是服务端约定
    if (patch.budget !== undefined) body.budget = patch.budget;
    if (patch.evidence_requirements !== undefined) body.evidence_requirements = patch.evidence_requirements;
    init.body = JSON.stringify(body);
  }
  const response = await apiFetch(`${API_URL}/api/tasks/${taskId}?${query.toString()}`, init);
  if (!response.ok) throw await apiError(response, "任务更新失败");
  return response.json();
}

export async function createTask(
  projectId: string,
  payload: {
    title: string;
    description: string;
    stage: string;
    assignee: string;
    priority: string;
    requires_review: boolean;
    allow_future_data: boolean;
    /** 输入成果物：只有已批准且允许下游的成果物能通过领取校验（服务端 `_task_dependencies_ready`）。 */
    input_artifacts?: string[];
    /** 派单：指派给某个项目成员，只有其名下设备能领取；不填 = 谁先轮到谁跑 */
    assignee_member_id?: string | null;
    /** 截止时间（ISO）：过期后不再被自动领取，队列里优先级更高 */
    deadline?: string | null;
  },
): Promise<Task> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/tasks`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw await apiError(response, "任务创建失败");
  return response.json();
}


// ---- 竞赛领域包（阶段 6 数学建模模板） ----------------------------------

export type CompetitionPackSummary = {
  pack_id: string;
  display_name: string;
  version: string;
  description: string;
  competition_aliases: string[];
  problem_codes: string[];
  default_problem_code: string;
  questions: number[];
  template_count: number;
  dag_task_count: number;
  required_artifacts: string[];
};

export type CompetitionPackTemplate = {
  template_id: string;
  name: string;
  artifact_type: string;
  filename: string;
  stage: string;
  description: string;
  questions: number[];
  official_format: boolean;
  placeholders: string[];
};

export type CompetitionPackDetail = CompetitionPackSummary & {
  schema_version: string;
  stage_sequence: string[];
  optional_artifacts: string[];
  templates: CompetitionPackTemplate[];
  dag: { task_id: string; title: string; stage: string; question: number | null; depends_on: string[]; produces: string[]; role: string }[];
  validation_rules: Record<string, unknown>;
  boundary_rules: Record<string, unknown>;
  upgrade_rules: { from_version: string; to_version: string; compatibility: string; added_artifacts: string[]; renamed_artifacts: Record<string, string> }[];
};

export type PackMaterializationStatus = {
  pack_id: string;
  pack_version: string;
  materialized: boolean;
  task_total: number;
  task_present: number;
  artifact_total: number;
  artifact_present: number;
  planned_total: number;
  planned_present: number;
  progress: number;
  missing_tasks: string[];
  missing_artifacts: string[];
};

export type ProjectCompetitionPack = {
  project_id: string;
  competition_pack: string;
  pack: CompetitionPackDetail;
  materialization: PackMaterializationStatus;
};

export type PackValidationFinding = {
  severity: "fatal" | "major" | "minor" | "info";
  code: string;
  subject: string;
  message: string;
};

export type PackValidationReport = {
  project_id: string;
  pack_id: string;
  pack_version: string;
  status: "PASS" | "PASS_WITH_ASSUMPTIONS" | "NEEDS_REVISION" | "BLOCKED";
  allowed: boolean;
  worst_severity: string | null;
  missing_artifacts: string[];
  checked_artifacts: string[];
  coverage: { expected_questions?: number[]; questions_covered?: number[]; per_artifact?: Record<string, number[]> };
  findings: PackValidationFinding[];
};

export type PackApplyResult = {
  project_id: string;
  pack_id: string;
  pack_version: string;
  problem_code: string;
  questions: number[];
  created_task_count: number;
  created_artifact_count: number;
  tasks: { dag_task_id: string; task_id: string; title: string; stage: string; question: number | null; created: boolean }[];
  artifacts: { template_id: string; artifact_type: string; name: string; created: boolean; content_hash: string }[];
  warnings: string[];
};

export async function getCompetitionPacks(): Promise<CompetitionPackSummary[]> {
  const response = await apiFetch(`${API_URL}/api/competition-packs`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "竞赛模板包列表读取失败");
  return response.json();
}

export async function getProjectCompetitionPack(projectId: string): Promise<ProjectCompetitionPack> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/competition-pack`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "项目模板包状态读取失败");
  return response.json();
}

export async function applyProjectCompetitionPack(
  projectId: string,
  payload: { problem_code?: string; questions?: number[] } = {},
): Promise<PackApplyResult> {
  const idempotencyKey = crypto.randomUUID();
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/competition-pack/apply`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
    body: JSON.stringify({ ...payload, created_by: "web-materializer", idempotency_key: idempotencyKey }),
  });
  if (!response.ok) throw await apiError(response, "模板包应用失败");
  return response.json();
}

export async function validateProjectCompetitionPack(projectId: string): Promise<PackValidationReport> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/competition-pack/validate`, { method: "POST" });
  if (!response.ok) throw await apiError(response, "模板包校验失败");
  return response.json();
}

export async function fetchProjectPackTemplate(
  projectId: string,
  templateId: string,
  questions?: number[],
): Promise<string> {
  const query = questions?.length ? `?questions=${questions.join(",")}` : "";
  const response = await apiFetch(
    `${API_URL}/api/projects/${projectId}/competition-pack/templates/${templateId}${query}`,
    { cache: "no-store" },
  );
  if (!response.ok) throw await apiError(response, "模板渲染失败");
  return response.text();
}


// ---- 文档三层版本与证据链（阶段 7 最小切片） ---------------------------

export type DocumentLayer = "draft" | "submitted" | "approved";

export type DocumentLayerDefinition = {
  layer: DocumentLayer;
  artifact_status: string;
  editable: boolean;
  downstream_allowed: boolean;
};

export type DocumentLayers = {
  layers: DocumentLayerDefinition[];
  rules: {
    submit_requires_evidence: boolean;
    approval_requires_human_review: boolean;
    draft_cannot_be_downstream_input: boolean;
    revision_creates_new_draft_version: boolean;
  };
  status_to_layer: Record<string, DocumentLayer>;
};

export type DocumentEvidenceEntry = {
  id: string;
  claim: string;
  evidence_type: string;
  run_id: string | null;
  source_ref: string | null;
  created_by: string;
  artifact_id?: string;
  layer?: DocumentLayer;
};

export type DocumentRevision = {
  revision: number;
  artifact_id: string;
  layer: DocumentLayer;
  status: string;
  version: number;
  content_hash: string;
  editable: boolean;
  downstream_allowed: boolean;
  immutable: boolean;
  parent_artifact_id: string | null;
  traceability: {
    created_by: string;
    created_by_kind: string;
    created_at: string | null;
    task_id: string | null;
    run_id: string | null;
    git_commit: string | null;
    snapshot_ref: string | null;
    approved_by: string | null;
    approved_at: string | null;
  };
  evidence: DocumentEvidenceEntry[];
};

export type DocumentTimeline = {
  project_id: string;
  artifact_id: string;
  root_artifact_id: string;
  current_layer: DocumentLayer;
  current_revision: number;
  revision_count: number;
  revisions: DocumentRevision[];
  layers: DocumentLayers;
};

export type DocumentEvidenceChain = {
  project_id: string;
  artifact_id: string;
  evidence: DocumentEvidenceEntry[];
  evidence_count: number;
  runs: string[];
  traceable_to: DocumentRevision["traceability"][];
};


export async function getDocumentTimeline(projectId: string, artifactId: string): Promise<DocumentTimeline> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/documents/${artifactId}/timeline`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "文档版本时间线读取失败");
  return response.json();
}


export async function submitDocument(projectId: string, artifactId: string): Promise<{ layer: DocumentLayer; changed: boolean }> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/documents/${artifactId}/submit`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ actor_kind: "member" }),
  });
  if (!response.ok) throw await apiError(response, "文档提交失败", { document_evidence_required: "提交需要先关联证据" });
  return response.json();
}

export async function reviseDocument(projectId: string, artifactId: string, description?: string): Promise<{ artifact_id: string; layer: DocumentLayer; changed: boolean }> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/documents/${artifactId}/revise`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ actor_kind: "member", description }),
  });
  if (!response.ok) throw await apiError(response, "文档修订失败");
  return response.json();
}

// ---- 版本 Diff / 合并 / 评论 / 快照 / 关系（阶段 7） -------------------

export type DocumentDiff = {
  from_revision: number;
  to_revision: number;
  content_changed: boolean;
  identical: boolean;
  stats: { added_lines: number; removed_lines: number; hunks: number };
  unified_diff: string[];
  layer_change: { from: DocumentLayer; to: DocumentLayer; changed: boolean };
  git_commit: { from: string | null; to: string | null; changed: boolean };
  task_id: { from: string | null; to: string | null; changed: boolean };
  author: { from: string | null; to: string | null; changed: boolean };
  approver: { from: string | null; to: string | null; changed: boolean };
};

export type DocumentComment = {
  id: string;
  body: string;
  kind: "comment" | "suggestion";
  anchor: string | null;
  actor: string;
  actor_kind: string;
  layer: DocumentLayer;
  created_at: string | null;
};

export type DocumentSnapshot = {
  id: string;
  revision: number;
  content_hash: string;
  label: string;
  actor: string;
  created_at: string | null;
};

export type DocumentRelation = {
  id: string;
  target_type: string;
  target_id: string;
  paragraph: string;
  note: string;
  actor: string;
  created_at: string | null;
};

export type ImpactLookup = {
  target_type: string;
  target_id: string;
  affected: { artifact_id: string; paragraph: string; note: string; linked_by: string }[];
  affected_count: number;
  affected_artifacts: string[];
  paragraphs: string[];
};

export async function getDocumentDiff(
  projectId: string,
  artifactId: string,
  fromRevision?: number,
  toRevision?: number,
): Promise<DocumentDiff> {
  const query = new URLSearchParams();
  if (fromRevision) query.set("from_revision", String(fromRevision));
  if (toRevision) query.set("to_revision", String(toRevision));
  const suffix = query.toString() ? `?${query.toString()}` : "";
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/documents/${artifactId}/diff${suffix}`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "版本差异读取失败");
  return response.json();
}

export async function mergeDocument(
  projectId: string,
  artifactId: string,
  sourceRevision: number,
  note?: string,
  gitCommit?: string,
): Promise<{ artifact_id: string; layer: DocumentLayer; content_hash: string; git_commit: string | null }> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/documents/${artifactId}/merge`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source_revision: sourceRevision, note: note ?? "", git_commit: gitCommit ?? null, actor_kind: "member" }),
  });
  if (!response.ok) throw await apiError(response, "合并确认失败（已批准版本不可合并）");
  return response.json();
}

export async function addDocumentComment(
  projectId: string,
  artifactId: string,
  body: string,
  kind: "comment" | "suggestion" = "comment",
  anchor?: string,
): Promise<DocumentComment> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/documents/${artifactId}/comments`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ body, kind, anchor: anchor ?? null, actor_kind: "member" }),
  });
  if (!response.ok) throw await apiError(response, "评论提交失败");
  return response.json();
}

export async function getDocumentComments(projectId: string, artifactId: string): Promise<{ comments: DocumentComment[]; comment_count: number; suggestion_count: number }> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/documents/${artifactId}/comments`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "评论读取失败");
  return response.json();
}

export async function createDocumentSnapshot(projectId: string, artifactId: string, label = ""): Promise<DocumentSnapshot> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/documents/${artifactId}/snapshots`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ label, actor_kind: "member" }),
  });
  if (!response.ok) throw await apiError(response, "快照创建失败");
  return response.json();
}

export async function getDocumentSnapshots(projectId: string, artifactId: string): Promise<{ snapshots: DocumentSnapshot[]; snapshot_count: number }> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/documents/${artifactId}/snapshots`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "快照读取失败");
  return response.json();
}

export async function linkDocumentRelation(
  projectId: string,
  artifactId: string,
  payload: { target_type: string; target_id: string; paragraph: string; note?: string },
): Promise<DocumentRelation> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/documents/${artifactId}/relations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...payload, note: payload.note ?? "", actor_kind: "member" }),
  });
  if (!response.ok) throw await apiError(response, "关系关联失败");
  return response.json();
}

export async function getDocumentRelations(projectId: string, artifactId: string): Promise<{ relations: DocumentRelation[]; relation_count: number }> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/documents/${artifactId}/relations`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "关系读取失败");
  return response.json();
}

export async function getImpactLookup(projectId: string, targetType: string, targetId: string): Promise<ImpactLookup> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/impact?target_type=${targetType}&target_id=${targetId}`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "影响面查询失败");
  return response.json();
}
/** 保存文档内容：覆盖当前草稿内容（不产生新版本），版本由「提交待审」产生。 */
export async function saveArtifactText(artifactId: string, text: string): Promise<Artifact> {
  const form = new FormData();
  form.append("file", new Blob([text], { type: "text/markdown; charset=utf-8" }), `${artifactId}.md`);
  const response = await apiFetch(`${API_URL}/api/artifacts/${artifactId}/content`, {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: form,
  });
  if (!response.ok) {
    throw await apiError(response, "文档保存失败", {
      approved_artifact_is_immutable: "文档已批准，内容不可再修改；请先派生新草稿",
      artifact_not_found: "文档不存在",
    });
  }
  return response.json();
}

/** 成果物详情/修订需要的字段（后端契约里已有，前端类型此前只声明了一部分）。 */
export type ArtifactVersionInput = {
  name: string;
  artifact_type: string;
  description?: string;
  status?: "DRAFT" | "PENDING_REVIEW";
  task_id?: string;
  run_id?: string;
  content_hash?: string;
};

/** 以既有成果物为父版本创建新版本（名称与类型必须与父版本一致，服务端会校验）。 */
export async function createArtifactVersion(projectId: string, artifactId: string, payload: ArtifactVersionInput): Promise<Artifact> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/artifacts/${artifactId}/versions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    throw await apiError(response, "提交新版本失败", {
      artifact_version_identity_mismatch: "新版本的名称与类型必须与旧版本一致",
      parent_artifact_not_found: "旧版本不存在",
    });
  }
  return response.json();
}

/** 成果物全貌（CL-3）：来源、谱系、引用、复核与事件。 */
export type ArtifactDetail = {
  artifact: Artifact;
  lineage: { artifact_id: string; version: number; status: string; content_hash: string | null; created_at: string; is_current: boolean }[];
  consumers: { task_id: string; title: string; status: string }[];
  reviews: { id: string; verdict: string; reviewer: string; reviewer_kind: string; summary: string; created_at: string }[];
  gate: { id: string; status: string; required_human_approval: boolean } | null;
  source_task: { id: string; title: string; status: string } | null;
  source_run: { id: string; status: string; agent_id: string; started_at: string } | null;
  events: Event[];
  orphan: boolean;
  orphan_reason: string | null;
};

export async function getArtifactDetail(projectId: string, artifactId: string): Promise<ArtifactDetail> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/artifacts/${artifactId}/detail`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "成果物详情读取失败");
  return response.json();
}

/** 归档：内容退休但保留审计与引用（D-CL-7，不做硬删）。 */
export async function archiveArtifact(artifactId: string): Promise<Artifact> {
  const response = await apiFetch(`${API_URL}/api/artifacts/${artifactId}/archive`, { method: "POST" });
  if (!response.ok) {
    throw await apiError(response, "归档失败", { artifact_not_found: "成果物不存在" });
  }
  return response.json();
}

/** 协作草稿（CL-6）：未保存的编辑在刷新/换设备后仍在。 */
export type DocumentDraft = {
  artifact_id: string;
  project_id: string;
  content: string;
  revision: number;
  updated_by: string;
  updated_at: string;
  created_at: string;
};

export async function getDocumentDraft(projectId: string, artifactId: string): Promise<DocumentDraft | null> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/documents/${artifactId}/draft`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "协作草稿读取失败");
  return response.json();
}

/** 保存协作草稿：`baseRevision` 落后于服务端时抛 409（错误码 `document_draft_conflict`），不静默覆盖。 */
export async function saveDocumentDraft(
  projectId: string,
  artifactId: string,
  content: string,
  baseRevision: number,
): Promise<DocumentDraft> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/documents/${artifactId}/draft`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content, base_revision: baseRevision }),
  });
  if (!response.ok) {
    throw await apiError(response, "协作草稿保存失败", {
      document_draft_conflict: "这份文档已被他人修改：请选择以谁为准",
      approved_artifact_is_immutable: "文档已批准，不能再存草稿",
    });
  }
  return response.json();
}

export async function getArtifactText(artifactId: string): Promise<string> {
  const response = await apiFetch(`${API_URL}/api/artifacts/${artifactId}/content`, { cache: "no-store" });
  if (!response.ok) return "";
  return response.text();
}

// ---- 信息边界审计与交付链路 ---------------------------------------------

export type BoundaryAudit = {
  project_id: string;
  task_id: string | null;
  pack_id: string;
  pack_version: string;
  rules: Record<string, unknown>;
  run_count: number;
  findings: { severity: string; code: string; subject: string; message: string }[];
  verdict: string | null;
  allowed: boolean;
  created?: boolean;
  reason?: string;
  review_id?: string;
  gate_status?: string | null;
};

export type DeliveryAssembly = {
  project_id: string;
  pack_id: string;
  pack_version: string;
  sections: { title: string; source_name: string; artifact_type: string; content_hash: string; approved_by: string | null; content: string; traceability: Record<string, unknown> }[];
  section_count: number;
  sources: { name: string; artifact_type: string; content_hash: string; approved_by: string | null }[];
  excluded_unapproved: { name: string; artifact_type: string; status: string }[];
  excluded_count: number;
  blocked: boolean;
  blocked_reasons: string[];
  generatable: boolean;
};

export type DeliveryChecklist = {
  project_id: string;
  checks: { code: string; status: "pass" | "warn" | "fail"; detail: string }[];
  blocking_count: number;
  warn_count: number;
  passed: boolean;
  approved_artifact_count: number;
  paper_text_hash: string;
};

export type DeliveryCompileReport = {
  status: string;
  engine?: string | null;
  pages?: number | null;
  artifact_id?: string;
  artifact_name?: string;
  pdf_sha256?: string;
  pdf_bytes?: number;
  reason?: string | null;
  log_tail?: string;
};

export type SubmissionBundleReport = {
  artifact_id: string;
  content_hash: string;
  manifest_hash: string;
  layer: string;
  status: string;
  section_count: number;
  source_count: number;
  excluded_unapproved_count: number;
  blocking_count: number;
  checks: DeliveryChecklist["checks"];
};

export type BundleVerification = {
  artifact_id: string;
  manifest_hash: string | null;
  checked_sources: number;
  expected_sources: number;
  mismatches: string[];
  verified: boolean;
  restore: { restored_project_id: string; restored_artifact_count: number; checked: number; mismatches: string[]; restored: boolean } | null;
};

export async function getBoundaryAudit(projectId: string, taskId?: string): Promise<BoundaryAudit> {
  const query = taskId ? `?task_id=${taskId}` : "";
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/competition-pack/boundary-audit${query}`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "边界审计读取失败");
  return response.json();
}

export async function createBoundaryGate(projectId: string, taskId?: string): Promise<BoundaryAudit> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/competition-pack/boundary-gate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task_id: taskId ?? null, actor_kind: "agent", idempotency_key: crypto.randomUUID() }),
  });
  if (!response.ok) throw await apiError(response, "边界审计落库失败");
  return response.json();
}

export async function getDeliveryAssembly(projectId: string): Promise<DeliveryAssembly> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/delivery/assembly`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "装配读取失败");
  return response.json();
}

export async function getDeliverySlides(projectId: string): Promise<string> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/delivery/slides`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "提纲生成失败");
  return response.text();
}

export async function runDeliveryChecklist(projectId: string, paperText?: string): Promise<DeliveryChecklist> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/delivery/checklist`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paper_text: paperText ?? null }),
  });
  if (!response.ok) throw await apiError(response, "交付检查失败");
  return response.json();
}

export async function compileDeliveryPaper(projectId: string, source?: string, artifactName = "main.pdf"): Promise<DeliveryCompileReport> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/delivery/compile`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source: source ?? null, artifact_name: artifactName }),
  });
  if (!response.ok) throw await apiError(response, "论文编译失败");
  return response.json();
}

export async function createSubmissionBundle(projectId: string, label = ""): Promise<SubmissionBundleReport> {
  const idempotencyKey = crypto.randomUUID();
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/delivery/submission-bundle`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
    body: JSON.stringify({ label, actor_kind: "member", idempotency_key: idempotencyKey }),
  });
  if (!response.ok) throw await apiError(response, "提交包生成失败（需先有已批准素材）");
  return response.json();
}

export async function verifySubmissionBundle(projectId: string, artifactId: string, restoreCheck = false): Promise<BundleVerification> {
  const response = await apiFetch(
    `${API_URL}/api/projects/${projectId}/delivery/submission-bundle/${artifactId}/verify?restore_check=${restoreCheck}`,
    { cache: "no-store" },
  );
  if (!response.ok) throw await apiError(response, "提交包校验失败");
  return response.json();
}

// ---- 个人云盘 -----------------------------------------------------------

export type DriveFile = {
  id: string;
  name: string;
  size_bytes: number;
  content_hash: string;
  is_archive: boolean;
  mime_type: string | null;
  project_ids: string[];
  created_at: string;
};

export type DriveUsage = {
  owner: string;
  quota_bytes: number;
  used_bytes: number;
  free_bytes: number;
  file_count: number;
  used_percent: number;
  /** 回收站里的文件数（FM-1 起后端返回；它们仍占配额） */
  trashed_count?: number;
};

export type DriveListing = { usage: DriveUsage; files: DriveFile[] };

export async function listDriveFiles(): Promise<DriveListing> {
  const response = await apiFetch(`${API_URL}/api/drive`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "个人云盘读取失败");
  return response.json();
}

export async function uploadDriveFile(file: globalThis.File): Promise<{ file: DriveFile; usage: DriveUsage }> {
  const form = new FormData();
  form.append("file", file);
  const response = await apiFetch(`${API_URL}/api/drive/upload`, { method: "POST", body: form });
  if (!response.ok) throw await apiError(response, "上传失败", { drive_quota_exceeded: "云盘空间不足（200MB 上限）", drive_upload_too_large: "单文件超过上限" });
  return response.json();
}

export async function deleteDriveFile(fileId: string): Promise<{ id: string; deleted: boolean }> {
  const response = await apiFetch(`${API_URL}/api/drive/${fileId}`, { method: "DELETE" });
  if (!response.ok) throw await apiError(response, "删除失败", { drive_file_referenced_by_project: "文件已被项目引用，不能删除" });
  return response.json();
}

export async function importDriveFile(
  projectId: string,
  fileId: string,
  taskId?: string,
): Promise<{ artifact_id: string; artifact_type: string; name: string; is_archive: boolean }> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/drive/import`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file_id: fileId, task_id: taskId ?? null }),
  });
  if (!response.ok) throw await apiError(response, "导入项目失败");
  return response.json();
}

// ---- 知识库与 AI 凭据 -----------------------------------------------------

export type KnowledgeBase = {
  id: string;
  name: string;
  description: string;
  project_id: string | null;
  owner_member_id: string;
  visibility: string;
  document_count: number;
  shares: { member_id: string; permission: string }[];
  created_at: string;
};

export type KbDocument = {
  id: string;
  kb_id: string;
  title: string;
  content_md: string;
  content_md_bytes?: number;
  content_hash: string;
  source_type: string;
  index_status: string;
  indexed_at: string | null;
  index_error: string | null;
  created_at: string;
};

export type AiSettings = {
  member_id: string;
  llm_api_key: string;
  llm_base_url: string;
  llm_model: string;
  embedding_api_key: string;
  embedding_base_url: string;
  embedding_model: string;
  embedding_dimensions: number;
  mineru_api_key: string;
};

export type Conversation = {
  id: string;
  member_id: string;
  kb_id: string | null;
  title: string;
  mode: "chat" | "rag";
  created_at: string;
  updated_at: string;
  messages?: { id: string; role: string; content: string; sources: Record<string, unknown>[]; created_at: string }[];
};

export async function listKbs(): Promise<KnowledgeBase[]> {
  const response = await apiFetch(`${API_URL}/api/kb`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "知识库列表读取失败");
  return response.json();
}

export async function createKb(data: { name: string; description?: string; project_id?: string | null }): Promise<KnowledgeBase> {
  const response = await apiFetch(`${API_URL}/api/kb`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
  });
  if (!response.ok) throw await apiError(response, "知识库创建失败");
  return response.json();
}

export async function listKbDocuments(kbId: string, includeContent = false): Promise<KbDocument[]> {
  const response = await apiFetch(`${API_URL}/api/kb/${kbId}/documents?include_content=${includeContent}`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "文档列表读取失败");
  return response.json();
}

export async function addKbDocument(kbId: string, data: { title: string; content_md: string; source_type?: string }): Promise<KbDocument> {
  const response = await apiFetch(`${API_URL}/api/kb/${kbId}/documents`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
  });
  if (!response.ok) throw await apiError(response, "文档登记失败");
  return response.json();
}

export async function indexKbDocuments(kbId: string, docIds: string[]): Promise<{ indexed: number; failed: number }> {
  const response = await apiFetch(`${API_URL}/api/kb/${kbId}/index`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ doc_ids: docIds }),
  });
  if (!response.ok) throw await apiError(response, "索引失败", { ai_credentials_missing: "请先在设置中配置 Embedding 凭据", hyper_rag_unavailable: "检索服务未启动" });
  return response.json();
}

export async function queryKb(kbId: string, question: string, mode = "hyper"): Promise<{ response: string; entities: unknown[]; text_units: unknown[] }> {
  const response = await apiFetch(`${API_URL}/api/kb/${kbId}/query`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question, mode }),
  });
  if (!response.ok) throw await apiError(response, "检索失败", { ai_credentials_missing: "请先在设置中配置 AI 凭据", hyper_rag_unavailable: "检索服务未启动" });
  return response.json();
}

export type AiProbeResult = { ok: boolean; detail: string; model?: string; dimensions?: number; verified?: boolean };
export type AiProbeReport = Record<"llm" | "embedding" | "mineru", AiProbeResult>;

/** 测试连接：由服务端用已保存的凭据各发一次最小请求（浏览器不持有密钥）。 */
export async function testAiConnections(): Promise<AiProbeReport> {
  const response = await apiFetch(`${API_URL}/api/settings/ai/test`, { method: "POST" });
  if (!response.ok) throw await apiError(response, "测试连接失败");
  return response.json();
}

export async function getAiSettings(): Promise<AiSettings> {
  const response = await apiFetch(`${API_URL}/api/settings/ai`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "AI 设置读取失败");
  return response.json();
}

export async function saveAiSettings(data: Partial<AiSettings>): Promise<AiSettings> {
  const response = await apiFetch(`${API_URL}/api/settings/ai`, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
  });
  if (!response.ok) throw await apiError(response, "AI 设置保存失败");
  return response.json();
}

export async function listConversations(): Promise<Conversation[]> {
  const response = await apiFetch(`${API_URL}/api/ai/conversations`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "会话列表读取失败");
  return response.json();
}

export async function createConversation(data: { title: string; mode: "chat" | "rag"; kb_id?: string | null }): Promise<Conversation> {
  const response = await apiFetch(`${API_URL}/api/ai/conversations`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
  });
  if (!response.ok) throw await apiError(response, "会话创建失败");
  return response.json();
}

export async function deleteConversation(conversationId: string): Promise<void> {
  const response = await apiFetch(`${API_URL}/api/ai/conversations/${conversationId}`, { method: "DELETE" });
  if (!response.ok) throw await apiError(response, "会话删除失败");
}

export async function appendMessage(conversationId: string, role: "user" | "assistant", content: string, sources: Record<string, unknown>[] = []): Promise<void> {
  const response = await apiFetch(`${API_URL}/api/ai/conversations/${conversationId}/messages`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ role, content, sources }),
  });
  if (!response.ok) throw await apiError(response, "消息写入失败");
}

// ---- 图谱数据（代理 hyper-rag-service） ------------------------------------

export type HyperEntity = {
  id: string;
  entity_name: string;
  entity_type: string;
  description: string;
};

export type HyperNeighborData = {
  vertices: Record<string, { entity_name?: string; entity_type?: string; description?: string; [key: string]: unknown }>;
  edges: Record<string, { keywords?: string; summary?: string; [key: string]: unknown }>;
};

export async function getKbEntities(kbId: string, page = 1, pageSize = 20): Promise<{ entities: HyperEntity[]; total: number }> {
  const response = await apiFetch(`${API_URL}/api/kb/${kbId}/graph/entities?page=${page}&page_size=${pageSize}`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "图实体读取失败");
  return response.json();
}

export async function getKbEntityNames(kbId: string, page = 1, pageSize = 200): Promise<{ names: string[]; total: number }> {
  const response = await apiFetch(`${API_URL}/api/kb/${kbId}/graph/entity-names?page=${page}&page_size=${pageSize}`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "实体名读取失败");
  return response.json();
}

export async function getKbVertexNeighbor(kbId: string, vertexId: string): Promise<HyperNeighborData> {
  const response = await apiFetch(`${API_URL}/api/kb/${kbId}/graph/vertex-neighbor/${encodeURIComponent(vertexId)}`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "邻居子图读取失败");
  return response.json();
}

export async function getKbRelationships(kbId: string, page = 1, pageSize = 20): Promise<{ relationships: { id: string; entity_set: string; keywords: string; summary: string }[]; total: number }> {
  const response = await apiFetch(`${API_URL}/api/kb/${kbId}/graph/relationships?page=${page}&page_size=${pageSize}`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "图关系读取失败");
  return response.json();
}

// ---- 设备与接入（UX-4） --------------------------------------------------

export type DeviceRuntimeState = {
  device_id: string;
  connection_id: string | null;
  agent_version: string | null;
  adapter_versions: Record<string, string>;
  capabilities: string[];
  running_run_ids: string[];
  local_queue_length: number;
  user_session_state: string;
  resource_summary: Record<string, unknown>;
  reported_at: string;
};

export type Device = {
  device_id: string;
  organization_id: string;
  agent_id: string;
  owner_member_id: string;
  device_name: string;
  public_key_fingerprint: string;
  platform: string;
  agent_version: string;
  capabilities: string[];
  status: "active" | "revoked";
  token_version?: number;
  created_at: string;
  last_seen: string;
  revoked_at: string | null;
  /** B4：最近一次心跳上报的运行态；设备从未上报过心跳时为 null。 */
  runtime?: DeviceRuntimeState | null;
};

export type DevicePairing = {
  id: string;
  organization_id: string;
  created_by: string;
  status: "PENDING" | "CONSUMED" | "EXPIRED" | "REVOKED";
  expires_at: string;
  device_id: string | null;
  consumed_at: string | null;
  created_at: string;
  pairing_code: string;
  challenge: string;
};

export type DeviceCredential = { device: Device; device_token: string };

export type Organization = { id: string; name: string };

export async function listOrganizations(): Promise<Organization[]> {
  const response = await apiFetch(`${API_URL}/api/organizations`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "组织列表读取失败");
  return response.json();
}

/** 事件流一次取多少：看板只带最近 20 条，时间线/日志类页面需要更多。 */
export async function listProjectEvents(projectId: string, limit = 200, latest = false): Promise<Event[]> {
  // latest=true 取"最近 N 条"：时间线与成员审计要的是最近发生的事，而不是最早的那批
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/events?limit=${limit}${latest ? "&latest=true" : ""}`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "事件流读取失败");
  return response.json();
}

/** 单次执行详情：回答、原始输出与边界结论（列表里的 summary 是压缩过的）。 */
export async function getRun(runId: string): Promise<Run> {
  const response = await apiFetch(`${API_URL}/api/runs/${runId}`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "执行详情读取失败");
  return response.json();
}

export async function listDevices(): Promise<Device[]> {
  const response = await apiFetch(`${API_URL}/api/devices`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "设备列表读取失败");
  return response.json();
}

export async function createDevicePairing(organizationId: string, expiresInSeconds = 900): Promise<DevicePairing> {
  const response = await apiFetch(`${API_URL}/api/devices/pairings`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ organization_id: organizationId, expires_in_seconds: expiresInSeconds }),
  });
  if (!response.ok) throw await apiError(response, "配对创建失败");
  return response.json();
}

export async function revokeDevice(deviceId: string): Promise<Device> {
  const response = await apiFetch(`${API_URL}/api/devices/${deviceId}/revoke`, { method: "POST" });
  if (!response.ok) throw await apiError(response, "设备撤销失败");
  return response.json();
}

export async function rotateDeviceToken(deviceId: string): Promise<DeviceCredential> {
  const response = await apiFetch(`${API_URL}/api/devices/${deviceId}/rotate-token`, { method: "POST" });
  if (!response.ok) throw await apiError(response, "Token 轮换失败");
  return response.json();
}

/** 配对串：base64url(JSON)，与 scripts/connect-agent.ps1 的解码格式一致。 */
export function pairingBlob(pairing: DevicePairing): string {
  const payload = JSON.stringify({
    pairing_id: pairing.id,
    pairing_code: pairing.pairing_code,
    challenge: pairing.challenge,
    expires_at: pairing.expires_at,
  });
  const bytes = new TextEncoder().encode(payload);
  let binary = "";
  bytes.forEach((byte) => { binary += String.fromCharCode(byte); });
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** 项目能力默认清单：与后端 DeviceProjectGrantCreate 的默认值一致。 */
export const DEFAULT_DEVICE_PROJECT_CAPABILITIES = [
  // 「对话」（我的智能体）：单纯对话轮次的领取/回传
  "chat.run",
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
] as const;

export type DeviceProjectGrant = {
  id: string;
  device_id: string;
  agent_id: string;
  project_id: string;
  capabilities: string[];
  granted_by: string;
  expires_at: string;
  revoked_at: string | null;
  created_at: string;
};

export type DeviceProjectCredential = { grant: DeviceProjectGrant; project_token: string };

export async function createDeviceProjectGrant(
  projectId: string,
  payload: { device_id: string; capabilities?: string[]; expires_in_seconds?: number },
): Promise<DeviceProjectCredential> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/device-grants`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw await apiError(response, "项目授权失败");
  return response.json();
}

export async function listDeviceProjectGrants(projectId: string): Promise<DeviceProjectGrant[]> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/device-grants`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "项目授权读取失败");
  return response.json();
}

/**
 * 授权串：base64url(JSON)，与 scripts/connect-agent.ps1 的 -Grant 解码格式一致。
 *
 * project_token 只在授权响应里出现一次，串里带着它，等价于一次短期凭证，
 * 因此向导页要提示用户不要外传、用完即弃。
 */
export function projectGrantBlob(credential: DeviceProjectCredential): string {
  const payload = JSON.stringify({
    project_id: credential.grant.project_id,
    project_token: credential.project_token,
    capabilities: credential.grant.capabilities,
    agent_id: credential.grant.agent_id,
    device_id: credential.grant.device_id,
    expires_at: credential.grant.expires_at,
  });
  const bytes = new TextEncoder().encode(payload);
  let binary = "";
  bytes.forEach((byte) => { binary += String.fromCharCode(byte); });
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/* ================= 账号系统（AUTH-1） ================= */

export type AccountMember = {
  id: string;
  organization_id: string;
  team_id: string | null;
  email: string;
  display_name: string;
  status: "active" | "invited" | "suspended";
  created_at: string;
};

export type AccountView = {
  member: AccountMember;
  has_password: boolean;
  is_admin: boolean;
  last_login_at: string | null;
};

export type AuthSession = {
  token: string;
  expires_at: string;
  account: AccountView;
};

export type InvitationRecord = {
  id: string;
  organization_id: string;
  team_id: string | null;
  email: string;
  role: string;
  token: string;
  status: "PENDING" | "ACCEPTED" | "EXPIRED" | "REVOKED";
  expires_at: string;
  created_at: string;
};

/** 账号相关的稳定错误码 → 人话。未覆盖的码回落到服务端 detail。 */
const ACCOUNT_ERROR_MESSAGES: Record<string, string> = {
  invalid_credentials: "邮箱或密码不正确",
  authentication_required: "请先登录",
  invalid_session: "登录状态已失效，请重新登录",
  session_expired: "登录已过期，请重新登录",
  account_suspended: "该账号已被停用，请联系管理员",
  admin_required: "需要管理员权限",
  invite_code_required: "注册需要邀请码",
  invite_code_invalid: "邀请码无效",
  invite_code_used: "邀请码已被使用",
  invite_code_expired: "邀请码已过期",
  invite_code_email_mismatch: "邀请码与这个邮箱不匹配",
  email_invalid: "邮箱格式不正确",
  email_already_registered: "这个邮箱已经注册过了",
  password_too_short: "密码至少 10 位",
  password_too_long: "密码过长",
  password_all_digits: "密码不能全是数字",
  password_all_letters: "密码不能全是字母",
  current_password_invalid: "当前密码不正确",
  display_name_too_short: "显示名至少 2 个字符",
  too_many_attempts: "尝试次数过多，请稍后再试",
  cannot_suspend_self: "不能停用自己的账号",
  cannot_demote_self: "不能取消自己的管理员权限",
  last_admin_cannot_be_demoted: "系统里至少要保留一个管理员",
  use_password_change_for_self: "改自己的密码请用「修改密码」",
  dev_session_disabled_in_required_mode: "开发会话入口在正式环境下已关闭",
};

export async function registerAccount(payload: {
  email: string;
  password: string;
  display_name: string;
  invite_code?: string;
}): Promise<AuthSession> {
  const response = await apiFetch(`${API_URL}/api/auth/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw await apiError(response, "注册失败", ACCOUNT_ERROR_MESSAGES);
  return response.json();
}

export async function loginAccount(payload: { email: string; password: string }): Promise<AuthSession> {
  const response = await apiFetch(`${API_URL}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw await apiError(response, "登录失败", ACCOUNT_ERROR_MESSAGES);
  return response.json();
}

export async function logoutAccount(): Promise<void> {
  const response = await apiFetch(`${API_URL}/api/auth/logout`, { method: "POST" });
  // 会话可能已经失效：登出永远不该失败到阻塞用户
  if (!response.ok && response.status !== 401) throw await apiError(response, "退出登录失败", ACCOUNT_ERROR_MESSAGES);
}

export async function getCurrentAccount(): Promise<AccountView> {
  const response = await apiFetch(`${API_URL}/api/auth/me`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "读取账号信息失败", ACCOUNT_ERROR_MESSAGES);
  return response.json();
}

export async function changeAccountPassword(payload: { current_password: string; new_password: string }): Promise<AccountView> {
  const response = await apiFetch(`${API_URL}/api/auth/password`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw await apiError(response, "修改密码失败", ACCOUNT_ERROR_MESSAGES);
  return response.json();
}

export async function listAccounts(): Promise<AccountView[]> {
  const response = await apiFetch(`${API_URL}/api/accounts`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "读取账号列表失败", ACCOUNT_ERROR_MESSAGES);
  return response.json();
}

export async function updateAccount(
  memberId: string,
  payload: { status?: "active" | "suspended"; is_admin?: boolean },
): Promise<AccountView> {
  const response = await apiFetch(`${API_URL}/api/accounts/${memberId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw await apiError(response, "更新账号失败", ACCOUNT_ERROR_MESSAGES);
  return response.json();
}

export async function resetAccountPassword(memberId: string): Promise<{ temporary_password: string; account: AccountView }> {
  const response = await apiFetch(`${API_URL}/api/accounts/${memberId}/reset-password`, { method: "POST" });
  if (!response.ok) throw await apiError(response, "重置密码失败", ACCOUNT_ERROR_MESSAGES);
  return response.json();
}

export async function createInvitation(payload: {
  organization_id: string;
  email: string;
  role?: string;
  team_id?: string;
  expires_in_seconds?: number;
}): Promise<InvitationRecord> {
  const response = await apiFetch(`${API_URL}/api/invitations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw await apiError(response, "生成邀请码失败", ACCOUNT_ERROR_MESSAGES);
  return response.json();
}

export async function listInvitations(): Promise<InvitationRecord[]> {
  const response = await apiFetch(`${API_URL}/api/invitations?limit=50`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "读取邀请码失败", ACCOUNT_ERROR_MESSAGES);
  return response.json();
}

/* ---------- 从页面里收敛进来的直连调用（原先各自 fetch，登录后会缺令牌） ---------- */

export async function getPlatformMetrics<T = Record<string, any>>(): Promise<T> {
  const response = await apiFetch(`${API_URL}/api/platform/metrics`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "平台指标读取失败");
  return response.json();
}

export async function getPlatformQuota<T = Record<string, any>>(): Promise<T> {
  const response = await apiFetch(`${API_URL}/api/platform/quota`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "配额读取失败");
  return response.json();
}

export async function getProjectUsage<T = Record<string, any>>(projectId: string): Promise<T> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/usage`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "项目用量读取失败");
  return response.json();
}

/** 云盘文件转 Markdown 并入知识库（MinerU 队列，入队即返回）。 */
export async function enqueueDriveConversion(file: { id: string; name: string }, kbId?: string): Promise<Record<string, any>> {
  const response = await apiFetch(`${API_URL}/api/convert`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      source_type: "drive_file",
      source_id: file.id,
      file_name: file.name,
      ...(kbId ? { kb_id: kbId } : {}),
    }),
  });
  if (!response.ok) {
    throw await apiError(response, "入队失败", {
      mineru_credentials_missing: "请先在设置中配置 MinerU API Token",
    });
  }
  return response.json();
}

/** 内置 AI 普通对话（非流式）。渠道优先：via=channel 表示走了平台渠道（扣免费额度）。 */
export async function askAiChat(messages: { role: string; content: string }[]): Promise<{ content?: string; reply?: string; via?: "channel" | "own_key" }> {
  const response = await apiFetch(`${API_URL}/api/ai/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages, stream: false }),
  });
  if (!response.ok)
    throw await apiError(response, "AI 对话失败", {
      ai_credentials_missing: "平台没有接得住这个模型的渠道，且你未在设置里配置自己的 LLM 凭据",
      llm_quota_exceeded: "平台免费额度已用完（联系管理员调整，或在设置里配置自己的模型）",
      llm_upstream_error: "渠道上游返回错误",
      llm_upstream_unreachable: "渠道上游不可达",
    });
  return response.json();
}

/**
 * 下载成果物内容。
 *
 * 不能直接用 <a href>：普通跳转不带 Authorization，强制鉴权下会 401。
 * 这里带令牌取回后交给浏览器保存，令牌不出现在 URL / 历史记录 / 服务器访问日志里。
 */
export async function downloadArtifactContent(artifactId: string, fileName: string): Promise<void> {
  const response = await apiFetch(`${API_URL}/api/artifacts/${artifactId}/content`);
  if (!response.ok) throw await apiError(response, "下载失败");
  const blob = await response.blob();
  const url = window.URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = fileName || "artifact";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.URL.revokeObjectURL(url);
}


/* ---------- 派单与个人任务中心（P1-1） ---------- */

export type ProjectMemberView = {
  member_id: string;
  role: string;
  display_name: string;
  email: string;
  status: string;
};

export type TaskBoardItem = {
  task: Task;
  project_name: string;
  assignee_member_name: string | null;
  executor_agent_id: string | null;
  lease_active: boolean;
};

export type MyTasks = {
  assigned: TaskBoardItem[];
  running: TaskBoardItem[];
  recent: TaskBoardItem[];
};

export async function listProjectMembers(projectId: string): Promise<ProjectMemberView[]> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/members`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "项目成员读取失败");
  return response.json();
}

export async function getMyTasks(): Promise<MyTasks> {
  const response = await apiFetch(`${API_URL}/api/tasks/mine`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "个人任务读取失败");
  return response.json();
}


/* ---------- 团队：工作量、项目成员管理、多团队（P2） ---------- */

export type MemberWorkload = {
  member_id: string;
  display_name: string;
  email: string;
  status: string;
  is_admin: boolean;
  assigned_open: number;
  running: number;
  completed: number;
  agents: number;
  devices: number;
  devices_active: number;
  projects: number;
  teams: string[];
};

export type Team = { id: string; organization_id: string; name: string; created_at: string };

export type TeamMemberRow = { member_id: string; role: string; display_name: string; email: string; status: string };

export type ProjectMemberRemoval = { member_id: string; released_tasks: number; revoked_grants: number; revoked_devices: string[] };

export async function getTeamWorkload(): Promise<MemberWorkload[]> {
  const response = await apiFetch(`${API_URL}/api/team/workload`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "成员工作量读取失败", ACCOUNT_ERROR_MESSAGES);
  return response.json();
}

export async function updateProjectMember(projectId: string, memberId: string, role: string): Promise<ProjectMemberView> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/members/${memberId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ role }),
  });
  if (!response.ok) throw await apiError(response, "改项目角色失败", ACCOUNT_ERROR_MESSAGES);
  return response.json();
}

export async function removeProjectMember(projectId: string, memberId: string): Promise<ProjectMemberRemoval> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/members/${memberId}`, { method: "DELETE" });
  if (!response.ok) throw await apiError(response, "移出项目失败", ACCOUNT_ERROR_MESSAGES);
  return response.json();
}

export async function listTeams(organizationId: string): Promise<Team[]> {
  const response = await apiFetch(`${API_URL}/api/organizations/${organizationId}/teams`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "团队列表读取失败");
  return response.json();
}

export async function createTeam(organizationId: string, name: string): Promise<Team> {
  const response = await apiFetch(`${API_URL}/api/teams`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ organization_id: organizationId, name }),
  });
  if (!response.ok) throw await apiError(response, "建团队失败", ACCOUNT_ERROR_MESSAGES);
  return response.json();
}

export async function listTeamMembers(teamId: string): Promise<TeamMemberRow[]> {
  const response = await apiFetch(`${API_URL}/api/teams/${teamId}/members`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "团队成员读取失败");
  return response.json();
}

export async function addTeamMember(teamId: string, memberId: string, role = "contributor"): Promise<{ joined_projects: number }> {
  const response = await apiFetch(`${API_URL}/api/teams/${teamId}/members/${memberId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ role }),
  });
  if (!response.ok) throw await apiError(response, "加入团队失败", ACCOUNT_ERROR_MESSAGES);
  return response.json();
}

export async function removeTeamMember(teamId: string, memberId: string): Promise<{ left_projects: number }> {
  const response = await apiFetch(`${API_URL}/api/teams/${teamId}/members/${memberId}`, { method: "DELETE" });
  if (!response.ok) throw await apiError(response, "移出团队失败", ACCOUNT_ERROR_MESSAGES);
  return response.json();
}


/* ---------- P3：能力目录 / 吞吐 / 项目归队 ---------- */

export type CapabilityCard = {
  skill: string;
  version: string;
  inputs: string[];
  outputs: string[];
  description: string;
};

export type CapabilityAgent = {
  agent_id: string;
  display_name: string;
  owner_member_id: string | null;
  owner_name: string;
  agent_status: string;
  last_seen: string | null;
  /** 技能名（归一化后）。版本见 skill_versions；授权范围见 scope_capabilities。 */
  capabilities: string[];
  skill_versions: Record<string, string>;
  cards: CapabilityCard[];
  scope_capabilities: string[];
  package_id: string | null;
  instance_id: string | null;
  /** reported = 内核上报；inferred = 平台按设备探测值推断（界面要标出来，别把猜测当事实）。 */
  package_source: string;
  runs_total: number;
  success_rate: number | null;
  devices: { device_id: string; status: string; last_seen: string | null }[];
  devices_online: number;
};

export type TaskCandidate = {
  agent_id: string;
  display_name: string;
  member_id: string;
  online: boolean;
  load: number;
  runs_total: number;
  succeeded: number;
  failed: number;
  success_rate: number;
  matched_skills: string[];
  missing_skills: string[];
  reason: string;
};

export type TaskCandidates = {
  task_id: string;
  required_capabilities: string[];
  satisfied: TaskCandidate[];
  partial: TaskCandidate[];
  satisfied_total: number;
  partial_total: number;
};

export type CapabilityPackage = {
  package_id: string;
  source: string;
  instances: number;
  instances_online: number;
  skills: string[];
  succeeded: number;
  failed: number;
  total: number;
  success_rate: number | null;
  members: string[];
};

export type UnmetCapabilityTask = {
  task_id: string;
  title: string;
  project_id: string;
  project_name: string;
  required_capabilities: string[];
  missing_capabilities: string[];
  /** AIP-1b：不再只给"没人能跑"，带上"这几台差哪一项" */
  candidates: TaskCandidate[];
};

export type CapabilityCatalog = {
  agents: CapabilityAgent[];
  packages: CapabilityPackage[];
  unmet_tasks: UnmetCapabilityTask[];
};
export type ThroughputDay = { day: string; total: number; succeeded: number; failed: number };
export type ThroughputMember = { member_id: string; display_name: string; total: number; succeeded: number; failed: number };
export type TeamThroughput = { days: number; daily: ThroughputDay[]; members: ThroughputMember[] };

export async function getCapabilityCatalog(): Promise<CapabilityCatalog> {
  const response = await apiFetch(`${API_URL}/api/team/capabilities`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "能力目录读取失败");
  return response.json();
}

/** 一条任务的候选执行体（AIP-1b）：与 auto 调度器共用同一套排序，推荐即派单结果。 */
export async function getTaskCandidates(taskId: string): Promise<TaskCandidates> {
  const response = await apiFetch(`${API_URL}/api/tasks/${taskId}/candidates`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "候选执行体读取失败");
  return response.json();
}

export async function getTeamThroughput(days = 14): Promise<TeamThroughput> {
  const response = await apiFetch(`${API_URL}/api/team/throughput?days=${days}`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "吞吐统计读取失败");
  return response.json();
}

export async function updateProjectTeam(projectId: string, teamId: string | null): Promise<Project> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ team_id: teamId }),
  });
  if (!response.ok) throw await apiError(response, "改项目团队失败", ACCOUNT_ERROR_MESSAGES);
  return response.json();
}


/** 把成员加入项目（或改其项目内角色）。用于"恢复被移出的成员"。 */
export async function addProjectMember(projectId: string, memberId: string, role = "contributor"): Promise<ProjectMemberView> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/members/${memberId}?role=${encodeURIComponent(role)}`, { method: "POST" });
  if (!response.ok) throw await apiError(response, "加入项目失败", ACCOUNT_ERROR_MESSAGES);
  return response.json();
}


/* ---------- W-1 项目工作区：聊天流、成员概览、成果摘要 ---------- */

export type ProjectMessage = {
  id: string;
  project_id: string;
  /** 项目内单调递增序号：聊天流的排序与分页游标 */
  seq: number;
  sender_kind: "human" | "agent" | "system";
  sender_member_id: string | null;
  sender_agent_id: string | null;
  sender_name: string;
  content: string;
  /** text = 人类发言；card = 从事件派生的 Agent/系统卡片（ref_* 可跳转） */
  message_type: "text" | "card";
  ref_event_id: number | null;
  ref_artifact_id: string | null;
  ref_task_id: string | null;
  created_at: string;
};

export type WorkspaceMember = {
  member_id: string;
  display_name: string;
  email: string;
  role: string;
  status: string;
  open_tasks: number;
  running_tasks: number;
  agents: number;
  agents_online: number;
  last_activity: string | null;
};

export type WorkspaceAgent = {
  agent_id: string;
  display_name: string;
  owner_member_id: string;
  owner_name: string;
  status: string;
  connected: boolean;
  last_seen: string | null;
  current_task_id: string | null;
  current_task_title: string | null;
};

export type WorkspaceTaskBrief = {
  id: string;
  title: string;
  status: string;
  stage: string;
  priority: string;
  assignee_member_id: string | null;
  assignee_name: string | null;
  deadline: string | null;
};

export type WorkspaceArtifactBrief = {
  id: string;
  name: string;
  status: string;
  version: number;
  kind: string;
  created_at: string;
};

export type WorkspaceViewer = {
  member_id: string | null;
  role: string | null;
  can_chat: boolean;
  can_manage: boolean;
};

export type ProjectWorkspaceOverview = {
  project: Project;
  /** 我在这个项目里的身份：界面据此隐藏发言框与模式开关 */
  viewer: WorkspaceViewer;
  members: WorkspaceMember[];
  agents: WorkspaceAgent[];
  tasks: { total: number; by_status: Record<string, number>; open_items: WorkspaceTaskBrief[] };
  artifacts: { total: number; approved: number; pending_review: number; recent: WorkspaceArtifactBrief[] };
  messages: ProjectMessage[];
};

/** 工作区首屏聚合：一个请求拿到成员概览 + 任务/成果摘要 + 最近聊天。 */
export async function getProjectWorkspace(projectId: string): Promise<ProjectWorkspaceOverview> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/workspace`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "工作区数据读取失败");
  return response.json();
}

/** 聊天流：before 向后翻历史页，after 向前增量补齐，都不给则取最近 limit 条。 */
export async function getProjectMessages(
  projectId: string,
  options: { before?: number; after?: number; limit?: number } = {},
): Promise<ProjectMessage[]> {
  const params = new URLSearchParams();
  if (options.before !== undefined) params.set("before", String(options.before));
  if (options.after !== undefined) params.set("after", String(options.after));
  if (options.limit !== undefined) params.set("limit", String(options.limit));
  const query = params.toString();
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/messages${query ? `?${query}` : ""}`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "聊天记录读取失败");
  return response.json();
}

export async function postProjectMessage(
  projectId: string,
  content: string,
  refArtifactId?: string,
  refTaskId?: string,
): Promise<ProjectMessage> {
  const body: Record<string, string> = { content };
  if (refArtifactId) body.ref_artifact_id = refArtifactId;
  if (refTaskId) body.ref_task_id = refTaskId;
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw await apiError(response, "发送失败", { project_chat_denied: "当前角色只能查看，不能发言" });
  return response.json();
}

/** 项目设置更新（PATCH 语义：只发要改的字段）。 */
export async function updateProjectSettings(
  projectId: string,
  patch: { team_id?: string | null; goal?: string; target_member_count?: number; task_mode?: TaskMode },
): Promise<Project> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!response.ok) throw await apiError(response, "项目设置更新失败", ACCOUNT_ERROR_MESSAGES);
  return response.json();
}


/* ---------- W-2 成果空间与批量派单 ---------- */

export type DeliverableArtifact = {
  id: string;
  name: string;
  artifact_type: string;
  version: number;
  status: string;
  downstream_allowed: boolean;
  task_id: string | null;
  run_id: string | null;
  created_by: string;
  created_by_kind: string;
  created_at: string;
};

export type DeliverableDocument = DeliverableArtifact & {
  /** 草稿 / 提交 / 批准三层版本 */
  layer: "draft" | "submitted" | "approved";
  has_draft: boolean;
};

export type DeliverableHandoff = {
  id: string;
  task_id: string | null;
  sender_agent_id: string;
  status: string;
  objective: string;
  key_conclusions: string[];
  open_questions: string[];
  created_at: string;
};

export type DeliverableGate = {
  id: string;
  target_type: string;
  target_id: string;
  status: string;
  blocking_count: number;
  approved_by: string | null;
  approved_at: string | null;
};

export type DeliverableReview = {
  id: string;
  target_type: string;
  target_id: string;
  verdict: string;
  reviewer: string;
  reviewer_kind: string;
  summary: string;
  created_at: string;
};

export type ProjectDeliverables = {
  artifacts: { total: number; by_status: Record<string, number>; recent: DeliverableArtifact[] };
  handoffs: { total: number; by_status: Record<string, number>; recent: DeliverableHandoff[] };
  documents: { total: number; by_layer: Record<string, number>; recent: DeliverableDocument[] };
  gates: { total: number; open: number; items: DeliverableGate[] };
  reviews: { total: number; recent: DeliverableReview[] };
  risks: { total: number; open: number };
};

/** 成果空间聚合：成果物 / 交接 / 文档 / 门禁与复核 / 风险。 */
export async function getProjectDeliverables(projectId: string): Promise<ProjectDeliverables> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/deliverables`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "成果空间读取失败");
  return response.json();
}

/** ---- 团队视图 / 生产路径（W2.3，A 的 project_team.py 聚合；前端只渲染，不知道的不编）---- */

export type TeamTaskBrief = {
  task_id: string;
  title: string;
  stage: string;
  status: string;
  priority: string;
  blocked_reason: string;
};

export type ProjectTeamMember = {
  agent_id: string | null;
  agent_name: string;
  device_id: string;
  device_name: string;
  model_name: string;
  online: boolean;
  device_status: string;
  capabilities: string[];
  load: number;
  current_tasks: TeamTaskBrief[];
  next_tasks: TeamTaskBrief[];
  waiting_tasks: TeamTaskBrief[];
  pending_handoffs: { handoff_id: string; task_id: string; status: string }[];
};

export type ProjectTeam = {
  project_id: string;
  agents: ProjectTeamMember[];
  agent_count: number;
  online_count: number;
  task_status_counts: Record<string, number>;
  pending_handoff_count: number;
  generated_at: string;
};

export type ProductionPathNode = {
  artifact_id: string;
  name: string;
  artifact_type: string;
  status: string;
  version: number;
  created_by: string;
  created_at: string;
  approved_by: string | null;
  approved_at: string | null;
  source: { task_id: string; title: string; status: string } | null;
  run_id: string | null;
  handoffs: { handoff_id: string; task_id: string; status: string; receipt_status: string }[];
  downstream_tasks: TeamTaskBrief[];
};

export type ProjectProductionPath = {
  project_id: string;
  nodes: ProductionPathNode[];
  artifact_total: number;
  task_total: number;
  handoff_total: number;
  generated_at: string;
};

/** 团队视图："谁在做什么"——每 Agent 的身份/能力/在线/当前任务/负载/在等什么。 */
export async function getProjectTeam(projectId: string): Promise<ProjectTeam> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/team`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "团队视图读取失败");
  return response.json();
}

/** 生产路径："东西从哪来到哪去"——成果物 → 交接 → 下游任务的因果链（最近 50 个成果物）。 */
export async function getProjectProductionPath(projectId: string): Promise<ProjectProductionPath> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/production-path`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "生产路径读取失败");
  return response.json();
}

export type TaskBulkAssignResult = {
  updated: number;
  task_ids: string[];
  failures: { task_id: string; reason: string }[];
};

/** 批量派单（队长）：assignee 传空串 = 全部收回未指派；逐条返回失败原因。 */
export async function bulkAssignTasks(
  projectId: string,
  taskIds: string[],
  assigneeMemberId: string,
): Promise<TaskBulkAssignResult> {
  const response = await apiFetch(`${API_URL}/api/projects/${projectId}/tasks/assign`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task_ids: taskIds, assignee_member_id: assigneeMemberId }),
  });
  if (!response.ok) {
    throw await apiError(response, "批量派单失败", {
      task_self_claim_disabled_in_manual_mode: "当前是「队长派单」模式：成员不能自己认领任务",
      task_dispatch_requires_lead: "只有队长能把任务派给别人",
      assignee_not_project_member: "目标成员不在这个项目里",
    });
  }
  return response.json();
}

/** 单条派单（走既有 PATCH）：成员认领 = 把自己设为负责人。 */
/** 任务详情（AIP-1d）：任务本体 + 预算执行态 + 证据缺口（后两者服务端读时算）。 */
export async function getTaskDetail(taskId: string): Promise<TaskDetail> {
  const response = await apiFetch(`${API_URL}/api/tasks/${taskId}`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "任务详情读取失败");
  return response.json();
}

export async function assignTask(taskId: string, assigneeMemberId: string): Promise<Task> {
  return updateTask(taskId, { assignee_member_id: assigneeMemberId });
}

/* ================= 「我的智能体」（MY-AGENT）：单纯对话 ================= */
/*
 * 与「项目工作」（任务体系）分开：这些接口不建 Task、不进任务板、不走复核。
 * 执行体侧另有 /api/agents/{id}/chat-turns/*（能力令牌），浏览器这边不碰。
 */

export type MyAgentRole = {
  /** 执行体上真实存在的角色名（`opencode agent list` 探测得到）；空串表示「默认（无角色）」。 */
  name: string;
  /** 角色说明，取自角色定义文件自己的 frontmatter（页面直接显示它，不另起一套文案）。 */
  description: string;
  /** 这个角色**能执行命令或写文件**（来自角色文件的 tools 开关）：页面要如实标注"会改动工作目录"。 */
  executes: boolean;
  /** 角色文件内容的 sha256 前 12 位：页面标"定义版本"，便于与仓库里那份对照（R-4）。 */
  sha256: string;
  bytes: number;
  modified_at: string;
  /** 「硬规则（禁令）」一节的前三条摘要：抽屉里直接摆出来（R-4）。 */
  rules: string[];
  /** 与**部署清单**不一致 = 执行体上这份被人手工改过（页面要告警，不要假装是仓库里那份）。 */
  drifted: boolean;
  /** 部署清单写下的时间（就是"这份角色是什么时候从仓库装过来的"）。 */
  installed_at: string;
};

export type MyAgentEndpoint = {
  device_id: string;
  device_name: string;
  agent_id: string | null;
  platform: string;
  status: string;
  online: boolean;
  project_id: string;
  project_name: string;
  executor: string;
  models: string[];
  default_model: string;
  /** 可选角色；空数组 = 这台执行体没有角色可选（页面只显示「默认」，不显示空下拉）。 */
  roles: MyAgentRole[];
  conversation_count: number;
};

export type MyAgentConversation = {
  id: string;
  title: string;
  model: string;
  /** 会话上选的角色（空 = 默认）。中途可改，**只影响下一轮**。 */
  role: string;
  /** 推理强度；非默认值由执行体用 `--variant` 走 CLI 通道，当前会降级为分段输出。 */
  variant: "" | "minimal" | "high" | "max";
  session_key: string | null;
  turn_count: number;
  project_id: string;
  device_id: string;
  created_at: string;
  updated_at: string;
};

export type MyAgentTurn = {
  id: string;
  conversation_id: string;
  seq: number;
  status: "PENDING" | "CLAIMED" | "DONE" | "FAILED" | "CANCELLED";
  prompt: string;
  content: string;
  model: string;
  /** 这一轮**实际**用的角色（建轮次时从会话抄下来）：历史里"这轮谁跑的"不会被后来的设置改写。 */
  role: string;
  /** 这一轮实际使用的推理强度。 */
  variant: "" | "minimal" | "high" | "max";
  session_key: string | null;
  usage: Record<string, unknown>;
  error: string;
  /** 终态原因（W1.1）：这轮怎么结束的机器口径——completed/failed/cancelled/
   * token_capped/turn_capped/timeout/permission_timeout/unknown。执行中的轮次为空；
   * 平台契约落地前接口不会返回该字段，前端一律按可选处理。 */
  stop_reason?: string;
  /** 这一轮带的输入文件（名子供气泡显示；执行体会把它们下到工作目录）。 */
  artifacts: { artifact_id: string; name: string }[];
  /** 这一轮**产出**的文件（成果物，待审）：页面据此给出「下载 / 转入云盘」。 */
  outputs: MyAgentTurnOutput[];
  created_at: string;
  completed_at: string | null;
};

export type MyAgentTurnOutput = {
  artifact_id: string;
  name: string;
  size_bytes: number;
  artifact_type: string;
  mime_type: string;
  relative_path: string;
};

/** 一轮里的权限请求（S-3 的"待批准卡片"）。`status` 四态：待批 / 已批 / 已拒 / 过期（= 没人批）。 */
export type MyAgentTurnApproval = {
  id: string;
  turn_id: string;
  conversation_id: string;
  status: "PENDING" | "APPROVED" | "DENIED" | "EXPIRED";
  permission: string;
  patterns: string[];
  summary: string;
  tool: string;
  call_id: string;
  decision: string;
  decided_by: string | null;
  created_at: string;
  decided_at: string | null;
};

export async function listMyAgentTurnApprovals(turnId: string): Promise<MyAgentTurnApproval[]> {
  const response = await apiFetch(`${API_URL}/api/my-agent/turns/${turnId}/approvals`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "读取权限请求失败");
  return response.json();
}

/** 对一张待批准卡片做决定：`once` 批准这一次 / `always` 本次会话都允许 / `reject` 拒绝（实测拒绝真的不执行）。 */
export async function decideMyAgentTurnApproval(
  turnId: string,
  requestId: string,
  decision: "once" | "always" | "reject",
): Promise<MyAgentTurnApproval> {
  const response = await apiFetch(`${API_URL}/api/my-agent/turns/${turnId}/approvals/${requestId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision }),
  });
  if (!response.ok)
    throw await apiError(response, "提交决定失败", {
      approval_not_found: "这条请求已经不在了",
      turn_not_found: "这一轮已经不在了",
    });
  return response.json();
}

/** 把一份成果物**复制**进个人云盘（对话产出的「转入云盘」；成果物本身留在项目里）。 */
export async function copyArtifactToDrive(artifactId: string): Promise<{ id: string; name: string }> {
  const response = await apiFetch(`${API_URL}/api/drive/from-artifact/${artifactId}`, { method: "POST" });
  if (!response.ok)
    throw await apiError(response, "转入云盘失败", {
      artifact_not_found: "这份成果物已经取不到了",
      drive_quota_exceeded: "云盘空间不足（200MB 上限）",
      project_membership_required: "你不是这个项目的成员，取不了这份产出",
    });
  return response.json();
}

export type MyAgentTurnEvent = {
  id: string;
  turn_id: string;
  sequence: number;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
};

export async function listMyAgents(): Promise<MyAgentEndpoint[]> {
  const response = await apiFetch(`${API_URL}/api/my-agent/agents`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "执行体列表读取失败");
  return response.json();
}

export async function listMyAgentConversations(): Promise<MyAgentConversation[]> {
  const response = await apiFetch(`${API_URL}/api/my-agent/conversations`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "会话列表读取失败");
  return response.json();
}

export async function createMyAgentConversation(body: {
  project_id: string;
  device_id: string;
  model?: string;
  role?: string;
  variant?: "" | "minimal" | "high" | "max";
  title?: string;
}): Promise<MyAgentConversation> {
  const response = await apiFetch(`${API_URL}/api/my-agent/conversations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw await apiError(response, "会话创建失败", {
      device_grant_missing_chat_capability: "这台设备没有「对话」权限：去设备页重新授权（勾上 chat.run）",
      device_not_active: "设备不在线或已撤销",
    });
  }
  return response.json();
}

export async function deleteMyAgentConversation(conversationId: string): Promise<void> {
  const response = await apiFetch(`${API_URL}/api/my-agent/conversations/${conversationId}`, { method: "DELETE" });
  if (!response.ok) throw await apiError(response, "会话删除失败");
}

/** 改会话设置（角色 / 模型 / 强度）：**只影响下一轮**（已经跑过的轮次保留实际设置）。 */
export async function updateMyAgentConversation(
  conversationId: string,
  body: { role?: string; model?: string; variant?: "" | "minimal" | "high" | "max"; title?: string },
): Promise<MyAgentConversation> {
  const response = await apiFetch(`${API_URL}/api/my-agent/conversations/${conversationId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw await apiError(response, "会话设置保存失败");
  return response.json();
}

/** 转入项目生产（W1.2）：本会话轮次的产出 → 任务输入附件 + 新任务。
 * target_agent_id/handoff_context 非空时，A 会在任务描述里写**结构化交接块**——
 * 是描述不是硬性钉定：任务仍按能力匹配领取（响应 handoff_note 会原文说明，页面照实展示）。 */
export type MyAgentPromoteInput = {
  title: string;
  description?: string;
  output_artifact_ids?: string[];
  target_agent_id?: string | null;
  handoff_context?: string | null;
};

export type MyAgentPromoteResult = {
  task: Task;
  attached_artifact_ids: string[];
  handoff_note: string;
};

export async function promoteMyAgentConversation(
  conversationId: string,
  input: MyAgentPromoteInput,
): Promise<MyAgentPromoteResult> {
  const response = await apiFetch(`${API_URL}/api/my-agent/conversations/${conversationId}/promote`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      title: input.title,
      description: input.description ?? "",
      output_artifact_ids: input.output_artifact_ids ?? [],
      target_agent_id: input.target_agent_id ?? null,
      handoff_context: input.handoff_context ?? null,
    }),
  });
  if (!response.ok) throw await apiError(response, "转入项目生产失败");
  return response.json();
}

export async function listMyAgentTurns(conversationId: string): Promise<MyAgentTurn[]> {
  const response = await apiFetch(`${API_URL}/api/my-agent/conversations/${conversationId}/turns`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "对话记录读取失败");
  return response.json();
}

export async function sendMyAgentMessage(
  conversationId: string,
  content: string,
  artifactIds: string[] = [],
): Promise<MyAgentTurn> {
  const response = await apiFetch(`${API_URL}/api/my-agent/conversations/${conversationId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content, artifact_ids: artifactIds }),
  });
  if (!response.ok) {
    throw await apiError(response, "发送失败", { conversation_busy: "上一轮还在跑，等它结束再发" });
  }
  return response.json();
}

export async function listMyAgentTurnEvents(turnId: string): Promise<MyAgentTurnEvent[]> {
  const response = await apiFetch(`${API_URL}/api/my-agent/turns/${turnId}/events`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "过程事件读取失败");
  return response.json();
}

export async function stopMyAgentTurn(turnId: string): Promise<MyAgentTurn> {
  const response = await apiFetch(`${API_URL}/api/my-agent/turns/${turnId}/stop`, { method: "POST" });
  if (!response.ok) {
    throw await apiError(response, "停止失败", { turn_already_finished: "这一轮已经结束了" });
  }
  return response.json();
}

// ---- 个人云盘文件管理器（FM-1/FM-2） ---------------------------------------
//
// 这一组走的是新的节点树接口（`/api/drive/nodes*`）。旧的 `listDriveFiles/uploadDriveFile/...`
// 保持不动（`/drive` 老入口与别处的调用还在用）。**下载必须走 `apiFetch`**：普通 `<a href>` 不带
// Authorization，在 `PLATFORM_AUTH_MODE=required` 下会 401（踩过这个坑）。

export type DriveNode = {
  id: string;
  parent_id: string | null;
  kind: "file" | "directory";
  name: string;
  size_bytes: number;
  content_hash: string | null;
  mime_type: string | null;
  is_archive: boolean;
  source_artifact_id: string | null;
  revision: number;
  scan_status: string;
  is_root: boolean;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
  trashed_with: string | null;
  trashed_count?: number;
};

export type DriveBreadcrumb = { id: string; name: string; kind: string; is_root: boolean };

export type DriveNodeListing = {
  parent: DriveNode;
  nodes: DriveNode[];
  total: number;
  next_cursor: string | null;
  truncated: boolean;
  usage: DriveUsage;
  breadcrumb: DriveBreadcrumb[];
};

export type DriveProjectRef = {
  id: string;
  drive_node_id: string;
  project_id: string;
  artifact_id: string | null;
  imported_by: string;
  imported_at: string;
  legacy_import: boolean;
};

export type DriveAuditEvent = {
  id: string;
  action: string;
  capability: string;
  decision: string;
  reason: string;
  content_hash: string | null;
  size_bytes: number;
  revision: number | null;
  created_at: string;
};

export type DriveNodeDetail = {
  node: DriveNode;
  breadcrumb: DriveBreadcrumb[];
  refs: DriveProjectRef[];
  audit: DriveAuditEvent[];
};

export type DriveExtraction = {
  node: DriveNode;
  archive_id: string;
  files: number;
  bytes: number;
  directories: number;
  skipped_nested: string[];
  skipped_junk: number;
  usage: DriveUsage;
};

const DRIVE_UPLOAD_MESSAGES: Record<string, string> = {
  file_name_conflict: "同目录下已有同名文件（不会覆盖，请改名或换目录）",
  file_quota_exceeded: "云盘空间不足",
  file_name_invalid: "文件名不合法（不能含路径分隔符、保留名或尾随点/空格）",
  drive_upload_too_large: "单个文件超过上传上限",
  file_empty: "空文件不能上传",
};

export function driveUploadMessages(): Record<string, string> {
  return { ...DRIVE_UPLOAD_MESSAGES };
}

export async function listDriveNodes(params: {
  parentId?: string | null;
  query?: string;
  sort?: string;
  cursor?: string | null;
  limit?: number;
} = {}): Promise<DriveNodeListing> {
  const search = new URLSearchParams();
  if (params.parentId) search.set("parent_id", params.parentId);
  if (params.query) search.set("query", params.query);
  if (params.sort) search.set("sort", params.sort);
  if (params.cursor) search.set("cursor", params.cursor);
  if (params.limit) search.set("limit", String(params.limit));
  const response = await apiFetch(`${API_URL}/api/drive/nodes?${search.toString()}`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "目录读取失败");
  return response.json();
}

export async function getDriveNode(nodeId: string): Promise<DriveNodeDetail> {
  const response = await apiFetch(`${API_URL}/api/drive/nodes/${nodeId}`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "文件详情读取失败", { file_node_not_found: "文件不存在或已被清除" });
  return response.json();
}

/** 下载：拿字节 → 浏览器造链接（带 Authorization 的 fetch，不能用 <a href>）。 */
export async function downloadDriveNode(node: DriveNode): Promise<void> {
  const response = await apiFetch(`${API_URL}/api/drive/nodes/${node.id}/content`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "下载失败", { file_node_not_found: "文件不存在" });
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = node.name;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

export async function createDriveDirectory(parentId: string | null, name: string): Promise<{ node: DriveNode; usage: DriveUsage }> {
  const response = await apiFetch(`${API_URL}/api/drive/directories`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ parent_id: parentId, name }),
  });
  if (!response.ok) throw await apiError(response, "新建文件夹失败", DRIVE_UPLOAD_MESSAGES);
  return response.json();
}

/**
 * 上传（带进度）：用 XHR 而不是 fetch —— fetch 拿不到上传进度，"文件级进度条"就没法做真。
 * 403/404 之外的状态统一走 `apiError` 的文案映射。
 */
export function uploadDriveNode(
  file: globalThis.File,
  parentId: string | null,
  onProgress?: (percent: number) => void,
): Promise<{ node: DriveNode; usage: DriveUsage }> {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    if (parentId) form.append("parent_id", parentId);
    form.append("file", file);
    const request = new XMLHttpRequest();
    request.open("POST", `${API_URL}/api/drive/files`);
    const token = getSessionToken();
    if (token) request.setRequestHeader("Authorization", `Bearer ${token}`);
    request.upload.onprogress = (event) => {
      if (event.lengthComputable && onProgress) onProgress(Math.round((event.loaded / event.total) * 100));
    };
    request.onload = () => {
      let payload: any = {};
      try {
        payload = JSON.parse(request.responseText || "{}");
      } catch {
        payload = {};
      }
      if (request.status >= 200 && request.status < 300) {
        resolve(payload);
        return;
      }
      const detail = String(payload?.detail ?? "");
      const code = detail.includes(":") ? detail.split(":")[0] : detail;
      if (request.status === 403 || request.status === 404 || request.status === 401) {
        reject(new ApiError(request.status, DRIVE_UPLOAD_MESSAGES[code] ?? (detail || "上传失败")));
        return;
      }
      reject(new ApiError(request.status, DRIVE_UPLOAD_MESSAGES[code] ?? (detail || "上传失败")));
    };
    request.onerror = () => reject(new ApiError(0, "上传失败：网络中断"));
    request.send(form);
  });
}

export async function renameDriveNode(nodeId: string, name: string, expectedRevision?: number): Promise<{ node: DriveNode }> {
  const response = await apiFetch(`${API_URL}/api/drive/nodes/${nodeId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, expected_revision: expectedRevision }),
  });
  if (!response.ok) {
    throw await apiError(response, "改名失败", { ...DRIVE_UPLOAD_MESSAGES, file_revision_conflict: "这个文件刚被别人改过，请刷新后再试" });
  }
  return response.json();
}

export async function moveDriveNode(nodeId: string, parentId: string | null, expectedRevision?: number): Promise<{ node: DriveNode }> {
  const response = await apiFetch(`${API_URL}/api/drive/nodes/${nodeId}/move`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ parent_id: parentId, expected_revision: expectedRevision }),
  });
  if (!response.ok) {
    throw await apiError(response, "移动失败", {
      file_name_conflict: "目标目录里已有同名文件",
      file_directory_cycle: "不能把目录移动到它自己的子目录里",
      file_revision_conflict: "这个文件刚被别人改过，请刷新后再试",
    });
  }
  return response.json();
}

export async function copyDriveNode(nodeId: string, parentId?: string | null, name?: string): Promise<{ node: DriveNode; usage: DriveUsage }> {
  const response = await apiFetch(`${API_URL}/api/drive/nodes/${nodeId}/copy`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ parent_id: parentId ?? null, name: name ?? null }),
  });
  if (!response.ok) throw await apiError(response, "复制失败", DRIVE_UPLOAD_MESSAGES);
  return response.json();
}

export async function trashDriveNode(nodeId: string): Promise<{ id: string; trashed_count: number; usage: DriveUsage }> {
  const response = await apiFetch(`${API_URL}/api/drive/nodes/${nodeId}`, { method: "DELETE" });
  if (!response.ok) throw await apiError(response, "删除失败", { file_root_immutable: "根目录不能删除" });
  return response.json();
}

export async function listDriveTrash(): Promise<{ nodes: DriveNode[]; usage: DriveUsage }> {
  const response = await apiFetch(`${API_URL}/api/drive/trash`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "回收站读取失败");
  return response.json();
}

export async function restoreDriveNode(nodeId: string): Promise<{ node: DriveNode; usage: DriveUsage }> {
  const response = await apiFetch(`${API_URL}/api/drive/nodes/${nodeId}/restore`, { method: "POST" });
  if (!response.ok) {
    throw await apiError(response, "恢复失败", {
      file_name_conflict: "原目录下已有同名文件，先改名或删掉那个再恢复",
      file_not_trashed: "这个文件不在回收站里",
    });
  }
  return response.json();
}

export async function purgeDriveNode(nodeId: string): Promise<{ id: string; objects_deleted: number; objects_pending: number }> {
  const response = await apiFetch(`${API_URL}/api/drive/trash/${nodeId}`, { method: "DELETE" });
  if (!response.ok) {
    throw await apiError(response, "彻底清除失败", {
      file_referenced_by_project: "这个文件被项目引用过，不能彻底清除",
      file_directory_not_empty: "目录里还有东西，先清空再彻底清除",
      file_not_trashed: "只有回收站里的文件能彻底清除",
    });
  }
  return response.json();
}

export async function extractDriveArchive(nodeId: string, targetParentId?: string | null): Promise<DriveExtraction> {
  const response = await apiFetch(`${API_URL}/api/drive/extractions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ node_id: nodeId, target_parent_id: targetParentId ?? null }),
  });
  if (!response.ok) {
    throw await apiError(response, "解压失败", {
      archive_format_unsupported: "这个格式还不支持解压（目前支持 zip / tar / tar.gz / tgz）",
      archive_path_unsafe: "归档里有不安全的路径或链接，已拒绝整份解压",
      archive_too_many_entries: "归档里条目太多",
      archive_uncompressed_size_exceeded: "解压后体积超过上限",
      archive_ratio_exceeded: "压缩比异常（疑似压缩炸弹），已拒绝",
      archive_target_conflict: "归档里有重复或冲突的路径",
      archive_empty: "归档是空的",
      file_quota_exceeded: "云盘空间不足，解压需要更多空间",
    });
  }
  return response.json();
}

export async function retryDriveCleanup(): Promise<Record<string, number>> {
  const response = await apiFetch(`${API_URL}/api/drive/cleanup/retry`, { method: "POST" });
  if (!response.ok) throw await apiError(response, "清理重试失败");
  return response.json();
}

// ---- Agent 工作区文件服务（FM-3/FM-4） -------------------------------------
//
// 工作区那侧是**队列语义**：人点一下 = 入队一个操作，Agent 领到才执行。所以这里没有"同步返回目录"
// 的接口——`runWorkspaceOperation` 负责"入队 → 轮询 → 拿到终态"，页面只等它。

export type AgentWorkspace = {
  id: string;
  agent_id: string;
  device_id: string | null;
  project_id: string | null;
  display_name: string;
  workspace_identity: string;
  kind: "cloud" | "desktop";
  status: "online" | "offline" | "unavailable";
  policy_version: string;
  protected_paths: string[];
  last_seen_at: string | null;
  created_at: string;
  updated_at: string;
};

export type WorkspaceOperation = {
  id: string;
  workspace_id: string;
  operation_type: string;
  relative_path: string;
  arguments: Record<string, any>;
  status: "queued" | "claimed" | "running" | "succeeded" | "failed" | "cancelled" | "expired";
  result: Record<string, any> | null;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
};

export type WorkspaceEntry = {
  name: string;
  relative_path: string;
  kind: "file" | "directory";
  size_bytes: number;
  modified_at: string;
  is_symlink: boolean;
};

export async function listAgentWorkspaces(): Promise<{
  workspaces: AgentWorkspace[];
  operations: WorkspaceOperation[];
  large_file_bytes: number;
}> {
  const response = await apiFetch(`${API_URL}/api/agent-workspaces`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "工作区列表读取失败");
  return response.json();
}

export async function getAgentWorkspace(workspaceId: string): Promise<{ workspace: AgentWorkspace; operations: WorkspaceOperation[]; audit: any[] }> {
  const response = await apiFetch(`${API_URL}/api/agent-workspaces/${workspaceId}`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "工作区读取失败", { workspace_not_found: "这个工作区不存在（或 Agent 已注销）" });
  return response.json();
}

export async function createWorkspaceOperation(
  workspaceId: string,
  payload: { operation_type: string; relative_path?: string; arguments?: Record<string, any>; idempotency_key: string; fail_when_offline?: boolean },
): Promise<{ operation: WorkspaceOperation; workspace: AgentWorkspace }> {
  const response = await apiFetch(`${API_URL}/api/agent-workspaces/${workspaceId}/operations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    throw await apiError(response, "操作入队失败", {
      workspace_path_outside_root: "路径越界（只能用工作区内的相对路径）",
      workspace_path_invalid: "路径不合法",
      workspace_path_required: "要指定路径",
      operation_idempotency_conflict: "同一个幂等键对应了不同的内容",
    });
  }
  return response.json();
}

export async function getWorkspaceOperation(workspaceId: string, operationId: string): Promise<{ operation: WorkspaceOperation }> {
  const response = await apiFetch(`${API_URL}/api/agent-workspaces/${workspaceId}/operations/${operationId}`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "操作状态读取失败");
  return response.json();
}

export async function cancelWorkspaceOperation(workspaceId: string, operationId: string): Promise<{ operation: WorkspaceOperation }> {
  const response = await apiFetch(`${API_URL}/api/agent-workspaces/${workspaceId}/operations/${operationId}/cancel`, { method: "POST" });
  if (!response.ok) throw await apiError(response, "取消失败");
  return response.json();
}

export async function createWorkspaceTransfer(payload: {
  source_type: "drive" | "workspace" | "temp";
  target_type: "drive" | "workspace" | "temp";
  workspace_id?: string;
  operation_id?: string;
  expected_size?: number;
  expected_hash?: string;
  ttl_seconds?: number;
}): Promise<{ transfer: { id: string; storage_key: string; status: string } }> {
  const response = await apiFetch(`${API_URL}/api/workspace-transfers`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw await apiError(response, "传输会话创建失败");
  return response.json();
}

export async function putWorkspaceTransferContent(transferId: string, content: Blob): Promise<void> {
  const response = await apiFetch(`${API_URL}/api/workspace-transfers/${transferId}/content`, {
    method: "PUT",
    headers: { "Content-Type": "application/octet-stream" },
    body: content,
  });
  if (!response.ok) {
    throw await apiError(response, "内容上传失败", {
      workspace_transfer_hash_mismatch: "内容哈希与声明不一致",
      workspace_transfer_size_mismatch: "内容大小与声明不一致",
    });
  }
}

export async function getWorkspaceTransferContent(transferId: string): Promise<Blob> {
  const response = await apiFetch(`${API_URL}/api/workspace-transfers/${transferId}/content`, { cache: "no-store" });
  if (!response.ok) {
    throw await apiError(response, "内容下载失败", { workspace_transfer_expired: "传输会话已过期（重新发起一次）" });
  }
  return response.blob();
}

const WORKSPACE_TERMINAL = new Set(["succeeded", "failed", "cancelled", "expired"]);

export function isWorkspaceOperationFinished(operation: WorkspaceOperation): boolean {
  return WORKSPACE_TERMINAL.has(operation.status);
}

/** 入队 + 轮询到终态。`onTick` 让页面能显示 queued → running 的真实过程（不乐观假成功）。 */
export async function runWorkspaceOperation(
  workspaceId: string,
  payload: { operation_type: string; relative_path?: string; arguments?: Record<string, any>; fail_when_offline?: boolean },
  options: { onTick?: (operation: WorkspaceOperation) => void; timeoutMs?: number; intervalMs?: number } = {},
): Promise<WorkspaceOperation> {
  const key = `${payload.operation_type}:${payload.relative_path ?? ""}:${Date.now()}:${Math.random().toString(36).slice(2, 8)}`;
  const created = await createWorkspaceOperation(workspaceId, { ...payload, idempotency_key: key });
  let operation = created.operation;
  options.onTick?.(operation);
  const deadline = Date.now() + (options.timeoutMs ?? 120_000);
  while (!isWorkspaceOperationFinished(operation)) {
    if (Date.now() > deadline) {
      throw new Error("操作等太久还没结果（Agent 可能不在线）；可以在下面的队列里取消它");
    }
    await new Promise((resolve) => setTimeout(resolve, options.intervalMs ?? 1200));
    const latest = await getWorkspaceOperation(workspaceId, operation.id);
    operation = latest.operation;
    options.onTick?.(operation);
  }
  return operation;
}

export function workspaceOperationPayload(operation: WorkspaceOperation): Record<string, any> {
  return { operation_type: operation.operation_type, relative_path: operation.relative_path, arguments: operation.arguments };
}

// ---- 云盘文件访问授权（FM-5） ----------------------------------------------

export type FileAccessGrant = {
  id: string;
  owner_member_id: string;
  agent_id: string;
  device_id: string | null;
  project_id: string;
  task_id: string | null;
  run_id: string | null;
  conversation_id: string | null;
  turn_id: string | null;
  scope_type: "file" | "folder" | "drive";
  root_node_id: string | null;
  include_future_nodes: boolean;
  capabilities: string[];
  expires_at: string;
  revoked_at: string | null;
  revoke_reason: string | null;
  created_at: string;
  active?: boolean;
  node_count?: number;
  node_ids?: string[];
};

export async function listFileAccessGrants(params: { nodeId?: string; agentId?: string } = {}): Promise<{ grants: FileAccessGrant[] }> {
  const search = new URLSearchParams();
  if (params.nodeId) search.set("node_id", params.nodeId);
  if (params.agentId) search.set("agent_id", params.agentId);
  const response = await apiFetch(`${API_URL}/api/file-access-grants?${search.toString()}`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "授权列表读取失败");
  return response.json();
}

export async function createFileAccessGrant(payload: {
  agentId: string;
  deviceId?: string | null;
  projectId: string;
  nodeId?: string | null;
  scopeType?: "file" | "folder" | "drive";
  includeFutureNodes?: boolean;
  expiresInSeconds?: number;
}): Promise<{ grant: FileAccessGrant }> {
  const response = await apiFetch(`${API_URL}/api/file-access-grants`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      agent_id: payload.agentId,
      device_id: payload.deviceId ?? null,
      project_id: payload.projectId,
      node_id: payload.nodeId ?? null,
      scope_type: payload.scopeType ?? "file",
      include_future_nodes: payload.includeFutureNodes ?? false,
      expires_in_seconds: payload.expiresInSeconds ?? 7 * 24 * 3600,
    }),
  });
  if (!response.ok) {
    throw await apiError(response, "授权创建失败", {
      file_access_capability_denied: "这个权限不允许授予（第一期只开放只读与导入）",
      device_revoked: "设备已被撤销，不能再授权",
      device_project_grant_invalid: "这台设备还没被授权进入该项目",
      file_node_not_found: "文件不存在（或不是你的）",
    });
  }
  return response.json();
}

export async function revokeFileAccessGrant(grantId: string, reason = "revoked_by_member"): Promise<{ grant: FileAccessGrant }> {
  const response = await apiFetch(`${API_URL}/api/file-access-grants/${grantId}/revoke`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason }),
  });
  if (!response.ok) throw await apiError(response, "撤销失败");
  return response.json();
}

// ---- 显式跨空间传输（FM-6） ------------------------------------------------
//
// 语义是"显式动作"：点一次 = 复制一次（不是后台同步）。云端 → 工作区由平台把内容搬进传输会话、
// 再入队一个 upload 操作；工作区 → 云端则反过来，Agent 传完再由人确认"存进云盘"。
// 同名默认**不覆盖**：冲突会明确报出来，让用户改名或换目录。

export type FileTransferSession = {
  id: string;
  operation_id: string | null;
  workspace_id: string | null;
  source_type: string;
  target_type: string;
  source_id: string | null;
  source_hash: string | null;
  expected_hash: string | null;
  expected_size: number;
  uploaded_size: number;
  status: string;
  expires_at: string;
};

export async function copyDriveFileToWorkspace(payload: {
  nodeId: string;
  workspaceId: string;
  relativePath?: string | null;
  overwrite?: boolean;
}): Promise<{ transfer: FileTransferSession; operation: WorkspaceOperation }> {
  const response = await apiFetch(`${API_URL}/api/file-transfers/drive-to-workspace`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      node_id: payload.nodeId,
      workspace_id: payload.workspaceId,
      relative_path: payload.relativePath ?? null,
      overwrite: payload.overwrite ?? false,
    }),
  });
  if (!response.ok) {
    throw await apiError(response, "复制到工作区失败", {
      file_node_not_found: "这个文件读不到（可能已被清除）",
      workspace_not_found: "工作区不存在（Agent 可能已注销）",
      file_transfer_source_invalid: "目录不能直接复制到工作区（先选文件）",
      workspace_path_protected: "目标路径受保护（工作区根/.git/平台元数据目录）",
    });
  }
  return response.json();
}

export async function copyWorkspaceFileToDrive(payload: {
  workspaceId: string;
  relativePath: string;
}): Promise<{ transfer: FileTransferSession; operation: WorkspaceOperation }> {
  const response = await apiFetch(`${API_URL}/api/file-transfers/workspace-to-drive`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ workspace_id: payload.workspaceId, relative_path: payload.relativePath }),
  });
  if (!response.ok) {
    throw await apiError(response, "保存到云盘失败", {
      workspace_path_outside_root: "路径越界（只能用工作区内的相对路径）",
      workspace_path_invalid: "路径不合法",
    });
  }
  return response.json();
}

/** 人确认后把传输会话的内容写进云盘（同名冲突 409，不静默覆盖）。 */
export async function saveTransferToDrive(payload: {
  transferId: string;
  parentId?: string | null;
  name?: string | null;
}): Promise<{ node: DriveNode; transfer_id: string; saved_bytes: number }> {
  const response = await apiFetch(`${API_URL}/api/file-transfers/save-to-drive`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ transfer_id: payload.transferId, parent_id: payload.parentId ?? null, name: payload.name ?? null }),
  });
  if (!response.ok) {
    throw await apiError(response, "存进云盘失败", {
      file_name_conflict: "云盘里已有同名文件（换个名字或先处理那个）",
      file_quota_exceeded: "云盘空间不足",
      workspace_transfer_expired: "传输会话已过期（重新发起一次）",
      file_upload_hash_mismatch: "内容与声明的哈希不一致，已拒绝",
    });
  }
  return response.json();
}

export async function listFileTransfers(params: { workspaceId?: string; limit?: number } = {}): Promise<{
  transfers: (FileTransferSession & { operation: WorkspaceOperation | null; retryable: boolean })[];
}> {
  const search = new URLSearchParams();
  if (params.workspaceId) search.set("workspace_id", params.workspaceId);
  if (params.limit) search.set("limit", String(params.limit));
  const response = await apiFetch(`${API_URL}/api/file-transfers?${search.toString()}`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "传输历史读取失败");
  return response.json();
}

/** 分片上传的默认片大小（与服务端观察到的对齐；服务端没收到片时会回 0 = 由客户端定）。 */
export const WORKSPACE_TRANSFER_CHUNK_BYTES = 8 * 1024 * 1024;

/** 传输会话的分片游标：已收到哪些、还缺哪些（续传只补缺的）。 */
export async function getWorkspaceTransferParts(transferId: string): Promise<{
  part_size_bytes: number;
  expected_size: number;
  total_parts: number;
  received_parts: number[];
  missing_parts: number[];
  parts: { part_number: number; size_bytes: number; content_hash: string }[];
}> {
  const response = await apiFetch(`${API_URL}/api/workspace-transfers/${transferId}/parts`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "分片游标读取失败", { workspace_transfer_not_found: "传输会话不存在或已过期" });
  return response.json();
}

async function sha256OfBlob(content: Blob): Promise<string> {
  const buffer = await content.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

/**
 * 把内容分片传进传输会话：**大文件才走这条**（小文件一次 PUT 更省事）。
 *
 * 续传的要点与内核侧一致：先问服务端已经收到哪些片，哈希一致的就跳过；每片单独重试；
 * 全部到齐再 `complete`。这样中途断网只要重传缺的那几片。
 */
export async function putWorkspaceTransferChunked(
  transferId: string,
  content: Blob,
  options: { chunkBytes?: number; retries?: number; onProgress?: (percent: number) => void } = {},
): Promise<{ mode: "single" | "chunked"; parts: number; uploaded: number; resumed: number }> {
  const chunkBytes = options.chunkBytes ?? WORKSPACE_TRANSFER_CHUNK_BYTES;
  const retries = options.retries ?? 3;
  if (content.size <= chunkBytes) {
    await putWorkspaceTransferContent(transferId, content);
    options.onProgress?.(100);
    return { mode: "single", parts: 1, uploaded: 1, resumed: 0 };
  }
  const cursor = await getWorkspaceTransferParts(transferId);
  const partSize = cursor.part_size_bytes || chunkBytes;
  const existing = new Map(cursor.parts.map((item) => [item.part_number, item.content_hash]));
  const parts: Blob[] = [];
  for (let offset = 0; offset < content.size; offset += partSize) {
    parts.push(content.slice(offset, Math.min(offset + partSize, content.size)));
  }
  let uploaded = 0;
  let resumed = 0;
  for (const [index, chunk] of parts.entries()) {
    const number = index + 1;
    const digest = await sha256OfBlob(chunk);
    if (existing.get(number) === digest) {
      resumed += 1;
      options.onProgress?.(Math.round(((index + 1) / parts.length) * 100));
      continue;
    }
    let lastError = "";
    for (let attempt = 1; attempt <= retries; attempt += 1) {
      const response = await apiFetch(`${API_URL}/api/workspace-transfers/${transferId}/parts/${number}`, {
        method: "PUT",
        headers: { "Content-Type": "application/octet-stream", "X-Part-SHA256": digest },
        body: chunk,
      });
      if (response.ok) {
        uploaded += 1;
        lastError = "";
        break;
      }
      const failure = await apiError(response, `第 ${number} 片上传失败`, {
        workspace_transfer_part_hash_mismatch: "这一片的内容与声明的哈希不一致",
        workspace_transfer_expired: "传输会话已过期（重新发起）",
      });
      lastError = `${failure.message}（第 ${attempt} 次）`;
      await new Promise((resolve) => setTimeout(resolve, Math.min(2000, 300 * attempt)));
    }
    if (lastError) {
      throw new Error(`第 ${number} 片反复失败：${lastError}`);
    }
    options.onProgress?.(Math.round(((index + 1) / parts.length) * 100));
  }
  const completed = await apiFetch(`${API_URL}/api/workspace-transfers/${transferId}/complete`, { method: "POST" });
  if (!completed.ok) {
    throw await apiError(completed, "分片收口失败", {
      workspace_transfer_parts_missing: "还有分片没传上去（平台拒绝收口，避免拼出半个文件）",
      workspace_transfer_hash_mismatch: "整体哈希与声明不符，已拒绝",
    });
  }
  return { mode: "chunked", parts: parts.length, uploaded, resumed };
}
/* ---------- LLM 渠道与全员免费额度 ----------
   管理员在「渠道」页录入 OpenAI 兼容上游，成员用平台额度调 /api/llm/v1/chat/completions。
   硬约束：**api_key 只在服务端**——列表/详情只回 key_hint（末 4 位），前端绝不显示明文。 */

export type LlmChannel = {
  id: string;
  name: string;
  base_url: string;
  models: string[];
  priority: number;
  enabled: boolean;
  /** 末 4 位提示（明文 key 永不回传）。 */
  key_hint: string;
  last_check_at: string | null;
  last_check_ok: boolean | null;
  last_check_detail: string;
  last_latency_ms: number | null;
  created_at: string;
  updated_at: string;
};

export type LlmChannelList = { channels: LlmChannel[]; default_quota_tokens: number };

export type LlmChannelInput = {
  name: string;
  base_url: string;
  /** 新建时必填；编辑时留空 = 不变（服务端语义：缺省/null 不变、空串清空）。 */
  api_key?: string;
  models: string[];
  priority?: number;
  enabled?: boolean;
};

export type LlmCheckResult = { ok: boolean; detail: string; latency_ms: number; model: string; checked_at: string };

export type LlmSpeedRound = { round: number; ok: boolean; latency_ms: number; detail: string };
export type LlmSpeedResult = {
  model: string;
  rounds: LlmSpeedRound[];
  ok_rounds: number;
  avg_ms: number | null;
  min_ms: number | null;
  max_ms: number | null;
};

export type LlmQuota = {
  member_id: string;
  token_limit: number;
  tokens_used: number;
  tokens_remaining: number | null;
  unlimited: boolean;
};

export type LlmUsage = {
  totals: { requests: number; ok_requests: number; total_tokens: number };
  members: {
    member_id: string;
    requests: number;
    log_tokens: number;
    tokens_used: number | null;
    token_limit: number | null;
    unlimited: boolean;
  }[];
};

/** 渠道接口的错误码 → 人话（管理员看得懂"为什么存不进去"）。 */
const LLM_CHANNEL_MESSAGES: Record<string, string> = {
  llm_channel_not_found: "渠道不存在（可能已被删除）",
  llm_name_required: "渠道名称不能为空",
  llm_name_taken: "已有同名渠道，换个名字",
  llm_base_url_invalid: "Base URL 必须以 http:// 或 https:// 开头",
  llm_models_required: "至少填一个模型名（成员按模型名路由到渠道）",
  llm_model_required: "这条渠道没有可测的模型",
  llm_upstream_error: "上游返回错误（看下方详情）",
  llm_upstream_unreachable: "上游不可达（网络或地址不对）",
  admin_required: "只有管理员能管理渠道",
};

export async function listLlmChannels(): Promise<LlmChannelList> {
  const response = await apiFetch(`${API_URL}/api/admin/llm-channels`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "渠道列表读取失败", LLM_CHANNEL_MESSAGES);
  return response.json();
}

export async function createLlmChannel(payload: LlmChannelInput): Promise<LlmChannel> {
  const response = await apiFetch(`${API_URL}/api/admin/llm-channels`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw await apiError(response, "渠道创建失败", LLM_CHANNEL_MESSAGES);
  return response.json();
}

/** 编辑：api_key 留空表示**不变**（不传该字段即可，服务端不会清空）。 */
export async function updateLlmChannel(id: string, payload: Partial<LlmChannelInput>): Promise<LlmChannel> {
  const response = await apiFetch(`${API_URL}/api/admin/llm-channels/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw await apiError(response, "渠道保存失败", LLM_CHANNEL_MESSAGES);
  return response.json();
}

export async function deleteLlmChannel(id: string): Promise<void> {
  const response = await apiFetch(`${API_URL}/api/admin/llm-channels/${id}`, { method: "DELETE" });
  if (!response.ok) throw await apiError(response, "渠道删除失败", LLM_CHANNEL_MESSAGES);
}

export async function checkLlmChannel(id: string, model?: string): Promise<LlmCheckResult> {
  const response = await apiFetch(`${API_URL}/api/admin/llm-channels/${id}/check`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(model ? { model } : {}),
  });
  if (!response.ok) throw await apiError(response, "渠道检测失败", LLM_CHANNEL_MESSAGES);
  return response.json();
}

export async function speedTestLlmChannel(id: string, rounds = 3, model?: string): Promise<LlmSpeedResult> {
  const response = await apiFetch(`${API_URL}/api/admin/llm-channels/${id}/speed-test`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rounds, ...(model ? { model } : {}) }),
  });
  if (!response.ok) throw await apiError(response, "渠道测速失败", LLM_CHANNEL_MESSAGES);
  return response.json();
}

export async function getLlmUsage(): Promise<LlmUsage> {
  const response = await apiFetch(`${API_URL}/api/admin/llm-usage`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "用量总览读取失败", LLM_CHANNEL_MESSAGES);
  return response.json();
}

export async function getLlmQuota(memberId: string): Promise<LlmQuota> {
  const response = await apiFetch(`${API_URL}/api/admin/llm-quotas/${memberId}`, { cache: "no-store" });
  if (!response.ok) throw await apiError(response, "成员额度读取失败", LLM_CHANNEL_MESSAGES);
  return response.json();
}

/** 设置成员额度；**负数 = 不限量**。 */
export async function setLlmQuota(memberId: string, tokenLimit: number): Promise<LlmQuota> {
  const response = await apiFetch(`${API_URL}/api/admin/llm-quotas/${memberId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token_limit: tokenLimit }),
  });
  if (!response.ok) throw await apiError(response, "成员额度保存失败", LLM_CHANNEL_MESSAGES);
  return response.json();
}
