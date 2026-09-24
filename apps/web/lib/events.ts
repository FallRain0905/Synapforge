/**
 * 事件流的"人话"层：把 `task.claimed`、`review.created` 这类机器码翻译成
 * 用户能直接读懂的一条记录（标题 / 细节 / 角色 / 关联去处）。
 *
 * 为什么单独放一个模块：时间线、项目总览、审核页都要展示事件，
 * 翻译规则必须只有一处，否则同一个事件在不同页面会有不同说法。
 *
 * 三条约定（避免"看起来详细其实在编"）：
 * 1. 只翻译 `event_type` 已知的族；未知类型原样显示机器码，不猜含义；
 * 2. 细节行只来自 `payload` 里真实存在的字段，缺就留空；
 * 3. `actor_kind` 不可信（历史数据里人类操作常被记成 system），所以按**主体 id** 判定角色。
 */

import { Event } from "./api";

export type EventCategory = "task" | "run" | "review" | "handoff" | "artifact" | "agent" | "project" | "other";

export const EVENT_CATEGORIES: { id: EventCategory; label: string }[] = [
  { id: "task", label: "任务" },
  { id: "run", label: "执行" },
  { id: "review", label: "审核与证据" },
  { id: "handoff", label: "交接" },
  { id: "artifact", label: "成果物" },
  { id: "agent", label: "设备与 Agent" },
  { id: "project", label: "项目与模板" },
];

/** 事件族 → 分类。前缀命中即归类，未命中的进 "other"（仍会显示，不隐藏）。 */
const CATEGORY_PREFIXES: { prefix: string; category: EventCategory }[] = [
  { prefix: "task.", category: "task" },
  { prefix: "run.", category: "run" },
  { prefix: "review.", category: "review" },
  { prefix: "gate.", category: "review" },
  { prefix: "handoff.", category: "handoff" },
  { prefix: "artifact.", category: "artifact" },
  { prefix: "document.", category: "artifact" },
  { prefix: "evidence.", category: "review" },
  { prefix: "agent.process.", category: "run" },
  { prefix: "agent.agent.", category: "run" },
  { prefix: "agent.tool.", category: "run" },
  { prefix: "agent.file.", category: "run" },
  { prefix: "agent.", category: "agent" },
  { prefix: "device.", category: "agent" },
  { prefix: "project.", category: "project" },
  { prefix: "pack.", category: "project" },
  { prefix: "git.", category: "project" },
  { prefix: "boundary.", category: "review" },
];

/** 事件类型 → 中文标题。只写确定含义的；没把握的一律留空走兜底文案。 */
const EVENT_TITLES: Record<string, string> = {
  "task.created": "新建任务",
  "task.updated": "任务被修改",
  "task.claimed": "Agent 领取任务",
  "task.lease.recycled": "租约过期，任务回到待领取",
  "task.lease.expired": "任务租约过期",
  "task.progress": "任务执行中",
  "task.result_submitted": "任务结果已提交",
  "task.result": "任务结果已提交",
  "task.upstream_gate_invalidated": "上游门禁失效，任务被挡",
  "task.execute": "任务开始执行",
  "run.created": "登记一次执行",
  "run.completed": "执行完成",
  "run.failed": "执行失败",
  "run.blocked": "执行被挡下",
  "run.event": "执行过程事件",
  "review.created": "提交审核结论",
  "review.approve": "审核通过",
  "review.rejected": "审核退回",
  "gate.invalidated": "门禁失效",
  "handoff.created": "创建交接",
  "handoff.accepted": "接收交接",
  "handoff.rejected": "退回交接",
  "artifact.created": "新增成果物",
  "artifact.content_stored": "成果物内容已落库",
  "artifact.multipart.abort": "分片上传中断",
  "artifact.read": "读取成果物",
  "artifact.revised": "成果物出新版本",
  "artifact.archived": "成果物已归档",
  "artifact.reviewed": "成果物复核完成",
  "artifact.submitted": "成果物提交审核",
  "document.comment": "文档评论",
  "evidence.created": "补交证据",
  "agent.registered": "Agent 注册",
  "agent.online": "Agent 上线",
  "agent.offline": "Agent 离线",
  // 执行体过程事件（DP-2 追加的实时反馈通道）
  "agent.process.started": "开始执行（执行体已启动）",
  "agent.agent.message": "执行体输出",
  "agent.tool.completed": "执行体调用了工具",
  "agent.file.changed": "执行体修改了文件",
  "agent.process.exited": "执行结束",
  "agent.artifact.create": "Agent 产出成果物",
  "agent.event": "Agent 事件",
  "project.created": "创建项目",
  "project.restored": "恢复项目",
  "project.exported": "导出项目",
  "project.imported": "导入项目",
};

/** 状态/结论类枚举 → 中文（出现在 payload 里，展示时必须翻译）。 */
const VALUE_LABELS: Record<string, string> = {
  APPROVED: "通过",
  NEEDS_REVISION: "需返工",
  REJECTED: "已退回",
  WAITING_REVIEW: "待审核",
  READY: "待领取",
  CLAIMED: "已领取",
  RUNNING: "执行中",
  FAILED: "失败",
  BLOCKED: "被挡",
  CANCELLED: "已取消",
  SUCCEEDED: "成功",
  PASS: "通过",
  PASS_WITH_ASSUMPTIONS: "带假设通过",
  FAIL: "未通过",
  PENDING: "待接收",
  RECEIVED: "已接收",
  RELAY: "传递",
  REVIEW: "评审",
  FINAL: "最终交付",
  heartbeat_timeout: "心跳超时（超过 90 秒没有心跳）",
  gateway_disconnected: "平台连接断开",
  input_snapshot_changed: "输入快照已变化（结果不再可信）",
  executor_not_configured: "任务没声明执行方式",
  device_revoked: "设备被撤销",
  revoked_by_member: "被成员撤销",
};

export type PresentedEvent = {
  id: string;
  category: EventCategory;
  /** 中文标题；未知类型时回落到机器码本身。 */
  title: string;
  /** 机器码，保留给审计（折叠显示）。 */
  rawType: string;
  /** 主体展示名（Agent 用注册名，成员按 id 规则判定）。 */
  actorLabel: string;
  actorRole: "member" | "agent" | "system";
  /** 由 payload 拼出的细节行；没有可用字段时为空串。 */
  detail: string;
  objectType: string | null;
  objectId: string | null;
  /** 该对象的去处（有页面可跳时才给）。 */
  link: { href: string; label: string } | null;
  sequence: number;
  createdAt: string;
};

const OBJECT_ROUTES: Record<string, { href: string; label: string }> = {
  task: { href: "/tasks", label: "去任务页" },
  artifact: { href: "/artifacts", label: "去成果物库" },
  review: { href: "/review", label: "去审核门禁" },
  gate: { href: "/review", label: "去审核门禁" },
  handoff: { href: "/handoffs", label: "去交接中心" },
  agent: { href: "/devices", label: "去设备页" },
  device: { href: "/devices", label: "去设备页" },
  run: { href: "/runs", label: "去运行控制台" },
};

export function categoryOf(eventType: string): EventCategory {
  const match = CATEGORY_PREFIXES.find((item) => eventType.startsWith(item.prefix));
  return match ? match.category : "other";
}

/** 对象类型（`task`/`artifact`/…）→ 页面上的中文名。 */
const OBJECT_LABELS: Record<string, string> = {
  task: "任务",
  artifact: "成果物",
  review: "审核",
  gate: "门禁",
  handoff: "交接单",
  agent: "Agent",
  device: "设备",
  run: "执行",
  project: "项目",
  evidence: "证据",
};

export function objectLabel(value: unknown): string {
  const text = String(value ?? "").trim();
  if (!text) return "";
  return OBJECT_LABELS[text] ?? text;
}

/** 证据类型（同一批枚举值在 payload 里以 `*_type` 出现）。 */
const EVIDENCE_TYPE_LABELS: Record<string, string> = {
  artifact: "成果物",
  run: "执行记录",
  review: "审核结论",
  external: "外部材料",
};

export function humanizeValue(value: unknown): string {
  const text = String(value ?? "").trim();
  if (!text) return "";
  return VALUE_LABELS[text] ?? text;
}

/** 角色判定优先看主体 id 前缀：历史事件里 `actor_kind` 普遍是默认的 `system`。 */
function roleOf(actor: string, actorKind?: string): PresentedEvent["actorRole"] {
  if (actor.startsWith("member-")) return "member";
  if (actor.startsWith("agent-") || actor.startsWith("device-")) return "agent";
  if (actorKind === "agent") return "agent";
  if (actorKind === "member") return "member";
  return "system";
}

const ROLE_LABELS: Record<PresentedEvent["actorRole"], string> = {
  member: "成员",
  agent: "Agent",
  system: "系统",
};

function shortId(value: unknown): string {
  const text = String(value ?? "");
  return text.length > 8 ? `${text.slice(0, 8)}…` : text;
}

/** 每条事件各自的细节拼装：只取 payload 里真实存在的键。 */
function detailOf(eventType: string, payload: Record<string, unknown>): string {
  const parts: string[] = [];
  const push = (label: string, value: unknown, humanize = true) => {
    const text = value === undefined || value === null ? "" : String(value).trim();
    if (!text) return;
    parts.push(`${label}${humanize ? humanizeValue(text) : text}`);
  };

  if (eventType.startsWith("review.")) {
    push("结论：", payload.verdict);
    push("对象：", objectLabel(payload.target_type), false);
    if (payload.comment) parts.push(`意见：${String(payload.comment).slice(0, 80)}`);
  } else if (eventType.startsWith("handoff.")) {
    push("状态：", payload.status);
    push("类型：", payload.handoff_type);
    push("交接单：", shortId(payload.handoff_id), false);
  } else if (eventType.startsWith("run.")) {
    push("状态：", payload.status);
    if (payload.summary) parts.push(String(payload.summary).slice(0, 120));
    push("执行：", shortId(payload.run_id ?? payload.id), false);
  } else if (eventType.startsWith("artifact.")) {
    push("成果物：", shortId(payload.artifact_id), false);
    push("版本：", payload.version);
  } else if (eventType.startsWith("agent.")) {
    push("原因：", payload.reason);
    push("Agent：", payload.agent_id, false);
    if (payload.summary) parts.push(String(payload.summary).slice(0, 100));
  } else if (eventType.startsWith("agent.process.") || eventType === "agent.agent.message" || eventType.startsWith("agent.tool.") || eventType.startsWith("agent.file.")) {
    // Gateway 把执行体事件原样放在 payload.event.payload 下
    const inner = (payload.event as Record<string, unknown> | undefined)?.payload as Record<string, unknown> | undefined;
    if (inner) {
      if (inner.executor) parts.push(`执行体：${inner.executor}`);
      if (inner.command) parts.push(`命令：${String(inner.command).slice(0, 80)}`);
      if (inner.text) parts.push(String(inner.text).slice(0, 200));
      if (inner.tool) parts.push(`工具：${inner.tool}`);
      if (inner.path) parts.push(`文件：${inner.path}`);
      if (inner.exit_code !== undefined && inner.exit_code !== null) parts.push(`退出码：${inner.exit_code}`);
      if (inner.summary && eventType === "agent.process.exited") parts.push(String(inner.summary).slice(0, 160));
    }
  } else if (eventType.startsWith("task.")) {
    push("状态：", payload.status);
    push("执行者：", payload.assignee, false);
    if (payload.message) parts.push(String(payload.message).slice(0, 90));
    if (payload.summary) parts.push(String(payload.summary).slice(0, 120));
  } else if (eventType.startsWith("gate.")) {
    push("原因：", payload.reason);
    push("对象：", objectLabel(payload.target_type), false);
    push("门禁：", shortId(payload.gate_id), false);
  } else if (eventType.startsWith("evidence.")) {
    const kind = String(payload.evidence_type ?? "").trim();
    if (kind) parts.push(`证据类型：${EVIDENCE_TYPE_LABELS[kind] ?? kind}`);
  } else if (eventType.startsWith("document.")) {
    push("文档：", shortId(payload.artifact_id), false);
    if (payload.body) parts.push(`内容：${String(payload.body).slice(0, 80)}`);
  }

  return parts.join(" · ");
}

export function presentEvent(event: Event, agentNames: Record<string, string> = {}): PresentedEvent {
  const payload = (event.payload ?? {}) as Record<string, unknown>;
  const role = roleOf(event.actor, event.actor_kind);
  const actorLabel = agentNames[event.actor] || (role === "system" ? "平台系统" : event.actor);
  const category = categoryOf(event.event_type);
  const objectType = event.object_type ?? null;
  const route = objectType ? OBJECT_ROUTES[objectType] : undefined;

  return {
    id: event.id,
    category,
    title: EVENT_TITLES[event.event_type] ?? event.event_type,
    rawType: event.event_type,
    actorLabel,
    actorRole: role,
    detail: detailOf(event.event_type, payload),
    objectType,
    objectId: event.object_id ?? null,
    link: route ? { ...route } : null,
    sequence: event.sequence,
    createdAt: event.created_at,
  };
}

export function roleLabel(role: PresentedEvent["actorRole"]): string {
  return ROLE_LABELS[role];
}

export type TimelineEntry = {
  /** 代表条目（保留最新一条的时间与 payload 细节）。 */
  item: PresentedEvent;
  /** 被合并的同质事件条数；1 表示没有合并。 */
  count: number;
  /** 合并区间里最早的一条时间（与 item.createdAt 不同时才显示）。 */
  firstAt: string;
};

/**
 * 合并连续的同质事件。
 *
 * 批量操作（应用模板包一次建 16 个任务、一次上传 20 个成果物）会灌进十几条
 * 几乎一样的记录，把真正有信息量的事件挤下去。这里把**相邻、同类型、同主体、同细节**
 * 的事件合成一条（`×N`），列表顺序与时间线语义都不变（仍然最新在前）。
 */
export function collapseRepeats(items: PresentedEvent[]): TimelineEntry[] {
  const entries: TimelineEntry[] = [];
  for (const item of items) {
    const previous = entries[entries.length - 1];
    if (previous && previous.item.rawType === item.rawType && previous.item.actorLabel === item.actorLabel && previous.item.detail === item.detail) {
      previous.count += 1;
      previous.firstAt = item.createdAt;
      continue;
    }
    entries.push({ item, count: 1, firstAt: item.createdAt });
  }
  return entries;
}

/** 按本地日期分组：今天 / 昨天 / 具体日期。返回顺序为"最新的一天在前"。 */
export function groupByDay(entries: TimelineEntry[]): { key: string; label: string; items: TimelineEntry[] }[] {
  const groups = new Map<string, { key: string; label: string; items: TimelineEntry[] }>();
  const today = new Date();
  const startOfToday = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime();

  for (const entry of entries) {
    const date = new Date(entry.item.createdAt);
    const dayStart = Number.isNaN(date.getTime())
      ? 0
      : new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
    const key = Number.isNaN(date.getTime()) ? "unknown" : `${date.getFullYear()}-${date.getMonth() + 1}-${date.getDate()}`;
    if (!groups.has(key)) {
      const label =
        key === "unknown"
          ? "时间未知"
          : dayStart === startOfToday
            ? "今天"
            : dayStart === startOfToday - 86400000
              ? "昨天"
              : date.toLocaleDateString("zh-CN", { month: "long", day: "numeric", weekday: "short" });
      groups.set(key, { key, label, items: [] });
    }
    groups.get(key)!.items.push(entry);
  }
  return [...groups.values()];
}

/** 相对时间：让"多久之前"一眼可读，精确时间仍随条目显示。 */
export function relativeTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  const seconds = Math.round((Date.now() - date.getTime()) / 1000);
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  const days = Math.floor(seconds / 86400);
  if (days < 30) return `${days} 天前`;
  return date.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
}

/**
 * Run 摘要的读取器：Agent 侧把**回答放在最前面**，`---` 之后是执行诊断。
 * 界面上只展示回答（诊断折叠在"原始输出"里），所以这里负责切分。
 */
export function splitRunSummary(summary: string): { answer: string; diagnostics: string } {
  const text = (summary ?? "").trim();
  if (!text) return { answer: "", diagnostics: "" };
  const marker = text.indexOf("\n---\n");
  if (marker !== -1) {
    return { answer: text.slice(0, marker).trim(), diagnostics: text.slice(marker + 5).trim() };
  }

  // 兼容旧格式（`codex exit=… | reply: 回答 | warnings: …`）：把回答单独摘出来，
  // 否则历史记录会被整条当成"回答"显示，读起来像一串机器码。
  const legacy = text.match(/\|\s*reply:\s*([^|]*)/);
  if (legacy) {
    return { answer: legacy[1].trim(), diagnostics: text.replace(legacy[0], "").replace(/\s*\|\s*\|/g, " | ").trim() };
  }
  return { answer: text, diagnostics: "" };
}

