# 项目事件流契约（AGENT_EVENT_CONTRACT）

> 状态：v1（目录版本 CATALOG_VERSION = 1，由 `apps/api/app/event_catalog.py` 权威定义）
> 所有者：工作包 C（改契约先找 C；A 负责服务端执行，B 负责前端消费）
> 适用范围：项目级多 Agent 协作事件（`project.*`）。单会话轮次事件
> （`agent_turn_events`）沿用既有通道，本契约是其"项目级"的姊妹协议，不是替代。

## 1. 四条规则（不可协商）

1. **只增不改（additive only）。** 新增事件、给既有事件的 payload 增加可选字段，
   都是允许的演进；改名、删除事件、改动既有字段语义、改变 seq 语义，都不允许。
   想换含义就新增一个事件名，旧名保留。
2. **新增字段必须是可选的。** 消费方不得假设任何非必填字段存在。
3. **消费方必须忽略未知内容。** 前端/脚本遇到目录里没有的事件名或 payload 里的
   未知字段，跳过即可，不得报错、不得中断渲染。这是前端可以晚于后端上线的根据。
4. **seq 严格递增。** 同一事件流（项目流）内 `seq` 严格递增、无空洞地由服务端
   分配；消费方用它做去重与断点续传（`after=<seq>`）。客户端**永远不生产 seq**。

## 2. 事件名与目录

- 形态：`project.<family>.<action>`，全小写，family ∈
  `task | run | artifact | handoff | review | gate | agent`。
- **事件名必须先注册**在 `apps/api/app/event_catalog.py`；服务端对未注册名
  直接拒绝（`UnknownEventError`），前端对未知名按规则 3 忽略。
- 新增事件流程：C 在目录中注册（`version=1`，`CATALOG_VERSION` +1）→ 契约文档
  补一行 → A 才允许发出。没有"临时字符串事件名"。

## 3. 信封（envelope）

```json
{
  "event": "project.gate.blocked",
  "seq": 42,
  "schema_version": 1,
  "occurred_at": "2026-09-26T12:00:00+00:00",
  "payload": { },
  "project_id": "…",
  "organization_id": "…",
  "actor": "agent:b3…"
}
```

- 必填：`event / seq / schema_version / occurred_at / payload`（payload 是对象）。
- 推荐：`project_id / organization_id / actor`（`actor` 形如 `agent:<uuid>` 或
  `member:<uuid>`，**由服务端从令牌解析注入，客户端自报的 actor 一律剥离**）。
- `schema_version` 当前恒为 `1`；将来单事件语义演进时才 +1。
- 校验入口：`event_catalog.validate_envelope()`，A 在写 outbox 前调用。

## 4. SSE 传输帧

帧格式固定为 `event / data / id` 三段；`id` 即该事件的 `seq`（十进制字符串）：

```
event: project.task.claimed
id: 43
data: {"event":"project.task.claimed","seq":43,...}

```

- `data` 是完整信封 JSON（消费方不依赖 SSE event 名也能工作，event 名只是便利）。
- 断线重连：`GET .../stream?after=<最后收到的 seq>`，服务端补发 seq 更大的事件。
- 心跳/注释行（`:` 开头）不是事件，消费方忽略。
- HTTP 错误码只属于传输层；**业务失败也必须是事件**
  （`project.run.failed`、`project.gate.blocked`、`project.artifact.rejected`），
  保证"任务失败但前端永久等待"在协议层不可能发生。

## 5. 首批事件清单（v1，共 24 个）

| family | 事件 | payload 关键字段（推荐） |
|---|---|---|
| task | `project.task.created` | task_id, title, depends_on, mode |
| task | `project.task.claimed` | task_id, agent_id, lease_expires_at |
| task | `project.task.status_changed` | task_id, from, to |
| task | `project.task.failed` | task_id, stop_reason |
| task | `project.task.retried` | task_id, attempt |
| run | `project.run.started` | run_id, task_id |
| run | `project.run.finished` | run_id, stop_reason, usage |
| run | `project.run.failed` | run_id, error{code,message} |
| artifact | `project.artifact.uploaded` | artifact_id, receipt{...RECEIPT_FORMAT} |
| artifact | `project.artifact.version_created` | artifact_id, parent_artifact_id |
| artifact | `project.artifact.approved` | artifact_id, approved_by |
| artifact | `project.artifact.rejected` | artifact_id, reason |
| handoff | `project.handoff.sent` | handoff_id, to_agent_id |
| handoff | `project.handoff.accepted` | handoff_id, accepted_by |
| handoff | `project.handoff.rejected` | handoff_id, reason |
| review | `project.review.requested` | review_id, reviewer_role |
| review | `project.review.concluded` | review_id, conclusion |
| gate | `project.gate.evaluated` | gate_id, leaves[]（acceptance.py 的逐条结果） |
| gate | `project.gate.passed` | gate_id |
| gate | `project.gate.blocked` | gate_id, unchecked[] |
| gate | `project.gate.escalated` | gate_id, reason |
| agent | `project.agent.joined` | agent_id, capabilities[] |
| agent | `project.agent.left` | agent_id |
| agent | `project.agent.lease_lost` | agent_id, task_id |

payload 具体字段以服务端实现为准（规则 1/2 保证只会更多不会变义）。

## 6. 与既有通道的关系

- `agent_turn_events`（单会话轮次事件）继续按 `(turn_id, sequence)` 幂等，不变。
- 本契约的项目事件走 `events` / `event_outbox` 落库，由 A 统一发帧；
  团队视图（W2.3）与工作流推进器（W3.2）只消费 `project.*`，不回读轮次表。
- 两套流的 `seq` 各自独立计数，不共享命名空间。

### 6.1 桥上的 seq 归属（stream_bridge 配套约定，与 A 对齐 2026-09-26）

`publish(seq=...)` 的"同 topic 严格递增"约束决定了流的两种 seq 归属：

| topic | seq 归属 | 语义 |
|---|---|---|
| `turn:{turn_id}` | **镜像** `agent_turn_events.sequence` | 权威序列只有一份，桥不二次记账 |
| `conversation:{id}` | **桥自增** | 语义收窄为**会话生命周期信号**（开始/结束/轮次边界）；不承载全量回放，历史一律回 `agent_turn_events` 分页接口 |
| `project:{id}` | **镜像**事件信封的 `seq` | 本契约第 4 节的 SSE `id:` 即此 seq |

原则：凡有权威序列的流一律镜像；凡聚合多源的流（各轮 sequence 各自从 1 起算）
只能桥自增，并把语义收窄到生命周期信号——两套 seq 不打架，消费方游标不换算。
