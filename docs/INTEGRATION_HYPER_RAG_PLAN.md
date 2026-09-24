# Hyper-RAG / question-bank 集成计划（第一版）

> 日期：2026-09-15
>
> 状态：`PLAN`（待下一轮确认后实施）
>
> 上游项目：`C:\Users\19855\Desktop\Hyper-rag\question-bank`（SynapFlow）与其 `hyper-rag-service/`

## 0. 已确认的决策（本轮用户拍板）

| # | 决策 | 内容 |
| --- | --- | --- |
| D1 | 检索结果**不作为门禁** | 只做知识库检索增强，不进 Review/Gate；理由：可行性不高、无必要 |
| D2 | 云盘 → 知识库链路包含 **MinerU PDF 转 Markdown** | 通过 MinerU API 调用实现，**不自建转换** |
| D3 | 可视化页面同时包含**向量库管理** | 不只是超图探索，还有向量库的查看/重建/清理 |
| D4 | hyper-rag-service **保持服务独立** | 部署为独立 FastAPI 进程（平台代理调用），便于后续单独修改检索管线 |

## 1. 总体架构

```text
Next.js 工作台（math-agent-platform apps/web）
  ├─ /graph        超图可视化 + 向量库管理页（自研，参考 KnowledgeGraph.tsx）
  ├─ /kb           知识库页（列表/文档/索引进度/检索问答）
  └─ /ask          内置 AI 问答页（检索增强 + 普通对话）
        │  fetch
        ▼
平台 FastAPI 控制平面（apps/api）
  ├─ /api/kb/**            知识库 CRUD（复用现有成员/项目授权）
  ├─ /api/kb/{id}/index    文档索引触发（调用 hyper-rag-service）
  ├─ /api/kb/{id}/query    检索问答代理（调用 hyper-rag-service）
  ├─ /api/convert/**       MinerU 转换代理（PDF→Markdown，外部 API）
  └─ /api/ai/chat         内置 AI 对话（OpenAI 兼容接口直连用户配置的模型）
        │  HTTP（服务间，本机/内网）
        ▼
hyper-rag-service（独立部署，端口 8100，不改动其代码）
  ├─ POST /api/sync-batch      索引构建（LLM+Embedding 凭据随请求传入）
  ├─ POST /api/query           5 种模式检索（hyper/naive/...）
  ├─ GET  /api/entities|relationships|entity-names|vertex-neighbor   超图数据
  └─ GET  /api/status|sync-progress                                 状态
```

**服务间协议**：完全沿用 question-bank 的请求形态（`ServiceConfig` 每请求带 LLM/Embedding 凭据）。平台把这些凭据存在成员设置表（新增 `user_settings` 表或复用现有结构），不写死在环境变量。

## 2. 分阶段实施计划

### Phase A：检索管线接入（最小可用）

1. **部署**：`hyper-rag-service` 以 uvicorn 独立进程运行（`scripts/start-demo.ps1` 增加一步，端口 8100）；`hyperrag_cache/kb-{kb_id}/` 挂在平台数据目录下。
2. **平台侧知识库表**（SQLite 开发版 + PG 迁移 015）：
   - `knowledge_bases`（id/project_id/name/description/created_by/created_at）——**挂到项目**（复用项目授权），不是独立顶级实体
   - `kb_documents`（id/kb_id/title/content_md/source_artifact_id/index_status/indexed_at/error）
   - `kb_conversations` / `kb_messages`（检索问答历史，多轮）
3. **平台端点**：
   - `POST /api/projects/{id}/kb`（建库）、`GET .../kb`、`GET .../kb/{kb_id}`
   - `POST .../kb/{kb_id}/documents`（上传 Markdown 文本或从云盘/成果物导入 → 触发 sync-batch）
   - `GET .../kb/{kb_id}/index-status`（代理 sync-progress）
   - `POST .../kb/{kb_id}/query`（代理 /api/query，结果含 text_units/entities/hyperedges；**溯源把 full_doc_id 映射回 kb_documents**，替代 question-bank 的 MD5 反查——我们在建索引时直接记录 hash→doc_id 映射，更稳）
4. **凭据**：成员设置表存 LLM/Embedding/MinerU 三组配置（api_key/base_url/model/dimensions），端点读取后随请求传给服务；无配置时返回明确错误码（`ai_credentials_missing`）。

### Phase B：云盘 → MinerU 转换 → 知识库

1. `POST /api/convert`：接收云盘 file_id（PDF/DOCX），调用 **MinerU API**（用户配置的 token），轮询任务状态直到完成，取回 Markdown 与 ZIP 结果。
2. 转换结果：Markdown 存入 `kb_documents.content_md`（可指定目标知识库），原始 PDF 保留在云盘引用链里；`source_artifact_id` 记录来源。
3. 转换完成即可触发 Phase A 的索引流程——形成「上传 PDF → MinerU → Markdown → Hyper-RAG 索引 → 检索问答」完整链路。
4. 参考实现：question-bank `app/api/agent/files/[id]/convert/route.ts`（轮询/状态判定/ZIP 物化逻辑成熟，可直接移植其**状态机**，仅替换存储层与鉴权）。

### Phase C：超图可视化 + 向量库管理页（/graph）

1. **前端**：新建 `/graph` 页（`nav-graph` 导航入口）：
   - 实体搜索/分页列表（entity-names 端点）
   - 选中实体 → vertex-neighbor 一阶邻居子图（@antv/G6 v5 + graphin，**超边用 bubble-sets 气泡圈**而非连线，按 entity_type 着色，force 聚类布局）
   - 图谱移植自 question-bank `components/KnowledgeGraph.tsx`（313 行，自包含）
2. **向量库管理**（同页第二标签）：
   - 每个知识库的向量统计（实体数/关系数/chunk 数，从 entities/relationships 端点 total 取）
   - **重建索引**（清空该 kb 的 hyperrag_cache 目录 + 重新 sync-batch）——服务端新增一个平台代理端点调服务的删除/重建能力；hyper-rag-service 本身不改，重建由平台侧删缓存目录再重放文档实现
   - 索引状态/进度实时展示（sync-progress 轮询）

### Phase D：内置 AI（/ask + 任务分发增强）——详见第 3 节

## 3. 平台内置 AI 方案（参考 question-bank 的成熟模式）

**结论：可以内置，且成本可控。** question-bank 已经验证了四个可直接借鉴的模式：

| 模式 | question-bank 实现 | 平台移植方案 |
| --- | --- | --- |
| **配置管理** | `user_settings` 表存 LLM/Embedding/MinerU 配置，回退链：用户设置 → 系统设置 → 环境变量 | 成员设置表 + 同样三级回退；`GET/PUT /api/settings/ai` |
| **多轮对话** | `qa_conversations/qa_messages` 表存历史，前端传 history | `kb_conversations/kb_messages` 已列入 Phase A |
| **检索增强问答** | 悬浮 AI 助手（FloatingAIButton，任意页面）+ QA 页（知识库范围 RAG） | `/ask` 页两种模式：普通对话（直连 LLM）+ 知识库 RAG（先检索再生成） |
| **流式响应** | AI 助手 route 用 OpenAI 兼容接口直连（可流式） | `POST /api/ai/chat` 代理用户配置的 OpenAI 兼容端点（DeepSeek/千问等），支持 SSE 流式 |

**新增任务分发/管理的 AI 增强**（初步设想，供下轮细化）：
- `POST /api/projects/{id}/tasks/ai-decompose`：把任务描述发给 LLM，产出子任务建议（标题/阶段/依赖），**只作为建议返回前端供人工确认**，不自动建任务（符合平台“Agent 不做人工决策”的权限原则）
- `POST /api/tasks/{id}/ai-summary`：Agent 运行结果/stdout 喂给 LLM 生成进度摘要，挂在任务上作为辅助信息
- 这两个都不改状态机、不进门禁，与 D1 决策精神一致

## 4. 关键技术决策记录

- **hyper-rag-service 不改代码**（D4）：平台侧用「删缓存目录 + 重放文档」实现重建，用「记录 hash→doc_id 映射」实现溯源，全部是代理层逻辑
- **检索结果不进 Evidence/Gate**（D1）：text_units 仅在 `/ask` 与 KB 问答界面展示来源，不写平台 Evidence 表
- **MinerU 只调 API**（D2）：不自建解析，PDF/DOCX → Markdown 的全部逻辑在平台侧的转换代理端点
- **索引触发模型**：文档入库即触发（自动），失败可从向量库管理页手动重试
- **鉴权**：KB 挂在项目下，复用现有项目 RBAC；AI 端点用成员会话；对 hyper-rag-service 的调用只在平台后端发生（服务不暴露公网）

## 5. 依赖与环境

- hyper-rag-service 运行需要：Python 3.10+、nano-vectordb、tiktoken、openai、hypergraph-db（requirements.txt 已列全）
- 模型凭据：任一 OpenAI 兼容 LLM + Embedding（DeepSeek/千问都行）；MinerU 需要 api token
- 前端新增依赖：`@antv/g6` + `@antv/graphin`（超图可视化必需）

## 6. 已确认的决策（第二轮拍板）

| # | 问题 | 决策 |
| --- | --- | --- |
| Q1 | AI 任务分解入口 | **任务详情页**内嵌触发，不另设页面 |
| Q2 | `/ask` 普通对话 | **需要多会话列表**（会话创建/切换/删除/重命名） |
| Q3 | MinerU 限速 | **队列化**（设计见 6.1） |
| Q4 | 知识库归属 | **双形态**：项目知识库（挂项目）+ 个人知识库（跨项目，可分享给其他成员）；KB 只存文档，与个人云盘（文件暂存 + 200MB 配额）语义分离 |

### 6.1 MinerU 转换队列设计

问题-bank 的做法是同步轮询（请求挂起直到任务完成，`maxDuration = 300` 秒）。缺点：
并发多文件时会占满请求线程、限速时无法排队、超时即丢结果。

平台方案（三张表 + 单工作循环，SQLite 开发版先行）：

```text
convert_jobs:
  id, member_id, source_type (drive_file/upload), source_id,
  file_name, status (queued/running/done/failed),
  result_markdown (完成后), error, attempts, created_at, updated_at

转换循环（平台 FastAPI 内单后台任务）：
  1. 从 convert_jobs 取 queued 且未超并发上限（默认 2）的任务
  2. 置 running → 调 MinerU API 创建任务 → 轮询（间隔 5s，上限 10 分钟）
  3. 完成：取回 Markdown 写 result_markdown，置 done
  4. 失败：attempts+1，<3 次则回 queued（指数退避），≥3 次置 failed
  5. done 的任务由前端领取：写入 kb_documents 并触发索引

端点：
  POST /api/convert              入队（返回 job_id，立即返回）
  GET  /api/convert/{job_id}     查询状态/结果
  POST /api/convert/{job_id}/to-kb  把结果写入指定知识库并触发索引
```

好处：限速天然排队（并发上限）、失败重试不丢任务、前端不挂 300 秒长请求。