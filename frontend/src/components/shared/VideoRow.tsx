import type { JSX } from "react";
import { Link } from "react-router-dom";

import type { Video } from "@/api/hooks/useVideos";
import { MemphisButton } from "@/components/memphis/MemphisButton";
import { PlatformBadge } from "@/components/memphis/PlatformBadge";
import { formatCount, formatDuration, formatTime } from "@/lib/formatters";
import { cn } from "@/lib/utils";

export interface VideoRowProps {
  video: Video;
  /**
   * 作者名。真源是 `/api/creators` 那份 `id → name`（作品行只有 `creator_id`）。
   *
   * 显式允许 `undefined`：调用方拿到的就是 `Map.get()` 那个形状，而
   * `exactOptionalPropertyTypes` 下"可省略"与"可传 undefined"是两件事。
   *
   * 缺值时显示 `博主 #<id>` 而**不是留空**：空掉的那一格看起来跟"这条没有作者"
   * 一模一样，而实际发生的是"作者表没读到" —— 那是两件事（`AGENTS.md §1.3`）。
   */
  creatorName?: string | undefined;
  /** 给了才画"隐藏"按钮：总览页只是看，作品流才动手（V1 §7.25 的墓碑）。 */
  onHide?: (video: Video) => void;
  /** 已隐藏的行：给了才画"取消隐藏"。只进不出的墓碑等于一次误点就回不来。 */
  onUnhide?: (video: Video) => void;
  /** 这一行的隐藏请求正在飞：禁用按钮，别让连点发出两笔 PATCH。 */
  hiding?: boolean;
  /** 同上，取消隐藏那一笔。 */
  unhiding?: boolean;
  className?: string;
}

/**
 * 作品流 / 总览里的一条作品。
 *
 * 只做展示 + 一个动作：行本身不进 `<Link>` 整块可点，因为"隐藏"按钮必须在行里，
 * 整块可点会让键盘用户 Tab 到两个目标而且点按钮先触发跳转。
 */
export function VideoRow({
  video,
  creatorName,
  onHide,
  onUnhide,
  hiding = false,
  unhiding = false,
  className,
}: VideoRowProps): JSX.Element {
  const title = video.title === "" ? `（没有标题 · ${video.platform_video_id}）` : video.title;

  return (
    <article
      className={cn(
        "flex flex-wrap items-center gap-3 memphis-border border-ink-black bg-paper-cream p-3",
        video.is_hidden && "opacity-60",
        className,
      )}
      data-video-id={video.id}
    >
      <PlatformBadge platform={video.platform} size="sm" />

      <div className="min-w-0 flex-1">
        <Link
          to={`/video/${String(video.id)}`}
          className="block truncate font-heading text-body-lg font-bold text-ink-black no-underline"
          title={title}
        >
          {title}
        </Link>
        {/* 每一个字段单独一个节点：挤成一句 meta 的话，"缺哪个字段"在测试里问不出来。 */}
        <p className="flex flex-wrap gap-3 text-body-sm">
          {/* 空串按"没有名字"处理：`??` 会把空串当成有值，那一格就安静地空掉了
              （与经验 37 同一条坑：undefined 与"用户/数据给的是空"不是一回事）。 */}
          <span data-slot="creator">
            {creatorName ? creatorName : `博主 #${String(video.creator_id ?? "未知")}`}
          </span>
          <span data-slot="published">{formatTime(video.published_at)}</span>
          <span data-slot="duration">{formatDuration(video.duration_seconds)}</span>
          <span data-slot="views">{formatCount(video.view_count)} 次播放</span>
          {video.is_hidden && (
            <span
              className="bg-grey-mist px-2"
              title={video.hidden_reason ?? ""}
              data-slot="hidden-reason"
            >
              已隐藏：{video.hidden_reason ?? "没写原因"}
            </span>
          )}
        </p>
      </div>

      {onHide !== undefined && !video.is_hidden && (
        <MemphisButton
          variant="danger"
          disabled={hiding}
          onClick={() => onHide(video)}
          aria-label={`隐藏《${title}》`}
        >
          {hiding ? "隐藏中…" : "隐藏"}
        </MemphisButton>
      )}

      {onUnhide !== undefined && video.is_hidden && (
        <MemphisButton
          variant="secondary"
          disabled={unhiding}
          onClick={() => onUnhide(video)}
          aria-label={`取消隐藏《${title}》`}
        >
          {unhiding ? "处理中…" : "取消隐藏"}
        </MemphisButton>
      )}
    </article>
  );
}
