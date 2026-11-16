"use strict";

// 只暴露"壳能力"，不暴露 Node。页面里的业务仍然走本地 HTTP API。
const { contextBridge, ipcRenderer, webUtils } = require("electron");

contextBridge.exposeInMainWorld("synapforgeStudio", {
  isDesktop: true,
  platform: process.platform,
  restartCore: () => ipcRenderer.invoke("core:restart"),
  status: () => ipcRenderer.invoke("core:status"),
  info: () => ipcRenderer.invoke("app:info"),
  autostart: () => ipcRenderer.invoke("app:autostart"),
  setAutostart: (enabled) => ipcRenderer.invoke("app:setAutostart", Boolean(enabled)),
  pickDirectory: (options) => ipcRenderer.invoke("dialog:pickDirectory", options),
  contextMenu: (spec) => ipcRenderer.invoke("menu:popup", spec),
  openPath: (target) => ipcRenderer.invoke("shell:openPath", target),
  copyText: (text) => ipcRenderer.invoke("clipboard:write", text),
  setTitlebarTheme: (theme) => ipcRenderer.invoke("shell:titlebarTheme", theme),
  windowInfo: () => ipcRenderer.invoke("shell:windowInfo"),
  appMenu: () => ipcRenderer.invoke("menu:appPopup"),
  taskbarState: (state) => ipcRenderer.invoke("taskbar:state", state),
  setRecentClusters: (clusters) => ipcRenderer.invoke("taskbar:recentClusters", clusters),
  panelHide: () => ipcRenderer.invoke("panel:hide"),
  panelBootstrap: () => ipcRenderer.invoke("panel:bootstrap"),
  panelTask: (payload) => ipcRenderer.invoke("panel:task", payload),
  panelOpen: (target) => ipcRenderer.invoke("panel:open", target),
  openDataDir: () => ipcRenderer.invoke("app:openDataDir"),
  openDiagnostics: () => ipcRenderer.invoke("app:diagnostics"),
  checkUpdate: () => ipcRenderer.invoke("app:checkUpdate"),
  quit: () => ipcRenderer.invoke("app:quit"),
  tailLog: () => ipcRenderer.invoke("log:tail"),
  // 拖进窗口的文件：Electron 32+ 删掉了 File.path，只能走 webUtils。
  pathForFile: (file) => {
    try {
      return webUtils.getPathForFile(file);
    } catch {
      return "";
    }
  },
});
