import type { JSX } from "react";

import { Outlet } from "react-router-dom";

import { Sidebar } from "./Sidebar";
import { useUi } from "@/stores/ui";

/** 侧栏 + 内容区。图案**不在这里**（每页自己 `<PageShell pattern=…>`）。 */
export function Layout(): JSX.Element {
  const mobileNavOpen = useUi((s) => s.mobileNavOpen);
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
