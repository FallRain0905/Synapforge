"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Menu } from "lucide-react";
import { HUBS, MOBILE_TAB_HREFS, type NavBadge } from "../lib/nav";

/**
 * 手机底部标签栏（六主导航版）：从 nav 单一来源派生——
 * 前 4 个常用 Hub（MOBILE_TAB_HREFS）+ 第 5 格「更多」打开抽屉走全量导航。
 * 图标/文案/测试 id 全部取自 HUBS 定义，桌面端由 CSS 隐藏，桌面体验不变。
 */
const MOBILE_BADGES: Partial<Record<string, NavBadge>> = {
  "/workspace": "projects",
  "/tasks": "tasks",
};

export function MobileTabs({
  badges,
  onMore,
}: {
  badges: { tasks: number; review: number };
  onMore: () => void;
}) {
  const pathname = usePathname();
  const tabs = MOBILE_TAB_HREFS.map((href) => HUBS.find((hub) => hub.href === href)!).filter(Boolean);
  const inTabs = tabs.some((tab) => tab.href === pathname);

  return (
    <nav className="mobile-tabs" aria-label="移动端主导航" data-testid="mobile-tabs">
      {tabs.map((tab) => {
        const badge = MOBILE_BADGES[tab.href];
        const value = badge ? (badge === "review" ? badges.review : badges.tasks) : 0;
        return (
          <Link
            key={tab.href}
            href={tab.href}
            className={`mobile-tab ${pathname === tab.href ? "is-active" : ""}`.trim()}
            data-testid={`mtab-${tab.testId.replace(/^nav-/, "")}`}
          >
            <tab.icon size={19} />
            <span>{tab.label}</span>
            {value > 0 && badge !== "projects" && <span className="mobile-tab-dot" />}
          </Link>
        );
      })}
      <button
        type="button"
        className={`mobile-tab ${inTabs ? "" : "is-active"}`.trim()}
        onClick={onMore}
        aria-label="更多页面"
        data-testid="mtab-more"
      >
        <Menu size={19} />
        <span>更多</span>
      </button>
    </nav>
  );
}
