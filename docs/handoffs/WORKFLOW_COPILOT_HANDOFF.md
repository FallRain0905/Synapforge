# 工作流 Copilot 交接

> 日期：2026-10-02
>
> 状态：**代码完成，未部署、未 push**
>
> 目标：用户通过多轮对话和结构化选择生成 workflow definition 草稿，服务端校验与预览后，由用户分别确认发布和启动 Run。

## 1. 生命周期

```text
DRAFTING
  → NEEDS_USER_DECISION
  → READY_FOR_PREVIEW
  → PREVIEW_FAILED / READY_FOR_PREVIEW
  → AWAITING_PUBLISH_CONFIRMATION
  → PUBLISHED
  → AWAITING_RUN_CONFIRMATION
  → RUN_STARTED
```

草稿、预览和已发布版本是不同对象。preview 不创建任务、Run 或事件；发布确认和 Run 确认是两个独立动作。

## 2. 后端落点

- `apps/api/app/workflow_copilot.py`：会话、轮次、选择决策、草稿指纹、服务端 choice 校验、围栏解析、最多两轮自动修复、preview、幂等发布和 Run 启动。
- `apps/api/migrations/041_workflow_copilot.sql`：Postgres 表和组织隔离/RLS。
- `apps/api/app/main.py`：Copilot HTTP 路由。写路由均声明 Pydantic Request 模型，并在服务端检查成员、组织和项目权限。
- `apps/api/app/contracts.py`：会话、轮次、choice、草稿、发布和运行请求契约。

主要端点：

```text
GET  /api/workflows/copilot/sessions
POST /api/workflows/copilot/sessions
GET  /api/workflows/copilot/sessions/{id}
POST /api/workflows/copilot/sessions/{id}/turns
POST /api/workflows/copilot/sessions/{id}/choice
PUT  /api/workflows/copilot/sessions/{id}/draft
POST /api/workflows/copilot/sessions/{id}/preview
POST /api/workflows/copilot/sessions/{id}/publish
POST /api/workflows/copilot/sessions/{id}/run
```

服务端只承认 `workflow-draft` 围栏中的 JSON；自然语言不能直接变成已生成定义。校验复用 `workflow_service.validate_definition()`，预览复用 `preview_definition()`，发布和运行复用既有 workflow engine API。

## 3. 前端落点

- `apps/web/components/workflow-copilot.tsx`：工作流 Hub 内的 Copilot 面板；会话列表、恢复、对话轮次、`synapforge-choice` 选择卡、草稿状态、预览输出、发布确认和 Run 确认。
- `apps/web/app/workflows/page.tsx`：主工作流 Tab 挂载 Copilot；既有工作流包、编辑器、运行列表、推进和 `WorkflowFlowView` 保留。
- `apps/web/lib/api.ts`：Copilot 会话、轮次、choice、preview、publish、run 客户端方法和类型。
- `apps/web/lib/agent-choice.ts`、`apps/web/components/agent-response.tsx`：沿用既有 choice 协议和 Markdown 回复渲染，不另建选择协议。

前端确认按钮分别为「预览草稿」「确认发布」「确认启动 Run」。发布和运行前都显示浏览器确认，并把最新修订号交给服务端做 stale 检查。

## 4. 安全与一致性红线

- AI 不能调用 publish 或 run 服务函数；只有带 `confirmed=true` 的 HTTP 动作可以推进高影响状态。
- choice 答案在服务端根据上一轮持久化问题集合校验，未知问题、未知选项和缺少必答项拒绝。
- 同一草稿重复发布使用 hash/修订号幂等，不创建第二个 workflow key。
- Run 启动前服务端再次检查项目 `project.write` 权限。
- token、上游 key、完整 prompt 中的敏感授权信息不进入 Copilot 回执。

## 5. 验证证据

- `PYTHONPATH='apps/api;.' python -m unittest test_workflow_copilot test_agent_llm_proxy test_platform_contracts`：38 项通过。
- 完整 API 回归：1010 项运行，除本机缺少 LaTeX 工具导致的 2 项既有编译失败外通过。
- `npm run build`：通过，28 条路由生成。
- Python `compileall`：通过。

## 6. 后续边界

- Copilot 当前使用平台已有渠道和 `chat_routed()`；模型不可用时返回明确错误，不伪造草稿成功。
- 预览结果是计划和警告，不是执行结果；工作流应用仍会创建真实任务骨架，结果必须经过既有执行、审核和 receipt 链路。
- 尚未做浏览器黑盒实机验收；部署和生产渠道真跑均留给后续发布阶段。
