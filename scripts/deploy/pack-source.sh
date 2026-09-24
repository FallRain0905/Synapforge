#!/usr/bin/env bash
# 打包「可部署源码」：排除本机构建产物与运行期数据，产出可上传的 tar.gz。
#
#   bash scripts/deploy/pack-source.sh [输出路径]
#
# 默认输出 dist-deploy/math-agent-platform-src.tar.gz。
# 注意：apps/api/vendor（本机自带的依赖副本）不打进去——服务器上用 venv + PyPI 安装；
#       apps/api/data（SQLite 与对象存储）不打进去——服务器从空库开始。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${1:-$ROOT/dist-deploy/math-agent-platform-src.tar.gz}"
mkdir -p "$(dirname "$OUT")"

cd "$ROOT"
EXCLUDES=(
  --exclude=./apps/web/node_modules
  --exclude=./apps/web/.next
  --exclude=./apps/desktop/node_modules
  --exclude=./apps/desktop/dist
  --exclude=./apps/api/vendor
  --exclude=./apps/api/data
  --exclude=./dist-sidecar
  --exclude=./dist-sidecar-build
  --exclude=./dist-deploy
  --exclude=./.infra
  --exclude=./.infra-downloads
  --exclude=./.venv
  --exclude=./.git
  --exclude='*/__pycache__'
  --exclude='*.pyc'
  --exclude=./tmp
)

# 防呆：Windows 上改过的 shell 脚本容易带 CR 行尾，上传到 Linux 会报 invalid option name。
python -X utf8 - "$ROOT/scripts/deploy" <<'PY'
import pathlib, sys
# 用 bytes([13]) 而不是转义序列：跨 shell/Python 多层写转义太容易写坏（这个坑踩过一次）
CR = bytes([13])
bad = [p.name for p in pathlib.Path(sys.argv[1]).glob('*') if p.is_file() and CR in p.read_bytes()]
if bad:
    print('以下部署文件含 CR 行尾（Linux 上会报 invalid option name），请先转成 LF：', file=sys.stderr)
    for name in bad:
        print('  ' + name, file=sys.stderr)
    raise SystemExit(1)
PY

tar "${EXCLUDES[@]}" -czf "$OUT" .

SIZE="$(du -h "$OUT" | cut -f1)"
COUNT="$(tar -tzf "$OUT" | wc -l)"
echo "打包完成：$OUT"
echo "  体积：$SIZE   条目数：$COUNT"
echo "  顶层内容："
tar -tzf "$OUT" | awk -F/ 'NF>1 {print $2}' | sort -u | head -20 | sed 's/^/    /'
