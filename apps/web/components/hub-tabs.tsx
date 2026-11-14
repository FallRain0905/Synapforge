"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import type { NavItem } from "../lib/nav";

/**
 * Hub 页内的二级 Tab 条（六主导航的可复用组件）。
 *
 * 与 workspace 页自带的 `.tabs` 不同，这里每一格是一个**链接**（可被中键/新标签打开），
 * 并且双通道同步 URL：
 * - 读取：优先 `?tab=<id>`（深链、前进后退都还原到同一个 Tab）；
 * - 写入：点 Tab 时用 history.replaceState 原地改 query（不推历史、不丢其它参数），
 *   再对 popstate 监听做还原（用户点浏览器返回时 Tab 跟着回退）。
 *
 * 首选 Tab（index 0）允许不带 query：只有当用户切到非首选 Tab 时 URL 才出现 `?tab=`。
 * 组件受控：`activeId` 由父页面持有；`onChange` 里父页面除了 setState 外不需要再管 URL。
 */
export function HubTabs({
  tabs,
  activeId,
  onChange,
  ariaLabel,
  queryKey = "tab",
  testIdPrefix,
}: {
  /** Tab 清单；第一项是首选（无 query 时落在它上）。 */
  tabs: NavItem[];
  /** 当前选中的 tab id（= NavItem.href）。 */
  activeId: string;
  onChange: (nextId: string) => void;
  ariaLabel: string;
  /** URL 参数名；同页多级 Tab 时用不同 key 避免互相覆盖。 */
  queryKey?: string;
  testIdPrefix?: string;
}) {
  const readFromUrl = useCallback((): string | null => {
    if (typeof window === "undefined") return null;
    const value = new URLSearchParams(window.location.search).get(queryKey);
    if (!value) return null;
    // /xxx?tab=/yyy 形式；也容忍不带斜杠的写法
    const normalized = value.startsWith("/") ? value : `/${value}`;
    return tabs.some((tab) => tab.href === normalized) ? normalized : null;
    // tabs 清单在调用点是静态的，这里刻意不依赖它
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queryKey]);

  const [hydrated, setHydrated] = useState(false);

  // 首帧后读一次 URL：SSR 的静态 HTML 与客户端首帧保持一致（避免 hydration 警告），
  // 深链 /documents?tab=/artifacts 直接落到对应 Tab。
  useEffect(() => {
    const fromUrl = readFromUrl();
    if (fromUrl && fromUrl !== activeId) onChange(fromUrl);
    setHydrated(true);
    // 只在挂载时同步一次；之后 URL 由本组件自己写
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // popstate / hashchange：前进后退时把 Tab 还原回 URL 里的状态
  useEffect(() => {
    if (!hydrated) return;
    const onPop = () => {
      const fromUrl = readFromUrl() ?? tabs[0]?.href;
      if (fromUrl && fromUrl !== activeId) onChange(fromUrl);
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hydrated, activeId, onChange, readFromUrl]);

  /** 点 Tab：原地改 query（保留其它参数、不推历史栈）；选首选则把参数去掉。 */
  const select = (tab: NavItem) => {
    if (typeof window !== "undefined") {
      const url = new URL(window.location.href);
      if (tab.href === tabs[0]?.href) url.searchParams.delete(queryKey);
      else url.searchParams.set(queryKey, tab.href);
      window.history.replaceState(window.history.state, "", url.toString());
    }
    onChange(tab.href);
  };

  return (
    <div className="tabs hub-tabs" role="tablist" aria-label={ariaLabel}>
      {tabs.map((tab, index) => {
        const Icon = tab.icon;
        const active = tab.href === activeId;
        return (
          <Link
            key={tab.href}
            href={tab.href}
            role="tab"
            aria-selected={active}
            className={`tab${active ? " tab-active" : ""}`}
            data-testid={`${testIdPrefix ?? "hub-tab"}-${tab.testId.replace(/^nav-/, "")}`}
            title={tab.hint}
            onClick={(event) => {
              // 左键本地接管（同步 URL + 父组件切换）；修饰键/新标签交给浏览器默认行为
              if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
              event.preventDefault();
              select(tab);
            }}
          >
            {index === 0 ? null : <Icon size={14} />} {tab.label}
          </Link>
        );
      })}
    </div>
  );
}
