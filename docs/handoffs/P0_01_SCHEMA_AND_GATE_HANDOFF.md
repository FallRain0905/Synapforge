# 阶段 0 第一执行批次交接

> 交接状态：PASS_WITH_ASSUMPTIONS
>
> 日期：2026-09-12
>
> 对应任务：基线修复、P0-03、P0-04、P0-10、P0-11

## 1. 本轮目标

恢复上一轮中断后的 SQLite 开发基线，完成核心领域 Schema 的第一版，收紧服务端任务状态机和人工审批门禁，并用纯后端测试验证接力式与分发式协作。

本轮明确不进入 PostgreSQL、OIDC、NATS、Temporal、Yjs、Docker Runner 或生产多租户实现。

## 2. 实际完成内容

### 2.1 基线修复

- 修复 `handoffs.schema_version` 新增后种子数据仍使用旧列数插入的问题。
- 修复 Handoff 创建时的显式列插入。
- 修复 Review 创建时的显式列插入。
- 修复 Artifact 扩展字段加入后种子、创建和导入写入语句的列兼容。
- 修复 Task 新字段的读取和 Pydantic 返回。

### 2.2 正式领域 Schema

- 新增 `packages/contracts/domain.schema.json`。
- 更新 `packages/contracts/domain.json`，登记 Schema 版本和核心枚举目录。
- 新增 `packages/contracts/task-state-machine.json`，固化状态转换和 `APPROVED` 的 Review-only 规则。
- Schema 覆盖 Task、Handoff、Artifact、Run、Review、Gate、Evidence、Event、TaskTransition 和 IdempotencyEnvelope。
- Pydantic 模型增加 ReviewerKind、GateStatus、InformationBoundary、Evidence、Gate 和 IdempotencyEnvelope。
- Artifact 增加输入成果物、Git commit、快照、批准主体/时间和下游引用字段。
- Event 增加 actor kind、对象引用、幂等键和 schema version 的领域字段。

### 2.3 服务端状态机与门禁

- Task 更新统一检查状态转换矩阵。
- 直接将任务写入 `APPROVED` 被拒绝，批准必须经过 Review。
- `APPROVED` Review 强制要求 `reviewer_kind=member`。
- Review 开始驱动 Task、Artifact 和 Handoff 的开发版状态变化。
- 任务领取检查依赖任务是否全部 `APPROVED`。
- 任务领取检查 `input_artifacts` 是否全部为批准且允许下游引用。
- Artifact 不能通过创建接口直接声明为 `APPROVED`。
- Handoff 校验任务和成果物属于同一项目，并支持结构化 receiver。
- 结果提交成功但需要人工确认时只能进入 `WAITING_REVIEW`，不能直接进入 `APPROVED`。

### 2.4 纯后端契约测试

新增 `apps/api/test_workflows.py`，覆盖：

- 接力式流程：A Agent 产出、创建结构化交接、Agent Review 被拒绝、成员批准、B Agent 使用批准输入领取下游任务。
- 分发式流程：B/C 并行执行、汇总任务在依赖未批准前被阻断、全部批准后允许汇总 Agent 领取。
- 重复结果提交使用同一幂等键时不会重复推进或重复产生事件。
- 直接批准和非法状态跳转被拒绝。
- Schema 目录包含核心对象定义。

## 3. 修改文件

- `apps/api/app/contracts.py`
- `apps/api/app/store.py`
- `apps/api/app/main.py`
- `apps/api/test_workflows.py`
- `packages/contracts/domain.schema.json`
- `packages/contracts/domain.json`
- `packages/contracts/task-state-machine.json`
- `docs/IMPLEMENTATION_STATUS.md`
- `docs/PROJECT_EXECUTION_PLAN.md`
- `docs/handoffs/P0_01_SCHEMA_AND_GATE_HANDOFF.md`

## 4. 数据库和协议变化

SQLite 开发 Schema 增加或迁移了：

- Task：父任务、依赖、验收标准、能力、deadline、信息边界、资源策略、人工审批。
- Handoff：schema version。
- Artifact：输入成果物、Git commit、快照、创建主体类型、批准信息、下游引用标记。
- Review：reviewer kind。

这些变化通过 `_ensure_columns()` 做开发版向后兼容迁移。当前仍没有正式数据库迁移工具，不能视为 PostgreSQL 生产迁移方案。

## 5. 已运行验证

命令：

```powershell
$env:PYTHONPATH = (Resolve-Path 'apps\api\vendor').Path
& 'C:\Users\19855\Documents\ChatGPT\数学建模\C题工作区\.venv\Scripts\python.exe' -m unittest discover -s apps\api -p 'test_*.py' -v
```

结果：10 个测试全部通过。

其他验证：

- `python -m compileall -q apps\api`：通过。
- API 模块导入：通过，标题为 `Math Agent Platform API`，版本 `0.1.0`。
- `apps/web` 执行 `npm run build`：通过。

## 6. 尚未完成

- P0-04 仍是开发版状态机，不包含真实成员身份、RBAC/ABAC 和并发事务保证。
- 尚未建立独立 Gate、Evidence 数据表、规则快照和风险规则引擎。
- Handoff 尚未有独立接收、拒绝、返工和幂等状态协议。
- 任务依赖尚未做环检测，取消、重试、租约冲突语义仍需补全。
- Event 扩展字段尚未持久化到事件表，也没有事务 outbox。
- 信息边界仍是 Manifest/路径级检查，不是系统调用级文件和网络审计。
- 还没有 PostgreSQL、对象存储、登录、组织/队伍/成员和生产 Agent 授权。

## 7. 风险与注意事项

- `domain.schema.json` 是跨语言契约的第一版，后续任何破坏性字段变化必须附迁移方案。
- 当前 SQLite 的 `_ensure_columns()` 只适合开发环境，生产环境必须由版本化迁移管理。
- `update_task` 保留了开发版直接推进部分非批准状态的能力，正式权限系统接入后要改为基于 actor 和角色的授权。
- 测试中的成员审批使用固定 `member-lead` 字符串，不能当作真实身份认证。
- 现有 `apps/api/data/platform.db` 可能由旧代码创建过，启动后应检查迁移字段和备份，不要直接删除数据库。

## 8. 下一轮建议

按执行计划进入阶段 0 剩余契约工作：

1. P0-02：建立领域术语表。
2. P0-05：冻结 Artifact 生命周期和批准版本规则。
3. P0-06：补齐 Handoff 接收、返工、汇总和幂等协议。
4. P0-07：建立 Review、Gate、Evidence 规则和开发版持久化边界。
5. P0-08/P0-09：冻结 Event 信封和信息边界 Schema。
6. P0-12 至 P0-15：补充状态机性质测试、权限矩阵、威胁模型和导出恢复协议。
7. P0-16：完成阶段 0 评审，决定是否允许进入阶段 1。

## 9. 启动和复现

后端测试仍使用现有 `apps/api/vendor` 和 C 题工作区 Python 环境。当前没有新增依赖，不需要数据库重建；启动 API 前应保留 `apps/api/data/platform.db`，让开发版迁移逻辑补齐新增字段。
