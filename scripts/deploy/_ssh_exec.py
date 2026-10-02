"""执行体 SSH 助手（paramiko）：本地没有 sshpass，统一走这里。

用法：
    python scripts/deploy/_ssh_exec.py "命令"            # root@XX.XX.XX.XX
    python scripts/deploy/_ssh_exec.py --user synapforge "命令"

只用于**运维探查与部署**（命令经 shell 原样执行）。凭据**不进仓库**：
密码从环境变量 `EXECUTOR_PASSWORD` 读，或从本机密钥文件 `~/.ssh/executor.secret` 读
（与既有的 `~/.ssh/map-deploy.secret` 同一套做法）。主机名可用 `EXECUTOR_HOST` 覆盖。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import paramiko

HOST = os.environ.get("EXECUTOR_HOST", "XX.XX.XX.XX")
SECRET_FILE = Path.home() / ".ssh" / "executor.secret"


def _password() -> str:
    from_env = os.environ.get("EXECUTOR_PASSWORD")
    if from_env:
        return from_env
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text(encoding="utf-8").strip()
    raise SystemExit(
        f"缺少执行体密码：设 EXECUTOR_PASSWORD，或把密码写进 {SECRET_FILE}（0600，不进仓库）"
    )


def run(command: str, *, user: str = "root", timeout: float = 120.0) -> int:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, port=22, username=user, password=_password(), timeout=20.0, banner_timeout=30.0)
    try:
        _, stdout, stderr = client.exec_command(command, timeout=timeout, get_pty=False)
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
    finally:
        client.close()
    sys.stdout.write(out)
    if err.strip():
        sys.stdout.write("\n[stderr]\n" + err)
    return code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command")
    parser.add_argument("--user", default="root")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    return run(args.command, user=args.user, timeout=args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())