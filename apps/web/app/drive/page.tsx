"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Cloud, FileText, FolderPlus, RefreshCcw, Trash2, Upload } from "lucide-react";
import { PageHeading, formatTime } from "../../components/shell";
import { ConfirmDialog, EmptyState, Metric, Panel, Progress } from "../../components/ui";
import {
  DriveFile,
  DriveUsage,
  deleteDriveFile,
  enqueueDriveConversion,
  importDriveFile,
  listDriveFiles,
  uploadDriveFile,
} from "../../lib/api";
import { useWorkspace } from "../../lib/workspace";

function formatBytes(value: number) {
  if (value >= 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${value} B`;
}

const ARCHIVE_LABEL = "压缩包";

export default function DrivePage() {
  const { projectId, notify, refresh } = useWorkspace();
  const [usage, setUsage] = useState<DriveUsage | null>(null);
  const [files, setFiles] = useState<DriveFile[]>([]);
  const [busy, setBusy] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<DriveFile | null>(null);
  const [importTarget, setImportTarget] = useState<DriveFile | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try {
      const listing = await listDriveFiles();
      setUsage(listing.usage);
      setFiles(listing.files);
    } catch {
      notify("个人云盘读取失败");
    }
  }, [notify]);

  useEffect(() => {
    void load();
  }, [load]);

  const handleUpload = async (fileList: FileList | null) => {
    if (!fileList?.length) return;
    setBusy("upload");
    try {
      for (const file of Array.from(fileList)) {
        await uploadDriveFile(file);
      }
      await load();
      notify(`已上传 ${fileList.length} 个文件`);
    } catch (error) {
      notify(error instanceof Error ? error.message : "上传失败");
    } finally {
      setBusy("");
      if (fileInput.current) fileInput.current.value = "";
    }
  };

  const handleDelete = async (file: DriveFile) => {
    setBusy(`delete-${file.id}`);
    try {
      await deleteDriveFile(file.id);
      await load();
      notify("文件已删除");
    } catch (error) {
      notify(error instanceof Error ? error.message : "删除失败");
    } finally {
      setBusy("");
    }
  };

  const handleConvert = async (file: DriveFile) => {
    setBusy(`convert-${file.id}`);
    try {
      const job = await enqueueDriveConversion({ id: file.id, name: file.name });
      notify(`已入队转换（job: ${job.id.slice(0, 8)}）；完成后可在知识库页写入`);
    } catch (error) {
      notify(error instanceof Error ? error.message : "转换入队失败");
    } finally {
      setBusy("");
    }
  };

  const handleImport = async (file: DriveFile) => {
    if (!projectId) {
      notify("请先选择项目");
      return;
    }
    setBusy(`import-${file.id}`);
    try {
      const result = await importDriveFile(projectId, file.id);
      await load();
      await refresh();
      setImportTarget(null);
      notify(`已导入项目空间：${result.artifact_type}`);
    } catch (error) {
      notify(error instanceof Error ? error.message : "导入失败");
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="page-content" id="drive">
      <PageHeading
        hint="成员级私人文件暂存：200MB 配额，压缩包可识别，一键加入当前项目空间"
        actions={
          <>
            <button className="button button-primary" data-testid="drive-upload-open" disabled={busy !== ""} onClick={() => fileInput.current?.click()}>
              {busy === "upload" ? <RefreshCcw size={15} className="spin" /> : <Upload size={15} />} 上传文件
            </button>
            <input ref={fileInput} type="file" multiple hidden data-testid="drive-file-input" onChange={(event) => void handleUpload(event.target.files)} />
          </>
        }
      />

      <section className="metrics-grid">
        <Metric label="已用空间" value={usage ? formatBytes(usage.used_bytes) : "—"} detail={`配额 200MB · 剩余 ${usage ? formatBytes(usage.free_bytes) : "—"}`} />
        <Metric label="文件数" value={usage?.file_count ?? "—"} detail="同内容自动去重" />
        <Metric label="压缩包" value={files.filter((file) => file.is_archive).length} detail="zip/tar/gz/7z/rar" />
        <Metric label="已导入项目" value={files.filter((file) => file.project_ids.length > 0).length} detail="登记为项目成果物" tone="positive" />
      </section>

      <Panel
        title="我的文件"
        subtitle="上传后可一键加入当前项目空间；被项目引用的文件不可删除"
        testId="drive-file-list"
        actions={<button className="text-button" onClick={() => void load()}>刷新</button>}
      >
        {usage ? (
          <div data-testid="drive-usage">
            <Progress label="云盘用量" value={usage.used_percent} detail={`${formatBytes(usage.used_bytes)} / 200MB（${usage.file_count} 个文件）`} />
          </div>
        ) : null}
        <div className="table">
          <div className="table-head"><span>文件</span><span>类型</span><span>大小</span><span>状态</span><span /></div>
          {files.map((file) => (
            <div className="table-row" key={file.id} data-testid={`drive-file-${file.id}`}>
              <div className="table-title">
                <strong>{file.name}</strong>
                <small>上传于 {formatTime(file.created_at)} · sha256 {file.content_hash.slice(0, 10)}</small>
              </div>
              <span className={`chip ${file.is_archive ? "status-violet" : ""}`} data-label="类型">
                {file.is_archive ? ARCHIVE_LABEL : file.name.split(".").pop()?.toUpperCase() ?? "文件"}
              </span>
              <span className="hint" data-label="大小">{formatBytes(file.size_bytes)}</span>
              {file.project_ids.length > 0 ? (
                <span className="badge status-green" data-label="状态">已导入 {file.project_ids.length} 个项目</span>
              ) : (
                <span className="badge status-neutral" data-label="状态">私人文件</span>
              )}
              <div className="row-actions">
                <button className="text-button" data-testid="drive-import" disabled={busy !== ""} onClick={() => setImportTarget(file)}>
                  <FolderPlus size={14} /> 加入项目
                </button>
                <button
                  className="text-button"
                  data-testid="drive-convert"
                  disabled={busy !== ""}
                  onClick={() => void handleConvert(file)}
                  title="PDF 转 Markdown 后写入知识库（需在设置中配置 MinerU Token）"
                >
                  <FileText size={14} /> 转换入知识库
                </button>
                <button className="text-button" disabled={busy !== "" || file.project_ids.length > 0} onClick={() => setDeleteTarget(file)}>
                  <Trash2 size={14} /> 删除
                </button>
              </div>
            </div>
          ))}
        </div>
        {!files.length && <EmptyState><Cloud size={16} /> 云盘还是空的；上传数据、压缩包或论文素材后可一键加入项目空间</EmptyState>}
      </Panel>

      {importTarget && (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => setImportTarget(null)}>
          <div className="modal" role="dialog" aria-modal="true" data-testid="drive-import-modal" onMouseDown={(event) => event.stopPropagation()}>
            <div className="modal-heading">
              <div>
                <h2>加入项目空间</h2>
                <p>{importTarget.name}（{formatBytes(importTarget.size_bytes)}）将登记为当前项目的成果物</p>
              </div>
              <button className="app-icon" aria-label="关闭" onClick={() => setImportTarget(null)}>×</button>
            </div>
            <div className="hint">
              导入后文件内容会进入项目对象存储并登记 SHA-256；正式下游使用仍需通过项目审批门禁。
              {importTarget.is_archive && " 压缩包会按 problem_source 类型登记，解包属后续工作。"}
            </div>
            <div className="modal-actions">
              <button className="button button-secondary" onClick={() => setImportTarget(null)}>取消</button>
              <button className="button button-primary" data-testid="drive-import-confirm" disabled={busy !== ""} onClick={() => void handleImport(importTarget)}>
                确认导入
              </button>
            </div>
          </div>
        </div>
      )}
      {deleteTarget && (
        <ConfirmDialog
          title="删除云盘文件？"
          description={
            <>
              <strong>{deleteTarget.name}</strong>
              <div style={{ marginTop: 6 }}>删除后无法恢复；已被项目引用的文件不能删除（服务端会拒绝）。</div>
            </>
          }
          confirmLabel="删除文件"
          tone="danger"
          busy={busy === `delete-${deleteTarget.id}`}
          testId="drive-delete-confirm"
          onCancel={() => setDeleteTarget(null)}
          onConfirm={() => { const target = deleteTarget; setDeleteTarget(null); void handleDelete(target); }}
        />
      )}
    </div>
  );
}
