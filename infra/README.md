# 自托管部署说明（阶段 9 最小切片）

本目录提供两套运行方式：**本机 Demo**（无需 Docker）与 **Docker Compose 自托管**。

## 1. 本机 Demo（推荐第一次演示）

```powershell
# 基础设施（用户态 PostgreSQL 54329 + MinIO 9100/9101）
.\scripts\local-infra.ps1 start
.\scripts\local-infra.ps1 provision   # 设置 platform / app_runtime 密码并恢复 scram-sha-256

# 一键起 API + Web（首次运行会自动生产构建 Web）
.\scripts\start-demo.ps1
```

入口：

| 入口 | 地址 |
| --- | --- |
| 工作台 | http://127.0.0.1:3000 |
| API 文档 | http://127.0.0.1:8000/docs |
| 平台健康 | http://127.0.0.1:8000/api/platform/health |
| 可观测指标 | http://127.0.0.1:8000/api/platform/metrics |
| 配额与成本口径 | http://127.0.0.1:8000/api/platform/quota |
| 竞赛模板包 | http://127.0.0.1:8000/api/competition-packs |
| MinIO 控制台 | http://127.0.0.1:9101（platform / platform-dev-only） |

## 2. Docker Compose 自托管

```bash
docker compose -f infra/docker-compose.yml up -d --build
```

服务与端口：`postgres:5432`、`minio:9000/9001`、`nats:4222/8222`（为后续 outbox 消费者预留）、
`api:8000`、`web:3000`。

生产部署前必须做的三件事：

1. **覆盖默认口令**：`POSTGRES_PASSWORD`、`MINIO_ROOT_USER/PASSWORD`、`NEXT_PUBLIC_API_URL`。
   远程访问还要设 `PLATFORM_CORS_ORIGINS`（工作台来源，逗号分隔，例如 `http://10.0.0.5:3000`）——
   默认只允许本机 3000，漏了会出现「页面能开、数据全空、控制台 Failed to fetch」。
2. **使用非 owner 运行角色**：应用连接串用迁移 `014_runtime_role_and_grants.sql` 建立的
   `app_runtime`（仅 DML、不可绕过 RLS），而不是 schema owner；迁移由管理员身份执行一次。
3. **配置限额与成本口径**：`PLATFORM_QUOTA_*`（运行数/成果物数/存储字节）与
   `PLATFORM_COST_*`（每次运行、每 GB·月单价），未设置时使用代码内默认值。

### 可观测与配额

```text
GET /api/platform/health     探活（compose healthcheck 使用）
GET /api/platform/metrics    平台事实：项目/任务/运行/成果物/事件/outbox 待投递/存储字节
GET /api/platform/quota      限额与成本单价
GET /api/projects/{id}/usage 单项目用量：运行、存储、能力 Token、事件与成本估算
```

超限行为：创建运行或登记成果物时超出配额返回 **429**，错误码形如
`quota_exceeded_runs`、`quota_exceeded_artifacts`、`quota_exceeded_storage`。

### 尚未完成的部署项（已记录，后续统一验证）

- Helm/Kubernetes 清单与水平扩展边界；
- PostgreSQL 强制 RLS 在容器化部署下的复验（本机用户态已验收一次）；
- 真实跨机器 Bundle 恢复演练（目前为进程内第二套 Store 模拟）；
- 备份/恢复与灾难演练脚本；
- OpenTelemetry/Prometheus 指标导出（当前为 JSON 端点）。