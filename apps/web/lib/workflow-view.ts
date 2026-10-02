/**
 * workflow-view DTO 类型与图数据转换（可视化 V1，消费 A 的 coordination_store.build_workflow_view）。
 *
 * 原则（设计文档 §5.2）：节点/边来自服务端权威对象，前端不自行猜状态机；
 * 未知节点/边类型显示通用样式继续渲染；状态映射统一走 lib/status-dictionary 的视觉语义。
 */

import type { WorkflowNodeSpec } from "./api";

export type WorkflowViewSummary = Record<string, number>;

export type WorkflowViewNode = {
  id: string;
  kind: string; // "task"（当前实现）；未知 kind 前端显示通用节点
  stage_id: string;
  task_id: string;
  title: string;
  status: string;
  mode: string;
  assignee: string | null;
  blocked_reason: string | null;
  budget: Record<string, unknown> | null;
};

export type WorkflowViewEdge = {
  kind: string; // "dependency"（当前实现）；设计 §2.4 其余语义表由服务端逐步提供
  source: string;
  target: string;
  label: string;
};

export type WorkflowRuntimeRequest = {
  request_id: string;
  requester_node_id: string;
  requester_agent_id: string;
  provider_agent_id: string | null;
  request_type: string;
  question: string;
  blocking: string;
  status: string;
  response_deadline: string | null;
};

export type WorkflowBlockingChain = {
  root_node_id: string;
  reason: string;
  request_id?: string;
  affected_node_ids?: string[];
};

export type WorkflowViewData = {
  project_id: string;
  workflow: { key: string | null; version_id: string | null; state: string | null };
  generated_at: string;
  summary: WorkflowViewSummary;
  stages: { id: string; title: string }[];
  nodes: WorkflowViewNode[];
  edges: WorkflowViewEdge[];
  runtime_requests: WorkflowRuntimeRequest[];
  blocking_chains: WorkflowBlockingChain[];
  runs: Record<string, unknown>[];
};

/** 状态 → 视觉语义（设计 §3.2；词与 status-dictionary 对齐，UNVERIFIED 斜线问号特殊文案）。 */
export type StatusVisual = "green" | "blue" | "amber" | "red" | "gray" | "purple" | "unverified";

const STATUS_VISUAL: Record<string, StatusVisual> = {
  APPROVED: "green",
  ACCEPTED: "green",
  PASSED: "green",
  SUCCEEDED: "green",
  CLAIMED: "blue",
  RUNNING: "blue",
  WAITING_REVIEW: "amber",
  PENDING: "amber",
  PENDING_REVIEW: "amber",
  information_requested: "amber",
  BLOCKED: "red",
  FAILED: "red",
  REJECTED: "red",
  NOT_HOLDS: "red",
  cannot_continue: "red",
  impossible_goal: "red",
  DRAFT: "gray",
  unknown: "gray",
  needs_decision: "purple",
  approval_required: "purple",
  escalated: "purple",
  UNVERIFIED: "unverified",
};

export function statusVisual(status: string): StatusVisual {
  return STATUS_VISUAL[status] ?? "gray";
}

export const STATUS_VISUAL_COLOR: Record<StatusVisual, string> = {
  green: "#22c55e",
  blue: "#3b82f6",
  amber: "#f59e0b",
  red: "#ef4444",
  gray: "#9ca3af",
  purple: "#8b5cf6",
  unverified: "#d97706",
};

export const STATUS_VISUAL_LABEL: Record<StatusVisual, string> = {
  green: "已通过",
  blue: "执行中",
  amber: "等待中",
  red: "失败/阻塞",
  gray: "未开始",
  purple: "待人工决定",
  unverified: "未能验证",
};

/** 边语义 → 视觉（设计 §2.4：线型+颜色+标签三通道同时表达；未知 kind 通用灰实线）。 */
export type EdgeVisual = { color: string; dashed: boolean; label: string };

const EDGE_VISUAL: Record<string, EdgeVisual> = {
  dependency: { color: "#6b7280", dashed: false, label: "依赖" },
  artifact_output: { color: "#3b82f6", dashed: false, label: "产出" },
  artifact_input: { color: "#3b82f6", dashed: true, label: "输入" },
  handoff: { color: "#8b5cf6", dashed: false, label: "交接" },
  review: { color: "#f59e0b", dashed: false, label: "送审" },
  gate_block: { color: "#ef4444", dashed: true, label: "门禁阻塞" },
  retry: { color: "#ef4444", dashed: false, label: "重试" },
  revision: { color: "#f97316", dashed: false, label: "退回新版本" },
  escalation: { color: "#8b5cf6", dashed: true, label: "升级人工" },
  information_request: { color: "#06b6d4", dashed: true, label: "请求信息" },
  information_response: { color: "#06b6d4", dashed: false, label: "信息回复" },
  runtime_dependency: { color: "#f59e0b", dashed: true, label: "临时依赖" },
  feasibility_concern: { color: "#d946ef", dashed: true, label: "可行性异议" },
};

export function edgeVisual(kind: string): EdgeVisual {
  return EDGE_VISUAL[kind] ?? { color: "#9ca3af", dashed: false, label: kind };
}

/** G6 的数据结构（dagre 布局，节点按 stage 分层靠 rank 次序近似泳道）。 */
export type FlowGraphNode = {
  id: string;
  label: string;
  status: string;
  visual: StatusVisual;
  raw: WorkflowViewNode;
};

export type FlowGraphEdge = {
  id: string;
  source: string;
  target: string;
  kind: string;
  label: string;
  visual: EdgeVisual;
};

export function buildFlowGraph(view: WorkflowViewData): { nodes: FlowGraphNode[]; edges: FlowGraphEdge[] } {
  const byId = new Map(view.nodes.map((node) => [node.id, node]));
  const edges = view.edges
    .filter((edge) => byId.has(edge.source) && byId.has(edge.target))
    .map((edge, index) => ({
      id: `edge-${index}`,
      source: edge.source,
      target: edge.target,
      kind: edge.kind,
      label: edge.label || edgeVisual(edge.kind).label,
      visual: edgeVisual(edge.kind),
    }));
  return {
    nodes: view.nodes.map((node) => ({
      id: node.id,
      label: node.title,
      status: node.status,
      visual: statusVisual(node.status),
      raw: node,
    })),
    edges,
  };
}

/** 预览（dry-run）plan 的节点形状（API 形状文档 §145 行）。 */
export type PreviewPlanNode = {
  node_id: string;
  title: string;
  stage_id: string;
  mode: string;
  role_binding?: string;
  resolved_prompt?: string;
  depends_on: string[];
  outputs: { name: string; artifact_type: string; path?: string }[];
  gate_policy?: string;
  budget?: Record<string, unknown>;
  on_fail?: string;
  human_intervention?: string;
  delivery_adapter?: string;
  requires_human?: boolean;
  condition?: { input: string; equals?: string; in?: string[] } | null;
  condition_active?: boolean | null;
  condition_reason?: string;
};

export type PreviewResult = {
  valid: boolean;
  errors: string[];
  warnings: string[];
  plan: {
    workflow: Record<string, unknown>;
    inputs_provided: Record<string, unknown>;
    nodes: PreviewPlanNode[];
    edges: { source: string; target: string; kind: string; label?: string }[];
  } | null;
};

/** definition.node.condition 的 TS 形状（schema v2：{input, equals?, in?}，缺一服务端拒绝）。 */
export type NodeCondition = NonNullable<WorkflowNodeSpec["condition"]>;
