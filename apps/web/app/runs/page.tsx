"use client";

import { useState } from "react";
import Link from "next/link";
import { Activity, Box, Copy, TerminalSquare } from "lucide-react";
import { PageHeading, formatTime } from "../../components/shell";
import { EmptyState, LoadingSkeleton, Metric, Panel, StatusPill } from "../../components/ui";
import { Event, Run, getRun } from "../../lib/api";
import { presentEvent, splitRunSummary } from "../../lib/events";
import { Modal } from "../../components/ui";
import { useWorkspace } from "../../lib/workspace";

/** 与后端 `store.AGENT_HEARTBEAT_TIMEOUT_SECONDS` 一致：3 × 30s 心跳周期。 */
const HEARTBEAT_TIMEOUT_SECONDS = 90;

/** 距最近心跳的秒数；时间缺失或不可解析时返回 null（不要假装是 0）。 */
function secondsSince(timestamp: string): number | null {
  if (!timestamp) return null;
  const parsed = new Date(timestamp).getTime();
  if (Number.isNaN(parsed)) return null;
  return Math.max(0, Math.round((Date.now() - parsed) / 1000));
}

/** 执行体过程事件：平台侧唯一能看到的"正在做什么"。 */
const PROGRESS_PREFIXES = ["agent.process.", "agent.agent.", "agent.tool.", "agent.file."];

function progressEvents(events: Event[], runId: string): Event[] {
  return events.filter(
    (event) =>
      PROGRESS_PREFIXES.some((prefix) => event.event_type.startsWith(prefix)) &&
      String(event.payload?.event?.run_id ?? "") === runId,
  );
}

function elapsedSeconds(startedAt: string): number | null {
  if (!startedAt) return null;
  const parsed = new Date(startedAt).getTime();
  if (Number.isNaN(parsed)) return null;
  return Math.max(0, Math.round((Date.now() - parsed) / 1000));
}

export default function RunsPage() {
  const { dashboard, loading, notify } = useWorkspace();
  const [detail, setDetail] = useState<Run | null>(null);
  const [detailBusy, setDetailBusy] = useState(false);
  const [showRaw, setShowRaw] = useState(false);
  const onlineAgents = dashboard.agents.filter((agent) => String(agent.status) === "online");

  const openDetail = async (runId: string) => {
    setDetailBusy(true);
    setShowRaw(false);
    try {
      setDetail(await getRun(runId));
    } catch (error) {
      notify(error instanceof Error ? error.message : "执行详情读取失败");
    } finally {
      setDetailBusy(false);
    }
  };

  const copyAnswer = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      notify("回答已复制");
    } catch {
      notify("复制失败：请手动选中文本");
    }
  };

  return (
    <div className="page-content" id="runs">
      <PageHeading hint="每次执行都保留环境、参数、输入与信息边界；执行中的过程会随执行体上报实时出现（回复/工具/文件，约每 1.5 秒一条，最多 40 条）" />
      <section className="metrics-grid">
        <Metric label="运行总数" value={dashboard.runs.length} detail="含所有状态" />
        <Metric label="在线 Agent" value={onlineAgents.length} detail={`共 ${dashboard.agents.length} 个已登记`} tone="positive" />
        <Metric label="运行中" value={dashboard.runs.filter((run) => String(run.status) === "RUNNING").length} detail="正在执行" />
        <Metric label="边界阻断" value={dashboard.runs.filter((run) => run.information_boundary?.allowed === false).length} detail="信息边界未通过" tone="warning" />
      </section>

      <section className="grid grid-main-side">
        <Panel title="运行记录" subtitle="状态、摘要与边界结论" testId="run-list">
          <div className="list">
            {dashboard.runs.map((run) => {
              const running = String(run.status) === "RUNNING";
              const progress = progressEvents(dashboard.events, run.id);
              const latest = progress.length ? presentEvent(progress[progress.length - 1]) : null;
              const elapsed = elapsedSeconds(run.started_at);
              const task = run.task_id ? dashboard.tasks.find((item) => item.id === run.task_id) : undefined;
              return (
                <div className="list-item list-item-static" key={run.id} data-testid={`run-row-${run.id}`}>
                  <span className="icon-tile icon-tile-blue"><TerminalSquare size={15} /></span>
                  <div className="item-copy">
                    <strong>{run.agent_id} · {run.status}</strong>
                    {task && <small>任务：{task.title}（{task.status}）</small>}
                    <small>{run.summary || (running ? "正在执行：尚无结果摘要" : "运行已登记")} · {formatTime(run.started_at)}
                      {running && elapsed !== null ? ` · 已运行 ${elapsed} 秒` : ""}
                    </small>
                    {running && (
                      <small data-testid={`run-progress-${run.id}`}>
                        {latest
                          ? `最新进度（${progress.length} 条）：${latest.title}${latest.detail ? ` — ${latest.detail}` : ""}`
                          : "还没有过程上报：执行体刚启动或未开启过程上报"}
                      </small>
                    )}
                    {!running && splitRunSummary(run.summary).answer && (
                      <small data-testid={`run-answer-${run.id}`}>
                        回答：{splitRunSummary(run.summary).answer.slice(0, 160)}
                        {splitRunSummary(run.summary).answer.length > 160 ? "…" : ""}
                      </small>
                    )}
                    <small>
                      网络策略 {run.network_policy} · 输出 {run.output_artifact_ids?.length ?? 0} 个成果物
                      {typeof run.usage?.total_tokens === "number" ? ` · 用量 ${run.usage.total_tokens} tokens` : ""}
                      {typeof run.usage?.seconds === "number" ? ` · 耗时 ${Math.round(run.usage.seconds)} 秒` : ""}
                    </small>
                  </div>
                  <div className="list-actions">
                    <span className={`badge ${run.information_boundary?.allowed === false ? "status-red" : "status-green"}`}>
                      {run.information_boundary?.allowed === false ? "边界阻断" : "边界通过"}
                    </span>
                    <button
                      className="text-button"
                      disabled={detailBusy}
                      data-testid={`run-detail-${run.id}`}
                      onClick={() => void openDetail(run.id)}
                    >
                      详情
                    </button>
                  </div>
                </div>
              );
            })}
            {loading ? <LoadingSkeleton rows={3} label="正在加载运行记录" /> : !dashboard.runs.length && <EmptyState>尚未登记运行记录</EmptyState>}
          </div>
        </Panel>

        <div className="grid">
          <Panel title="Agent 编队" subtitle="服务身份、最近心跳与在线判定依据" testId="agent-list">
            <div className="hint">
              在线判定：最近心跳在 {HEARTBEAT_TIMEOUT_SECONDS} 秒内（3 × 30s 心跳周期）；超时由平台维护扫描置为离线。
            </div>
            <div className="list">
              {dashboard.agents.map((agent) => {
                const stale = secondsSince(agent.last_seen);
                const timeout = stale !== null && stale > HEARTBEAT_TIMEOUT_SECONDS;
                return (
                  <div className="list-item list-item-static" key={agent.agent_id}>
                    <span className={`pulse-dot ${String(agent.status) === "online" ? "" : "pulse-dot-offline"}`} />
                    <div className="item-copy">
                      <strong>{agent.display_name}</strong>
                      <small>{agent.agent_id} · {agent.model_provider}/{agent.model_name}</small>
                      <small data-testid={`agent-last-seen-${agent.agent_id}`}>
                        最近心跳 {agent.last_seen ? formatTime(agent.last_seen) : "从未上报"}
                        {stale !== null ? `（${stale} 秒前${timeout ? "，已超过判定阈值" : ""}）` : ""}
                      </small>
                    </div>
                    <StatusPill status={String(agent.status)} />
                  </div>
                );
              })}
              {!dashboard.agents.length && <EmptyState>等待 Agent 接入</EmptyState>}
            </div>
          </Panel>
          <Panel title="运行产出" subtitle="最近运行的输出成果物" testId="run-outputs">
            <div className="list">
              {dashboard.artifacts.slice(0, 6).map((artifact) => (
                <div className="list-item list-item-static" key={artifact.id}>
                  <span className="icon-tile"><Box size={15} /></span>
                  <div className="item-copy"><strong>{artifact.name}</strong><small>{artifact.artifact_type} · v{artifact.version}</small></div>
                  <StatusPill status={String(artifact.status)} />
                </div>
              ))}
              {!dashboard.artifacts.length && <EmptyState>暂无成果物</EmptyState>}
            </div>
          </Panel>
          <Panel title="事件脉搏" subtitle="最近事件" testId="event-pulse">
            <div className="list">
              {dashboard.events.slice(-5).reverse().map((event) => (
                <div className="list-item list-item-static" key={event.id}>
                  <span className="icon-tile"><Activity size={15} /></span>
                  <div className="item-copy">
                    <strong>{presentEvent(event).title}</strong>
                    <small>{presentEvent(event).detail || event.actor} · {formatTime(event.created_at)}</small>
                  </div>
                </div>
              ))}
              {!dashboard.events.length && <EmptyState>暂无事件</EmptyState>}
            </div>
          </Panel>
        </div>
      </section>

      {detail && (
        <Modal
          title={`执行详情 · ${detail.status}`}
          subtitle={`${detail.agent_id} · 登记于 ${formatTime(detail.started_at)}${detail.task_id ? ` · 任务 ${detail.task_id.slice(0, 8)}` : ""}`}
          wide
          testId="run-detail-modal"
          onClose={() => setDetail(null)}
          actions={
            <>
              <button className="button button-secondary" onClick={() => setShowRaw((value) => !value)} data-testid="run-detail-toggle-raw">
                {showRaw ? "隐藏原始输出" : "查看原始输出"}
              </button>
              <button
                className="button button-primary"
                data-testid="run-detail-copy-answer"
                onClick={() => void copyAnswer(splitRunSummary(detail.summary).answer)}
              >
                <Copy size={15} /> 复制回答
              </button>
            </>
          }
        >
          <div style={{ display: "grid", gap: 14 }}>
            <div className="field">
              <span>回答</span>
              {splitRunSummary(detail.summary).answer ? (
                <pre
                  className="code-block"
                  data-testid="run-detail-answer"
                  style={{ whiteSpace: "pre-wrap", fontFamily: "inherit", maxHeight: 360, overflowY: "auto" }}
                >
                  {splitRunSummary(detail.summary).answer}
                </pre>
              ) : (
                <div className="hint">这次执行没有产出回答文本（可能在中途失败）。诊断见下方原始输出。</div>
              )}
            </div>

            <div className="field">
              <span>产出成果物（{detail.output_artifact_ids?.length ?? 0}）</span>
              {(detail.output_artifact_ids?.length ?? 0) ? (
                <div className="list" data-testid="run-detail-outputs">
                  {detail.output_artifact_ids.map((artifactId) => {
                    const artifact = dashboard.artifacts.find((item) => item.id === artifactId);
                    return (
                      <div className="list-item list-item-static" key={artifactId}>
                        <span className="icon-tile icon-tile-blue"><Box size={14} /></span>
                        <div className="item-copy">
                          <strong>{artifact ? artifact.name : `${artifactId.slice(0, 8)}…`}</strong>
                          <small>
                            {artifact ? `v${artifact.version} · ${artifact.artifact_type} · 内容哈希 ${(artifact.content_hash || "—").slice(0, 10)}…` : "成果物记录未找到"}
                          </small>
                        </div>
                        {artifact && <StatusPill status={String(artifact.status)} />}
                        <Link className="text-button" href="/artifacts">去成果物库 →</Link>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <div className="hint">这次执行没有产出成果物（可能是只读任务，或产出被采集规则排除）。</div>
              )}
            </div>

            <div className="field">
              <span>执行信息</span>
              <div className="chips">
                <span className="chip">状态 {detail.status}</span>
                {/* 执行归属：设计文档要求"可追溯到设备、Agent、成员和 Run" */}
                <span className="chip" data-testid="run-attribution">
                  执行者 {detail.agent_id}
                  {detail.device_id ? ` · 设备 ${detail.device_id}` : ""}
                  {detail.member_id ? ` · 归属 ${detail.member_id}` : ""}
                </span>
                <span className="chip">网络策略 {detail.network_policy}</span>
                <span className="chip">产出成果物 {detail.output_artifact_ids?.length ?? 0} 个</span>
                <span className="chip" data-testid="run-usage">
                  {typeof detail.usage?.total_tokens === "number"
                    ? `用量 ${detail.usage.total_tokens} tokens（${detail.usage.source || "未标注"}）`
                    : "token 用量：执行体未回报"}
                  {typeof detail.usage?.seconds === "number"
                    ? ` · 耗时 ${Math.round(detail.usage.seconds)} 秒（${detail.usage.seconds_source === "agent" ? "执行体自报" : "平台观测"}）`
                    : ""}
                </span>
                <span className="chip">输入文件 {detail.observed_input_files?.length ?? 0} 个</span>
                <span className={`chip ${detail.information_boundary?.allowed === false ? "status-red" : ""}`}>
                  信息边界 {detail.information_boundary?.allowed === false ? "阻断" : "通过"}
                </span>
              </div>
              {splitRunSummary(detail.summary).diagnostics && (
                <div className="hint" style={{ marginTop: 6 }}>
                  执行诊断：{splitRunSummary(detail.summary).diagnostics}
                </div>
              )}
            </div>

            {showRaw && (
              <>
                <div className="field">
                  <span>原始输出（stdout，保留末尾 40000 字符）</span>
                  <pre className="code-block" data-testid="run-detail-stdout" style={{ maxHeight: 320, overflow: "auto", whiteSpace: "pre-wrap" }}>
                    {detail.stdout || "（无 stdout）"}
                  </pre>
                </div>
                <div className="field">
                  <span>stderr</span>
                  <pre className="code-block" data-testid="run-detail-stderr" style={{ maxHeight: 200, overflow: "auto", whiteSpace: "pre-wrap" }}>
                    {detail.stderr || "（无 stderr）"}
                  </pre>
                </div>
              </>
            )}
          </div>
        </Modal>
      )}
    </div>
  );
}
