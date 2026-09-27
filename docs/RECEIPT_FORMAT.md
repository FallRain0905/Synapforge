# 产物溯源 Receipt 格式（RECEIPT_FORMAT）

> 状态：v1 草案（待 A 在 artifact 落库时确认列形态，待 B 在 agentd 生成侧确认采集点）
> 目的：回答"这个成果物是哪次执行、哪次工具调用产出的，输入来自哪里"。
> 对应实施计划 W2.4；格式语义借鉴 deer-flow `tool_receipt.py`，但纠正其两处
> 命名误导（见 §2）。

## 1. 一句话职责

- **B（agentd）**：在把产物上传为 artifact 时生成 receipt，随上传一并提交。
- **A（平台）**：校验 receipt 结构与哈希长度，落库到 artifact 的溯源字段，
  并在 `project.artifact.uploaded` 事件的 payload 里原样携带。
- **前端/审核页（B）**：展示"由哪个 Run / 哪次工具调用产出，输入 hash 是什么"。

## 2. 字段（receipt_version = 1）

| 字段 | 类型 | 说明 |
|---|---|---|
| `receipt_version` | int | 恒为 `1` |
| `tool_name` | string ≤128 | 产出该产物的工具名（如 `write`、`bash`、`upload`） |
| `tool_call_id` | string ≤128 | 执行体侧的调用 id（opencode part id 等） |
| `args_hash` | 16 hex | 工具调用参数的 SHA-256 **前 16 位**（小写） |
| `output_hash` | 16 hex | 工具输出的 SHA-256 **前 16 位**（小写） |
| `output_bytes` | int | 计入哈希的输出字节数 |
| `status` | string | `success` 或 `failed`（仅 success 的产物可作正式成果） |
| `created_at` | string | ISO-8601 UTC（执行体时钟） |
| `truncated` | bool（可选） | 输出被截断时为 `true`（哈希只覆盖保留段） |
| `source_artifact_hashes` | string[]（可选） | 本次产出读取的输入产物 hash 列表（溯源链的"输入来自哪里"） |

### 命名与哈希的两处纠正（相对 deer-flow）

1. **字段名用 `*_hash` 而不是 `*_sha256`**——deer-flow 字段名叫 sha256 但实际
   只存 SHA-256 前 16 位 hex（`tool_receipt.py:104-105` 的 `_short_hash`），
   名实不符；我们名字如实、长度进文档。
2. **哈希对象明确**：`args_hash` 对参数的**规范化 JSON**（UTF-8、键排序、
   无空白）计算；`output_hash` 对输出**原始字节**（截断时注明 `truncated`）。

## 3. 校验规则（A 的落库侧）

- `receipt_version` 必须是已知版本；未知版本 → 拒收该 receipt（artifact 本身
  可收，但不带溯源），不静默丢弃。
- 两个 hash 必须匹配 `^[0-9a-f]{16}$`；`output_bytes ≥ 0`；`status` 枚举。
- receipt 总大小 ≤ 4KB；字段超限按拒收处理。
- **客户端剥离**：`run 归属、receipt、来源` 属服务端所有权——receipt 里的
  run/agent 身份以服务端从令牌解析的为准，body 里自报的这些字段一律忽略
  （对齐既有 RLS 与"事件身份服务端注入"纪律）。

## 4. 与其他契约的关系

- `project.artifact.uploaded` 事件的 `payload.receipt` 即本文格式的 JSON 对象
  （见 AGENT_EVENT_CONTRACT.md §5）。
- 门禁条件 `file_written:<path>`（acceptance.py）证明"字节可读回"，
  receipt 证明"字节来自哪次调用"——两者互补，缺一都不构成完整溯源。
- 交接（handoff）与文档证据链引用 artifact 时，沿用到 receipt 的
  `output_hash`，实现"交接的是哪个版本的字节"可核对。

## 5. 待确认（给 A / B 的问题）

1. A：溯源落库是加列（`artifacts.source_run_id` / `source_tool_hash`，实施计划
   迁移 035 预案）还是独立 `artifact_receipts` 表？建议前者（一产物一 receipt
   的主形态）+ 可选附件表兜底多 receipt。
2. B：agentd 能稳定拿到 tool_call_id 与原始输出字节吗？若 opencode 侧输出
   已是派生视图，`output_hash` 先对"写入文件的内容字节"计算，并在 docstring
   注明口径。
3. 采集点：仅"产生文件产物"的工具调用生成 receipt，还是所有工具调用？
   建议前者（v1 最小闭环），全量留档由事件日志承担。
