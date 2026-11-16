"use strict";

// 更新检查：读 latest.json → 比版本 → 打开下载页。
// 不做静默下载/自动安装（安装包未签名，交给用户确认更稳）。

const { app, dialog, shell } = require("electron");
const { fetchFeed } = require("./feed");
const { isNewer } = require("./version");

const DEFAULT_FEED = "https://synapforge.top/downloads/studio/latest.json";

function feedUrl() {
  return process.env.STUDIO_UPDATE_FEED || DEFAULT_FEED;
}

async function checkForUpdate() {
  const payload = await fetchFeed(feedUrl());
  return {
    latest: payload.version,
    url: payload.url,
    notes: payload.notes,
    newer: isNewer(payload.version, app.getVersion()),
  };
}

async function promptUpdate(parent = null) {
  try {
    const info = await checkForUpdate();
    if (!info.newer) {
      await dialog.showMessageBox(parent, {
        type: "info",
        title: "检查更新",
        message: "已是最新版本",
        detail: `当前 ${app.getVersion()}`,
      });
      return { ok: true, updated: false, latest: info.latest };
    }
    const detail = [
      `当前版本：${app.getVersion()}`,
      info.notes ? `更新内容：\n${info.notes}` : "",
    ].filter(Boolean).join("\n\n");
    const { response } = await dialog.showMessageBox(parent, {
      type: "info",
      title: "发现新版本",
      message: `synapforge studio ${info.latest}`,
      detail,
      buttons: ["下载", "稍后"],
      defaultId: 0,
      cancelId: 1,
      noLink: true,
    });
    if (response === 0 && info.url) await shell.openExternal(info.url);
    return { ok: true, updated: true, latest: info.latest };
  } catch (error) {
    await dialog.showMessageBox(parent, {
      type: "warning",
      title: "检查更新失败",
      message: "暂时连不上更新源",
      detail: `${feedUrl()}\n${String(error && error.message ? error.message : error)}`,
    });
    return { ok: false, error: String(error) };
  }
}

module.exports = { checkForUpdate, promptUpdate, feedUrl };
