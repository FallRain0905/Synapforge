# P4-05 浏览器工作区交接文档

> 日期：2026-09-14
>
> 状态：`PASS_WITH_ASSUMPTIONS`
>
> 任务：浏览器端到端验收准备、审核中心真实 API 联调前置检查、阶段 4 开发版退出准备

## 1. 本轮目标

本轮承接 P4-04 的真实基础设施验收准备，先把 Web 工作区整理到可以进行稳定浏览器验收的状态。重点是消除示例数据造成的误导、建立自动化定位契约，并修复移动端导航状态问题。

## 2. 已完成

### 2.1 前端数据驱动

修改文件：`apps/web/components/dashboard-shell.tsx`

- 项目总览侧栏数量使用当前项目列表数量。
- 任务与流程数量统计 `READY`、`WAITING_REVIEW`、`BLOCKED` 和 `NEEDS_REVISION` 任务，表示需要队伍关注的任务。
- 交接中心数量统计当前项目中状态不是 `PASS` 的交接。
- 审核门禁使用真实审核中心中状态不是 `PASSED` 的 Gate 作为待处理指示。
- 在线 Agent 数量仅统计 `online` 状态；最近同步时间取项目 Agent 中最新的 `last_seen`。
- 活跃任务指标的辅助文案改为实际在线 Agent 数量，不再显示固定的示例数字。

上述数量属于前端派生展示值，不改变服务端的任务、交接或 Gate 业务判定。

### 2.2 浏览器定位契约

已增加以下 `data-testid`：

| 定位点 | 用途 |
| --- | --- |
| `project-overview` | 项目总览根区域 |
| `task-workflow` | 任务流面板 |
| `task-list` | 任务列表 |
| `review-center` | 审核门禁面板 |
| `risk-register` | 风险登记面板 |
| `handoff-receipts` | Fanout 交接收据面板 |
| `nav-overview` | 项目总览导航 |
| `nav-tasks` | 任务流导航 |
| `nav-handoffs` | 交接中心导航 |
| `nav-artifacts` | 成果物导航 |
| `nav-reviews` | 审核门禁导航 |

这些定位点是浏览器验收的 UI 契约，后续测试不应依赖易变的中文文案或 CSS 类名。

### 2.3 移动端导航

- 侧栏所有内部锚点点击都调用 `onClose`。
- 移动端点击导航项后，侧栏抽屉会自动收起。
- 点击页面遮罩关闭侧栏的既有行为保留。
- 桌面端不改变现有布局和导航语义。

## 3. 已验证内容

执行命令：

```text
cd apps/web
npm run build
```

结果：Next.js 15 生产构建通过，TypeScript 类型检查通过，静态页面生成通过。

本轮启动了本地 Web 开发服务并完成浏览器冒烟：默认视口下 11 个关键定位点均唯一存在；移动视口下导航抽屉可以打开，点击 `nav-tasks` 后抽屉收起并跳转到 `#tasks`。当前 8000 端口已有开发 API，健康检查通过，但 `/api/projects` 返回空列表，所以页面只进入空项目状态，没有形成带 Gate、Risk、Evidence 和 Fanout 数据的真实 API 联调。未执行 Playwright 测试套件或其他持久化浏览器自动化，因此不能把本次冒烟解释为端到端验收通过。

本轮尝试使用 `scripts/start-api.ps1` 启动 API 时，工作区 Python 的 Uvicorn reload 子进程遇到 `_cffi_backend` DLL 加载拒绝；随后发现 8000 端口已有此前运行的开发 API，因此复用了该服务完成健康检查和空项目页面冒烟。下一轮应先统一 API 启动器的 CFFI 预加载方式，并确认使用的是预期开发数据库。

## 4. 未完成事项

- 未完成有数据的真实 API 联调，审核中心返回的 Gate、Review、Evidence、Risk 和 Handoff 收据仍需在运行服务中验证；本轮只确认健康检查和空项目响应。
- 未在真实 PostgreSQL/RLS/MinIO 环境下打开浏览器验收。
- 尚未新增 Playwright 浏览器测试脚手架和 CI 入口。
- 尚未完成桌面和移动端截图、全断点及空状态视觉回归；移动端菜单收起已完成一次浏览器冒烟验证。
- `scripts/start-api.ps1` 在当前 bundled Python 的 reload 路径仍存在 CFFI 加载问题，尚未修复或纳入启动脚本验收。
- 侧栏中的项目名称和工作区副标题仍来自当前前端展示约定，尚未接入完整 Organization/Team 查询。
- P4-04 的真实基础设施测试仍受本机没有 PostgreSQL、MinIO、Docker 或 Podman 影响。

## 5. 下一步

1. 提供可用的 API、Web、PostgreSQL 和 MinIO 服务，执行迁移 001 至 013。
2. 建立浏览器测试入口，覆盖项目加载、审核中心数据渲染、风险分配/关闭/重新打开、Fanout 收据展示和移动端导航收起。
3. 在浏览器测试中验证 API 错误、空数据、Gate 失效和服务断线状态。
4. 完成桌面/移动端视觉回归后，再进行阶段 4 开发版退出评审。
5. 真实验收通过前，阶段 4 继续保持 `PASS_WITH_ASSUMPTIONS`，不能标记为生产完成。

## 6. 交接输入

- 当前前端入口：`apps/web/components/dashboard-shell.tsx`
- API 类型和请求封装：`apps/web/lib/api.ts`
- 前端样式：`apps/web/app/globals.css`
- 阶段主计划：`docs/PROJECT_EXECUTION_PLAN.md`
- 实施状态：`docs/IMPLEMENTATION_STATUS.md`
- P4-04 基础设施验收准备：`docs/handoffs/P4_04_REAL_INFRASTRUCTURE_ACCEPTANCE_PREP_HANDOFF.md`

## 7. 结论

P4-05 的前端验收准备工作已完成，构建链路通过，数据展示和浏览器定位契约已经具备。真实服务联调、浏览器自动化、视觉回归和生产基础设施验收尚未完成，下一位执行者应从 `P4-05-RUN` 开始，而不是重复实现前端准备工作。
