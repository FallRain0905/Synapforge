"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { ArrowUpRight } from "lucide-react";
import Link from "next/link";
import { PageHeading } from "../components/shell";
import { Panel } from "../components/ui";
import { ProjectOverviewView } from "../components/overview/project-overview";

/**
 * 根路由（六主导航版）：`/` 立即 replace 到 `/workspace`（带原 query）。
 *
 * - 「项目总览」收进工作区 Hub 的「概览」Tab；主导航不再把 `/` 当独立入口。
 * - 兼容旧习惯 `/`（或旧深链 `/?xxx`）：replace 而非 push，不产生"返回又跳回来"的死循环。
 * - `/workspace?tab=/` 是总览 Tab 的地址；`/?tab=/` 这类旧写法也能落到总览内容
 *   （`?tab=/` 出现在根路由时直接渲染总览视图，不跳转——保住旧收藏链接）。
 * - ProjectOverviewView 的代码是按需分片的：默认落地页不背它的体积。
 */
export default function RootPage() {
  const router = useRouter();

  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    // 旧写法 ?tab=/：直接渲染总览内容（不再跳 /workspace，避免多层跳转）
    if (params.get("tab") === "/") return;
    const query = window.location.search;
    router.replace(query ? `/workspace${query}` : "/workspace");
  }, [router]);

  const overviewLanding = typeof window !== "undefined" && new URLSearchParams(window.location.search).get("tab") === "/";

  if (overviewLanding) {
    return <ProjectOverviewView suppressHeading />;
  }

  return (
    <div className="page-content" id="overview" data-testid="root-redirect">
      <PageHeading hint="项目总览已收进「项目协作」Hub" />
      <Panel title="正在进入项目协作…" subtitle="项目总览现在是工作区里的「概览」Tab">
        <div className="empty-cta" data-testid="root-redirect-cta">
          <strong>首页已迁移</strong>
          <small>
            六主导航后「项目协作」是项目的主入口：群聊、任务、团队、生产链、成果与总览都在那里。
            正在跳转……
          </small>
          <div className="form-row" style={{ justifyContent: "center" }}>
            <Link className="button button-primary" href="/workspace">
              前往项目协作 <ArrowUpRight size={16} />
            </Link>
          </div>
        </div>
      </Panel>
    </div>
  );
}
