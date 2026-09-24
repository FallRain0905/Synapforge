"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter, usePathname } from "next/navigation";
import { CornerDownLeft, Search, X } from "lucide-react";
import { searchNav } from "../lib/nav";

/**
 * 快速跳转：顶栏下拉 + Ctrl/Cmd+K。
 * 全部 16 个页面一次列全（空查询即全部），支持中文标签、说明与英文别名的模糊匹配，
 * 用于替代"在侧边栏里一页一页找"。
 */
export function QuickJump({ open, onClose }: { open: boolean; onClose: () => void }) {
  const router = useRouter();
  const pathname = usePathname();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const results = useMemo(() => searchNav(query), [query]);

  useEffect(() => {
    if (!open) return;
    setQuery("");
    setActive(0);
    const timer = window.setTimeout(() => inputRef.current?.focus(), 30);
    return () => window.clearTimeout(timer);
  }, [open]);

  useEffect(() => {
    setActive(0);
  }, [query]);

  useEffect(() => {
    if (!open) return;
    const node = listRef.current?.querySelector<HTMLElement>(`[data-index="${active}"]`);
    node?.scrollIntoView({ block: "nearest" });
  }, [active, open, results.length]);

  if (!open) return null;

  const go = (href: string) => {
    onClose();
    if (href !== pathname) router.push(href);
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((index) => (results.length ? (index + 1) % results.length : 0));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((index) => (results.length ? (index - 1 + results.length) % results.length : 0));
    } else if (event.key === "Enter") {
      event.preventDefault();
      const target = results[active];
      if (target) go(target.item.href);
    } else if (event.key === "Escape") {
      event.preventDefault();
      onClose();
    }
  };

  let lastSection = "";

  return (
    <div className="palette-backdrop" role="presentation" onClick={onClose} data-testid="quick-jump-panel">
      <div className="palette" role="dialog" aria-modal="true" aria-label="快速跳转" onClick={(event) => event.stopPropagation()}>
        <div className="palette-search">
          <Search size={16} />
          <input
            ref={inputRef}
            value={query}
            placeholder="搜索页面：任务、论文、配对、图谱、PDF…"
            aria-label="搜索页面"
            data-testid="quick-jump-input"
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={onKeyDown}
          />
          <button className="app-icon" aria-label="关闭快速跳转" onClick={onClose}><X size={15} /></button>
        </div>
        <div className="palette-list" ref={listRef}>
          {results.map(({ item, section }, index) => {
            const header = section.label === lastSection ? null : section.label;
            lastSection = section.label;
            return (
              <div key={item.href}>
                {header && <div className="palette-group-label">{header} · {section.hint}</div>}
                <button
                  type="button"
                  className={`palette-item ${index === active ? "is-active" : ""}`}
                  data-index={index}
                  data-testid={`jump-${item.testId.replace(/^nav-/, "")}`}
                  onMouseEnter={() => setActive(index)}
                  onClick={() => go(item.href)}
                >
                  <item.icon size={16} />
                  <span className="palette-item-label">{item.label}</span>
                  <small>{item.hint}</small>
                  {item.href === pathname && <span className="palette-current">当前</span>}
                </button>
              </div>
            );
          })}
          {!results.length && (
            <div className="palette-empty">
              没有匹配的页面。试试「论文」「配对」「图谱」「设置」这类说法，或直接打开侧边栏分组。
            </div>
          )}
        </div>
        <div className="palette-footer">
          <span><kbd className="palette-kbd">↑</kbd><kbd className="palette-kbd">↓</kbd> 选择</span>
          <span><kbd className="palette-kbd"><CornerDownLeft size={11} /></kbd> 打开</span>
          <span><kbd className="palette-kbd">Esc</kbd> 关闭</span>
          <span className="palette-footer-right">{results.length} / {searchNav("").length} 个页面</span>
        </div>
      </div>
    </div>
  );
}