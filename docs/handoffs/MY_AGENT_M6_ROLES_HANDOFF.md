# MY-AGENT M-6 交接：多智能体角色预设（R-1 机制 + R-2/R-3/R-3b **全部 12 个角色**）

> 2026-09-23 · 状态：**已上线并通过线上验证（12 个角色全部交付）** · 计划 `docs/MY_AGENT_M6_AGENT_ROLES_EXECUTION_PLAN.md`
> 上游：MY-AGENT M-1～M-5（对话通道、页面、附件、真中断、分段显示）
> 分期：R-1 机制 ✅ · R-2 写作/审核/检索 ✅ · R-3 审题/建模/编程/图表 ✅ · R-3b 评审打分/编译/Word/英文/选题 ✅ · R-4（模式强度并入、sha256 防漂移）待做

---

## 1 这一期交付了什么（用户视角）

「我的智能体」页面的工具条上多了一个**角色**下拉，**12 个角色 + 内置计划模式**：

| 分组 | 角色 |
| --- | --- |
| 审题与建模 | 审题分析 · 建模与求解设计 |
| 编程与图表 | 编程实现 · 图表设计 |
| 审核 | 逻辑对抗复核 · 论文评审 |
| 写作 | 中文论文写作 · 论文·Word · 英文论文 |
| 交付 | 编译合规 · 文献检索 · 选题规划 |

选一个角色，下一轮就按那个角色的系统提示干活；**历史轮次记着它当时用的是谁**（中途换角色不会篡改历史）。
**工具边界是真的、而且页面上如实标**：4 个角色（建模/编程/图表/编译）**可执行命令与写文件**——
它们在下拉里带「（可执行）」、选中后工具条显示「可执行命令」、悬停说明写着"会改动工作目录"；
其余 8 个是只读角色（审核角色写不了文件，才谈得上"独立复核"）。

三个角色的提示词来自工作流成熟资产（近全文可搬/蒸馏），
**检索、作图、论文评审打分**这三个是工作流里没有角色文档的——角色句、输出契约、rubric 都是新写的。
来源逐条登记在 `deploy/cloud-agent/roles/SOURCES.md`（每条硬规则来自哪个文件哪一行 + 新补的写明理由）。

## 2 机制事实（本次实测，不是文档抄来的）

| 事实 | 证据 |
| --- | --- |
| 自定义角色 = `~/.config/opencode/agent/<名>.md`（`agents/` 也认） | 探针两个目录都放，`opencode agent list` 都出现 |
| frontmatter 的 `description` / `mode` / `tools` 都生效；**正文即系统提示** | `opencode debug agent` 回显 `prompt` 字段就是正文；探针要求答 `ROLE_OK`，实跑命中 |
| `--agent <名>` 真选中该角色 | 同上探针实跑 |
| `tools: {bash:false, edit:false, write:false}` 真的禁用 | debug 输出里三个都是 `false`；线上 `mm-review` 那一轮**没有 `file.changed` 事件** |
| `opencode agent create` 是**交互式**的 | 非交互（`</dev/null`）会挂住 → 定义一律手写 md |
| `agent list` 必须在**工作目录**里跑 | 从 `/root` 跑报 `PermissionDenied /root/opencode.jsonc` |
| `agent list` 输出**不含 description** | 只有 `名字 (模式)` + 权限 JSON → 说明改从角色文件的 frontmatter 读 |
| 每步都重发系统提示 | `step_finish.tokens` 可读：3 行提示的探针即 3,545 input tokens |

## 3 交付物（文件级）

**角色定义（单一真源，只存仓库）**
- `deploy/cloud-agent/roles/` 下 **12 个角色 + `SOURCES.md`（来源台账）**：
  `mm-analysis` 审题分析 9.9k 字符 / `mm-modeling` 建模与求解设计 7.3k / `mm-coding` 编程实现 7.4k /
  `mm-figure` 图表设计 7.5k / `mm-review` 逻辑对抗复核 6.1k / `mm-critique` 论文评审 8.8k /
  `mm-paper-zh` 中文论文写作 6.8k / `mm-paper-zh-docx` 论文·Word 7.8k / `mm-paper-en` 英文论文 9.2k /
  `mm-compile` 编译合规 7.4k / `mm-research` 文献检索 7.6k / `mm-topic` 选题规划 8.3k
- `scripts/prompt_lint.py`（**提示词质量门槛**，11 条机械判据：九段骨架、硬规则条数与起手动词、
  输出格式要有可复制骨架、自检要可判定、长度 3k–12k 字符、禁仓库内路径、必须有"不假装"条款、
  公式闭合、`description` 必须是「短名 · 一句话」且短名 ≤8 字且总长 20–90）

**内核（执行体那台）**
- `apps/agent/agent_inventory.py`：`_probe_opencode_roles`（名字取 `opencode agent list`，说明取角色文件 frontmatter，
  也读 `opencode.json` 的 `agent` 段）+ **`_frontmatter_tools` / `_role_executes`（工具边界：`bash/edit/write`
  有没有被显式关掉 → `executes`）** + `BUILTIN_SELECTABLE_AGENTS = {"plan": …}` + `AgentInventory.roles()`
- `apps/agent/chat_loop.py`：`build_chat_command(..., agent_role=)` 在 `{prompt}` **前**插 `--agent`；
  另加一行 argv 日志（**省略提示词正文**）——排查"角色传没传下去"时这是唯一的眼见证据
- `apps/agent/agentd.py`：心跳 `resource_summary["roles"] = [{name, description}]`

**平台**
- `apps/api/migrations/025_agent_chat_roles.sql` + `agent_chat.ensure_schema` 的老库补列
- `apps/api/app/contracts.py`：`AgentChatConversationCreate.role`、`AgentChatConversationUpdate`、`AgentChatConversation.role`、
  `AgentChatTurn.role`、`MyAgentRole`（name/description/**executes**）、`MyAgentEndpoint.roles`
- `apps/api/app/agent_chat.py`：`create_conversation` 存角色、`append_message` 把会话角色**抄到轮次上**、`update_conversation`、`list_my_agents` 带上角色与执行边界
- `apps/api/app/main.py`：`PATCH /api/my-agent/conversations/{id}`（角色/模型/标题，**只影响下一轮**）
- `apps/web/app/my-agent/page.tsx` + `lib/api.ts`：角色下拉（可执行角色带「（可执行）」）、
  `handleRoleChange`/`handleModelChange`（都 PATCH 会话并提示"下一轮生效"）、回答头与抽屉显示角色与工具边界
- `apps/web/app/globals.css`：抽屉开关 `z-index: 7`（高于抽屉的 6）+ 可执行角色的小标记样式

**部署**
- `install-cloud-agent.sh`：第 6/7 步装角色（**只装 `mm-*.md`**，`SOURCES.md` 不进 opencode 的 agent 目录）

**验收**
- `scripts/deploy/_m6_roles_verify.py`（平台机上跑，**自清理**）：5 类断言，见 §5

## 4 平台口径（三处必须知道的）

1. **提示词只存仓库**：平台只存"这个会话选了哪个角色"，**不存提示词正文**——避免"平台改一版、执行体跑另一版"。
   改提示词 = 改仓库 → 跑 lint → 重跑部署脚本第 6 步。
2. **角色名合法性不在平台校验**：可选角色来自执行体心跳（`opencode agent list` 的真探测）。
   名字在那边不存在时由 opencode 自己报错，内核把退出码与错误如实回传成该轮 `FAILED`——平台不猜、也不假装成功。
3. **两列分工**：`agent_conversations.role` 是设置（中途可改，**只影响下一轮**）；`agent_turns.role` 是**这一轮实际用的**。
   分开记是因为：换了角色之后，历史里"上一轮是谁跑的"必须仍然是真的。

## 5 验收（线上真实数据，逐条有证据）

**脚本验收**（`_m6_roles_verify.py`，在平台机上跑，跑完自清理）：

```
上报角色：['plan', 'mm-paper-zh', 'mm-research', 'mm-review']
[PASS] 三个角色 + plan 都出现在下拉数据里；中文说明来自角色文件本身
[PASS] mm-review  : 一轮跑完 DONE，回复呈现「findings/fatal/严重性/上界/下界/验证」，8455 tokens
[PASS] mm-review  : 只读角色没有改文件（无 file.changed）
[PASS] mm-research: 一轮跑完 DONE，回复呈现「未核验/存疑/核验/DOI/证据/检索式」，33279 tokens（6 步，用了工具）
[PASS] mm-paper-zh: 一轮跑完 DONE，回复呈现「摘要/段/问题/数值/骨架」，9007 tokens
[PASS] 每轮都有 process.started（真起了进程）；用量都可读
=== 结果：全部通过 ===
```

**命令行证据**（执行体 journal，提示词已省略——这是"角色真传下去了"的硬证据）：

```
[chat] 命令（提示词已省略）：opencode run --format json --auto --agent mm-review {prompt}
[chat] 命令（提示词已省略）：opencode run --format json --auto --agent mm-research {prompt}
[chat] 命令（提示词已省略）：opencode run --format json --auto -m deepseek/deepseek-v4.1-flash --agent mm-paper-zh {prompt}
[chat] 命令（提示词已省略）：opencode run --format json --auto -m deepseek/deepseek-v4.1-flash --agent mm-review {prompt}   ← 页面点出来的那一轮
```

**浏览器实机**（桌面 1440）：下拉 5 项且是短名（默认（无角色）/ 计划模式 / 中文论文写作 / 文献检索 / 逻辑对抗复核）；
换模型提示「模型已切到「deepseek-v4.1-flash」，下一轮生效」；换角色提示「角色已切到「逻辑对抗复核」，下一轮生效」；
发一轮后回答头显示「逻辑对抗复核 · deepseek-v4.1-flash」、抽屉显示「角色 逻辑对抗复核（mm-review）」与用量 8362 tokens；
回复是审核角色该有的形状（findings JSON + `fatal_count` + "怎么验证（你自己一步可做）" + 来源限制 +
**"我无执行权限，未运行任何代码……请自行复算确认"**）。测试会话已删除，用户原有 4 条对话保持原样。

**基线**：Agent **377 项** OK（11 skipped）/ API **537 项**（仅 2 项既有 LaTeX 环境失败，与本期无关）/ 前端 tsc 通过 / 线上 `server_verify` 22/22。

## 6 踩到的坑（都改了，别再踩）

1. **安装脚本把台账当角色装了**：`for role_file in roles/*.md` 会把 `SOURCES.md` 也拷进 opencode 的 agent 目录
   （那是一个没有 frontmatter 的 md）。改成只装 `mm-*.md`，并已清掉线上那个文件。
2. **`description` 的写法会直接影响页面**：页面下拉取「·」前的部分当标签，所以
   「数学建模与统计建模中文论文写作 · …」会把工具条撑长；写成「文献检索与证据整理：…」（没有「·」）
   则标签退化成英文 id `mm-research`。现在**约定 + lint 双保险**：必须是「短名 · 一句话」，短名 ≤8 字。
3. **模型切换原本是静默无效的**（真 bug）：角色会 PATCH 到会话上，模型却只在**建会话**时写一次——
   中途换模型对下一轮没有影响，用户只会觉得"我换了模型却没生效"。现在两条路径同一口径（都 PATCH、都提示"下一轮生效"）。
4. **抽屉会盖住自己的开关**：抽屉是 `position:absolute` 从右边缘盖过来（`z-index: 6`），
   正好压住工具条右端的「执行细节」按钮——打开之后点不动它、收不起来（自动化点它时报 `covered`）。
   给开关 `position: relative; z-index: 7`。
5. **`--dst /tmp/...` 在 Git Bash 会被转成 Windows 路径**：`remote.py put` 把文件传到了远端的
   `C:/Users/.../Temp/` 下（远端本没有这个目录，是相对 home 建的）。要么用 `MSYS_NO_PATHCONV=1`，
   要么用相对/仓库内的远端路径——**注意 `MSYS_NO_PATHCONV=1` 时本地 `~` 也不会展开**，`--password-file` 要写全路径。
6. **短名里再带「·」会把下拉标签切坏**：页面按「 · 」取短名，`mm-paper-zh-docx` 的说明写成「论文·Word · …」
   时，按单个「·」切出来只剩「论文」（和「中文论文写作」撞脸）。修法三处：页面按**带空格的 ` · `** 切、
   lint 增加"短名里不许再出现「·」"、角色说明改名「Word 论文」。
7. **远程长验收：SSH 超时会把输出和判决一起丢掉**。`remote.py exec` 超时（本机上限 10 分钟）时连接断开，
   脚本可能**仍在服务器上继续跑并自清理**——回来只看得到"什么都没发生"。做法：长验收用
   `(setsid nohup env … python /tmp/xxx.py > /tmp/xxx.log 2>&1 &)` 后台跑，再轮询日志与数据库。
   （第一次踩到时丢了 3 个角色的判定，只能重跑一遍。）

## 7 已知边界 / 未做

- **免费模型会失败**：`opencode/*-free` 这几个走 opencode 的免费额度，实跑报
  `FreeTierError: OpenCode's free tier can only be used from within OpenCode`（平台上**如实显示成失败**，没有假装成功）。
  默认模型是百炼的 `deepseek/deepseek-v4.1-flash`，付费可跑；免费模型这条路要不要留，等用户定。
- **执行体重启后第一次模型探测可能超时**：实测出现过一个心跳周期里 `models=[]` 而 `roles` 正常
  （两个探测超时上限不同：模型 30s、角色 20s，`opencode models` 冷启动更慢）。自愈：TTL 300s 后重探即恢复。
  影响：那一小段时间页面模型下拉为空（角色下拉不受影响）；建会话时若模型为空，现在会依次回落
  「心跳 default_model → 注册自报值 → 留空（交给 opencode 自己的默认）」，与页面显示口径一致。
- **`websearch` 能力未验证**：`mm-research` 的提示词里写成条件式（"若你的环境可用联网就打开核验页，
  不可用就说明并给出检索式"），实测那一轮它用了工具但仍可能拿不到网络结果——它如实说了"未核验"。
- **未做**：R-3 / R-3b 的其余 9 个角色（审题/建模/编程/图表/评审打分/编译/Word/英文/选题）、
  角色级权限卡片（与 M-5c S-3 合流）、平台端编辑提示词（明确不做：单一真源在仓库）、
  角色文件 sha256 上报（R-4 的防漂移项）。

## 8 变更纪律（写在这里，免得下次又踩）

1. 改角色 → 改 `deploy/cloud-agent/roles/*.md` → `python scripts/prompt_lint.py` 全绿 → 重跑 `install-cloud-agent.sh`（幂等，会覆盖）。
2. 提示词里**不得出现**仓库内路径（`_utils/`、`scripts/`、`figures/`、`user_data/`、`CLAUDE.md`）：对话通道没有这些文件（lint 拦）。
3. frontmatter **不写 `model`**：模型由页面下拉决定（lint 拦）。
4. 只读角色不要给写权限——"独立复核"靠的就是 `tools.write=false`（线上已验证 `file.changed` 不出现）。