import type { JSX } from "react";

import type { ReactNode } from "react";

import type { PatternName } from "@/lib/patterns";
import { cn } from "@/lib/utils";

/**
 * 每页的外壳：图案（§6 一页一种）+ 统一留白。
 *
 * 图案由**页面**声明而不是由路由表声明：路由表看不见 `/video/:id` 这类不进侧栏的路由，
 * 而"这一页是什么图案"本来就是这个页面自己的决定。
 */
export function PageShell({
  pattern,
  children,
  className,
}: {
  pattern: PatternName;
  children: ReactNode;
  className?: string;
}): JSX.Element {
  return (
    <div
      className={cn(`bg-pattern-${pattern}`, "min-h-screen p-6", className)}
      data-pattern={pattern}
    >
      {children}
    </div>
  );
}
