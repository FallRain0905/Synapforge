"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Download, FileText, Loader2, RefreshCcw, X } from "lucide-react";
import {
  ArtifactDetail,
  Artifact,
  downloadArtifactContent,
  errorMessage,
  getArtifactDetail,
  getArtifactText,
} from "../lib/api";

/** 可以直接在抽屉里读的文本类成果物（其余类型只给元信息 + 下载）。 */
const TEXT_TYPES = new Set([
  "problem_source",
  "problem_facts",
  "problem_analysis",
  "data_profile",
  "model_spec",
  "code",
  "experiment_plan",
  "result_table",
  "review_report",
  "audit_report",
  "paper_source",
  "agent_answer",
]);

/**
 * 成果物侧边抽屉（W-10）：在聊天区/成果空间点一下就地展开查看，不用跳页。
 *
 * 两个请求各司其职：`/content` 取正文（文本类才有意义），`/detail` 取来源（哪个任务/运行产出的、
 * 复核与门禁状态、版本血缘）。二进制类型不硬解，直接给下载入口——猜着渲染只会得到乱码。
 */
export function ArtifactDrawer({
  artifact,
  projectId,
  onClose,
}: {
  artifact: Artifact | { id: string; name: string; artifact_type?: string; status?: string; version?: number };
  projectId: string;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<ArtifactDetail | null>(null);
  const [content, setContent] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [downloading, setDownloading] = useState(false);
  const drawerRef = useRef<HTMLElement | null>(null);
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const onCloseRef = useRef(onClose);
  // 类型以服务端详情为准：聊天卡片里的引用通常只带 id，客户端不知道是文本还是二进制
  const type = String(detail?.artifact.artifact_type ?? artifact.artifact_type ?? "");
  const readable = type ? TEXT_TYPES.has(type) : true; // 类型未知时先按文本试读，读到空再提示

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const nextDetail = await getArtifactDetail(projectId, artifact.id).catch(() => null);
      const resolvedType = String(nextDetail?.artifact.artifact_type ?? artifact.artifact_type ?? "");
      const shouldReadText = resolvedType ? TEXT_TYPES.has(resolvedType) : true;
      const text = shouldReadText ? await getArtifactText(artifact.id).catch(() => "") : null;
      setDetail(nextDetail);
      setContent(text);
      setError("");
    } catch (failure) {
      setError(errorMessage(failure, "成果物读取失败"));
    } finally {
      setLoading(false);
    }
  }, [projectId, artifact.id, artifact.artifact_type]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeRef.current?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab" || !drawerRef.current) return;
      const focusable = Array.from(
        drawerRef.current.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      );
      if (!focusable.length) {
        event.preventDefault();
        drawerRef.current.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      previousFocus?.focus();
    };
  }, []);

  const download = async () => {
    setDownloading(true);
    try {
      await downloadArtifactContent(artifact.id, artifact.name);
    } catch (failure) {
      setError(errorMessage(failure, "下载失败"));
    } finally {
      setDownloading(false);
    }
  };

  const shown = detail?.artifact ?? artifact;

  return (
    <div className="drawer-backdrop" onClick={onClose} data-testid="artifact-drawer-backdrop">
      <aside
        ref={drawerRef}
        className="artifact-drawer"
        role="dialog"
        aria-modal="true"
        aria-label={`成果物：${artifact.name}`}
        tabIndex={-1}
        onClick={(event) => event.stopPropagation()}
        data-testid="artifact-drawer"
      >
        <header className="drawer-head">
          <div className="drawer-title">
            <FileText size={15} />
            <div>
              <strong>{artifact.name}</strong>
              <small>
                {type || "成果物"} · v{artifact.version ?? shown.version ?? 1} · {String(shown.status ?? "")}
              </small>
            </div>
          </div>
          <div className="drawer-actions">
            <button type="button" className="text-button" onClick={() => void load()} disabled={loading} title="重新读取">
              {loading ? <Loader2 size={13} className="spin" /> : <RefreshCcw size={13} />}
            </button>
            <button type="button" className="text-button" onClick={() => void download()} disabled={downloading}>
              {downloading ? <Loader2 size={13} className="spin" /> : <Download size={13} />} 下载
            </button>
            <button ref={closeRef} type="button" className="app-icon" aria-label="关闭" onClick={onClose} data-testid="artifact-drawer-close">
              <X size={15} />
            </button>
          </div>
        </header>

        {error ? <p className="hint" style={{ color: "var(--red)" }}>{error}</p> : null}

        <div className="drawer-body">
          {readable ? (
            loading ? (
              <p className="hint">读取内容…</p>
            ) : content ? (
              <pre className="drawer-content" data-testid="artifact-drawer-content">
                {content}
              </pre>
            ) : (
              <p className="hint">这条成果物还没有存内容（只有元信息）。</p>
            )
          ) : (
            <p className="hint">
              这是二进制/结构化产出（{type || "未知类型"}），抽屉里不硬解——点右上角「下载」取原件。
            </p>
          )}
          {readable && !loading && content !== null && !content ? (
            <p className="hint">这条成果物还没有存内容（只有元信息）——可以「下载」或去成果物库看来源。</p>
          ) : null}

          {detail ? (
            <section className="drawer-meta">
              <h4>来源与状态</h4>
              <ul className="drawer-list">
                {detail.source_task ? (
                  <li>
                    产出任务：<Link className="inline-link" href="/tasks">{detail.source_task.title}</Link>（{detail.source_task.status}）
                  </li>
                ) : null}
                {detail.source_run ? (
                  <li>
                    执行运行：{detail.source_run.agent_id} · {detail.source_run.status}
                  </li>
                ) : null}
                <li>
                  门禁：{detail.gate ? `${detail.gate.status}${detail.gate.required_human_approval ? "（需人工批准）" : ""}` : "尚无门禁"}
                </li>
                <li>版本血缘：{detail.lineage.length} 个版本</li>
                {detail.consumers.length ? <li>被 {detail.consumers.length} 条任务引用</li> : null}
                {detail.reviews.length ? (
                  <li>
                    复核：{detail.reviews.slice(0, 2).map((review) => `${review.verdict}（${review.reviewer}）`).join("、")}
                  </li>
                ) : null}
              </ul>
              <div className="link-row">
                <Link className="inline-link" href="/artifacts">在成果物库打开（版本与来源全视图）</Link>
              </div>
            </section>
          ) : null}
        </div>
      </aside>
    </div>
  );
}