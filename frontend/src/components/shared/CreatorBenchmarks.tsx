import type { JSX } from "react";
import { Link } from "react-router-dom";

import type { Creator } from "@/api/hooks/useCreators";
import { BENCHMARK_SORT, useVideos } from "@/api/hooks/useVideos";
import { HardShadowCard } from "@/components/memphis/HardShadowCard";
import { QueryState } from "@/components/shared/QueryState";
import { formatCount, formatTime } from "@/lib/formatters";

/** 一次回溯取几条。V1 的 `--benchmark N` 默认是 5，这里同值：
 *  爆款回溯是"看这个人的天花板在哪"，不是翻页看全库。 */
export const BENCHMARK_SIZE = 5;

/**
 * 一位博主的历史爆款（T6.6）。就是 `/api/videos?creator_id=&sort=benchmark`，
 * 界面上不另算一遍顺序 —— 前端排序与后端排序迟早会不一致（V1 §7.10 那一族）。
 *
 * 那个数字是**库里最近一次入库的点赞读数**，不是"发布当天的点赞"，也不是快照表里的峰值：
 * `videos.like_count` 每次采集都会被更新成当时的值，而点赞只涨不跌，所以它就是已知最高水位。
 * 快照表（ADR-0020）回答的是另一件事（"发布 24 小时到没到千"这种增长形状），
 * 那句话写在面板上而不是只写在代码里 —— 这一栏很容易被读成"爆款榜"而忘了它没有时间窗。
 */
export function CreatorBenchmarks({ creator }: { creator: Creator }): JSX.Element {
  const videos = useVideos({
    creator_id: creator.id,
    sort: BENCHMARK_SORT,
    size: BENCHMARK_SIZE,
  });

  return (
    <HardShadowCard className="flex flex-col gap-2">
      <p className="text-mono-sm">
        爆款回溯 · 按库里的点赞读数前 {String(BENCHMARK_SIZE)} 条（读数是最近一次采集时的值）
      </p>
      <QueryState gate={videos} subject="这一位的作品">
        {(page) => (
          <>
            {page.items.length === 0 ? (
              // 空列表与"读不到"必须分开说：这一位很可能只是还没采过。
              <p>这一位库里还没有作品 —— 先跑一次采集，或确认跟踪开关是开着的。</p>
            ) : (
              <ol className="flex flex-col gap-1">
                {page.items.map((video, index) => (
                  <li key={video.id} className="flex flex-wrap items-baseline gap-2 text-body-md">
                    <code className="text-mono-sm opacity-70">
                      {String(index + 1).padStart(2, "0")}
                    </code>
                    <Link to={`/video/${String(video.id)}`} className="min-w-0 flex-1 truncate">
                      {video.title || "（没有标题）"}
                    </Link>
                    <span className="text-mono-sm">{formatCount(video.like_count)} 赞</span>
                    <span className="text-mono-sm opacity-70">
                      {formatTime(video.published_at)}
                    </span>
                  </li>
                ))}
              </ol>
            )}
          </>
        )}
      </QueryState>
    </HardShadowCard>
  );
}
