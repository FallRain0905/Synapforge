# 阶段 3 P3-18 设备 challenge/signature 持有证明交接

## 1. 本轮目标

将设备注册从“提交公钥并登记指纹”收紧为“提交一次性 pairing 凭据，并证明本地确实持有对应
Ed25519 私钥”。本轮完成开发版协议、SQLite/PostgreSQL 存储接入、CLI 参数和回归测试；不宣称
生产设备身份体系已经完成。

## 2. 已完成

### 2.1 共享身份协议

新增 `packages/device_identity/__init__.py`，API 与测试工具共享以下规则：

- 注册上下文固定为 `math-agent-platform/device-pairing/v1`。
- 签名消息采用长度分隔字段，依次绑定 `pairing_id`、`challenge`、`agent_id`、`device_id` 和
  规范化公钥指纹，避免字段拼接歧义和身份替换。
- 只接受 Ed25519 公钥，支持 PEM 和 OpenSSH 表示。
- 指纹基于 DER `SubjectPublicKeyInfo` 计算 SHA-256，不再对用户输入的公钥文本直接哈希。
- 签名采用严格长度检查的 Base64URL 表示，服务端验签失败时 fail-closed。

API 侧的 `apps/api/app/device_identity.py` 是共享包的导入边界，便于存储层保持应用内接口稳定。

### 2.2 Pairing 与注册存储

- `DevicePairing` 创建时生成高熵一次性 challenge。
- 响应只在创建结果中返回原始 `pairing_code` 和 `challenge`；数据库只保存 `code_hash` 与
  `challenge_hash`。
- `DeviceRegisterRequest` 增加 `pairing_id`、`challenge`、`challenge_signature`，并要求明确的
  `device_id`。device ID 必须参与签名，服务端不再为新注册随机生成 ID。
- SQLite Store 和 PostgreSQL Repository 都按 pairing ID 检查 pairing code、状态、过期时间和
  challenge，再验证公钥签名，最后才创建设备并消费 pairing。
- pairing 消费仍为一次性操作；错误签名、错误 challenge、错误 pairing ID、错误 device ID、
  公钥替换和重放均不会产生设备或消费有效 pairing。
- PostgreSQL 增量迁移为 `apps/api/migrations/008_device_registration_challenge.sql`。
  历史 pairing 没有可验证 challenge 时保持不可注册，必须创建新 pairing。

### 2.3 CLI

`apps/agent/agentd.py device-register` 新增：

- `--pairing-id`
- `--challenge`
- `--challenge-signature`
- `--device-id` 现在必填
- `--private-key` 可选。提供 PEM Ed25519 私钥时，本地生成公钥和签名，私钥不进入 HTTP 请求。

也可以使用 `--public-key` 与预先生成的 `--challenge-signature`。如果同时提供私钥、公钥或签名，
CLI 会检查它们是否匹配。

## 3. 变更文件

- `packages/device_identity/__init__.py`
- `apps/api/app/device_identity.py`
- `apps/api/app/contracts.py`
- `apps/api/app/store.py`
- `apps/api/app/postgres_repository.py`
- `apps/api/migrations/005_agent_devices.sql`
- `apps/api/migrations/008_device_registration_challenge.sql`
- `apps/agent/agentd.py`
- `packages/contracts/domain.json`
- `packages/contracts/domain.schema.json`
- `apps/api/test_devices.py`
- `apps/api/device_test_support.py`
- `apps/api/test_agent_capability.py`
- `apps/api/test_gateway.py`
- `apps/api/test_platform_contracts.py`

## 4. 验证结果

在项目工作区 Python 运行时执行：

```text
apps/api：75 passed, 2 skipped
apps/agent：106 passed, 1 skipped
compileall apps/api/app apps/agent packages：passed
packages/contracts/domain.json：JSON parse passed
packages/contracts/domain.schema.json：JSON parse passed
```

设备专项包含 8 项测试，覆盖 pairing challenge 哈希、成功注册、规范化指纹、错误签名、错误
challenge、错误 pairing ID、device ID 替换、公钥替换和 pairing 重放。

## 5. 尚未完成

- 设备私钥还没有接入 Windows Credential Manager 或其他系统密钥环；目前 `--private-key` 只是
  开发版本地读取方式，不能作为生产凭据管理方案。
- pairing 创建/注册尚未接入正式 OIDC、CSRF、防滥用速率限制和审计告警策略。
- 尚未完成真实 PostgreSQL 迁移、RLS、并发注册竞态、设备撤销竞态和 Token 轮换/恢复验收。
- 设备公钥轮换协议尚未建立；当前设备注册成功后公钥不可变。
- 真实 Windows Service、跨账户/跨 Session 和长时间 Gateway 恢复仍待实机验证。

## 6. 下一步

1. 先接入系统密钥环抽象，至少覆盖 Windows Credential Manager 的读写、删除、轮换和权限错误。
2. 启动真实 PostgreSQL/MinIO，执行 `001` 至 `008` 迁移并验证 RLS、连接池、事务回滚和并发注册。
3. 为设备 Token 增加轮换、吊销后的恢复策略和丢失设备处置流程。
4. 完成 OIDC/正式会话边界后，再把 pairing 注册纳入生产设备认证链路。
5. 继续做 Windows Service、真实 CLI 任务与 Gateway 长时间断线组合验收。

## 7. 交接结论

P3-18 开发版完成，阶段 3 仍为 `PARTIAL`。设备注册现在具备密码学私钥持有证明和一次性挑战
消费，但生产退出条件仍受系统密钥环、正式认证、真实 PostgreSQL/RLS、设备凭据生命周期和实机
长时间运行验收约束。
