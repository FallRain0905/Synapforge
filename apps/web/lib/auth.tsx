"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import {
  AccountView,
  AuthSession,
  getCurrentAccount,
  loginAccount,
  logoutAccount,
  onUnauthorized,
  registerAccount,
  restoreSessionToken,
  setSessionToken,
} from "./api";

/**
 * 登录态 Provider。
 *
 * 与 `lib/theme.tsx` 同样的取舍：首帧不读 localStorage（否则静态 HTML 与客户端渲染不一致），
 * 挂载后再恢复会话并向 `/api/auth/me` 校验一次——令牌可能已被登出/改密/停用而失效。
 */

type AuthContextValue = {
  /** 是否已完成"从本地恢复 + 向服务端校验"这一步；未完成时页面不该判断"未登录" */
  ready: boolean;
  account: AccountView | null;
  authenticated: boolean;
  login: (email: string, password: string) => Promise<AccountView>;
  register: (payload: { email: string; password: string; display_name: string; invite_code?: string }) => Promise<AccountView>;
  logout: () => Promise<void>;
  refresh: () => Promise<AccountView | null>;
  /** 账号页改完资料后同步顶部显示 */
  applyAccount: (account: AccountView) => void;
};

const AuthContext = createContext<AuthContextValue | null>(null);

/**
 * 演示自动登录（构建时开关）：
 *   NEXT_PUBLIC_DEMO_AUTO_LOGIN=1 + NEXT_PUBLIC_DEMO_EMAIL / NEXT_PUBLIC_DEMO_PASSWORD
 * 只影响"本浏览器还没有会话"的首屏；已有会话仍走正常恢复，失效会话仍会被清理。
 * 不设开关时行为与原来一致。
 */
const DEMO_AUTO_LOGIN = process.env.NEXT_PUBLIC_DEMO_AUTO_LOGIN === "1";
const DEMO_EMAIL = process.env.NEXT_PUBLIC_DEMO_EMAIL ?? "";
const DEMO_PASSWORD = process.env.NEXT_PUBLIC_DEMO_PASSWORD ?? "";

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [ready, setReady] = useState(false);
  const [account, setAccount] = useState<AccountView | null>(null);

  const refresh = useCallback(async () => {
    if (!restoreSessionToken()) {
      setAccount(null);
      return null;
    }
    try {
      const current = await getCurrentAccount();
      setAccount(current);
      return current;
    } catch {
      // 令牌失效：清掉本地会话，进入未登录态
      setSessionToken(null);
      setAccount(null);
      return null;
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const current = await refresh();
      if (!current && DEMO_AUTO_LOGIN && DEMO_EMAIL && DEMO_PASSWORD) {
        // 演示入口：没有本地会话时直接用演示账号登录。登录失败不阻断，照常落到登录页。
        try {
          const session = await loginAccount({ email: DEMO_EMAIL, password: DEMO_PASSWORD });
          setSessionToken(session.token);
          if (!cancelled) setAccount(session.account);
        } catch {
          // 演示账号不可用时保持未登录，避免把错误伪装成已登录
        }
      }
      if (!cancelled) setReady(true);
    })();
    return () => {
      cancelled = true;
    };
  }, [refresh]);

  // 任何请求收到 401（会话被撤销、过期、账号被停用）都回到未登录态
  useEffect(() => {
    onUnauthorized(() => setAccount(null));
    return () => onUnauthorized(null);
  }, []);

  const applySession = useCallback((session: AuthSession) => {
    setSessionToken(session.token);
    setAccount(session.account);
    return session.account;
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      ready,
      account,
      authenticated: Boolean(account),
      login: async (email, password) => applySession(await loginAccount({ email, password })),
      register: async (payload) => applySession(await registerAccount(payload)),
      logout: async () => {
        try {
          await logoutAccount();
        } finally {
          setSessionToken(null);
          setAccount(null);
        }
      },
      refresh,
      applyAccount: setAccount,
    }),
    [ready, account, applySession, refresh],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth 必须在 AuthProvider 内使用");
  return context;
}