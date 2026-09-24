# 阶段 3 P3-02 Gateway 协议交接

> 交接状态：PASS_WITH_ASSUMPTIONS
>
> 日期：2026-09-13
>
> 对应任务：P3-02 Gateway Token 握手、连接生命周期、心跳与序号协议

## 1. 本轮目标

把 P3-01 的设备身份和连接元数据推进到可运行的 Gateway 协议边界：

1. 设备通过 Token 建立主动 WebSocket 连接。
2. 连接必须绑定 `device_id`、`agent_id`、`session_id` 和 `connection_id`。
3. 使用 `GatewayEnvelope` 校验消息身份、消息类型、幂等键和序号。
4. 支持心跳、服务端 ACK、重复消息确认和序号缺口补传提示。
5. 设备撤销后不能继续使用原连接收发协议消息。

## 2. 实际完成内容

### 2.1 Gateway 服务

新增 `apps/api/app/gateway.py`：

- `GatewayService.authenticate()`：验证设备 Token、URL 中的设备 ID，并创建连接元数据。
- `GatewayService.connected_message()`：发送带持久化出站序号的连接确认。
- `GatewayService.receive()`：校验封套身份，处理 `agent.heartbeat` 和 `agent.event`。
- 重复入站序号返回 `gateway.ack`，不重复推进入站序号。
- 入站序号出现缺口返回 `gateway.replay_required`，提示从缺口前一个序号开始补传。
- 非法封套、身份不一致和未支持消息类型返回结构化 `gateway.error`。
- 设备连接被撤销后直接终止协议处理，不再尝试从撤销连接发送错误帧。

### 2.2 FastAPI WebSocket 路由

新增：

```text
WS /ws/agents/{device_id}?session_id=...&connection_id=...
Authorization: Bearer <device_token>
```

路由行为：

1. 在接受连接前读取设备 Token、Session ID 和 Connection ID。
2. 认证成功后创建连接并发送 `gateway.connected`。
3. 持续读取 JSON 封套并返回协议响应。
4. 正常断开时将连接标记为 `DISCONNECTED`。
5. 认证失败、设备撤销或连接身份异常时关闭 WebSocket。

### 2.3 Store 连接序号和心跳

`Store` 新增：

- `get_agent_connection()`。
- `record_gateway_receive()`：接受连续序号、识别重复序号、拒绝序号缺口。
- `next_gateway_send_sequence()`：原子推进持久化出站序号。
- `record_agent_heartbeat()`：校验四元身份并更新 Device、Agent、Connection 的时间状态。
- `close_agent_connection()`：正常断开或撤销连接。

以上方法已加入 `PlatformRepository` 协议；PostgreSQL 实现暂未补齐，仍由 SQLite 开发 Store 提供运行时能力。

## 3. 消息语义

当前允许的设备入站消息：

| 消息类型 | 作用 | 状态 |
| --- | --- | --- |
| `agent.heartbeat` | 上报版本、能力、运行任务、队列和用户会话状态 | 已实现 |
| `agent.event` | 为后续进程/运行事件保留封套入口 | 已接受但暂不写入领域事件 |

当前服务端响应：

| 消息类型 | 作用 |
| --- | --- |
| `gateway.connected` | 连接成功和连接元数据 |
| `gateway.ack` | 确认已接受或重复的入站消息 |
| `gateway.replay_required` | 提示客户端补传序号缺口 |
| `gateway.error` | 返回协议或身份错误 |

## 4. 测试与验证

新增 `apps/api/test_gateway.py`，覆盖：

1. Token 握手、设备路径绑定和连接身份。
2. 连接确认的出站序号。
3. 心跳处理和在线状态更新。
4. 重复消息幂等 ACK。
5. 序号缺口和补传请求。
6. 非法心跳身份/不支持消息不推进输入序号。
7. 设备撤销后连接不能继续收发。
8. FastAPI WebSocket 路由的认证、收发和断开清理。

本轮验证结果：

```text
Gateway 定向测试：5 passed
全量 unittest：56 passed，2 skipped
compileall：通过
domain.schema.json / domain.json：解析通过
API：67 routes / 51 OpenAPI paths
```

跳过项仍为真实 MinIO/S3 和真实 PostgreSQL 集成测试。

## 5. 尚未完成与边界

本轮不能标记阶段 3 完成，原因如下：

- 当前 Gateway 只接入 SQLite Store；PostgreSQL Repository 还没有设备连接、序号和心跳实现。
- 当前 WebSocket 尚未配置 TLS、反向代理、连接限流和跨进程广播。
- 设备登记仍未执行公钥 challenge/signature 持有证明。
- `agent.event` 目前只是协议占位，不会自动创建 Run、Artifact 或 Event。
- 项目 Token/capability 尚未强制进入 Gateway 的任务领取、租约、上传和 Run 控制路径。
- 尚未建立客户端本地 SQLite 事件队列、ACK 游标、断线缓存和补传执行器。
- 尚未实现任务控制、终端控制和桌面控制三级权限，也未接入 User Session Worker。
- 尚未进行真实客户端、长连接、睡眠/断线、并发重连和重复投递故障演练。

## 6. 下一步

1. 实现 PostgreSQL Repository 的设备、连接、心跳和序号方法，并进行真实 RLS/并发测试。
2. 把项目范围 Token 与 Gateway 命令授权关联，首先接入任务领取和租约心跳。
3. 为设备登记加入 challenge/signature，设计 Token 轮换和丢失设备恢复。
4. 进入 P3-03：重构本地 `agentd`，增加 SQLite 队列、事件 ACK、断线重连和幂等补传。
5. 再进入 Adapter/Runner：先实现 Headless Python Adapter，再扩展 Codex/Claude Code 和用户会话 Worker。

## 7. 复现命令

```powershell
$env:PYTHONPATH = "$(Resolve-Path apps/api);$(Resolve-Path apps/api/vendor)"
C:\Users\19855\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest discover -s apps/api -p 'test_*.py' -v
C:\Users\19855\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m compileall -q apps/api
```

不要把 Fake WebSocket 测试或 SQLite 结果解读为生产 Gateway、TLS、PostgreSQL 或多设备验收。
