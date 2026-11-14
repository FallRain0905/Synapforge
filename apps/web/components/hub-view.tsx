"use client";

import { Component, Suspense, useEffect, useMemo, useState, type ReactNode } from "react";
import dynamic from "next/dynamic";
import { EmptyState, LoadingSkeleton } from "./ui";
import { PAGE_GRID } from "../lib/page-layout";

/**
 * Hub 内嵌视图（六主导航的可复用组件）：把一个**既有页面模块**当作 Tab 内容动态加载。
 *
 * 工作方式（只复用、不复制业务代码）：
 * - `loaders` 给出「tab id → () => import(".../page")」的映射，只有选中的 Tab 才会真正
 *   发起 chunk 请求（next/dynamic 打包时各自独立代码分片）；
 * - 被嵌入的页面模块**保持原样**（原路由仍然独立可用），因此它们自带 `.page-content`
 *   外壳与 PageHeading。为避免"Hub 页里再套一个页面头"的双重标题，Hub 渲染该模块时
 *   加上 `data-hub-embedded` 外壳，配套 CSS 把内层的页面壳/标题压平（见 globals.css
 *   的 `.hub-embedded` 规则）——不改被嵌页面的任何一行代码。
 *
 * 适用对象：settings / documents 这类"我方拥有页面"的 Hub。my-agent / tasks / workflows
 * 页面归其它会话所有，等它们完成后由父会话用同一组件接入（见交接文档）。
 */

type LoaderMap = Record<string, () => Promise<{ default: React.ComponentType }>>;

/** 兜底错误视图：chunk 加载失败（离线/发版瞬间）不把 Hub 页整页搞挂。 */
class EmbeddedErrorBoundary extends Component<{ children: ReactNode; label: string }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    if (this.state.failed) {
      return (
        <EmptyState>
          {this.props.label}加载失败：刷新页面或稍后再试（Tab 内容是按需加载的，网络恢复后重选一次即可）。
        </EmptyState>
      );
    }
    return this.props.children;
  }
}

export function HubView({
  loaders,
  activeId,
  labels,
}: {
  loaders: LoaderMap;
  /** 当前选中的二级 Tab id。 */
  activeId: string;
  /** 各 Tab 的显示名（错误提示用）。 */
  labels: Record<string, string>;
}) {
  const loader = loaders[activeId];
  const EmbeddedView = useMemo(
    () => (loader ? dynamic(loader, { ssr: false, loading: () => <LoadingSkeleton rows={5} label="正在加载视图…" /> }) : null),
    [loader],
  );

  // 记录"这个 Tab 是否加载过"：切走再切回不重新挂载组件（状态保留），
  // 但组件本身保持卸载状态直到首次选中——首屏不背全部 Tab 的代码与请求。
  const [mountedOnce, setMountedOnce] = useState<Record<string, boolean>>({});
  useEffect(() => {
    if (EmbeddedView) setMountedOnce((current) => ({ ...current, [activeId]: true }));
  }, [EmbeddedView, activeId]);

  return (
    <div className="hub-embedded" style={PAGE_GRID} data-hub-embedded={activeId}>
      {Object.entries(mountedOnce).map(([id]) => {
        const loader2 = loaders[id];
        if (!loader2) return null;
        const View = dynamic(loader2, { ssr: false, loading: () => <LoadingSkeleton rows={5} label="正在加载视图…" /> });
        return (
          <div key={id} style={{ display: id === activeId ? "contents" : "none" }}>
            <EmbeddedErrorBoundary label={labels[id] ?? id}>
              <Suspense fallback={<LoadingSkeleton rows={5} label="正在加载视图…" />}>
                <View />
              </Suspense>
            </EmbeddedErrorBoundary>
          </div>
        );
      })}
      {!Object.keys(mountedOnce).length && <LoadingSkeleton rows={5} label="正在加载视图…" />}
    </div>
  );
}
