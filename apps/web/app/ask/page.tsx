"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { ArrowUpRight } from "lucide-react";
import Link from "next/link";
import { PageHeading } from "../../components/shell";
import { Panel } from "../../components/ui";

/**
 * /ask 已并入 /my-agent（产品重定位阶段 0 / W0.3：全平台唯一的对话入口）。
 *
 * 老轨**只隐藏不删除**：路由文件保留，进入后立刻 replace 到 /my-agent（带原 query），
 * 深链接不会 404，也不会在浏览器历史里留下"返回又跳回来"的死循环。
 * 旧问答里的知识库检索在 /my-agent 的角色体系之外，后续按工作流包形态重新归位。
 */
export default function AskPage() {
  const router = useRouter();

  useEffect(() => {
    // 用 window.location 读原始 query（避免 useSearchParams 的 Suspense 约束）；
    // replace 而非 push：旧入口不该出现在"返回"里。
    const search = window.location.search;
    router.replace(search ? `/my-agent${search}` : "/my-agent");
  }, [router]);

  return (
    <div className="page-content" id="ask" data-testid="ask-redirect">
      <PageHeading hint="旧问答入口已并入 Agent 工作台" />
      <Panel title="此页面已并入「我的智能体」" subtitle="对话入口唯一化：/ask → /my-agent">
        <div className="empty-cta" data-testid="ask-redirect-cta">
          <strong>AI 问答已迁移</strong>
          <small>
            平台只保留一个对话入口「我的智能体」：那里支持多轮对话、上传文件、
            查看真实流式过程与思考过程，并能直接转入项目生产。正在跳转……
          </small>
          <div className="form-row" style={{ justifyContent: "center" }}>
            <Link className="button button-primary" href="/my-agent">
              前往我的智能体 <ArrowUpRight size={16} />
            </Link>
          </div>
        </div>
      </Panel>
    </div>
  );
}
