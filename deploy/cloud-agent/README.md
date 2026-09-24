# 云端执行体部署（CLOUD-1）

把 Agent 内核装到"另一台机器"上，用 **opencode** 当执行体接入平台。
计划与背景见 `docs/CLOUD_1_CLOUD_AGENT_PLAN.md`（**v2**），适配器细节见
`docs/handoffs/CLOUD_1_OPENCODE_ADAPTER_HANDOFF.md`。

## 顺序

```bash
# 0) 把仓库里的 apps/agent 与 packages 打包上传到服务器（例：/root/synapforge-agent-src）
# 1) 装内核 + opencode + 目录 + systemd 单元（root）
bash install-cloud-agent.sh --source /root/synapforge-agent-src --instance cloud
# 2) 配模型 provider（key 进 opencode 自己的 0600 配置，不进 systemd env）
bash configure-opencode-provider.sh --api-key <百炼 key> \
     --base-url https://dashscope.aliyuncs.com/compatible-mode/v1 --model deepseek-v4.1-flash
# 3) 配对（配对串从平台「设备与接入」页拿，15 分钟有效；授权串从「云端智能体」项目页拿）
bash pair-cloud-agent.sh --url https://synapforge.top \
     --agent-id agent-cloud-01 --device-id device-cloud-01 \
     --pairing-file /root/pairing.json --grant <base64url>
# 4) 启动
systemctl enable --now map-agent@cloud
journalctl -u map-agent@cloud -f
```

## 三个必须知道的坑（都实测过）

1. **任务必须声明执行体**：`resource_policy` 用
   `{"worker_executor":"cli","worker_command":["opencode","run","--format","json","--auto","{prompt}"],"worker_events":"opencode"}`。
   模板首词是 `opencode` 时协议会自动判为 opencode；写了 `worker_events` 就以声明为准。
2. **opencode 读 stdin**：不带 `< /dev/null`（或 stdin 不是 EOF）时会**挂住不返回**。
   内核已经对非交互进程改用 `DEVNULL`（`machine_service.py`），systemd 单元再加 `StandardInput=null` 兜底。
3. **坏配置是挂住、不是报错**（例如模型名写错）：靠任务 `budget.max_seconds` 与单元
   `TimeoutStopSec=30` 兜底，别指望它快速失败。

## 边界（用户定的三条）

- 这台机器**只有**设备令牌 + 项目能力令牌（0600 文件），**没有**平台会话/管理员令牌、没有平台机 SSH 私钥、没有部署脚本。
- 运行期不装包、不改系统配置（装包与改配置只发生在部署时，也就是上面 1~3 步）。
- 只授权**一个专用项目**（「云端智能体」），初始 `task_mode` 不设 `auto`，先人工派单。

## 失败排查

| 现象 | 看哪里 |
| --- | --- |
| 起不来 / 反复重启 | `journalctl -u map-agent@cloud -n 50` |
| 领不到任务 | `worker.json` 的项目/Agent 是否与平台一致；授权是否给到了这个项目 |
| 任务完成但没有过程事件/用量 | 任务的 `worker_executor` 是不是 `cli`、模板首词是不是 `opencode`（否则协议判为 `none`） |
| 任务一直不结束 | 看 `budget.max_seconds`；执行体可能挂住了，`systemctl stop` 后看 run 台账里的原始事件 |
| `opencode models` 报 `PermissionDenied: FileSystem.access (/root/opencode.jsonc)` | 不是在配置坏了：opencode 会读**当前目录**的项目配置，从 `/root` 这类没权限的目录跑就会这样。服务的工作目录是 `/srv/synapforge/<实例>`，从那里跑正常 |

## 模型串怎么写

`opencode -m` 是 `<provider 别名>/<模型 id>`，**前半段是我们在 `opencode.json` 里自己起的别名**，不是厂商要求的前缀。
当前部署：别名 `deepseek` → `https://dashscope.aliyuncs.com/compatible-mode/v1`，模型串 `deepseek/deepseek-v4.1-flash`
（两种别名 `dashscope` / `deepseek` 都实测跑通，最终按用户习惯用后者）。

两个附带事实：

- 别名与 opencode **内置 provider 同名**时会合并——`opencode models` 里会多出 `deepseek-flash`、`deepseek-v4-pro`
  这些内置条目，它们走的是我们配的 baseURL；我们只用自己注册的 `deepseek-v4.1-flash`。
- 配置里写了默认 `"model": "deepseek/deepseek-v4.1-flash"`，所以任务的命令模板**不需要带 `-m`**。