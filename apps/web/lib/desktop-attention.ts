"use client";

/**
 * 桌面通知的关注清单接线（DESKTOP-NOTIFY）。
 *
 * 分工：
 *   - **壳**（Electron 主进程）负责轮询 `/api/my-attention` 并发系统通知——窗口关掉（托盘常驻）时也能提醒；
 *   - **前端**只做两件事：① 登录后把会话上下文交给壳（令牌只在内存里，不落盘）；② 事件里出现
 *     "任务派给我/退回给我"时叫壳立刻拉一次（低延迟，最多 1 次/5 秒）。
 * 浏览器里没有桥 → 整个 hook 是空操作（同一份前端跑两处）。
 */

import { useEffect, useRef } from "react";
import { API_URL, getSessionToken } from "./api";
import { desktopShell } from "./desktop";

/** 立刻催一次拉取的最小间隔：避免事件密集时把接口打爆（壳自己还有 60 秒的常规轮询）。 */
const MIN_POKE_INTERVAL_MS = 5000;

export function useDesktopAttention(options: {
  ready: boolean;
  authenticated: boolean;
  memberId?: string;
  events?: { event_type?: string; payload?: Record<string, unknown> }[];
}): void {
  const { ready, authenticated, memberId, events } = options;
  const lastPokeAt = useRef(0);

  // ① 登录态变化 → 交出/收回上下文
  useEffect(() => {
    const bridge = desktopShell();
    if (!bridge || !ready) return;
    if (!authenticated) return;
    const token = getSessionToken();
    if (!token || !memberId) return;
    void bridge.setAttentionContext({ token, api_url: API_URL, member_id: memberId });
  }, [ready, authenticated, memberId]);

  // ② 派单/退回事件 → 催一次（去抖）
  useEffect(() => {
    const bridge = desktopShell();
    if (!bridge || !authenticated || !events?.length) return;
    const interesting = events.some((event) => {
      const type = String(event.event_type ?? "");
      if (type !== "task.dispatched" && type !== "task.auto_assigned") return false;
      const payload = event.payload ?? {};
      // 只关心"派给我"：没有 assignee 或不是我就跳过（否则别人的派单也会催一次）
      const target = String(payload.assignee_member_id ?? "");
      return !memberId || target === memberId;
    });
    if (!interesting) return;
    const now = Date.now();
    if (now - lastPokeAt.current < MIN_POKE_INTERVAL_MS) return;
    lastPokeAt.current = now;
    void bridge.pollAttention();
  }, [events, authenticated, memberId]);
}