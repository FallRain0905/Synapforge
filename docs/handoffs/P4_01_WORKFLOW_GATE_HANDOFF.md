# P4-01 阶段 4 工作流与门禁开发交接

> 交接状态：`PASS_WITH_ASSUMPTIONS`
>
> 任务编号：`P4-01`
>
> 完成日期：2026-09-14
>
> 下一主线：`P4-02` Fanout 多接收方收据、风险关闭/重新审核、HTTP Agent 主体认证和跨对象 Gate 快照

## 1. 本轮目标

补齐阶段 4 开发版的核心后端语义：

- Handoff 拒绝、返工修订和汇总输入关系。
- Handoff Gate 对下游任务的正式阻断。
- Review 的统一风险规则和 Evidence 关系。
- Gate 对 Review、Evidence、风险摘要的追溯。
- 审核中心后端聚合接口。
- Gateway 接收方拒绝命令。

本轮不宣称真实 PostgreSQL/MinIO、多实例竞争、OIDC、Fanout 多收据或前端审核中心生产完成。

## 2. 已完成

### 2.1 Handoff 拒绝与返工

Handoff 新增：

```text
receipt_status: PENDING | ACCEPTED | REJECTED
decision_reason
decision_findings
revision_of_handoff_id
revision_number
input_handoff_ids
```

接收方拒绝后：

```text
receipt_status = REJECTED
status = NEEDS_REVISION
```

被拒绝的 Handoff 不能再次接受。返工必须创建新的 Handoff，并通过 `revision_of_handoff_id` 指向原交接；原交接仍保留为审计事实。

### 2.2 汇总交接与下游门禁

`AGGREGATE` Handoff 必须携带 `input_handoff_ids`。每一个输入交接必须：

1. 属于同一项目。
2. 为 `ACCEPTED`。
3. 状态为 `PASS` 或 `PASS_WITH_ASSUMPTIONS`。
4. 如果要求人工批准，则对应 Handoff Gate 必须为 `PASSED`。

下游 Task 领取现在同时检查输入 Handoff 的接受状态和 Gate 状态。仅仅“收到交接”不能绕过人工审核门禁。

### 2.3 统一风险规则

新增 `apps/api/app/risk_rules.py`：

- `fatal`：始终阻断。
- 未解决 `major`：阻断批准。
- `minor`：进入风险登记，但默认不阻断。
- 缺失稳定 code 时生成确定性 code。
- 兼容旧字段 `text`，同时规范化为 `message`。
- Gate 保存阻断 findings 和风险摘要。

Review 以 `APPROVED` 提交时，如果存在未解决 `fatal` 或 `major`，服务端返回 `review_blocked_by_findings`，不会创建 Gate、Review 或事件副作用。

### 2.4 Review、Gate、Evidence 关系

Review 新增 `evidence_ids`；Gate 新增：

```text
review_ids
evidence_ids
risk_summary
```

审核中心接口：

```text
GET /api/projects/{project_id}/review-center
```

返回项目范围内的 Gates、Reviews、Evidence 和风险登记项，风险登记项由 Review findings 规范化生成，并保留 Review 与目标对象关系。

### 2.5 Gateway 与本地 Agent

新增：

```text
agent.handoff.reject
```

Gateway 强制使用当前连接 Agent 作为拒绝主体，校验项目能力、Handoff 项目归属和接收方身份；Agent Client 增加 `reject_handoff()`。

## 3. 数据库与契约变化

SQLite 开发 Store 增加兼容列：

- `handoffs.input_handoff_ids`
- `handoffs.revision_of_handoff_id`
- `handoffs.revision_number`
- `handoffs.decision_reason`
- `handoffs.decision_findings`
- `reviews.evidence_ids`
- `gates.review_ids`
- `gates.evidence_ids`
- `gates.risk_summary`

PostgreSQL 新增：

```text
apps/api/migrations/011_workflow_review_relations.sql
```

领域 Schema、领域目录、Repository Protocol、SQLite Repository 和 PostgreSQL Repository 已同步。

Bundle 导出恢复已同步保存和恢复以上交接修订、汇总输入、Review Evidence 和 Gate 关系字段，并增加引用校验。

## 4. 测试与验证

新增或扩展：

- `apps/api/test_workflows.py`：拒绝返工、汇总门禁、风险阻断和 Review Center 关系测试。
- `apps/api/test_gateway.py`：Gateway 接收方拒绝和重复命令结果测试。
- `apps/api/test_platform_contracts.py`：迁移集合更新至 `001` 至 `011`，Gateway 命令数量更新为 11。

本轮验证结果：

```text
API：86 passed，3 个真实 PostgreSQL/MinIO 条件测试 skipped
Agent：114 passed，9 个 ConPTY/平台条件测试 skipped
导出/恢复：5 passed
目标 Python 模块 py_compile：通过
领域 Schema、Gateway Schema JSON 解析：通过
```

## 5. 未完成与风险

1. `FANOUT` 当前只完成类型和接收者结构校验；仍使用单个 Handoff 收据，尚未实现每个接收方独立的 Accepted/Rejected 收据。
2. HTTP Handoff 接收/拒绝已从请求体推导人工成员主体，但 Agent HTTP 接收尚未接入正式设备会话身份；Gateway Agent 路径已完成当前连接身份校验。
3. 风险登记项当前由 Review findings 派生，没有独立的风险关闭表；`resolved=true` 可用于审核输入，但正式风险关闭、重新审核和责任人操作仍待 P4-02。
4. Gate 保存当前 Review/Evidence 引用和风险摘要，尚未实现跨对象 Gate 快照和受影响下游自动回退。
5. PostgreSQL 迁移脚本和 Repository 已同步，但当前环境未提供真实 PostgreSQL/MinIO，尚未做 SQL 参数、RLS、并发唯一约束和跨实例事件验证。
6. Gateway 业务副作用与命令结果跨实例统一事务仍未完成。

## 6. 下一步

### P4-02

1. 将 Fanout Handoff 拆为可独立确认的接收记录，汇总任务必须等待所有必需接收人完成。
2. 增加风险关闭/重新打开、责任人和关闭证据，禁止只修改 Review 文本来清除风险。
3. 为 HTTP Agent 接收、拒绝和审核接入设备 Token 与项目能力主体校验。
4. 将 Gate 从单目标规则升级为可记录多前置对象快照，并在输入版本变化时自动失效。
5. 在真实 PostgreSQL 环境执行迁移 `001` 至 `011`，补充 Review/Handoff 并发与 RLS 集成测试。

## 7. 复现入口

```text
python -m unittest discover -s apps/api -p 'test_workflows.py' -v
python -m unittest discover -s apps/api -p 'test_gateway.py' -v
python -m unittest discover -s apps/api -p 'test_*.py'
python -m unittest discover -s apps/agent -p 'test_*.py'
```

不要把 `apps/api/vendor` 目录加入优先级最高的 `PYTHONPATH`；直接使用项目已有的 Python 环境运行测试。
