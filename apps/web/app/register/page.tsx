"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Loader2, Network } from "lucide-react";
import { errorMessage } from "../../lib/api";
import { useAuth } from "../../lib/auth";

/**
 * 开放注册（默认）：邮箱 + 密码即注册即用（contributor）；带邀请码可按码上的角色加入。
 *
 * 首个账号（系统里还没有任何真实账号时）不需要邀请码，并自动成为管理员——
 * 这条路径只对部署后的第一位使用者开放，之后必须由管理员发码。
 */
export default function RegisterPage() {
  const router = useRouter();
  const { register, authenticated, ready } = useAuth();
  const [form, setForm] = useState({ email: "", display_name: "", password: "", confirm: "", invite_code: "" });
  const [failure, setFailure] = useState("");
  const [busy, setBusy] = useState(false);

  // 邀请链接形如 /register?code=xxxx：挂载后读一次并自动填好（不用 useSearchParams，保持静态预渲染）
  useEffect(() => {
    const code = new URLSearchParams(window.location.search).get("code");
    if (code) setForm((current) => ({ ...current, invite_code: code }));
  }, []);

  useEffect(() => {
    if (ready && authenticated) router.replace("/");
  }, [ready, authenticated, router]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setFailure("");
    if (!form.email.trim()) {
      setFailure("请填写邮箱");
      return;
    }
    if (form.display_name.trim().length < 2) {
      setFailure("显示名至少 2 个字符");
      return;
    }
    if (form.password.length < 10) {
      setFailure("密码至少 10 位");
      return;
    }
    if (form.password !== form.confirm) {
      setFailure("两次输入的密码不一致");
      return;
    }
    setBusy(true);
    try {
      await register({
        email: form.email.trim(),
        password: form.password,
        display_name: form.display_name.trim(),
        invite_code: form.invite_code.trim() || undefined,
      });
      router.replace("/");
    } catch (error) {
      setFailure(errorMessage(error, "注册失败"));
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
            <small>Agent 协作平台 · 注册即用</small>
          </div>
        </div>

        <h1>注册</h1>
        <p className="auth-hint">
          开放注册中：填邮箱与密码即可加入，注册即用（暂不验证邮箱）。
          有邀请码可以填上——能带上管理员给你指定的角色。
        </p>

        <form onSubmit={submit} className="auth-form" data-testid="register-form">
          <label className="field">
            <span>邀请码（可选，带指定角色）</span>
            <input
              value={form.invite_code}
              onChange={(event) => setForm({ ...form, invite_code: event.target.value })}
              placeholder="留空 = 开放注册（普通成员）"
              data-testid="register-code"
            />
          </label>
          <label className="field">
            <span>邮箱</span>
            <input
              type="email"
              autoComplete="username"
              value={form.email}
              onChange={(event) => setForm({ ...form, email: event.target.value })}
              placeholder="you@example.com"
              data-testid="register-email"
            />
          </label>
          <label className="field">
            <span>显示名</span>
            <input
              value={form.display_name}
              onChange={(event) => setForm({ ...form, display_name: event.target.value })}
              placeholder="队友看到的名字"
              data-testid="register-name"
            />
          </label>
          <label className="field">
            <span>密码</span>
            <input
              type="password"
              autoComplete="new-password"
              value={form.password}
              onChange={(event) => setForm({ ...form, password: event.target.value })}
              placeholder="至少 10 位，不要纯数字或纯字母"
              data-testid="register-password"
            />
          </label>
          <label className="field">
            <span>确认密码</span>
            <input
              type="password"
              autoComplete="new-password"
              value={form.confirm}
              onChange={(event) => setForm({ ...form, confirm: event.target.value })}
              data-testid="register-confirm"
            />
          </label>
          {failure && <div className="pack-missing" data-testid="register-error">{failure}</div>}
          <button className="button button-primary auth-submit" type="submit" disabled={busy} data-testid="register-submit">
            {busy && <Loader2 size={15} className="spin" />} 注册并登录
          </button>
        </form>

        <div className="auth-footer">
          已有账号？<Link href="/login">去登录</Link> · <a href="/downloads/synapforge-setup-0.2.1-x64.exe" data-testid="desktop-download" title="Windows 安装包（约 150MB，未签名）">下载桌面端（Windows）</a>
        </div>
      </div>
    </div>
  );
}