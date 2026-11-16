"use strict";

// 只暴露"壳能力"，不暴露 Node。页面里的业务仍然走本地 HTTP API。
const { contextBridge, ipcRenderer, webUtils } = require("electron");

contextBridge.exposeInMainWorld("synapforgeStudio", {
  isDesktop: true,
  platform: process.platform,
  restartCore: () => ipcRenderer.invoke("core:restart"),
  status: () => ipcRenderer.invoke("core:status"),
  autostart: () => ipcRenderer.invoke("app:autostart"),
  setAutostart: (enabled) => ipcRenderer.invoke("app:setAutostart", Boolean(enabled)),
  openDataDir: () => ipcRenderer.invoke("app:openDataDir"),
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
