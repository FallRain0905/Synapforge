/**
 * 任务"能不能跑起来"的诊断层。
 *
 * 为什么需要它：任务不是靠界面上的按钮启动的——平台是**拉取模型**（决策 D5/DE5），
 * 桌面端内核连上平台后自己领取 READY 任务。所以用户问"启动按钮在哪"时，
 * 界面的责任不是给一个假的"开始"按钮，而是明确回答：
 *
 *   1. 这个任务现在能不能被领走？不能的话卡在哪一条？
 *   2. 要让它能跑，下一步该做什么（一个按钮）。
 *
 * **规则必须镜像服务端**（`apps/api/app/store.py`）：
 * - 可领取状态：`claim_task` 只接受 `READY` / `NEEDS_REVISION`，且不能有生效中的租约；
 * - 上游门禁：`_task_dependencies_ready` 要求依赖任务 APPROVED、输入交接已接收且门禁通过、
 *   输入成果物 APPROVED 且 `downstream_allowed`；
 * - 执行方式：Agent 侧 `agentd._execute_task` 没有 `worker_executor`/`worker_command` 就显式失败
 *   （`executor_not_configured`）——这一条也在界面提前拦住。
 * 与服务端不一致时，界面会给出假的"能跑/不能跑"，那比不给提示更糟。
 */

import { Artifact, Handoff, Task } from "./api";

export type RunState =
  | "runnable" // 可被 Agent 领取
  | "no_executor" // 缺少执行方式
  | "waiting_upstream" // 上游任务/交接/成果物没过
  | "no_agent" // 没有可用的 Agent
  | "leased" // 已被领取（有生效租约）
  | "in_progress" // 执行中
  | "waiting_review" // 等待人工审核
  | "done" // 已通过或已取消
  | "blocked"; // 人工阻塞

export type TaskDiagnosis = {
  state: RunState;
  /** 一句话说明当前状态（列表里直接显示）。 */
  label: string;
  /** 卡住的原因（有则显示，写清"差什么"）。 */
  reason: string;
  /** 下一步动作：要么给一个能点的按钮，要么给一个去处。 */
  action?: { kind: "executor" | "link"; href?: string; label: string };
};

const CLAIMABLE_STATUSES = ["READY", "NEEDS_REVISION"];

export type TaskFlowContext = {
  tasks: Task[];
  handoffs: Handoff[];
  artifacts: Artifact[];
  /** 有项目授权且在线（`online`）的 Agent 数量。 */
  onlineAgents: number;
};

function taskById(tasks: Task[], id: string): Task | undefined {
  return tasks.find((task) => task.id === id);
}

/** 复刻 `store._task_dependencies_ready`：返回第一处"还没满足"的可读原因。 */
export function upstreamBlocker(task: Task, context: TaskFlowContext): string {
  for (const dependencyId of task.dependency_task_ids ?? []) {
    const dependency = taskById(context.tasks, dependencyId);
    if (!dependency) return "依赖的任务已不存在（数据可能被删过）";
    if (String(dependency.status) !== "APPROVED") {
      return `等待上游任务「${dependency.title}」（当前 ${dependency.status}）通过审核`;
    }
  }

  for (const handoffId of task.input_handoff_ids ?? []) {
    const handoff = context.handoffs.find((item) => item.id === handoffId);
    if (!handoff) return "输入交接单已不存在";
    if ((handoff.receipt_status ?? "PENDING") !== "ACCEPTED") return "输入交接还没被接收";
    if (!["PASS", "PASS_WITH_ASSUMPTIONS"].includes(String(handoff.status))) return "输入交接结论不是通过";
  }

  for (const artifactId of task.input_artifacts ?? []) {
    const artifact = context.artifacts.find((item) => item.id === artifactId);
    if (!artifact) return "输入成果物已不存在";
    if (String(artifact.status) !== "APPROVED") return `输入成果物「${artifact.name}」还没通过审核`;
    if (artifact.downstream_allowed === false) return `输入成果物「${artifact.name}」不允许下游使用`;
  }

  return "";
}

/** 任务是否声明了执行方式（与 Agent 侧判定一致：codex 或声明式命令）。 */
export function hasExecutor(task: Task): boolean {
  const policy = (task.resource_policy ?? {}) as Record<string, unknown>;
  const executor = String(policy.worker_executor ?? "").trim();
  const command = policy.worker_command;
  const hasCommand = Array.isArray(command) ? command.length > 0 : typeof command === "string" && command.trim().length > 0;
  return Boolean(executor) || hasCommand;
}

export function diagnoseTask(task: Task, context: TaskFlowContext): TaskDiagnosis {
  const status = String(task.status);

  if (status === "APPROVED") return { state: "done", label: "已通过", reason: "" };
  if (status === "CANCELLED") return { state: "done", label: "已取消", reason: "" };
  if (status === "BLOCKED") {
    return {
      state: "blocked",
      label: "被人工阻塞",
      reason: task.blocked_reason || "任务被标记为阻塞，解除后才会回到待领取队列",
      action: { kind: "link", href: "/tasks", label: "在任务页解除" },
    };
  }
  if (status === "WAITING_REVIEW") {
    return {
      state: "waiting_review",
      label: "等待人工审核",
      reason: "批准必须由人工复核产生，审核通过后下游任务才会解锁",
      action: { kind: "link", href: "/review", label: "去审核" },
    };
  }
  if (status === "RUNNING") return { state: "in_progress", label: "执行中", reason: "" };
  if (status === "CLAIMED") {
    return { state: "leased", label: "已被 Agent 领取", reason: "正在执行，完成后会自动上报结果" };
  }
  if (status === "FAILED") {
    return {
      state: "runnable",
      label: "上次执行失败",
      reason: String(task.blocked_reason ?? "") || "可在任务详情里重试（回到待领取队列）",
    };
  }

  if (!CLAIMABLE_STATUSES.includes(status)) {
    return { state: "waiting_upstream", label: status, reason: "状态不是可领取状态（需要先进入待办）" };
  }

  const blocker = upstreamBlocker(task, context);
  if (blocker) {
    return {
      state: "waiting_upstream",
      label: "等待上游",
      reason: blocker,
      action: { kind: "link", href: "/review", label: "去审核门禁" },
    };
  }

  if (!hasExecutor(task)) {
    return {
      state: "no_executor",
      label: "缺少执行方式",
      reason: "任务没声明执行体，Agent 领走后只会报 executor_not_configured——先设置执行方式",
      action: { kind: "executor", label: "设置执行方式" },
    };
  }

  if (context.onlineAgents === 0) {
    return {
      state: "no_agent",
      label: "等待 Agent",
      reason: "没有在线 Agent：把桌面端接入并授权到本项目，任务才会被领走",
      action: { kind: "link", href: "/devices", label: "去接入 Agent" },
    };
  }

  return {
    state: "runnable",
    label: "等待 Agent 领取",
    reason: "任务可执行：内核连上平台后通常几秒内领走（无需在这里点开始）",
  };
}

/** 首个能推进流程的任务（用来给"启动任务"按钮定位）。 */
export function firstActionable(tasks: Task[], context: TaskFlowContext): Task | null {
  return tasks.find((task) => diagnoseTask(task, context).state === "no_executor") ?? null;
}