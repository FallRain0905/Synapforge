#!/usr/bin/env python
"""远程部署用 SSH/SFTP 客户端（paramiko）。

为什么不用系统 ssh：本机（Windows Git Bash）没有 TTY，`ssh` 的密码提示无法交互输入，
也没有 sshpass/plink。paramiko 可以在脚本里完成认证，密码从文件或环境变量读，不写进输出。

用法：

    # 连通性与环境体检（只读）
    python scripts/deploy/remote.py check --host 1.2.3.4 --user root --password-file .secret

    # 执行命令（默认非交互；--sudo 走 sudo -S 喂密码）
    python scripts/deploy/remote.py exec --host 1.2.3.4 --user root --password-file .secret \
        --cmd "uname -a" [--sudo] [--timeout 600]

    # 上传文件 / 目录
    python scripts/deploy/remote.py put  --host ... --src local.tar.gz --dst /tmp/app.tar.gz [--mode 600]
    python scripts/deploy/remote.py put-dir --host ... --src ./dist --dst /opt/app

密码来源优先级：--password-file > 环境变量 MAP_DEPLOY_PASSWORD > --key-file（密钥认证）。
"""

from __future__ import annotations

import argparse
import os
import posixpath
import stat
import sys
from pathlib import Path

import paramiko

DEFAULT_PORT = 22
CONNECT_TIMEOUT = 20


def _password(args: argparse.Namespace) -> str | None:
    if args.password_file:
        value = Path(args.password_file).read_text(encoding="utf-8").strip()
        if not value:
            raise SystemExit(f"password file {args.password_file} is empty")
        return value
    value = os.environ.get("MAP_DEPLOY_PASSWORD")
    return value.strip() if value else None


def connect(args: argparse.Namespace) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    password = _password(args)
    kwargs: dict[str, object] = {
        "hostname": args.host,
        "port": args.port,
        "username": args.user,
        "timeout": CONNECT_TIMEOUT,
        "banner_timeout": CONNECT_TIMEOUT,
        "auth_timeout": CONNECT_TIMEOUT,
        "allow_agent": False,
    }
    if args.key_file:
        kwargs["key_filename"] = args.key_file
        kwargs["look_for_keys"] = False
    elif password:
        kwargs["password"] = password
        kwargs["look_for_keys"] = False
    else:
        raise SystemExit("没有可用的认证方式：给 --password-file / MAP_DEPLOY_PASSWORD，或 --key-file")
    client.connect(**kwargs)
    return client


def _wrap_sudo(command: str, password: str | None) -> tuple[str, str | None]:
    """把命令包进 sudo -S；返回 (真实命令, 需要写进 stdin 的密码)。"""

    if password is None:
        raise SystemExit("--sudo 需要密码（--key-file 场景请改用 root 或免密 sudo）")
    # -S 从 stdin 读密码；-p '' 关掉提示串，避免把提示混进 stdout
    return f"sudo -S -p '' bash -lc {paramiko.util.safe_string(command)!r}", password + "\n"


def run(
    client: paramiko.SSHClient,
    command: str,
    *,
    password: str | None = None,
    sudo: bool = False,
    timeout: float | None = None,
    quiet: bool = False,
) -> tuple[int, str, str]:
    if sudo:
        command, stdin_secret = _wrap_sudo(command, password)
    else:
        stdin_secret = None
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout, get_pty=False)
    if stdin_secret is not None:
        stdin.write(stdin_secret)
        stdin.flush()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    if not quiet:
        if out:
            sys.stdout.write(out if out.endswith("\n") else out + "\n")
        if err:
            sys.stderr.write(err if err.endswith("\n") else err + "\n")
    return code, out, err


def put_file(sftp: paramiko.SFTPClient, src: Path, dst: str, mode: int | None) -> None:
    directory = posixpath.dirname(dst)
    if directory:
        _makedirs(sftp, directory)
    sftp.put(str(src), dst)
    if mode is not None:
        sftp.chmod(dst, mode)


def _makedirs(sftp: paramiko.SFTPClient, path: str) -> None:
    parts: list[str] = []
    current = path
    while current and current != "/":
        parts.append(current)
        current = posixpath.dirname(current)
    for candidate in reversed(parts):
        try:
            sftp.stat(candidate)
        except FileNotFoundError:
            sftp.mkdir(candidate)


def put_dir(sftp: paramiko.SFTPClient, src: Path, dst: str) -> int:
    count = 0
    for path in sorted(src.rglob("*")):
        relative = path.relative_to(src).as_posix()
        target = posixpath.join(dst, relative)
        if path.is_dir():
            _makedirs(sftp, target)
            continue
        _makedirs(sftp, posixpath.dirname(target))
        sftp.put(str(path), target)
        if path.suffix in {".sh", ".py"} or path.name in {"agentd"}:
            sftp.chmod(target, 0o755)
        count += 1
    return count


CHECK_SCRIPT = r"""
set -u
echo "== os =="; cat /etc/os-release 2>/dev/null | grep -E '^(PRETTY_NAME|VERSION_ID)=' || echo unknown
echo "== kernel/arch =="; uname -srm
echo "== cpu/mem =="; nproc; free -m | head -2
echo "== disk =="; df -h / /var /opt 2>/dev/null | grep -v tmpfs
echo "== swap =="; swapon --show 2>/dev/null || echo "no swap"
echo "== tools =="
for tool in python3 pip3 node npm nginx docker git curl tar sqlite3 ufw; do
  printf '%-9s ' "$tool"; command -v "$tool" >/dev/null 2>&1 && "$tool" --version 2>/dev/null | head -1 || echo "MISSING"
done
echo "== python version =="; python3 -c 'import sys; print(sys.version.split()[0])' 2>/dev/null || true
echo "== listening =="; (ss -lntp 2>/dev/null || netstat -lntp 2>/dev/null) | head -20
echo "== whoami/sudo =="; whoami; sudo -n true 2>&1 | head -1
echo "== outbound =="; (curl -sS -m 8 -o /dev/null -w 'pypi %{http_code}\n' https://pypi.org/simple/ || echo 'pypi unreachable')
(npm ping 2>/dev/null | head -2) || true
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="remote SSH/SFTP helper for deployment")
    parser.add_argument("action", choices=["check", "exec", "put", "put-dir", "get"])
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--password-file")
    parser.add_argument("--key-file")
    parser.add_argument("--cmd", help="exec: 要执行的命令")
    parser.add_argument("--sudo", action="store_true", help="用 sudo -S 执行")
    parser.add_argument("--timeout", type=float, default=None, help="命令超时秒数（默认不限）")
    parser.add_argument("--src", help="put/put-dir/get: 本地路径")
    parser.add_argument("--dst", help="put/put-dir/get: 远端路径")
    parser.add_argument("--mode", help="put: 上传后的八进制权限，如 600")
    args = parser.parse_args()

    password = _password(args)
    client = connect(args)
    try:
        if args.action == "check":
            code, _, _ = run(client, CHECK_SCRIPT, password=password, sudo=args.sudo, timeout=args.timeout)
            return code
        if args.action == "exec":
            if not args.cmd:
                raise SystemExit("exec 需要 --cmd")
            code, _, _ = run(client, args.cmd, password=password, sudo=args.sudo, timeout=args.timeout)
            return code
        sftp = client.open_sftp()
        try:
            if args.action == "put":
                mode = int(args.mode, 8) if args.mode else None
                put_file(sftp, Path(args.src), args.dst, mode)
                print(f"uploaded {args.src} -> {args.dst}")
            elif args.action == "put-dir":
                count = put_dir(sftp, Path(args.src), args.dst)
                print(f"uploaded {count} files -> {args.dst}")
            else:  # get
                sftp.get(args.src, args.dst)
                print(f"downloaded {args.src} -> {args.dst}")
        finally:
            sftp.close()
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())