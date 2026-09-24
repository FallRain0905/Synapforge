"use client";

import { useCallback, useEffect, useState } from "react";
import { BookOpen, Database, FileText, Plus, RefreshCcw, Send, Upload, Zap } from "lucide-react";
import { PageHeading } from "../../components/shell";
import { EmptyState, Modal, Panel, StatusPill } from "../../components/ui";
import { KnowledgeBase, KbDocument, addKbDocument, createKb, errorMessage, indexKbDocuments, listKbDocuments, listKbs } from "../../lib/api";
import { useWorkspace } from "../../lib/workspace";

const SOURCE_LABEL: Record<string, string> = { manual: "手动", drive: "云盘", artifact: "成果物", convert: "MinerU" };

export default function KbPage() {
  const { notify, projectId, project } = useWorkspace();
  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [docs, setDocs] = useState<KbDocument[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [docModalOpen, setDocModalOpen] = useState(false);
  const [form, setForm] = useState<{ name: string; description: string; scope: "personal" | "project" }>({ name: "", description: "", scope: "personal" });
  const [docForm, setDocForm] = useState({ title: "", content_md: "" });

  const loadKbs = useCallback(async () => {
    try {
      const list = await listKbs();
      setKbs(list);
      if (list.length > 0 && !selectedId) setSelectedId(list[0].id);
    } catch { notify("知识库列表读取失败"); } finally { setLoading(false); }
  }, [notify, selectedId]);

  const loadDocs = useCallback(async (kbId: string) => {
    if (!kbId) return;
    try { setDocs(await listKbDocuments(kbId)); } catch { notify("文档读取失败"); }
  }, [notify]);

  useEffect(() => { void loadKbs(); }, [loadKbs]);
  useEffect(() => { if (selectedId) void loadDocs(selectedId); }, [selectedId, loadDocs]);

  const handleCreate = async () => {
    if (!form.name.trim()) return;
    setBusy("create");
    try {
      // 归属显式化：个人库可分享给其他成员；项目库复用项目授权，出现在该项目的知识库列表里。
      const kb = await createKb({
        name: form.name.trim(),
        description: form.description,
        project_id: form.scope === "project" ? projectId : null,
      });
      setCreateOpen(false); setForm({ name: "", description: "", scope: "personal" });
      await loadKbs(); setSelectedId(kb.id);
      notify(form.scope === "project" ? "项目知识库已创建" : "个人知识库已创建");
    } catch (error) { notify(errorMessage(error, "创建失败")); } finally { setBusy(""); }
  };

  const handleAddDoc = async () => {
    if (!selectedId || !docForm.title.trim() || !docForm.content_md.trim()) return;
    setBusy("add-doc");
    try {
      await addKbDocument(selectedId, { title: docForm.title.trim(), content_md: docForm.content_md });
      setDocModalOpen(false); setDocForm({ title: "", content_md: "" });
      await loadDocs(selectedId);
      notify("文档已登记（用「触发索引」开始构建索引）");
    } catch { notify("文档登记失败"); } finally { setBusy(""); }
  };

  const handleIndex = async () => {
    if (!selectedId) return;
    const pending = docs.filter((doc) => doc.index_status !== "indexed").map((doc) => doc.id);
    if (!pending.length) { notify("所有文档已索引"); return; }
    setBusy("index");
    try {
      const result = await indexKbDocuments(selectedId, pending);
      await loadDocs(selectedId);
      notify(`索引完成：${result.indexed} 成功${result.failed ? `、${result.failed} 失败` : ""}`);
    } catch (error) { notify(error instanceof Error ? error.message : "索引失败"); } finally { setBusy(""); }
  };

  const selected = kbs.find((kb) => kb.id === selectedId);

  return (
    <div className="page-content" id="kb">
      <PageHeading
        hint="文档知识库：登记 Markdown 文档、构建检索索引、支持项目内问答"
        actions={
          <>
            <button className="button button-secondary" data-testid="kb-add-doc" disabled={!selectedId || busy !== ""} onClick={() => setDocModalOpen(true)}><FileText size={15} /> 添加文档</button>
            <button className="button button-primary" data-testid="kb-index" disabled={!selectedId || busy !== ""} onClick={() => void handleIndex()}>
              {busy === "index" ? <RefreshCcw size={15} className="spin" /> : <Zap size={15} />} 触发索引
            </button>
            <button className="button button-secondary" onClick={() => setCreateOpen(true)}><Plus size={15} /> 新建知识库</button>
          </>
        }
      />

      <section className="grid grid-main-side">
        <Panel title="知识库列表" subtitle={`${kbs.length} 个（含项目库与个人库）`} testId="kb-list">
          {loading ? <EmptyState>正在加载…</EmptyState> : (
            <div className="list" style={{ maxHeight: 480, overflowY: "auto" }}>
              {kbs.map((kb) => (
                <button key={kb.id} type="button" className={`list-item list-item-button ${selectedId === kb.id ? "list-item-active" : ""}`.trim()} data-testid={`kb-item-${kb.id}`} onClick={() => setSelectedId(kb.id)}>
                  <span className="icon-tile icon-tile-blue"><BookOpen size={15} /></span>
                  <span className="item-copy">
                    <strong>{kb.name}</strong>
                    <small>{kb.project_id ? "项目库" : "个人库"} · {kb.document_count} 个文档{kb.shares.length > 0 ? ` · 已分享 ${kb.shares.length} 人` : ""}</small>
                  </span>
                </button>
              ))}
              {!kbs.length && <EmptyState>还没有知识库</EmptyState>}
            </div>
          )}
        </Panel>

        <Panel title={selected?.name ?? "选择知识库"} subtitle="文档与索引状态" testId="kb-documents">
          {selectedId ? (
            <div className="table">
              <div className="table-head"><span>文档</span><span>来源</span><span>索引</span><span>哈希</span><span /></div>
              {docs.map((doc) => (
                <div className="table-row" key={doc.id}>
                  <div className="table-title"><strong>{doc.title}</strong><small>{doc.content_md_bytes ?? doc.content_md.length} 字节</small></div>
                  <span className="chip" data-label="来源">{SOURCE_LABEL[doc.source_type] ?? doc.source_type}</span>
                  <span data-label="索引"><StatusPill status={doc.index_status === "indexed" ? "APPROVED" : doc.index_status === "failed" ? "BLOCKED" : "DRAFT"} label={doc.index_status} /></span>
                  <span className="hint" data-label="哈希">{doc.content_hash.slice(0, 10)}</span>
                </div>
              ))}
              {!docs.length && <EmptyState><Database size={16} /> 还没有文档；添加后触发索引构建</EmptyState>}
            </div>
          ) : <EmptyState>选择或创建一个知识库</EmptyState>}
        </Panel>
      </section>

      {createOpen && (
        <Modal title="新建知识库" subtitle="与个人云盘不同：知识库只存文档，用于检索与索引" testId="kb-create-modal" onClose={() => setCreateOpen(false)} actions={
          <>
            <button className="button button-secondary" onClick={() => setCreateOpen(false)}>取消</button>
            <button className="button button-primary" disabled={busy !== "" || !form.name.trim() || (form.scope === "project" && !projectId)} data-testid="kb-create-submit" onClick={() => void handleCreate()}>创建</button>
          </>
        }>
          <label className="field"><span>名称</span><input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="例如：CUMCM 2025 赛题资料" /></label>
          <label className="field"><span>描述</span><input value={form.description} onChange={(event) => setForm({ ...form, description: event.target.value })} placeholder="可选" /></label>
          <div className="field">
            <span>归属</span>
            <div className="chips">
              <button
                type="button"
                className={`chip ${form.scope === "personal" ? "status-blue" : ""}`.trim()}
                data-testid="kb-scope-personal"
                onClick={() => setForm({ ...form, scope: "personal" })}
              >
                个人知识库（可分享给他人）
              </button>
              <button
                type="button"
                className={`chip ${form.scope === "project" ? "status-blue" : ""}`.trim()}
                data-testid="kb-scope-project"
                disabled={!projectId}
                onClick={() => setForm({ ...form, scope: "project" })}
              >
                项目知识库{project ? `（${project.name}）` : ""}
              </button>
            </div>
            <small className="hint">
              {form.scope === "project"
                ? "项目库复用项目授权：项目成员都能看到，退出项目即失去访问。"
                : "个人库默认只自己可见，可在知识库页分享给指定成员。"}
            </small>
          </div>
        </Modal>
      )}

      {docModalOpen && (
        <Modal title="添加文档" subtitle="登记 Markdown 文本（云盘 PDF 先经 MinerU 转换）" testId="kb-doc-modal" onClose={() => setDocModalOpen(false)} actions={
          <>
            <button className="button button-secondary" onClick={() => setDocModalOpen(false)}>取消</button>
            <button className="button button-primary" disabled={busy !== "" || !docForm.title.trim() || !docForm.content_md.trim()} onClick={() => void handleAddDoc()}>登记</button>
          </>
        }>
          <label className="field"><span>标题</span><input value={docForm.title} onChange={(event) => setDocForm({ ...docForm, title: event.target.value })} placeholder="数据说明.md" /></label>
          <label className="field"><span>Markdown 正文</span><textarea rows={8} value={docForm.content_md} onChange={(event) => setDocForm({ ...docForm, content_md: event.target.value })} placeholder="# 文档内容" /></label>
        </Modal>
      )}
    </div>
  );
}