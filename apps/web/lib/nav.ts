import type { LucideIcon } from "lucide-react";
import {
  Activity,
  Box,
  ClipboardCheck,
  Cloud,
  Database,
  FileCheck2,
  FileText,
  Folder,
  Inbox,
  KeyRound,
  Layers3,
  ListChecks,
  MessagesSquare,
  Network,
  Send,
  Server,
  Settings2,
  ShieldCheck,
  Sparkles,
  TerminalSquare,
  UserRound,
  Users,
  Workflow,
  Wrench,
} from "lucide-react";

/**
 * 导航与页面元数据的唯一来源（六主导航版）。
 *
 * 侧边栏是 6 个一级 Hub（可点、可高亮、可带角标），每个 Hub 内的二级页面以 Tab 形式
 * 合并进 Hub 页（components/hub-tabs.tsx + components/hub-view.tsx），老路由全部保留
 * （只隐藏不删除）：深链、Ctrl+K 搜索、页面间跳转都继续可用。
 *
 * 单一来源约定：
 * - 侧边栏（shell.tsx）从 HUBS 读一级入口与二级菜单；
 * - 移动端底部标签栏（mobile-tabs.tsx）从 MOBILE_TAB_HREFS 读（前 4 + 更多）；
 * - Hub 页内的 Tab 条（hub-tabs.tsx）从 hubTabsOf() 读；
 * - 快速跳转（command-palette.tsx）从 searchNav 读全部条目；
 * - 面包屑（pageMeta）与测试 id 也从这里派生。
 */

/** 需要动态数据的角标：具体数值由 shell 用工作区数据算，nav 只声明"这里有提醒"。 */
export type NavBadge = "tasks" | "pack" | "review" | "projects";

/** 一条导航入口：既是一级 Hub，也可能是某个 Hub 里的二级 Tab。 */
export type NavItem = {
  href: string;
  label: string;
  icon: LucideIcon;
  testId: string;
  /** 一句话说明：跳转面板里显示，同时参与搜索。 */
  hint: string;
  badge?: NavBadge;
  /** 搜索别名：中英文口语说法，解决"我记得有这个页但想不起叫什么"。 */
  keywords: string[];
};

/** 一级 Hub：侧边栏的 6 行 + Hub 页的 Tab 条来源。 */
export type NavHub = NavItem & {
  /** Hub 内的二级 Tab（不含 Hub 自己的主 Tab；空数组 = 单页 Hub）。 */
  children: NavItem[];
};

/* ------------------------------------------------------------------ */
/* 二级入口（收进 Hub 的页面，路由保持原样）                            */
/* ------------------------------------------------------------------ */

const OVERVIEW: NavItem = {
  href: "/",
  label: "项目总览",
  icon: Layers3,
  testId: "nav-overview",
  hint: "队伍、Agent 与门禁的当前状态（已收进工作区「概览」Tab）",
  keywords: ["首页", "看板", "dashboard", "home", "概览", "进度", "总览"],
};

const MY_TASKS: NavItem = {
  href: "/my-tasks",
  label: "我的任务",
  icon: ListChecks,
  testId: "nav-my-tasks",
  hint: "派给我的活、我的 Agent 在执行与最近完成",
  keywords: ["我的任务", "指派", "派单", "个人", "待办", "我的 agent", "my"],
};

const REVIEW: NavItem = {
  href: "/review",
  label: "审核门禁",
  icon: ShieldCheck,
  testId: "nav-reviews",
  hint: "门禁、复核意见、风险登记与交接收据",
  badge: "review",
  keywords: ["审核", "门禁", "批准", "复核", "风险", "gate", "把关"],
};

const HANDOFFS: NavItem = {
  href: "/handoffs",
  label: "交接中心",
  icon: Inbox,
  testId: "nav-handoffs",
  hint: "接力与分发交接及其收据",
  keywords: ["交接", "接力", "分发", "收据", "handoff"],
};

const TEAM: NavItem = {
  href: "/team",
  label: "团队与成员",
  icon: Users,
  testId: "nav-team",
  hint: "成员工作量、项目成员管理与团队",
  keywords: ["团队", "成员", "工作量", "谁在忙", "角色", "邀请", "队伍"],
};

const CHANNELS: NavItem = {
  href: "/channels",
  label: "LLM 渠道",
  icon: KeyRound,
  testId: "nav-channels",
  hint: "管理员录入 OpenAI 兼容上游，全员共享免费额度",
  keywords: ["渠道", "llm", "模型", "免费额度", "管理员", "代理", "测速", "channel"],
};

const ACCOUNT: NavItem = {
  href: "/account",
  label: "账号设置",
  icon: UserRound,
  testId: "nav-account",
  hint: "账号信息、密码与（管理员）全站账号和邀请码",
  keywords: ["账号", "密码", "邀请码", "管理员", "个人", "account"],
};

const ARTIFACTS: NavItem = {
  href: "/artifacts",
  label: "成果物库",
  icon: Box,
  testId: "nav-artifacts",
  hint: "版本、状态与来源保持可追溯（已收进文档库「成果物」Tab）",
  keywords: ["成果物", "产物", "版本", "下载", "归档", "artifact", "血缘"],
};

const DRIVE: NavItem = {
  href: "/drive",
  label: "个人云盘",
  icon: Cloud,
  testId: "nav-drive",
  hint: "私人文件管理器（上传/文件夹/回收站/授权）并加入项目",
  keywords: ["云盘", "网盘", "文件", "上传", "暂存", "附件", "文件夹", "回收站"],
};

const PACK: NavItem = {
  href: "/pack",
  label: "建模模板包",
  icon: FileCheck2,
  testId: "nav-pack",
  hint: "首个垂直工作流包（数学建模）：物化四问流程与模板骨架",
  badge: "pack",
  keywords: ["模板", "工作流", "领域包", "cumcm", "物化", "校验", "题号", "问题一"],
};

const DELIVERY: NavItem = {
  href: "/delivery",
  label: "论文交付",
  icon: Send,
  testId: "nav-delivery",
  hint: "装配、检查、编译、提交包与跨部署校验",
  keywords: ["交付", "论文", "编译", "pdf", "latex", "提交包", "答辩", "就绪度"],
};

const KB: NavItem = {
  href: "/kb",
  label: "知识库",
  icon: Database,
  testId: "nav-kb",
  hint: "文档登记、索引构建与检索状态",
  keywords: ["知识库", "索引", "检索", "mineru", "rag", "资料"],
};

const GRAPH: NavItem = {
  href: "/graph",
  label: "图谱可视化",
  icon: Network,
  testId: "nav-graph",
  hint: "实体邻居子图与向量库浏览（只读）",
  keywords: ["图谱", "图", "实体", "关系", "可视化", "graph", "hypergraph"],
};

const RUNS: NavItem = {
  href: "/runs",
  label: "运行控制台",
  icon: TerminalSquare,
  testId: "nav-runs",
  hint: "运行、设备与 Agent 编队",
  keywords: ["运行", "日志", "控制台", "输出", "run", "执行过程"],
};

const TIMELINE: NavItem = {
  href: "/timeline",
  label: "项目时间线",
  icon: Activity,
  testId: "nav-timeline",
  hint: "事件流审计",
  keywords: ["时间线", "事件", "审计", "记录", "历史", "timeline"],
};

const DEVICES: NavItem = {
  href: "/devices",
  label: "设备与接入",
  icon: Server,
  testId: "nav-devices",
  hint: "配对向导、设备身份与 Token 轮换——没有执行体时从这里接入",
  keywords: ["设备", "接入", "配对", "令牌", "token", "agent", "授权", "安装"],
};

/* ------------------------------------------------------------------ */
/* 六个一级 Hub（侧边栏顺序即此数组顺序）                               */
/* ------------------------------------------------------------------ */

export const HUBS: NavHub[] = [
  {
    href: "/my-agent",
    label: "我的智能体",
    icon: Sparkles,
    testId: "nav-my-agent",
    hint: "Agent 工作台：像主流 Agent 产品一样对话、传文件、看流式过程（单 Agent 主入口）",
    keywords: ["我的智能体", "工作台", "对话", "聊天", "chat", "agent", "opencode", "模型", "智能体"],
    children: [DEVICES],
  },
  {
    href: "/workspace",
    label: "项目协作",
    icon: MessagesSquare,
    testId: "nav-workspace",
    hint: "群聊 + 任务 + 团队 + 生产链 + 成果 + 概览的项目主工作区",
    badge: "projects",
    keywords: ["工作区", "群聊", "聊天", "协作", "成员", "workspace", "chat", "主界面", "项目总览", "概览"],
    children: [OVERVIEW, MY_TASKS, REVIEW, HANDOFFS],
  },
  {
    href: "/tasks",
    label: "任务与流程",
    icon: ClipboardCheck,
    testId: "nav-tasks",
    hint: "任务列表、依赖诊断与执行方式设置",
    badge: "tasks",
    keywords: ["任务", "待办", "流程", "依赖", "开始", "执行", "codex", "工单", "我的任务"],
    children: [MY_TASKS, RUNS],
  },
  {
    href: "/documents",
    label: "文档与成果",
    icon: FileText,
    testId: "nav-documents",
    hint: "文档版本 + 成果物库 + 个人云盘：内容的家",
    keywords: ["文档", "论文", "草稿", "编辑", "协作", "版本", "tex", "markdown", "成果物", "云盘", "文件"],
    children: [ARTIFACTS, DRIVE],
  },
  {
    href: "/workflows",
    label: "工作流",
    icon: Workflow,
    testId: "nav-workflows",
    hint: "通用工作流包：内置包安装、定义编辑、应用到项目物化任务骨架",
    keywords: ["工作流", "包", "workflow", "编排", "内置包", "模板", "阶段", "节点", "建模模板包", "论文交付", "知识库", "图谱"],
    children: [PACK, DELIVERY, KB, GRAPH],
  },
  {
    href: "/settings",
    label: "设置",
    icon: Settings2,
    testId: "nav-settings",
    hint: "空间凭据与配额 + 团队成员 + LLM 渠道 + 账号",
    keywords: ["设置", "凭据", "密钥", "api key", "llm", "embedding", "mineru", "配额", "团队", "成员", "渠道", "账号", "邀请码"],
    children: [TEAM, CHANNELS, ACCOUNT],
  },
];

/** 二级条目清单（去重后）：Ctrl+K 与面包屑用。 */
export const SECONDARY_ITEMS: NavItem[] = (() => {
  const seen = new Set<string>();
  const items: NavItem[] = [];
  for (const hub of HUBS) {
    for (const child of hub.children) {
      if (!seen.has(child.href)) {
        seen.add(child.href);
        items.push(child);
      }
    }
  }
  return items;
})();

/**
 * 给定路由找到它所属的 Hub 与（若命中二级条目）对应 Tab：
 * 侧边栏一级高亮与 Hub 页 Tab 高亮都从这里取。
 */
export function findNavLocation(pathname: string): { hub: NavHub; item?: NavItem } | undefined {
  const hub = HUBS.find((entry) => entry.href === pathname);
  if (hub) return { hub };
  for (const entry of HUBS) {
    const child = entry.children.find((item) => item.href === pathname);
    if (child) return { hub: entry, item: child };
  }
  return undefined;
}

/** 兼容旧调用点（ask 等页面、可能的深链）：仍然能查到任意入口与所在分组。 */
export function findNavItem(pathname: string): { item: NavItem; section: NavHub } | undefined {
  const location = findNavLocation(pathname);
  if (!location) return undefined;
  return { item: location.item ?? location.hub, section: location.hub };
}

/** Hub 页的 Tab 条（hub-tabs.tsx 用）：主 Tab 是 Hub 自己，后面是二级 Tab。 */
export function hubTabsOf(hubHref: string): NavItem[] {
  const hub = HUBS.find((entry) => entry.href === hubHref);
  if (!hub) return [];
  const self: NavItem = {
    href: hub.href,
    label: hub.label,
    icon: hub.icon,
    testId: hub.testId,
    hint: hub.hint,
    badge: hub.badge,
    keywords: hub.keywords,
  };
  return [self, ...hub.children];
}

/** 移动端底部标签栏：4 个常用 + 「更多」（第 5 格打开抽屉）。 */
export const MOBILE_TAB_HREFS = ["/workspace", "/tasks", "/my-agent", "/documents"] as const;

/** 折叠状态的持久化键（用户手动开合过的分组）。 */
export const NAV_STORAGE_KEY = "math-agent-platform.nav.open-hubs";

/** 页面标题/副标题：与导航标签解耦（标题更完整，导航标签更短）。 */
const PAGE_TITLES: Record<string, { title: string; subtitle: string }> = {
  "/workspace": { title: "项目协作", subtitle: "群聊、任务、团队与成果空间——项目的主工作区域" },
  "/": { title: "项目总览", subtitle: "队伍、Agent 与门禁的当前状态" },
  "/tasks": { title: "任务与流程", subtitle: "按阶段查看待办、依赖与负责人" },
  "/my-tasks": { title: "我的任务", subtitle: "指派给我的、我的 Agent 在执行与最近完成的" },
  "/pack": { title: "建模模板包", subtitle: "首个垂直工作流包（数学建模）：物化任务与成果骨架——骨架不是最终结果" },
  "/documents": { title: "文档与成果", subtitle: "文档版本、成果物库与个人云盘——项目内容的家" },
  "/kb": { title: "知识库", subtitle: "文档登记、索引构建与检索状态" },
  "/graph": { title: "图谱可视化", subtitle: "实体邻居子图与向量库浏览（只读）" },
  "/my-agent": { title: "我的智能体", subtitle: "Agent 工作台：像主流 Agent 产品一样直接对话、传文件、看流式过程" },
  "/ask": { title: "AI 问答（旧入口）", subtitle: "普通对话与知识库检索问答——已在导航中由「我的智能体」取代" },
  "/review": { title: "审核门禁", subtitle: "门禁、复核意见、风险登记与交接收据" },
  "/delivery": { title: "论文交付", subtitle: "装配、检查、编译、提交包与跨部署校验" },
  "/drive": { title: "个人云盘", subtitle: "私人文件暂存（200MB）并一键加入项目空间" },
  "/artifacts": { title: "成果物库", subtitle: "版本、状态与来源保持可追溯" },
  "/handoffs": { title: "交接中心", subtitle: "接力与分发交接及其收据" },
  "/runs": { title: "运行控制台", subtitle: "运行、设备与 Agent 编队" },
  "/devices": { title: "设备与接入", subtitle: "配对向导、设备身份与 Token 轮换" },
  "/timeline": { title: "项目时间线", subtitle: "事件流审计" },
  "/team": { title: "团队与成员", subtitle: "成员工作量、项目成员管理与团队" },
  "/settings": { title: "空间设置", subtitle: "平台 API 凭据（LLM / Embedding / MinerU）、团队、渠道与账号" },
  "/channels": { title: "LLM 渠道", subtitle: "平台代管的 OpenAI 兼容上游与全员免费额度" },
  "/account": { title: "账号设置", subtitle: "账号信息、密码与（管理员）全站账号和邀请码" },
  "/workflows": { title: "工作流", subtitle: "通用工作流包：定义阶段、角色、门禁与交付，应用到项目物化任务骨架" },
};

/** 面包屑分组文案：一级 Hub 名。 */
export function pageMeta(pathname: string): { title: string; subtitle: string; group: string } {
  const location = findNavLocation(pathname);
  const titles = PAGE_TITLES[pathname];
  return {
    title: titles?.title ?? location?.item?.label ?? location?.hub.label ?? "工作台",
    subtitle: titles?.subtitle ?? "",
    group: location?.hub.label ?? "工作台",
  };
}

/** 快速跳转面板的搜索：空查询返回全部（Hub 在前、二级在后），多词空格分隔取交集。 */
export function searchNav(query: string): { item: NavItem; section: NavHub }[] {
  const tokens = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const all: { item: NavItem; section: NavHub }[] = [
    ...HUBS.map((hub) => ({ item: hub as NavItem, section: hub })),
    ...SECONDARY_ITEMS.map((item) => ({
      item,
      section: HUBS.find((hub) => hub.href === findNavLocation(item.href)?.hub.href) as NavHub,
    })),
  ];
  if (!tokens.length) return all;
  const scored = all
    .map((entry) => {
      const label = entry.item.label.toLowerCase();
      const haystack = [label, entry.section.label, entry.item.hint, entry.item.href, ...entry.item.keywords]
        .join(" ")
        .toLowerCase();
      let score = 0;
      for (const token of tokens) {
        if (!haystack.includes(token)) return null;
        if (label.startsWith(token)) score += 0;
        else if (label.includes(token)) score += 1;
        else if (entry.item.keywords.some((word) => word.toLowerCase().includes(token))) score += 2;
        else score += 3;
      }
      return { ...entry, score };
    })
    .filter((entry): entry is { item: NavItem; section: NavHub; score: number } => entry !== null);
  return scored.sort((a, b) => a.score - b.score).map(({ item, section }) => ({ item, section }));
}

/** 侧边栏「设备与接入」等在视觉上归为工具类时的图标补充（避免各页自行 import）。 */
export const HUB_TOOL_ICON = Wrench;
export const HUB_FOLDER_ICON = Folder;
