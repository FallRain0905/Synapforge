# 六 Hub 导航与旧路由交接

> 日期：2026-10-02
>
> 状态：**代码完成，未部署、未 push**
>
> 目标：把一级入口收敛为六个 Hub，同时保持旧路由、深链接、权限边界和原有页面能力。

## 1. 一级入口

```text
/my-agent   智能体
/workspace  项目
/tasks      任务
/documents  成果
/workflows  工作流
/settings   设置
```

`apps/web/lib/nav.ts` 是导航、侧栏、移动端标签、Hub Tab、QuickJump 和页面元数据的单一来源。侧栏只渲染六个 Hub；旧页保留在搜索和深链映射中。

## 2. Hub 能力映射

| Hub | 保留能力 |
| --- | --- |
| 智能体 | 对话、会话、角色、模型、云端/本地执行体选择；`/devices` 作为接入入口，`/ask` 继续 replace |
| 项目 | 原首页总览、项目选择、聊天、团队、任务、生产和成果 Tab；根 `/` 映射到 `/workspace?tab=overview` |
| 任务 | 任务、我的任务、审核、交接和执行方式；跨项目过滤与旧深链保留 |
| 成果 | 文档版本、协作编辑、评论、快照、关系、成果物和个人云盘导入/管理 |
| 工作流 | 工作流包、Copilot、编辑器、preview/dry-run、运行、推进、流程图、交付、知识库和图谱 |
| 设置 | 常规 AI 配置、团队成员、LLM 渠道、账号和额度；渠道管理员权限仍由原服务端接口决定 |

## 3. 旧路由策略

旧路径只承担兼容入口，使用 `LegacyRedirect` 的 `router.replace`，并把原 query 追加到 Hub Tab：

```text
/artifacts → /documents?tab=/artifacts
/drive     → /documents?tab=/drive
/team      → /settings?tab=/team
/channels  → /settings?tab=/channels
/account   → /settings?tab=/account
/pack      → /workflows?tab=/pack
/delivery  → /workflows?tab=/delivery
/kb        → /workflows?tab=/kb
/graph     → /workflows?tab=/graph
```

原页面实现保存在同目录 `content.tsx`，由 Hub 通过动态导入嵌入；旧 `page.tsx` 是兼容跳转壳，避免 Hub 内部再次跳转形成循环。`/runs`、`/timeline`、`/review`、`/handoffs`、`/my-tasks` 等高频深页继续作为独立页面访问。

## 4. 交互与权限不变量

- `router.replace` 不制造返回死循环；query 参数继续可分享。
- 桌面侧栏最多出现六个一级入口；移动端由同一 `nav.ts` 派生四个高频 Tab 和「更多」抽屉。
- Hub 内的二级页面保持自身权限请求和错误展示，例如非管理员打开渠道仍得到明确 403/不可用提示。
- 页面根继续使用 `PAGE_GRID`/`FILL_COLUMN`，不以 grid stretch 把空白分摊到所有卡片。
- 旧页面能力通过内容组件保留，不能以只显示“页面已迁移”的空壳替代真实功能。

## 5. 代码落点

- `apps/web/lib/nav.ts`：六 Hub、子项、搜索、面包屑和移动端来源。
- `apps/web/components/shell.tsx`：桌面侧栏只渲染 primary Hub。
- `apps/web/components/mobile-tabs.tsx`：移动端 Tab 从导航配置派生。
- `apps/web/components/hub-tabs.tsx`、`hub-view.tsx`：Hub 子视图和 URL Tab 同步。
- `apps/web/components/legacy-redirect.tsx`：兼容路由 replace 和 query 保留。
- `apps/web/app/documents/page.tsx`：文档/成果物/云盘。
- `apps/web/app/workflows/page.tsx`：工作流/Copilot/运行/流程图及二级内容。
- `apps/web/app/settings/page.tsx`：常规/团队/渠道/账号及权限分支。
- `apps/web/components/overview/project-overview.tsx`：原首页总览内容。

## 6. 验证证据与限制

- `npm run build`：通过，28 条路由生成；旧路由仍在构建清单中。
- 后端聚焦测试：38 项通过；完整 API 回归 1010 项中仅本机缺少 LaTeX 编译工具导致 2 项既有失败。
- 尚未完成浏览器黑盒验收，因此移动端更多抽屉、各旧路由 query 保留、管理员渠道 403 和 Copilot 选择卡仍应在发布前实机验证。
- 本轮没有部署或 push。
