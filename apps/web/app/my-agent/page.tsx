"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Activity, ArrowDown, ArrowUp, Bot, Brain, Cpu, FileText, FolderInput, Gauge, History, MessageSquare, Paperclip, Play, Plus, RefreshCcw, Send, Server, ShieldAlert, Sparkles, Square, Trash2, Wrench, X } from "lucide-react";
import Link from "next/link";
import { AgentResponse } from "../../components/agent-response";
import { Markdown } from "../../components/markdown";
import { PageHeading } from "../../components/shell";
import { ConfirmDialog, EmptyState, Panel } from "../../components/ui";
import {
  MyAgentConversation,
  MyAgentEndpoint,
  MyAgentTurn,
  MyAgentTurnApproval,
  MyAgentTurnEvent,
  MyAgentTurnOutput,
  copyArtifactToDrive,
  createMyAgentConversation,
  decideMyAgentTurnApproval,
  deleteMyAgentConversation,
  downloadArtifactContent,
  listMyAgentConversations,
  listMyAgentTurnApprovals,
  listMyAgentTurnEvents,
  listMyAgentTurns,
  importDriveFile,
  listMyAgents,
  sendMyAgentMessage,
  uploadDriveFile,
  stopMyAgentTurn,
  updateMyAgentConversation,
} from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { useWorkspace } from "../../lib/workspace";
import { STOP_REASON_LABEL } from "../../lib/status-dictionary";
import { openAgentConversationStream, openAgentTurnStream } from "../../lib/agent-stream";
import { PromoteDialog } from "../../components/promote-dialog";

const ACTIVE_STATUSES = new Set(["PENDING", "CLAIMED"]);

/** 时间戳的紧凑写法（抽屉里显示"装于 …"用；解析不了就退化成原串前 16 位）。 */
function formatTime(iso: string): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso.slice(0, 16);
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** 文件大小的可读写法（产出清单里显示，避免"1234567 字节"这种读不出来的数）。 */
function formatBytes(size: number): string {
  const value = Number(size) || 0;
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}
const STATUS_LABEL: Record<string, string> = {
  PENDING: "准备中",
  CLAIMED: "执行中",
  DONE: "已完成",
  FAILED: "失败",
  CANCELLED: "已停止",
};

/** 终态补充说明（W1.1）：stop_reason 说明"这轮是怎么结束的"。
 * 平台响应尚未带上该字段（execute 侧契约未落地）时它就是 undefined，这里自然不显示——
 * 不伪造、不猜测。FAILED 且 error 文案已在时不再重复"执行失败"。 */
function stopReasonNote(turn: MyAgentTurn): string {
  if (ACTIVE_STATUSES.has(turn.status)) return "";
  const label = STOP_REASON_LABEL[turn.stop_reason ?? ""] ?? "";
  if (!label) return "";
  if (turn.status === "FAILED" && turn.stop_reason === "failed") return "";
  return label;
}
type Variant = "" | "minimal" | "high" | "max";
const VARIANT_LABEL: Record<Variant, string> = {
  "": "默认强度",
  minimal: "精简",
  high: "深入",
  max: "最大",
};
/** 过程事件的中文名——与 lib/events.ts 的口径一致（执行体那一侧的事件类型）。 */
const EVENT_LABEL: Record<string, string> = {
  "process.started": "开始执行",
  "process.exited": "执行结束",
  "agent.message": "执行体输出",
  "tool.completed": "调用了工具",
  "file.changed": "修改了文件",
};
const EVENT_ICON: Record<string, typeof Activity> = {
  "process.started": Play,
  "process.exited": Square,
  "agent.message": MessageSquare,
  "tool.completed": Wrench,
  "file.changed": FileText,
};

type Mode = "chat" | "work";

type TurnEventMap = Record<string, MyAgentTurnEvent[]>;
type TurnApprovalMap = Record<string, MyAgentTurnApproval[]>;

function answerSegments(events: MyAgentTurnEvent[]): string[] {
  const whole = events
    .filter((event) => event.event_type === "agent.message")
    .map((event) => String(event.payload.text ?? "").trim())
    .filter(Boolean);
  const streamed = events
    .filter((event) => event.event_type === "delta")
    .map((event) => String(event.payload.text ?? ""))
    .join("");
  return streamed ? [...whole, streamed] : whole;
}

function thinkingFrom(events: MyAgentTurnEvent[]): string {
  return events
    .filter((event) => event.event_type === "thinking")
    .map((event) => String(event.payload.text ?? ""))
    .join("");
}

function ThinkingDisclosure({ turnId, text, active }: { turnId: string; text: string; active: boolean }) {
  const [open, setOpen] = useState(false);
  if (!text) return null;
  return (
    <div className="my-agent-thinking" data-testid={`my-agent-thinking-${turnId}`}>
      <button
        type="button"
        className="my-agent-thinking-head"
        aria-expanded={open}
        data-testid={`my-agent-thinking-toggle-${turnId}`}
        onClick={() => setOpen((current) => !current)}
      >
        <Brain size={13} /> {active ? "正在思考" : "思考过程"}
        <span className="my-agent-thinking-hint">{open ? "点一下收起" : `${text.length} 字，点一下展开`}</span>
      </button>
      {open ? (
        <div className="my-agent-thinking-body" data-testid={`my-agent-thinking-body-${turnId}`}>
          {text}
        </div>
      ) : null}
    </div>
  );
}

/**
 * 我的智能体（MY-AGENT M-2）：一个**单纯对话**工作页。
 *
 * 用户拍板：「对话」和「项目工作」是两类——这个页面把它们分成两个 tab：
 * - **对话**：与接到平台的执行体（云端 opencode）多轮聊天，不建任务、不进任务板、不走复核；
 * - **项目工作**：跑任务的那条线（任务板 / 派单 / 成果物），这里只做入口与项目选择。
 *
 * 多轮上下文由执行体的会话句柄维持（`session_key`，第 1 轮拿到后每轮带回去）；
 * 模型与设备来自执行体的真探测（心跳上报），不是写死的下拉。
 */
export default function MyAgentPage() {
  const { notify, projects, project: activeProject, selectProject } = useWorkspace();
  // 谁批的权限请求：拿当前账号的成员 id 比对（是本人就说"你"）
  const { ready, authenticated, account } = useAuth();

  const [mode, setMode] = useState<Mode>("chat");
  const [agents, setAgents] = useState<MyAgentEndpoint[]>([]);
  const [agentIndex, setAgentIndex] = useState(0);
  const [conversations, setConversations] = useState<MyAgentConversation[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const selectedIdRef = useRef(selectedId);
  selectedIdRef.current = selectedId;
  const finishingTurn = useRef("");
  const [turns, setTurns] = useState<MyAgentTurn[]>([]);
  const [eventsByTurn, setEventsByTurn] = useState<TurnEventMap>({});
  const [approvalsByTurn, setApprovalsByTurn] = useState<TurnApprovalMap>({});
  const [model, setModel] = useState("");
  // M-6：角色（执行体上的 opencode agent）。空串 = 默认（不传 --agent）。选项来自执行体真探测。
  const [role, setRole] = useState("");
  // S-4：非默认强度使用 opencode CLI 的 `--variant`；serve 会静默忽略该值，所以不能假装仍是真流式。
  const [variant, setVariant] = useState<Variant>("");
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<{ id: string; title: string } | null>(null);
  // 右侧「本轮执行」默认收起：主区域留给对话，需要时再拉开（用户要求）
  const [detailOpen, setDetailOpen] = useState(false);
  // M-3：随这一轮给执行体的文件（云盘 → 项目成果物 → 轮次带 id 与名字；执行体下到工作目录 inputs/）
  const [attachments, setAttachments] = useState<{ artifact_id: string; name: string }[]>([]);
  const [uploading, setUploading] = useState(false);
  const fileInput = useRef<HTMLInputElement | null>(null);
  const messageScroller = useRef<HTMLDivElement | null>(null);
  const streamEnd = useRef<HTMLDivElement | null>(null);
  const stickToBottom = useRef(true);
  const lastVisibleLength = useRef(0);
  const inputBox = useRef<HTMLTextAreaElement | null>(null);
  const sessionDrawerTrigger = useRef<HTMLButtonElement | null>(null);
  const sessionDrawerClose = useRef<HTMLButtonElement | null>(null);
  const sessionDrawerPanel = useRef<HTMLElement | null>(null);
  const detailDrawerTrigger = useRef<HTMLButtonElement | null>(null);
  const detailDrawerClose = useRef<HTMLButtonElement | null>(null);
  const detailDrawerPanel = useRef<HTMLElement | null>(null);
  const [sessionDrawerOpen, setSessionDrawerOpen] = useState(false);
  const [newReply, setNewReply] = useState(false);
  // 「+」展开的添加面板（仿 zcode 的添加区：附件 / 角色 / 执行体）
  const [addOpen, setAddOpen] = useState(false);
  // 转入项目生产（W1.2）：把这次对话的产出建成正式任务
  const [promoteOpen, setPromoteOpen] = useState(false);
  const composerRef = useRef<HTMLDivElement | null>(null);

  const agent = agents[agentIndex] ?? null;
  const selected = conversations.find((item) => item.id === selectedId) ?? null;
  const activeTurn = useMemo(() => turns.find((turn) => ACTIVE_STATUSES.has(turn.status)) ?? null, [turns]);
  const lastTurn = turns.length ? turns[turns.length - 1] : null;

  /* ---- 载入 ------------------------------------------------------------ */

  const loadAgents = useCallback(async () => {
    try {
      const list = await listMyAgents();
      setAgents(list);
      setAgentIndex((current) => (current < list.length ? current : 0));
      if (!model && list.length) setModel(list[0].default_model || list[0].models[0] || "");
    } catch {
      notify("执行体列表读取失败");
    }
  }, [model, notify]);

  const loadConversations = useCallback(
    async (preferredId?: string) => {
      try {
        const list = await listMyAgentConversations();
        setConversations(list);
        if (preferredId) {
          setSelectedId(preferredId);
        } else {
          // 刷新后自动选中最近一条：否则中栏空着，用户会以为"对话没了"（实测体验问题）
          setSelectedId((current) => (current && list.some((item) => item.id === current) ? current : list[0]?.id ?? ""));
        }
      } catch {
        notify("会话列表读取失败");
      }
    },
    [notify],
  );

  const loadTurns = useCallback(async (conversationId: string) => {
    if (!conversationId) {
      setTurns([]);
      return [] as MyAgentTurn[];
    }
    try {
      const fresh = await listMyAgentTurns(conversationId);
      if (selectedIdRef.current === conversationId) setTurns(fresh);
      return fresh;
    } catch {
      /* 轮询失败不打扰用户（下一拍会补上） */
      return [] as MyAgentTurn[];
    }
  }, []);

  // 会话就绪门控：AuthProvider 恢复令牌前发请求会 401（与 /ask 页同样的坑）
  useEffect(() => {
    if (!ready || !authenticated) return;
    void loadAgents();
    void loadConversations();
  }, [ready, authenticated, loadAgents, loadConversations]);

  useEffect(() => {
    if (!selectedId) {
      setTurns([]);
      setEventsByTurn({});
      setApprovalsByTurn({});
      return;
    }
    let cancelled = false;
    finishingTurn.current = "";
    setTurns([]);
    setEventsByTurn({});
    setApprovalsByTurn({});
    void loadTurns(selectedId).then(async (freshTurns) => {
      if (cancelled) return;
      const realTurns = freshTurns.filter((turn) => !turn.id.startsWith("local-"));
      for (let index = 0; index < realTurns.length && !cancelled; index += 4) {
        const batch = await Promise.all(
          realTurns.slice(index, index + 4).map(async (turn) => {
            const [turnEvents, turnApprovals] = await Promise.all([
              listMyAgentTurnEvents(turn.id).catch(() => []),
              listMyAgentTurnApprovals(turn.id).catch(() => []),
            ]);
            return { turnId: turn.id, turnEvents, turnApprovals };
          }),
        );
        if (cancelled) return;
        setEventsByTurn((current) => ({ ...Object.fromEntries(batch.map((item) => [item.turnId, item.turnEvents])), ...current }));
        setApprovalsByTurn((current) => ({ ...Object.fromEntries(batch.map((item) => [item.turnId, item.turnApprovals])), ...current }));
      }
    });
    return () => {
      cancelled = true;
    };
  }, [selectedId, loadTurns]);

  // 切会话时把工具条上的角色与模型同步成这条会话的设置：否则下拉显示的是上一条会话的值（看着像"设置丢了"）
  useEffect(() => {
    if (!selected) return;
    setRole(selected.role || "");
    setVariant((selected.variant || "") as Variant);
    if (selected.model) setModel(selected.model);
    // 只在切换会话时同步（selected 的身份变化），不跟着每次列表刷新跑——否则用户刚改的下拉会被覆盖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);

  // 一轮在跑：每 900ms 刷新当前轮。历史轮次只在切换会话时重放，避免 20+ 轮时放大请求。
  useEffect(() => {
    if (!selectedId || !activeTurn || activeTurn.conversation_id !== selectedId) return;
    let cancelled = false;
    let polling = false;
    const tick = async () => {
      if (polling) return;
      polling = true;
      try {
        const [fresh, freshEvents, freshApprovals] = await Promise.all([
          listMyAgentTurns(selectedId),
          listMyAgentTurnEvents(activeTurn.id),
          listMyAgentTurnApprovals(activeTurn.id),
        ]);
        if (cancelled || selectedIdRef.current !== selectedId) return;
        setTurns(fresh);
        setEventsByTurn((current) => ({ ...current, [activeTurn.id]: freshEvents }));
        setApprovalsByTurn((current) => ({ ...current, [activeTurn.id]: freshApprovals }));
      } catch {
        /* 忽略单次失败 */
      } finally {
        polling = false;
      }
    };
    void tick();
    const timer = window.setInterval(tick, 900);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [selectedId, activeTurn]);

  // 结束时无条件补拉末尾事件；运行时的缓存可能早于最终持久化。
  useEffect(() => {
    if (!lastTurn || lastTurn.conversation_id !== selectedId || ACTIVE_STATUSES.has(lastTurn.status) || lastTurn.id.startsWith("local-")) return;
    if (finishingTurn.current === lastTurn.id) return;
    finishingTurn.current = lastTurn.id;
    const turnId = lastTurn.id;
    const conversationId = selectedId;
    void Promise.all([
      listMyAgentTurnEvents(turnId),
      listMyAgentTurnApprovals(turnId),
    ])
      .then(([turnEvents, turnApprovals]) => {
        if (selectedIdRef.current !== conversationId) return;
        setEventsByTurn((current) => ({ ...current, [turnId]: turnEvents }));
        setApprovalsByTurn((current) => ({ ...current, [turnId]: turnApprovals }));
      })
      .catch(() => {
        if (selectedIdRef.current === conversationId) finishingTurn.current = "";
      });
  }, [lastTurn, selectedId]);

  // ---- 实时流（W1.3 B 侧）：轮次事件流 + 会话生命周期流 --------------------
  // 定位是"加速器"：900ms 轮询效应原样保留（权威兜底），事件按 sequence 去重合并，
  // 两边谁缺了都能被另一方补上。连接反复失败时静默降级（实时层不可用≠功能不可用）。

  const mergeTurnEvent = useCallback((turnId: string, incoming: MyAgentTurnEvent) => {
    setEventsByTurn((current) => {
      const existing = current[turnId] ?? [];
      if (existing.some((event) => event.sequence === incoming.sequence && incoming.sequence > 0)) return current;
      const next = [...existing, incoming].sort((a, b) => a.sequence - b.sequence);
      return { ...current, [turnId]: next };
    });
  }, []);

  // 轮次流：跟着"当前活跃轮次"走。依赖用 id 而不是对象——轮询每 900ms 产生新对象，不能跟着重连。
  const activeTurnId = activeTurn?.id ?? "";
  useEffect(() => {
    if (!activeTurnId || activeTurnId.startsWith("local-")) return;
    const conversationId = selectedIdRef.current;
    const handle = openAgentTurnStream({
      turnId: activeTurnId,
      onEvent: (event) => mergeTurnEvent(activeTurnId, event),
      onGap: () => {
        // 桥被裁剪：回权威接口全量重同步（轮询也会补），然后不再续这条流——
        // 轮询仍在跑，重新开流交给下一个 effect 触发时机
        void listMyAgentTurnEvents(activeTurnId)
          .then((fresh) => {
            if (selectedIdRef.current === conversationId) {
              setEventsByTurn((current) => ({ ...current, [activeTurnId]: fresh }));
            }
          })
          .catch(() => undefined);
      },
      onEnd: () => {
        // 正常结束：回 GET /turns 取最终 content/stop_reason 覆盖临时流（与 finishing 效应同口径）
        if (selectedIdRef.current === conversationId) void loadTurns(conversationId);
      },
      onGiveUp: () => undefined, // 静默降级轮询
    });
    return () => handle.close();
  }, [activeTurnId, mergeTurnEvent, loadTurns]);

  // 会话流：turn.created / finished / cancelled 生命周期信号 → 立刻重拉轮次列表
  // （没有活跃轮次时轮询不跑，这条流让"新轮次出现"不再等下一次交互）。
  useEffect(() => {
    if (!selectedId || selectedId.startsWith("local-")) return;
    const conversationId = selectedId;
    const handle = openAgentConversationStream({
      conversationId,
      onSignal: () => {
        if (selectedIdRef.current === conversationId) void loadTurns(conversationId);
      },
      onGiveUp: () => undefined,
    });
    return () => handle.close();
  }, [selectedId, loadTurns]);

  const activeEvents = activeTurn ? eventsByTurn[activeTurn.id] ?? [] : [];
  const thinkingLength = useMemo(
    () => Object.values(eventsByTurn).reduce((sum, turnEvents) => sum + thinkingFrom(turnEvents).length, 0),
    [eventsByTurn],
  );
  const liveAnswerLength = useMemo(
    () => answerSegments(activeEvents).reduce((sum, segment) => sum + segment.length, 0),
    [activeEvents],
  );

  useEffect(() => {
    const approvalCount = Object.values(approvalsByTurn).reduce((sum, rows) => sum + rows.length, 0);
    const visibleLength = turns.length + thinkingLength + liveAnswerLength + approvalCount;
    if (stickToBottom.current) {
      streamEnd.current?.scrollIntoView({ behavior: "smooth", block: "end" });
      setNewReply(false);
    } else if (visibleLength > lastVisibleLength.current) {
      setNewReply(true);
    }
    lastVisibleLength.current = visibleLength;
  }, [turns.length, thinkingLength, liveAnswerLength, approvalsByTurn]);

  const closeSessionDrawer = useCallback(() => {
    setSessionDrawerOpen(false);
    window.requestAnimationFrame(() => sessionDrawerTrigger.current?.focus());
  }, []);
  const closeDetailDrawer = useCallback(() => {
    setDetailOpen(false);
    window.requestAnimationFrame(() => detailDrawerTrigger.current?.focus());
  }, []);

  useEffect(() => {
    if (!sessionDrawerOpen && !detailOpen) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const target = sessionDrawerOpen ? sessionDrawerClose.current : detailDrawerClose.current;
    target?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        if (sessionDrawerOpen) closeSessionDrawer();
        else closeDetailDrawer();
        return;
      }
      if (event.key !== "Tab") return;
      const panel = sessionDrawerOpen ? sessionDrawerPanel.current : detailDrawerPanel.current;
      if (!panel) return;
      const focusable = Array.from(
        panel.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      );
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
    };
  }, [sessionDrawerOpen, detailOpen, closeSessionDrawer, closeDetailDrawer]);

  /** 添加面板：点面板外或按 Esc 收起（与 zcode 的添加区一致，不用点空白处找关闭）。 */
  useEffect(() => {
    if (!addOpen) return;
    const onPointerDown = (event: MouseEvent) => {
      if (!composerRef.current?.contains(event.target as Node)) setAddOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setAddOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [addOpen]);

  /* ---- 动作 ------------------------------------------------------------ */

  const ensureConversation = useCallback(async (): Promise<MyAgentConversation | null> => {
    if (selected) return selected;
    if (!agent) {
      notify("没有可用执行体：先让设备在线并授权「对话」");
      return null;
    }
    try {
      const conversation = await createMyAgentConversation({
        project_id: agent.project_id,
        device_id: agent.device_id,
        model,
        role,
        variant,
      });
      setSelectedId(conversation.id);
      await loadConversations(conversation.id);
      return conversation;
    } catch (error) {
      notify(error instanceof Error ? error.message : "会话创建失败");
      return null;
    }
  }, [agent, loadConversations, model, notify, role, selected, variant]);

  const handleNewChat = async () => {
    setBusy(true);
    try {
      if (!agent) {
        notify("没有可用执行体：先让设备在线并授权「对话」");
        return;
      }
      // 新会话从**默认角色**开始（角色是"这一条对话的活儿"，不该被上一条对话继承——踩过：
      // 上一条用「文献检索」，新建后它仍是只读角色，人说"写个文件"却一直被拒）。
      // 模型是全局偏好，保留当前选择。
      setRole("");
      const conversation = await createMyAgentConversation({
        project_id: agent.project_id,
        device_id: agent.device_id,
        model,
        role: "",
        variant,
      });
      setSelectedId(conversation.id);
      await loadConversations(conversation.id);
      setTurns([]);
      notify("已新建对话");
      inputBox.current?.focus();
    } catch (error) {
      notify(error instanceof Error ? error.message : "会话创建失败");
    } finally {
      setBusy(false);
    }
  };

  const sendContent = async (
    content: string,
    files: { artifact_id: string; name: string }[] = [],
  ): Promise<boolean> => {
    const question = content.trim();
    if (!question || busy || activeTurn) return false;
    const conversation = await ensureConversation();
    if (!conversation) return false;
    setBusy(true);
    try {
      const optimistic: MyAgentTurn = {
        id: `local-${Date.now()}`,
        conversation_id: conversation.id,
        seq: (turns[turns.length - 1]?.seq ?? 0) + 1,
        status: "PENDING",
        prompt: question,
        content: "",
        model,
        role,
        variant,
        session_key: conversation.session_key,
        usage: {},
        error: "",
        artifacts: files,
        outputs: [],
        created_at: new Date().toISOString(),
        completed_at: null,
      };
      setTurns((current) => [...current, optimistic]);
      await sendMyAgentMessage(
        conversation.id,
        question,
        files.map((item) => item.artifact_id),
      );
      await loadTurns(conversation.id);
      await loadConversations(conversation.id);
      return true;
    } catch (error) {
      notify(error instanceof Error ? error.message : "发送失败");
      await loadTurns(conversation.id);
      return false;
    } finally {
      setBusy(false);
    }
  };

  const handleSend = async () => {
    const question = input.trim();
    if (!question || busy || activeTurn) return;
    const attached = attachments;
    setInput("");
    setAttachments([]);
    const sent = await sendContent(question, attached);
    if (!sent) {
      // 没有发出去（例如会话正忙）就把用户刚填的内容还回来，不吃掉输入
      setInput(question);
      setAttachments(attached);
    }
  };

  /** 上传附件：走云盘 → 导入本项目成果物（与项目工作区聊天同一条链路，不另造一套）。 */
  const handleAttach = async (file: File) => {
    if (!agent) {
      notify("没有可用执行体：先让设备在线并授权「对话」");
      return;
    }
    if (attachments.length >= 5) {
      notify("一轮最多带 5 个附件");
      return;
    }
    setUploading(true);
    try {
      const uploaded = await uploadDriveFile(file);
      const imported = await importDriveFile(agent.project_id, uploaded.file.id);
      setAttachments((current) => [...current, { artifact_id: imported.artifact_id, name: imported.name }]);
      notify(`「${imported.name}」已加入本轮附件`);
    } catch (error) {
      notify(error instanceof Error ? error.message : "上传失败");
    } finally {
      setUploading(false);
    }
  };

  /** 谁批的：是自己就说"你"，别人显示平台给的成员 id（平台不存成员昵称快照，这里不编）。 */
  const deciderLabel = (item: MyAgentTurnApproval): string => {
    if (!item.decided_by) return "";
    if (account?.member?.id && item.decided_by === account.member.id) return `由你（${account.member.display_name}）`;
    return `由 ${item.decided_by}`;
  };

  /** 权限卡片上的决定：`once`/`always`/`reject` 三档原样交给平台（平台再回给执行体）。 */
  const handleApproval = async (item: MyAgentTurnApproval, decision: "once" | "always" | "reject") => {
    try {
      const updated = await decideMyAgentTurnApproval(item.turn_id, item.id, decision);
      setApprovalsByTurn((current) => ({
        ...current,
        [item.turn_id]: (current[item.turn_id] ?? []).map((one) => (one.id === updated.id ? updated : one)),
      }));
      notify(
        decision === "reject"
          ? "已拒绝：执行体不会做那件事"
          : decision === "always"
            ? "已允许（本次会话内都允许）"
            : "已批准这一次",
      );
    } catch (error) {
      notify(error instanceof Error ? error.message : "提交决定失败");
    }
  };

  /** 产出文件「下载」：带令牌取回内容再交给浏览器保存（不能用 <a href>，见 lib/api.ts 的说明）。 */
  const handleDownload = async (item: MyAgentTurnOutput) => {
    try {
      await downloadArtifactContent(item.artifact_id, item.name);
    } catch (error) {
      notify(error instanceof Error ? error.message : "下载失败");
    }
  };

  /** 产出文件「转入云盘」：把成果物**复制**一份进个人云盘（成果物本身留在项目里）。 */
  const handleToDrive = async (item: MyAgentTurnOutput) => {
    try {
      const entry = await copyArtifactToDrive(item.artifact_id);
      notify(`「${item.name}」已转入个人云盘（云盘里的名字：${entry.name}）`);
    } catch (error) {
      notify(error instanceof Error ? error.message : "转入云盘失败");
    }
  };

  const handleStop = async () => {
    if (!activeTurn) return;
    try {
      await stopMyAgentTurn(activeTurn.id);
      notify("已请求停止：平台会把本轮标记为已停止；执行体收到取消检查后会中止当前执行");
      if (selectedId) await loadTurns(selectedId);
    } catch (error) {
      notify(error instanceof Error ? error.message : "停止失败");
    }
  };

  const handleRoleChange = async (next: string) => {
    setRole(next);
    if (!selected) return; // 还没建会话：等发消息时一起带过去
    try {
      await updateMyAgentConversation(selected.id, { role: next });
      await loadConversations(selected.id);
      const suffix = roleExecutes(next) ? "（该角色可执行命令、会改动工作目录）" : "";
      notify(next ? `角色已切到「${roleLabel(next)}」${suffix}，下一轮生效` : "已回到默认角色（下一轮生效）");
    } catch (error) {
      notify(error instanceof Error ? error.message : "角色保存失败");
    }
  };

  /** 换模型：与换角色同样的口径——**存到会话上、下一轮生效**。
   *
   * 为什么必须存：轮次的模型是在建轮次时从会话抄下来的，只改本地 state 的话下一轮还是旧模型——
   * 用户会以为"我换了模型却没生效"（实测踩到：这条路径原本只在建会话时写模型，中途换是静默无效）。 */
  const handleModelChange = async (next: string) => {
    setModel(next);
    if (!selected) return;
    try {
      await updateMyAgentConversation(selected.id, { model: next });
      await loadConversations(selected.id);
      notify(`模型已切到「${next.split("/").pop() || next}」，下一轮生效`);
    } catch (error) {
      notify(error instanceof Error ? error.message : "模型保存失败");
    }
  };

  const handleVariantChange = async (next: Variant) => {
    setVariant(next);
    if (!selected) return;
    try {
      await updateMyAgentConversation(selected.id, { variant: next });
      await loadConversations(selected.id);
      notify(
        next
          ? `强度已切到「${VARIANT_LABEL[next]}」：下一轮通过 CLI --variant 生效，并如实降级为分段输出`
          : "已恢复默认强度：下一轮继续使用常驻服务真流式",
      );
    } catch (error) {
      notify(error instanceof Error ? error.message : "强度保存失败");
    }
  };

  const handleDelete = async (conversationId: string) => {
    try {
      await deleteMyAgentConversation(conversationId);
      if (selectedId === conversationId) {
        setSelectedId("");
        setTurns([]);
      }
      await loadConversations();
      notify("会话已删除");
    } catch {
      notify("删除失败");
    }
  };

  /* ---- 渲染辅助 -------------------------------------------------------- */

  const grouped = useMemo(() => {
    const dayOf = (value: string) => new Date(value).toDateString();
    const today = new Date().toDateString();
    const yesterday = new Date(Date.now() - 86400000).toDateString();
    const groups: { label: string; items: MyAgentConversation[] }[] = [
      { label: "今天", items: [] },
      { label: "昨天", items: [] },
      { label: "更早", items: [] },
    ];
    for (const conversation of conversations) {
      const day = dayOf(conversation.updated_at || conversation.created_at);
      if (day === today) groups[0].items.push(conversation);
      else if (day === yesterday) groups[1].items.push(conversation);
      else groups[2].items.push(conversation);
    }
    return groups.filter((group) => group.items.length > 0);
  }, [conversations]);

  const shortDate = (value: string) => {
    const date = new Date(value);
    return `${date.getMonth() + 1}/${date.getDate()}`;
  };

  /** 角色选项的显示名：说明的约定格式是「短名 · 一句话」（**分隔符是带空格的「 · 」**），
   * 取它前面的短名当标签，完整说明放 title。按单个「·」切会把「Word 论文」这类短名切坏（实测踩到）。
   * 万一说明没按约定写，也不要把英文 id 直接甩给用户——退而截取说明开头。 */
  const roleLabel = (name: string) => {
    const described = agent?.roles.find((item) => item.name === name);
    const description = described?.description || "";
    if (description.includes(" · ")) return description.split(" · ")[0].trim();
    if (description) return description.slice(0, 12);
    return name || "默认";
  };

  const roleDescription = (name: string) => agent?.roles.find((item) => item.name === name)?.description || "";

  /** 这个角色能不能动手（执行命令/改文件）——来自执行体探测到的角色文件工具开关。 */
  const roleExecutes = (name: string) => Boolean(agent?.roles.find((item) => item.name === name)?.executes);

  const sessionsContent = (
    <>
      <button
        className="ask-new-chat"
        data-testid="my-agent-new-chat"
        disabled={busy || !agents.length}
        onClick={() => {
          closeSessionDrawer();
          void handleNewChat();
        }}
      >
        <Plus size={15} /> 新对话
      </button>
      <div className="ask-sessions-scroll" data-testid="my-agent-conversations">
        {grouped.map((group) => (
          <div className="ask-session-group" key={group.label}>
            <div className="ask-session-group-label">{group.label}</div>
            {group.items.map((conversation) => (
              <div
                key={conversation.id}
                className={`ask-session-row${selectedId === conversation.id ? " is-active" : ""}`}
                onClick={() => {
                  setSelectedId(conversation.id);
                  closeSessionDrawer();
                }}
                data-testid={`my-agent-session-${conversation.id}`}
              >
                <div className="ask-session-main">
                  <strong>{conversation.title || "新对话"}</strong>
                  <small className="my-agent-session-meta">
                    {shortDate(conversation.updated_at || conversation.created_at)}
                    <span className="my-agent-session-badge">{conversation.turn_count} 轮</span>
                    {conversation.model ? conversation.model.split("/").pop() : ""}
                  </small>
                </div>
                <button
                  className="ask-session-delete"
                  aria-label={`删除 ${conversation.title || "会话"}`}
                  onClick={(event) => {
                    event.stopPropagation();
                    setDeleteTarget({ id: conversation.id, title: conversation.title || "会话" });
                  }}
                >
                  <Trash2 size={13} />
                </button>
              </div>
            ))}
          </div>
        ))}
        {!conversations.length ? <EmptyState>还没有对话：点上面「新对话」或直接在下面输入</EmptyState> : null}
      </div>
    </>
  );

  const detailTurn = activeTurn ?? lastTurn;
  const detailEvents = detailTurn ? eventsByTurn[detailTurn.id] ?? [] : [];

  /** 「对话 / 项目工作」不占页面顶端：放到输入框最下面的说明行里（用户指定的位置）。 */
  const modeSwitch = (
    <div className="my-agent-tabs my-agent-tabs-inline" role="tablist" aria-label="模式">
      <button
        type="button"
        role="tab"
        aria-selected={mode === "chat"}
        className={mode === "chat" ? "is-active" : ""}
        data-testid="my-agent-tab-chat"
        onClick={() => setMode("chat")}
      >
        <Bot size={13} /> 对话
      </button>
      <button
        type="button"
        role="tab"
        aria-selected={mode === "work"}
        className={mode === "work" ? "is-active" : ""}
        data-testid="my-agent-tab-work"
        onClick={() => setMode("work")}
      >
        <Wrench size={13} /> 项目工作
      </button>
    </div>
  );

  /** 执行中已经拿到的正文，两种口径拼在一起：
   *
   * - `agent.message`：**整段**（`opencode run` CLI 通道每完成一段发一条）；
   * - `delta`：**增量**（常驻 serve 通道，S-2；内核按节奏发，收尾会补发剩下的，不丢字）。
   *
   * 页面只负责"按到达顺序拼出来"；轮次结束后一律用权威的 `content` 覆盖（两者内容一致，后者是权威值）。
   * 刷新页面也能重建：这两种事件都在轮次事件表里，重新拉一次就等于重放。
   */
  const liveSegments = useMemo(() => answerSegments(activeEvents), [activeEvents]);
  /** 抽屉里的"过程事件"：增量与思考都不算过程事件（几百条会把抽屉刷屏），它们在别处显示。 */
  const processEvents = useMemo(
    () => detailEvents.filter((event) => event.event_type !== "delta" && event.event_type !== "thinking"),
    [detailEvents],
  );
  /** S-4：当前执行细节里的思考过程，与正文严格分开。 */
  const thinkingText = useMemo(() => thinkingFrom(detailEvents), [detailEvents]);
  const [eventsOpen, setEventsOpen] = useState(false);
  /** 这一轮有没有走增量通道——决定状态文案（**不假装**：CLI 通道就说"分段"）。 */
  const hasDeltas = useMemo(() => activeEvents.some((event) => event.event_type === "delta"), [activeEvents]);
  const liveTurnId = detailTurn?.id ?? "";
  /** 这一轮**实际**用的角色在角色文件上的定义（R-4）：说明 + 硬规则摘要 + 版本 + 是否与部署清单一致。 */
  const roleDefinition = useMemo(() => {
    const name = detailTurn?.role || role;
    if (!name) return null;
    return agent?.roles.find((item) => item.name === name) ?? null;
  }, [agent, detailTurn?.role, role]);
  const streamingNow = Boolean(activeTurn);

  return (
    <div className="page-content" id="my-agent" data-page="my-agent">
      {/* 标题只给读屏与导航；视觉上不再占一整行，模式切换已移到输入框下方 */}
      <h1 className="sr-only">我的智能体</h1>

      {mode === "work" ? (
        <section className="my-agent-work-view" data-testid="my-agent-work">
          <Panel title="项目工作（跑任务的那条线）" testId="my-agent-work-panel">
            <p className="hint">
              任务、派单、成果物与复核都在项目工作区里；这里只选项目并进去。对话与任务两套互不干扰：
              对话不会在任务板留痕，任务也不会挤进对话流。
            </p>
            <div className="grid" style={{ marginTop: 12 }}>
              {projects.map((item) => (
                <div className="list-row" key={item.id}>
                  <div className="list-row-main">
                    <strong>{item.name}</strong>
                    <span className="hint">{activeProject?.id === item.id ? "当前项目" : "点击进入"}</span>
                  </div>
                  <Link
                    className="button button-secondary"
                    href="/workspace"
                    onClick={() => selectProject(item.id)}
                    data-testid={`my-agent-project-${item.id}`}
                  >
                    进入工作区 →
                  </Link>
                </div>
              ))}
              {!projects.length ? <EmptyState>还没有项目：先去项目总览建一个</EmptyState> : null}
            </div>
          </Panel>
          <div className="my-agent-mode-footer my-agent-mode-footer-work">
            <span className="hint">项目工作会建任务、进入任务板，并保留成果物与复核记录。</span>
            {modeSwitch}
          </div>
        </section>
      ) : (
        <section className="my-agent-shell">
          <div className="my-agent-layout">
          {/* 左列：桌面会话列表；窄屏复用同一份内容到抽屉，不另造数据与动作。 */}
          <aside className="ask-sessions" aria-label="会话管理">
            {sessionsContent}
          </aside>

          {/* 中列：对话流 + 输入 */}
          <div className="my-agent-main">
            <Panel testId="my-agent-chat-panel">
              <div className="my-agent-toolbar">
                <button
                  ref={sessionDrawerTrigger}
                  type="button"
                  className="button button-secondary my-agent-session-toggle"
                  data-testid="my-agent-session-toggle"
                  aria-expanded={sessionDrawerOpen}
                  onClick={() => setSessionDrawerOpen(true)}
                >
                  <History size={14} /> 会话
                </button>
                <span className="my-agent-title">{selected?.title || "新对话"}</span>
                <label className="my-agent-picker">
                  <Cpu size={13} />
                  <select
                    value={agentIndex}
                    onChange={(event) => {
                      const next = Number(event.target.value);
                      setAgentIndex(next);
                      const nextAgent = agents[next];
                      setModel(nextAgent?.default_model || nextAgent?.models[0] || "");
                    }}
                    data-testid="my-agent-device"
                  >
                    {agents.map((item, index) => (
                      <option key={item.device_id} value={index}>
                        {item.device_name || item.device_id} · {item.online ? "在线" : "离线"}
                      </option>
                    ))}
                    {!agents.length ? <option value={0}>没有可用执行体</option> : null}
                  </select>
                </label>
                <button
                  type="button"
                  className="button button-secondary"
                  data-testid="my-agent-promote"
                  title="转入项目生产：把这次对话的产出建成正式任务（可带交接说明）"
                  disabled={!selected}
                  onClick={() => setPromoteOpen(true)}
                >
                  <FolderInput size={14} /> 转入项目生产
                </button>
                {/* 模型与角色挪到输入框下面的控件行里（仿 zcode：控件事跟输入区在一起），
                    这里只留"这一轮跑在哪台执行体上"和如实标注的动手权限说明。 */}
                {/* 选中一个能动手的角色时，如实标一句：它会改动执行体的工作目录（不是警告，是事实说明） */}
                {role && roleExecutes(role) ? (
                  <span className="my-agent-role-note" title="该角色可执行命令并写文件（来自角色定义里的工具开关）">
                    可执行命令
                  </span>
                ) : null}
                <span className="my-agent-meta-line">{agent ? `${agent.executor || "执行体"} · ${agent.project_name}` : ""}</span>
                <button
                  ref={detailDrawerTrigger}
                  type="button"
                  className="button button-secondary my-agent-drawer-toggle"
                  data-testid="my-agent-detail-toggle"
                  aria-expanded={detailOpen}
                  onClick={() => (detailOpen ? closeDetailDrawer() : setDetailOpen(true))}
                >
                  <Activity size={14} /> 执行细节
                  {activeTurn ? <span className="my-agent-live-dot" aria-hidden /> : null}
                </button>
              </div>

              <div
                className="ask-messages"
                ref={messageScroller}
                data-testid="my-agent-stream"
                onScroll={(event) => {
                  const node = event.currentTarget;
                  stickToBottom.current = node.scrollHeight - node.scrollTop - node.clientHeight < 80;
                  if (stickToBottom.current) setNewReply(false);
                }}
              >
                {turns.map((turn) => {
                  const turnEvents = eventsByTurn[turn.id] ?? [];
                  const turnApprovals = approvalsByTurn[turn.id] ?? [];
                  const turnThinking = thinkingFrom(turnEvents);
                  return (
                  <div className="my-agent-turn" key={turn.id}>
                    <div className="ask-row is-user">
                      <div className="ask-bubble">
                        {turn.artifacts?.length ? (
                          <div className="my-agent-attach-list">
                            {turn.artifacts.map((item) => (
                              <span className="my-agent-attach-chip" key={item.artifact_id}>
                                <Paperclip size={11} /> {item.name || "附件"}
                              </span>
                            ))}
                          </div>
                        ) : null}
                        {turn.prompt}
                      </div>
                    </div>
                    <div className="ask-row">
                      <div className="ask-answer">
                        <div className="my-agent-answer-head">
                          <Bot size={12} /> {turn.role ? roleLabel(turn.role) : "智能体"}
                          {turn.model ? ` · ${turn.model.split("/").pop()}` : ""}
                          {turn.variant ? ` · ${VARIANT_LABEL[turn.variant as Variant] || turn.variant}` : ""}
                        </div>
                        <ThinkingDisclosure
                          turnId={turn.id}
                          text={turnThinking}
                          active={turn.id === activeTurn?.id}
                        />
                        {turn.content ? (
                          <AgentResponse
                            text={turn.content}
                            disabled={Boolean(activeTurn) || busy}
                            onSubmitChoice={async (message) => {
                              await sendContent(message);
                            }}
                          />
                        ) : turn.id === liveTurnId && liveSegments.length ? (
                          <div className="my-agent-live">
                            <div className="my-agent-live-text">
                              {liveSegments.map((segment, segmentIndex) => (
                                <Markdown key={segmentIndex} text={segment} />
                              ))}
                            </div>
                            <div className="my-agent-status">
                              <RefreshCcw size={12} className="spin" />{" "}
                              {turnApprovals.some((item) => item.status === "PENDING")
                                ? "等待授权（执行体已停下等你的决定）"
                                : hasDeltas
                                  ? "生成中（真实增量）"
                                  : turnThinking
                                    ? "思考中（还没开始写正文）"
                                    : turn.variant
                                      ? `执行中（${VARIANT_LABEL[turn.variant as Variant] || turn.variant}强度，CLI 分段输出）`
                                      : "执行中（分段显示，不是逐字流）"}
                            </div>
                          </div>
                        ) : (
                          <span className="my-agent-status">
                            {ACTIVE_STATUSES.has(turn.status) ? <RefreshCcw size={13} className="spin" /> : null}
                            {STATUS_LABEL[turn.status] ?? turn.status}
                            {stopReasonNote(turn) ? `（${stopReasonNote(turn)}）` : ""}
                            {turn.error ? `：${turn.error}` : ""}
                          </span>
                        )}
                        {turn.content && turn.status === "FAILED" ? (
                          <span className="my-agent-status">（失败：{turn.error}）</span>
                        ) : null}
                        {/* 被拒绝之后这一轮可能**没有文字回复**（执行体停在工具调用那一步就收尾了）：
                            如实说明，别让人对着空气找答案。 */}
                        {turn.id === liveTurnId &&
                        !turn.content &&
                        turn.status === "DONE" &&
                        turnApprovals.some((item) => item.status === "DENIED" || item.status === "EXPIRED") ? (
                          <span className="my-agent-status">
                            这一轮没有文字回复：执行体想做的事没被批准，它在这一步就结束了。
                          </span>
                        ) : null}
                        {turnApprovals.length ? (
                          <div className="my-agent-approvals" data-testid={`my-agent-approvals-${turn.id}`}>
                            {turnApprovals.map((item) => (
                              <div
                                className={`my-agent-approval is-${item.status.toLowerCase()}`}
                                key={item.id}
                                data-testid={`my-agent-approval-${item.id}`}
                              >
                                <ShieldAlert size={16} />
                                <div className="my-agent-approval-body">
                                  <strong>
                                    {item.status === "PENDING"
                                      ? "执行体要动工作区外的文件，需要你批准"
                                      : item.status === "APPROVED"
                                        ? "已批准"
                                        : item.status === "DENIED"
                                          ? "已拒绝（执行体没有做那件事）"
                                          : "没人批准，已按不执行处理"}
                                  </strong>
                                  <span className="my-agent-approval-summary">{item.summary || item.permission}</span>
                                  <span className="my-agent-approval-meta">
                                    {item.permission}
                                    {item.patterns.length ? ` · ${item.patterns.join("、")}` : ""}
                                    {deciderLabel(item) ? ` · ${deciderLabel(item)}` : ""}
                                    {item.status !== "PENDING" && item.decision ? ` · ${item.decision}` : ""}
                                  </span>
                                  {item.status === "PENDING" ? (
                                    <div className="my-agent-approval-actions">
                                      <button
                                        type="button"
                                        className="button button-primary"
                                        data-testid={`my-agent-approval-once-${item.id}`}
                                        onClick={() => void handleApproval(item, "once")}
                                      >
                                        批准一次
                                      </button>
                                      <button
                                        type="button"
                                        className="button button-secondary"
                                        data-testid={`my-agent-approval-always-${item.id}`}
                                        onClick={() => void handleApproval(item, "always")}
                                      >
                                        本次都允许
                                      </button>
                                      <button
                                        type="button"
                                        className="button button-secondary"
                                        data-testid={`my-agent-approval-reject-${item.id}`}
                                        onClick={() => void handleApproval(item, "reject")}
                                      >
                                        拒绝
                                      </button>
                                    </div>
                                  ) : null}
                                </div>
                              </div>
                            ))}
                          </div>
                        ) : null}
                        {turn.usage && Object.keys(turn.usage).length ? (
                          <div className="my-agent-usage">
                            {String((turn.usage as { total_tokens?: number }).total_tokens ?? 0)} tokens ·{" "}
                            {String((turn.usage as { source?: string }).source ?? "")}
                          </div>
                        ) : null}
                        {turn.outputs?.length ? (
                          <div className="my-agent-outputs" data-testid={`my-agent-outputs-${turn.id}`}>
                            <div className="my-agent-outputs-label">这一轮产出的文件（成果物，待审）</div>
                            {turn.outputs.map((item) => (
                              <div className="my-agent-output" key={item.artifact_id}>
                                <FileText size={15} />
                                <span className="my-agent-output-name" title={item.relative_path || item.name}>
                                  {item.name}
                                </span>
                                <span className="my-agent-output-size">{formatBytes(item.size_bytes)}</span>
                                {/* 走 `downloadArtifactContent` 而不是 <a href>：普通跳转不带 Authorization，
                                    强制鉴权下会 401（这条坑在 lib/api.ts 里有现成的说明）。 */}
                                <button
                                  type="button"
                                  className="button button-secondary my-agent-output-action"
                                  data-testid={`my-agent-output-download-${item.artifact_id}`}
                                  onClick={() => void handleDownload(item)}
                                >
                                  下载
                                </button>
                                <button
                                  type="button"
                                  className="button button-secondary my-agent-output-action"
                                  data-testid={`my-agent-output-drive-${item.artifact_id}`}
                                  onClick={() => void handleToDrive(item)}
                                >
                                  转入云盘
                                </button>
                              </div>
                            ))}
                          </div>
                        ) : null}
                      </div>
                    </div>
                  </div>
                  );
                })}
                {!turns.length && !busy ? (
                  <EmptyState>
                    <MessageSquare size={16} /> 说点什么开始——没有选中会话时会自动新建一个
                  </EmptyState>
                ) : null}
                <div ref={streamEnd} />
              </div>

              {newReply ? (
                <button
                  type="button"
                  className="my-agent-new-reply"
                  data-testid="my-agent-new-reply"
                  onClick={() => {
                    stickToBottom.current = true;
                    setNewReply(false);
                    streamEnd.current?.scrollIntoView({ behavior: "smooth", block: "end" });
                  }}
                >
                  <ArrowDown size={14} /> 有新回复 · 回到底部
                </button>
              ) : null}

              {attachments.length ? (
                <div className="my-agent-attach-list">
                  {attachments.map((item) => (
                    <span className="my-agent-attach-chip" key={item.artifact_id}>
                      <Paperclip size={11} /> {item.name}
                      <button
                        type="button"
                        aria-label={`移除 ${item.name}`}
                        onClick={() => setAttachments((current) => current.filter((one) => one.artifact_id !== item.artifact_id))}
                      >
                        <X size={11} />
                      </button>
                    </span>
                  ))}
                </div>
              ) : null}
              <div className="composer-card" ref={composerRef}>
                <input
                  ref={fileInput}
                  type="file"
                  hidden
                  data-testid="my-agent-file"
                  onChange={(event) => {
                    const file = event.target.files?.[0];
                    event.target.value = "";
                    if (file) void handleAttach(file);
                  }}
                />
                {/* 「+」展开的添加面板：仿 zcode 的添加区——图标 + 名称 + 一行说明，点一下即生效 */}
                {addOpen ? (
                  <div className="composer-add" data-testid="my-agent-add-panel">
                    <div className="composer-add-title">添加</div>
                    <div className="composer-add-label">附件</div>
                    <button
                      type="button"
                      className="composer-add-row"
                      data-testid="my-agent-attach"
                      disabled={uploading || Boolean(activeTurn)}
                      onClick={() => {
                        setAddOpen(false);
                        fileInput.current?.click();
                      }}
                    >
                      <Paperclip size={17} />
                      <span className="composer-add-copy">
                        <strong>{uploading ? "上传中…" : "添加附件"}</strong>
                        <small>先传进个人云盘、再登记成本项目成果物；执行体会把它下到工作目录的 inputs/ 下（一轮最多 5 个）</small>
                      </span>
                    </button>
                    <div className="composer-add-label">角色（改完对下一轮生效）</div>
                    <button
                      type="button"
                      className={`composer-add-row${role ? "" : " is-active"}`}
                      data-testid="my-agent-add-role-default"
                      onClick={() => {
                        setAddOpen(false);
                        void handleRoleChange("");
                      }}
                    >
                      <Bot size={17} />
                      <span className="composer-add-copy">
                        <strong>默认（无角色）</strong>
                        <small>通用对话，不加载任何角色提示词</small>
                      </span>
                    </button>
                    {(agent?.roles ?? []).map((item) => (
                      <button
                        key={item.name}
                        type="button"
                        className={`composer-add-row${role === item.name ? " is-active" : ""}`}
                        data-testid={`my-agent-add-role-${item.name}`}
                        onClick={() => {
                          setAddOpen(false);
                          void handleRoleChange(item.name);
                        }}
                      >
                        {item.executes ? <Wrench size={17} /> : <Sparkles size={17} />}
                        <span className="composer-add-copy">
                          <strong>
                            {roleLabel(item.name)}
                            {item.executes ? "（可执行）" : ""}
                          </strong>
                          <small>{item.description || "（角色文件里没写说明）"}</small>
                        </span>
                      </button>
                    ))}
                    <div className="composer-add-label">执行体</div>
                    <button
                      type="button"
                      className="composer-add-row"
                      data-testid="my-agent-add-agent"
                      onClick={() => {
                        setAddOpen(false);
                        setDetailOpen(true);
                      }}
                    >
                      <Cpu size={17} />
                      <span className="composer-add-copy">
                        <strong>
                          {agent ? `${agent.device_name || agent.device_id} · ${agent.online ? "在线" : "离线"}` : "没有可用执行体"}
                        </strong>
                        <small>
                          {agent
                            ? `${agent.executor || "执行体"} · ${agent.project_name}；点这里看本轮执行细节`
                            : "先让设备在线并授权「对话」"}
                        </small>
                      </span>
                    </button>
                    {!agents.length ? (
                      <Link className="composer-add-row" href="/devices" data-testid="my-agent-goto-devices">
                        <Server size={17} />
                        <span className="composer-add-copy">
                          <strong>接入执行体</strong>
                          <small>没有执行体就无法对话：到「设备与接入」配对设备或下载桌面端，接好后回来继续</small>
                        </span>
                      </Link>
                    ) : null}
                    <p className="composer-add-note">历史轮次记着它当时用的角色与模型；切换只影响下一轮。</p>
                  </div>
                ) : null}

                <textarea
                  className="composer-input"
                  ref={inputBox}
                  value={input}
                  rows={1}
                  placeholder="给执行体说一件事，Enter 发送（Shift+Enter 换行）"
                  data-testid="my-agent-input"
                  onChange={(event) => setInput(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && !event.shiftKey) {
                      event.preventDefault();
                      void handleSend();
                    }
                  }}
                />

                <div className="composer-bar">
                  <button
                    type="button"
                    className={`composer-icon-button${addOpen ? " is-open" : ""}`}
                    aria-label="添加"
                    aria-expanded={addOpen}
                    title="添加：附件 / 角色 / 执行体"
                    data-testid="my-agent-add"
                    onClick={() => setAddOpen((open) => !open)}
                  >
                    <Plus size={18} />
                  </button>
                  <label className="composer-picker" title={roleDescription(role)}>
                    <Sparkles size={14} />
                    <select
                      value={role}
                      onChange={(event) => void handleRoleChange(event.target.value)}
                      data-testid="my-agent-role"
                      title={agent?.roles.length ? undefined : "这台执行体上没有安装角色（默认即通用对话）"}
                    >
                      <option value="">默认（无角色）</option>
                      {(agent?.roles ?? []).map((item) => (
                        <option key={item.name} value={item.name} title={item.description}>
                          {roleLabel(item.name)}
                          {item.executes ? "（可执行）" : ""}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label
                    className="composer-picker"
                    title={variant ? "非默认强度通过 CLI --variant 生效；当前会从真流式降级为分段输出" : "默认强度使用常驻服务真流式"}
                  >
                    <Gauge size={14} />
                    <select
                      value={variant}
                      onChange={(event) => void handleVariantChange(event.target.value as Variant)}
                      data-testid="my-agent-variant"
                      aria-label="推理强度"
                    >
                      {(Object.keys(VARIANT_LABEL) as Variant[]).map((item) => (
                        <option key={item || "default"} value={item}>{VARIANT_LABEL[item]}</option>
                      ))}
                    </select>
                  </label>
                  <span className="composer-count" title="这一轮带的附件数">
                    {attachments.length ? `附件 ${attachments.length}` : "无附件"}
                  </span>
                  <label className="composer-picker">
                    <Bot size={14} />
                    <select
                      value={model}
                      onChange={(event) => void handleModelChange(event.target.value)}
                      disabled={!agent?.models.length}
                      data-testid="my-agent-model"
                    >
                      {(agent?.models.length ? agent.models : [""]).map((item) => (
                        <option key={item} value={item}>
                          {item || "（执行体默认）"}
                        </option>
                      ))}
                    </select>
                  </label>
                  {activeTurn ? (
                    <button
                      className="composer-send is-stop"
                      data-testid="my-agent-stop"
                      title="停止这一轮"
                      aria-label="停止"
                      onClick={() => void handleStop()}
                    >
                      <Square size={15} />
                    </button>
                  ) : (
                    <button
                      className="composer-send"
                      data-testid="my-agent-send"
                      title="发送"
                      aria-label="发送"
                      disabled={busy || !input.trim()}
                      onClick={() => void handleSend()}
                    >
                      {busy ? <RefreshCcw size={16} className="spin" /> : <ArrowUp size={17} />}
                    </button>
                  )}
                </div>
              </div>
              <div className="my-agent-mode-footer">
                <span className="hint">
                  {variant
                    ? `${VARIANT_LABEL[variant]}强度会真实传给 CLI --variant；当前通道如实显示为分段输出。`
                    : "默认强度使用常驻服务真流式；对话不建任务，附件落到工作目录 inputs/。"}
                </span>
                {modeSwitch}
              </div>
            </Panel>
          </div>

          {/* 右列：本轮执行细节 */}
          <aside
            ref={detailDrawerPanel}
            className={`my-agent-drawer${detailOpen ? " is-open" : ""}`}
            aria-label="执行细节"
            aria-hidden={!detailOpen}
            data-testid="my-agent-drawer"
          >
            <div className="my-agent-drawer-head">
              <strong>本轮执行</strong>
              <button
                ref={detailDrawerClose}
                type="button"
                className="icon-button"
                aria-label="收起执行细节"
                data-testid="my-agent-drawer-close"
                onClick={closeDetailDrawer}
              >
                <X size={14} />
              </button>
            </div>
            <Panel testId="my-agent-detail-panel">
              {detailTurn ? (
                <>
                  <div className="my-agent-detail-head">
                    <span className={`my-agent-status-pill${activeTurn ? " is-live" : ""}`}>
                      {STATUS_LABEL[detailTurn.status] ?? detailTurn.status}
                    </span>
                    <span className="my-agent-round">第 {detailTurn.seq} 轮</span>
                  </div>
                  <dl className="my-agent-meta">
                    <dt>模型</dt>
                    <dd>{detailTurn.model || "（执行体默认）"}</dd>
                    <dt>角色</dt>
                    <dd title={detailTurn.role ? roleDescription(detailTurn.role) : ""}>
                      {detailTurn.role
                        ? `${roleLabel(detailTurn.role)}（${detailTurn.role}${roleExecutes(detailTurn.role) ? " · 可执行命令" : " · 只读"}）`
                        : "默认（无角色）"}
                    </dd>
                    <dt>强度</dt>
                    <dd>{detailTurn.variant ? `${VARIANT_LABEL[detailTurn.variant as Variant] || detailTurn.variant}（CLI --variant）` : "默认（serve 真流式）"}</dd>
                    <dt>会话句柄</dt>
                    <dd title={detailTurn.session_key ?? ""}>{detailTurn.session_key ? `${detailTurn.session_key.slice(0, 10)}…` : "—"}</dd>
                    <dt>用量</dt>
                    <dd>
                      {detailTurn.usage && Object.keys(detailTurn.usage).length
                        ? `${String((detailTurn.usage as { total_tokens?: number }).total_tokens ?? 0)} tokens（${String(
                            (detailTurn.usage as { source?: string }).source ?? "",
                          )}）`
                        : "—"}
                    </dd>
                  </dl>
                  {roleDefinition ? (
                    <div className="my-agent-role-card" data-testid="my-agent-role-card">
                      <div className="my-agent-role-card-head">
                        <strong>{roleLabel(roleDefinition.name)}</strong>
                        <span className="my-agent-role-card-version" title={`文件 sha256：${roleDefinition.sha256}；${roleDefinition.bytes} 字节`}>
                          {roleDefinition.sha256 ? `定义版本 ${roleDefinition.sha256}` : "无定义文件"}
                        </span>
                      </div>
                      {roleDefinition.description ? <p className="my-agent-role-card-desc">{roleDefinition.description}</p> : null}
                      {roleDefinition.rules.length ? (
                        <ul className="my-agent-role-card-rules" data-testid="my-agent-role-rules">
                          {roleDefinition.rules.map((rule, index) => (
                            <li key={index}>{rule}</li>
                          ))}
                        </ul>
                      ) : null}
                      {roleDefinition.drifted ? (
                        <p className="my-agent-role-card-drift" data-testid="my-agent-role-drift">
                          ⚠ 这份角色定义与部署清单不一致（可能是执行体上被手工改过）：页面显示的说明与实际跑的不保证一致，
                          请重跑部署脚本第 6 步把它盖回仓库里那版。
                        </p>
                      ) : roleDefinition.installed_at ? (
                        <p className="my-agent-role-card-note">与部署清单一致 · 装于 {formatTime(roleDefinition.installed_at)}</p>
                      ) : null}
                    </div>
                  ) : null}
                  <div className="my-agent-events" data-testid="my-agent-events">
                    {processEvents.length ? (
                      <button
                        type="button"
                        className="my-agent-process-toggle"
                        aria-expanded={eventsOpen}
                        onClick={() => setEventsOpen((open) => !open)}
                      >
                        <Activity size={13} /> 过程事件 · {processEvents.length} 条
                        <span>{eventsOpen ? "收起" : "展开"}</span>
                      </button>
                    ) : null}
                    {(eventsOpen ? processEvents : processEvents.slice(-3)).map((event) => {
                      const Icon = EVENT_ICON[event.event_type] ?? Activity;
                      const detail = String(
                        event.payload.text ?? event.payload.tool ?? event.payload.path ?? event.payload.executor ?? "",
                      );
                      return (
                        <div className="my-agent-event" key={event.id}>
                          <code>#{event.sequence}</code>
                          <span className="my-agent-event-icon">
                            <Icon size={12} />
                          </span>
                          <span className="my-agent-event-kind">{EVENT_LABEL[event.event_type] ?? event.event_type}</span>
                          {detail ? <span className="my-agent-event-text">{detail}</span> : null}
                        </div>
                      );
                    })}
                    {!processEvents.length ? <div className="hint">还没有过程事件（执行体一开始跑就会出现）</div> : null}
                    {!eventsOpen && processEvents.length > 3 ? (
                      <div className="hint">已显示最近 3 条，其余 {processEvents.length - 3} 条已折叠</div>
                    ) : null}
                  </div>
                </>
              ) : (
                <EmptyState>发一条消息，这里会显示执行过程</EmptyState>
              )}
            </Panel>
          </aside>
          </div>
          {detailOpen ? <div className="my-agent-backdrop" onClick={closeDetailDrawer} aria-hidden /> : null}
          {sessionDrawerOpen ? (
            <>
              <div className="my-agent-session-backdrop" onClick={closeSessionDrawer} aria-hidden />
              <aside
                ref={sessionDrawerPanel}
                className="my-agent-session-drawer"
                role="dialog"
                aria-modal="true"
                aria-label="历史会话"
                data-testid="my-agent-session-drawer"
              >
                <div className="my-agent-drawer-head">
                  <strong>历史会话</strong>
                  <button
                    ref={sessionDrawerClose}
                    type="button"
                    className="icon-button"
                    aria-label="关闭会话列表"
                    onClick={closeSessionDrawer}
                  >
                    <X size={16} />
                  </button>
                </div>
                {sessionsContent}
              </aside>
            </>
          ) : null}
        </section>
      )}

      {deleteTarget ? (
        <ConfirmDialog
          title="删除对话？"
          description={
            <>
              <strong>{deleteTarget.title}</strong>
              <div style={{ marginTop: 6 }}>对话及其轮次记录会被删除，无法恢复。</div>
            </>
          }
          confirmLabel="删除对话"
          tone="danger"
          testId="my-agent-delete-confirm"
          onCancel={() => setDeleteTarget(null)}
          onConfirm={() => {
            const target = deleteTarget;
            setDeleteTarget(null);
            void handleDelete(target.id);
          }}
        />
      ) : null}

      {promoteOpen && selected ? (
        <PromoteDialog
          conversationId={selected.id}
          conversationTitle={selected.title}
          agents={agents}
          turns={turns}
          onClose={() => setPromoteOpen(false)}
          onError={(message) => notify(message)}
        />
      ) : null}
    </div>
  );
}