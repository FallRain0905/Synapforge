# UX-4 接入向导与设备管理 交接

> 交接状态：`PASS_WITH_ASSUMPTIONS`
>
> 日期：2026-09-16
>
> 对应阶段：UX-4（工作项定义见 `docs/DEMO_1_0_IMPLEMENTATION_PLAN.md` §5 UX-4）
>
> 上一份交接：`docs/handoffs/UX_3_AGENT_LIVENESS_HANDOFF.md`

---

## 1. 本阶段目标

从计划书 §5 UX-4 抄写：

> **目标**：把"接入一个 Agent"从"手拼 ws URI + 自备密钥 + 手抄配对码"变成"三条命令"。
>
> **入口条件**：UX-3 退出（状态真实性是接入体验可信的前提）。
>
> **退出条件**：文档与 UI 中不再出现"让用户手拼 `session_id`/`connection_id`"的路径。

## 2. 实际完成内容

| 工作项 | 状态 | 实际做法 / 关键改动位置 |
| --- | --- | --- |
| UX-4-01 `agentd keygen` | 完成 | `apps/agent/agentd.py` 新增 `keygen` 子命令：生成 Ed25519 密钥对，私钥为**未加密 PKCS8 PEM**（`device-register --private-key` 正是以 `password=None` 加载，加密私钥会让配对在运行时才失败），公钥为 SPKI PEM，另外落一个 `.fingerprint` 文件供接入脚本读取；指纹算法与平台一致（DER SPKI 的 SHA-256）。已存在同名密钥默认拒绝覆盖（`keygen_refused_existing_key:<路径>`），`--force` 才替换。POSIX 下 chmod 600 |
| UX-4-02 配对向导 | 完成 | 新增 `apps/web/app/devices/page.tsx`：「生成配对」→ 弹窗展示配对码、剩余有效期倒计时、以及**一条可复制的接入命令**（`-Url/-AgentName/-Pairing`，配对串为 base64url(JSON)，与脚本解码格式一致）；过期后复制按钮禁用。数据层见 `lib/api.ts` 的"设备与接入（UX-4）"段 |
| UX-4-03 设备管理 | 完成 | 同页设备列表：设备名、device_id、公钥指纹、平台与版本、Agent、最近心跳、状态（active/revoked）；行内「轮换 Token」与「撤销」（撤销走 `ConfirmDialog` 说明后果）。轮换后弹窗展示**一次性 Token** 并明确"这是最后一次显示，平台只存 SHA-256" |
| UX-4-04 一键接入脚本 | 完成 | 新增 `scripts/connect-agent.ps1`（UTF-8 BOM，避免 Windows PowerShell 5.1 中文乱码）：`-Url -AgentName -Pairing` 三条输入，内部完成 登记 Agent（幂等 upsert）→ keygen（已有则按设备标识复用）→ device-register（私钥签名）→ Token 写入 Windows 凭据管理器 → 打印可直接粘贴的 `gateway-run` 命令（session/connection id 自动生成）。另提供 `-DeviceId / -KeyDirectory / -SkipCredential / -Force / -Start` |
| UX-4-05 README 重写 | 完成 | `README.md`「本地 Agent」改为向导主路径 + 要点边界（一公钥一设备、设备已存在时的出路、Token 只显示一次、15 分钟配对有效期、90 秒离线判定、接入 ≠ 项目授权）；老轨手工命令与原始 Gateway 调用移入「开发调试：手工命令与老轨 HTTP」附录，并注明老轨不发放设备凭证、仅本机开发使用（决策 D2） |

**顺带修掉的两个真实缺陷（都在真机跑通链路时暴露）**：

1. **`--challenge` 被 argparse 当成选项**：`secrets.token_urlsafe()` 生成的 challenge 可能以 `-` 或 `_` 开头（实测这次就是），空格分隔传给原生 CLI 时 argparse 报 `expected one argument`。约 3% 的配对会随机踩到。脚本改用 `--opt=value` 形式，README 附录同步写明。
2. **CLI 只抛 `HTTPError: 409 Conflict`，看不到服务端原因**：`agentd.request()` 现在读取响应体并抛 `ValueError(f"http_{code}:{detail}")`；`main()` 把 `ValueError/KeyError` 打成一行"错误：…"而不是 traceback（含服务端稳定错误码）。脚本据此给出按错误码分列的处理建议。

## 3. 与计划的偏差

**有偏差，共 2 处：**

1. **脚本自动登记 Agent（计划未写）**：设备注册要求 Agent 已存在且归属与配对创建者一致。若把这一步留给用户，就违背了"不需要手工编造任何 ID"。因此脚本第 1 步调用 `agentd register`（服务端是 upsert，可重复执行），用户只提供平台地址、Agent 名称与配对串。
2. **"停止接入"的语义落在脚本预检上，而不是等 409**：脚本在注册前查一次 `GET /api/devices`，命中同名 device_id 时直接给出出路（重启连接就直接跑 gateway-run；换机器就先去设备页撤销；或换 `-DeviceId`）。理由是让失败可行动，而不是让用户读一个 HTTP 409。`-Force` 可跳过预检。

## 4. 测试与验证

**基线命令（计划书 §7）**

```text
后端：python -X utf8 -m unittest discover -s . -p "test_*.py"（apps/api）
      → Ran 282 tests, OK (skipped=13)，33.3s（本阶段未改后端）
Agent：python -X utf8 -m unittest discover -s . -p "test_*.py"（apps/agent）
      → Ran 152 tests, OK (skipped=9)，3.0s
前端：cd apps/web && npm run build → 19 个静态页（新增 /devices，4.62 kB）
类型：npx tsc --noEmit → 无错误
```

新增 `apps/agent/test_agent_keygen.py`（7 项）：输出路径与指纹、私钥可被 `device-register` 的加载方式读取、**生成的密钥对能完成配对签名与验签**、拒绝覆盖、`--force` 替换、自定义名称与 next_step 提示、子命令确实注册在 CLI 上。

**端到端验收（真实平台 + 真实脚本 + 浏览器）**

| 验收标准 | 操作 | 结果 |
| --- | --- | --- |
| keygen 产物能真的用于配对 | `keygen` 生成密钥 → 真实 `device-register` | 注册成功，服务端回的 `public_key_fingerprint` 与 keygen 打印的完全一致（`d3118c98…36af`），配对一次性消费 |
| 界面上生成配对 | `/devices` → 生成配对 | 弹窗显示配对码、`14:57 后失效，且只能用一次`、可复制命令（含 Agent 名） |
| 复制命令后执行即可接入（无需手工 ID） | 复制 → 粘贴执行 | 5 步全部完成：Agent 登记 → 密钥 → 设备注册 → **Token 写入 Windows 凭据管理器（`{"status":"STORED"}`）** → 打印可粘贴的 gateway-run 命令 |
| Agent 出现在设备列表且在线 | 启动 Gateway（凭据自动读取） | `agent_connections` 出现 `conn-gw-001` 状态 **CONNECTED**（该行只在认证成功时创建）；`/devices` 显示设备与最近心跳；`agent-gw` 状态 online |
| 失败路径可读 | 用已注册的 device_id 重新接入 | 脚本预检拦住并给出三条出路，而不是 409；CLI 侧错误为 `错误：http_409:{"detail":"device_pairing_not_pending"}`（一行信息，无 traceback） |
| 设备已存在 / 公钥复用 | 复用同一密钥注册第二台设备 | 服务端拒绝 `device_public_key_already_registered`（设计如此：一公钥一设备），脚本默认"密钥名跟随设备标识"避免踩到 |

## 5. 尚未完成与边界

- **接入 ≠ 项目授权**：新接入的 Agent 只有设备身份，没有项目范围能力。它不会出现在项目看板（`dashboard.agents` 按项目授权过滤），也还不能领任务。这是 UX-5（Agent 任务闭环）的输入。
- **连接状态在客户端被强杀时不会收敛**：实测 `taskkill /F` 杀掉 Gateway 后，`agent_connections` 仍为 `CONNECTED`、`disconnected_at` 为空（80 秒后仍未收敛，服务端未观测到断开）。Agent 状态本身会在 90 秒心跳超时后变 offline（已验证）。影响面：连接表残留、以及未来"最后一条连接断开即离线"的判定可能被旧行干扰。建议后续在维护扫描里回收"所属 Agent 已离线"的连接（本轮未改，避免越界修改 UX-3 已交付的行为）。
- **撤销设备的 UI 未在浏览器里点过**：接口与按钮已实现（`devices-revoke-*` + 确认弹窗），本轮验收聚焦"接入成功"路径，撤销只做了代码级检查。
- **Token 轮换弹窗未在浏览器里点过**：同上，`rotateDeviceToken` 的一次性 token 展示逻辑已实现但未实操。
- **`/devices` 没有分页与筛选**：设备数量大时需要（demo 规模够用）。
- **老轨 HTTP 端点保留未删**（决策 D2 只隐藏不删除），README 已把它标为开发调试。

## 6. 下一步

- 下一阶段：**UX-5 Agent 任务闭环**（入口条件 UX-4 已满足）
- 建议的下一批工作项：`UX-5-01`（`agentd worker-run` 常驻循环）、`UX-5-02`（空队列退避与紧急停止）、`UX-5-03`（执行适配：至少跑通一种真实任务类型）、`UX-5-04`（`/runs` 显示执行者与进度）、`UX-5-05`（端到端契约测试：建任务 → Agent 领取 → 上报结果）
- UX-5 必须先解决"接入后的 Agent 没有项目授权"这一步：设计上应让向导或脚本在接入时同时创建项目授权（`agent_project_grants` / 项目能力 Token），否则"人只点界面、Agent 自动干活"这条验收无法达成。
- 另外把「连接状态收敛」作为 UX-5 的一个小前置项（见 §5 第二条），它会影响 worker 重连时的连接复用判定。

## 7. 复现命令

```powershell
# 1) 基线
cd apps\api; $env:PYTHONPATH = "$(Resolve-Path '..\..');$(Resolve-Path '.')"
python -X utf8 -m unittest discover -s . -p "test_*.py"      # Ran 282 tests, OK (skipped=13)
cd ..\agent; $env:PYTHONPATH = "$(Resolve-Path '..\..');$(Resolve-Path '.')"
python -X utf8 -m unittest discover -s . -p "test_*.py"      # Ran 152 tests, OK (skipped=9)
cd ..\web; npm run build                                      # 19 个静态页（含 /devices）

# 2) 端到端接入（两个终端）
python -X utf8 -m uvicorn scripts.acceptance_empty_api:app --host 127.0.0.1 --port 8010
cd apps\web; $env:NEXT_PUBLIC_API_URL = "http://127.0.0.1:8010"; npx next dev -p 3014

# 3) 走查要点
#    /devices → 生成配对 → 复制命令 → 在仓库根执行（粘贴即可）
#    执行脚本最后打印的 gateway-run → /devices 设备 last_seen 更新、agent_connections 出现 CONNECTED
```