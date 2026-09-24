# 服务器部署手册（单机自托管）

> 状态：`PLAN → 执行中`（脚本已就绪并本地干跑通过，等待目标服务器地址）
>
> 路线：**原生部署（systemd）**，不是 Docker Compose
>
> 相关：`docs/deploy/` 下四个脚本；`infra/README.md`（Docker 路线）

## 0. 为什么不是 Docker，也不是 PostgreSQL

这两条都是摸过代码后改的主意，不是省事：

1. **平台 API 实际不使用 PostgreSQL**。`apps/api/app/main.py:122` 是
   `store = Store(BASE_DIR / "data" / "platform.db", object_store=create_object_store(BASE_DIR / "data" / "objects"))`，
   全仓搜 `DATABASE_URL` 只有 `infra/docker-compose.yml` 和 `docs/POSTGRES_REPOSITORY.md` 提到它，
   **应用进程从来不读**。`PostgresRepository` 是给契约测试和特定脚本用的另一条链路。
   → 服务器上可以不装 PostgreSQL：少 200MB+ 内存、少一套备份/调参/权限对象。
2. **对象存储默认本地目录**（`OBJECT_STORE_BACKEND=local`，`apps/api/app/object_store.py`），
   所以 MinIO 也不是必需；要换成 MinIO/S3 时再设 `OBJECT_STORE_BACKEND=s3` + `S3_BUCKET` / `S3_ENDPOINT_URL`。
3. **Docker 路线的两个实际障碍**（本机实测）：本机没有 Docker/WSL，无法本地构建 linux/amd64 镜像；
   而在 4G 服务器上构建前端镜像，`next build` 实测峰值 **2764MB**，叠加已运行的容器基本是赌 OOM。
   原生部署同样要在服务器上构建一次前端，但可以用 swap + 限堆解决（见 §2），且不必再维护镜像层。
   `.dockerignore` 已补（仓库根），Docker 路线以后想走也不会再把 3.4GB 上下文和 Windows 的
   `node_modules` 送进 Linux 镜像。

**仍然需要的组件**：Python ≥3.11（代码用了 `datetime.UTC`）、Node 20（Next 15）、nginx（统一入口 + 鉴权）。

## 1. 服务器要求

| 项 | 建议 | 说明 |
| --- | --- | --- |
| CPU | 2–4 核 | 平台本身很轻，重活（Codex 执行、文件读写）都在成员机器上 |
| 内存 | 4GB + **4GB swap** | 稳定运行只需约 550MB（API 62 / Web 117 / 其余系统）；swap 是给 `next build` 兜底的，必须有 |
| 磁盘 | 50GB | 系统+依赖约 10GB，源码 1MB，node_modules 约 450MB，数据（SQLite + 对象目录）随成果物增长 |
| 端口 | 22、80 | 8000/3000 只监听 127.0.0.1，对外只走 nginx；**云安全组要放行 80**，否则外网打不开 |
| 系统 | Ubuntu 22.04 / 24.04 x86_64 | 其他发行版需要改 `server_bootstrap.sh` 的包管理部分 |

## 2. 一次性初始化

```bash
# 在服务器上（root）
bash server_bootstrap.sh --origin http://<服务器IP或域名>
```

做四件事（幂等，可重复跑）：

1. **依赖**：apt 装 Python/venv/sqlite3/nginx/htpasswd；Python < 3.11 时自动加 deadsnakes 装 3.12；
   Node 20 用**官方二进制**装到 `/usr/local/lib/nodejs`（不引第三方源）。
2. **swap**：默认 4GB（`--swap-gb` 可改），写进 `/etc/fstab`，`vm.swappiness=20`。
3. **账号与目录**：用户 `map`；`/opt/math-agent-platform`（应用）、`/var/log/math-agent-platform`（日志）、
   `/etc/math-agent-platform`（配置与凭据）。
4. **进程与入口**：
   - `map-api.service`：`uvicorn app.main:app`，**单 worker**（SQLite Store 是单连接串行化的，多 worker 会争锁），
     监听 `127.0.0.1:8000`，`EnvironmentFile=/etc/math-agent-platform/api.env`；
   - `map-web.service`：`next start -p 3000 -H 127.0.0.1`；
   - nginx：80 端口统一入口，`/api/` → 8000（Basic 鉴权 + Bearer 直通）、`/` → 3000（Basic 鉴权）、
     `/ws/` → 8000（WebSocket，不套 Basic）。

`api.env` 里已经替你设好的关键项：

```ini
PLATFORM_CORS_ORIGINS=http://<你的地址>          # 漏了的表现：页面能开、数据全空、控制台 Failed to fetch
PLATFORM_AUTH_MODE=development                   # 见 §4
PLATFORM_QUOTA_MAX_STORAGE_BYTES_PER_PROJECT=2147483648   # 代码默认 5GB，50G 盘上十个项目就爆，这里压到 2GB
```

## 3. 发布与升级

```bash
# ① 本机（Windows）打包源码：排除 3.4GB 构建产物与本机数据，只留源码（约 1.1MB）
bash scripts/deploy/pack-source.sh
# ② 上传（用 scripts/deploy/remote.py，或你自己的 scp）
python scripts/deploy/remote.py put --host <IP> --user root --password-file .secret \
    --src dist-deploy/math-agent-platform-src.tar.gz --dst /tmp/map-src.tar.gz
# ③ 服务器上发布
bash server_release.sh --tarball /tmp/map-src.tar.gz --public-url http://<服务器IP>
```

`server_release.sh` 依次做：解包（**不碰** `apps/api/data`，运行数据在源码包外）→ venv + `pip install -r requirements.txt`
→ `npm ci` → `next build --no-lint`（`NODE_OPTIONS=--max-old-space-size=1536`）→ 重启两个服务 → 调 `server_verify.sh` 自检。

**升级**：改完代码重新打包 → 上传 → 重跑 release，数据目录不受影响。
**回滚**：保留上一版 tarball，重跑 release 指向它即可（源码是幂等覆盖）。

## 4. 鉴权与安全边界（重要）

平台当前**没有真正的登录**（OIDC 还没做，见 `docs/IMPLEMENTATION_STATUS.md` 待办）。
`PLATFORM_AUTH_MODE=development` 时，浏览器请求没有 Bearer 令牌就一律当作 `member-001` ——
**只要有人能打开网址，就等于拿到了这个平台的全部操作权**（包括读取成员配置里已脱敏的 LLM/Embedding/MinerU 凭据、
批准门禁、生成提交包）。所以部署必须补一层访问控制，本次用的是 nginx Basic 认证：

- 浏览器：先弹一次 Basic 认证，之后同源请求浏览器自动带上；
- **nginx 会把 Basic 头摘掉再转发**（`map $http_authorization $map_upstream_auth`），
  因为平台会拒绝非 `Bearer` 的 `Authorization` 头（`invalid_authorization_header`）；
- Agent 的两种凭据都跳过 Basic 并原样透传，由平台自己判令牌：
  `Authorization: Bearer dvc_…`（设备令牌）与 **`X-Project-Capability-Token: prj_…`**（项目能力令牌，
  claim / progress / result / 运行创建 / 成果物上传都走这个自定义头，不是 Bearer——漏了它会让整条任务循环 401）；
- 接入流程两个"凭据即令牌"的端点免 Basic：`/api/devices/register`（配对码 + 私钥签名，15 分钟一次性）、
  `/api/agents/register`（老轨幂等登记，不发放凭据）；
- `/ws/`（设备网关 + 文档协作中继）：不套 Basic，由协议层令牌鉴权。

> 实现细节：两个 `map` 先把"是否带 Agent 凭据"折算成 `$map_bearer_flag` / `$map_capability_flag`，
> 在 location 里 `set $map_agent_credential "$map_bearer_flag$map_capability_flag"`，
> 再用第三个 `map` 把 `~1` 映射成 `auth_basic off`。不这么绕是因为 nginx 的 `map` 只能有一个源变量。

凭据在 `/etc/math-agent-platform/credentials.txt`（600）。改密码：

```bash
htpasswd -b /etc/nginx/.math-agent-platform.htpasswd <用户> <新密码> && systemctl reload nginx
```

**残余风险（明确记录）**：`/ws/projects/{project_id}` 协作中继不需要 Basic，知道项目 UUID 的人可以订阅该项目的协作流。
要收紧：把 `/ws/` 也纳入 Basic（代价是浏览器协作面板可能握手失败，需要实测），或等真正的认证上线。

**这套鉴权写法已实测**（本机用 nginx 1.26.2 + 彩排实例按同一份配置逐项打过）：

| 探针 | 期望 | 实测 |
| --- | --- | --- |
| `GET /api/projects` 无认证 | nginx 401 | 401（nginx 的 HTML 页） |
| `GET /api/projects` 正确 Basic | 200 JSON | 200，返回项目列表 |
| `GET /api/projects` 错误 Basic | nginx 401 | 401 |
| `GET /api/agent/me` 带 Bearer | 平台自己的 401 JSON | 401 `{"detail":"device_token_invalid"}` |
| `GET /` 无认证 | nginx 401 | 401 |
| `GET /ws/probe` 无认证 | 上游 404（未被 Basic 拦） | 404 `{"detail":"Not Found"}` |

实测中踩到并已规避的坑：`auth_basic_user_file` 的**相对路径是相对 nginx.conf 所在目录**解析的
（写成 `conf/htpasswd` 会去找 `conf/conf/htpasswd`，表现为认证请求 500）。生成脚本里用的是绝对路径
`/etc/nginx/.math-agent-platform.htpasswd`，不受影响。

**后续该做的**：① 有域名后上 TLS（`certbot --nginx`），把 80 跳 443；
② 把 `PLATFORM_AUTH_MODE` 切到 `required` 之前必须先有登录入口（现在切了工作台会白屏）；
③ 云安全组只放行 22/80/443。

## 5. 数据、备份与恢复

数据全在这两个地方（`BASE_DIR = apps/api`）：

```
/opt/math-agent-platform/apps/api/data/platform.db        # SQLite：项目/任务/运行/成果物/事件/门禁
/opt/math-agent-platform/apps/api/data/objects/           # 成果物内容（本地对象存储）
```

**备份**（不停机，SQLite 用 `.backup` 保证一致性）：

```bash
DEST=/opt/backups/math-agent-platform-$(date +%F)
mkdir -p "$DEST"
sqlite3 /opt/math-agent-platform/apps/api/data/platform.db ".backup '$DEST/platform.db'"
tar -C /opt/math-agent-platform/apps/api/data -czf "$DEST/objects.tar.gz" objects
```

**恢复**：停服务 → 用备份覆盖 `platform.db` 与 `objects/` → `chown -R map:map` → 起服务。

**首次启动的种子项目**：`Store._seed()`（`store.py:814`）在库为空时会写入一个演示项目
「C题 · 风光储能协同优化」（含 5 个任务/成果物）。这是开发期的设计，不是故障。
不想要它的话，在**首次启动之后、投入使用之前**清掉：

```bash
systemctl stop map-api
sqlite3 /opt/math-agent-platform/apps/api/data/platform.db \
  "DELETE FROM artifacts WHERE project_id IN (SELECT id FROM projects WHERE name LIKE 'C题 ·%');
   DELETE FROM tasks     WHERE project_id IN (SELECT id FROM projects WHERE name LIKE 'C题 ·%');
   DELETE FROM project_memberships WHERE project_id IN (SELECT id FROM projects WHERE name LIKE 'C题 ·%');
   DELETE FROM projects  WHERE name LIKE 'C题 ·%';"
systemctl start map-api
```

（平台没有「删除项目」接口，所以只能这样清；要我做成"启动时不种子"的开关就需要改代码，属于新决策。）

## 6. 排障对照表

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| 页面能开、数据全空、控制台 `Failed to fetch` | CORS 白名单没包含工作台来源 | 改 `api.env` 的 `PLATFORM_CORS_ORIGINS` → `systemctl restart map-api` |
| 外网打不开、服务器本机 curl 正常 | 云安全组没放行 80 | 控制台放行 80（脚本只能改 ufw） |
| `next build` 被 Killed / OOM | 没有 swap 或堆没压住 | 确认 `swapon --show` 有 4GB；`NODE_OPTIONS=--max-old-space-size=1536` 已在 release 脚本里 |
| 上传大文件 413 | nginx 默认 1MB 限制 | 已在站点配置设 `client_max_body_size 512m` |
| 浏览器一直弹认证 / Agent 401 | Authorization 透传规则 | 见 §4；`curl -H 'Authorization: Bearer x' /api/agent/me` 应返回平台的 401 而不是 nginx 的 401 页 |
| 任务不被领取 | 没有在线 Agent（平台是拉取模型，没有"开始"按钮） | 在 `/devices` 生成配对 → 在成员机器跑接入命令 → 授权到项目 |
| 交付编译报 `latex engine missing` | 服务器没装 TeX | 需要编译就装 `tectonic`（约 50MB，按需拉宏包），见 §7 |

日志：`/var/log/math-agent-platform/{api,web}.log`、`journalctl -u map-api -n 100`、nginx 的 `/var/log/nginx/error.log`。

## 7. 可选：让服务器能编译论文 PDF

交付编译在 API 进程里跑（`apps/api/app/latex_compile.py` 找 `xelatex` / `pdflatex` / `tectonic`）。
不装引擎时该功能会 fail-closed 明确报错，不影响其它功能。要装的话**别装 texlive-full**（2–4GB），
用 tectonic 最省（4G/50G 机器友好）：

```bash
apt-get install -y tectonic    # 或从 GitHub Releases 下载单文件二进制
```

中文论文需要 `ctex`/`xeCJK` 与中文字体，首次编译会按需拉取宏包（需要出网 + 几百 MB 缓存）。

## 8. 与 Docker 路线的关系

`infra/docker-compose.yml`（禁区文件，未改动）依然可用，前提是：能本地构建 linux/amd64 镜像、
或愿意在 4G 服务器上冒一次 OOM 风险构建。本仓库根新增了 `.dockerignore`，
把 `node_modules`/`.next`/`.infra`/`dist-sidecar`/`apps/desktop/dist`/`apps/api/data` 等排除，
避免 3.4GB 构建上下文和"Windows 的 node_modules 被 COPY 进 Linux 镜像"这个真实缺陷。

## 8. 账号与访问控制（AUTH-1，2026-09-17 上线）

平台现在有真正的账号体系：**邀请码注册 + 邮箱口令登录 + 会话令牌（服务端可撤销）**。
`PLATFORM_AUTH_MODE=required`，域名入口不再套 nginx Basic，由平台自己把关。

| 入口 | 访问方式 | 用途 |
| --- | --- | --- |
| `https://synapforge.top` | **账号登录**（无会话的 API 调用一律 401；页面自动跳 `/login`） | 日常使用 |
| `http://156.239.229.143` | **nginx Basic**（`map` / 见 `/etc/math-agent-platform/credentials.txt`）+ 账号登录 | 应急后门：Basic 失效或前端故障时仍能进 API 与页面 |

**首次使用必须由你本人注册第一个账号**（我刻意没有代建）：第一个注册的账号自动成为管理员，
并把此前 `member-001` 名下的项目/任务/成果物/设备一次性接管过来（`member-001` 置为 suspended 保留）。
注册页不需要邀请码；**之后再注册就必须有管理员发的邀请码**。

日常操作：

```
管理员：登录 → 右上角头像 → 账号设置
  · 生成邀请码（填队友邮箱，把邀请链接发给他；每个码只能用一次）→ 队友打开链接注册即入队
  · 停用 / 启用账号、设为 / 取消管理员、重置密码（返回一次性临时口令，请用可靠渠道转达）
任何账号：账号设置 → 修改密码（会撤销其它设备上的登录，本机保持在线）
```

手机上没有顶栏头像，账号入口在**抽屉底部**（底部标签栏「更多」→ 侧栏底部「账号设置 / 退出登录」）。

排障对照（账号相关）：

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| 页面能开、数据全空且被跳回登录页 | 会话失效（被停用、改密、超过 30 天） | 重新登录；必要时管理员重置密码 |
| 登录提示「尝试次数过多」 | 登入节流：同邮箱 + IP 连续 5 次失败锁 60 秒，指数退避到 15 分钟 | 等待，或换正确口令 |
| 队友注册后看不到项目 | 受邀成员会自动加入**注册当时**已存在的项目；此后新建的项目不会自动加入（项目级成员管理界面尚未做） | 让管理员把他加入项目（当前需走 API `POST /api/projects/{id}/members`） |
| 忘记管理员口令且无人可重置 | 应急通道：`http://IP` 的 Basic 后门只解决 nginx 层；平台口令需直接用 sqlite3 清空该账号 `password_hash`（随后它成为"无口令不可登录"） | 用 `sqlite3 platform.db "UPDATE human_members SET password_hash=NULL WHERE email='…'"`，再让该账号走"首个可注册"路径不适用——改由另一个管理员重置 |

**已知边界**：无 SMTP（没有自助找回密码、没有邮件验证）；登入节流是进程内的（多实例需换共享存储）；
项目级成员管理没有界面；不做 OIDC/2FA。

**自检脚本的账号面断言**：`server_verify.sh` 会先读 `/health` 的 `mode` 再决定断言——
`required` 模式下"无会话访问成员级端点"期望的就是 **401**（那是设计行为），
并额外检查注册接口可达、匿名组织/团队/成员端点已收口、以及"Agent 能力令牌能穿过中间件"。
想连会话内断言一起跑（`/api/projects`、`/api/tasks/mine` 应为 200）：

```bash
MAP_VERIFY_EMAIL=you@example.com MAP_VERIFY_PASSWORD=<口令> bash /root/deploy/server_verify.sh
```

**派单与个人任务中心（DISPATCH-1）**：任务可「指派给」某个项目成员——**只有该成员名下的设备能领取**；
不指派则保持"谁先轮到谁跑"。被派的人在工作台「我的任务」（`/my-tasks`）里能看到三组视图：
指派给我的 / 我的 Agent 正在跑 / 我的 Agent 最近完成。执行体归属由**接入时的配对**决定：
谁在那台机器上完成配对，这台机器就算谁的 Agent（因此配对应由设备所有者本人做）。
复核口径：**允许**批准自己 Agent 的产出（不做职责分离约束）；项目内草稿/待审内容对**所有**项目成员可见。

**团队与成员（TEAM-1）**：工作台新增「团队与成员」（`/team`）——成员工作量（谁在忙什么）、
当前项目的成员管理（改角色 / 移出）、团队（建队、加人）。要点：

- **移出项目成员会连带撤销他在这个项目的设备/Agent 授权**，并释放派给他但未完成的任务
  （只删成员关系的话，他的机器还会继续领任务——那是个洞）；最后一个项目负责人不可被移出。
- **新建项目自动带上组织内在职成员**（创建者是项目负责人），避免"建完项目队友看不见"。
- **加入团队 = 自动加入该团队的所有项目**；移出团队同理连带移出。项目可在新建时选择归属团队。
- 组织默认是开发种子组织，可用 `PLATFORM_ORG_ID` / `PLATFORM_ORG_NAME` 指到别的组织行；
  **一个部署 = 一个组织**（多租户隔离未做，跨组织隔离需要先把 RLS 落到运行时）。
- 任务可设截止时间：**过期任务不会被自动领取**（需要改期或取消），队列排序"有截止时间的优先"；
  任务可声明 `required_capabilities`，**不满足的 Agent 领不到**（能力来自 Agent 自报 tools ∪ 设备声明 ∪ 心跳）。

## 9. 部署记录（2026-09-17 实测）

目标机：`156.239.229.143`（Ubuntu 24.04.1 / 4 vCPU / 3915MB 内存 / **根分区 39G 而非 50G**，36G 可用）。

实际执行顺序（全部由本机经 `scripts/deploy/remote.py` 驱动）：

| 步骤 | 结果 |
| --- | --- |
| `remote.py check` | Python 3.12.3 自带、无 node/nginx/pip/swap、仅 sshd 在监听（无既有站点） |
| `server_bootstrap.sh --origin http://156.239.229.143` | 依赖 + Node 20 + 4GB swap + `map` 用户 + 两个 systemd 单元 + nginx 就绪 |
| `pack-source.sh` → 上传 1.1MB 包 | 378 条目，服务器 `tar -xzf` 展开 |
| `server_release.sh --public-url http://156.239.229.143` | **前端在服务器上构建成功（19/19 静态页）**——`NODE_OPTIONS=--max-old-space-size=1536` + 4GB swap 把 2.7GB 的构建峰值压住了，swap 实际只用到 0B |
| `server_verify.sh` | **16 项全过** |
| 端到端冒烟 | 见下 |

**端到端冒烟（真实链路，不是桩）**：

1. 经公网入口生成配对（`POST /api/devices/pairings`，Basic 认证）→ 本机跑
   `scripts/connect-agent.ps1 -Url http://156.239.229.143 -Pairing <blob>` → Agent 登记 + Ed25519 签名 +
   `POST /api/devices/register` **201** + 设备 Token 写入 Windows 凭据管理器；
2. `agentd.py gateway-run` 连 `ws://156.239.229.143/ws/agents/device-fallrain` → 平台侧设备状态 **active**、
   心跳持续（**证明 WebSocket 经 nginx 的 Upgrade 代理可用**）；
3. 建声明式命令任务（`worker_command: ["cmd","/c","echo","deployment-smoke-ok"]`）→
   授权设备到项目 → `agentd.py worker-run --once --grant <blob>` →
   领取 → 执行 → 回报：任务进 `WAITING_REVIEW`，运行 `SUCCEEDED`，stdout = `deployment-smoke-ok`，
   事件流（task.created / task.claimed / task.progress / run.created）齐全；
4. 清理：撤销设备与项目授权、删除探针 Agent、把误领的种子任务还原为 READY、删除本机那份已失效的
   项目凭据条目。

**部署过程中修掉的两个真缺陷**（都已回灌到脚本）：

1. **Basic 密码文件权限**：`chmod 640 root:root` 让 nginx worker（`www-data`）读不到，
   所有认证请求返回 **500**（nginx 日志 `open() ... failed (13: Permission denied)`）。
   改为 `chown root:www-data` + `640`。
2. **Agent 凭据头漏判**：第一版只按 `Authorization: Bearer` 分流，而任务循环用的是
   `X-Project-Capability-Token` 自定义头 —— 结果是"能接入、能连 WS、但一 claim 就 401"。
   改为"两种凭据任一存在即跳过 Basic，由平台判令牌"。

另外两处非缺陷但需知道：

- `connect-agent.ps1` 第 3 步会打印一条 **401 警告**（它在预检"本机已有设备"时调 `GET /api/devices` 却没有成员令牌），
  脚本自己降级继续，**不影响接入**。放行它等于放开成员级设备列表，所以刻意不放。
- 服务器上线后几小时内就有互联网扫描器敲门（`GET /` 被 Basic 挡成 401）。

## 10. 增量发布记录

| 日期 | 内容 | 结果 |
| --- | --- | --- |
| 2026-09-17 | P3（成员审计/项目归队/能力目录/工时趋势/组织收口） | `server_verify.sh` 22/22 |
| 2026-09-22 | **W-1 项目工作区**（迁移 020、`/api/projects/{id}/workspace|messages`、事件→卡片桥 + WS 实时推送、`/workspace` 单页四 Tab） | `server_verify.sh` **22/22**；域名 `/workspace` → 200；启动回填 2 个项目共 29 张卡片；发布后就地验证（临时 300 秒会话，用完即删）确认概览 `viewer`/成员/Agent 正确、`POST /messages` 201 |
| 2026-09-22 | **W-2 任务板与成果空间**（`POST /api/projects/{id}/tasks/assign`、`GET /api/projects/{id}/deliverables`、`task.dispatched` 事件与卡片、任务板与成果空间两个 Tab） | `server_verify.sh` **22/22**；发布后就地验证：`/deliverables` 五段结构完整、`/tasks/assign` 非法目标 → 409 `assignee_not_project_member`、任务不存在 → `failures=[task_not_found]`，**0 新增事件/卡片**（验证只走「必然被拒」的路径，不动线上数据） |
| 2026-09-22 | **W-3 全自动调度器**（`task_mode=auto`：能力匹配自动派单、派不出去时的一次性提示、三个驱动入口） | `server_verify.sh` **22/22**；发布后就地验证：`auto_dispatch_tick` 可调用、线上项目均为 manual → tick 无动作、**事件 46→46 / 消息 29→29（零副作用）**、map-api / map-web / nginx 均 active |
| 2026-09-22 | **W-4 导航收敛**（侧栏 6 组，「高级工具」收编 8 个深页；纯前端） | `server_verify.sh` **22/22**；域名 `/workspace` 200 且 SSR 含 `nav-group-tools`；SSR 不变量 19×19×6 |
| 2026-09-22 | **Basic 口令轮换**（用户要求「简单一些」）：`<旧口令已脱敏>` → `<Basic 口令已脱敏>` | 实测 `map:<Basic 口令已脱敏>` → 200、旧口令 → 401、无认证 → 401；`server_verify.sh` 22/22（脚本从 `credentials.txt` 读凭据）；备份：`/etc/math-agent-platform/credentials.txt.bak-20260922`、`/root/.math-agent-platform.htpasswd.bak-20260922` |
| 2026-09-22 | **W-5 黑白灰配色**（强调色近黑、去蓝紫渐变与彩色底、状态改「中性底 + 语义色圆点」；纯 CSS） | `server_verify.sh` **22/22**；线上 CSS 实测 `--accent:#171717` / `--bg:#fafafa` / `--sidebar:#111111` 就位，`#2563eb` / `#6d4aff` / `#4f46e5` / `#c7d7fb` / `#ddd6ff` 残留计数全为 0 |
| 2026-09-22 | **W-6 工作区铺满 + 输入框附件/提及**（页面补 `.page-content`、内容区改为自己滚、聊天 Tab 左栏撑满且右栏等高；新增上传文件与 @成员 / /Agent 提及；消息可带成果物引用） | `server_verify.sh` **22/22**；桌面实测左右栏各 611px、底边对齐、页面不滚动；手机 390px 无横向溢出；线上 CSS 含 `.workspace-grid.is-fill` 与 `.mention-pop` |
| 2026-09-22 | **W-7 四项体验修正**（引导双路径、新建任务弹窗、消息卡片收紧、/ask 重构为 ChatGPT 式） | `server_verify.sh` **22/22**；线上 CSS 含 `.ask-grid`/`.ask-messages` 与 `.chat-line`；浏览器实测四项闭环 |
| 2026-09-22 | **W-8 品牌+开放注册+演示**（synapforge、免码注册即用（`PLATFORM_OPEN_REGISTRATION` 默认开）、ask 侧栏美化、新用户引导、多 Agent 文档撰写演示项目） | `server_verify.sh` **22/22**；公网免码注册 → **201+令牌**；演示项目已种（4 任务/4 成果物/42 条群聊，归属首位管理员）；探针账号验证后已彻底清理 |
| 2026-09-22 | **W-9 桌面端 0.1.1 + 下载入口 + 通用 CLI 执行体**（壳改名 synapforge、重建 sidecar/安装包；`/downloads/` 静态直出；`worker_executor=cli` 提示词驱动模板） | `server_verify.sh` **22/22**；`/downloads/synapforge-setup-0.1.1-x64.exe` → 200、143,811,038 字节、**sha256 与本地一致**；cli 策略 HTTP 实测（合法 200 / 缺占位符 409 / 未知执行体 409） |
| 2026-09-22 | **W-10 聊天命令 + 成果物抽屉 + /ask 改版**（`/task` 建任务、消息 `ref_task_id`、右侧成果物抽屉、/ask 左列会话布局；**修中文文件名下载 500**（RFC 5987）；演示重建为带正文版） | `server_verify.sh` **22/22**；`/ask`、`/workspace`、`/downloads/…exe` 均 200；线上成果物内容接口 200 + `filename*=UTF-8''…` 头；演示项目 4 任务/4 成果物（含正文）/42 条群聊 |
| 2026-09-22 | **W-11 对外口径**（用户可见文案不再出现「超图」/「Hyper-RAG」，统一说「图谱/索引」；纯文案） | `server_verify.sh` **22/22**；线上 `/graph` 无「超图」字样、`/kb` 无「Hyper-RAG」字样 |
| 2026-09-22 | **AIP-1 第 1 批：能力卡 · 候选推荐 · 身份两段**（迁移 `021_agent_description.sql`：`agents` + `capability_cards`/`package_id`/`instance_id`/`package_source`；新 `skill_match.py`（技能归一化 + 版本下限）；**授权范围从技能域拆出**（修误匹配）；`rank_task_candidates` 与 auto 调度器共用排序；新端点 `GET /api/tasks/{id}/candidates`；`/team` 技能卡与程序包聚合；任务板「推荐执行体」） | `server_verify.sh` **22/22**；线上 `/team` SSR 含新面板文案；公网免码注册**临时探针**读 `/api/team/capabilities`（6 执行体带版本技能）与 `/api/tasks/{id}/candidates`（6 候选带理由），**探针账号连同会话/成员关系一并删除**；`_aip1_verify.py` 走真实 store 路径四项全 PASS（版本下限、大小写归一化、推荐排序带样本量、调度器与推荐一致）并自清理临时任务；`_aip1_enrich_agents.py` 给 5 个**示例**执行体补能力卡（不动真实接入的机器） |

| 2026-09-22 | **AIP-1 第 2 批：意图对象**（迁移 `022_task_intent.sql`：`tasks` + `budget`/`evidence_requirements`；`max_seconds` 压租约且续租不越界、`max_attempts` 用尽拒绝领取 + 幂等告警、`max_tokens` 只记录；证据要求批准时校验并留门禁 finding；新端点 `GET /api/tasks/{id}`；`/tasks` 详情新增编辑区块） | `server_verify.sh` **22/22**；`_aip1d_verify.py` 线上四项全 PASS（租约 60s≤60s 且续租不越界 / 第二次领取被拒且事件恰好一条 / 缺口 + 批准被拒 + 门禁 `FAILED` 含 `task:evidence_requirements` / 清理后 0 残留）；线上包内 `tasks` chunk 含新编辑器；发布后线上库 `AIP1%` 任务 0、孤儿租约 0、`task.budget_exhausted` 事件 0。**发布插曲**：`Path.write_text` 把若干源码写成 CRLF，`pack-source.sh` 防呆 `SystemExit(1)` 拦下打包（当时 tarball 还是第 1 批的）→ 转 LF 后重打重传才发布 |

| 2026-09-23 | **COST-1 执行用量回报**（迁移 `023_run_usage.sql`：`runs.usage`；执行体 JSONL 用量 + 循环观测耗时；完成 Run 时超预算留一次性事件与聊天卡片，批准时门禁 blocking finding；`GET /api/projects/{id}/task-flags` 角标；`/runs`、`/tasks`、工作区任务板显示用量与缺口） | `server_verify.sh` **22/22**；`_cost1_verify.py` 线上 **13 项全 PASS**（用量推导/出处保留/一次性事件/聊天卡片/累计回显/批准被拒 + 门禁 `FAILED`/不报用量不判 0/清理 0 残留）；线上库 `COST1%` 任务 0、`budget_exceeded` 事件 0、`runs.usage` 列已建；`tasks` chunk 含 `task-overrun-` 与"已回报" |

| 2026-09-23 | **DESKTOP-GUI 桌面端 0.2.0**（工作台内嵌为应用窗口、本机 Agent 面板并入主窗口、系统通知/角标/开机自启/可配地址/深链 `map://task/<id>`/检查更新/拖拽上传；下载入口指向 0.2.0；`/downloads/latest.json` 版本清单） | `server_verify.sh` **22/22**；`latest.json` 与安装包线上字节数/sha256 一致（143,822,449 B / `b534cf2b…`）；**打包应用自检**（真实解包目录 + 线上站点 + 真实内核）`ok:true, panel_open:true, kernel_contract:v1`，退出码 0；用空状态目录复现并修复了"未配对时内核因中文日志在 cp1252 下崩溃"的首次运行 bug |

| 2026-09-23 | **DESKTOP-NOTIFY 桌面通知**（新端点 `GET /api/my-attention`：派给我的 + 我该复核的；桌面端后台每 60 秒检查并发系统通知、点击直达任务、待办进角标；前端交令牌 + 事件催拉；安装包 0.2.1） | `server_verify.sh` **22/22**；`/api/my-attention` 匿名 401；**打包 0.2.1 对生产**实测两条通知按预期出现（`[通知验证] 派给我的任务` / `[通知验证] 待我复核的任务`），`SELFCHECK ok:true, attention.ok:true`，退出码 0；同目录二次启动 0 通知（跨启动去重）；`latest.json` → 0.2.1（sha256 `4dab588f…`）；线上探针数据（2 账号 + 4 任务）已删净 |

| 2026-09-24 | **「我的智能体」对话体验：Markdown/公式渲染 · 模式位置 · 结构化选择卡**（`components/markdown.tsx` 接 `react-markdown`+`remark-gfm`+`remark-math`+`rehype-katex`；模式 tab 移到输入框下方；`lib/agent-choice.ts` 解析 `synapforge-choice` 卡片 → 可点选可提交的表单；**内核每轮提示词末尾注入协议**（`chat_loop.py` 的 `CHOICE_PROTOCOL_INSTRUCTION`，此前只是定义未接）；审题/建模/选题三角色补"何时出卡片"；修掉全局 `input{width:100%}` 把卡片 radio 拉成整行导致文字挤成一字一行） | `server_verify.sh` **22/22**（发布两次：功能 + 样式修复）；线上量取模式 tab 在输入框下方（输入 410 → tab 434）；真跑一轮：表格 1 / KaTeX 2 / 卡片 1；选完三题才可提交 → 提交后卡片锁「已提交选择」并回发 `用户已完成「…」的选择：…` → 续跑 1172 字并再出一张卡；刷新后历史完整。**执行体侧**：内核 + 12 个角色重装、`map-agent@cloud` active；遗留探针进程已清。**发布坑**：线上 `node_modules` 没有新前端依赖，`server_release.sh` 会跳过 `npm ci` → 必须先 `rm -rf apps/web/node_modules`；远端路径上传要 `MSYS_NO_PATHCONV=1` |

| 2026-09-24 | **「我的智能体」页面高度修正 + M-5c S-1 常驻 serve 通道**（前端：`#my-agent` 的 grid 行改成 `minmax(min-content, 1fr) auto`——原先是 `auto minmax(0,1fr)`，而页面唯一在流子元素是 shell，于是剩余高度全给了**空着的第二行**，视口越高下方空白越大；内核：`apps/agent/opencode_server.py` 常驻 `opencode serve` + 轮次走 v1 通道 + 失败自动降级 CLI，单元加 `--chat-server-port 4199`） | `server_verify.sh` **22/22**；线上 1040 高视口实测 shell 93→988（原来 93→551，下方 490px 空白）；执行体侧：真跑一轮 journal `走常驻服务通道（端口 4199）`→`完成（66 字，serve 通道）`、平台 `usage.source=opencode-serve`、`kill -9` 后自动重启并续上下文、`systemctl restart` 无孤儿进程。**排障记录**：v1 `POST /session` 带 `model` 会 400（模型只在发消息时带） |

| 2026-09-24 | **M-5c S-2：回答边生成边出现**（内核把 SSE 的 `message.part.delta` 按节奏推成 `delta` 事件——绕开 reporter 的节流/上限、封顶后收尾补发不丢字；页面拼 `agent.message`+`delta`、执行中 900ms 轮询、抽屉过滤增量、文案按事件显示"增量显示/分段显示"；**按 `part.updated` 的 part 类型过滤思考**，修掉第一版把英文思考混进气泡） | `server_verify.sh` **22/22**；线上实测：20s 时气泡已有 201 字中文正文（4 个 KaTeX 已渲染）、23s 完成（467 字 / 8546 tokens · opencode-serve）；刷新后完整；全库 `delta` 含英文思考条数 = 0；`process.exited` 带 `delta_events/unknown_deltas`。测试：`test_opencode_server.py` 19 项、Agent 全量 402 项 |

| 2026-09-24 | **「我的智能体」体验四改**（① 对话区固定高度：`#my-agent`/`.my-agent-layout` 的行都改成 `minmax(0,1fr)`——原来 `min-content` 下限让页面越聊越长、把顶部工具栏顶走；②③ 输入区仿 zcode 重做：卡片 + 底部控件行（+/角色/附件数/模型/发送）+「+」展开面板（附件/角色/执行体），图标 14→17/18；④ **产出文件**：内核快照差分上传成果物（无 Run、轮次级幂等键、`inputs/` 不算产出、失败不改轮次成败）+ 迁移 `026`（`agent_turns.outputs`）+ 页面「下载 / 转入云盘」+ 新端点 `POST /api/drive/from-artifact/{id}`） | `server_verify.sh` **22/22**（两次发布）；线上量取：`.main-area` 不滚、`.ask-messages` 内滚、工具栏 top=115 常驻；产出闭环实测（写 `notes/squares.md` → 页面 chip → 转入云盘 → 云盘 3→4 → 下载触发下载事件）；**哈希对账**：执行体文件与平台成果物 `content_hash` 同为 `b7473cb4…`、136 B、`PENDING_REVIEW`。**坑**：下载不能用 `<a href>`（普通跳转不带 Authorization，强制鉴权下 401，`lib/api.ts` 早有注释）；换角色后模型可能"接着上一条的拒绝"，协议层 `agent=mm-coding` 是真的换了 |

| 2026-09-24 | **M-5c S-3 权限卡片**（迁移 `027_agent_turn_approvals.sql` + 四个接口；内核把 `permission.asked` 组卡上报并**有界等待**人批，超时=不执行；页面待批准卡片「批准一次/本次都允许/拒绝」；顺带修"新会话继承上一条角色"与"被拒后 0 字回复无说明"） | `server_verify.sh` **22/22**（本轮三次发布）；**真机两端验收**：拒绝 → 卡片 `is-denied`、journal `→ reject（已回复：True）`、**执行体私有 tmp 里没有该文件**；批准（always）→ 文件存在、内容 `APPROVED`。**机关**：单元 `PrivateTmp=yes` ⇒ 执行体的 `/tmp` 是私有的（`/tmp/systemd-private-…-map-agent@cloud.service-…/tmp`），在宿主 `/tmp` 查文件会误判"没写出来" |

| 2026-09-24 | **M-5c S-4：思考块可见**（内核把 `reasoning` 段按 `thinking` 事件发出，绕开 reporter、封顶后补发不丢字；页面抽屉「思考过程」折叠块 + 「思考中」文案）+ **修中间列横向溢出**（`.my-agent-main` 与 `.panel` 缺 `min-width:0`：宽内容把列撑到 1353px、容器只有 708px，输入框与发送键跑出视口→"点不着打不进字"） | `server_verify.sh` **22/22**（两次发布）；真跑实测：224 字推理在抽屉可见、气泡只有答案、事件 `thinking×5/delta×4`、**全库回答混入思考 = 0**。**强度（`--variant`）按证据暂缓**：常驻通道收下 `model.variant` 但不生效（回读仍是 `variant:"default"`） |

| 2026-09-24 | **运维事件：设备授权过期导致对话通道中断（已处置）**（`device-cloud-01` 的三条项目授权都是 24h TTL，最后一条 04:38 到期 → 执行体每 5 秒 `401 device_project_token_expired`、页面新建会话报裸错误码 `device_project_grant_missing`） | 生成 **30 天**新授权（含 `chat.run` 全套 14 项能力）→ 执行体 `--grant-only` 应用 → 重启 → 恢复（设备 active、无 401、新建会话与真跑一轮均成功）；**设计缺口已记录**：默认 TTL 86400s 且到期无提醒/无续期提示，建议"授权可选有效期 + 列表显示到期时间 + 一键续期" |

发布 W-1 时同步更新了服务器上的 `/root/deploy/server_verify.sh`（tarball 只覆盖 `/opt` 下的副本，
`/root/deploy` 里的脚本要单独上传——**下次改自检脚本别忘了这一步**）。新增的两条会话内断言
（工作区概览结构 + 聊天流 200）在提供 `MAP_VERIFY_EMAIL/PASSWORD` 时才会执行。

**开放注册（2026-09-22 起默认开）**：`PLATFORM_OPEN_REGISTRATION` 不设或设 1 时，任何人填邮箱+密码即可注册
（contributor，自动加入组织内既有项目）；带回邀请码则按码上角色。公网部署不想公开内容时，
`systemctl edit map-api` 加 `Environment=PLATFORM_OPEN_REGISTRATION=0` 并重启即回到邀请码模式。

**留给使用者的收尾项**：① 首次启动的种子演示项目（含冒烟任务）按 §5 清库 SQL 处理；
② 有域名后上 TLS；③ ~~换掉自动生成的 Basic 密码~~（2026-09-22 已按用户要求改为 `<Basic 口令已脱敏>`）；④ 用向导给自己的机器配对（我这台测试设备已撤销）。