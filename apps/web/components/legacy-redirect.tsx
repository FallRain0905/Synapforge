"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { ArrowUpRight } from "lucide-react";
import Link from "next/link";
import { PageHeading } from "./shell";
import { Panel } from "./ui";

/**
 * 旧路由 → Hub Tab 的统一跳转页（六主导航：只隐藏不删除）。
 *
 * - replace 而非 push：旧入口不出现在"返回"里，也不会死循环；
 * - query 原样保留：/artifacts?artifact=xxx → /documents?tab=/artifacts&artifact=xxx；
 * - 页面本身给出"去往哪里"的说明与手动链接（跳转失败/被拦时用户不迷路）。
 */
export function LegacyRedirect({
  from,
  to,
  label,
  hint,
  testId,
}: {
  from: string;
  /** 目标地址；query 会自动接在后面。 */
  to: string;
  label: string;
  hint: string;
  testId: string;
}) {
  const router = useRouter();

  useEffect(() => {
    if (typeof window === "undefined") return;
    const search = window.location.search;
    const joiner = to.includes("?") ? "&" : "?";
    router.replace(`${to}${search ? `${joiner}${search.slice(1)}` : ""}`);
  }, [router, to]);

  return (
    <div className="page-content" id={testId} data-testid={testId}>
      <PageHeading hint={hint} />
      <Panel title={`「${label}」已收进对应 Hub`} subtitle={`${from} → ${to}`}>
        <div className="empty-cta" data-testid={`${testId}-cta`}>
          <strong>页面已迁移</strong>
          <small>
            六主导航后这个页面成为对应 Hub 里的一个 Tab；正在跳转……
            如果浏览器没有自动跳，用下面的按钮手动前往。
          </small>
          <div className="form-row" style={{ justifyContent: "center" }}>
            <Link className="button button-primary" href={to}>
              前往{label} <ArrowUpRight size={16} />
            </Link>
          </div>
        </div>
      </Panel>
    </div>
  );
}
