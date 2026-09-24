# MY-AGENT S-4 / W12 本地交接（2026-09-24）

## 本轮交付与边界

已实现推理事件与正文分离、每轮历史恢复、默认收起的「思考过程」、强度设置贯通会话/轮次/执行命令、移动会话抽屉、阅读时暂停自动跟底、停止状态文案，以及首页、工作区、任务、审核的层级优化。保留既有 S-3 审批协议、附件和结构化选择卡；未新增审批或推理系统，未改 Agent Protocol/Gateway 逐帧语义，未删除旧路由，未引入 UI 框架或升级依赖，未实施文件管理系统，未部署线上、提交或推送。工作树存在并行文件管理等未提交改动，本交接仅覆盖 S-4/W12。

## 关键位置与事件契约

- `apps/web/app/my-agent/page.tsx`：`answerSegments` 仅合并 `agent.message` 和 `delta` 正文；`thinkingFrom` 仅合并真实 `thinking` 事件；`ThinkingDisclosure` 按轮次默认收起。事件/审批缓存以 turn id 为键，历史按四轮一批补载；切换会话的旧请求不覆盖新会话，活动轮询不重叠，结束状态无条件补拉最终事件和审批。
- `apps/agent/opencode_server.py`、`apps/agent/executor_events.py`：现有 serve SSE 使用 `part.id → part.type` 判断 reasoning 与 text；`field=text` 本身不足以分辨。CLI 未产生真实 reasoning 时页面不会伪造。API 将 `thinking` 与 `delta` 按事件序列持久化于 `agent_turn_events`，刷新后按轮重放；未增加轮次子表，现有删除顺序仍是审批、事件、轮次、会话。
- `apps/api/app/contracts.py`、`apps/api/app/agent_chat.py`、`apps/api/migrations/028_agent_chat_variants.sql`：会话接受空串、`minimal`、`high`、`max` 强度；建轮次时冻结为轮次字段，领取轮次时交付执行体。PostgreSQL 迁移新增两个列；仅通过 SQL 契约检查，未在真实 PostgreSQL 实例执行迁移。
- `apps/agent/chat_loop.py`：非默认强度显式走 CLI 并在命令中传 `--variant`，因为现有 serve 侧 `message.model.variant` 实测静默失效；默认强度维持常驻 serve 真增量流。角色通过既有 `--agent`、模型通过轮次设置抵达执行链；非默认强度页面明确写为 CLI 分段输出。
- `apps/web/components/ui.tsx`、`components/artifact-drawer.tsx`：对话框/抽屉初始焦点、Escape、Tab 焦点约束、焦点返回与 body 锁滚动。`app/my-agent/page.tsx` 与 `app/globals.css`：移动会话抽屉、执行抽屉、跟底控制、已停止状态、触控尺寸与 reduced-motion。
- `apps/web/app/page.tsx`、`app/workspace/page.tsx`、`app/tasks/page.tsx`、`app/review/page.tsx`、`app/globals.css`：当前项目/阶段/下一步/阻塞项的首页主区、工作区可横向滚动标签、任务文字状态轨道、待审核优先层级。浏览器实测工作区窄屏聊天面板曾撑到 1652px，修正 `.workspace-main` 与面板最小宽度，并移除窄屏 `flex: 0 0 auto`；复测 375/390/900/1280/1440px 无页面横向溢出。

## 验证记录

- `cd apps/web && npx tsc --noEmit && npm run build`：最后的工作区 CSS 窄屏修复之后再次通过；Next.js 15.5.25 编译、类型与 26/26 静态页生成通过。停止旧 Web 进程后，`npm run start -- -p 3000` 已启动并显示 `Ready`。
- `cd apps/agent && python -X utf8 -m unittest test_agent_chat.py test_opencode_server.py`：48 项通过，包括 variant CLI 参数与 serve 绕路。
- `cd apps/api && PYTHONPATH='<repo>;<repo>/apps/api;<repo>/.venv/Lib/site-packages' python -X utf8 -m unittest -v test_agent_chat test_http_contracts test_platform_contracts`：35 项通过，包括轮次 variant 冻结/领取、持久化 thinking、删除、HTTP 合同、迁移文件合同。Windows Python 不能读取 `apps/api/vendor/fastapi/__init__.py`（ACL PermissionError），所以使用已有 `.venv/Lib/site-packages`，未更改 ACL/依赖。
- `git -c core.whitespace=cr-at-eol diff --check -- <本轮文件>`：通过；仓库 CRLF 需按行尾处理。
- `python scripts/_w1_ssr.py`：失败，多个页面共同报告 `nav-ask` 缺失，属于现有导航不变量不匹配；不能记作通过。
- 真浏览器：隔离 `scripts.acceptance_empty_api`（临时库）与 3014 Web 创建测试账户/项目，不碰原开发库。在 `/my-agent` 验证 1440/1280/900/390/375px 页面宽度不溢出，375px 会话抽屉开/关、Escape、初始与返回焦点、body 锁滚动，执行细节抽屉同样通过；深色主题切换、无执行体时强度提示从默认真流式变为 `CLI --variant` 分段输出通过。工作区聊天面板宽度修复复测：375px 335、390px 349、900px 579、1280px 589、1440px 688；对应 `documentWidth` 等于视口宽。

## 未完成的验收和环境限制

隔离 API 没有在线/授权的对话执行体，故 20+ 轮真实历史、刷新还原、思考流、审批卡、停止与附件端到端未做真浏览器证明；已有 API/Agent 测试只覆盖相应契约。首页、任务与审核在有项目数据时的明暗/键盘/视觉矩阵尚未完整人工视觉验收；reduced-motion 媒体偏好没有真浏览器模拟结果。没有真实 PostgreSQL 迁移验证。

本地同时运行 `next dev` 和 `next start` 共用 `apps/web/.next`，曾出现开发遮罩 `ENOENT .next/server/app/my-agent/page.js`；现已停止两者、完整重建并只启动生产 Web。并行文件管理代码曾在写入时短暂截断 `apps/web/lib/api.ts`，随后恢复且 TypeScript 通过，未覆盖它。隔离临时实例曾在 8010/3014，临时库位于系统 TEMP，避免在有数据的开发库复现注册/项目创建。

## 恢复与回滚

最终复验：停止共享 `.next` 的 Web 进程，`cd apps/web && npm run build`，只启动 `npm run start -- -p 3000`；用隔离 API/专用 Web 构建重做用户态浏览器矩阵。若要回滚本轮，只按上述文件与 `028_agent_chat_variants.sql` 的具体变更反向恢复；不要对整个脏工作树 `git reset --hard`，并行文件管理改动不属于本轮。PostgreSQL 已应用的迁移须单独评估数据后按数据库迁移流程处理。本轮没有线上部署或 Git 提交。
