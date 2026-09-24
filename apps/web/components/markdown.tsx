"use client";

import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";

/**
 * 智能体回复的 Markdown 渲染（用户反馈：以前是纯文本，表格/列表/公式都看不出来）。
 *
 * 为什么用 react-markdown 而不是自己写解析：角色提示词要求输出**表格**（对齐表、findings、评分表、
 * 证据卡）与**公式**（`$…$` / `$$…$$`，数模场景的核心），手写解析在这些结构上很容易出错。
 *
 * 安全口径：react-markdown **默认不渲染原始 HTML**（不给 rehype-raw），所以执行体回复里的
 * `<script>` / `<img onerror>` 这类只会当文本显示——这正是我们要的：回复是不可信内容。
 * 链接统一 `target=_blank` + `rel=noreferrer`（不带 opener）。
 */
export function Markdown({ text, className = "" }: { text: string; className?: string }) {
  if (!text) return null;
  return (
    <div className={`md-body${className ? ` ${className}` : ""}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[[rehypeKatex, { throwOnError: false, strict: false }]]}
        components={{
          a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noreferrer" />,
          // 代码块外面套一层：宽代码要能横向滚动，而不是把整页顶开
          pre: ({ node: _node, ...props }) => (
            <div className="md-pre">
              <pre {...props} />
            </div>
          ),
          table: ({ node: _node, ...props }) => (
            <div className="md-table-wrap">
              <table {...props} />
            </div>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}