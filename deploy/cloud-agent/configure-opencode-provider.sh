#!/usr/bin/env bash
# 给云端执行体配置 opencode 的模型 provider（在服务器上以 root 执行）。
#
# 为什么不是 systemd 的 EnvironmentFile：内核给执行体传环境变量是**按白名单**的
# （`agentd._EXECUTOR_ENVIRONMENT_KEYS`：HOME/PATH/TMP/TEMP…），模型厂商的 key 不在其中——
# 平台不该知道模型是谁家的。所以 key 交给 opencode 自己的配置文件（0600，属主 synapforge）。
#
# 用法（推荐第二种：key 不出现在命令行里，也就不会进 shell 历史/ps）：
#   bash configure-opencode-provider.sh --api-key sk-xxx ...
#   bash configure-opencode-provider.sh --api-key-file /etc/synapforge-agent/cloud.env ...
#
# `--provider` 是**我们自己在 opencode 配置里起的别名**（`-m <别名>/<模型 id>`），不是厂商要求的前缀；
# 当前部署用别名 `deepseek`，于是模型串是 `deepseek/deepseek-v4.1-flash`。
#
# **合并语义（2026-09-26 起）**：重复运行是安全的——只更新 `--provider` 指的那一个条目，
# 其它 provider（如 deepseek）原样保留；默认模型仅在传 `--set-default` 或配置里还没有
# model 时才会改动。方案 A 的接法就是再跑一次：
#   bash configure-opencode-provider.sh --provider synapforge \
#     --base-url https://synapforge.top/api/agent/llm/v1/<agent_id>/<project_id> \
#     --api-key-file /root/grant-token.txt --model <渠道模型名>
# （apiKey 是**项目能力令牌**，授权串需带 `llm.invoke` 能力；令牌走文件，别放命令行。）
set -euo pipefail

API_KEY=""; API_KEY_FILE=""; BASE_URL=""; MODEL=""; PROVIDER="deepseek"; INSTANCE="cloud"
SET_DEFAULT=0
SERVICE_USER="synapforge"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --api-key) API_KEY="$2"; shift 2 ;;
    --api-key-file) API_KEY_FILE="$2"; shift 2 ;;
    --base-url) BASE_URL="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --provider) PROVIDER="$2"; shift 2 ;;
    --instance) INSTANCE="$2"; shift 2 ;;
    --set-default) SET_DEFAULT=1 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done
[[ "$(id -u)" -eq 0 ]] || { echo "需要 root" >&2; exit 1; }
if [[ -z "$API_KEY" && -n "$API_KEY_FILE" ]]; then
  # 支持 `KEY=value` 形式的 env 文件，也支持文件里就是裸 key
  API_KEY="$(sed -n 's/^[A-Za-z_][A-Za-z0-9_]*=//p' "$API_KEY_FILE" | head -1)"
  [[ -n "$API_KEY" ]] || API_KEY="$(head -1 "$API_KEY_FILE")"
fi
[[ -n "$API_KEY" && -n "$BASE_URL" && -n "$MODEL" ]] || {
  echo "必填：--api-key 或 --api-key-file，以及 --base-url --model" >&2; exit 2; }

HOME_DIR="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
CONFIG_DIR="$HOME_DIR/.config/opencode"
CONFIG_FILE="$CONFIG_DIR/opencode.json"
install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$CONFIG_DIR"

# key 直接写进配置（不落 shell 历史、不进平台库）；文件 0600、属主是执行体用户。
# **合并**而非覆盖：只更新本 provider 的条目，其它 provider 与既有默认模型都保留。
python3 - "$CONFIG_FILE" "$PROVIDER" "$BASE_URL" "$MODEL" "$API_KEY" "$SET_DEFAULT" <<'PY'
import json, sys
from pathlib import Path
path, provider, base_url, model, api_key, set_default = sys.argv[1:7]
config = {}
if Path(path).exists():
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
    except ValueError:
        config = {}  # 坏文件就重建（0600 且属主正确，风险可控）
if not isinstance(config, dict):
    config = {}
config.setdefault("$schema", "https://opencode.ai/config.json")
providers = config.setdefault("provider", {})
providers[provider] = {
    "npm": "@ai-sdk/openai-compatible",
    "name": provider,
    "options": {"baseURL": base_url, "apiKey": api_key},
    "models": {model: {"name": model}},
}
if set_default == "1" or not config.get("model"):
    config["model"] = f"{provider}/{model}"
Path(path).write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_FILE"
chmod 0600 "$CONFIG_FILE"

echo "已写入 $CONFIG_FILE（0600，属主 $SERVICE_USER；合并模式，其它 provider 保留）"
echo "provider=$PROVIDER model=$PROVIDER/$MODEL baseURL=$BASE_URL"
echo
echo "验证（会真发一次请求，key 不出现在命令行里）："
echo "  sudo -u $SERVICE_USER -H bash -lc 'cd /tmp && opencode run --format json --auto -m $PROVIDER/$MODEL \"Reply with exactly: OK\" < /dev/null'"
