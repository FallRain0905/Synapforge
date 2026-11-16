"use strict";

// synapforge studio 桌面壳：单实例、托盘常驻、内核生命周期、窗口与菜单。
// 业务一切都在本地内核里；壳只负责"让一个本地网页活得像桌面应用"。

const {
  app, BrowserWindow, Menu, Notification, Tray, clipboard, dialog, globalShortcut,
  ipcMain, nativeImage, screen, shell,
} = require("electron");
const fs = require("node:fs");
const path = require("node:path");

const { StudioCore } = require("./core");
const { loadShellConfig } = require("./shell-config");
const { promptUpdate } = require("./updater");

// 开发期开一个本地调试端口：用 CDP 查页面地址、抓截图、看控制台错误。
// 打包后不生效（调试端口对用户没有价值，还会多开一个监听）。
// 打包后默认不开；排查现场时用 SF_DEBUG_PORT=9333 临时打开。
if (!app.isPackaged || process.env.SF_DEBUG_PORT) {
  app.commandLine.appendSwitch("remote-debugging-port", process.env.SF_DEBUG_PORT || "9333");
}

// 开发期隔离 userData：用户自己的桌面端可能正开着，单实例锁和缓存会打架。
//   SF_USER_DATA=%TEMP%\sf-shell-dev SF_CORE_SOURCE=1 npm start
if (!app.isPackaged && process.env.SF_USER_DATA) {
  app.setPath("userData", path.resolve(process.env.SF_USER_DATA));
}

const REPO_ROOT = app.isPackaged
  ? process.resourcesPath
  : path.resolve(__dirname, "..", "..", "..");
const WINDOW_STATE = path.join(app.getPath("userData"), "window-state.json");
const SHELL_CONFIG = loadShellConfig(app.getPath("userData"));
// WCO：网页画标题栏，最小化/最大化/关闭仍由系统绘制（Snap Layouts 保留）。
const WCO_ENABLED = SHELL_CONFIG.titleBar === "wco" && process.platform === "win32";

function titlebarColors(theme) {
  return String(theme) === "light"
    ? { color: "#f4f4f6", symbolColor: "#1c1c21" }
    : { color: "#16181d", symbolColor: "#ededf1" };
}

function shellUrl(info) {
  const url = new URL(info.local_url);
  if (WCO_ENABLED) url.searchParams.set("shell", "wco");
  return url;
}
const ASSETS = path.join(__dirname, "..", "assets");
const ERROR_PAGE = path.join(__dirname, "..", "renderer", "error.html");
const DIAG_PAGE = path.join(__dirname, "..", "renderer", "diagnostics.html");

let core = null;
let win = null;
let tray = null;
let diagWin = null;
let appMenu = null;
let quitting = false;
let lastStatus = "idle";
let loadedOrigin = null;
let saveTimer = null;
let notifyTimer = null;
let notifyCursor = "";
let pendingTarget = null;
const notifySeen = new Set();
const STATUS_LABEL = { DONE: "完成", FAILED: "失败", STOPPED: "已停止" };

function readWindowState() {
  try {
    const saved = JSON.parse(fs.readFileSync(WINDOW_STATE, "utf8"));
    if (typeof saved.width === "number" && typeof saved.height === "number") return saved;
  } catch {
    /* 第一次启动没有文件 */
  }
  return { width: 1280, height: 860 };
}

function scheduleSaveWindowState() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveWindowState, 400);
}

function saveWindowState() {
  if (!win || win.isDestroyed()) return;
  try {
    const bounds = win.getNormalBounds();
    fs.mkdirSync(path.dirname(WINDOW_STATE), { recursive: true });
    fs.writeFileSync(WINDOW_STATE, JSON.stringify({ ...bounds, maximized: win.isMaximized() }), "utf8");
  } catch {
    /* 记不住窗口尺寸不影响使用 */
  }
}

function trayIcon() {
  const name = { ready: "tray-green.png", starting: "tray-yellow.png",
                 crashed: "tray-red.png", failed: "tray-red.png" }[lastStatus] || "tray-grey.png";
  const image = nativeImage.createFromPath(path.join(ASSETS, name));
  return image.isEmpty() ? nativeImage.createFromPath(path.join(ASSETS, "tray-grey.png")) : image;
}

function setStatus(status) {
  lastStatus = status;
  if (tray) {
    tray.setImage(trayIcon());
    const text = {
      ready: core && core.external ? "内核运行中（外部实例）" : "内核运行中",
      starting: "正在启动内核…",
      crashed: "内核异常退出，正在重启…",
      failed: "内核启动失败",
      stopped: "内核已停止",
      idle: "未启动",
    }[status] || status;
    tray.setToolTip(`synapforge studio · ${text}`);
  }
  if (status === "ready" && win && !win.isDestroyed()) {
    win.setTitle(core && core.external ? "synapforge studio（外部内核）" : "synapforge studio");
  }
}

function createTray() {
  tray = new Tray(trayIcon());
  tray.setToolTip("synapforge studio");
  tray.on("click", () => toggleWindow());
  tray.on("right-click", () => tray.popUpContextMenu(buildTrayMenu()));
  setStatus(lastStatus);
}

function buildTrayMenu() {
  return Menu.buildFromTemplate([
    { label: "打开工作台", click: () => showWindow() },
    { type: "separator" },
    {
      label: core && core.external ? "重启内核（外部实例，跳过）" : "重启内核",
      enabled: Boolean(core) && !core.external,
      click: () => restartCore(),
    },
    { label: "打开数据目录", click: () => openDataDir() },
    { label: "查看内核日志", click: () => openLog() },
    { type: "separator" },
    {
      label: "开机自启",
      type: "checkbox",
      checked: loginAtStartup(),
      click: (item) => setLoginAtStartup(item.checked),
    },
    { type: "separator" },
    { label: "诊断信息…", click: () => openDiagnostics() },
    { label: "检查更新…", click: () => void promptUpdate(win) },
    { label: "退出", click: () => app.quit() },
  ]);
}

function loginAtStartup() {
  try {
    return Boolean(app.getLoginItemSettings().openAtLogin);
  } catch {
    return false;
  }
}

function setLoginAtStartup(enabled) {
  try {
    app.setLoginItemSettings({ openAtLogin: Boolean(enabled), openAsHidden: true });
  } catch (error) {
    void dialog.showMessageBox({ type: "warning", message: "设置开机自启失败", detail: String(error) });
  }
}

function buildAppMenu() {
  const isMac = process.platform === "darwin";
  return Menu.buildFromTemplate([
    ...(isMac ? [{
      label: app.name,
      submenu: [
        { role: "about", label: "关于 synapforge studio" },
        { type: "separator" },
        { role: "hide", label: "隐藏" },
        { role: "hideOthers", label: "隐藏其他" },
        { role: "unhide", label: "全部显示" },
        { type: "separator" },
        { role: "quit", label: "退出" },
      ],
    }] : []),
    {
      label: "文件",
      submenu: [
        { label: "打开数据目录", click: () => openDataDir() },
        { label: "查看内核日志", click: () => openLog() },
        { type: "separator" },
        isMac ? { role: "close", label: "关闭窗口" } : { label: "退出", accelerator: "Ctrl+Q", click: () => app.quit() },
      ],
    },
    { role: "editMenu", label: "编辑" },
    {
      label: "视图",
      submenu: [
        { role: "reload", label: "刷新" },
        { role: "forceReload", label: "强制刷新" },
        ...(app.isPackaged ? [] : [{ role: "toggleDevTools", label: "开发者工具" }]),
        { type: "separator" },
        { role: "resetZoom", label: "实际大小" },
        { role: "zoomIn", label: "放大" },
        { role: "zoomOut", label: "缩小" },
        { type: "separator" },
        { role: "togglefullscreen", label: "全屏" },
      ],
    },
    { role: "windowMenu", label: "窗口" },
    {
      label: "帮助",
      submenu: [
        {
          label: "关于 synapforge studio",
          click: () => {
            const info = core && core.info;
            void dialog.showMessageBox({
              type: "info",
              title: "synapforge studio",
              message: `synapforge studio ${app.getVersion()}`,
              detail: [
                `内核：${info ? `127.0.0.1:${info.port}` : "未连接"}`,
                `数据目录：${core ? core.stateDir : "-"}`,
                `Electron：${process.versions.electron}`,
              ].join("\n"),
            });
          },
        },
        { label: "查看内核日志", click: () => openLog() },
        { label: "诊断信息…", click: () => openDiagnostics() },
        { type: "separator" },
        { label: "检查更新…", click: () => void promptUpdate(win) },
      ],
    },
  ]);
}

function toggleWindow() {
  if (!win || win.isDestroyed()) {
    createWindow();
    return;
  }
  // 全局快捷键要能"按一下收、再按一下回来"：只在已聚焦时收起，
  // 否则（被别的窗口盖住）先把它叫到前面。
  if (win.isVisible() && win.isFocused() && !win.isMinimized()) {
    win.hide();
  } else {
    showWindow();
  }
}

function showWindow() {
  if (!win || win.isDestroyed()) {
    createWindow();
    return;
  }
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
}

function openDataDir() {
  const target = core ? core.stateDir : app.getPath("userData");
  fs.mkdirSync(target, { recursive: true });
  void shell.openPath(target);
}

function openLog() {
  if (!core) return;
  fs.mkdirSync(path.dirname(core.logPath), { recursive: true });
  if (fs.existsSync(core.logPath)) {
    shell.showItemInFolder(core.logPath);
  } else {
    void shell.openPath(core.stateDir);
  }
}

function openDiagnostics() {
  if (diagWin && !diagWin.isDestroyed()) {
    diagWin.show();
    diagWin.focus();
    return;
  }
  diagWin = new BrowserWindow({
    width: 780,
    height: 640,
    minWidth: 560,
    minHeight: 420,
    parent: win && !win.isDestroyed() ? win : undefined,
    show: false,
    backgroundColor: "#0b0f14",
    title: "synapforge studio · 诊断",
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: false,
    },
  });
  diagWin.webContents.setWindowOpenHandler(({ url }) => {
    if (isHttp(url)) void shell.openExternal(url);
    return { action: "deny" };
  });
  diagWin.once("ready-to-show", () => diagWin.show());
  diagWin.on("closed", () => {
    diagWin = null;
  });
  void diagWin.loadFile(DIAG_PAGE);
}

function registerGlobalShortcut() {
  try {
    const ok = globalShortcut.register("CommandOrControl+Shift+Space", () => toggleWindow());
    if (!ok) console.log("[shortcut] Ctrl+Shift+Space 注册失败（可能被其他应用占用）");
  } catch (error) {
    console.log("[shortcut] 注册失败", String(error));
  }
}

function isHttp(url) {
  return /^https?:\/\//i.test(url);
}

function wireWindow(window) {
  const coreOrigin = core && core.info ? new URL(core.info.local_url).origin : null;

  window.webContents.setWindowOpenHandler(({ url }) => {
    if (isHttp(url)) void shell.openExternal(url);
    return { action: "deny" };
  });

  window.webContents.on("will-navigate", (event, url) => {
    const sameOrigin = coreOrigin && url.startsWith(coreOrigin);
    if (sameOrigin) return;
    event.preventDefault();
    if (isHttp(url)) void shell.openExternal(url);
  });

  window.webContents.on("did-fail-load", (_event, code, description, url, isMainFrame) => {
    if (!isMainFrame || code === -3) return; // -3 = 主动中断（重定向/刷新）
    void showError(`页面加载失败（${code} ${description}）\n${url}`);
  });

  window.on("close", (event) => {
    if (quitting) return;
    event.preventDefault();
    saveWindowState();
    window.hide();
  });

  window.on("ready-to-show", () => window.show());
  window.on("resize", scheduleSaveWindowState);
  window.on("move", scheduleSaveWindowState);
}

/** 记住的尺寸可能来自另一块屏/另一个缩放比例：夹到当前屏幕工作区内。 */
function clampWindowState(state) {
  const display = screen.getDisplayNearestPoint({
    x: Number.isFinite(state.x) ? state.x : 0,
    y: Number.isFinite(state.y) ? state.y : 0,
  }) || screen.getPrimaryDisplay();
  const area = display.workArea;
  const width = Math.min(Math.max(state.width || 1280, 960), area.width);
  const height = Math.min(Math.max(state.height || 860, 640), area.height);
  const x = Math.min(Math.max(state.x ?? area.x + 40, area.x), area.x + area.width - width);
  const y = Math.min(Math.max(state.y ?? area.y + 40, area.y), area.y + area.height - height);
  return { ...state, width, height, x, y };
}

function createWindow() {
  const state = clampWindowState(readWindowState());
  win = new BrowserWindow({
    width: state.width,
    height: state.height,
    x: state.x,
    y: state.y,
    minWidth: 960,
    minHeight: 640,
    show: false,
    backgroundColor: "#0b0f14",
    title: "synapforge studio",
    titleBarStyle: WCO_ENABLED ? "hidden" : "default",
    ...(WCO_ENABLED ? { titleBarOverlay: { ...titlebarColors("dark"), height: 40 } } : {}),
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: false,
    },
  });
  if (state.maximized) win.maximize();
  wireWindow(win);
  win.on("closed", () => {
    win = null;
  });
  return win;
}

async function loadWorkbench(info) {
  if (!win || win.isDestroyed()) createWindow();
  loadedOrigin = new URL(info.local_url).origin;
  await win.loadURL(shellUrl(info).toString());
}

async function showError(message) {
  if (!win || win.isDestroyed()) createWindow();
  const log = core ? await core.tailLog() : "";
  await win.loadFile(ERROR_PAGE, {
    query: { message: String(message || "").slice(0, 2000), log: log.slice(-8000) },
  });
  if (!win.isVisible()) win.show();
}

async function loadUrlWithTarget(target) {
  if (!core || !core.info || !win || win.isDestroyed()) return;
  const url = shellUrl(core.info);
  if (target.view) url.searchParams.set("view", target.view);
  if (target.cluster) url.searchParams.set("cluster", target.cluster);
  if (target.run) url.searchParams.set("run", target.run);
  loadedOrigin = url.origin;
  await win.loadURL(url.toString());
}

/** synapforge://cluster/<id> | run/<id> | view/<name> */
function parseDeepLink(value) {
  if (typeof value !== "string" || !value.toLowerCase().startsWith("synapforge://")) return null;
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    return null;
  }
  const kind = parsed.hostname.toLowerCase();
  const id = decodeURIComponent(parsed.pathname.replace(/^\/+/, "")).trim();
  if (kind === "cluster" && id) return { cluster: id };
  if (kind === "run" && id) return { run: id };
  if (kind === "view" && id) return { view: id };
  return null;
}

function deepLinkFromArgv(argv) {
  for (const arg of argv || []) {
    const target = parseDeepLink(arg);
    if (target) return target;
  }
  return null;
}

async function openTarget(target) {
  if (!target) return;
  showWindow();
  if (!win || win.isDestroyed()) return;
  try {
    // 页面已经在跑：直接调前端入口，不重新加载（保留当前未提交的输入）。
    const handled = await win.webContents.executeJavaScript(
      `window.sfOpen ? window.sfOpen(${JSON.stringify(target)}) : false`, true);
    if (handled) return;
  } catch {
    /* 页面还没加载好，退回 URL */
  }
  await loadUrlWithTarget(target);
}

/** 启动未完成时先存着，等内核和页面就绪再跳。 */
function queueTarget(target) {
  if (!target) return;
  const url = win && !win.isDestroyed() ? win.webContents.getURL() : "";
  if (core && core.status === "ready" && url.startsWith("http://127.0.0.1")) {
    void openTarget(target);
  } else {
    pendingTarget = target;
  }
}

function registerProtocol() {
  try {
    if (app.isPackaged) {
      app.setAsDefaultProtocolClient("synapforge");
    } else if (process.env.SF_REGISTER_PROTOCOL === "1") {
      app.setAsDefaultProtocolClient("synapforge", process.execPath,
        [path.resolve(process.argv[1] || ".")]);
    }
  } catch (error) {
    if (!app.isPackaged) console.log("[protocol] register failed", String(error));
  }
}

async function openNotificationTarget(item) {
  await openTarget(item.kind === "cluster" ? { cluster: item.id } : { run: item.id });
}

function showNativeNotification(item) {
  if (!Notification.isSupported()) return;
  const label = STATUS_LABEL[item.status] || item.status;
  const isCluster = item.kind === "cluster";
  const title = isCluster
    ? `集群「${item.title}」${label}`
    : `${item.agent_name || "Agent"} · 任务${label}`;
  const body = isCluster ? "点开查看任务与成果" : item.title;
  if (!app.isPackaged) console.log("[notify]", title, body);
  const note = new Notification({ title, body });
  note.on("click", () => {
    void openNotificationTarget(item);
  });
  note.show();
}

async function pollNotifications() {
  if (!core || core.status !== "ready") return;
  const payload = await core.notifications(notifyCursor);
  if (!payload) return;
  notifyCursor = payload.cursor || notifyCursor;
  for (const item of payload.items || []) {
    const key = `${item.kind}:${item.id}:${item.status}`;
    if (notifySeen.has(key)) continue;
    notifySeen.add(key);
    // 用户正盯着窗口时不再弹系统通知：页面自己的流式状态已经说明了结果。
    if (win && !win.isDestroyed() && win.isVisible() && win.isFocused()) continue;
    showNativeNotification(item);
  }
}

function startNotificationPolling() {
  if (notifyTimer) return;
  notifyTimer = setInterval(() => {
    void pollNotifications();
  }, 5000);
}

async function boot() {
  setStatus("starting");
  try {
    const info = await core.start();
    setStatus("ready");
    await loadWorkbench(info);
    if (pendingTarget) {
      const target = pendingTarget;
      pendingTarget = null;
      await openTarget(target);
    }
    return true;
  } catch (error) {
    setStatus("failed");
    await showError(error && error.message ? error.message : String(error));
    return false;
  }
}

async function restartCore() {
  setStatus("starting");
  try {
    const info = await core.restart();
    setStatus("ready");
    await loadWorkbench(info);
  } catch (error) {
    setStatus("failed");
    await showError(error && error.message ? error.message : String(error));
  }
}

function registerIpc() {
  ipcMain.handle("core:restart", async () => {
    await restartCore();
    return { status: core.status, external: core.external };
  });
  ipcMain.handle("core:status", () => ({
    status: core.status,
    external: core.external,
    error: core.lastError,
    port: core.info ? core.info.port : null,
    pid: core.info ? core.info.pid : null,
    version: core.version,
    stateDir: core.stateDir,
    logPath: core.logPath,
  }));
  ipcMain.handle("app:info", () => ({
    version: app.getVersion(),
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    platform: process.platform,
    shell: SHELL_CONFIG,
    safeMode: process.env.SF_SHELL_SAFE === "1",
  }));
  ipcMain.handle("app:autostart", () => loginAtStartup());
  ipcMain.handle("app:setAutostart", (_event, enabled) => {
    setLoginAtStartup(enabled);
    return loginAtStartup();
  });
  ipcMain.handle("dialog:pickDirectory", async (_event, options = {}) => {
    const settings = {
      title: String(options.title || "选择文件夹"),
      defaultPath: String(options.defaultPath || ""),
      properties: ["openDirectory", "createDirectory"],
    };
    const parent = win && !win.isDestroyed() ? win : null;
    const result = parent
      ? await dialog.showOpenDialog(parent, settings)
      : await dialog.showOpenDialog(settings);
    if (result.canceled || !result.filePaths.length) return "";
    return result.filePaths[0];
  });
  // 右键菜单：渲染进程只传 {id,label,enabled,type}，返回被点的 id 或 null。
  ipcMain.handle("menu:popup", (event, spec = {}) => new Promise((resolve) => {
    const items = (Array.isArray(spec.items) ? spec.items : [])
      .slice(0, 24)
      .filter((item) => item && (item.type === "separator" || item.id));
    if (!items.length) {
      resolve(null);
      return;
    }
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    const menu = Menu.buildFromTemplate(items.map((item) => (item.type === "separator"
      ? { type: "separator" }
      : {
        label: String(item.label || item.id),
        enabled: item.enabled !== false,
        click: () => finish(String(item.id)),
      })));
    const owner = BrowserWindow.fromWebContents(event.sender) || win || undefined;
    menu.popup({ window: owner, callback: () => finish(null) });
  }));

  ipcMain.handle("shell:openPath", async (_event, target) => {
    const value = String(target || "").trim();
    if (!value) return { ok: false, error: "empty_path" };
    const error = await shell.openPath(value);
    return error ? { ok: false, error } : { ok: true };
  });

  ipcMain.handle("clipboard:write", (_event, text) => {
    clipboard.writeText(String(text ?? ""));
    return true;
  });

  // WCO 下菜单栏不显示：标题栏里的「⋯」重新打开应用菜单。
  ipcMain.handle("menu:appPopup", () => {
    if (!appMenu || !win || win.isDestroyed()) return false;
    appMenu.popup({ window: win });
    return true;
  });

  ipcMain.handle("shell:windowInfo", () => {
    if (!win || win.isDestroyed()) return null;
    return {
      wco: WCO_ENABLED,
      visible: win.isVisible(),
      focused: win.isFocused(),
      bounds: win.getBounds(),
      content: win.getContentBounds(),
      menuVisible: win.isMenuBarVisible(),
    };
  });

  ipcMain.handle("shell:titlebarTheme", (_event, theme) => {
    if (!WCO_ENABLED || !win || win.isDestroyed()) return false;
    win.setTitleBarOverlay({ ...titlebarColors(String(theme)), height: 40 });
    return true;
  });

  ipcMain.handle("app:openDataDir", () => openDataDir());
  ipcMain.handle("app:diagnostics", () => openDiagnostics());
  ipcMain.handle("app:quit", () => app.quit());
  ipcMain.handle("log:tail", () => core.tailLog());
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", (_event, argv) => {
    const target = deepLinkFromArgv(argv);
    if (target) void queueTarget(target);
    else showWindow();
  });

  // macOS 的深链不走 argv，走 open-url（可能早于 ready）。
  app.on("open-url", (event, url) => {
    event.preventDefault();
    const target = parseDeepLink(url);
    if (target) queueTarget(target);
  });

  app.whenReady().then(async () => {
    app.setAppUserModelId("top.synapforge.studio");
    if (!app.isPackaged) console.log("[shell] config", JSON.stringify(SHELL_CONFIG));
    registerProtocol();
    pendingTarget = deepLinkFromArgv(process.argv) || pendingTarget;
    core = new StudioCore({
      repoRoot: REPO_ROOT,
      packaged: app.isPackaged,
      onState: (state) => {
        setStatus(state.status);
        // 内核崩溃重启后端口和 token 都会变；窗口要跟着换到新地址，
        // 否则页面还连着已经死掉的端口（SSE 全断，看起来像卡住）。
        if (state.status === "ready" && state.info && win && !win.isDestroyed() && loadedOrigin) {
          const origin = new URL(state.info.local_url).origin;
          if (origin !== loadedOrigin) void loadWorkbench(state.info);
        }
      },
      onLog: () => {},
    });

    registerIpc();
    appMenu = buildAppMenu();
    Menu.setApplicationMenu(appMenu);
    createTray();
    registerGlobalShortcut();
    createWindow();
    startNotificationPolling();
    await boot();

    app.on("activate", () => showWindow());
  });

  app.on("before-quit", (event) => {
    if (quitting) return;
    event.preventDefault();
    quitting = true;
    if (notifyTimer) {
      clearInterval(notifyTimer);
      notifyTimer = null;
    }
    globalShortcut.unregisterAll();
    saveWindowState();
    const done = () => app.exit(0);
    if (!core) {
      done();
      return;
    }
    core.stop().then(done).catch(done);
  });

  // 托盘常驻：关掉窗口不等于退出（Windows/Linux）。
  app.on("window-all-closed", () => {
    if (quitting) app.quit();
  });
}
