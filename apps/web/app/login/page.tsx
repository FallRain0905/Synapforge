"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Loader2, Network } from "lucide-react";
import { errorMessage } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { useTheme } from "../../lib/theme";

/** 从 ?next= 取登录后要回的页面（只接受站内路径，避免被当成跳转跳板）。 */
function safeNext(value: string | null): string {
  if (!value || !value.startsWith("/") || value.startsWith("//")) return "/";
  return value;
}

export default function LoginPage() {
  const router = useRouter();
  const { login, authenticated, ready } = useAuth();
  const { theme, toggle } = useTheme();
  const [form, setForm] = useState({ email: "", password: "" });
  const [failure, setFailure] = useState("");
  const [busy, setBusy] = useState(false);
  // 不用 useSearchParams：它会让预渲染走 CSR bailout（本页要保持纯静态）。挂载后读一次即可。
  const [next, setNext] = useState("/");
  useEffect(() => {
    setNext(safeNext(new URLSearchParams(window.location.search).get("next")));
  }, []);

  // 已登录（或被 401 回退到本页）时直接进工作台
  useEffect(() => {
    if (ready && authenticated) router.replace(next);
  }, [ready, authenticated, next, router]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setFailure("");
    if (!form.email.trim() || !form.password) {
      setFailure("请填写邮箱与密码");
      return;
    }
    setBusy(true);
    try {
      await login(form.email.trim(), form.password);
      router.replace(next);
    } catch (error) {
      setFailure(errorMessage(error, "登录失败"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-page">
      <div className="auth-card">
        <div className="auth-brand">
          <span className="brand-mark"><Network size={18} /></span>
          <div>
            <strong>synapforge</strong>
            <small>Agent 协作平台 · 群聊式人机协作、任务派单与成果交付</small>
          </div>
          <button className="app-icon" aria-label="切换主题" data-testid="theme-toggle" onClick={toggle} title={theme === "dark" ? "切换到白天" : "切换到黑夜"}>
            {theme === "dark" ? "☀" : "☾"}
          </button>
        </div>

        <h1>登录</h1>
        <p className="auth-hint">用管理员发给你的邮箱与密码登录；忘记密码请找管理员重置。</p>

        <form onSubmit={submit} className="auth-form" data-testid="login-form">
          <label className="field">
            <span>邮箱</span>
            <input
              type="email"
              autoComplete="username"
              value={form.email}
              onChange={(event) => setForm({ ...form, email: event.target.value })}
              placeholder="you@example.com"
              data-testid="login-email"
            />
          </label>
          <label className="field">
            <span>密码</span>
            <input
              type="password"
              autoComplete="current-password"
              value={form.password}
              onChange={(event) => setForm({ ...form, password: event.target.value })}
              placeholder="至少 10 位"
              data-testid="login-password"
            />
          </label>
          {failure && <div className="pack-missing" data-testid="login-error">{failure}</div>}
          <button className="button button-primary auth-submit" type="submit" disabled={busy} data-testid="login-submit">
            {busy && <Loader2 size={15} className="spin" />} 登录
          </button>
        </form>

        <div className="auth-footer">
          第一次使用？<Link href="/register">注册即用</Link> · <a href="/downloads/synapforge-setup-0.2.1-x64.exe" data-testid="desktop-download" title="Windows 安装包（约 150MB，未签名）">下载桌面端（Windows）</a>
        </div>
      </div>
    </div>
  );
}