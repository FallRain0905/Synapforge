"use strict";

// 桌面壳的视觉特性开关。放在 userData/shell.json：
//   {"titleBar":"wco","mica":"auto","quickPanel":true,"taskbar":true,"contextMenu":true}
// SF_SHELL_SAFE=1 时全部回落到"系统标题栏 + 纯色 + 无面板"的保守形态。

const fs = require("node:fs");
const path = require("node:path");

const DEFAULTS = {
  titleBar: "wco",      // wco | system
  mica: "auto",         // auto | on | off
  quickPanel: true,
  taskbar: true,
  contextMenu: true,
};

function loadShellConfig(userData) {
  if (process.env.SF_SHELL_SAFE === "1") {
    return { ...DEFAULTS, titleBar: "system", mica: "off", quickPanel: false, taskbar: false };
  }
  let raw = {};
  try {
    raw = JSON.parse(fs.readFileSync(path.join(userData, "shell.json"), "utf8"));
  } catch {
    raw = {}; // 没有配置文件是最常见的情况
  }
  const config = { ...DEFAULTS };
  if (raw.titleBar === "wco" || raw.titleBar === "system") config.titleBar = raw.titleBar;
  if (raw.mica === "auto" || raw.mica === "on" || raw.mica === "off") config.mica = raw.mica;
  for (const key of ["quickPanel", "taskbar", "contextMenu"]) {
    if (typeof raw[key] === "boolean") config[key] = raw[key];
  }
  return config;
}

module.exports = { DEFAULTS, loadShellConfig };
