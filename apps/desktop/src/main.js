// 桌面端客户端：主进程（DESKTOP-GUI D-1/D-2）
//
// 职责划分（壳只做壳的事，业务都在 Python 内核与服务器里）：
//   - 主窗口 = 平台工作台（内嵌，可配服务器地址）；托盘常驻、单实例锁
//   - 启动/健康检查/异常重启 Python sidecar，并通过契约 v1 读它的状态
//   - 桌面专属：系统通知、托盘角标、开机自启、深链、快捷键、检查更新
//   - 首次运行向导 / 本机状态页（renderer/setup.html，离线也能开）
//
// 与旧版的差别：工作台从"丢给系统浏览器"（shell.openExternal）改成**在应用窗口里打开**，
// 并把内核能力通过 preload 桥（`window.synapforgeShell`）接进工作台，供「本机 Agent」面板使用。
const { app, BrowserWindow, Tray, Menu, ipcMain, shell, nativeImage, clipboard, Notification, dialog } = require("electron");
const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");
const { spawn } = require("node:child_process");
const { diffAttention } = require("./attention.js");

const CONTRACT = "v1";
const STATE_DIR = process.env.MAP_STATE_DIR || path.join(process.env.LOCALAPPDATA || os.homedir(), "MathAgentPlatform");
const INFO_PATH = path.join(STATE_DIR, "sidecar.json");
const CONFIG_PATH = path.join(STATE_DIR, "desktop.json");
const ATTENTION_PATH = path.join(STATE_DIR, "attention.json");
const ATTENTION_INTERVAL_MS = 60_000;
const ASSETS = path.join(__dirname, "..", "assets");
const DEFAULT_PLATFORM_URL = process.env.MAP_DESKTOP_PLATFORM_URL || "https://synapforge.top";
const SELF_CHECK = process.env.MAP_DESKTOP_SELFCHECK === "1";

const runtime = {
  sidecar: null,
  info: null,
  status: null,
  lastError: null,
  paired: false,
  platformUrl: DEFAULT_PLATFORM_URL,
  restartAttempts: 0,
  config: { platform_url: DEFAULT_PLATFORM_URL, auto_launch: false, window_bounds: null, configured: false },
  pending: 0,
  lastTaskKey: null,
  // 关注清单（DESKTOP-NOTIFY）：token 只存内存（不落盘），已见键落 attention.json 做跨启动去重
  attention: { context: null, seen: [], timer: null, last_error: null, last_poll_at: null, assigned_total: 0, review_total: 0 },
  lastCompleted: null,
  connectionState: null,
};

let tray = null;
let workbench = null; // 主窗口：平台工作台
let setupWindow = null; // 首次运行向导 / 本机状态页（本地页）
let quitting = false;

// ---- 配置（desktop.json） -------------------------------------------------

function loadConfig() {
  try {
    const raw = JSON.parse(fs.readFileSync(CONFIG_PATH, "utf-8"));
    runtime.config = { ...runtime.config, ...raw };
  } catch {
    /* 首次运行没有配置文件 */
  }
  runtime.platformUrl = String(runtime.config.platform_url || DEFAULT_PLATFORM_URL);
  return runtime.config;
}

function saveConfig(patch) {
  runtime.config = { ...runtime.config, ...patch };
  runtime.platformUrl = String(runtime.config.platform_url || DEFAULT_PLATFORM_URL);
  try {
    fs.mkdirSync(STATE_DIR, { recursive: true });
    fs.writeFileSync(CONFIG_PATH, JSON.stringify(runtime.config, null, 2), "utf-8");
  } catch (error) {
    logLine(`[shell] 配置写入失败：${error}`);
  }
  return runtime.config;
}

// ---- Agent 工作区（FM-0） -------------------------------------------------
//
// 工作区是内核的**启动参数**（`--workspace`）：任务在这里跑、输入文件下到 `<工作区>/inputs/`、
// 产物也从这里扫。以前壳不传这个参数，内核只好用进程当前目录——打包后那正是安装目录
// （`resources\sidecar`），于是"用户的文件"写进了程序安装目录（还常是只读），
// 平台看到的 `local_workspace` 也就永远和用户以为的不是一回事。
// 现在：用户选 → 记进 desktop.json → 每次启动都显式传给内核（内核自己也会把它记一份）。

function defaultWorkspace() {
  return path.join(app.getPath("documents"), "MathAgentWorkspace");
}

function workspacePath() {
  const configured = String(runtime.config.workspace || "").trim();
  return configured ? path.resolve(configured) : defaultWorkspace();
}

function ensureWorkspace() {
  const target = workspacePath();
  try {
    fs.mkdirSync(target, { recursive: true });
  } catch (error) {
    // 建不出来不拦启动：内核那边会如实报 workspace_unavailable，用户能在本机页看到原因
    logLine(`[shell] 工作区不可用：${target}（${String(error)}）`);
  }
  return target;
}

async function chooseWorkspace() {
  const current = workspacePath();
  const parent = BrowserWindow.getFocusedWindow() || workbench || setupWindow || null;
  const options = {
    title: "选择 Agent 工作目录",
    defaultPath: current,
    buttonLabel: "用这个目录",
    properties: ["openDirectory", "createDirectory"],
  };
  const result = parent && !parent.isDestroyed() ? await dialog.showOpenDialog(parent, options) : await dialog.showOpenDialog(options);
  if (result.canceled || !result.filePaths.length) {
    return { cancelled: true, workspace: current };
  }
  const chosen = path.resolve(result.filePaths[0]);
  saveConfig({ workspace: chosen });
  logLine(`[shell] Agent 工作目录改为 ${chosen}（重启内核生效）`);
  // 必须重启内核：工作区是启动参数。热改会让"正在跑的那一轮"中途换地址，
  // 输入与产物落在两个目录里，比晚几秒生效糟糕得多。
  restartSidecar();
  broadcast();
  return { cancelled: false, workspace: chosen };
}

// ---- sidecar 生命周期 ----------------------------------------------------

function resolveSidecarCommand() {
  const workspace = ensureWorkspace();
  const workspaceArg = ["--workspace", workspace];
  // 1) 打包后：随安装包一起分发的内核
  const packaged = path.join(process.resourcesPath || "", "sidecar", "math-agent-sidecar.exe");
  if (fs.existsSync(packaged)) return { cmd: packaged, args: ["--state-dir", STATE_DIR, ...workspaceArg], mode: "packaged" };
  // 2) 开发态：直接用仓库里的 Python 源码跑
  const repo = path.resolve(__dirname, "..", "..", "..");
  const entry = path.join(repo, "apps", "agent", "agentd.py");
  if (fs.existsSync(entry)) {
    return {
      cmd: process.env.MAP_PYTHON || "python",
      args: ["-X", "utf8", entry, "daemon-run", "--state-dir", STATE_DIR, ...workspaceArg],
      mode: "source",
      cwd: path.join(repo, "apps", "agent"),
    };
  }
  return null;
}

function startSidecar() {
  const resolved = resolveSidecarCommand();
  if (!resolved) {
    runtime.lastError = "找不到内核：既没有打包的 sidecar，也没有仓库源码";
    updateTray();
    return;
  }
  try {
    fs.unlinkSync(INFO_PATH); // 确保读到的是本次启动的信息
  } catch {
    /* 不存在即可 */
  }
  runtime.sidecar = spawn(resolved.cmd, resolved.args, {
    cwd: resolved.cwd || path.dirname(resolved.cmd),
    // PYTHONUTF8/PYTHONIOENCODING：老内核在 Windows 上可能用 cp1252 打中文日志而崩溃；
    // 新内核自己会 reconfigure（`sidecar_entry._force_utf8_stdio`），这里是双保险。
    env: { ...process.env, MAP_STATE_DIR: STATE_DIR, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8" },
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });
  runtime.sidecar.stdout.on("data", (chunk) => logLine(`[sidecar] ${String(chunk).trim()}`));
  runtime.sidecar.stderr.on("data", (chunk) => logLine(`[sidecar:err] ${String(chunk).trim()}`));
  runtime.sidecar.on("exit", (code) => {
    logLine(`[shell] sidecar 退出，code=${code}`);
    runtime.sidecar = null;
    runtime.info = null;
    runtime.status = null;
    updateTray();
    // 异常退出自动重启（最多 3 次，避免崩溃循环）
    if (runtime.restartAttempts < 3) {
      runtime.restartAttempts += 1;
      setTimeout(startSidecar, 2000);
    } else {
      runtime.lastError = `内核连续退出 ${runtime.restartAttempts} 次，已停止自动重启`;
      updateTray();
    }
  });
  waitForInfo();
}

function restartSidecar() {
  // 计数归零：换工作区是我们**主动**杀内核，不该算进"异常退出"的 3 次上限里
  runtime.restartAttempts = 0;
  runtime.info = null;
  runtime.status = null;
  if (runtime.sidecar && !runtime.sidecar.killed) {
    runtime.sidecar.kill(); // exit 处理器会在 2 秒后用新参数重启
    return;
  }
  startSidecar();
}

function waitForInfo(deadlineSeconds = 10) {
  const started = Date.now();
  const timer = setInterval(() => {
    if (fs.existsSync(INFO_PATH)) {
      try {
        const info = JSON.parse(fs.readFileSync(INFO_PATH, "utf-8"));
        if (info.contract === CONTRACT && info.port && info.token) {
          clearInterval(timer);
          runtime.info = info;
          runtime.restartAttempts = 0;
          logLine(`[shell] 内核就绪 port=${info.port} contract=${info.contract}`);
          void pollStatus();
          return;
        }
      } catch {
        /* 文件可能还在写，下一轮再读 */
      }
    }
    if (Date.now() - started > deadlineSeconds * 1000) {
      clearInterval(timer);
      runtime.lastError = `内核未在 ${deadlineSeconds} 秒内就绪`;
      updateTray();
    }
  }, 500);
}

// ---- 契约调用（sidecar 本地 HTTP） --------------------------------------

async function api(pathname, { method = "GET", body } = {}) {
  if (!runtime.info) throw new Error("sidecar_not_ready");
  const response = await fetch(`http://127.0.0.1:${runtime.info.port}${pathname}`, {
    method,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${runtime.info.token}` },
    body: body ? JSON.stringify(body) : undefined,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `http_${response.status}`);
  return payload;
}

async function pollStatus() {
  try {
    runtime.status = await api("/status");
    runtime.paired = Boolean(runtime.status.identity?.device_id);
    runtime.platformUrl = runtime.status.identity?.platform_url || runtime.platformUrl;
    runtime.lastError = null;
  } catch (error) {
    runtime.lastError = String(error.message || error);
  }
  notifyTransitions();
  updateTray();
  updateBadge();
  broadcast();
}

function snapshot() {
  return {
    ...runtime.status,
    shell: {
      paired: runtime.paired,
      platform_url: runtime.platformUrl,
      last_error: runtime.lastError,
      contract: CONTRACT,
      app_version: app.getVersion(),
      pending: runtime.pending,
      auto_launch: Boolean(runtime.config.auto_launch),
      workspace: workspacePath(),
      attention: attentionSnapshot(),
    },
  };
}

function broadcast() {
  const payload = snapshot();
  for (const target of [workbench, setupWindow]) {
    if (target && !target.isDestroyed()) target.webContents.send("status", payload);
  }
}

// ---- 桌面专属：通知、角标、更新 -----------------------------------------

/** 从内核状态变化里推通知：领取到任务、任务完成、连接断开/恢复。 */
function notifyTransitions() {
  const status = runtime.status || {};
  const tasks = status.tasks || {};
  const current = tasks.claimed_current || null;
  const completed = Number(tasks.completed_since_start || 0);
  const connection = status.connection?.state || "disconnected";
  const key = current ? String(current.id || current.title || "") : null;

  if (key && key !== runtime.lastTaskKey) {
    notify("开始执行任务", String(current.title || current.id || "").slice(0, 120), "task");
  }
  runtime.lastTaskKey = key;
  if (runtime.lastCompleted !== null && completed > runtime.lastCompleted) {
    notify("任务已完成", `本机内核已完成 ${completed - runtime.lastCompleted} 个任务`, "task");
  }
  runtime.lastCompleted = completed;
  if (runtime.connectionState === "connected" && connection !== "connected") {
    notify("与平台断开", `连接状态：${connection}`, "status");
  }
  if (runtime.connectionState && runtime.connectionState !== connection && connection === "connected") {
    notify("已连回平台", "内核与平台的连接已恢复", "status");
  }
  runtime.connectionState = connection;
  runtime.pending = Number(tasks.open_for_me ?? tasks.pending ?? 0) || 0;
}

function notify(title, body, kind = "info", url = "") {
  try {
    if (!Notification.isSupported()) return;
    const item = new Notification({ title: `${title} · synapforge`, body: String(body || "") });
    item.on("click", () => {
      showWorkbench();
      // 关注清单的通知带站内路径（任务详情）→ 点开直达；内核那几条仍落到「我的任务」
      const target = url ? `${runtime.platformUrl.replace(/\/$/, "")}${url}` : kind === "task" ? `${runtime.platformUrl.replace(/\/$/, "")}/my-tasks` : "";
      if (target) navigateWorkbench(target);
    });
    item.show();
  } catch (error) {
    logLine(`[shell] 通知失败：${error}`);
  }
}

/** 托盘角标：Windows 用任务栏叠加图标（`setBadgeCount` 在 Windows 是 no-op，仍调用以便 mac/linux 生效）。 */
function updateBadge() {
  const count = runtime.pending;
  try {
    app.setBadgeCount(count);
  } catch {
    /* 平台不支持就当没有 */
  }
  if (!workbench || workbench.isDestroyed()) return;
  try {
    if (count > 0) {
      const dot = nativeImage.createFromPath(path.join(ASSETS, "tray-yellow.png"));
      workbench.setOverlayIcon(dot, `${count} 项待办`);
    } else {
      workbench.setOverlayIcon(null, "");
    }
  } catch {
    /* 图标缺失不影响主流程 */
  }
}

/** 检查更新：读平台 `/downloads/latest.json`（发布时生成），版本更高就提示并打开下载页。 */
async function checkUpdate({ silent = true } = {}) {
  try {
    const url = `${runtime.platformUrl.replace(/\/$/, "")}/downloads/latest.json`;
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) throw new Error(`http_${response.status}`);
    const manifest = await response.json();
    const latest = String(manifest.version || "");
    const current = app.getVersion();
    if (!latest || compareVersions(latest, current) <= 0) {
      if (!silent) notify("已是最新版本", `当前 ${current}`);
      return { ok: true, up_to_date: true, current, latest };
    }
    const page = manifest.download_url || `${runtime.platformUrl.replace(/\/$/, "")}/downloads/`;
    if (silent) {
      notify("有新的桌面端版本", `${current} → ${latest}，点此打开下载页`);
      return { ok: true, up_to_date: false, current, latest, download_url: page };
    }
    const choice = await dialog.showMessageBox({
      type: "info",
      buttons: ["打开下载页", "稍后"],
      defaultId: 0,
      cancelId: 1,
      title: "synapforge 桌面端有新版本",
      message: `当前 ${current}，最新 ${latest}`,
      detail: manifest.notes || "下载新安装包覆盖安装即可；配置、登录态与设备配对都会保留。",
    });
    if (choice.response === 0) shell.openExternal(page);
    return { ok: true, up_to_date: false, current, latest, download_url: page };
  } catch (error) {
    return { ok: false, error: String(error.message || error) };
  }
}

function compareVersions(a, b) {
  const parse = (value) => String(value).split(".").map((part) => parseInt(part, 10) || 0);
  const left = parse(a);
  const right = parse(b);
  for (let index = 0; index < Math.max(left.length, right.length); index += 1) {
    const diff = (left[index] || 0) - (right[index] || 0);
    if (diff !== 0) return diff > 0 ? 1 : -1;
  }
  return 0;
}

/* ---- 关注清单（DESKTOP-NOTIFY）：派给我的任务 / 待我复核 -------------------
 *
 * 为什么在壳里轮询而不是只靠前端：窗口关掉（托盘常驻）时渲染进程已经销毁，
 * 而"有人把任务派给你""有个东西等你复核"恰恰是关着窗口时也要知道的。
 * 令牌只保存在**内存**里（不落盘、不进配置文件）；重启后需要重新登录才会继续轮询，
 * 这是刻意的取舍：宁可少通知，也不把会话令牌写到磁盘上。
 */

function loadAttentionSeen() {
  try {
    const raw = JSON.parse(fs.readFileSync(ATTENTION_PATH, "utf-8"));
    runtime.attention.seen = Array.isArray(raw.seen) ? raw.seen : [];
  } catch {
    runtime.attention.seen = [];
  }
}

function saveAttentionSeen() {
  try {
    fs.mkdirSync(STATE_DIR, { recursive: true });
    fs.writeFileSync(ATTENTION_PATH, JSON.stringify({ seen: runtime.attention.seen.slice(-300), updated_at: new Date().toISOString() }, null, 2), "utf-8");
  } catch (error) {
    logLine(`[shell] 关注清单去重记录写入失败：${error}`);
  }
}

/** 前端登录后把会话上下文交过来（token 只在内存里）。 */
function setAttentionContext(context) {
  const token = String(context?.token || "");
  const apiUrl = String(context?.api_url || context?.apiUrl || "").replace(/\/$/, "");
  const memberId = String(context?.member_id || context?.memberId || "");
  if (!token || !apiUrl) {
    runtime.attention.context = null;
    stopAttentionTimer();
    return { ok: false, error: "token_and_api_url_required" };
  }
  const changed = runtime.attention.context?.token !== token || runtime.attention.context?.api_url !== apiUrl;
  runtime.attention.context = { token, api_url: apiUrl, member_id: memberId };
  if (changed) loadAttentionSeen();
  startAttentionTimer();
  void pollAttention();
  return { ok: true, member_id: memberId, api_url: apiUrl, seen: runtime.attention.seen.length };
}

function startAttentionTimer() {
  stopAttentionTimer();
  runtime.attention.timer = setInterval(() => void pollAttention(), ATTENTION_INTERVAL_MS);
}

function stopAttentionTimer() {
  if (runtime.attention.timer) {
    clearInterval(runtime.attention.timer);
    runtime.attention.timer = null;
  }
}

/** 拉一次关注清单：新条目发系统通知（点击直达任务）。 */
async function pollAttention() {
  const context = runtime.attention.context;
  if (!context) return { ok: false, error: "no_attention_context" };
  let payload;
  try {
    const response = await fetch(`${context.api_url}/api/my-attention`, {
      headers: { Authorization: `Bearer ${context.token}` },
      cache: "no-store",
    });
    if (response.status === 401 || response.status === 403) {
      // 会话失效：停止轮询并**只提示一次**，不做"每 60 秒报一次未登录"的骚扰
      runtime.attention.context = null;
      stopAttentionTimer();
      runtime.attention.last_error = `attention_unauthorized_${response.status}`;
      notify("登录已过期", "桌面端已停止检查派单与复核提醒，重新登录后自动恢复");
      return { ok: false, error: "unauthorized" };
    }
    if (!response.ok) throw new Error(`http_${response.status}`);
    payload = await response.json();
  } catch (error) {
    runtime.attention.last_error = String(error.message || error);
    return { ok: false, error: runtime.attention.last_error };
  }

  const result = diffAttention({ seen: runtime.attention.seen }, payload);
  runtime.attention.seen = result.seen;
  runtime.attention.assigned_total = result.assigned_total;
  runtime.attention.review_total = result.review_total;
  runtime.attention.last_error = null;
  runtime.attention.last_poll_at = new Date().toISOString();
  // 待办数进托盘角标（内核的"本地队列"之外，人该干的活也算待办）
  runtime.pending = result.assigned_total + result.review_total;
  updateBadge();
  updateTray();

  for (const item of result.notifications) {
    logLine(`[shell] 通知(${item.kind})：${item.title} — ${item.body}`);
    notify(item.title, item.body, item.kind, item.url);
  }
  if (result.notifications.length) saveAttentionSeen();
  return { ok: true, ...result };
}

function attentionSnapshot() {
  return {
    active: Boolean(runtime.attention.context),
    member_id: runtime.attention.context?.member_id || null,
    api_url: runtime.attention.context?.api_url || null,
    assigned_total: runtime.attention.assigned_total,
    review_total: runtime.attention.review_total,
    seen: runtime.attention.seen.length,
    last_poll_at: runtime.attention.last_poll_at,
    last_error: runtime.attention.last_error,
  };
}

// ---- 托盘 ---------------------------------------------------------------

function trayImage() {
  const state = runtime.status?.connection?.state;
  const name = runtime.lastError ? "red" : !runtime.paired ? "grey" : state === "connected" ? "green" : "yellow";
  return nativeImage.createFromPath(path.join(ASSETS, `tray-${name}.png`));
}

function updateTray() {
  if (!tray) return;
  tray.setImage(trayImage());
  const status = runtime.status || {};
  const state = status.connection?.state || "disconnected";
  const inventory = (status.local_agents || []).filter((item) => item.state === "AVAILABLE").length;
  const identity = status.identity || {};
  const tasks = status.tasks || {};
  const current = tasks.claimed_current;
  tray.setToolTip(
    [
      "synapforge · 桌面端",
      `连接：${state}${runtime.paired ? "" : "（未配对）"}`,
      runtime.paired ? `设备：${identity.device_id}` : "尚未接入平台",
      `可用执行体：${inventory} 个`,
      `待办：${runtime.pending} 项 · 已完成 ${tasks.completed_since_start || 0} 个`,
      current ? `正在执行：${current.title || current.id}` : "",
      status.emergency_stop ? "紧急停止生效中" : "",
      runtime.lastError ? `最后错误：${runtime.lastError}` : "",
    ]
      .filter(Boolean)
      .join("\n"),
  );
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: current ? `正在执行：${String(current.title || current.id).slice(0, 28)}` : "当前没有执行中的任务", enabled: false },
      { type: "separator" },
      { label: "打开工作台", click: () => showWorkbench() },
      { label: "本机 Agent（窗口内）", click: () => openKernelPanel() },
      { label: "本机 Agent（独立窗口）", click: () => showSetup() },
      { type: "separator" },
      { label: "立即重扫本机 Agent", click: () => withSidecar(() => api("/agents/rescan", { method: "POST" })) },
      { label: tasks.paused ? "恢复领取任务" : "暂停领取任务", click: () => withSidecar(() => api(tasks.paused ? "/tasks/resume" : "/tasks/pause", { method: "POST" })) },
      { label: status.emergency_stop ? "解除紧急停止" : "紧急停止", click: () => withSidecar(() => api(status.emergency_stop ? "/clear-emergency-stop" : "/emergency-stop", { method: "POST" })) },
      { type: "separator" },
      { label: "开机自启", type: "checkbox", checked: Boolean(runtime.config.auto_launch), click: (item) => setAutoLaunch(Boolean(item.checked)) },
      { label: "检查更新", click: () => void checkUpdate({ silent: false }) },
      { label: "服务器地址…", click: () => showSetup() },
      { label: "Agent 工作目录…", click: () => void chooseWorkspace() },
      { type: "separator" },
      { label: "复制诊断信息", click: () => clipboard.writeText(JSON.stringify(snapshot(), null, 2)) },
      { label: "退出", click: () => { quitting = true; app.quit(); } },
    ]),
  );
}

async function withSidecar(action) {
  try {
    await action();
  } catch (error) {
    runtime.lastError = String(error.message || error);
  }
  await pollStatus();
}

function setAutoLaunch(enabled) {
  saveConfig({ auto_launch: enabled });
  app.setLoginItemSettings({ openAtLogin: enabled, path: process.execPath });
  updateTray();
  return runtime.config.auto_launch;
}

// ---- 窗口 ---------------------------------------------------------------

function windowBounds() {
  const saved = runtime.config.window_bounds;
  if (saved && typeof saved.width === "number" && typeof saved.height === "number") return saved;
  return { width: 1280, height: 860 };
}

function rememberBounds(win) {
  if (!win || win.isDestroyed()) return;
  try {
    const bounds = win.getBounds();
    saveConfig({ window_bounds: { x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height } });
  } catch {
    /* 关闭过程中的读取失败忽略 */
  }
}

function appMenu() {
  return Menu.buildFromTemplate([
    {
      label: "文件",
      submenu: [
        { label: "打开工作台", accelerator: "CmdOrCtrl+1", click: () => showWorkbench() },
        { label: "本机 Agent 面板", accelerator: "CmdOrCtrl+2", click: () => openKernelPanel() },
        { type: "separator" },
        { label: "检查更新", click: () => void checkUpdate({ silent: false }) },
        { label: "服务器地址…", click: () => showSetup() },
        { label: "Agent 工作目录…", click: () => void chooseWorkspace() },
        { type: "separator" },
        { label: "退出", role: "quit" },
      ],
    },
    {
      label: "视图",
      submenu: [
        { label: "刷新工作台", accelerator: "CmdOrCtrl+R", click: () => reloadWorkbench() },
        { role: "zoomIn", label: "放大" },
        { role: "zoomOut", label: "缩小" },
        { role: "resetZoom", label: "实际大小" },
        { type: "separator" },
        { role: "togglefullscreen", label: "全屏" },
        { role: "toggleDevTools", label: "开发者工具" },
      ],
    },
    {
      label: "窗口",
      submenu: [{ role: "minimize", label: "最小化" }, { role: "close", label: "关闭（保持托盘常驻）" }],
    },
    {
      label: "帮助",
      submenu: [
        { label: `版本 ${app.getVersion()}`, enabled: false },
        { label: "打开平台下载页", click: () => shell.openExternal(`${runtime.platformUrl.replace(/\/$/, "")}/downloads/`) },
        { label: "复制诊断信息", click: () => clipboard.writeText(JSON.stringify(snapshot(), null, 2)) },
      ],
    },
  ]);
}

function workbenchUrl() {
  return `${runtime.platformUrl.replace(/\/$/, "")}/workspace`;
}

function isInternal(url) {
  try {
    return new URL(url).origin === new URL(runtime.platformUrl).origin;
  } catch {
    return false;
  }
}

function createWorkbenchWindow() {
  if (workbench && !workbench.isDestroyed()) {
    workbench.show();
    workbench.focus();
    return workbench;
  }
  const bounds = windowBounds();
  workbench = new BrowserWindow({
    width: bounds.width,
    height: bounds.height,
    x: bounds.x,
    y: bounds.y,
    title: "synapforge · Agent 协作平台",
    backgroundColor: "#fafafa",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      // 登录态放应用自己的持久分区：与系统浏览器隔离，卸载/重装便于清理
      partition: "persist:synapforge",
      spellcheck: false,
    },
  });
  Menu.setApplicationMenu(appMenu());
  void workbench.loadURL(workbenchUrl());
  workbench.on("closed", () => {
    rememberBounds(workbench);
    workbench = null;
  });
  workbench.on("resize", () => rememberBounds(workbench));
  workbench.on("move", () => rememberBounds(workbench));

  // 外链一律交给系统浏览器；站内链接留在应用窗口里
  workbench.webContents.setWindowOpenHandler(({ url }) => {
    if (isInternal(url)) {
      void workbench.loadURL(url);
    } else {
      shell.openExternal(url);
    }
    return { action: "deny" };
  });
  workbench.webContents.on("will-navigate", (event, url) => {
    if (!isInternal(url)) {
      event.preventDefault();
      shell.openExternal(url);
    }
  });
  workbench.webContents.on("did-fail-load", (_event, errorCode, errorDescription, validatedURL) => {
    if (String(validatedURL || "").startsWith("file:")) return;
    logLine(`[shell] 工作台加载失败：${errorCode} ${errorDescription}`);
    void workbench.loadFile(path.join(__dirname, "..", "renderer", "offline.html"));
    workbench.webContents.once("did-finish-load", broadcast);
  });
  workbench.webContents.on("did-finish-load", () => {
    logLine(`[shell] 工作台已加载：${workbench.webContents.getURL()}`);
    broadcast();
  });
  // 下载（成果物导出、安装包）走系统默认行为，完成后提示
  workbench.webContents.session.on("will-download", (_event, item) => {
    item.once("done", (_e, state) => {
      if (state === "completed") notify("下载完成", item.getFilename());
    });
  });
  if (SELF_CHECK) setTimeout(() => void runSelfCheck(), 7000);
  return workbench;
}

function reloadWorkbench() {
  if (workbench && !workbench.isDestroyed()) workbench.webContents.reload();
}

function navigateWorkbench(url) {
  if (workbench && !workbench.isDestroyed()) void workbench.loadURL(url);
}

function showWorkbench() {
  createWorkbenchWindow();
  if (workbench && !workbench.isDestroyed()) {
    if (workbench.isMinimized()) workbench.restore();
    workbench.show();
    workbench.focus();
  }
}

/** 让工作台打开「本机 Agent」面板（面板在前端，数据走 preload 桥）。 */
function openKernelPanel() {
  showWorkbench();
  if (workbench && !workbench.isDestroyed()) workbench.webContents.send("open-kernel-panel");
}

/** 首次运行向导 / 本机状态页（本地页，不依赖服务器）。 */
function showSetup() {
  if (setupWindow && !setupWindow.isDestroyed()) {
    setupWindow.show();
    setupWindow.focus();
    broadcast();
    return setupWindow;
  }
  setupWindow = new BrowserWindow({
    width: 760,
    height: 640,
    title: "synapforge · 本机 Agent 与设置",
    backgroundColor: "#111111",
    webPreferences: { preload: path.join(__dirname, "preload.js"), contextIsolation: true, nodeIntegration: false },
  });
  void setupWindow.loadFile(path.join(__dirname, "..", "renderer", "setup.html"));
  setupWindow.on("closed", () => {
    setupWindow = null;
  });
  broadcast();
  return setupWindow;
}

// ---- 深链（map://pair?… / map://grant?… / map://task/<id>） ---------------

function handleDeepLink(argv) {
  const link = (argv || []).find((value) => typeof value === "string" && value.startsWith("map://"));
  if (!link) return;
  logLine(`[shell] 深链：${link.slice(0, 80)}…`);
  let parsed;
  try {
    parsed = new URL(link.replace("map://", "https://"));
  } catch {
    runtime.lastError = "深链无法解析";
    updateTray();
    return;
  }
  const action = parsed.hostname || parsed.pathname.replace(/\//g, "");
  const blob = parsed.searchParams.get("blob");
  const platform = parsed.searchParams.get("platform") || runtime.platformUrl;

  if (action === "task") {
    // 深链直达任务：平台的 /tasks 页支持 ?task=<id> 打开详情
    const taskId = parsed.pathname.replace(/^\//, "") || parsed.searchParams.get("id") || "";
    showWorkbench();
    if (taskId) navigateWorkbench(`${platform.replace(/\/$/, "")}/tasks?task=${encodeURIComponent(taskId)}`);
    return;
  }
  if (!blob) {
    runtime.lastError = "深链缺少 blob 参数";
    updateTray();
    showSetup();
    return;
  }
  showSetup();
  if (action === "grant") {
    void withSidecar(() => api("/grant", { method: "POST", body: { platform_url: platform, grant_blob: blob } }));
    return;
  }
  void withSidecar(() => api("/pair", { method: "POST", body: { platform_url: platform, pairing_blob: blob } }));
}

// ---- 自检（发布前/CI 用：MAP_DESKTOP_SELFCHECK=1） -----------------------

async function runSelfCheck() {
  const result = { ok: false, url: null, bridge: [], panel: false, version: app.getVersion() };
  try {
    if (workbench && !workbench.isDestroyed()) {
      // 自检可带一个平台会话令牌（MAP_DESKTOP_SELFCHECK_TOKEN）：登录墙后面的工作台才看得到壳的入口，
      // 不注入就只能测到 /login 页（那是"桥在、面板不在"的假阴性）。
      const token = process.env.MAP_DESKTOP_SELFCHECK_TOKEN;
      if (token) {
        await workbench.webContents.executeJavaScript(
          `window.localStorage.setItem('map.sessionToken', ${JSON.stringify(token)}); true`,
          true,
        );
        await new Promise((resolve) => {
          workbench.webContents.once("did-finish-load", resolve);
          workbench.webContents.reload();
        });
        await new Promise((resolve) => setTimeout(resolve, 4000));
      }
      result.url = workbench.webContents.getURL();
      const attention = await pollAttention();
      result.attention = { ok: Boolean(attention?.ok), assigned_total: attention?.assigned_total ?? null, review_total: attention?.review_total ?? null, notifications: (attention?.notifications || []).length };
      const probe = await workbench.webContents.executeJavaScript(
        `({ bridge: Object.keys(window.synapforgeShell || {}), panel: Boolean(document.querySelector('[data-testid="local-agent-toggle"]')), title: document.title })`,
        true,
      );
      result.bridge = probe.bridge || [];
      result.panel = Boolean(probe.panel);
      result.title = probe.title || null;
      // 真的点开面板并读一次内核状态：只断言"按钮在"太弱（按钮在、面板打不开也会通过）
      if (result.panel) {
        await workbench.webContents.executeJavaScript(
          `document.querySelector('[data-testid="local-agent-toggle"]').click(); true`,
          true,
        );
        await new Promise((resolve) => setTimeout(resolve, 2500));
        const opened = await workbench.webContents.executeJavaScript(
          `(async () => {
             const panel = document.querySelector('[data-testid="local-agent-panel"]');
             const snapshot = await window.synapforgeShell.snapshot();
             return { open: Boolean(panel), contract: snapshot?.shell?.contract || null, paired: Boolean(snapshot?.shell?.paired), agents: (snapshot?.local_agents || []).length };
           })()`,
          true,
        );
        result.panel_open = Boolean(opened?.open);
        result.kernel_contract = opened?.contract || null;
        result.kernel_agents = opened?.agents ?? 0;
      }
      result.ok =
        (probe.bridge || []).length > 0 &&
        Boolean(probe.panel) &&
        Boolean(result.panel_open) &&
        result.kernel_contract === CONTRACT &&
        !String(result.url).startsWith("file:");
    }
  } catch (error) {
    result.error = String(error.message || error);
  }
  console.log(`SELFCHECK ${JSON.stringify(result)}`);
  quitting = true;
  app.exit(result.ok ? 0 : 1);
}

// ---- 启动 ---------------------------------------------------------------

if (SELF_CHECK) {
  app.disableHardwareAcceleration();
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", (_event, argv) => {
    showWorkbench();
    handleDeepLink(argv);
  });
  app.on("window-all-closed", (event) => {
    // 托盘常驻：关窗不退出
    event.preventDefault?.();
  });
  app.whenReady().then(() => {
    if (process.defaultApp) {
      // 开发态（npm start）：显式带上脚本路径，注册才生效
      app.setAsDefaultProtocolClient("map", process.execPath, [path.resolve(process.argv[1])]);
    } else {
      app.setAsDefaultProtocolClient("map");
    }
    loadConfig();
    tray = new Tray(trayImage());
    tray.on("double-click", () => showWorkbench());
    updateTray();
    startSidecar();
    setInterval(() => {
      if (runtime.info) void pollStatus();
    }, 3000);
    handleDeepLink(process.argv);
    showWorkbench();
    if (!runtime.config.configured) showSetup(); // 首次运行：确认服务器地址与开机自启
    // 启动 20 秒后静默检查一次更新（不打断使用）
    setTimeout(() => void checkUpdate({ silent: true }), 20000);
  });
  app.on("before-quit", () => {
    quitting = true;
    if (runtime.info) void api("/shutdown", { method: "POST" }).catch(() => {});
    if (runtime.sidecar && !runtime.sidecar.killed) runtime.sidecar.kill();
  });
}

// ---- IPC（渲染进程：只读状态 + 配对 + 设置 + 桌面能力） ------------------

ipcMain.handle("snapshot", () => snapshot());
ipcMain.handle("config", () => ({ ...runtime.config, platform_url: runtime.platformUrl, app_version: app.getVersion() }));
ipcMain.handle("save-config", (_event, patch) => {
  const saved = saveConfig(patch || {});
  if (patch && "auto_launch" in patch) setAutoLaunch(Boolean(patch.auto_launch));
  return { ...saved, platform_url: runtime.platformUrl, app_version: app.getVersion() };
});
// 选 Agent 工作目录：弹系统目录选择框 → 落 desktop.json → 重启内核（工作区是启动参数）
ipcMain.handle("choose-workspace", () => chooseWorkspace());
ipcMain.handle("pair", async (_event, { platformUrl, pairingBlob }) => {
  if (platformUrl) saveConfig({ platform_url: platformUrl });
  await api("/pair", { method: "POST", body: { platform_url: platformUrl || runtime.platformUrl, pairing_blob: pairingBlob } });
  await pollStatus();
  return snapshot();
});
ipcMain.handle("grant", async (_event, { platformUrl, grantBlob }) => {
  await api("/grant", { method: "POST", body: { platform_url: platformUrl || runtime.platformUrl, grant_blob: grantBlob } });
  await pollStatus();
  return snapshot();
});
ipcMain.handle("rescan", async () => {
  await withSidecar(() => api("/agents/rescan", { method: "POST" }));
  return snapshot();
});
ipcMain.handle("toggle-pause", async () => {
  await withSidecar(() => api(runtime.status?.tasks?.paused ? "/tasks/resume" : "/tasks/pause", { method: "POST" }));
  return snapshot();
});
ipcMain.handle("toggle-emergency", async () => {
  await withSidecar(() => api(runtime.status?.emergency_stop ? "/clear-emergency-stop" : "/emergency-stop", { method: "POST" }));
  return snapshot();
});
ipcMain.handle("logs", async () => {
  // 窗口加载时内核可能还没就绪：返回空列表而不是抛错
  try {
    return (await api("/logs?tail=200")).lines;
  } catch {
    return [];
  }
});
ipcMain.handle("open-workbench", () => {
  showWorkbench();
  return snapshot();
});
ipcMain.handle("reload-workbench", () => {
  reloadWorkbench();
});
ipcMain.handle("open-setup", () => {
  showSetup();
});
ipcMain.handle("open-external", (_event, url) => {
  if (typeof url === "string" && /^https?:\/\//.test(url)) shell.openExternal(url);
});
ipcMain.handle("notify", (_event, { title, body }) => {
  // 前端也能请求系统通知（例如"有人 @ 我"）；限流由调用方负责
  if (title) notify(String(title), String(body || ""));
});
ipcMain.handle("check-update", () => checkUpdate({ silent: false }));
ipcMain.handle("set-attention-context", (_event, context) => setAttentionContext(context));
ipcMain.handle("attention-status", () => attentionSnapshot());
ipcMain.handle("poll-attention", () => pollAttention());
ipcMain.handle("set-auto-launch", (_event, enabled) => setAutoLaunch(Boolean(enabled)));

function logLine(line) {
  const stamped = `${new Date().toISOString()} ${line}`;
  console.log(stamped);
  for (const target of [setupWindow, workbench]) {
    if (target && !target.isDestroyed()) target.webContents.send("log", stamped);
  }
}