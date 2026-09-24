# W-4 导航收敛（深页收进「高级工具」） 交接

> 交接状态：`PASS`
>
> 日期：2026-09-22
>
> 上游：`docs/handoffs/W3_AUTO_SCHEDULER_HANDOFF.md`
>
> 设计依据：`docs/PROJECT_WORKSPACE_DESIGN.md`（四条拍板中的"旧页收进「高级工具」分组"）
>
> 已发布：`https://synapforge.top`（`server_verify.sh` 22/22；随本次发布还按用户要求把
> IP 入口的 Basic 口令改为 `<Basic 口令已脱敏>`，见 §5）

---

## 1. 做了什么

侧栏从 7 组 19 入口收敛为 **6 组 19 入口**——入口一个没少，只是把"每天用的"和"偶尔用的"分开了：

| 分组 | 入口 |
| --- | --- |
| 项目 | 项目工作区、项目总览 |
| 任务与流程 | 任务与流程、我的任务 |
| 内容与文档 | 成果物库、文档版本、个人云盘 |
| 审核与交付 | 审核门禁、交接中心 |
| 团队与空间 | 团队与成员、空间设置 |
| **高级工具**（新） | 建模模板包、论文交付、知识库、超图可视化、AI 问答、运行控制台、设备与接入、项目时间线 |

**只隐藏不删除**（沿用平台既有原则）：路由、页面、深页之间的互相跳转、工作区里的深链全部原样；
`Ctrl+K` 快速跳转仍能搜到所有 19 个入口（含高级工具里的 8 个）。项目工作区仍在第一位。

## 2. 改了什么文件

- `apps/web/lib/nav.ts`：重排 `NAV_SECTIONS`（新增 `tools` 分组，`Wrench` 图标；删掉 `knowledge` / `ops`
  两个分组 id），文件头注释同步说明分层口径。
- `scripts/_w1_ssr.py`：SSR 不变量脚本的分组按钮期望值从 7 个改为 6 个
  （`nav-group-project/flow/content/delivery/workspace/tools`），断言口径不变：每个页面都能看到全部入口与分组按钮、
  当前页唯一高亮。

没有任何后端改动——本次是纯前端 + 部署口令。

## 3. 验收

| 项 | 结果 |
| --- | --- |
| 前端构建 | 25 页全静态预渲染通过 |
| SSR 不变量 | **19 路由 × 19 入口 × 6 分组按钮**（旧页全部仍可达） |
| 浏览器实机 | 侧栏 6 行平铺（项目 18 / 任务与流程 2 / 内容与文档 3 / 审核与交付 2 / 团队与空间 2 / 高级工具 8），当前分组自动展开；访问被移动的 `/kb` → 面包屑显示「高级工具 › 知识库」、所在分组自动展开；`Ctrl+K` 面板仍列出全部分组与入口 |
| 线上 | 域名 `/workspace` → 200 且 SSR 里能看到 `nav-group-tools`；`server_verify.sh` **22/22** |

## 4. 边界

- 没有做页面合并/删除：`/graph`、`/kb`、`/ask`、`/delivery`、`/timeline`、`/runs`、`/devices`、`/pack`
  都还是原页面原路由。
- 移动端底部标签栏（UX-9 的 5 个 tab）没动：`总览 / 任务 / 审核 / 问答 / 更多` 仍是快捷入口，
  「更多」打开抽屉后可到全部分组。
- 侧栏分组的开合记忆沿用 localStorage（`math-agent-platform.nav.open-sections`）；改过分组 id 后
  `knowledge` / `ops` 的旧记忆会失效（无害，首次点开后会重新记住）。

## 5. 随本次发布的口令变更（用户要求）

- **IP 入口的 nginx Basic 口令**：`<旧口令已脱敏>` → **`<Basic 口令已脱敏>`**（用户明确要求"简单一些"）。
- 已做的动作：`htpasswd -b /etc/nginx/.math-agent-platform.htpasswd map <Basic 口令已脱敏>` → `systemctl reload nginx`
  → `/etc/math-agent-platform/credentials.txt` 同步更新（自检脚本从这个文件读凭据）。
- 备份：旧口令文件备份为 `/etc/math-agent-platform/credentials.txt.bak-20260922`、
  htpasswd 备份为 `/root/.math-agent-platform.htpasswd.bak-20260922`（要回滚就还原这两个 + reload nginx）。
- 实测：`Basic(map:<Basic 口令已脱敏>)` → 200；旧口令 → 401；无认证 → 401；`server_verify.sh` 22/22。
- **注意**：这是 IP 入口（运维后门）的口令，不是平台账号密码。域名入口 `https://synapforge.top`
  仍然走账号登录（邮箱 + 口令），那个口令有平台策略（≥10 位、不能纯数字），**设不成 `<Basic 口令已脱敏>`**——
  要改的话请用平台「账号设置」页自助改，或告诉我一个符合策略的短口令、我在服务器上给你重置。
- 口令变简单意味着 IP 入口更容易被扫（互联网扫描器一直在敲门）。域名入口没有这个风险；
  如果不想留着 IP 后门，我可以把 80 端口的 Basic 关掉（`server_bootstrap.sh` 里有现成的开关位置）。

## 6. 项目工作区（W-1 ～ W-4）到此收口

四期全部上线：工作区骨架（W-1）→ 任务板派单与成果空间（W-2）→ 全自动调度器（W-3）→ 导航收敛（W-4）。
设计文档 `docs/PROJECT_WORKSPACE_DESIGN.md` 里承诺的范围已全部交付，未做的都写在各自交接的"边界"一节：
@Agent 指令（二期）、批量路径写成员事件、卡片内容不随对象改名回溯、能力词表仍是自由字符串。

## 7. 复现命令

```bash
cd apps/web && npm run build && npm run start     # 改完前端必须重建并重启
python scripts/_w1_ssr.py                          # SSR 不变量（19×19×6）
# 服务器上改 Basic 口令（如需再次轮换）
ssh root@<IP> "htpasswd -b /etc/nginx/.math-agent-platform.htpasswd map <新口令> && \
  sed -i 's/^password: .*/password: <新口令>/' /etc/math-agent-platform/credentials.txt && \
  systemctl reload nginx"
```