# UX-7 打磨与 Demo 1.0 验收 交接

> 交接状态：`PASS_WITH_ASSUMPTIONS`
>
> 日期：2026-09-16
>
> 对应阶段：UX-7（工作项定义见 `docs/DEMO_1_0_IMPLEMENTATION_PLAN.md` §5 UX-7）
>
> 上一份交接：`docs/handoffs/UX_6_COLLABORATION_CORRECTNESS_HANDOFF.md`
>
> **本阶段是 Demo 1.0 主线的最后一个阶段。**

---

## 1. 本阶段目标

从计划书 §5 UX-7 抄写：

> **目标**：补齐一致性细节，并产出可重复演示的验收脚本。
>
> **入口条件**：UX-1 至 UX-6 全部退出。
>
> **退出条件**：§2.1 的 4 条目标全部有可出示的证据。

## 2. 实际完成内容

| 工作项 | 状态 | 实际做法 / 关键改动位置 |
| --- | --- | --- |
| UX-7-01 加载态全覆盖 | 完成 | 用 `WorkspaceProvider` 的 `loading` 给 6 个页面（`/artifacts`、`/handoffs`、`/review`、`/runs`、`/timeline`、`/tasks`）的列表加 `LoadingSkeleton`，不再把"数据未到"显示成"暂无" |
| UX-7-02 死代码处置 | 完成 | **删除** `importCumcmWorkspace`、`getDocumentLayers`、`getDocumentEvidence`（零调用；后两者与文档页已有的时间线/证据展示冗余；CUMCM 导入属运维能力，端点用法留在调试附录）；**接线** `getAiSettings`/`saveAiSettings`（设置页不再自己裸 fetch，错误也走统一 `errorMessage`） |
| UX-7-03 测试连接 | 完成 | 新增 `apps/api/app/ai_probe.py`（LLM 打 `/chat/completions` max_tokens=1；Embedding 打 `/embeddings` 并**回报实际维度、维度与设置不符时点出来**；MinerU 只报"已配置"并明说没有免费探活接口，不假装测过）与 `POST /api/settings/ai/test`；设置页加「测试连接」按钮与逐项结果面板。新增 9 项契约测试（打桩网络，覆盖成功/HTTP 错误/网络不可达/维度不一致/单组件失败不影响其它） |
| UX-7-04 一致性打磨 | 完成 | 答辩提纲加「下载」（Blob 下载 `答辩提纲.md`）；`/graph` 的「实体管理/关系管理」改名为「实体浏览/关系浏览」并在提示里写明"只读（写入 capability 未开放）"，同步 `/graph` 页副标题（决策 D12/P-3） |
| UX-7-05 演示脚本 | 完成 | 新增 `scripts/demo-1.0.ps1`（UTF-8 BOM）：自动完成可 API 化的步骤并逐项断言，界面步骤打印检查点后回查平台状态；`-AutoApprove` 供无人值守自检。**实测 17/17 PASS** |
| UX-7-06 验收清单 | 完成 | 新增 `docs/DEMO_1_0_ACCEPTANCE.md`：把 §2.1 四条目标逐条映射到自动化断言与实测证据，并列出"尚未达成"的诚实清单 |
| UX-7-07 文档收尾 | 完成 | README 新增「最快上手（推荐）」：一键启动 + 上手后的前 15 分钟六步 + 一键自检命令 + 检索服务独立进程说明；原有分步说明降级为「本地启动（分步）」保留 |

**验收中修掉的三个真缺陷（都在 UX-7 引入或暴露）**：

1. **`PUT /api/settings/ai` 被路由覆盖**：新增 `POST /api/settings/ai/test` 时插在了 PUT 的装饰器与原函数之间，导致 PUT 路由指向探针函数——**保存凭据静默失效**（响应还返回探针结果）。已恢复正确顺序。
2. **凭据明文回显**：`PUT /api/settings/ai` 的响应把 API Key 原样返回（GET 是掩码）。现抽出 `_mask_ai_settings()` 供 GET/PUT 共用，并改掉了一条**把明文回显当预期行为固定下来**的旧测试（`assertEqual(saved["llm_api_key"], "sk-test")` → 断言掩码且响应中不含明文）。
3. **演示脚本自身的三处问题**：断言用"管道表达式直接当函数参数"在 PS 5.1 下不稳定（改为显式变量 + `@()` + 输出实际计数）；catch-all 只打印"已存在，跳过"掩盖真实报错（改为打印原因）；用 `-InFile` 发原始体打 multipart 端点会 422（改由 python 发 multipart）。平台侧在这些失败中始终是正确的。

## 3. 与计划的偏差

**有偏差，共 2 处：**

1. **计划写"用 G6 全量渲染替换 SVG"**（§2.2 非目标中列为不做）——本阶段同样未做，`/graph` 保持自研 SVG。这是**遵守**非目标，此处记录以免后来者误以为遗漏。
2. **UX-7-03 需要新增后端端点**（计划只写"设置页加测试连接"）。理由：浏览器不能持有密钥直连模型，探针必须在服务端跑；新增 `ai_probe.py` + 一个端点，属于必要实现而非范围扩张。

## 4. 测试与验证

**基线命令（计划书 §7）**

```text
后端：python -X utf8 -m unittest discover -s . -p "test_*.py"（apps/api）
      → Ran 291 tests, OK (skipped=13)   （UX-6 为 282 → 新增 9 项探针测试）
Agent：apps/agent 全量 → Ran 164 tests, OK (skipped=9)
前端：cd apps/web && npm run build → 17 个页面静态预渲染；npx tsc --noEmit 无错误
```

**Demo 1.0 一键验收（`scripts/demo-1.0.ps1 -AutoApprove`，独立临时库）**

```text
全部 17 项通过：Demo 1.0 全链路可用。
```

**浏览器侧补充验收**

| 项 | 证据 |
| --- | --- |
| 测试连接（未配置） | 三项均报"未配置完整/未配置 Token"，不谎报可用 |
| 测试连接（成功路径） | 起一个 OpenAI 兼容桩服务（`/chat/completions` + `/embeddings`）并真实保存凭据后，界面显示 LLM「模型 stub-chat 可用」、Embedding「可用，维度 1024」、MinerU「Token 已配置但没有免费探活接口」 |
| 凭据脱敏 | `PUT` 与 `GET` 响应均只回显 `configured`（实测两个接口的 `*_api_key` 字段） |
| 加载骨架 | 6 个页面的服务端渲染 HTML 均含 `loading-skeleton`，4 个页面不再出现误导性空状态 |

## 5. 尚未完成与边界

完整清单在 `docs/DEMO_1_0_ACCEPTANCE.md` §3，这里列最影响演示叙事的几条：

- **Agent 不产出正式成果物**（worker 只上报 summary + Run 台账）。
- **协作编辑是单机草稿模型**（并发保存后写覆盖先写）。
- **长任务会被租约回收**（worker 无 lease 心跳）。
- **Codex/Claude 作为执行体未实测**：本机 `codex`/`claude` 均未安装（平台探针返回 `NOT_INSTALLED`），通道已备好（`resource_policy.worker_command`）。
- **离线感知最坏 ~100 秒**；**连接状态在客户端被强杀时不收敛**。
- **`Ctrl+C` 是 worker 唯一的停机方式**（紧急停止未接进循环）。
- **计划书 §10 的三项待拍板**（自定义模板、老轨端点、`/graph` 命名）已按默认值执行：前两项仍未做，第三项已按"改名"完成。

## 6. 下一步（Demo 1.1 候选）

按计划书 §2.2 的非目标与上面的边界，以下是自然的下一批：

1. **成果物上传接进 worker 循环**（`ResultUploader`/`AgentArtifactClient`）——让 Agent 真正产出 `result_table` 等正式成果物；
2. **worker 租约心跳 + 紧急停止**——支撑长任务与可控停机；
3. **任务表单暴露执行命令**（`resource_policy.worker_command`）——让"界面建任务 → Agent 执行"完全自助；
4. **自定义模板**（计划书 P-1，Demo 1.1 的候选主线）；
5. **连接状态收敛**（维护扫描回收"所属 Agent 已离线"的连接）。

## 7. 复现命令

```powershell
# 1) 一键验收
.\scripts\start-demo.ps1                        # 终端 A
.\scripts\demo-1.0.ps1                          # 终端 B（界面步骤需配合浏览器）
.\scripts\demo-1.0.ps1 -Api http://127.0.0.1:8010 -AutoApprove   # 无人值守自检

# 2) 基线回归
cd apps\api;  $env:PYTHONPATH = "$(Resolve-Path '..\..');$(Resolve-Path '.')"
python -X utf8 -m unittest discover -s . -p "test_*.py"      # 291 tests, OK (skipped=13)
cd ..\agent; python -X utf8 -m unittest discover -s . -p "test_*.py"   # 164 tests, OK (skipped=9)
cd ..\web;   npm run build                                    # 17 个页面
```