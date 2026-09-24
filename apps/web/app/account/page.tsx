"use client";

import { useCallback, useEffect, useState } from "react";
import { Box, ClipboardCopy, KeyRound, Loader2, ShieldCheck, UserPlus, Users } from "lucide-react";
import { PageHeading, formatTime } from "../../components/shell";
import { ConfirmDialog, EmptyState, LoadingSkeleton, Metric, Panel, StatusPill } from "../../components/ui";
import {
  AccountView,
  InvitationRecord,
  apiError,
  createInvitation,
  errorMessage,
  changeAccountPassword,
  listOrganizations,
  listAccounts,
  listInvitations,
  resetAccountPassword,
  updateAccount,
} from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { useWorkspace } from "../../lib/workspace";

/**
 * 账号设置。
 *
 * 普通成员：改密码 + 查看自己的账号信息。
 * 管理员：多出「账号管理」（停用/启用、授予管理员、重置密码）和「邀请码」（生成、复制邀请链接）。
 */
export default function AccountPage() {
  const { account, applyAccount, ready, authenticated } = useAuth();
  const { notify } = useWorkspace();
  const [accounts, setAccounts] = useState<AccountView[]>([]);
  const [invitations, setInvitations] = useState<InvitationRecord[]>([]);
  const [organizationId, setOrganizationId] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [inviteForm, setInviteForm] = useState({ email: "", role: "contributor" });
  const [inviteResult, setInviteResult] = useState<InvitationRecord | null>(null);
  const [resetResult, setResetResult] = useState<{ email: string; password: string } | null>(null);
  const [passwordForm, setPasswordForm] = useState({ current_password: "", new_password: "", confirm: "" });
  const [passwordFailure, setPasswordFailure] = useState("");
  const [confirmAction, setConfirmAction] = useState<{ account: AccountView; next: "active" | "suspended" | null; admin: boolean | null } | null>(null);

  const isAdmin = Boolean(account?.is_admin);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      if (isAdmin) {
        const [list, invites, organizations] = await Promise.all([listAccounts(), listInvitations(), listOrganizations()]);
        setAccounts(list);
        setInvitations(invites);
        setOrganizationId(organizations[0]?.id ?? "");
      }
    } catch (error) {
      notify(errorMessage(error, "读取账号数据失败"));
    } finally {
      setLoading(false);
    }
  }, [isAdmin, notify]);

  useEffect(() => {
    // 同上：账号数据也要等会话令牌就绪（否则硬刷新时整页数据为空）
    if (!ready || !authenticated) return;
    void load();
  }, [ready, authenticated, load]);

  const submitPassword = async (event: React.FormEvent) => {
    event.preventDefault();
    setPasswordFailure("");
    if (passwordForm.new_password.length < 10) {
      setPasswordFailure("新密码至少 10 位");
      return;
    }
    if (passwordForm.new_password !== passwordForm.confirm) {
      setPasswordFailure("两次输入的新密码不一致");
      return;
    }
    setBusy("password");
    try {
      const updated = await changeAccountPassword({
        current_password: passwordForm.current_password,
        new_password: passwordForm.new_password,
      });
      applyAccount(updated);
      setPasswordForm({ current_password: "", new_password: "", confirm: "" });
      notify("密码已更新（其它设备的登录已失效）");
    } catch (error) {
      setPasswordFailure(errorMessage(error, "修改密码失败"));
    } finally {
      setBusy("");
    }
  };

  const applyAccountChange = async () => {
    if (!confirmAction) return;
    const { account: target, next, admin } = confirmAction;
    setBusy(target.member.id);
    try {
      const updated = await updateAccount(target.member.id, {
        ...(next ? { status: next } : {}),
        ...(admin === null ? {} : { is_admin: admin }),
      });
      setAccounts((current) => current.map((item) => (item.member.id === updated.member.id ? updated : item)));
      notify(next === "suspended" ? "账号已停用" : next === "active" ? "账号已启用" : updated.is_admin ? "已设为管理员" : "已取消管理员");
      setConfirmAction(null);
    } catch (error) {
      notify(errorMessage(error, "更新账号失败"));
    } finally {
      setBusy("");
    }
  };

  const doReset = async (target: AccountView) => {
    setBusy(target.member.id);
    try {
      const result = await resetAccountPassword(target.member.id);
      setResetResult({ email: target.member.email, password: result.temporary_password });
      setAccounts((current) => current.map((item) => (item.member.id === result.account.member.id ? result.account : item)));
      notify("已生成一次性临时密码");
    } catch (error) {
      notify(errorMessage(error, "重置密码失败"));
    } finally {
      setBusy("");
    }
  };

  const createInvite = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!organizationId) {
      notify("缺少组织信息，无法生成邀请码");
      return;
    }
    setBusy("invite");
    try {
      const created = await createInvitation({
        organization_id: organizationId,
        email: inviteForm.email.trim(),
        role: inviteForm.role,
      });
      setInvitations((current) => [created, ...current]);
      setInviteResult(created);
      setInviteForm({ email: "", role: inviteForm.role });
      notify("邀请码已生成");
    } catch (error) {
      notify(errorMessage(error, "生成邀请码失败"));
    } finally {
      setBusy("");
    }
  };

  const inviteLink = (invitation: InvitationRecord) =>
    typeof window === "undefined" ? "" : `${window.location.origin}/register?code=${encodeURIComponent(invitation.token)}`;

  const copy = async (text: string, label: string) => {
    try {
      await navigator.clipboard.writeText(text);
      notify(`${label}已复制`);
    } catch {
      notify("复制失败：浏览器拒绝了剪贴板访问，请手动选中复制");
    }
  };

  const activeAccounts = accounts.filter((item) => item.member.status === "active");
  const pendingInvites = invitations.filter((item) => item.status === "PENDING");

  return (
    <div className="page-content" id="account">
      <PageHeading hint={isAdmin ? "你的账号信息，以及全站账号与邀请码管理" : "你的账号信息与密码"} />

      <section className="metrics-grid">
        <Metric label="当前账号" value={account?.member.display_name ?? "—"} detail={account?.member.email ?? ""} />
        <Metric label="角色" value={isAdmin ? "管理员" : "成员"} detail={isAdmin ? "可管理账号与邀请码" : "可管理项目内容"} />
        <Metric label="上次登录" value={account?.last_login_at ? formatTime(account.last_login_at) : "首次登录"} detail="本机登录时间" />
        {isAdmin && <Metric label="在职账号" value={activeAccounts.length} detail={`共 ${accounts.length} 个账号`} />}
      </section>

      <section className="grid grid-main-side">
        <Panel title="修改密码" subtitle="改完会撤销其它设备上的登录，本机保持在线" testId="account-password">
          <form className="auth-form" onSubmit={submitPassword}>
            <label className="field">
              <span>当前密码</span>
              <input
                type="password"
                autoComplete="current-password"
                value={passwordForm.current_password}
                onChange={(event) => setPasswordForm({ ...passwordForm, current_password: event.target.value })}
                data-testid="password-current"
              />
            </label>
            <label className="field">
              <span>新密码</span>
              <input
                type="password"
                autoComplete="new-password"
                value={passwordForm.new_password}
                onChange={(event) => setPasswordForm({ ...passwordForm, new_password: event.target.value })}
                placeholder="至少 10 位，不要纯数字或纯字母"
                data-testid="password-new"
              />
            </label>
            <label className="field">
              <span>确认新密码</span>
              <input
                type="password"
                autoComplete="new-password"
                value={passwordForm.confirm}
                onChange={(event) => setPasswordForm({ ...passwordForm, confirm: event.target.value })}
                data-testid="password-confirm"
              />
            </label>
            {passwordFailure && <div className="pack-missing" data-testid="password-error">{passwordFailure}</div>}
            <div className="form-row">
              <button className="button button-primary" type="submit" disabled={busy === "password"} data-testid="password-submit">
                {busy === "password" && <Loader2 size={15} className="spin" />} <KeyRound size={15} /> 更新密码
              </button>
            </div>
          </form>
        </Panel>

        <Panel title="我的账号" subtitle="由系统记录，显示名可让队友认出你" testId="account-self">
          <div className="list">
            <div className="list-item list-item-static">
              <span className="icon-tile"><Users size={15} /></span>
              <div className="item-copy">
                <strong>{account?.member.display_name ?? "—"}</strong>
                <small>{account?.member.email ?? ""}</small>
              </div>
              <StatusPill status={account?.member.status === "active" ? "APPROVED" : "BLOCKED"} label={account?.member.status ?? "—"} />
            </div>
            <div className="list-item list-item-static">
              <span className="icon-tile"><ShieldCheck size={15} /></span>
              <div className="item-copy">
                <strong>{isAdmin ? "管理员" : "普通成员"}</strong>
                <small>
                  {isAdmin
                    ? "可以管理账号、发放邀请码、重置他人密码"
                    : "可以创建项目、派发任务、审核内容；账号管理请联系管理员"}
                </small>
              </div>
            </div>
            <div className="list-item list-item-static">
              <span className="icon-tile"><Box size={15} /></span>
              <div className="item-copy">
                <strong>账号创建于 {account?.member.created_at ? formatTime(account.member.created_at) : "—"}</strong>
                <small>会话有效期 30 天，登出或改密会立即失效</small>
              </div>
            </div>
          </div>
        </Panel>
      </section>

      {isAdmin && (
        <>
          <Panel
            title="账号管理"
            subtitle="停用会立即断开该账号的登录；重置密码会返回一次性临时密码"
            testId="account-admin"
            actions={<span className="gate-count">{activeAccounts.length}</span>}
          >
            {loading ? (
              <LoadingSkeleton rows={3} label="正在加载账号" />
            ) : !accounts.length ? (
              <EmptyState><Users size={16} /> 还没有账号</EmptyState>
            ) : (
              <div className="table">
                <div className="table-head"><span>账号</span><span>角色</span><span>状态</span><span>上次登录</span><span /></div>
                {accounts.map((item) => (
                  <div className="table-row" key={item.member.id} data-testid={`account-row-${item.member.id}`}>
                    <div className="table-title">
                      <strong>{item.member.display_name}</strong>
                      <small>{item.member.email} · 创建于 {formatTime(item.member.created_at)}</small>
                    </div>
                    <span className="chip" data-label="角色">{item.is_admin ? "管理员" : "成员"}</span>
                    <span data-label="状态">
                      <StatusPill
                        status={item.member.status === "active" ? "APPROVED" : item.member.status === "suspended" ? "REVOKED" : "DRAFT"}
                        label={item.member.status === "active" ? "在职" : item.member.status === "suspended" ? "已停用" : item.member.status}
                      />
                    </span>
                    <span className="hint" data-label="上次登录">{item.last_login_at ? formatTime(item.last_login_at) : "从未"}</span>
                    <div className="row-actions">
                      <button
                        className="text-button"
                        disabled={busy === item.member.id || item.member.id === account?.member.id}
                        onClick={() => setConfirmAction({ account: item, next: item.member.status === "active" ? "suspended" : "active", admin: null })}
                        data-testid={`account-toggle-${item.member.id}`}
                      >
                        {item.member.status === "active" ? "停用" : "启用"}
                      </button>
                      <button
                        className="text-button"
                        disabled={busy === item.member.id || item.member.id === account?.member.id}
                        onClick={() => setConfirmAction({ account: item, next: null, admin: !item.is_admin })}
                        data-testid={`account-admin-${item.member.id}`}
                      >
                        {item.is_admin ? "取消管理员" : "设为管理员"}
                      </button>
                      <button
                        className="text-button"
                        disabled={busy === item.member.id || item.member.id === account?.member.id}
                        onClick={() => void doReset(item)}
                        data-testid={`account-reset-${item.member.id}`}
                      >
                        <KeyRound size={13} /> 重置密码
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </Panel>

          <Panel title="邀请码" subtitle="把邀请链接发给队友，对方注册后即可登录；每个码只能用一次" testId="account-invites">
            <form className="form-row" onSubmit={createInvite}>
              <input
                type="email"
                value={inviteForm.email}
                onChange={(event) => setInviteForm({ ...inviteForm, email: event.target.value })}
                placeholder="队友邮箱（必须与注册邮箱一致）"
                data-testid="invite-email"
              />
              <select value={inviteForm.role} onChange={(event) => setInviteForm({ ...inviteForm, role: event.target.value })} data-testid="invite-role">
                <option value="contributor">成员</option>
                <option value="reviewer">复核人</option>
                <option value="project_lead">项目负责人</option>
                <option value="observer">只读观察者</option>
              </select>
              <button className="button button-primary" type="submit" disabled={busy === "invite"} data-testid="invite-create">
                {busy === "invite" && <Loader2 size={15} className="spin" />} <UserPlus size={15} /> 生成邀请码
              </button>
            </form>

            {inviteResult && (
              <div className="invite-result" data-testid="invite-result">
                <div className="invite-result-copy">
                  <strong>邀请链接（发给 {inviteResult.email}）</strong>
                  <code>{inviteLink(inviteResult)}</code>
                  <small>有效期至 {formatTime(inviteResult.expires_at)}</small>
                </div>
                <button className="button button-secondary" type="button" onClick={() => void copy(inviteLink(inviteResult), "邀请链接")}>
                  <ClipboardCopy size={15} /> 复制链接
                </button>
              </div>
            )}

            {resetResult && (
              <div className="invite-result" data-testid="reset-result">
                <div className="invite-result-copy">
                  <strong>一次性临时密码（{resetResult.email}）</strong>
                  <code>{resetResult.password}</code>
                  <small>请通过可靠渠道转达；对方登录后应在「修改密码」里改掉</small>
                </div>
                <button className="button button-secondary" type="button" onClick={() => void copy(resetResult.password, "临时密码")}>
                  <ClipboardCopy size={15} /> 复制密码
                </button>
              </div>
            )}

            {invitations.length > 0 && (
              <div className="list">
                {invitations.slice(0, 8).map((item) => (
                  <div className="list-item list-item-static" key={item.id}>
                    <span className="icon-tile icon-tile-blue"><UserPlus size={15} /></span>
                    <div className="item-copy">
                      <strong>{item.email}</strong>
                      <small>
                        {item.role} · 有效期至 {formatTime(item.expires_at)}
                      </small>
                    </div>
                    <span className="badge status-neutral">{item.status === "PENDING" ? `待使用 ${pendingInvites.length}` : item.status}</span>
                  </div>
                ))}
              </div>
            )}
          </Panel>
        </>
      )}

      {confirmAction && (
        <ConfirmDialog
          title={confirmAction.next ? (confirmAction.next === "suspended" ? "停用账号" : "启用账号") : confirmAction.admin ? "设为管理员" : "取消管理员"}
          description={
            confirmAction.next === "suspended"
              ? `停用后 ${confirmAction.account.member.email} 会立即被登出，且无法再登录。`
              : confirmAction.admin
                ? `${confirmAction.account.member.email} 将获得账号管理与邀请码权限。`
                : `${confirmAction.account.member.email} 将失去账号管理与邀请码权限。`
          }
          confirmLabel="确认"
          onCancel={() => setConfirmAction(null)}
          onConfirm={() => void applyAccountChange()}
        />
      )}
    </div>
  );
}