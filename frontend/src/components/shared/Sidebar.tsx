import type { JSX } from "react";

import { NavLink } from "react-router-dom";

import { NAV_ITEMS } from "@/lib/nav";
import { PlatformBadge } from "@/components/memphis/PlatformBadge";
import { usePlatforms } from "@/api/hooks/useConfig";
import { cn } from "@/lib/utils";
import { useUi } from "@/stores/ui";

export function Sidebar(): JSX.Element {
  const collapsed = useUi((s) => s.sidebarCollapsed);
  const toggle = useUi((s) => s.toggleSidebar);
  const platforms = usePlatforms();

  return (
    <nav
      aria-label="主导航"
      className={cn(
        "flex h-full shrink-0 flex-col gap-6 memphis-border border-r-ink-black bg-paper-cream p-4",
        collapsed ? "w-20" : "w-60",
      )}
    >
      <div className="flex items-center justify-between gap-3">
        {!collapsed && (
          <span className="font-display text-display-md leading-none">
            IH<span className="text-electric-blue">·</span>V2
          </span>
        )}
        <button
          type="button"
          onClick={toggle}
          className="memphis-btn memphis-btn--secondary !px-3 !py-1"
          aria-label={collapsed ? "展开侧栏" : "收起侧栏"}
        >
          {collapsed ? "»" : "«"}
        </button>
      </div>

      <ul className="flex list-none flex-col gap-2 p-0">
        {NAV_ITEMS.map((item) => (
          <li key={item.to}>
            <NavLink
              to={item.to}
              end={item.to === "/"}
              className={({ isActive }: { isActive: boolean }) =>
                cn(
                  "block memphis-border border-ink-black px-3 py-2 no-underline",
                  isActive
                    ? "bg-electric-blue text-paper-cream shadow-hard-sm"
                    : "bg-paper-cream text-ink-black",
                )
              }
            >
              {item.label}
              {!item.built && <span className="ml-2 text-body-sm">·待建</span>}
            </NavLink>
          </li>
        ))}
      </ul>

      <div className="mt-auto flex flex-col gap-2">
        {/* 平台清单来自 `/api/platforms`：侧栏这块是"哪些平台现在是活的"，
            不是前端自己列的四家 —— 关掉的平台会从这里消失，这正是要看见的。 */}
        <span className="text-body-sm">平台</span>
        {/* paused 与 pending 必须分开说：窗口不在前台时 react-query 会挂起补发，
            此时 `isPending` 永远是 true —— 只写"读取中…"就等于让界面看起来
            "马上就出来了"，而它其实不动了（2026-09-23 在真实页面里量到的就是这个）。 */}
        {platforms.isPaused && <span className="text-body-sm">读取被暂停（窗口不在前台）</span>}
        {platforms.isPending && !platforms.isPaused && (
          <span className="text-body-sm">读取中…</span>
        )}
        {platforms.isError && (
          <span className="text-body-sm text-coral-red">读不到：{platforms.error.message}</span>
        )}
        {platforms.data?.platforms.map((platform) => (
          <PlatformBadge
            key={platform.name}
            platform={platform.name}
            label={platform.display_name}
            size="sm"
            className={platform.enabled ? "" : "opacity-40"}
          />
        ))}
      </div>
    </nav>
  );
}
