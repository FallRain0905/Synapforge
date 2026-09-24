# Demo 1.0 验收清单

> 日期：2026-09-16
>
> 验收对象：`docs/DEMO_1_0_IMPLEMENTATION_PLAN.md` §2.1 的四条目标
>
> 验收方式：可执行命令 + 实测证据（不采信"代码看起来对"）

---

## 0. 完成定义（计划书原文）

> **一个没读过源码的人，能在 15 分钟内，从零把项目建起来、把模板铺开、把自己的 Agent 接进来、看着 Agent 领走一个任务并产出成果物。**

对应的四条目标（§2.1）：

1. **零手调 API 的完整旅程**：新建项目 → 应用模板包 → 建/改任务 → 接入 Agent → Agent 自动领取任务 → 上报结果 → 人工审核门禁 → 交付物生成，全程只在 Web 界面操作。
2. **界面不说谎**：Agent 掉线显示离线；协作编辑的内容要么被保存、要么明确告知未保存；门禁能真的被人工批准。
3. **接入一个 Agent 不超过 3 条命令**，且不需要用户手工编造任何 ID。
4. **回归与构建维持绿色**：后端测试全通过且数量不低于基线，前端构建通过、无类型错误。

---

## 1. 一键验收

```powershell
# 终端 A：平台（基础设施 + API + Web）
cd math-agent-platform
.\scripts\start-demo.ps1

# 终端 B：验收脚本（自动跑可 API 化的步骤，界面步骤会提示你操作后回车）
.\scripts\demo-1.0.ps1
# 无人值守自检（用 API 代替人工点界面，仅用于回归）：
.\scripts\demo-1.0.ps1 -AutoApprove
```

最近一次实测（2026-09-16，独立临时数据库）：

```text
[PASS] 平台健康检查
[PASS] 项目已创建 · 项目出现在列表
[PASS] 模板包物化出任务与成果物 — 17 任务 / 9 成果物
[PASS] 看板可见任务与成果物
[PASS] 任务已创建并进入 READY
[PASS] 配对已生成（15 分钟一次性） · 设备已登记且状态 active
[PASS] 项目授权已签发（一次性 project_token）
[PASS] 任务被 Agent 自动领取并执行完成 — status=APPROVED assignee=agent-demo-…
[PASS] 运行台账出现该 Agent — 该 Agent 的 Run 数=1
[PASS] 门禁已建立（待人工批准）
[PASS] 交接已建立（待接收方确认）
[PASS] 存在已通过的门禁 — 已通过 1 个
[PASS] 交接收据已接受
[PASS] 文档保存已落库（artifact.content_stored 事件） — 1 条
[PASS] 取消确认后未新增提交包 — 提交包 0 个
全部 17 项通过：Demo 1.0 全链路可用。
```

---

## 2. 逐条验收

### 目标 1：零手调 API 的完整旅程

| 步骤 | 界面位置 | 自动化断言 | 实测结果 |
| --- | --- | --- | --- |
| 新建项目 | 总览页「新建项目」（侧栏与顶栏也有入口） | 项目创建并出现在列表 | PASS |
| 应用模板包 | 建模模板包页 → 一键应用（有确认弹窗，预告将建数量） | 物化出 17 任务 + 9 成果物 | PASS |
| 建任务 | 任务页 → 新建任务 | 任务进入 READY | PASS |
| 接入 Agent | 设备与接入页 → 生成配对 → 复制命令到目标机器执行 | 设备 active、指纹一致 | PASS |
| Agent 授权 | 设备与接入页 → 授权到项目 | 一次性 project_token 签发 | PASS |
| Agent 自动领任务 | 复制 worker 命令执行 | 任务 **APPROVED**、assignee=该 Agent、Run 台账 SUCCEEDED | PASS |
| 人工审核门禁 | 审核门禁页 → 批准 | 门禁 FAILED → **PASSED** | PASS（UX-6 实测） |
| 协作编辑 | 文档版本页 → 保存草稿 | 刷新后内容仍在（1869 → 1886 字节） | PASS（UX-6 实测） |

**结论**：整条旅程不需要 `curl`，也不需要手工拼任何 ID。

### 目标 2：界面不说谎

| 承诺 | 反例（修复前） | 现状与证据 |
| --- | --- | --- |
| Agent 在线状态 | 永远显示 online，无 offline 写入 | 心跳超时 90s 置离线；实测界面**未刷新**在 24s 内自动变 offline，并显示「308 秒前，已超过判定阈值」 |
| 协作编辑内容 | 敲进去的内容刷新即丢，文案却说"提交待审写入正式版本" | 显式「保存草稿」；实测刷新后内容仍在；文案已改为说明服务端不保存协作状态 |
| 门禁可批准 | 卡片写着"需要人工确认"却没有按钮；任务页的"通过"必然 403 | 审核页可批准（实测 FAILED → PASSED）；任务页改为「去审核」链接 |
| 交接收据 | 整页只读 | 逐条待确认收据可接受/拒绝（实测 PENDING → ACCEPTED） |
| 加载态 | 数据未到时显示"暂无成果物" | 6 个页面在加载时显示骨架（SSR HTML 断言 `loading-skeleton` 存在） |
| 危险操作 | 删除文件/会话、落库门禁、生成提交包都是一点即执行 | 四处均接入确认弹窗（实测取消后无副作用） |

### 目标 3：接入一个 Agent 不超过 3 条命令

| 输入 | 说明 |
| --- | --- |
| 平台地址 | 向导页已填好，通常不用改 |
| Agent 名称 | 用户自己起 |
| 配对串 | 向导页一键复制 |

脚本 `scripts/connect-agent.ps1` 内部完成：登记 Agent（幂等）→ 生成 Ed25519 密钥 → 私钥签名设备注册 → Token 写入 Windows 凭据管理器 → 打印可粘贴的 `gateway-run` 命令。
**实测**：用向导复制的命令在干净环境执行，5 步全绿（含凭据 `STORED`），无需手工编造 session_id/connection_id。

### 目标 4：回归与构建

```text
后端（apps/api）：python -X utf8 -m unittest discover -s . -p "test_*.py"
                  → Ran 291 tests, OK (skipped=13)
Agent（apps/agent）：同上 → Ran 164 tests, OK (skipped=9)
前端：cd apps/web && npm run build → 17 个页面静态预渲染；npx tsc --noEmit 无错误
```

数量均**高于** UX-0 冻结基线（后端 266 / Agent 152 / 前端 16 路由）。

---

## 3. 尚未达成的部分（诚实清单）

以下条目**不在** Demo 1.0 的完成定义内，但演示时可能被问到：

- **Agent 不产出正式成果物**：worker 上报 summary + Run 台账，`output_artifact_ids` 恒空（成果物上传尚未接进循环）。
- **协作编辑是单机草稿模型**：多人并发保存是"后写覆盖先写"（决策 D8 不引入 Yjs 持久化）。
- **长任务会被回收**：worker 不做租约心跳，超过 `--lease-seconds` 的任务会被判 `RUNNING → NEEDS_REVISION`。
- ~~Codex 作为执行体未打通~~ → **已打通（2026-09-16）**：ChatGPT 桌面端自带的 Codex CLI（`codex-cli 0.154.0-alpha.6.2`）已接入为任务执行体，实测两种场景——纯对话（`events=4`、reply 精确匹配）与需要工具调用（`tools=1`、reply 与 README 首行一致）；任务 APPROVED、Run.SUCCEEDED。用法与排障见 `docs/CODEX_EXECUTOR.md`。**Claude Code 仍未打通**（本机只发现 Claude 的配置目录，未发现可执行文件）。
- **离线感知最坏约 100 秒**（90s 阈值 + ≤10s 扫描间隔）。
- **连接状态在客户端被强杀时不收敛**（`agent_connections` 会残留 CONNECTED；Agent 状态本身仍会按心跳超时置离线）。
- **多实例部署未验证**：维护扫描在每个实例都会跑（写操作幂等）。

---

## 4. 演示脚本的运行前提

1. 平台已启动（`scripts\start-demo.ps1`）；
2. 若要看到"测试连接 → 可用"，需先在设置页保存可用的 LLM/Embedding 凭据；
3. 第 6 步需要浏览器配合（或在回归时用 `-AutoApprove`）；
4. MinerU 没有免费探活接口，其"可用"只表示 Token 已配置——真正验证要用一次云盘转换。
