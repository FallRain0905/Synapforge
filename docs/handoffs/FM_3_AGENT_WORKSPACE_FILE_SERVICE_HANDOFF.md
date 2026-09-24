# FM-3 交接：Agent 工作区文件服务（平台侧队列 + 内核 Worker）

> 2026-09-24 · 状态：**已交付**——内核 20 项 + 平台 22 项 + 端到端 **27/27**（真平台 + 真 Worker 真动文件）
> 上游：`FM_2_DRIVE_FILE_MANAGER_HANDOFF.md` · 计划 §4/§6.4/§7.1/§9 FM-3 · 下一包：**FM-4（统一文件管理 UI）**

---

## 1 这一期做了什么

把"Agent 的工作目录"变成平台能安全浏览与操作的东西，**同时**守住那条硬边界：平台不入站连接任何人家的电脑。

```
浏览器 ──POST 操作──► workspace_operations 队列 ──Agent 轮询 claim──► 内核 Worker 在本机执行
   ▲                                                                        │
   └──────────────── 状态/结果/审计 ◄──────── complete/fail 回报 ◄──────────┘
        （大文件不经任何帧：内容走 file_transfer_sessions + 对象存储）
```

| 层 | 交付物 | 关键决定 |
| --- | --- | --- |
| 迁移 `030_agent_workspaces.sql` | `agent_workspaces` / `workspace_operations` / `file_transfer_sessions` / `workspace_audit`，全部开 RLS | 工作区只存 **`workspace_identity`（路径的 sha256 前缀）**，平台不持有宿主机绝对路径；操作表 `(workspace_id, idempotency_key)` 唯一 |
| 平台服务 `apps/api/app/workspace_files.py` | 登记、入队（幂等 + request hash）、领取、start/progress/complete、取消、过期回收、传输会话、审计 | **平台只做语法校验**（绝对路径/`..`/盘符/UNC/控制字符/超深），权威校验在 Agent 侧（只有它知道符号链接指向哪）；离线**照常入队**保持 `queued`，`fail_when_offline` 才立刻失败；`complete` **只认第一次** |
| HTTP 契约 | Agent 侧：`POST /api/agent/workspaces/register`、`/api/agent/workspace-operations/{claim,{id}/{start,progress,complete}}`、`/api/agent/workspace-transfers/{id}/content`（GET/PUT）；浏览器侧：`/api/agent-workspaces*`、`/api/workspace-transfers*` | **Gateway 零改动**（命令列表仍是 11 种，`test_platform_contracts` 继续断言）；Agent 侧要 `X-Agent-Id` + `X-Project-Capability-Token`，新能力 `workspace.files.{read,write,claim}` |
| 内核 `apps/agent/file_worker.py` | 领取 → 执行 list/stat/mkdir/upload/download/rename/move/copy/delete/extract → 回报 | **两道锁**：逐级 `lstat` 拒符号链接/junction/reparse point（Windows 的 junction `os.path.islink()` 认不出来，必须看 reparse 位）＋ `resolve()` 后 `commonpath` 必须仍在根内；上传**先写临时文件再 `os.replace`**（不留半个文件） |
| 内核 `apps/agent/safe_archive.py` | 工作区内的安全解压：先解到 `.math-agent-platform/tmp/<随机>`，全部通过后 `os.replace` 原子移动 | 与平台侧（FM-2 的 `archive.py`）**同一套规则的第二个实现**（两个部署物无法共享模块），靠"同一组攻击样本在两个测试套件各测一遍"防漂移 |
| 内核接入 | `daemon-run --workspace-files`（默认**关**）+ `--workspace-id/--workspace-protected/--workspace-files-idle-seconds/--workspace-files-batch`；快照里带 `workspace_files` 统计 | 默认关的原因见 §4——它需要新能力，旧授权串没有 |

## 2 端到端验收（真平台 + 真 Worker，27/27）

`scripts/deploy/_fm3_verify.py`：临时库起真 uvicorn、真设备+项目授权（含 `workspace.files.*`）、
真 `WorkspaceFileWorker` 走 HTTP，工作区是临时目录。

| 对账项 | 结果 |
| --- | --- |
| 登记接口只认 Agent（缺 `X-Agent-Id` 被拒） | ✅ 400 |
| Agent 登记工作区；浏览器侧看得到且**不含宿主机路径** | ✅ `identity=ws-1c6905f683d7` |
| mkdir / list（内核回报真实目录内容） | ✅ |
| 浏览器写内容进传输会话 → Agent 落到工作区（**按哈希**） | ✅ 40 字节 |
| Agent 读文件 → 传输会话 → 浏览器拿到（字节一致） | ✅ |
| rename / move / copy | ✅ |
| extract 解出目录树；**Zip Slip 被拒且不留东西** | ✅ `archive_path_unsafe` |
| 逃逸样本（`..` / 绝对路径 / 盘符）：平台入队就拒 + 内核也拒 | ✅ 两道锁都验过 |
| 保护路径不可删：工作区根 / `.git` / `.math-agent-platform` / 自定义 | ✅ 文件仍在 |
| Agent 离线：操作保持 `queued`、Worker 领不到活、`fail_when_offline` 如实失败 | ✅ |
| 上线后那条 `queued` 被捡起来执行 | ✅ |
| 同 key 同内容 → 同一条；同 key 不同内容 → **409** | ✅ |
| 审计记下入队与完成，且**不含路径明文** | ✅ |

测试：内核 `test_file_worker.py` **20 项**（含 Windows junction 用例）、平台 `test_workspace_files.py` **22 项**。

## 3 与计划的偏差 · 未做

- **大文件阈值**：内容交换一律走 `file_transfer_sessions` + 对象存储（没有"小文件内联进 JSON"的旁路），
  `LARGE_FILE_BYTES = 8MB` 目前只作为**契约里回报给前端**的提示值（FM-6 做断点续传时会真正分块）。
- **`progress` 只写心跳与自由进度字典**：没有字节级进度（要多段上传才有意义，FM-6）。
- **工作区删除不实现递归清空**：只支持"显式 `recursive`"，且项目录一律拒绝（计划 §7.3 的第一版口径）。
- **PG 侧只到 schema/RLS**（与 FM-1 同一偏差）：服务层跑在 SQLite 路径上，PG 生产化归 FM-6。
- **未部署**：内核侧默认关闭，线上执行体没开（见下）。

## 4 部署注意事项（重要，别踩）

1. **能力是新增的**：`workspace.files.read/write/claim` 三项**不在旧的项目授权串里**。
   已配对的执行体（含 `device-cloud-01`）必须**重新签发授权**才会拿到；否则 `claim` 会 403。
   为此内核侧 `--workspace-files` **默认关闭**，且遇到 403 会**退避 5 分钟**而不是每轮回调刷日志。
2. 打开方式：`map-agent@.service` 的 `ExecStart` 追加 `--workspace-files`（可选 `--workspace-id <平台上的 id>`），
   然后 `systemctl daemon-reload && systemctl restart `。
3. 平台侧还要一次发布（`server_release.sh`）才会有 `030` 迁移与这套路由；**本次未执行**。
4. 桌面端同理：内核下一次封包后，壳可以用 `--workspace-files` 打开（当前壳不传这个参数）。

## 5 FM-4 的入场条件（已满足）

- 浏览器侧接口齐了：`GET /api/agent-workspaces`（含在线状态与最近同步）、`GET /api/agent-workspaces/{id}`
  （操作历史 + 审计）、`POST .../operations`（入队）、`GET .../operations/{id}`（状态）、`POST .../cancel`、
  `POST /api/workspace-transfers`（+ PUT/GET content）。
- 术语与状态机是稳定的：`queued / claimed / running / succeeded / failed / cancelled / expired`；
  失败原因走 `error_code`（`workspace_path_protected`、`workspace_offline`、`archive_path_unsafe` …）。
- UI 侧要记住两条口径：**离线工作区只显示缓存的目录元数据并标注"最后同步"**（不假装实时）；
  **工作区操作不乐观假成功**（等 `succeeded` 才算完成）。