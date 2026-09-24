import type { JSX, ReactNode } from "react";
import { Link } from "react-router-dom";

import type { Creator } from "@/api/hooks/useCreators";
import { PlatformBadge } from "@/components/memphis/PlatformBadge";
import { formatCount, formatTime } from "@/lib/formatters";
import { cn } from "@/lib/utils";

export interface CreatorCardProps {
  creator: Creator;
  /** 给了才画开关。只读的地方（总览、详情页）不该能改跟踪状态。
   *  回调只收"要改成什么"：这一行自己就是那位博主，再传一遍是多余参数。 */
  onToggleTracking?: (next: boolean) => void;
  /** 这一位的开关请求正在飞：禁用，避免连点发出两个相反的值。 */
  busy?: boolean;
  /** 给了才画「爆款回溯」按钮（T6.6）。只读列表不该能展开查询。 */
  onToggleBenchmarks?: () => void;
  /** 展开态：只影响那个按钮的 `aria-expanded`，面板本体走 `children`。 */
  benchmarksOpen?: boolean;
  /** 展开时挂在行下方那一整块（爆款面板）。没有就不占位。 */
  children?: ReactNode;
}

/**
 * 一位博主。跟踪开关是真值写入（V1 §7.24：`set_tracking` 是唯一入口，值必须是真 bool），
 * 所以这里只发布尔，不发字符串、不发"看起来像 true 的东西"。
 */
export function CreatorCard({
  creator,
  onToggleTracking,
  busy = false,
  onToggleBenchmarks,
  benchmarksOpen = false,
  children,
}: CreatorCardProps): JSX.Element {
  return (
    <div className="flex flex-col gap-2">
      <article
        className="flex flex-wrap items-center gap-3 memphis-border border-ink-black bg-paper-cream p-3"
        data-creator-id={creator.id}
      >
        <PlatformBadge platform={creator.platform} size="sm" />

        <div className="min-w-0 flex-1">
          <p className="truncate font-heading text-body-lg font-bold">
            {creator.name || "（没有昵称）"}
          </p>
          <p className="flex flex-wrap gap-3 text-body-sm">
            {/* 平台侧 id 一定要在：昵称会漂（V1 §7.1 那条），出问题时唯一能对着后端查的就是它。 */}
            <code data-slot="platform-id">{creator.platform_id}</code>
            <span data-slot="followers">{formatCount(creator.follower_count)} 粉丝</span>
            <span data-slot="added">收录于 {formatTime(creator.created_at)}</span>
          </p>
        </div>

        <Link
          to={creator.profile_url}
          className="memphis-btn memphis-btn--secondary"
          target="_blank"
          rel="noreferrer noopener"
        >
          主页
        </Link>

        {onToggleBenchmarks !== undefined && (
          <button
            type="button"
            onClick={onToggleBenchmarks}
            aria-expanded={benchmarksOpen}
            aria-label={`爆款回溯 ${creator.name || creator.platform_id}`}
            className="memphis-btn memphis-btn--secondary"
          >
            {benchmarksOpen ? "收起爆款" : "爆款回溯"}
          </button>
        )}

        {onToggleTracking === undefined ? (
          <span className={cn("text-body-sm", !creator.is_tracking && "opacity-60")}>
            {creator.is_tracking ? "跟踪中" : "只存档"}
          </span>
        ) : (
          <label className="flex items-center gap-2">
            <button
              type="button"
              role="switch"
              aria-checked={creator.is_tracking}
              data-on={String(creator.is_tracking)}
              className="memphis-switch"
              disabled={busy}
              onClick={() => onToggleTracking(!creator.is_tracking)}
              aria-label={`跟踪 ${creator.name || creator.platform_id}`}
            />
            <span className="text-body-sm">
              {busy ? "写入中…" : creator.is_tracking ? "跟踪中" : "只存档"}
            </span>
          </label>
        )}
      </article>
      {children}
    </div>
  );
}
