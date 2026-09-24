"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowUpRight, FileText, RefreshCcw } from "lucide-react";
import { PageHeading, formatTime } from "../../components/shell";
import { EmptyState, Modal, Panel } from "../../components/ui";
import {
  DocumentComment,
  DocumentDiff,
  DocumentRelation,
  DocumentSnapshot,
  DocumentTimeline,
  addDocumentComment,
  createDocumentSnapshot,
  errorMessage,
  getArtifactText,
  getDocumentComments,
  getDocumentDiff,
  getDocumentRelations,
  getDocumentSnapshots,
  getDocumentTimeline,
  getImpactLookup,
  linkDocumentRelation,
  mergeDocument,
  reviseDocument,
  getDocumentDraft,
  saveArtifactText,
  saveDocumentDraft,
  submitDocument,
} from "../../lib/api";
import { CollaborativeDocument, createCollaborativeDocument } from "../../lib/collab";
import { projectSocketUrl } from "../../lib/api";
import { useWorkspace } from "../../lib/workspace";

const DOCUMENT_TYPES = ["paper_source", "model_spec", "problem_analysis", "audit_report", "review_report", "submission_bundle", "experiment_plan"];
const LAYER_LABEL: Record<string, string> = { draft: "草稿", submitted: "提交", approved: "批准" };
const TABS = [
  { id: "timeline", label: "版本" },
  { id: "diff", label: "差异" },
  { id: "comments", label: "评论" },
  { id: "snapshots", label: "快照" },
  { id: "relations", label: "关系" },
] as const;

type TabId = (typeof TABS)[number]["id"];

export default function DocumentsPage() {
  const { projectId, dashboard, notify, refresh } = useWorkspace();
  const [selectedId, setSelectedId] = useState("");
  const [timeline, setTimeline] = useState<DocumentTimeline | null>(null);
  const [diff, setDiff] = useState<DocumentDiff | null>(null);
  const [comments, setComments] = useState<DocumentComment[]>([]);
  const [snapshots, setSnapshots] = useState<DocumentSnapshot[]>([]);
  const [relations, setRelations] = useState<DocumentRelation[]>([]);
  const [tab, setTab] = useState<TabId>("timeline");
  const [busy, setBusy] = useState("");
  const [commentDraft, setCommentDraft] = useState("");
  const [commentKind, setCommentKind] = useState<"comment" | "suggestion">("comment");
  const [snapshotLabel, setSnapshotLabel] = useState("");
  const [relationTargetType, setRelationTargetType] = useState("figure");
  const [relationTargetId, setRelationTargetId] = useState("");
  const [relationParagraph, setRelationParagraph] = useState("");
  const [impact, setImpact] = useState<Awaited<ReturnType<typeof getImpactLookup>> | null>(null);
  const [collabText, setCollabText] = useState("");
  // 服务端草稿（CL-6）：恢复未保存的编辑；别人先保存时让用户选"以谁为准"，不静默覆盖
  const draftRef = useRef<{ revision: number } | null>(null);
  const draftTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const draftDirty = useRef(false);
  const [draftInfo, setDraftInfo] = useState<{ revision: number; updatedBy: string; updatedAt: string } | null>(null);
  const [conflict, setConflict] = useState<{ mine: string; theirs: string; theirsBy: string } | null>(null);
  const [collabStatus, setCollabStatus] = useState({ connected: false, peers: 1 });
  const [savedText, setSavedText] = useState("");
  const [savedAt, setSavedAt] = useState("");
  const [saving, setSaving] = useState(false);
  const collabRef = useRef<CollaborativeDocument | null>(null);

  const documents = useMemo(
    () => dashboard.artifacts.filter((artifact) => DOCUMENT_TYPES.includes(artifact.artifact_type)),
    [dashboard.artifacts],
  );

  const layerOf = (status: string) => (status === "APPROVED" || status === "ARCHIVED" ? "approved" : status === "PENDING_REVIEW" ? "submitted" : "draft");

  const startCollaboration = useCallback(
    async (artifactId: string) => {
      collabRef.current?.destroy();
      const platformText = await getArtifactText(artifactId);
      // 服务端草稿优先于平台内容：未保存的编辑刷新后要能接着写（CL-6）。
      // 没有草稿时用平台内容做种子，"已保存"基线始终是平台内容，diff 才有意义。
      let seed = platformText;
      try {
        const draft = await getDocumentDraft(projectId, artifactId);
        if (draft && draft.content.trim() && draft.content.trim() !== platformText.trim()) {
          seed = draft.content;
          draftRef.current = { revision: draft.revision };
          setDraftInfo({ revision: draft.revision, updatedBy: draft.updated_by, updatedAt: draft.updated_at });
          notify(`已恢复服务端草稿 r${draft.revision}（来自 ${draft.updated_by}）`);
        }
      } catch {
        /* 草稿读取失败就用平台内容，不阻塞编辑 */
      }
      const document = createCollaborativeDocument({
        projectId,
        documentId: artifactId,
        wsUrl: projectSocketUrl(projectId),
        initialText: seed,
        onStatus: (status) => setCollabStatus({ connected: status.connected, peers: status.peerCount }),
      });
      collabRef.current = document;
      latestText.current = document.getText();
      setCollabText(document.getText());
      // "已保存"基线是**平台内容**：与它比较才知道有没有未保存改动（草稿不算已保存）。
      setSavedText(platformText);
      setSavedAt("");
      document.text.observe(() => setCollabText(document.getText()));
    },
    [projectId],
  );

  const openDocument = useCallback(
    async (artifactId: string) => {
      if (!projectId) return;
      setSelectedId(artifactId);
      setBusy("load");
      try {
        const [nextTimeline, nextComments, nextSnapshots, nextRelations] = await Promise.all([
          getDocumentTimeline(projectId, artifactId),
          getDocumentComments(projectId, artifactId),
          getDocumentSnapshots(projectId, artifactId),
          getDocumentRelations(projectId, artifactId),
        ]);
        setTimeline(nextTimeline);
        setComments(nextComments.comments);
        setSnapshots(nextSnapshots.snapshots);
        setRelations(nextRelations.relations);
        setImpact(null);
        setDiff(nextTimeline.revision_count > 1 ? await getDocumentDiff(projectId, artifactId).catch(() => null) : null);
        await startCollaboration(artifactId);
      } catch {
        notify("文档读取失败");
      } finally {
        setBusy("");
      }
    },
    [projectId, notify, startCollaboration],
  );


  /** 节流把编辑写进服务端草稿（5 秒一次；切文档/离开时由 flushDraft 兜底）。 */
  const flushDraft = useCallback(
    async (content: string) => {
      if (!projectId || !selectedId || !draftDirty.current) return;
      draftDirty.current = false;
      try {
        const saved = await saveDocumentDraft(projectId, selectedId, content, draftRef.current?.revision ?? 0);
        draftRef.current = { revision: saved.revision };
        setDraftInfo({ revision: saved.revision, updatedBy: saved.updated_by, updatedAt: saved.updated_at });
      } catch (error) {
        const detail = errorMessage(error, "");
        if (!detail.includes("已被他人修改")) return; // 其它失败（如已批准不可改）只提示，不打断编辑
        try {
          const theirs = await getDocumentDraft(projectId, selectedId);
          setConflict({ mine: content, theirs: theirs?.content ?? "", theirsBy: theirs?.updated_by ?? "他人" });
          if (theirs) draftRef.current = { revision: theirs.revision };
        } catch {
          notify("协作文档冲突：请刷新后重试");
        }
      }
    },
    [projectId, selectedId, notify],
  );

  // 最新编辑文本：卸载兜底 flush 用（effect 不能依赖 collabText，否则每次按键都会
  // 触发 cleanup 取消节流定时器、把中间状态写进服务端）
  const latestText = useRef("");
  useEffect(
    () => () => {
      if (draftTimer.current) clearTimeout(draftTimer.current);
      if (!draftDirty.current) return;
      draftDirty.current = true;
      void flushDraft(latestText.current);
    },
    [flushDraft],
  );

  const scheduleDraftSave = useCallback(
    (content: string) => {
      draftDirty.current = true;
      if (draftTimer.current) clearTimeout(draftTimer.current);
      draftTimer.current = setTimeout(() => void flushDraft(content), 5000);
    },
    [flushDraft],
  );


  const handleCollabEdit = (next: string) => {
    const collab = collabRef.current;
    if (!collab) {
      setCollabText(next);
      return;
    }
    const current = collab.getText();
    let prefix = 0;
    while (prefix < current.length && prefix < next.length && current[prefix] === next[prefix]) prefix += 1;
    let suffix = 0;
    while (suffix < current.length - prefix && suffix < next.length - prefix && current[current.length - 1 - suffix] === next[next.length - 1 - suffix]) suffix += 1;
    const removed = current.length - prefix - suffix;
    const inserted = next.slice(prefix, next.length - suffix);
    if (removed > 0) collab.delete(prefix, removed);
    if (inserted) collab.insert(prefix, inserted);
    const nextText = collab.getText();
    latestText.current = nextText;
    setCollabText(nextText);
    scheduleDraftSave(nextText);
  };

  const handleSaveDraft = async () => {
    if (!selectedId) return;
    setSaving(true);
    try {
      await saveArtifactText(selectedId, collabText);
      setSavedText(collabText);
      draftRef.current = null; // 正式保存已覆盖内容，服务端草稿的修订号重新起步
      setSavedAt(new Date().toISOString());
      notify("草稿已保存到平台（提交待审会把它冻结为提交版本）");
      await refresh();
    } catch (error) {
      // 已批准文档不可改等边界由服务端判定，这里展示真实原因。
      notify(errorMessage(error, "文档保存失败"));
    } finally {
      setSaving(false);
    }
  };

  const run = async (label: string, action: () => Promise<string>) => {
    setBusy(label);
    try {
      notify(await action());
      if (selectedId) await openDocument(selectedId);
      await refresh();
    } catch (error) {
      notify(error instanceof Error ? error.message : "操作失败");
    } finally {
      setBusy("");
    }
  };

  const current = timeline?.revisions[timeline.revisions.length - 1];

  return (
    <div className="page-content" id="documents">
      <PageHeading
        hint="草稿 / 提交 / 批准三层版本；正式版本可追溯到成员、任务与 Git commit"
        actions={
          <button className="button button-secondary" disabled={!selectedId || busy !== "" || current?.layer !== "draft"} data-testid="document-submit" onClick={() => void run("submit", async () => { await submitDocument(projectId, selectedId); return "文档已提交待审"; })}>
            提交待审
          </button>
        }
      />

      <section className="grid grid-main-side">
        <Panel title="文档版本" subtitle={`共 ${documents.length} 个文档类成果物`} testId="document-panel" actions={<span className="gate-count">{documents.length}</span>}>
          <div className="list document-list-scroll" data-testid="document-list">
            {documents.map((artifact) => (
              <button
                key={artifact.id}
                type="button"
                className={`list-item list-item-button ${selectedId === artifact.id ? "list-item-active" : ""}`.trim()}
                data-testid={`document-item-${artifact.id}`}
                onClick={() => void openDocument(artifact.id)}
              >
                <span className={`document-layer document-layer-${layerOf(String(artifact.status))}`}>{LAYER_LABEL[layerOf(String(artifact.status))]}</span>
                <span className="item-copy">
                  <strong>{artifact.name}</strong>
                  <small>{artifact.artifact_type} · v{artifact.version} · {artifact.created_by}</small>
                </span>
                <ArrowUpRight size={14} />
              </button>
            ))}
            {!documents.length && <EmptyState>暂无文档类成果物；先在模板包中物化</EmptyState>}
          </div>
        </Panel>

        <div className="grid">
          {timeline ? (
            <Panel
              title={documents.find((item) => item.id === selectedId)?.name ?? "文档"}
              subtitle={`共 ${timeline.revision_count} 个版本 · 当前第 ${timeline.current_revision} 版`}
              testId="document-timeline"
              actions={
                <>
                  <button className="text-button" disabled={busy !== ""} data-testid="document-revise" onClick={() => void run("revise", async () => { const result = await reviseDocument(projectId, selectedId); await openDocument(result.artifact_id); return "已派生新的草稿版本"; })}>派生新草稿</button>
                </>
              }
            >
              <div className={`status-pill ${timeline.current_layer === "approved" ? "status-green" : timeline.current_layer === "submitted" ? "status-amber" : "status-blue"}`}>
                {LAYER_LABEL[timeline.current_layer]}
              </div>

              <div className="tabs">
                {TABS.map((item) => (
                  <button key={item.id} type="button" className={`tab ${tab === item.id ? "tab-active" : ""}`.trim()} data-testid={`document-tab-${item.id}`} onClick={() => setTab(item.id)}>
                    {item.label}
                  </button>
                ))}
              </div>

              {tab === "timeline" && (
                <div className="list" data-testid="document-revisions">
                  {timeline.revisions.map((revision) => (
                    <div className="list-item list-item-static" key={revision.artifact_id}>
                      <span className={`document-layer document-layer-${revision.layer}`}>{LAYER_LABEL[revision.layer]}</span>
                      <div className="item-copy">
                        <strong>第 {revision.revision} 版 · {revision.content_hash.slice(0, 10)}</strong>
                        <small>{revision.traceability.created_by}（{revision.traceability.created_by_kind}）{revision.traceability.git_commit ? ` · ${revision.traceability.git_commit}` : ""}{revision.traceability.approved_by ? ` · 批准人 ${revision.traceability.approved_by}` : ""}</small>
                        <small>证据 {revision.evidence.length} 项{revision.downstream_allowed ? " · 允许下游引用" : " · 不进入下游"}</small>
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {tab === "diff" && (
                <div className="grid" data-testid="document-diff">
                  {diff ? (
                    <>
                      <div className="document-diff-stats" data-testid="document-diff-stats">
                        <span>第 {diff.from_revision} → {diff.to_revision} 版</span>
                        <span>+{diff.stats.added_lines} / -{diff.stats.removed_lines} · {diff.stats.hunks} 段</span>
                        <span>{diff.identical ? "内容一致" : "内容有差异"}</span>
                        {diff.git_commit.changed && <span>Git: {diff.git_commit.from ?? "—"} → {diff.git_commit.to ?? "—"}</span>}
                      </div>
                      <pre className="code-block diff-body">{diff.unified_diff.slice(0, 80).join("\n") || "（无差异行）"}</pre>
                      <button className="button button-secondary" data-testid="document-merge" disabled={busy !== "" || current?.layer !== "draft"} onClick={() => void run("merge", async () => { await mergeDocument(projectId, selectedId, diff.from_revision, "工作台合并确认"); return `已按第 ${diff.from_revision} 版内容合并出新的草稿`; })}>
                        按第 {diff.from_revision} 版合并
                      </button>
                    </>
                  ) : <EmptyState>当前只有一个版本，暂无可比较差异</EmptyState>}
                </div>
              )}

              {tab === "comments" && (
                <div className="document-comments" data-testid="document-comments">
                  <div className="form-row">
                    <select value={commentKind} onChange={(event) => setCommentKind(event.target.value as "comment" | "suggestion")} style={{ width: "auto" }}>
                      <option value="comment">评论</option>
                      <option value="suggestion">建议</option>
                    </select>
                    <input value={commentDraft} onChange={(event) => setCommentDraft(event.target.value)} placeholder="写下评论或建议" data-testid="document-comment-input" />
                    <button className="button button-secondary" data-testid="document-comment-submit" disabled={busy !== "" || !commentDraft.trim()} onClick={() => void run("comment", async () => { await addDocumentComment(projectId, selectedId, commentDraft.trim(), commentKind); setCommentDraft(""); return commentKind === "suggestion" ? "建议已记录" : "评论已记录"; })}>
                      提交
                    </button>
                  </div>
                  {comments.length ? comments.map((comment) => (
                    <div className="document-comment-item" key={comment.id}>
                      <strong>{comment.kind === "suggestion" ? "建议" : "评论"}{comment.anchor ? ` · ${comment.anchor}` : ""}</strong>
                      <p>{comment.body}</p>
                      <small>{comment.actor}（{comment.actor_kind}） · {LAYER_LABEL[comment.layer] ?? comment.layer}</small>
                    </div>
                  )) : <EmptyState>还没有评论或建议</EmptyState>}
                </div>
              )}

              {tab === "snapshots" && (
                <div className="document-snapshots" data-testid="document-snapshots">
                  <div className="form-row">
                    <input value={snapshotLabel} onChange={(event) => setSnapshotLabel(event.target.value)} placeholder="快照标签（例如：初稿检查点）" />
                    <button className="button button-secondary" data-testid="document-snapshot" disabled={busy !== ""} onClick={() => void run("snapshot", async () => { const snapshot = await createDocumentSnapshot(projectId, selectedId, snapshotLabel.trim()); setSnapshotLabel(""); return `已创建快照（第 ${snapshot.revision} 版）`; })}>
                      创建快照
                    </button>
                  </div>
                  {snapshots.length ? snapshots.map((snapshot) => (
                    <div className="document-snapshot-item" key={snapshot.id}>
                      <strong>第 {snapshot.revision} 版 · {snapshot.label || "未命名"}</strong>
                      <small>{snapshot.content_hash.slice(0, 12)} · {snapshot.actor}</small>
                    </div>
                  )) : <EmptyState>还没有快照</EmptyState>}
                </div>
              )}

              {tab === "relations" && (
                <div className="document-relations" data-testid="document-relations">
                  <div className="form-row">
                    <select value={relationTargetType} onChange={(event) => setRelationTargetType(event.target.value)} style={{ width: "auto" }}>
                      <option value="figure">图表</option>
                      <option value="result_table">结果表</option>
                      <option value="artifact">成果物</option>
                      <option value="run">运行</option>
                    </select>
                    <input value={relationTargetId} onChange={(event) => setRelationTargetId(event.target.value)} placeholder="目标 ID" data-testid="document-relation-target" />
                    <input value={relationParagraph} onChange={(event) => setRelationParagraph(event.target.value)} placeholder="文档段落" data-testid="document-relation-paragraph" />
                    <button className="button button-secondary" data-testid="document-relation-link" disabled={busy !== "" || !relationTargetId.trim() || !relationParagraph.trim()} onClick={() => void run("relation", async () => { await linkDocumentRelation(projectId, selectedId, { target_type: relationTargetType, target_id: relationTargetId.trim(), paragraph: relationParagraph.trim() }); setRelationParagraph(""); return "关系已关联"; })}>
                      关联
                    </button>
                    <button className="button button-secondary" data-testid="document-impact" disabled={busy !== "" || !relationTargetId.trim()} onClick={() => void run("impact", async () => { const result = await getImpactLookup(projectId, relationTargetType, relationTargetId.trim()); setImpact(result); return `受影响 ${result.affected_count} 处`; })}>
                      查影响面
                    </button>
                  </div>
                  {relations.length ? relations.map((relation) => (
                    <div className="document-relation-item" key={relation.id}>
                      <strong>{relation.target_type} · {relation.paragraph}</strong>
                      <small>{relation.target_id.slice(0, 12)} · {relation.actor}</small>
                    </div>
                  )) : <EmptyState>还没有建立结论-图表-段落关系</EmptyState>}
                  {impact && (
                    <div className="document-impact-result" data-testid="document-impact-result">
                      <strong>受影响 {impact.affected_count} 处</strong>
                      <small>段落：{impact.paragraphs.join("、") || "—"}</small>
                    </div>
                  )}
                </div>
              )}

              <div className="document-collab" data-testid="document-collab">
                <div className="document-collab-heading">
                  <span className="eyebrow">协作编辑</span>
                  <span className={`document-collab-status ${collabStatus.connected ? "is-online" : ""}`} data-testid="document-collab-status">
                    {collabStatus.connected ? "已连接" : "连接中"}
                  </span>
                  <small data-testid="document-collab-presence">在线 {collabStatus.peers} 人</small>
                  {(() => {
                    const dirty = collabText !== savedText;
                    return (
                      <small
                        data-testid="document-collab-dirty"
                        style={{ marginLeft: "auto", color: dirty ? "var(--amber, var(--amber))" : "var(--muted)" }}
                      >
                        {dirty ? "有未保存的修改" : savedAt ? `已保存 ${new Date(savedAt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}` : "与平台内容一致"}
                      </small>
                    );
                  })()}
                  <button
                    className="button button-primary"
                    style={{ padding: "4px 10px", fontSize: 12 }}
                    disabled={saving || collabText === savedText || !selectedId || current?.layer === "approved"}
                    data-testid="document-collab-save"
                    onClick={() => void handleSaveDraft()}
                  >
                    {saving ? <RefreshCcw size={13} className="spin" /> : null} 保存草稿
                  </button>
                </div>
                <textarea className="document-collab-editor" data-testid="document-collab-editor" rows={6} value={collabText} onChange={(event) => handleCollabEdit(event.target.value)} placeholder="多人可同时编辑；编辑通过 Yjs CRDT 合并，不会互相覆盖" />
                {draftInfo && (
                  <small className="document-collab-hint" data-testid="document-draft-status">
                    服务端草稿 r{draftInfo.revision} · 最近保存 {formatTime(draftInfo.updatedAt)} · 保存者 {draftInfo.updatedBy}
                    （刷新或换设备后会自动恢复）
                  </small>
                )}
                <small className="document-collab-hint">
                  {current?.layer === "approved"
                    ? "该文档已批准，内容不可再修改（服务端会拒绝保存）；如需改动请先「派生新草稿」。"
                    : "编辑会自动存成服务端草稿（每 5 秒一次，刷新或换设备后自动恢复）；「保存草稿」写回平台内容，「提交待审」把已保存的内容冻结为提交版本。他人先保存时会让你选以谁为准，不会互相覆盖。"}
                </small>
              </div>
            </Panel>
          ) : (
            <Panel title="选择文档" subtitle="左侧选择一份文档查看三层版本、差异与证据">
              {busy === "load" ? <EmptyState><RefreshCcw size={15} className="spin" /> 正在读取…</EmptyState> : <EmptyState><FileText size={16} /> 尚未选择文档</EmptyState>}
            </Panel>
          )}
        </div>
      </section>

      {conflict && (
        <Modal
          title="这份文档已被他人修改"
          subtitle={`最新草稿来自 ${conflict.theirsBy}：请选择以谁为准（不会自动覆盖任何一方）`}
          wide
          testId="document-conflict-modal"
          onClose={() => setConflict(null)}
          actions={
            <>
              <button
                className="button button-secondary"
                data-testid="document-conflict-take-theirs"
                onClick={() => { setCollabText(conflict.theirs); setConflict(null); notify("已采用他人版本（可继续编辑后再保存）"); }}
              >
                采用他人版本
              </button>
              <button
                className="button button-primary"
                data-testid="document-conflict-keep-mine"
                onClick={() => { setConflict(null); draftDirty.current = true; void flushDraft(conflict.mine); notify("已用我的版本保存为服务端草稿"); }}
              >
                保留我的版本并保存
              </button>
            </>
          }
        >
          <div style={{ display: "grid", gap: 10 }}>
            <div className="field">
              <span>我的编辑（{conflict.mine.length} 字）</span>
              <pre className="code-block" style={{ whiteSpace: "pre-wrap", maxHeight: 160, overflow: "auto" }}>{conflict.mine.slice(0, 1200)}</pre>
            </div>
            <div className="field">
              <span>服务端最新（来自 {conflict.theirsBy}，{conflict.theirs.length} 字）</span>
              <pre className="code-block" style={{ whiteSpace: "pre-wrap", maxHeight: 160, overflow: "auto" }}>{conflict.theirs.slice(0, 1200)}</pre>
            </div>
          </div>
        </Modal>
      )}
    </div>
  );
}