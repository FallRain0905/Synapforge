# MY-AGENT M-5c S-2 交接：逐字增量到页面（`delta` 事件 + 边生成边显示）

> 2026-09-24 · 状态：**S-2 已交付并线上真跑验证** · 计划 `docs/MY_AGENT_M5C_STREAMING_EXECUTION_PLAN.md`
> 上游：S-0 采样（`MY_AGENT_M5C_S0_SSE_SAMPLES_HANDOFF.md`）、S-1 常驻 serve（`MY_AGENT_M5C_S1_SERVE_CHANNEL_HANDOFF.md`）

---

## 1 这一期交付了什么（用户视角）

在「我的智能体」里，回答**边生成边出现**：以前要等这一轮跑完才看到正文（纯问答尤其明显），
现在常驻服务通道会把正文按增量推给页面，气泡里文字一段一段长出来（公式 LaTeX 也是边到边渲染）。

**不假装**：只有当这一轮真的走增量通道时才写「生成中（增量显示）」；
走 `opencode run` CLI 通道（或降级）时页面照旧写「生成中（分段显示，不是逐字流）」——两种文案由**事件本身**决定，不是我们嘴上说的。

## 2 怎么做的（三层，各管一段）

| 层 | 改动 | 关键决定 |
| --- | --- | --- |
| 内核 `apps/agent/opencode_server.py` | 从 SSE 的 `message.part.delta` 攒增量 → 按 `delta` 事件发出去 | **绕开 reporter**：reporter 有 1.5s 节流 + 40 条上限，压"过程事件"对，但**压掉一条增量就是丢正文**。增量自己有节奏：攒够 0.8s 或 32 字发一条，**收尾一定补发剩下的**（`force`），封顶 240 条 |
| 内核 `apps/agent/chat_loop.py` | `emit_delta=lambda payload: emit("delta", payload)` | 走与过程事件同一条上报通道（`/api/agents/{id}/chat-turns/{id}/events`），**平台零改动** |
| 页面 `apps/web/app/my-agent/page.tsx` | `liveSegments` = `agent.message`（整段，CLI 通道）+ `delta` 拼接（增量，serve 通道）；执行中轮询 900ms（空闲仍 3000ms）；抽屉把 `delta` 过滤掉（几百条会刷屏）；状态文案按有没有 `delta` 二选一 | 轮次结束时一律用权威的 `content` 覆盖；刷新页面靠轮次事件表**重放**重建（`delta`/`agent.message` 都在里面） |

平台侧确认过**不需要迁移也不需要白名单**：`AgentChatTurnEventReport.event_type` 本来就是自由字符串（2–80 字符）。

## 3 踩到的真坑（这一期最重要的发现）

**思考（reasoning）与正文（text）的增量长得一模一样**：两者都是 `message.part.delta`、`field` 都是 `text`，
`partID` 不同但**光看 field 分不开**。第一版按 `field == "text"` 过滤，结果把模型的英文思考（`The user wants a ~300 character explanation…`）
混进了气泡——线上复现时一眼看到。

**证据**（同一轮的事件表）：
```text
prt_0d124c07c001c1mLvD6h7WiXJZ  → "The user wants a ~300 character explanation of power-law …"   ← reasoning 段
prt_0d124c76f001GcZNetvcB2dwjE  → "幂律分布描述一种「少数极大、多数极小」的不均衡现象…"              ← 真正的正文段
```
**修法**：按 `partID` 记一张类型表，类型从 `message.part.updated` 的 `part.id + part.type` 学（实测顺序是先声明类型、再发该段增量）；
只把**已知是 `text`** 的段并入正文，`reasoning` 段丢弃，**类型还没学到**的增量先攒着不猜（`unknown_deltas` 计数会进 `process.exited`，
正常为 0——不为 0 说明事件形状变了，从这个数字查）。

顺带纠正 S-0/S-1 的一个误读：**reasoning 也会发 `field=text` 的 delta**（S-0 那份样本里 51 条"text delta"其实混着思考），
不是"思考只有整段快照"。

## 4 线上真跑证据（2026-09-24）

一轮「解释幂律分布 + 行内公式 + 三点例子」，每 2.5s 采一次页面状态：

```text
at=3s … at=15s   liveLen=0                       （模型在思考：思考增量被正确丢弃，气泡保持空的）
at=18s           liveLen=0
at=20s           liveLen=201  「幂律分布描述一种"少数极大、多数极小"的不均衡现象：变量取值…双对数坐标下近似为一条直线。这与」
                 标签 = 生成中（增量显示）；此时页面已有 4 个 KaTeX 节点（公式边到边渲染）
at=23s           完成：气泡换成权威 content（467 字），用量 8546 tokens · opencode-serve
```

- 平台事件表：该轮 `delta` 13 条、`process.exited` 显示 `{"delta_events":13,"unknown_deltas":0,…}`；
  **全库 `delta` 事件里含英文思考的条数 = 0**（修复前那一轮才有，会话已删）。
- 刷新页面后：回答完整（467 字、4 个公式、用量照旧），没有残留的"流式中"状态。
- 降级文案：本轮全程 `delta` 在场，标签是「增量显示」；CLI 通道的轮次仍是「分段显示，不是逐字流」（文案由事件决定）。

## 5 测试

`apps/agent/test_opencode_server.py` **19 项**（+3）：

- 新增「思考增量绝不进正文」：同一轮里 reasoning 段与 text 段交错，断言拼出来的正文只有 text 段、且 `unknown_deltas=0`；
- 新增「封顶不丢字」：12 条增量 + `max_delta_events=5` → 事件数 ≤ 6（5 条封顶 + 收尾补发），**拼起来仍是完整 12 段**；
- 新增「没配 delta 通道时退回整段 `agent.message`」（S-1 口径保留，模块可独立用）。

全量：Agent **402 项**通过（11 skipped）。前端 `tsc` 通过 + 生产构建通过；平台发布 `server_verify` 22/22。

## 6 已知边界

- **思考暂时不显示**（丢弃，不混进正文）——"思考块可折叠展示"属 **S-4**；内核已经把类型区分好了，S-4 只需把 reasoning 段另发一条事件。
- **权限仍是一律放行**（`permission.asked` → `always`），真卡片属 **S-3**。
- **事件条数有天花板**：轮次事件读取端 `LIMIT 500`（按 sequence 升序，超了会切掉**最新**的）。
  这一期用"增量节奏 0.8s + 封顶 240 条"把单轮事件压在 500 以内；若将来单轮更长，要么提高节奏上限、要么给读取端加分页。
- 增量是"按批"（约 0.8s 一批）不是逐 token：页面文案写「增量显示」而不是「逐字流」，不夸大。