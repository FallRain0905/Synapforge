"use client";

/**
 * 桌面端桥（DESKTOP-GUI D-1/D-2）：工作台在桌面客户端里运行时，壳会注入 `window.synapforgeShell`。
 *
 * 设计原则：**浏览器里一切都还是老样子**——没有桥时这里全部返回 undefined / no-op，
 * 所以同一份前端既能跑在浏览器，也能跑在桌面端（不搞两套 UI）。
 * 桥的具体实现在 `apps/desktop/src/preload.js`；这里只是类型与薄封装。
 */

export type KernelAgent = {
  name?: string;
  state?: string;
  version?: string;
  adapter?: string;
};

export type KernelSnapshot = {
  connection?: { state?: string; retries?: number; last_error?: string };
  identity?: { device_id?: string; project_id?: string; platform_url?: string };
  local_agents?: KernelAgent[];
  tasks?: { paused?: boolean; completed_since_start?: number; claimed_current?: { id?: string; title?: string } | null };
  queue?: { local_queue_length?: number };
  emergency_stop?: boolean;
  shell?: {
    attention?: AttentionStatus;
    paired?: boolean;
    platform_url?: string;
    last_error?: string | null;
    contract?: string;
    app_version?: string;
    pending?: number;
    auto_launch?: boolean;
  };
};

export type AttentionStatus = {
  active: boolean;
  member_id: string | null;
  api_url: string | null;
  assigned_total: number;
  review_total: number;
  seen: number;
  last_poll_at: string | null;
  last_error: string | null;
};

export type ShellBridge = {
  snapshot: () => Promise<KernelSnapshot>;
  config: () => Promise<{ platform_url?: string; auto_launch?: boolean; app_version?: string }>;
  saveConfig: (patch: Record<string, unknown>) => Promise<Record<string, unknown>>;
  version: () => Promise<string>;
  rescan: () => Promise<KernelSnapshot>;
  togglePause: () => Promise<KernelSnapshot>;
  toggleEmergency: () => Promise<KernelSnapshot>;
  logs: () => Promise<string[]>;
  openWorkbench: () => Promise<unknown>;
  reloadWorkbench: () => Promise<unknown>;
  openSetup: () => Promise<unknown>;
  openExternal: (url: string) => Promise<unknown>;
  notify: (title: string, body?: string) => Promise<unknown>;
  /** 交出会话上下文，让壳后台轮询"派给我的任务 / 待我复核"并发系统通知（token 只在内存里）。 */
  setAttentionContext: (context: { token: string; api_url: string; member_id?: string }) => Promise<{ ok: boolean }>;
  attentionStatus: () => Promise<AttentionStatus>;
  pollAttention: () => Promise<{ ok: boolean; notifications?: unknown[] }>;
  checkUpdate: () => Promise<{ ok?: boolean; up_to_date?: boolean; current?: string; latest?: string; error?: string }>;
  setAutoLaunch: (enabled: boolean) => Promise<boolean>;
  onStatus: (handler: (snapshot: KernelSnapshot) => void) => void;
  onLog: (handler: (line: string) => void) => void;
  onOpenKernelPanel: (handler: () => void) => void;
};

/** 桌面端桥：浏览器里返回 undefined（调用方据此隐藏桌面专属入口）。 */
export function desktopShell(): ShellBridge | undefined {
  if (typeof window === "undefined") return undefined;
  const candidate = (window as unknown as { synapforgeShell?: ShellBridge }).synapforgeShell;
  if (!candidate || typeof candidate.snapshot !== "function") return undefined;
  return candidate;
}

export function isDesktopShell(): boolean {
  return Boolean(desktopShell());
}