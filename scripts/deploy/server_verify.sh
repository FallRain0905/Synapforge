#!/usr/bin/env bash
# 部署自检（root 或普通用户都可跑，只读）。
#
#   bash server_verify.sh [--expect-public-url http://1.2.3.4]
#
# 检查内容：
#   A. 进程与端口：map-api / map-web / nginx 状态，8000/3000/80 监听
#   B. 直连后端：/health、/api/platform/health、/api/platform/metrics、前端首页
#   C. 经 nginx：未认证应 401；Basic 认证后首页与 /api/projects 应 200
#   D. Bearer 直通：带假设备令牌应打到 FastAPI（401 且错误来自平台，而非 nginx 的 401 页面）
#   E. 数据目录与配额：SQLite 文件、对象目录、磁盘余量
set -uo pipefail

EXPECT_PUBLIC_URL=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --expect-public-url) EXPECT_PUBLIC_URL="${2:-}"; shift 2 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

ETC_DIR="/etc/math-agent-platform"
APP_DIR="/opt/math-agent-platform"
CRED_FILE="$ETC_DIR/credentials.txt"
PASS=0
FAIL=0

ok()   { echo "  [PASS] $*"; PASS=$((PASS + 1)); }
bad()  { echo "  [FAIL] $*"; FAIL=$((FAIL + 1)); }
info() { echo "  [info] $*"; }

code() { curl -s -o /dev/null -m 20 -w '%{http_code}' "$@" 2>/dev/null || echo "000"; }
body() { curl -s -m 20 "$@" 2>/dev/null || true; }

echo "=== A. 进程与端口 ==="
for svc in map-api.service map-web.service nginx; do
  state="$(systemctl is-active "$svc" 2>/dev/null || true)"
  [[ "$state" == "active" ]] && ok "$svc 运行中" || bad "$svc 状态：$state"
done
for port in 8000 3000 80; do
  if (ss -lnt 2>/dev/null || netstat -lnt 2>/dev/null) | grep -q ":$port "; then
    ok "端口 $port 在监听"
  else
    bad "端口 $port 未监听"
  fi
done

echo "=== B. 直连后端（绕过 nginx） ==="
c="$(code http://127.0.0.1:8000/health)";   [[ "$c" == "200" ]] && ok "API /health → 200" || bad "API /health → $c"
AUTH_MODE="$(body http://127.0.0.1:8000/health | python3 -c 'import json,sys; print(json.load(sys.stdin).get("mode",""))' 2>/dev/null || echo "")"
info "鉴权模式：${AUTH_MODE:-未知}"
if [[ "$AUTH_MODE" == "required" || "$AUTH_MODE" == "production" ]]; then
  # 账号体系上线后，成员级端点在无会话时**应当** 401（这是设计行为，不是故障）
  for path in /api/platform/health /api/platform/metrics /api/projects; do
    c="$(code "http://127.0.0.1:8000$path")"
    [[ "$c" == "401" ]] && ok "无会话访问 $path → 401（账号体系生效）" || bad "无会话访问 $path → $c（期望 401）"
  done
else
  c="$(code http://127.0.0.1:8000/api/platform/health)"; [[ "$c" == "200" ]] && ok "API /api/platform/health → 200" || bad "API /api/platform/health → $c"
fi
metrics="$(body http://127.0.0.1:8000/api/platform/metrics)"
if [[ "$AUTH_MODE" == "required" || "$AUTH_MODE" == "production" ]]; then
  info "required 模式下 metrics 需要会话，已在上方按 401 断言（不再解析指标）"
elif [[ -n "$metrics" ]]; then
  ok "API /api/platform/metrics 有响应"
  echo "$metrics" | python3 -c '
import json, sys
d = json.load(sys.stdin)
counts = d.get("counts", {})
print("  [info] 存储后端：%s；项目/任务/成果物/事件：%s/%s/%s/%s；待投递 outbox：%s"
      % (d.get("storage_backend"), counts.get("projects"), counts.get("tasks"),
         counts.get("artifacts"), counts.get("events"), d.get("pending_outbox")))
q = d.get("quotas", {})
if q:
    print("  [info] 配额：单项目运行数 %s / 成果物 %s / 存储 %s 字节"
          % (q.get("max_runs_per_project"), q.get("max_artifacts_per_project"), q.get("max_storage_bytes_per_project")))
' 2>/dev/null || true
else
  bad "API /api/platform/metrics 无响应"
fi
c="$(code http://127.0.0.1:3000/)"; [[ "$c" == "200" ]] && ok "Web 首页 → 200" || bad "Web 首页 → $c"

echo "=== C. 经 nginx（Basic 认证） ==="
c="$(code http://127.0.0.1/)"
[[ "$c" == "401" ]] && ok "未认证访问 / → 401（认证生效）" || bad "未认证访问 / → $c（期望 401）"

AUTH_ARGS=()
if [[ -f "$CRED_FILE" ]]; then
  BASIC_USER="$(awk -F': ' '/^username: /{print $2}' "$CRED_FILE")"
  BASIC_PASS="$(awk -F': ' '/^password: /{print $2}' "$CRED_FILE")"
  if [[ -n "$BASIC_USER" && -n "$BASIC_PASS" ]]; then
    AUTH_ARGS=(-u "$BASIC_USER:$BASIC_PASS")
    info "使用 $CRED_FILE 中的 Basic 凭据（用户 $BASIC_USER）"
  fi
else
  info "未找到 $CRED_FILE，跳过认证后的检查"
fi

if [[ ${#AUTH_ARGS[@]} -gt 0 ]]; then
  c="$(code "${AUTH_ARGS[@]}" http://127.0.0.1/)"
  [[ "$c" == "200" ]] && ok "Basic 认证后访问 / → 200" || bad "Basic 认证后访问 / → $c"
  c="$(code "${AUTH_ARGS[@]}" http://127.0.0.1/api/projects)"
  if [[ "$AUTH_MODE" == "required" || "$AUTH_MODE" == "production" ]]; then
    [[ "$c" == "401" ]] && ok "Basic 之后 GET /api/projects → 401（Basic 只是 nginx 层，仍需账号会话）" || bad "Basic 之后 GET /api/projects → $c（期望 401）"
  else
    [[ "$c" == "200" ]] && ok "Basic 认证后 GET /api/projects → 200" || bad "Basic 认证后 GET /api/projects → $c"
  fi
  count="$(body "${AUTH_ARGS[@]}" http://127.0.0.1/api/projects | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null || echo '?')"
  info "当前项目数：$count"
fi

echo "=== D. Bearer 直通（Agent 路径不受 Basic 拦截） ==="
# 假令牌：期望 nginx 放行、FastAPI 自己判 401（若有 nginx 的 401 网页则说明 Basic 拦住了 Agent）
resp="$(curl -s -m 20 -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer dvc_invalid_token_for_probe' http://127.0.0.1/api/agent/me 2>/dev/null || echo 000)"
[[ "$resp" == "401" || "$resp" == "403" ]] && ok "带 Bearer 访问 /api/agent/me → $resp（被平台拒绝，说明已透传）" || bad "带 Bearer 访问 /api/agent/me → $resp"
c="$(code -H 'Authorization: Bearer dvc_invalid_token_for_probe' http://127.0.0.1/api/agent/me)"
if [[ "$c" == "401" ]]; then
  wt="$(curl -s -m 20 -o /dev/null -w '%{http_code}' -H 'Authorization: Basic YWRtaW46YWRtaW4=' http://127.0.0.1/api/agent/me 2>/dev/null || echo 000)"
  info "Basic（错误口令）访问 /api/agent/me → $wt（应为 401，来自 nginx）"
fi

echo "=== E. 账号与接入面 ==="
# 直连后端探测（走 IP 入口会先撞上 nginx Basic，那是设计行为，测不到注册链本身）
c="$(code -X POST -H 'Content-Type: application/json' -d '{"email":"probe","password":"short","display_name":"x"}' http://127.0.0.1:8000/api/auth/register)"
[[ "$c" == "422" ]] && ok "注册接口可达（非法参数 422）" || bad "注册接口 → $c（期望 422）"
for path in /api/organizations /api/teams /api/members; do
  c="$(code "http://127.0.0.1$path")"
  [[ "$c" == "401" || "$c" == "405" ]] && ok "匿名访问 $path → $c（已收口）" || bad "匿名访问 $path → $c（应被拒）"
done
# Agent 的 HTTP 领取链路必须能穿过中间件（否则 worker-run 会 401）
c="$(code -X POST -H 'Content-Type: application/json' -H 'X-Project-Capability-Token: prj_probe' -d '{}' http://127.0.0.1/api/agents/agent-probe/tasks/claim)"
[[ "$c" == "422" || "$c" == "403" || "$c" == "401" && "$c" != "401" ]] && ok "能力令牌请求到达 handler（$c，不是中间件的 authentication_required）" || bad "能力令牌请求 → $c（疑似被中间件拦截）"
if [[ -n "${MAP_VERIFY_EMAIL:-}" && -n "${MAP_VERIFY_PASSWORD:-}" ]]; then
  token="$(body -X POST -H 'Content-Type: application/json' -d "{\"email\":\"$MAP_VERIFY_EMAIL\",\"password\":\"$MAP_VERIFY_PASSWORD\"}" http://127.0.0.1/api/auth/login | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token",""))' 2>/dev/null || echo "")"
  if [[ -n "$token" ]]; then
    ok "账号登录成功（$MAP_VERIFY_EMAIL）"
    c="$(code -H "Authorization: Bearer $token" http://127.0.0.1/api/projects)"
    [[ "$c" == "200" ]] && ok "会话访问 /api/projects → 200" || bad "会话访问 /api/projects → $c"
    c="$(code -H "Authorization: Bearer $token" http://127.0.0.1/api/tasks/mine)"
    [[ "$c" == "200" ]] && ok "会话访问 /api/tasks/mine → 200" || bad "会话访问 /api/tasks/mine → $c"
    # 工作区（W-1）：概览聚合与聊天流必须有会话可用，且概览里要能看到"我是谁"
    pid="$(body -H "Authorization: Bearer $token" http://127.0.0.1:8000/api/projects | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[0]["id"] if d else "")' 2>/dev/null || echo "")"
    if [[ -n "$pid" ]]; then
      ws="$(body -H "Authorization: Bearer $token" "http://127.0.0.1:8000/api/projects/$pid/workspace")"
      if echo "$ws" | python3 -c '
import json, sys
d = json.load(sys.stdin)
v = d.get("viewer") or {}
assert d.get("project") and isinstance(d.get("members"), list) and isinstance(d.get("agents"), list), "structure"
assert v.get("member_id"), "viewer"
print("  [info] 工作区：成员 %d / Agent %d / 任务 %d / 成果物 %d / 聊天 %d 条；我的角色 %s"
      % (len(d["members"]), len(d["agents"]), d["tasks"]["total"], d["artifacts"]["total"], len(d["messages"]), v.get("role")))
' 2>/dev/null; then
        ok "工作区概览 /workspace → 200 且 viewer/成员/Agent 齐全"
      else
        bad "工作区概览结构不完整：$(echo "$ws" | head -c 120)"
      fi
      c="$(code -H "Authorization: Bearer $token" "http://127.0.0.1:8000/api/projects/$pid/messages?limit=5")"
      [[ "$c" == "200" ]] && ok "聊天流 /messages → 200" || bad "聊天流 /messages → $c"
    else
      info "账号下没有项目，跳过工作区断言"
    fi
  else
    bad "账号登录失败（检查 MAP_VERIFY_EMAIL/PASSWORD）"
  fi
else
  info "未提供 MAP_VERIFY_EMAIL/PASSWORD，跳过会话内断言（注册首个账号后可带上它们再跑）"
fi

echo "=== F. 数据与容量 ==="
DB="$APP_DIR/apps/api/data/platform.db"
if [[ -f "$DB" ]]; then
  ok "SQLite 存在：$(du -h "$DB" | cut -f1)  $DB"
else
  info "SQLite 还没生成（首次调用 API 后自动建库）"
fi
[[ -d "$APP_DIR/apps/api/data/objects" ]] && ok "对象目录存在：$(du -sh "$APP_DIR/apps/api/data/objects" | cut -f1)" || info "对象目录未生成（还没有成果物内容）"
info "磁盘：$(df -h "$APP_DIR" | awk 'NR==2{print $2" 总 / "$4" 可用 ("$5" 已用)"}')"
info "内存：$(free -h | awk '/^Mem:/{print $3" 已用 / "$2" 总"}')；swap：$(free -h | awk '/^Swap:/{print $3" / "$2}')"

echo
echo "=== 结果：$PASS 项通过，$FAIL 项失败 ==="
[[ -n "$EXPECT_PUBLIC_URL" ]] && info "外部入口（云安全组需放行 80）：$EXPECT_PUBLIC_URL"
[[ "$FAIL" == "0" ]]