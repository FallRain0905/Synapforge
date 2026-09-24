#!/usr/bin/env bash
# CLOUD-1 配对脚本：把云端执行体接到平台上（在服务器上以 root 执行）。
#
# 五步（与 Windows 的 connect-agent.ps1 同一套流程，只是凭据落在 0600 文件里）：
#   1 登记 Agent（upsert）→ 2 生成设备密钥 → 3 设备注册（Ed25519 私钥签名）
#   → 4 设备令牌写入 0600 文件 → 5 首次把项目授权串落盘
#
# 用法：
#   bash pair-cloud-agent.sh --url https://synapforge.top \
#        --agent-id agent-cloud-01 --device-id device-cloud-01 \
#        --pairing-file /root/pairing.json --grant <base64url 授权串>
#
# 配对串从平台「设备与接入」页生成（15 分钟有效）；授权串从「云端智能体」项目页生成。
set -euo pipefail

URL=""; AGENT_ID=""; DEVICE_ID=""; PAIRING_FILE=""; GRANT=""; GRANT_ONLY=""; OWNER=""
INSTANCE="cloud"; DEVICE_NAME="cloud-executor"
SOURCE_ROOT="/opt/synapforge-agent"
SERVICE_USER="synapforge"
PYTHON="/opt/synapforge-agent/venv/bin/python"
AGENTD="/opt/synapforge-agent/apps/agent/agentd.py"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url) URL="$2"; shift 2 ;;
    --agent-id) AGENT_ID="$2"; shift 2 ;;
    --device-id) DEVICE_ID="$2"; shift 2 ;;
    --pairing-file) PAIRING_FILE="$2"; shift 2 ;;
    --grant) GRANT="$2"; shift 2 ;;
    --grant-only) GRANT_ONLY=1; shift ;;
    --owner) OWNER="$2"; shift 2 ;;
    --instance) INSTANCE="$2"; shift 2 ;;
    --device-name) DEVICE_NAME="$2"; shift 2 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

[[ "$(id -u)" -eq 0 ]] || { echo "需要 root（要以 $SERVICE_USER 身份写状态目录）" >&2; exit 1; }
if [[ -n "$GRANT_ONLY" ]]; then
  # 只落盘授权串：设备已注册过（重复 device-register 会被服务端按"公钥/设备已存在"拒绝），
  # 而授权串要在设备存在之后才能生成，所以这两件事必须能分开跑。
  [[ -n "$GRANT" ]] || { echo "--grant-only 需要同时给 --grant" >&2; exit 2; }
  STATE_DIR="/var/lib/synapforge-agent/$INSTANCE"
  WORKSPACE="/srv/synapforge/$INSTANCE"
  [[ -n "$URL" ]] || { echo "--grant-only 仍需 --url" >&2; exit 2; }
  echo "=== 只落盘授权串（跑 20 秒即退，期间不领任务）==="
  set +e
  sudo -u "$SERVICE_USER" -H timeout 20 "$PYTHON" "$AGENTD" --url "$URL" daemon-run \
    --grant "$GRANT" --state-dir "$STATE_DIR" --workspace "$WORKSPACE" \
    --credential-backend file --start-paused >/dev/null 2>&1
  status=$?
  set -e
  [[ $status -eq 0 || $status -eq 124 ]] || { echo "落盘失败（退出码 $status）" >&2; exit 1; }
  echo "完成。启动：systemctl enable --now map-agent@$INSTANCE"
  exit 0
fi
[[ -n "$URL" && -n "$AGENT_ID" && -n "$DEVICE_ID" && -n "$PAIRING_FILE" ]] || {
  echo "必填：--url --agent-id --device-id --pairing-file" >&2; exit 2; }
[[ -f "$PAIRING_FILE" ]] || { echo "配对串文件不存在：$PAIRING_FILE" >&2; exit 2; }

STATE_DIR="/var/lib/synapforge-agent/$INSTANCE"
WORKSPACE="/srv/synapforge/$INSTANCE"
run_as_agent() { sudo -u "$SERVICE_USER" -H "$@"; }

echo "=== 0/5 检查配对串（含过期时间）==="
mapfile -t PAIRING < <("$PYTHON" - "$PAIRING_FILE" <<'PY'
import base64, json, sys
from datetime import UTC, datetime

raw = open(sys.argv[1], encoding="utf-8").read().strip()
if raw.startswith("{"):
    # 也接受直接给 JSON 的情况（手工拼的、或从日志里抄的）
    data = json.loads(raw)
else:
    # 配对串是 base64url(JSON)（与 Web 向导页、connect-agent.ps1 一致）
    padded = raw.replace("-", "+").replace("_", "/")
    padded += "=" * ((4 - len(padded) % 4) % 4)
    data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
for field in ("pairing_id", "pairing_code", "challenge"):
    if not data.get(field):
        raise SystemExit(f"配对串缺少字段 {field}，请在向导页重新生成")
expires = data.get("expires_at")
if expires:
    when = datetime.fromisoformat(str(expires).replace("Z", "+00:00")).astimezone(UTC)
    if when < datetime.now(UTC):
        raise SystemExit(f"配对已过期（{expires}），请重新生成")
print(data["pairing_id"]); print(data["pairing_code"]); print(data["challenge"])
PY
)
[[ ${#PAIRING[@]} -eq 3 ]] || { echo "配对串解析失败" >&2; exit 2; }

echo "=== 1/5 登记 Agent（幂等）==="
# Agent 归属必须与配对创建者是同一成员，否则设备注册会被判 owner_mismatch —— 所以 --owner 传登录者 member_id
OWNER_ARGS=()
[[ -n "$OWNER" ]] && OWNER_ARGS=(--owner "$OWNER")
run_as_agent "$PYTHON" "$AGENTD" --url "$URL" register \
  --agent-id "$AGENT_ID" --display-name "$DEVICE_NAME" --workspace "$WORKSPACE" "${OWNER_ARGS[@]}" >/dev/null

echo "=== 2/5 设备身份密钥（Ed25519，落在状态目录）==="
KEY_PATH="$STATE_DIR/keys/$DEVICE_ID.key"
if [[ -f "$KEY_PATH" ]]; then
  echo "已有密钥，复用：$KEY_PATH"
else
  run_as_agent "$PYTHON" "$AGENTD" keygen --directory "$STATE_DIR/keys" --name "$DEVICE_ID" >/dev/null
  echo "已生成：$KEY_PATH"
fi

echo "=== 3/5 设备注册（私钥签名）==="
REGISTER_JSON="$(run_as_agent "$PYTHON" "$AGENTD" --url "$URL" device-register \
  "--pairing-code=${PAIRING[1]}" "--pairing-id=${PAIRING[0]}" "--challenge=${PAIRING[2]}" \
  --agent-id "$AGENT_ID" --device-id "$DEVICE_ID" --device-name "$DEVICE_NAME" \
  --private-key "$KEY_PATH" --platform linux --agent-version 0.1.0 \
  --capabilities task.claim)" || {
  cat <<'MSG' >&2
设备注册失败（上面那行 http_<状态码>:<服务端 detail> 是原因）。常见原因：
  device_public_key_already_registered —— 公钥已注册过别的设备：换 --device-id，或先在平台撤销旧设备
  device_pairing_not_pending / expired —— 配对码用过或过期：重新生成
  device_pairing_challenge_invalid     —— 配对串与服务端不一致：重新复制
  device_agent_owner_mismatch          —— Agent 与配对不是同一个成员创建的
MSG
  exit 1
}
DEVICE_TOKEN="$(printf '%s' "$REGISTER_JSON" | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["device_token"])')"

echo "=== 4/5 设备令牌写入 0600 文件（$SERVICE_USER 家目录外的状态目录）==="
printf '%s' "$DEVICE_TOKEN" | run_as_agent "$PYTHON" "$AGENTD" credential-save \
  --device-id "$DEVICE_ID" --token-stdin --credential-backend file --state-path "$STATE_DIR/agentd.db" >/dev/null
echo "已写入：$STATE_DIR/credentials/（0600，文件名是目标哈希、不含明文）"

if [[ -n "$GRANT" ]]; then
  echo "=== 5/5 首次落盘项目授权串（跑 20 秒即退，期间不领任务）==="
  set +e
  run_as_agent timeout 20 "$PYTHON" "$AGENTD" --url "$URL" daemon-run \
    --grant "$GRANT" --state-dir "$STATE_DIR" --workspace "$WORKSPACE" \
    --credential-backend file --start-paused >/dev/null 2>&1
  status=$?
  set -e
  # 124 = 被 timeout 正常掐断（授权串已落盘）；其它非 0 视为失败
  if [[ $status -ne 0 && $status -ne 124 ]]; then
    echo "授权串落盘失败（退出码 $status）。看细节：run_as_agent 手动跑一次不要 2>/dev/null。" >&2
    exit 1
  fi
  echo "项目授权串已落盘（worker.json + 0600 凭据）"
else
  echo "=== 5/5 跳过授权串（未给 --grant）：启动前必须给一次，否则领不到任务 ==="
fi

cat <<EOF

配对完成。启动：
  systemctl enable --now map-agent@$INSTANCE
  journalctl -u map-agent@$INSTANCE -f

自检（不启动常驻体也能看）：
  sudo -u $SERVICE_USER -H bash -lc 'opencode --version'          # 执行体在不在
  sudo cat $STATE_DIR/worker.json                                 # 身份与项目（不含令牌）
  sudo ls -l $STATE_DIR/credentials/                              # 凭据文件（0600、哈希命名）
EOF