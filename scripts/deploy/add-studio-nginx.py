# synapforge studio 反代片段：追加进既有 server 块，不动其它 location。
#
# 用 python 注入而不是整份覆盖站点文件，避免踩掉 certbot 管理的 TLS 行。
#   python3 add-studio-nginx.py --site /etc/nginx/sites-enabled/agent.fallrain0905.top.conf \
#       --port 8896 --base-path /studio
#
# 幂等：已存在标记块时替换该块，不重复追加。

from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import sys
import time

BEGIN = "# >>> synapforge-studio (managed)"
END = "# <<< synapforge-studio (managed)"


def block(port: int, base_path: str) -> str:
    base = "/" + base_path.strip("/")
    return f"""{BEGIN}
    # 前端里的接口路径是绝对的 `/api/studio/...`，所以必须同时挂根路径。
    # nginx 按最长前缀匹配，这条会优先于 demo 站的 `location /api/`；
    # 而 demo 站自己不用 `/api/studio/` 这个命名空间，两边不会互相遮挡。
    location /api/studio/ {{
        proxy_pass http://127.0.0.1:{port}/api/studio/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # SSE：关缓冲，否则流式输出会攒到结束才吐
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
        chunked_transfer_encoding on;
    }}

    location {base}/api/studio/ {{
        proxy_pass http://127.0.0.1:{port}/api/studio/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
        chunked_transfer_encoding on;
    }}

    location {base}/ {{
        proxy_pass http://127.0.0.1:{port}/;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
    }}
    {END}"""


def inject(text: str, port: int, base_path: str) -> str:
    """把片段插到站点第一个 443 server 块里（没有就插到第一个 server 块）。"""

    new_block = block(port, base_path)
    pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.S)
    if pattern.search(text):
        return pattern.sub(lambda _: new_block, text, count=1)

    # 找第一个 server { ... } 块，在其开始行之后插入
    match = re.search(r"^server\s*\{", text, re.M)
    if not match:
        raise SystemExit("站点文件里没有 server 块，无法注入")
    insert_at = match.end()
    return text[:insert_at] + "\n" + new_block + "\n" + text[insert_at:]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--port", type=int, default=8896)
    parser.add_argument("--base-path", default="/studio")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    path = pathlib.Path(args.site)
    text = path.read_text(encoding="utf-8")
    updated = inject(text, args.port, args.base_path)
    if args.dry_run:
        print(updated)
        return 0
    if updated == text:
        print("已是最新，未改动")
        return 0
    backup = path.with_suffix(path.suffix + f".bak-{time.strftime('%Y%m%d%H%M%S')}")
    shutil.copy2(path, backup)
    path.write_text(updated, encoding="utf-8")
    print(f"已更新 {path}（备份 {backup.name}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
