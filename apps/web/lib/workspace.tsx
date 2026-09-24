"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import {
  Dashboard,
  PackValidationReport,
  Project,
  ProjectMessage,
  ProjectCompetitionPack,
  ReviewCenter,
  getDashboard,
  getProjectCompetitionPack,
  getProjects,
  getReviewCenter,
  projectSocketUrl,
} from "./api";
import { useAuth } from "./auth";

/** 全站共享：项目选择、看板/审核/模板包数据、连接状态与提示。 */

type WorkspaceState = {
  projects: Project[];
  project: Project | null;
  projectId: string;
  dashboard: Dashboard;
  reviewCenter: ReviewCenter;
  packState: ProjectCompetitionPack | null;
  packReport: PackValidationReport | null;
  loading: boolean;
  connected: boolean;
  error: string | null;
  toast: string;
  setPackReport: (report: PackValidationReport | null) => void;
  notify: (message: string, durationMs?: number) => void;
  selectProject: (projectId: string) => void;
  refresh: (preferredProjectId?: string) => Promise<void>;
  /** 订阅项目聊天流：WS 上收到的新消息（含连上时的历史帧）。返回退订函数。 */
  subscribeMessages: (listener: (messages: ProjectMessage[]) => void) => () => void;
};

const emptyDashboard: Dashboard = {
  project: { id: "", name: "未连接项目", competition_pack: "", problem_code: null, description: "", stage: "problem_intake", progress: 0, created_at: "", updated_at: "" },
  tasks: [],
  handoffs: [],
  artifacts: [],
  agents: [],
  events: [],
  runs: [],
  metrics: {},
};

const emptyReviewCenter: ReviewCenter = { gates: [], reviews: [], evidence: [], risks: [], handoffs: [] };

/** 项目选择的持久化键：刷新后回到上次的项目，而不是列表第一项。 */
const SELECTED_PROJECT_KEY = "map.selectedProjectId";

function readStoredProjectId(): string {
  if (typeof window === "undefined") return "";
  try {
    return window.localStorage.getItem(SELECTED_PROJECT_KEY) ?? "";
  } catch {
    return "";
  }
}

function persistProjectId(projectId: string): void {
  if (typeof window === "undefined") return;
  try {
    if (projectId) window.localStorage.setItem(SELECTED_PROJECT_KEY, projectId);
    else window.localStorage.removeItem(SELECTED_PROJECT_KEY);
  } catch {
    // 隐私模式下 localStorage 不可写：选择仍在本会话内生效，不阻断使用。
  }
}

const WorkspaceContext = createContext<WorkspaceState | null>(null);

export function WorkspaceProvider({ children }: { children: React.ReactNode }) {
  const { ready, authenticated } = useAuth();
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [dashboard, setDashboard] = useState<Dashboard>(emptyDashboard);
  const [reviewCenter, setReviewCenter] = useState<ReviewCenter>(emptyReviewCenter);
  const [packState, setPackState] = useState<ProjectCompetitionPack | null>(null);
  const [packReport, setPackReport] = useState<PackValidationReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState("");

  // 聊天流订阅：工作区页面注册回调，WS 帧里的消息直接分发过去。
  // 用一个 ref 里的 Set，避免每来一条消息就重建 WS 连接。
  const messageListeners = useRef(new Set<(messages: ProjectMessage[]) => void>());

  const subscribeMessages = useCallback((listener: (messages: ProjectMessage[]) => void) => {
    messageListeners.current.add(listener);
    return () => {
      messageListeners.current.delete(listener);
    };
  }, []);

  const notify = useCallback((message: string, durationMs = 3200) => {
    setToast(message);
    if (typeof window !== "undefined") {
      window.setTimeout(() => setToast((current) => (current === message ? "" : current)), durationMs);
    }
  }, []);

  const refresh = useCallback(async (preferredProjectId?: string) => {
    try {
      const list = await getProjects();
      setProjects(list);
      // 优先级：显式首选（如刚新建的项目）> 当前选中 > 上次持久化 > 最近更新的项目。
      // 显式首选必须排在当前选中之前：refresh 的闭包里可能还是切换前的旧 projectId。
      const candidates = [preferredProjectId, projectId, readStoredProjectId()].filter(Boolean) as string[];
      const alive = candidates.filter((id) => list.some((item) => item.id === id));
      const dropped = candidates.length > 0 && alive.length === 0 && list.length > 0;
      const id = alive[0] ?? list[0]?.id ?? "";
      if (dropped) notify("上次选中的项目已不存在，已切换到最近更新的项目");
      if (id) {
        setProjectId(id);
        persistProjectId(id);
        const [nextDashboard, nextReview, nextPack] = await Promise.all([
          getDashboard(id),
          getReviewCenter(id),
          getProjectCompetitionPack(id).catch(() => null),
        ]);
        setDashboard(nextDashboard);
        setReviewCenter(nextReview);
        setPackState(nextPack);
      } else {
        // 一个项目都没有：清空选中，让界面走"新建项目"空状态引导。
        setProjectId("");
        persistProjectId("");
        setPackState(null);
      }
      setConnected(true);
      setError(null);
    } catch (failure) {
      setConnected(false);
      setError(failure instanceof Error ? failure.message : "API 未连接");
    } finally {
      setLoading(false);
    }
  }, [notify, projectId]);

  useEffect(() => {
    // 未登录（或还没确认会话状态）时不发业务请求：否则每个页面都会打出一串 401
    if (!ready || !authenticated) return;
    void refresh();
    // 仅在会话就绪时拉取一次项目列表。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, authenticated]);

  useEffect(() => {
    if (!projectId || !authenticated || typeof window === "undefined") return;
    const socket = new WebSocket(projectSocketUrl(projectId));
    socket.onopen = () => setConnected(true);
    socket.onclose = () => setConnected(false);
    socket.onmessage = (event: MessageEvent) => {
      let frame: { type?: string; message?: ProjectMessage; messages?: ProjectMessage[] } | null = null;
      try {
        frame = JSON.parse(String(event.data));
      } catch {
        frame = null;
      }
      // 聊天消息只分发给订阅者：如果每条消息都触发一次整盘刷新，
      // 群里一活跃界面就会不停重取看板（这也是刷新抖动的一个来源）。
      if (frame?.type === "project.message" && frame.message) {
        for (const listener of messageListeners.current) listener([frame.message]);
        return;
      }
      if (frame?.type === "connected") {
        if (Array.isArray(frame.messages)) {
          for (const listener of messageListeners.current) listener(frame.messages as ProjectMessage[]);
        }
        void refresh();
        return;
      }
      void refresh();
    };
    return () => socket.close();
  }, [projectId, authenticated, refresh]);

  const selectProject = useCallback((nextId: string) => {
    setProjectId(nextId);
    persistProjectId(nextId);
    setPackReport(null);
  }, []);

  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    void (async () => {
      try {
        const [nextDashboard, nextReview, nextPack] = await Promise.all([
          getDashboard(projectId),
          getReviewCenter(projectId),
          getProjectCompetitionPack(projectId).catch(() => null),
        ]);
        if (cancelled) return;
        setDashboard(nextDashboard);
        setReviewCenter(nextReview);
        setPackState(nextPack);
      } catch (failure) {
        if (!cancelled) setError(failure instanceof Error ? failure.message : "数据读取失败");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  const value = useMemo<WorkspaceState>(
    () => ({
      projects,
      project: projects.find((item) => item.id === projectId) ?? null,
      projectId,
      dashboard,
      reviewCenter,
      packState,
      packReport,
      loading,
      connected,
      error,
      toast,
      setPackReport,
      notify,
      selectProject,
      refresh,
      subscribeMessages,
    }),
    [projects, projectId, dashboard, reviewCenter, packState, packReport, loading, connected, error, toast, notify, selectProject, refresh, subscribeMessages],
  );

  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
}

export function useWorkspace(): WorkspaceState {
  const context = useContext(WorkspaceContext);
  if (!context) throw new Error("useWorkspace 必须在 WorkspaceProvider 内使用");
  return context;
}