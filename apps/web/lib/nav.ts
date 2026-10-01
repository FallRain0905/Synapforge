import type { LucideIcon } from "lucide-react";
import {
  Activity,
  Box,
  ClipboardCheck,
  Cloud,
  Database,
  FileCheck2,
  FileText,
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
  Users,
  Workflow,
  Wrench,
} from "lucide-react";

/**
 * 导航与页面元数据的唯一来源：侧边栏、顶栏面包屑、快速跳转面板都从这里读。
 *
 * 产品重定位阶段 0（W0.2）起按三层产品结构分组：**工作台 / 项目协作 / 工作流工具**——
 * 单 Agent 工作台是基础入口，多 Agent 协作是核心差异，数学建模等是首个垂直工作流包；
 * 旧的六组分层（W-4 的"日常/偶尔"）由此取代。**只隐藏不删除**：路由、页面、
 * 深页内的互相跳转全部保持原样（/ask 已重定向进工作台，路由仍在），Ctrl+K 也能搜到。
 */
/** 需要动态数据的角标：具体数值由 shell 用工作区数据算，nav 只声明"这里有提醒"。 */
export type NavBadge = "tasks" | "pack" | "review" | "projects";

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

export type NavSection = {
  id: string;
  label: string;
  /** 折叠状态下悬停提示 + 跳转面板分组说明。 */
  hint: string;
  icon: LucideIcon;
  items: NavItem[];
};

export const NAV_SECTIONS: NavSection[] = [
  {
    id: "workbench",
    label: "工作台",
    hint: "单 Agent 工作台与执行体接入",
    icon: Sparkles,
    items: [
      {
        href: "/my-agent",
        label: "我的智能体",
        icon: Sparkles,
        testId: "nav-my-agent",
        hint: "Agent 工作台：像主流 Agent 产品一样对话、传文件、看流式过程（单 Agent 主入口）",
        badge: "projects",
        keywords: ["我的智能体", "工作台", "对话", "聊天", "chat", "agent", "opencode", "模型", "智能体"],
      },
      {
        href: "/devices",
        label: "设备与接入",
        icon: Server,
        testId: "nav-devices",
        hint: "配对向导、设备身份与 Token 轮换——没有执行体时从这里接入",
        keywords: ["设备", "接入", "配对", "令牌", "token", "agent", "授权", "安装"],
      },
    ],
  },
  {
    id: "project",
    label: "项目协作",
    hint: "多 Agent 协作生产：工作区、任务、成果物、审核与交接",
    icon: Layers3,
    items: [
      {
        href: "/workspace",
        label: "项目工作区",
        icon: MessagesSquare,
        testId: "nav-workspace",
        hint: "群聊 + 成员概览 + 成果入口的主工作区域",
        badge: "projects",
        keywords: ["工作区", "群聊", "聊天", "协作", "成员", "workspace", "chat", "主界面"],
      },
      {
        href: "/",
        label: "项目总览",
        icon: Layers3,
        testId: "nav-overview",
        hint: "队伍、Agent 与门禁的当前状态",
        badge: "projects",
        keywords: ["首页", "看板", "dashboard", "home", "概览", "进度"],
      },
      {
        href: "/tasks",
        label: "任务与流程",
        icon: ClipboardCheck,
        testId: "nav-tasks",
        hint: "任务列表、依赖诊断与执行方式设置",
        badge: "tasks",
        keywords: ["任务", "待办", "流程", "依赖", "开始", "执行", "codex", "工单"],
      },
      {
        href: "/my-tasks",
        label: "我的任务",
        icon: ListChecks,
        testId: "nav-my-tasks",
        hint: "派给我的活、我的 Agent 在执行与最近完成",
        keywords: ["我的任务", "指派", "派单", "个人", "待办", "我的 agent", "my"],
      },
      {
        href: "/review",
        label: "审核门禁",
        icon: ShieldCheck,
        testId: "nav-reviews",
        hint: "门禁、复核意见、风险登记与交接收据",
        badge: "review",
        keywords: ["审核", "门禁", "批准", "复核", "风险", "gate", "把关"],
      },
      {
        href: "/handoffs",
        label: "交接中心",
        icon: Inbox,
        testId: "nav-handoffs",
        hint: "接力与分发交接及其收据",
        keywords: ["交接", "接力", "分发", "收据", "handoff"],
      },
      {
        href: "/artifacts",
        label: "成果物库",
        icon: Box,
        testId: "nav-artifacts",
        hint: "版本、状态与来源保持可追溯",
        keywords: ["成果物", "产物", "版本", "下载", "归档", "artifact", "血缘"],
      },
      {
        href: "/documents",
        label: "文档版本",
        icon: FileText,
        testId: "nav-documents",
        hint: "草稿 / 提交 / 批准三层版本与协作编辑",
        keywords: ["文档", "论文", "草稿", "编辑", "协作", "版本", "tex", "markdown"],
      },
      {
        href: "/drive",
        label: "个人云盘",
        icon: Cloud,
        testId: "nav-drive",
        hint: "私人文件暂存（200MB）并一键加入项目",
        keywords: ["云盘", "网盘", "文件", "上传", "暂存", "附件"],
      },
      {
        href: "/team",
        label: "团队与成员",
        icon: Users,
        testId: "nav-team",
        hint: "成员工作量、项目成员管理与团队",
        keywords: ["团队", "成员", "工作量", "谁在忙", "角色", "邀请", "队伍"],
      },
      {
        href: "/channels",
        label: "LLM 渠道",
        icon: KeyRound,
        testId: "nav-channels",
        hint: "管理员录入 OpenAI 兼容上游，全员共享免费额度",
        keywords: ["渠道", "llm", "模型", "免费额度", "管理员", "代理", "测速", "channel"],
      },
      {
        href: "/settings",
        label: "空间设置",
        icon: Settings2,
        testId: "nav-settings",
        hint: "平台 API 凭据（LLM / Embedding / MinerU）与空间配额",
        keywords: ["设置", "凭据", "密钥", "api key", "llm", "embedding", "mineru", "配额"],
      },
    ],
  },
  {
    id: "workflow",
    label: "工作流工具",
    hint: "首个工作流包（数学建模）、交付编译与知识/运行诊断",
    icon: Wrench,
    items: [
      {
        href: "/workflows",
        label: "工作流包",
        icon: Workflow,
        testId: "nav-workflows",
        hint: "通用工作流包：内置包安装、定义编辑、应用到项目物化任务骨架",
        keywords: ["工作流", "包", "workflow", "编排", "内置包", "模板", "阶段", "节点"],
      },
      {
        href: "/pack",
        label: "建模模板包",
        icon: FileCheck2,
        testId: "nav-pack",
        hint: "首个垂直工作流包（数学建模）：物化四问流程与模板骨架",
        badge: "pack",
        keywords: ["模板", "工作流", "领域包", "cumcm", "物化", "校验", "题号", "问题一"],
      },
      {
        href: "/delivery",
        label: "论文交付",
        icon: Send,
        testId: "nav-delivery",
        hint: "装配、检查、编译、提交包与跨部署校验",
        keywords: ["交付", "论文", "编译", "pdf", "latex", "提交包", "答辩", "就绪度"],
      },
      {
        href: "/kb",
        label: "知识库",
        icon: Database,
        testId: "nav-kb",
        hint: "文档登记、索引构建与检索状态",
        keywords: ["知识库", "索引", "检索", "mineru", "rag", "资料"],
      },
      {
        href: "/graph",
        label: "图谱可视化",
        icon: Network,
        testId: "nav-graph",
        hint: "实体邻居子图与向量库浏览（只读）",
        keywords: ["图谱", "图", "实体", "关系", "可视化", "graph", "hypergraph"],
      },
      {
        href: "/runs",
        label: "运行控制台",
        icon: TerminalSquare,
        testId: "nav-runs",
        hint: "运行、设备与 Agent 编队",
        keywords: ["运行", "日志", "控制台", "输出", "run", "执行过程"],
      },
      {
        href: "/timeline",
        label: "项目时间线",
        icon: Activity,
        testId: "nav-timeline",
        hint: "事件流审计",
        keywords: ["时间线", "事件", "审计", "记录", "历史", "timeline"],
      },
    ],
  },
];

/** 折叠状态的持久化键（用户手动开合过的分组）。 */
export const NAV_STORAGE_KEY = "math-agent-platform.nav.open-sections";

/** 页面标题/副标题：与导航标签解耦（标题更完整，导航标签更短）。 */
const PAGE_TITLES: Record<string, { title: string; subtitle: string }> = {
  "/workspace": { title: "项目工作区", subtitle: "群聊、成员概览与成果空间——项目的主工作区域" },
  "/": { title: "项目总览", subtitle: "队伍、Agent 与门禁的当前状态" },
  "/tasks": { title: "任务与流程", subtitle: "按阶段查看待办、依赖与负责人" },
  "/my-tasks": { title: "我的任务", subtitle: "指派给我的、我的 Agent 在执行与最近完成的" },
  "/pack": { title: "建模模板包", subtitle: "首个垂直工作流包（数学建模）：物化任务与成果骨架——骨架不是最终结果" },
  "/documents": { title: "文档与版本", subtitle: "草稿 / 提交 / 批准三层版本与协作编辑" },
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
  "/settings": { title: "空间设置", subtitle: "平台 API 凭据（LLM / Embedding / MinerU）与空间配额" },
  "/channels": { title: "LLM 渠道", subtitle: "平台代管的 OpenAI 兼容上游与全员免费额度" },
  "/workflows": { title: "工作流包", subtitle: "通用工作流包：定义阶段、角色、门禁与交付，应用到项目物化任务骨架" },
};

export function findSection(pathname: string): NavSection | undefined {
  return NAV_SECTIONS.find((section) => section.items.some((item) => item.href === pathname));
}

export function findNavItem(pathname: string): { item: NavItem; section: NavSection } | undefined {
  for (const section of NAV_SECTIONS) {
    const item = section.items.find((entry) => entry.href === pathname);
    if (item) return { item, section };
  }
  return undefined;
}

export function pageMeta(pathname: string): { title: string; subtitle: string; group: string } {
  const section = findSection(pathname);
  const titles = PAGE_TITLES[pathname];
  return {
    title: titles?.title ?? findNavItem(pathname)?.item.label ?? "工作台",
    subtitle: titles?.subtitle ?? "",
    group: section?.label ?? "工作台",
  };
}

/** 跳转面板的搜索：空查询返回全部（分组展示），多词空格分隔取交集。 */
export function searchNav(query: string): { item: NavItem; section: NavSection }[] {
  const tokens = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const all = NAV_SECTIONS.flatMap((section) => section.items.map((item) => ({ item, section })));
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
    .filter((entry): entry is { item: NavItem; section: NavSection; score: number } => entry !== null);
  return scored.sort((a, b) => a.score - b.score).map(({ item, section }) => ({ item, section }));
}