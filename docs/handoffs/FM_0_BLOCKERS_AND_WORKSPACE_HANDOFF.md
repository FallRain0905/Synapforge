# FM-0 交接：阻塞修复 · 工作区口径统一 · 文件服务契约边界

> 2026-09-24 · 状态：**FM-0 全部完成并端到端验收**（9/9 对账项通过，脚本可重跑：`python -X utf8 scripts/deploy/_fm0_verify.py`）
> 上游计划：`docs/FILE_MANAGEMENT_AND_AGENT_DRIVE_EXECUTION_PLAN.md` §9 FM-0 · 下一包：**FM-1（个人云盘正式数据模型）**

---

## 1 这一期解决什么（为什么必须先做）

文件管理要把"个人云盘"和"Agent 工作区"摆进同一个页面。在那之前，有几件事**今天就是错的**，
不修的话后面每一个功能都建在流沙上：

1. **带输入的任务其实从来没拿到输入**——`agentd._execute_task` 里引用了从未定义的 `loop_identity`
   （参数名其实是 `identity`），`NameError` 被"取不到输入也要照常跑"的兜底 `except` 吃掉：
   任务照常执行、照常上报成功，只是提示词里写着"输入文件处理失败"、`inputs/` 永远空着。
2. **平台看到的"工作区"和 Agent 真正跑的地方可能不是同一个目录**——四处各写一遍 `Path(args.workspace).resolve()`，
   桌面端更是**根本不传** `--workspace`，内核退回进程当前目录，而打包后那正是安装目录
   （`resources\sidecar`，通常还只读）。
3. **`inputs/` 里的输入会被当成"本轮产出"重新上传成成果物**（任务通道的扫描器没排除它，对话通道排除了）：
   同一份文件在项目里出现两次，审计上看起来像 Agent 又生成了一份东西。
4. **成员的响应里带着执行体那台机器的绝对路径**（`agents.local_workspace`、`runs.observed_input_files`、
   `artifacts.source_path`，以及 Run 边界结论里嵌的同一份）。

## 2 改了什么（按文件）

| 文件 | 改动 | 关键决定 |
| --- | --- | --- |
| `apps/agent/agentd.py` | ① 第 544 行 `loop_identity` → `identity`（真 bug）；② 新增 `_workspace_root()` 作为**唯一**解析口径（`expanduser + resolve`），注册/输入物化/Runner/产出采集/对话循环全部改用它；③ 新增 `_resolve_daemon_workspace()`：命令行 > `platform.json` > 当前目录，命令行给了就**合并写**进 `platform.json`；④ daemon 启动时 mkdir 工作区，失败只如实报 `workspace_unavailable` | 注册上报的 `local_workspace`、Runner 的 cwd、输入落点、产出扫描根**必须是同一个目录**——四处各算一次迟早算出两个路径。桌面壳不传参数时靠落盘的那份记住用户的选择 |
| `apps/agent/input_fetcher.py` | `materialize_inputs` 落盘后写 `<workspace>/.math-agent-platform/inputs-manifest.json`：`artifact id → inputs/<文件名> → sha256 → 字节数`，并记录失败清单；清单合并写（同路径覆盖、文件已删的条目剔除）、`os.replace` 原子替换、写失败只记日志 | 平台只发 id、磁盘上只有文件名，中间这一跳不留证，事后就答不了"这次 Run 读到的到底是不是那个成果物"（FM-5 的输入 Manifest 以它为底） |
| `apps/agent/workspace_scan.py` | 新增 `INPUT_DIR_NAMES = {"inputs","user_data"}`，`is_excluded` 里**只认顶层**排除 | 与对话通道（`chat_outputs`）合并成一条判定，不再各写一份；工具自己建的 `src/inputs/` 仍然算产出 |
| `apps/agent/chat_outputs.py` | 删掉本地的 `INPUT_DIR_NAMES`，改为从 `workspace_scan` 导入 | 单一真源 |
| `apps/agent/sidecar_entry.py` | 新增 `--workspace`；`_daemon_args` 不再硬写 `Path.cwd()`，改为 `CLI > platform.json`（都没有就交给内核决定）；`_daemon_defaults` 改读**显式传入的 state 目录** | 打包后 cwd 是安装目录，把它当工作区是错的；两边读不同的 `platform.json` 会出现"配对记录在 A、默认值从 B 读" |
| `apps/agent/sidecar_api.py` | 新增 `remember_workspace()`：合并写 `platform.json` 的 `workspace` 键（不动 url/device_id/paired_at） | 契约 §4b 的 `platform.json` 是**追加可选键**，已同步文档 |
| `apps/desktop/src/main.js` | 新增 `defaultWorkspace()/workspacePath()/ensureWorkspace()/chooseWorkspace()/restartSidecar()`：工作区从 `desktop.json` 取（默认 `<文档>/MathAgentWorkspace`），**每次启动都显式传 `--workspace`**；托盘与「文件」菜单加「Agent 工作目录…」；`snapshot().shell.workspace` 暴露当前值 | 换目录要重启内核（工作区是启动参数），热改会让正在跑的那一轮输入与产物落在两个目录 |
| `apps/desktop/src/preload.js`、`renderer/{setup.html,status.js}` | 桥 `chooseWorkspace()`；本机页加只读的「Agent 工作目录」行 + 「选择工作目录…」按钮 | 选择结果必须看得见（否则又是"设置项静默无效"） |
| `apps/api/app/path_privacy.py`（新） | `workspace_identity()`（sha256 前 12 位）、`path_label()`（只留文件名 `…/x.csv`）、`public_agent/public_run/public_artifact` | 库里真值**不动**（边界判定与审计要用），只在成员可见响应里收窄；设备侧（`/api/agent/*`、注册/心跳）仍拿得到原值——执行体要回读对账 |
| `apps/api/app/main.py` | 5 个成员读接口过脱敏：`GET /api/agents`、`GET /api/projects/{id}/runs`、`GET /api/runs/{id}`、`GET /api/projects/{id}/artifacts`、`GET .../artifacts/{id}/detail` | 我方测试**自己抓出**边界结论里嵌的同一份路径（`information_boundary.observed_input_files` 与违规项的 `path`），一并收窄 |
| `apps/api/app/contracts.py` | `Agent` 追加 `workspace_identity: str \| None = None` | 追加可选字段，不改既有语义 |
| `docs/SIDECAR_CONTRACT.md` §4b | `platform.json` 行补 `workspace` 键说明 | 契约要求"新增字段必须同步文档与测试" |

**没有动**：`packages/agent_protocol/`、`apps/api/app/gateway.py`、`apps/api/app/collaboration.py`、
`packages/competition_packs/`、`apps/api/app/kb_gateway.py`、`infra/docker-compose.yml`、既有 migration。
Gateaway 命令列表与 Sidecar v1 既有字段语义**零改动**。

## 3 文件服务的新契约边界（FM-1 起照这个走）

1. **两个存储根，一套操作语义**：个人云盘在平台对象存储；Agent 工作区在 Agent 所在机器。
   平台**不入站连接**任何人家的电脑，所有工作区操作由 Agent 主动领取（轮询契约，独立版本化）。
2. **绝不新增 Gateway 命令**：文件操作走新的 `/api/agent/workspace-operations/*`（FM-3），
   `GATEWAY_COMMAND_TYPES` 保持冻结；Sidecar v1 只能追加端点/可选字段。
3. **工作区路径只允许相对路径**，且每一条都要重新过 `WorkspacePolicy`（含 `..`、绝对路径、盘符、UNC、
   symlink/junction/reparse point 的拒绝）。平台侧**永远不持有**可用于访问远端文件系统的绝对路径——
   `local_workspace` 只是执行体的自报信息，成员侧只看到 `workspace_identity`。
4. **大文件走对象存储**（`file_transfer_sessions`），不进 Gateway/WebSocket 帧。
5. **文件访问 Grant ≠ 工具执行审批**：Grant 只解决"能读哪些数据"，Agent 自主发起的工作区外操作仍走
   M-5c S-3 的权限卡片；文件管理器里人亲自点的上传/移动/删除走普通权限校验 + 确认框，
   **不再生成第二套权限卡片**。
6. **Artifact 不等于普通文件**：导入项目仍走 `PENDING_REVIEW`/Run 归属/输出校验，"复制到云盘"是复制。
7. **输入物化的台账**是 `<workspace>/.math-agent-platform/inputs-manifest.json`（FM-0 起）；
   `runs.observed_input_files` 是**观察器**记的另一件事（进程实际读了什么），两者不要混为一谈。
   平台侧成员可见的那份只给文件名。

## 4 验收（真流程，不是"页面能点"）

`scripts/deploy/_fm0_verify.py`：临时 SQLite + 临时对象目录起**同一个 FastAPI 应用**（不碰开发库），
真内核子进程跑 `register` 与 `worker-run --once`（真 HTTP、真能力令牌、真 LocalRunner），9 项对账全过：

| 对账项 | 结果 |
| --- | --- |
| 内核 `register` 上报的工作区 = `--workspace` 解析后的绝对路径 | ✅ `…\fm0-verify-…\ws` |
| 输入落到 `<workspace>/inputs/题目原文.txt` | ✅ 52 字节 |
| 落地字节与平台侧成果物一致（含 sha256） | ✅ |
| 清单记录 `artifact id → 相对路径 → sha256 → 字节数` | ✅ `ba9d92f2-… / inputs/题目原文.txt / 80ceec85… / 52` |
| 提示词列出输入文件且**没有**"处理失败" | ✅ `[本轮提供了 1 个文件，已放在工作目录的 inputs/ 下：题目原文.txt]` |
| Runner 真正的 cwd == 注册上报的工作区 | ✅ 同一个目录 |
| 平台下发的输入**没有**被当成产出再传一份 | ✅ 新增成果物只有 `prompt.txt`/`cwd.txt`（真产出） |
| 平台侧留下运行记录 | ✅ `SUCCEEDED` |
| 任务终态 | ✅ `WAITING_REVIEW` |

单元/契约测试：内核 `test_input_materialization.py`（5 项，含**中文名走 RFC 5987 `filename*`**、
失败项如实回报、清单合并与剔除、Runner cwd 与注册一致）、`test_agent_daemon.py` 新增
`WorkspaceResolutionTests`（5 项）、`test_input_fetcher.py` 新增 `InputsManifestTests`（4 项）、
`test_sidecar_entry.py` 新增 `WorkspaceArgumentTests`（4 项）、`test_workspace_scan.py` 新增输入排除（3 项）、
平台 `test_path_privacy.py`（7 项，含坏数据与"脱敏不得就地改原对象"）。

基线：Agent **439 项**通过（11 skipped）；平台 **563 项**（仅 2 项既有 LaTeX 环境失败，与本包无关）；
`node --check` 三个桌面文件通过；前端本期**未改动**（见 §6 偏差）。

## 5 对象存储与云端工作区事实核对（读线上，未改任何配置）

- 生产平台（`XX.XX.XX.XX`，`map-api.service`）：`/etc/math-agent-platform/api.env` 里
  `OBJECT_STORE_BACKEND=local` → **本地目录** `/opt/math-agent-platform/apps/api/data/objects`，
  **22 个对象、212K**；`artifacts` 24 行里有 **5 行没有 `storage_key`**（元数据在、内容不在）。
  → FM-1 迁移必须把这 5 行标成"无内容"（如 `scan_status=unavailable`），不能假装每行都有内容。
- 云端 Agent（`XX.XX.XX.XX`，`map-agent@cloud.service`）：`WorkingDirectory=/srv/synapforge/%i`，
  `ExecStart` 带 `--workspace /srv/synapforge/cloud`，实测 cwd 与之一致；`/srv/synapforge/cloud/inputs/`
  已有早期验证留下的 `sample.md`。
- **Compose 变量不一致（只记录，不改）**：`infra/docker-compose.yml`（README 禁区）给 api 服务传的是
  `STAGE4_MINIO_ENDPOINT` / `STAGE4_POSTGRES_DSN`，而 `create_object_store` 读的是
  `OBJECT_STORE_BACKEND` / `S3_BUCKET` / `S3_ENDPOINT_URL` / `S3_REGION` —— **全仓库没有任何生产代码读
  `STAGE4_MINIO_ENDPOINT`**（只有条件集成测试直接读环境变量）。结论：用 Compose 起的那套里 MinIO
  会起来但**从不被使用**，API 仍在容器内写本地目录。FM-6 做 S3/MinIO 生产化时必须一起修这个映射
  （要先请用户解除 compose 的禁区，或者把开关做成 `OBJECT_STORE_BACKEND=s3` + `S3_*` 由 compose 传入）。

## 6 与计划的偏差 · 未做 · 已知风险

- **`artifact.source_path` 的脱敏是本期加的**（计划里只点名了 `local_workspace`）：同一类泄漏，顺手一起收；
  前端没有任何页面渲染它，改动零影响。
- **`observed_input_files` 的"完整审计映射"没做完**：本期落了工作区侧的 Manifest，但它与
  `runs.observed_input_files` 的**平台侧**对账表要等 FM-5（Grant/Lease/审计）——那才是这些字段的归属地。
  成员侧现在只看到文件名，这是有意的收窄，不是遗漏。
- **桌面端只做到"选择 + 持久化 + 传参 + 可见"**：安装包重打（`build-desktop.ps1`）与真机安装验证留到需要发版时做；
  `MAP_DESKTOP_SELFCHECK` 需要 Electron，本期只做了 `node --check` 与逻辑走查。
- **`apps/web` 一行未改**（另一个 Agent 正在改 `/my-agent`、`globals.css`、`lib/api.ts`）：新增的
  `workspace_identity` 暂时只存在于 API 响应里，等 FM-4 做统一文件管理 UI 时再进页面类型。
- **未提交、未部署**：本期只改代码与文档，**没有跑 `server_release.sh`、没有重启线上服务**
  （FM-0 不改线上行为，且树里有另一个 Agent 的在途改动，不适合由我提交）。
- 已知风险：`INPUT_DIR_NAMES` 只认顶层——工作区里若有人手工建一个顶层 `inputs/` 目录放产出，
  那些文件不会被采集。这与"平台把输入下到顶层 `inputs/`"的约定互斥，属于有意取舍（两个通道一致）。

## 7 用户现在能做什么 / 还不能做什么

能：桌面端**能选** Agent 工作目录（托盘与「文件」菜单、本机页按钮），选完重启内核后生效并跨重启保留；
带输入的任务**真的**会把文件放进 `<工作区>/inputs/`，执行体的提示词里会列出文件名；
成员在平台上看 Agent 只看到工作区标识，不再看到别人机器的绝对路径。

还不能：在页面上管理这些文件（浏览/上传/移动/解压都要等 FM-2/FM-3/FM-4）；
Agent 还不能读个人云盘里的文件（要等 FM-5 的 Grant/Lease）；云端 → 本地的大文件传输没做（FM-6）。

## 8 部署、回滚、数据恢复

- **部署**：本期没有 migration、没有新表、没有新环境变量。内核改动随下一次桌面端/执行体发版生效；
  API 改动随下一次 `server_release.sh` 生效（**本次未执行**）。
- **回滚**：纯代码回滚，无数据形态变化。唯一需要留意的是 `inputs-manifest.json` 与 `platform.json`
  的 `workspace` 键——两者都是**追加**，旧版本内核读到会忽略，不需要清理。
- **数据恢复**：不涉及。`inputs/` 是执行体本地的可重建内容（重新物化一次即可）。

## 9 FM-1 的入场条件（已满足）

- 带输入的链路可信（本节 §4 的对账）；工作区口径统一；成员响应不泄漏宿主机路径；对象存储现状有实测数据；
  新增 migration 前必须**重新扫描** `apps/api/migrations/` 取下一个可用编号——注意 `028_agent_chat_variants.sql`
  已由并行的 MY-AGENT 强度工作占用，**不要假设 028 可用**。