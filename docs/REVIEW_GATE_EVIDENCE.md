# Review、Gate 与 Evidence 规则

## Review

- Agent 和系统可以提出 `NEEDS_REVISION` 或 `BLOCKED`。
- `APPROVED` 必须由真实 `member` 提交，Agent 不能伪造成员审批。
- Review 必须绑定一个项目内的 Task、Artifact 或 Handoff，并保存 findings。
- `fatal` finding 阻断下游；`major` 和 `minor` 必须进入风险清单，未解决的 `major` 默认不放行。

## Gate

Gate 是规则快照，不等同于一条 Review。它保存目标、规则、阻断 findings、是否需要人工批准、状态和批准主体。

```text
OPEN -> PASSED
  \-> FAILED
  \-> BLOCKED
```

只有 `PASSED` Gate 才允许下游正式引用。Gate 通过时必须能定位支撑它的 Review 和 Evidence。

## Evidence

Evidence 将论文结论或审核判断连接到 Artifact、Run、Event 或外部来源。关键结论至少需要一条可追溯 Evidence；外部来源必须记录引用地址或来源标识。

