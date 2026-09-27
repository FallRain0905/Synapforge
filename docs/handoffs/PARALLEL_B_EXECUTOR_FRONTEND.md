# 任务书 B：执行体 + 前端（三会话并行开发）

> 日期：2026-09-26。你是三个并行会话中的 **会话 B**。本任务书自包含，开工前通读一遍即可。
> 权威文档：`docs/PRODUCT_STATUS_AND_REPOSITIONING_PLAN.md`（产品定位）、`docs/REPOSITIONING_IMPLEMENTATION_PLAN.md`（实施计划，工作项编号 W*.x 以它为准）。
> 仓库根：`C:\Users\19855\Documents\ChatGPT\数学建模\math-agent-platform`。

## 0. 你的领地（独占，其他会话绝不碰）

- `apps/web/**` 全部（页面、组件、`lib/api.ts`、`lib/nav.ts`、`lib/page-layout.ts` 等）
- `apps/agent/**` 全部（agentd.py、chat_loop.py、runner.py、opencode_server.py 等）

会话 A 拥有 `apps/api/**` 与迁移；会话 C 只在 `apps/api/app/` 下**新建**独立模块（`acceptance.py`、`stream_bridge.py`、`event_catalog.py`），你同样不碰。
需要后端新接口时：接口形状以任务书/契约为准，`lib/api.ts` 里你写调用函数；后端缺失的部分列进"给 A 的接口需求"，**不要自己改 apps/api**。

## 1. 三会话分工速览

| 会话 | 领地 | 定位 |
|---|---|---|
| A | apps/api 现有文件 + 迁移 + 部署 | 路由/迁移/落库 + 整合 |
| B（你） | apps/web + apps/agent | 前端全链 + 执行体 |
| C | 只新建独立模块 + 契约文档 | 纯逻辑 + 单测 |

## 2. 并行纪律（必须遵守）

1. **不建迁移**：迁移号 034–037 全归 A；你的数据库诉求通过接口需求提出。
2. **构建互斥**：跑 `npm run build` 前确认其他会话没在跑构建；日常验证用 `npx tsc --noEmit` + `next lint`（不写 `.next`）。
3. **只 commit 自己领地的文件**；push/部署一律不做（A 的职责）。
4. **禁区**：`apps/web/app/globals.css`、`login/page.tsx`、`register/page.tsx`、`components/shell.tsx` 属另一条平行线，不碰。老路由只隐藏不删除。
5. 凭据、服务器地址、口令不进任何文件；不改 README。

## 3. 前端既有规矩（踩过的坑，直接沿用）

- **页面自己别发身份请求**：用 `useAuth()` 的 `ready`/`account` 门控（照 workspace 模式）。任何新页面在 mount 里裸调 `/api/auth/me` 会复现"打开即被登出"bug（apiFetch 收到 401 会无条件清掉有效令牌）。
- **布局纪律**：中间层 wrapper 必须显式加入 flex（`FILL_COLUMN`：`flex:1 1 auto; min-height:0; min-width:0`，见 `lib/page-layout.ts`）；`.page-content` 网格页面挂 `PAGE_GRID` 防止内容偏短时拉伸出大片空白。出现"莫名空白"先量尺寸找真因，不要盲改。
- 产品取向：复刻 opencode 级交互（真实流式/思考折叠/结构化选择卡）；空白零容忍；控件能收进抽屉就收；状态不许伪造（不在线就说不在线）。
- 回归命令：`cd apps/agent && python -X utf8 -m unittest discover -s . -p "test_*.py"` → ≥473（12 skipped）；`cd apps/web && npx tsc --noEmit` 无错；build 路由 ≥26。

## 4. 决策点默认值（D1–D5 已冻结）

与 A 任务书相同。与你相关：D2 `/ask` 302 → `/my-agent`（路由文件保留，只重定向）；D5 stop_reason 平台侧先落 4 种。

## 5. 第一期任务（现在就做）

### 5.1 阶段 0 前端（对应实施计划 W0.1–W0.5）

1. **W0.1 首页三入口**：Agent 工作台 / 协作项目 / 自动化工作流，每卡一句话说明 + 直达按钮（落地页或登录后 dashboard，选信息架构最顺的一处）。
2. **W0.2 导航重组**（`lib/nav.ts`）：三组——工作台 / 项目协作 / 工作流工具；数学建模模板类入口移入"工作流工具"组；老路由只隐藏不删除。
3. **W0.3 /ask 归并**：`apps/web/app/ask/page.tsx` 改为重定向到 `/my-agent`（保留原 query 参数）；导航移除该入口。
4. **W0.4 状态词典**：新建 `apps/web/lib/status-dictionary.ts`——task/run/artifact/handoff/review/gate 每个状态一句用户可读解释 + 固定文案"模板骨架 ≠ 完成结果""自动 = 受约束的调度推进"；在任务页/成果物页/审核页挂提示组件。
5. **W0.5 my-agent 空态**：无 Agent 时直接给"接入执行体"引导入口。

### 5.2 stop_reason 状态条（W1.1 前端侧）

- turn 结束状态如实渲染以下枚举（A 的契约已冻结同值）：
  `completed / failed / cancelled / token_capped / turn_capped / timeout / permission_timeout / unknown`
- 先按此硬编码渲染，A 的接口字段就绪后（看 git log 契约 commit）切到真实字段。
- 执行体侧（`apps/agent/chat_loop.py` / `agentd.py`）：在 turn 终态回报里带上 `stop_reason` 字段（平台侧可判定的四种：cancelled/timeout/permission_timeout/failed 先落；opencode 信号能映射的以后补）。回报体形状以 A 的契约为准，未知值让平台归一化，你不必穷举。
- 用户可读文案进 `status-dictionary.ts`，中英不混排。

### 5.3 验收与收尾

- `tsc --noEmit` 零错、`apps/agent` 单测 ≥473。
- 自查：不进数学建模模板也能从首页开始一次正常 Agent 工作；分得清"对话"与"正式项目"；模板入口有"骨架≠结果"提示。
- 报告：改了哪些页面、与 A 的接口对齐情况、遗留项。

## 6. 第二期任务预告（完成后按此继续）

- my-agent 消息操作组：「保存为成果物 / 创建任务 / 交给另一个 Agent」——调 A 的 `POST /api/agent/conversations/{id}/promote`（`lib/api.ts` 加函数；幂等，重复提交不重复建 artifact）。
- my-agent SSE 直连：EventSource 消费 A 的 turns/conversations stream 路由（`after=<seq>` 断线重连），现有 900ms 轮询降级为兜底。
- 附件→执行体 `inputs/`→产出→下载/转云盘/转成果物连续路径打磨，上传失败/权限超时如实呈现。
- 第三期预告：workspace「团队 / 生产」视图（消费 A 的 team/production-path API）。第四期预告：agentd 侧按 C 的 receipt 契约生成产物溯源（`*_hash` 16 hex）并随上传携带；租约续约适配 fencing token。

## 7. 完成标准（第一期）

- [ ] 首页三入口 + 导航三组，老路由无一删除
- [ ] /ask 重定向生效且带 query，导航已移除入口
- [ ] 状态词典落地，任务/成果物/审核页可见"骨架≠结果"提示
- [ ] my-agent 空态有接入引导；turn 结束状态条如实显示 stop_reason
- [ ] tsc 零错 + Agent 基线 ≥473；报告含给 A 的接口需求清单
