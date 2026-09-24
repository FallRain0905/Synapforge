# FM-6 交接：显式跨空间传输与生产加固

> 2026-09-24 · 状态：**部分交付**——跨空间传输与维护口已交付并端到端验收 **15/15**；
> 生产加固里"S3/MinIO 真机验证、PostgreSQL/RLS 生产验证、断点续传、备份/灾难恢复演练"**未做**（本机无 Docker，见 §5）
> 上游：`FM_5_AGENT_DRIVE_GRANTS_HANDOFF.md` · 计划 §9 FM-6 · 本包是 FM 计划的最后一包

---

## 1 交付了什么

**显式跨空间传输**（计划 §1 D4：只做显式动作，不做后台双向同步）：

```text
云盘 → Agent 工作区   POST /api/file-transfers/drive-to-workspace
                      （平台把节点内容搬进传输会话 → 入队一个 upload 操作 → 内核落盘）
Agent 工作区 → 云盘   POST /api/file-transfers/workspace-to-drive
                      （入队 download 操作 → Agent 传进传输会话）
                      POST /api/file-transfers/save-to-drive（人确认后存进云盘）
```

**冲突口径**（计划 §6.5/安全要求第 7 条"默认不覆盖"）：

| 场景 | 行为 |
| --- | --- |
| 云盘 → 工作区，目标同名 | 内核拒 `workspace_path_conflict`，**操作如实失败**（历史里能看见原因）；要覆盖得显式 `overwrite=true` |
| 工作区 → 云盘，同名 | 409 `file_name_conflict`（让人选改名/换目录/跳过），**不静默覆盖** |
| 内容与声明不符 | 读写两侧都比对 sha256 → `*_hash_mismatch`，拒收 |
| 每次调用 | 是**独立的一次显式复制**（不是幂等"去重"）：再点一次就是再复制一次；要幂等请重试**同一条操作** |

**维护口**（生产加固的"先能看见"那部分）：

- `GET /api/file-transfers?workspace_id=`：传输历史 = 会话 + 关联操作终态 + `error_code` + `retryable`；
- `POST /api/file-maintenance/transfers/cleanup`：过期会话回收（删对象 + 置 expired，先落库后删对象）；
- `POST /api/file-maintenance/orphans/scan`：孤儿对象扫描（云盘对象 + 传输对象），**只报告不删**。

**S3 后端契约测试**（`apps/api/test_s3_object_store.py`，5 项）：用注入的假客户端验证 S3 路径的**协议形状**
（`put_object` 带 `Metadata.sha256`、`get/delete`、分片上传四步、`abort`），并验证**云盘服务在 S3 后端上照样能跑**
（上传/下载/彻底清除 + 用量归零）——说明服务层没有绑死本地文件系统。

## 2 端到端验收（真 HTTP + 真内核 Worker，15/15）

`scripts/deploy/_fm6_verify.py`：临时库起真 uvicorn、真设备+项目授权、后台跑真 `WorkspaceFileWorker`。

| 对账项 | 结果 |
| --- | --- |
| 云端 → 工作区：建会话 + 入队 upload | ✅ 201 |
| 内容真的落到工作区且 sha256 一致 | ✅ |
| 同名再复制：失败并如实报 `workspace_path_conflict` | ✅ |
| 工作区 → 云端：入队 download → Agent 传进会话 | ✅ |
| 存进云盘；云盘内容与工作区一致 | ✅ 27 字节 |
| 同名再存：409 冲突（不覆盖） | ✅ |
| **并发 8 个跨空间传输全部有终态**（无静默丢件） | ✅ 全 succeeded，1.3 秒 |
| 配额用量与写入内容一致 | ✅ |
| 传输历史带操作终态与可重试标记；失败原因可见 | ✅ |
| 过期会话回收（删对象） | ✅ expired=1 objects_deleted=1 |
| 孤儿扫描只报告不删 | ✅ |

测试：平台 `test_file_transfers.py` **12 项**、`test_s3_object_store.py` **5 项**。
**测试抓到的一个真问题**：会话与操作之间原本没有对号（`operation_id` 不回填）→ 传输历史里只有孤立会话，
看不到成败与失败原因；现在两个编排都回填，历史才真能回答"这次搬运成没成、为什么失败、能不能重试"。

## 3 旧 API 兼容与弃用计划（计划 §9 FM-6 第 10 条）

| 接口 | 现状 | 计划 |
| --- | --- | --- |
| `GET /api/drive`（平面列表） | 兼容壳，映射到根目录，返回老形状 | **保留**：外部脚本还在用；FM-2 之后新前端只用 `/api/drive/nodes*` |
| `POST /api/drive/upload` | 兼容壳（根目录上传；同名冲突 409，不再静默改名） | 保留到下一次破坏性变更窗口 |
| `DELETE /api/drive/{file_id}` | **硬删**（历史语义），新接口是软删进回收站 | 保留；迁移完成后再标 deprecated（响应头加 `Deprecation`） |
| `POST /projects/{id}/drive/import`、`/drive/from-artifact/{id}` | 保留入口，内部走新服务 | 保留 |
| 老表 `personal_drive_files` | 只读备份（启动时幂等回填进节点树） | 迁移校验完成后可删；**删之前必须**再跑一次回填对账 |

## 4 与计划的偏差 · 未做（**这一包没有全做完，逐条列清楚**）

1. **大文件断点续传（计划第 4 条）没做**：目前传输一次性 PUT/GET；对象存储层已有 `initiate_multipart/
   upload_part/complete_multipart`（成果物上传在用），但**没有接到文件传输会话上**。要做需要：会话分片表、
   续传游标、平台侧 `PUT /uploads/{id}/parts/{n}`、Agent 侧分块重试。
2. **S3/MinIO 真机验证没做**：本机没有 Docker（`docker` 命令不存在），跑不起 MinIO；已用注入客户端的
   协议测试替代（§1），但**没有对真 MinIO/S3 做过一次端到端**。
3. **PostgreSQL/RLS 生产验证没做**：同样缺基础设施（本机 `psycopg` 在，但没有可连的 PG 实例）。
   迁移 `029/030/031` 都写了 RLS 策略，`test_platform_contracts` 断言了它们的形状，但**没有在真 PG 上验过**。
4. **压力与灾难恢复演练只做了一小步**：本次做了"并发 8 路跨空间传输"（§2），没有做长时间 soak、
   没有做备份/恢复演练（仓库里已有 `test_export_recovery.py` 与 `restore-check` 机制可复用）。
5. **临时对象回收只覆盖"过期会话"**：没有做定时任务（要靠运维 cron/K8s job 调
   `POST /api/file-maintenance/transfers/cleanup`）。
6. **前端没有为跨空间传输加交互**：接口齐了，`/drive` 上还没有"复制到工作区 / 从工作区保存"的按钮
   （FM-4 的 UI 只做到"各自浏览"）。这是最值得补的一块。
7. **旧接口的 `Deprecation` 响应头没加**（计划里"弃用计划"的落地形式）。

## 5 部署、回滚与数据恢复

- **部署**：迁移 `029/030/031` + 平台发布；执行体侧 `--workspace-files`（需先重签授权，见 FM-3 §4）。
- **回滚**：全是**新增**表与新增路由，旧接口未改语义 → 回滚代码即可；老表 `personal_drive_files` 仍在。
- **数据恢复**：`drive_object_cleanup`（云盘对象删除待办）与 `file_transfer_sessions`（传输对象生命周期）
  是两条独立的清理链，都在库里留痕；恢复时先跑 `POST /api/file-maintenance/orphans/scan` 看清单，
  **不要**直接删对象（扫描故意只报告）。
- **配额**：`used_bytes` 按去重后的物理对象算，软删除仍计入；彻底清除（或会话回收）才释放。

## 6 FM 计划收尾状态

| 包 | 状态 | 交接 |
| --- | --- | --- |
| FM-0 阻塞修复与契约冻结 | ✅ 端到端 9/9 | `FM_0_BLOCKERS_AND_WORKSPACE_HANDOFF.md` |
| FM-1 云盘正式模型与服务层 | ✅ 端到端 26/26 | `FM_1_DRIVE_SCHEMA_AND_SERVICE_HANDOFF.md` |
| FM-2 云盘文件管理器 | ✅ 攻击样本 24 项 + 端到端 47/47 + 浏览器实机 | `FM_2_DRIVE_FILE_MANAGER_HANDOFF.md` |
| FM-3 Agent 工作区文件服务 | ✅ 端到端 27/27 | `FM_3_AGENT_WORKSPACE_FILE_SERVICE_HANDOFF.md` |
| FM-4 统一文件管理 UI | ✅ 浏览器实机（多根/离线缓存/窄屏） | `FM_4_UNIFIED_FILE_UI_HANDOFF.md` |
| FM-5 云盘授权与 Agent 读取 | ✅ 端到端 22/22（前端未做浏览器实机） | `FM_5_AGENT_DRIVE_GRANTS_HANDOFF.md` |
| FM-6 跨空间传输与生产加固 | ⚠️ **部分交付**（§4 的 7 条未做） | 本文 |

**下一步建议顺序**（按"用户能看到/能用到"的价值排）：
① 前端跨空间传输按钮（用户在页面上真正用起来）→ ② 断点续传（大文件才敢用）→
③ 起一台 MinIO + 一台 PG 做真机验证（生产化的前提）→ ④ 定时回收与孤儿清理任务 → ⑤ 旧接口弃用头。