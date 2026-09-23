/** 五种图案的名字（定义在 `src/styles/patterns.css` 的 `--pattern-*`）。
 *
 * 单独一个文件而不是跟着组件导出：`react-refresh` 要求"组件文件只导出组件"，
 * 混在一起时这个组件的 HMR 会整文件失效（fast-refresh 那条 lint 就是为这个）。
 * 每条都有对应工具类这件事由 `src/styles/tokens.spec.ts` 核。 */
export const PATTERNS = ["dots", "stripes", "waves", "confetti", "checker"] as const;
export type PatternName = (typeof PATTERNS)[number];
