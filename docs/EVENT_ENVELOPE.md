# 事件信封

每个事件至少包含：

```json
{
  "event_id": "uuid",
  "project_id": "uuid",
  "sequence": 1,
  "event_type": "task.approved",
  "actor": "member-001",
  "actor_kind": "member",
  "object_type": "task",
  "object_id": "uuid",
  "idempotency_key": "optional-key",
  "schema_version": "1.0",
  "payload": {},
  "created_at": "2026-09-12T00:00:00Z"
}
```

事件是追加记录，业务状态和事件写入必须在同一事务中完成。消息总线只能投递事件，不能成为事实来源；重复投递由消费者幂等处理。

## 事务 Outbox

每条新事件在同一数据库事务中同步创建一条 `EventOutbox` 记录。Outbox 只描述投递状态，不改变事件事实：

```text
PENDING -> PROCESSING -> DELIVERED
                     \-> FAILED -> PENDING/重试窗口
```

当前存储接口提供：

- 按项目或状态查询 outbox。
- 查询已到达 `available_at` 的待投递记录。
- 通过 `lock_expires_at` 原子领取记录；锁过期后可再次领取。
- 投递成功标记，重复标记保持幂等。
- 投递失败记录错误、累计 `attempts` 并设置下一次重试时间。

`EventOutboxDispatcher` 只依赖平台仓储和一个发布器接口，因此可以在本地测试发布器、NATS 或其他消息系统之间替换。单次投递流程为“领取 -> 读取事件 -> 发布 -> 成功确认”；发布异常会按指数退避回写 `FAILED`。

事件 outbox 仍不是消息总线本身。NATS JetStream 适配器和实际消费者将在后续 Agent Gateway 阶段接入；在此之前，`PENDING`/`FAILED` 是明确的待投递事实，不能被解释为已经广播成功。
