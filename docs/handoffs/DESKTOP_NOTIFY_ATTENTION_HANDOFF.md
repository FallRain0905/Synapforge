# DESKTOP-NOTIFY 交接：派给我的任务 / 待我复核 → 系统通知 · 已上线

> 2026-09-23 · 接上一轮 `docs/handoffs/DESKTOP_GUI_EMBEDDED_WORKBENCH_HANDOFF.md` §6 的"未做第一条"
> 用户要求：**开始下一步覆盖「派给我的任务/待复核」**
> 安装包：`https://synapforge.top/downloads/synapforge-setup-0.2.1-x64.exe`（143.8MB，未签名）
> sha256 `4dab588f33a878748008b967cd8a5df2cfa697917b6af2b24240ed87c0b03498`

## 1 交付

| 层 | 做了什么 |
| --- | --- |
| **平台** | 新端点 `GET /api/my-attention`（`MyAttention`）：**派给我的未结束任务** + **我该复核的未决任务**，只含最小字段（任务/项目/状态/负责执行体/是否过期）。口径与 `review.approve` 权限对齐（owner / project_lead / reviewer） |
| **桌面端** | 后台**每 60 秒**拉一次该端点，新条目发系统通知，**点通知直达任务详情**（`/tasks?task=<id>`）；待办数（派给我 + 待复核）进托盘角标；本机 Agent 面板显示"桌面提醒"块（派给我 N · 待复核 M · 上次检查时间 / 上次错误） |
| **前端** | `lib/desktop-attention.ts`：登录后把会话上下文交给壳；收到"派给我/退回给我"的事件时催壳**立刻**拉一次（≥5 秒去抖，壳自己的 60 秒轮询是兜底）。浏览器里没桥 → 整个 hook 空操作 |

## 2 关键设计（为什么这么做）

1. **在壳里轮询，不在前端轮询**：窗口关掉（托盘常驻）时渲染进程已销毁，而"有人把任务派给你""有个东西等你复核"恰恰是关着窗口也要知道的。前端只负责"交令牌"和"催一次"。
2. **会话令牌只在内存里**：`setAttentionContext({token, api_url, member_id})` 不落盘、不进 `desktop.json`。重启后需要重新登录才会继续轮询——刻意取舍：**宁可少通知，也不把会话令牌写到磁盘上**（设备/项目令牌本来就在 Windows 凭据管理器，属于内核的地盘）。
3. **降噪规则**（`apps/desktop/src/attention.js`，纯函数 + 8 项单测）：
   - 首见即通知；
   - **`NEEDS_REVISION` 会再通知一次**（"退回给我改"是新的一次要你动手），其余状态变化（READY→CLAIMED→RUNNING→WAITING_REVIEW→BLOCKED）**不通知**——机器在干活，不该吵人；
   - 处理完消失的条目不通知（"没事了"不值得打扰）；
   - 已见键落 `attention.json`（保留最近 300 条）→ **跨启动不重复提醒**（实测：第二次启动通知数 0）；
   - 401/403 时停止轮询并**只提示一次**"登录已过期"（不做每 60 秒骚扰）。
4. **服务端不存"已读"**：已读/已通知是客户端的事，避免服务端多一份真相。

## 3 落点

- `apps/api/app/store.py`：`my_attention()` + `REVIEW_APPROVER_ROLES`；`apps/api/app/contracts.py`：`AttentionItem` / `MyAttention`；`apps/api/app/main.py`：`GET /api/my-attention`。
- `apps/desktop/src/attention.js`（新，纯逻辑）、`apps/desktop/test/attention.test.js`（新，`node --test`）。
- `apps/desktop/src/main.js`：关注清单轮询/去重/通知点击跳转/角标/401 处理、IPC `set-attention-context|attention-status|poll-attention`、`snapshot().shell.attention`、自检里带上 `attention` 摘要。
- `apps/desktop/src/preload.js`：`setAttentionContext` / `attentionStatus` / `pollAttention`，`notify` 支持 `{title, body, url}`。
- `apps/web/lib/desktop-attention.ts`（新）、`components/shell.tsx`（接线）、`components/local-agent-panel.tsx`（提醒状态块）、4 处下载链接 → 0.2.1。
- `scripts/deploy/_desktop_notify_seed.py` / `_desktop_notify_cleanup.py`（线上验证数据，成对使用）。

## 4 验收（全部实测）

| 项 | 结果 |
| --- | --- |
| 后端全量 | **515 项**（+7 `test_attention`），仅 2 项既有 LaTeX 环境失败 |
| Agent 套件 | 304 项（9 skipped） |
| 桌面端纯逻辑 | `node --test` **8 项通过**（去重、退回重报、流转不刷屏、消失不报、路径编码、300 条上限、坏输入不炸） |
| 前端 | `tsc --noEmit` 干净、`next build` 25 页全静态 |
| 本地 dev 自检 | 首跑 16 条通知（2 派给我 + 14 待复核，本地库有历史验收残留）；**同目录第二次启动通知数 0**（跨启动去重生效） |
| **打包 0.2.1 对生产** | `通知(assigned)：有任务派给你 — [通知验证] 派给我的任务（演示 · 多 Agent 文档撰写）`、`通知(review)：待你复核 — [通知验证] 待我复核的任务（…）` 均按预期出现；`SELFCHECK ok:true, version:0.2.1, attention:{ok:true, assigned_total:1, review_total:6}, panel_open:true, kernel_contract:v1`，退出码 0 |
| 线上发布 | `server_release.sh` 成功；`server_verify.sh` **22/22**；`/api/my-attention` 匿名 401 |
| 线上产物 | `latest.json` → 0.2.1（sha256 与本地一致）；安装包 200 且字节数一致；`/login` 已指向 0.2.1 |
| 清理 | 线上临时探针数据（2 个账号 + 4 条 `[通知验证]` 任务，连会话/成员关系/事件/门禁/卡片）已按外键顺序删净 |

## 5 构建踩坑（值得记住）

**electron-builder 在 GitHub 不可达时不能只靠"走缓存"**：`winCodeSign` 可能不在缓存里，
失败信息是 `connect ETIMEDOUT 20.205.243.166:443`（GitHub）。可靠命令是**带镜像**：

```bash
cd apps/desktop && \
ELECTRON_BUILDER_BINARIES_MIRROR=https://npmmirror.com/mirrors/electron-builder-binaries/ \
ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/ \
npm run build:win
```

（上一轮记的"改用 `npm run build:win` 走缓存"只对 electron 主包有效。）

## 6 边界与未做

- **重启后需重新登录**才会继续轮询（令牌不落盘，见 §2）。
- 提醒范围是"派给我的任务 + 我该复核的任务"；**@我、评论、交接回执**暂未纳入（同一套机制加一类即可）。
- 通知不发邮件/不推手机（没有 SMTP 与推送通道）。
- 侧栏"本机 Agent"仍是顶栏抽屉面板（未改成侧栏标签页）。