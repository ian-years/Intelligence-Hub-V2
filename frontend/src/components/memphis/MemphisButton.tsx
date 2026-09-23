import { motion, useReducedMotion } from "framer-motion";
import type { HTMLMotionProps } from "framer-motion";
import type { JSX } from "react";

import { DURATION, EASING } from "@/lib/tokens";
import { cn } from "@/lib/utils";

type Variant = "primary" | "secondary" | "danger";

/**
 * props 直接取 `HTMLMotionProps<"button">` 而不是 `ComponentPropsWithoutRef<"button">`：
 * 被渲染的元素就是 motion.button，用它的类型才不会在 `exactOptionalPropertyTypes` 下
 * 撞 `style`（motion 侧是 `MotionStyle`，不接受 `undefined`）。
 */
export interface MemphisButtonProps extends HTMLMotionProps<"button"> {
  variant?: Variant;
}

/**
 * 孟菲斯按钮（§8 component.button + §7 button_press）。
 *
 * 按下压扁 → 弹回走 framer-motion（§7 的纪律是"所有动效走 framer-motion"），
 * 时长与缓动从 `tokens.json` 读，不在这写 `0.15` 这种字面量。
 * `useReducedMotion()` 是 §7 第三条的 JS 侧那一半 —— CSS 那条 `@media`
 * 管不到 motion 的属性动画。
 */
export function MemphisButton({
  variant = "primary",
  className,
  disabled,
  type = "button",
  children,
  ...rest
}: MemphisButtonProps): JSX.Element {
  const reduce = useReducedMotion();
  return (
    <motion.button
      type={type}
      // `whileTap={undefined}` 在 exactOptionalPropertyTypes 下不合法，而且
      // "减少动效"要的语义是**没有这个属性**，不是"有一个空的"。
      {...(reduce ? {} : { whileTap: { scale: 0.95 } })}
      transition={{ duration: DURATION.fast, ease: EASING.bounce }}
      className={cn("memphis-btn", `memphis-btn--${variant}`, className)}
      disabled={disabled}
      {...rest}
    >
      {children}
    </motion.button>
  );
}
