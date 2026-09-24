# Demo 1.0 项目实施计划书（用户体验主线）

> 日期：2026-09-16
>
> 状态：`PLAN`（阶段划分与冻结决策已成型；§10 有 3 项待拍板，均已给默认值，不停工等待）
>
> 主线：把平台从「工程上能用」推进到「用户能自助用」
>
> 上游依据：`docs/PROJECT_EXECUTION_PLAN.md`（P0–P9 工程主线）、`docs/PRODUCT_AND_SAAS_DECISIONS.md`（产品与 SaaS 决策）、`docs/INTEGRATION_HYPER_RAG_PLAN.md`（检索集成）、`docs/DOMAIN_GLOSSARY.md`（术语）

---

## 0. 本文档的用法（防偏移的第一道闸）

本文档是 **Demo 1.0 阶段的单一事实来源**。它存在的目的不只是"记录计划"，更是**在对话上下文被压缩、任务被切换、隔几天回来续做时，把人和 Agent 拉回同一条基线**。

因此有两条硬性使用规则：

**规则一：开工前必读。** 任何一次续接（新会话、上下文压缩后、隔天回来），按顺序读：

1. 本文档 `§0`（用法）、`§3`（冻结决策）、`§4`（阶段总览）——约 3 分钟；
2. 当前正在做的那个阶段对应的 `§5.x` 小节**全文**；
3. `docs/handoffs/` 下最近一份 `UX-*` 交接文档（如果当前阶段已开工）；
4. `docs/IMPLEMENTATION_STATUS.md` 的最后一节。

**规则二：先跑基线再动手。** 执行 `§7` 的基线命令，确认当前工作树是绿的。基线不绿时，先修基线，不开始新工作项——否则无法区分"我改坏的"和"本来就坏的"。

**不做的事**：不要在本文档之外另起计划文档，不要把决策记在聊天里。所有决策变更走 `§6.5` 的追加流程。

---

## 1. 任务背景

### 1.1 平台是什么

`math-agent-platform` 是一个面向数学建模竞赛队伍的多 Agent 协作平台：FastAPI 控制平面（`apps/api`）+ Next.js 工作台（`apps/web`）+ 本地 Agent 运行时（`apps/agent`）+ 跨端协议包（`packages/`）。平台掌握 Task / Handoff / Artifact / Run / Evidence / Review / Gate / Event 八类事实，定位是"把协作过程变成可接力、可复核、可交付的结构化过程"（`docs/PRODUCT_AND_SAAS_DECISIONS.md`）。

### 1.2 已经完成的部分

工程主线 P0–P9 的开发版已完成：多租户与 RBAC、成果物与对象存储、Agent Gateway 协议与设备身份、任务编排与交接审核、Runner 与信息边界审计、CUMCM 领域包、协同文档、论文交付流水线、自托管部署切片。最近的整合工作是把另一个项目 `question-bank`（SynapFlow）的 Hyper-RAG 检索能力作为**独立服务**接入，形成三条新链路：知识库索引与检索问答（`/kb`、`/ask`）、超图与向量库可视化（`/graph`）、云盘 PDF 经 MinerU 转 Markdown 入知识库（`/drive` → `/api/convert`）。

当前可运行状态（本机已验证）：API 与 Web 可一键启动，检索服务独立运行在 8100 端口，前端 16 条路由构建通过，后端回归 266 项通过。

### 1.3 本轮触发：一次用户体验走查

工程能力齐备之后，我们做了一次**以用户视角的端到端走查**：假设一个真实用户拿到这个平台，他要怎么新建项目、怎么用模板、怎么把自己的 Agent 接进来。

走查结论用一句话概括：**后端能力相当完整，断点几乎全部集中在「最后一公里」——前端入口与 onboarding 脚本。** 平台名义上是多 Agent 协作平台，但用户实际能走通的链路里，Agent 从头到尾是缺席的。

三条链路的具体结论：

**链路一 · 新建项目：前端完全没有这个动作。** 后端 `POST /api/projects`（`apps/api/app/main.py:382`）是现成的，但前端对 `/api/projects` 只有 `GET`（`apps/web/lib/api.ts:245`），全仓无任何页面或脚本调用创建接口。项目只能来自 SQLite 开发库种子（`apps/api/app/store.py:696-751`，自动插入样例项目「C题 · 风光储能协同优化」）、`curl` 直调 API、或 `POST /api/projects/restore` 恢复导出包。界面上侧栏项目块是静态展示不可点击（`apps/web/components/shell.tsx:122`），项目列表为空时页面进入死状态（侧栏「未选择项目」、总览页「等待项目数据」），没有任何新建引导。

**链路二 · 模板：只能"应用已绑定的那一个包"，不能选择也不能新建。** `/pack` 页面有四个动作（一键应用模板包、运行校验、预览骨架、边界审计/落库门禁），但全部作用于当前项目已绑定的那个包；`getCompetitionPacks()` 列表接口在前端从未被调用（`apps/web/lib/api.ts:392`，死代码），用户看不到有哪些包可选，`problem_code` 也不能在 apply 时选（API 支持，页面只发 `questions`）。**「新建模板」当前不存在**——模板来自服务端 `load_pack` 加载的内置 CUMCM 领域包，页面上没有任何创建/上传/编辑模板的入口。

**链路三 · 连接 Agent：协议扎实，但用户要手拼四五个 ID。** 存在双轨：老轨 `POST /api/agents/register` 无凭证下发、`agent_id` 由客户端自带且无归属校验；新轨（设备配对 + Gateway WebSocket）做得相当完整——`POST /api/devices/pairings` 一次性返回 `pairing_code` 与 `challenge`（只存哈希、默认 TTL 900s），Agent 用 Ed25519 私钥签名后调 `device-register`，服务端一次性下发 `device_token`（只存 SHA-256），支持轮换与撤销，Gateway 有序号校验、幂等键、命令结果重放。**但这条链路在前端是零**：没有配对页、没有设备管理 UI、仓库内没有 keygen 命令（用户需自备 PEM 私钥），README 要求用户手拼 ws URI 并自选 `session_id`/`connection_id`。

除接入难度外，还有三个更本质的缺口：

1. **没有任何代码写 `offline`**（全仓仅有类型字面量，无超时扫描），Agent 崩溃后界面永远显示在线；
2. **平台不能主动派活**——Gateway 是逐帧应答模型，`agentd` 只有一次性 `claim/progress/complete` 子命令，没有常驻任务循环；
3. **掉线后任务卡死**——`_expire_leases` 只置 `EXPIRED` 且为懒执行（`apps/api/app/store.py:1640-1642`），`claim_next_task` 又要求任务处于 READY/NEEDS_REVISION（`store.py:1706-1713`），于是任务停在 CLAIMED 既不被回收也不能被他人领取。

另外，前端 `/ws/projects/{id}` 的服务端 `broadcast_event`（`apps/api/app/main.py:259`）**全仓无调用者**，是死代码；实际效果是只有建连首帧触发一次刷新，此后别人的改动与 Agent 上下线都必须手动刷新才能看到。

完整证据索引见 **附录 A**。

### 1.4 为什么叫 "Demo 1.0"

平台已有大量"实现完成但用户够不到"的能力（后端端点齐备、UI 缺入口）。继续加后端能力，边际收益会持续下降。所以 Demo 1.0 的范围**不是新功能**，而是把已有能力接上用户可见的入口，并让"在线状态""协作编辑""门禁批准"这类**已经在界面上表达了承诺、但行为与承诺不符**的地方对齐。

Demo 1.0 的完成定义是一句话：**一个没读过源码的人，能在 15 分钟内，从零把项目建起来、把模板铺开、把自己的 Agent 接进来、看着 Agent 领走一个任务并产出成果物。**

---

## 2. Demo 1.0 的目标与非目标

### 2.1 目标（可验收口径）

1. **零手调 API 的完整旅程**：新建项目 → 应用模板包 → 建/改任务 → 接入 Agent → Agent 自动领取任务 → 上报结果 → 人工审核门禁 → 交付物生成，全程只在 Web 界面操作。
2. **界面不说谎**：Agent 掉线显示离线；协作编辑的内容要么被保存、要么明确告知未保存；门禁能真的被人工批准。
3. **接入一个 Agent 不超过 3 条命令**，且不需要用户手工编造任何 ID。
4. **回归与构建维持绿色**：后端测试全通过且数量不低于当前基线，前端构建通过、无类型错误。

### 2.2 非目标（Demo 1.0 明确不做）

- 检索结果进入 Review/Gate 门禁（沿用 `INTEGRATION_HYPER_RAG_PLAN.md` 的冻结决策 D1）；
- 改动 `hyper-rag-service` 的代码（冻结决策 D4：保持服务独立、零改动）；
- 用 G6 全量替换现有 SVG 超图渲染；
- 引入 Yjs 协同持久化后端（协作编辑改为显式保存，见 D8）；
- 真实跨机恢复、备份演练、Helm/K8s、NATS 消费者、OTel 导出；
- 模板的可视化编辑器与模板 DSL（推迟，见 §10 待拍板 P-1）。

---

## 3. 冻结决策

以下决策在 Demo 1.0 期间视为**冻结基线**。变更必须走 `§6.5` 的追加流程，不得在实现中悄悄改变。

| # | 决策 | 内容与理由 | 来源 |
| --- | --- | --- | --- |
| D1 | 验收口径是"零手调 API" | Demo 1.0 的所有验收标准都以"用户只通过 Web 界面能否完成"为准；任何需要 curl/改配置文件才能走通的链路都算未完成 | 本计划 |
| D2 | Agent 协议收敛到新轨 | 设备配对 + Gateway 为唯一用户路径；老轨 `POST /api/agents/register` 标记为 **dev-only**，保留但不在 UI/文档中暴露。理由：老轨无凭证下发、`agent_id` 无归属校验，无法回答"谁连了谁的机器"；但删除会破坏既有测试与 `agentd` 子命令，故保留 | 本计划 |
| D3 | 在线状态以心跳超时判定 | 心跳周期 30s，超时阈值 **90s**（3×周期）；由后台周期扫描（10s 一次）落库 `offline`，并在事件流写 `agent.offline`。理由：懒执行会让 UI 撒谎；不引入 Redis，用数据库扫描即可满足 demo 规模 | 本计划 |
| D4 | 租约过期后的任务去向区分"是否已开工" | 从 `CLAIMED`（未开工）超时 → 回 `READY`，可被任何 Agent 重新领取；从 `RUNNING`（已开工）超时 → 回 `NEEDS_REVISION` 并登记风险。理由：把半成品当未开工会导致重复劳动且丢失"这个任务曾被做过"的事实 | 本计划 |
| D5 | 派活先用 Agent 侧拉取循环 | 实现 `agentd worker-run` 常驻循环（claim → 执行 → progress → result，含租约心跳与重连退避），**不**把 Gateway 改成服务端推送。理由：Gateway 现为逐帧应答模型，改推送的改动面与风险都大；拉取循环在断线/重启下语义更简单。服务端推送记为后续演进 | 本计划 |
| D6 | 实时刷新沿用粗粒度策略 | 接上现有 `broadcast_event`（死代码）在写路径调用；前端保持"收到任意事件即全量 refresh"。理由：Demo 1.0 不引入前端增量 diff，避免把体验问题变成前端状态机复杂度问题 | 本计划 |
| D7 | 项目选择持久化用 localStorage | key 为 `map.selectedProjectId`，**不**引入 URL 查询参数。理由：全部页面为静态预渲染客户端页面，用 `useSearchParams` 需要额外 Suspense 边界；URL 分享（可把项目链接发给队友）列为后续 | 本计划 |
| D8 | 协作编辑改为**显式保存** | 不引入 Yjs 服务端持久化；编辑器内容通过显式"保存草稿"动作写回 `POST /api/artifacts/{id}/content`，UI 明确区分"本地未保存"与"已保存版本"。同时**修正误导性文案**（当前页面提示"定稿请用提交待审写入正式版本"，但提交的是旧骨架内容）。理由：协作中继服务明确声明不保存状态（`apps/api/app/collaboration.py:3-5`） | 本计划 |
| D9 | Agent 不能替人批准 | 门禁批准/交接收据的 accept/reject 只做人工入口，UI 上人工动作与 Agent 动作视觉区分。沿用 `PROJECT_EXECUTION_PLAN.md §3` 已冻结的"审批边界"。**实现方式已定**：批准＝提交 `verdict=APPROVED` 的人工 Review（`POST /api/projects/{id}/reviews`），门禁状态由 Review 派生（`store.py:2867`），**不新增独立的门禁批准端点**；`reviewer_kind` 只接受 `member` | 已有决策 + 本计划补充实现方式 |
| D10 | 错误信息必须透传到用户 | `apps/web/lib/api.ts` 统一抛出 `ApiError{status, code, detail}`（解析服务端 `detail`），页面**禁止**再写裸 `catch { notify("…失败") }`。稳定错误码沿用现有风格（`ai_credentials_missing`、`hyper_rag_unavailable`、`kb_access_denied` 等） | 本计划 |
| D11 | 知识库归属显式化 | `/kb` 新建时提供"个人库 / 项目库"选择，默认个人库；项目库复用项目 RBAC。理由：`createKb` 已支持 `project_id`，但 UI 从不传，导致从 Web 建的永远是个人库 | 本计划 |
| D12 | 模板包范围 | Demo 1.0 交付「包选择器 + `problem_code` 选择 + 包版本可见」；**自定义模板/新建模板推迟**（见 P-1）。理由：需要模板 DSL、校验规则与版本化，成本高于 demo 收益 | 本计划（待拍板） |
| D13 | 前端改动必须重建并重启 | 改 `apps/web` 后必须 `npm run build` **并重启 Web 进程**；`next start` 在启动时缓存构建清单，只重建文件不重启会导致页面持续发旧 HTML（引用旧 chunk），症状酷似浏览器缓存。已写入 `README.md` 本地启动章节 | 已有教训 |
| D14 | 不改 `hyper-rag-service`，不改门禁语义 | 检索增强与门禁解耦；`hyper-rag-service` 保持独立进程（8100），平台只做 HTTP 代理；服务凭据随请求传入，平台不落明文 | 已有决策 |

---

## 4. 阶段与依赖总览

编号规则：阶段 `UX-0` … `UX-7`，工作项 `UX-{阶段}-{序号}`（如 `UX-3-02`）。`UX-` 前缀用于与工程主线的 `P0`–`P9` 明确区分，两者不共用编号空间。

```text
UX-0 基线冻结与防偏移机制落地
  │
  └─> UX-1 前端地基与项目起点        ← 建项目、项目选择持久化、ApiError、骨架、确认弹窗
        │
        ├─> UX-2 任务与模板闭环      ← 任务详情页、改派、模板包选择器、problem_code
        │
        ├─> UX-3 Agent 状态真实性    ← offline 判定、租约回收、事件广播接通
        │     │
        │     ├─> UX-4 接入向导与设备管理  ← keygen、配对向导、设备撤销/轮换、一键脚本
        │     │     │
        │     │     ─> UX-5 Agent 任务闭环 ← agentd worker-run 常驻循环
        │     │
        │     ─> UX-6 协作正确性    ← 协作编辑落库、门禁批准、交接收据、项目知识库
        │
        └─> UX-7 打磨与 Demo 1.0 验收  ← 依赖 UX-1..UX-6 全部退出
```

依赖是**入口条件**而非建议：`UX-3` 未退出前不开始 `UX-4`（否则向导接进来的 Agent 状态本身就是假的，无法验证）；`UX-7` 必须等 `UX-1`–`UX-6` 全部退出。`UX-2` 与 `UX-3` 可并行，`UX-6` 与 `UX-4/5` 可并行。

---

## 5. 阶段细则

### UX-0：基线冻结与防偏移机制落地

**目标**：让后续每个阶段都从同一条可复现的基线出发。

**入口条件**：无。

**工作项**

| 编号 | 工作项 | 交付物 |
| --- | --- | --- |
| UX-0-01 | 记录基线数字：后端 266 项通过（13 skipped）、前端构建 16 路由、部署脚本三件套可用 | 本文档 §7 与 `docs/IMPLEMENTATION_STATUS.md` 追加一节 |
| UX-0-02 | 建立 `docs/handoffs/` 的 UX 阶段交接文档模板（见 §6.6） | `docs/handoffs/_TEMPLATE_UX_HANDOFF.md` |
| UX-0-03 | 在 `docs/IMPLEMENTATION_STATUS.md` 顶部加"当前主线指向本文档"的指针 | 文档改动 |
| UX-0-04 | 把 §6.4 禁区清单同步到 `README.md` 的贡献须知 | 文档改动 |

**验收标准**

```powershell
# 后端回归（在 apps/api 目录下）
python -X utf8 -m unittest discover -s . -p "test_*.py"   # 期望：Ran 266 tests, OK (skipped=13)
# 前端构建
cd apps\web; npm run build                                # 期望：16 条路由全部静态预渲染，无类型错误
```

**退出条件**：基线数字与本文档一致；交接模板就位。

**Do NOT**：不要在这个阶段顺手改任何业务代码。

---

### UX-1：前端地基与项目起点

**目标**：让"新建项目"成为可能，并把后续所有阶段都要复用的前端基础设施一次做对。

**入口条件**：UX-0 退出。

**工作项**

| 编号 | 工作项 | 关键文件 / 端点 |
| --- | --- | --- |
| UX-1-01 | 统一错误对象：`ApiError{status, code, detail}`，所有 `lib/api.ts` 函数在非 2xx 时解析服务端 `detail` 并抛出（决策 D10） | `apps/web/lib/api.ts` |
| UX-1-02 | 通用组件：`LoadingSkeleton`、`ConfirmDialog`（替代"危险操作一点即执行"） | `apps/web/components/ui.tsx` |
| UX-1-03 | `createProject()` + 新建项目 Modal（名称、竞赛模板包、题号 `problem_code`、描述），成功后**自动选中新项目并跳转总览** | `lib/api.ts` 新增函数；`apps/web/app/page.tsx`；端点 `POST /api/projects`（`main.py:382`） |
| UX-1-04 | 空状态引导：项目列表为空时，总览页与侧栏给出"还没有项目 → 立即新建"的引导，替换现在的"等待项目数据"死状态 | `apps/web/app/page.tsx`、`apps/web/components/shell.tsx` |
| UX-1-05 | 项目选择持久化：`localStorage["map.selectedProjectId"]`，刷新后恢复；选中的项目被删除时回退到列表首项并提示 | `apps/web/lib/workspace.tsx:71-115` |
| UX-1-06 | 项目切换器常在：`projects.length > 1` 才显示的逻辑改为始终显示（含单项目时的"新建项目"入口） | `apps/web/components/shell.tsx:179-192` |

**验收标准**

1. 全新数据库（删掉 SQLite 开发库）启动后，界面不再出现"等待项目数据"，而是引导新建；在界面完成新建后，项目出现在侧栏并被选中。
2. 刷新页面，选中的项目保持不变；切换到另一个项目后刷新，仍是切换后的那个。
3. 手动制造一次后端错误（例如用非法名称建项目），界面显示服务端返回的具体原因，而非"创建失败"。

**退出条件**：`POST /api/projects` 在前端可达且被 1 处 UI 调用；骨架/确认组件被至少 1 处真实使用（避免"建了不用"）。

**Do NOT**：不要顺手改 `apps/api` 的建项目语义（权限、字段校验）；不要引入 URL 参数持久化（决策 D7）。

---

### UX-2：任务与模板闭环

**目标**：把"模板包"从"只能操作已绑定的那个"变成"可选、可懂"，把任务从"只能看列表行"变成"能看详情、能改派"。

**入口条件**：UX-1 退出。

**工作项**

| 编号 | 工作项 | 关键文件 / 端点 |
| --- | --- | --- |
| UX-2-01 | 任务详情：点击任务行打开详情抽屉/弹窗（描述、阶段、负责人、状态、依赖、关联成果物、事件流、交接记录）。Demo 1.0 用弹窗，**不**引入动态路由 | `apps/web/app/tasks/page.tsx` |
| UX-2-02 | 改派负责人：负责人下拉改为从 `dashboard.agents` + 成员列表派生（删除硬编码的 Mira/Aster/Nova/Reviewer）；创建后也可改派，走 `PATCH /api/tasks/{task_id}`（前端目前只发 status，需扩展） | `apps/web/app/tasks/page.tsx:174-179`、`lib/api.ts:272-276`、`main.py:1636-1641` |
| UX-2-03 | 状态机提示：状态流转按钮显示目标状态与后果；非法流转时展示服务端拒绝原因 | `apps/web/app/tasks/page.tsx:130-135` |
| UX-2-04 | 模板包选择器：接入 `getCompetitionPacks()`（当前死代码），展示可用包与版本；项目未绑定包时给出绑定入口 | `lib/api.ts:392`、`apps/web/app/pack/page.tsx` |
| UX-2-05 | `problem_code` 选择：apply 时可选题号（API 已支持，页面未发） | `apps/web/app/pack/page.tsx:36-38`、`lib/api.ts:404-407` |
| UX-2-06 | apply 前确认：展示"将创建 N 个任务与 M 个模板成果物"的确认弹窗（复用 UX-1-02 的 `ConfirmDialog`） | `apps/web/app/pack/page.tsx:32-51` |

**验收标准**

1. 在界面上能看到可用模板包列表与版本，能选择题号后应用，且应用前有确认、应用后提示实际创建数量。
2. 新建任务时负责人来自真实 Agent/成员列表；创建后可在界面改派，刷新后保持。
3. 任务详情弹窗能看到该任务的依赖与关联成果物。

**退出条件**：`getCompetitionPacks` 不再是死代码；任务负责人无任何硬编码名字残留。

**Do NOT**：不要实现自定义模板的创建（决策 D12）；不要给任务加新的状态（状态词表已在 `docs/DOMAIN_GLOSSARY.md` 冻结）。

---

### UX-3：Agent 状态真实性

**目标**：让界面上关于 Agent 的一切都与事实一致——在线就是在线，掉线就是掉线，掉线后任务可被回收。

**入口条件**：UX-1 退出（可并行于 UX-2）。

**工作项**

| 编号 | 工作项 | 关键文件 / 端点 |
| --- | --- | --- |
| UX-3-01 | 心跳超时扫描：后台周期任务（10s）把 `last_seen` 超过 90s 的 Agent 置 `offline`，写 `agent.offline` 事件（决策 D3） | `apps/api/app/store.py`（新增扫描）、`apps/api/app/main.py`（启动期注册） |
| UX-3-02 | 新增状态写入点：Gateway 断开、HTTP 心跳超时、设备撤销都要能落 `offline`；当前全仓只写 `online` | `store.py:2518/2523`、`store.py:1276-1294`、`gateway.py:697` |
| UX-3-03 | 租约回收：把 `_expire_leases` 从"仅懒执行"升级为周期执行，并按 D4 区分 `CLAIMED→READY` 与 `RUNNING→NEEDS_REVISION`（后者登记风险） | `store.py:1640-1642`、`store.py:1706-1713` |
| UX-3-04 | 事件广播接通：在写路径调用现有 `broadcast_event`（当前是死代码），让 `/ws/projects/{id}` 真的推送 | `apps/api/app/main.py:259`、前端 `apps/web/lib/workspace.tsx:103-110` |
| UX-3-05 | 界面联动：Agent 掉线/上线、任务被回收，界面无需手动刷新即可反映（决策 D6 的粗粒度 refresh） | `apps/web/components/shell.tsx:105`、`apps/web/app/runs/page.tsx` |
| UX-3-06 | 在线状态可解释：`/runs` 显示每个 Agent 的 `last_seen` 与判定依据（"超过 90 秒未心跳"） | `apps/web/app/runs/page.tsx:43-53` |

**验收标准**

1. 启动一个 Agent 后杀掉进程，90–100 秒内界面自动变为离线，无需手动刷新。
2. 一个任务被领取后杀掉 Agent，租约过期后：未开工的任务回到可领取状态并出现在其他 Agent 的可见队列里；已开工的任务变为 `NEEDS_REVISION` 且有风险记录。
3. `broadcast_event` 有真实调用方（可用 grep 与运行时日志双向确认）。

**退出条件**：全仓不存在"只写 online、从不写 offline"的路径；租约回收有对应的自动化测试。

**Do NOT**：不要引入 Redis/消息队列做状态存储；不要把超时阈值做成可配置项塞进环境变量（demo 期固定 90s，理由写进代码注释）。

---

### UX-4：接入向导与设备管理

**目标**：把"接入一个 Agent"从"手拼 ws URI + 自备密钥 + 手抄配对码"变成"三条命令"。

**入口条件**：UX-3 退出（状态真实性是接入体验可信的前提）。

**工作项**

| 编号 | 工作项 | 关键文件 / 端点 |
| --- | --- | --- |
| UX-4-01 | 密钥生成：新增 `agentd keygen`（Ed25519，输出私钥路径与公钥指纹），补齐仓库内缺失的密钥生成能力 | `apps/agent/agentd.py`、`packages/device_identity` |
| UX-4-02 | 配对向导页：Web 上一键生成配对（展示 `pairing_code` 与 `challenge`，倒计时 TTL）+ 复制待执行的完整命令 | 新页面（建议 `apps/web/app/devices/page.tsx`）；端点 `POST /api/devices/pairings`（`main.py:454-459`） |
| UX-4-03 | 设备管理：设备列表（名称、平台、Agent、最后心跳、状态）、撤销、轮换 Token；Token 一次性明文只在创建响应里出现并明确提示"只显示一次" | 同上；端点 `main.py:478-483`、`486-491` |
| UX-4-04 | 一键接入脚本：`scripts/connect-agent.ps1`，参数只有平台地址与 Agent 显示名，内部完成 keygen → device-register → credential-save → 启动 Gateway 连接 | `scripts/connect-agent.ps1` |
| UX-4-05 | README 重写「本地 Agent」章节：以向导 + 三条命令为主路径；老轨（`register/heartbeat/claim` 手调）移入"开发调试"附录（决策 D2） | `README.md:50-108` |

**验收标准**

1. 在 Web 上生成配对，复制命令到另一台机器（或本机新目录）执行 `scripts\connect-agent.ps1`，Agent 出现在设备列表且状态在线——全程无需手工编造任何 ID。
2. 撤销设备后，该设备的 Gateway 连接被断开且无法重连（服务端 4401）。
3. 轮换 Token 后旧 Token 失效、新 Token 可用，界面有明确的一次性提示。

**退出条件**：文档与 UI 中不再出现"让用户手拼 `session_id`/`connection_id`"的路径。

**Do NOT**：不要删除老轨端点与其测试（D2 只要求隐藏，不要求删除）；不要在 UI 中显示已保存 Token 的明文（服务端只存哈希，无法回显，必须一次提示）。

---

### UX-5：Agent 任务闭环

**目标**：让"Agent 自动领任务"真实发生——这是"多 Agent 协作平台"这个名字成立的前提。

**入口条件**：UX-4 退出。

**工作项**

| 编号 | 工作项 | 关键文件 / 端点 |
| --- | --- | --- |
| UX-5-01 | `agentd worker-run` 常驻循环：轮询 claim → 执行 → progress → result → 释放租约；含租约心跳、指数退避重连、优雅退出（决策 D5） | `apps/agent/agentd.py`、`apps/agent/machine_service.py:467-525`、`apps/agent/runner.py` |
| UX-5-02 | 空队列行为：无任务时退避轮询并上报本地队列长度；紧急停止标志仍优先生效 | `apps/agent/local_state.py`、`machine_service.py:583-592` |
| UX-5-03 | 执行适配：把现有 Runner/Adapter 接到循环里（至少跑通一种真实任务类型，如脚本执行类），其余类型显式返回"不支持"而不是静默失败 | `apps/agent/cli_adapters.py`、`apps/agent/runner.py` |
| UX-5-04 | 任务可见性：`/runs` 显示当前被哪个 Agent 执行、进度与最近事件 | `apps/web/app/runs/page.tsx` |
| UX-5-05 | 端到端契约测试：从"建任务"到"Agent 领取并上报结果"的自动化用例（可用真实 HTTP + 本地 agentd 子进程） | `apps/api/test_*`、`apps/agent/test_*` |

**验收标准**

1. 在界面建一个可执行任务，已接入的 Agent **在一分钟内自动领取**并执行完成，结果出现在成果物库，无需任何手工 claim 命令。
2. 执行中杀掉 Agent：任务按 D4 回到可领取或 `NEEDS_REVISION`，界面能解释原因。
3. 端到端契约测试在 CI 命令下可复现。

**退出条件**：存在一条"人只点界面、Agent 自动干活"的自动化验证。

**Do NOT**：不要改 Gateway 为服务端推送（D5）；不要在 demo 期实现多任务并发调度（保持单并发，先要正确性）。

---

### UX-6：协作正确性

**目标**：把界面上已经承诺、但行为不符的三处对齐：协作编辑、门禁批准、交接收据。

**入口条件**：UX-1 退出（可并行于 UX-4/5）。

**工作项**

| 编号 | 工作项 | 关键文件 / 端点 |
| --- | --- | --- |
| UX-6-01 | 协作编辑显式保存：编辑器加"保存草稿"动作，写回成果物内容；显示"未保存/已保存 + 时间"；修正当前误导性文案（决策 D8）。**写端点已存在**，属接线而非新建 | `apps/web/app/documents/page.tsx:316-326`、`lib/api.ts`（新增写调用）；端点 `POST /api/artifacts/{artifact_id}/content`（`apps/api/app/main.py:1865`） |
| UX-6-02 | 门禁人工批准接线：批准的实现方式是**提交一条人工 Review**（`verdict=APPROVED`），门禁状态由它派生，**不要新增独立的"门禁批准"端点**：<br>`POST /api/projects/{project_id}/reviews`（`main.py:2166`）→ task→`APPROVED`／artifact→`APPROVED`+`downstream_allowed=1`／handoff→`PASS`／gate→`PASSED`（`store.py:2867`）<br>UI 需展示服务端守卫的拒绝原因：`review_blocked_by_open_risks`（存在 fatal/major 未关闭风险）、`task_not_waiting_for_review`、`approved_task_is_immutable`、`rejected_handoff_requires_revision`；`reviewer_kind` 必须为 `member`（决策 D9） | `apps/web/app/review/page.tsx:56-69` |
| UX-6-03 | 交接收据 accept/reject：在 `/handoffs` 与审核页收据块加操作入口 | `apps/web/app/handoffs/page.tsx`；端点 `main.py:1729-1746` |
| UX-6-04 | 项目知识库：`/kb` 新建时可选"个人库/项目库"，列表显示归属（决策 D11） | `apps/web/app/kb/page.tsx:44`、`lib/api.ts:983` |
| UX-6-05 | 危险操作确认：删除云盘文件、删除会话、落库门禁、生成提交包接入 `ConfirmDialog`；下载类与只读类不加确认 | `apps/web/app/drive/page.tsx:174-176`、`ask/page.tsx:150`、`pack/page.tsx:186`、`delivery/page.tsx:81` |

**验收标准**

1. 在文档编辑器里输入内容 → 保存 → 刷新页面，内容仍在；未保存时界面有明确提示；"提交待审"冻结的是已保存内容。
2. 门禁可以在界面上被人工批准，批准后下游物化状态随之变化。
3. 交接可以在界面上被接受/拒绝，收据状态更新。
4. 四个危险操作有二次确认。

**退出条件**：`docs/IMPLEMENTATION_STATUS.md` 中"协作编辑不保存"这条已知边界被移除。

**Do NOT**：不要让 Agent 具备批准能力（D9）；不要引入 Yjs/CRDT 服务端持久化（D8）。

---

### UX-7：打磨与 Demo 1.0 验收

**目标**：补齐一致性细节，并产出可重复演示的验收脚本。

**入口条件**：UX-1 至 UX-6 全部退出。

**工作项**

| 编号 | 工作项 | 关键文件 |
| --- | --- | --- |
| UX-7-01 | 加载态全覆盖：数据未到时显示骨架而非"暂无"（当前 `/artifacts` 等 6 处会误导） | 各页面 + `components/ui.tsx` |
| UX-7-02 | 死代码清理或接线：`importCumcmWorkspace`、`getDocumentLayers`、`getDocumentEvidence`、`getAiSettings/saveAiSettings`（设置页自己裸 fetch）——逐个决定"接线"或"删除"，不允许留着不说 | `apps/web/lib/api.ts` |
| UX-7-03 | 设置页加"测试连接"：对 LLM/Embedding/MinerU 各发一次最小请求并显示结果 | `apps/web/app/settings/page.tsx` |
| UX-7-04 | 答辩提纲可下载；`/graph` 的"实体管理/关系管理"要么补增删改、要么改名为"实体浏览/关系浏览"（避免名不副实） | `apps/web/app/delivery/page.tsx:193-197`、`app/graph/page.tsx:167-202` |
| UX-7-05 | 端到端演示脚本：`scripts/demo-1.0.ps1`，从空库开始跑完整旅程并逐步打印检查点 | `scripts/` |
| UX-7-06 | Demo 1.0 验收清单：逐条对照 §2.1 的目标给出证据（截图/日志/测试名） | `docs/DEMO_1_0_ACCEPTANCE.md` |
| UX-7-07 | 文档收尾：`README.md` 快速开始重写为"零基础用户视角"；`docs/IMPLEMENTATION_STATUS.md` 归档本阶段 | 文档 |

**验收标准**：`scripts/demo-1.0.ps1` 在干净环境一次跑通，且第 5 项列出的四个危险操作、四类错误提示、三处状态显示都符合预期。

**退出条件**：§2.1 的 4 条目标全部有可出示的证据。

**Do NOT**：不要在验收阶段引入新功能；发现的问题记入 `docs/IMPLEMENTATION_STATUS.md` 的后续清单，不在本阶段修。

---

## 6. 防偏移机制

这一节是本文档存在的核心理由。上下文一长，最容易发生的偏移有四种：**目标漂移**（做着做着去优化别的东西）、**决策漂移**（同一个问题两次给出不同答案）、**范围漂移**（顺手重构把稳定模块改坏）、**事实漂移**（把"子代理说的"当成"我验证过的"）。下面分别给出机制。

### 6.1 上下文压缩后的恢复协议

每当会话续接或上下文被压缩，按以下顺序恢复，**不要凭记忆开工**：

1. 读本文档 `§0`、`§3`、`§4`；
2. 读当前阶段的 `§5.x` 全文（工作项表格里每一行的文件锚点就是这次要动的范围）；
3. 读 `docs/handoffs/` 里最近一份 `UX-*` 交接文档的 `§2 实际完成内容` 与 `§5 尚未完成与边界`；
4. 执行 `§7` 基线命令；
5. 在回复里**用一句话复述**："当前阶段是 UX-x，本批工作项是 UX-x-yy，退出条件是 ……"。这句话是防偏移的锚，写不出来说明上下文没恢复。

### 6.2 术语与编号冻结

- 领域术语以 `docs/DOMAIN_GLOSSARY.md` 为准，**不得**在代码、UI 文案、文档中引入同义词（例如不要把 Gate 叫"检查点"、不要把 Handoff 叫"转交单"）。
- 状态词表冻结在 `DOMAIN_GLOSSARY.md` 的"状态词"节；新增状态必须走 `§6.5`。
- 阶段编号空间：工程主线 `P0`–`P9`（`docs/PROJECT_EXECUTION_PLAN.md`）、检索集成 `Phase A`–`D`（`INTEGRATION_HYPER_RAG_PLAN.md`）、本主线 `UX-0`–`UX-7`。**三个空间不混用**：不要在 UX 阶段里发明新的 P 编号。

### 6.3 不变式（每个阶段退出前都必须成立）

这些是"无论做到哪一步都不能破"的性质。任何一批改动提交前自检：

| # | 不变式 |
| --- | --- |
| I1 | 检索增强与门禁解耦：检索结果不参与 Review/Gate 的任何判定 |
| I2 | `hyper-rag-service` 代码零改动；平台只通过 HTTP 代理访问它 |
| I3 | 模型与转换凭据按请求传入，平台不落明文（只存掩码/哈希） |
| I4 | 知识库只存文档，不承担个人网盘语义 |
| I5 | 所有机器写请求携带项目范围能力 Token；`/api/agent/*` 由路由自行校验 |
| I6 | 人工审批不可被 Agent 替代（D9） |
| I7 | 老轨 Agent 端点保留可用（D2 只隐藏不删除） |
| I8 | 后端测试数量不低于 266 且全绿；前端构建 16 路由无类型错误 |

### 6.4 禁区清单（Demo 1.0 期间禁止改动）

- `apps/api/app/collaboration.py` 的中继语义（D8 只加保存，不改中继）；
- `apps/api/app/gateway.py` 的逐帧应答模型（D5）；
- `packages/agent_protocol/` 的信封与序号语义（任何变更都会破坏跨端兼容）；
- `packages/competition_packs/` 内置包的内容（1.1.0 为已冻结交付物）；
- `apps/api/app/kb_gateway.py` 的错误码映射（`hyper_rag_*` 是稳定契约）；
- 生产部署相关：`infra/docker-compose.yml`、PostgreSQL 迁移脚本（本阶段不新增迁移，除非某工作项明确要求）。

### 6.5 决策变更流程

1. **不允许**修改 §3 表格里已有决策的结论；要变更，**追加**一条新决策 `D15`、`D16`…，内容包含：变更内容、原因、影响面、迁移方案、生效阶段。
2. 在原决策行的"来源"列末尾加 `（被 Dx 取代）`，保留原行不删。
3. 若变更影响已完成阶段，必须在 `docs/IMPLEMENTATION_STATUS.md` 追加"回归影响"说明并重跑 §7 基线。

**为什么是追加而不是改写**：上下文压缩后，重新读到"只有一条决策"的人无法知道曾经有过分歧与推翻过程，容易把已被否决的方案重新提出来。

### 6.6 阶段交接文档模板

每个 UX 阶段退出时，在 `docs/handoffs/` 下产出 `UX-{n}_{短名}_HANDOFF.md`，结构固定（与现有 P 系列交接一致）：

```markdown
# UX-{n} {阶段名} 交接

> 交接状态：PASS / PASS_WITH_ASSUMPTIONS / FAIL
> 日期：YYYY-MM-DD
> 对应阶段：UX-{n}（见 docs/DEMO_1_0_IMPLEMENTATION_PLAN.md §5.{n}）

## 1. 本阶段目标
## 2. 实际完成内容（逐条对应工作项编号）
## 3. 与计划的偏差（哪条工作项没做/做法变了，原因）
## 4. 测试与验证（命令 + 实际输出摘要）
## 5. 尚未完成与边界（明确写出"用户现在还不能做什么"）
## 6. 下一步（下一阶段入口条件是否满足）
## 7. 复现命令
```

其中 **§3 与 §5 是防偏移的关键**：偏差必须写出来，否则下一阶段的接手者会以为计划被完整执行了。

### 6.7 禁止的"顺手"行为

以下行为在 Demo 1.0 期间一律视为偏移，即使看起来是改进：

- 顺手升级依赖（Next/React/lucide 等）；
- 顺手把某个只读页面重构成可写（除 §5 明确列出的工作项）；
- 顺手统一代码风格、批量重命名；
- 顺手删除"看起来没人用"的代码——死代码的处置是 `UX-7-02` 的显式工作项，需要逐个决定；
- 顺手把 266 项测试里的 skip 改成 pass（13 项 skipped 是有意为之，动它需要单独说明）。

---

## 7. 测试与验收基线

**后端回归**（在 `apps/api` 目录执行）：

```powershell
$env:PYTHONPATH = "$(Resolve-Path '..\..');$(Resolve-Path '.')"
python -X utf8 -m unittest discover -s . -p "test_*.py"
# 基线：Ran 266 tests, OK (skipped=13)，耗时约 110 秒
# 13 项 skipped 是环境门控（未装 requirements-prod 的 PostgreSQL/MinIO 用例、
# 非 owner 运行时角色 DSN、本机 LaTeX 引擎各若干条），不是失败，也不要为了"变绿"去改它们。
```

**Agent 侧测试**（在 `apps/agent` 目录执行，前缀同上）：

```powershell
python -X utf8 -m unittest discover -s . -p "test_*.py"
```

**前端构建**：

```powershell
cd apps\web; npm run build
# 基线：16 条路由（15 页面 + not-found）全部静态预渲染，无类型错误
```

**端到端启动**（本机）：

```powershell
# 终端 1：检索服务（独立进程）
cd C:\Users\19855\Desktop\Hyper-rag\question-bank\hyper-rag-service
$env:PORT = "8100"
python -X utf8 main.py
# 终端 2：平台
cd math-agent-platform
.\scripts\local-infra.ps1 start
.\scripts\start-demo.ps1
```

**前端改动的强制提醒（D13）**：改 `apps/web` 后必须 `npm run build` **并重启 Web 进程**。`start-demo.ps1` 只在 `.next` 缺失时才构建，不会替你重建。

---

## 8. 风险与回滚

| 风险 | 触发信号 | 应对 |
| --- | --- | --- |
| 心跳扫描误判（网络抖动导致 Agent 被标 offline） | 用户反馈"我明明在线" | 阈值固定 90s（3× 周期）已留余量；若仍误判，改为"两次连续超时"再落 offline（决策追加流程） |
| 租约回收导致任务被重复执行 | 同一任务出现两次 Run | D4 已按"是否开工"区分；仍冲突时在 result 上报处做幂等校验并记录事件 |
| `worker-run` 常驻循环在 demo 机器上行为不稳定 | Agent 反复重连 | 保留 `--once` 模式（跑一轮退出）用于演示兜底；退避参数可调 |
| 协作编辑落库引入新端点破坏既有测试 | 回归变红 | 先写测试再加端点；不动 `collaboration.py`（禁区） |
| 前端改动引入构建回归 | `npm run build` 失败 | 每个工作项完成后立即构建；失败不回退到"稍后统一修" |
| 上下文压缩导致阶段错位 | 接手者说不清当前阶段 | `§6.1` 的复述动作是强制项 |

**回滚粒度**：按工作项。每个工作项应能独立回退（不跨工作项耦合改动）。不提供"整阶段回滚"——因为阶段之间有依赖，回滚一个阶段等于回滚后续所有阶段。

---

## 9. 工作量估算

估算单位为人日，含自测与文档，不含等待评审。

| 阶段 | 内容 | 估算 |
| --- | --- | --- |
| UX-0 | 基线冻结与机制落地 | 0.5 |
| UX-1 | 前端地基与项目起点 | 2.0 |
| UX-2 | 任务与模板闭环 | 2.5 |
| UX-3 | Agent 状态真实性 | 2.5 |
| UX-4 | 接入向导与设备管理 | 3.0 |
| UX-5 | Agent 任务闭环 | 4.0 |
| UX-6 | 协作正确性 | 3.0 |
| UX-7 | 打磨与验收 | 2.5 |
| 合计 | | **20** |

关键路径为 `UX-0 → UX-1 → UX-3 → UX-4 → UX-5 → UX-7`，约 14.5 人日；`UX-2`、`UX-6` 可与 `UX-3/4/5` 并行。

---

## 10. 待拍板事项

以下 3 项已给默认值，按默认值即可开工；如需调整，改动会体现在阶段范围而非整体架构。

| # | 事项 | 默认值（已按此执行） | 影响 |
| --- | --- | --- | --- |
| P-1 | 是否在 Demo 1.0 支持"新建自定义模板" | **推迟到 Demo 1.1**（D12）。理由：需要模板 DSL、校验与版本化，成本高于 demo 收益；1.0 先让"选包 + 选题号"可用 | 若改为 1.0 做，`UX-2` 需增加约 3–5 人日，并新增模板存储设计 |
| P-2 | 老轨 Agent 端点是否删除 | **保留但隐藏**（D2） | 若改为删除，需同步清理 `agentd` 子命令与既有测试，约 1 人日 |
| P-3 | `/graph` 的"实体管理/关系管理"是补功能还是改名 | **改名**（`UX-7-04`），补增删改推迟 | 若改为补功能，需在 `hyper-rag-service` 侧提供写入端点——与冻结决策 D14（服务零改动）冲突，需先走决策变更 |

---

## 附录 A：现状证据索引

走查结论的原始证据，供后续核查与推翻（行号以 2026-09-16 工作树为准）。

| 结论 | 证据位置 |
| --- | --- |
| 后端有建项目端点，前端零调用 | `apps/api/app/main.py:382`；`apps/web/lib/api.ts:245`（唯一 `/api/projects` 调用为 GET） |
| 项目来自种子数据 | `apps/api/app/store.py:696-751`（含样例项目与 `project.seeded` 事件） |
| 侧栏项目块不可点击、下拉仅在多项目时出现 | `apps/web/components/shell.tsx:122-128`、`:179-192` |
| 空项目时进入死状态 | `apps/web/app/page.tsx:46`、`components/shell.tsx:125-126` |
| 模板包列表接口是死代码 | `apps/web/lib/api.ts:392`（`getCompetitionPacks` 无调用方） |
| 模板 apply 不支持题号选择 | `apps/web/app/pack/page.tsx:36-38`（只发 `questions`）；API 侧支持 `problem_code`（`lib/api.ts:404-407`） |
| 无自定义模板入口 | `apps/web/app/pack/page.tsx`（模板清单只读）；服务端 `load_pack` 加载内置包 |
| 老轨 Agent 注册无凭证下发 | `apps/api/app/main.py:2145-2147`、`apps/api/app/store.py:2516-2520` |
| 新轨配对与一次性 Token | `apps/api/app/main.py:454-467`、`:478-483`、`:486-491`；`store.py:973-1106` |
| Ed25519 配对签名 | `packages/device_identity/__init__.py:56-114` |
| 仓库内无密钥生成命令 | `apps/agent/agentd.py`（子命令清单中无 keygen） |
| 前端无设备/配对 UI | 全仓无 pairing / device-grants / rotate-token 的前端调用；`apps/web/app/runs/page.tsx:43-53` 仅只读展示 |
| 从不写 offline | 全仓 grep 仅命中类型字面量 `contracts.py:751`；写路径见 `store.py:2518`、`:2523`、`:1276-1294` |
| 租约过期懒执行、任务卡 CLAIMED | `store.py:1640-1642`；`store.py:1706-1713`（claim 要求 READY/NEEDS_REVISION） |
| 事件广播是死代码 | `apps/api/app/main.py:259`（`broadcast_event` 无调用方） |
| 前端只在首帧刷新 | `apps/web/lib/workspace.tsx:103-110` |
| 任务负责人硬编码 | `apps/web/app/tasks/page.tsx:174-179` |
| 前端不能改派 | `apps/web/lib/api.ts:272-276`（只发 status）；API 支持 `assignee`（`main.py:1636-1641`） |
| 无任务详情页（无动态路由） | `apps/web/app` 下无 `[id]` 目录 |
| 协作编辑不落库 | 前端仅 GET `/api/artifacts/{id}/content`（`lib/api.ts:708`）；`apps/api/app/collaboration.py:3-5` 声明不保存状态 |
| 门禁无人工批准入口（后端能力其实完整） | `apps/web/app/review/page.tsx:56-69` 纯展示；能力在 `POST /api/projects/{id}/reviews`（`main.py:2166`）→ 门禁派生 `PASSED`（`store.py:2867`），守卫见 `store.py:2841-2864` |
| 成果物内容有写端点但前端只读 | 写端点 `POST /api/artifacts/{artifact_id}/content`（`main.py:1865`）；前端仅有 GET（`lib/api.ts:708`） |
| 交接收据不可操作 | 后端 `main.py:1729-1746`；`apps/web/app/handoffs/page.tsx` 只读 |
| 从 Web 建的知识库恒为个人库 | `apps/web/app/kb/page.tsx:44`（不传 `project_id`）；`lib/api.ts:983` 支持 |
| 危险操作无确认 | `apps/web/app/drive/page.tsx:174-176`、`ask/page.tsx:150`、`pack/page.tsx:186`、`delivery/page.tsx:81` |
| 错误原因被吞 | 多个页面 `catch { notify("…失败") }`，如 `app/tasks/page.tsx:58`、`app/pack/page.tsx:48` |
| 只读页面清单 | `/`、`/graph`、`/artifacts`、`/handoffs`、`/runs`、`/timeline` |

## 附录 B：本阶段涉及的关键文件

| 区域 | 文件 | 本阶段动作 |
| --- | --- | --- |
| 前端数据层 | `apps/web/lib/api.ts` | 加 `ApiError`、`createProject`、内容写回、任务改派 |
| 前端上下文 | `apps/web/lib/workspace.tsx` | 持久化项目选择、订阅事件刷新 |
| 前端壳层 | `apps/web/components/shell.tsx` | 新建项目入口、切换器常在、在线状态 |
| 前端组件 | `apps/web/components/ui.tsx` | 骨架、确认弹窗 |
| 前端页面 | `app/page.tsx`、`app/tasks`、`app/pack`、`app/review`、`app/handoffs`、`app/kb`、`app/graph`、`app/runs`、`app/settings`、`app/documents`、`app/drive`、`app/delivery`、`app/artifacts` | 按 §5 各阶段工作项 |
| 后端存储 | `apps/api/app/store.py` | offline 扫描、租约回收、事件广播调用 |
| 后端路由 | `apps/api/app/main.py` | 广播接线、内容写入端点（如需） |
| Agent 运行时 | `apps/agent/agentd.py`、`machine_service.py`、`runner.py` | keygen、worker-run、执行适配 |
| 脚本 | `scripts/connect-agent.ps1`、`scripts/demo-1.0.ps1` | 新增 |
| 文档 | `README.md`、`docs/IMPLEMENTATION_STATUS.md`、`docs/handoffs/UX_*` | 按 §6.6 与新手指引更新 |