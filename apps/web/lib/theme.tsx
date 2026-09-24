"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";

/** 黑夜 / 白天主题：默认白天，选择持久化在 localStorage。 */

type Theme = "light" | "dark";

type ThemeState = { theme: Theme; toggle: () => void };

const STORAGE_KEY = "math-agent-theme";
const ThemeContext = createContext<ThemeState | null>(null);

function applyTheme(theme: Theme) {
  if (typeof document === "undefined") return;
  document.documentElement.setAttribute("data-theme", theme);
}

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [theme, setTheme] = useState<Theme>("light");

  useEffect(() => {
    const stored = typeof window !== "undefined" ? window.localStorage.getItem(STORAGE_KEY) : null;
    const initial = stored === "dark" || stored === "light" ? stored : "light";
    setTheme(initial as Theme);
    applyTheme(initial as Theme);
  }, []);

  const toggle = useCallback(() => {
    setTheme((current) => {
      const next: Theme = current === "dark" ? "light" : "dark";
      applyTheme(next);
      if (typeof window !== "undefined") {
        window.localStorage.setItem(STORAGE_KEY, next);
      }
      return next;
    });
  }, []);

  return <ThemeContext.Provider value={{ theme, toggle }}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeState {
  const context = useContext(ThemeContext);
  if (!context) throw new Error("useTheme 必须在 ThemeProvider 内使用");
  return context;
}