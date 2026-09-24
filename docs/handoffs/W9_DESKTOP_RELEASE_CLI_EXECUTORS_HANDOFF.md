# W-9 桌面端发布 · 下载入口 · 通用 CLI 执行体 交接

> 交接状态：`PASS`
>
> 日期：2026-09-22
>
> 来源：用户两条指令——①"桌面端是否同步最新并打包好？要在平台加下载桌面端的链接" ②"添加更多 CLI 适配：workbuddy、ZCode harness、豆包桌面端（如果可以）"
>
> 已发布：`https://synapforge.top`（`server_verify.sh` 22/22；安装包线上可下载）

---

## 1. 桌面端同步状态（先回答问题）

**结论：桌面端是"壳"，不内嵌网页，所以不会被前端迭代拖旧。**

- 桌面端（`apps/desktop`）= Electron 壳：托盘常驻 + Python 内核（sidecar）管理 + 配对深链；
  **不含**平台网页——网页永远来自服务器，因此 W-1～W-8 的前端改动对已装客户端零影响。
- 内核（`apps/agent`）走的是平台**稳定的 Agent 协议**（claim/progress/result/run/gateway），
  这轮没有改动协议，所以 9-16 那版安装包功能上仍可用。
- 但它确实"旧"在两点：品牌（还叫 Math Agent Platform）与**本轮新增的 CLI 执行体**。
  因此本次：**品牌改名 synapforge + 版本 0.1.1 + 重建 sidecar（PyInstaller）+ 重建 NSIS 安装包**，
  产物 `apps/desktop/dist/synapforge-setup-0.1.1-x64.exe`（143,811,038 字节，未签名）。
- 构建过程踩坑：`scripts/build-desktop.ps1` 的 electron-builder 步骤因**连 GitHub 超时**失败
  （`connect ETIMEDOUT 20.205.243.166:443`，sidecar 已成功重建）；直接跑 `npm run build:win` 用缓存即成功——
  网络抖动时按此重试即可。

## 2. 平台上的下载入口

- **服务方式**：nginx 域名入口新增 `location ^~ /downloads/`（`alias /opt/math-agent-platform/downloads/`，
  静态直出、不套 Basic、不经过 Node），文件放在 `/opt/math-agent-platform/downloads/`。
  配置片段同步进仓库 `scripts/deploy/nginx-locations-public.conf`。**线上实测**：
  `GET https://synapforge.top/downloads/synapforge-setup-0.1.1-x64.exe` → **200**，
  `Content-Length: 143811038`，**sha256 与本地构建产物完全一致**（`bb2f0d4d…2e7f`）。
- **界面入口 4 处**：登录页（第一次使用？注册即用 · 下载桌面端）、注册页、工作区新用户引导面板、
  设备与接入页新增常驻面板「桌面端（推荐）」（原先的下载提示藏在"生成配对"弹窗里，不到那一步看不见）。
  链接统一指向 `/downloads/synapforge-setup-0.1.1-x64.exe`（相对路径，换域名/自托管也成立）。
- 未签名安装包的提示写在设备页与文档里：首次运行选「更多信息 → 仍要运行」。

## 3. 通用 CLI 执行体（workbuddy / ZCode / 其他）

新增 `worker_executor = "cli"`——**提示词驱动的命令模板**，语义与既有"声明式命令"完全一致
（stdout 即结果、退出码即成败），只是提示词来自任务而不是写死在 argv 里：

```
resource_policy = {
  "worker_executor": "cli",
  "worker_command": ["workbuddy", "exec", "{prompt}", "--out", "report.md"],
  "worker_prompt": "给这个 CLI 的指令（留空则用 标题+说明+完成标准）"
}
```

- **API 侧**（`Store._validated_resource_policy`）：白名单从 `{codex}` 扩到 `{codex, cli}`；
  `cli` 必须带命令模板且**必须含 `{prompt}` 占位符**（没有就说明该走 command 路径）；
  其他未知执行体仍然拒绝（实测 `doubao` → `unsupported_worker_executor:doubao`）。
- **Agent 侧**（`agentd`）：`_executor_kind()` 判定 codex/cli/command/未声明；`_cli_command()`
  把 `{prompt}` 替换成任务提示词后走原有的 LocalRunner 路径（边界、输出采集、事件回报全部复用）。
- **界面**：`/tasks` 的「设置执行方式」弹窗新增第三项「通用 CLI（workbuddy / zcode 等，命令模板带 `{prompt}` 占位符）」，
  填模板 + 提示词即可保存。
- **安装探测**：`agent_inventory` 新增 workbuddy / zcode 探测（装了记 AVAILABLE，没装记 NOT_INSTALLED）。
  实测本机：codex AVAILABLE、claude-code/workbuddy/zcode NOT_INSTALLED——**如实报告，不假装支持**。

**为什么不用猜的协议**：仓库既有原则写在 `cli_adapters.py` 开头——"协议没被明确理解就拒绝，
而不是当成普通文本或成功"。workbuddy 与 ZCode 都没有可核验的无头 JSON 协议（ZCode 是 Electron 桌面应用、
`zcode` 不在 PATH；本机也没有 workbuddy 可执行），所以正确的做法是：**平台提供提示词驱动的通用通道，
各家 CLI 的真实调用方式由任务模板声明**。等它们有了稳定的机器协议，再各写一个语义适配器（如 CodexAdapter）。

**豆包桌面端：接不了**（如实结论）。它是 GUI 聊天应用，没有 CLI/自动化接口、没有可编排的本地协议，
平台的执行体是"能起子进程并拿到退出码"的东西——GUI 应用不在这个范畴。若将来出了官方 CLI/API，
用本轮的 `cli` 执行体填它的调用方式即可接入，不需要平台再改代码。

## 4. 验收

| 项 | 结果 |
| --- | --- |
| 测试 | 新增 `apps/agent/test_cli_executor.py` **12 项**（策略 5：模板合法/缺模板/缺占位符/未知执行体拒绝/codex 与 command 不变；命令行 4：类型判定、占位符替换、honors worker_prompt、回退；探测 3）；**Agent 套件 293 项全通过（9 skipped）**；API 全量 **453 项**（仅 2 项 LaTeX 环境失败） |
| 前端 | 25 页构建；SSR 19×19×6 |
| HTTP 实测（本地） | `cli` 模板 → 200 且策略回读一致；无占位符 → 409 `cli_executor_template_requires_prompt_placeholder`；`doubao` → 409；`codex` 仍 200 |
| 浏览器 | 登录页/注册页显示下载链接；设备页常驻「桌面端（推荐）」面板（`/downloads/…exe`）；执行方式弹窗三项齐全，选「通用 CLI」出现模板与提示词字段（预填 `workbuddy exec {prompt}`） |
| 线上 | `server_verify.sh` **22/22**（两次发布）；`/downloads/…exe` → 200 + 143,811,038 字节 + sha256 一致；登录/注册页 SSR 含下载链接 |

## 5. 边界

- 安装包**未签名**（Windows SmartScreen 会提示）；也没有自动更新通道（app-update.yml 只写了 provider 占位）。
- `cli` 执行体**不做协议解析**：看不到工具事件/审批事件，只有 stdout/退出码——要结构化事件仍得用 codex 适配器。
- 命令模板按**空格分词**（与声明式命令一致）：含空格参数的路径要自己加引号，或改用 `worker_command` 数组形式。
- 探测只回答"在不在"（`--version` 能否跑），不判断该 CLII 的语义能力；workbuddy/zcode 装没装以本机实测为准。
- 桌面端安装包随源码一起演进，但**不会**因为网页改动而需要重发——只有内核（apps/agent）或壳改动时才需要重建。

## 6. 复现

```bash
# 重建桌面端（网络抖动时：先用 npm run build:win 走缓存，再补 -SkipSidecar 的完整脚本）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-desktop.ps1
cd apps/desktop && npm run build:win            # 仅重打安装包（用已重建的 sidecar）

# 上传安装包 + 应用 nginx 片段
python scripts/deploy/remote.py put --host <IP> --user root --password-file <secret> \
  --src apps/desktop/dist/synapforge-setup-0.1.1-x64.exe \
  --dst /opt/math-agent-platform/downloads/synapforge-setup-0.1.1-x64.exe
python scripts/deploy/remote.py put --host <IP> --user root --password-file <secret> \
  --src scripts/deploy/nginx-locations-public.conf --dst /etc/nginx/snippets/math-agent-platform-locations-public.conf
ssh root@<IP> "nginx -t && systemctl reload nginx"

# 测试
PYTHONPATH="apps/api;apps/agent" python -m unittest discover -s apps/agent -p "test_*.py" -t .
```