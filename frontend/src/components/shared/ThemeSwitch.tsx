import type { JSX } from "react";

import { THEME_MODES, type ThemeMode } from "@/lib/theme";
import { useSettings } from "@/stores/settings";
import { cn } from "@/lib/utils";

/** 三档的界面文案。`system` 不写作"自动"：它跟随的是**操作系统**的偏好，
 *  而"自动"会被读成"按时间换"或"按内容换"这一类根本没实现的东西。 */
const THEME_LABELS: Record<ThemeMode, string> = {
  light: "亮",
  dark: "暗",
  system: "跟随系统",
};

/**
 * 主题切换（T5.6）。写的是 store 里那一档，**不直接碰 `data-theme`** ——
 * DOM 那一侧只有 `Layout` 一个写点，两处写就会互相覆盖（表现为"点了没反应"）。
 *
 * 三档都在界面上按得动，且当前档带 `aria-pressed`：不用 `<select>` 是因为这一页的
 * 控件都是孟菲斯的块状按钮，一个下拉框为了三档值不值得的问题不该由配色回答。
 */
export function ThemeSwitch(): JSX.Element {
  const themeMode = useSettings((state) => state.themeMode);
  const setThemeMode = useSettings((state) => state.setThemeMode);

  return (
    <div
      className="flex flex-wrap items-center gap-2"
      role="group"
      aria-label="主题"
      data-testid="theme-switch"
    >
      <span className="text-body-sm">主题</span>
      {THEME_MODES.map((mode: ThemeMode) => (
        <button
          key={mode}
          type="button"
          aria-pressed={themeMode === mode}
          className={cn(
            "memphis-btn memphis-border border-ink-black !px-3 !py-1 text-body-sm",
            themeMode === mode ? "bg-electric-blue text-paper-cream" : "bg-paper-cream",
          )}
          onClick={() => setThemeMode(mode)}
        >
          {THEME_LABELS[mode]}
        </button>
      ))}
    </div>
  );
}
