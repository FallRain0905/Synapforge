"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Bot, CircleDot, Inbox, Loader2, RefreshCcw, UserCheck } from "lucide-react";
import { PageHeading, formatTime } from "../../components/shell";
import { EmptyState, LoadingSkeleton, Metric, Panel, StatusPill } from "../../components/ui";
import { MyTasks, TaskBoardItem, errorMessage, getMyTasks } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { useWorkspace } from "../../lib/workspace";

/**
 * 个人任务中心（P1-1）。
 *
 * 三组视图，全部是"与我有关"的任务：
 *   1. 指派给我的 —— 派单目标是我，还没结束（只有我的 Agent 能领）
 *   2. 我的 Agent 正在跑 —— 执行体属于我名下的机器
 *   3. 我的 Agent 最近完成 —— 从执行记录里看得到我这边产出过什么
 *
 * 之所以要这一页：任务在执行体一侧是"拉取"的（谁先轮到谁跑），
 * 没有这一页的话，被派了活的人只能靠自己在任务列表里翻。
 */
export default function MyTasksPage() {
  const { account, ready, authenticated } = useAuth();
  const { notify } = useWorkspace();
  const [board, setBoard] = useState<MyTasks>({ assigned: [], running: [], recent: [] });
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setBoard(await getMyTasks());
    } catch (error) {
      notify(errorMessage(error, "个人任务读取失败"));
    } finally {
      setLoading(false);
    }
  }, [notify]);

  useEffect(() => {
    // 必须等会话恢复完成：React 的子组件 effect 先于 Provider 跑，
    // 直接加载会拿到"还没注入令牌"的请求 → 401 → 页面显示成空
    if (!ready || !authenticated) return;
    void load();
  }, [ready, authenticated, load]);

  const renderRows = (items: TaskBoardItem[], emptyHint: string, testId: string) => (
    <div className="table" data-testid={testId}>
      <div className="table-head"><span>任务</span><span>项目</span><span>阶段</span><span>状态</span><span /></div>
      {items.map((item) => (
        <div className="table-row" key={item.task.id} data-testid={`${testId}-${item.task.id}`}>
          <div className="table-title">
            <strong>{item.task.title}</strong>
            <small>
              {item.task.description || "暂无说明"} · 更新于 {formatTime(item.task.updated_at)}
              {item.executor_agent_id ? ` · 执行者 ${item.executor_agent_id}` : ""}
              {item.assignee_member_name ? ` · 指派给 ${item.assignee_member_name}` : ""}
            </small>
          </div>
          <span className="hint" data-label="项目">{item.project_name || "—"}</span>
          <span className="chip" data-label="阶段">{item.task.stage}</span>
          <span data-label="状态">
            <StatusPill status={String(item.task.status)} />
          </span>
          <div className="row-actions">
            <Link className="text-button" href={`/tasks?task=${item.task.id}`}>去任务页</Link>
          </div>
        </div>
      ))}
      {!items.length && <EmptyState>{emptyHint}</EmptyState>}
    </div>
  );

  const assignedOpen = board.assigned.length;

  return (
    <div className="page-content" id="my-tasks">
      <PageHeading
        hint={`${account?.member.display_name ?? "我"} 的待办与我的 Agent 的执行情况`}
        actions={
          <button className="button button-secondary" onClick={() => void load()} disabled={loading} data-testid="my-tasks-refresh">
            {loading ? <Loader2 size={15} className="spin" /> : <RefreshCcw size={15} />} 刷新
          </button>
        }
      />

      <section className="metrics-grid">
        <Metric label="指派给我" value={assignedOpen} detail={assignedOpen ? "只有我的 Agent 能领取" : "当前没有派给我的任务"} tone={assignedOpen ? "warning" : "default"} />
        <Metric label="我的 Agent 正在跑" value={board.running.length} detail="执行体属于我名下的机器" />
        <Metric label="最近完成" value={board.recent.length} detail="我的 Agent 跑完的任务" />
      </section>

      <Panel title="指派给我的" subtitle="派单模式：只有你名下的设备能领取这些任务" testId="my-tasks-assigned">
        {loading ? <LoadingSkeleton rows={3} label="正在加载个人任务" /> : renderRows(board.assigned, "没有指派给你的任务——所有待办都在项目任务页里，谁先轮到谁跑", "my-assigned")}
      </Panel>

      <Panel title="我的 Agent 正在跑" subtitle="来自你名下机器的执行记录" testId="my-tasks-running">
        {loading ? <LoadingSkeleton rows={2} label="正在加载" /> : renderRows(board.running, "你的 Agent 当前没有在执行任务", "my-running")}
      </Panel>

      <Panel title="我的 Agent 最近完成" subtitle="跑完的任务（含失败与取消）" testId="my-tasks-recent">
        {loading ? <LoadingSkeleton rows={2} label="正在加载" /> : renderRows(board.recent, "还没有完成记录", "my-recent")}
      </Panel>

      <Panel title="怎么用" subtitle="派单与领取的关系" testId="my-tasks-help">
        <div className="list">
          <div className="list-item list-item-static">
            <span className="icon-tile icon-tile-blue"><UserCheck size={15} /></span>
            <div className="item-copy">
              <strong>被别人派了活</strong>
              <small>出现在「指派给我的」；只有你名下的 Agent 能领走，别人抢不到。你的机器连上平台就会自动领取。</small>
            </div>
          </div>
          <div className="list-item list-item-static">
            <span className="icon-tile icon-tile-amber"><Inbox size={15} /></span>
            <div className="item-copy">
              <strong>没被指派的任务</strong>
              <small>保持"谁先轮到谁跑"：任何有项目权限的 Agent 都能领取，适合并行推进的公共任务。</small>
            </div>
          </div>
          <div className="list-item list-item-static">
            <span className="icon-tile icon-tile-green"><Bot size={15} /></span>
            <div className="item-copy">
              <strong>我的机器在替谁干活</strong>
              <small>执行体归属由接入时的配对决定：谁在你的机器上完成配对，这台机器就算谁的 Agent。</small>
            </div>
          </div>
          <div className="list-item list-item-static">
            <span className="icon-tile"><CircleDot size={15} /></span>
            <div className="item-copy">
              <strong>写盘类任务</strong>
              <small>需要放宽沙箱（<code>--codex-sandbox workspace-write</code>）；产出会进成果物库等你或队友审核。</small>
            </div>
          </div>
        </div>
      </Panel>
    </div>
  );
}