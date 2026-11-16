# studio 原生桌面端计划（STUDIO-DESKTOP）

> 2026-11-16 · 状态：**P0–P3 已验收，P4 部分交付（Mica/独立快速面板未做）**
> 范围：把 `apps/local` 的 studio（本地 Agent 工具）做成双击即用的桌面应用。
> 不是云端工作台客户端（那是 `apps/desktop` 0.2.1，另一条产品线）。

## 0 结论先说

**Electron 壳 + Python 内核（PyInstaller sidecar）**，壳与内核进程分离，UI 仍由内核同源提供。

理由（按权重）：

1. **内核是 Python**。Wails/Tauri 只解决“壳”，换不来内核收益；为薄壳引入 Go/Rust 工具链不划算。
2. **本仓已有生产级 Electron 外壳**（`apps/desktop/src/main.js`，976 行）：单实例、托盘、通知、自启、深链、更新检查、sidecar 守护、窗口记忆、离线页全部踩过坑。
3. **Electron 44 支持 Win11 Mica**，观感不打折；`backgroundMaterial: "mica"` 即可。
4. 现有云端 sidecar 是 87MB、安装包 138MB，说明**体积由 Python 内核主导**。换成 Tauri 也只能到 ~100MB，省不了一个数量级，却要多装 ~6GB 工具链、把壳重写一遍。

明确不选：

| 方案 | 不选的原因 |
| --- | --- |
| Tauri v2 | 唯一优点是 ~60MB 体积和 ~100MB 内存；代价是 Rust + MSVC 工具链、壳重写、工期 15–20 天 |
| Wails v3 | 为 Python 内核引入 Go 不产生收益；v3 仍是 beta |
| pywebview | 单进程最省事，但托盘/更新/深链/多窗口全要自己拼，做不到“完整” |
| 原生控件重写 UI | magpie、DeepSeek Harness 都没这么干；重写 5600 行 UI 是负收益 |

## 1 形态与进程模型

```
synapforge studio.exe          Electron 壳（新 app：apps/studio-desktop）
├─ 托盘 / 菜单 / 快捷键 / 单实例 / 深链 / 通知 / 自启 / 更新
└─ BrowserWindow ──► http://127.0.0.1:<随机端口>/?token=<一次性>
                     │
                     └─ core/                PyInstaller ONEDIR
                        └─ python 进程：ThreadingHTTPServer（UI + API + SSE 同源）
```

三条硬约束：

1. **UI 由内核提供**，不把 HTML 打进壳。否则绝对路径 `/api/studio/*`、SSE、token 全要改，还会出现“壳版本 ≠ UI 版本”。
2. **壳只做生命周期与桌面能力**，不含业务逻辑；将来换壳，内核与 UI 一行不动。
3. **只绑 127.0.0.1**（不绑 0.0.0.0），避免 Windows 防火墙弹窗与局域网暴露。

### 1.1 启动握手

内核启动后写 `studio.json`（已有实现，`studio/server.py:1309-1333`）：

```json
{"contract":"studio-v1","port":51234,"url":"...","local_url":"...",
 "token":"...","host":"127.0.0.1","base_path":"","pid":12345,
 "state_path":"C:/Users/x/.synapforge/state.db","started_at":"..."}
```

壳的时序：

1. 删除残留 `studio.json`（上一次崩溃留下的）
2. spawn `core/synapforge-core.exe --no-browser --log-file <log>`
3. 轮询 `studio.json`，直到 `contract == studio-v1` 且 `pid == 子进程 pid`（≤10s）
4. `GET /api/studio/health`（带 token）→ `{ok:true}`
5. `BrowserWindow.loadURL(local_url)`；首次加载失败进错误页（可看日志、重启内核）

### 1.2 退出与孤儿进程

- 关窗 → 隐藏到托盘（可配置）；`Cmd/Ctrl+Q`、托盘「退出」→ 先 `POST /api/studio/shutdown`，3s 未退再 `kill`
- 壳崩溃时：内核侧 `--parent-pid` 看门狗（P1）每 2s 检查父进程，父进程消失即退出
- 内核侧再加 **实例锁**（`state_dir/studio.lock`），同一 state 目录只允许一个内核；第二个实例拿不到锁就退出（exit 3），壳改为连已有实例

### 1.3 安全边界

| 面 | 现状 | 桌面端补充 |
| --- | --- | --- |
| 监听 | 127.0.0.1 随机端口 | 不变 |
| 鉴权 | 启动生成 token，`X-Studio-Token` / `?token=` | 壳每次启动前删 `studio.json`，强制新 token |
| Origin | 只认回环 + 显式白名单 | 不变 |
| Host | 未校验 | 新增：本地形态只接受 `127.0.0.1:<port>` / `localhost:<port>`；反代形态按 `public_url`/`allowed_origins` 放行 |
| 引用 | — | 静态页加 `Referrer-Policy: no-referrer` |

## 2 P0 · 内核桌面化（1–1.5 天）

目标：**双击 core.exe 无黑窗，能起、能停、能被壳接管。**

### 2.1 接口契约

| 变更 | 说明 |
| --- | --- |
| `studio --log-file PATH` | 追加写、UTF-8、启动时按 5MB 轮转一次（`.1`） |
| stdio 兜底 | `--noconsole` 下 `sys.stdout is None`，`print()` 会抛错；启动即把 stdout/stderr 重定向到日志（未给 `--log-file` 时写 `state_dir/logs/studio.log`） |
| `GET /api/studio/health` | 需 token；返回 `{ok, version, pid, uptime_s, port, contract:"studio-v1"}` |
| `POST /api/studio/shutdown` | 需 token + Origin；异步 `httpd.shutdown()`，返回 `{ok:true}`，让 `serve()` 的 finally 正常清理 supervisor/agent |
| 实例锁 | `serve()` 内 `studio.lock` 非阻塞独占；失败打印已有实例并 exit 3。`build_server()` 不加锁（测试不受影响） |
| `studio.json` | 补 `started_at`；写入用 tmp + `os.replace`，避免壳读到半截文件 |
| `Host` 校验 | 仅对 `/api/studio/*`；本地形态白名单回环，反代形态按配置放行 |

### 2.2 打包

`scripts/build-studio-core.ps1` + `scripts/studio-core.spec`：

- entry：`apps/local/studio_entry.py`，按 exe 名分流
- 两个 exe：`synapforge-core.exe`（GUI，跑控制台）+ `synapforge-mcp.exe`（控制台，stdio MCP）
  - **GUI 子系统没有 stdout**，不能当 MCP 的 stdio 传输，必须分一个控制台 exe
- `--onedir`；`pathex`：仓库根、`apps/local`、`apps/api`、**`apps/agent`**
  （`receipts` 等复用模块在运行时才挂路径，构建期必须显式给）
- `datas`：`apps/local/synapforge/studio/static/*` → `synapforge/studio/static/`
- `excludes`：pydantic 在 `if TYPE_CHECKING` 下 import `rich`，本机装了 IPython/
  matplotlib/pandas/scipy/torch/PySide6，不排除会打 **699MB**；再加上 boto3/email_validator
  等可选链，最终 45.3MB
- 产物：`dist-studio-core/synapforge-core/synapforge-core.exe` + `synapforge-mcp.exe`

### 2.3 验收

- [ ] 双击 exe：无控制台窗口，`studio.json` 10s 内出现且 pid 正确
- [ ] `curl -H "X-Studio-Token: ..." /api/studio/health` 返回 200
- [ ] `POST /api/studio/shutdown` 后进程 3s 内退出，端口释放，`studio.json` 删除
- [ ] 再启动一次：新 token（不与上次相同）
- [ ] 两个实例同时起：第二个 exit 3，不写坏 `studio.json`
- [ ] `Host: evil.com` 调 API → 403 `host_rejected`
- [ ] 现有 212 项测试全绿；新增用例覆盖上面 4 条

## 3 P1 · 外壳 MVP（2–3 天）

新建 `apps/studio-desktop`（Electron 44 + electron-builder，复用 `apps/desktop` 的**模式**，不复制业务）。

| 模块 | 内容 |
| --- | --- |
| `src/core.js` | 内核生命周期：解析路径（dev = `python scripts/synapforge_studio.py`；packaged = `resources/core/...`）、spawn、握手轮询、健康检查、日志尾部、异常重启 3 次 |
| `src/main.js` | 主窗 1280×860（最小 960×640）、尺寸/位置记忆、托盘、菜单、单实例、IPC、退出清理、通知/深链/拖拽编排 |
| `src/preload.js` | 只暴露壳能力（重启、状态、自启、打开数据目录、日志、pathForFile） |
| `renderer/error.html` | 内核起不来时的错误页（重试 / 看日志 / 打开数据目录） |

验收（全部实测通过）：

- [x] 冷启动 1s 级出窗口；托盘颜色与真实状态一致
- [x] 第二次双击聚焦已有窗口，不起第二个内核
- [x] 关窗后托盘仍在，点托盘恢复；退出后无 python 残留、端口释放
- [x] `kill core.exe` 后壳自动重启 3 次，仍失败进错误页（可看日志、可手动重启）

## 4 P2 · 桌面能力（2–3 天）

- [x] **通知**：内核 `GET /api/studio/notifications?since=` 游标轮询；壳 5s 一次，
  窗口失焦时弹系统通知，点击调用页面 `sfOpen` 直达（不重载、不丢未提交输入）
- [x] **自启**：托盘勾选 + `app.setLoginItemSettings`，另有 IPC 供设置页复用
- [x] **深链**：`synapforge://cluster/<id>`、`run/<id>`、`view/<name>`；
  `second-instance` 转发 argv、macOS `open-url`；前端 `?view=&cluster=&run=` 启动解析
- [x] **文件**：拖文件进窗口 → 绝对路径插入当前对话（空格自动加引号）；
  `webUtils.getPathForFile`（Electron 32+ 已移除 `File.path`）
- [x] **外链**：一律 `shell.openExternal`

验收已完成：窗口隐藏时完成 run 能弹通知并点开运行详情；`synapforge://view/clustertasks`、
`run/<uuid>` 从资源管理器唤起可落到对应页；带空格的文件拖进 `#planInput` 后得到带引号的绝对路径。

## 5 P3 · 打包与更新（2–3 天）

- [x] NSIS **每用户安装**（免管理员）、`win-unpacked`、图标/版本/关于页
- [x] 更新：`latest.json` + 打开下载页；`STUDIO_UPDATE_FEED` 可覆盖（不做差量）
- [x] `scripts/build-studio-desktop.ps1` 一键：内核 → 依赖 → electron-builder
- [ ] CI（windows-latest，可选，未做）

验收已完成：静默安装 → 启动（内核从安装目录的 `resources/core` 拉起、协议注册到安装后的 exe、
UI 200）→ 卸载（安装目录清空，`~/.synapforge` 用户数据保留）。
覆盖升级未实测（需要先发布 0.2.0 的安装包）；协议注册键卸载器不清，已在 README 写明手工清理。

## 6 P4 · 原生观感（约 2 天，可选）

- [ ] Win11 Mica + 自绘标题栏（未做：需要整页透明化 + 自绘窗口按钮，风险大于收益）
- [x] `Ctrl+Shift+Space` 全局快捷键：唤起/收起主窗（已聚焦再按时收起）
- [x] 诊断页：壳/内核版本、端口、PID、数据目录、日志尾部、重启内核；每 2s 刷新
- [ ] 独立快速面板（未做：当前快捷键直接唤起主窗，不做第二个 UI 面）

## 7 风险清单

| 风险 | 处置 |
| --- | --- |
| PyInstaller 被杀软误报 | 用 ONEDIR（不用 onefile，本仓已验证）；不混淆、不加壳 |
| 内核成了孤儿 | 壳退出先 graceful；P1 加 `--parent-pid` 看门狗；实例锁兜底 |
| 端口/token 读错实例 | 壳启动前删 `studio.json`；握手文件带 pid 且原子写 |
| 反代部署被 Host 校验打断 | 仅本地形态严格校验；`public_url`/`allowed_origins` 存在时按配置放行，并加回归测试 |
| 未签名安装包 SmartScreen | 沿用现有文档说明；后续可上 Azure Trusted Signing |
| 更新把用户数据搞丢 | 数据在 `~/.synapforge`（或 `SYNAPFORGE_STATE_DIR`），安装目录只放程序 |
| Electron 内存占用高 | 接受；托盘常驻时窗口可 `hide()` 不销毁，P4 再评估 `webContents.setBackgroundThrottling` |

## 8 里程碑

| 阶段 | 产出 | 工作量 |
| --- | --- | --- |
| P0 | 无控制台的内核 exe + 健康/退出/锁/Host 校验 | 1–1.5 天 |
| P1 | 双击可用的窗口版（托盘/菜单/单实例/崩溃重启） | 2–3 天 |
| P2 | 通知/自启/深链/拖拽/文件集成 | 2–3 天 |
| P3 | NSIS 安装包 + 更新 | 2–3 天 |
| P4 | Mica/快速面板/诊断页 | ~2 天 |
| P5 | macOS（Developer ID + 公证） | 另计 |

**P0 + P1 = 4–5 个工作日**可交付“双击就能用”的版本。

## 9 实施记录（2026-11-16）

**P0 已交付**

- `studio --log-file`：`--noconsole` 下 stdout 为 None 时自动接日志；启动时按 5MB 轮转
- `GET /api/studio/health`、`POST /api/studio/shutdown`
- `Host` 头校验（本地只认回环；`--public-url` / `--allowed-origin` 放行反代域名）
- `studio.lock` 单实例锁（第二个内核 exit 3）
- `studio.json` 原子写（tmp + replace），补 `started_at`
- `--parent-pid` 看门狗（2s 轮询；Windows 用 OpenProcess，**不能用 `os.kill(pid, 0)`**）
- PyInstaller：`synapforge-core.exe`（GUI）+ `synapforge-mcp.exe`（stdio）
- 测试：221 项全绿（新增健康/退出/Host/锁/日志轮转/子进程 E2E/看门狗 9 项）

实测：core 45.3MB，PE subsystem=2（无控制台）；双击→握手 1s 内；shutdown→退出码 0、
`studio.json` 删除；MCP 17 工具、UTF-8 正常。

**P1 已交付**

- `apps/studio-desktop`：`src/main.js`（单实例/窗口/托盘/菜单/IPC）、`src/core.js`
  （定位内核、spawn、握手、健康、崩溃重启）、`src/preload.js`、`renderer/error.html`
- 行为：关窗到托盘、`Ctrl+Q` 优雅退出、第二次启动聚焦、内核被杀后自动重启并让窗口
  跟到新端口/新 token、壳被杀后内核跟随退出
- 开发期自动开 `127.0.0.1:9333` CDP（打包不生效）

实测（真机）：窗口 1280×860 出图、菜单/托盘正常；`CloseMainWindow` 后窗口隐藏且进程存活；
`Ctrl+Q` 后 electron/core 全部退出且 `studio.json` 被清理；`kill` core 6s 内换新端口并重新加载。

**P2 已交付（2026-11-16，提交 `48b8a92` + 后续一笔）**

- `GET /api/studio/notifications`：游标取最后一条时间（不取 now），终态过滤、limit 分页
- 壳 5s 轮询；失焦时系统通知，点击 `sfOpen` 跳运行详情/集群任务
- 开机自启（托盘勾选 + IPC 回读）；`synapforge://cluster|run|view` 深链（含 argv 转发）
- 拖拽文件 → 绝对路径插入 `#planInput` / `#composer`，带空格自动加引号
- 实测：插入 `"…/drop me.txt"`；深链 `view/clustertasks`、`run/<uuid>` 均落到对应页；
  `setAutostart(true)` 写入 Run 注册表、关掉后删除

**P3 已交付（2026-11-16）**

- electron-builder NSIS 每用户安装 + `win-unpacked`；`extraResources` 把内核放到 `resources/core`
- 一键脚本 `scripts/build-studio-desktop.ps1`（electron / builder 二进制走 npmmirror，绕开 GitHub 阻断）
- 更新检查拆成可测的 `src/feed.js` + `src/version.js` + `src/updater.js`；
  `node scripts/check_studio_shell.js` 9 项全过（版本比较、feed 解析、缺字段、404）
- 安装包 136MB；实测静默安装 → 启动（安装目录内核、协议指向安装后的 exe、UI 200）→
  卸载（安装目录清空，`~/.synapforge` 保留）
- 未发布到 synapforge.top（需要云端发布流程）；`latest.json` 格式见 `latest.example.json`

**P4 部分交付（2026-11-16）**

- 全局快捷键 `Ctrl+Shift+Space` 唤起/收起（实测：`SendKeys` 两次可见性 True→False→True）
- 诊断页 `renderer/diagnostics.html`：壳/内核版本、端口、PID、数据目录、日志尾部、重启内核；
  托盘与「帮助」都有入口；实测窗口内容与日志读取正常
- 未做：Mica/自绘标题栏（整页透明化影响所有视图）、独立快速面板（避免第二个 UI 面）

**未做（按计划留给后续）**

- CI（windows-latest）可选
- 已知：`workbench.html` 没有 CSP（Electron 开发期会告警，打包后不显示）；窗口首次启动
  前会在后台等内核，用户看到的是系统标题栏而不是空白页（窗口 show 在 `ready-to-show`）；
  卸载器不清 `HKCU\Software\Classes\synapforge`（README 给了手工清理命令）
- 已知：`workbench.html` 没有 CSP（Electron 开发期会告警，打包后不显示）；窗口首次启动
  前会在后台等内核，用户看到的是系统标题栏而不是空白页（窗口 show 在 `ready-to-show`）
