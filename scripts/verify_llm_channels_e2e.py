"""端到端验收：起真 API 服务 + 本机假上游，走一遍管理员建渠道→检测→测速→成员真实对话→额度扣减/耗尽。

与单测的区别：这里不是 TestClient 进程内调用，而是**真 uvicorn 进程 + 真 HTTP**，
验证的是部署形态下的行为（路由、鉴权头、SSE、流式记账）。

凭据一律用假值；假上游是本机 http.server，不出网。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # scripts/ 的上一级 = 仓库根
API_DIR = ROOT / "apps" / "api"
assert (API_DIR / "app" / "main.py").exists(), f"仓库根推断错了：{ROOT}"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeUpstream:
    """本机假 OpenAI 上游：总是回固定格式，带 usage（便于验证扣减）。"""

    def __init__(self, *, usage: dict | None = None, fail: bool = False) -> None:
        self.requests: list[dict] = []
        self.usage = usage if usage is not None else {"prompt_tokens": 120, "completion_tokens": 45, "total_tokens": 165}
        self.fail = fail
        self.port = free_port()
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8") if length else ""
                body = json.loads(raw) if raw else {}
                upstream.requests.append({"path": self.path, "auth": self.headers.get("Authorization", ""), "body": body})
                if upstream.fail:
                    payload = {"error": {"message": "upstream exploded"}}
                    encoded = json.dumps(payload).encode()
                    self.send_response(500)
                elif body.get("stream"):
                    frames = [
                        'data: {"choices":[{"delta":{"content":"你好"}}]}\n\n',
                        'data: {"choices":[{"delta":{"content":"，世界"}}]}\n\n',
                        'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":80,"completion_tokens":20}}\n\n',
                        "data: [DONE]\n\n",
                    ]
                    encoded = "".join(frames).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                else:
                    payload = {
                        "id": "chatcmpl-fake",
                        "object": "chat.completion",
                        "model": body.get("model", "fake"),
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}],
                        "usage": upstream.usage,
                    }
                    encoded = json.dumps(payload).encode()
                    self.send_response(200)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *args: object) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def call(url: str, *, method: str = "GET", token: str | None = None, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


def main() -> int:
    failures: list[str] = []

    def check(label: str, condition: bool, extra: str = "") -> None:
        print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' — ' + extra) if extra else ''}")
        if not condition:
            failures.append(label)

    upstream = FakeUpstream()
    # 用**临时库**跑：首个注册账号才会成为管理员（"首个账号免码当管理员"依赖空库）。
    # 启动器 scripts/_llm_e2e_launcher.py 把 store 指向临时目录，
    # 开发库（apps/api/data/platform.db）自始至终不被读写。
    workdir = Path(tempfile.mkdtemp(prefix="llm-e2e-"))
    port = free_port()
    env = {
        **os.environ,
        # 用 os.pathsep（Windows ";"，POSIX ":"），不要写死分号
        "PYTHONPATH": os.pathsep.join([str(API_DIR), str(ROOT)]),
        "PLATFORM_AUTH_MODE": "required",
        "PLATFORM_LLM_FREE_TOKENS_PER_MEMBER": "300",
        "LLM_E2E_DATA_DIR": str(workdir),
        "LLM_E2E_PORT": str(port),
    }
    print(f"启动 API（端口 {port}，临时库 {workdir}）")
    process = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(ROOT / "scripts" / "_llm_e2e_launcher.py")],
        cwd=str(API_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        # 等服务起来
        ready = False
        for _ in range(60):
            try:
                status, _ = call(f"{base}/api/auth/me")
                if status in {200, 401}:
                    ready = True
                    break
            except Exception:
                time.sleep(0.5)
        if not ready:
            output = process.stdout.read().decode("utf-8", "replace") if process.stdout else ""
            print("服务没起来，输出如下：\n", output[-3000:])
            return 1
        print("服务已就绪。\n")

        # ① 注册第一个账号 → 自动成为管理员
        status, raw = call(f"{base}/api/auth/register", method="POST", body={
            "email": "admin@e2e.test", "password": "admin-pass-12345", "display_name": "验收管理员",
        })
        check("注册首个账号（成为管理员）", status == 201, f"HTTP {status} {raw[:160]}")
        if status != 201:
            return 1
        admin = json.loads(raw)
        admin_token = admin["token"]
        check("首个账号是管理员", bool(admin["account"]["is_admin"]), str(admin["account"].get("is_admin")))

        # ② 第二个账号 = 普通成员
        status, raw = call(f"{base}/api/auth/register", method="POST", body={
            "email": "member@e2e.test", "password": "member-pass-12345", "display_name": "验收成员",
        })
        check("注册普通成员", status == 201, f"HTTP {status} {raw[:160]}")
        member = json.loads(raw)
        member_token = member["token"]
        check("普通成员不是管理员", not member["account"]["is_admin"])

        # ③ 非管理员访问渠道接口 → 403
        status, raw = call(f"{base}/api/admin/llm-channels", token=member_token)
        check("非管理员读渠道 → 403", status == 403, f"HTTP {status}")

        # ④ 管理员建渠道（假 key）
        status, raw = call(f"{base}/api/admin/llm-channels", method="POST", token=admin_token, body={
            "name": "验收渠道", "base_url": upstream.base_url,
            "api_key": "sk-fake-e2e-9999", "models": ["e2e-model"], "priority": 10, "enabled": True,
        })
        check("管理员建渠道 → 201", status == 201, f"HTTP {status} {raw[:200]}")
        if status != 201:
            return 1
        channel = json.loads(raw)
        check("响应不回传 api_key 明文", "api_key" not in raw and "sk-fake-e2e-9999" not in raw)
        check("只回 key_hint 末 4 位", channel["key_hint"] == "9999", channel["key_hint"])

        # ⑤ 渠道检测（真打假上游）
        status, raw = call(f"{base}/api/admin/llm-channels/{channel['id']}/check", method="POST", token=admin_token, body={})
        check("渠道检测成功", status == 200 and json.loads(raw)["ok"] is True, f"HTTP {status} {raw[:200]}")
        detail = call(f"{base}/api/admin/llm-channels/{channel['id']}", token=admin_token)
        stored = json.loads(detail[1])
        check("检测结果已回填 last_check_ok", stored["last_check_ok"] is True)
        check("检测结果已回填延迟", isinstance(stored["last_latency_ms"], int))

        # ⑥ 渠道测速（3 轮）
        before = len(upstream.requests)
        status, raw = call(f"{base}/api/admin/llm-channels/{channel['id']}/speed-test", method="POST", token=admin_token, body={"rounds": 3})
        speed = json.loads(raw)
        check("测速 3 轮全通过", status == 200 and speed["ok_rounds"] == 3, f"HTTP {status} {raw[:200]}")
        check("测速确实打了 3 次上游", len(upstream.requests) - before == 3)
        check("测速给了均值", isinstance(speed["avg_ms"], int))

        # ⑦ 成员看可用模型与自己的额度
        status, raw = call(f"{base}/api/llm/v1/models", token=member_token)
        check("成员可读可用模型", status == 200 and json.loads(raw)["models"] == ["e2e-model"], raw[:160])
        status, raw = call(f"{base}/api/llm/quota", token=member_token)
        quota = json.loads(raw)
        check("默认额度取环境变量（300）", quota["token_limit"] == 300, str(quota["token_limit"]))

        # ⑧ 成员真实对话（非流式）→ 按上游 usage 扣减
        status, raw = call(f"{base}/api/llm/v1/chat/completions", method="POST", token=member_token, body={
            "model": "e2e-model", "messages": [{"role": "user", "content": "你好"}],
        })
        ok = status == 200 and json.loads(raw)["choices"][0]["message"]["content"] == "pong"
        check("成员非流式对话成功", ok, f"HTTP {status} {raw[:200]}")
        check("上游收到了 Bearer（密钥被用上）", upstream.requests[-1]["auth"] == "Bearer sk-fake-e2e-9999")
        check("响应体里没有明文密钥", "sk-fake-e2e-9999" not in raw)
        quota = json.loads(call(f"{base}/api/llm/quota", token=member_token)[1])
        check("额度按 usage 扣减 165", quota["tokens_used"] == 165, str(quota["tokens_used"]))

        # ⑨ 流式对话 → 按最后一个 usage chunk 扣减（80+20=100）
        status, raw = call(f"{base}/api/llm/v1/chat/completions", method="POST", token=member_token, body={
            "model": "e2e-model", "messages": [{"role": "user", "content": "给我讲讲"}], "stream": True,
        })
        check("成员流式对话成功", status == 200 and "你好" in raw, f"HTTP {status} {raw[:200]}")
        check("SSE 有 [DONE]", "[DONE]" in raw)
        quota = json.loads(call(f"{base}/api/llm/quota", token=member_token)[1])
        check("流式按最后 usage 扣减（165+100=265）", quota["tokens_used"] == 265, str(quota["tokens_used"]))

        # ⑩ 模型未配置 → 明确报错，不猜不回落
        status, raw = call(f"{base}/api/llm/v1/chat/completions", method="POST", token=member_token, body={
            "model": "不存在的模型", "messages": [{"role": "user", "content": "hi"}],
        })
        check("未配置模型 → 404 且错误码明确", status == 404 and "llm_channel_no_route" in raw, f"HTTP {status} {raw[:160]}")

        # ⑪ 管理员把额度压到已用量 → 下一次调用 429
        status, raw = call(f"{base}/api/admin/llm-quotas/{member['account']['member']['id']}", method="PUT", token=admin_token, body={"token_limit": 265})
        check("管理员设置成员额度", status == 200, f"HTTP {status} {raw[:160]}")
        before = len(upstream.requests)
        status, raw = call(f"{base}/api/llm/v1/chat/completions", method="POST", token=member_token, body={
            "model": "e2e-model", "messages": [{"role": "user", "content": "hi"}],
        })
        check("额度耗尽 → 429 且原因明确", status == 429 and "llm_quota_exceeded" in raw, f"HTTP {status} {raw[:160]}")
        check("耗尽时不再打上游（不白花）", len(upstream.requests) == before)

        # ⑫ 负数 = 不限量
        status, raw = call(f"{base}/api/admin/llm-quotas/{member['account']['member']['id']}", method="PUT", token=admin_token, body={"token_limit": -1})
        check("额度设为 -1 = 不限量", status == 200 and json.loads(raw)["unlimited"] is True, raw[:160])
        status, raw = call(f"{base}/api/llm/v1/chat/completions", method="POST", token=member_token, body={
            "model": "e2e-model", "messages": [{"role": "user", "content": "hi"}],
        })
        check("不限量后恢复可调用", status == 200, f"HTTP {status} {raw[:160]}")

        # ⑬ 用量总览：只记**真的打到上游**的调用。
        #    非流式(165) + 流式(100) + 恢复不限量后(165) = 3 次 / 430 token；
        #    额度耗尽那次在打上游之前就被拒，因此不产生流水（这是设计，不是漏记）。
        status, raw = call(f"{base}/api/admin/llm-usage", token=admin_token)
        usage = json.loads(raw)
        check("用量总览=3 次成功调用", status == 200 and usage["totals"]["requests"] == 3, raw[:200])
        check("用量总览 token 合计 430", usage["totals"]["total_tokens"] == 430, str(usage["totals"]["total_tokens"]))
        check("总览按成员聚合出该成员", any(item["member_id"] == member["account"]["member"]["id"] for item in usage["members"]))

        # ⑭ 上游故障 → 如实记账并标 error
        upstream.fail = True
        status, raw = call(f"{base}/api/llm/v1/chat/completions", method="POST", token=member_token, body={
            "model": "e2e-model", "messages": [{"role": "user", "content": "hi"}],
        })
        check("上游 500 → 502 且错误码明确", status == 502 and "llm_upstream_error" in raw, f"HTTP {status} {raw[:160]}")
        upstream.fail = False

        # ⑮ 额度编辑后仍不回传密钥
        status, raw = call(f"{base}/api/admin/llm-channels", token=admin_token)
        check("渠道列表整体不含明文密钥", "sk-fake-e2e-9999" not in raw)

    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        upstream.close()
        # 临时库直接删掉；开发库全程没被碰过
        shutil.rmtree(workdir, ignore_errors=True)

    print("\n" + "=" * 60)
    if failures:
        print(f"验收失败 {len(failures)} 项：")
        for item in failures:
            print("  -", item)
        return 1
    print("端到端验收全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
