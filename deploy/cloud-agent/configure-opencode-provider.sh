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
set -euo pipefail

API_KEY=""; API_KEY_FILE=""; BASE_URL=""; MODEL=""; PROVIDER="deepseek"; INSTANCE="cloud"
SERVICE_USER="synapforge"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --api-key) API_KEY="$2"; shift 2 ;;
    --api-key-file) API_KEY_FILE="$2"; shift 2 ;;
    --base-url) BASE_URL="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --provider) PROVIDER="$2"; shift 2 ;;
    --instance) INSTANCE="$2"; shift 2 ;;
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

# key 直接写进配置（不落 shell 历史、不进平台库）；文件 0600、属主是执行体用户
python3 - "$CONFIG_FILE" "$PROVIDER" "$BASE_URL" "$MODEL" "$API_KEY" <<'PY'
import json, os, sys
from pathlib import Path
path, provider, base_url, model, api_key = sys.argv[1:6]
config = {
    "$schema": "https://opencode.ai/config.json",
    "provider": {
        provider: {
            "npm": "@ai-sdk/openai-compatible",
            "name": provider,
            "options": {"baseURL": base_url, "apiKey": api_key},
            "models": {model: {"name": model}},
        }
    },
    "model": f"{provider}/{model}",
}
Path(path).write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_FILE"
chmod 0600 "$CONFIG_FILE"

echo "已写入 $CONFIG_FILE（0600，属主 $SERVICE_USER）"
echo "provider=$PROVIDER model=$PROVIDER/$MODEL baseURL=$BASE_URL"
echo
echo "验证（会真发一次请求，key 不出现在命令行里）："
echo "  sudo -u $SERVICE_USER -H bash -lc 'cd /tmp && opencode run --format json --auto -m $PROVIDER/$MODEL \"Reply with exactly: OK\" < /dev/null'"