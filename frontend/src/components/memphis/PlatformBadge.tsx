import type { CSSProperties, JSX } from "react";

import { PLATFORM_NAMES, type PlatformName } from "@/lib/tokens";
import { cn } from "@/lib/utils";

/** §9：一个平台一个几何形状。形状是**设计令牌**，不是后端数据。 */
const GLYPHS: Record<PlatformName, JSX.Element> = {
  douyin: <polygon points="7,1 13,12 1,12" />,
  bilibili: <circle cx="7" cy="7" r="5.5" />,
  xiaohongshu: <rect x="1.5" y="1.5" width="11" height="11" />,
  youtube: <path d="M1,9 Q4,3 7,9 T13,9" fill="none" strokeWidth={2} />,
};

/**
 * 占位用的展示名表。**有数据时一律传 `label`** —— 展示名的真源是后端
 * `PlatformConfig.display_name`（经 `/api/platforms` 出来），这一份只服务于
 * "还没有任何响应可问"的时刻（空列表、离线兜底），别让徽章只剩一个形状。
 */
const FALLBACK_LABELS: Record<PlatformName, string> = {
  douyin: "抖音",
  bilibili: "B站",
  xiaohongshu: "小红书",
  youtube: "YouTube",
};

const SIZES = { sm: 10, md: 14 } as const;

export interface PlatformBadgeProps {
  platform: string;
  /** 展示名。真源是 API 的 `display_name`；不传才用上面的占位表。 */
  label?: string;
  size?: keyof typeof SIZES;
  className?: string;
}

/**
 * 平台徽章：色块 + 几何形状 + 名字（`docs/specs/ui-tokens.md §9`）。
 *
 * 认不出的平台名**不折叠成"未知平台"**，而是把那个字符串原样打出来并给灰底：
 * 平台只有配置里那四家，出现第五个名字说明配置与前端漂了，
 * 那正是该在屏幕上跳出来的时刻（AGENTS.md §1.3「不许臆造成功」的 UI 版）。
 */
export function PlatformBadge({
  platform,
  label,
  size = "md",
  className,
}: PlatformBadgeProps): JSX.Element {
  const known = (PLATFORM_NAMES as readonly string[]).includes(platform);
  const glyph = known ? GLYPHS[platform as PlatformName] : UNKNOWN_GLYPH;
  const style: CSSProperties = {
    background: known ? `var(--color-platform-${platform})` : "var(--color-grey-mist)",
  };

  return (
    <span
      className={cn("memphis-badge", className)}
      data-platform={platform}
      data-known={String(known)}
      style={style}
    >
      <svg
        width={SIZES[size]}
        height={SIZES[size]}
        viewBox="0 0 14 14"
        fill="var(--color-ink-black)"
        stroke="var(--color-ink-black)"
        aria-hidden="true"
      >
        {glyph}
      </svg>
      <span>{label ?? (known ? FALLBACK_LABELS[platform as PlatformName] : platform)}</span>
    </span>
  );
}

const UNKNOWN_GLYPH = (
  <text x="7" y="11" textAnchor="middle" fontSize="11" fill="var(--color-ink-black)">
    ?
  </text>
);
