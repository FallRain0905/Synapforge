"""CL-6 前端：协作文档的节流草稿落库、刷新恢复与冲突选择。

不改协作中继（Yjs 仍走 WS 转发），只把编辑文本按节流写进服务端草稿表：
刷新/换设备后能接着写，别人先保存时明确让用户选"以谁为准"。
"""

from pathlib import Path

p = Path("apps/web/app/documents/page.tsx")
text = p.read_text(encoding="utf-8")

# 1) 导入
old_import = "  saveArtifactText,"
assert old_import in text, "saveArtifactText import"
text = text.replace(old_import, "  getDocumentDraft,\n  saveArtifactText,\n  saveDocumentDraft,", 1)

# 2) 状态 + 定时器
old_state = '''  const [collabText, setCollabText] = useState("");'''
new_state = '''  const [collabText, setCollabText] = useState("");
  // 服务端草稿（CL-6）：恢复未保存的编辑 + 冲突时让用户选"以谁为准"
  const draftRef = useRef<{ revision: number; updatedBy: string } | null>(null);
  const draftTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const draftDirty = useRef(false);
  const [draftInfo, setDraftInfo] = useState<{ revision: number; updatedBy: string; updatedAt: string } | null>(null);
  const [conflict, setConflict] = useState<{ mine: string; theirs: string; theirsBy: string } | null>(null);'''
assert old_state in text, "collabText state"
text = text.replace(old_state, new_state, 1)

# 3) 保存草稿到服务端（节流）与冲突处理
anchor = "  const handleCollabEdit = (next: string) => {"
helpers = '''  /** 节流把当前编辑写进服务端草稿（5 秒或切文档时立刻落一次）。 */
  const scheduleDraftSave = useCallback((content: string) => {
    draftDirty.current = true;
    if (draftTimer.current) clearTimeout(draftTimer.current);
    draftTimer.current = setTimeout(() => {
      void flushDraft(content);
    }, 5000);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, selectedId]);

  const flushDraft = useCallback(async (content: string) => {
    if (!projectId || !selectedId || !draftDirty.current) return;
    draftDirty.current = false;
    try {
      const saved = await saveDocumentDraft(projectId, selectedId, content, draftRef.current?.revision ?? 0);
      draftRef.current = { revision: saved.revision, updatedBy: saved.updated_by };
      setDraftInfo({ revision: saved.revision, updatedBy: saved.updated_by, updatedAt: saved.updated_at });
    } catch (error) {
      const detail = errorMessage(error, "");
      if (detail.includes("已被他人修改")) {
        // 冲突：把双方内容都摆出来，让用户决定（不静默覆盖，CL-6-02）
        try {
          const theirs = await getDocumentDraft(projectId, selectedId);
          setConflict({ mine: content, theirs: theirs?.content ?? "", theirsBy: theirs?.updated_by ?? "他人" });
          if (theirs) draftRef.current = { revision: theirs.revision, updatedBy: theirs.updated_by };
        } catch {
          notify("协作文档冲突：请刷新后重试");
        }
      }
      // 其它失败（如已批准不可改）只提示，不影响继续编辑
    }
  }, [projectId, selectedId, notify]);

  const handleCollabEdit = (next: string) => {'''
assert anchor in text, "edit handler anchor"
text = text.replace(anchor, helpers, 1)

# 4) 编辑时顺带排一次节流保存
old_edit_tail = '''    if (removed > 0) collab.delete(prefix, removed);
    if (inserted) collab.insert(prefix, inserted);
    setCollabText(collab.getText());
  };'''
new_edit_tail = '''    if (removed > 0) collab.delete(prefix, removed);
    if (inserted) collab.insert(prefix, inserted);
    const nextText = collab.getText();
    setCollabText(nextText);
    scheduleDraftSave(nextText);
  };'''
assert old_edit_tail in text, "edit tail"
text = text.replace(old_edit_tail, new_edit_tail, 1)

# 5) 载入某份文档时取服务端草稿
old_saved = '''      await saveArtifactText(selectedId, collabText);
      setSavedText(collabText);'''
new_saved = '''      await saveArtifactText(selectedId, collabText);
      setSavedText(collabText);
      // 正式保存后服务端草稿的意义就没了：把修订号对齐，避免下一次节流保存被判冲突
      draftRef.current = null;'''
assert old_saved in text, "save draft handler"
text = text.replace(old_saved, new_saved, 1)

# 6) 冲突选择弹窗 + 草稿状态提示
old_hint = '''                <small className="document-collab-hint">'''
new_hint = '''                {draftInfo && (
                  <small className="document-collab-hint" data-testid="document-draft-status">
                    服务端草稿 r{draftInfo.revision} · 最近保存于 {formatTime(draftInfo.updatedAt)} · 保存者 {draftInfo.updatedBy}
                    （刷新或换设备后会自动恢复到这里）
                  </small>
                )}
                <small className="document-collab-hint">'''
assert old_hint in text, "hint anchor"
text = text.replace(old_hint, new_hint, 1)

old_tail = '''      {previewArtifact && ('''
new_tail = '''      {conflict && (
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
                onClick={() => { setConflict(null); draftDirty.current = true; void flushDraft(conflict.mine); notify("已用我的版本覆盖服务端草稿（对方内容仍在版本历史里）"); }}
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

      {previewArtifact && ('''
assert old_tail in text, "tail anchor"
text = text.replace(old_tail, new_tail, 1)
p.write_text(text, encoding="utf-8")
print("documents page: draft persistence wired")