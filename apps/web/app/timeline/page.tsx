"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { Activity, Bot, ClipboardCheck, Cpu, FileText, Filter, GitBranch, Info, PlayCircle, Search, User, Wrench } from "lucide-react";
import { PageHeading, formatTime } from "../../components/shell";
import { EmptyState, LoadingSkeleton, Panel, StatusPill } from "../../components/ui";
import { Event, listProjectEvents } from "../../lib/api";
import {
  EVENT_CATEGORIES,
  EventCategory,
  PresentedEvent,
  collapseRepeats,
  groupByDay,
  presentEvent,
  relativeTime,
  roleLabel,
} from "../../lib/events";
import { useWorkspace } from "../../lib/workspace";

/** 分类图标：同一族事件在列表里长得一样，扫一眼就能定位。 */
const CATEGORY_ICONS: Record<EventCategory, React.ReactNode> = {
  task: <ClipboardCheck size={15} />,
  run: <PlayCircle size={15} />,
  review: <GitBranch size={15} />,
  handoff: <GitBranch size={15} />,
  artifact: <FileText size={15} />,
  agent: <Cpu size={15} />,
  project: <Wrench size={15} />,
  other: <Activity size={15} />,
};

const ROLE_ICONS: Record<PresentedEvent["actorRole"], React.ReactNode> = {
  member: <User size={12} />,
  agent: <Bot size={12} />,
  system: <Wrench size={12} />,
};

export default function TimelinePage() {
  const { dashboard, reviewCenter, projectId, loading } = useWorkspace();
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState<EventCategory | "all">("all");
  const [showRaw, setShowRaw] = useState(false);
  // 看板只带最近 20 条事件；时间线的"共 N 条"必须是真的，所以这里单独取。
  const [events, setEvents] = useState<Event[] | null>(null);
  // 「在时间线看它的事件」：按成果物过滤，形成那份内容的完整故事线（CL-3-04）。
  // 从 location 读而不是 useSearchParams：后者会让这个静态页面在预渲染阶段要求 Suspense 边界。
  const [artifactFilter, setArtifactFilter] = useState("");
  useEffect(() => {
    setArtifactFilter(new URLSearchParams(window.location.search).get("artifact") ?? "");
  }, []);

  useEffect(() => {
    if (!projectId) {
      setEvents(null);
      return;
    }
    let cancelled = false;
    void listProjectEvents(projectId, 300, true)
      .then((list) => { if (!cancelled) setEvents(list); })
      .catch(() => { if (!cancelled) setEvents(null); });
    return () => { cancelled = true; };
  }, [projectId, dashboard.events.length]);

  const all = events ?? dashboard.events;
  const source = artifactFilter
    ? all.filter(
        (event) =>
          String(event.object_id ?? "") === artifactFilter ||
          String(event.payload?.artifact_id ?? "") === artifactFilter ||
          // 复核事件挂在 review 上，但 payload 里带着它审的是哪份内容（target_id）
          String(event.payload?.target_id ?? "") === artifactFilter,
      )
    : all;

  // Agent 注册名：事件里只有 agent-xxx 这样的 id，展示时要换成"人给的名字"
  const agentNames = useMemo(() => {
    const names: Record<string, string> = {};
    for (const agent of dashboard.agents) names[agent.agent_id] = agent.display_name || agent.agent_id;
    return names;
  }, [dashboard.agents]);

  const presented = useMemo(
    () => source.map((event) => presentEvent(event, agentNames)).slice().reverse(),
    [source, agentNames],
  );

  const counts = useMemo(() => {
    const result: Record<string, number> = { all: presented.length };
    for (const item of presented) result[item.category] = (result[item.category] ?? 0) + 1;
    return result;
  }, [presented]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return presented.filter((item) => {
      if (category !== "all" && item.category !== category) return false;
      if (!needle) return true;
      return `${item.title} ${item.rawType} ${item.actorLabel} ${item.detail}`.toLowerCase().includes(needle);
    });
  }, [presented, category, query]);

  const groups = useMemo(() => groupByDay(collapseRepeats(filtered)), [filtered]);

  // 当前进展：直接取自看板与审核中心，避免"时间线只有历史、看不到现在到哪一步"
  const taskTotal = dashboard.tasks.length;
  const taskApproved = dashboard.tasks.filter((task) => task.status === "APPROVED").length;
  const waitingReview = dashboard.tasks.filter((task) => task.status === "WAITING_REVIEW").length;
  const pendingHandoffs = dashboard.handoffs.filter((handoff) => (handoff.receipt_status ?? "PENDING") === "PENDING").length;
  const blockingGates = reviewCenter.gates.filter((gate) => gate.status !== "PASSED").length;
  const lastEvent = presented[0];

  return (
    <div className="page-content" id="timeline">
      <PageHeading
        hint={`共 ${presented.length} 条事件（最近 ${presented.length >= 300 ? "300" : presented.length} 条以内）· 当前筛选 ${filtered.length} 条 · 最新在前`}
        actions={
          <>
            <button
              className="button button-secondary"
              onClick={() => setShowRaw((current) => !current)}
              data-testid="timeline-toggle-raw"
              title="显示事件类型机器码与序号，便于和平台日志对照"
            >
              <Info size={15} /> {showRaw ? "隐藏机器码" : "显示机器码"}
            </button>
            <div className="form-row" style={{ gap: 6 }}>
              <Search size={15} style={{ opacity: 0.6 }} />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="搜索：任务、交接、agent-…"
                className="timeline-search"
                data-testid="timeline-search"
              />
            </div>
          </>
        }
      />

      {artifactFilter && (
        <div className="pack-missing" data-testid="timeline-artifact-filter">
          <strong>正在看单份内容的事件</strong>
          <span>
            只显示与成果物 <code>{artifactFilter.slice(0, 8)}…</code> 相关的事件（创建、内容落库、提交审核、被驳回等）。
            点下方按钮回到完整事件流。
          </span>
          <div className="form-row">
            <Link className="button button-secondary" href="/timeline">看完整事件流</Link>
            <Link className="button button-secondary" href="/artifacts">回到成果物库</Link>
          </div>
        </div>
      )}

      <Panel title="当前进展" subtitle="时间线告诉你发生过什么，这里告诉你现在到哪一步了" testId="timeline-summary">
        <div className="list">
          <div className="list-item list-item-static">
            <span className="icon-tile icon-tile-blue"><ClipboardCheck size={15} /></span>
            <div className="item-copy">
              <strong>任务 {taskApproved} / {taskTotal} 已通过</strong>
              <small>
                {taskTotal === 0
                  ? "还没有任务：先去「建模模板包」一键应用，或到「任务与流程」新建"
                  : waitingReview > 0
                    ? `${waitingReview} 个任务在等审核：去「审核门禁」给出结论后，下游任务才会解锁`
                    : "没有待审核的任务"}
              </small>
            </div>
            <StatusPill status={taskTotal && taskApproved === taskTotal ? "APPROVED" : "RUNNING"} label={taskTotal ? `${Math.round((taskApproved / taskTotal) * 100)}%` : "—"} />
          </div>
          <div className="list-item list-item-static">
            <span className="icon-tile"><GitBranch size={15} /></span>
            <div className="item-copy">
              <strong>交接 {pendingHandoffs} 份待接收</strong>
              <small>{pendingHandoffs ? "「交接中心」里逐份确认，退回要写原因" : "没有待接收的交接"}</small>
            </div>
            <StatusPill status={pendingHandoffs ? "WAITING_REVIEW" : "APPROVED"} label={pendingHandoffs ? "待处理" : "清空"} />
          </div>
          <div className="list-item list-item-static">
            <span className="icon-tile"><GitBranch size={15} /></span>
            <div className="item-copy">
              <strong>{blockingGates ? `${blockingGates} 个门禁未通过` : "门禁全部通过"}</strong>
              <small>{blockingGates ? "未通过的门禁会挡住下游任务（不是提醒，是真的拦）" : "下游任务可以正常推进"}</small>
            </div>
            <StatusPill status={blockingGates ? "BLOCKED" : "APPROVED"} label={blockingGates ? "有阻塞" : "无阻塞"} />
          </div>
          <div className="list-item list-item-static">
            <span className="icon-tile"><Activity size={15} /></span>
            <div className="item-copy">
              <strong>最近活动：{lastEvent ? lastEvent.title : "还没有事件"}</strong>
              <small>{lastEvent ? `${lastEvent.actorLabel} · ${formatTime(lastEvent.createdAt)}（${relativeTime(lastEvent.createdAt)}）` : "任何操作都会在这里留痕"}</small>
            </div>
          </div>
        </div>
      </Panel>

      <Panel title="事件流" subtitle="按天分组；每条都写清谁、做了什么、结果如何" testId="timeline-list">
        <div className="chips" style={{ marginBottom: 10 }}>
          <button
            className={`chip ${category === "all" ? "chip-active" : ""}`.trim()}
            onClick={() => setCategory("all")}
            data-testid="timeline-filter-all"
          >
            <Filter size={12} /> 全部 {counts.all ?? 0}
          </button>
          {EVENT_CATEGORIES.map((item) => (
            <button
              key={item.id}
              className={`chip ${category === item.id ? "chip-active" : ""}`.trim()}
              onClick={() => setCategory(item.id)}
              disabled={!counts[item.id]}
              data-testid={`timeline-filter-${item.id}`}
            >
              {item.label} {counts[item.id] ?? 0}
            </button>
          ))}
        </div>

        {loading && !presented.length ? (
          <LoadingSkeleton rows={5} label="正在加载事件" />
        ) : !filtered.length ? (
          <EmptyState>
            {presented.length
              ? "没有匹配的事件：换个关键词，或点「全部」清除筛选"
              : "这个项目还没有事件。建项目、应用模板包、建任务、接入 Agent 都会在这里留痕。"}
          </EmptyState>
        ) : (
          <div style={{ display: "grid", gap: 14 }}>
            {groups.map((group) => (
              <div key={group.key}>
                <div className="form-row" style={{ justifyContent: "space-between", marginBottom: 6 }}>
                  <strong style={{ fontSize: 13 }}>{group.label}</strong>
                  <span className="hint">
                    {group.items.length} 条记录
                    {group.items.reduce((total, entry) => total + entry.count, 0) !== group.items.length
                      ? `（含 ${group.items.reduce((total, entry) => total + entry.count, 0)} 个事件）`
                      : ""}
                  </span>
                </div>
                <div className="list">
                  {group.items.map((entry) => {
                    const item = entry.item;
                    return (
                      <div className="list-item list-item-static" key={item.id} data-testid={`timeline-event-${item.sequence}`}>
                        <span className="icon-tile">{CATEGORY_ICONS[item.category]}</span>
                        <div className="item-copy">
                          <strong>
                            {item.title}
                            {entry.count > 1 ? <span className="chip" style={{ marginLeft: 6 }}>×{entry.count} 次</span> : null}
                          </strong>
                          <small>
                            <span className="chip" style={{ marginRight: 6 }}>
                              {ROLE_ICONS[item.actorRole]} {roleLabel(item.actorRole)} · {item.actorLabel}
                            </span>
                            {formatTime(item.createdAt)}（{relativeTime(item.createdAt)}）
                            {entry.count > 1 ? ` · 最早一条 ${formatTime(entry.firstAt)}` : ""}
                            {item.detail ? <div style={{ marginTop: 3 }}>{item.detail}</div> : null}
                            {showRaw ? (
                              <div style={{ marginTop: 3, fontFamily: "ui-monospace, monospace", opacity: 0.7 }}>
                                {item.rawType} · 序号 {item.sequence}
                                {item.objectId ? ` · 对象 ${item.objectId.slice(0, 8)}…` : ""}
                              </div>
                            ) : null}
                          </small>
                        </div>
                        {item.link ? (
                          <Link className="text-button" href={item.link.href}>
                            {item.link.label} →
                          </Link>
                        ) : null}
                      </div>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        )}
      </Panel>
    </div>
  );
}