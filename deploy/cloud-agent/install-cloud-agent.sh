#!/usr/bin/env bash
# CLOUD-1 安装脚本：把 Agent 内核装到"云端执行体"服务器上（Ubuntu 22.04+，root 执行）。
#
# 只做三件事：建专用用户与目录、把内核装成 venv、把 systemd 模板单元放好。
# 不碰平台机，也不给这台机器任何平台运维凭据（用户第 ③ 条边界）。
#
# 用法（在服务器上）：
#   bash install-cloud-agent.sh --source /root/synapforge-agent-src --instance cloud
#
# 前置：源码已解包到 --source（包含 apps/agent 与 packages 两个目录）。
set -euo pipefail

INSTANCE="cloud"
SOURCE_DIR=""
SERVICE_USER="synapforge"
INSTALL_ROOT="/opt/synapforge-agent"
STATE_ROOT="/var/lib/synapforge-agent"
WORKSPACE_ROOT="/srv/synapforge"
SYSTEMD_DIR="/etc/systemd/system"
PYTHON_BIN="python3.12"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --instance) INSTANCE="$2"; shift 2 ;;
    --source) SOURCE_DIR="$2"; shift 2 ;;
    --user) SERVICE_USER="$2"; shift 2 ;;
    --python) PYTHON_BIN="$2"; shift 2 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

if [[ "$(id -u)" -ne 0 ]]; then
  echo "需要 root（建用户、装 systemd 单元）" >&2
  exit 1
fi
if [[ -z "$SOURCE_DIR" || ! -d "$SOURCE_DIR/apps/agent" ]]; then
  echo "--source 必须指向解包后的仓库根（含 apps/agent 与 packages）" >&2
  exit 2
fi

echo "=== 1/6 专用用户（非 root、无 sudo）==="
if id "$SERVICE_USER" >/dev/null 2>&1; then
  echo "用户已存在：$SERVICE_USER"
else
  useradd --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
  echo "已创建：$SERVICE_USER"
fi

echo "=== 2/6 Python $PYTHON_BIN ==="
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  # 内核用了 `from datetime import UTC`（3.11+）：Ubuntu 22.04 自带的 3.10 跑不了，
  # 所以这里显式装 3.12（与平台机同版本，语义一致）。
  echo "未找到 $PYTHON_BIN，从 deadsnakes 安装……"
  apt-get update -qq
  apt-get install -y -qq software-properties-common >/dev/null
  add-apt-repository -y ppa:deadsnakes/ppa >/dev/null
  apt-get update -qq
  apt-get install -y -qq "$PYTHON_BIN" "$PYTHON_BIN-venv" >/dev/null
fi
"$PYTHON_BIN" --version

echo "=== 3/6 部署内核源码到 $INSTALL_ROOT ==="
mkdir -p "$INSTALL_ROOT"
rm -rf "$INSTALL_ROOT/apps" "$INSTALL_ROOT/packages"
mkdir -p "$INSTALL_ROOT/apps"
cp -r "$SOURCE_DIR/apps/agent" "$INSTALL_ROOT/apps/agent"
cp -r "$SOURCE_DIR/packages" "$INSTALL_ROOT/packages"
# 运行时不需要的东西不带走：测试、缓存、Windows 专用适配（留着也不跑，但别让目录变脏）
find "$INSTALL_ROOT" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$INSTALL_ROOT" -name 'test_*.py' -delete 2>/dev/null || true
chown -R root:root "$INSTALL_ROOT"

echo "=== 4/6 venv 与依赖 ==="
"$PYTHON_BIN" -m venv "$INSTALL_ROOT/venv"
"$INSTALL_ROOT/venv/bin/python" -m pip install --quiet --upgrade pip
"$INSTALL_ROOT/venv/bin/python" -m pip install --quiet -r "$INSTALL_ROOT/apps/agent/requirements.txt" websockets
"$INSTALL_ROOT/venv/bin/python" -c "import cryptography, websockets; print('依赖就绪：cryptography', cryptography.__version__)"

echo "=== 5/7 opencode 执行体（以 $SERVICE_USER 身份装，不用 root）==="
# 用绝对路径判断：官方安装器把 PATH 写进该用户的 .bashrc，登录 shell 里 `command -v opencode` 看不到它
OPENCODE_BIN="$(getent passwd "$SERVICE_USER" | cut -d: -f6)/.opencode/bin/opencode"
if [[ -x "$OPENCODE_BIN" ]]; then
  echo "opencode 已装：$($OPENCODE_BIN --version)（$OPENCODE_BIN）"
else
  sudo -u "$SERVICE_USER" -H bash -lc 'curl -fsSL https://opencode.ai/install | bash' >/dev/null
  if [[ ! -x "$OPENCODE_BIN" ]]; then
    echo "opencode 安装失败：$OPENCODE_BIN 不存在" >&2
    exit 1
  fi
  echo "已安装：$($OPENCODE_BIN --version)（$OPENCODE_BIN）"
fi

echo "=== 6/7 对话角色（M-6：装到 opencode 自己的 agent 目录）==="
# 角色 = opencode 的一个 agent 定义文件（frontmatter 写说明与工具开关，正文即系统提示）。
# 单一真源在仓库 `deploy/cloud-agent/roles/*.md`：**只从这里装**，不要在服务器上手改——
# 手改会与仓库漂移，页面显示的说明与实际跑的角色就不是一回事了。
SERVICE_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
ROLES_SRC="$SCRIPT_DIR/roles"
ROLES_DST="$SERVICE_HOME/.config/opencode/agent"
# 只装 `mm-*.md`：角色一律以 `mm-` 开头；`SOURCES.md`（来源台账）之类**不是**角色，
# 装进去会让 opencode 看到一个没有 frontmatter 的文件（踩过：它会把台账也当 agent 加载）。
if [[ -d "$ROLES_SRC" ]] && compgen -G "$ROLES_SRC/mm-*.md" >/dev/null; then
  install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_USER" "$ROLES_DST"
  # 先清掉旧的 `mm-*.md`（我们只管自己装的角色；用户自己放的文件不动）
  rm -f "$ROLES_DST"/mm-*.md
  installed=0
  for role_file in "$ROLES_SRC"/mm-*.md; do
    install -m 0644 -o "$SERVICE_USER" -g "$SERVICE_USER" "$role_file" "$ROLES_DST/$(basename "$role_file")"
    installed=$((installed + 1))
  done
  echo "已安装 $installed 个角色到 $ROLES_DST：$(ls "$ROLES_DST" | tr '\n' ' ')"
  # 提示词 lint 是**交付前**的门槛（在开发机上跑）：这里只核对最小结构，防止装上一个空文件
  for role_file in "$ROLES_DST"/mm-*.md; do
    head -1 "$role_file" | grep -q '^---$' || { echo "角色文件缺 frontmatter：$role_file" >&2; exit 1; }
  done
  # M-6 R-4：写一份**部署清单**（角色名 → 内容 sha256 + 安装时间）。
  # 内核拿它当漂移判据：执行体上的 md 与清单不一致 = 有人手工改过（页面会标出来）。
  # 它不是角色（opencode 只认 *.md），放同目录便于"清单与文件一起搬"。
  python3 - "$ROLES_DST" <<'PY'
import hashlib, json, pathlib, sys
from datetime import datetime, timezone

directory = pathlib.Path(sys.argv[1])
roles = {}
for path in sorted(directory.glob("mm-*.md")):
    roles[path.stem] = hashlib.sha256(path.read_bytes()).hexdigest()
manifest = directory / ".mm-roles.json"
manifest.write_text(
    json.dumps({"installed_at": datetime.now(timezone.utc).isoformat(), "roles": roles}, ensure_ascii=False, indent=1),
    encoding="utf-8",
)
print(f"已写部署清单：{manifest.name}（{len(roles)} 个角色的 sha256）")
PY
  chown "$SERVICE_USER:$SERVICE_USER" "$ROLES_DST/.mm-roles.json" 2>/dev/null || true
  chmod 0644 "$ROLES_DST/.mm-roles.json" 2>/dev/null || true
else
  echo "没有角色文件（$ROLES_SRC/mm-*.md）：对话页只会显示「默认（无角色）」" >&2
fi

echo "=== 7/7 状态/工作目录与 systemd 模板单元 ==="
install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE_ROOT/$INSTANCE"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$WORKSPACE_ROOT/$INSTANCE"
install -d -m 0700 -o root -g root /etc/synapforge-agent
install -m 0644 "$SCRIPT_DIR/map-agent@.service" "$SYSTEMD_DIR/map-agent@.service"
systemctl daemon-reload
echo "已安装单元：$SYSTEMD_DIR/map-agent@.service（未启动，配对后再 start）"

cat <<EOF

安装完成。下一步（配对需要平台的配对码）：
  1) 平台「设备与接入」页生成配对串（15 分钟有效），并准备好「云端智能体」项目的授权串；
  2) 在本机执行：bash pair-cloud-agent.sh --url https://synapforge.top --agent-id <id> \\
       --device-id <id> --pairing <配对串文件> --grant <授权串>;
  3) 启动：systemctl enable --now map-agent@$INSTANCE

注意：模型 provider 的 key **不进** systemd env——内核给执行体传环境变量是按白名单的
（platform 不该知道模型厂商），所以 key 写在执行体自己的配置里（见 pair-cloud-agent.sh 的提示）。

角色（M-6）：装好的角色在 $ROLES_DST；平台页面的角色下拉读的是内核心跳里上报的
\`opencode agent list\` 结果 + 这些文件的 \`description\`。**改了角色要重跑本脚本的第 6 步**
（重跑整个脚本也行，它是幂等的），手改服务器上的文件会与仓库漂移。
EOF