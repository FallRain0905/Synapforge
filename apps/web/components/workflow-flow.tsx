"use client";

/**
 * 项目生产流程图（可视化 V1 / D5）。数据来自 GET /api/projects/{id}/workflow-view
 * ——节点/边/阻断链是服务端权威对象，前端不猜状态机（设计文档 §5.2）。
 *
 * V1 范围：只读 DAG + 节点详情抽屉 + 阻塞链聚焦 + 列表降级通道 + 空态如实。
 * 布局：确定性 dagre（layer 按 stage 分组、依赖定向）——相同 node.id 的层级固定，刷新不乱跳。
 * 实时：5s 轮询 workflow-view 兜底（SSE project.* 增量在 V2 接，事件契约已就位）。
 * 降级：图形不可用/移动端/用户切换 → 列表视图（与图同一份数据，等价信息）。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { AlertTriangle, Eye, EyeOff, Info, List, Network, RefreshCcw } from "lucide-react";
import { EmptyState, LoadingSkeleton, Panel, StatusPill } from "./ui";
import {
  errorMessage,
  getProjectWorkflowView,
  type WorkflowViewData,
} from "../lib/api";
import {
  buildFlowGraph,
  STATUS_VISUAL_COLOR,
  STATUS_VISUAL_LABEL,
  statusVisual,
  type FlowGraphNode,
} from "../lib/workflow-view";

const REFRESH_MS = 5000;

/** 连接状态（设计 §6.4；V1 只有轮询与失败两态，SSE 在 V2）。 */
type SyncState = "polling" | "stale";

type SelectedNode = { node: FlowGraphNode; view: WorkflowViewData };

export function WorkflowFlowView({ projectId }: { projectId: string }) {
  const [view, setView] = useState<WorkflowViewData | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [sync, setSync] = useState<SyncState>("polling");
  const [mode, setMode] = useState<"graph" | "list">("graph");
  const [selected, setSelected] = useState<SelectedNode | null>(null);
  const [onlyBlocking, setOnlyBlocking] = useState(false);
  const canvasRef = useRef<HTMLDivElement | null>(null);
  const graphRef = useRef<{ destroy: () => void } | null>(null);

  const load = useCallback(async () => {
    try {
      const fresh = await getProjectWorkflowView(projectId);
      setView(fresh);
      setSync("polling");
      setError("");
    } catch {
      // 单次失败不清空已渲染的图：保留最后一次可信图，标记可能滞后（设计 §6.4/§8.3）
      setSync("stale");
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const graph = useMemo(() => (view ? buildFlowGraph(view) : null), [view]);

  // G6 v5 渲染：client-only，dynamic import 避免静态预渲染问题；失败自动降级列表
  useEffect(() => {
    if (mode !== "graph" || !graph || !graph.nodes.length || !canvasRef.current) return;
    let disposed = false;
    const mount = canvasRef.current;
    void (async () => {
      try {
        const { Graph } = await import("@antv/g6");
        if (disposed) return;
        const blockingRoots = new Set((view?.blocking_chains ?? []).map((chain) => chain.root_node_id));
        // 确定性 dagre 布局：相同数据 → 相同布局；节点样式用 data 回调按状态着色
        const instance = new Graph({
          container: mount,
          animation: false,
          data: {
            nodes: graph.nodes.map((node) => ({
              id: node.id,
              data: {
                label: node.label,
                statusLabel: STATUS_VISUAL_LABEL[node.visual],
                blockingRoot: blockingRoots.has(node.id),
                visual: node.visual,
              },
            })),
            edges: graph.edges.map((edge) => ({
              id: edge.id,
              source: edge.source,
              target: edge.target,
              data: { kind: edge.kind, label: edge.kind === "dependency" ? "" : edge.label, color: edge.visual.color, dashed: edge.visual.dashed },
            })),
          },
          layout: { type: "antv-dagre", rankdir: "LR", nodesep: 24, ranksep: 60 },
          node: {
            type: "rect",
            style: (datum: { id: string; data?: Record<string, unknown> }) => {
              const payload = (datum.data ?? {}) as { label?: string; statusLabel?: string; blockingRoot?: boolean; visual?: string };
              const color = STATUS_VISUAL_COLOR[(payload.visual as keyof typeof STATUS_VISUAL_COLOR) ?? "gray"] ?? "#9ca3af";
              return {
                size: [170, 56],
                fill: "#ffffff",
                stroke: color,
                lineWidth: payload.blockingRoot ? 3 : 2,
                lineDash: payload.visual === "unverified" ? [4, 3] : undefined,
                radius: 6,
                labelText: `${payload.label ?? ""}\n${payload.statusLabel ?? ""}${payload.blockingRoot ? " · 阻塞源" : ""}`,                labelPlacement: "bottom",
                labelBackground: true,
                labelBackgroundFill: "rgba(255,255,255,0.92)",
                labelPadding: [1, 4],
                labelFontSize: 11,
              };
            },
          },
          edge: {
            type: "polyline",
            style: (datum: { data?: Record<string, unknown> }) => {
              const payload = (datum.data ?? {}) as { color?: string; dashed?: boolean; label?: string };
              return {
                stroke: payload.color ?? "#9ca3af",
                lineWidth: 1.6,
                lineDash: payload.dashed ? [5, 4] : undefined,
                endArrow: true,
                labelText: payload.label || undefined,
                labelFontSize: 10,
                labelBackground: true,
                labelBackgroundFill: "rgba(255,255,255,0.9)",
              };
            },
          },
          behaviors: ["drag-canvas", "zoom-canvas", "drag-element"],
        });
        instance.on("node:click", (event: unknown) => {
          // v5 事件 payload 带 target.id；防御式读取，形状变化也不崩
          const targetId = (event as { target?: { id?: unknown } })?.target?.id;
          const nodeId = targetId == null ? "" : String(targetId);
          const node = graph.nodes.find((item) => item.id === nodeId);
          if (node && view) setSelected({ node, view });
        });
        void instance.render();
        graphRef.current = instance as unknown as { destroy: () => void };
      } catch {
        // 图形库初始化失败（无 canvas 环境/移动端老设备）：自动降级列表，不空屏
        setMode("list");
      }
    })();
    return () => {
      disposed = true;
      graphRef.current?.destroy();
      graphRef.current = null;
    };
    // view 变化时全量重渲染（5s 轮询；V2 换事件增量）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, graph, view?.generated_at]);

  if (loading && !view) {
    return <Panel title="生产流程图" subtitle="谁在做什么、谁等谁、卡在哪"><LoadingSkeleton rows={4} label="正在加载流程图" /></Panel>;
  }

  // 空态如实（§8.3）：无工作流运行就不画假节点
  if (!view || !view.nodes.length) {
    return (
      <Panel title="生产流程图" subtitle="谁在做什么、谁等谁、卡在哪">
        <EmptyState>
          <Network size={18} />
          <div style={{ marginTop: 6 }}>
            当前项目还没有工作流运行：到<Link href="/workflows" className="inline-link">工作流包</Link>
            应用一个包生成任务骨架，流程图会按真实任务与依赖展开——不画示例节点。
          </div>
        </EmptyState>
      </Panel>
    );
  }

  const summaryText = Object.entries(view.summary)
    .map(([status, count]) => `${count} 个 ${STATUS_VISUAL_LABEL[statusVisual(status)] === "未开始" ? status : STATUS_VISUAL_LABEL[statusVisual(status)]}`)
    .join(" · ");
  const blockingRoots = new Set(view.blocking_chains.map((chain) => chain.root_node_id));

  return (
    <Panel
      title="生产流程图"
      subtitle={`${summaryText || "暂无节点"} · ${view.generated_at.slice(11, 19)} 生成`}
      testId="workflow-flow"
      actions={
        <>
          <span className="hint" style={{ fontSize: 12 }}>
            {sync === "polling" ? "轮询同步（5s）" : "数据可能滞后"}
          </span>
          <button type="button" className="text-button" onClick={() => void load()}>
            <RefreshCcw size={12} /> 刷新
          </button>
          <button
            type="button"
            className="text-button"
            data-testid="workflow-flow-toggle-blocking"
            onClick={() => setOnlyBlocking((current) => !current)}
          >
            {onlyBlocking ? <Eye size={12} /> : <EyeOff size={12} />} 只看阻塞
          </button>
          <button type="button" className="text-button" data-testid="workflow-flow-toggle-mode" onClick={() => setMode((current) => (current === "graph" ? "list" : "graph"))}>
            <List size={12} /> {mode === "graph" ? "切换为列表" : "切换为图形"}
          </button>
        </>
      }
    >
      {sync === "stale" ? (
        <div className="pack-missing"><strong>数据可能滞后</strong><span>最近一次刷新失败，正在用最后一次可信图；可手动重试。</span></div>
      ) : null}

      {view.blocking_chains.length ? (
        <div className="hint" style={{ marginBottom: 8 }} data-testid="workflow-flow-blocking">
          <AlertTriangle size={13} style={{ display: "inline", marginRight: 4 }} />
          阻塞链（服务端计算）：
          {view.blocking_chains.map((chain, index) => (
            <span key={index} style={{ marginRight: 10 }}>
              {view.nodes.find((node) => node.id === chain.root_node_id)?.title ?? chain.root_node_id}
              （{chain.reason}，波及 {chain.affected_node_ids?.length ?? 0} 个下游）
            </span>
          ))}
        </div>
      ) : null}

      {mode === "graph" ? (
        <div
          ref={canvasRef}
          style={{ height: 420, border: "1px solid var(--line, #e5e7eb)", borderRadius: 8, overflow: "hidden", minHeight: 0 }}
          data-testid="workflow-flow-canvas"
        />
      ) : (
        // 列表降级（§8.1/§8.2）：与图同一份数据的等价线性视图
        <div className="list" data-testid="workflow-flow-list">
          {(onlyBlocking ? graph!.nodes.filter((node) => blockingRoots.has(node.id)) : graph!.nodes).map((node) => (
            <div className="list-item list-item-static" key={node.id}>
              <span className={`icon-tile ${node.visual === "green" ? "icon-tile-green" : node.visual === "blue" ? "icon-tile-blue" : node.visual === "red" ? "icon-tile-amber" : ""}`.trim()}>
                <Network size={14} />
              </span>
              <div className="item-copy" style={{ minWidth: 0 }}>
                <strong>{node.label}</strong>
                <small>
                  {view.stages.find((stage) => stage.id === node.raw.stage_id)?.title ?? (node.raw.stage_id || "—")}
                  {node.raw.assignee ? ` · ${node.raw.assignee.slice(0, 8)}` : ""}
                  {node.raw.blocked_reason ? ` · ${node.raw.blocked_reason}` : ""}
                </small>
              </div>
              <StatusPill status={node.status} />
              <button type="button" className="text-button" onClick={() => setSelected({ node, view })}>详情</button>
            </div>
          ))}
        </div>
      )}

      {selected ? <NodeDetailDrawer selected={selected} onClose={() => setSelected(null)} /> : null}
    </Panel>
  );
}

/** 节点详情抽屉（设计 §4.1 的 V1 子集）：状态/为什么在这/谁负责/依赖/去任务板。 */
function NodeDetailDrawer({ selected, onClose }: { selected: SelectedNode; onClose: () => void }) {
  const { node, view } = selected;
  const raw = node.raw;
  const stage = view.stages.find((item) => item.id === raw.stage_id);
  const upstream = view.edges.filter((edge) => edge.target === node.id);
  const downstream = view.edges.filter((edge) => edge.source === node.id);
  const requests = view.runtime_requests.filter((request) => request.requester_node_id === node.id);
  const chain = view.blocking_chains.find((item) => item.root_node_id === node.id);
  return (
    <div className="list" style={{ marginTop: 10, borderTop: "1px solid var(--line, #e5e7eb)", paddingTop: 10 }} data-testid="workflow-node-detail">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <strong>{raw.title}</strong>
        <button type="button" className="text-button" onClick={onClose}>收起</button>
      </div>
      <div className="list-item list-item-static" style={{ paddingLeft: 0 }}>
        <div className="item-copy" style={{ minWidth: 0 }}>
          <small>当前状态</small>
          <div><StatusPill status={raw.status} /> <small>{STATUS_VISUAL_LABEL[node.visual]}</small></div>
          <small style={{ display: "block" }}>
            {stage ? `阶段：${stage.title}` : "未分组"} · 模式 {raw.mode || "—"}
            {raw.assignee ? ` · 负责 ${raw.assignee.slice(0, 8)}` : " · 未指派"}
          </small>
          {chain ? (
            <small style={{ display: "block", color: "#b91c1c" }}>阻塞源：{chain.reason}</small>
          ) : null}
          {raw.blocked_reason ? <small style={{ display: "block" }}>受阻原因：{raw.blocked_reason}</small> : null}
          {upstream.length ? (
            <small style={{ display: "block" }}>上游：{upstream.map((edge) => `${edgeVisualLabel(edge.kind)}←${nodeName(view, edge.source)}`).join("、")}</small>
          ) : (
            <small style={{ display: "block" }}>上游：无（可直接执行）</small>
          )}
          {downstream.length ? <small style={{ display: "block" }}>下游：{downstream.map((edge) => `${nodeName(view, edge.target)}（${edgeVisualLabel(edge.kind)}）`).join("、")}</small> : null}
          {requests.length ? (
            <small style={{ display: "block" }} data-testid="workflow-node-requests">
              主动信息请求：{requests.map((request) => `${request.request_type} → ${request.provider_agent_id?.slice(0, 8) ?? "人工"}（${request.blocking}，${request.status}）`).join("；")}
            </small>
          ) : null}
          {raw.budget ? (
            <small style={{ display: "block" }}>
              预算：{Object.entries(raw.budget).map(([key, value]) => `${key}=${String(value)}`).join(" · ")}
            </small>
          ) : null}
        </div>
        <Link className="text-button" href="/tasks">任务板 <Info size={12} /></Link>
      </div>
      <div className="hint">动作只放真实入口：执行与审核走任务板与审核门禁（拉取模型，没有"开始"按钮）。</div>
    </div>
  );
}

function nodeName(view: WorkflowViewData, nodeId: string): string {
  return view.nodes.find((node) => node.id === nodeId)?.title ?? nodeId.slice(0, 8);
}

function edgeVisualLabel(kind: string): string {
  const labels: Record<string, string> = {
    dependency: "依赖",
    artifact_output: "产出",
    artifact_input: "输入",
    handoff: "交接",
    review: "送审",
    gate_block: "门禁阻塞",
    retry: "重试",
    revision: "退回",
    escalation: "升级",
    information_request: "请求信息",
    runtime_dependency: "临时依赖",
    feasibility_concern: "可行性异议",
  };
  return labels[kind] ?? kind;
}
