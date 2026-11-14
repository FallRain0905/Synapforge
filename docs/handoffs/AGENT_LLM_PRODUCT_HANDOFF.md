# 云端 Agent 与平台代理模型选择交接

> 日期：2026-10-02
>
> 状态：**代码完成，未部署、未 push**
>
> 范围：任务执行体和「我的智能体」选择云端 Agent，并让云端执行体通过平台 LLM 代理使用可用模型。

## 1. 用户可见结果

- `/my-agent` 和 `/tasks` 共用执行体/模型选择器，能区分本地模型与平台渠道模型。
- 平台模型来自当前成员组织的已启用渠道；没有可用渠道时显示为空，不伪造本地模型，也不静默回落。
- 云端 Agent 没有当前项目的 `llm.invoke` 能力、授权过期或授权撤销时，界面显示不可用/需要重新授权。
- 任务策略保存执行体、模型和相关资源策略；历史执行仍能显示实际模型摘要。

## 2. 代码落点

| 区域 | 文件 | 责任 |
| --- | --- | --- |
| Agent 目录 | `apps/api/app/agent_chat.py` | 组合 Agent 在线状态、能力摘要、本地模型和平台代理模型；不返回 grant token |
| Provider setup | `apps/api/app/main.py`、`apps/api/app/agent_chat.py` | `/api/agents/{agent_id}/llm-setup/{project_id}`；执行体能力令牌校验、归属校验和模型目录 |
| 契约 | `apps/api/app/contracts.py` | `MyAgentEndpoint`、`MyAgentPlatformModel`、`MyAgentExecutorSetup` |
| 执行体 | `apps/agent/chat_loop.py`、`apps/agent/opencode_executor.py` | 按项目缓存 provider setup，写入项目隔离的 opencode provider 配置，使用 `synapforge/<channel-model>` |
| 前端选择 | `apps/web/components/agent-executor-selector.tsx`、`apps/web/app/my-agent/page.tsx`、`apps/web/app/tasks/page.tsx` | 云端/本地 Agent、项目授权、LLM 来源和模型选择 |
| 前端 API | `apps/web/lib/api.ts` | Agent、平台模型、额度、provider setup 客户端类型和方法 |

## 3. 安全不变量

1. 浏览器永远不接触项目能力 token；token 只由执行体沿既有能力链路消费。
2. provider setup 响应只返回执行体已经持有、用于下一步平台代理调用的配置材料；平台不保存明文 grant token。
3. 普通事件、日志、错误和前端状态不能写入 token、上游 key 或完整 Authorization 头。
4. 代理入口要求 `llm.invoke`，并验证 agent 归属、项目授权、过期和撤销状态。
5. 模型只能来自平台返回的可用目录，不能由前端自行拼接渠道模型名。
6. 平台渠道失败不会静默回落到成员自配 key；失败要显示稳定错误。

## 4. 验证证据

- `PYTHONPATH='apps/api;.' python -m unittest test_agent_llm_proxy test_platform_contracts`：通过；本轮聚焦集合共 38 项（含 Copilot 和迁移契约）。
- Agent 测试：489 项通过，跳过 12 项。
- `python -m compileall -q apps/api/app apps/agent`：通过。
- `npm run build`：通过，28 条路由生成。
- 完整 API 回归：1010 项运行，只有既有 `test_latex_compile` 的 2 项因本机缺少 LaTeX 编译工具失败，其余通过。

## 5. 交接边界

- 本轮没有部署云端执行体，也没有执行真实生产渠道调用。
- 既有 `llm_channels`、Agent LLM proxy、额度流水、receipt/fencing 和能力令牌协议被复用，没有新增第二套权限协议。
- 授权过期后的重新签发和执行体重新 configure 仍是运维动作；产品页面应明确提示，不应尝试在浏览器显示 token。
