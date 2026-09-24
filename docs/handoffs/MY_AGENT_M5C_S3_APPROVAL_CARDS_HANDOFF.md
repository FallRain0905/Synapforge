# MY-AGENT M-5c S-3 交接：权限卡片（把 `permission.asked` 变成"待批准"，真拒绝就不执行）

> 2026-09-24 · 状态：**S-3 已交付并线上两端都验过** · 计划 `docs/MY_AGENT_M5C_STREAMING_EXECUTION_PLAN.md`
> 上游：S-0 采样（权限会拦路）、S-1 常驻 serve（当时沿用 `--auto` 一律放行）

---

## 1 先说实测口径（这一期的地基，三遍探针换来的）

同一个 serve 上跑三次"往工作区外写文件"，只换权限回复的取值：

| 回复 | HTTP | 文件到底写出来没有 |
| --- | --- | --- |
| `reject` | 200 | **没有**（这就是"真闸门"的证据） |
| `once` | 200 | 写出来了 |
| `always` | 200 | 写出来了 |

权限请求的字段（原样收进平台）：`permission=external_directory`、`patterns=["/tmp/*"]`、
`metadata={filepath, parentDir}`、`tool={messageID, callID}`——卡片上的说明就是拿这些拼的
（工具名从同一 `callID` 的 `tool` part 里取，所以卡片会写"**write** 动工作区外的文件：/tmp/x"）。

## 2 三层怎么落

| 层 | 做了什么 | 关键决定 |
| --- | --- | --- |
| 平台 | 迁移 `027_agent_turn_approvals.sql` + `agent_turn_approvals` 表；契约 `AgentChatTurnApproval{,Request,Decision,State}`；四个接口：执行体**上报**（`POST /api/agents/{id}/chat-turns/{tid}/approvals`）、执行体**轮询状态**（`GET …/{rid}`）、执行体**标过期**（`POST …/{rid}/expire`）、人**做决定**（`POST /api/my-agent/turns/{tid}/approvals/{rid}`，`once`/`always`/`reject`） | 幂等：主键就是 opencode 的 `per_…`（重报不产生第二张卡）；**决策只认第一次**（后到的点不动已定的结局）；`decided_by` 记下是谁批的 |
| 内核 | `run_serve_turn` 收到 `permission.asked` → 组一张卡 → **上报 → 有界等待人批 → 按决定回复 opencode**；等不到就 `reject` + 标 EXPIRED（`chat_approval_timeout_seconds` 默认 300s）。过程事件 `approval.requested` / `approval.decided` 进抽屉；`process.exited` 里带 `permissions` 计数 | **没人批 = 不执行**（与 `--auto` 时代相反，是刻意的：不假装有人同意）。上报/轮询失败**不把这一轮挂死**——退回自动策略并如实记日志；没接审批通道时仍按 `auto_approve_permissions` 走 |
| 页面 | 待批准卡片（标题 + 一句话 + `permission · patterns` + 「批准一次 / 本次都允许 / 拒绝」），批准后变「已批准 · 由你（fallrain） · once」；轮询与事件同一个 900ms 节奏；等批时状态写「等你批准（执行体已停下等你的决定）」 | 三档按钮与 opencode 的取值一一对应；`decided_by` 是自己就显示"你（昵称）"，否则显示成员 id（平台不存昵称快照，不编） |

## 3 线上验收（两个方向都做了，避免"看起来对"）

先做了**一次拒绝**（`/tmp/zcode-approval-check.txt`）→ 卡片出现 → 拒绝 → 页面显示"已拒绝（执行体没有做那件事）"、
journal 里 `权限请求 per_0d1873ce → reject（已回复：True）`、平台那一行 `DENIED | reject`。

然后发现一个**必须记录的机关**：执行体那个 systemd 单元有 `PrivateTmp=yes`，**它看到的 `/tmp` 是私有的**
（真实路径 `/tmp/systemd-private-…-map-agent@cloud.service-…/tmp`）——所以在宿主 `/tmp` 里查文件永远是"没有"，
拿它当证据会得出错误结论。改成**在私有 tmp 里、用两个不同的文件名**重做了一遍：

```text
zcode-reject-check.txt   （拒绝）→ 不存在        ✅ 拒绝真的没执行
zcode-approve-check.txt  （always）→ 存在，内容 "APPROVED"（8 字节）  ✅ 批准真的执行了
```

另外两侧都有页面证据：卡片从 `is-pending` → `is-denied` / `is-approved`，toast「已拒绝：执行体不会做那件事」/「已批准这一次」，
opencode 自己的日志也对得上：拒绝那次只有 `asking`，批准那次多了一行 `evaluated permission=edit … action=allow`。

## 4 顺手修的三处（都是实测逼出来的）

1. **新会话会继承上一条对话的角色**：上一条用「文献检索」（只读），新建后仍是它，人说"写个文件"一直被拒——
   而且因为历史里已有"我是检索角色"的自述，**同一会话内换角色模型也会接着拒绝**（协议层确实换了角色，实测 `agent=mm-coding`）。
   现在 `handleNewChat` 把角色重置为「默认（无角色）」（模型是全局偏好，保留）。
2. **被拒绝之后这一轮可能没有任何文字回复**（执行体停在被拒的工具调用那一步就收尾了，实测 0 字）：
   页面如实写一句"这一轮没有文字回复：执行体想做的事没被批准，它在这一步就结束了"，别让人对着空气找答案。
3. **删会话删不掉了（本期自己引入又修掉的回归）**：新表 `agent_turn_approvals.turn_id` 有外键，
   而 `delete_conversation` 只清了 `agent_turn_events` 与 `agent_turns` → 带权限请求的会话**删不掉**（界面上点确认没反应）。
   修法：先删审批行再删轮次，并加了一条"删会话要把挂在轮次上的每张表都清掉"的测试。
   **教训**：往"挂在轮次上"的表里加东西时，删除路径要同步改——这不是第一次（历史上 events 也是这么补上的）。

## 5 测试与基线

- Agent：**413 项**（`test_opencode_server.py` +4：卡片上报/人的决定生效、超时=不执行、上报失败降级、无通道时按开关拒绝）。
- API：**554 项**（`test_agent_chat_approvals.py` 10 项：上报建卡与字段、按 request id 幂等、决定记录谁批的、只认第一次、
  always 也是批准、EXPIRED 之后点不动、执行体轮询读到状态与决定、未知/越界 404、HTTP 三接口往返、非法 decision 422）；
  迁移清单契约补 `027`。
- 前端 tsc + 生产构建通过；平台 `server_verify` 22/22（本轮共发布三次：S-3 主体、新会话重置角色、卡片文案与"你"）。

## 6 边界（都写清楚，别当成没做）

- **CLI 回退通道没有权限事件**：降级到 `opencode run` 时仍是 `--auto`（那条通道不给审批机会）。所以"权限卡片"只在常驻通道成立。
- 默认**没人批就不执行**：如果哪天要无人值守地放行，把 `chat_approvals=false`（回到 `--auto` 等价语义）——
  这是刻意留的开关，默认值选了更保守的一侧。
- `permission.asked` 的**类别**取决于 opencode 的权限配置（实测工作区内的普通写文件不弹卡片，弹的是"工作区外目录"这类）。
- 卡片只活在**这一轮**里：轮次结束就定格为已批准/已拒绝/已过期（不会跨轮复用 `always`——open ``always` 的"会话内"范围由 opencode 自己管）。