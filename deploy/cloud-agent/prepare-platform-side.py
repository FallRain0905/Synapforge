#!/usr/bin/env python3
"""CLOUD-1 平台侧准备：登录 →（必要时）建项目 → 生成配对串 / 授权串。

为什么需要它：配对串与授权串平时是在 Web 向导页点的，而云端机器上没人点——
把同样的两步做成脚本，P2 要起第二个实例时也就不用再来一遍手工点击。

产物与 Web 向导页**完全一致**（`apps/web/lib/api.ts` 的 pairingBlob / projectGrantBlob：
base64url(JSON)，字段同名），所以 `pair-cloud-agent.sh` 与 Windows 的 connect-agent.ps1 都能直接吃。

用法：
  # 1) 配对前：生成配对串（15 分钟有效）
  python3 prepare-platform-side.py pairing --url https://synapforge.top \
      --email you@example.com --password '***' --out /root/pairing.json
  # 2) 设备注册完成后：生成授权串（只授权这一个项目）
  python3 prepare-platform-side.py grant --url https://synapforge.top \
      --email you@example.com --password '***' --project 云端智能体 \
      --device-id device-cloud-01 --agent-id agent-cloud-01 --out /root/grant.txt

注意：授权串里带一次性 project_token，等同短期凭据——用完删除，不要外传。
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.request
from typing import Any

DEFAULT_CAPABILITIES = [
    "task.claim",
    "task.lease",
    "task.progress",
    "task.result",
    "artifact.read",
    "artifact.write",
    "run.create",
    "run.complete",
    "run.event",
    "handoff.create",
    "handoff.accept",
    "handoff.reject",
    "review.submit",
]


def _call(url: str, method: str, path: str, token: str | None = None, payload: Any = None) -> Any:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(f"{url.rstrip('/')}{path}", data=body, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise SystemExit(f"{method} {path} 失败：http_{error.code}:{detail[:300]}") from error
    except urllib.error.URLError as error:
        raise SystemExit(f"{method} {path} 失败：{error.reason}") from error
    return json.loads(raw) if raw.strip() else None


def _blob(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _read_secret(value: str | None, path: str | None) -> str:
    if value:
        return value
    if path:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    raise SystemExit("需要 --password 或 --password-file")


def _login(url: str, email: str, password: str) -> tuple[str, dict]:
    """登录并取出 member。

    实测响应形状（AUTH-1 线上）：`{"token": …, "expires_at": …, "account": {"member": {…}, "is_admin": …}}`
    ——member 嵌在 account 里，不是顶层字段。
    """

    session = _call(url, "POST", "/api/auth/login", payload={"email": email, "password": password})
    account = session.get("account") if isinstance(session.get("account"), dict) else {}
    member = account.get("member") if isinstance(account.get("member"), dict) else session.get("member") or {}
    return session["token"], {**member, "is_admin": account.get("is_admin")}


def _find_project(url: str, token: str, name: str) -> dict | None:
    for project in _call(url, "GET", "/api/projects", token=token):
        if project.get("name") == name:
            return project
    return None


def command_pairing(args: argparse.Namespace) -> None:
    password = _read_secret(args.password, args.password_file)
    token, member = _login(args.url, args.email, password)
    project = _find_project(args.url, token, args.project)
    if project is None:
        if not args.create_project:
            raise SystemExit(
                f"项目「{args.project}」不存在。加 --create-project 让脚本建它（task_mode 默认 manual，先人工派单）。"
            )
        project = _call(args.url, "POST", "/api/projects", token=token, payload={
            "name": args.project,
            "description": "云端执行体专用项目：只授权给云端 Agent，先人工派单",
            "goal": "验证云端执行体接入与真跑；不承载对外演示",
            "task_mode": "manual",
            "organization_id": member.get("organization_id"),
        })
        print(f"已创建项目：{project['name']} ({project['id']})")
    else:
        print(f"复用项目：{project['name']} ({project['id']})")

    pairing = _call(args.url, "POST", "/api/devices/pairings", token=token, payload={
        "organization_id": project["organization_id"],
        "expires_in_seconds": args.expires_in,
    })
    blob = _blob({
        "pairing_id": pairing["id"],
        "pairing_code": pairing["pairing_code"],
        "challenge": pairing["challenge"],
        "expires_at": pairing["expires_at"],
    })
    _write(args.out, blob)
    # 设备注册会校验「Agent 归属 == 配对创建者」，所以把 member id 一起落盘，
    # 给 pair-cloud-agent.sh 的 --owner 用（默认的 member-001 会被判 owner_mismatch）。
    _write(f"{args.out}.member", str(member.get("id") or ""))
    print(f"配对串已写入 {args.out}（{args.expires_in // 60} 分钟有效，用一次即失效）")
    print(f"登录身份：{member.get('email')} · member_id={member.get('id')}（已写到 {args.out}.member）")
    print(f"项目 id（授权时用）：{project['id']}")


def command_grant(args: argparse.Namespace) -> None:
    password = _read_secret(args.password, args.password_file)
    token, _ = _login(args.url, args.email, password)
    project = _find_project(args.url, token, args.project)
    if project is None:
        raise SystemExit(f"项目「{args.project}」不存在：先跑 pairing 子命令（或加 --create-project）")
    payload: dict[str, Any] = {"device_id": args.device_id, "expires_in_seconds": args.expires_in}
    if args.capability:
        # 少给一项能力，Agent 就会在那一步被 403 挡住；不给就用平台默认清单
        payload["capabilities"] = list(args.capability)
    credential = _call(args.url, "POST", f"/api/projects/{project['id']}/device-grants", token=token, payload=payload)
    grant = credential["grant"]
    blob = _blob({
        "project_id": grant["project_id"],
        "project_token": credential["project_token"],
        "capabilities": grant["capabilities"],
        "agent_id": grant["agent_id"],
        "device_id": grant["device_id"],
        "expires_at": grant.get("expires_at"),
    })
    _write(args.out, blob)
    print(f"授权串已写入 {args.out}")
    print(f"  项目：{grant['project_id']}")
    print(f"  设备：{grant['device_id']} · Agent：{grant['agent_id']}（agent 由平台按设备归属推导）")
    print(f"  能力：{'、'.join(grant['capabilities'])}")
    print("提醒：串里带一次性 project_token，配完删掉这个文件。")


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    try:
        import os

        os.chmod(path, 0o600)
    except OSError:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="CLOUD-1 平台侧准备（配对串 / 授权串）")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(target: argparse.ArgumentParser) -> None:
        target.add_argument("--url", required=True, help="平台地址，例如 https://synapforge.top")
        target.add_argument("--email", required=True)
        target.add_argument("--password", default="", help="与 --password-file 二选一（推荐用文件：口令不进命令行）")
        target.add_argument("--password-file", default=None)
        target.add_argument("--project", default="云端智能体")
        target.add_argument("--out", required=True, help="输出文件（0600）")

    pairing_parser = sub.add_parser("pairing", help="生成配对串（15 分钟有效）")
    common(pairing_parser)
    pairing_parser.add_argument("--create-project", action="store_true", help="项目不存在时创建它")
    pairing_parser.add_argument("--expires-in", type=int, default=900)
    pairing_parser.set_defaults(func=command_pairing)

    grant_parser = sub.add_parser("grant", help="生成项目授权串（设备注册之后）")
    common(grant_parser)
    grant_parser.add_argument("--device-id", required=True)
    grant_parser.add_argument("--capability", action="append", help="覆盖平台默认能力清单（可重复）")
    grant_parser.add_argument("--expires-in", type=int, default=86400)
    grant_parser.set_defaults(func=command_grant)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()