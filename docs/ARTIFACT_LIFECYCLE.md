# 成果物生命周期

```text
DRAFT -> PENDING_REVIEW -> APPROVED
                    \-> REJECTED -> DRAFT
APPROVED -> ARCHIVED
```

- 创建接口只能产生 `DRAFT` 或 `PENDING_REVIEW`，不能直接产生 `APPROVED`。
- `APPROVED` 必须有 `member` 类型 Review、批准人和批准时间。
- `APPROVED` 版本不可覆盖；新内容必须创建新的版本记录。
- 只有 `APPROVED` 且 `downstream_allowed=true` 的成果物可以作为正式下游输入。
- `REJECTED` 不得被下游引用；修订应产生新内容哈希或新版本。
- `ARCHIVED` 保留审计和引用关系，但不再作为新任务输入。
- 成果物必须保存内容哈希、来源任务、运行、输入成果物、Git commit 或快照引用。

## 产出采集口径（CL-1，2026-09-16）

常驻内核（`daemon-run`）跑完任务后，把**本次执行新增/修改的工作区文件**与**回答**自动入库：

| 项 | 口径 |
| --- | --- |
| 采集范围 | 执行前后各扫一次工作区（相对路径 → 大小 + mtime），差集即产出；删除不算产出 |
| 剪枝 | `.git`、`node_modules`、`.next`、`dist*`、`__pycache__`、`.venv`、缓存与 IDE 目录不扫；`*.log`/`*.db`/`*.tmp` 等后缀与 `platform.json`/`worker.json`/`sidecar.json` 不收 |
| 上限 | 文件数超过 20000 停止扫描并如实置 `truncated`（摘要里写"产出清单可能不完整"） |
| 文件大小 | ≤8MB 直传；8–100MB 分片；**>100MB 只登记路径/哈希/MIME，不上传** |
| 初始状态 | **一律 `PENDING_REVIEW`**：产出绝不自动 `APPROVED`，能否进下游/交付由人工审核决定 |
| 回答入库 | 回答写成 `<workspace>/.math-agent-platform/answers/<run_id>.md`，以 `agent_answer` 类型入库；可用 `resource_policy.inline_answer_artifact=false` 关闭 |
| 开关 | 任务级 `resource_policy.collect_outputs=false` 可关闭文件采集 |
| 失败语义 | 上传失败**不改变任务成败**；产出进入本地持久化队列（`%LOCALAPPDATA%\MathAgentPlatform\uploads.db`），下次执行前补传 |
| 引用回填 | 成果物 id 同时写入 Run（`output_artifact_ids`）与任务结果，Run 详情显示"产出 N 个成果物" |

实现位置：`apps/agent/workspace_scan.py`（快照/差分）、`apps/agent/output_collector.py`（采集与上传）、
`apps/agent/result_uploader.py`（既有队列与客户端，本阶段起被常驻循环复用）。设计依据见
`docs/CONTENT_LIFECYCLE_PLAN.md` 的 D-CL-1/2/3/5/8/9。
