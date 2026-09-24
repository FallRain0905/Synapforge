"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ClipboardCheck, Layers3, Menu, ShieldCheck, Sparkles } from "lucide-react";

/**
 * 手机底部标签栏：只放手机上真正会做的四件事，第五项「更多」打开抽屉走全量导航。
 * 桌面端由 CSS 隐藏（`.mobile-tabs { display: none }`），所以桌面体验完全不变。
 */
const TABS = [
  { href: "/", label: "总览", icon: Layers3, testId: "mtab-overview" },
  { href: "/tasks", label: "任务", icon: ClipboardCheck, testId: "mtab-tasks", badge: "tasks" as const },
  { href: "/review", label: "审核", icon: ShieldCheck, testId: "mtab-review", badge: "review" as const },
  { href: "/my-agent", label: "智能体", icon: Sparkles, testId: "mtab-my-agent" },
];

export function MobileTabs({
  badges,
  onMore,
}: {
  badges: { tasks: number; review: number };
  onMore: () => void;
}) {
  const pathname = usePathname();
  const inTabs = TABS.some((tab) => tab.href === pathname);

  return (
    <nav className="mobile-tabs" aria-label="移动端主导航" data-testid="mobile-tabs">
      {TABS.map((tab) => {
        const value = tab.badge ? badges[tab.badge] : 0;
        return (
          <Link
            key={tab.href}
            href={tab.href}
            className={`mobile-tab ${pathname === tab.href ? "is-active" : ""}`.trim()}
            data-testid={tab.testId}
          >
            <tab.icon size={19} />
            <span>{tab.label}</span>
            {value > 0 && <span className="mobile-tab-dot" />}
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