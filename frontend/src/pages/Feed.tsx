import type { FormEvent, JSX, ReactNode } from "react";
import { useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";

import { usePlatforms } from "@/api/hooks/useConfig";
import { useCreators } from "@/api/hooks/useCreators";
import {
  useHideVideo,
  useUnhideVideo,
  useVideos,
  VIDEO_HIDDEN_MODES,
  type Video,
  type VideoFilter,
  type VideoHiddenMode,
} from "@/api/hooks/useVideos";
import { HardShadowCard } from "@/components/memphis/HardShadowCard";
import { MemphisButton } from "@/components/memphis/MemphisButton";
import { PageShell } from "@/components/shared/PageShell";
import { QueryState } from "@/components/shared/QueryState";
import { VideoRow } from "@/components/shared/VideoRow";
import { cn } from "@/lib/utils";

/** 一页取多少条。虚拟滚动只看得到屏幕上那十几行，所以一页可以比"能画出来的"大得多。 */
const PAGE_SIZE = 50;

/** 每行的高度只是**初值**：`measureElement` 会在真实渲染后改正它。
 *  给小了会白屏（滚动条跳），给大了会少画几行 —— 30 行高的卡片 + 上下留白取的 96。 */
const ROW_ESTIMATE = 96;

const HIDDEN_LABELS: Record<VideoHiddenMode, string> = {
  visible: "只看未隐藏",
  hidden: "只看已隐藏（墓碑）",
  all: "全部",
};

/**
 * 作品流（`/feed`）：分页 + 过滤 + 墓碑操作，长列表走虚拟滚动。
 *
 * 三点决定：
 * 1. **筛选要点"查询"才发请求**，不随打字发。每敲一个字一个新 queryKey 就是
 *    一次 `/api/videos`，而这句 SQL 是全库 LIKE —— 那是自己给自己做 DoS。
 * 2. **换筛选条件时把页码退回第 1 页**，在同一次提交里做，不用 effect 补：
 *    effect 里 setState 是这个仓库第 N 次躲开 `react-hooks` 规则的地方。
 * 3. 虚拟滚动只画视口内 + `overscan` 的行。**隐藏/取消隐藏的按钮在行里**，
 *    所以行卸载即按钮消失 —— 不共享选中状态，也就没有"选中项被滚走了"那一类 bug。
 */
export function Feed(): JSX.Element {
  const [draft, setDraft] = useState<Draft>({ platform: "", hidden: "visible", search: "" });
  const [applied, setApplied] = useState<Draft>(draft);
  const [page, setPage] = useState(1);

  const videos = useVideos(toFilter(applied, page));
  const platforms = usePlatforms();
  const creators = useCreators();
  const hide = useHideVideo();
  const unhide = useUnhideVideo();

  const names = new Map(
    (creators.data ?? []).map((creator) => [creator.id, creator.name] as const),
  );

  const total = videos.data?.total ?? 0;
  const lastPage = Math.max(1, Math.ceil(total / PAGE_SIZE));

  function submit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    setPage(1);
    setApplied(draft);
  }

  function reset(): void {
    const blank: Draft = { platform: "", hidden: "visible", search: "" };
    setPage(1);
    setDraft(blank);
    setApplied(blank);
  }

  return (
    <PageShell pattern="stripes" className="mx-auto flex max-w-[1100px] flex-col gap-5">
      <header>
        <h1 className="font-display text-display-md">作品流</h1>
        <p className="text-body-md">
          库里每一条作品，按发布时间倒序。隐藏＝打墓碑（不删文件、不删稿子），
          在"只看已隐藏"那一档可以取消。
        </p>
      </header>

      <form
        className="flex flex-wrap items-end gap-4 memphis-border border-ink-black bg-paper-cream p-4"
        onSubmit={submit}
      >
        <Field label="平台">
          <select
            className="memphis-input"
            value={draft.platform}
            onChange={(event) => setDraft({ ...draft, platform: event.target.value })}
          >
            <option value="">全部平台</option>
            {/* 选项来自 `/api/platforms`：写死四家的话，关掉的平台还会在这里出现，
                而按它查出来的空列表会被读成"这个平台没有作品"。 */}
            {(platforms.data?.platforms ?? []).map((platform) => (
              <option key={platform.name} value={platform.name}>
                {platform.display_name}
              </option>
            ))}
          </select>
        </Field>

        <Field label="可见性">
          <select
            className="memphis-input"
            value={draft.hidden}
            onChange={(event) =>
              setDraft({ ...draft, hidden: event.target.value as VideoHiddenMode })
            }
          >
            {VIDEO_HIDDEN_MODES.map((mode) => (
              <option key={mode} value={mode}>
                {HIDDEN_LABELS[mode]}
              </option>
            ))}
          </select>
        </Field>

        <Field label="标题关键词" className="min-w-[220px] flex-1">
          <input
            className="memphis-input"
            type="search"
            placeholder="标题或描述里的关键词（点查询才发请求）"
            value={draft.search}
            onChange={(event) => setDraft({ ...draft, search: event.target.value })}
          />
        </Field>

        <div className="flex gap-3">
          <MemphisButton type="submit" disabled={videos.isFetching}>
            {videos.isFetching ? "查询中…" : "查询"}
          </MemphisButton>
          <MemphisButton type="button" variant="secondary" onClick={reset}>
            重置
          </MemphisButton>
        </div>
      </form>

      <p className="flex flex-wrap items-center gap-3 text-mono-sm">
        <span>
          第 {String(page)} / {String(lastPage)} 页 · 共 {String(total)} 条
        </span>
        {/* `placeholderData` 会带着上一页的数据进入新一轮请求：此时数据是旧的，
            必须说出来，否则"翻页中"看起来像"这一页就是这些"。 */}
        {videos.isPlaceholderData && (
          <span className="bg-lemon-yellow px-2">显示的是上一页的结果</span>
        )}
        {creators.isError && (
          <span className="bg-coral-red px-2">
            作者名读不到，下面每行会显示博主 id（不是"没有作者"）
          </span>
        )}
      </p>

      <QueryState gate={videos} subject="作品流">
        {(data) => (
          <>
            {data.items.length === 0 ? (
              <HardShadowCard>
                <p>
                  这个筛选条件下没有任何作品。这是"没有内容"，不是"读不到" —— 上面那条计数是 0
                  条，而请求本身成功了。
                </p>
              </HardShadowCard>
            ) : (
              <VirtualList
                items={data.items}
                names={names}
                onHide={(video) => hide.mutate({ id: video.id, reason: "用户在作品流里手动隐藏" })}
                hidingId={hide.variables?.id}
                onUnhide={(video) => unhide.mutate(video.id)}
                unhidingId={unhide.variables === undefined ? null : unhide.variables}
              />
            )}
            <div className="flex items-center gap-3">
              <MemphisButton
                variant="secondary"
                disabled={page <= 1 || videos.isFetching}
                onClick={() => setPage(page - 1)}
              >
                上一页
              </MemphisButton>
              <MemphisButton
                variant="secondary"
                disabled={page >= lastPage || videos.isFetching}
                onClick={() => setPage(page + 1)}
              >
                下一页
              </MemphisButton>
              <span className="text-mono-sm">每页 {String(PAGE_SIZE)} 条</span>
            </div>
          </>
        )}
      </QueryState>
    </PageShell>
  );
}

interface Draft {
  platform: string;
  hidden: VideoHiddenMode;
  search: string;
}

/** 空串＝"不加这个筛选"。送 `null` 而不是 `undefined`：
 *  `exactOptionalPropertyTypes` 下 `undefined` 压根不是这些字段的合法取值，
 *  而 `client.ts` 的 `url()` 会把 null 与空串一起丢掉。 */
function toFilter(draft: Draft, page: number): VideoFilter {
  return {
    platform: draft.platform === "" ? null : draft.platform,
    hidden: draft.hidden,
    search: draft.search === "" ? null : draft.search,
    page,
    size: PAGE_SIZE,
  };
}

function Field({
  label,
  children,
  className,
}: {
  label: string;
  children: ReactNode;
  className?: string;
}): JSX.Element {
  return (
    <label className={cn("flex flex-col gap-1", className)}>
      <span className="text-body-sm">{label}</span>
      {children}
    </label>
  );
}

/** 只有视口内 + overscan 那十几行在 DOM 里。 */
function VirtualList({
  items,
  names,
  onHide,
  hidingId,
  onUnhide,
  unhidingId,
}: {
  items: Video[];
  names: Map<number, string>;
  onHide: (video: Video) => void;
  hidingId: number | undefined;
  onUnhide: (video: Video) => void;
  unhidingId: number | null;
}): JSX.Element {
  const parentRef = useRef<HTMLDivElement>(null);
  // TanStack Virtual 返回的是一组不可安全 memoize 的函数，React Compiler 因此**故意**
  // 跳过对这个 hook 的缓存 —— 列表本来就该随滚动重渲染。这条规则把那个"跳过"报成警告，
  // 而 `--max-warnings=0` 让它红。不在 eslint 配置里全局关掉它：那样别处真正不兼容的
  // 写法也看不见了，所以只在这一行放行。
  // eslint-disable-next-line react-hooks/incompatible-library
  const virtualizer = useVirtualizer({
    count: items.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => ROW_ESTIMATE,
    // 10 行 = 约一屏的余量。滚轮快滚时白屏比少画一行更难看。
    overscan: 10,
  });

  return (
    <div
      ref={parentRef}
      className="h-[60vh] overflow-auto memphis-border border-ink-black bg-paper-cream p-3"
      data-slot="feed-scroll"
    >
      <div
        className="relative w-full"
        style={{ height: `${String(virtualizer.getTotalSize())}px` }}
      >
        {virtualizer.getVirtualItems().map((row) => {
          const video = items[row.index];
          if (video === undefined) return null;
          return (
            /* `transform: translateY(...)` 是**算出来的几何**，不是设计令牌：
               这一格必须是任意 px，收进 tokens 反而会把虚拟列表钉死在一个行高上。 */
            <div
              key={row.key}
              data-index={row.index}
              ref={virtualizer.measureElement}
              className="absolute left-0 top-0 w-full pb-3"
              style={{ transform: `translateY(${String(row.start)}px)` }}
            >
              <VideoRow
                video={video}
                creatorName={
                  video.creator_id === undefined || video.creator_id === null
                    ? undefined
                    : names.get(video.creator_id)
                }
                onHide={onHide}
                hiding={hidingId === video.id}
                onUnhide={onUnhide}
                unhiding={unhidingId === video.id}
              />
            </div>
          );
        })}
      </div>
    </div>
  );
}
