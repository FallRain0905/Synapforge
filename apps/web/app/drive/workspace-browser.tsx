"use client";

/**
 * Agent 工作区浏览器（FM-4）。
 *
 * 与个人云盘那半边的**唯一**区别是语义：这里每个动作都是"入队一个操作 → Agent 领到才执行"。
 * 所以这个组件从不乐观地改本地列表：改完之后**重新拉一次目录**，状态一律以平台上的操作为准
 * （queued → claimed → running → succeeded/failed 全程可见，失败能重试、排队能取消）。
 *
 * 三条计划里的口径：
 * - **Agent 离线只显示缓存的元数据**并标注"最后同步"时间，不假装实时；
 * - **保护路径不显示为可执行**（删/改名/移动按钮直接不给）；
 * - 页面上**不出现宿主机绝对路径**（工作区只有哈希标识与相对路径）。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  CheckSquare,
  ChevronRight,
  Cloud,
  Copy,
  Download,
  FileArchive,
  FileText,
  Folder,
  FolderPlus,
  Move,
  Pencil,
  RefreshCcw,
  RotateCcw,
  Search,
  Square,
  Trash2,
  Upload,
  UploadCloud,
  WifiOff,
  XCircle,
} from "lucide-react";
import { ConfirmDialog, EmptyState, Panel, Progress } from "../../components/ui";
import {
  AgentWorkspace,
  WorkspaceEntry,
  WorkspaceOperation,
  cancelWorkspaceOperation,
  copyWorkspaceFileToDrive,
  createWorkspaceTransfer,
  getWorkspaceOperation,
  getWorkspaceTransferContent,
  isWorkspaceOperationFinished,
  putWorkspaceTransferChunked,
  runWorkspaceOperation,
  saveTransferToDrive,
} from "../../lib/api";
import { formatTime } from "../../components/shell";

const STATUS_LABEL: Record<string, string> = {
  queued: "排队中",
  claimed: "已领取",
  running: "执行中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
  expired: "已过期（Agent 失联）",
};

const STATUS_TONE: Record<string, string> = {
  queued: "status-neutral",
  claimed: "status-violet",
  running: "status-violet",
  succeeded: "status-green",
  failed: "status-red",
  cancelled: "status-neutral",
  expired: "status-red",
};

function formatBytes(value: number) {
  if (value >= 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${value} B`;
}

/** 目录缓存：离线时页面显示的就是它（并标注"最后同步"时间，不假装实时）。 */
function cacheKey(workspaceId: string, path: string) {
  return `map.workspaceCache.${workspaceId}.${path || "__root__"}`;
}

type CachedListing = { entries: WorkspaceEntry[]; protected: string[]; truncated: boolean; syncedAt: string };

function readCache(workspaceId: string, path: string): CachedListing | null {
  try {
    const raw = window.localStorage.getItem(cacheKey(workspaceId, path));
    return raw ? (JSON.parse(raw) as CachedListing) : null;
  } catch {
    return null;
  }
}

function writeCache(workspaceId: string, path: string, value: CachedListing) {
  try {
    window.localStorage.setItem(cacheKey(workspaceId, path), JSON.stringify(value));
  } catch {
    /* 隐私模式下写不了：忽略（缓存只是体验优化） */
  }
}

async function sha256Hex(content: Blob): Promise<string> {
  const buffer = await content.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

function isProtected(workspace: AgentWorkspace, protectedFromAgent: string[], relative: string) {
  const rules = [...protectedFromAgent, ...(workspace.protected_paths ?? [])];
  if (!relative) return true; // 工作区根：永远不给删/改名/移动
  return rules.some((rule) => relative === rule || relative.startsWith(`${rule}/`));
}

export function WorkspaceBrowser({
  workspace,
  notify,
  onWorkspaceChanged,
}: {
  workspace: AgentWorkspace;
  notify: (message: string) => void;
  onWorkspaceChanged: () => void;
}) {
  const [path, setPath] = useState("");
  const [entries, setEntries] = useState<WorkspaceEntry[]>([]);
  const [protectedPaths, setProtectedPaths] = useState<string[]>([]);
  const [truncated, setTruncated] = useState(false);
  const [syncedAt, setSyncedAt] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState("");
  const [operations, setOperations] = useState<WorkspaceOperation[]>([]);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [detail, setDetail] = useState<WorkspaceEntry | null>(null);
  const [creating, setCreating] = useState("");
  const [renaming, setRenaming] = useState<{ relative: string; name: string } | null>(null);
  const [pendingDelete, setPendingDelete] = useState<WorkspaceEntry[] | null>(null);
  const [moveTargets, setMoveTargets] = useState<WorkspaceEntry[] | null>(null);
  const [moveDestination, setMoveDestination] = useState("");
  const [uploads, setUploads] = useState<{ name: string; percent: number; error: string }[]>([]);
  const [saveState, setSaveState] = useState<{
    entry: WorkspaceEntry;
    transferId: string;
    status: string;
    conflict: boolean;
    name: string;
    error: string;
  } | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const offline = workspace.status !== "online";
  const breadcrumb = useMemo(() => ["工作区", ...path.split("/").filter(Boolean)], [path]);

  // 本地消息条（工作区这侧没有 toast 队列，用一行"最近结果"如实展示成败）
  const [notes, setNotes] = useState<string[]>([]);
  const note = useCallback((message: string) => setNotes((previous) => [message, ...previous].slice(0, 3)), []);

  const track = useCallback((operation: WorkspaceOperation) => {
    setOperations((previous) => {
      const next = previous.filter((item) => item.id !== operation.id);
      return [operation, ...next].slice(0, 12);
    });
  }, []);

  const applyCache = useCallback(
    (target: string) => {
      const cached = readCache(workspace.id, target);
      if (!cached) return false;
      setEntries(cached.entries);
      setProtectedPaths(cached.protected);
      setTruncated(cached.truncated);
      setSyncedAt(cached.syncedAt);
      return true;
    },
    [workspace.id],
  );

  const refresh = useCallback(
    async (target: string) => {
      // 离线：不再往队列里塞一个注定失败的 list（那会白等一轮），直接显示缓存并如实标注时间
      if (offline) {
        if (!applyCache(target)) notify("Agent 离线且本地没有这个目录的缓存：等它上线后再看这一层");
        return;
      }
      setLoading(true);
      try {
        const operation = await runWorkspaceOperation(
          workspace.id,
          { operation_type: "list", relative_path: target },
          { onTick: track, timeoutMs: offline ? 15_000 : 60_000 },
        );
        if (operation.status !== "succeeded") {
          notify(`目录读取失败：${operation.error_code ?? operation.status}`);
          return;
        }
        const result = operation.result ?? {};
        const nextEntries = (result.entries ?? []) as WorkspaceEntry[];
        const nextProtected = (result.protected ?? []) as string[];
        const nextTruncated = Boolean(result.truncated);
        const syncedAtValue = new Date().toISOString();
        setEntries(nextEntries);
        setProtectedPaths(nextProtected);
        setTruncated(nextTruncated);
        setSyncedAt(syncedAtValue);
        writeCache(workspace.id, target, {
          entries: nextEntries,
          protected: nextProtected,
          truncated: nextTruncated,
          syncedAt: syncedAtValue,
        });
      } catch (error) {
        notify(error instanceof Error ? error.message : "目录读取失败");
      } finally {
        setLoading(false);
      }
    },
    [applyCache, notify, offline, track, workspace.id],
  );

  useEffect(() => {
    setPath("");
    setEntries([]);
    setSelected(new Set());
    setDetail(null);
    applyCache(""); // 先把缓存的目录垫上（离线时这就是全部内容）
    void refresh("");
  }, [applyCache, refresh, workspace.id]);

  const openDirectory = (entry: WorkspaceEntry) => {
    setPath(entry.relative_path);
    setSelected(new Set());
    void refresh(entry.relative_path);
  };

  const goTo = (target: string) => {
    setPath(target);
    void refresh(target);
  };

  const goUp = () => {
    const parent = path.split("/").filter(Boolean).slice(0, -1).join("/");
    goTo(parent);
  };

  const run = useCallback(
    async (label: string, payload: { operation_type: string; relative_path?: string; arguments?: Record<string, any> }) => {
      setBusy(label);
      try {
        const operation = await runWorkspaceOperation(workspace.id, payload, { onTick: track, timeoutMs: offline ? 20_000 : 120_000 });
        await refresh(path);
        onWorkspaceChanged();
        return operation;
      } finally {
        setBusy("");
      }
    },
    [offline, onWorkspaceChanged, path, refresh, track, workspace.id],
  );

  const handleCreateDirectory = async () => {
    const name = creating.trim();
    if (!name) return;
    const target = path ? `${path}/${name}` : name;
    const operation = await run("mkdir", { operation_type: "mkdir", relative_path: target, arguments: { parents: false } });
    if (operation.status === "succeeded") {
      setCreating("");
      notify(`已新建文件夹 ${name}`);
    } else {
      notify(`新建失败：${operation.error_code ?? operation.status}`);
    }
  };

  const handleUpload = async (files: FileList | null) => {
    if (!files?.length) return;
    const picked = Array.from(files);
    setUploads(picked.map((file) => ({ name: file.name, percent: 0, error: "" })));
    for (const [index, file] of picked.entries()) {
      try {
        const digest = await sha256Hex(file);
        const transfer = await createWorkspaceTransfer({
          source_type: "drive",
          target_type: "workspace",
          workspace_id: workspace.id,
          expected_hash: digest,
          expected_size: file.size,
        });
        // 大文件走分片（断点续传 + 每片重试）；小文件一次 PUT
        const transferResult = await putWorkspaceTransferChunked(transfer.transfer.id, file, {
          onProgress: (percent) =>
            setUploads((previous) => previous.map((job, position) => (position === index ? { ...job, percent: Math.round(percent * 0.6) } : job))),
        });
        setUploads((previous) => previous.map((job, position) => (position === index ? { ...job, percent: 60 } : job)));
        if (transferResult.mode === "chunked" && transferResult.resumed) {
          // 续传：已经传过的片没重传，如实说一句
          note(`${file.name}：续传（跳过已传的 ${transferResult.resumed}/${transferResult.parts} 片）`);
        }
        const target = path ? `${path}/${file.name}` : file.name;
        const operation = await run(`upload-${file.name}`, {
          operation_type: "upload",
          relative_path: target,
          arguments: { transfer_id: transfer.transfer.id },
        });
        if (operation.status !== "succeeded") {
          throw new Error(operation.error_code ?? operation.status);
        }
        setUploads((previous) => previous.map((job, position) => (position === index ? { ...job, percent: 100 } : job)));
      } catch (error) {
        const message = error instanceof Error ? error.message : "上传失败";
        setUploads((previous) => previous.map((job, position) => (position === index ? { ...job, error: message } : job)));
        notify(`${file.name}：${message}`);
      }
    }
    if (fileInput.current) fileInput.current.value = "";
  };

  const handleDownload = async (entry: WorkspaceEntry) => {
    setBusy(`download-${entry.relative_path}`);
    try {
      const transfer = await createWorkspaceTransfer({ source_type: "workspace", target_type: "temp", workspace_id: workspace.id });
      const operation = await run("download", {
        operation_type: "download",
        relative_path: entry.relative_path,
        arguments: { transfer_id: transfer.transfer.id },
      });
      if (operation.status !== "succeeded") {
        throw new Error(operation.error_code ?? operation.status);
      }
      const blob = await getWorkspaceTransferContent(transfer.transfer.id);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = entry.name;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    } catch (error) {
      notify(error instanceof Error ? error.message : "下载失败");
    } finally {
      setBusy("");
    }
  };

  const handleDelete = async (targets: WorkspaceEntry[]) => {
    setBusy("delete");
    let done = 0;
    for (const entry of targets) {
      const operation = await run("delete", {
        operation_type: "delete",
        relative_path: entry.relative_path,
        arguments: entry.kind === "directory" ? { recursive: true } : {},
      });
      if (operation.status === "succeeded") done += 1;
      else notify(`${entry.name}：${operation.error_code ?? operation.status}`);
    }
    setBusy("");
    setSelected(new Set());
    setDetail(null);
    await refresh(path);
    if (done) notify(`已删除 ${done} 项（工作区里的删除不进回收站）`);
  };

  const handleMove = async () => {
    if (!moveTargets) return;
    setBusy("move");
    for (const entry of moveTargets) {
      const operation = await run("move", {
        operation_type: "move",
        relative_path: entry.relative_path,
        arguments: { destination: moveDestination },
      });
      if (operation.status !== "succeeded") notify(`${entry.name}：${operation.error_code ?? operation.status}`);
    }
    setBusy("");
    notify(`已移动到 ${moveDestination || "工作区根"}`);
    setMoveTargets(null);
    setSelected(new Set());
    await refresh(path);
  };

  const handleRename = async () => {
    if (!renaming) return;
    const name = renaming.name.trim();
    if (!name) return;
    const operation = await run("rename", { operation_type: "rename", relative_path: renaming.relative, arguments: { name } });
    if (operation.status === "succeeded") {
      setRenaming(null);
      notify(`已改名为 ${name}`);
    } else {
      notify(`改名失败：${operation.error_code ?? operation.status}`);
    }
  };

  const handleExtract = async (entry: WorkspaceEntry) => {
    const operation = await run("extract", { operation_type: "extract", relative_path: entry.relative_path, arguments: { destination: path } });
    if (operation.status === "succeeded") {
      const result = operation.result ?? {};
      notify(`解压完成：${result.files ?? 0} 个文件、${result.directories ?? 0} 个目录`);
    } else {
      notify(`解压失败：${operation.error_code ?? operation.status}`);
    }
  };

  const handleSaveToDrive = async (entry: WorkspaceEntry) => {
    setBusy(`save-${entry.relative_path}`);
    setSaveState({ entry, transferId: "", status: "已入队，等 Agent 把文件传到传输会话…", conflict: false, name: entry.name, error: "" });
    try {
      const started = await copyWorkspaceFileToDrive({ workspaceId: workspace.id, relativePath: entry.relative_path });
      const transferId = started.transfer.id;
      track(started.operation);
      setSaveState((previous) => (previous ? { ...previous, transferId, status: "排队中（Agent 还没领）" } : previous));
      const deadline = Date.now() + 90_000;
      let operation = started.operation;
      while (!isWorkspaceOperationFinished(operation)) {
        if (Date.now() > deadline) {
          setSaveState((previous) => (previous ? { ...previous, status: "等太久还没结果（Agent 可能不在线）；操作留在队列里，可以在下面取消" } : previous));
          return;
        }
        await new Promise((resolve) => setTimeout(resolve, 1200));
        operation = (await getWorkspaceOperation(workspace.id, operation.id)).operation;
        track(operation);
        setSaveState((previous) =>
          previous
            ? { ...previous, status: operation.status === "claimed" ? "Agent 已领取，正在读文件…" : operation.status === "running" ? "正在传…" : previous.status }
            : previous,
        );
      }
      if (operation.status !== "succeeded") {
        setSaveState((previous) =>
          previous ? { ...previous, error: `Agent 没能把文件传上来：${operation.error_code ?? operation.status}`, status: "" } : previous,
        );
        return;
      }
      const saved = await saveTransferToDrive({ transferId, name: entry.name });
      setSaveState(null);
      note(`已存进云盘：${saved.node.name}（${saved.saved_bytes} 字节）`);
    } catch (error) {
      const message = error instanceof Error ? error.message : "保存到云盘失败";
      const conflict = message.includes("同名");
      setSaveState((previous) => (previous ? { ...previous, conflict, error: conflict ? "" : message, status: "" } : previous));
      if (!conflict) notify(message);
    } finally {
      setBusy("");
    }
  };

  const retrySaveWithNewName = async () => {
    if (!saveState?.transferId) return;
    setBusy("save-rename");
    try {
      const saved = await saveTransferToDrive({ transferId: saveState.transferId, name: saveState.name.trim() || saveState.entry.name });
      setSaveState(null);
      note(`已存进云盘：${saved.node.name}`);
    } catch (error) {
      setSaveState((previous) => (previous ? { ...previous, error: error instanceof Error ? error.message : "仍失败" } : previous));
    } finally {
      setBusy("");
    }
  };

  const handleCopy = async (entry: WorkspaceEntry) => {
    const operation = await run("copy", {
      operation_type: "copy",
      relative_path: entry.relative_path,
      arguments: { destination: path, name: `副本-${entry.name}` },
    });
    if (operation.status === "succeeded") notify(`已复制为 副本-${entry.name}`);
    else notify(`复制失败：${operation.error_code ?? operation.status}`);
  };

  const retry = async (operation: WorkspaceOperation) => {
    const payload = { operation_type: operation.operation_type, relative_path: operation.relative_path, arguments: operation.arguments };
    try {
      const again = await run(`retry-${operation.operation_type}`, payload);
      notify(again.status === "succeeded" ? "重试成功" : `重试仍失败：${again.error_code ?? again.status}`);
    } catch (error) {
      notify(error instanceof Error ? error.message : "重试失败");
    }
  };

  const cancel = async (operation: WorkspaceOperation) => {
    try {
      const result = await cancelWorkspaceOperation(workspace.id, operation.id);
      track(result.operation);
      notify("已取消");
    } catch (error) {
      notify(error instanceof Error ? error.message : "取消失败");
    }
  };

  const visible = entries.filter((entry) => !query || entry.name.toLowerCase().includes(query.toLowerCase()));
  const allSelected = visible.length > 0 && visible.every((entry) => selected.has(entry.relative_path));
  const selectedEntries = visible.filter((entry) => selected.has(entry.relative_path));
  const failedUploads = uploads.filter((job) => job.error);

  return (
    <div className="drive-source-body" data-testid="workspace-browser">
      {offline ? (
        <div className="drive-offline" data-testid="workspace-offline-banner">
          <WifiOff size={14} />
          Agent 现在不在线（{workspace.status === "unavailable" ? "工作区不可用" : "离线"}）。
          下面显示的是最后一次读到的缓存{syncedAt ? `（${formatTime(syncedAt)}）` : ""}；
          你现在发起的操作会排在队列里，等它上线后自动执行——不会假装已经完成。
        </div>
      ) : null}

      <Panel
        title={workspace.display_name}
        subtitle={breadcrumb.join(" / ")}
        testId="workspace-files"
        actions={
          <div className="drive-toolbar">
            <label className="drive-search">
              <Search size={14} />
              <input value={query} placeholder="过滤当前目录" onChange={(event) => setQuery(event.target.value)} />
            </label>
            <button className="text-button" data-testid="workspace-mkdir" disabled={busy !== ""} onClick={() => setCreating("新建文件夹")}>
              <FolderPlus size={14} /> 新建文件夹
            </button>
            <button className="text-button" data-testid="workspace-upload" disabled={busy !== ""} onClick={() => fileInput.current?.click()}>
              <Upload size={14} /> 上传
            </button>
            <input ref={fileInput} type="file" multiple hidden onChange={(event) => void handleUpload(event.target.files)} />
            <button className="text-button" data-testid="workspace-refresh" disabled={loading} onClick={() => void refresh(path)}>
              {loading ? <RefreshCcw size={14} className="spin" /> : <RefreshCcw size={14} />} 刷新
            </button>
          </div>
        }
      >
        <nav className="drive-crumbs" aria-label="工作区路径">
          <span className="drive-crumb">
            <button className={path ? "" : "drive-crumb-current"} onClick={() => goTo("")}>
              工作区
            </button>
            <span className="drive-crumb-sep">/</span>
          </span>
          {breadcrumb.slice(1).map((segment, index) => {
            const target = breadcrumb.slice(1, index + 2).join("/");
            return (
              <span key={target} className="drive-crumb">
                <button className={index === breadcrumb.length - 2 ? "drive-crumb-current" : ""} onClick={() => goTo(target)}>
                  {segment}
                </button>
                {index < breadcrumb.length - 2 ? <span className="drive-crumb-sep">/</span> : null}
              </span>
            );
          })}
          {path ? (
            <button className="text-button" onClick={goUp}>
              <ChevronRight size={13} style={{ transform: "rotate(180deg)" }} /> 上一级
            </button>
          ) : null}
        </nav>

        {creating ? (
          <div className="drive-new-row">
            <Folder size={15} />
            <input
              autoFocus
              placeholder="文件夹名"
              value={creating === "新建文件夹" ? "" : creating}
              onChange={(event) => setCreating(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") void handleCreateDirectory();
                if (event.key === "Escape") setCreating("");
              }}
            />
            <button className="button button-primary" disabled={busy === "mkdir"} onClick={() => void handleCreateDirectory()}>
              创建
            </button>
            <button className="button button-secondary" onClick={() => setCreating("")}>
              取消
            </button>
          </div>
        ) : null}

        {selectedEntries.length > 0 ? (
          <div className="drive-batch" data-testid="workspace-batch-bar">
            <strong>已选 {selectedEntries.length} 项</strong>
            <button
              className="text-button"
              onClick={() => {
                setMoveDestination("");
                setMoveTargets(selectedEntries);
              }}
            >
              <Move size={14} /> 移动到…
            </button>
            <button
              className="text-button drive-danger"
              onClick={() => setPendingDelete(selectedEntries.filter((entry) => !isProtected(workspace, protectedPaths, entry.relative_path)))}
            >
              <Trash2 size={14} /> 删除
            </button>
            <button className="text-button" onClick={() => setSelected(new Set())}>
              取消选择
            </button>
          </div>
        ) : null}

        <div className="drive-list">
          <div className="drive-row drive-row-head">
            <span className="drive-cell-check">
              <button
                className="drive-icon-button"
                aria-label="全选"
                onClick={() => setSelected(allSelected ? new Set() : new Set(visible.map((entry) => entry.relative_path)))}
              >
                {allSelected ? <CheckSquare size={15} /> : <Square size={15} />}
              </button>
            </span>
            <span className="drive-cell-name">名称</span>
            <span className="drive-cell-size">大小</span>
            <span className="drive-cell-time">修改时间</span>
            <span className="drive-cell-actions" />
          </div>
          {visible.map((entry) => {
            const guarded = isProtected(workspace, protectedPaths, entry.relative_path);
            return (
              <div className="drive-row" key={entry.relative_path} data-testid={`workspace-entry-${entry.name}`}>
                <span className="drive-cell-check">
                  <button className="drive-icon-button" aria-label="选择" onClick={() =>
                    setSelected((previous) => {
                      const next = new Set(previous);
                      if (next.has(entry.relative_path)) next.delete(entry.relative_path);
                      else next.add(entry.relative_path);
                      return next;
                    })
                  }>
                    {selected.has(entry.relative_path) ? <CheckSquare size={15} /> : <Square size={15} />}
                  </button>
                </span>
                <span className="drive-cell-name">
                  {renaming && renaming.relative === entry.relative_path ? (
                    <span className="drive-name-button">
                      <Pencil size={14} />
                      <input
                        autoFocus
                        className="drive-rename-input"
                        value={renaming.name}
                        onChange={(event) => setRenaming({ relative: entry.relative_path, name: event.target.value })}
                        onKeyDown={(event) => {
                          if (event.key === "Enter") void handleRename();
                          if (event.key === "Escape") setRenaming(null);
                        }}
                        onBlur={() => void handleRename()}
                      />
                    </span>
                  ) : (
                    <button
                      className="drive-name-button"
                      title={entry.kind === "directory" ? "双击进入" : "点一下看详情"}
                      onDoubleClick={() => (entry.kind === "directory" ? openDirectory(entry) : void handleDownload(entry))}
                      onClick={() => setDetail(entry)}
                    >
                      {entry.kind === "directory" ? <Folder size={15} /> : entry.name.toLowerCase().endsWith(".zip") ? <FileArchive size={15} /> : <FileText size={15} />}
                      <span className="drive-node-name">{entry.name}</span>
                      {entry.is_symlink ? <AlertTriangle size={13} aria-label="符号链接（内核已按策略处理）" /> : null}
                      {guarded ? <span className="drive-guard" title="受保护路径：不可删除/改名/移动">受保护</span> : null}
                    </button>
                  )}
                  <small>{entry.kind === "directory" ? "文件夹" : `${formatBytes(entry.size_bytes)}`}</small>
                </span>
                <span className="drive-cell-size" data-label="大小">
                  {entry.kind === "directory" ? "—" : formatBytes(entry.size_bytes)}
                </span>
                <span className="drive-cell-time" data-label="修改时间">
                  {formatTime(entry.modified_at)}
                </span>
                <span className="drive-cell-actions">
                  {entry.kind === "file" ? (
                    <>
                      <button className="drive-icon-button" title="下载" disabled={busy !== ""} onClick={() => void handleDownload(entry)}>
                        <Download size={15} />
                      </button>
                      <button
                        className="drive-icon-button"
                        title="保存到个人云盘"
                        disabled={busy !== ""}
                        data-testid={`workspace-save-${entry.name}`}
                        onClick={() => void handleSaveToDrive(entry)}
                      >
                        <UploadCloud size={15} />
                      </button>
                    </>
                  ) : null}
                  {entry.kind === "file" && entry.name.toLowerCase().endsWith(".zip") ? (
                    <button className="drive-icon-button" title="解压到当前目录" disabled={busy !== ""} onClick={() => void handleExtract(entry)}>
                      <FileArchive size={15} />
                    </button>
                  ) : null}
                  {guarded ? null : (
                    <>
                      <button className="drive-icon-button" title="改名" onClick={() => setRenaming({ relative: entry.relative_path, name: entry.name })}>
                        <Pencil size={15} />
                      </button>
                      <button className="drive-icon-button" title="复制一份到当前目录" disabled={busy !== ""} onClick={() => void handleCopy(entry)}>
                        <Copy size={15} />
                      </button>
                      <button className="drive-icon-button" title="删除（工作区里的删除不进回收站）" onClick={() => setPendingDelete([entry])}>
                        <Trash2 size={15} />
                      </button>
                    </>
                  )}
                </span>
              </div>
            );
          })}
        </div>

        {!entries.length && !loading ? (
          <EmptyState>
            <Cloud size={16} /> 这个目录是空的{offline ? "（离线时可能只是还没读到——点刷新重试）" : ""}
          </EmptyState>
        ) : null}
        {truncated ? <p className="hint">目录条目太多，只显示了前一部分（内核有上限，避免一次拉太多）</p> : null}
      </Panel>

      {detail ? (
        <aside className="drive-drawer" role="dialog" aria-label="工作区文件详情" data-testid="workspace-detail">
          <div className="drive-drawer-head">
            <div>
              <strong>{detail.name}</strong>
              <small>
                {detail.kind === "directory" ? "文件夹" : formatBytes(detail.size_bytes)} · {formatTime(detail.modified_at)}
              </small>
            </div>
            <button className="drive-icon-button" aria-label="关闭" onClick={() => setDetail(null)}>
              ×
            </button>
          </div>
          <dl className="drive-detail-grid">
            <dt>相对路径</dt>
            <dd>{detail.relative_path}</dd>
            <dt>类型</dt>
            <dd>{detail.kind === "directory" ? "目录" : "文件"}</dd>
            <dt>符号链接</dt>
            <dd>{detail.is_symlink ? "是（内核按策略拒绝跟随）" : "否"}</dd>
            <dt>保护状态</dt>
            <dd>{isProtected(workspace, protectedPaths, detail.relative_path) ? "受保护（不可删/改名/移动）" : "可操作"}</dd>
            <dt>所在工作区</dt>
            <dd>
              {workspace.display_name} · {workspace.workspace_identity}（{workspace.kind === "cloud" ? "云端" : "桌面"}）
            </dd>
          </dl>
          <div className="drive-drawer-actions">
            {detail.kind === "file" ? (
              <>
                <button className="text-button" disabled={busy !== ""} onClick={() => void handleDownload(detail)}>
                  <Download size={14} /> 下载
                </button>
                <button className="text-button" data-testid="workspace-save-to-drive" disabled={busy !== ""} onClick={() => void handleSaveToDrive(detail)}>
                  <UploadCloud size={14} /> 保存到云盘
                </button>
              </>
            ) : null}
            {isProtected(workspace, protectedPaths, detail.relative_path) ? (
              <span className="hint">受保护路径不给改动入口</span>
            ) : (
              <>
                <button className="text-button" onClick={() => setRenaming({ relative: detail.relative_path, name: detail.name })}>
                  <Pencil size={14} /> 改名
                </button>
                <button
                  className="text-button"
                  onClick={() => {
                    setMoveDestination("");
                    setMoveTargets([detail]);
                  }}
                >
                  <Move size={14} /> 移动
                </button>
                <button className="text-button drive-danger" onClick={() => setPendingDelete([detail])}>
                  <Trash2 size={14} /> 删除
                </button>
              </>
            )}
          </div>
        </aside>
      ) : null}

      {saveState ? (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => setSaveState(null)}>
          <div className="modal" role="dialog" aria-modal="true" data-testid="workspace-save-modal" onMouseDown={(event) => event.stopPropagation()}>
            <div className="modal-heading">
              <div>
                <h2>保存到个人云盘</h2>
                <p>{saveState.entry.name} · 显式保存一次（云盘里那份是复制）</p>
              </div>
              <button className="app-icon" aria-label="关闭" onClick={() => setSaveState(null)}>
                ×
              </button>
            </div>
            {saveState.status ? <p className="hint" data-testid="workspace-save-status">{saveState.status}</p> : null}
            {saveState.conflict ? (
              <>
                <p className="hint">云盘里已有同名文件（不会覆盖）。换个名字再存一次：</p>
                <label className="drive-grant-field">
                  <span>存成</span>
                  <input
                    value={saveState.name}
                    data-testid="workspace-save-name"
                    onChange={(event) => setSaveState((previous) => (previous ? { ...previous, name: event.target.value } : previous))}
                  />
                </label>
              </>
            ) : null}
            {saveState.error ? (
              <p className="drive-danger" data-testid="workspace-save-error">
                {saveState.error}
              </p>
            ) : null}
            <div className="modal-actions">
              <button className="button button-secondary" onClick={() => setSaveState(null)}>
                关闭
              </button>
              {saveState.conflict ? (
                <button className="button button-primary" data-testid="workspace-save-retry" disabled={busy !== ""} onClick={() => void retrySaveWithNewName()}>
                  存成这个名字
                </button>
              ) : null}
            </div>
          </div>
        </div>
      ) : null}

      {moveTargets ? (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => setMoveTargets(null)}>
          <div className="modal" role="dialog" aria-modal="true" data-testid="workspace-move-modal" onMouseDown={(event) => event.stopPropagation()}>
            <div className="modal-heading">
              <div>
                <h2>移动到工作区里的哪个目录</h2>
                <p>{moveTargets.length === 1 ? moveTargets[0].name : `${moveTargets.length} 项`} → 目标（相对路径）</p>
              </div>
              <button className="app-icon" aria-label="关闭" onClick={() => setMoveTargets(null)}>
                ×
              </button>
            </div>
            <input
              className="drive-rename-input"
              style={{ width: "100%" }}
              placeholder="留空 = 工作区根，例如 papers/2026"
              value={moveDestination}
              onChange={(event) => setMoveDestination(event.target.value)}
            />
            <p className="hint">目录不存在会失败（工作区里的移动不会自动建目录）——可以先「新建文件夹」再来移动。</p>
            <div className="modal-actions">
              <button className="button button-secondary" onClick={() => setMoveTargets(null)}>
                取消
              </button>
              <button className="button button-primary" disabled={busy === "move"} onClick={() => void handleMove()}>
                移动
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {pendingDelete ? (
        <ConfirmDialog
          title="删除工作区里的文件？"
          description={
            <>
              <strong>{pendingDelete.length === 1 ? pendingDelete[0].name : `${pendingDelete.length} 项`}</strong>
              <div style={{ marginTop: 6 }}>
                工作区里的删除不进回收站，删了就没了；受保护路径（工作区根、.git、.math-agent-platform、Agent 自报的保护目录）
                内核会拒绝，这里也不给入口。
              </div>
            </>
          }
          confirmLabel="删除"
          tone="danger"
          busy={busy === "delete"}
          testId="workspace-delete-confirm"
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => {
            const targets = pendingDelete;
            setPendingDelete(null);
            void handleDelete(targets);
          }}
        />
      ) : null}

      {operations.length || uploads.length ? (
        <Panel title="工作区操作" subtitle="入队 → Agent 领取 → 执行（状态以平台为准；失败可重试，排队可取消）" testId="workspace-operations">
          {uploads.map((job) => (
            <div className="drive-job" key={job.name}>
              {job.error ? (
                <span className="drive-danger">
                  {job.name}：{job.error}
                </span>
              ) : (
                <Progress label={job.name} value={job.percent} detail={`${job.percent}%`} />
              )}
            </div>
          ))}
          {operations.map((operation) => (
            <div className="drive-job drive-op-row" key={operation.id} data-testid={`workspace-operation-${operation.id}`}>
              <span className={`badge ${STATUS_TONE[operation.status] ?? "status-neutral"}`}>{STATUS_LABEL[operation.status] ?? operation.status}</span>
              <span className="drive-node-name">
                {operation.operation_type} {operation.relative_path || "(工作区根)"}
              </span>
              {operation.error_code ? <small className="drive-danger">{operation.error_code}</small> : null}
              {isWorkspaceOperationFinished(operation) ? (
                operation.status === "failed" ? (
                  <button className="text-button" disabled={busy !== ""} onClick={() => void retry(operation)}>
                    <RotateCcw size={13} /> 重试
                  </button>
                ) : null
              ) : (
                <button className="text-button" onClick={() => void cancel(operation)}>
                  <XCircle size={13} /> 取消
                </button>
              )}
            </div>
          ))}
          {notes.map((line) => (
            <div className="drive-job drive-note" key={line}>
              {line}
            </div>
          ))}
          {failedUploads.length ? <p className="hint">失败的传输在上面的进度区里如实列出，不会标成成功。</p> : null}
        </Panel>
      ) : null}
    </div>
  );
}

export default WorkspaceBrowser;