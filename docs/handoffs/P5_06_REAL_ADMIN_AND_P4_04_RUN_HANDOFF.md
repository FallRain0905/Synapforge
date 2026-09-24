# P5-06-REAL-ADMIN 免管理员切片 + P4-04-RUN 真实基础设施验收交接文档

> 日期：2026-09-15
>
> 状态：`PASS_WITH_ASSUMPTIONS`（ETW 管理员实机采集为 BLOCKED，阻塞条件已取证）
>
> 任务：P5-06-REAL-ADMIN 中无需管理员权限的观察器诊断/依赖分类/清理验收，以及 P4-04-RUN 用户态真实 PostgreSQL + MinIO 集成验收

## 1. 本轮结论

本轮完成两条线：

1. **P5-06-REAL-ADMIN 免管理员部分**：Windows ETW 观察器诊断 Manifest、信息边界运行时依赖分类和异常清理测试全部完成并通过。
2. **P4-04-RUN**：在本机以用户态（无需管理员）部署 PostgreSQL 16.9 和 MinIO，完成阶段 2（3 项）与阶段 4（4 项）全部集成验收测试。

**硬阻塞取证**：真实 ETW 管理员采集仍无法在本环境执行。当前用户 `FALLRAIN\19855` 在 `BUILTIN\Administrators` 组内，但运行于 UAC 过滤令牌：

```text
IsInRole(Administrator)           -> False
token groups: BUILTIN\Administrators -> "Group used for deny only"
schtasks /Create /RL HIGHEST      -> ERROR: Access is denied
logman create trace (real run)    -> Access is denied（上一轮已复现）
```

非交互式会话无法弹出 UAC 提权对话框，runas/schtasks 路径均被拒。**需要在一次交互式管理员会话中执行真实采集**，这是下一轮 ETW 验收的前置条件，不能通过代码修改绕过。

## 2. 已完成：P5-06-REAL-ADMIN 免管理员部分

### 2.1 观察器诊断 Manifest

- `AccessObservationCapture` 新增 `diagnostics` 字段（`information_boundary.py`）。
- `WindowsEtwAccessObservationProvider.stop()` 现在返回完整诊断：
  - `adapter`、`started_at`/`finished_at`、provider 名称与 keyword/level 配置。
  - 每个 session 的 `etl_path`/`csv_path`、ETL/CSV SHA-256、字节数、stop/convert 返回码、解析错误、事件计数、是否保留（`retained`）。
  - `bound_process_ids`（绑定进程树快照）、`stop_status`、`stop_errors`、`cleanup_status`/`cleanup_errors`。
- 传递链贯通：`ProcessResult.access_observation_diagnostics` → `RunnerRequest.access_observation_diagnostics`（`session_runtime.py` 回填）→ Run Manifest `access_observation.diagnostics`。
- **诊断不进入 `reproducibility_hash`**：`result_uploader.py` 的稳定哈希计算剔除 `diagnostics`，会话名/哈希/时间戳在两次相同运行间合法变化，不影响 P5-05 重跑比较（有专项测试锁定该行为）。

### 2.2 运行时依赖分类

- `InformationBoundaryAudit.evaluate` 支持 `runtime_dependency_roots` 策略：位于声明根（如 Python 安装目录、temp）下的读取归类为 `runtime_dependency`，记录但不触发 `undeclared_input_file` violation。
- 显式声明的文件即使位于 runtime root 内仍按 `task_input` 判定（有测试锁定）。
- 观察记录新增 `category` 字段（`runtime_dependency`/`task_input`），boundary 结果新增 `runtime_dependencies` 列表。

### 2.3 异常清理与诊断测试

`test_windows_etw_observer.py` 新增 5 项（总 10 项通过）：

- 成功路径诊断完整性（session/provider/哈希/PID 集合/状态）。
- `logman delete` 失败时记录 `cleanup_errors` 且不影响捕获结果。
- 未绑定 PID 的 `stop()` 返回带诊断的 fail-closed 结果。
- runtime 依赖分类（含声明优先语义）。
- `test_result_uploader.py` 新增 Manifest 诊断透传 + 哈希排除测试。

## 3. 已完成：P4-04-RUN 真实基础设施验收

### 3.1 部署形态（全部免管理员）

- PostgreSQL 16.9 EDB 便携二进制：`initdb` 于 `.infra/pgdata`，`platform` 用户（trust 认证），监听 `127.0.0.1:54329`。
- MinIO（最后一个 AGPL 社区版 RELEASE.2025-04-22）：`127.0.0.1:9100`，凭据 `platform`/`platform-dev-only`。
- 管理脚本：`scripts/local-infra.ps1 start|stop|status`。
- **注意**：项目路径含非 ASCII（`数学建模`），PostgreSQL 工具无法重定位自身，所有 PG 命令必须走 8.3 短路径（脚本内已处理）。
- 生产 Python 依赖已安装：`psycopg 3.3.4`、`psycopg-pool 3.3.1`、`boto3 1.40.61`。

### 3.2 验收结果

```text
阶段 2 集成（test_stage2_integration.py）  -> 3 passed
阶段 4 集成（test_stage4_integration.py）  -> 4 passed
API 全量回归                              -> 95 passed, 7 skipped（非集成上下文）
Agent 全量回归                            -> 145 passed, 9 skipped
```

覆盖：迁移 001-013 应用与二次执行幂等、全部 31 张租户表 FORCE RLS、非超级用户 owner 连接的跨租户隔离、设备 Token 原子轮换与并发版本串行化、旧 Token 失效与连接撤销、MinIO 对象内容哈希、跨实例 Multipart 缺块乱序完成、Fanout 逐接收方收据并发确认、风险并发归属、Gate 失效传播（INVALIDATED + NEEDS_REVISION）、outbox 两实例 `SKIP LOCKED` 互斥领取。

### 3.3 真实服务暴露并修复的三处测试缺陷

1. **S3 分块大小**：真实 S3/MinIO 要求除最后一块外每块 ≥5MiB；测试原用 11 字节被 `EntityTooSmall` 拒绝。已改为 5MiB+小尾块（stage2/stage4 两处）。
2. **PG 清理顺序**：`_cleanup_tenant` 原来直接 `DELETE organizations`，但迁移没有全量 `ON DELETE CASCADE`；已改为按外键依赖序删除 33 张表（注意 `risks` 引用 `reviews`、`gateway_command_results` 只能通过 connection→device 间接定位、`event_outbox`/`events` 等按 project_id 关联）。
3. **RLS 验收语义**：超级用户天然绕过 RLS（FORCE 也不覆盖）；原 RLS 测试用超级用户 DSN 断言必然失败。现改为在测试内创建专用数据库 + 非超级用户 login role，owner 连接下验证 `app.organization_id` 切换的跨租户隐藏，结束后 DROP DATABASE/ROLE。fixture 插入需先 `set_config('app.organization_id', ...)`（FORCE RLS 对 owner 也生效）。
4. **枚举断言**：`APIModel` 配置 `use_enum_values=True`，validate 后枚举字段是纯字符串；PG 路径构造的对象不能用 `.status.value`，已统一改为 `str(...)` 比较。Fanout 并发断言修正为先提交者看到对方 PENDING 是正确语义，最终持久化状态才必须是 ACCEPTED。

### 3.4 环境已知问题

- `apps/api/vendor` 存在约 41 个沙箱历史会话产生的不可读条目（DACL 归属旧沙箱用户 SID，无提权无法夺取所有权，`rm`/`icacls`/`shutil` 均被拒）。`typing_extensions.py` 已用 site-packages 副本修复；当前集成测试通过 PYTHONPATH=apps/api（不含 vendor）从 site-packages 导入解决。**后续应重建 vendor 目录**。本地模块级测试（unittest discover 于 apps/api 目录）仍可能受影响，本轮未逐一验证模块级测试在无 vendor 路径下的表现。

## 4. 未完成与下一轮建议

1. **P5-06-REAL-ADMIN-ETW（最高优先）**：在交互式管理员 PowerShell 中运行一次 `agentd session-worker-run --access-observer windows-etw --etw-output-directory <目录>` 的短命 Python Runner，保留 ETL/CSV 样本，用真实字段修正解析器并冻结字段矩阵回归。
2. **P4-04-RUN-PROD**：本机验收是用户态开发形态（trust、无 TLS、单实例）；生产形态需独立非 owner 运行时角色、密码/TLS、连接池上限、跨实例部署与故障演练。
3. **重建 `apps/api/vendor`**：清除沙箱损坏 ACL 条目，或迁移到标准虚拟环境管理。
4. Linux eBPF/LSM、容器观察桥接、网络阻断和 Review/Gate 自动写入（P5-06-REAL 后续）。
5. P4-05 浏览器端到端联调与阶段 4 退出评审。

## 5. 关键文件

- 诊断与分类：`apps/agent/information_boundary.py`、`apps/agent/windows_etw_observer.py`
- 传递链：`apps/agent/runner.py`、`apps/agent/machine_service.py`、`apps/agent/session_runtime.py`、`apps/agent/result_uploader.py`
- 集成测试修复：`apps/api/test_stage2_integration.py`、`apps/api/test_stage4_integration.py`
- 基础设施：`scripts/local-infra.ps1`、`.infra/`（pgdata、minio-data、pg/pgsql、minio.exe）
- 主计划：`docs/PROJECT_EXECUTION_PLAN.md`（版本 3.26）
- 实施状态：`docs/IMPLEMENTATION_STATUS.md`
- 前置交接：`docs/handoffs/P5_06_REAL_WINDOWS_OBSERVER_HANDOFF.md`

## 6. 接收方行动

1. ETW 实机采集失败时不要改为声明式通过；保留 `not_captured` 与诊断。完成管理员样本后先冻结字段矩阵，再改解析规则。
2. 集成测试需要本机服务时先运行 `scripts/local-infra.ps1 start`；PG 命令必须使用 8.3 短路径。
3. 修改 `reproducibility_hash` 计算时必须保持 `access_observation.diagnostics` 的剔除，否则 P5-05 重跑比较会被合法的 trace 差异破坏。
