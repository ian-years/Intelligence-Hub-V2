import tokensJson from "../../tokens.json";

/**
 * 从 `tokens.json`（`src/styles/tokens.css` 的机器可读投影）读令牌给 JS 侧用。
 *
 * 为什么走 JSON 而不是在 TS 里再写一遍 `"cubic-bezier(0.34, 1.56, ...)"`：
 * §7 的纪律是"所有动效走 framer-motion"，而 framer-motion 拿的是 JS 对象 ——
 * 那正是最容易长出第二份常量的地方。这里读投影，令牌仍然只有一处。
 *
 * 取不到就**抛**：动效令牌缺失时返回 `undefined` 会得到一个"静默没有弹跳"的界面，
 * 而那看起来完全像是设计本来就这样。
 */
const TABLE = tokensJson.tokens as Record<string, unknown>;

export function token(name: string): string {
  const value = TABLE[name];
  if (typeof value !== "string") {
    throw new Error(`tokens.json 里没有字符串令牌 ${name}（源是 src/styles/tokens.css 的 @theme）`);
  }
  return value;
}

/** framer-motion 要秒数，令牌写的是 `150ms`。单位必须在令牌里 —— 不认单位的值直接抛。 */
export function durationSeconds(name: string): number {
  const raw = token(name);
  const match = /^([\d.]+)(ms|s)$/.exec(raw);
  const value = match ? Number.parseFloat(match[1] as string) : Number.NaN;
  if (!Number.isFinite(value) || !match) {
    throw new Error(`动效时长令牌看不懂（要 150ms / 0.4s 这种带单位的）：${name} = ${raw}`);
  }
  return match[2] === "ms" ? value / 1000 : value;
}

/**
 * `cubic-bezier(0.34, 1.56, 0.64, 1)` → framer-motion 要的四个数。
 *
 * 为什么要解析而不是直接把字符串塞进去：`Easing` 是一个联合类型，
 * 直接把 `string` 交出去要么靠 cast（把类型系统关掉一节），要么编译期就红。
 * 解析一遍的额外好处是"令牌写歪了会当场炸"，而不是运行时静默没有缓动。
 */
export function cubicBezier(name: string): [number, number, number, number] {
  const raw = token(name);
  const inner = /^cubic-bezier\(([-\d.,\s]+)\)$/.exec(raw)?.[1];
  const parts = inner
    ?.split(",")
    .map((part) => Number.parseFloat(part.trim()))
    .filter((value) => Number.isFinite(value));
  const [a, b, c, d] = parts ?? [];
  if (a === undefined || b === undefined || c === undefined || d === undefined) {
    throw new Error(`缓动令牌看不懂（要 cubic-bezier(a, b, c, d)）：${name} = ${raw}`);
  }
  return [a, b, c, d];
}

export const EASING = {
  bounce: cubicBezier("--ease-bounce"),
  snap: cubicBezier("--ease-snap"),
} as const;

export const DURATION = {
  instant: durationSeconds("--duration-instant"),
  fast: durationSeconds("--duration-fast"),
  normal: durationSeconds("--duration-normal"),
  slow: durationSeconds("--duration-slow"),
} as const;

export const PLATFORM_NAMES = ["douyin", "bilibili", "xiaohongshu", "youtube"] as const;
export type PlatformName = (typeof PLATFORM_NAMES)[number];

/** §2.4：徽章的形状也是令牌（色已经走 CSS 变量了，形状只能走 TS）。 */
export const PLATFORM_SHAPES: Record<PlatformName, "triangle" | "circle" | "square" | "wave"> = {
  douyin: "triangle",
  bilibili: "circle",
  xiaohongshu: "square",
  youtube: "wave",
};
