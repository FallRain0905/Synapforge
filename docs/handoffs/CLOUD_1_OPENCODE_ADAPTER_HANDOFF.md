# CLOUD-1 交接：opencode 执行体适配（方案 B）· 代码完成，未上线

> 2026-09-23 · 上游 `docs/CLOUD_1_CLOUD_AGENT_PLAN.md`（v2 修订版）· 状态：**内核侧已完成并测试，未部署、未真跑**
> 关联：`docs/handoffs/COST_1_USAGE_BUDGET_HANDOFF.md`（用量与出处口径）、`docs/handoffs/W9_DESKTOP_RELEASE_CLI_EXECUTORS_HANDOFF.md`（通用 CLI 通道）
> 目的：让**云端执行体那台机器用 opencode**时，过程事件与用量都能进平台——而不是只有一行 stdout

## 1 解决了什么

原计划（CLOUD-1 v1）说"换执行体 = 平台零改动"，实测发现只对"能不能跑起来"成立：

| 问题 | 实测事实 | 后果 |
| --- | --- | --- |
| stdout 不是结果 | `opencode run --format json` 输出的是**事件流**（每行一个 JSON） | 直接当结果 → Run 摘要与成果物里塞进一整串 JSON |
| 通用通道不解析事件 | 解析器是照 codex 的事件形状写的（`turn.completed` / `item.completed`） | 聊天区**看不到** Agent 的实时进展（agent.message / tool / file 卡片全没有） |
| 用量拿不到 | opencode 的用量在 `step_finish.part.tokens`，不在 codex 那类事件里 | COST-1 的 **token 预算判定失效**（回到"无数据"） |

本期（方案 B）给 opencode 加了协议适配，补齐这三件事，且**不改变通用 CLI 通道的既有语义**：
workbuddy / zcode 等仍按"stdout 即结果"处理。

| 环节 | 做法 |
| --- | --- |
| 协议选择 | `agentd._event_protocol(task)`：**显式声明 `resource_policy.worker_events`** > 命令模板首词命中已知执行体（`opencode`）> `none`（不解析）。声明与推断不一致时以**声明**为准 |
| 过程事件 | `text` → `agent.message`、`tool_use` → `tool.completed` / `file.changed`（`write`/`edit` 算文件动作）、按 `callID` 去重且**只报完成态** |
| 用量 | `step_finish.part.tokens` 跨 step 累加 → `input_tokens`/`output_tokens`/`total_tokens`/`turns`，出处标 **`opencode-jsonl`**（与 codex 的 `codex-jsonl` 区分开） |
| 摘要 | 答案在最前、诊断在后；诊断含 events / tools / files / tokens（含出处）/ cost / 坏行数 |
| 成功判定 | **只看退出码**——错误事件形状没采到样本（见 §3），不假装能判 |
| 原生 JSONL | 仍然原样进 Run 台账（与 codex 同一约定：排查时能看原始事件） |

## 2 落点

**Agent 内核**
- `apps/agent/opencode_executor.py`（新）：`parse_opencode_jsonl`、`summarize_opencode_result`、
  `OPENCODE_WORKER_TEMPLATE = ("opencode","run","--format","json","--auto","{prompt}")`。
- `apps/agent/executor_events.py`：新增 `event_protocol`（`codex` | `opencode` | `none`，**默认 codex** 保证零回归）；
  opencode 分支见上表；`usage()` 的 `source` 按协议给。
- `apps/agent/agentd.py`：`_event_protocol()` + `_KNOWN_CLI_PROTOCOLS`；执行结束按协议走对应摘要；
  `reporter.started(..., protocol=...)`。
- `apps/agent/agent_inventory.py`：`opencode` 进 `GENERIC_CLI_EXECUTABLES`（面板能看见"装没装"）。

**API**
- `apps/api/app/store.py`：`_validated_resource_policy` 新增 `worker_events` 校验
  （取值 `codex`/`opencode`/`none`；`codex` 必须配 codex 执行体、`opencode` 必须配 `cli`）——
  把"配了但拿不到事件"挡在入口，而不是让它在 Agent 侧静默降级。

**测试**
- `apps/agent/test_opencode_executor.py`（新，23 项）：解析 / 摘要 / 上报 / 协议选择 / API 校验五组，
  样本是**服务器实跑原文**（§3），不是手写近似值。

## 3 顺手修掉的一个真 bug（与本期强相关）

`ExecutorEventReporter` 在常驻体（`daemon-run`）里是**进程级复用**的：`_build_task_loop` 建一个 reporter，
每个任务只更新 `task_id`/`run_id`——而用量计数器**只在构造时归零**。
于是同一台设备上的第二个任务会报出"第一个 + 第二个"的累计 token，第三个更多：
**COST-1 的 token 预算判定会跟着虚高**（COST-1 的测试用的是固定返回值的 provider，所以没照出来）。

修法：`started()` 每次执行先 `reset_for_run()`——缓冲、用量、事件计数、节流时间戳、工具去重集合一起清。
这样 `process.exited` 的统计也只反映本次执行。测试：`test_started_clears_the_previous_run`。

## 4 服务器实测证据（opencode 1.18.32，2026-09-23）

新机器 `154.219.99.75`（Ubuntu 22.04、4C8G、40G、**全新**，无 node/npm、Python 3.10.4）：

```bash
curl -fsSL https://opencode.ai/install | bash      # → /root/.opencode/bin/opencode（185 MB 独立二进制，不用 Node）
opencode --version                                  # 1.18.32
opencode run --help                                 # --format default|json、--auto、-m provider/model、--attach、--session…
opencode models                                     # 7 个免费模型（opencode/…-free），无需 key
```

**样本一（逐字原文）**：`opencode run --format json --auto -m opencode/mimo-v2.6-flash-free "Reply with exactly: OK"`
（3 行，`step_start` / `text` / `step_finish`）——测试里对应 `SAMPLE_TEXT_ONLY`。

**样本二（逐字原文）**：让它写文件再跑 shell（7 行，含两次 `tool_use`、两次 `step_finish`）——对应 `SAMPLE_WITH_TOOLS`，
其中工具事件的形状是：`{"type":"tool_use","part":{"type":"tool","tool":"write","callID":"call_…","state":{"status":"completed","input":{"filePath":"…"},"output":"Wrote file successfully.","title":"…","time":{…}}}}`。
两份样本的完整原文在测试文件 `apps/agent/test_opencode_executor.py` 顶部常量里（逐字，未改写）。

**真凶不是模型，是 stdin（这条推翻了本交接初稿的判断）**：`opencode run` 在 **stdin 不是 EOF** 时会
**挂住不返回**——零输出、零退出码，日志停在 `init`，看起来就像"模型/网络坏了"。证据链：同一台机器、同一个模型，
不重定向 stdin 就挂住，`< /dev/null` 就出结果；用真 key（百炼）复现同一现象，`< /dev/null` 后立即返回 `OK`。
**初稿写的"免费模型不可靠"是错的**，那 5 次"连续挂住"全都是这个原因。

**已修的隐患**：内核给"非交互进程"起子进程时用的是**继承父进程 stdin**（`stdin=None`），
一旦父进程的 stdin 是打开的管道（SSH、某些 CI、前台调 `worker-run`），执行体就会一直等下去。
现在 `machine_service.py` 对 `stdin_enabled=False` 改给 `asyncio.subprocess.DEVNULL`，并加了回归测试
（`test_non_interactive_process_gets_closed_stdin`）；systemd 单元再加 `StandardInput=null` 作第二道保险。

**仍然未验证的**：坏配置（例如模型名写错）到底会不会干净报错——唯一那次测试**没有重定向 stdin**，
所以挂住完全可以用 stdin 解释。部署按"可能挂住"设防（`max_seconds` + 单元 `TimeoutStopSec=30`），
但**不要**把它当成已证结论。

**另一个部署级事实（环境变量白名单）**：内核给执行体传环境变量是**按白名单**的
（`agentd._EXECUTOR_ENVIRONMENT_KEYS`：HOME/PATH/TEMP/TMP…，遵循"平台不继承整个父环境"）。所以
**百炼 key 放进 systemd 的 `EnvironmentFile` 是传不到 opencode 的**——key 必须交给 opencode 自己的配置
（`~/.config/opencode/opencode.json`，0600，属主是执行体用户），这也是"平台不该知道模型是谁家的"的自然结论。

**内核要 Python 3.12**：`from datetime import UTC`（3.11+）在内核里到处都是，
Ubuntu 22.04 自带的 3.10 会在 import 阶段就失败（本交接初稿的"3.10 可跑"是错的：静态检查漏了这种导入写法）。
执行体机器上用 deadsnakes 装 3.12.14。

## 5 验收

| 项 | 结果 |
| --- | --- |
| Agent 套件 | **327 项通过（9 skipped）**——上一版基线 304，本期 +23（新测试文件全部来自真实样本） |
| API 套件 | **515 项**，仅 2 项**既有** LaTeX 环境失败（与本期无关，历史如此） |
| 编译检查 | `py_compile` 通过（`executor_events.py` / `agentd.py` / `opencode_executor.py` / `store.py`） |
| 真实端到端 | **没做**——需要百炼 key；当前只有"解析真实样本"这一层是真数据验证 |
| 部署 | **没做**（P0 的文件凭据后端与 systemd 封装尚未开工） |

## 6 边界与未做

- **前端没有 opencode 预设**：任务弹窗（`apps/web/app/tasks/page.tsx`）目前只有 codex / 通用 CLI / 声明式命令三个选项。
  从界面上仍可手填 `opencode run --format json --auto {prompt}` 模板——协议会按模板首词**自动**判为 opencode；
  但加一个预设（含 `worker_events`）更不容易配错，留到 P1 与前端一起发。
- **`cost` 没进契约**：opencode 每步报 `cost`（免费模型为 0），已解析进摘要，但 `RunUsage` 只收
  input/output/total——花费视图要做就得动契约与前端，放 P4，**不假装现在有花费视图**。
- **cache / reasoning 明细丢弃**：opencode 的 `tokens.total` 含 `cache.read`，平台按 `input+output` 记（与 codex 同口径）；
  `reasoning` 与 `cache` 明细暂不上报（契约里没有字段，不虚报）。
- **协议推断是"已知执行体"白名单**，不是通用猜测：`_KNOWN_CLI_PROTOCOLS` 目前只有 `opencode`，
  加新执行体前必须先采真实样本、先写测试。
- **`--auto` 的取舍**：模板默认带 `--auto`（无人值守需要它）；这意味着任务的工作区里 opencode 的动作**不再逐次审批**——
  边界靠 `WorkspacePolicy` 白名单、专用非 root 用户、workspace 目录与本机隔离，**不靠 CLI 审批**。

## 7 部署进度（2026-09-23 当天，同一会话续做）

**服务器 `154.219.99.75` 已就绪**（Ubuntu 22.04 / 4C8G / 40G，root 密码直登）：

| 项 | 状态 |
| --- | --- |
| `synapforge` 用户（非 root、无 sudo） | ✅ 已建（uid 1000） |
| 内核 | ✅ `/opt/synapforge-agent`（apps/agent + packages，**不含测试**）+ venv（Python **3.12.14** + cryptography 50.0.1 + websockets） |
| opencode | ✅ 1.18.32，装在 `synapforge` 家目录（`~/.opencode/bin/opencode`） |
| 百炼 provider | ✅ 别名 `deepseek` → 百炼 OpenAI 兼容端点；模型串 `deepseek/deepseek-v4.1-flash`（配置里同时设为默认 model，任务模板无需带 `-m`）；**以服务用户身份实跑返回 `OK`**（用量 input 7339 / output 2）。别名是我们自己起的，`dashscope/…` 与 `deepseek/…` 两种写法都实测跑通；与内置 `deepseek` provider 同名会合并（列表里多出内置条目，走我们配的 baseURL） |
| systemd 单元 | ✅ `map-agent@.service`（`%i` 实例名）已装，**未启动**（配对后再 start） |
| 目录 | ✅ 状态 `/var/lib/synapforge-agent/cloud`(0700)、工作区 `/srv/synapforge/cloud`(0750) |
| **配对** | ✅ 完成：`device-cloud-01` / `agent-cloud-01`（platform=linux、active），只授权「云端智能体」项目（`06f9df2b-…`） |
| 服务 | ✅ `map-agent@cloud` enabled + active，**0 次重启**、常驻 ~152MB；日志可见"领取任务 → 通用 CLI 执行体：opencode（事件协议 opencode）→ 结果：成功" |

**交付的部署脚本**（`deploy/cloud-agent/`，都过了 `bash -n` / `py_compile`）：

- `install-cloud-agent.sh`：用户 → Python 3.12（deadsnakes）→ 源码 + venv → opencode → 目录 → 单元；
- `configure-opencode-provider.sh`：把 provider 写进 opencode 的 0600 配置（支持 `--api-key-file`，**key 不进命令行**）；
- `prepare-platform-side.py`：登录 → 找/建项目 → 生成**配对串**（`pairing`）与**授权串**（`grant`），
  产物格式与 Web 向导页一致（base64url(JSON)，字段同名，Windows 的 connect-agent.ps1 也能吃）；
- `pair-cloud-agent.sh`：登记 Agent → 密钥 → 设备注册 → 令牌入 0600 文件 → 落盘授权串；支持 `--grant-only`
  （授权串必须在设备存在之后生成，所以这两件事能分开跑）；
- `map-agent@.service` + `README.md`（含三个必知坑与失败排查表）。

**端到端结果（2026-09-23，线上）**：真跑多条任务，每条 11–16 秒完成、4000–7600 tokens、产出成果物；
`_cloud_agent_verify.py`（平台机上跑）**六项断言全 PASS + 自清理通过**：

```
[PASS] device-cloud-01 已注册 / platform=linux / 状态 active
[PASS] 任务被执行体跑完（WAITING_REVIEW）      [PASS] Run 成功
[PASS] token 出处为 opencode-jsonl            [PASS] 确实报了 token（>0）
[PASS] 耗时由执行体自报（agent）              [PASS] agent.process.started / agent.agent.message / agent.process.exited
[PASS] 至少一个成果物（跑不动就没产出）        [PASS] 临时任务与 Run 已清理
```
用量样例：`{"input_tokens": 6325, "output_tokens": 7, "total_tokens": 6332, "turns": 1, "source": "opencode-jsonl", "seconds": 11.17, "seconds_source": "agent"}`；
Run 摘要：`CLOUD-VERIFY\n\n---\nopencode exit=0 run=run-… events=3 tools=0 files=0 | tokens=6332（opencode-jsonl） | cost=0 | 产出 1 个成果物（待审）`。

**部署期发现的 6 个真问题（都已修）**：

1. **`--url` 子命令遮蔽**（最隐蔽）：`--url` 既在全局 parser 又在 `daemon-run` 子命令上，子命令的默认值
   `None` 把写在子命令**前面**的全局值吃掉了 → daemon 回落到默认 `http://127.0.0.1:8010`，事件全卡在本地 outbox、
   任务也领不到。修法：子命令用 `default=argparse.SUPPRESS`，并新增 `_resolve_platform_url()`
   （命令行 > platform.json > 本地默认；识别"等于全局默认值 = 用户没指定"），带 3 项测试。
2. **`credential-save` 没有 `--state-path`**：0600 凭据会落到**当前目录**的 `credentials/`（找都找不到）。
   修法：加参数 + 无参数时回落到约定状态目录（`~/.math-agent-platform`）。
3. **`apps/agent/requirements.txt` 少了 `pydantic`**（以前靠 API 侧依赖蹭着）：只有内核的机器上
   `import packages.agent_protocol` 直接 `ModuleNotFoundError`。已补 `pydantic` 与 `websockets`。
4. **`pair-cloud-agent.sh --grant-only` 解析错**：把它后面的 `--url` 当成了自己的值。
5. **日志看不见**：Python 输出缓冲 → journal 里只有 systemd 那一行。单元加 `Environment=PYTHONUNBUFFERED=1`。
6. **`_cloud_agent_verify.py` 自己的两个坑**：`runs` 表没有 `exit_code` 列（它是契约字段）；
   删任务要先清引用它的 `handoffs` / `task_leases`，否则 `FOREIGN KEY constraint failed`。

**未验证**：不带 `--auto` 时 opencode 的审批行为（见 §4 末）。