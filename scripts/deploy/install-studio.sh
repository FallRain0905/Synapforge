#!/usr/bin/env bash
# 在服务器上装 synapforge studio（独立实例，不动同机的 map-api/map-web/HyperChE）。
#
#   bash install-studio.sh [--port 8896] [--origin https://example.com] [--base-path /studio]
#
# 幂等：重复跑只更新 venv 依赖、单元文件和 nginx location。
# 前置：源码已解包到 /opt/synapforge-studio（脚本自己会做，如果还没解）。
set -euo pipefail

PORT=8896
ORIGIN=""
BASE_PATH="/studio"
APP_DIR=/opt/synapforge-studio
SRC_DIR=/opt/math-agent-platform
USER_NAME=map

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --origin) ORIGIN="$2"; shift 2 ;;
    --base-path) BASE_PATH="$2"; shift 2 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

say() { printf '\n== %s ==\n' "$*"; }

say "前置检查"
id "$USER_NAME" >/dev/null 2>&1 || { echo "缺少用户 $USER_NAME（先跑 server_bootstrap.sh）" >&2; exit 1; }
[[ -d "$SRC_DIR" ]] || { echo "缺少源码目录 $SRC_DIR" >&2; exit 1; }
ss -ltn | grep -q ":$PORT " && echo "注意：$PORT 已被占用（若是上次的 studio，重启即可）"

say "代码目录"
# studio 复用 apps/api 的协议实现，所以整棵源码树都要在；
# 这里只做一层指向，避免再复制一份 2.6MB 源码（两份真源会立刻漂移）。
if [[ -e "$APP_DIR" && ! -L "$APP_DIR" ]]; then
  echo "$APP_DIR 已存在且不是软链，保留原样（请人工确认）" >&2
  exit 1
fi
[[ -L "$APP_DIR" ]] || ln -s "$SRC_DIR" "$APP_DIR"
echo "$APP_DIR -> $(readlink -f "$APP_DIR")"

say "venv 与依赖"
VENV="$SRC_DIR/.venv"
[[ -x "$VENV/bin/python" ]] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$SRC_DIR/requirements.txt"
"$VENV/bin/python" -c "import fastapi, uvicorn, pydantic; print('deps ok', pydantic.VERSION)"

say "运行目录"
install -d -o "$USER_NAME" -g "$USER_NAME" -m 750 /var/lib/synapforge-studio
install -d -o "$USER_NAME" -g "$USER_NAME" -m 750 /var/log/synapforge-studio
install -d -m 750 /etc/synapforge-studio

# map 用户的 home 就是源码目录（既有 bootstrap 建的，nologin）；
# 用 getent 取真实值，别假设 /home/map 存在（踩过一次：226/NAMESPACE）。
USER_HOME="$(getent passwd "$USER_NAME" | cut -d: -f6)"
[[ -d "$USER_HOME" ]] || USER_HOME="$SRC_DIR"
echo "HOME=$USER_HOME"

say "systemd 单元"
cat >/etc/systemd/system/synapforge-studio.service <<EOF
[Unit]
Description=synapforge studio（本地 Agent 集群控制台，反代在 $BASE_PATH）
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
Group=$USER_NAME
WorkingDirectory=$SRC_DIR
Environment=HOME=$USER_HOME
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
# 状态目录必须显式指向运行目录：默认是 ~/.synapforge，而这台机器上 map 的 home
# 就是源码目录，工作区/成果物会跟着落进 /opt（踩过一次）。
Environment=SYNAPFORGE_STATE_DIR=/var/lib/synapforge-studio
Environment=SYNAPFORGE_STUDIO_HOST=127.0.0.1
Environment=SYNAPFORGE_STUDIO_BASE_PATH=$BASE_PATH
Environment=SYNAPFORGE_STUDIO_PUBLIC_URL=$ORIGIN$BASE_PATH
EnvironmentFile=-/etc/synapforge-studio/studio.env
ExecStart=$VENV/bin/python $SRC_DIR/scripts/synapforge_studio.py \\
  --port $PORT --no-browser --host 127.0.0.1 \\
  --base-path $BASE_PATH \\
  --state /var/lib/synapforge-studio/state.db
Restart=always
RestartSec=3
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF
chmod 644 /etc/systemd/system/synapforge-studio.service

say "环境文件（Origin 白名单）"
ENV_FILE=/etc/synapforge-studio/studio.env
if [[ -n "$ORIGIN" ]]; then
  # 只写白名单这一行，不动其它人工维护的内容
  touch "$ENV_FILE"
  chmod 640 "$ENV_FILE"; chown "root:$USER_NAME" "$ENV_FILE"
  if grep -q '^SYNAPFORGE_STUDIO_ALLOWED_ORIGINS=' "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^SYNAPFORGE_STUDIO_ALLOWED_ORIGINS=.*|SYNAPFORGE_STUDIO_ALLOWED_ORIGINS=$ORIGIN|" "$ENV_FILE"
  else
    printf 'SYNAPFORGE_STUDIO_ALLOWED_ORIGINS=%s\n' "$ORIGIN" >>"$ENV_FILE"
  fi
  echo "白名单：$ORIGIN"
else
  echo "未给 --origin，跳过（公网访问会 403 origin_rejected）"
fi

say "systemd 启动"
systemctl daemon-reload
systemctl enable synapforge-studio.service >/dev/null
systemctl restart synapforge-studio.service
sleep 2
systemctl is-active synapforge-studio.service || { journalctl -u synapforge-studio -n 40 --no-pager; exit 1; }

say "本机探活"
for i in $(seq 1 10); do
  if curl -fsS -m 5 "http://127.0.0.1:$PORT$BASE_PATH/" -o /tmp/studio-index.html; then
    echo "页面 OK（$(wc -c </tmp/studio-index.html) 字节）"
    break
  fi
  sleep 1
done
TOKEN=$(python3 - <<'PY'
import json, pathlib
p = pathlib.Path('/var/lib/synapforge-studio/studio.json')
try:
    print(json.loads(p.read_text()).get('token', ''))
except Exception:
    print('')
PY
)
if [[ -n "$TOKEN" ]]; then
  curl -fsS -m 5 -H "X-Studio-Token: $TOKEN" "http://127.0.0.1:$PORT$BASE_PATH/api/studio/state" \
    | head -c 200; echo
else
  echo "未取到 token（studio.json 可能还没写出来）"
fi

echo
echo "完成。入口：${ORIGIN}${BASE_PATH}/?token=<见 /var/lib/synapforge-studio/studio.json>"
