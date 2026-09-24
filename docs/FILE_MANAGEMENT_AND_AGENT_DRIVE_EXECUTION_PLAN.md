# 文件管理与个人云盘—Agent 工作区打通执行计划

> 状态：`READY`
>
> 日期：2026-09-24
>
> 任务线：`FM-0` ～ `FM-6`
>
> 目标：交给执行 Agent 后，无需回看本次聊天即可按阶段实施
>
> 相关现状：`docs/handoffs/MY_AGENT_M5C_S3_APPROVAL_CARDS_HANDOFF.md` 已完成；`docs/MY_AGENT_M5C_STREAMING_EXECUTION_PLAN.md` 只剩 S-4

---

## 0. 执行摘要

本计划建设一个类似 1Panel 的文件管理工作台，在一个页面中管理两类文件空间：

1. **个人云盘**：平台对象存储中的成员私有目录；
2. **Agent 工作区**：云端或桌面 Agent 实际工作目录的受控视图。

两类空间只在 UI 和操作语义上统一，**不物理合并、不做共享盘挂载、不做后台双向同步**。个人云盘继续由平台对象存储承载；Agent 工作区继续由 Agent 所在机器承载，平台通过受控的文件操作队列与 Agent 主动拉取完成操作。

Agent 访问个人云盘使用短期、可撤销、绑定成员/Agent/设备/项目/任务或会话的文件授权；Agent 不继承用户 Session，不直接获得整盘永久权限，不默认拥有删除权限。

执行顺序：先修安全阻塞和目录数据模型，再做个人云盘文件管理器，然后做 Agent 工作区文件服务，最后接入授权、跨空间复制与生产加固。

---

## 1. 已拍板的核心决策

以下决定已由用户确认采用默认方案，执行 Agent 不需要再次讨论选型。

### D1：Agent 默认权限

Agent 对个人云盘默认只拥有：

```text
drive.metadata.read
drive.file.read
drive.file.import
```

默认不拥有：

```text
drive.file.write
drive.file.delete
drive.file.move
drive.file.rename
drive.file.copy_to_drive
```

工作区内部权限与个人云盘权限分开。Agent 在自己的受控工作区中可以按任务策略写文件；这不自动等于可以修改用户个人云盘。

### D2：授权粒度

个人云盘授权支持：

- 指定文件；
- 指定文件夹；
- 用户明确选择时授权整个云盘。

授权必须绑定：

- 云盘所有者；
- Agent；
- 设备；
- 项目；
- 可选任务、Run 或对话轮次；
- 权限集合；
- 过期时间；
- 撤销版本。

默认文件夹授权只覆盖授权当时已有节点；用户明确打开“包含未来新增文件”后才覆盖后续新增文件。

### D3：打通方式

第一期采用：

- 平台 API；
- Agent 主动领取文件操作；
- 任务输入物化到 `<workspace>/inputs/`；
- 大文件经对象存储传输。

第一期不实现：

- FUSE；
- WebDAV；
- SMB；
- 真实网络盘挂载；
- 平台直接连接用户电脑文件系统。

### D4：同步语义

第一期只提供显式动作：

- 复制到 Agent 工作区；
- 从 Agent 工作区保存到个人云盘；
- 导入项目；
- 作为任务输入；
- 复制、移动、解压和删除。

不做后台双向实时同步。源文件后续变化不会自动覆盖目标；再次复制时进行 hash/revision 冲突检查。

### D5：两个存储根，一个统一界面

文件管理器顶层展示：

```text
文件管理
├── 个人云盘
└── Agent 工作区
    ├── 云端 Agent
    └── 已接入桌面 Agent
```

统一的是浏览和操作体验，不是底层存储。个人云盘使用对象存储；Agent 工作区由各执行体本地目录承载。

### D6：不改冻结协议

以下现有契约是禁区：

- `packages/agent_protocol/` 的既有信封和序号语义；
- `apps/api/app/gateway.py` 的逐帧应答模型；
- `docs/SIDECAR_CONTRACT.md` v1 既有字段语义。

文件服务不得直接给 `GATEWAY_COMMAND_TYPES` 塞入一批新命令。第一版采用**独立、版本化、Agent 主动轮询的文件操作契约**；Sidecar v1 如需增加本地状态字段或端点，只能追加，不能改已有字段语义，并同步契约文档与测试。

### D7：成果物不等于普通文件

- 个人云盘文件是成员私有文件；
- Agent 工作区文件是执行体本地工作文件；
- Artifact 是项目资产，具有版本、审核、来源和下游引用语义。

“导入项目”会创建 Artifact；普通复制到工作区不会自动成为 Artifact。Agent 输出进入项目成果物仍沿用既有 `PENDING_REVIEW`、Run 归属和输出校验流程。

---

## 2. 与现有 MY-AGENT 前端工作的边界

### 2.1 已完成，不得重复实现

`docs/handoffs/MY_AGENT_M5C_S3_APPROVAL_CARDS_HANDOFF.md` 已完成并上线：

- `permission.asked` 权限卡片；
- 批准一次、当前会话允许、拒绝；
- 超时默认拒绝；
- 页面待批准状态；
- 审批持久化和执行体轮询；
- 被拒绝后不执行的真实闭环。

文件管理计划不得重新设计或重新实现这套卡片。

文件管理页面中由用户亲自点击的“上传、移动、删除、解压”等动作属于**显式人类操作**，走文件权限校验和危险操作确认框，不再生成一张 opencode 权限卡片。

Agent 在对话或任务中自主发起工作区外操作时，继续由 S-3 的权限通道处理；文件访问 Grant 只是数据访问授权，不能代替 S-3 的工具执行批准。

### 2.2 S-4 正在计划中，本计划不碰

`docs/MY_AGENT_M5C_STREAMING_EXECUTION_PLAN.md` 的 S-4 负责：

- reasoning 增量单独发事件；
- 页面折叠展示思考块；
- 模式/强度并入下拉；
- `--agent` / `--variant` 会话级设置。

本文件管理计划不得修改：

- reasoning 事件分类；
- 思考折叠块；
- 模式/强度下拉；
- 模型、角色和 variant 参数管线。

文件管理与 `/my-agent` 的前端交点只包括：

- 附件选择器增加“从个人云盘选择”；
- 已有“转入云盘”按钮迁移到新文件服务，但行为和入口保留；
- 文件传输显示独立进度；
- 产出文件可以显式保存到云盘或指定 Agent 工作区；
- 复用已存在的权限卡片、执行详情和 S-4 思考块，不再创建第二套。

### 2.3 W12 前端美化的复用原则

复用 `docs/handoffs/W12_FRONTEND_POLISH_HANDOFF.md` 中通用规范：

- 抽屉、Modal、Toast 的动效和焦点规则；
- 上传进度、拖拽反馈和网络状态；
- 低干扰、可中断、支持 `prefers-reduced-motion` 的动态效果；
- 移动端触控目标；
- 统一表面、边框、状态色和空状态。

不得重复要求 W12 中已经落地的共享 Drawer、Modal、Toast、动效或移动端基础组件。执行前先检查代码和最新 W12 交接，能复用就直接复用。

---

## 3. 当前基线与已知阻塞

### 3.1 个人云盘现状

现有端点：

```text
GET    /api/drive
POST   /api/drive/upload
DELETE /api/drive/{file_id}
POST   /api/projects/{project_id}/drive/import
POST   /api/drive/from-artifact/{artifact_id}
```

现有能力：上传、平面列表、配额、内容 hash 去重、删除、导入项目、Artifact 复制到云盘。

现有缺口：

- 没有目录；
- 没有下载 HTTP 端点；
- 没有重命名、移动、复制、搜索和分页；
- 没有回收站；
- 没有安全解压；
- 没有 Multipart；
- 表由运行时动态创建，不在正式 migration；
- 没有 PostgreSQL/RLS；
- `project_ids` 是 JSON 字符串；
- `source_artifact_id` 不落库；
- 云盘操作没有完整审计事件；
- 并发上传的配额和 hash 去重没有数据库约束。

关键位置：

- `apps/api/app/personal_drive.py`
- `apps/api/app/main.py` 的个人云盘路由
- `apps/web/app/drive/page.tsx`

### 3.2 Agent 工作区现状

- Agent 使用 `--workspace` 作为执行目录；
- Runner 以该目录作为 `cwd`；
- `WorkspacePolicy` 使用 `realpath/commonpath` 防止路径逃逸；
- 任务输入按 Artifact ID 下载到 `<workspace>/inputs/`；
- 运行前后扫描新增/修改文件并上传为 Artifact；
- 目前没有远程目录浏览、文件操作、挂载或双向同步。

必须先修的真实问题：

1. `apps/agent/agentd.py` 输入处理使用未定义的 `loop_identity`，带 `input_artifacts` 的任务可能没有实际下载输入；
2. 桌面端启动 sidecar 时没有明确传入 `--workspace`，默认工作目录可能是 sidecar 或源码目录；
3. Agent 注册上报的 `local_workspace` 不能当作平台可直接访问的远程路径；
4. 输出扫描不记录删除，按 size/mtime 判断变化，不足以支撑双向同步；
5. `observed_input_files` 与个人云盘来源尚未形成完整审计映射。

### 3.3 权限现状

- Device Token 证明设备身份；
- Project Capability Token 证明 Agent 对项目的能力；
- Agent 不能继承所属成员权限；
- 设备撤销、Token 轮换和移除成员已有项目授权撤销链；
- 个人云盘尚未纳入该撤销链。

文件访问必须增加独立授权，不得复用成员 Bearer Token 或仅依赖设备 Token。

---

## 4. 目标架构

```text
浏览器
  │ 人类 Session
  ▼
平台文件 API
  ├─ 个人云盘服务 ───────────────► ObjectStore
  ├─ 文件授权服务 ───────────────► Grant / Lease / Audit
  ├─ 工作区操作服务 ─────────────► workspace_operations
  └─ 文件传输服务 ───────────────► 临时 ObjectStore
                                      ▲
                                      │ Agent 主动 claim / progress / complete
                                      │ Project Token + Agent ID + File Lease
                                      ▼
                                Agent File Worker
                                      │
                                WorkspacePolicy
                                      │
                                本地 workspace
```

原则：

1. 浏览器只访问平台 API；
2. 平台不入站连接 Agent 机器；
3. Agent 主动领取文件操作；
4. 所有工作区路径只使用相对路径；
5. 大文件走对象存储，不塞进 Gateway/WebSocket 帧；
6. 每个操作有幂等键、状态、超时、审计和明确失败码；
7. 文件内容不写入普通日志。

---

## 5. 数据模型

迁移编号必须在实施时重新扫描 `apps/api/migrations/` 后取下一个可用编号；当前最高为 `027`，不要在发生并行开发后硬抢编号。

### 5.1 `drive_nodes`

替代当前平面 `personal_drive_files`：

```text
id                    uuid/text PK
organization_id       text NOT NULL
owner_member_id       text NOT NULL
parent_id              uuid/text NULL REFERENCES drive_nodes(id)
kind                   file | directory
name                   text NOT NULL
name_key               text NOT NULL
storage_key            text NULL
size_bytes             bigint NOT NULL DEFAULT 0
content_hash           text NULL
mime_type              text NULL
is_archive             boolean NOT NULL DEFAULT false
source_artifact_id     uuid/text NULL
revision               bigint NOT NULL DEFAULT 1
scan_status            pending | clean | rejected | unavailable
created_at             timestamp NOT NULL
updated_at             timestamp NOT NULL
deleted_at             timestamp NULL
```

约束：

- 同一 owner、同一 parent 下 `name_key` 唯一；
- 文件必须有 storage key、size、hash；
- 目录不得有 storage key；
- `parent_id` 必须属于同一个 owner 和 organization；
- 禁止形成目录环；
- 根目录按成员唯一；
- 软删除文件仍计入配额，彻底清除后才释放；
- revision 用于并发冲突和 ETag。

### 5.2 `drive_project_refs`

```text
drive_node_id
project_id
artifact_id NULL
imported_by
imported_at
legacy_import boolean
PRIMARY KEY (drive_node_id, project_id, artifact_id)
```

替代 `project_ids` JSON 字符串。已有 legacy 数据无法恢复准确 Artifact ID 时，允许 `artifact_id=NULL` 并标记 `legacy_import=true`。

### 5.3 `agent_workspaces`

```text
id
organization_id
agent_id
device_id
project_id
display_name
workspace_identity
kind                  cloud | desktop
status                online | offline | unavailable
policy_version
last_seen_at
created_at
updated_at
```

`workspace_identity` 是稳定标识，不向普通浏览器响应暴露绝对路径。

### 5.4 `file_access_grants`

```text
id
organization_id
owner_member_id
agent_id
device_id
project_id
task_id NULL
run_id NULL
conversation_id NULL
turn_id NULL
scope_type            file | folder | drive
root_node_id NULL
include_future_nodes  boolean DEFAULT false
capabilities          json/list
expires_at
revocation_epoch
created_by
created_at
revoked_at NULL
revoked_by NULL
revoke_reason NULL
```

### 5.5 `file_access_grant_nodes`

用于“授权时快照”模式：

```text
grant_id
drive_node_id
content_hash NULL
revision
PRIMARY KEY (grant_id, drive_node_id)
```

默认文件夹授权写入当前后代节点快照；`include_future_nodes=true` 时才按目录祖先动态判断。

### 5.6 `file_access_leases`

```text
id
grant_id
agent_id
device_id
project_id
run_id NULL
operation_id NULL
token_hash
expires_at
revocation_epoch
used_at NULL
revoked_at NULL
created_at
```

Lease 是短期访问凭证，只返回一次明文，数据库只存 hash。不得写日志。

### 5.7 `workspace_operations`

```text
id
organization_id
workspace_id
project_id
requested_by_member_id
agent_id
device_id
operation_type        list | stat | mkdir | upload | download | rename | move | copy | delete | extract
relative_path
arguments             json
expected_revision NULL
status                queued | claimed | running | succeeded | failed | cancelled | expired
idempotency_key
request_hash
claimed_at NULL
started_at NULL
completed_at NULL
result                json NULL
error_code NULL
created_at
updated_at
```

同一个 idempotency key 如果 request hash 不同，必须返回冲突，不能复用旧结果。

### 5.8 `file_transfer_sessions`

```text
id
operation_id
source_type           drive | workspace | temp
source_id
source_hash NULL
target_type
target_id
storage_key
expected_size
expected_hash
uploaded_size
status                initialized | uploading | ready | consumed | failed | expired
expires_at
created_at
updated_at
```

大文件使用 Multipart；临时对象消费完成或过期后清理。

### 5.9 文件审计

文件操作审计至少记录：

```text
organization_id
actor_kind
actor_id
member_id
agent_id
device_id
project_id
task_id
run_id
conversation_id
grant_id
lease_id
workspace_id
file_node_id
relative_path_hash
content_hash
capability
decision
reason
created_at
```

禁止记录：Token 明文、Authorization Header、文件正文、API Key、普通用户可见响应中的宿主机绝对路径。

---

## 6. API 与契约

### 6.1 人类个人云盘 API

建议新增 v2 路由并暂时保留旧路由兼容：

```text
GET    /api/files/roots
GET    /api/drive/nodes?parent_id=&cursor=&sort=&query=
GET    /api/drive/nodes/{node_id}
GET    /api/drive/nodes/{node_id}/content
POST   /api/drive/directories
POST   /api/drive/uploads/init
PUT    /api/drive/uploads/{upload_id}/parts/{part_number}
POST   /api/drive/uploads/{upload_id}/complete
PATCH  /api/drive/nodes/{node_id}
POST   /api/drive/nodes/{node_id}/move
POST   /api/drive/nodes/{node_id}/copy
DELETE /api/drive/nodes/{node_id}
POST   /api/drive/nodes/{node_id}/restore
DELETE /api/drive/trash/{node_id}/purge
POST   /api/drive/extractions
GET    /api/file-operations/{operation_id}
```

兼容要求：

- 原 `POST /api/drive/upload` 暂时映射到根目录上传；
- 原 `GET /api/drive` 暂时返回根目录文件和旧 usage 结构；
- 原文件 ID 尽量保持不变；
- 原 `/drive/from-artifact/{id}` 保留入口，内部改走新服务；
- 原项目导入入口保留，内部改走带 actor/context 的新领域服务。

### 6.2 文件授权 API

```text
POST   /api/file-access-grants
GET    /api/file-access-grants
GET    /api/file-access-grants/{grant_id}
POST   /api/file-access-grants/{grant_id}/revoke
POST   /api/file-access-grants/{grant_id}/renew
```

创建时服务端必须验证：

- 当前成员拥有被授权的云盘节点；
- 当前成员属于目标项目；
- Agent/设备有目标项目 Grant；
- capability 不超过当前成员允许授予的集合；
- 任务、Run、对话属于同一项目；
- expiry 在允许上限内。

### 6.3 Agent 云盘访问 API

Agent 不能调用人类 `/api/drive/*` 路由。使用单独接口：

```text
POST /api/agent/file-leases/exchange
GET  /api/agent/drive/nodes/{node_id}
GET  /api/agent/drive/nodes/{node_id}/content
POST /api/agent/drive/nodes/{node_id}/materialize
```

请求需要：

```text
X-Project-Capability-Token
X-Agent-Id
X-File-Access-Lease
```

每次访问重新校验 grant、lease、epoch、agent、device、project、run 和文件范围。

### 6.4 Agent 工作区操作契约

不修改冻结 Gateway 协议，新增版本化轮询接口：

```text
POST /api/agent/workspace-operations/claim
POST /api/agent/workspace-operations/{operation_id}/start
POST /api/agent/workspace-operations/{operation_id}/progress
POST /api/agent/workspace-operations/{operation_id}/complete
POST /api/agent/workspace-operations/{operation_id}/fail
```

浏览器侧：

```text
GET  /api/agent-workspaces
GET  /api/agent-workspaces/{workspace_id}
POST /api/agent-workspaces/{workspace_id}/operations
GET  /api/agent-workspaces/{workspace_id}/operations/{operation_id}
POST /api/agent-workspaces/{workspace_id}/operations/{operation_id}/cancel
```

Agent 文件 Worker 只能接受相对路径，所有路径由 WorkspacePolicy 重新验证。

### 6.5 错误码

至少定义稳定错误码：

```text
file_node_not_found
file_name_conflict
file_revision_conflict
file_parent_invalid
file_directory_cycle
file_quota_exceeded
file_upload_hash_mismatch
file_transfer_expired
file_access_grant_required
file_access_grant_expired
file_access_grant_revoked
file_access_scope_denied
workspace_offline
workspace_operation_expired
workspace_path_invalid
workspace_path_outside_root
workspace_symlink_denied
archive_format_unsupported
archive_path_unsafe
archive_too_many_entries
archive_uncompressed_size_exceeded
archive_ratio_exceeded
archive_target_conflict
```

错误响应头必须 latin-1 安全；下载文件名继续使用 RFC 5987 `filename*`。

---

## 7. 路径、解压和删除安全

### 7.1 路径规则

所有 Agent 工作区请求只允许规范化相对路径：

- 拒绝绝对路径；
- 拒绝 `..`；
- 拒绝 NUL 和控制字符；
- 拒绝 Windows 盘符、UNC 路径；
- 每个路径段做 Unicode 规范化；
- Windows 保留名和尾随点/空格需要归一或拒绝；
- 最大深度、路径长度和单段长度有限制；
- 不跟随指向工作区外的 symlink、junction 或 reparse point；
- 对每一级目录使用 `lstat`/等价检查，最终仍做 `realpath/commonpath`。

### 7.2 安全解压

第一期仅支持：

```text
.zip
.tar
.tar.gz
.tgz
```

`.7z`、`.rar`、`.bz2`、`.xz` 只识别和下载，除非后续明确增加受控解压器。

解压前完整扫描归档目录：

- 拒绝绝对路径和 `../`；
- 拒绝 Windows 盘符和 UNC；
- 拒绝 symlink、hardlink、设备文件、FIFO；
- 限制条目数；
- 限制单文件大小；
- 限制总解压大小；
- 限制压缩比；
- 限制目录深度；
- 默认不递归解压嵌套归档；
- 默认不覆盖；
- 先解压到临时目录，全部校验成功后原子移动到目标目录；
- 失败时清理临时目录并记录审计。

### 7.3 删除语义

个人云盘：

- 默认进入回收站；
- 回收站内容仍计入配额；
- 被项目引用时允许软删除但不允许立即彻底清除；
- 彻底清除必须检查引用并二次确认；
- 对象删除失败进入清理队列，不先丢失数据库追踪记录。

Agent 工作区：

- 人类显式删除需要确认；
- Agent 默认没有个人云盘删除权限；
- 工作区批量删除必须列出受影响数量和路径根；
- 删除 workspace 根、`.git`、`.math-agent-platform`、运行状态目录和策略保护目录必须拒绝；
- 第一版不实现“通过文件管理器递归删除整个 Agent 工作区”。

---

## 8. 前端信息架构与交互

### 8.1 `/drive` 升级为文件管理器

桌面布局：

```text
┌────────────┬──────────────────────────────────────┐
│ 文件根     │ 面包屑 / 搜索 / 上传 / 新建 / 刷新   │
│            ├──────────────────────────────────────┤
│ 个人云盘   │ 文件列表                             │
│ Agent A    │ 名称 / 大小 / 修改时间 / 来源 / 状态 │
│ Agent B    │                                      │
│            ├──────────────────────────────────────┤
│ 授权管理   │ 传输与解压任务队列                   │
└────────────┴──────────────────────────────────────┘
```

右侧详情抽屉展示：

- 文件元数据；
- hash 和 revision；
- 来源 Artifact；
- 项目引用；
- 授权给哪些 Agent；
- 最近操作与审计摘要；
- 可执行操作。

移动端：

- 文件根进入左侧抽屉；
- 工具栏收敛为“上传/新建/更多”；
- 文件列表改为紧凑行，不做大面积卡片；
- 批量选择使用底部操作栏；
- 详情使用全宽抽屉。

### 8.2 基本交互

- 双击或 Enter 打开文件夹；
- 单击选中，右侧抽屉查看详情；
- 面包屑可跳转；
- 拖拽上传时显示“松开以上传到当前目录”；
- 文件级上传进度，不用全局无限旋转；
- 创建目录和重命名可行内编辑，但服务端成功前保持 pending 标识；
- 删除、覆盖、解压等破坏性动作必须确认；
- Agent 离线时保留最近目录元数据只读缓存，并明确标注“最后同步时间”，不得假装是实时状态；
- 工作区操作采用 queued/running/succeeded/failed 状态，不乐观假装成功；
- 同名冲突让用户选择：保留两份、替换、跳过；默认保留两份。

### 8.3 动态效果

复用 W12 的短、克制、可中断规范：

- 上传/下载/解压显示局部进度；
- 工作区操作从 queued → running → succeeded/failed 平滑改变状态；
- 新建、重命名成功只做一次短暂高亮；
- 删除后从当前列表移除，但保留撤销 Toast；
- Agent 从在线变离线时更新状态，不清空当前文件列表；
- 禁止整页白屏刷新、逐行飞入和持续闪烁；
- 支持 `prefers-reduced-motion`。

### 8.4 与现有页面联动

`/workspace`：

- 聊天附件增加“从云盘选择”；
- 保留原有直接上传；
- 选中文件后按既有语义导入项目 Artifact，再引用到消息；
- 不绕过 Artifact 项目归属和审核。

`/my-agent`：

- 只增加文件选择/传输入口；
- 已有“转入云盘”按钮改用新服务；
- 不改权限卡片、reasoning、模式/强度、角色和流式正文；
- 文件传输进度与回答正文分开；
- 文件操作事件可以进入已有执行详情，但不另造第二套详情抽屉。

`/artifacts`：

- 保留项目成果物语义；
- “复制到云盘”继续是复制，不是移动；
- 保存来源关系；
- 不在文件管理器中提供对已批准 Artifact 的原地覆盖。

`/devices`：

- 展示 Agent 工作区可用性、最近同步和文件能力；
- 不展示本地绝对路径；
- 补充文件授权列表和撤销入口时，复用本计划 Grant API。

---

## 9. 分阶段执行计划

### FM-0：阻塞修复与契约冻结确认

**状态：READY**

目标：先修会让后续文件链路不可信的现有问题。

工作项：

1. 修复 `agentd.py` 的 `loop_identity` 未定义问题；
2. 为带 `input_artifacts` 的任务增加真实下载测试；
3. 明确桌面端 workspace 选择、持久化和 sidecar `--workspace` 传参；
4. 注册上报的 `local_workspace` 与实际 Runner cwd 必须一致；
5. 平台响应不向普通成员暴露绝对路径；
6. 记录 `observed_input_files` 和 Artifact → 本地相对路径映射；
7. 确认文件服务不修改冻结 Gateway/Agent 协议；
8. 确认当前对象存储实际使用 local 还是 S3/MinIO，修正配置名不一致问题。

验收：

- 带输入 Artifact 的任务确实在 `<workspace>/inputs` 看到文件；
- hash、文件名和 observed input 对账；
- 桌面端用户选择工作区后重启仍使用该目录；
- 云端 Agent 保持 `/srv/synapforge/<instance>`；
- 没有协议禁区改动。

### FM-1：个人云盘正式数据模型和服务层

**状态：READY，依赖 FM-0**

工作项：

1. 新增正式 migration；
2. SQLite 和 PostgreSQL 同步 schema；
3. 增加 RLS；
4. 建立 `drive_nodes`、`drive_project_refs`；
5. 迁移现有文件到每个成员根目录；
6. 保留旧文件 ID、storage key、hash 和创建时间；
7. 将 `project_ids` 展开为引用表；
8. `source_artifact_id` 持久化；
9. 增加领域服务 actor/context，权限校验不再只依赖 middleware；
10. 增加下载、目录、重命名、移动、复制、软删除、恢复服务；
11. 增加并发 revision、唯一约束和配额事务；
12. 增加对象删除清理队列与孤儿对象检查。

验收：

- 老文件无损可见和可下载；
- 跨成员、跨组织访问全部拒绝；
- 同目录同名冲突稳定返回 409；
- 并发上传不重复计费、不突破配额；
- 删除对象失败时数据库仍能追踪；
- required/production 无 Token 请求被拒绝。

### FM-2：个人云盘文件管理器

**状态：READY，依赖 FM-1**

工作项：

1. `/drive` 攦为目录式文件管理页；
2. 面包屑、搜索、排序、分页；
3. 新建文件夹、上传、下载、重命名、移动、复制；
4. 批量选择和批量操作；
5. 回收站、恢复和彻底删除；
6. ZIP/TAR/TGZ 安全解压；
7. 文件详情和引用/来源抽屉；
8. 文件级进度和任务队列；
9. 保留原上传和“加入项目”入口；
10. 复用 W12 已存在的通用交互组件，不重复造 Drawer/Modal/Toast。

验收：

- 1440、900、390px 都能完成基本操作；
- 拖拽上传、失败重试和进度可见；
- 解压攻击样本均被拒绝；
- 暗色模式和减少动态效果可用；
- 原 `/drive` 核心功能没有回归。

### FM-3：Agent 工作区文件服务

**状态：READY，依赖 FM-0**

工作项：

1. 增加 `agent_workspaces`、`workspace_operations`；
2. 新增独立版本化 File Worker；
3. Agent 主动 claim 操作；
4. 支持 list/stat/mkdir/upload/download/rename/move/copy/delete/extract；
5. 所有路径走 WorkspacePolicy；
6. 增加 symlink、junction/reparse point 防护；
7. 大文件走 `file_transfer_sessions` 和对象存储；
8. 增加幂等、request hash、超时、取消和失败重试；
9. Agent 离线时操作保持 queued 或明确失败，不假装成功；
10. 不修改现有 Gateway command 列表。

验收：

- 桌面和云端 Agent 使用同一操作语义；
- `..`、绝对路径、UNC、盘符、symlink 逃逸全部拒绝；
- 大文件不进入 Gateway/WebSocket 帧；
- Agent 离线/重连/撤销时状态准确；
- workspace 根和保护目录不能删除。

### FM-4：统一文件管理 UI

**状态：READY，依赖 FM-2、FM-3**

工作项：

1. 左侧根列表加入 Agent 工作区；
2. 工作区状态和最近同步时间；
3. 操作权限动态显示；
4. Agent 离线只读缓存；
5. 工作区操作队列和失败重试；
6. 个人云盘与 Agent 工作区的复制入口；
7. 移动端根目录抽屉和批量操作栏；
8. 文件详情复用统一 Drawer。

验收：

- 用户可在一个页面切换云盘和多个 Agent 工作区；
- 不泄露宿主机绝对路径；
- 无权限操作不会显示为可执行；
- 操作状态真实、可取消、可重试；
- 页面不存在横向滚动和大面积空白。

### FM-5：云盘授权与 Agent 读取

**状态：READY，依赖 FM-1、FM-3**

工作项：

1. 增加 Grant、Grant Node、Lease 数据模型；
2. 增加创建、查看、续期和撤销接口；
3. 增加 Agent 专用云盘读取接口；
4. 将 Grant 与 device/project/task/run/conversation 绑定；
5. 任务开始时将获授权文件物化到 `inputs/`；
6. 生成输入 Manifest；
7. 与设备撤销、Token 轮换、项目成员移除和任务结束联动；
8. 所有允许和拒绝操作写审计与 Outbox；
9. 用户可在文件详情和 `/devices` 查看/撤销授权；
10. 不重复 S-3 权限卡片。

验收：

- Agent 只能看到被授权范围；
- 跨成员、跨组织、跨项目、跨设备、跨 Run 均拒绝；
- Grant 撤销后新读取立即失败；
- 已落地到 workspace 的临时副本按清理策略处理；
- 已导入项目的 Artifact 不因源 Grant 撤销而回溯删除；
- 审计能回答谁、何时、通过哪个 Agent/设备、为哪个项目/Run 读取了什么。

### FM-6：显式跨空间传输与生产加固

**状态：READY，依赖 FM-4、FM-5**

工作项：

1. 云盘 → Agent 工作区显式复制；
2. Agent 工作区 → 云盘显式保存；
3. revision/hash 冲突处理；
4. 大文件断点续传和恢复；
5. 传输历史、失败原因和重试；
6. 临时对象回收；
7. 对象孤儿扫描；
8. 压力、备份和灾难恢复测试；
9. PostgreSQL/RLS 和 S3/MinIO 生产验证；
10. 旧 `/drive` API 兼容期和后续弃用计划。

验收：

- 不静默覆盖同名文件；
- 默认保留两份或要求确认；
- hash 不一致时返回冲突；
- 上传失败不把任务或文件误标成成功；
- 重启后传输队列可恢复；
- 不存在后台双向同步。

---

## 10. 依赖关系与并行边界

```text
FM-0 ───────┬──► FM-1 ─► FM-2 ─────┐
             │                       ├──► FM-4 ─► FM-6
             └──► FM-3 ─────────────┘       ▲
                       └──► FM-5 ───────────┘
```

MY-AGENT S-4 可以和 FM-0/FM-1/FM-3 并行。涉及 `/my-agent` 页面接入的 FM-2/FM-4/FM-5 代码在合并前必须以 S-4 最新页面为基线，避免覆盖 reasoning、模式/强度和审批卡片相关变化。

每轮只实施一个 FM 工作包或一组强相关子项，并在 `docs/handoffs/` 写独立交接。

建议交接命名：

```text
FM_0_BLOCKERS_AND_WORKSPACE_HANDOFF.md
FM_1_DRIVE_SCHEMA_AND_SERVICE_HANDOFF.md
FM_2_DRIVE_FILE_MANAGER_HANDOFF.md
FM_3_AGENT_WORKSPACE_FILE_SERVICE_HANDOFF.md
FM_4_UNIFIED_FILE_UI_HANDOFF.md
FM_5_AGENT_DRIVE_GRANTS_HANDOFF.md
FM_6_FILE_TRANSFER_HARDENING_HANDOFF.md
```

---

## 11. 测试矩阵

### 11.1 数据与迁移

- 空库迁移；
- 旧库升级；
- 旧文件 ID/storage key/hash 保持；
- project_ids 迁移；
- SQLite/PostgreSQL 一致；
- RLS 跨组织拒绝；
- 回滚后旧接口仍可读取。

### 11.2 权限负向矩阵

必须覆盖：

- A 成员读取 B 成员云盘；
- A Agent 读取 B 成员文件；
- 同成员但错误项目；
- 同项目但错误 Agent；
- 同 Agent 但错误设备；
- 错误 Run/会话；
- Grant 过期；
- Grant 撤销；
- Token 轮换；
- 成员移除；
- 设备撤销；
- 无 Bearer 的 required/production；
- 伪造 grant id；
- 重放过期 lease。

### 11.3 路径和归档安全

- `../`；
- 绝对路径；
- Windows 盘符；
- UNC；
- symlink；
- junction/reparse point；
- Unicode 归一冲突；
- Windows 保留名；
- Zip Slip；
- tar symlink/hardlink；
- 高压缩比；
- 超多条目；
- 超大解压量；
- 嵌套归档；
- 目标同名冲突。

### 11.4 并发与幂等

- 同 hash 并发上传；
- 配额临界点并发上传；
- 同目录并发新建同名；
- rename/move revision 冲突；
- 同 operation idempotency key 同 payload；
- 同 key 不同 payload；
- Agent 重连重复领取；
- complete 重复提交；
- 传输中平台或 Agent 重启。

### 11.5 浏览器

- 1440px、1280px、900px、390px；
- 亮色/暗色；
- `prefers-reduced-motion`；
- 键盘操作；
- 拖拽上传；
- 批量选择；
- Agent 离线/重连；
- 文件冲突；
- 解压失败；
- 授权创建和撤销；
- `/workspace` 从云盘选附件；
- `/my-agent` 保持 S-3/S-4 行为且文件入口可用。

---

## 12. 部署、兼容与回滚

建议 Feature Flag：

```text
FILE_MANAGER_V2
AGENT_WORKSPACE_FILES
AGENT_DRIVE_GRANTS
FILE_TRANSFER_V2
```

默认部署顺序：

1. migration 与兼容服务；
2. API 旧端点继续可用；
3. Agent File Worker；
4. 前端逐项开关；
5. 授权功能最后开启。

回滚要求：

- 不删除旧 `personal_drive_files` 数据，完成迁移校验前保留只读备份；
- 新表停用后旧上传/列表仍能回退；
- 不回滚对象存储内容；
- 临时对象和传输队列可独立清理；
- 关闭 Agent 文件功能不会影响现有任务、聊天和 Artifact 输出链路；
- 改完 Web 必须重新构建并重启 Web 进程。

---

## 13. 每阶段交付要求

执行 Agent 每阶段必须交付：

1. 实际完成项；
2. 修改文件及关键行号；
3. migration 和协议变化；
4. 权限和审计变化；
5. 测试命令与结果；
6. 浏览器或真机验证；
7. 与计划偏差；
8. 用户现在仍不能做什么；
9. 已知风险；
10. 下一阶段入口条件；
11. 部署、回滚和数据恢复注意事项。

不得因为文件管理页面“看起来可用”而跳过路径穿越、归档安全、跨成员权限、设备撤销、并发配额、对象清理和真 Agent 验证。

---

## 14. 第一轮执行 Agent 开工指令

第一轮只执行 `FM-0`，不要直接开始画文件管理 UI。

开工顺序：

1. 阅读本计划；
2. 阅读 `README.md` 禁区、`docs/PERMISSION_MATRIX.md`、`docs/INFORMATION_BOUNDARY.md`；
3. 阅读最新 S-3 交接与 S-4 计划；
4. 检查未提交改动，避免覆盖并行前端工作；
5. 修复输入 Artifact 下载 bug并加测试；
6. 打通桌面 workspace 选择和真实 cwd；
7. 记录对象存储实际配置；
8. 给文件服务写冻结契约说明，不改既有 Gateway 协议；
9. 运行 Agent/API/桌面相关测试；
10. 写 `docs/handoffs/FM_0_BLOCKERS_AND_WORKSPACE_HANDOFF.md`。

FM-0 未通过前，不进入 FM-1～FM-6。
