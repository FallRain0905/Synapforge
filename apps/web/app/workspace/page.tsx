"use client";

import Link from "next/link";
import { FormEvent, ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  ArrowRight,
  AtSign,
  Bot,
  Box,
  ClipboardCheck,
  Cpu,
  Info,
  Loader2,
  MessagesSquare,
  Paperclip,
  RefreshCcw,
  Send,
  Target,
  Users,
} from "lucide-react";
import { ArtifactDrawer } from "../../components/artifact-drawer";
import { PageHeading, formatTime } from "../../components/shell";
import { ConfirmDialog, EmptyState, LoadingSkeleton, Metric, Modal, Panel, Progress, StatusPill } from "../../components/ui";
import {
  ProjectDeliverables,
  ProjectMessage,
  ProjectWorkspaceOverview,
  Task,
  TaskCandidates,
  TaskFlags,
  TaskMode,
  assignTask,
  bulkAssignTasks,
  createTask,
  errorMessage,
  getProjectDeliverables,
  getProjectMessages,
  getProjectTaskFlags,
  getProjectWorkspace,
  getTaskCandidates,
  importDriveFile,
  postProjectMessage,
  updateProjectSettings,
  uploadDriveFile,
} from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { useWorkspace } from "../../lib/workspace";

const TABS = [
  { id: "chat", label: "聊天", icon: MessagesSquare, hint: "人和 Agent 的主反馈流" },
  { id: "tasks", label: "任务", icon: ClipboardCheck, hint: "谁在做什么、还差什么" },
  { id: "artifacts", label: "成果空间", icon: Box, hint: "成果物、交接与文档入口" },
  { id: "overview", label: "概览", icon: Info, hint: "立项目标、人数与推进模式" },
] as const;

type TabId = (typeof TABS)[number]["id"];

const ROLE_LABEL: Record<string, string> = {
  owner: "负责人",
  project_lead: "队长",
  contributor: "成员",
  reviewer: "复核人",
  observer: "观察者",
};

const TASK_MODE_LABEL: Record<TaskMode, { label: string; hint: string }> = {
  manual: { label: "队长派单", hint: "任务由队长在任务板分配给成员；未指派的任务先到先得" },
  hybrid: { label: "派单 + 认领", hint: "队长派单之外，成员也可以自己从任务板认领" },
  auto: {
    label: "模板全自动",
    hint: "调度器按能力匹配自动派单（依赖已满足的任务派给有合格在线执行体的成员），拿不准或没人能跑时会在群聊里说，并停下等你处理",
  },
};

const LAYER_LABEL: Record<string, string> = { draft: "草稿", submitted: "已提交", approved: "已批准" };

const HANDOFF_STATUS_LABEL: Record<string, string> = {
  PENDING: "待接收",
  ACCEPTED: "已接收",
  REJECTED: "已退回",
  COMPLETED: "已完成",
};

/** 任务板筛选用的"未结束"状态集合（与后端 open_items 口径一致）。 */
const OPEN_TASK_STATES = ["READY", "CLAIMED", "RUNNING", "BLOCKED", "NEEDS_REVISION", "WAITING_REVIEW"];

const STATUS_LABEL: Record<string, string> = {
  READY: "待执行",
  CLAIMED: "已领取",
  RUNNING: "执行中",
  BLOCKED: "受阻",
  NEEDS_REVISION: "需修改",
  WAITING_REVIEW: "待复核",
  APPROVED: "已通过",
  DONE: "已完成",
};

/**
 * 项目工作区（W-1）——项目的主工作区域。
 *
 * 中间是聊天流：人类消息走 REST 落库，Agent 的动作由服务端从事件派生为卡片
 * （Agent 协议零改动）；右侧常驻成员概览（人类与 Agent 的在线/在跑什么），
 * 下方是成果空间入口。四个 Tab 把日常操作收在一页里，深度功能页保持原样，
 * 只是不再抢占主入口（导航收敛在 W-4）。
 */
export default function WorkspacePage() {
  const { ready, authenticated } = useAuth();
  const { projectId, project, projects, dashboard, selectProject, notify, connected, subscribeMessages, refresh } =
    useWorkspace();
  const [tab, setTab] = useState<TabId>("chat");
  const [overview, setOverview] = useState<ProjectWorkspaceOverview | null>(null);
  const [messages, setMessages] = useState<ProjectMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [hasMore, setHasMore] = useState(true);
  const [error, setError] = useState("");
  const [modeBusy, setModeBusy] = useState(false);
  // W-2：任务板（派单/认领/批量）与成果空间聚合
  const [filters, setFilters] = useState({ status: "open", assignee: "all" });
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkTarget, setBulkTarget] = useState("");
  const [bulkBusy, setBulkBusy] = useState(false);
  const [busyTask, setBusyTask] = useState("");
  const [tasksLoading, setTasksLoading] = useState(false);
  // AIP-1b：逐条任务拉"候选执行体"（与 auto 调度器同一套排序，推荐即派单结果）
  const [candidates, setCandidates] = useState<Record<string, TaskCandidates | undefined>>({});
  // 桌面端/浏览器通用的拖拽上传：拖文件进聊天区即走既有的"云盘 → 成果物 → 发消息"链路
  const [dropActive, setDropActive] = useState(false);
  // 任务行角标（COST-1）：证据缺口 / 用量超预算
  const [taskFlags, setTaskFlags] = useState<Record<string, TaskFlags>>({});
  const [candidateBusy, setCandidateBusy] = useState("");
  const [deliverables, setDeliverables] = useState<ProjectDeliverables | null>(null);
  const [deliverablesLoading, setDeliverablesLoading] = useState(false);
  const [deliverablesError, setDeliverablesError] = useState("");
  // ④ 自定义任务：不依赖模板包，队长/成员直接建一条任务
  const [createOpen, setCreateOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [taskForm, setTaskForm] = useState({
    title: "",
    description: "",
    stage: "modeling",
    priority: "medium",
    assignee_member_id: "",
    deadline: "",
  });
  // 输入框增强（W-6）：附件上传与 @成员 / /Agent 提及
  const [uploading, setUploading] = useState(false);
  // 新用户引导：关掉一次就不再出现（localStorage 记住）
  const [guideOpen, setGuideOpen] = useState(false);
  // W-10：成果物就地查看（右侧抽屉）与聊天区 /task 快捷建任务
  const [drawerArtifact, setDrawerArtifact] = useState<{ id: string; name: string; artifact_type?: string; version?: number } | null>(null);
  const [creatingFromChat, setCreatingFromChat] = useState(false);
  useEffect(() => {
    if (typeof window === "undefined") return;
    setGuideOpen(!window.localStorage.getItem("map.guide.dismissed.v1"));
  }, []);
  const demoProject = projects.find((item) => item.name.startsWith("演示 ·"));
  const [mention, setMention] = useState<{ kind: "member" | "agent"; query: string; start: number; end: number; active: number } | null>(null);
  const fileInput = useRef<HTMLInputElement | null>(null);
  const composer = useRef<HTMLTextAreaElement | null>(null);
  const scroller = useRef<HTMLDivElement | null>(null);
  const stickToBottom = useRef(true);

  /* `?project=` 直链：从项目总览或通知点进来时落到指定项目。
     读 window.location.search 而不是 useSearchParams——保持静态预渲染不被打断（沿用时间线页的做法）。 */
  useEffect(() => {
    if (!ready || !authenticated || typeof window === "undefined") return;
    const wanted = new URLSearchParams(window.location.search).get("project");
    if (wanted && projects.some((item) => item.id === wanted) && wanted !== projectId) selectProject(wanted);
  }, [ready, authenticated, projects, projectId, selectProject]);

  const load = useCallback(async () => {
    if (!projectId) {
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const [nextOverview, history] = await Promise.all([
        getProjectWorkspace(projectId),
        getProjectMessages(projectId, { limit: 80 }),
      ]);
      setOverview(nextOverview);
      setMessages(history);
      setHasMore(history.length >= 80);
      setError("");
    } catch (failure) {
      setError(errorMessage(failure, "工作区数据读取失败"));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    if (!ready || !authenticated) return;
    void load();
  }, [ready, authenticated, load]);

  // WS 推送：新消息按 seq 去重后追加（连上时的历史帧与之后的增量帧都走这条路）
  useEffect(() => {
    if (!ready || !authenticated) return;
    return subscribeMessages((incoming) => {
      setMessages((current) => {
        const seen = new Set(current.map((message) => message.seq));
        const fresh = incoming.filter((message) => !seen.has(message.seq));
        if (!fresh.length) return current;
        return [...current, ...fresh].sort((a, b) => a.seq - b.seq);
      });
    });
  }, [ready, authenticated, subscribeMessages]);

  // 新消息贴底滚动；用户手动往上翻历史时不打扰
  useEffect(() => {
    const node = scroller.current;
    if (!node || !stickToBottom.current) return;
    node.scrollTop = node.scrollHeight;
  }, [messages, tab, loading]);

  const loadOlder = useCallback(async () => {
    if (!projectId || loadingMore || !messages.length) return;
    setLoadingMore(true);
    try {
      const older = await getProjectMessages(projectId, { before: messages[0].seq, limit: 60 });
      if (!older.length) setHasMore(false);
      setMessages((current) => {
        const seen = new Set(current.map((message) => message.seq));
        return [...older.filter((message) => !seen.has(message.seq)), ...current].sort((a, b) => a.seq - b.seq);
      });
    } catch (failure) {
      notify(errorMessage(failure, "更早的记录读取失败"));
    } finally {
      setLoadingMore(false);
    }
  }, [projectId, loadingMore, messages, notify]);

  // 聊天区命令（/ 开头）：/task 直接建任务，不用切 Tab
  const CHAT_COMMANDS = useMemo(
    () => [{ label: "task", hint: "直接新建任务：/task 任务标题", value: "/task" }],
    [],
  );

  /** 提及候选：@ 找成员、/ 找 Agent（先列命令）；名单来自工作区概览（已加载）。 */
  const mentionCandidates = useMemo(() => {
    if (!mention) return [] as { label: string; hint: string; value: string }[];
    const query = mention.query.toLowerCase();
    if (mention.kind === "agent") {
      const commands = CHAT_COMMANDS.filter((command) => !query || command.label.includes(query));
      const agents = (overview?.agents ?? [])
        .filter((agent) => !query || agent.display_name.toLowerCase().includes(query))
        .slice(0, 5)
        .map((agent) => ({
          label: agent.display_name,
          hint: `${agent.owner_name} · ${agent.connected ? "在线" : "离线"}${agent.current_task_title ? ` · 正在跑「${agent.current_task_title}」` : ""}`,
          value: `/${agent.display_name}`,
        }));
      return [...commands, ...agents];
    }
    if (mention.kind === "member") {
      return (overview?.members ?? [])
        .filter((member) => !query || member.display_name.toLowerCase().includes(query))
        .slice(0, 6)
        .map((member) => ({
          label: member.display_name,
          hint: `${ROLE_LABEL[member.role] ?? member.role} · ${member.open_tasks} 个待办`,
          value: `@${member.display_name}`,
        }));
    }
    return [];
  }, [mention, overview, CHAT_COMMANDS]);

  /** 光标前的文本是否正处在一个未完成的提及里（`@` 或 `/` 开头、还没打空格）。 */
  const syncMention = useCallback((value: string, caret: number) => {
    const before = value.slice(0, caret);
    const matched = /(?:^|\s)([@/])([^\s@/]*)$/.exec(before);
    if (!matched) {
      setMention(null);
      return;
    }
    const start = caret - matched[2].length - 1;
    setMention({
      kind: matched[1] === "@" ? "member" : "agent",
      query: matched[2],
      start,
      end: caret,
      active: 0,
    });
  }, []);

  const applyMention = useCallback(
    (value: string) => {
      const node = composer.current;
      if (!mention || !node) return;
      const next = `${draft.slice(0, mention.start)}${value} ${draft.slice(mention.end)}`;
      setDraft(next);
      setMention(null);
      const caret = mention.start + value.length + 1;
      requestAnimationFrame(() => {
        node.focus();
        node.setSelectionRange(caret, caret);
      });
    },
    [mention, draft],
  );

  const send = useCallback(
    async (event: FormEvent) => {
      event.preventDefault();
      const content = draft.trim();
      if (!content || !projectId || sending) return;
      setSending(true);
      try {
        const message = await postProjectMessage(projectId, content);
        setMention(null);
        stickToBottom.current = true;
        setMessages((current) => (current.some((item) => item.seq === message.seq) ? current : [...current, message]));
        setDraft("");
      } catch (failure) {
        notify(errorMessage(failure, "发送失败"));
      } finally {
        setSending(false);
      }
    },
    [draft, projectId, sending, notify],
  );

  const switchMode = useCallback(
    async (mode: TaskMode) => {
      if (!projectId || modeBusy) return;
      setModeBusy(true);
      try {
        await updateProjectSettings(projectId, { task_mode: mode });
        notify(`任务推进模式已切换为「${TASK_MODE_LABEL[mode].label}」`);
        await Promise.all([load(), refresh()]);
      } catch (failure) {
        notify(errorMessage(failure, "模式切换失败（需要队长或管理员）"));
      } finally {
        setModeBusy(false);
      }
    },
    [projectId, modeBusy, notify, load, refresh],
  );

  /** 消息高亮用的名字名单：成员 + Agent（只认存在的名字）。 */
  const mentionNames = useMemo(
    () => [...(overview?.members ?? []).map((member) => member.display_name), ...(overview?.agents ?? []).map((agent) => agent.display_name)],
    [overview],
  );

  /** 任务板数据源：dashboard.tasks 是全量任务（overview 只给未结束的前 30 条）。 */
  const allTasks: Task[] = dashboard.tasks ?? [];
  const openTasks = overview?.tasks.open_items ?? [];
  const unassignedCount = allTasks.filter((task) => !task.assignee_member_id && OPEN_TASK_STATES.includes(String(task.status))).length;

  const filteredTasks = useMemo(() => {
    return allTasks
      .filter((task) => {
        const status = String(task.status);
        if (filters.status === "open" && !OPEN_TASK_STATES.includes(status)) return false;
        if (filters.status !== "open" && filters.status !== "all" && status !== filters.status) return false;
        if (filters.assignee === "unassigned" && task.assignee_member_id) return false;
        if (filters.assignee !== "all" && filters.assignee !== "unassigned" && task.assignee_member_id !== filters.assignee) {
          return false;
        }
        return true;
      })
      .sort((a, b) => {
        // 与后端队列同序：有截止时间的优先，其次优先级，最后更新时间
        const deadlineA = a.deadline ? Date.parse(a.deadline) : Number.POSITIVE_INFINITY;
        const deadlineB = b.deadline ? Date.parse(b.deadline) : Number.POSITIVE_INFINITY;
        if (deadlineA !== deadlineB) return deadlineA - deadlineB;
        return String(b.updated_at).localeCompare(String(a.updated_at));
      });
  }, [allTasks, filters]);

  const loadTaskFlags = useCallback(async () => {
    if (!projectId) return;
    try {
      const rows = await getProjectTaskFlags(projectId);
      setTaskFlags(Object.fromEntries(rows.map((row) => [row.task_id, row])));
    } catch {
      setTaskFlags({});
    }
  }, [projectId]);

  const refreshTasks = useCallback(async () => {
    setTasksLoading(true);
    try {
      await Promise.all([refresh(), load(), loadTaskFlags()]);
      setSelected(new Set());
    } finally {
      setTasksLoading(false);
    }
  }, [refresh, load, loadTaskFlags]);

  /** 聊天区 /task 命令：`/task 任务标题` 回车即建任务（不跳 Tab），随后发一条挂任务引用的消息。 */
  const taskCommandTitle = useMemo(() => {
    const matched = /^\/task\s+(.+)$/i.exec(draft.trim());
    return matched ? matched[1].trim() : "";
  }, [draft]);

  const createTaskFromChat = useCallback(async () => {
    if (!projectId || !taskCommandTitle || creatingFromChat) return;
    setCreatingFromChat(true);
    try {
      const created = await createTask(projectId, {
        title: taskCommandTitle.slice(0, 180),
        description: `由 ${"聊天区 /task"} 创建`,
        stage: "modeling",
        assignee: "Unassigned",
        assignee_member_id: null,
        deadline: null,
        priority: "medium",
        requires_review: true,
        allow_future_data: false,
      });
      const message = await postProjectMessage(projectId, `已新建任务「${created.title}」（在「任务」Tab 里派单与设置执行方式）`, undefined, created.id);
      stickToBottom.current = true;
      setMessages((current) => (current.some((item) => item.seq === message.seq) ? current : [...current, message]));
      setDraft("");
      notify(`任务「${created.title}」已创建`);
      await refreshTasks();
    } catch (failure) {
      notify(errorMessage(failure, "建任务失败"));
    } finally {
      setCreatingFromChat(false);
    }
  }, [projectId, taskCommandTitle, creatingFromChat, notify, refreshTasks]);

  /** 输入区的统一提交：`/task …` 建任务，其余当聊天发出。 */
  const submitCompose = useCallback(
    async (event: FormEvent) => {
      if (taskCommandTitle) {
        event.preventDefault();
        await createTaskFromChat();
        return;
      }
      await send(event);
    },
    [taskCommandTitle, createTaskFromChat, send],
  );

  const submitTask = useCallback(
    async (event: FormEvent) => {
      event.preventDefault();
      if (!projectId || creating) return;
      if (taskForm.title.trim().length < 2) {
        notify("任务名称至少 2 个字符");
        return;
      }
      setCreating(true);
      try {
        await createTask(projectId, {
          title: taskForm.title.trim(),
          description: taskForm.description.trim(),
          stage: taskForm.stage,
          assignee: "Unassigned",
          assignee_member_id: taskForm.assignee_member_id || null,
          // datetime-local 是本地时间，转 ISO 再发（服务端按 UTC 解析会偏时区）
          deadline: taskForm.deadline ? new Date(taskForm.deadline).toISOString() : null,
          priority: taskForm.priority,
          requires_review: true,
          allow_future_data: false,
        });
        setCreateOpen(false);
        setTaskForm({ title: "", description: "", stage: "modeling", priority: "medium", assignee_member_id: "", deadline: "" });
        notify("任务已创建（模板包不是必选项：这条就是自定义任务）");
        await refreshTasks();
      } catch (failure) {
        notify(errorMessage(failure, "任务创建失败"));
      } finally {
        setCreating(false);
      }
    },
    [projectId, creating, taskForm, notify, refreshTasks],
  );

  const assignSingle = useCallback(
    async (taskId: string, memberId: string) => {
      if (busyTask) return;
      setBusyTask(taskId);
      try {
        await assignTask(taskId, memberId);
        notify(memberId ? "派单已更新" : "已收回指派");
        await refreshTasks();
      } catch (failure) {
        notify(errorMessage(failure, "派单失败"));
      } finally {
        setBusyTask("");
      }
    },
    [busyTask, notify, refreshTasks],
  );

  const toggleCandidates = useCallback(
    async (taskId: string) => {
      if (candidates[taskId]) {
        setCandidates((current) => ({ ...current, [taskId]: undefined }));
        return;
      }
      setCandidateBusy(taskId);
      try {
        const loaded = await getTaskCandidates(taskId);
        setCandidates((current) => ({ ...current, [taskId]: loaded }));
      } catch (failure) {
        notify(errorMessage(failure, "候选执行体读取失败"));
      } finally {
        setCandidateBusy("");
      }
    },
    [candidates, notify],
  );

  const runBulkAssign = useCallback(async () => {
    if (!projectId || !selected.size || !bulkTarget || bulkBusy) return;
    const target = bulkTarget === "__none__" ? "" : bulkTarget;
    setBulkBusy(true);
    try {
      const result = await bulkAssignTasks(projectId, [...selected], target);
      const failed = result.failures.length;
      notify(
        failed
          ? `已更新 ${result.updated} 条，${failed} 条未变（${result.failures[0].reason}）`
          : target
            ? `已把 ${result.updated} 条任务派出`
            : `已收回 ${result.updated} 条任务的指派`,
      );
      setSelected(new Set());
      setBulkTarget("");
      await refreshTasks();
    } catch (failure) {
      notify(errorMessage(failure, "批量派单失败"));
    } finally {
      setBulkBusy(false);
    }
  }, [projectId, selected, bulkTarget, bulkBusy, notify, refreshTasks]);

  const loadDeliverables = useCallback(async () => {
    if (!projectId) return;
    setDeliverablesLoading(true);
    try {
      setDeliverables(await getProjectDeliverables(projectId));
      setDeliverablesError("");
    } catch (failure) {
      setDeliverablesError(errorMessage(failure, "成果空间读取失败"));
    } finally {
      setDeliverablesLoading(false);
    }
  }, [projectId]);

  /** 附件：上传到个人云盘 → 导入本项目成果物 → 发一条带引用的消息（点开能跳到成果物库）。 */
  const attachFile = useCallback(
    async (file: File) => {
      if (!projectId || uploading) return;
      setUploading(true);
      try {
        const uploaded = await uploadDriveFile(file);
        const imported = await importDriveFile(projectId, uploaded.file.id);
        const message = await postProjectMessage(
          projectId,
          `已上传「${imported.name}」（已加入本项目成果物库）`,
          imported.artifact_id,
        );
        stickToBottom.current = true;
        setMessages((current) => (current.some((item) => item.seq === message.seq) ? current : [...current, message]));
        await Promise.all([refresh(), loadDeliverables()]);
        notify(`「${imported.name}」已上传并加入成果物`);
      } catch (failure) {
        notify(errorMessage(failure, "上传失败"));
      } finally {
        setUploading(false);
      }
    },
    [projectId, uploading, notify, refresh, loadDeliverables],
  );

  /** 拖拽上传：一次最多 5 个文件（逐个走 attachFile，避免并发把云盘打满）。 */
  const handleDrop = useCallback(
    async (event: React.DragEvent) => {
      event.preventDefault();
      setDropActive(false);
      const files = Array.from(event.dataTransfer?.files ?? []).slice(0, 5);
      for (const file of files) {
        await attachFile(file);
      }
    },
    [attachFile],
  );

  // 成果空间按需加载：切到该 Tab 时拉一次（避免每次进页面都多一个请求）
  useEffect(() => {
    if (tab !== "artifacts" || !projectId) return;
    if (deliverables) return;
    void loadDeliverables();
  }, [tab, projectId, deliverables, loadDeliverables]);

  const mode = (overview?.project.task_mode ?? project?.task_mode ?? "manual") as TaskMode;
  const viewer = overview?.viewer;
  const canChat = viewer ? viewer.can_chat : true;
  /** 队长/负责人：能派单、改派、批量派 */
  const canManage = Boolean(viewer?.can_manage);
  /** 非 manual 模式才允许成员自己认领（hybrid / auto） */
  const hybridMode = mode !== "manual";

  const statusLine = useMemo(() => {
    const entries = Object.entries(overview?.tasks.by_status ?? {}).sort((a, b) => b[1] - a[1]);
    return entries.map(([status, count]) => `${STATUS_LABEL[status] ?? status} ${count}`).join(" · ");
  }, [overview]);

  if (!ready || !authenticated) return <LoadingSkeleton rows={4} label="正在确认会话…" />;

  if (!projectId) {
    return (
      <div className="page-content" data-page="workspace">
        <PageHeading hint="群聊、成员概览与成果空间——项目的主工作区域" />
        <Panel title="还没有项目">
          <EmptyState>先去「项目总览」新建一个项目，工作区会自动接入它的成员、Agent 与聊天流。</EmptyState>
        </Panel>
      </div>
    );
  }

  return (
    <div className="page-content" data-page="workspace">
      {guideOpen ? (
        <section className="guide-panel" data-testid="onboarding-guide">
          <div className="guide-main">
            <h3>欢迎使用 synapforge · Agent 协作平台</h3>
            <p>人在群里说话，Agent 在群里干活：这里是项目的主工作区——左边群聊（任务、进度、成果、复核都会实时出现），右边是谁在线、谁在跑什么。</p>
            <ol className="guide-steps">
              <li>
                <strong>1 · 准备任务</strong>「任务」Tab 点「+ 新建任务」：写清楚要什么（比如"让 Agent 起草一份周报"），不依赖模板包；
                数模全流程可以用模板包一键生成（高级工具）。
              </li>
              <li>
                <strong>2 · 接入 Agent</strong> 在「设备与接入」给一台机器配对，它就会自动领取任务、把过程和产出实时发回这个群聊。
              </li>
              <li>
                <strong>3 · 收成果</strong> Agent 的产出进「成果空间」，复核通过后才算交付；门禁和风险都在那里盯着。
              </li>
            </ol>
            <div className="guide-links">
              {demoProject ? (
                <Link
                  className="button button-primary"
                  href={`/workspace?project=${demoProject.id}`}
                  data-testid="guide-demo-link"
                >
                  看演示：多 Agent 文档撰写 <ArrowRight size={14} />
                </Link>
              ) : null}
              <Link className="inline-link" href="/devices">接入我的第一台 Agent</Link>
              <a className="inline-link" href="/downloads/synapforge-setup-0.2.1-x64.exe" data-testid="guide-download">下载桌面端（Windows）</a>
              <Link className="inline-link" href="/">回到项目总览</Link>
            </div>
          </div>
          <button
            type="button"
            className="text-button"
            onClick={() => {
              window.localStorage.setItem("map.guide.dismissed.v1", "1");
              setGuideOpen(false);
            }}
            data-testid="guide-dismiss"
          >
            知道了，不再显示
          </button>
        </section>
      ) : null}

      <PageHeading
        hint={
          overview?.project.goal
            ? `项目「${overview.project.name}」· 目标：${overview.project.goal}`
            : project
              ? `项目「${project.name}」· 群聊、成员概览与成果空间`
              : "群聊、成员概览与成果空间——项目的主工作区域"
        }
        actions={
          <span className={`connection-state${connected ? "" : " is-offline"}`} title={connected ? "实时连接已建立" : "实时连接断开，正在重连"}>
            <span className={connected ? "pulse-dot" : "pulse-dot-offline"} />
            {connected ? "实时" : "离线"}
          </span>
        }
      />

      <div className="tabs" role="tablist" aria-label="工作区视图">
        {TABS.map((item) => {
          const Icon = item.icon;
          const active = tab === item.id;
          return (
            <button
              key={item.id}
              type="button"
              role="tab"
              aria-selected={active}
              className={`tab${active ? " tab-active" : ""}`}
              onClick={() => setTab(item.id)}
              data-testid={`workspace-tab-${item.id}`}
              title={item.hint}
            >
              <Icon size={14} /> {item.label}
            </button>
          );
        })}
      </div>

      {error ? <p className="hint" style={{ color: "var(--red)" }}>{error}</p> : null}

      <div className={`grid grid-main-side workspace-grid${tab === "chat" ? " is-fill" : ""}`}>
        <div className="grid workspace-main">
          {tab === "chat" ? (
            <Panel
              title="项目群聊"
              subtitle="人类发言 + Agent 的动作卡片：领任务、进度、成果、复核都由服务端从事件实时派生；把文件拖进来即可上传"
            >
              <div
                className={dropActive ? "drop-active" : undefined}
                data-testid="chat-dropzone"
                onDragOver={(event) => {
                  if (!canChat) return;
                  event.preventDefault();
                  setDropActive(true);
                }}
                onDragLeave={() => setDropActive(false)}
                onDrop={(event) => {
                  if (!canChat) return;
                  void handleDrop(event);
                }}
              >
              <div
                className="chat-stream"
                ref={scroller}
                data-testid="chat-stream"
                onScroll={(event) => {
                  const node = event.currentTarget;
                  stickToBottom.current = node.scrollHeight - node.scrollTop - node.clientHeight < 80;
                }}
              >
                {loading ? <LoadingSkeleton rows={3} label="正在读取聊天记录…" /> : null}
                {!loading && hasMore ? (
                  <div className="chat-more">
                    <button type="button" className="text-button" onClick={() => void loadOlder()} disabled={loadingMore}>
                      {loadingMore ? <Loader2 size={13} className="spin" /> : null} 读取更早的记录
                    </button>
                  </div>
                ) : null}
                {!loading && !messages.length ? (
                  <EmptyState>还没有消息。说点什么，或者等 Agent 领走第一条任务——它的动作会自动出现在这里。</EmptyState>
                ) : null}
                {messages.map((message) => (
                  <ChatMessage key={message.id} message={message} names={mentionNames} onOpenArtifact={setDrawerArtifact} />
                ))}
              </div>

              <form className="chat-compose" onSubmit={submitCompose}>
                {mention && mentionCandidates.length ? (
                  <div className="mention-pop" role="listbox" aria-label={mention.kind === "member" ? "选择成员" : "选择 Agent"} data-testid="mention-pop">
                    <div className="mention-pop-head">
                      {mention.kind === "member" ? <AtSign size={12} /> : <Bot size={12} />}
                      {mention.kind === "member" ? "提及成员" : "提及 Agent"}（↑↓ 选择，Enter 确认，Esc 取消）
                    </div>
                    {mentionCandidates.map((candidate, index) => (
                      <button
                        type="button"
                        key={candidate.value}
                        role="option"
                        aria-selected={index === mention.active}
                        className={`mention-item${index === mention.active ? " is-active" : ""}`}
                        data-testid={`mention-option-${index}`}
                        onMouseDown={(event) => {
                          event.preventDefault();
                          applyMention(candidate.value);
                        }}
                      >
                        <strong>{candidate.value}</strong>
                        <small>{candidate.hint}</small>
                      </button>
                    ))}
                  </div>
                ) : null}
                {mention && !mentionCandidates.length ? (
                  <div className="mention-pop" data-testid="mention-pop-empty">
                    <div className="mention-pop-head">没有匹配的{mention.kind === "member" ? "成员" : "Agent"}</div>
                  </div>
                ) : null}
                {taskCommandTitle ? (
                  <div className="compose-command-hint" data-testid="task-command-hint">
                    <span className="card-badge">/task</span>
                    回车将新建任务：<strong>{taskCommandTitle}</strong>
                  </div>
                ) : null}
                <div className="compose-field">
                  <textarea
                    ref={composer}
                    value={draft}
                    onChange={(event) => {
                      setDraft(event.target.value);
                      syncMention(event.target.value, event.target.selectionStart ?? event.target.value.length);
                    }}
                    onClick={(event) => syncMention(draft, event.currentTarget.selectionStart ?? draft.length)}
                    onBlur={() => setMention(null)}
                    onKeyDown={(event) => {
                      if (mention && mentionCandidates.length) {
                        if (event.key === "ArrowDown") {
                          event.preventDefault();
                          setMention({ ...mention, active: (mention.active + 1) % mentionCandidates.length });
                          return;
                        }
                        if (event.key === "ArrowUp") {
                          event.preventDefault();
                          setMention({ ...mention, active: (mention.active - 1 + mentionCandidates.length) % mentionCandidates.length });
                          return;
                        }
                        if (event.key === "Enter" || event.key === "Tab") {
                          event.preventDefault();
                          applyMention(mentionCandidates[mention.active].value);
                          return;
                        }
                        if (event.key === "Escape") {
                          event.preventDefault();
                          setMention(null);
                          return;
                        }
                      }
                      if (event.key === "Enter" && !event.shiftKey) {
                        event.preventDefault();
                        void submitCompose(event as unknown as FormEvent);
                      }
                    }}
                    placeholder={
                      canChat ? "@ 提及成员 · / 提及 Agent · /task 建任务 · Enter 发送" : "当前角色只能查看，不能发言"
                    }
                    rows={2}
                    maxLength={4000}
                    disabled={!canChat}
                    data-testid="chat-input"
                  />
                  <div className="compose-actions">
                    <input
                      ref={fileInput}
                      type="file"
                      className="hidden-file-input"
                      data-testid="chat-file-input"
                      onChange={(event) => {
                        const file = event.target.files?.[0];
                        event.target.value = "";
                        if (file) void attachFile(file);
                      }}
                    />
                    <button
                      type="button"
                      className="text-button"
                      disabled={!canChat || uploading}
                      onClick={() => fileInput.current?.click()}
                      title="上传文件：先进你的云盘，再自动加入本项目成果物库（单文件 ≤200MB）"
                      data-testid="chat-attach"
                    >
                      {uploading ? <Loader2 size={13} className="spin" /> : <Paperclip size={13} />}
                      {uploading ? "上传中…" : "上传文件"}
                    </button>
                    <button
                      type="button"
                      className="text-button"
                      disabled={!canChat}
                      onClick={() => {
                        composer.current?.focus();
                        const at = " @";
                        setDraft((current) => current + at);
                        syncMention(draft + at, draft.length + at.length);
                      }}
                      data-testid="chat-mention-member"
                    >
                      <AtSign size={13} /> 成员
                    </button>
                    <button
                      type="button"
                      className="text-button"
                      disabled={!canChat}
                      onClick={() => {
                        composer.current?.focus();
                        const slash = " /";
                        setDraft((current) => current + slash);
                        syncMention(draft + slash, draft.length + slash.length);
                      }}
                      data-testid="chat-mention-agent"
                    >
                      <Bot size={13} /> Agent
                    </button>
                  </div>
                </div>
                <button type="submit" className="button button-primary" disabled={!canChat || sending || !draft.trim()} data-testid="chat-send">
                  {sending ? <Loader2 size={14} className="spin" /> : <Send size={14} />}
                  发送
                </button>
              </form>
              <p className="hint">
                发言按项目权限：<b>队长 / 成员 / 复核人</b>可以说话，观察者只读。Agent 不"发言"，它的一切动作由事件派生。
                「上传文件」会先存进你的云盘（200MB 上限），再自动加入本项目成果物库并在这条消息上留引用。
                提及（@ 成员 / / Agent）用于点名与留痕：<b>不会自动派活</b>——派活请用「任务」Tab 的派单。
              </p>
              </div>
            </Panel>
          ) : null}

          {tab === "tasks" ? (
            <Panel
              title="任务板"
              subtitle={
                canManage
                  ? "勾选任务后可批量派给某个成员；也可以逐条改派或收回未指派"
                  : hybridMode
                    ? "可以认领还没有负责人的任务；认领后由你的 Agent 执行"
                    : "当前是「队长派单」模式：任务由队长分配，你在「我的任务」里看自己的活"
              }
              actions={
                <button type="button" className="button button-primary" onClick={() => setCreateOpen(true)} data-testid="task-create-open">
                  + 新建任务
                </button>
              }
            >
              <div className="metrics-grid">
                <Metric label="任务总数" value={overview?.tasks.total ?? 0} detail="本项目全部任务" />
                <Metric
                  label="进行中"
                  value={(overview?.tasks.by_status.RUNNING ?? 0) + (overview?.tasks.by_status.CLAIMED ?? 0)}
                  detail="已领取或执行中"
                />
                <Metric
                  label="待复核"
                  value={overview?.tasks.by_status.WAITING_REVIEW ?? 0}
                  detail="等人工或复核人确认"
                  tone={(overview?.tasks.by_status.WAITING_REVIEW ?? 0) > 0 ? "warning" : "default"}
                />
                <Metric label="未指派" value={unassignedCount} detail="谁先轮到谁跑" />
              </div>

              <div className="form-row" style={{ marginTop: 10 }}>
                <label className="field" style={{ flex: "0 1 170px" }}>
                  <span>状态</span>
                  <select
                    value={filters.status}
                    data-testid="task-filter-status"
                    onChange={(event) => setFilters({ ...filters, status: event.target.value })}
                  >
                    <option value="open">未结束</option>
                    <option value="all">全部</option>
                    <option value="READY">待执行</option>
                    <option value="RUNNING">执行中</option>
                    <option value="WAITING_REVIEW">待复核</option>
                    <option value="BLOCKED">受阻</option>
                    <option value="APPROVED">已通过</option>
                  </select>
                </label>
                <label className="field" style={{ flex: "0 1 200px" }}>
                  <span>负责人</span>
                  <select
                    value={filters.assignee}
                    data-testid="task-filter-assignee"
                    onChange={(event) => setFilters({ ...filters, assignee: event.target.value })}
                  >
                    <option value="all">全部</option>
                    <option value="unassigned">未指派</option>
                    {(overview?.members ?? []).map((member) => (
                      <option key={member.member_id} value={member.member_id}>
                        {member.display_name}
                      </option>
                    ))}
                  </select>
                </label>
                <button
                  type="button"
                  className="button button-secondary"
                  onClick={() => void refreshTasks()}
                  data-testid="task-refresh"
                >
                  {tasksLoading ? <Loader2 size={14} className="spin" /> : <RefreshCcw size={14} />} 刷新
                </button>
              </div>

              {canManage ? (
                <div className="bulk-bar" data-testid="task-bulk-bar">
                  <span className="hint">已选 {selected.size} 条</span>
                  <select value={bulkTarget} data-testid="bulk-target" onChange={(event) => setBulkTarget(event.target.value)}>
                    <option value="">派给…</option>
                    {(overview?.members ?? []).map((member) => (
                      <option key={member.member_id} value={member.member_id}>
                        {member.display_name}（{ROLE_LABEL[member.role] ?? member.role}）
                      </option>
                    ))}
                    <option value="__none__">收回指派（回到未指派）</option>
                  </select>
                  <button
                    type="button"
                    className="button button-primary"
                    disabled={!selected.size || !bulkTarget || bulkBusy}
                    onClick={() => void runBulkAssign()}
                    data-testid="bulk-assign"
                  >
                    {bulkBusy ? <Loader2 size={14} className="spin" /> : <Send size={14} />} 批量派单
                  </button>
                  <button
                    type="button"
                    className="text-button"
                    disabled={!selected.size}
                    onClick={() => setSelected(new Set())}
                    data-testid="bulk-clear"
                  >
                    清空选择
                  </button>
                </div>
              ) : null}

              <div className="list">
                {filteredTasks.map((task) => {
                  const mine = Boolean(viewer?.member_id) && task.assignee_member_id === viewer?.member_id;
                  const canClaim = hybridMode && !task.assignee_member_id && !canManage && Boolean(viewer?.member_id);
                  const selectable = canManage || canClaim;
                  return (
                    <div className="list-row is-task" key={task.id} data-testid="task-row">
                      {selectable ? (
                        <input
                          type="checkbox"
                          aria-label={`选择 ${task.title}`}
                          checked={selected.has(task.id)}
                          onChange={(event) => {
                            const next = new Set(selected);
                            if (event.target.checked) next.add(task.id);
                            else next.delete(task.id);
                            setSelected(next);
                          }}
                        />
                      ) : (
                        <span className="list-row-spacer" aria-hidden />
                      )}
                      <div className="list-row-main">
                        <strong>{task.title}</strong>
                        <small>
                          {task.stage} · {task.priority}
                          {task.deadline ? ` · 截止 ${formatTime(task.deadline)}` : ""}
                        </small>
                      </div>
                      {taskFlags[task.id]?.evidence_missing ? (
                        <span className="badge status-neutral" data-testid={`task-gap-${task.id}`} title="显式证据要求还差几条；批准时会拦下">
                          缺证据 {taskFlags[task.id].evidence_missing}
                        </span>
                      ) : null}
                      {taskFlags[task.id]?.usage_overrun ? (
                        <span className="badge status-neutral" data-testid={`task-overrun-${task.id}`} title="本次执行超出预算；批准时会被门禁拦下">
                          超预算
                        </span>
                      ) : null}
                      <StatusPill status={task.status} label={STATUS_LABEL[task.status] ?? task.status} />
                      <div className="task-owner">
                        {canManage ? (
                          <select
                            value={task.assignee_member_id ?? ""}
                            disabled={busyTask === task.id}
                            data-testid={`task-assignee-${task.id}`}
                            onChange={(event) => void assignSingle(task.id, event.target.value)}
                          >
                            <option value="">未指派（先到先得）</option>
                            {(overview?.members ?? []).map((member) => (
                              <option key={member.member_id} value={member.member_id}>
                                {member.display_name}
                              </option>
                            ))}
                          </select>
                        ) : (
                          <span className="hint">
                            {task.assignee_member_id
                              ? overview?.members.find((member) => member.member_id === task.assignee_member_id)
                                  ?.display_name ?? task.assignee_member_id
                              : "未指派"}
                          </span>
                        )}
                        {canClaim ? (
                          <button
                            type="button"
                            className="text-button"
                            disabled={busyTask === task.id}
                            onClick={() => void assignSingle(task.id, viewer?.member_id ?? "")}
                            data-testid={`task-claim-${task.id}`}
                          >
                            认领
                          </button>
                        ) : null}
                        {mine && !canManage ? (
                          <button
                            type="button"
                            className="text-button"
                            disabled={busyTask === task.id}
                            onClick={() => void assignSingle(task.id, "")}
                            data-testid={`task-release-${task.id}`}
                          >
                            释放
                          </button>
                        ) : null}
                      </div>
                      <button
                        type="button"
                        className="text-button task-candidate-toggle"
                        disabled={candidateBusy === task.id}
                        onClick={() => void toggleCandidates(task.id)}
                        data-testid={`task-candidates-${task.id}`}
                      >
                        {candidateBusy === task.id ? <Loader2 size={12} className="spin" /> : <Cpu size={12} />}
                        {candidates[task.id] ? "收起候选" : "推荐执行体"}
                      </button>
                      {candidates[task.id] ? (
                        <div className="task-candidates" data-testid={`task-candidates-panel-${task.id}`}>
                          {candidates[task.id]!.required_capabilities.length ? (
                            <small className="hint">
                              要求：{candidates[task.id]!.required_capabilities.join("、")}
                            </small>
                          ) : (
                            <small className="hint">这条任务没有声明技能要求，任何在线执行体都能跑。</small>
                          )}
                          {candidates[task.id]!.satisfied.length ? (
                            candidates[task.id]!.satisfied.slice(0, 3).map((candidate, index) => (
                              <div className={`candidate-row ${index === 0 ? "is-top" : ""}`} key={candidate.agent_id}>
                                <span className="candidate-name">
                                  {index === 0 ? "推荐" : ""} {candidate.display_name}
                                  {candidate.online ? "" : "（离线）"}
                                </span>
                                <small>{candidate.reason}</small>
                                {canManage ? (
                                  <button
                                    type="button"
                                    className="text-button"
                                    disabled={busyTask === task.id}
                                    onClick={() => void assignSingle(task.id, candidate.member_id)}
                                    data-testid={`task-candidate-assign-${task.id}-${candidate.agent_id}`}
                                  >
                                    派给 {candidate.display_name}
                                  </button>
                                ) : null}
                              </div>
                            ))
                          ) : (
                            <>
                              <small className="hint">当前没有全满足的执行体；下面是差得最少的：</small>
                              {[...candidates[task.id]!.partial, ...candidates[task.id]!.satisfied].slice(0, 3).map((candidate) => (
                                <div className="candidate-row" key={candidate.agent_id}>
                                  <span className="candidate-name">
                                    {candidate.display_name}
                                    {candidate.online ? "" : "（离线）"}
                                  </span>
                                  <small>{candidate.reason}</small>
                                </div>
                              ))}
                              {!candidates[task.id]!.partial_total && !candidates[task.id]!.satisfied_total ? (
                                <small className="hint">这个项目里还没有对该项目授权的执行体；先在一台机器上接入。</small>
                              ) : null}
                            </>
                          )}
                        </div>
                      ) : null}
                    </div>
                  );
                })}
                {!filteredTasks.length && !tasksLoading ? (
                  <EmptyState>{filters.status === "open" ? "没有未结束的任务。" : "没有符合筛选条件的任务。"}</EmptyState>
                ) : null}
              </div>
              <p className="hint">
                派单只决定"谁负责"，真正执行的是领到任务的 Agent。「新建任务」不依赖模板包——模板包适合数模全流程，
                自定义任务适合"让 Agent 写一份文档"这类活。完整任务板（执行方式、依赖诊断）在{" "}
                <Link href="/tasks" className="inline-link">任务与流程</Link>；我的待办在{" "}
                <Link href="/my-tasks" className="inline-link">我的任务</Link>。
              </p>
            </Panel>
          ) : null}

          {tab === "artifacts" ? (
            <Panel
              title="成果空间"
              subtitle="成果物、文档三层版本、交接单与复核门禁——项目的产出都在这里"
              actions={
                <button
                  type="button"
                  className="button button-secondary"
                  onClick={() => void loadDeliverables()}
                  data-testid="deliverables-refresh"
                >
                  {deliverablesLoading ? <Loader2 size={14} className="spin" /> : <RefreshCcw size={14} />} 刷新
                </button>
              }
            >
              <div className="metrics-grid">
                <Metric label="成果物" value={deliverables?.artifacts.total ?? 0} detail="含各版本" />
                <Metric
                  label="已批准"
                  value={deliverables?.artifacts.by_status.APPROVED ?? 0}
                  detail="下游可用"
                  tone="positive"
                />
                <Metric
                  label="待复核"
                  value={
                    (deliverables?.artifacts.by_status.SUBMITTED ?? 0) +
                    (deliverables?.artifacts.by_status.PENDING_REVIEW ?? 0)
                  }
                  detail="等门禁确认"
                  tone={(deliverables?.artifacts.by_status.SUBMITTED ?? 0) > 0 ? "warning" : "default"}
                />
                <Metric
                  label="门禁未通过"
                  value={deliverables?.gates.open ?? 0}
                  detail={`共 ${deliverables?.gates.total ?? 0} 个门禁`}
                  tone={(deliverables?.gates.open ?? 0) > 0 ? "warning" : "positive"}
                />
              </div>

              {deliverablesError ? (
                <p className="hint" style={{ color: "var(--red)" }}>
                  {deliverablesError}
                </p>
              ) : null}

              <h3 className="section-title">
                成果物（{deliverables?.artifacts.recent.length ?? 0} / {deliverables?.artifacts.total ?? 0}）
              </h3>
              <div className="list">
                {(deliverables?.artifacts.recent ?? []).map((artifact) => (
                  <div className="list-row" key={artifact.id}>
                    <div className="list-row-main">
                      <strong>{artifact.name}</strong>
                      <small>
                        v{artifact.version} · {artifact.artifact_type} ·{" "}
                        {artifact.created_by_kind === "agent" ? "Agent" : "成员"}产出 · {formatTime(artifact.created_at)}
                      </small>
                    </div>
                    <StatusPill status={artifact.status} />
                    <button
                      type="button"
                      className="text-button"
                      onClick={() =>
                        setDrawerArtifact({ id: artifact.id, name: artifact.name, artifact_type: artifact.artifact_type, version: artifact.version })
                      }
                      data-testid="artifacts-open-drawer"
                    >
                      查看
                    </button>
                  </div>
                ))}
                {!deliverables?.artifacts.recent.length ? <EmptyState>还没有成果物。</EmptyState> : null}
              </div>

              <h3 className="section-title">
                文档三层版本（草稿 {deliverables?.documents.by_layer.draft ?? 0} · 提交{" "}
                {deliverables?.documents.by_layer.submitted ?? 0} · 批准 {deliverables?.documents.by_layer.approved ?? 0}）
              </h3>
              <div className="list">
                {(deliverables?.documents.recent ?? []).map((document) => (
                  <div className="list-row" key={document.id}>
                    <div className="list-row-main">
                      <strong>{document.name}</strong>
                      <small>
                        v{document.version} · {document.artifact_type}
                        {document.has_draft ? " · 有未提交草稿" : ""}
                      </small>
                    </div>
                    <span className={`layer-pill is-${document.layer}`}>{LAYER_LABEL[document.layer]}</span>
                  </div>
                ))}
                {!deliverables?.documents.recent.length ? (
                  <EmptyState>还没有文档（论文骨架、建模报告这类文本成果物会出现在这里）。</EmptyState>
                ) : null}
              </div>

              <h3 className="section-title">
                交接单（{deliverables?.handoffs.total ?? 0} 张
                {Object.keys(deliverables?.handoffs.by_status ?? {}).length
                  ? ` · ${Object.entries(deliverables?.handoffs.by_status ?? {})
                      .map(([status, count]) => `${HANDOFF_STATUS_LABEL[status] ?? status} ${count}`)
                      .join(" / ")}`
                  : ""}
                ）
              </h3>
              <div className="list">
                {(deliverables?.handoffs.recent ?? []).map((handoff) => (
                  <div className="list-row" key={handoff.id}>
                    <div className="list-row-main">
                      <strong>{handoff.objective || "（未写目标）"}</strong>
                      <small>
                        {handoff.sender_agent_id} · {formatTime(handoff.created_at)}
                        {handoff.key_conclusions.length ? ` · 结论：${handoff.key_conclusions[0]}` : ""}
                      </small>
                    </div>
                    <StatusPill status="APPROVED" label={HANDOFF_STATUS_LABEL[handoff.status] ?? handoff.status} />
                  </div>
                ))}
                {!deliverables?.handoffs.recent.length ? (
                  <EmptyState>还没有交接单。Agent 之间接力时会自动生成。</EmptyState>
                ) : null}
              </div>

              <h3 className="section-title">
                复核与门禁（{deliverables?.reviews.total ?? 0} 次复核 · 未关闭风险 {deliverables?.risks.open ?? 0}）
              </h3>
              <div className="list">
                {(deliverables?.gates.items ?? []).map((gate) => (
                  <div className="list-row" key={gate.id}>
                    <div className="list-row-main">
                      <strong>
                        {gate.target_type === "artifact" ? "成果物" : gate.target_type === "task" ? "任务" : gate.target_type}
                        门禁
                      </strong>
                      <small>
                        {gate.blocking_count ? `${gate.blocking_count} 条阻断意见` : "无阻断意见"}
                        {gate.approved_by ? ` · 由 ${gate.approved_by} 批准` : ""}
                      </small>
                    </div>
                    <StatusPill status={gate.status} />
                  </div>
                ))}
                {(deliverables?.reviews.recent ?? []).map((review) => (
                  <div className="list-row" key={review.id}>
                    <div className="list-row-main">
                      <strong>
                        复核{review.verdict === "APPROVED" ? "通过" : "未通过"} · {review.target_type}
                      </strong>
                      <small>
                        {review.reviewer_kind === "agent" ? "Agent" : "成员"} {review.reviewer} ·{" "}
                        {formatTime(review.created_at)}
                        {review.summary ? ` · ${review.summary}` : ""}
                      </small>
                    </div>
                    <StatusPill status={review.verdict} />
                  </div>
                ))}
                {!deliverables?.gates.items.length && !deliverables?.reviews.recent.length ? (
                  <EmptyState>还没有门禁与复核记录。</EmptyState>
                ) : null}
              </div>

              <div className="link-row">
                <Link className="inline-link" href="/artifacts">
                  成果物库（全部版本与来源）
                </Link>
                <Link className="inline-link" href="/handoffs">
                  交接中心（接力与收据）
                </Link>
                <Link className="inline-link" href="/documents">
                  文档版本（草稿 / 提交 / 批准）
                </Link>
                <Link className="inline-link" href="/review">
                  审核门禁
                </Link>
              </div>
            </Panel>
          ) : null}

          {tab === "overview" ? (
            <Panel title="项目概览" subtitle="立项目标、人数与任务推进方式">
              <div className="kv-list">
                <div className="kv">
                  <span className="kv-key"><Target size={14} /> 项目目标</span>
                  <span>{overview?.project.goal || "（未填写）"}</span>
                </div>
                <div className="kv">
                  <span className="kv-key"><Users size={14} /> 目标人数</span>
                  <span>
                    {overview?.project.target_member_count ? `${overview.project.target_member_count} 人` : "（未设定）"}
                    <span className="hint"> · 当前成员 {overview?.members.length ?? 0} 人（只作参考，不做加入拦截）</span>
                  </span>
                </div>
                <div className="kv">
                  <span className="kv-key"><Info size={14} /> 项目简介</span>
                  <span>{overview?.project.description || "（未填写）"}</span>
                </div>
                <div className="kv">
                  <span className="kv-key"><ClipboardCheck size={14} /> 阶段与进度</span>
                  <span className="grow">
                    <Progress label={overview?.project.stage ?? ""} value={overview?.project.progress ?? 0} />
                  </span>
                </div>
              </div>

              <h3 className="section-title">任务推进方式</h3>
              <p className="hint">{TASK_MODE_LABEL[mode]?.hint}</p>
              <div className="chips">
                {(Object.keys(TASK_MODE_LABEL) as TaskMode[]).map((key) => (
                  <button
                    key={key}
                    type="button"
                    className={`chip${mode === key ? " chip-active" : ""}`}
                    disabled={!viewer?.can_manage || modeBusy}
                    onClick={() => void switchMode(key)}
                    data-testid={`task-mode-${key}`}
                    title={viewer?.can_manage ? TASK_MODE_LABEL[key].hint : "只有队长或管理员能改"}
                  >
                    {TASK_MODE_LABEL[key].label}
                  </button>
                ))}
              </div>
              <p className="hint">
                默认是<b>队长派单</b>，不是全自动：成员在「我的任务」看到派给自己的活。切到
                <b>模板全自动</b>后，调度器每拍只推进一件事（派一条任务），每一步都写进群聊——
                队长随时切回「队长派单」即可让它停手，已经派出去的活不受影响。
              </p>
              {mode === "auto" ? (
                <p className="hint" data-testid="auto-scheduler-note">
                  调度器只做两件事：按能力匹配把任务派给有合格<b>在线</b>执行体的成员；派不出去时提示缺什么。
                  它<b>不会</b>自动批准门禁——批准仍然要人来点。
                </p>
              ) : null}
            </Panel>
          ) : null}
        </div>

        <aside className="grid workspace-side" aria-label="成员概览">
          <Panel
            title="成员"
            subtitle={`${overview?.members.length ?? 0} 人${overview?.project.target_member_count ? ` / 目标 ${overview.project.target_member_count} 人` : ""}`}
          >
            <div className="list">
              {(overview?.members ?? []).map((member) => (
                <div className="list-row" key={member.member_id}>
                  <span className="avatar-dot" aria-hidden>{member.display_name.slice(0, 1)}</span>
                  <div className="list-row-main">
                    <strong>{member.display_name}</strong>
                    <small>
                      {ROLE_LABEL[member.role] ?? member.role} · {member.open_tasks} 个待办
                      {member.agents ? ` · ${member.agents} 个 Agent（${member.agents_online} 在线）` : " · 未接入 Agent"}
                    </small>
                  </div>
                </div>
              ))}
              {!overview?.members.length && !loading ? <EmptyState>还没有成员。</EmptyState> : null}
            </div>
            <p className="hint">
              加人 / 改角色 / 移出在 <Link className="inline-link" href="/team">团队与成员</Link>。
            </p>
          </Panel>

          <Panel title="Agent" subtitle="在线状态与当前在跑什么">
            <div className="list">
              {(overview?.agents ?? []).map((agent) => (
                <div className="list-row" key={agent.agent_id}>
                  <span className={`avatar-dot${agent.connected ? " is-online" : ""}`} aria-hidden>
                    <Bot size={14} />
                  </span>
                  <div className="list-row-main">
                    <strong>{agent.display_name}</strong>
                    <small>
                      {agent.owner_name} · {agent.connected ? "在线" : "离线"}
                      {agent.current_task_title ? ` · 正在跑「${agent.current_task_title}」` : ""}
                      {!agent.current_task_title && agent.last_seen ? ` · 最近在线 ${formatTime(agent.last_seen)}` : ""}
                    </small>
                  </div>
                </div>
              ))}
              {!overview?.agents.length && !loading ? (
                <EmptyState>
                  还没有 Agent 接入本项目。去 <Link className="inline-link" href="/devices">设备与接入</Link> 配对一台。
                </EmptyState>
              ) : null}
            </div>
            <p className="hint">
              <Cpu size={12} /> 运行详情在 <Link className="inline-link" href="/runs">运行控制台</Link>；接入与授权在{" "}
              <Link className="inline-link" href="/devices">设备与接入</Link>。
            </p>
          </Panel>

          <Panel title="成果空间" subtitle="产出与交接的入口">
            <div className="metrics-grid is-2">
              <Metric label="成果物" value={overview?.artifacts.total ?? 0} detail="含各版本" />
              <Metric label="已批准" value={overview?.artifacts.approved ?? 0} detail="可下游使用" tone="positive" />
            </div>
            <div className="link-row">
              <button type="button" className="button button-secondary" onClick={() => setTab("artifacts")} data-testid="open-artifacts">
                <Box size={14} /> 打开成果空间
              </button>
              <Link className="inline-link" href="/handoffs">交接中心</Link>
            </div>
          </Panel>
        </aside>
      </div>

      {drawerArtifact ? (
        <ArtifactDrawer artifact={drawerArtifact} projectId={projectId} onClose={() => setDrawerArtifact(null)} />
      ) : null}

      {createOpen ? (
        <Modal
          title="新建任务"
          subtitle="自定义任务不依赖模板包：给 Agent 安排任何活（写文档、查资料、跑代码都行）"
          onClose={creating ? () => undefined : () => setCreateOpen(false)}
          testId="task-create-modal"
          actions={
            <>
              <button className="button button-secondary" onClick={() => setCreateOpen(false)} disabled={creating}>
                取消
              </button>
              <button className="button button-primary" onClick={(event) => void submitTask(event as unknown as FormEvent)} disabled={creating} data-testid="task-create-submit">
                {creating ? "创建中…" : "创建任务"}
              </button>
            </>
          }
        >
          <form className="task-create-form" onSubmit={(event) => void submitTask(event)}>
            <label className="field">
              <span>任务标题（必填，至少 2 个字符）</span>
              <input
                value={taskForm.title}
                autoFocus
                maxLength={180}
                placeholder="例如：让 Agent 起草一份项目周报"
                data-testid="task-create-title"
                onChange={(event) => setTaskForm({ ...taskForm, title: event.target.value })}
              />
            </label>
            <label className="field">
              <span>说明（给 Agent 看的要求，越具体越好）</span>
              <textarea
                value={taskForm.description}
                rows={3}
                maxLength={2000}
                placeholder="例如：汇总本周任务进展与风险，输出 markdown，引用成果物编号"
                data-testid="task-create-description"
                onChange={(event) => setTaskForm({ ...taskForm, description: event.target.value })}
              />
            </label>
            <div className="form-row">
              <label className="field" style={{ flex: "1 1 160px" }}>
                <span>阶段</span>
                <select value={taskForm.stage} data-testid="task-create-stage" onChange={(event) => setTaskForm({ ...taskForm, stage: event.target.value })}>
                  <option value="problem_intake">审题与事实</option>
                  <option value="problem_analysis">审题与拆解</option>
                  <option value="modeling">模型建立</option>
                  <option value="coding">代码与计算</option>
                  <option value="experiment">仿真与实验</option>
                  <option value="review">独立复核</option>
                  <option value="paper">论文交付</option>
                  <option value="delivery">交付冻结</option>
                </select>
              </label>
              <label className="field" style={{ flex: "1 1 140px" }}>
                <span>优先级</span>
                <select value={taskForm.priority} data-testid="task-create-priority" onChange={(event) => setTaskForm({ ...taskForm, priority: event.target.value })}>
                  <option value="low">低</option>
                  <option value="medium">中</option>
                  <option value="high">高</option>
                  <option value="critical">紧急</option>
                </select>
              </label>
              <label className="field" style={{ flex: "1 1 180px" }}>
                <span>负责人（可留空：谁先轮到谁跑）</span>
                <select
                  value={taskForm.assignee_member_id}
                  data-testid="task-create-assignee"
                  onChange={(event) => setTaskForm({ ...taskForm, assignee_member_id: event.target.value })}
                >
                  <option value="">未指派（先到先得）</option>
                  {(overview?.members ?? []).map((member) => (
                    <option key={member.member_id} value={member.member_id}>
                      {member.display_name}（{ROLE_LABEL[member.role] ?? member.role}）
                    </option>
                  ))}
                </select>
              </label>
              <label className="field" style={{ flex: "1 1 170px" }}>
                <span>截止时间（可留空）</span>
                <input
                  type="datetime-local"
                  value={taskForm.deadline}
                  data-testid="task-create-deadline"
                  onChange={(event) => setTaskForm({ ...taskForm, deadline: event.target.value })}
                />
              </label>
            </div>
          </form>
        </Modal>
      ) : null}
    </div>
  );
}

/** 把 @成员 / /Agent 渲染成高亮（只认名单里存在的名字，避免误伤普通文本）。 */
function renderWithMentions(text: string, names: string[]) {
  if (!names.length) return text;
  const escaped = names
    .filter(Boolean)
    .sort((a, b) => b.length - a.length)
    .map((name) => name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (!escaped.length) return text;
  const pattern = new RegExp(`([@/])(${escaped.join("|")})`, "g");
  const parts: (string | ReactNode)[] = [];
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    const index = match.index ?? 0;
    if (index > last) parts.push(text.slice(last, index));
    parts.push(
      <span className="mention" key={`${index}-${match[0]}`}>
        {match[0]}
      </span>,
    );
    last = index + match[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

/** 一条聊天消息：人类发言是气泡；Agent/系统动作是卡片（可跳到对应对象）。 */
/** 从卡片文案里取成果物名：卡片形如「提交成果物「指南提纲.md」，等待复核」/「复核「终稿.md」：通过」。 */
function artifactNameFromContent(content: string): string {
  const matched = /「([^」]+)」/.exec(content);
  if (matched && /\.[a-z0-9]{2,5}$/i.test(matched[1])) return matched[1];
  return matched ? matched[1] : "成果物";
}

function ChatMessage({
  message,
  names,
  onOpenArtifact,
}: {
  message: ProjectMessage;
  names: string[];
  onOpenArtifact: (artifact: { id: string; name: string }) => void;
}) {
  const isHuman = message.sender_kind === "human";
  const time = formatTime(message.created_at);
  if (isHuman) {
    return (
      <div className="chat-row is-human" data-testid="chat-message">
        <div className="chat-bubble">
          <div className="chat-meta">
            <strong>{message.sender_name || "成员"}</strong>
            <span className="hint">{time}</span>
          </div>
          <div className="chat-text">{renderWithMentions(message.content, names)}</div>
          {message.ref_task_id || message.ref_artifact_id ? (
            <div className="chat-refs">
              {message.ref_task_id ? <Link className="inline-link" href="/tasks">查看任务</Link> : null}
              {message.ref_artifact_id ? (
                <button
                  type="button"
                  className="inline-link chat-file-button"
                  onClick={() => onOpenArtifact({ id: message.ref_artifact_id as string, name: message.content.replace(/^已上传「|」（.*$/g, "") || "成果物" })}
                  data-testid="chat-open-artifact"
                >
                  就地查看文件
                </button>
              ) : null}
            </div>
          ) : null}
        </div>
      </div>
    );
  }
  const isAgent = message.sender_kind === "agent";
  // 系统事件（建任务/复核/模式切换这类）不配占一个盒子：一行小字即可，群聊才不会被机器噪声淹没
  if (!isAgent) {
    return (
      <div className="chat-line" data-testid="chat-card">
        <span className="chat-line-text">{renderWithMentions(message.content, names)}</span>
        <span className="chat-line-time">{time}</span>
        {message.ref_task_id ? (
          <Link className="chat-line-ref" href="/tasks">任务</Link>
        ) : null}
        {message.ref_artifact_id ? (
          <button
            type="button"
            className="chat-line-ref chat-file-button"
            onClick={() => onOpenArtifact({ id: message.ref_artifact_id as string, name: artifactNameFromContent(message.content) })}
            data-testid="chat-open-artifact"
          >
            成果物
          </button>
        ) : null}
      </div>
    );
  }
  return (
    <div className="chat-row" data-testid="chat-card">
      <div className="chat-card is-agent">
        <div className="chat-meta">
          <span className="card-badge is-agent">
            <Bot size={11} />
            {message.sender_name || message.sender_agent_id || "Agent"}
          </span>
          <span className="hint">{time}</span>
        </div>
        <div className="chat-text">{renderWithMentions(message.content, names)}</div>
        {message.ref_task_id || message.ref_artifact_id ? (
          <div className="chat-refs">
            {message.ref_task_id ? <Link className="inline-link" href="/tasks">查看任务</Link> : null}
            {message.ref_artifact_id ? (
              <button
                type="button"
                className="inline-link chat-file-button"
                onClick={() => onOpenArtifact({ id: message.ref_artifact_id as string, name: artifactNameFromContent(message.content) })}
                data-testid="chat-open-artifact"
              >
                就地查看
              </button>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}