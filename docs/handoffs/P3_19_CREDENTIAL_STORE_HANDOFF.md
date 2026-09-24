# 阶段 3 P3-19 系统凭据存储交接

## 1. 本轮目标

把长期设备 Token 从命令行和业务状态库中移到操作系统凭据边界，并让 Gateway/Service 在默认情况下
从系统凭据存储取证。本轮完成开发版 Windows Credential Manager 适配和 CLI 接入；不宣称目标部署环境
的跨登录会话持久化已经完成。

## 2. 已完成

### 2.1 凭据存储边界

新增 `apps/agent/credential_store.py`：

- `CredentialStore` Protocol 统一 `put/get/delete` 操作。
- `CredentialInvalid`、`CredentialNotFound`、`CredentialPermissionDenied`、
  `CredentialBackendUnavailable` 和 `CredentialStoreError` 提供稳定错误边界。
- 所有目标名、秘密值、NUL 字符、UTF-8 编码和最大长度在进入后端前校验。
- `device_token_target(device_id)` 统一生成
  `MathAgentPlatform/device-token/<device_id>` 目标名。
- `InMemoryCredentialStore` 仅供测试或显式开发注入，默认工厂不会将它作为 Windows 后端。

### 2.2 Windows Credential Manager

`WindowsCredentialManager` 通过 `Advapi32` 的 `CredWriteW`、`CredReadW`、`CredDeleteW` 实现 Generic
Credential 存储，使用 `CRED_PERSIST_LOCAL_MACHINE`。读取时重新检查 Blob 大小和 UTF-8，删除不存在
目标返回 `False`。

`ERROR_ACCESS_DENIED`、凭据不存在和后端异常转换为稳定错误。`ERROR_NO_SUCH_LOGON_SESSION (1312)`
转换为 `credential_persistence_unavailable`，不会静默降级为 Session Credential。

### 2.3 Agent 接入

- `agentd credential-save` 使用隐藏输入或 `--token-stdin` 写入 Credential Manager。
- `agentd credential-delete` 删除设备 Token。
- `gateway-run`、`service-run` 的 `--device-token` 改为可选；缺省时根据 `device_id` 读取 Credential Manager。
- 显式 `--device-token` 仍保留作为开发兼容路径，但不会写入 `LocalAgentState`。
- CLI 直接运行时会自动加入仓库根目录，保证共享包可导入。

## 3. 重要设计限制

设备 Token 属于长期机器凭据，不进入本地 SQLite 状态库、Gateway 事件或普通日志。当前设计没有把
Credential Manager 的用户凭据伪装成 LocalSystem 的服务凭据：Windows Service 使用哪一个身份读取
凭据、凭据 ACL 如何授权，必须在目标部署拓扑中单独验收。

本机真实 API 探测结果：使用 `CRED_PERSIST_LOCAL_MACHINE` 写入时返回
`ERROR_NO_SUCH_LOGON_SESSION (1312)`。因此当前环境不能把真实往返测试当作长期持久化已完成；代码
明确失败，测试使用 Fake API 覆盖成功和错误路径。

## 4. 变更文件

- `apps/agent/credential_store.py`
- `apps/agent/test_credential_store.py`
- `apps/agent/agentd.py`
- `apps/agent/requirements.txt`
- `README.md`
- `docs/PROJECT_EXECUTION_PLAN.md`
- `docs/IMPLEMENTATION_STATUS.md`
- `docs/AGENT_DEVICE_CONNECTION_DESIGN.md`

## 5. 验证结果

```text
凭据存储专项测试：8 passed
apps/agent full suite：114 passed, 1 skipped
apps/api full suite：75 passed, 2 skipped
compileall：passed
agentd gateway-run --help：passed
agentd credential-save --help：passed
```

## 6. 尚未完成

- 目标用户、LocalSystem/服务账户和用户配置文件组合下的 Credential Manager ACL 与重启/注销恢复。
- 设备 Token 轮换、双 Token 过渡窗口、撤销后的本地清理和丢失设备处置。
- 正式 OIDC/设备会话认证和 pairing 端点的防滥用策略。
- Windows Service 使用系统身份时的安全凭据传递设计；不能直接假定服务账户可以读取交互用户凭据。
- 真实 PostgreSQL/RLS/并发注册、Gateway 长时间断线和设备生命周期组合验收。

## 7. 下一步

1. 先确定 Machine Service 与 User Session Worker 各自读取凭据的身份边界，再实现 Token 轮换协议。
2. 在真实目标 Windows 账户和部署配置中验证 Credential Manager 持久化、ACL、重启、注销和服务恢复。
3. 增加设备 Token 轮换/撤销清理 API 和审计事件。
4. 继续真实 PostgreSQL/MinIO、Windows Service 和 Gateway 长时间恢复验收。

## 8. 交接结论

P3-19 开发版完成，阶段 3 仍为 `PARTIAL`。系统凭据存储边界已存在，默认运行路径已不再要求把
Token 放在命令行中；但当前环境的长期 Credential Manager 写入被 Windows 返回码 1312 阻断，
生产凭据生命周期和服务身份访问仍不能视为完成。
