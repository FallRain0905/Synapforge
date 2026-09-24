#!/usr/bin/env bash
# 发布/升级（root 执行，可反复跑）。前提：先跑过 server_bootstrap.sh，并把源码包上传到服务器。
#
#   bash server_release.sh --tarball /tmp/map-src.tar.gz --public-url http://1.2.3.4
#
# 流程：解包 → venv + pip → npm ci + 前端构建（限内存）→ 重启 → 健康检查。
# 运行期数据（apps/api/data）不在源码包里，解包不会碰它。
set -euo pipefail

TARBALL=""
PUBLIC_URL=""
APP_USER="map"
APP_DIR="/opt/math-agent-platform"
LOG_DIR="/var/log/math-agent-platform"
ETC_DIR="/etc/math-agent-platform"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tarball) TARBALL="${2:-}"; shift 2 ;;
    --public-url) PUBLIC_URL="${2:-}"; shift 2 ;;
    --app-dir) APP_DIR="${2:-}"; shift 2 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

[[ "$(id -u)" == "0" ]] || { echo "需要 root 执行" >&2; exit 2; }
[[ -n "$TARBALL" && -f "$TARBALL" ]] || { echo "用法：bash server_release.sh --tarball <包路径> --public-url http://<地址>" >&2; exit 2; }
[[ -n "$PUBLIC_URL" ]] || { echo "--public-url 必填（前端构建期内联，浏览器要访问这个地址）" >&2; exit 2; }
if [[ "$PUBLIC_URL" == *127.0.0.1* || "$PUBLIC_URL" == *localhost* ]]; then
  echo "警告：--public-url=$PUBLIC_URL 是回环地址，别的机器打不开工作台（确实如此请忽略）" >&2
fi

mkdir -p "$LOG_DIR"
BUILD_LOG="$LOG_DIR/build.log"
PYTHON_BIN="$(command -v python3.12 || command -v python3)"
# 脚本目录必须在任何 cd 之前解析成绝对路径：本脚本后面会 cd 到前端目录构建，
# 那时 $(dirname "${BASH_SOURCE[0]}") 会变成相对当前目录的 "."，导致找不到同级脚本。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say() { echo; echo "=== $* ==="; }

say "1. 解包源码到 $APP_DIR"
tar -xzf "$TARBALL" -C "$APP_DIR"
echo "源码就位：$(ls "$APP_DIR" | tr '\n' ' ')"
# 运行数据目录必须存在且属于应用账号（Store 启动时会自建表结构）
mkdir -p "$APP_DIR/apps/api/data"
chown -R "$APP_USER:$APP_USER" "$APP_DIR/apps/api/data"

say "2. Python 虚拟环境与依赖（$PYTHON_BIN）"
if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$APP_DIR/venv"
  echo "已创建 venv"
fi
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/apps/api/requirements.txt"
echo "已安装："
"$APP_DIR/venv/bin/pip" list --format=freeze 2>/dev/null | grep -Ei "^(fastapi|uvicorn|pydantic|python-multipart|cryptography)=" | sed 's/^/  /'
# SQLite 侧不需要 psycopg/boto3；要用 S3/MinIO 或 PostgreSQL 时再装 requirements-prod.txt

say "3. 前端依赖与构建（构建期内联 NEXT_PUBLIC_API_URL=$PUBLIC_URL）"
cd "$APP_DIR/apps/web"
export NODE_OPTIONS="--max-old-space-size=1536"   # 4G 机器 + swap：把 V8 堆压到 1.5G，宁可慢不要 OOM
export NEXT_TELEMETRY_DISABLED=1
export NEXT_PUBLIC_API_URL="$PUBLIC_URL"
if [[ ! -d node_modules || ! -f node_modules/.package-lock.json ]]; then
  echo "npm ci（首次安装，约 1-2 分钟）"
  npm ci --no-audit --no-fund 2>&1 | tail -5
else
  echo "node_modules 已存在，跳过 npm ci（依赖有变动请删除后重跑）"
fi
echo "next build（跳过 lint 以省内存，类型检查保留）"
if ! npm run build -- --no-lint 2>&1 | tee "$BUILD_LOG" | tail -25; then
  echo "构建失败，完整日志：$BUILD_LOG" >&2
  tail -40 "$BUILD_LOG" >&2
  exit 1
fi
grep -E "Generating static pages|✓ Compiled|Route \(app\)" "$BUILD_LOG" | tail -3 || true

say "4. 权限与重启"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
systemctl restart map-api.service map-web.service
sleep 4
systemctl is-active map-api.service map-web.service

say "5. 健康检查"
bash "$SCRIPT_DIR/server_verify.sh" --expect-public-url "$PUBLIC_URL" || {
  echo "健康检查未通过；排查：journalctl -u map-api -n 50 / $LOG_DIR/*.log" >&2
  exit 1
}
echo
echo "发布完成。后续升级：重新打包上传 → 重跑本脚本（数据目录不受影响）。"