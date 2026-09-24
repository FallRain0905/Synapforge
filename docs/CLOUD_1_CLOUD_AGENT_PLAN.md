# CLOUD-1 计划：云端执行体服务器（另一台机器跑 Agent，接入平台）

> 2026-09-23（**v3：P1 已完成——云端执行体上线，可跑真任务**）· 状态：**六项断言全绿（`_cloud_agent_verify.py` 全部通过）**
> 用户已拍板：①**另买一台服务器**（已到手并装好，见 §5）②执行体跑 **opencode**（v1 写的 zcode 作废）③模型走**阿里百炼**（已给并已配：`deepseek/deepseek-v4.1-flash`）④云端 Agent **只执行派给它的任务**，与平台运维、与它自己所在服务器的运维都无关
> **v2 相对 v1 的修订**：
> 1. 执行体 **zcode → opencode**；「换执行体 ⇒ 平台零改动」这个判断**被实测推翻**（见 §3.1）；
> 2. 把「opencode 协议适配（方案 B）」写进 P0，并**已完成**（Agent 327 项 / API 515 项，见 §6.0 与交接文档）；
> 3. 门禁从"zcode 能否在 Linux 非交互跑"改成"opencode 已验证 / 百炼 provider 待验"；
> 4. 补上新服务器的实测环境事实与两个新发现的坑。
> 上游：`docs/AGENT_DEVICE_CONNECTION_DESIGN.md`、`docs/DESKTOP_CLIENT_FULL_GUI_PLAN.md`（桌面端形态）、`docs/handoffs/AIP_1AB_DESCRIPTION_DISCOVERY_HANDOFF.md`（能力卡/候选）、`docs/handoffs/COST_1_USAGE_BUDGET_HANDOFF.md`（用量与预算）、`docs/handoffs/CLOUD_1_OPENCODE_ADAPTER_HANDOFF.md`（本次适配器，含真实样本）

---

## 1 结论先说

**技术上成立。** Agent 内核（`apps/agent/agentd.py`）是纯 Python + HTTP 的常驻体：注册 / 心跳 / 领取 /
执行 / 上报全走平台已有端点，没有 Windows 专属依赖，设备配对也能纯命令行完成。

**但"换执行体 = 平台零改动"这句话，只对"能不能跑起来"成立，对"跑得好不好"不成立。**
v1 我写的是：opencode 走通用 `cli` 通道、stdout 即结果、平台侧完全不用改。实测（§3.1）发现两件事：

- `opencode run --format json` 的 stdout 是**事件流**，不是结果文本——直接当结果会把一整串 JSON
  塞进 Run 摘要与成果物；
- 通用通道**不解析** opencode 的事件，于是**过程事件（聊天区的实时进展）与用量（COST-1 的 token 预算判定）双双拿不到**。

所以按用户拍板走了**方案 B**：给 opencode 加协议适配（已完成，§6.0）。**现在**要做的只剩两件，都在执行体那一台：

1. **凭据存储**：`credential_store.py` 是 **Windows 凭据管理器专用**（非 Windows 抛 `credential_backend_unavailable`）
   → 加 **POSIX 文件后端**（0600 + 0700 目录，专用非 root 用户）；
2. **运行封装**：systemd 模板单元 + 安装/配对脚本 + 加固项（P0，本机可测）。

---

## 2 为什么值得做（收益按价值排序）

| # | 收益 | 为什么 |
| --- | --- | --- |
| 1 | **演示不再依赖你的笔记本** | 现在的「演示 · 多 Agent 文档撰写」是**静态剧本**（`_demo_doc_writing.py` 直接写库）。有常驻执行体后它可以**真跑出来**：任务被领取、Run 有记录、用量有数字、成果物真产出 |
| 2 | **网页 AI 从"只对话"变成"能派活"** | 链路已齐：聊天区 `/task` 建任务 → 派单/认领/auto → 常驻执行体 → 结果回成果空间 + 系统通知。缺的只是一座小桥（`/ask` → 任务），留到 P3 |
| 3 | **平台的高级功能终于有真数据** | 能力卡、候选排序、平滑成功率、用量/预算判定在线上几乎是空表——因为没有常驻执行体 |
| 4 | **多执行体同场对比** | 一台 8G 机器可放多个内核实例（各自 `MAP_STATE_DIR` + 独立设备身份），让 opencode / codex 在同一项目里竞技 |
| 5 | **测试更快** | 可以从服务器侧跑真实闭环，不占用你的 Windows 机器，也不影响线上演示数据 |

---

## 3 核验结果（v1 的"门禁"现在有了答案）

### 3.1 已在服务器上实测（不是推算）

2026-09-23 在新机器上实跑（原样命令与输出在交接文档里）：

| 核验项 | 结果 |
| --- | --- |
| opencode 有没有 Linux 版 | **有**。`curl -fsSL https://opencode.ai/install \| bash` 装成 **1.18.32**，落地 `/root/.opencode/bin/opencode`（185 MB **独立二进制**，不需要 Node） |
| 有没有非交互入口 | **有**。`opencode run [message..]`，`--format json` 输出 raw JSON 事件，`--model provider/model` 选模型 |
| 事件流长什么样 | 采到**真实样本**：`step_start` / `text`（回复文本在 `part.text`）/ `tool_use`（`part.tool` + `part.state`）/ `step_finish`（用量在 `part.tokens`、花费在 `part.cost`）。**与 codex 的 `turn.completed` / `item.completed` 完全不同**——这就是通用通道拿不到数据的原因 |
| 能不能不用 key 跑 | **能**（7 个免费模型，如 `opencode/mimo-v2.6-flash-free`），**但生产不用它**：它只是"没有 key 时也能采样本"的手段 |
| **执行体会读 stdin（关键坑）** | `opencode run` 在 **stdin 不是 EOF** 时会**挂住不返回**（零输出、零退出码）。实测：同一台机器、同一个模型，不重定向 stdin 就挂住，`< /dev/null` 就出结果。**内核已修**：非交互进程的 stdin 改成 `/dev/null`（`machine_service.py`），systemd 单元再加 `StandardInput=null` 兜底。**v2 初稿把这误判成"免费模型不可靠"**——那是错的，已更正 |
| 坏配置怎么失败 | **未验证**。用不存在的模型名试过一次是挂住，但那次**没重定向 stdin**，所以挂住的原因可能仍然是 stdin。部署仍按"可能挂住"设防（`max_seconds` + 单元 `TimeoutStopSec`），但**不要**当成已证结论 |
| 真 key 能不能通（百炼） | **通**：`deepseek/deepseek-v4.1-flash` 实跑返回 `OK`，`step_finish` 带用量（input 7339 / output 2）。**以服务用户身份也通**（`/home/synapforge/.opencode/bin/opencode`）。写法说明：opencode 的 `-m` 是 `<provider 别名>/<模型 id>`，**前半段是我们在 `opencode.json` 里自己起的别名**（当前别名就叫 `deepseek`，指向百炼的 OpenAI 兼容端点），不是厂商要求的前缀 |
| 内核能在 Ubuntu 22.04 上跑吗 | **要 Python 3.12**：内核用了 `from datetime import UTC`（3.11+ 才有），系统自带的 3.10 会在 import 就失败。**v2 初稿的"3.10 可跑"判断是错的**（我的静态检查漏了这种导入写法）→ 已用 deadsnakes 装 3.12.14（与平台机同版本） |
| 出网 | 正常（`opencode.ai` / `api.opencode.ai` / `models.dev` / npm registry 均 200；平台 `https://synapforge.top` 从该机可达 HTTP/2 200） |

### 3.2 仍然是门禁（要等你给值）

**Gate B：opencode 能不能接阿里百炼 + deepseek-4.1-flash。** 文档上这条路是通的——
`opencode.json` 里配 `@ai-sdk/openai-compatible` + `options.baseURL` + `apiKey: "{env:XXX}"` + `models` 即可
（我已核对官网 providers 文档）。**但没有真实 key 就没法验**，而免费模型已证明不可靠（§3.1）。

我需要你给三个值：**`base_url`、`api_key`、精确模型 id**（是 `deepseek-v4.1-flash` 还是带版本后缀，
以及它在百炼上的确切写法）。它们只写在服务器 `0600` 的 env 文件里，由 systemd `EnvironmentFile=` 读，
**不进平台数据库、不进代码库**。

**待实测（拿到 key 后第一件事）**：`--auto` 在无人值守下的真实行为。
`opencode run --help` 写明它是"auto-approve permissions that are not explicitly denied (dangerous!)"，
但**没有 `--auto` 时会不会卡在审批上，我这次没验成**（免费模型随后全部挂住，样本没采到）。
所以模板先带 `--auto`（见 §6.0），P1 时要专门验一次"不带 `--auto` 会怎样"。

### 3.3 执行体选型（v2）

| | opencode（**本次选定**） | codex CLI（退路/第二执行体） |
| --- | --- | --- |
| Linux | 官方安装器，独立二进制 | 有官方版 |
| 非交互 | `opencode run --format json` | `codex exec --json` |
| 平台适配 | **本次新增**（事件 + 用量，见 §6.0） | 原生（JSONL 事件、用量解析、能力卡） |
| 第三方模型 | `opencode.json` 配 OpenAI 兼容 provider（文档直白） | 需配自定义 provider（未验） |
| 选用理由 | 开源、安装最省事、你已经熟悉；平台适配已补齐 | 平台支持最完整，适合当**第二执行体**做对比 |

claude-code 仍在"只做安装探测、未实现语义适配"的状态，不选。

---

## 4 目标架构

```
┌─────────────────────────────┐         HTTPS 443（只出站）        ┌────────────────────────────────────────┐
│  平台服务器（现有，156.…143）│◄───────────────────────────────────│  云端执行体服务器（154.219.99.75）      │
│  nginx(TLS) + FastAPI + Next │                                    │  Ubuntu 22.04 · 4C8G · 40G             │
│  SQLite + /downloads/        │──── 设备注册/心跳/领取/上报 ──────►│  map-agent@<实例>.service（内核实例）   │
│  （平台运维只在这台）         │                                    │   ├─ 状态目录 /var/lib/synapforge/…    │
└─────────────────────────────┘                                    │   ├─ workspace /srv/synapforge/…       │
                                                                    │   └─ 执行体 CLI：opencode run …        │
        ▲                                                           └────────────────────────────────────────┘
        │ 浏览器 / 桌面端                                              每实例：独立 Ed25519 密钥 + 独立 device_id
        └── 人在网页/桌面端派活 → 云端执行体领取并执行 → 成果物回平台 → 系统通知
```

**关键点**

- 云端 Agent 与平台之间**只有 HTTPS 出站**：它不需要 SSH 到平台，也不持有任何平台运维凭据。
- **一个内核实例 = 一个项目 + 一个执行体**（内核的身份解析是单项目的）：要跑多个执行体/项目就起多个实例，各自 `MAP_STATE_DIR`。
- 平台侧不新增"云端执行体"概念：它就是普通设备（`/devices` 显示 `linux`）。
- **事件流不进人眼**：`--format json` 的 stdout 由内核解析成"人看的摘要 + 过程事件 + 用量"，原始 JSONL 仍留在 Run 台账里备查（与 codex 同一约定）。

---

## 5 服务器（已到手，实测事实）

| 项 | 实测值 |
| --- | --- |
| 地址 / 端口 | `154.219.99.75:22`，root + 密码登录（**是全新机器**，2026-09-23 00:37 UTC 开通） |
| 系统 | Ubuntu 22.04 LTS，内核 5.15.0-30 |
| 规格 | **4 vCPU / 7951 MB 内存 / 40G 盘**（已用 2.6G）→ 与 v1 推荐的 4C8G 一致，够 3–5 个内核实例 |
| 已装（部署后） | `git`、`curl`、**Python 3.12.14**（deadsnakes；系统自带 3.10 跑不了内核）、`/opt/synapforge-agent/venv`（cryptography 50.0.1 + websockets）、**opencode 1.18.32**（装在 `synapforge` 家目录 `~/.opencode/bin`，**不需要 Node**）、`/etc/systemd/system/map-agent@.service`（已装、未启动） |
| **没有 swap** | `Swap: 0` → 建议加 2–4G swap 或给每个单元设 `MemoryMax=`，避免执行体峰值把机器打死 |

**容量估算（不变）**：内核自身 ~80–120 MB RSS；执行体每次运行 200–500 MB；内核任务循环串行（一次一个任务），
**单实例峰值 ~0.5–1 GB**。

**部署方式（不变）**：服务器上**跑源码 + venv**，不要交叉打 Linux 包（PyInstaller 产物是平台相关的）。
Python 用系统自带的 3.10 即可（§3.1 已静态验证）。

**安全加固（新增，建议在 P1 前一并做）**：当前是 root + 密码直登。建议按这个顺序改：
①你先加好你的 SSH 公钥并确认能登录 → ②`PermitRootLogin prohibit-password` + `PasswordAuthentication no` →
③开 fail2ban。**顺序不能反**（先关密码登录再发现密钥没生效 = 把机器锁在门外）。
注意：opencode 是**以 opencode 自己的账号/订阅身份**调模型（若用它的登录态），而百炼 key 是给 provider 用的 env 变量——
两者都不该给 root 权限运行，单元里用专用的 `synapforge` 用户（见 §6.2）。

---

## 6 第 0 步代码改造（零风险，本机可测，不碰线上）

### 6.0 opencode 协议适配（方案 B）——**已完成**

| 落点 | 内容 |
| --- | --- |
| `apps/agent/opencode_executor.py`（新） | `parse_opencode_jsonl`（回复文本 / 工具与文件动作 / 跨 step 累加的用量与花费 / 坏行计数）、`summarize_opencode_result`（答案在最前、诊断在后；**成功判定以退出码为准**——错误事件形状没采到样本，不假装能判）、`OPENCODE_WORKER_TEMPLATE = ("opencode","run","--format","json","--auto","{prompt}")` |
| `apps/agent/executor_events.py` | 新增 `event_protocol`（`codex` / `opencode` / `none`，默认 codex 保证零回归）；opencode 分支把 `text`→`agent.message`、`tool_use`→`tool.completed`/`file.changed`（按 `callID` 去重、只报完成态）、`step_finish`→用量；用量出处标 `opencode-jsonl` |
| `apps/agent/agentd.py` | `_event_protocol(task)`：**显式声明 `resource_policy.worker_events` > 命令模板首词命中已知执行体（`opencode`）> `none`**；执行结束走 `summarize_opencode_result`，原始 JSONL 仍进 Run 台账 |
| `apps/api/app/store.py` | `worker_events` 取值校验（`codex`/`opencode`/`none`，且必须与 `worker_executor` 匹配），把"配了但拿不到"挡在入口 |
| `apps/agent/agent_inventory.py` | `opencode` 进通用 CLI 探测清单（本机 Agent 面板能看到它） |
| 测试 | Agent **327 项**（304 → +23，9 项既有跳过）；API **515 项**（仅 2 项既有 LaTeX 环境失败）。样本是**服务器实跑原文**，不是手写近似值 |

**顺手修掉一个真 bug（与本次相关，必须说清）**：`ExecutorEventReporter` 在常驻体里是**进程级复用**的，
而用量计数器只在构造时归零——于是**同一台设备上第二个任务会报出"第一个 + 第二个"的 token**，
COST-1 的 token 预算判定会跟着虚高。现在 `started()` 每次执行先 `reset_for_run()`（缓冲、用量、事件计数、
工具去重集合一起清），并有测试固定住这个行为。

**没做的（留到 P1，别当成已完成）**：前端任务弹窗里加 opencode 预设（现在只有 codex / 通用 CLI / 声明式命令三个选项）；
部署脚本与 systemd 单元（§6.2）；真实端到端跑（等百炼 key）。

### 6.1 文件凭据后端（唯一真正的代码缺口）

- 新增 `FileCredentialStore`：目录 `<state_dir>/credentials/`，每目标一个文件（`0600`，父目录 `0700`），
  写入用"临时文件 + `os.replace`"（避免半截文件），读取校验权限（权限过宽时**拒绝**并提示，而不是默默用）。
- 工厂改为显式选择 + 自动兜底：`create_credential_store(backend="auto")` → Windows 走凭据管理器，POSIX 走文件后端；
  CLI 增加 `--credential-backend {auto,windows,file,memory}`（默认 auto）。
- **与既有硬约束的关系**：`桌面端不得把设备/项目令牌持久化到 Windows 凭据管理器之外` 这条约束的**意图**是
  "别在 Windows 上到处撒密钥"。headless POSIX 没有凭据管理器，等价物就是"0600 文件 + 0700 目录 + 专用非 root 用户"，
  所以这是**限定例外**：Windows 行为不变；文件后端只在非 Windows 生效（或在 Windows 上必须显式指定）。
- 测试：权限校验（0644 必须被拒）、往返读写、`os.replace` 原子性、`auto` 在 Windows 上仍选凭据管理器。

### 6.2 运行封装（新目录 `deploy/cloud-agent/`）

- `install-cloud-agent.sh`：建用户 `synapforge`（非 root、无 sudo）→ 部署源码到 `/opt/synapforge-agent/`
  （tar + venv，pip 装 `cryptography websockets`）→ 建 `/var/lib/synapforge-agent/<instance>`（0700）与
  `/srv/synapforge/<instance>`（0750）→ 装 opencode（官方安装器，落到 `synapforge` 家目录，**不用 root**）。
- `map-agent@.service`（systemd **模板**单元，`%i` = 实例名）：
  `User=synapforge`、`EnvironmentFile=/etc/synapforge-agent/%i.env`（0600，模型 key 与 `--credential-backend file`）、
  `ExecStart=… agentd daemon-run --state-dir /var/lib/synapforge-agent/%i --workspace /srv/synapforge/%i`、
  加固：`NoNewPrivileges=yes`、`PrivateTmp=yes`、`ProtectSystem=strict`、`ProtectHome=no`（opencode 装在
  `synapforge` 家目录里，**这里与 v1 的 `ProtectHome=yes` 有冲突，以实际安装位置为准**）、
  `ReadWritePaths=/var/lib/synapforge-agent/%i /srv/synapforge/%i`、`MemoryMax=`、`CPUQuota=`、
  `Restart=on-failure`、`RestartSec=5`、**`TimeoutStopSec=`**（opencode 会挂住，得能被打死）。
- `pair-cloud-agent.sh`：平台「设备与接入」拿配对码 → `agentd keygen` → `agentd device-register` → 用项目授权串写 `worker.json`。
- 防火墙：只放行出站 443（`synapforge.top` 与模型 API 域名），其余拒绝；并写明**平台 `network_policy` 在 Linux 上未强制**（eBPF/LSM 未做），隔离靠宿主机。

### 6.3 验证脚本 `scripts/deploy/_cloud_agent_verify.py`（✅ 已交付，2026-09-23 线上跑通）

在**平台机**上跑（与 `_cost1_verify.py` 同一模式：需要 store 级断言与清理）：
`cd /opt/math-agent-platform && PYTHONPATH=apps/api:. venv/bin/python /tmp/_cloud_agent_verify.py`

断言（跑完自清理）：

1. 设备在线且 `platform=linux`（`/api/devices`）；
2. 该执行体的**能力卡**出现在 `/api/team/capabilities`；
3. 专用项目里建一条**极小任务** → 被领取 → Run 完成 → **产出成果物**（内容非空）→ `/runs` 有**用量**
   （应为 `opencode-jsonl` 出处）；
4. **过程事件**：聊天区能看到该 Run 的 `agent.message`/`tool.completed` 卡片（这是方案 B 的直接验收点）；
5. **失败关闭**：把模型 key 临时改错 → 必须在 `budget.max_attempts` 内失败并留可读错误，**不许无限重试或静默挂住**
   （实测 opencode 遇到坏配置会挂住，所以这一条靠 `max_seconds` 兜底，必须真验）；
6. 清理：删掉验证任务/成果物/运行记录，删掉临时错误配置。

---

## 7 安全模型（把你第 3 条落成硬边界）

| 边界 | 做法 |
| --- | --- |
| **不碰平台运维** | 该机器上**只有**设备令牌 + 项目能力令牌（能力范围限定）；**没有**平台会话/管理员令牌、**没有**平台机 SSH 私钥、**没有**部署脚本 |
| **不碰宿主机运维** | 专用非 root 用户、无 sudo、`ProtectSystem=strict`、workspace 与状态目录是唯一可写路径；执行体可执行文件与 workspace 根由内核 `WorkspacePolicy` 白名单约束 |
| **权限最小化** | 只授权**一个专用项目**；初始 `task_mode=manual`（或 hybrid），**不 auto**——先人工派，信任后再放开 |
| **配额** | 任务预算默认值：`max_seconds`（如 600）、`max_attempts`（如 2）；需要时给 `max_tokens`（COST-1 已可判定，opencode 的用量从本轮起可采）；实例级 `MemoryMax`/`CPUQuota` |
| **紧急制动** | 平台侧可暂停/紧急停止；必要时 `systemctl stop map-agent@*` |
| **模型凭据** | 百炼 key 写在 **opencode 自己的配置**里：`/home/synapforge/.config/opencode/opencode.json`（0600、属主 synapforge），由 `deploy/cloud-agent/configure-opencode-provider.sh --api-key-file` 写入（key 不出现在命令行）。**为什么不是 systemd env**：内核给执行体传环境变量是**按白名单**的（`agentd._EXECUTOR_ENVIRONMENT_KEYS`：HOME/PATH/TMP/TEMP…），模型厂商的 key 不在其中——平台不该知道模型是谁家的。不进平台库、不进代码库 |
| **SSH** | 见 §5 加固顺序（密钥登录 → 关密码登录 → fail2ban） |
| **诚实标注** | 平台 `network_policy` 只是**声明**；opencode 遇坏配置会**挂住而不是报错**——所以"超时"是安全网，不是可选项 |

---

## 8 分期与验收

| 期 | 内容 | 验收（必须实测） |
| --- | --- | --- |
| **P0 代码准备**（不花钱，本机可测） | ✅ **opencode 适配器**（Agent 342 / API 515 全绿）✅ **stdin 隐患修复**（`machine_service.py` + 测试）✅ **`FileCredentialStore`**（POSIX 0600/0700 + 工厂 `auto` + 测试）✅ **`deploy/cloud-agent/`**（install / configure-opencode-provider / pair / prepare-platform-side / systemd 单元 / README） | ✅ 已达成（脚本语法检查通过、内核套件全绿） |
| **P1 单实例跑通** | ✅ **完成（2026-09-23）**：机器装好（Python 3.12.14 + venv + opencode 1.18.32 + 百炼 provider）、配对完成（`device-cloud-01` / `agent-cloud-01`，platform=linux，状态 active）、只授权「云端智能体」一个项目、`map-agent@cloud` 已 enable 且 0 次重启；真跑过多条任务（每条 11–16 秒、4000–7600 tokens、产出成果物） | ✅ `_cloud_agent_verify.py` **六项全 PASS**（设备在线 / 真跑完成 / 用量出处 `opencode-jsonl` / 耗时出自执行体 / 过程事件到达 / 有成果物）+ 自清理通过 |
| **P2 多实例 + 演示真跑** | 起 2 个实例（opencode + codex 各一）；把演示项目从静态剧本改成**真跑**（剧本保留兜底） | 平台「推荐执行体」出现多个候选且成功率有样本；演示项目任务真被执行、成果物可下载 |
| **P3 网页派活桥** | `/ask` 或对话里一键"派给云端 Agent"（复用 `/task` 与自动派单） | 网页里说一句 → 云端执行 → 成果物回成果空间 + 桌面通知弹出 |
| **P4（可选）** | 多执行体同竞技 + "云端执行体花费"只读视图（`opencode` 的 `cost` 字段已在解析里，届时接 UI） | 花费视图与 `/runs` 对得上 |

---

## 9 成本与观测

- **唯一的真实成本是模型 token**（百炼按量计费）。已有控费手段：任务 `budget.max_tokens`（超出在门禁留痕）、
  `max_seconds`/`max_attempts`、`/runs` 的用量与出处。
- opencode 每次 step 还报 `cost`（免费模型为 0）。这个值已解析进摘要，但**平台的用量契约（`RunUsage`）暂时不收**——
  要接到 UI 需要动契约与前端，放到 P4，不假装现在有花费视图。
- **免费模型不作为任何生产路径**（实测不可靠，§3.1）；它只适合采样本、看协议。
- 服务器成本：4C8G 月费按你的供应商账单，内核本身不耗算力，耗的是执行体那条命令。

---

## 10 待确认（部署已完成，剩下的是运维与下一期）

1. ✅ 百炼 `base_url` / `api_key` / 模型 id —— 已配（`deepseek/deepseek-v4.1-flash`）
2. ✅ 专用项目名 —— 「**云端智能体**」（id `06f9df2b-264d-4d7d-9ea7-0bff0bbb6c2c`），只授权给这台设备
3. ✅ 配对 —— 已完成（设备 `device-cloud-01`、Agent `agent-cloud-01`、`platform=linux`、active）
4. ⏳（安全，可选）**SSH 改密钥登录**：该机现在仍是 root 密码直登。要改需要你给公钥，**顺序不能反**（先验密钥能登，再关密码登录）。
5. ⏳（P2 决定）要不要**同时上 codex 作第二执行体**（多执行体对比；平台对 codex 支持最完整）。
6. ⏳（P2/P3）演示项目从静态剧本改成真跑、`/ask` 派活桥。

---

## 11 明确不做

- **平台运维**：云端执行体不接触平台机的任何运维面。
- **宿主机运维**：运行期不装包、不改系统配置（装包与改配置只在部署时做）。
- **本地模型托管**：不在该机器上跑本地大模型，模型一律走百炼 API。
- **默认全自动**：专用项目初始不设 `auto`，先人工派单。
- **Windows 交叉打 Linux 包**：服务器上跑源码 + venv。
- **用免费模型当生产执行体**（实测不可靠）。
- **假装能判定 opencode 的错误事件**（形状未采到样本；成功判定只用退出码 + 外层超时）。

---

## 附：v2 修订的证据链

- 适配器实现与测试：`apps/agent/opencode_executor.py`、`apps/agent/executor_events.py`、`apps/agent/agentd.py`、
  `apps/agent/test_opencode_executor.py`
- 真实样本原文（服务器实跑输出，逐字）：`docs/handoffs/CLOUD_1_OPENCODE_ADAPTER_HANDOFF.md`
- 状态登记：`docs/IMPLEMENTATION_STATUS.md`（CLOUD-1 段落）