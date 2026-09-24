# 阶段 3 P3-04 PostgreSQL 设备与 Gateway Repository 交接

> 交接状态：PASS_WITH_ASSUMPTIONS
>
> 日期：2026-09-13
>
> 对应任务：P3-04 将 P3-01/P3-02 设备与 Gateway 连接语义扩展到 PostgreSQL Repository

## 1. 本轮目标

减少 SQLite 开发 Store 和 PostgreSQL 目标运行时之间的协议差距：

1. 实现 PostgreSQL 的设备、配对和设备 Token 查询/写入。
2. 实现项目范围能力 Token 的签发、查询、撤销和校验。
3. 实现 AgentConnection 的建立、读取、关闭和设备撤销联动。
4. 实现连接入站/出站序号的事务锁和心跳更新。
5. 用静态契约和领域映射测试提前捕获 SQL 方言或字段映射错误。

## 2. 实际完成内容

### 2.1 Repository 映射

`apps/api/app/postgres_repository.py` 新增：

- `_device()`：将 PostgreSQL 行映射为 `Device`，不返回 `public_key` 和 `device_token_hash`。
- `_device_project_grant()`：映射项目授权并隐藏 `token_hash`。
- `_agent_connection()`：映射连接状态和序号。
- `_device_pairing()`：映射配对状态，只有创建响应携带原始配对码。

### 2.2 设备与授权方法

新增方法与 SQLite Store 保持相同命名和错误语义：

- `get_device()`、`list_devices_for_member()`。
- `create_device_pairing()`、`register_device()`、`resolve_device_token()`。
- `revoke_device()`：联动撤销项目 Token 和现有连接。
- `create_device_project_grant()`、`list_device_project_grants()`。
- `revoke_device_project_grant()`、`resolve_device_project_token()`。

权限边界：

- 创建/撤销设备配对要求组织 owner/project_lead。
- 签发/撤销项目授权要求项目 owner/project_lead。
- 设备组织必须与项目组织一致。
- 设备必须为 active 才能签发项目 Token 或建立连接。

### 2.3 连接与 Gateway 方法

新增：

- `open_agent_connection()`。
- `get_agent_connection()`。
- `record_gateway_receive()`：使用 `FOR UPDATE`，实现连续序号、重复序号和缺口拒绝。
- `next_gateway_send_sequence()`：使用 `FOR UPDATE` 推进出站序号。
- `record_agent_heartbeat()`：检查 device/agent/session/connection 四元身份，更新三类在线时间。
- `close_agent_connection()`。

所有方法都通过现有 `transaction()` 进入事务，并继承 `app.organization_id` 的 RLS 作用域。

## 3. 测试与验证

更新 `apps/api/test_postgres_repository.py`：

- 检查设备/连接 Repository 方法存在。
- 检查 Device 映射不泄漏公钥和 Token 哈希。
- 检查连接入站/出站序号映射。

更新 `apps/api/test_platform_contracts.py`：

- 确认 `005_agent_devices.sql` 包含四张表和 RLS。
- 确认迁移文件不混入 SQLite `?` 占位符。

本轮验证结果：

```text
后端全量 unittest：57 passed，2 skipped
本地 Agent 测试：6 passed
compileall：通过
domain.schema.json / domain.json：解析通过
```

## 4. 尚未完成与风险

- 当前机器没有 PostgreSQL，无法执行 `001` 至 `005` 迁移。
- 尚未验证 PostgreSQL RLS 在设备表、连接表和项目授权表上的真实行为。
- 尚未做两个 Gateway 实例并发推进同一连接序号、设备撤销与握手竞态测试。
- PostgreSQL Repository 尚未覆盖完整 `PlatformRepository`，API 默认仍使用 SQLite。
- `register_device()` 仍未执行公钥 challenge/signature 持有证明。
- 项目 Token 尚未强制注入 Gateway 的任务领取、租约、Run 和 Artifact 路径。

## 5. 下一步

1. 启动真实 PostgreSQL，执行所有迁移并运行 RLS/事务集成测试。
2. 把 API Store 选择抽象成 Repository 工厂，禁止生产模式悄悄回落 SQLite。
3. 为 Gateway 增加项目 Token/capability 上下文，并接入任务领取与租约续期。
4. 进入 Machine Agent Service：自动重连、心跳调度、进程监督和本地紧急停止。
5. 在真实服务可用后补跨实例序号、撤销竞态和长连接恢复测试。

## 6. 复现命令

```powershell
$env:PYTHONPATH = "$(Resolve-Path apps/api);$(Resolve-Path apps/api/vendor)"
C:\Users\19855\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest discover -s apps/api -p 'test_*.py' -v
$env:PYTHONPATH = "$(Resolve-Path apps/agent);$(Resolve-Path apps/api);$(Resolve-Path apps/api/vendor)"
C:\Users\19855\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest discover -s apps/agent -p 'test_*.py' -v
```

不要把静态 Repository 测试解释为 PostgreSQL、RLS、TLS 或跨实例 Gateway 已经生产验收。
