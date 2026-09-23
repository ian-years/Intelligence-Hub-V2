import type { HTMLAttributes, JSX } from "react";

import { cn } from "@/lib/utils";

export interface HardShadowCardProps extends HTMLAttributes<HTMLDivElement> {
  /** §7 card_hover：抬起 2px 并换 `hard_shadow_lg`。列表卡要，静态分组卡不要。 */
  hover?: boolean;
}

/**
 * 3px 黑边 + 6px 硬阴影的卡片（§3 形状语言的默认载体）。
 *
 * 为什么给一个类而不是让每页写 `border-[3px] shadow-[6px_6px_0_#0A0A0A]`：
 * 后者把 `#0A0A0A` 抄进了 7 个文件，而 §3 的"禁止带模糊的阴影"要靠
 * stylelint 检查 CSS，工具类里的任意值它看不见。
 */
export function HardShadowCard({
  hover = false,
  className,
  children,
  ...rest
}: HardShadowCardProps): JSX.Element {
  return (
    <div
      className={cn("memphis-card", hover && "memphis-card--hover", className)}
      data-hover={String(hover)}
      {...rest}
    >
      {children}
    </div>
  );
}
