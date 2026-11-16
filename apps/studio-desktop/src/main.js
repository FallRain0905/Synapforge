"use strict";

// synapforge studio 桌面壳：单实例、托盘常驻、内核生命周期、窗口与菜单。
// 业务一切都在本地内核里；壳只负责"让一个本地网页活得像桌面应用"。

const { app, BrowserWindow, Menu, Tray, dialog, ipcMain, nativeImage, shell } = require("electron");
const fs = require("node:fs");
const path = require("node:path");

const { StudioCore } = require("./core");

// 开发期开一个本地调试端口：用 CDP 查页面地址、抓截图、看控制台错误。
// 打包后不生效（调试端口对用户没有价值，还会多开一个监听）。
if (!app.isPackaged) {
  app.commandLine.appendSwitch("remote-debugging-port", process.env.SF_DEBUG_PORT || "9333");
}

const REPO_ROOT = app.isPackaged
  ? process.resourcesPath
  : path.resolve(__dirname, "..", "..", "..");
const WINDOW_STATE = path.join(app.getPath("userData"), "window-state.json");
const ASSETS = path.join(__dirname, "..", "assets");
const ERROR_PAGE = path.join(__dirname, "..", "renderer", "error.html");

let core = null;
let win = null;
let tray = null;
let quitting = false;
let lastStatus = "idle";
let loadedOrigin = null;
let saveTimer = null;

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
    { label: "退出", click: () => app.quit() },
  ]);
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
      ],
    },
  ]);
}

function toggleWindow() {
  if (!win || win.isDestroyed()) {
    createWindow();
    return;
  }
  if (win.isVisible() && !win.isMinimized()) {
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

function createWindow() {
  const state = readWindowState();
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
  await win.loadURL(info.local_url);
}

async function showError(message) {
  if (!win || win.isDestroyed()) createWindow();
  const log = core ? await core.tailLog() : "";
  await win.loadFile(ERROR_PAGE, {
    query: { message: String(message || "").slice(0, 2000), log: log.slice(-8000) },
  });
  if (!win.isVisible()) win.show();
}

async function boot() {
  setStatus("starting");
  try {
    const info = await core.start();
    setStatus("ready");
    await loadWorkbench(info);
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
    stateDir: core.stateDir,
  }));
  ipcMain.handle("app:openDataDir", () => openDataDir());
  ipcMain.handle("app:quit", () => app.quit());
  ipcMain.handle("log:tail", () => core.tailLog());
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => showWindow());

  app.whenReady().then(async () => {
    app.setAppUserModelId("top.synapforge.studio");
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
    Menu.setApplicationMenu(buildAppMenu());
    createTray();
    createWindow();
    await boot();

    app.on("activate", () => showWindow());
  });

  app.on("before-quit", (event) => {
    if (quitting) return;
    event.preventDefault();
    quitting = true;
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
