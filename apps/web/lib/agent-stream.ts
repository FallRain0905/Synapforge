/**
 * my-agent 实时流客户端（W1.3 B 侧，消费 A 的两条 SSE 路由）。
 *
 * 为什么是 fetch 流而不是 EventSource：两条 stream 路由只认 `Authorization` 头
 * （main.py 的 `_request_member_id` 只读该头），而 EventSource 不能自定义请求头、
 * 平台会话令牌也不走 Cookie。fetch 流能带令牌，帧语义与 A 的口径完全一致：
 * - 游标：帧里的 `id:` 就是 seq；重连带 `?after=<seq>`（A：显式 after 优先于 Last-Event-ID）；
 * - `__gap__`：桥被裁剪、回放不完整 → 调用方全量重同步后重连，绝不把残缺回放当完整流；
 * - `__end__`：正常结束 → 调用方回权威接口取最终 content/stop_reason 覆盖临时流；
 * - `:` 开头的注释帧（心跳）忽略；未知事件名交调用方按契约忽略（AGENT_EVENT_CONTRACT 规则 3）。
 *
 * 实时层是加速器：900ms 轮询仍是权威兜底，事件按 sequence 去重合并，两边谁缺了都能被另一方补上。
 */

import { API_URL, apiFetch, type MyAgentTurnEvent } from "./api";

export type AgentStreamFrame = { event: string; seq: number | null; data: string };

export type AgentStreamHandle = { close: () => void };

/** 连不上的退避序列；全部失败后放弃实时层（调用方留在轮询模式），不对用户刷错误。 */
const CONNECT_BACKOFF_MS = [1000, 2000, 4000, 8000];

type StreamOptions = {
  url: string;
  /** 起始游标（不含）：服务端补发 seq 更大的事件；缺省从头回放。 */
  lastSeq?: number;
  onFrame: (frame: AgentStreamFrame) => void;
  /** 受控结束：gap（需全量重同步）或 end（正常收尾）。此后不再自动重连。 */
  onControlEnd: (reason: "gap" | "end") => void;
  /** 反复连接失败：放弃实时层，静默降级轮询。 */
  onGiveUp: () => void;
};

function parseFrame(block: string): AgentStreamFrame | null {
  let event = "message";
  let seq: number | null = null;
  const dataLines: string[] = [];
  for (const rawLine of block.split("\n")) {
    if (!rawLine || rawLine.startsWith(":")) continue; // 注释帧：心跳
    const colon = rawLine.indexOf(":");
    const field = colon < 0 ? rawLine : rawLine.slice(0, colon);
    let value = colon < 0 ? "" : rawLine.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    else if (field === "id") {
      const parsed = Number(value);
      if (Number.isFinite(parsed) && value.trim() !== "") seq = parsed;
    } else if (field === "data") dataLines.push(value);
  }
  if (!dataLines.length && event === "message") return null;
  return { event, seq, data: dataLines.join("\n") };
}

export function openAgentStream(options: StreamOptions): AgentStreamHandle {
  const controller = new AbortController();
  let closed = false;
  let lastSeq = options.lastSeq ?? null;
  let failures = 0;

  const run = async () => {
    while (!closed) {
      let sawFrame = false;
      try {
        const url =
          lastSeq !== null
            ? `${options.url}${options.url.includes("?") ? "&" : "?"}after=${lastSeq}`
            : options.url;
        const response = await apiFetch(url, { cache: "no-store", signal: controller.signal });
        if (!response.ok || !response.body) throw new Error(`stream_http_${response.status}`);
        failures = 0;
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (!closed) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let separator = buffer.indexOf("\n\n");
          while (separator >= 0) {
            const block = buffer.slice(0, separator);
            buffer = buffer.slice(separator + 2);
            separator = buffer.indexOf("\n\n");
            const frame = parseFrame(block);
            if (!frame) continue;
            sawFrame = true;
            // 两个控制帧都终止本条流：gap 由调用方重同步后重连；end 是正常收尾
            if (frame.event === "__gap__" || frame.event === "__end__") {
              options.onControlEnd(frame.event === "__gap__" ? "gap" : "end");
              return;
            }
            if (frame.seq !== null) lastSeq = frame.seq;
            options.onFrame(frame);
          }
        }
        if (closed) return;
        // 流被服务端/网络关掉但没发 __end__：当普通断线，带游标重连
        if (sawFrame) {
          failures = 0;
          continue;
        }
        throw new Error("stream_closed_without_frames");
      } catch (error) {
        if (closed || (error instanceof DOMException && error.name === "AbortError")) return;
        failures += 1;
        if (failures > CONNECT_BACKOFF_MS.length) {
          options.onGiveUp();
          return;
        }
        await new Promise((resolve) => setTimeout(resolve, CONNECT_BACKOFF_MS[failures - 1] ?? 8000));
      }
    }
  };
  void run();
  return {
    close: () => {
      closed = true;
      controller.abort();
    },
  };
}

/** 轮次事件流：帧 data 是事件 payload（event 名与 seq 在帧字段里），这里重建成页面的事件对象。 */
export function openAgentTurnStream(options: {
  turnId: string;
  lastSeq?: number;
  onEvent: (event: MyAgentTurnEvent) => void;
  onGap: () => void;
  onEnd: () => void;
  onGiveUp: () => void;
}): AgentStreamHandle {
  return openAgentStream({
    url: `${API_URL}/api/my-agent/turns/${options.turnId}/events/stream`,
    lastSeq: options.lastSeq,
    onFrame: (frame) => {
      let payload: Record<string, unknown> = {};
      try {
        const parsed = JSON.parse(frame.data) as unknown;
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
          payload = parsed as Record<string, unknown>;
        }
      } catch {
        payload = { text: frame.data }; // data 不是 JSON 时按纯文本增量处理，不丢字
      }
      options.onEvent({
        id: "",
        turn_id: options.turnId,
        sequence: frame.seq ?? 0,
        event_type: frame.event,
        payload,
        created_at: "",
      });
    },
    onControlEnd: (reason) => (reason === "gap" ? options.onGap() : options.onEnd()),
    onGiveUp: options.onGiveUp,
  });
}

/** 会话流（turn.created / finished / cancelled 生命周期信号）：任何帧（含 gap/end）都让页面重拉轮次。 */
export function openAgentConversationStream(options: {
  conversationId: string;
  onSignal: (action: string) => void;
  onGiveUp: () => void;
}): AgentStreamHandle {
  return openAgentStream({
    url: `${API_URL}/api/my-agent/conversations/${options.conversationId}/stream`,
    onFrame: (frame) => options.onSignal(frame.event),
    onControlEnd: () => options.onSignal("__resync__"),
    onGiveUp: options.onGiveUp,
  });
}
