# FM-1 交接：个人云盘正式数据模型与服务层

> 2026-09-24 · 状态：**已交付并通过端到端验收（26/26，脚本可重跑：`PLATFORM_DRIVE_QUOTA_BYTES=1000 python -X utf8 scripts/deploy/_fm1_verify.py`）**
> 上游计划：`docs/FILE_MANAGEMENT_AND_AGENT_DRIVE_EXECUTION_PLAN.md` §5.1/§5.2/§9 FM-1 · 上一包：`FM_0_BLOCKERS_AND_WORKSPACE_HANDOFF.md` · 下一包：**FM-2（个人云盘文件管理器 UI）**

---

## 1 这一期解决什么

`personal_drive_files` 是「一成员一堆平铺文件」：没有目录、没有下载端点、没有回收站，表是
**运行时建**的（不在迁移里、没有 RLS），`project_ids` 是个 JSON 字符串，`source_artifact_id` 不落库，
并发上传既不锁也不校验唯一。文件管理要把它当正经云盘用，就得先把地基换成：
**节点树 + 引用表 + 对象清理队列 + 审计**，并且让"删除、配额、去重、并发"这四件事有确定答案。

## 2 数据模型（`029_drive_nodes.sql` + `app/drive.py` 的 SQLite 侧）

| 表 | 作用 | 关键约束 |
| --- | --- | --- |
| `drive_nodes` | 目录/文件节点树 | 部分唯一索引 `(owner_member_id, parent_id, name_key) WHERE deleted_at IS NULL`（回收站里的名字可重用）+ `(owner_member_id) WHERE is_root = 1`（每成员一个根） |
| `drive_project_refs` | 「这份文件被哪个项目导入过」 | 唯一 `(drive_node_id, project_id, artifact_key)`；`artifact_key` 用空串代替 NULL（NULL 在唯一约束里互不相等，会让同一条引用插两遍） |
| `drive_object_cleanup` | **对象清理队列** | 先落库、后删对象；`status/attempts/last_error` 留痕 |
| `drive_audit` | 操作审计 | **只记名字的哈希**，不记文件名本身（计划 §5.9） |

四张表都开了 RLS（`drive_project_refs` 跟着节点走，因为引用表本身没有 organization_id）。

**老表 `personal_drive_files` 一行不动**：启动时 `drive.backfill_legacy()` 一次性、幂等地搬进节点树
（同 id、同 storage_key、同 content_hash、同 created_at），`project_ids` 展开成引用行并标
`legacy_import = true`。搬完它是只读备份——回滚时还能对照。

## 3 四个"必须有确定答案"的地方

**名字唯一**：归一化键 = NFKC + 大小写折叠 + 空白折叠（`name_key`）。为什么按大小写折叠——用户机器上
（Windows/macOS）`Data.csv` 与 `data.csv` 是同一个名字，平台判成两个的话，导出落地时才会撞车。
非法名**拒绝**而不是清洗：路径分隔符、`.`/`..`、控制字符、Windows 保留名、尾随点/空格一律 400。
同目录同名稳定 409，**不覆盖**。

**配额按物理对象算**：`used_bytes` 是去重后的字节数（同一 `storage_key` 只算一份），
**软删除仍计入**（回收站占着空间），彻底清除才释放。所以"同 hash 上传两份文件"只收一份费，
而"删除"是两段式。上限默认仍是 200MB，可用 `PLATFORM_DRIVE_QUOTA_BYTES` 覆盖。

**并发**：`Store` 只有一条 SQLite 连接，`commit()/rollback()` 是全局的——一个线程回滚会把另一个线程
**尚未提交**的插入一起丢掉（并发测试直接抓出来的）。现在所有写操作走一把进程内可重入锁
（`_DRIVE_LOCK`）+ 事务，把"读配额 → 去重查询 → 写对象 → 插入 → 提交"串成原子段。
代价是写操作串行（含对象写入那几十毫秒），多进程/多实例要靠 PostgreSQL 的隔离与 advisory lock（FM-6）。

**对象删除**：`purge` 先把节点标 `purged_at`、把要删的 `storage_key` 写进队列并**提交**，再动对象存储；
删失败留 `pending` + `attempts` + `last_error`，由 `retry_cleanup`（`POST /api/drive/cleanup/retry`）重试。
只有**没有其它节点引用**该 key 时才真删（复制出来的"副本"共用对象，删一个不该把另一个弄坏）。
`orphan_objects()` 反向扫描**只报告不删**（对象存储的列举能力在后端之间不一致，删无主对象不可逆）。

## 4 接口

新增（计划 §6.1 的路径）：

```text
GET    /api/drive/nodes?parent_id=&query=&sort=&cursor=&limit=   列表（分页 + 搜索 + 排序 + 面包屑 + 用量）
GET    /api/drive/nodes/{id}                                     详情（元数据 + 面包屑 + 项目引用 + 近 20 条审计）
GET    /api/drive/nodes/{id}/content                             下载（RFC 5987 filename*，附 X-Content-SHA256）
POST   /api/drive/directories                                    新建目录
POST   /api/drive/files                                          上传（multipart，可带 parent_id）
PATCH  /api/drive/nodes/{id}                                     改名（可带 expected_revision）
POST   /api/drive/nodes/{id}/move                                移动（拒环）
POST   /api/drive/nodes/{id}/copy                                复制（同名自动 -副本/-副本2）
DELETE /api/drive/nodes/{id}                                     软删除 → 回收站
GET    /api/drive/trash                                          回收站
POST   /api/drive/nodes/{id}/restore                             恢复（名字被占用报 409）
DELETE /api/drive/trash/{id}                                     彻底清除
GET    /api/drive/nodes/{id}/audit                               节点审计
POST   /api/drive/cleanup/retry                                  重试对象清理队列
```

错误码用计划 §6.5 的 `file_*` 词表；**旧路由**（`/api/drive`、`/api/drive/upload`、
`/api/drive/{file_id}`、`/projects/{id}/drive/import`、`/drive/from-artifact/{id}`）保持历史响应形状与
历史错误码（`drive_file_not_found`/`drive_quota_exceeded`/`drive_file_referenced_by_project`），
由 `personal_drive.py` 这层壳翻译（`legacy_code()`）。

## 5 两处刻意的行为变化（不是 bug）

1. **同内容上传不再合并成同一条记录**：现在两个不同名的文件是**两个文件**（共用底层对象，只收一份费）。
   旧实现命中同 hash 就返回同一条——上传 `a.csv` 与 `b.csv`（内容相同）在云盘里会"少一个文件"。
   去重该发生在对象层，不该吃掉用户的文件（计划 §11.4 要的是"不重复计费"）。
2. **旧 `DELETE /api/drive/{id}` 仍是硬删**（进回收站后立刻彻底清除），因为它历史语义就是"删掉、配额立刻释放"；
   新接口 `DELETE /api/drive/nodes/{id}` 才是软删除进回收站。

另外「转入云盘」（`/api/drive/from-artifact/{id}`）保持**幂等**：同一个成果物点两次 → 同一份文件；
同名但内容不同（两个成果物恰好同名）→ 自动加 `-2` 后缀，不覆盖也不报错（与文件管理器的「复制」区分开）。

## 6 验收（真 HTTP、真对象存储，26/26）

| 对账项 | 结果 |
| --- | --- |
| 回填把老平面文件搬进节点树（同 id） | ✅ `{"scanned":1,"created":1}` |
| 老文件在旧接口可见 / 可下载且字节与哈希一致 | ✅ 42 字节，`X-Content-SHA256` 相同 |
| 别的成员看不到 / 别的组织看不到 / 别人下载被拒 | ✅ 三处全 404 |
| 新建目录 + 目录内上传 | ✅ 201 |
| 同目录同名 409（且不静默复用同内容） | ✅ 409 |
| 中文名下载头 latin-1 安全 + RFC 5987 | ✅ `filename*=UTF-8''%E9%A2%98…` |
| 并发上传同一份内容：四个文件、只占一份空间 | ✅ `201×4 used=351`（基线 51） |
| 并发上传不同内容：413 兜住、不突破配额 | ✅ `413×1`，用量与"接受数×300"精确相等 |
| 删除 → 回收站 → 恢复 → 再删 → 彻底清除 | ✅ 清除后用量 `351→342`，对象删 1 个、队列无残留 |
| `required` 模式下无令牌 401（列表/新建/下载），带令牌 200 | ✅ |
| 审计有记录且**不出现文件名**（只有哈希） | ✅ |

测试：新增 `apps/api/test_drive_nodes.py`（40 项）+ 验收脚本；旧 `test_personal_drive.py` 全部保留通过
（只改了一条断言以反映"同内容两份文件"的新语义）。API 全量 **602 项**（仅 2 项既有 LaTeX 环境失败）。

## 7 与计划的偏差 · 未做

- **PostgreSQL 侧只到 schema/RLS/索引**，服务层方法仍在 SQLite 路径上（`store.db` + 问号占位符）。
  生产就是 SQLite（`map-api.service` + `api.env` 里没有 DSN），PG 是"要切的时候"的路径；
  服务层的 PG 实现与 advisory lock 一并留给 **FM-6**（那里本来就有"PostgreSQL/RLS 生产验证"这一条）。
  这一点**没有假装完成**：迁移与 SQLite DDL 两边字段一致，契约测试断言了 029 的表/RLS/部分唯一索引。
- **写锁是进程内的**：单 worker 部署下正确；多实例部署要么换 PG，要么把这把锁换成按 owner 的行锁（FM-6）。
- **解压（ZIP/TAR/TGZ）没做**：计划里归 FM-2（安全解压与攻击样本一起做）。
- **搜索只按名字子串**，没有全文/内容检索（计划里也没要求）。
- **未部署**：没跑 `server_release.sh`、没重启线上服务（树里还有并行 Agent 在途的前端改动，不适合由我发布）。
- 已知取舍：`sort != name` 时游标无效（会明确报 `file_cursor_invalid` 而不是默默给错页）。

## 8 用户现在能做什么 / 还不能做什么

能（**经 API**）：建目录、上传（不覆盖）、下载、改名、移动、复制、搜索/排序/分页、软删除进回收站、
恢复、彻底清除；老接口照旧。**还不能**：在页面上做这些——`/drive` 现在仍是旧平面列表（FM-2 才换 UI）。

## 9 部署、回滚、数据恢复

- **部署**：无新环境变量（`PLATFORM_DRIVE_QUOTA_BYTES` 可选）；SQLite 侧启动即建表 + 回填（幂等）；
  PostgreSQL 侧跑 `029_drive_nodes.sql`。回填只**新增**行，不改老表。
- **回滚**：代码回滚即可；节点树里的行不影响旧表，旧接口在新代码下也能跑。
  若回滚到 FM-0 版本，旧表仍是完整的（老文件 ID/键/哈希都在）。
- **数据恢复**：`drive_object_cleanup` 的 `pending` 行就是"库已删、对象还在"的待办清单，
  重跑 `POST /api/drive/cleanup/retry` 即可继续；对象存储里的孤儿用 `drive.orphan_objects()` 只读盘点。

## 10 FM-2 的入场条件（已满足）

- 服务层可用且有真 HTTP 验收；旧接口零回归；错误码词表统一（`file_*` / 旧 `drive_*` 各就各位）；
  审计与回收站已就位（FM-2 的详情抽屉与回收站页直接取）。
- FM-2 的 UI 要复用这套接口，**不要**再往 `/api/drive` 上叠新语义；`/drive` 页改造时注意
  `apps/web` 里有并行 Agent 在途改动（合并前先看一眼 `git status`）。