# FM-5 交接：云盘文件访问授权与 Agent 读取（Grant / Lease / 物化）

> 2026-09-24 · 状态：**已交付**——平台 20 项 + 内核 6 项 + 端到端 **22/22**（含撤销演练与负向矩阵）
> 上游：`FM_3_AGENT_WORKSPACE_FILE_SERVICE_HANDOFF.md`、`FM_4_UNIFIED_FILE_UI_HANDOFF.md` · 计划 §5.4–5.6/§6.2–6.3/§9 FM-5 · 下一包：**FM-6（跨空间传输与生产加固）**

---

## 1 这一期做了什么

给"Agent 读成员云盘里的文件"补上**授权**这一环。核心是把三件事分开：

| 东西 | 回答什么问题 | 谁签发 |
| --- | --- | --- |
| 项目能力令牌（已有） | 这台设备能不能替这个项目干活 | 设备页/授权串 |
| **文件访问 Grant**（本期） | 它能读我云盘里的**哪些**文件、能干什么 | 文件所有者（成员） |
| **文件访问 Lease**（本期） | 这**一次**、这**一台**、这**一个 Run** 的短期凭证 | 平台（用 Grant + 设备身份换） |

**Agent 不能用人令牌访问云盘**：它只有 lease（明文只签发一次，库里只存哈希），每次读取都重新校验
Grant 状态、lease、epoch、Agent、设备、项目、Run、文件范围。这与 M-5c S-3 的权限卡片互不替代：
那是"工具执行要不要批准"，这里是"能读哪些数据"。

## 2 数据模型（`031_drive_grants.sql`）

- `file_access_grants`：所有者 / Agent / 设备 / 项目 + **可选** 任务·Run·对话轮次 + 范围
  （`file` / `folder` / `drive`）+ `capabilities` + 过期时间 + `revocation_epoch` + 撤销字段；
- `file_access_grant_nodes`：文件夹授权的**快照**（含 `content_hash` 与 `revision`；物化时按它校验）；
- `file_access_leases`：`token_hash` + `revocation_epoch` + 过期 + `used_at`（**从不存明文**）。

三张表都开 RLS。SQLite 侧在 `drive_grants.py` 的 `ensure_schema`（与迁移字段一一对应）。

## 3 已拍板口径的落地

| 决定（计划 §1） | 实现 |
| --- | --- |
| D1 默认只读 + 可导入 | 默认 `drive.metadata.read` / `drive.file.read` / `drive.file.import`；**写权限（删除/移动/改名/覆盖/复制到云盘）不在可授予集合里**，授了直接拒（`file_access_capability_denied`） |
| D2 粒度与绑定 | 单文件 / 文件夹 / 整盘；绑定成员·Agent·设备·项目（+可选任务/Run/对话轮次）；**文件夹默认只覆盖授权当时的节点**，勾"包含以后新增"才动态覆盖 |
| D2 过期与撤销 | 过期上限 30 天（默认 7 天）；撤销 = `revocation_epoch+1` + 作废该 Grant 下所有 lease；**不删行**（审计要能回答当时授权给了谁） |
| 联动 | `revoke_grants_for(device/member/task/agent)` 一条口子：设备撤销、成员移除、任务结束、Agent 注销都能调它 |
| 审计 | 允许/拒绝都写 `drive_audit`（grant:create / lease:exchange / agent:list / agent:read / grant:revoke），**只记节点与哈希，不记文件名** |

## 4 Agent 侧接口与物化

```text
POST /api/agent/file-leases/exchange           换短期 lease（X-Project-Capability-Token + X-Agent-Id [+ X-Device-Id]）
GET  /api/agent/drive/files                    列被授权范围内的文件（只能看到这一小块）
GET  /api/agent/drive/nodes/{id}               单个节点元数据（范围外 → 403）
GET  /api/agent/drive/nodes/{id}/content       读正文（每次重判；附 X-Content-SHA256）
POST /api/agent/drive/materialize              物化清单（名字/大小/sha256/revision）
```

内核 `apps/agent/drive_materializer.py`：换 lease → 拉清单 → 逐个下到 `<workspace>/inputs/`
（**先写临时文件再 `os.replace`**，重名加序号不覆盖）→ 合并进 `inputs-manifest.json`
（`source=drive_grant` + `grant_id`），失败项如实写 `failures`（不假装拿到了文件）。
接入：`daemon-run --drive-grant <grant_id>`（启动时物化一次）。

## 5 端到端验收（真 HTTP + 真内核物化器，22/22）

| 对账项 | 结果 |
| --- | --- |
| 跨成员的云盘节点不能授权 | ✅ 404 |
| 建授权：默认能力只读 + 可导入（无写/删/移动） | ✅ |
| 物化到 `inputs/` 且 sha256 对账通过；Manifest 记来源与哈希 | ✅ |
| 未授权文件**没有**被物化 | ✅ |
| 目录授权默认只覆盖当时节点；勾"包含以后新增"后新文件进范围 | ✅ |
| 范围外读取 → 403 `file_access_scope_denied` | ✅ |
| 负向：错误 Agent → 403、错误设备 → 403、缺 lease → 401 | ✅ |
| 写权限不在可授予集合里（创建即拒） | ✅ |
| **撤销演练**：撤销后同段 lease 立刻读不到、新换 lease 也被拒 | ✅ |
| 已物化的副本按计划保留（撤销管后续读取，不回溯删本地副本） | ✅ |
| 设备撤销 / 任务结束 → 联动撤销 | ✅ |
| 审计含授权·领取·读取·撤销，且不含文件名 | ✅ |

测试：平台 `test_drive_grants.py` **20 项**（负向矩阵逐格覆盖：跨成员/跨组织/跨项目/错误设备/错误 Run/
过期/撤销/能力不足/范围外），内核 `test_drive_materializer.py` **6 项**（哈希不符拒收、失败如实记账、
不覆盖已有输入、lease 失败）。
**测试抓到的一个真问题**：授权列表原本只按 `organization_id` 收口 → 同组织任何人都能看到你把哪些文件
授权给了哪台设备；现在 `get/list` 都按 `owner_member_id` 收口（云盘是成员私有资产，这里不能照抄别处的组织口径）。

## 6 前端（已接上，但未做浏览器实机）

- `/drive` 详情抽屉新增「授权给哪些 Agent」：列出覆盖这个文件的 Grant（Agent / 范围 / 到期 / 是否撤销），
  可**一键撤销**；动作区新增「授权给 Agent」入口（选工作区 + 有效期 1/7/30 天）。
- 前端 `tsc` 与生产构建通过；**本轮没有做浏览器实机验收**（时间与上下文预算留给 FM-6），
  授权语义本身已在平台侧端到端验证（§5）。`/devices` 上的授权列表与撤销入口**没做**（计划 §8.4 的
  那一项）——留作后继项，接口已经齐（`GET /api/file-access-grants?agent_id=` 即可）。

## 7 与计划的偏差 · 未做

- **`file_access_grants` 的"续期"接口做了，但页面上没有续期入口**（接口 `POST .../renew` 可用）。
- **租约的"多次使用"语义**：lease 可以多次读（每次校验），不是一次性；计划没禁止，但值得在 UI 上说明
  （本期文案写的是"短期凭证"）。
- **撤销后工作区副本的清理策略**：按 D7/计划口径**不清**（已物化的输入保留），只保证后续读取失败。
- **`/devices` 的授权视图**未做（见 §6）。
- **未部署**：`server_release.sh` 与执行体开关（`--drive-grant`）都没动。

## 8 FM-6 的入场条件

- 跨空间搬运的两半都齐了：云盘侧有 `read_content`/`put_file`（含去重与配额），工作区侧有
  传输会话 + `upload`/`download` 操作；FM-6 要的是**把两边串成一条显式动作** + 生产加固
  （S3/MinIO 生产验证、断点续传、压力、孤儿对象扫描、旧 API 弃用计划）。