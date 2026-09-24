import type { Metadata, Viewport } from "next";
import "katex/dist/katex.min.css";
import "./globals.css";
import { WorkspaceProvider } from "../lib/workspace";
import { ThemeProvider } from "../lib/theme";
import { AuthProvider } from "../lib/auth";
import { AppShell } from "../components/shell";

export const metadata: Metadata = {
  title: "synapforge · Agent 协作平台",
  description: "数学建模竞赛多 Agent 协作工作台：模板包、文档版本、审核门禁与论文交付",
};

// viewportFit: cover 才能用 env(safe-area-inset-*)（底部标签栏与 toast 要避开 iPhone 的横条）；
// 刻意不设 maximumScale / userScalable，保留双指缩放（无障碍）。
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#fafafa" },
    { media: "(prefers-color-scheme: dark)", color: "#111111" },
  ],
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>
        <ThemeProvider>
          <AuthProvider>
            <WorkspaceProvider>
              <AppShell>{children}</AppShell>
            </WorkspaceProvider>
          </AuthProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}