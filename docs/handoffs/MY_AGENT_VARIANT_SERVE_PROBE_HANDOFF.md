# MY-AGENT 强度（`variant`）观测交接：serve 与 CLI 两条通道的实测取证

> 2026-09-24 · 状态：**结论已实测（有条件能）**，产品代码未改，线上配置未改
> 上游：`docs/handoffs/MY_AGENT_M5C_S4_THINKING_HANDOFF.md` §3「强度按证据暂缓」——本文件把那条"暂缓"查成了"有条件能"。
> 执行体：`154.219.99.75`，opencode **1.18.32**，provider 别名 `deepseek` → 百炼 `deepseek/deepseek-v4.1-flash`

---

## 0 结论（一句话）

**能——但必须同时满足两个条件**：① 配置里给**这个模型**声明 `variants`（`provider.<id>.models.<model>.variants.<名>`，内容是 `{"reasoningEffort": ...}`）；
② 调用时把 variant 放在**消息体的顶层**字段 `variant`（serve）/ `--variant`（CLI）。
**当前平台把 variant 放进 `model` 对象里**（`message.model.variant`），这个位置 opencode 1.18.32 **不读**——HTTP 200 收下、静默丢弃，
这正是 S-4 看到的「收下但不生效 / 回读仍是 `default`」的根因。两条通道的**正确形状都实测生效到线上请求体**（`reasoning_effort` 真的出现在发给厂商的 body 里）。

---

## 1 问题 2 的权威来源：serve 自带的 OpenAPI 3.1（不是猜的）

`opencode serve` 在 **`GET /doc`** 暴露完整 OpenAPI 3.1 规格（HTTP 200、`Content-Type: application/json`、**478,968 字节**）。
`/openapi.json`、`/api/doc`、`/api/openapi.json` 都只是 Web UI 的 HTML（2,884 字节），**别拿那几个当文档**。

```bash
curl -s -u opencode:<口令> http://127.0.0.1:4321/doc -o /tmp/variant-probe/openapi.json   # 478968 bytes
```

### 1.1 `POST /session/{sessionID}/message` 的请求体（原文摘录）

```json
"requestBody": {"content": {"application/json": {"schema": {
  "type": "object",
  "properties": {
    "messageID": {"type": "string", "pattern": "^msg"},
    "model": {
      "type": "object",
      "properties": {"providerID": {"type": "string"}, "modelID": {"type": "string"}},
      "required": ["providerID", "modelID"],
      "additionalProperties": false
    },
    "agent": {"type": "string"},
    "noReply": {"type": "boolean"},
    "tools": {"type": "object", "additionalProperties": {"type": "boolean"}},
    "format": {"$ref": "#/components/schemas/OutputFormat"},
    "system": {"type": "string"},
    "variant": {"type": "string"},          // ← ★ 顶层字段，与 model 平级
    "parts": {"type": "array", "items": {"anyOf": [...]}}
  }}}}}
```

三条要点：
1. **`variant` 是消息体的顶层字段**，不在 `model` 里；`model` 只声明了 `providerID` / `modelID` 且 `additionalProperties: false`。
2. 运行时**不做严格校验**：`model` 里塞 `variant`/`bogus` 都返回 200，未知键**被丢弃**（不报错）——所以旧探针看到的是"被收下"，其实是"被扔掉"。
3. 同族接口一致：`/session/{id}/prompt_async`、`/session/{id}/command` 的请求体里 `variant` 也是顶层字段。

### 1.2 相关 schema（同一份 OpenAPI 里）

| schema | 字段 | 含义 |
| --- | --- | --- |
| `ModelRef` | `{id, providerID, variant}` | 会话/用户消息里记录的模型引用（`variant` 可选） |
| `AssistantMessage` | 顶层有 `variant`，另有 `modelID`/`providerID`/`mode`/`agent` | **回读位置**：助手消息的 `info.variant` |
| `UserMessage.model` | ModelRef（含 `variant`） | 回读位置：用户消息的 `info.model.variant` |
| `ProviderConfig.models.<modelID>.variants` | `object`，值 `{disabled?: boolean, ...任意}`，描述 "Variant-specific configuration" | **配置声明的形状** |
| `Model.variants` | `object`，值为任意 object | 运行期解析出的模型 variants（`GET /provider` 里可见） |
| `AgentConfig.variant` / `Config.command.<x>.variant` | `string` | 另有这两处 variant 字段（见 §2.3 实测：不生效） |

### 1.3 顺手证实的旧坑仍然存在

`POST /session`（建会话）的 OpenAPI **写着**可以带 `model`（含 `variant`），但运行时**任何** `model` 都 400：

```text
POST /session {title} only                       -> 200 {"id": "ses_f2de51d32ffe..."}
POST /session + model(providerID,modelID)        -> 400 {"_tag":"BadRequest"}
POST /session + model(+variant)                  -> 400 {"_tag":"BadRequest"}
POST /session + model(+bogus)                    -> 400 {"_tag":"BadRequest"}
```

→ `opencode_server.py:create_session()` 里"显式忽略 model"的做法**继续保留**（注释里的实测依然成立）。模型与强度都在**发消息**那一步带。

---

## 2 实测矩阵（全部在隔离 HOME 里跑，线上配置零改动）

### 2.1 serve 通道（`POST /session/{id}/message`）

判据有三层：`info.variant`（消息落库）→ 发出去的 HTTP body（本地 mock provider 抓包）→ 真厂商返回的 usage/耗时。

| # | 请求体形状 | HTTP | `info.variant` | 线上 body 里的 `reasoning_effort` |
| --- | --- | --- | --- | --- |
| A | 顶层 `"variant":"high"`，**且配置已声明** | 200 | `'high'` | **`'high'`** ✅ 真生效 |
| B | `"model":{...,"variant":"max"}`（**平台现在的形状**） | 200 | `None` | 无 ❌ 静默丢弃 |
| C | 不带 variant | 200 | `None` | 无（基线） |
| D | 顶层 `"variant":"ultra"`（**名字没在配置里声明**） | 200 | `'ultra'` | 无 ⚠️ 名字记下了，参数一条不加 |
| E | 顶层 `"variant":"high"`，**配置没声明 variants** | 200 | `'high'` | 无 ⚠️ 同上 |

**真厂商（百炼 compatible-mode）端到端**（同一 serve、真 baseURL、真 key）：

```text
[real] variant=high           http=200 variant='high' tokens={"total":7724,"input":7722,"output":2,"cache":{"read":0}}    text='OK' elapsed=5.2s
[real] no variant             http=200 variant=None   tokens={"total":7724,"input":42,"output":2,"cache":{"read":7680}}  text='OK' elapsed=2.2s
[real] variant inside model   http=200 variant=None   tokens={"total":7724,"input":42,"output":2,"cache":{"read":7680}}  text='OK' elapsed=1.4s
```

读法：`variant=high` 那一轮 **`cache.read` 从 7680 掉到 0**（请求体变了 → 前缀缓存键变了），而"variant 塞进 model"与"不传"两轮**逐字段完全一致**——
这是"顶层 variant 真的改了请求、model.variant 什么都没做"的独立佐证（厂商对 `reasoning_effort` 不报错，正常出字）。

### 2.2 CLI 通道（`opencode run`）

`opencode run --help` 原文：`--variant  model variant (provider-specific reasoning effort, e.g., high, max, minimal)`。
用 mock provider 抓 body + `opencode export <sessionID>` 回读：

| 命令 | exit | 回读（`export`） | 线上 body `reasoning_effort` |
| --- | --- | --- | --- |
| `--variant high`（配置已声明） | 0 | user.model.variant=`'high'`，assistant.variant=`'high'` | 主调用 **`'high'`** ✅ |
| `--variant max`（配置已声明） | 0 | `'max'` / `'max'` | 主调用 **`'max'`** ✅ |
| `--variant ultra`（名字未声明） | 0 | `'ultra'` / `'ultra'` | 无 ⚠️（同 serve 的 D） |
| 不带 `--variant` | 0 | `None` / `None` | 无（基线） |
| `--variant high`（**配置没声明**） | 0 | `'high'` / `'high'` | 无 ⚠️ |

（每次 CLI 运行会有 2 个请求：标题生成的小调用 + 正文调用；上表"主调用"指正文那条。）

**所以 CLI 通道的答案：`--variant` 真的生效**，但同样受"配置必须声明这个名字"约束；`--variant` 本身不会因为未声明而报错。

### 2.3 配置形状排除表（问题 3/5：逐一验证）

同一轮探针里换配置、重启 serve、发同一条消息（mock 抓 body，判据 `outbound.reasoning_effort`）：

| 形状 | 配置被接受？ | 真的影响行为？ |
| --- | --- | --- |
| ① `provider.deepseek.models.deepseek-v4.1-flash.variants.{low,high,max} = {"reasoningEffort": ...}` + 消息顶层 `variant` | ✅ | ✅ **唯一"开关式"生效形状** |
| ② `provider.deepseek.options.variant = "high"` | 配置能加载，无报错 | ❌ body 里没有 `reasoning_effort` |
| ③ 顶层 `"variants": {...}`（不在 provider/model 下） | 能加载，无报错 | ❌ 无 |
| ④ `"agent": {"build": {"variant": "high"}}`（OpenAPI 里 `AgentConfig.variant` 确实存在） | 能加载，无报错 | ❌ 消息的 `info.variant` 仍是 `None`，body 无参数 |
| ⑤ `models.<id>.options.reasoningEffort = "high"` | ✅ | ✅ 但**静态**——等于把默认拉满，不能按轮切换（不适合做"强度下拉"） |
| ⑥ `models.<id>.options.variant = "high"` | 能加载 | ❌ 无 |
| ⑦ `models.<id>.variants` 已声明，但消息把 variant 放在 `model` 里（平台现状） | ✅ | ❌ 无（= §2.1 的 B） |

**为什么"不支持"这个旧结论错了**：旧探针同时踩了两个坑——(a) variant 放在 `model` 里，(b) 模型没有 `variants` 声明（我们的模型 id `deepseek-v4.1-flash` 是自定义的，models.dev 目录里**没有它**，
所以 `GET /provider` 里它的 `variants` 是 `{}`；目录里同厂商的 `deepseek-v4-pro`/`deepseek-flash` 都带 `{low,high,max}`）。
两个坑各自都足以让参数静默消失——`(a)` 决定"读不读"，`(b)` 决定"读到名字后有没有参数可加"。

---

## 3 最小可复现命令序列（可直接粘贴；隔离 HOME，不动线上）

> 前提：以 root SSH 登录执行体；`P=/tmp/variant-probe` 是隔离 HOME；provider 配置是**从线上拷贝**（含 key，别打印、别进仓库），
> 探完 `rm -rf $P`。`opencode` 二进制在 `/home/synapforge/.opencode/bin/opencode`（**别用 root 的 `/root/.opencode`**）。

```bash
# 0) 隔离 HOME：只把 provider 配置拷进去（复制而非符号链接，方便改形状）
P=/tmp/variant-probe; rm -rf $P; mkdir -p $P/.config/opencode $P/work
cp /home/synapforge/.config/opencode/opencode.json $P/.config/opencode/opencode.json   # 含 apiKey：别 cat
python3 - <<'PY'      # 给模型声明 variants（这份是探针副本，线上文件不动）
import json
f="/tmp/variant-probe/.config/opencode/opencode.json"
d=json.load(open(f))
d["provider"]["deepseek"]["models"]["deepseek-v4.1-flash"]["variants"]={
    "low":{"reasoningEffort":"low"},"high":{"reasoningEffort":"high"},"max":{"reasoningEffort":"max"}}
json.dump(d,open(f,"w"),indent=2)
PY
chown -R synapforge:synapforge $P; chmod 600 $P/.config/opencode/opencode.json

# 1) 起探针 serve（自带口令，只监听回环）
cd $P/work
setsid nohup runuser -u synapforge -- env HOME=$P OPENCODE_SERVER_PASSWORD=<探针口令> \
  /home/synapforge/.opencode/bin/opencode serve --hostname 127.0.0.1 --port 4321 > $P/serve.log 2>&1 < /dev/null &
sleep 8; curl -s -u opencode:<探针口令> http://127.0.0.1:4321/api/health   # {"healthy":true}

# 2) 确认 serve 认得我们声明的 variants
curl -s -u opencode:<探针口令> http://127.0.0.1:4321/provider \
 | python3 -c 'import json,sys;d=json.load(sys.stdin);p=[x for x in d["all"] if x["id"]=="deepseek"][0];print(json.dumps(p["models"]["deepseek-v4.1-flash"]["variants"]))'
# {"low": {"reasoningEffort": "low"}, "high": {"reasoningEffort": "high"}, "max": {"reasoningEffort": "max"}}

# 3) 建会话 + 发消息（variant 在**顶层**）
python3 - <<'PY'
import base64,json,urllib.request
AUTH="Basic "+base64.b64encode(b"opencode:<探针口令>").decode()
def call(m,p,pay=None):
    r=urllib.request.Request("http://127.0.0.1:4321"+p,data=None if pay is None else json.dumps(pay).encode(),method=m)
    r.add_header("Content-Type","application/json"); r.add_header("Authorization",AUTH)
    with urllib.request.urlopen(r,timeout=240) as x:
        raw=x.read().decode(); return x.status,(json.loads(raw) if raw.strip() else None)
sid=call("POST","/session",{"title":"variant-probe"})[1]["id"]
code,resp=call("POST",f"/session/{sid}/message",
    {"parts":[{"type":"text","text":"Reply with exactly: OK"}],
     "model":{"providerID":"deepseek","modelID":"deepseek-v4.1-flash"},
     "variant":"high"})                                   # ← ★ 顶层，不在 model 里
print("http",code,"assistant variant =",resp["info"].get("variant"))     # http 200 assistant variant = high
msgs=call("GET",f"/session/{sid}/message")[1]
for m in msgs:
    i=m["info"]; print(i["role"],"| variant=",i.get("variant"),"| model=",json.dumps(i.get("model") or {}))
PY
# 期望（实测原文）：user 行 model={"providerID":"deepseek","modelID":"deepseek-v4.1-flash","variant":"high"}；assistant 行 variant='high'

# 4) CLI 通道同款（隔离 HOME，同一台机器）
cd $P/work
runuser -u synapforge -- env HOME=$P PATH=/home/synapforge/.opencode/bin:/usr/local/bin:/usr/bin:/bin \
  /home/synapforge/.opencode/bin/opencode run --format json --variant high \
  -m deepseek/deepseek-v4.1-flash "Reply with exactly: OK" < /dev/null | tail -1
# step_finish 里能看到 tokens；再用 `opencode export <上面输出里的 sessionID>` 回读 assistant.variant == 'high'
```

**零成本看"请求体到底变了没有"的做法（推荐给后续任何强度/参数改动）**：把探针配置的 `options.baseURL` 指向一个本地 mock
（`/v1/chat/completions` 收到就写盘 + 回一段 canned SSE），跑上面第 3/4 步，直接看 body。
本次就是这么拿到"`model.variant` 无参数、顶层 `variant` 带 `reasoning_effort`"的硬证据的（本地 mock 见 §6 的痕迹说明，已删）。

---

## 4 要把它做进产品，改哪些文件 / 哪几处

**层 0 · 执行体配置（必须先做，否则上层全是"设了没生效"）**
- `deploy/cloud-agent/configure-opencode-provider.sh`：生成 `opencode.json` 时给 `provider.<别名>.models.<模型>` 增加
  `"variants": {"low": {"reasoningEffort": "low"}, "high": {"reasoningEffort": "high"}, "max": {"reasoningEffort": "max"}}`。
  **键名/取值就是我们要暴露给页面的那几个**（实测只有被声明的名字才有参数；未声明的名字会被记录但毫无效果）。
  已装机的机器要重跑这一步（或加一条幂等的 "ensure variants" 路径），否则强度下拉在线上继续是静默无效果。
- 建议同时在安装脚本的收尾自检里加一条：`GET /provider`（或 `opencode models --verbose`）确认该模型的 `variants` 非空。

**层 1 · 常驻通道（线上默认通道）**
- `apps/agent/opencode_server.py`
  - `ServerClient.send_message()`：**新增顶层 `variant` 参数**（`payload["variant"] = variant`），并在 docstring 里写明
    「variant 必须在顶层；放进 `model` 会被 200 静默丢弃（1.18.32 实测）」——避免下一个人再踩。
  - `run_serve_turn()`：新增 `variant: str | None` 形参，透传给 `send_message`。
  - 回读位置提醒：助手消息是 `info.variant`（不是 `info.model.variant`）；用户消息才是 `info.model.variant`。

**层 2 · CLI 回退通道**
- `apps/agent/chat_loop.py`
  - `build_chat_command()`：加 `variant` 参数，与 `--session` / `-m` / `--agent` **同一个插入点**（`{prompt}` 前）插 `--variant <名>`。
  - `ChatLoopConfig` / `ChatLoop`：每轮从 claim 取会话级强度并传给两条通道（serve 与 CLI 都要带，否则降级轮次行为不一致）。

**层 3 · 平台侧（管线与页面）**
- claim 回传：内核把"这一轮的强度"带下来（与 model/agent 同一处）。
- 存储/契约：会话表加一列（如 `agent_conversations.reasoning_variant`），API 读写沿用现有模式切换的路径。
- 页面：**我不改 `apps/web/app/my-agent/page.tsx`（另一个 agent 在改）**——需要在该文件里加一个与模型下拉同排的强度下拉；
  下拉项建议**只列执行体已声明的名字**（来自层 0，可经心跳上报），不要做自由文本输入（未声明的名字无效果，会重演"设了没生效"）。
  文案建议写"推理强度（由模型服务商解释）"，不要写"更聪明/更努力"——语义由厂商决定（S-4 的 token 观测就是非单调的）。
- 探测：不建议从 opencode 侧"枚举可用 variant"（`opencode models` 不给 variants 列表；`/provider` 给的是**我们声明的那几个**）。
  权威集合就是层 0 写进配置的键名，把它当作常量上报即可（不编）。

---

## 5 这次取证临时动过哪些线上状态（以及是否恢复）

| 项 | 结果 |
| --- | --- |
| 线上 provider 配置 `/home/synapforge/.config/opencode/opencode.json` | **没动过**。改的全是 `/tmp/variant-probe/` 下的副本。核验：`mtime 2026-09-23 01:48:58`、`553 bytes`、`sha256 3ee146c005bb5586067a4ec0…`（与探针前一致） |
| 隔离 HOME `/tmp/variant-probe/`（含一份带 apiKey 的配置副本与 `GET /provider` 的完整 dump） | **已 `rm -rf` 删除**（含 apiKey 的副本只以 0600 存在于该目录，未打印、未进仓库；本文件所有密钥一律 `<已脱敏>`） |
| 探针进程 | 探针 serve（4321）与本地 mock（4399）已全部杀掉；`ss -ltn` 两个端口 0 监听 |
| 生产 `opencode serve`（4199） | 探测期间**没起过**（`/api/health` = `http 000`）。这是**正常空闲态**：serve 是按轮惰性启动的（`chat_loop.py:460` 建、`:470` `ensure_running()`），不是被弄坏了 |
| `agentd` | 未重启（PID 191433，`Thu Sep 24 05:48:41` 起，取证结束时已运行 45 分钟，状态 `Ssl`） |
| 线上真实对话（生产路径回归） | 取证收尾跑了一轮**生产 HOME + 线上配置 + 真 key** 的 `opencode run -m deepseek/deepseek-v4.1-flash "Reply with exactly: OK"` → 正常返回 `OK`，`step_finish` 记账 `tokens total=7344, cache.read=1024`；临时目录 `/tmp/sf-cli-check` 已删 |
| 说明：生产 DB（`/home/synapforge/.local/share/opencode/opencode.db`）在 06:25 / 06:30 有 mtime 变动 | **不是我写的**。生产 log 里那两条 `run=551ec9ff / 960bb02b`（`directory=/home/synapforge`、只到 `init` 就退出、无会话无模型调用）是 **agentd 自己的心跳清单探测**（`apps/agent/agent_inventory.py` 的 `opencode models` / `opencode agent list`，带 TTL 缓存）。我的探针全部走隔离 HOME（probe DB） |

---

## 6 未测边界（别当成已知）

- **会话级**强度：测的是"每轮消息带 `variant`"。未测 `PATCH /session/{id}` 给会话设 `model.variant`（schema 支持）是否随会话保持；
  也未测 v2 的 `POST /api/session/{id}/model`（v2 认不出自定义 provider，S-0 已判死，别回头）。
- **`--thinking` 与 variant 的联动**：没测（CLI 有 `--thinking` 开关；serve 的思考是另一条链路，S-4 已交付）。
- **多轮稳定性**：本次每档只跑 1–2 轮；`reasoning_effort` 对**输出质量**的影响没有做统计（不做承诺，页面文案也别承诺）。
- **厂商解释权**：`reasoning_effort` 是 openai-compatible provider 的透传参数，百炼 compatible-mode **接受**（实测 200 + 正常出字），
  但它到底怎么解释、和 `deepseek-v4.1-flash` 的 reasoning 行为怎么对应，由厂商决定。