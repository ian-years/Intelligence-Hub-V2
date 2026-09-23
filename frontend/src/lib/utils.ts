import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** Tailwind 感知的 class 合并：后面的工具类覆盖前面的，而不是两个都留。 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
