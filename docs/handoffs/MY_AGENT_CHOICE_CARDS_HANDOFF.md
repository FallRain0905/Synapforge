# 「我的智能体」对话体验交付：Markdown/公式渲染 · 模式位置 · 结构化选择卡（含内核协议注入）

> 2026-09-24 · 状态：**已上线并线上实测通过** · 上游 M-1…M-6（对话通道 / 页面 / 附件 / 真中断 / 分段显示 / 角色预设）
> 部署证据：平台 `server_verify.sh` 22/22（两次发布）· 云端执行体重装内核 + 角色并重启

---

## 1 这一期交付了什么（用户视角）

1. **回答按 Markdown 渲染**：表格、列表、代码块、引用都按排版显示（角色提示词要求输出表格的场景多）。
2. **公式按 KaTeX 渲染**：行内 `$…$` 与行间 `$$…$$` 都出真公式（数模场景的核心）。
3. **模式切换移到对话框底部**：`对话 / 项目工作` 两个 tab 从标题右上角挪到输入框下方（实测位置见 §4）。
4. **结构化选择卡**：智能体需要用户拍板时，回复末尾带一个 `synapforge-choice` 卡片 → 页面渲染成**可点选、可提交**的表单
   （单选 / 多选 / 补充文本，必填项未选完不给提交）；提交后选择以**下一条用户消息**发回执行体，执行体据此继续。
5. **协议注入在内核层**（不是只写某个角色提示词）：任何角色、任何一轮都带协议说明——换角色也照样生效。
6. **三个"把选择权交回用户"的角色**（审题分析 / 建模与求解设计 / 选题规划）额外写明了"什么时候该出卡片、什么时候不要出"。

## 2 文件级交付

| 文件 | 作用 |
| --- | --- |
| `apps/web/components/markdown.tsx` | Markdown + KaTeX 渲染层（`react-markdown` + `remark-gfm` + `remark-math` + `rehype-katex`）；**不开 `rehype-raw`**（回复是不可信内容，HTML 只当文本） |
| `apps/web/lib/agent-choice.ts` | 协议解析与回发文案：`parseChoiceResponse` / `validChoice` / `formatChoiceAnswer`（只认**末尾**的合法 fence；普通编号列表、别的 json fence、坏 JSON 都不成卡，且**不吞原文**） |
| `apps/web/components/agent-response.tsx` | 回复渲染：Markdown 正文 + 卡片表单；提交失败**不显示成已提交** |
| `apps/web/app/my-agent/page.tsx` | 模式 tab 移到输入框下方；`onSubmitChoice` 接 `sendContent`（返回值判成败） |
| `apps/web/app/globals.css` | 卡片样式；**修掉全局 `input, select, textarea { width: 100% }` 把 radio 拉成整行**导致的文字挤成 0 宽（§5 坑①） |
| `apps/agent/chat_loop.py` | `CHOICE_PROTOCOL_INSTRUCTION` **真的接进提示词**（每轮末尾追加一次），此前只是定义未使用 |
| `apps/agent/test_agent_chat.py` | 新增 2 项：协议注入在普通轮存在且只追加一次；带附件 + 带角色时**顺序**（附件前缀→正文→协议在最后） |
| `apps/web/scripts/verify-agent-choice.mjs` | 29 项断言：解析 / 拒绝（20 类非法形状）/ HTML 只当文本 / 回发文案；**测的是真源码**（本地 tsc 编出 JS 再导入） |
| `deploy/cloud-agent/roles/{mm-analysis,mm-modeling,mm-topic}.md` + `SOURCES.md` | 三角色补"需要拍板时用选择卡、要材料时仍用文字清单"；台账新增「选择卡协议」一节（标明不是工作流资产） |

## 3 协议形状（写在这里，免得下次去代码里找）

````text
```synapforge-choice
{"id":"稳定的短标识","title":"卡片标题","description":"可选说明","questions":[
 {"id":"topic","label":"主题范围","type":"single","required":true,
  "options":[{"value":"llm","label":"LLM 多智能体协作","description":"可选简述"}]},
 {"id":"format","label":"产物格式","type":"multiple","options":[{"value":"md","label":"Markdown 表格"}]},
 {"id":"extra","label":"其他要求","type":"text","placeholder":"可选补充"}]}
```
````

- 约束（解析器强制，违反即不成卡）：`questions` 1–20 条；问题 `id` 唯一非空；`type ∈ {single,multiple,text}`；
  `text` 不许带 `options`；选项 1–12 个、`value` 唯一非空、`label` 非空。
- 渲染器不做任何"猜测"：编号列表、普通 json code fence、出现在回复中段的 fence 都不会变成控件；坏 JSON 保留原文显示。
- 提交文案（`formatChoiceAnswer`）把 value 换回**可读 label**、未填项写"未填写/未选择"，并附"请按以上选择继续执行，不要重复询问已经回答的问题"。

## 4 线上实测证据（2026-09-24）

**布局（量出来的，不是目测）**：输入框 `top 366 / bottom 410`，模式 tablist `top 434 / bottom 466` → **在输入框下方**。

**一轮真对话**（会话选「默认（无角色）」，执行体 `云端执行体 · 在线`，模型 `deepseek/deepseek-v4.1-flash`）：

| 断言 | 实测 |
| --- | --- |
| Markdown 表格 | `.md-body table` = 1（三列：方案 / 适用场景 / 主要风险） |
| KaTeX 公式 | `.katex` = 2（行内 `E=mc²` + 行间 `∫₀¹x²dx=1/3`） |
| 选择卡出现 | `[data-testid^=agent-choice-]` = 1（标题「选择深入的多智能体协作方案」，三题：单选/多选/文本） |
| 未选完不给提交 | 三题都选完前 `button.disabled = true` |
| 提交 | 卡片变「已提交选择」、输入全部 disabled；出现新用户消息：`用户已完成「…」的选择：1. 深入哪种方案：分层混合 Hierarchical/Hybrid …` |
| 续跑 | 第二轮完成（1172 字），并**再次**出一张卡（问下一步深入方向）→ 卡片链路可连续使用 |
| 刷新后 | 历史仍在（2 条用户消息 / 2 张卡 / 公式 2 个），卡片重新渲染为可提交状态 |

**清理**：测试会话已删除（用户原有 4 条对话未动）；执行体上 M-6 遗留的 3 个挂住的 `opencode agent create` 探针进程已杀、`/tmp/probe-agent*` 无残留。

## 5 踩到的坑（都改了）

1. **卡片选项文字被挤成一字一行（真 bug，线上可见）**：全局样式 `input, select, textarea { width: 100% }` 把 radio
   拉成整行（实测 radio **287px**），后面文字容器被压到 **0 宽**（`grid-template-columns` 退化成 12px，按字换行）。
   修法：卡片里的 radio/checkbox 写死 `width/height: 16px`，文字容器 `flex: 1 1 auto; min-width: 0`。
   定位方式就是**量元素尺寸 + 逐条匹配生效的 CSS 规则**（不是看图猜），与 W-6 那次"样式没接上类名"同一套查法。
2. **`CHOICE_PROTOCOL_INSTRUCTION` 曾经只是定义、没有接进提示词**：卡片逻辑写完了但因为协议没下发，执行体根本不会输出卡片。
   现在接在 `prompt_with_inputs` 之后、`build_chat_command` 之前，并加了"只追加一次、顺序固定"的回归测试。
3. **上传路径被 Git Bash 转成 Windows 路径**：`--dst /root/agent-src.tar.gz` 变成远端 `D:/Git/root/...`。
   远端路径一律 `MSYS_NO_PATHCONV=1`（此时本地 `~` 不展开，`--password-file` 要写绝对路径）。
4. **前端新依赖没进服务器 `node_modules`**：`server_release.sh` 见 `node_modules` 存在就跳过 `npm ci`，
   而线上那份没有 `react-markdown` 等新包（构建会失败）。发布前先 `rm -rf apps/web/node_modules`（`package-lock.json` 里已有新依赖）。
5. **README 的"后端测试命令"缺 `PYTHONPATH`**：按 README 在 `apps/api` 里跑会 5 个模块 `ModuleNotFoundError: No module named 'packages'`
   （449 项 / 2 failures / 5 errors 的假象）。正确口径：仓库根 `PYTHONPATH=apps/api:. python -X utf8 -m unittest discover -s apps/api -p "test_*.py"` → **537 项**。

## 6 边界与未做

- 卡片是**协议约定**，不是强保证：执行体（模型）可以不输出卡片，或输出后你不想选——照旧可以打字回复。
- 解析器不做"宽松兜底"（不猜不了字段、不修坏 JSON）：宁可显示原文，也不造一个点不动的假控件。
- 卡片状态是**前端内存态**：刷新后卡片回到"可提交"（历史消息不记录"已提交过"）——要记录就得把提交事件落库，属后续项。
- 未做：`M-6 R-4`（角色说明摘要进抽屉、角色文件 sha256 防漂移、模式/强度并入下拉）、`M-5c S-0…S-4`（真流式）。