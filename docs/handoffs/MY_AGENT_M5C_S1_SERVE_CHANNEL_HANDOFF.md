# MY-AGENT M-5c S-1 交接：内核常驻 `opencode serve` 通道（对话轮次改走 server）

> 2026-09-24 · 状态：**S-1 已交付并线上真跑验证** · 计划 `docs/MY_AGENT_M5C_STREAMING_EXECUTION_PLAN.md`
> 上游 S-0 样本：`docs/handoffs/MY_AGENT_M5C_S0_SSE_SAMPLES_HANDOFF.md`（协议事实全部来自那份真机样本）
> 执行体：`154.219.99.75` · opencode 1.18.32 · provider `deepseek/deepseek-v4.1-flash`

---

## 1 这一期交付了什么

对话轮次不再每轮起一个 `opencode run` 进程，而是**复用一个常驻 `opencode serve`**：

- **内核管生命周期**（`apps/agent/opencode_server.py`）：懒启动（第一次用到才起）、就绪探测、探活失败重启、退场清理；
- **口令随机生成**（`secrets.token_urlsafe`，每次启动一个新口令），只在内核内存里流转，靠环境变量交给子进程；
  服务默认只监听回环（`--hostname 127.0.0.1`）；
- **轮次执行**：建/续会话 → `POST /session/{id}/message`（阻塞到轮次结束）→ 结果取响应的 `parts` 与 `info.tokens`；
  过程事件从 `GET /event` 的 SSE 里翻译成平台既有事件名（`process.started / agent.message / tool.completed / file.changed / process.exited`）；
- **取消语义换成 `POST /session/{id}/abort`**（不是杀进程）；
- **起不来就如实降级**：这一轮仍走原来的 `opencode run` CLI 通道，日志写明"降级到 CLI 通道"，不假装用了新通道；
- **开关在部署侧**：系统单元加 `--chat-server-port 4199`；**不配就是 0，内核连 server 都不看一眼**（其它机器不受影响）。

## 2 实测到的协议细节（本次新增，写下来免得再撞）

| 事实 | 证据 |
| --- | --- |
| 设了 `OPENCODE_SERVER_PASSWORD` 后，认证是 **HTTP Basic、用户名固定 `opencode`** | 探针：`Basic opencode:<密码>` → 200；`Bearer <密码>` / 自定义头 / 别的用户名 → 401 |
| 就绪探针用 `GET /api/health` | 带认证 → `{"healthy":true}`；不带 → 401 |
| **v1 `POST /session` 不接受 `model` 字段** | 带 `model` → `400 {"_tag":"BadRequest"}`；同一请求去掉 `model` → 200（`agent` 是接受的）。**模型只在发消息那一步带** |
| `POST /session/{id}/message` 阻塞到轮次结束 | 响应体 = `{"info": {...,"tokens":{...}}, "parts":[...]}`，正文与 token 账都在里面 |
| SSE 的自然终点是 `session.idle` | 读到它就收流（只认**本会话**的 idle）；没有 idle 时靠 `close_stream()` 关连接叫醒读线程 |
| 权限请求会拦路，`--auto` 等价语义 = 一律回 `{"response":"always"}` | S-1 沿用这个语义（与 CLI 通道一致），日志留一行"已按 --auto 等价语义放行"；**真卡片属 S-3** |

## 3 线上真跑证据（2026-09-24）

内核 journal（执行体）：

```
[chat] 取到轮次 1598b374（opencode，模型 deepseek/deepseek-v4.1-flash，角色 plan）
[chat] 常驻执行体服务已启动（端口 4199，口令只在内核内存里）
[chat] 走常驻服务通道（端口 4199）
[chat] 轮次 1598b374 完成（66 字，serve 通道）
```

平台记录（`agent_turns`）：`DONE`，用量
`{"input_tokens":8494,"output_tokens":41,"total_tokens":8570,"turns":1,"source":"opencode-serve","reasoning_tokens":35}`
——**`source` 是 `opencode-serve`**，与 CLI 通道的 `opencode-jsonl` 一眼可分；页面上也照实显示 `8570 tokens · opencode-serve`。

**重启路径**：`kill -9` 掉 serve → 下一轮日志出现"常驻执行体服务已启动"→ 这一轮照常完成（13 字），
且**续上了上一轮的上下文**（问"上一轮我让你写的是哪个公式"，它答出了同一个公式）→ 会话句柄在 server 上继续可用。

**平台重启不会留孤儿**：`systemctl restart map-agent@cloud` 后 `pgrep opencode serve` 为空、端口 4199 释放
（单元的 `KillMode=mixed` 会把 cgroup 里的子进程一并收掉）。若哪天真留下一个陈旧 serve：它用的是**旧口令**，
内核的探活会因为口令不符而失败 → 日志写明降级 → 陈旧进程退出后自愈。

## 4 代码结构（谁负责什么）

| 文件 | 职责 |
| --- | --- |
| `apps/agent/opencode_server.py` | `split_model_ref`（模型串→provider/model）、`ServerClient`（v1 薄封装：健康/建会话/发消息/回权限/中止/读 SSE）、`OpenCodeServer`（起停保活，`popen` 可注入）、`run_serve_turn`（一轮的完整编排 + SSE 翻译 + 取消） |
| `apps/agent/chat_loop.py` | 通道开关 `_serve_enabled`（**端口 > 0 且这一轮执行体是 opencode**）、`_run_via_server`（事件回传与完成口径与 CLI 通道完全一致）、`close()`（退场停 server）、`snapshot()` 带 server 状态 |
| `apps/agent/executor_events.py` | 新增 `progress(event_type, payload)`：常驻通道直接投递过程事件，**复用同一套节流/上限/计数** |
| `apps/agent/agentd.py` | `--chat-server-port`（默认 0）；daemon 退场的 `finally` 里 `chat_loop.close()` |
| `deploy/cloud-agent/map-agent@.service` | 单元加 `--chat-server-port 4199`（带注释说明端口选择与降级语义） |

## 5 测试（离线可跑，不依赖真 opencode）

`apps/agent/test_opencode_server.py`（**16 项**）+ `test_agent_chat.py` 新增 3 项：

- 模型串拆分（含模型 id 自带 `/`、非法形状返回 None）；
- **真 HTTP** 假 server（`http.server`，带 Basic 校验）：认证头形状、路径与请求体（含"建会话不许带 model"）、权限回复、中止；
- 生命周期：就绪等待、起不来 → False、超时 → 进程被收掉、活着但探活失败 → 重启；
- 一轮编排：delta 按 partID 收成一条 `agent.message`（reasoning 增量不进正文）、工具完成/文件改动/收尾顺序（`session.idle` 收流，**不许提前置位 stop 丢事件**）、用量映射、权限自动放行、取消 → `abort` 且 `ok=false`；
- 循环接线：开启走 serve（CLI 不跑）、不可用降级到 CLI、**默认（端口 0）连 server 都不构造**。

**基线**：Agent **399 项**通过（11 skipped，+19）；API 537 项（2 项既有 LaTeX 环境失败）；角色 lint 12/12。

## 6 边界与未做

- **还没有逐字 delta 帧**：这一轮的"过程可见"仍按原语义给（每段完成的 `agent.message`）——真正的逐字追加属 **S-2**（平台加 `delta` 帧 + 页面渲染），内核这边把 `message.part.delta` 已经读到了，接上即可。
- **权限仍是一律放行**（与 `--auto` 等价）：`permission.asked` 只记日志；**待批准卡片属 S-3**。
- 单实例单工作目录；同 serve 并发两个会话未测（S-1 按"一次一轮"设计，与 `executor_slot` 一致）。
- `POST /session/{id}/message` 是阻塞调用（放在工作线程里），没有改用 `prompt_async`；若将来要"先返回再流式"，再切。