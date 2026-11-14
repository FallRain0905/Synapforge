# magpie 源码调研报告：本地 Agent 发现、接入与会话呈现

- 调研对象：`yetone/magpie`（MIT，Go）
- 调研基线：`e0889719c8cbb08a63edabc8d48727ec65e45e4c`（2026-11-14）
- 本地副本：`reference-repos/magpie-src`（`--depth 1`，267 MB）
- 规模：1,881 个非测试 `.go` 文件、约 57.1 万行非测试代码；`internal/agent` 189 个文件/67.5k 行，`internal/sessions` 116 个文件/35.6k 行，`internal/gui` 359 条 HTTP 路由
- 用途：为 `synapforge studio`（本地 Agent 集群工作台）提供参照与反向清单

---

## 0. 一句话结论

**magpie 不与任何 Agent 建立运行时连接。** 没有 IPC、没有 socket、没有协议握手、不注入进程、不做 MCP 桥（唯一例外是 Claude 订阅那条路的 `internal/claudebridge/mcp.go`）。它的"连接"是一次**文件改写**：找到 Agent 的配置文件，把里面的 endpoint / provider / model 字段改成指向 `127.0.0.1:3425`，然后等 Agent 下次自己发请求。

代码里的"已连接"就是一个布尔判断——`Wired()`（`internal/agent/applied.go:590`），读配置文件确认"现在写的是 magpie 吗"。"断开"就是把 stash 里存的原始值写回去。

这个前提决定了一切：magpie 的全部复杂度集中在**如何可靠地找到并安全地改写 89 种不同格式的配置文件**，而不在运行时。它与 `synapforge studio` 的集成方向（自己拉起进程、用 MCP 通信、管任务板与租约）几乎正交——能借鉴的是"探测与状态呈现"的方法论，不是它的架构。

---

## 1. 如何找到本地 Agent

### 1.1 三层结构

| 层 | 位置 | 作用 |
|---|---|---|
| 静态注册表 | `internal/agent/agents.go:296` `onMachineAll()` | 89 个内置 Agent 构造函数，硬编码 |
| 判定器 | `internal/agent/agent.go:447` `Detected()` | 五级试探，任一级命中即返回 |
| 自定义覆盖 | 各 `*.go` 的 `detect:` 闭包 | 默认判定不成立时替换 |

另有 8 个"非 Agent 的已知客户端"（`internal/agent/agents.go` 的 `others`）：`magpie`、`curl`、`pi-desktop`、`flowdown`、`bob`、`kimi-code-desktop`、`raft`、`memory`。它们不参与探测，只参与网关请求归因。

### 1.2 Agent 结构体的探测锚点

`internal/agent/agent.go:113`：

```go
type Agent struct {
    ID   string  // "claude"
    Name string  // "Claude Code"
    Bin  string  // "claude"                    ← 可执行文件名
    Dir  string  // ~/.claude                   ← 配置目录
    Path string  // ~/.claude/settings.json     ← magpie 改写的那个文件
    // ... 另有 60 余个可选钩子
}
```

`internal/agent/claude.go:2422` 的实例：

```go
ID: "claude", Name: "Claude Code", Icon: "claudecode-color",
Aliases: []string{"cc", "claude-code"},
UA:  []string{"claude-cli", "claude-code"},
Bin: "claude", Dir: filepath.Dir(path), Path: path,
signIn: ".credentials.json",
```

### 1.3 五级判定（最值得复用的部分）

`internal/agent/agent.go:447`：

```go
func (a *Agent) Detected() bool {
    if a.detect != nil { return a.detect() }                    // 0. 自定义，覆盖一切
    if a.Dir != "" && Taken(a.Dir) { return false }             // 守卫
    if _, ok := statKind(a.Path); ok && (a.pathOwn == nil || a.pathOwn()) {
        return true                                             // 1. 配置文件存在
    }
    if a.Dir != "" && !a.dirShared && agentDir(a.Dir, a.signIn) {
        return true                                             // 2. 配置目录有内容
    }
    if a.sign != nil && a.sign() { return true }                // 3. 登录凭证存在
    if a.Bin != "" && a.binsOwn(binAt(a.Bin)) { return true }   // 4. 可执行文件在 PATH
    if a.Bin == "" && a.WSL == "" && brewBin(cliSpecs[a.ID]) != "" {
        return true                                             // 5. Homebrew 装了但没跑过
    }
    return false
}
```

按代价递增排列：`stat` 文件 → `readdir` 目录 → 查凭证 → 走 PATH → 查 Homebrew。前两级覆盖绝大多数情况，只有前两级全落空才去碰 PATH（最贵）。

#### 三个防误报机制

**(a) `Taken()` 路径类型守卫**（`agent.go:526`）

```go
func Taken(p string) bool {
    for d := filepath.Clean(p); ; {
        if dir, ok := statKind(d); ok { return !dir }   // 存在但是文件 → 被占了
        up := filepath.Dir(d)
        if up == d { return false }
        d = up
    }
}
```

向上逐级检查：如果 `~/.dsh` 是个**普通文件**而不是目录，那它不是 DeepSeek Harness 的配置目录（可能是某个 shell 的 dotfile），直接判否。

**(b) `agentDir()` 空目录与残留识别**（`agent.go:501`）

```go
func agentDir(p string, own ...string) bool {
    es, err := os.ReadDir(p)
    if err != nil { return isDir(p) }
    if len(es) == 0 { return true }
    for _, e := range es {
        n := e.Name()
        if n != "skills" && n != ".DS_Store" &&
           !(e.IsDir() && leftover[strings.ToLower(n)]) &&
           !(!e.IsDir() && slices.Contains(own, n)) {
            return true      // 找到了一个"不是安装器/残留"的条目
        }
    }
    return false
}
```

两个真实故障驱动了它：

- **技能安装器污染**：`npx skills` 之类的工具会给它认识的**每一个** Agent 建 `<dir>/skills` 文件夹，装没装都建。所以一个只含 `skills/` 和 `.DS_Store` 的目录不能证明 Agent 存在。
- **卸载残留**：`~/.codebuddy` 卸载后可能只剩 `logs/` 和 `diagnostics/`，`~/.qwen` 只剩 `debug/`。`leftover` 白名单把这些运行期目录排除（注释引用 issue #1495）。

**(c) `goProgram()` 同名二进制消歧**（`agent.go:544`）

如果某个 Agent 本身不是 Go 写的，而 PATH 上的同名二进制**是 Go 编译的**，那是另一个工具占了名字。检测方式是读二进制的 `\xff Go buildinf:` 魔数，再用 `debug/buildinfo.ReadFile` 确认。

代价很高——注释写明："对于 Block 的 goose，一个 260 MB 的 Rust 程序，第一次 state 请求就分配了 200 MB"。所以用 `hasGoMagic` 先做廉价的字节扫描，命中才解析，并且按 `(路径, 大小, mtime)` 三元组缓存。

#### Windows 的 PATH 特化

`binAt()`（`agent.go:489`）：

```go
func binAt(name string) string {
    if p, err := proc.LookPath(name); err == nil { return p }
    return proc.FindTool(name)
}
```

先查 magpie 自己进程的 PATH，失败再走 `proc.FindTool`——后者在 Windows 上直接读**注册表里的当前用户 PATH**，再依次扫 `~/.local/bin`、npm、bun、Volta、pnpm 的目录。

注释解释了为什么必须这样（issue #839、#1482）：Windows 没有 login shell 可以借 PATH。magpie 启动之后 Claude Code 安装器才把 `claude.exe` 放进 `~/.local/bin` 并加进用户 PATH——magpie 的进程 PATH 是启动时的快照，永远看不到它。而登录流程用的是 `FindTool`，所以出现"登录说装好了、Agent 列表里没有"的割裂。

### 1.4 自定义 detect 的两个典型

**Gemini CLI**（`internal/agent/gemini.go:198`）：

```go
detect: func() bool {
    if Taken(dir) { return false }
    _, err := proc.Look("gemini")
    return err == nil
},
```

注释：`~/.gemini` 卸载后**会留下来**，而且 Antigravity 也在里面放自己的文件夹（`antigravity/`、`antigravity-cli/`、`config/`）并读 `GEMINI.md`。所以"目录存在"完全不能证明 Gemini CLI 在，只能靠二进制。

**Claude Desktop**（`internal/agent/claudedesktop.go:144`）：

```go
detect: func() bool {
    if desktopdir.OS == "windows" {
        return desktopInstalled(home) || desktopWiredAny(ps)
    }
    for _, s := range sets {
        for _, d := range s.All() { if isDir(d) { return true } }
    }
    return iso && isDir(ps[0].dir3p)
},
```

Windows 上卸载后 `%APPDATA%\Claude` 和里面的 `claude_desktop_config.json` 都留着，所以只看程序是否安装；但**曾经被 magpie 接上过的仍然列出**，否则用户没法给它断开。

### 1.5 多配置目录展开

Claude Code 支持 `CLAUDE_CONFIG_DIR`，多账户用户常这么用：

```sh
alias claude-work='CLAUDE_CONFIG_DIR=~/.claude-work claude'
```

`claudeDirAgents()`（`internal/agent/claude_dirs.go:59`）扫 home 下所有 `.claude*` 文件夹，用 `claudeDirIsClaude()`（`claude_dirs.go:47`）判断真假——必须有 `settings.json` 或 `.credentials.json`，或者有 `projects/` 子目录。每个真文件夹生成一行独立 Agent，ID 为 `claude#work`，显示名 `Claude Code · work`。

排除名单（`claudeDirName()`，`claude_dirs.go:27`）：`.claude` 本身、`.claude.json` 及其备份、`.claude-launch`、以及 `.claude.` 开头的。

**配套的会话侧扫描**（`internal/appdir/claude_homes.go:25` `ClaudeHomesIn`）独立做一遍：扫 home 和 `ClaudeDir` 的父目录下所有以 `claude` 开头的文件夹，判断标准是**有 `projects/` 子目录**（`claudeHomeAt`），再加上 magpie 自己保存的账户目录 `~/.config/magpie/claude-accounts/*`。

注释写明动机（issue #1482）：多账户用户的会话散在多个目录，magpie 原来只读一个，导致"会话数和 token 用量只统计了一个 Claude 目录"。

### 1.6 WSL：每个发行版独立一行

`internal/agent/wsl.go:1166`：

```go
func wslAgents() []*Agent {
    if !wslLooks() { return nil }
    return wslAgentsOf(wslDistros())
}
```

流程：`wsl.exe -l -q` 列出所有发行版 → 对**正在运行的**每个问一次 `$HOME` 和 `wslKinds` 里哪些 Agent 在那里 → 每个生成 `<id>@wsl:<distro>`（如 `codex@wsl:Ubuntu`）。

**硬约束**（`wsl.go:1176` 注释）：绝不启动已停止的发行版。因为打开 `\\wsl.localhost\<distro>\...` 下的任何文件都会唤醒它。停止的发行版只能用上次探测记住的信息（`place.cold`）。

gateway 地址也按发行版算：mirrored / consomme 网络下 `127.0.0.1` 直通 Windows；NAT 下要用 Windows 在 WSL 网络里的地址，且只有 gateway 监听在 loopback 之外时才可达（`wslrun.HostLoopback`）。

### 1.7 缓存与重扫

`Rescan()`（`internal/agent/rescan.go:16`）对应 UI 刷新按钮。它只清"因为慢所以缓存"的东西：

```go
proc.Forget()          // PATH 查找结果
forgetLooks()          // 显示名缓存（30 秒 TTL）
latests.expireAll()    // 版本检查
wslProbes.expireAll()  // WSL 探测
cursorLocalSeen        // Cursor 本地
almaRead               // Alma 的 HTTP 响应
```

文件级读取（`stat`、`readdir`）每次都重做，不需要清。

---

## 2. 如何"连接"——改写配置

### 2.1 Fields 抽象

`Agent.Fields []Field`（`internal/agent/agent.go:104`）声明可调项，每项三个函数：

```go
type Field struct {
    Key     string                              // "model"
    Label   string                              // 显示名
    Get     func() string                       // 从配置文件读
    Set     func(string) error                  // 写回配置文件
    Options func(cur map[string]string) []Option // 可选值
    Quiet   bool   // 空值时不列出（跟随另一个字段）
    Live    bool   // magpie 自己的设置，网关每次请求读取，改了不用重启 Agent
    Follows string // Quiet 字段跟随哪个字段
}
```

`Option`（`agent.go:24`）有 30 余个字段，关键的：

| 字段 | 含义 |
|---|---|
| `Value` | 写进配置文件的值 |
| `Ref` | catalog id（`provider/model`），跨 Agent 同一模型用同一 ref |
| `Direct` | Agent 自己直连厂商，不经 magpie（显示为"这是对的，不是配置失败"） |
| `Via` | 经 magpie 的 Agent 自有模型 |
| `Same` | magpie 在 Agent 自己登录的那个账户上服务，两行合一 |
| `Served` | Agent 自有模型也被 magpie 的列表展示 |
| `Alias` | 带日期的 id 的别名（`claude-opus-4-5-20251101` → `claude-opus-4-5`） |
| `Free` / `Zero` | 订阅免费 / 厂商标价 $0 |
| `Rate` / `RateWas` | 一次请求消耗的订阅额度倍数，及折扣前值 |
| `Context` | 上下文窗口 |

### 2.2 四种连接方式 + 一种兜底

| 方式 | 机制 | 代表 |
|---|---|---|
| 改 endpoint | `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` → `127.0.0.1:3425` | Claude Code、Codex |
| 改 provider 段 | 写一个叫 `magpie` 的 provider 条目进 Agent 自己的配置 | Hermes `providers.magpie`、VS Code Custom Endpoint、Kimi `[providers.magpie]` |
| `Join` | 不换模型，只把 magpie 的模型**加进**列表 | Codex 登录 ChatGPT 时（`agent.go:196`） |
| `Import` | 一次性链接，用户在 App 内确认 | Cindy（`agent.go:299`） |
| 兜底 `Launch`/`Typed` | 只从环境变量取 gateway 的 Agent，给启动命令 + 往用户 shell 装同名函数 | agy（读 `GEMINI_API_KEY` + `GOOGLE_GEMINI_BASE_URL`） |

Claude Code 的写入目标是 `~/.claude/settings.json` 的 `env.ANTHROPIC_BASE_URL` 和 `model`（`internal/agent/claude.go:663` `claudeIn()`）。

### 2.3 写前 stash，写后可还原

`stash.json`（`internal/agent/stash.go:31`）记录"接上之前原来是什么"：

```go
func stashPath() string { return filepath.Join(filepath.Dir(provider.Path()), "stash.json") }
```

`Disconnect()`（`applied.go:912`）顺序：

1. `Unwire` —— 把 magpie 写进去的键删掉
2. 字段 `Set("")` —— 回到 Agent 自己的默认
3. `Restore` —— 把 magpie 第一次写之前**整份复制到一边**的文件放回

一个细节值得抄（`stash.go:22` 注释）：`stash.json` 如果解析失败（手改坏了、旧版 magpie 写了一半），**先复制一份到旁边再覆盖**，保证里面的东西还能捞回来。整个读写走 `stashMu` 互斥 + `edit.WriteAtomic` 原子写，避免"两个改动并发 → 一方读到空 stash → 把所有人的原始值都覆盖掉"。

另有 `disconnectedKey`（`applied.go:716`）：`stash["<id>.disconnected"] == "1"` 表示用户主动断开过，用来区分"从没连过"和"连过又断开"。

### 2.4 连接状态判定

**`Wired()`**（`applied.go:590`）：

```go
func (a *Agent) Wired() bool {
    if a.Native != nil { return a.Native.Read().Provider == "connected" }
    if a.Joined != nil && a.Joined() || a.Beside != nil && a.Beside() {
        return !a.handWired()
    }
    vals := a.Values()
    for _, f := range a.Fields {
        if v := vals[f.Key]; v == magpieID || magpieValue(a, f, v, vals) ||
           a.Routed != nil && setAsGroup(a.ID, f.Key, v) && a.Routed() {
            return !a.handWired()
        }
    }
    return false
}
```

`handWired()` 是反向守卫：如果配置是**用户手写的**而不是 magpie 写的，不算"已连接"（避免 magpie 声称拥有用户自己的配置）。

**`Drift()`**（`applied.go:175`）检测"magpie 设好了但后来被改掉"。五种状态，UI 文案在 `internal/gui/assets/app.js:1131`：

| kind | 用户看到的 |
|---|---|
| `unwired` | `{agent}` 不再走 magpie —— 它的配置被改了 |
| `replaced` | `{agent}` 在 magpie 之外被切换到了 `{model}` |
| `bypassed` | `{agent}` 被使用但没有经过 magpie —— 应用后重启它 |
| `unreachable` | `{agent}` 配好了，但它用来访问 magpie 的地址上没有东西应答 |
| `proxied` | `{agent}` 通过一个不转发请求的代理访问 magpie |

**`Source()`**（`internal/agent/connectinfo.go:33`）告诉用户"未连接时它现在跑在什么上"，返回值：`"sub"`（Claude 订阅）、`"chatgpt"`、`"key"`（自有 API key）、`"providers:N"`（自有 N 个 provider）、`""`（未知）。

读法很具体：
- Claude：看 `~/.claude.json` 的 `oauthAccount.accountUuid`
- Codex：看 `auth.json` 的 `codexChatGPT(dir)` 或 `OPENAI_API_KEY`
- OpenCode 系：数 `provider` 段的键 + 各自的 auth 文件

注意 `Source()` 尊重 `settings.AccountDiscoveryOff()`（issue #1535）——关掉账户发现时，不读任何登录文件，只报 Agent 自己配置里写的。

**`StaleCopies()`**（`connectinfo.go:295`）——"哪些还在跑的进程用的是旧配置"。判定：进程启动时间早于配置文件 mtime（留 1 秒容差）即为 stale。Windows 上列进程慢且拿不到启动时间，所以只对 Codex daemon 特判（`codexDaemonBehind()`）。

---

## 3. 如何显示 Agent 列表

### 3.1 服务端：`state()`

`GET /api/state` → `stateJSON`（`internal/gui/api.go:303`）→ `Agents []agentJSON`。

`agentJSON`（`api.go:195`）**30 个字段**：

```
id, name, icon, path, fields, drift, wired, import, added, launch, typed,
models, menu, noNames, source, stale, staleCopies, appRestart, isolated,
telemetry, joined, appOut, appBack, appSnag, failover, cliMissing,
native, command, terminal, openApp
```

关键流程（`api.go:3288`）：

```go
all := heldAgents()
for _, a := range agent.ClientsIn(all) { s.Clients = append(...) }  // 全部已知，含未安装
detected := agent.DetectedIn(all)                                    // 只留本机存在
for _, a := range detected {
    if a.Follow != nil { _ = a.Follow() }   // Claude Code 的 tier 跟随主模型
    vals := a.Values()
    aj := agentJSON{ID: ..., Fields: agentFields(a, vals)}
    aj.Models = agentModelCount(a, aj.Fields)
    aj.Drift  = a.Drift()
    aj.Wired  = a.Wired()
    if aj.Wired { aj.StaleCopies = a.StaleCopies(); aj.Joined = a.Joined != nil && a.Joined() }
    else        { aj.Source = a.Source(); aj.Failover = a.FailingOver != nil && a.FailingOver() }
    // ... 还有 telemetry / import / launch / native / command / cliMissing
}
```

`Clients` 与 `Agents` 分离：前者是全部 89+ 个已知 Agent（供"请求可能来自谁"的归因、以及"未安装但有安装命令"列表），后者只含本机检测到的。

### 3.2 增量传输：行级哈希

`writeJSONRows()`（`api.go:3504`）——逐行序列化，每行单独算 `sha256[:16]`，整体也算一个 tag 放进 `X-Magpie-Tag`。

前端下次请求带 `X-Magpie-Had: <上次的 tag>`，服务端发现整个列表没变就直接回：

```
X-Magpie-Tag: <原 tag>
X-Magpie-Same: 1
（空 body）
```

连 JSON 都不生成。

`/api/sessions`（`internal/gui/sessions.go:152`）用同一套：先比对 `logVersion + 显示名 + 隐藏标记` 三个因素算出的 tag，命中就把 `made` 传进 `sessions.ListUnless()`，后者发现列表没变返回 `same = true`，于是直接 `WriteHeader(200)` 加两个头返回，**不做行构造、不做 JSON 编码、不重算哈希**。省掉的正是这三步。

注释里的性能账（都是实测数）：`~22 MB` 一次轮询、`~140 MB` 八次轮询、`~2.7 MB` 每次的行拷贝、`~60%` 的未变轮询被这个机制省掉。

### 3.3 前端：`renderAgents()`

`internal/gui/assets/app.js:486`。每行 = 图标 + 名字 + 一排字段按钮 + 连接开关。

几个设计点：

- **空状态**："Install Claude Code, Codex, Gemini CLI, OpenCode… and magpie will list them here."
- **无值字段不画**（`app.js:517` 的 `none(f)`）：既没有可选项也没有当前值的字段直接跳过，避免一排空下拉框。
- **两种布局**：面板模式（菜单栏下拉）每行只显示"模型名 + effort 三根小柱子"，点开才展开；窗口模式直接铺开。
- **宽窄列分配**：`model` / `large` / `executor` / `planner` 占宽列，其余窄列，保证控件上下对齐。

### 3.4 轮询策略

| 内容 | 间隔 | 条件 |
|---|---|---|
| 全量 state | 事件驱动 | `loadBack()`（`app.js:24284`）带 1 秒去抖 + 请求合流 |
| drift | 15 秒 | `app.js:24297`，`document.hidden` 或有弹出菜单打开时跳过 |
| CLI 版本 | 一次性 + 重试 | `loadCLIs()`，`pending` 时 4 秒后重试，最多 3 次 |
| quota 时间戳 | 30 秒 | 仅更新"多久前"文字 |

drift 单独拉一次而不是塞进 state，注释的理由是"config 可能在窗口开着时被改写"——避免每次都全量重算 89 个 Agent 的 drift。

---

## 4. 如何发现会话

### 4.1 包级声明

`internal/sessions/sessions.go:1` 的包注释就是完整的支持清单：

> Claude Code 的 `projects/*/<id>.jsonl`（以及 Qoder 的，同一种格式）、Codex 的 rollout 文件、OpenCode 的数据库（或旧版 JSON 文件）和 ZCode 的、Pi 的会话文件和 omp 的、DeepSeek Harness 的、Cline 的、Grok Build 的、WorkBuddy 的和 CodeBuddy Code 的、Droid 的、Cursor CLI 的和 Alma 的 chat store——带每个花了多少 token、按有效价格折算多少钱，以及恢复它的命令。**它只读 Agent 的文件夹。**

### 4.2 汇总入口

`allFilesWith()`（`sessions.go:987`）：

```go
lists := callFileLists(codexFiles())
lists = append(lists, openCodeFiles(), piFiles(), raftFiles(), cindyFiles(),
    wslFiles("pi", "raft", "omp", "kimi"), zcode())
lists = append(lists, dshFiles(), clineFiles(),
    ccFiles("qoder", QoderDir("qoder")), ccFiles("qoder-cn", QoderDir("qoder-cn")),
    grokFiles(), workbuddyFiles(), workbuddyAIFiles(), codebuddyFiles(),
    droidFiles(), ompFiles(), cursorFiles(), hermesFiles(), almaFiles(),
    kimiFiles(), agyFiles(), antigravityFiles())  // #2190
return allowed(kept.distinct(lists...))
```

最后 `distinctPaths()` 去重——同一个文件可能被两个 Agent 列到（共享目录、或数据库里 TEXT/BLOB 两行同 id）。

### 4.3 两种存储形态

**形态 A：JSONL 文件（Claude Code 系）**

`listCCFiles()`（`sessions.go:905`）遍历 `<dir>/projects/*/`：

```
projects/<project-slug>/
    <session-id>.jsonl                          ← 主会话
    <session-id>/subagents/*.jsonl              ← 子 Agent
    <session-id>/subagents/workflows/<run>/agent-*.jsonl  ← workflow 内的 Agent
```

**形态 B：SQLite 数据库（OpenCode 系）**

ZCode 的会话在 `~/.zcode/cli/db/db.sqlite`（`internal/sessions/zcode.go:26`），用 OpenCode 的表结构。

一个值得注意的坑（`zcode.go:59` 注释）：ZCode 会把会话消息**复制**到另一个会话（侧边聊天、fork、恢复检查点），副本带 `metadata.forkOrigin = {sessionId, messageId}`，且继承原消息的时间、模型、token。如果两个都统计，token 会翻倍——有用户报了 0.42B 的 GLM token 记错了日期（`skyhuang1994`）。

解法：`copied()` 判断只有**副本的源已经不在了**（被删或不在库里）才计副本。

### 4.4 性能设计：目录缓存 + 增量解析

**`readDirectory()`**（`internal/sessions/discovery.go:45`）——按 `(mtime, size)` 缓存目录列表，**不缓存文件内容**。

有个很细的坑（`discovery.go:36`）：

```go
const racyDirectory = 2 * time.Second
```

目录 mtime 精度很粗（Linux 一个内核 tick，某些文件系统一秒），而且**目录大小经常不变**。所以同一个 tick 内新增的条目可能 mtime 没变，导致新会话"隐形"。解法：只有当一个列表的年龄超过 2 秒才信任它，否则重读。

另外 `unreadDirectory()` 区分"空"和"读不了"：读失败（EMFILE、杀毒软件占用、网络 home 卡住）时返回**上次的好列表**，而不是空。注释写明教训：当成空的，整个文件夹的会话会从页面上消失，解析结果也被丢弃，直到下次轮询重读。

**`SessionGlob()`**（`discovery.go:124`）是 `filepath.Glob` 的替代，走同一套缓存，且保证不抑制新条目。

**增量解析**：`state` 结构记录每个文件的解析进度（`Off` 偏移量），文件只是**变长**时从上次位置续读，不重头来。解析结果落盘到 magpie 的 cache 目录，重启不用重算。

### 4.5 分页与版本

```go
const Limit = 200    // 默认页大小
const All   = 5000   // "长过任何一台电脑的历史"
```

`ListUnless(limit, made)`（`sessions.go:2016`）的 `made` 是前端上次拿到的版本号：列表没变就返回 `same = true` 且不拷贝数据。

### 4.6 Session 结构体

`sessions.go:118`，23 个 JSON 字段 + 若干内部字段：

```go
read_only, gateway, agent, id, cwd, title, nickname, start, last, role,
parent_session, root_session, model_effort, models, cost, unpriced,
resume, path, app, carry, transcript, subagents, wsl
```

嵌入的 `Tokens`：`Input`、`Output`、`CacheRead`、`CacheWrite`、`CacheWrite1h`。

**`Resume` 是可执行命令**，由 `resumeCommand()`（`sessions.go:2991`）按 Agent 拼：

```go
case "claude":   run = "claude --resume " + id
case "codex":    run = "codex resume " + id
case "opencode": run = "opencode --session " + id
case "pi":       run = "pi --session " + id
case "omp":      run = "omp --resume " + id   // 有 profile 时插 --profile
case "cursor":   run = "cursor-agent --resume " + id   // 它的会话按文件夹找，cwd 为空则不给
case "agy":      run = "agy --conversation " + id      // 在 magpie gateway 上启动的要带原变量和 --model
```

然后包 `cd <cwd> && `（Windows 上 `Set-Location -LiteralPath '...'; `）。**`safeID` 正则先校验 id**，防注入。`cliGone(agent)` 为真时（CLI 已卸载）返回空串，不给一条跑不通的命令。

### 4.7 网关侧会话（无本地文件）

`internal/sessions/external.go` —— 有些请求经过了 gateway 但本机没有对应文件（别的机器、或 Agent 没写文件）。这些作为只读会话合并进列表。

合并规则：同一 `(Agent, ID)` 以本地文件为准，网关的多余条目作为 `ReadOnly: true, Gateway: true` 追加，没有 `Path` 也没有 `Resume`。

`EachMerged()`（`external.go:44`）的注释解释了为什么不直接 `MergeExternal()`：页面轮询会把 17k 会话复制两遍（一次进合并列表，一次进行），所以改成**逐个产出**，外部会话按需构造。

### 4.8 隐私闸门

`Reads(agent)`（`internal/sessions/access.go:42`）——每个 reader 开头都问一次。

首次安装时 Sessions 页会问用户"允许读哪些 Agent 的会话历史"（`GET/POST /api/sessions/reads`，`internal/gui/sessions_reads.go`），之后在设置里改。

`OwnOff`（gateway mode，`access.go:56`）为真时全部不读，只显示经过网关的——用于"magpie web 从另一台电脑访问"的场景，不泄露本机 Agent 的历史。

---

## 5. 如何显示会话

### 5.1 双页面

| 页面 | 文件 | 定位 |
|---|---|---|
| Usage 页的 Sessions 标签 | `internal/gui/assets/plugins/usage/page.js` | 偏统计：KPI、日历热力图、按模型/项目/小时的柱状图、分页列表 |
| Sessions 插件页 | `internal/gui/assets/plugins/sessions/client.js`（1,889 行） | 偏管理：按文件夹分组、删除（进 magpie 自己的回收站）、恢复、看对话、看子 Agent |

### 5.2 轮询

`loadSessions()`（`page.js:3874`）——三个请求并发：

```js
[s, st, o] = await Promise.all([
  api("sessions"),                            // 列表
  api("sessions/stats?days=" + sessDays()),   // 统计
  sessOverview(q)                             // 概览（带过滤条件）
]);
```

间隔 15 秒（`page.js:5187`），只在页面可见且无弹出菜单时。返回相同就跳过重绘（`sameRead()` 用对象同一性判断，依赖 `readTags` 机制）。

### 5.3 行渲染

`sessionItem()`（`page.js:5035`）：

```
[图标] 标题                                    token数 成本   >
       文件夹名 · 模型 · 多久之前
```

- `sub.title` 属性放完整路径 + via 信息（gateway 把它路由到了哪些模型/effort）
- WSL 会话在 `sub` 前面加一个 badge
- 点击走 `magpie.call("sessions", "reveal", s, e)` 切到 Sessions 插件页
- Sessions 插件被关掉时改为原地展开 `sessionDetail()`（显示时间、WSL、文件夹、模型明细、恢复命令、对话记录、子 Agent）

### 5.4 服务端 `/api/sessions`

`internal/gui/sessions.go:114`。流程：

```go
n := 请求的 limit，默认 sessions.All

// 版本号检查：log 版本 + 显示名 + 隐藏标记都没变 且 前端带来的 tag 匹配
list, made, same := sessions.ListUnless(sessions.All, made)
if same { X-Magpie-Same: 1; return }

// 合并网关侧会话（逐个产出，不造中间列表）
sessions.EachMerged(list, ext, func(s *sessions.Session) bool { ... })

// 补 Codex 的 model/effort 归属（usage.CodexModelEffort）
// 补 via（gateway 路由记录，usage.Vias）
// 补显示名和图标
```

最后那句值得注意：

```go
// 画成它实际运行的 Agent：Claude Desktop 的 Code 标签页的会话在 Claude Code 的文件夹里
j.Name, j.Icon, j.Via = s.Agent, "generic", viaOf(vias, s)
if a, ok := agents[cmp.Or(s.App, s.Agent)]; ok {
    j.Name, j.Icon = a.Shared, a.Icon
}
```

`s.App` 是"会话实际运行在哪个 App"，`s.Agent` 是"文件属于哪个 Agent"。Claude Desktop 的 Code 标签页跑出来的是 Claude Code 会话（文件在 `~/.claude/projects/`），但图标和名字要显示成 Claude Desktop。

### 5.5 请求归因：怎么知道是哪个 Agent 打的

三种手段，按优先级：

**(a) User-Agent 前缀匹配**（`internal/usage/usage.go:511` `AgentOf()`）

```go
func AgentOf(ua string) string {
    ua = strings.TrimSpace(ua)
    name, _, _ := strings.Cut(ua, "/")   // "claude-cli/2.1.3" → "claude-cli"
    name, _, _ = strings.Cut(name, " ")
    l := strings.ToLower(name)
    for _, k := range knownAgents() {     // 先精确匹配名字
        if slices.Contains(k.Names, l) { return k.ID }
    }
    for _, k := range knownAgents() {     // 再前缀匹配
        if slices.ContainsFunc(k.UA, func(p string) bool { return strings.HasPrefix(l, p) }) {
            return k.ID
        }
    }
    if name == "" { return "other" }
    return name
}
```

名字精确匹配优先于前缀匹配——注释写明原因：`cursor-local` 是独立的，而 Cursor 的 UA 前缀也是 `cursor`。

**(b) 路径前缀**（`internal/gateway/agentpath.go:17`）

```
<gateway>/agent/openbot/v1/chat/completions  →  OpenBot
```

给那些既拿不到独立 key 又改不了 UA 的 Agent 用。注释里的例子：OpenBot 把 endpoint 的 key 封在 `safeStorage` 里（magpie 不开），然后把它交给它自己跑的 OpenCode，请求说自己是 `opencode/…`，于是被记成 OpenCode 的（用户 `SSNQT` 报告）。

**(c) 专属 key**（`internal/agent/gatewaykey.go:47` `agentKeyAt()`）

`gateway.TokenFor(agent)` 生成带 Agent 名的 token，网关侧 `TokenFor` 反查。

**另有反查表 `libraries`**（`usage.go:487`）—— `curl`、`bun`、`python-httpx`、`openai`、`anthropic`、`go-http-client`、`okhttp` 等 40 余个是**库不是 Agent**，单独标出来。注释里的用户反馈（`theforeveriris`）："Bun、curl 和 python-httpx 每个都在 Context 标签页上有一个 Agent 的记分卡"。

---

## 6. 对 synapforge studio 的取舍清单

### 6.1 建议直接复用

| 机制 | 出处 | 理由 |
|---|---|---|
| 五级探测 + `Taken()` + `agentDir()` | `agent.go:447/526/501` | 这套误报防护全是踩出来的，照搬到 `studio/backends.py` 的 `probe()` 能省掉一批"装没装"的 bug |
| 多配置目录展开 | `claude_dirs.go:59` | `CLAUDE_CONFIG_DIR` / 多账户是真实场景，一个 Agent 一行不够 |
| 写前 stash + 断开还原 | `stash.go` + `applied.go:912` | 计划里目前没有。改用户配置文件必须有回滚路径，否则用户不敢用 |
| Drift 五态 | `applied.go:175` | 用户手改配置后静默失效是最坏体验。定期比对"我写的"vs"文件里的" |
| `Source()` 的"现在跑在什么上" | `connectinfo.go:33` | 用户开开关前要知道会动到什么 |
| 行级哈希 + `X-Magpie-Same` | `api.go:3504` | 计划里 UI 1.5 秒轮询 state。集群跑几小时后全量 JSON 会成瓶颈 |
| `readDirectory` mtime+size 缓存 + 2 秒 racy 窗口 | `discovery.go:45/36` | 长跑场景下日志/会话目录反复扫，这个坑一定会踩 |
| `StaleCopies` 的"进程启动时间 vs 配置 mtime" | `connectinfo.go:295` | 可直接用于"重启后把 RUNNING 的 run 标成 PAUSED"的判定 |

### 6.2 明确不要抄

| 机制 | 为什么不要 |
|---|---|
| 89 个硬编码 Agent 构造函数 | 这是"Every agent's model"产品定位的代价。你只需要 `claude` + 通用命令模板 |
| `agentplug` JS 插件系统 | 在 moejs 里跑、无 Node、要打包依赖。你的 `{prompt} {mcp_config} {workspace}` 占位符模板更简单，别升级成插件体系 |
| WSL 支持 | 300+ 行 + 一堆边界（停止的发行版不能唤醒、NAT vs mirrored 网络、junction 要走 `proc.RealPath`）。除非用户明确在 WSL 跑 |
| `#2278` 那批内存优化 | 是 17k 会话 + 720k 调用规模下的必要手段。你的量级是几十个任务，`json.Marshal` 足够 |
| 多协议翻译网关 | `internal/gateway` 33+ 文件做四协议互转。你要调度 Agent，不是当模型代理 |

### 6.3 一个结构性提醒

magpie 的整个模型建立在"**Agent 是别人写的、我改它的配置文件**"这个前提上。

`synapforge studio` 的计划是"**我自己拉起 Agent 进程、用 MCP 和它通信**"——集成深度完全不同。

后果：

- magpie 能做的事你**不需要做**（不必探测 89 种配置格式、不必做 stash 还原、不必做 drift 检测）
- 你要做的事 magpie **完全没有参考**（进程生命周期、租约、任务板、暂停恢复、崩溃恢复、预算控制）

它唯一能给你的运行时参考是 `StaleCopies` 那个判定思路，以及"把状态做成可断言的布尔函数"这个设计习惯——`Wired()`、`Joined()`、`Beside()`、`Routed()`、`FailingOver()` 全是不带副作用的纯判断，被 UI、CLI、网关三处共用。这个习惯值得学：**状态判定与状态写入分离**。

---

## 7. 与既有计划的偏差（需修正）

`plan-sess_ca327833-a068-4905-8e04-9e02b395375b.md` 有两处事实错误：

| 行 | 原文 | 实际 |
|---|---|---|
| 5 | "目标已改为 **magpie 式的本地工具**：把多个 Agent 组成集群，跑跨小时/跨天的长周期任务，并能随时暂停、恢复、干预" | magpie **没有**集群、角色、调度器、暂停/恢复、长周期任务。全库 `cluster` 零命中，唯一的 `multi-agent` 指的是 Codex 自己的 `multi_agent_v1/v2` 配置项 |
| 110 | "复用 `apps/desktop` 的 Electron 壳做窗口+托盘（magpie 同款）" | magpie 桌面壳是 **Wails v3**（`wailsapp/wails/v3 v3.0.0-beta.24`，Go + 系统 webview），不是 Electron |

建议改为：产品形态参照 magpie（单二进制、托盘 + 窗口 + TUI + Web + CLI 五形态同源、零账号零服务器、`~/.config/<app>` 存配置、安装脚本带干净卸载），但**集群编排部分是全新工作，无现成实现可对标**；桌面壳继续用已有的 Electron，不要为此换 Go/Wails。

---

## 8. 附录：关键文件索引

| 路径 | 行数 | 内容 |
|---|---|---|
| `internal/agent/agent.go` | 836 | `Agent`/`Field`/`Option` 类型，`Detected()` 五级判定，`Taken`/`agentDir`/`goProgram`/`binAt` |
| `internal/agent/agents.go` | 2,197 | 89 个内置构造函数、`All()`/`Clients()`/`local()`/`homes()`、`init()` 的跨包钩子 |
| `internal/agent/applied.go` | 1,181 | `Wired()`/`Drift()`/`ConnectHow()`/`Disconnect()`/`Pick()` |
| `internal/agent/connectinfo.go` | 438 | `Source()`/`StaleCopies()` |
| `internal/agent/stash.go` | 123 | 原值保存与还原 |
| `internal/agent/claude_dirs.go` | 101 | 多配置目录展开 |
| `internal/agent/wsl.go` | 2,239 | WSL 发行版探测 |
| `internal/appdir/claude_homes.go` | 80 | 会话文件所在目录的独立扫描 |
| `internal/sessions/sessions.go` | 3,317 | 各 Agent 的 lister、`Session` 结构、`List()`/`ListUnless()`、`resumeCommand()` |
| `internal/sessions/discovery.go` | 149 | `readDirectory` 目录缓存、`SessionGlob` |
| `internal/sessions/access.go` | 262 | `Reads()` 隐私闸门、`Found()` |
| `internal/sessions/zcode.go` | 83 | SQLite 型会话读取 + 副本去重 |
| `internal/sessions/external.go` | 275 | 网关侧只读会话合并 |
| `internal/gui/api.go` | 3,813 | `state()`、`agentJSON`、`writeJSONRows` |
| `internal/gui/sessions.go` | 1,445 | `/api/sessions` 全流程 |
| `internal/gui/assets/app.js` | 24,655 | `renderAgents()`、轮询 |
| `internal/gui/assets/plugins/usage/page.js` | 5,268 | `renderSessions()`、`sessionItem()` |
| `internal/gui/assets/plugins/sessions/client.js` | 1,889 | 会话管理页 |
| `internal/usage/usage.go` | 1,282 | `AgentOf()` UA 归因、`libraries` 反查表 |
| `internal/gateway/agentpath.go` | 57 | `/agent/<id>` 路径归因 |
