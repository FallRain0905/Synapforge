// 预加载：把壳的能力以最小面暴露给渲染进程（contextIsolation=true）
//
// 两个消费方：
//   1) 本地页（setup.html / offline.html）：配对、设置、日志、内核动作；
//   2) 平台工作台（服务器页面）：只读状态 + 通知 + 打开外链 + 版本/更新 —— 供「本机 Agent」面板用。
// 注意：桌面端**不**把设备令牌交给渲染进程；配对与授权都在内核里完成（凭据留在 Windows 凭据管理器）。
const { contextBridge, ipcRenderer } = require("electron");

const bridge = {
  // 只读状态与设置
  snapshot: () => ipcRenderer.invoke("snapshot"),
  config: () => ipcRenderer.invoke("config"),
  saveConfig: (patch) => ipcRenderer.invoke("save-config", patch),
  version: () => ipcRenderer.invoke("config").then((value) => value.app_version),
  // 内核动作（本地页与工作台面板共用）
  pair: (platformUrl, pairingBlob) => ipcRenderer.invoke("pair", { platformUrl, pairingBlob }),
  grant: (platformUrl, grantBlob) => ipcRenderer.invoke("grant", { platformUrl, grantBlob }),
  rescan: () => ipcRenderer.invoke("rescan"),
  togglePause: () => ipcRenderer.invoke("toggle-pause"),
  toggleEmergency: () => ipcRenderer.invoke("toggle-emergency"),
  logs: () => ipcRenderer.invoke("logs"),
  // 窗口与系统
  openWorkbench: () => ipcRenderer.invoke("open-workbench"),
  reloadWorkbench: () => ipcRenderer.invoke("reload-workbench"),
  openSetup: () => ipcRenderer.invoke("open-setup"),
  openExternal: (url) => ipcRenderer.invoke("open-external", url),
  // 通知：`notify("标题", "正文")` 或 `notify({ title, body, url })`（url 为站内路径，点击直达）
  notify: (title, body) =>
    ipcRenderer.invoke(
      "notify",
      typeof title === "object" && title !== null ? title : { title, body },
    ),
  // 关注清单（DESKTOP-NOTIFY）：前端登录后交会话上下文；壳自己轮询并发系统通知
  setAttentionContext: (context) => ipcRenderer.invoke("set-attention-context", context),
  attentionStatus: () => ipcRenderer.invoke("attention-status"),
  pollAttention: () => ipcRenderer.invoke("poll-attention"),
  checkUpdate: () => ipcRenderer.invoke("check-update"),
  setAutoLaunch: (enabled) => ipcRenderer.invoke("set-auto-launch", enabled),
  // 事件
  onStatus: (handler) => ipcRenderer.on("status", (_event, payload) => handler(payload)),
  onLog: (handler) => ipcRenderer.on("log", (_event, line) => handler(line)),
  onOpenKernelPanel: (handler) => ipcRenderer.on("open-kernel-panel", () => handler()),
};

// 新名字（工作台用它判断"我在桌面端里"）；旧名字 `shell` 保留，避免既有本地页失效
contextBridge.exposeInMainWorld("synapforgeShell", bridge);
contextBridge.exposeInMainWorld("shell", bridge);