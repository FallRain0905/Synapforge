# 用 Codex 作为平台执行体

> 状态：**已打通并实测**（2026-09-16）
>
> 适用：本机 ChatGPT 桌面端自带的 Codex CLI，或独立安装的 Codex CLI

---

## 1. 一句话说明

任务在 `resource_policy` 里声明 `worker_executor = "codex"` 与 `worker_prompt`，平台就会把该任务交给本机的 Codex CLI 去执行：
平台负责派活、租约、结果与运行台账；Codex 负责思考与动手。执行完的**最终回复、工具/文件事件计数、用量**都会写进 Run 记录。

## 2. 先找到本机 CLI

ChatGPT 桌面端把 Codex CLI 一起打包，并且**不能**直接从 `C:\Program Files\WindowsApps\...` 运行（ACL 拒绝）。可用的入口有两类：

| 来源 | 路径 | 说明 |
| --- | --- | --- |
| 桌面端解包目录（推荐） | `%LOCALAPPDATA%\OpenAI\Codex\bin\<hash>\codex.exe` | 无 ACL 限制；`<hash>` 随版本变化，取最新即可 |
| 应用执行别名 | `%LOCALAPPDATA%\Microsoft\WindowsApps\codex.exe` | 商店包注册的 ExecutionAlias（若已注册） |
| 独立安装 | PATH 上的 `codex` | 例如 npm 全局安装 |
| 显式指定 | `CODEX_CLI_PATH` 环境变量 / `--codex-path` | 优先级最高 |

**平台侧的探测顺序**（`apps/agent/codex_executor.py` 的 `detect_codex_cli`）：显式参数 → `CODEX_CLI_PATH` → 桌面端解包目录（取最新）→ WindowsApps 别名 → PATH。

自检：

```powershell
# 让平台探测并打印结果
.\scripts\connect-agent.ps1 -Url http://127.0.0.1:8000 -AgentName "我的工作站" -Pairing <配对串> -Codex
# 输出示例：
#   已找到 Codex CLI：C:\Users\<你>\AppData\Local\OpenAI\Codex\bin\<hash>\codex.exe
#   版本：codex-cli 0.154.0-alpha.6.2
```

也可以在 worker 启动参数里直接给：`--codex-path "<路径>"`。首次带 `--grant` 跑 worker 时该路径会被记进 `worker.json`，之后免填。

## 3. 怎么用

### 3.1 建任务（界面或 API）

```json
{
  "title": "用 Codex 读 README 并给出结论",
  "description": "读取 README.md 的第一行标题",
  "stage": "coding",
  "priority": "critical",
  "resource_policy": {
    "worker_executor": "codex",
    "worker_prompt": "读取当前工作目录下的 README.md，只回复它的第一行标题文本。"
  }
}
```

- `worker_prompt` 省略时，平台用「任务标题 + 说明 + 完成标准」拼提示词；
- 也可以不用 `worker_executor`，直接把 Codex 可执行文件写进 `worker_command`（等价，但不会被 Codex 专用解析与安全校验覆盖）。

### 3.2 跑 Agent

```powershell
# 接入（一次）
.\scripts\connect-agent.ps1 -Url http://127.0.0.1:8000 -AgentName "我的工作站" -Pairing <配对串> -Codex
# 在设置页把该设备「授权到项目」，拿到 worker 命令后执行（可常驻）
python -X utf8 apps/agent/agentd.py --url http://127.0.0.1:8000 worker-run --grant <授权串>
```

写盘类任务需要放宽沙箱（默认只读）：

```powershell
... worker-run --grant <授权串> --codex-sandbox workspace-write
```

### 3.3 看结果

- `/tasks`：任务状态（成功即 `APPROVED`，`requires_review=false` 时）；
- `/runs`：Run 台账，摘要形如
  `codex exit=0 run=run-… events=6 tools=1 files=0 | reply: <Codex 的最终回复>`
- stdout 一列保留**原始 JSONL**，便于排查事件级问题。

### 3.3b 回答在摘要里的位置（DP-2 补强）

摘要的**第一段永远是回答**，`---` 之后才是执行诊断：

```text
Agent 的回答全文（可多段、保留换行）
---
codex exit=0 run=run-xxx events=30 tools=9 files=0 | warnings: Reconnecting... 2/5
```

为什么这样排：界面（任务详情的「最终回答」、`/runs` 的回答预览与「复制回答」按钮）直接取
第一段展示；夹在诊断中间时，任何截断都会切掉半句话。字段上限（Agent 侧统一截断，平台侧只存不截）：

| 字段 | 上限 | 用途 |
| --- | --- | --- |
| `summary` | 8000 字符 | 回答 + 诊断；任务结果上报用 4000 |
| `stdout` | 末尾 40000 字符 | 原始 JSONL，回溯"它到底做了什么" |
| `stderr` | 末尾 8000 字符 | 执行体自己的诊断信息 |

平台侧查看位置：`/runs` →「详情」（回答全文、执行信息、信息边界、原始输出折叠）、任务详情 →「最终回答」。
接口：`GET /api/runs/{run_id}`（按项目做成员授权，越权 403）。

### 3.4 成功判定：瞬断重试不算失败（DP-2 追加）

`summarize_codex_result` 的规则（`apps/agent/codex_executor.py`，可单测）：

| 情况 | 判定 | 摘要 |
| --- | --- | --- |
| 退出码 0，无错误事件 | **成功** | `reply: …` |
| 退出码 0，只有瞬断提示（`Reconnecting...`、`stream disconnected before completion`、`stream closed before response.completed`） | **成功** | `reply: … \| warnings: …` |
| 退出码 0，有致命错误（`turn.failed`、其它 `error` 事件） | 失败 | `errors: …` |
| 退出码非 0 | 失败 | `exit=N` |

为什么这样分：Codex 自己会重试流中断并最终给出答复，把这类提示当成失败会让"每次网络抖动 = 任务失败"，
而完全忽略它们又会掩盖真问题——所以**分开记录**：warnings 照常出现在台账里，但不翻成败。

### 3.5 执行过程上报（平台侧实时反馈）

平台是拉取模型：任务由内核领取，网页里没有"开始"按钮。执行期间平台能看到什么，取决于内核
**上报了什么**——DP-2 之前只有"领取 → 结果"两帧，中间过程完全不可见。

现在 Codex 的 JSONL 事件流会被翻译成平台事件（走 Gateway 的持久化 `agent.event` 队列，
断网时本地补传，不丢）：

| 执行体事件 | 平台事件 | 内容 |
| --- | --- | --- |
| 起进程 | `agent.process.started` | 执行体（codex）与前 3 段命令 |
| `item.completed(agent_message)` | `agent.agent.message` | 执行体的回复片段（截断 500 字） |
| `item.completed(tool_call…)` | `agent.tool.completed` | 工具/命令名 |
| `item.completed(file_change…)` | `agent.file.changed` | 被改动的文件路径 |
| 进程退出 | `agent.process.exited` | 退出状态、结果摘要、以及**节流统计**（压掉了多少条、坏行多少） |

节流（`apps/agent/executor_events.py`，默认值可调）：过程事件每条间隔 ≥ 1.5 秒、整轮 ≤ 40 条，
起停两条不受限；被压掉的条数如实写进 `process.exited`，不假装都报上去了。

**终态不走这条路**：`run.completed`/`run.failed` 经 Gateway 会完成 Run，而完成 Run 是 HTTP
`/api/runs/{id}/complete` 的职责，两条路径同时发会把同一个 Run 完成两次。

平台侧可见位置：`/runs`（运行中显示"已运行 N 秒 + 最新一条过程"）、任务详情（"执行过程"与"执行结果"）、
`/timeline`（事件流按天分组）。

## 4. 安全约束（平台强制，不是建议）

| 约束 | 实现位置 | 说明 |
| --- | --- | --- |
| 拒绝危险沙箱/审批绕过 | `cli_adapters.CodexAdapter._reject_unsafe_flags` | `--dangerously-bypass-approvals-and-sandbox`、`--dangerously-bypass-hook-trust`、`--approve-for-me` 一律拒绝 |
| 拒绝 `danger-full-access` | `codex_executor.build_codex_command` | 只有 `read-only` / `workspace-write` 可选 |
| 拒绝路径逃逸 | 同上（adapter 内） | `--cd`、`-C`、`--add-dir`、`--image` 会被拒 |
| 拒绝配置覆盖 | 同上（adapter 内） | `--config`、`-c`、`--profile`、`-p` 会被拒 |
| 工作区白名单 | `runner.WorkspacePolicy` | 命令必须在 `allowed_roots` 内 |
| **环境变量显式白名单** | `agentd._executor_environment` | 平台禁止继承父进程环境；只传白名单内存在的变量（`USERPROFILE`/`HOME`/`APPDATA`/`LOCALAPPDATA`/`CODEX_HOME`/`PATH`/`TEMP`/`SYSTEMROOT` 等）。**缺 `USERPROFILE` 时 Codex 找不到 `~/.codex`，会表现为"连不上服务商"** |
| 提示词位置 | `build_codex_command` | 提示词永远是最后一个参数，附加 flag 一律插在它之前 |

## 5. 实测记录（2026-09-16）

| 场景 | 命令要点 | 结果 |
| --- | --- | --- |
| 纯对话 | `worker_executor=codex`，`worker_prompt="Reply with exactly: PLATFORM_CODEX_OK"` | `codex exit=0 events=4 tools=0`、reply 精确匹配；任务 **APPROVED**、Run **SUCCEEDED** |
| 需要工具调用 | 提示词要求读 README 并回复首行；`--codex-sandbox workspace-write` | `tools=1`、reply = `# Math Agent Platform`（与文件首行一致，语义可验证） |
| **桌面端常驻体自动领取** | 装/配好后什么都不做；平台建 `worker_executor=codex` 任务 | 内核约 3–10 秒领取，约 30–60 秒完成；任务进 `WAITING_REVIEW`、Run `SUCCEEDED`，摘要含 reply（详见 `docs/handoffs/DP_2_*HANDOFF.md`） |

CLI 版本：`codex-cli 0.154.0-alpha.6.2`（ChatGPT 桌面端 26.908 解包）。
本机 Codex 配置走自定义 provider（`~/.codex/config.toml` 的 `model_provider = "custom"`），**不需要 ChatGPT 登录**也能无头运行。

## 6. 排障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `codex_cli_not_found:…` | 未装桌面端/CLI，或路径不在探测顺序内 | 装 ChatGPT 桌面端；或 `--codex-path` 显式指定 |
| Run 摘要里出现 `warnings: Reconnecting...` | Codex 流中断后自己重试并成功（**任务仍算成功**） | 偶发可忽略；频繁出现则检查网络与该 provider 的稳定性 |
| Run 摘要里出现 `errors: Reconnecting...` 且任务失败 | Codex 连不上它的服务商且重试未成功 | 检查 `~/.codex/config.toml` 的 provider/base_url/token；确认平台传了 `USERPROFILE`（见 §4） |
| `codex_requires_headless_profile` | 用了非 HEADLESS 的执行 profile | worker 走的是 HEADLESS，正常不会遇到 |
| `codex_unsafe_approval_or_sandbox_flag` | 命令里带了危险 flag | 去掉；需要写盘就用 `--codex-sandbox workspace-write` |
| 任务失败但没信息 | Codex 输出无法解析 | Run 的 stdout 保留原始 JSONL；`parse_codex_jsonl` 会把坏行计入 `unparsed` |

## 7. 相关代码

- `apps/agent/codex_executor.py`：探测、JSONL 解析、命令组装（含安全校验）
- `apps/agent/cli_adapters.py`：`CodexAdapter`（平台侧协议适配与拒绝清单）
- `apps/agent/agentd.py`：`worker-run` 的 codex 分支、环境白名单、`--codex-path` / `--codex-sandbox`
- `apps/agent/test_codex_executor.py`：18 项契约测试（解析 / 成功判定 / 探测 / 安全 / 分支）
- `scripts/connect-agent.ps1 -Codex`：一键探测与说明