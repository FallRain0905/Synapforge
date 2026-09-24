# AUTH-1 用户注册与账号管理 交接

> 交接状态：`PASS_WITH_ASSUMPTIONS`
>
> 日期：2026-09-17
>
> 上一份交接：`docs/handoffs/UX_9_MOBILE_ADAPTATION_HANDOFF.md`
>
> 部署目标：`https://synapforge.top`（IP 入口 `http://156.239.229.143` 保留 Basic 作运维后门）

---

## 1. 目标与拍板

用户提出："新增用户注册系统和账号管理"。开工前确认了四条：

| 决策 | 取值 |
| --- | --- |
| 注册门槛 | **邀请码注册**（复用现有 `invitations` 表） |
| 管理权限 | **首个注册账号自动成为管理员**，可停用/启用、重置密码、授予管理员、发放邀请码 |
| 存量数据 | **归到首个管理员账号**（`member-001` 保留为 suspended，不物理删除） |
| nginx Basic | **域名入口去掉 Basic**（走账号登录）；**IP 入口保留 Basic** 作应急后门 |

## 2. 必须先修的四个断点（都不是新功能，是原有缺口）

调查阶段发现，不修就会在切 `PLATFORM_AUTH_MODE=required` 时坏掉：

1. **中间件免鉴权清单不含接入端点**（`main.py:297`）：`/api/devices/register` 与 `/api/agents/register`
   在接入流程里本来就不带人类令牌（凭据是配对码 + Ed25519 签名），required 模式下会被中间件拦成 401 →
   **新设备再也接不进来**。已加入豁免并加注释与回归测试。
2. **协作 WebSocket `/ws/projects/{id}` 完全没有身份校验**：浏览器无法给 WS 设请求头，故用 `?token=`
   查询参数校验会话与项目查看权（required 模式强制；development 模式保持免令牌以便本机演示）。
   nginx 对该路径关闭访问日志，避免令牌落盘。
3. **`sessions.token` 明文入库且无法登出**：改为只存 SHA-256（与设备令牌一致），新增登出与会话撤销，
   改密/重置会撤销该成员的其它会话。
4. **设备注册的归属校验**（`store.py:1434`）：接入脚本登记的 Agent 归属是开发期种子账号 `member-001`，
   而配对创建者是真实账号 → 必然 `device_agent_owner_mismatch`。规则改为
   **"归属账号已停用/不存在时，由生成配对的人接管"**（在职归属仍拒绝，防冒名接管）。
   第一版判据写成"归属 == member-001 就接管"，被既有测试 `test_device_pairings_require_...` 拦住——
   测试是对的，判据改成了以"归属账号是否还有行为能力"为准。

## 3. 后端改动

**迁移 `017_accounts.sql`**（PG；SQLite 侧用既有的 `_ensure_columns` 给老库补列，并清理历史明文会话）：

- `human_members` 增列：`password_hash`（`scrypt$n$r$p$salt$digest` 自描述串）、`password_updated_at`、
  `last_login_at`、`is_admin`
- `sessions.token` 语义改为存 SHA-256；`DELETE FROM sessions` 清掉历史明文行

**新增 `apps/api/app/accounts.py`**：`hashlib.scrypt`（n=2¹⁴, r=8, p=1）口令哈希 + `hmac.compare_digest`
校验、口令策略（≥10 位、不可纯数字/纯字母）、会话令牌 SHA-256、一次性临时口令、**登入节流**
（进程内按「邮箱 + 客户端 IP」计数，5 次失败锁 60 秒并指数退避到 15 分钟；多实例部署需换共享存储，已记边界）。

**端点**：

| 端点 | 说明 |
| --- | --- |
| `POST /api/auth/register` | 邀请码注册；**首个注册者免码**并成为管理员，同时一次性接管 `member-001` 名下数据 |
| `POST /api/auth/login` | 邮箱口令登录；失败统一返回 `invalid_credentials`（不暴露邮箱是否存在）；超限 429 + `Retry-After` |
| `POST /api/auth/logout` | 删除当前会话 |
| `GET /api/auth/me` | 当前账号（含 `is_admin`） |
| `POST /api/auth/password` | 改自己的密码；保留当前会话，撤销其它会话 |
| `POST /api/auth/dev/session` | 开发入口，**required 模式下 403** |
| `GET /api/accounts` · `PATCH /api/accounts/{id}` · `POST /api/accounts/{id}/reset-password` | 管理员：列表 / 停用启用与改管理员 / 重置密码（返回一次性口令） |
| `GET /api/invitations` · `POST /api/invitations` | 邀请码列表与创建（已有账号后仅管理员可用） |

**两项默认行为**（写进代码注释与文档，属可调整的产品口径）：

- **受邀成员自动加入组织内现有项目**（角色取自邀请码）——否则新队友登录后项目列表是空的
  （列表按项目成员关系过滤）。项目级成员管理界面（增删成员）尚未做，是已知边界。
- 管理员不能停用/降级自己，且系统里必须保留至少一个在职管理员（`cannot_suspend_self` /
  `cannot_demote_self` / `last_admin_cannot_be_demoted`）。

## 4. 前端改动

- **`lib/auth.tsx`**（新）：AuthProvider，令牌存 localStorage（`map.sessionToken`），
  挂载后恢复并向 `/api/auth/me` 校验一次；401 统一回未登录态。
- **`lib/api.ts`**：新增 `apiFetch` 统一注入 `Authorization`、401 统一处理、`projectSocketUrl`（WS 带令牌）、
  账号 API 与错误码中文映射；**73 处 fetch 全部改走 `apiFetch`**，
  并把之前绕过 api.ts 的 5 处直连 fetch（drive 转换、ask 直连 LLM、settings 三项读接口）收敛进来——
  否则登录后这些请求会缺令牌。
- **成果物下载**从 `<a href>` 改成带令牌取回后触发保存（普通跳转不带 Authorization，强制鉴权下会 401；
  令牌也不进 URL/历史/日志）。
- **新页面 3 个**：`/login`、`/register`（支持 `?code=` 邀请链接自动填码）、`/account`
  （改密码 + 账号信息；管理员多出账号管理与邀请码面板）。不用 `useSearchParams`（会破坏静态预渲染，
  与 timeline 页当初的处理一致）。
- **`components/shell.tsx`**：登录/注册页不套工作台外壳（否则会先渲染侧栏并打出一串 401）；
  未登录（且会话状态已确认）跳 `/login?next=`；顶栏头像接真实用户 + 下拉菜单（账号设置/退出登录）；
  **侧栏/抽屉底部新增账号入口**——手机上顶栏头像在 ≤760px 是隐藏的，没有这个入口队友在手机上无法退出登录。
- **`lib/workspace.tsx`**：未登录不发业务请求；项目 WS 用 `projectSocketUrl`。

## 5. 验收

**后端**：新增 `apps/api/test_accounts.py` **28 项**（口令哈希与策略、节流锁定与过期、注册/邀请码四种失败、
首个管理员接管存量数据、**受邀成员加入现有项目**、会话只存哈希、登出、过期自动清理、改密保留当前会话、
登录失败同码、停用账号不能登录、管理员能力与自锁保护、`required` 模式下接入端点放行且普通端点 401、
开发会话入口关闭、协作 WS 无令牌 4403 / 坏令牌 4401、邀请码仅管理员可发）。全量 **358 项通过**（14 skipped）。

**前端**：构建 **22 页**（19 + login/register/account）；SSR 不变量（16 路由 × 16 导航入口 + 5 分组按钮 +
移动端标签栏 + viewport-fit）通过。

**本地实机（浏览器，API 以 `PLATFORM_AUTH_MODE=required` 运行，即生产配置）**：
未登录访问 `/` → 自动跳 `/login` 且不渲染工作台外壳 → 注册首个账号（免码）→ 自动登录、头像显示真实用户、
侧栏出现被接管的 16 个项目 → 账号页显示管理员面板与 2 个账号行 → 生成邀请码 → 退出登录（账号菜单）→
用错误密码登录得到「邮箱或密码不正确」→ 正确密码登录成功 → 用邀请链接注册队友（邀请码自动预填）→
队友账号页**无管理面板**、角色显示「成员」、能看到队伍项目 → 手机 320px：登录页无横向溢出、
抽屉里有账号入口「队友小王」与「退出登录」并能成功登出。

**线上（`https://synapforge.top`）**：域名入口无 Basic 弹窗、`/` 200、`/api/projects` 无会话 401、
`/login` 200；接入端点 `POST /api/agents/register` 与 `POST /api/devices/register` 返回**平台的 422**
（证明中间件放行，required 模式没弄断接入）；IP 入口仍是 401/Basic 200；浏览器实机（390px）打开域名 →
跳登录页、资源全部经 https 加载（无混合内容）、提交错误口令显示平台的中文错误。

## 6. 尚未完成与边界

1. **首位管理员的注册必须由使用者本人完成**——首个账号自动为管理员并接管现有数据，所以部署后第一步是
   用你自己的邮箱注册（见 `docs/SERVER_DEPLOYMENT.md` 的账号章节）。我刻意没有代建账号。
2. **没有邮件**：无 SMTP，忘记密码由管理员重置（返回一次性临时口令），注册靠邀请码。
3. 不做 OIDC/SSO、两步验证、多组织；会话是**服务端可撤销的 Bearer**，浏览器存 localStorage。
4. 项目级成员管理（把某人加入/移出某个项目、改项目内角色）**没有界面**，只有"受邀即加入现有项目"的默认；
   新项目创建后邀请进来的老成员不会被自动加入（需要后续做成员管理界面或调整默认）。
5. 登入节流是**进程内**的（单实例部署足够）；多实例要换共享存储。
6. `member-001` 保留为 `suspended`（可审计），不是删除。

## 7. 复现命令

```bash
# 后端（要求模式，等同生产配置）
cd apps/api
PLATFORM_AUTH_MODE=required PYTHONPATH="<repo>;<repo>/apps/api" python -X utf8 -m uvicorn app.main:app --port 8000
# 全量测试
PYTHONPATH="<repo>;<repo>/apps/api" python -X utf8 -m unittest discover -s . -p "test_*.py"

# 前端
cd apps/web && npm run build && node ./node_modules/next/dist/bin/next start -p 3000

# 首次使用：浏览器打开 http://127.0.0.1:3000/ → 自动跳 /login → 「用邀请码注册」→ 直接注册（首个账号免码）
# 之后：账号页生成邀请码 → 把邀请链接发给队友
```