/** 主题模式与那个属性的唯一写点（T5.6 / ADR-0023）。
 *
 * 界面上能给的三档：`light` / `dark` / `system`。CSS 侧只认两种
 * （`[data-theme="light"]` 与 `[data-theme="dark"]`），因为样式里没有"跟随"这种状态 ——
 * `system` 必须在写进 DOM 之前被**解成一个具体的值**，否则暗色块永远不生效，
 * 而症状是"选了跟随就变回亮色"，看起来像 bug，实际上是漏了一步解析。
 */

export const THEME_MODES = ["light", "dark", "system"] as const;
export type ThemeMode = (typeof THEME_MODES)[number];

export const RESOLVED_THEMES = ["light", "dark"] as const;
export type ResolvedTheme = (typeof RESOLVED_THEMES)[number];

/** 没存过、存坏了、或者 `system` 那一档拿不到偏好时的落点。
 * 亮色是**默认而不是第三个选项**：设计令牌的字面色就是按亮色调的（§2），
 * 首屏落错档会让整站第一次渲染就是未调过的那一套。 */
export const FALLBACK_THEME: ResolvedTheme = "light";

/** localStorage 里那份值 → 认识的三档之一。认不出来不猜（同一个理由见
 * `stores/settings.ts` 的 merge：那是外部输入，手改过或别的版本写过都算）。 */
export function themeFromStored(raw: unknown): ThemeMode | null {
  return THEME_MODES.find((mode) => mode === raw) ?? null;
}

/** 解析成 CSS 认的那两种之一。`systemPrefersDark` 由调用方给（不是这里读 matchMedia）：
 * 解析必须是纯函数，否则"跟随系统"这一档在测试与 SSR 里根本问不出对错。 */
export function resolveTheme(mode: ThemeMode, systemPrefersDark: boolean): ResolvedTheme {
  if (mode === "system") return systemPrefersDark ? "dark" : FALLBACK_THEME;
  return mode;
}

/** 系统偏好。`matchMedia` 不在场（老浏览器、非 DOM 环境）时报 false ——
 * 那不是"猜成亮色"，而是与 `FALLBACK_THEME` 同值：拿不到偏好就该落在调过的那一版。 */
export function systemPrefersDark(): boolean {
  const media = globalThis.matchMedia?.("(prefers-color-scheme: dark)");
  return media?.matches === true;
}

/** 把解析结果写进 `<html>`。全站只有这一处碰 `dataset.theme`。 */
export function applyTheme(mode: ThemeMode, prefersDark: boolean): ResolvedTheme {
  const resolved = resolveTheme(mode, prefersDark);
  document.documentElement.dataset.theme = resolved;
  return resolved;
}
