"use strict";

// 只暴露"壳能力"，不暴露 Node。页面里的业务仍然走本地 HTTP API。
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("synapforgeStudio", {
  isDesktop: true,
  platform: process.platform,
  restartCore: () => ipcRenderer.invoke("core:restart"),
  status: () => ipcRenderer.invoke("core:status"),
  openDataDir: () => ipcRenderer.invoke("app:openDataDir"),
  quit: () => ipcRenderer.invoke("app:quit"),
  tailLog: () => ipcRenderer.invoke("log:tail"),
});
