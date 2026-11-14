"use client";

/**
 * 个人云盘文件管理器（FM-2）。
 *
 * 与旧版的区别：旧版是"一堆平铺文件 + 上传/删除/加入项目"，新版是**目录式文件管理器**——
 * 面包屑、搜索、排序、分页、新建文件夹、改名、移动、复制、下载、批量操作、回收站（恢复/彻底清除）、
 * 安全解压、详情抽屉（来源成果物 / 项目引用 / 最近审计）、上传进度。
 *
 * 几条实现上的硬约束（踩过，或不遵守会出事）：
 * - **下载不能用 `<a href>`**：普通跳转不带 Authorization，`required` 模式下会 401；走 `downloadDriveNode`。
 * - **上传用 XHR**：fetch 拿不到上传进度，"文件级进度条"就会是假的。
 * - **删除是两段式**：列表里的删除进回收站（可恢复），回收站里才彻底清除；被项目引用的不能清除。
 * - 旧功能一个不少：上传、加入项目、转换入知识库都保留（老接口继续可用）。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft,
  CheckSquare,
  Cloud,
  Copy,
  CornerUpLeft,
  Download,
  ExternalLink,
  FileArchive,
  FileText,
  Folder,
  FolderPlus,
  Info,
  Move,
  Pencil,
  RefreshCcw,
  FolderInput,
  Search,
  ShieldCheck,
  Square,
  Trash2,
  Upload,
} from "lucide-react";
import Link from "next/link";
import { PageHeading, formatTime } from "../../components/shell";
import { ConfirmDialog, EmptyState, Metric, Panel, Progress } from "../../components/ui";
import {
  DriveNode,
  DriveNodeDetail,
  DriveNodeListing,
  DriveUsage,
  copyDriveNode,
  createDriveDirectory,
  downloadDriveNode,
  enqueueDriveConversion,
  extractDriveArchive,
  getDriveNode,
  importDriveFile,
  listDriveNodes,
  listDriveTrash,
  moveDriveNode,
  purgeDriveNode,
  renameDriveNode,
  restoreDriveNode,
  retryDriveCleanup,
  trashDriveNode,
  uploadDriveNode,
} from "../../lib/api";
import { useWorkspace } from "../../lib/workspace";
import {
  AgentWorkspace,
  FileAccessGrant,
  createFileAccessGrant,
  copyDriveFileToWorkspace,
  getWorkspaceOperation,
  listAgentWorkspaces,
  listFileAccessGrants,
  revokeFileAccessGrant,
} from "../../lib/api";
import { WorkspaceBrowser } from "./workspace-browser";
import "./drive.css";

const ARCHIVE_SUFFIXES = [".zip", ".tar", ".gz", ".tgz"];
const SORT_LABELS: { value: string; label: string }[] = [
  { value: "name", label: "按名称" },
  { value: "created", label: "按上传时间" },
  { value: "updated", label: "按修改时间" },
  { value: "size", label: "按大小" },
];
const PAGE_SIZE = 100;

function formatBytes(value: number) {
  if (value >= 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${value} B`;
}

function kindLabel(node: DriveNode) {
  if (node.kind === "directory") return "文件夹";
  const suffix = node.name.includes(".") ? node.name.split(".").pop()?.toUpperCase() ?? "文件" : "文件";
  return node.is_archive ? `压缩包 · ${suffix}` : suffix;
}

type UploadJob = { name: string; percent: number; error: string };

export default function DrivePage() {
  const { projectId: activeProjectId, notify, refresh } = useWorkspace();
  const [view, setView] = useState<"files" | "trash">("files");
  const [parentId, setParentId] = useState<string | null>(null);
  const [listing, setListing] = useState<DriveNodeListing | null>(null);
  const [trash, setTrash] = useState<DriveNode[]>([]);
  const [usage, setUsage] = useState<DriveUsage | null>(null);
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState("name");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [detail, setDetail] = useState<DriveNodeDetail | null>(null);
  const [busy, setBusy] = useState("");
  const [uploads, setUploads] = useState<UploadJob[]>([]);
  const [notes, setNotes] = useState<string[]>([]);
  const [creating, setCreating] = useState("");
  const [renaming, setRenaming] = useState<{ id: string; name: string } | null>(null);
  const [pendingTrash, setPendingTrash] = useState<DriveNode[] | null>(null);
  const [pendingPurge, setPendingPurge] = useState<DriveNode | null>(null);
  const [grantTarget, setGrantTarget] = useState<DriveNode | null>(null);
  const [copyTarget, setCopyTarget] = useState<DriveNode | null>(null);
  const [copyWorkspaceId, setCopyWorkspaceId] = useState("");
  const [copyPath, setCopyPath] = useState("");
  const [copyOverwrite, setCopyOverwrite] = useState(false);
  const [copyStatus, setCopyStatus] = useState("");
  const [grantWorkspaceId, setGrantWorkspaceId] = useState("");
  const [grantDays, setGrantDays] = useState(7);
  const [importTarget, setImportTarget] = useState<DriveNode | null>(null);
  const [moveTarget, setMoveTarget] = useState<DriveNode[] | null>(null);
  const [pickerListing, setPickerListing] = useState<DriveNodeListing | null>(null);
  const [dragging, setDragging] = useState(false);
  const [workspaces, setWorkspaces] = useState<AgentWorkspace[]>([]);
  const [sourceId, setSourceId] = useState<string>("drive");
  const [railOpen, setRailOpen] = useState(false);
  const [grants, setGrants] = useState<FileAccessGrant[]>([]);
  const fileInput = useRef<HTMLInputElement>(null);

  const nodes = listing?.nodes ?? [];
  const breadcrumb = listing?.breadcrumb ?? [];
  const activeWorkspace = workspaces.find((item) => item.id === sourceId) ?? null;

  const loadWorkspaces = useCallback(async () => {
    try {
      const payload = await listAgentWorkspaces();
      setWorkspaces(payload.workspaces);
    } catch {
      // 工作区列表读不到不影响云盘这半边（如实降级：只显示云盘）
      setWorkspaces([]);
    }
  }, []);


  // 目录/回收站的读取函数必须**稳定**（依赖为空）：它们的 identity 一旦随渲染变化，
  // 下面那个 useEffect 就会每次渲染都重新拉一次目录——实测表现是"页面不停发请求 + 刚点开的
  // 控件被下一次渲染换掉（点了没反应）"。变化的值（搜索词、排序、notify）走 ref 读。
  const queryRef = useRef(query);
  const sortRef = useRef(sort);
  const notifyRef = useRef(notify);
  queryRef.current = query;
  sortRef.current = sort;
  notifyRef.current = notify;

  const loadNodes = useCallback(
    async (targetParent: string | null, options: { cursor?: string | null; append?: boolean; search?: string; order?: string } = {}) => {
      try {
        const page = await listDriveNodes({
          parentId: targetParent,
          query: options.search ?? queryRef.current,
          sort: options.order ?? sortRef.current,
          cursor: options.cursor ?? null,
          limit: PAGE_SIZE,
        });
        setUsage(page.usage);
        setListing((previous) => (options.append && previous ? { ...page, nodes: [...previous.nodes, ...page.nodes] } : page));
      } catch (error) {
        notifyRef.current(error instanceof Error ? error.message : "目录读取失败");
      }
    },
    [],
  );

  const loadTrash = useCallback(async () => {
    try {
      const payload = await listDriveTrash();
      setTrash(payload.nodes);
      setUsage(payload.usage);
    } catch (error) {
      notifyRef.current(error instanceof Error ? error.message : "回收站读取失败");
    }
  }, []);

  useEffect(() => {
    if (view === "files") void loadNodes(parentId);
    else void loadTrash();
    setSelected(new Set());
  }, [view, parentId, loadNodes, loadTrash]);

  useEffect(() => {
    void loadWorkspaces();
  }, [loadWorkspaces]);

  // 切回「个人云盘」时**必须重新拉一次目录**：否则看到的是上次进云盘时的旧列表。
  // 实测踩到：从工作区「保存到云盘」成功后切回来，新文件不出现（平台其实已经有了）。
  // 依赖只放 sourceId：换根才是"需要刷新"的信号，加别的依赖会把每次渲染都变成一次请求。
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (sourceId === "drive" && view === "files") void loadNodes(parentId);
  }, [sourceId]);

  const showDetail = useCallback(
    async (node: DriveNode) => {
      try {
        setDetail(await getDriveNode(node.id));
      } catch (error) {
        notify(error instanceof Error ? error.message : "详情读取失败");
      }
      // 顺手把这个节点上的 Agent 授权拉出来（"谁可以读这个文件"是详情抽屉最该回答的问题之一）
      try {
        const payload = await listFileAccessGrants({ nodeId: node.id });
        setGrants(payload.grants);
      } catch {
        setGrants([]);
      }
    },
    [notify],
  );

  const openNode = (node: DriveNode) => {
    if (node.kind === "directory") {
      setParentId(node.id);
      setDetail(null);
      return;
    }
    void showDetail(node);
  };

  const refreshCurrent = async () => {
    if (view === "files") {
      await loadNodes(parentId);
      if (detail) await showDetail(detail.node);
    } else {
      await loadTrash();
    }
  };

  const note = (message: string) => setNotes((previous) => [message, ...previous].slice(0, 4));

  const handleUpload = async (files: FileList | null) => {
    if (!files?.length) return;
    const picked = Array.from(files);
    setUploads(picked.map((file) => ({ name: file.name, percent: 0, error: "" })));
    for (const [index, file] of picked.entries()) {
      try {
        await uploadDriveNode(file, parentId, (percent) => {
          setUploads((previous) => previous.map((job, position) => (position === index ? { ...job, percent } : job)));
        });
        setUploads((previous) => previous.map((job, position) => (position === index ? { ...job, percent: 100 } : job)));
      } catch (error) {
        const message = error instanceof Error ? error.message : "上传失败";
        setUploads((previous) => previous.map((job, position) => (position === index ? { ...job, error: message } : job)));
        notify(`${file.name}：${message}`);
      }
    }
    if (fileInput.current) fileInput.current.value = "";
    await refreshCurrent();
  };

  const handleCreateDirectory = async () => {
    const name = creating.trim();
    if (!name || creating === "新建文件夹") {
      notify("请填一个文件夹名");
      return;
    }
    setBusy("mkdir");
    try {
      await createDriveDirectory(parentId, name);
      setCreating("");
      await loadNodes(parentId);
      note(`已新建文件夹 ${name}`);
    } catch (error) {
      notify(error instanceof Error ? error.message : "新建文件夹失败");
    } finally {
      setBusy("");
    }
  };

  const handleRename = async () => {
    if (!renaming) return;
    const name = renaming.name.trim();
    if (!name) {
      setRenaming(null);
      return;
    }
    setBusy(`rename-${renaming.id}`);
    try {
      await renameDriveNode(renaming.id, name);
      setRenaming(null);
      await refreshCurrent();
      note(`已改名为 ${name}`);
    } catch (error) {
      notify(error instanceof Error ? error.message : "改名失败");
    } finally {
      setBusy("");
    }
  };

  const handleCopy = async (node: DriveNode) => {
    setBusy(`copy-${node.id}`);
    try {
      const result = await copyDriveNode(node.id, parentId);
      await loadNodes(parentId);
      note(`已复制为 ${result.node.name}`);
    } catch (error) {
      notify(error instanceof Error ? error.message : "复制失败");
    } finally {
      setBusy("");
    }
  };

  const handleDownload = async (node: DriveNode) => {
    setBusy(`download-${node.id}`);
    try {
      await downloadDriveNode(node);
    } catch (error) {
      notify(error instanceof Error ? error.message : "下载失败");
    } finally {
      setBusy("");
    }
  };

  const handleExtract = async (node: DriveNode) => {
    setBusy(`extract-${node.id}`);
    try {
      const result = await extractDriveArchive(node.id, parentId);
      await loadNodes(parentId);
      note(
        `解压完成：${result.files} 个文件、${result.directories} 个目录` +
          (result.skipped_nested.length ? `，跳过嵌套归档 ${result.skipped_nested.length} 个` : "") +
          (result.skipped_junk ? `，忽略系统文件 ${result.skipped_junk} 个` : ""),
      );
    } catch (error) {
      notify(error instanceof Error ? error.message : "解压失败");
    } finally {
      setBusy("");
    }
  };

  const handleTrash = async (targets: DriveNode[]) => {
    setBusy("trash");
    let moved = 0;
    for (const node of targets) {
      try {
        await trashDriveNode(node.id);
        moved += 1;
      } catch (error) {
        notify(`${node.name}：${error instanceof Error ? error.message : "删除失败"}`);
      }
    }
    setBusy("");
    setSelected(new Set());
    setDetail(null);
    await refreshCurrent();
    if (moved) note(`已移入回收站 ${moved} 项（可在回收站恢复）`);
  };

  const handleRestore = async (node: DriveNode) => {
    setBusy(`restore-${node.id}`);
    try {
      await restoreDriveNode(node.id);
      await loadTrash();
      note(`已恢复 ${node.name}（回到它原来的目录）`);
    } catch (error) {
      notify(error instanceof Error ? error.message : "恢复失败");
    } finally {
      setBusy("");
    }
  };

  const handlePurge = async (node: DriveNode) => {
    setBusy(`purge-${node.id}`);
    try {
      const result = await purgeDriveNode(node.id);
      await loadTrash();
      note(
        `已彻底清除 ${node.name}` +
          (result.objects_pending ? `（${result.objects_pending} 个对象删除失败，已进清理队列等重试）` : ""),
      );
    } catch (error) {
      notify(error instanceof Error ? error.message : "彻底清除失败");
    } finally {
      setBusy("");
    }
  };

  const handleMove = async () => {
    if (!moveTarget || !pickerListing) return;
    setBusy("move");
    let moved = 0;
    for (const node of moveTarget) {
      try {
        await moveDriveNode(node.id, pickerListing.parent.id);
        moved += 1;
      } catch (error) {
        notify(`${node.name}：${error instanceof Error ? error.message : "移动失败"}`);
      }
    }
    setBusy("");
    const destination = pickerListing.breadcrumb.map((crumb) => crumb.name).join(" / ");
    setMoveTarget(null);
    setSelected(new Set());
    await loadNodes(parentId);
    if (moved) note(`已移动 ${moved} 项到 ${destination}`);
  };

  const handleImport = async (node: DriveNode) => {
    if (!activeProjectId) {
      notify("请先选择项目");
      return;
    }
    setBusy(`import-${node.id}`);
    try {
      const result = await importDriveFile(activeProjectId, node.id);
      await refreshCurrent();
      await refresh();
      setImportTarget(null);
      notify(`已导入项目空间：${result.artifact_type}`);
    } catch (error) {
      notify(error instanceof Error ? error.message : "导入失败");
    } finally {
      setBusy("");
    }
  };

  const handleConvert = async (node: DriveNode) => {
    setBusy(`convert-${node.id}`);
    try {
      const job = await enqueueDriveConversion({ id: node.id, name: node.name });
      notify(`已入队转换（job: ${String(job.id).slice(0, 8)}）；完成后可在知识库页写入`);
    } catch (error) {
      notify(error instanceof Error ? error.message : "转换入队失败");
    } finally {
      setBusy("");
    }
  };

  const openCopyToWorkspace = (node: DriveNode) => {
    setCopyTarget(node);
    setCopyPath(node.name);
    setCopyOverwrite(false);
    setCopyStatus("");
    if (!copyWorkspaceId && workspaces.length) setCopyWorkspaceId(workspaces[0].id);
  };

  const handleCopyToWorkspace = async () => {
    const workspace = workspaces.find((item) => item.id === copyWorkspaceId);
    if (!workspace || !copyTarget) {
      notify("请先选择目标工作区");
      return;
    }
    setBusy("copy-to-workspace");
    setCopyStatus("已入队，等 Agent 领取…");
    try {
      const payload = await copyDriveFileToWorkspace({
        nodeId: copyTarget.id,
        workspaceId: workspace.id,
        relativePath: copyPath.trim() || copyTarget.name,
        overwrite: copyOverwrite,
      });
      // 轮询到终态：**不乐观假成功**——Agent 没执行完就一直显示排队/执行中
      const deadline = Date.now() + 90_000;
      let operation = payload.operation;
      while (!["succeeded", "failed", "cancelled", "expired"].includes(operation.status)) {
        if (Date.now() > deadline) {
          setCopyStatus("等太久还没结果（Agent 可能不在线）；操作留在队列里，可以在工作区那侧取消");
          return;
        }
        await new Promise((resolve) => setTimeout(resolve, 1200));
        operation = (await getWorkspaceOperation(workspace.id, operation.id)).operation;
        setCopyStatus(operation.status === "queued" ? "排队中（Agent 还没领）" : operation.status === "claimed" ? "Agent 已领取" : "Agent 执行中…");
      }
      if (operation.status === "succeeded") {
        setCopyStatus("");
        setCopyTarget(null);
        note(`已复制到 ${workspace.display_name}：${copyPath.trim() || copyTarget.name}`);
      } else {
        setCopyStatus(
          operation.error_code === "workspace_path_conflict"
            ? "目标已存在同名文件（默认不覆盖）：换个路径，或勾上「允许覆盖」再试"
            : `失败：${operation.error_code ?? operation.status}`,
        );
      }
    } catch (error) {
      setCopyStatus("");
      notify(error instanceof Error ? error.message : "复制到工作区失败");
    } finally {
      setBusy("");
    }
  };

  const handleCreateGrant = async () => {
    const workspace = workspaces.find((item) => item.id === grantWorkspaceId);
    if (!activeProjectId || !workspace || !grantTarget) {
      notify("请先选择 Agent 工作区（并且当前要有选中的项目）");
      return;
    }
    setBusy("grant");
    try {
      const payload = await createFileAccessGrant({
        agentId: workspace.agent_id,
        deviceId: workspace.device_id,
        projectId: activeProjectId,
        nodeId: grantTarget.id,
        scopeType: grantTarget.kind === "directory" ? "folder" : "file",
        expiresInSeconds: grantDays * 24 * 3600,
      });
      setGrants((previous) => [payload.grant, ...previous]);
      setGrantTarget(null);
      note(`已授权 ${workspace.agent_id} 读「${grantTarget.name}」（${grantDays} 天）`);
    } catch (error) {
      notify(error instanceof Error ? error.message : "授权创建失败");
    } finally {
      setBusy("");
    }
  };

  const handleRevokeGrant = async (grant: FileAccessGrant) => {
    setBusy(`grant-${grant.id}`);
    try {
      await revokeFileAccessGrant(grant.id, "revoked_in_drive_detail");
      setGrants((previous) =>
        previous.map((item) => (item.id === grant.id ? { ...item, revoked_at: new Date().toISOString(), active: false } : item)),
      );
      note(`已撤销对 ${grant.agent_id} 的授权（它下一次读取会立刻失败）`);
    } catch (error) {
      notify(error instanceof Error ? error.message : "撤销失败");
    } finally {
      setBusy("");
    }
  };

  const handleCleanupRetry = async () => {
    setBusy("cleanup");
    try {
      const result = await retryDriveCleanup();
      note(`清理队列重试：删除 ${result.deleted ?? 0} 个对象、失败 ${result.failed ?? 0} 个`);
    } catch (error) {
      notify(error instanceof Error ? error.message : "清理重试失败");
    } finally {
      setBusy("");
    }
  };

  const openPicker = async (targets: DriveNode[]) => {
    setMoveTarget(targets);
    try {
      setPickerListing(await listDriveNodes({ parentId: null, limit: 200 }));
    } catch (error) {
      notify(error instanceof Error ? error.message : "目录读取失败");
    }
  };

  const pickerEnter = async (node: DriveNode) => {
    if (node.kind !== "directory") return;
    try {
      setPickerListing(await listDriveNodes({ parentId: node.id, limit: 200 }));
    } catch (error) {
      notify(error instanceof Error ? error.message : "目录读取失败");
    }
  };

  const pickerUp = async () => {
    if (!pickerListing || pickerListing.parent.is_root) return;
    try {
      setPickerListing(await listDriveNodes({ parentId: pickerListing.parent.parent_id, limit: 200 }));
    } catch (error) {
      notify(error instanceof Error ? error.message : "目录读取失败");
    }
  };

  const toggleSelection = (node: DriveNode) => {
    setSelected((previous) => {
      const next = new Set(previous);
      if (next.has(node.id)) next.delete(node.id);
      else next.add(node.id);
      return next;
    });
  };

  const allSelected = nodes.length > 0 && nodes.every((node) => selected.has(node.id));
  const toggleAll = () => setSelected(allSelected ? new Set() : new Set(nodes.map((node) => node.id)));

  const selectedNodes = useMemo(() => nodes.filter((node) => selected.has(node.id)), [nodes, selected]);

  const railItems = [
    { id: "drive", label: "个人云盘", kind: "drive" as const, status: "online", detail: "平台对象存储" },
    ...workspaces.map((item) => ({
      id: item.id,
      label: item.display_name,
      kind: "workspace" as const,
      status: item.status,
      detail: `${item.kind === "cloud" ? "云端" : "桌面"} · ${item.workspace_identity}`,
      lastSeen: item.last_seen_at,
    })),
  ];

  return (
    <div className="page-content drive-page" id="drive">
      <PageHeading
        hint="文件：个人云盘 + 已接入的 Agent 工作区。统一的是界面与操作语义，不是物理存储——工作区始终由 Agent 本机执行"
        actions={
          <>
            <button className="button button-secondary drive-rail-toggle" data-testid="drive-roots-toggle" onClick={() => setRailOpen((open) => !open)}>
              <Folder size={15} /> 位置
            </button>
            {!activeWorkspace ? (
              <>
                <button className="button button-secondary" data-testid="drive-view-toggle" onClick={() => setView(view === "files" ? "trash" : "files")}>
                  {view === "files" ? <Trash2 size={15} /> : <ArrowLeft size={15} />}
                  {view === "files" ? "回收站" : "返回云盘"}
                </button>
                {view === "files" ? (
                  <>
                    <button className="button button-secondary" data-testid="drive-mkdir" disabled={busy !== ""} onClick={() => setCreating("新建文件夹")}>
                      <FolderPlus size={15} /> 新建文件夹
                    </button>
                    <button className="button button-primary" data-testid="drive-upload-open" disabled={busy !== ""} onClick={() => fileInput.current?.click()}>
                      <Upload size={15} /> 上传文件
                    </button>
                    <input ref={fileInput} type="file" multiple hidden data-testid="drive-file-input" onChange={(event) => void handleUpload(event.target.files)} />
                  </>
                ) : null}
              </>
            ) : null}
          </>
        }
      />

      <div className="drive-shell">
        <aside className={`drive-rail ${railOpen ? "drive-rail-open" : ""}`} data-testid="drive-roots">
          <h2>位置</h2>
          {railItems.map((item) => (
            <button
              key={item.id}
              className={`drive-rail-item ${item.id === sourceId ? "active" : ""}`}
              data-testid={`drive-root-${item.kind}`}
              onClick={() => {
                setSourceId(item.id);
                setRailOpen(false);
                setSelected(new Set());
                setDetail(null);
              }}
            >
              {item.kind === "drive" ? <Cloud size={15} /> : <Folder size={15} />}
              <span className="drive-rail-label">
                <strong>{item.label}</strong>
                <small>
                  {item.kind === "workspace" ? (
                    <>
                      <span className={`drive-dot ${item.status === "online" ? "ok" : "off"}`} />
                      {item.status === "online" ? "在线" : item.status === "offline" ? "离线" : "不可用"}
                      {item.lastSeen ? ` · 最后同步 ${formatTime(item.lastSeen)}` : ""}
                    </>
                  ) : (
                    item.detail
                  )}
                </small>
              </span>
            </button>
          ))}
          {!workspaces.length ? <p className="hint">还没有接入的 Agent 工作区；在「设备与接入」页接入后这里会出现。</p> : null}
        </aside>

        <div className="drive-body">
        {activeWorkspace ? (
          <WorkspaceBrowser workspace={activeWorkspace} notify={notify} onWorkspaceChanged={loadWorkspaces} />
        ) : (
        <>
      <section className="metrics-grid">
        <Metric
          label="已用空间"
          value={usage ? formatBytes(usage.used_bytes) : "—"}
          detail={`配额 ${usage ? formatBytes(usage.quota_bytes) : "—"} · 剩余 ${usage ? formatBytes(usage.free_bytes) : "—"}`}
        />
        <Metric label="文件数" value={usage?.file_count ?? "—"} detail={`回收站 ${usage?.trashed_count ?? 0} 个（仍占空间）`} />
        <Metric label={view === "files" ? "当前目录" : "待处理"} value={view === "files" ? listing?.total ?? "—" : trash.length} detail={view === "files" ? "项（含文件夹）" : "项在回收站"} />
        <Metric label="压缩包" value={view === "files" ? nodes.filter((node) => node.is_archive).length : "—"} detail="zip/tar/tar.gz/tgz 可解压" />
      </section>

      {view === "files" ? (
        <Panel
          title="文件"
          subtitle={breadcrumb.map((crumb) => crumb.name).join(" / ") || "个人云盘"}
          testId="drive-file-manager"
          actions={
            <div className="drive-toolbar">
              <label className="drive-search">
                <Search size={14} />
                <input
                  value={query}
                  placeholder="搜索当前目录"
                  data-testid="drive-search"
                  onChange={(event) => setQuery(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") void loadNodes(parentId, { search: query });
                  }}
                />
              </label>
              <select
                value={sort}
                data-testid="drive-sort"
                onChange={(event) => {
                  setSort(event.target.value);
                  void loadNodes(parentId, { order: event.target.value });
                }}
              >
                {SORT_LABELS.map((item) => (
                  <option key={item.value} value={item.value}>
                    {item.label}
                  </option>
                ))}
              </select>
              <button className="text-button" onClick={() => void refreshCurrent()}>
                <RefreshCcw size={14} /> 刷新
              </button>
              <button className="text-button" title="重试对象清理队列（删对象失败的那些）" onClick={() => void handleCleanupRetry()}>
                <Info size={14} /> 清理队列
              </button>
            </div>
          }
        >
          <nav className="drive-crumbs" aria-label="路径" data-testid="drive-breadcrumb">
            {breadcrumb.map((crumb, index) => (
              <span key={crumb.id} className="drive-crumb">
                <button
                  className={index === breadcrumb.length - 1 ? "drive-crumb-current" : ""}
                  onClick={() => setParentId(crumb.is_root ? null : crumb.id)}
                  data-testid={`drive-crumb-${index}`}
                >
                  {crumb.name}
                </button>
                {index < breadcrumb.length - 1 ? <span className="drive-crumb-sep">/</span> : null}
              </span>
            ))}
          </nav>

          {creating ? (
            <div className="drive-new-row">
              <Folder size={15} />
              <input
                autoFocus
                value={creating === "新建文件夹" ? "" : creating}
                placeholder="文件夹名"
                data-testid="drive-mkdir-input"
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

          {selectedNodes.length > 0 ? (
            <div className="drive-batch" data-testid="drive-batch-bar">
              <strong>已选 {selectedNodes.length} 项</strong>
              <button className="text-button" onClick={() => void openPicker(selectedNodes)}>
                <Move size={14} /> 移动到…
              </button>
              <button className="text-button drive-danger" onClick={() => setPendingTrash(selectedNodes)}>
                <Trash2 size={14} /> 删除
              </button>
              <button className="text-button" onClick={() => setSelected(new Set())}>
                取消选择
              </button>
            </div>
          ) : null}

          <div
            className={`drive-list ${dragging ? "drive-list-dragging" : ""}`}
            data-testid="drive-list"
            onDragOver={(event) => {
              event.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(event) => {
              event.preventDefault();
              setDragging(false);
              void handleUpload(event.dataTransfer?.files ?? null);
            }}
          >
            <div className="drive-row drive-row-head">
              <span className="drive-cell-check">
                <button className="drive-icon-button" aria-label="全选" onClick={toggleAll}>
                  {allSelected ? <CheckSquare size={15} /> : <Square size={15} />}
                </button>
              </span>
              <span className="drive-cell-name">名称</span>
              <span className="drive-cell-size">大小</span>
              <span className="drive-cell-time">修改时间</span>
              <span className="drive-cell-actions" />
            </div>

            {nodes.map((node) => (
              <div className="drive-row" key={node.id} data-testid={`drive-node-${node.id}`}>
                <span className="drive-cell-check">
                  <button className="drive-icon-button" aria-label="选择" onClick={() => toggleSelection(node)}>
                    {selected.has(node.id) ? <CheckSquare size={15} /> : <Square size={15} />}
                  </button>
                </span>
                <span className="drive-cell-name">
                  {renaming && renaming.id === node.id ? (
                    <span className="drive-name-button">
                      <Pencil size={14} />
                      <input
                        autoFocus
                        value={renaming.name}
                        className="drive-rename-input"
                        data-testid="drive-rename-input"
                        onChange={(event) => setRenaming({ id: node.id, name: event.target.value })}
                        onKeyDown={(event) => {
                          if (event.key === "Enter") void handleRename();
                          if (event.key === "Escape") setRenaming(null);
                        }}
                        onBlur={() => void handleRename()}
                      />
                    </span>
                  ) : (
                    <button className="drive-name-button" onDoubleClick={() => openNode(node)} onClick={() => void showDetail(node)} title="单击看详情，双击打开文件夹">
                      {node.kind === "directory" ? <Folder size={15} /> : node.is_archive ? <FileArchive size={15} /> : <FileText size={15} />}
                      <span className="drive-node-name">{node.name}</span>
                    </button>
                  )}
                  {node.kind === "directory" ? (
                    <small>{node.trashed_count ? `${node.trashed_count} 项` : "文件夹"}</small>
                  ) : (
                    <small>sha256 {(node.content_hash ?? "").slice(0, 10)} · 修订 {node.revision}</small>
                  )}
                </span>
                <span className="drive-cell-size" data-label="大小">
                  {node.kind === "directory" ? "—" : formatBytes(node.size_bytes)}
                </span>
                <span className="drive-cell-time" data-label="修改时间">
                  {formatTime(node.updated_at)}
                </span>
                <span className="drive-cell-actions">
                  {node.kind === "file" ? (
                    <>
                      <button className="drive-icon-button" title="下载" onClick={() => void handleDownload(node)}>
                        <Download size={15} />
                      </button>
                      <button className="drive-icon-button" title="复制到 Agent 工作区" onClick={() => openCopyToWorkspace(node)}>
                        <FolderInput size={15} />
                      </button>
                    </>
                  ) : null}
                  {node.is_archive ? (
                    <button className="drive-icon-button" title="解压" disabled={busy !== ""} onClick={() => void handleExtract(node)}>
                      <FileArchive size={15} />
                    </button>
                  ) : null}
                  <button className="drive-icon-button" title="改名" onClick={() => setRenaming({ id: node.id, name: node.name })}>
                    <Pencil size={15} />
                  </button>
                  <button className="drive-icon-button" title="复制到当前目录" disabled={busy !== ""} onClick={() => void handleCopy(node)}>
                    <Copy size={15} />
                  </button>
                  <button className="drive-icon-button" title="删除（进回收站）" onClick={() => setPendingTrash([node])}>
                    <Trash2 size={15} />
                  </button>
                </span>
              </div>
            ))}
          </div>

          {listing?.truncated ? (
            <button className="drive-more" onClick={() => void loadNodes(parentId, { cursor: listing.next_cursor, append: true })}>
              还有更多（已显示 {nodes.length} / {listing.total}），加载下一页
            </button>
          ) : null}

          {!nodes.length && !creating ? (
            <EmptyState>
              <Cloud size={16} /> 这个目录还是空的；把文件拖进来就能上传（同名不会覆盖，会先提示）
            </EmptyState>
          ) : null}
        </Panel>
      ) : (
        <Panel
          title="回收站"
          subtitle="这里的东西仍占空间；恢复是回到原目录，彻底清除才释放空间"
          testId="drive-trash"
          actions={
            <button className="text-button" onClick={() => void loadTrash()}>
              <RefreshCcw size={14} /> 刷新
            </button>
          }
        >
          <div className="drive-list">
            {trash.map((node) => (
              <div className="drive-row" key={node.id} data-testid={`drive-trash-${node.id}`}>
                <span className="drive-cell-check" />
                <span className="drive-cell-name">
                  <span className="drive-name-button">
                    {node.kind === "directory" ? <Folder size={15} /> : <FileText size={15} />}
                    <span className="drive-node-name">{node.name}</span>
                  </span>
                  <small>
                    {node.kind === "file" ? `${formatBytes(node.size_bytes)} · ` : ""}
                    {node.trashed_count && node.trashed_count > 1 ? `整棵 ${node.trashed_count} 项 · ` : ""}
                    删除于 {formatTime(node.deleted_at ?? node.updated_at)}
                  </small>
                </span>
                <span className="drive-cell-size" data-label="大小">
                  {node.kind === "directory" ? "—" : formatBytes(node.size_bytes)}
                </span>
                <span className="drive-cell-time" />
                <span className="drive-cell-actions">
                  <button className="text-button" disabled={busy !== ""} onClick={() => void handleRestore(node)}>
                    <CornerUpLeft size={14} /> 恢复
                  </button>
                  <button className="text-button drive-danger" disabled={busy !== ""} onClick={() => setPendingPurge(node)}>
                    <Trash2 size={14} /> 彻底清除
                  </button>
                </span>
              </div>
            ))}
          </div>
          {!trash.length ? (
            <EmptyState>
              <Trash2 size={16} /> 回收站是空的；删除的文件会先来这里（可恢复），彻底清除才会释放空间
            </EmptyState>
          ) : null}
        </Panel>
      )}

      {uploads.length || notes.length ? (
        <Panel title="传输与任务" subtitle="上传进度与最近操作结果（失败会如实写在这里，不假装成功）" testId="drive-jobs">
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
          {notes.map((line) => (
            <div className="drive-job drive-note" key={line}>
              {line}
            </div>
          ))}
        </Panel>
      ) : null}

      {detail ? (
        <aside className="drive-drawer" role="dialog" aria-label="文件详情" data-testid="drive-detail">
          <div className="drive-drawer-head">
            <div>
              <strong>{detail.node.name}</strong>
              <small>
                {kindLabel(detail.node)} · {detail.node.kind === "file" ? formatBytes(detail.node.size_bytes) : "文件夹"}
              </small>
            </div>
            <button className="drive-icon-button" aria-label="关闭" onClick={() => setDetail(null)}>
              ×
            </button>
          </div>
          <dl className="drive-detail-grid">
            <dt>路径</dt>
            <dd>{detail.breadcrumb.map((crumb) => crumb.name).join(" / ")}</dd>
            <dt>内容 sha256</dt>
            <dd title={detail.node.content_hash ?? ""}>{detail.node.content_hash ? `${detail.node.content_hash.slice(0, 24)}…` : "—"}</dd>
            <dt>修订</dt>
            <dd>
              {detail.node.revision}
              {detail.node.scan_status !== "clean" ? ` · 状态 ${detail.node.scan_status}` : ""}
            </dd>
            <dt>上传</dt>
            <dd>{formatTime(detail.node.created_at)}</dd>
            <dt>修改</dt>
            <dd>{formatTime(detail.node.updated_at)}</dd>
            <dt>来源成果物</dt>
            <dd>{detail.node.source_artifact_id ? `${detail.node.source_artifact_id.slice(0, 8)}…` : "—（不是从成果物转来的）"}</dd>
            <dt>项目引用</dt>
            <dd>
              {detail.refs.length ? (
                detail.refs.map((ref) => (
                  <Link key={ref.id} href={`/artifacts?artifact=${ref.artifact_id ?? ""}`} className="drive-ref">
                    <ExternalLink size={12} /> {ref.artifact_id ? `${String(ref.artifact_id).slice(0, 8)}…` : "历史导入（无成果物 id）"}
                  </Link>
                ))
              ) : (
                <span>—（还没导入过项目）</span>
              )}
            </dd>
          </dl>
          <div className="drive-drawer-actions">
            {detail.node.kind === "file" ? (
              <button className="text-button" onClick={() => void handleDownload(detail.node)}>
                <Download size={14} /> 下载
              </button>
            ) : null}
            <button className="text-button" data-testid="drive-grant-open" onClick={() => setGrantTarget(detail.node)}>
              <ShieldCheck size={14} /> 授权给 Agent
            </button>
            {detail.node.kind === "file" ? (
              <button className="text-button" data-testid="drive-copy-to-workspace" onClick={() => openCopyToWorkspace(detail.node)}>
                <FolderInput size={14} /> 复制到工作区
              </button>
            ) : null}
            {detail.node.is_archive ? (
              <button className="text-button" disabled={busy !== ""} onClick={() => void handleExtract(detail.node)}>
                <FileArchive size={14} /> 解压
              </button>
            ) : null}
            <button className="text-button" onClick={() => setRenaming({ id: detail.node.id, name: detail.node.name })}>
              <Pencil size={14} /> 改名
            </button>
            <button className="text-button" onClick={() => void openPicker([detail.node])}>
              <Move size={14} /> 移动
            </button>
            <button className="text-button" disabled={busy !== ""} onClick={() => void handleCopy(detail.node)}>
              <Copy size={14} /> 复制
            </button>
            {detail.node.kind === "file" ? (
              <>
                <button className="text-button" disabled={busy !== ""} onClick={() => setImportTarget(detail.node)}>
                  <FolderPlus size={14} /> 加入项目
                </button>
                <button className="text-button" disabled={busy !== ""} onClick={() => void handleConvert(detail.node)} title="PDF 转 Markdown 后写入知识库">
                  <FileText size={14} /> 转换入知识库
                </button>
              </>
            ) : null}
            <button className="text-button drive-danger" onClick={() => setPendingTrash([detail.node])}>
              <Trash2 size={14} /> 删除
            </button>
          </div>
          <div className="drive-audit" data-testid="drive-grants">
            <h3>授权给哪些 Agent（只能读 + 导入项目）</h3>
            {grants.length ? (
              grants.map((grant) => (
                <div className="drive-audit-row" key={grant.id}>
                  <span className={`badge ${grant.revoked_at ? "status-red" : grant.active ? "status-green" : "status-neutral"}`}>
                    {grant.agent_id}
                  </span>
                  <small>
                    {grant.scope_type === "file" ? "这个文件" : grant.scope_type === "folder" ? "整个文件夹（含当时已有节点）" : "整个云盘"}
                    {grant.include_future_nodes ? " · 含以后新增" : ""} · 到期 {formatTime(grant.expires_at)}
                    {grant.revoked_at ? " · 已撤销" : ""}
                  </small>
                  {grant.revoked_at ? null : (
                    <button className="text-button drive-danger" disabled={busy !== ""} onClick={() => void handleRevokeGrant(grant)}>
                      撤销
                    </button>
                  )}
                </div>
              ))
            ) : (
              <p className="hint">还没有 Agent 被授权读这个文件。授权在「设备与接入」或本抽屉的「授权给 Agent」入口里创建。</p>
            )}
          </div>
          <div className="drive-audit">
            <h3>最近操作</h3>
            {detail.audit.length ? (
              detail.audit.map((event) => (
                <div className="drive-audit-row" key={event.id}>
                  <span className={`badge ${event.decision === "allow" ? "status-neutral" : "status-red"}`}>{event.action}</span>
                  <small>
                    {event.decision === "deny" ? `拒绝（${event.reason}）· ` : ""}
                    {formatTime(event.created_at)}
                  </small>
                </div>
              ))
            ) : (
              <p className="hint">这个文件还没有操作记录</p>
            )}
          </div>
        </aside>
      ) : null}

      {moveTarget && pickerListing ? (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => setMoveTarget(null)}>
          <div className="modal" role="dialog" aria-modal="true" data-testid="drive-move-modal" onMouseDown={(event) => event.stopPropagation()}>
            <div className="modal-heading">
              <div>
                <h2>移动到…</h2>
                <p>
                  {moveTarget.length === 1 ? moveTarget[0].name : `${moveTarget.length} 项`} →{" "}
                  {pickerListing.breadcrumb.map((crumb) => crumb.name).join(" / ")}
                </p>
              </div>
              <button className="app-icon" aria-label="关闭" onClick={() => setMoveTarget(null)}>
                ×
              </button>
            </div>
            <div className="drive-picker" data-testid="drive-picker">
              {pickerListing.parent.is_root ? null : (
                <button className="text-button" onClick={() => void pickerUp()}>
                  <CornerUpLeft size={14} /> 返回上一级
                </button>
              )}
              {pickerListing.nodes
                .filter((node) => node.kind === "directory")
                .map((node) => (
                  <button key={node.id} className="drive-picker-row" onClick={() => void pickerEnter(node)}>
                    <Folder size={14} /> {node.name}
                  </button>
                ))}
              {!pickerListing.nodes.filter((node) => node.kind === "directory").length ? <p className="hint">这个目录里没有子文件夹</p> : null}
            </div>
            <div className="modal-actions">
              <button className="button button-secondary" onClick={() => setMoveTarget(null)}>
                取消
              </button>
              <button className="button button-primary" data-testid="drive-move-confirm" disabled={busy === "move"} onClick={() => void handleMove()}>
                移到这个目录
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {pendingTrash ? (
        <ConfirmDialog
          title="移入回收站？"
          description={
            <>
              <strong>{pendingTrash.length === 1 ? pendingTrash[0].name : `${pendingTrash.length} 项`}</strong>
              <div style={{ marginTop: 6 }}>
                删除后进回收站（仍占配额），可以恢复；要真正释放空间需要在回收站里「彻底清除」。被项目引用过的文件不会被彻底清除。
              </div>
            </>
          }
          confirmLabel="移入回收站"
          tone="danger"
          busy={busy === "trash"}
          testId="drive-trash-confirm"
          onCancel={() => setPendingTrash(null)}
          onConfirm={() => {
            const targets = pendingTrash;
            setPendingTrash(null);
            void handleTrash(targets);
          }}
        />
      ) : null}

      {pendingPurge ? (
        <ConfirmDialog
          title="彻底清除？"
          description={
            <>
              <strong>{pendingPurge.name}</strong>
              <div style={{ marginTop: 6 }}>
                彻底清除后无法恢复，这一步才会释放空间；如果它被项目引用过，服务端会拒绝。
              </div>
            </>
          }
          confirmLabel="彻底清除"
          tone="danger"
          busy={busy === `purge-${pendingPurge.id}`}
          testId="drive-purge-confirm"
          onCancel={() => setPendingPurge(null)}
          onConfirm={() => {
            const target = pendingPurge;
            setPendingPurge(null);
            void handlePurge(target);
          }}
        />
      ) : null}

      {copyTarget ? (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => setCopyTarget(null)}>
          <div className="modal" role="dialog" aria-modal="true" data-testid="drive-copy-modal" onMouseDown={(event) => event.stopPropagation()}>
            <div className="modal-heading">
              <div>
                <h2>复制到 Agent 工作区</h2>
                <p>{copyTarget.name} · 显式复制一次（不做后台同步）</p>
              </div>
              <button className="app-icon" aria-label="关闭" onClick={() => setCopyTarget(null)}>
                ×
              </button>
            </div>
            <label className="drive-grant-field">
              <span>目标工作区</span>
              <select value={copyWorkspaceId} onChange={(event) => setCopyWorkspaceId(event.target.value)} data-testid="drive-copy-workspace">
                <option value="">选择工作区…</option>
                {workspaces.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.display_name}（{item.status === "online" ? "在线" : "离线"}）
                  </option>
                ))}
              </select>
            </label>
            <label className="drive-grant-field">
              <span>目标相对路径（留空 = 用文件名）</span>
              <input
                value={copyPath}
                data-testid="drive-copy-path"
                onChange={(event) => setCopyPath(event.target.value)}
                placeholder="papers/数据.csv"
              />
            </label>
            <label className="drive-grant-field drive-grant-check">
              <input type="checkbox" checked={copyOverwrite} onChange={(event) => setCopyOverwrite(event.target.checked)} />
              <span>允许覆盖工作区里的同名文件（默认不覆盖）</span>
            </label>
            {copyStatus ? (
              <p className="hint" data-testid="drive-copy-status">
                {copyStatus}
              </p>
            ) : null}
            <div className="modal-actions">
              <button className="button button-secondary" onClick={() => setCopyTarget(null)}>
                关闭
              </button>
              <button className="button button-primary" data-testid="drive-copy-confirm" disabled={busy !== "" || !copyWorkspaceId} onClick={() => void handleCopyToWorkspace()}>
                复制
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {grantTarget ? (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => setGrantTarget(null)}>
          <div className="modal" role="dialog" aria-modal="true" data-testid="drive-grant-modal" onMouseDown={(event) => event.stopPropagation()}>
            <div className="modal-heading">
              <div>
                <h2>授权给 Agent</h2>
                <p>{grantTarget.name} · 只开放「读 + 导入项目」，不给删除/移动/改名</p>
              </div>
              <button className="app-icon" aria-label="关闭" onClick={() => setGrantTarget(null)}>
                ×
              </button>
            </div>
            <label className="drive-grant-field">
              <span>给哪台 Agent</span>
              <select value={grantWorkspaceId} onChange={(event) => setGrantWorkspaceId(event.target.value)} data-testid="drive-grant-agent">
                <option value="">选择工作区…</option>
                {workspaces.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.display_name}（{item.status === "online" ? "在线" : "离线"}）
                  </option>
                ))}
              </select>
            </label>
            <label className="drive-grant-field">
              <span>有效期</span>
              <select value={grantDays} onChange={(event) => setGrantDays(Number(event.target.value))}>
                <option value={1}>1 天</option>
                <option value={7}>7 天</option>
                <option value={30}>30 天（上限）</option>
              </select>
            </label>
            <p className="hint">
              授权绑定这台 Agent 与它所在的项目；到期或撤销后 Agent 的**下一次读取立刻失败**。已物化到工作区的输入副本按计划保留。
            </p>
            <div className="modal-actions">
              <button className="button button-secondary" onClick={() => setGrantTarget(null)}>
                取消
              </button>
              <button
                className="button button-primary"
                data-testid="drive-grant-confirm"
                disabled={busy !== "" || !grantWorkspaceId}
                onClick={() => void handleCreateGrant()}
              >
                授权
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {importTarget ? (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => setImportTarget(null)}>
          <div className="modal" role="dialog" aria-modal="true" data-testid="drive-import-modal" onMouseDown={(event) => event.stopPropagation()}>
            <div className="modal-heading">
              <div>
                <h2>加入项目空间</h2>
                <p>
                  {importTarget.name}（{formatBytes(importTarget.size_bytes)}）将登记为当前项目的成果物
                </p>
              </div>
              <button className="app-icon" aria-label="关闭" onClick={() => setImportTarget(null)}>
                ×
              </button>
            </div>
            <div className="hint">
              导入后内容进入项目对象存储并登记 SHA-256；正式下游使用仍需通过项目审批门禁。云盘里这份是**复制**语义，不会消失。
            </div>
            <div className="modal-actions">
              <button className="button button-secondary" onClick={() => setImportTarget(null)}>
                取消
              </button>
              <button className="button button-primary" data-testid="drive-import-confirm" disabled={busy !== ""} onClick={() => void handleImport(importTarget)}>
                确认导入
              </button>
            </div>
          </div>
        </div>
      ) : null}
        </>
        )}
        </div>
      </div>
    </div>
  );
}
