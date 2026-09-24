import { useEffect, type JSX } from "react";

import { Outlet } from "react-router-dom";

import { Sidebar } from "./Sidebar";
import { useUi } from "@/stores/ui";
import { useSettings } from "@/stores/settings";
import { applyTheme } from "@/lib/theme";

/** 侧栏 + 内容区。图案**不在这里**（每页自己 `<PageShell pattern=…>`）。
 *
 * 主题也只有这一处碰 DOM：`data-theme` 写在 `<html>` 上（不是这个 div —— 页面底色
 * 由 `html` 的 `background-color` 出，写在 div 上会在滚动溢出区留一条白）。
 * `system` 那一档要**跟着系统偏好变**，不是只在挂载时读一次：只读一次的话，
 * 用户在操作系统里切到暗色，界面要刷新才跟得上，而那看起来像"开关坏了"。 */
export function Layout(): JSX.Element {
  const mobileNavOpen = useUi((s) => s.mobileNavOpen);
  const themeMode = useSettings((s) => s.themeMode);

  useEffect(() => {
    const media = globalThis.matchMedia?.("(prefers-color-scheme: dark)");
    const paint = (): void => {
      applyTheme(themeMode, media?.matches ?? false);
    };
    paint();
    if (media === undefined) return undefined;
    media.addEventListener("change", paint);
    return () => media.removeEventListener("change", paint);
  }, [themeMode]);

  return (
    <div className="flex min-h-screen bg-paper-cream text-ink-black">
      <aside className="hidden md:block">
        <Sidebar />
      </aside>
      {mobileNavOpen && (
        <div className="fixed inset-0 z-10 md:hidden">
          <Sidebar />
        </div>
      )}
      <main className="min-w-0 flex-1">
        <Outlet />
      </main>
    </div>
  );
}
