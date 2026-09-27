/**
 * 状态词典（产品重定位阶段 0 / W0.4）。
 *
 * 定位：所有"状态标签"的**含义层**——每个状态一句用户可读的解释；
 * 颜色等视觉层仍在 `components/ui.tsx` 的 `statusTone`。两处的新增状态要同步。
 *
 * 取值来源（2026-09-26 与后端 contracts 对齐）：
 * - TaskStatus：DRAFT/READY/CLAIMED/RUNNING/WAITING_REVIEW/APPROVED/BLOCKED/NEEDS_REVISION/FAILED/CANCELLED
 * - Run：RUNNING/SUCCEEDED/FAILED（含 CANCELLED）
 * - Artifact：DRAFT/PENDING_REVIEW/APPROVED/REJECTED
 * - Handoff 交接：PENDING/ACCEPTED/REJECTED；复核结论 PASS/PASS_WITH_ASSUMPTIONS/NEEDS_REVISION/BLOCKED
 * - Gate：PENDING/PASSED/REJECTED/BLOCKED
 */
export type StatusDomain = "task" | "run" | "artifact" | "handoff" | "review" | "gate";

export const STATUS_DICTIONARY: Record<StatusDomain, Record<string, string>> = {
  task: {
    DRAFT: "草稿：还没排入执行，先写清楚要做什么",
    READY: "已可领取：在线 Agent 会自动领走执行（平台是拉取模型，没有「开始」按钮）",
    CLAIMED: "Agent 已领取：正在准备执行",
    RUNNING: "执行中：过程与输出可在运行控制台查看",
    WAITING_REVIEW: "执行完成：成果物待审核入库",
    APPROVED: "成果已批准：可被下游任务作为正式输入",
    NEEDS_REVISION: "被退回修改：按复核意见改完可再次进入待办",
    BLOCKED: "已阻塞：阻断下游依赖，需要人工解除",
    FAILED: "执行失败：可重试或转人工处理",
    CANCELLED: "已取消：不再执行",
  },
  run: {
    RUNNING: "正在执行：实时输出在运行控制台",
    SUCCEEDED: "执行成功：产出以成果物为准（是否可用看审核）",
    FAILED: "执行失败：查看输出定位原因",
    CANCELLED: "已取消",
  },
  artifact: {
    DRAFT: "草稿：未提交审核",
    PENDING_REVIEW: "待审核：等待人工确认，尚不能作为下游正式输入",
    APPROVED: "已批准：可被下游任务使用",
    REJECTED: "已退回：需修改后重新提交",
  },
  handoff: {
    PENDING: "交接待接收：下游 Agent 或成员尚未确认",
    ACCEPTED: "已接收：下游可以继续推进",
    REJECTED: "被退回：需要补充材料后重新交接",
  },
  review: {
    PASS: "复核通过",
    PASS_WITH_ASSUMPTIONS: "带假设通过：结论成立的前提已记录",
    NEEDS_REVISION: "需要修改：按复核意见处理后重新提交",
    BLOCKED: "复核阻塞：缺少必要材料",
    PENDING_REVIEW: "待复核",
    ACCEPTED: "复核通过",
    REJECTED: "复核退回",
  },
  gate: {
    PENDING: "门禁待确认：通过前下游不会开始",
    PASSED: "门禁已通过",
    REJECTED: "门禁未通过：按意见处理后重新提交",
    BLOCKED: "门禁阻塞",
  },
};

/** 阶段 0 的两条固定口径文案（规划文档 §6：骨架≠结果；自动=受约束推进）。 */
export const REPOSITIONING_COPY = {
  skeletonNotResult:
    "模板与工作流生成的是任务和成果物骨架，不是最终结果；结果由 Agent 按任务执行、经审核后产生。",
  autoSemantics:
    "「自动」指受约束的调度与推进（派发、领取、门禁检查），不承诺完全无人值守；关键门禁仍需人工确认。",
} as const;

/** 单轮结束原因（stop_reason）的用户可读口径——与执行体/平台侧枚举一致（W1.1）。 */
export const STOP_REASON_LABEL: Record<string, string> = {
  completed: "", // 正常完成不加注
  failed: "执行失败",
  cancelled: "已被停止",
  token_capped: "达到 token 预算上限，已收尾",
  turn_capped: "达到轮次上限，已收尾",
  timeout: "超时结束",
  permission_timeout: "权限请求超时未批，已结束",
  unknown: "结束原因未上报",
};
