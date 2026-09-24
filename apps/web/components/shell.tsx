"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  ChevronRight,
  FolderPlus,
  GitBranch,
  LogOut,
  Menu,
  Moon,
  Network,
  RefreshCcw,
  Search,
  Settings2,
  Sun,
  UserRound,
  X,
} from "lucide-react";
import { NAV_SECTIONS, NAV_STORAGE_KEY, findSection, pageMeta, type NavBadge, type NavItem, type NavSection } from "../lib/nav";
import { useWorkspace } from "../lib/workspace";
import { useAuth } from "../lib/auth";
import { useTheme } from "../lib/theme";
import { CreateProjectModal } from "./project-create";
import { QuickJump } from "./command-palette";
import { MobileTabs } from "./mobile-tabs";
import { LocalAgentPanel } from "./local-agent-panel";
import { desktopShell } from "../lib/desktop";
import { useDesktopAttention } from "../lib/desktop-attention";

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { projects, projectId, project, selectProject, connected, refresh, reviewCenter, dashboard, packState, toast } = useWorkspace();
  const { account, authenticated, ready, logout } = useAuth();
  const { theme, toggle } = useTheme();
  // 桌面端专属：本机 Agent 面板（浏览器里没有桥 → 入口不出现）
  const shell = desktopShell();
  const [kernelOpen, setKernelOpen] = useState(false);
  // 桌面通知（DESKTOP-NOTIFY）：登录后把会话交给壳后台轮询"派给我的任务 / 待我复核"；
  // 浏览器里没桥 → hook 是空操作。
  useDesktopAttention({
    ready,
    authenticated,
    memberId: account?.member.id,
    events: dashboard.events,
  });
  const [mobileNav, setMobileNav] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [jumpOpen, setJumpOpen] = useState(false);
  const [accountMenu, setAccountMenu] = useState(false);
  const [openSections, setOpenSections] = useState<Record<string, boolean>>({});

  // 登录/注册页不套工作台外壳：否则会先渲染侧栏、并立刻发起业务请求（未登录必然 401）
  const bareRoute = pathname === "/login" || pathname === "/register";

  // 未登录（且已确认过会话状态）→ 回登录页；带上原地址，登录后跳回
  useEffect(() => {
    if (bareRoute || !ready || authenticated) return;
    const target = pathname && pathname !== "/" ? `?next=${encodeURIComponent(pathname)}` : "";
    router.replace(`/login${target}`);
  }, [bareRoute, ready, authenticated, pathname, router]);

  // 折叠状态从 localStorage 读（首帧不读，保证每个路由的静态 HTML 一致）。
  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(NAV_STORAGE_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === "object") setOpenSections(parsed as Record<string, boolean>);
      }
    } catch {
      /* 隐私模式下 localStorage 不可用：忽略，退回默认展开规则。 */
    }
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setJumpOpen((open) => !open);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  // 抽屉打开时：Esc 关闭 + 锁住背景滚动（否则手指滑动会带着后面的页面一起滚）
  useEffect(() => {
    if (!shell) return;
    shell.onOpenKernelPanel(() => setKernelOpen(true));
    // 面板订阅只在挂载时建立一次：桥是稳定的（preload 注入）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [Boolean(shell)]);

  useEffect(() => {
    if (!mobileNav) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMobileNav(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previous;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [mobileNav]);

  const toggleSection = useCallback((id: string, next: boolean) => {
    setOpenSections((previous) => {
      const merged = { ...previous, [id]: next };
      try {
        window.localStorage.setItem(NAV_STORAGE_KEY, JSON.stringify(merged));
      } catch {
        /* 写不进去不影响本次会话内的展开状态。 */
      }
      return merged;
    });
  }, []);

  const pendingReview = useMemo(
    () => reviewCenter.gates.filter((gate) => gate.status !== "PASSED").length,
    [reviewCenter.gates],
  );
  const taskAttention = useMemo(
    () => dashboard.tasks.filter((task) => ["BLOCKED", "NEEDS_REVISION"].includes(String(task.status))).length,
    [dashboard.tasks],
  );

  /** 角标数值：把"哪一页需要看"和"工作区数据"接起来。 */
  const badgeValue = useCallback(
    (badge: NavBadge): number => {
      if (badge === "tasks") return taskAttention;
      if (badge === "review") return pendingReview;
      if (badge === "projects") return projects.length;
      if (badge === "pack") {
        const materialization = packState?.materialization;
        if (!materialization) return 0;
        const missing = materialization.missing_tasks.length + materialization.missing_artifacts.length;
        return materialization.materialized && missing === 0 ? 0 : Math.max(missing, 1);
      }
      return 0;
    },
    [taskAttention, pendingReview, projects.length, packState],
  );

  const activeSection = findSection(pathname);
  const onlineAgents = dashboard.agents.filter((agent) => String(agent.status) === "online").length;
  const latestSeen = dashboard.agents[0]?.last_seen ?? "";

  const sectionOpen = (section: NavSection) =>
    section.items.length === 1 ? true : openSections[section.id] ?? section.id === activeSection?.id;

  const renderItem = (item: NavItem, nested: boolean) => {
    const active = pathname === item.href;
    const value = item.badge ? badgeValue(item.badge) : 0;
    return (
      <Link
        key={item.href}
        href={item.href}
        className={`nav-item ${active ? "nav-item-active" : ""} ${nested ? "nav-item-child" : ""}`.trim()}
        data-testid={item.testId}
        title={item.hint}
        onClick={() => setMobileNav(false)}
      >
        <item.icon size={nested ? 15 : 17} />
        <span>{item.label}</span>
        {item.badge === "projects"
          ? value > 0 && <span className="nav-count">{value}</span>
          : value > 0 && <span className="nav-dot" />}
      </Link>
    );
  };

  if (bareRoute) {
    return (
      <>
        {children}
        {toast && <div className="toast" role="status" data-testid="toast">{toast}</div>}
      </>
    );
  }

  return (
    <div className="app-shell">
      {mobileNav && <div className="nav-scrim" role="presentation" data-testid="nav-scrim" onClick={() => setMobileNav(false)} />}
      <aside className={`sidebar ${mobileNav ? "sidebar-open" : ""}`.trim()}>
        <div className="brand-row">
          <div className="brand-mark"><Network size={18} /></div>
          <span className="brand-name">synapforge</span>
          <span className="mobile-only" style={{ marginLeft: "auto" }}>
            <button className="app-icon" aria-label="关闭导航" onClick={() => setMobileNav(false)}><X size={17} /></button>
          </span>
        </div>
        <button className="nav-jump" data-testid="quick-jump-sidebar" onClick={() => { setMobileNav(false); setJumpOpen(true); }}>
          <Search size={15} />
          <span>搜索页面</span>
          <kbd>Ctrl K</kbd>
        </button>
        <div className="workspace-switcher">
          <div className="workspace-avatar">C</div>
          <div className="workspace-copy">
            <span>{project ? project.name : "未选择项目"}</span>
            <small>{project ? `${project.competition_pack} · ${project.problem_code ?? "—"} 题` : "还没有项目，先创建一个"}</small>
          </div>
          <button className="app-icon" aria-label="新建项目" title="新建项目" data-testid="sidebar-new-project" onClick={() => { setMobileNav(false); setCreateOpen(true); }}>
            <FolderPlus size={16} />
          </button>
        </div>
        <nav className="primary-nav">
          {NAV_SECTIONS.map((section) => {
            const single = section.items.length === 1;
            const open = sectionOpen(section);
            const isActiveSection = section.id === activeSection?.id;
            const sectionAlert = section.items.reduce(
              (sum, item) => sum + (item.badge && item.badge !== "projects" && badgeValue(item.badge) > 0 ? 1 : 0),
              0,
            );
            return (
              <div className="nav-section" key={section.id}>
                {single ? (
                  renderItem(section.items[0], false)
                ) : (
                  <>
                    <button
                      type="button"
                      className={`nav-group-button ${isActiveSection ? "is-active" : ""}`.trim()}
                      onClick={() => toggleSection(section.id, !open)}
                      aria-expanded={open}
                      aria-controls={`nav-children-${section.id}`}
                      title={`${section.label} · ${section.hint}`}
                      data-testid={`nav-group-${section.id}`}
                    >
                      <section.icon size={17} />
                      <span>{section.label}</span>
                      {sectionAlert > 0 && <span className="nav-dot" />}
                      {!open && <span className="nav-count nav-count-plain">{section.items.length}</span>}
                      <ChevronRight size={14} className={`nav-group-chevron ${open ? "is-open" : ""}`.trim()} />
                    </button>
                    <div className={`nav-children ${open ? "is-open" : ""}`.trim()} id={`nav-children-${section.id}`}>
                      {section.items.map((item) => renderItem(item, true))}
                    </div>
                  </>
                )}
              </div>
            );
          })}
        </nav>
        <div className="sidebar-bottom">
          {/* 手机上顶栏的头像菜单是隐藏的（窄屏放不下），所以抽屉里必须也有一份账号入口 */}
          <div className="sidebar-account">
            <Link href="/account" className="sidebar-account-item" onClick={() => setMobileNav(false)} data-testid="sidebar-account">
              <UserRound size={15} />
              <span>{account?.member.display_name ?? "账号设置"}</span>
              {account?.is_admin && <span className="badge status-violet">管理员</span>}
            </Link>
            <button
              type="button"
              className="sidebar-account-item"
              data-testid="sidebar-logout"
              onClick={() => {
                setMobileNav(false);
                void logout();
              }}
            >
              <LogOut size={15} />
              <span>退出登录</span>
            </button>
          </div>
          <div className="agent-health">
            <span className={`pulse-dot ${onlineAgents ? "" : "pulse-dot-offline"}`} />
            <div>
              <strong>{onlineAgents} 个 Agent 在线</strong>
              <small>{latestSeen ? `最近同步于 ${formatTime(latestSeen)}` : "等待 Agent 接入"}</small>
            </div>
          </div>
        </div>
      </aside>

      <main className="main-area">
        <header className="topbar">
          <div className="topbar-left">
            <span className="mobile-only">
              <button className="app-icon" aria-label="打开导航" onClick={() => setMobileNav(true)}><Menu size={18} /></button>
            </span>
            <div className="breadcrumbs">
              <span>{pageMeta(pathname).group}</span>
              <ChevronRight size={13} />
              <strong>{pageMeta(pathname).title}</strong>
              <span>/</span>
              <span>{project?.name ?? "未连接"}</span>
            </div>
          </div>
          <div className="topbar-actions">
            <button
              className="button button-secondary quick-jump"
              data-testid="quick-jump"
              onClick={() => setJumpOpen(true)}
              title="搜索并跳转到任意页面（Ctrl + K）"
            >
              <Search size={15} />
              <span className="quick-jump-label">快速跳转</span>
              <kbd>Ctrl K</kbd>
            </button>
            {projects.length > 0 && (
              <select
                aria-label="切换项目"
                className="project-switcher"
                value={projectId}
                onChange={(event) => selectProject(event.target.value)}
                data-testid="project-switcher"
              >
                {projects.map((item) => (
                  <option key={item.id} value={item.id}>{item.name}</option>
                ))}
              </select>
            )}
            <button className="app-icon" aria-label="新建项目" title="新建项目" data-testid="topbar-new-project" onClick={() => setCreateOpen(true)}>
              <FolderPlus size={17} />
            </button>
            <div className={`connection-state ${connected ? "is-connected" : ""}`}>
              <span className="connection-dot" />
              {connected ? "已同步" : "等待连接"}
            </div>
            {shell ? (
              <button
                className="app-icon"
                aria-label="本机 Agent"
                title="本机 Agent：这台机器在跑什么（桌面端）"
                data-testid="local-agent-toggle"
                onClick={() => setKernelOpen((open) => !open)}
              >
                <Network size={17} />
              </button>
            ) : null}
            <button className="app-icon" aria-label="切换主题" data-testid="theme-toggle" onClick={toggle} title={theme === "dark" ? "切换到白天" : "切换到黑夜"}>
              {theme === "dark" ? <Sun size={17} /> : <Moon size={17} />}
            </button>
            <button className="app-icon" aria-label="刷新数据" onClick={() => void refresh()}><RefreshCcw size={17} /></button>
            <div className="account-menu">
              <button
                className="profile-avatar"
                aria-label="账号菜单"
                aria-expanded={accountMenu}
                data-testid="account-menu"
                title={account ? `${account.member.display_name}（${account.member.email}）` : "账号"}
                onClick={() => setAccountMenu((open) => !open)}
              >
                {accountInitials(account?.member.display_name, account?.member.email)}
              </button>
              {accountMenu && (
                <>
                  <div className="account-menu-backdrop" role="presentation" onClick={() => setAccountMenu(false)} />
                  <div className="account-menu-panel" data-testid="account-menu-panel">
                    <div className="account-menu-head">
                      <strong>{account?.member.display_name ?? "未登录"}</strong>
                      <small>{account?.member.email ?? ""}</small>
                      {account?.is_admin && <span className="badge status-violet">管理员</span>}
                    </div>
                    <Link href="/account" className="account-menu-item" onClick={() => setAccountMenu(false)} data-testid="account-menu-settings">
                      <Settings2 size={15} /> 账号设置
                    </Link>
                    <button
                      type="button"
                      className="account-menu-item"
                      data-testid="account-menu-logout"
                      onClick={() => {
                        setAccountMenu(false);
                        void logout();
                      }}
                    >
                      <LogOut size={15} /> 退出登录
                    </button>
                  </div>
                </>
              )}
            </div>
          </div>
        </header>
        {children}
      </main>
      {/* 桌面端：本机 Agent 面板（读内核契约 v1，与服务器数据无关） */}
      <LocalAgentPanel open={kernelOpen} onClose={() => setKernelOpen(false)} />
      <MobileTabs badges={{ tasks: taskAttention, review: pendingReview }} onMore={() => setMobileNav(true)} />
      {toast && <div className="toast" role="status" data-testid="toast">{toast}</div>}
      {jumpOpen && <QuickJump open={jumpOpen} onClose={() => setJumpOpen(false)} />}
      {createOpen && <CreateProjectModal open={createOpen} onClose={() => setCreateOpen(false)} />}
    </div>
  );
}

export function PageHeading({
  actions,
  hint,
}: {
  actions?: React.ReactNode;
  hint?: React.ReactNode;
}) {
  const pathname = usePathname();
  const meta = pageMeta(pathname);
  const { packState } = useWorkspace();
  return (
    <section className="page-heading">
      <div>
        <div className="eyebrow"><span className="eyebrow-dot" /> {packState ? `${packState.pack.display_name} · v${packState.pack.version}` : "synapforge · Agent 协作平台"}</div>
        <h1>{meta.title}</h1>
        <p>{hint ?? meta.subtitle}</p>
      </div>
      {actions && <div className="heading-actions">{actions}</div>}
    </section>
  );
}

export function GitBranchButton() {
  return (
    <button className="button button-secondary" type="button">
      <GitBranch size={16} /> 主分支
    </button>
  );
}

export function formatTime(value: string) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

/** 头像上的首字母：优先显示名，其次邮箱前缀（中文名取第一个字）。 */
function accountInitials(displayName?: string, email?: string): string {
  const source = (displayName || email || "?").trim();
  const first = Array.from(source)[0] ?? "?";
  return first.toUpperCase();
}