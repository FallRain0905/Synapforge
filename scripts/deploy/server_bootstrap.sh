#!/usr/bin/env bash
# 服务器一次性初始化（Ubuntu 22.04 / 24.04，root 执行，幂等可重复跑）。
#
#   bash server_bootstrap.sh --origin http://1.2.3.4 [--basic-auth-user map] [--basic-auth-pass 密码] \
#                            [--swap-gb 4] [--no-nginx]
#
# 做四件事：
#   1) 依赖：Python ≥3.11、Node 20（官方二进制，不引第三方源）、nginx、htpasswd、sqlite3
#   2) 内存保险：swap（4G 机器上 `next build` 峰值约 2.7G，必须有 swap 兜底）
#   3) 账号与目录：系统用户 map、/opt/math-agent-platform、/var/log/math-agent-platform、/etc/math-agent-platform
#   4) 进程与入口：map-api / map-web 两个 systemd 单元；nginx 反向代理 + Basic 认证（Bearer 直通）
#
# 设计说明（为什么这么配）：
#   - 平台 API 的 Store 是 SQLite（apps/api/app/store.py），DATABASE_URL 在应用里没有被读取，
#     PostgresRepository 只服务于特定脚本/契约，所以本部署**不需要 PostgreSQL**，省下 200MB+ 内存和运维面。
#   - 对象存储默认本地目录（OBJECT_STORE_BACKEND=local），也不需要 MinIO；要换 S3/MinIO 再设 S3_* 环境变量。
#   - uvicorn 单 worker：SQLite Store 是单连接串行化的，多 worker 会争锁。
set -euo pipefail

ORIGIN=""
BASIC_AUTH_USER="map"
BASIC_AUTH_PASS=""
SWAP_GB=4
WITH_NGINX=1
APP_USER="map"
APP_DIR="/opt/math-agent-platform"
LOG_DIR="/var/log/math-agent-platform"
ETC_DIR="/etc/math-agent-platform"
NODE_VERSION="v20.18.1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --origin) ORIGIN="${2:-}"; shift 2 ;;
    --basic-auth-user) BASIC_AUTH_USER="${2:-}"; shift 2 ;;
    --basic-auth-pass) BASIC_AUTH_PASS="${2:-}"; shift 2 ;;
    --swap-gb) SWAP_GB="${2:-4}"; shift 2 ;;
    --no-nginx) WITH_NGINX=0; shift ;;
    --app-dir) APP_DIR="${2:-}"; shift 2 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

if [[ -z "$ORIGIN" && "$WITH_NGINX" == "1" ]]; then
  echo "用法：bash server_bootstrap.sh --origin http://<服务器IP或域名>" >&2
  exit 2
fi
if [[ "$(id -u)" != "0" ]]; then
  echo "需要 root 执行（sudo bash server_bootstrap.sh ...）" >&2
  exit 2
fi

step() { echo; echo "=== $* ==="; }

step "0. 环境识别"
. /etc/os-release
echo "系统：${PRETTY_NAME:-unknown}  内核：$(uname -r)  架构：$(uname -m)"
echo "CPU：$(nproc) 核  内存：$(free -m | awk '/^Mem:/{print $2" MB"}')  磁盘：$(df -h / | awk 'NR==2{print $2" 总 / "$4" 可用"}')"

step "1. 安装系统依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl ca-certificates tar xz-utils sqlite3 >/dev/null
if [[ "$WITH_NGINX" == "1" ]]; then
  apt-get install -y -qq nginx apache2-utils >/dev/null
fi
echo "已安装：$(python3 --version)"

PY_OK=1
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  PY_OK=0
  echo "系统 Python < 3.11（代码用到 datetime.UTC），尝试安装 3.12"
  apt-get install -y -qq software-properties-common >/dev/null
  add-apt-repository -y ppa:deadsnakes/ppa >/dev/null 2>&1 || true
  apt-get update -qq
  apt-get install -y -qq python3.12 python3.12-venv >/dev/null
  if python3.12 -c 'import venv' 2>/dev/null; then
    PY_OK=1
    PYTHON_BIN=python3.12
    echo "已安装 python3.12"
  fi
fi
[[ "$PY_OK" == "1" ]] || { echo "Python ≥3.11 不可用，请手工处理后重跑" >&2; exit 1; }
PYTHON_BIN="${PYTHON_BIN:-python3}"

step "2. 安装 Node 20（官方二进制，校验版本）"
CURRENT_NODE="$(command -v node >/dev/null 2>&1 && node -v || true)"
if [[ "$CURRENT_NODE" == "$NODE_VERSION" ]]; then
  echo "已存在 $CURRENT_NODE，跳过"
else
  NODE_DIST="node-${NODE_VERSION}-linux-x64"
  mkdir -p /usr/local/lib/nodejs
  curl -fsSL "https://nodejs.org/dist/${NODE_VERSION}/${NODE_DIST}.tar.xz" -o /tmp/node.tar.xz
  tar -xJf /tmp/node.tar.xz -C /usr/local/lib/nodejs
  rm -f /tmp/node.tar.xz
  for bin in node npm npx; do
    ln -sf "/usr/local/lib/nodejs/${NODE_DIST}/bin/${bin}" "/usr/local/bin/${bin}"
  done
  echo "已安装 node $(node -v) / npm $(npm -v)"
fi

step "3. swap（构建前端时的内存保险）"
if swapon --show | grep -q .; then
  echo "已有 swap：$(swapon --show=NAME,SIZE --noheadings | tr '\n' ' ')"
elif [[ -f /swapfile ]]; then
  swapon /swapfile && echo "已启用既有 /swapfile"
else
  fallocate -l "${SWAP_GB}G" /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=$((SWAP_GB * 1024)) status=none
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "已创建并启用 ${SWAP_GB}G swap"
fi
sysctl -w vm.swappiness=20 >/dev/null

step "4. 账号与目录"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "$APP_USER"
  echo "已创建用户 $APP_USER"
fi
mkdir -p "$APP_DIR" "$LOG_DIR" "$ETC_DIR"
chown -R "$APP_USER:$APP_USER" "$APP_DIR" "$LOG_DIR"
chmod 750 "$APP_DIR" "$LOG_DIR" "$ETC_DIR"
echo "应用目录 $APP_DIR（owner $APP_USER）；日志 $LOG_DIR"

step "5. 运行参数 /etc/math-agent-platform/api.env"
# 配额默认值（代码内 2000 运行 / 5000 成果物 / 5GB）；存储配额下调，避免 50G 盘被单个项目吃满
cat > "$ETC_DIR/api.env" <<EOF
# 由 scripts/deploy/server_bootstrap.sh 生成；改完 systemctl restart map-api 生效
PLATFORM_CORS_ORIGINS=${ORIGIN}
PLATFORM_AUTH_MODE=development
PLATFORM_QUOTA_MAX_RUNS_PER_PROJECT=2000
PLATFORM_QUOTA_MAX_ARTIFACTS_PER_PROJECT=5000
PLATFORM_QUOTA_MAX_STORAGE_BYTES_PER_PROJECT=2147483648
PLATFORM_COST_PER_RUN=0
PLATFORM_COST_PER_GB_MONTH=0
OBJECT_STORE_BACKEND=local
EOF
chmod 640 "$ETC_DIR/api.env"
chown root:"$APP_USER" "$ETC_DIR/api.env"
echo "已写入（CORS 允许来源：$ORIGIN）"

step "6. systemd 单元"
cat > /etc/systemd/system/map-api.service <<EOF
[Unit]
Description=Math Agent Platform API (FastAPI + SQLite)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}/apps/api
EnvironmentFile=${ETC_DIR}/api.env
Environment=PYTHONPATH=${APP_DIR}:${APP_DIR}/apps/api
Environment=PYTHONUNBUFFERED=1
ExecStart=${APP_DIR}/venv/bin/python -X utf8 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=3
StandardOutput=append:${LOG_DIR}/api.log
StandardError=append:${LOG_DIR}/api.log
# 单 worker：SQLite Store 是单连接串行化的

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/map-web.service <<EOF
[Unit]
Description=Math Agent Platform Web (Next.js)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}/apps/web
Environment=NODE_ENV=production
Environment=PORT=3000
ExecStart=/usr/local/bin/node ${APP_DIR}/apps/web/node_modules/next/dist/bin/next start -p 3000 -H 127.0.0.1
Restart=always
RestartSec=3
StandardOutput=append:${LOG_DIR}/web.log
StandardError=append:${LOG_DIR}/web.log

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable map-api.service map-web.service >/dev/null
echo "已写入 map-api.service / map-web.service（未启动，等代码就位后由 release 脚本启动）"

if [[ "$WITH_NGINX" == "1" ]]; then
  step "7. nginx 反向代理 + Basic 认证"
  if [[ -z "$BASIC_AUTH_PASS" ]]; then
    BASIC_AUTH_PASS="$(head -c 12 /dev/urandom | base64 | tr -d '/+=' | head -c 16)"
    echo "未指定 --basic-auth-pass，已生成随机密码"
  fi
  HTPASSWD=/etc/nginx/.math-agent-platform.htpasswd
  htpasswd -bc "$HTPASSWD" "$BASIC_AUTH_USER" "$BASIC_AUTH_PASS" >/dev/null
  # nginx worker 以 www-data 运行，密码文件必须对它可读；只给 root:www-data 640，
  # 不要 600/640-root:root —— 那样认证请求会 500（nginx 日志：Permission denied）。
  chown root:www-data "$HTPASSWD"
  chmod 640 "$HTPASSWD"

  # 站点配置放在同级模板里（单一来源：脚本内联副本容易和线上文件漂移）
  if [[ ! -f "$SCRIPT_DIR/nginx-site.conf" ]]; then
    echo "缺少 $SCRIPT_DIR/nginx-site.conf（应与本脚本一同上传）" >&2
    exit 1
  fi
  for snippet in nginx-locations.conf nginx-locations-public.conf; do
    if [[ ! -f "$SCRIPT_DIR/$snippet" ]]; then
      echo "缺少 $SCRIPT_DIR/$snippet（应与本脚本一同上传）" >&2
      exit 1
    fi
    install -m 644 "$SCRIPT_DIR/$snippet" "/etc/nginx/snippets/${snippet/nginx-locations/math-agent-platform-locations}"
  done
  install -m 644 "$SCRIPT_DIR/nginx-site.conf" /etc/nginx/sites-available/math-agent-platform

  ln -sf /etc/nginx/sites-available/math-agent-platform /etc/nginx/sites-enabled/math-agent-platform
  rm -f /etc/nginx/sites-enabled/default
  nginx -t
  systemctl enable nginx >/dev/null
  systemctl reload nginx 2>/dev/null || systemctl restart nginx
  # 凭据留档：server_verify.sh 自检用，也方便部署后向使用者交付
  cat > "$ETC_DIR/credentials.txt" <<EOF
# Math Agent Platform 部署凭据（$(date -Iseconds)）
# 工作台入口（Basic 认证）
url: ${ORIGIN}
username: ${BASIC_AUTH_USER}
password: ${BASIC_AUTH_PASS}
# 改密码：htpasswd -b /etc/nginx/.math-agent-platform.htpasswd <用户> <新密码> && systemctl reload nginx
EOF
  chmod 600 "$ETC_DIR/credentials.txt"
  echo "nginx 已就绪；Basic 用户名 $BASIC_AUTH_USER，密码 $BASIC_AUTH_PASS（已存 $ETC_DIR/credentials.txt）"

  if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
    ufw allow 22/tcp >/dev/null || true
    ufw allow 80/tcp >/dev/null || true
    echo "ufw 已放行 22/80"
  fi
fi

step "完成"
cat <<EOF
已就绪：
  应用目录      ${APP_DIR}（等待上传源码）
  日志          ${LOG_DIR}/{api,web}.log
  运行参数      ${ETC_DIR}/api.env
  服务          map-api（127.0.0.1:8000）、map-web（127.0.0.1:3000）
  入口          ${ORIGIN:-<未配置 nginx>}
EOF
if [[ "$WITH_NGINX" == "1" ]]; then
  echo
  echo "Basic 认证：用户 $BASIC_AUTH_USER  密码 $BASIC_AUTH_PASS"
  echo "（改密码：htpasswd -b /etc/nginx/.math-agent-platform.htpasswd <用户> <新密码> && systemctl reload nginx）"
fi
echo
echo "下一步：上传源码包并执行 scripts/deploy/server_release.sh"