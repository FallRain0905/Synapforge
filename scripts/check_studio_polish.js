// 桌面端美化阶段的静态契约：关键样式令牌 + 壳特性开关。
// 用法：node scripts/check_studio_polish.js
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const ROOT = path.join(__dirname, "..");
const HTML = fs.readFileSync(
  path.join(ROOT, "apps", "local", "synapforge", "studio", "static", "workbench.html"),
  "utf8",
);
const { DEFAULTS, loadShellConfig } = require(
  path.join(ROOT, "apps", "studio-desktop", "src", "shell-config"),
);

let pass = 0;
function ok(name) {
  pass += 1;
  console.log(`ok ${pass} - ${name}`);
}
function fail(name, detail) {
  console.error(`FAIL - ${name}: ${detail}`);
  process.exitCode = 1;
}
function check(name, condition, detail = "断言不成立") {
  if (condition) ok(name);
  else fail(name, detail);
}

// ---- 页面令牌与桌面态入口 ----
check("desktop-shell 启动标记", HTML.includes('classList.add("desktop-shell")'));
check("标题栏高度令牌", HTML.includes("--titlebar-h:40px"));
check("焦点环令牌", HTML.includes("--focus-ring"));
check("半透明表面令牌", HTML.includes("--surface-glass"));
check("窗口阴影令牌", HTML.includes("--window-shadow"));
check("命令面板挂载点", HTML.includes('id="commandPalette"') && HTML.includes('id="cmdkInput"'));
check("命令面板入口", HTML.includes("window.sfCommandPalette"));
check("WCO 标题栏挂载点", HTML.includes('id="sfTitlebar"'));

// ---- shell.json 开关 ----
const dir = fs.mkdtempSync(path.join(os.tmpdir(), "sf-shell-"));
const defaults = loadShellConfig(dir);
check("没有配置时用默认值", JSON.stringify(defaults) === JSON.stringify(DEFAULTS),
  JSON.stringify(defaults));

fs.writeFileSync(path.join(dir, "shell.json"),
  JSON.stringify({ titleBar: "system", mica: "on", quickPanel: false, taskbar: false, contextMenu: false }),
  "utf8");
const custom = loadShellConfig(dir);
check("合法配置生效",
  custom.titleBar === "system" && custom.mica === "on"
  && custom.quickPanel === false && custom.taskbar === false && custom.contextMenu === false,
  JSON.stringify(custom));

fs.writeFileSync(path.join(dir, "shell.json"),
  JSON.stringify({ titleBar: "wat", mica: "neon", quickPanel: "no" }), "utf8");
const sanitized = loadShellConfig(dir);
check("非法值回落默认",
  sanitized.titleBar === "wco" && sanitized.mica === "auto" && sanitized.quickPanel === true,
  JSON.stringify(sanitized));

process.env.SF_SHELL_SAFE = "1";
const safe = loadShellConfig(dir);
check("安全模式全回落",
  safe.titleBar === "system" && safe.mica === "off"
  && safe.quickPanel === false && safe.taskbar === false,
  JSON.stringify(safe));
delete process.env.SF_SHELL_SAFE;

if (!process.exitCode) console.log(`OK ${pass} 项全部通过`);
