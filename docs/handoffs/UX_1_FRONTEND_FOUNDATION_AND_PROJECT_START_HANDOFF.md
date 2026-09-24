# UX-1 前端地基与项目起点 交接

> 交接状态：`PASS_WITH_ASSUMPTIONS`
>
> 日期：2026-09-16
>
> 对应阶段：UX-1（工作项定义见 `docs/DEMO_1_0_IMPLEMENTATION_PLAN.md` §5 UX-1）
>
> 上一份交接：`—`（本主线首份）

---

## 1. 本阶段目标

从计划书 §5 UX-1 抄写：

> **目标**：让"新建项目"成为可能，并把后续所有阶段都要复用的前端基础设施一次做对。
>
> **入口条件**：UX-0 退出。
>
> **退出条件**：`POST /api/projects` 在前端可达且被 1 处 UI 调用；骨架/确认组件被至少 1 处真实使用（避免"建了不用"）。

## 2. 实际完成内容

| 工作项 | 状态 | 实际做法 / 关键改动位置 |
| --- | --- | --- |
| UX-1-01 统一错误对象 | 完成 | `apps/web/lib/api.ts` 新增 `ApiError{status, code, detail}` 与 `apiError(response, fallback, codeMessages?)`；**49 处**单行 `if (!response.ok) throw new Error("…")` 批量替换为 `throw await apiError(...)`；**5 处**多行带错误码映射的块（文档提交、云盘上传/删除、KB 索引/检索）改为一处一映射，行为不变但类型统一。新增 `errorMessage(error, fallback)` 供页面侧取文案。`extractErrorCode` 容错处理 `drive_quota_exceeded:123/456` 这类"码+冒号明细"的 detail。FastAPI 422 的 `detail` 数组被映射为 `字段名：原因` |
| UX-1-02 通用组件 | 完成 | `apps/web/components/ui.tsx` 新增 `LoadingSkeleton`、`ConfirmDialog`；`apps/web/app/globals.css` 新增 `.skeleton*`、`.button-danger`、`.confirm-body`、`.empty-cta` 样式 |
| UX-1-03 新建项目 | 完成 | 新增 `apps/web/components/project-create.tsx`（名称/模板包/题号/说明 + 客户端长度守卫 + 服务端原因展示）；`apps/web/lib/api.ts` 新增 `createProject()` 与 `ProjectCreateInput`；成功后 `selectProject` + `router.push("/")`，落在总览页 |
| UX-1-04 空状态引导 | 完成 | `apps/web/app/page.tsx` 在"无项目"时走独立分支（`empty-cta` 引导 + 新建按钮 + 去模板包），替换原来的"等待项目数据"死状态；侧栏文案由"等待项目数据"改为"还没有项目，先创建一个" |
| UX-1-05 项目选择持久化 | 完成 | `apps/web/lib/workspace.tsx` 新增 `SELECTED_PROJECT_KEY = "map.selectedProjectId"` 与读写函数（隐私模式下写入失败不阻断）；`selectProject` 与 `refresh` 均落盘；候选项目失效时回退到列表首项并提示一次 |
| UX-1-06 入口常在 | 完成 | `apps/web/components/shell.tsx`：项目切换器由 `projects.length > 1` 改为 `> 0`；侧栏项目块与顶栏各加一个"新建项目"按钮（`sidebar-new-project` / `topbar-new-project`） |

**计划外但必要的修复（同属本阶段范围）**：`refresh` 原以闭包中的 `projectId` 为最高优先级，导致"已有项目时新建项目"会被立刻切回旧项目（首次创建不触发，因为没有旧项目）。现 `refresh(preferredProjectId?)` 把显式首选排在当前选中之前，`CreateProjectModal` 传入新项目 id。此缺陷由浏览器验收发现，详见 §4。

## 3. 与计划的偏差

**有偏差，共 3 处，均不改变阶段范围：**

1. **`ConfirmDialog` 的首个使用点与计划不同。** 计划把危险操作确认放在 UX-6-05（删除云盘文件、删除会话、落库门禁、生成提交包）。为使 UX-1 的退出条件"确认组件被至少 1 处真实使用"成立，本阶段先在新建项目弹窗的"放弃已填写内容"上使用（`create-project-discard`），UX-6-05 仍按原计划接入其余四处。
2. **页面级 `catch { notify("…失败") }` 未批量清理。** 计划决策 D10 要求页面禁止再写裸 catch。本阶段只统一了数据层并新增 `errorMessage` 助手，新代码与本次触碰的页面（总览、壳层、新建项目）已按新规书写；其余页面各自的错误透传随它们所属的阶段处理（任务/模板 → UX-2，协作 → UX-6，打磨 → UX-7）。理由：批量改动 15 个页面会超出本阶段的验证能力。
3. **`getArtifactText` 保留了静默失败。** 该函数在非 2xx 时 `return ""`（`apps/web/lib/api.ts`），调用方 `startCollaboration` 无 try/catch，改为抛出会产生未处理的 rejection，比现状更差。它随 UX-6-01（协作编辑落库）一起重写该路径。已记入 §5。

## 4. 测试与验证

**基线命令（计划书 §7）**

```text
后端：python -X utf8 -m unittest discover -s . -p "test_*.py"（apps/api）
      → Ran 266 tests, OK (skipped=13)，27.8s（与 UX-0 基线一致，本阶段未改后端）
前端：cd apps/web && npm run build
      → 16 条路由（15 页面 + not-found）全部静态预渲染，无类型错误
```

**浏览器端到端验收（真实前端 + 真实后端，独立空库）**

验收夹具：`scripts/acceptance_empty_api.py`（真实 FastAPI 应用 + 临时 SQLite 空库，只清项目域、保留身份种子；因产品 CORS 白名单硬编码 3000 端口，夹具内为临时实例放开验收端口，未改产品代码）。前端用 `NEXT_PUBLIC_API_URL=http://127.0.0.1:8010 npx next dev -p 3014`。

| 验收标准 | 操作 | 结果 |
| --- | --- | --- |
| 空库时不出现"等待项目数据"，而是引导新建 | 空库加载 `/` | `overview-empty-projects` 可见、文案为"从新建第一个项目开始"；`等待项目数据` 文本计数为 0；连接状态"已同步" |
| 界面新建后项目出现在侧栏并被选中 | 从总览 CTA 打开弹窗（模板包下拉真实拉到 `CUMCM 数学建模竞赛工作流包 v1.1.0`，默认 C 题）→ 提交 | 侧栏显示新项目名、面包屑同步、切换器出现该选项、`localStorage["map.selectedProjectId"]` 写入新 id、空状态消失 |
| 刷新后保持选中 | `reload()` | 仍为同一项目 |
| 切换项目后刷新仍是切换后的 | 切换器选回第一个项目 → `reload()` | 切换前后一致（`persisted: true`） |
| 非法名称显示服务端原因 | 1 字符（客户端守卫）与 130 字符（服务端 422）各提交一次 | 客户端："项目名称至少 2 个字符"；服务端："name：String should have at most 120 characters"（pydantic 字段级原因直达用户，而非"创建失败"） |
| 新建即选中（计划外回归项） | 已有 2 个项目时再建第 3 个 | 修复前切回旧项目；修复后切到新项目（`厘清见 §3`） |

## 5. 尚未完成与边界

用用户视角写清楚"现在还不能做什么"：

- **除总览、壳层与新建项目弹窗外的页面，错误仍可能只弹"…失败"**，服务端 detail 不展示（随各阶段处理，见 §3 偏差 2）。
- **文档编辑器的静默失败未处理**：如果成果物内容读取失败，编辑器会以空白内容启动而不报错（随 UX-6-01）。
- **端口/CORS 仍是硬假设**：产品后端只允许 `localhost:3000` 与 `127.0.0.1:3000`，前端 `NEXT_PUBLIC_API_URL` 在构建期内联。换端口/换机器部署时前端会表现为"请求被拦"，且首屏会显示"等待连接"。本阶段未改（属部署配置问题，计划书 §8 已列为风险）。
- **项目删除不存在**：因此"选中项目被删除后的回退提示"这条逻辑无法在界面上触发验证，只在代码层实现（`refresh` 中的 `dropped` 分支）。触发条件是后端删掉项目，可用 SQL 删除后刷新复现。
- **切换器仍是原生 `<select>`**，多项目时没有搜索/分组；项目数量增长后体验会退化（未列入 UX-2..UX-7）。
- **未做**：任务详情、模板包选择器、Agent 相关一切（属 UX-2..UX-5）。

## 6. 下一步

- 下一阶段：**UX-2 任务与模板闭环**（可与 UX-3 并行）
- 入口条件是否满足：**是**。UX-1 已退出（`POST /api/projects` 在前端可达并有真实 UI 调用；`LoadingSkeleton` 与 `ConfirmDialog` 均有真实使用点）。
- 建议的下一批工作项：`UX-2-01`（任务详情弹窗）、`UX-2-02`（负责人真实列表 + 改派）、`UX-2-04`（模板包选择器）
- 提醒：UX-2 触碰 `tasks/page.tsx`、`pack/page.tsx`，按 D10 同步把这两页的裸 catch 改为 `errorMessage`。

## 7. 复现命令

```powershell
# 1) 后端基线
cd apps\api
$env:PYTHONPATH = "$(Resolve-Path '..\..');$(Resolve-Path '.')"
python -X utf8 -m unittest discover -s . -p "test_*.py"     # 期望 Ran 266 tests, OK (skipped=13)

# 2) 前端构建
cd ..\web
npm run build                                                # 期望 16 条路由，无类型错误

# 3) 空库验收（两个终端）
cd ..\..                                                     # 回到仓库根
python -X utf8 -m uvicorn scripts.acceptance_empty_api:app --host 127.0.0.1 --port 8010
cd apps\web
$env:NEXT_PUBLIC_API_URL = "http://127.0.0.1:8010"
npx next dev -p 3014                                         # 打开 http://127.0.0.1:3014
```

验证要点：空库应显示"从新建第一个项目开始"；建项目后侧栏与切换器同步、刷新保持；用 130 字符名称提交应看到 `name：String should have at most 120 characters`。