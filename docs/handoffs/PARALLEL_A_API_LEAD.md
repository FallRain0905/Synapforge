# 任务书 A：平台 API 主线 + 整合者（三会话并行开发）

> 日期：2026-09-26。你是三个并行会话中的 **会话 A**。本任务书自包含，开工前通读一遍即可。
> 权威文档：`docs/PRODUCT_STATUS_AND_REPOSITIONING_PLAN.md`（产品定位）、`docs/REPOSITIONING_IMPLEMENTATION_PLAN.md`（实施计划，工作项编号 W*.x 以它为准）。
> 仓库根：`C:\Users\19855\Documents\ChatGPT\数学建模\math-agent-platform`。

## 0. 你的领地（独占，其他会话绝不碰）

- `apps/api/**` 全部**现有**文件（main.py、contracts.py、agent_chat.py、repository/store、outbox.py 等）
- `apps/api/migrations/**`（迁移号 034/035/036/037 已预分配给你）
- `scripts/**`、`deploy/**`、README 基线维护
- **构建、全量回归、部署、push 只由你执行**

会话 C 会在 `apps/api/app/` 下**新建**这些文件（归 C 所有，你不要修改，只 import）：
`acceptance.py`、`stream_bridge.py`、`event_catalog.py`、`workflow_engine.py`（后期）及对应 `test_*.py`。
接线时发现它们的模块缺接口，**不改 C 的文件**，在收尾报告里列"给 C 的接口需求"。

## 1. 三会话分工速览

| 会话 | 领地 | 定位 |
|---|---|---|
| A（你） | apps/api 现有文件 + 迁移 + 部署 | 路由/迁移/落库 + 整合 |
| B | `apps/web/**` + `apps/agent/**` | 前端 + 执行体 |
| C | 只新建独立模块 + 契约文档 | 纯逻辑 + 单测，交你接线 |

## 2. 并行纪律（必须遵守）

1. **迁移号**：034=stop_reason、035=provenance、036/037=workflows。B/C 永不建迁移。
2. **契约先行**：第一期第一天先提交共享形状（StopReason 枚举等）小 commit，B/C 靠 git log 对齐。
3. **构建互斥**：跑 `npm run build` / 全量回归前，确认其他会话没有正在跑构建。
4. **push 单一出口**：每期收尾你统一 push（先直试 `git push`，失败走 api.github.com 的 Git Data API fallback；**绝不 `reset --hard`**，会毁掉其他会话未提交文件）。
5. **禁区**：`apps/web/app/globals.css`、`login/page.tsx`、`register/page.tsx`、`components/shell.tsx` 属另一条平行线，不碰。老路由只隐藏不删除。
6. **部署**：仅在用户明确要求时执行 `pack-source.sh` → `remote.py put` → `server_release.sh --public-url https://synapforge.top` → `server_verify.sh`。凭据不进仓库，提交前脱敏扫描。

## 3. 全局工程纪律（沿用项目既有规矩）

- 写路由必须声明 Request（contracts 模型）并配 HTTP 层测试。
- 迁移最小增量 + 严格 RLS（模式照 030/033：组织隔离、`app.current_organization_id()`）。
- 老轨只隐藏不删除；README 禁区清单有效。
- 每段代码改完即 `python -m py_compile` 验证；环境出现内容串行损坏（heredoc 截断、工具结果自相矛盾）时**立即停手报告**，不要带病继续。
- 回归命令与基线：
  - `cd apps/api && python -X utf8 -m unittest discover -s . -p "test_*.py"` → ≥727（15 skipped；2 项 `test_latex_compile` 是本机没装 xelatex 的环境失败，不算数）
  - `cd apps/agent && python -X utf8 -m unittest discover -s . -p "test_*.py"` → ≥473
  - `cd apps/web && npm run build` → 路由 ≥26
  - 基线只升不降（C 的新测试会被 discover 收进来，属正常上涨）。

## 4. 决策点默认值（D1–D5 已按推荐冻结，用户有异议才改）

D1 同机 systemd 双实例做 e2e；D2 /ask 302 → /my-agent；D3 SSE 放第一期后段；D4 workflow 定义存 JSON 列；D5 平台侧先落 4 种 stop_reason。

## 5. 第一期任务（现在就做）

### 5.1 契约先行（第一天单独 commit）

在 `apps/api/app/contracts.py` 定义 StopReason 枚举常量：
`completed / failed / cancelled / token_capped / turn_capped / timeout / permission_timeout / unknown`。
并定义"执行体回报 turn 终态"的请求模型（含 `stop_reason` 字段，声明 Request）。commit message 注明"B/C 请以此为准"。

### 5.2 迁移 034（W1.1 后端）

- 新建 `apps/api/migrations/034_agent_turn_stop_reason.sql`：`agent_turns` 加 `stop_reason TEXT`（允许 NULL，历史行视为 unknown）。表定义见 `024_agent_conversations.sql`。
- 加入 `test_platform_contracts.py` 的迁移清单。

### 5.3 落库路径（W1.1）

- `agent_chat.py`：turn 进入终态时写 `stop_reason`。平台侧可判定四种：
  `cancelled`（现有 stop 路径）、`timeout`（平台超时门）、`permission_timeout`（审批超时）、`failed`（执行体异常回报）。
  `token_capped/turn_capped` 等 B 侧（chat_loop/opencode 信号映射）后补——回报接口字段留好即可。
- 接口点（与 B 对齐）：执行体回报 turn 终态的请求体加 `stop_reason`；未知值一律归一化为 `unknown`，绝不 500。

### 5.4 测试

HTTP 层测试（声明 Request）覆盖：四种平台侧 stop_reason 写入、未知值归一化、RLS 隔离。迁移测试 + 契约清单。

### 5.5 收尾

全量回归（三套命令）→ 报告结果 + B/C 接口确认清单。push 与部署等用户指令。

## 6. 第二期任务预告（完成后按此继续）

- `POST /api/agent/conversations/{id}/promote`：会话产物 → artifact + draft task（幂等，RLS 只允许本人会话）；"交给另一个 Agent"带交接意图字段（target + context 注入新任务提示词）。
- SSE 两条路由：`GET /api/agent/turns/{id}/events/stream`、`GET /api/agent/conversations/{id}/stream`（帧格式 `event/data/id`）；接线 C 的 `stream_bridge.py`（C 只交付模块与单测，你负责 import 与 StreamingResponse）。
- 消费 C 的 `event_catalog.py` 常量接入 outbox（第三期）；team/production-path 聚合 API（第三期）；迁移 035 + 租约 fencing + e2e 主脚本（第四期）。

## 7. 完成标准（第一期）

- [ ] 契约 commit 已提交，枚举值与本文 5.1 一致
- [ ] 034 迁移 + 契约清单 + HTTP 测试全绿
- [ ] 平台侧四种 stop_reason 真实落库，未知值归一化
- [ ] 全量回归 ≥727 / ≥473 / build ≥26 路由
- [ ] 报告：做了什么、B/C 需要对齐的接口清单、遗留项
