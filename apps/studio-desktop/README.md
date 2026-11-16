# synapforge studio 桌面壳

Electron 壳 + 本地 Python 内核。壳只负责窗口、托盘、菜单、单实例和内核生命周期；
UI 与全部业务仍由内核在 `127.0.0.1:<随机端口>` 上提供（同源，SSE/绝对路径都不用改）。

完整计划见 [`docs/STUDIO_DESKTOP_PLAN.md`](../../docs/STUDIO_DESKTOP_PLAN.md)。

## 开发

```powershell
# 1) 打内核（ONEDIR，约 45MB；含 synapforge-core.exe 与 synapforge-mcp.exe）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build-studio-core.ps1

# 2) 安装壳依赖（首次）
cd apps\studio-desktop
$env:ELECTRON_MIRROR = "https://npmmirror.com/mirrors/electron/"
npm install

# 3) 启动
npm start

# 4) 出安装包（NSIS，每用户安装，免管理员）
powershell -NoProfile -ExecutionPolicy Bypass -File ..\..\scripts\build-studio-desktop.ps1
# 产物：dist\synapforge-studio-setup-<version>-x64.exe（约 136MB）
#       dist\win-unpacked\（免安装版，验收用）
```

没有打包内核时，壳会退回源码模式：用 `SF_PYTHON`（或 `%LOCALAPPDATA%\Programs\Python\Python312\python.exe`）
跑 `scripts/synapforge_studio.py`，方便改内核代码即时生效。

开发模式会自动开 `127.0.0.1:9333` 的 CDP 端口，便于查页面地址、抓截图；
打包后默认不开，排查现场时用 `SF_DEBUG_PORT=9333` 临时打开。

## 发布与更新

1. `scripts\build-studio-desktop.ps1` 产出安装包与 `win-unpacked`
2. 把安装包和 `latest.json` 放到 `https://synapforge.top/downloads/studio/`
   （格式见 `latest.example.json`；地址可用 `STUDIO_UPDATE_FEED` 覆盖）
3. 客户端「帮助 → 检查更新…」读 `latest.json`，版本更高时打开下载页

安装包未签名（无 Authenticode 证书），首次安装会有 SmartScreen 提示，属预期。
卸载器不会删除 `HKCU\Software\Classes\synapforge`（协议注册），需要时手动清：
`reg delete HKCU\Software\Classes\synapforge /f`。

## 目录

| 文件 | 职责 |
| --- | --- |
| `src/main.js` | 单实例、窗口、托盘、菜单、IPC、退出清理 |
| `src/core.js` | 解析/拉起内核、等 `studio.json`、健康检查、崩溃重启 |
| `src/preload.js` | 只暴露壳能力（重启、状态、打开数据目录、日志），不暴露 Node |
| `renderer/error.html` | 内核起不来时的错误页（可重试、看日志） |

## 内核契约（壳只依赖这四个）

1. 启动参数：`--no-browser --port 0 --state <db> --log-file <log> --parent-pid <pid>`
2. 握手文件：`<state_dir>/studio.json`，`contract = "studio-v1"`，带 `port/token/pid`
3. `GET /api/studio/health`（需 `X-Studio-Token`）
4. `POST /api/studio/shutdown`（需 token；优雅退出，内核自己收尾）

内核还有两层兜底：`studio.lock` 单实例锁（第二个内核 exit 3），以及 `--parent-pid`
看门狗（壳退出后内核跟随，避免孤儿进程占端口）。

## 已验收（2026-11-16）

- 双击/启动后 1s 内出窗口；无控制台（PE subsystem = GUI）
- 关闭窗口 → 托盘常驻；`Ctrl+Q` → 内核优雅退出、`studio.json` 删除
- 第二次启动 → 聚焦已有窗口，不起第二个内核
- `kill` 内核 → 自动重启并让窗口跟到新端口/新 token
- `kill` 壳 → 内核跟随退出（Windows 下 Electron 的 job 对象会直接带走子进程）
- 内核 45.3MB；`synapforge-mcp.exe` 17 个 MCP 工具、UTF-8 stdio 正常

### P2 桌面能力（已验收）

- **通知**：内核 5s 轮询终态 run/集群；窗口失焦时弹系统通知，点击直接跳到运行详情/集群任务
- **开机自启**：托盘勾选（`app.setLoginItemSettings`），开关状态回读一致
- **深链**：`synapforge://cluster/<uuid>`、`synapforge://run/<uuid>`、`synapforge://view/<name>`
  - 打包后自动注册；开发期用 `SF_REGISTER_PROTOCOL=1 npm start` 注册到本机 electron
  - 第二次启动会把 URL 转发给已有实例（单实例 argv 转发）
- **拖拽**：文件拖进窗口 → 绝对路径插入当前对话（含空格的路径自动加引号），
  走 Electron 32+ 的 `webUtils.getPathForFile`（`File.path` 已被移除）

### P4 部分（已验收）

- **全局快捷键**：`Ctrl+Shift+Space` 唤起/收起主窗（已聚焦再按时收起）
- **诊断页**：托盘或「帮助 → 诊断信息…」；展示壳/内核版本、端口、PID、数据目录、日志尾部，
  可重启内核；每 2s 自动刷新
- 未做：Win11 Mica/自绘标题栏（需要整页透明化，风险大于收益）、独立快速面板（当前快捷键
  直接唤起主窗）
