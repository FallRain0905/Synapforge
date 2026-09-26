# LLM 渠道与全员免费额度 交接

> 交接状态：`PASS`
>
> 日期：2026-09-26
>
> 来源：用户需求——"管理员在后台录入 OpenAI 兼容的上游「渠道」，所有成员通过平台代理端点消费平台提供的免费 LLM 额度，要包含渠道检测与渠道测速"
>
> 前置：`docs/FILE_MANAGEMENT_AND_AGENT_DRIVE_EXECUTION_PLAN.md`（FM 系列的写法与分期口径）

---

## 1. 本阶段目标

把"平台自带上游"变成可运营的能力，而不是继续让每个成员各配一份密钥：

1. **管理员录入渠道**：OpenAI 兼容上游（base_url + api_key + 模型列表 + 优先级 + 启用开关）；
2. **全员免费额度**：成员不配任何密钥，用平台代理端点即可调用；按 token 真实扣减；
3. **渠道检测与测速**：管理员能在页面上验证"这条渠道现在到底通不通、有多快"；
4. **密钥不出库**：api_key 只落库，任何响应只回末 4 位提示。

退出条件（本阶段实际执行的验收）：

- 管理员能建渠道、检测、测速，结果如实回填；
- 成员用平台额度调通一次真实对话；
- 额度按 token 真实扣减，耗尽后拒绝且拒绝原因明确；
- 全量测试通过 + 前端构建通过 + 脱敏扫描通过。

## 2. 实际完成内容

| 工作项 | 状态 | 实际做法 / 关键改动位置 |
| --- | --- | --- |
| 数据迁移 033 | 完成 | `apps/api/migrations/033_llm_channels.sql`：三张表（channels / member_quotas / usage_log）+ RLS，policy 与既有迁移同口径 `app.current_organization_id()` |
| SQLite 镜像建表 | 完成 | `apps/api/app/llm_channels.py:98` `ensure_schema`（带 `_schema_ready` 守卫：生产 Postgres 上不探测 `sqlite_master`） |
| 渠道 CRUD | 完成 | `llm_channels.py` `create_channel` / `update_channel` / `delete_channel` / `list_channels` / `get_channel`；**api_key 语义**：缺省或 `null` = 不变、空串 = 清空 |
| 密钥脱敏 | 完成 | `_channel_view`（`llm_channels.py:171`）：响应只含 `key_hint`（末 4 位），永不含 `api_key`；测试有专项断言 |
| 渠道检测 | 完成 | `check_channel`：发一次 `max_tokens=1, temperature=0` 的真实最小请求，成功与失败**都**回填 `last_check_*` |
| 渠道测速 | 完成 | `speed_test_channel`：默认 3 轮、上限 10 轮，逐轮报 `ok/latency_ms/detail`，汇总 `ok_rounds/avg/min/max`，失败轮不冒充通过 |
| 模型路由 | 完成 | `resolve_channel`：启用 + 模型命中（大小写不敏感），`priority` 小者优先；找不到 → `llm_channel_no_route`（**不猜、不回落**） |
| 成员额度 | 完成 | `get_quota`（懒创建，默认读 `PLATFORM_LLM_FREE_TOKENS_PER_MEMBER`，兜底 200000）/ `set_quota`（**负数 = 不限量**） |
| 代理端点（非流式） | 完成 | `proxy_chat_completions`：额度闸门 → 选渠 → 转发 → 按上游 usage 扣减 → 记流水；上游不给 usage 时按字符数粗估（约 4 字符 = 1 token） |
| 代理端点（流式） | 完成 | `proxy_chat_completions_stream`：SSE 逐行透传，从最后一个带 usage 的 chunk 取用量，流结束后扣减；上游中途断连也如实记 error |
| 错误族 | 完成 | `LlmChannelError(code, detail)` + `ERROR_STATUS` 映射：`not_found / no_route / name_taken / base_url_invalid / models_required / quota_exceeded / upstream_error / upstream_unreachable` 等 |
| 出站 HTTP | 完成 | 一律 `urllib`（与 `apps/api/app/ai_probe.py` 同一先例，**未引入 httpx**） |
| 管理员路由 | 完成 | `main.py`：`GET/POST /api/admin/llm-channels`、`GET/PATCH/DELETE /{id}`、`POST /{id}/check`、`POST /{id}/speed-test`、`GET /api/admin/llm-usage`、`GET/PUT /api/admin/llm-quotas/{member_id}` |
| 成员路由 | 完成 | `GET /api/llm/quota`、`GET /api/llm/v1/models`、`POST /api/llm/v1/chat/completions`（`stream=true` 走 `StreamingResponse`） |
| 契约模型 | 完成 | `apps/api/app/contracts.py`：`LlmChannelCreate` / `LlmChannelUpdate` / `LlmQuotaSet` |
| 平台侧测试 | 完成 | `apps/api/test_llm_channels.py`：29 项，覆盖越权、密钥泄漏、路由四态、额度、上游故障、检测测速 |
| 迁移清单同步 | 完成 | `apps/api/test_platform_contracts.py` 迁移清单加入 `033_llm_channels.sql` |
| 前端渠道页 | 完成 | `apps/web/app/channels/page.tsx`：渠道列表（含最近检测状态/延迟）、新建与编辑弹窗（key 只显示末 4 位、留空即不变）、单条检测/测速并就地展示结果、用量总览按成员聚合并可调额度；**非管理员给出明确提示而非空白** |
| 前端 API 客户端 | 完成 | `apps/web/lib/api.ts`：9 个函数 + 类型 + 错误码→人话映射 |
| 导航注册 | 完成 | `apps/web/lib/nav.ts`：`/channels` 挂在「团队与空间」组（`shell.tsx` **未改动**——另一会话在改它） |
| 端到端验收脚本 | 完成 | `scripts/verify_llm_channels_e2e.py` + `scripts/_llm_e2e_launcher.py`：真 uvicorn 进程 + 本机假上游，临时库隔离 |

## 3. 与计划的偏差

**无偏差，但有四处需要接手者知道的设计决定**（都是为了不引入隐性风险）：

1. **RLS 用严格口径，未沿用废稿的宽松版。**
   本轮开工时仓库里留有一份未提交的 `033` 废稿，其 policy 写作
   `USING (organization_id = '' OR organization_id = app.current_organization_id())`。
   查过 029/030/031/032 全部既有迁移，**没有一例**这种宽松写法，因此改回
   `organization_id = app.current_organization_id()`，并由 `create_channel` 写入 actor 的真实组织。

2. **渠道行的 organization_id 来自 actor，不来自 Store 属性。**
   废稿用 `getattr(store, "default_organization_id", "")`，但 `Store` **没有**这个属性，
   照抄会让所有渠道都落在空组织（在严格 RLS 下等于全员看不见）。
   现在全部走 `str(actor.organization_id)`。

3. **api_key 语义按"缺省/null = 不变"实现，前端配合。**
   前端的编辑弹窗里 key 输入框**留空即不变**：留空时不把 `api_key` 放进 PATCH body，
   避免把已有密钥误清空。这是"编辑渠道"最容易出错的地方，已由
   `test_update_without_api_key_keeps_it` 双向锁定（不传=保留、空串=清空）。

4. **额度耗尽那次调用不记流水。**
   额度闸门在**打上游之前**执行（避免白花上游额度），因此被拒的调用不产生
   `llm_usage_log` 行。端到端验收里 `用量总览=3 次`（非流式 + 流式 + 恢复不限量后）
   正是这个设计的直接结果，不是漏记。

## 4. 测试与验证

```text
# 平台侧（含本次新增 29 项）
cd apps/api && PYTHONPATH=".;../.." python -X utf8 -m unittest discover -s . -p "test_*.py"
→ Ran 727 tests，FAILED (failures=2, skipped=15)
  两个失败均为 test_latex_compile 的既有环境失败（本机没有 xelatex/pdflatex/latexmk），与本次改动无关。

# 执行体侧
cd apps/agent && python -X utf8 -m unittest discover -s . -p "test_*.py"
→ Ran 473 tests，OK (skipped=12)

# 前端
cd apps/web && npx tsc --noEmit   → 无输出（通过）
cd apps/web && npm run build      → 成功，26 条路由（含新增 /channels），基线要求 ≥19

# 端到端（真服务 + 本机假上游，临时库隔离，凭据全为假值）
python -X utf8 scripts/verify_llm_channels_e2e.py
→ 端到端验收全部通过（34 项断言）
```

端到端覆盖的关键链路：注册首个账号成为管理员 → 非管理员访问渠道 403 →
建渠道（假 key）→ 响应无明文密钥且 `key_hint` 正确 → 检测回填成功与延迟 →
测速 3 轮全通过且上游确收 3 次 → 成员读可用模型与默认额度（300，来自环境变量）→
非流式对话按 usage 扣 165 → 流式按最后一个 usage 扣 100（累计 265）→
未配置模型 404 且不打上游 → 额度压到 265 后调用 429 且不打上游 → 设为 -1 恢复可调 →
用量总览 3 次/430 token 且按成员聚合 → 上游 500 时 502 并如实标 error。

## 5. 尚未完成与边界

**现在还不能做什么**（用户视角）：

- **没有"渠道健康自动巡检"**：检测/测速都要管理员手动点。想让平台定时自检并告警，需要再加调度（本阶段没做）。
- **额度只有"按 token 总量"一档**：没有按项目/按天/按模型的限额，也没有用量告警阈值。
- **额度是"事后扣减"**：单次请求可能略微超出剩余额度（先放行再按实际 usage 扣）。
  要硬性不超额，得改成预估预扣 + 结算退差。
- **不支持非 chat 端点**：只代理 `/v1/chat/completions`（含流式）；embeddings / 图像等还没走平台渠道。
- **成员侧没有独立 UI**：成员只能用 `GET /api/llm/quota` 和 OpenAI 兼容端点；
  「我的智能体」尚未接入平台渠道（仍走成员自配凭据）。接入需要另开一期。
- **渠道删除是硬删**：历史流水保留但 `channel_id` 变成悬空引用（页面展示为已删渠道口径）。

**运维注意**：

- 成员默认额度读环境变量 `PLATFORM_LLM_FREE_TOKENS_PER_MEMBER`（默认 200000），改了要重启服务。
- 生产 Postgres 走 `migrations/033_llm_channels.sql`；SQLite 侧由启动时的 `ensure_schema` 镜像。
- 渠道表**没有**对 `models` 建索引（JSON 文本，按启用集合在 Python 里匹配）；渠道数量级很小，无需优化。
