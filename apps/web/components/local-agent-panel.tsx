"use client";

/**
 * 本机 Agent 面板（DESKTOP-GUI D-2）：在桌面客户端的窗口里显示**这台机器**的内核状态。
 *
 * 数据来自壳的 preload 桥（`window.synapforgeShell`）——也就是 Python 内核（契约 v1），
 * 不是服务器 API：所以它显示的是"我的电脑在干什么"，与"平台上的任务"是两回事。
 *
 * 浏览器里没有桥 → 不渲染任何东西（`isDesktopShell()` 为 false）。
 */

import { useCallback, useEffect, useState } from "react";
import { Activity, AlertTriangle, BellRing, Cpu, Loader2, RefreshCcw, ServerCog, Square, X } from "lucide-react";
import { KernelSnapshot, desktopShell } from "../lib/desktop";

const STATE_LABEL: Record<string, string> = {
  connected: "已连接平台",
  connecting: "连接中",
  disconnected: "未连接",
  backoff: "退避重连中",
};

export function LocalAgentPanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const bridge = desktopShell();
  const [snapshot, setSnapshot] = useState<KernelSnapshot | null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const [busy, setBusy] = useState("");
  const [version, setVersion] = useState("");

  const refresh = useCallback(async () => {
    if (!bridge) return;
    try {
      setSnapshot(await bridge.snapshot());
      setLogs(await bridge.logs());
      setVersion(await bridge.version());
    } catch {
      /* 内核未就绪时静默：面板会显示"未就绪" */
    }
  }, [bridge]);

  useEffect(() => {
    if (!bridge || !open) return;
    void refresh();
    bridge.onStatus((next) => setSnapshot(next));
    bridge.onLog((line) => setLogs((current) => [...current.slice(-199), line]));
    const timer = setInterval(() => void refresh(), 5000);
    return () => clearInterval(timer);
  }, [bridge, open, refresh]);

  if (!bridge || !open) return null;

  const shell = snapshot?.shell;
  const connection = snapshot?.connection || {};
  const tasks = snapshot?.tasks || {};
  const available = (snapshot?.local_agents || []).filter((item) => item.state === "AVAILABLE");
  const state = connection.state || "disconnected";

  const act = async (name: string, action: () => Promise<unknown>) => {
    setBusy(name);
    try {
      await action();
      await refresh();
    } finally {
      setBusy("");
    }
  };

  return (
    <aside className="kernel-panel" data-testid="local-agent-panel">
      <header className="kernel-head">
        <span className="kernel-title">
          <ServerCog size={15} /> 本机 Agent
        </span>
        <div className="kernel-head-actions">
          <button
            type="button"
            className="app-icon"
            title="刷新"
            data-testid="local-agent-refresh"
            onClick={() => void act("refresh", refresh)}
          >
            {busy === "refresh" ? <Loader2 size={14} className="spin" /> : <RefreshCcw size={14} />}
          </button>
          <button type="button" className="app-icon" title="收起" onClick={onClose} data-testid="local-agent-close">
            <X size={15} />
          </button>
        </div>
      </header>

      <div className="kernel-body">
        <div className="kernel-status">
          <span className={`kernel-dot is-${shell?.last_error ? "red" : !shell?.paired ? "grey" : state === "connected" ? "green" : "yellow"}`} />
          <div>
            <strong>{STATE_LABEL[state] ?? state}</strong>
            <small>
              {shell?.paired ? `设备 ${snapshot?.identity?.device_id ?? "—"}` : "尚未接入平台（可在「设备与接入」页配对）"}
            </small>
          </div>
        </div>

        {shell?.last_error ? (
          <div className="kernel-error">
            <AlertTriangle size={13} /> {shell.last_error}
          </div>
        ) : null}

        <div className="kernel-grid">
          <div>
            <small>正在执行</small>
            <strong>{tasks.claimed_current ? String(tasks.claimed_current.title || tasks.claimed_current.id) : "空闲"}</strong>
          </div>
          <div>
            <small>已完成</small>
            <strong>{tasks.completed_since_start ?? 0} 个</strong>
          </div>
          <div>
            <small>本地队列</small>
            <strong>{snapshot?.queue?.local_queue_length ?? 0}</strong>
          </div>
          <div>
            <small>桌面端版本</small>
            <strong>{version || "—"}</strong>
          </div>
        </div>

        <div className="kernel-block" data-testid="local-agent-attention">
          <small className="kernel-label">
            <BellRing size={12} /> 桌面提醒（派给我的任务 / 待我复核）
          </small>
          {shell?.attention?.active ? (
            <div>
              <strong>
                派给我 {shell.attention.assigned_total} · 待复核 {shell.attention.review_total}
              </strong>
              <small className="hint">
                由桌面端后台检查（关掉窗口也会提醒）；上次检查{" "}
                {shell.attention.last_poll_at ? new Date(shell.attention.last_poll_at).toLocaleTimeString("zh-CN") : "—"}
                {shell.attention.last_error ? ` · 上次错误 ${shell.attention.last_error}` : ""}
              </small>
            </div>
          ) : (
            <small className="hint">还没有开启：登录后桌面端会自动开始检查（会话令牌只在内存里，不落盘）</small>
          )}
        </div>

        <div className="kernel-block">
          <small className="kernel-label">
            <Cpu size={12} /> 这台机器上的执行体（{available.length} 个可用）
          </small>
          <div className="chips">
            {(snapshot?.local_agents || []).length ? (
              (snapshot?.local_agents || []).map((item) => (
                <span className={`chip ${item.state === "AVAILABLE" ? "" : "chip-muted"}`} key={item.name}>
                  {item.name ?? "—"}
                  {item.version ? `@${item.version}` : ""}
                  {item.state === "AVAILABLE" ? "" : `（${item.state ?? "未知"}）`}
                </span>
              ))
            ) : (
              <span className="hint">没有探测到本机 Agent；点「重扫本机 Agent」再试。</span>
            )}
          </div>
        </div>

        <div className="kernel-actions">
          <button type="button" className="button button-secondary" disabled={busy === "rescan"} onClick={() => void act("rescan", () => bridge.rescan())}>
            {busy === "rescan" ? <Loader2 size={13} className="spin" /> : <RefreshCcw size={13} />} 重扫本机 Agent
          </button>
          <button type="button" className="button button-secondary" disabled={busy === "pause"} onClick={() => void act("pause", () => bridge.togglePause())}>
            {tasks.paused ? <Activity size={13} /> : <Square size={13} />} {tasks.paused ? "恢复领取" : "暂停领取"}
          </button>
          <button
            type="button"
            className="button button-secondary"
            disabled={busy === "emergency"}
            onClick={() => void act("emergency", () => bridge.toggleEmergency())}
          >
            <AlertTriangle size={13} /> {snapshot?.emergency_stop ? "解除紧急停止" : "紧急停止"}
          </button>
          <button type="button" className="text-button" onClick={() => void bridge.openSetup()}>
            独立窗口 / 设置
          </button>
          <button type="button" className="text-button" onClick={() => void bridge.checkUpdate()}>
            检查更新
          </button>
        </div>

        <div className="kernel-block">
          <small className="kernel-label">内核日志（最近 {logs.length} 行）</small>
          <pre className="kernel-logs" data-testid="local-agent-logs">
            {logs.length ? logs.slice(-40).join("\n") : "（还没有日志）"}
          </pre>
        </div>

        <p className="hint">
          面板读的是**本机内核**（Python sidecar，契约 v1），不是服务器：显示这台机器在跑什么、探测到哪些执行体。
          设备令牌只在 Windows 凭据管理器里，页面拿不到。
        </p>
      </div>
    </aside>
  );
}