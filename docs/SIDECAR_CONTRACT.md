# Sidecar 契约（桌面端壳 ↔ Python 内核）

> 日期：2026-09-16
>
> 版本：`v1`（**冻结**：新增只能追加，不得修改既有字段语义）
>
> 状态：`DP-0-02` 交付物；`DP-2` 追加 `/grant`、`platform.json`、`connection.state` 新取值。实现见 `apps/agent/sidecar_api.py`，测试见 `apps/agent/test_sidecar_api.py`（40 项）
>
> 上游：`docs/DESKTOP_CLIENT_IMPLEMENTATION_PLAN.md`（DE3）、`docs/DESKTOP_CLIENT_PLAN.md` §5

---

## 1. 为什么需要这份契约

桌面端是"壳 + 内核"两个进程：壳（Electron）负责托盘、窗口、深链、自启与更新；内核（现有 Python `agentd`/`machine_service`）负责连接、任务、凭据与执行。两者必须**只通过这份契约通信**，好处有三：

1. **壳可替换**：日后从 Electron 换到 Tauri，只重写壳，内核不动（DE1）；
2. **可独立测试**：内核的接口能用 HTTP 直接测，不依赖 Electron（本机工具链里 Rust 缺失、Electron 需下载，这一点尤其重要）；
3. **权限边界清晰**：内核是唯一持有凭据与平台连接的进程，壳不接触设备/项目 Token（DE6）。

## 2. 传输与鉴权

| 项 | 约定 |
| --- | --- |
| 监听地址 | **仅 `127.0.0.1`**（不得监听 `0.0.0.0`；收缩攻击面） |
| 端口 | 由内核启动时选定空闲端口并写入"启动信息文件"（见 §3），壳读取后连接 |
| 启动信息文件 | `%LOCALAPPDATA%\MathAgentPlatform\sidecar.json`，字段：`{ "port": 51234, "token": "<43 字符>", "pid": 1234, "started_at": "<ISO8601>", "contract": "v1" }`；文件权限限当前用户（POSIX 下 0600） |
| 鉴权 | 除 `/health` 外**所有**端点要求 `Authorization: Bearer <token>`；token 每次启动重新生成，退出即失效 |
| 鉴权失败 | `401` + `{"error": "sidecar_unauthorized"}`；不返回任何业务信息 |
| 请求体 | `application/json; charset=utf-8` |
| 响应体 | `application/json; charset=utf-8` |
| 幂等 | 所有写端点可重复调用：重复配对返回既有身份，重复暂停无副作用 |

**为什么用回环 HTTP 而不是 stdio/Named Pipe**：壳与内核的生命周期是解耦的（壳崩溃不应中断任务），HTTP 让内核可以独立存活与重启；且调试成本最低（curl 即可）。Named Pipe 已有实现（`apps/agent/named_pipe_transport.py`），作为后续加固选项而非首版。

## 3. 启动握手

```text
壳                                    内核
 │  spawn sidecar（无参数或 --config <路径>）
 │ ─────────────────────────────────────►│ 选端口、生成 token、写 sidecar.json
 │ 轮询读取 sidecar.json（最多 10s）
 │ ◄─────────────────────────────────────│ 文件就绪
 │  GET /health（无需鉴权）
 │ ─────────────────────────────────────►│
 │ ◄─────────────────────────────────────│ {status, contract, version...}
 │  后续所有请求带 Bearer token
```

握手失败处理：壳在 10 秒内未读到合法的 `sidecar.json` 或 `/health` 不返回 `status=ok`，则视为启动失败 → 记录日志 → 重试一次（间隔 2s）→ 仍失败则托盘显示红色并给出"查看日志"入口。

## 4. 端点

### 4.1 `GET /health`（无鉴权）

存活探针，**不泄露任何配置信息**。

```json
{ "status": "ok", "contract": "v1", "version": "0.1.0", "started_at": "2026-09-16T10:00:00+00:00", "pid": 1234 }
```

### 4.2 `GET /status`

壳用于托盘状态灯与本地状态页。

```json
{
  "connection": { "state": "connected", "since": "...", "reconnect_attempt": 0, "last_error": null },
  "identity": { "device_id": "device-abc", "agent_id": "agent-abc", "project_id": "e5cf61e3-…", "platform_url": "http://127.0.0.1:8000",
                "paired_at": "...", "last_pair_error": null },
  "tasks": { "running": [], "claimed_current": null, "paused": false, "completed_since_start": 3 },
  "queue": { "local_pending_events": 0, "local_queue_length": 0 },
  "local_agents": [
    { "adapter_id": "codex-cli", "state": "AVAILABLE", "version": "0.154.0-alpha.6.2", "executable": "C:\\...\\codex.exe", "checked_at": "..." },
    { "adapter_id": "claude-code", "state": "NOT_INSTALLED", "version": null, "executable": "claude", "checked_at": "..." }
  ],
  "emergency_stop": false,
  "contract": "v1"
}
```

`connection.state` 取值：`unpaired` | `starting` | `connecting` | `connected` | `reconnecting` | `disconnected` | `stopped` | `emergency_stopped` | `error`。
（`unpaired`/`starting`/`reconnecting`/`stopped`/`emergency_stopped` 为 DP-2 追加；壳按"未知值当灰色"处理即可。）
`identity.project_id` 为 DP-2 追加：未授权项目时为 `null`。
`queue.local_queue_length` 为 DP-2 追加：本机待执行/执行中的任务数（串行执行，取值 0 或 1）。
`local_agents[].state` 取值沿用 `CliCapabilityState`：`AVAILABLE` | `NOT_INSTALLED` | `UNSUPPORTED` | `ERROR`。

### 4.3 `POST /pair`

首次接入（深链或手输都走这里）。幂等：已配对且身份一致时返回既有身份。

请求：

```json
{
  "platform_url": "http://127.0.0.1:8000",
  "pairing_blob": "eyJwYWlyaW5nX2lkIjoi...",   // 网页复制的配对串（base64url(JSON)）
  "agent_name": "我的工作站",                    // 可选；缺省用主机名
  "agent_id": "agent-xxx",                       // 可选；缺省由主机名派生
  "device_id": "device-xxx",                     // 可选；缺省由主机名派生
  "codex": true                                  // 可选；是否顺带探测并记录 Codex 路径（默认 true）
}
```

响应：

```json
{ "paired": true, "device_id": "device-xxx", "agent_id": "agent-xxx", "public_key_fingerprint": "…", "credential_stored": true, "codex_path": "C:\\…\\codex.exe", "message": null }
```

错误（`4xx`，`error` 为稳定错误码，`detail` 为可展示文本）：

| HTTP | error | 触发 |
| --- | --- | --- |
| 400 | `sidecar_pairing_blob_invalid` | 配对串无法解析、缺少字段（`pairing_id`/`pairing_code`/`challenge`）或 `expires_at` 非法 |
| 400 | `sidecar_pairing_expired` | 配对串已过期（`expires_at` 早于当前时间）——在向导页重新生成 |
| 400 | `sidecar_platform_url_invalid` | 平台地址非法（非 http/https） |
| 409 | `sidecar_already_paired` | 已配对其他身份（设备 id/公钥不同）——需先撤销或换设备 id |
| 502 | `hyper_rag_unavailable` 风格的上游错误码 | 平台不可达/拒绝（沿用 `http_<code>:<detail>` 语义） |

> 说明：不新增"平台侧错误码"，直接透传上游语义，避免两套错误词表。

**设备标识冲突的自动处理（v1 追加行为）**：平台侧"一个 `device_id` 只能注册一次、一个公钥只能注册一台设备"。
当主机名派生的标识已被占用（重装、重置、同机多人使用）时，内核会**自动追加随机后缀重试一次**
（`device-<host>` → `device-<host>-a1b2`），并把最终标识回报在响应与 `/status` 里；
不这样做的话，同一台机器重装后永远无法再次接入。

### 4.5 `POST /grant`（v1 追加，DP-2-08）

把**项目授权串**交给内核：内核写入凭据管理器与本地配置，并**立刻开始领取该项目任务**（不必重启内核）。

请求：

```json
{
  "platform_url": "http://127.0.0.1:8000",
  "grant_blob": "eyJwcm9qZWN0X2lkIjoi…"      // 网页「授权到项目」后复制的授权串（base64url(JSON)）
}
```

响应：

```json
{ "granted": true, "project_id": "e5cf61e3-…", "agent_id": "agent-abc", "device_id": "device-abc",
  "capabilities": ["task.claim", "…"], "task_loop": "running", "message": null }
```

| HTTP | error | 触发 |
| --- | --- | --- |
| 400 | `sidecar_grant_payload_invalid` | 请求体不是对象 |
| 400 | `sidecar_platform_url_invalid` | 平台地址非法（非 http/https） |
| 400 | `sidecar_grant_blob_invalid` | 授权串缺失/无法解析（含 `grant_device_mismatch` / `grant_platform_mismatch`：授权串里的设备或平台与本内核不一致） |
| 503 | `sidecar_grant_unavailable` | 当前内核形态没有平台连接（例如 `sidecar-run`）——**如实报不可用，不假装成功** |
| 500 | `sidecar_grant_failed` | 其它未预期失败（`detail` 带异常类型） |

`task_loop` 取值 `running`（已开始领取）| `pending`（内核尚未开始连接，授权已保存，启动后生效）。

> 授权串含**项目 Token**，等同短期凭证：只经本机回环 + 启动令牌传输，**不写日志**（日志里只出现 `project_id`）。
> 内核侧只把它存进 Windows 凭据管理器（`MathAgentPlatform/project-token/<project_id>`）。

### 4.6 `POST /agents/rescan`

立即重扫本机 CLI（安装新 CLI 后不必等周期；探测结果带 TTL 缓存，重扫会强制真探测）。

响应：`{ "local_agents": [ …同 /status 的 local_agents… ] }`

### 4.7 `POST /tasks/pause` / `POST /tasks/resume`

暂停/恢复**领取**任务（不影响正在执行的任务）。

响应：`{ "paused": true }` / `{ "paused": false }`

### 4.8 `POST /emergency-stop` / `POST /clear-emergency-stop`

本地紧急停止：**停止领取 + 断开平台连接**（平台侧随即看到该 Agent 掉线）。
内核**保持存活**（契约服务继续可用），因此托盘能"解除紧急停止"——解除后自动重连并恢复领取；内核不因紧急停止而退出。

响应：`{ "emergency_stop": true }`

### 4.9 `GET /logs?tail=200`

最近日志（壳的"查看日志"）。`tail` 取值 1–2000，默认 200。

响应：`{ "lines": ["…"], "truncated": false }`

### 4.10 `POST /shutdown`

优雅退出：先断开平台连接（写 `agent.offline` 由平台侧判定或直接断开），再退出进程。

响应：`{ "shutting_down": true }`（壳随后等待进程退出，超时则强杀）

## 4b. 状态目录文件（v1 追加）

| 文件 | 内容 | 说明 |
| --- | --- | --- |
| `sidecar.json` | 端口 + 启动令牌 + pid | 每次启动重写，退出即删除 |
| `platform.json` | `{ "url", "device_id", "agent_id", "paired_at" }`，可追加 `workspace` | 配对成功后写入；内核重启后据此连回同一平台（**不含任何 Token**）。`workspace`（FM-0，新增可选键）是用户选定的 Agent 工作目录：内核收到显式 `--workspace` 时**合并写**入（不动其它键），下次不带参数启动时用它——否则会退回进程当前目录，而打包后的桌面端那正是安装目录 |
| `worker.json` | 项目身份（`project_id`/`agent_id`/`device_id`/能力/过期时间） | 项目 Token 不在这里，在凭据管理器 |
| `agentd.db` | 本地 outbox、Run 状态、上传队列 | 设备/项目 Token 永不入内 |

## 5. 契约演进规则

1. **只能追加**：新增端点用新路径；新增字段必须是可选（有默认值），不得改变既有字段的类型或语义；
2. `contract` 字段用于壳的兼容判断（`v1` → `v2` 表示有破坏性变更，届时壳需按版本分支）；
3. 破坏性变更必须同时更新：本文档版本号、`docs/DESKTOP_CLIENT_PLAN.md` §5、以及 `IMPLEMENTATION_STATUS.md` 的登记；
4. 内核不暴露"执行任意命令"的端点——任务执行只由平台派发的任务触发（壳无法借此越权）。

## 6. 与平台侧的关系（别混淆）

| 通道 | 谁连谁 | 鉴权 | 传什么 |
| --- | --- | --- | --- |
| 壳 ↔ 内核（本文档） | 壳连本机内核 | 启动令牌 | 状态、配对指令、暂停/停止 |
| 内核 ↔ 平台 | 内核连平台 | 设备 Token（Bearer）/ 项目能力 Token | 心跳、任务、结果、Run |

**Token 不跨通道**：壳永远拿不到设备 Token；平台永远看不到启动令牌（DE6）。