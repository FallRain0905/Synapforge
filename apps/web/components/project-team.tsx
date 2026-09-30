"use client";

/**
 * 工作区「团队 / 生产」视图（W2.3 B 侧，消费 A 的 project_team.py 聚合）。
 *
 * 两条视角（借 autogen 双订阅拓扑的划分）：
 * - TeamView（定向视角）："谁在做什么"——每 Agent 的身份/能力/在线/当前任务/负载/在等什么；
 * - ProductionPathView（广播视角）："东西从哪来到哪去"——成果物 → 交接 → 下游任务因果链。
 *
 * 空态纪律（A 的口径）：数据全部来自既有权威表，**如实没在跑就写没在跑**，不编。
 * 自取数 + 挂在 Tab 上时 5 秒轻刷新（组件随 Tab 卸载，不产生后台常驻请求）。
 */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { ArrowUpRight, Bot, Box, RefreshCcw } from "lucide-react";
import { EmptyState, LoadingSkeleton, Metric, Panel, StatusPill } from "./ui";
import {
  errorMessage,
  getProjectProductionPath,
  getProjectTeam,
  ProjectProductionPath,
  ProjectTeam,
  ProjectTeamMember,
  TeamTaskBrief,
} from "../lib/api";

const REFRESH_MS = 5000;

/** 在线/离线徽章：直接复用 status-pill 的两个色类，不新增 CSS（globals.css 不在领地内）。 */
function OnlineBadge({ online }: { online: boolean }) {
  return (
    <span className={`status-pill ${online ? "status-green" : "status-neutral"}`}>
      <span className="status-bullet" />
      {online ? "在线" : "离线"}
    </span>
  );
}

function TaskLine({ task }: { task: TeamTaskBrief }) {
  return (
    <div className="list-item list-item-static" style={{ paddingLeft: 0 }}>
      <div className="item-copy" style={{ minWidth: 0 }}>
        <strong style={{ fontSize: 13 }}>{task.title}</strong>
        <small>
          {task.stage || "—"}
          {task.blocked_reason ? ` · ${task.blocked_reason}` : ""}
        </small>
      </div>
      <StatusPill status={task.status} />
    </div>
  );
}

function TaskBucket({ label, tasks, emptyText }: { label: string; tasks: TeamTaskBrief[]; emptyText: string }) {
  return (
    <div style={{ minWidth: 0 }}>
      <div className="composer-add-label">{label}</div>
      {tasks.length ? (
        <div className="list">
          {tasks.slice(0, 3).map((task) => (
            <TaskLine key={task.task_id} task={task} />
          ))}
          {tasks.length > 3 ? <small>还有 {tasks.length - 3} 项…</small> : null}
        </div>
      ) : (
        <small>{emptyText}</small>
      )}
    </div>
  );
}

export function TeamView({ projectId }: { projectId: string }) {
  const [team, setTeam] = useState<ProjectTeam | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const fresh = await getProjectTeam(projectId);
      setTeam(fresh);
      setError("");
    } catch (failure) {
      setError(errorMessage(failure, "团队视图读取失败"));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  if (loading && !team) {
    return <Panel title="团队" subtitle="谁在做什么、在等什么"><LoadingSkeleton rows={4} label="正在加载团队视图" /></Panel>;
  }
  if (error && !team) {
    return (
      <Panel title="团队" subtitle="谁在做什么、在等什么">
        <EmptyState>
          {error}
          <button type="button" className="text-button" onClick={() => void load()}><RefreshCcw size={12} /> 重试</button>
        </EmptyState>
      </Panel>
    );
  }
  if (!team) return null;

  return (
    <div style={{ display: "grid", gap: 14 }} data-testid="workspace-team-view">
      <section className="metrics-grid">
        <Metric label="在线 Agent" value={team.online_count} detail={`共 ${team.agent_count} 个授权`} tone={team.online_count ? "positive" : "warning"} />
        <Metric label="待接收交接" value={team.pending_handoff_count} detail="下游还没确认" tone={team.pending_handoff_count ? "warning" : undefined} />
        <Metric
          label="执行中任务"
          value={team.agents.reduce((sum, member) => sum + member.load, 0)}
          detail={`全项目 ${Object.entries(team.task_status_counts).map(([status, count]) => `${status} ${count}`).join(" · ") || "暂无任务"}`}
        />
      </section>
      <Panel
        title="Agent 团队"
        subtitle="在线以执行体心跳为准（180 秒）；探不到就如实显示离线，不猜"
        actions={<Link className="text-button" href="/tasks">任务板 <ArrowUpRight size={13} /></Link>}
      >
        {team.agents.length ? (
          <div className="list">
            {team.agents.map((member) => (
              <AgentRow key={member.device_id} member={member} />
            ))}
          </div>
        ) : (
          <EmptyState>这个项目还没有授权任何执行体：到「设备与接入」接入，Agent 会出现在这里</EmptyState>
        )}
      </Panel>
    </div>
  );
}

function AgentRow({ member }: { member: ProjectTeamMember }) {
  const name = member.agent_name || member.device_name || member.device_id;
  const busy = member.load > 0;
  return (
    <div className="list-item list-item-static" data-testid={`team-agent-${member.device_id}`}>
      <span className={`icon-tile ${member.online ? (busy ? "icon-tile-blue" : "icon-tile-green") : ""}`.trim()}>
        <Bot size={15} />
      </span>
      <div className="item-copy" style={{ flex: 1, minWidth: 0 }}>
        <strong>
          {name}
          {member.model_name ? <small> · {member.model_name}</small> : null}
        </strong>
        <small>
          {member.capabilities.length ? member.capabilities.slice(0, 6).join("、") : "无能力授权"}
          {member.capabilities.length > 6 ? ` 等 ${member.capabilities.length} 项` : ""}
        </small>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 10, marginTop: 6 }}>
          <TaskBucket label={`执行中（${member.current_tasks.length}）`} tasks={member.current_tasks} emptyText="当前没有在跑的任务" />
          <TaskBucket label={`待领取（${member.next_tasks.length}）`} tasks={member.next_tasks} emptyText="没有排给它的待办" />
          <TaskBucket label={`受阻 / 待改（${member.waiting_tasks.length}）`} tasks={member.waiting_tasks} emptyText="没有受阻任务" />
          <div style={{ minWidth: 0 }}>
            <div className="composer-add-label">待接收交接（{member.pending_handoffs.length}）</div>
            {member.pending_handoffs.length ? (
              member.pending_handoffs.slice(0, 3).map((handoff) => (
                <div key={handoff.handoff_id} className="list-item list-item-static" style={{ paddingLeft: 0 }}>
                  <div className="item-copy" style={{ minWidth: 0 }}>
                    <small>交接 {handoff.handoff_id.slice(0, 8)} · 任务 {handoff.task_id.slice(0, 8)}</small>
                  </div>
                  <StatusPill status={handoff.status} label="待确认" />
                </div>
              ))
            ) : (
              <small>没有等待它接收的交接</small>
            )}
          </div>
        </div>
      </div>
      <OnlineBadge online={member.online} />
    </div>
  );
}

export function ProductionPathView({ projectId }: { projectId: string }) {
  const [path, setPath] = useState<ProjectProductionPath | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const fresh = await getProjectProductionPath(projectId);
      setPath(fresh);
      setError("");
    } catch (failure) {
      setError(errorMessage(failure, "生产路径读取失败"));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  if (loading && !path) {
    return <Panel title="生产路径" subtitle="成果物 → 交接 → 下游任务"><LoadingSkeleton rows={4} label="正在加载生产路径" /></Panel>;
  }
  if (error && !path) {
    return (
      <Panel title="生产路径" subtitle="成果物 → 交接 → 下游任务">
        <EmptyState>
          {error}
          <button type="button" className="text-button" onClick={() => void load()}><RefreshCcw size={12} /> 重试</button>
        </EmptyState>
      </Panel>
    );
  }
  if (!path) return null;

  return (
    <div style={{ display: "grid", gap: 14 }} data-testid="workspace-production-view">
      <section className="metrics-grid">
        <Metric label="成果物" value={path.artifact_total} detail="已入库版本" />
        <Metric label="交接" value={path.handoff_total} detail="接力与分发" />
        <Metric label="任务" value={path.task_total} detail="全项目" />
      </section>
      <Panel title="生产路径" subtitle="每个成果物：谁产出的、审了没有、喂给了谁——最近 50 个">
        {path.nodes.length ? (
          <div className="list">
            {path.nodes.map((node) => (
              <div className="list-item list-item-static" key={node.artifact_id} data-testid={`production-node-${node.artifact_id}`}>
                <span className="icon-tile"><Box size={15} /></span>
                <div className="item-copy" style={{ flex: 1, minWidth: 0 }}>
                  <strong>
                    {node.name} <small>v{node.version} · {node.artifact_type}</small>
                  </strong>
                  <small>
                    {node.source
                      ? `产出自任务「${node.source.title}」${node.run_id ? ` · Run ${node.run_id.slice(0, 8)}` : ""}`
                      : node.run_id
                        ? `产出自 Run ${node.run_id.slice(0, 8)}`
                        : "没有登记产出任务（历史数据或直接上传）"}
                    {node.approved_by
                      ? ` · 已由 ${node.approved_by.slice(0, 8)} 批准`
                      : node.status === "PENDING_REVIEW"
                        ? " · 待审核"
                        : ""}
                  </small>
                  {node.downstream_tasks.length || node.handoffs.length ? (
                    <small style={{ display: "block" }}>
                      {node.handoffs.length ? `经过 ${node.handoffs.length} 个交接（${node.handoffs.map((h) => h.receipt_status).join("、")}）` : ""}
                      {node.handoffs.length && node.downstream_tasks.length ? " · " : ""}
                      {node.downstream_tasks.length
                        ? `喂给下游任务：${node.downstream_tasks.map((task) => task.title).join("、")}`
                        : ""}
                    </small>
                  ) : (
                    <small style={{ display: "block" }}>还没有下游：未挂交接，也没有任务把它当输入</small>
                  )}
                </div>
                <StatusPill status={node.status} />
              </div>
            ))}
          </div>
        ) : (
          <EmptyState>项目还没有成果物：任务执行产出或对话转入生产后会出现在这里</EmptyState>
        )}
      </Panel>
    </div>
  );
}
