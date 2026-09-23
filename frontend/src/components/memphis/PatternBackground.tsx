import type { HTMLAttributes, JSX } from "react";

import type { PatternName } from "@/lib/patterns";
import { cn } from "@/lib/utils";

export interface PatternBackgroundProps extends HTMLAttributes<HTMLDivElement> {
  pattern: PatternName;
}

/**
 * SVG 平铺背景（§6）。每页一种，映射表在 `src/styles/patterns.css` 的
 * `.page-*` 那几条 —— 这个组件只负责"我就是要这一种图案"，
 * 不复制色值也不内联 data URI，否则图案就有两处定义了。
 */
export function PatternBackground({
  pattern,
  className,
  children,
  ...rest
}: PatternBackgroundProps): JSX.Element {
  return (
    <div className={cn(`bg-pattern-${pattern}`, className)} data-pattern={pattern} {...rest}>
      {children}
    </div>
  );
}
