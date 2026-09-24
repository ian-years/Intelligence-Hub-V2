import { useState, type JSX, type RefObject } from "react";

import { mediaUrl } from "@/lib/media";

/**
 * 本地媒体播放器（T6.5）。`<video>` 的字节来自 `/api/videos/{id}/media`（ADR-0023），
 * 前端不碰磁盘路径。
 *
 * 三处刻意的设计：
 * - **`preload="metadata"`**：一集视频几百 MB，默认值会把整段拖下来才肯画进度条。
 *   只要时长与首帧，打开详情页才是一次可接受的请求。
 * - **出错要说出来**：`<video>` 读不到文件时只留一块黑（控件都不给），症状与
 *   "这一条本来就没有媒体"一模一样。所以 `onError` 落一行原文，
 *   且指针在能播之后**清掉**它（`onPlaying`）—— 一次抖动的网络不该留下永久判词。
 * - **`elementRef` 由调用方给**：口播稿的跳转按钮在页面另一张卡片里，两边摸的是
 *   同一个 `<video>` 元素。组件自己存 ref 就等于把"谁能跳"关在这个文件里，
 *   而跳转的语义属于页面。
 */
export function VideoPlayer({
  videoId,
  elementRef,
}: {
  videoId: number;
  elementRef: RefObject<HTMLVideoElement | null>;
}): JSX.Element {
  const [note, setNote] = useState<string | null>(null);

  return (
    <figure className="flex flex-col gap-2">
      <video
        ref={elementRef}
        controls
        preload="metadata"
        src={mediaUrl(videoId)}
        aria-label="播放器"
        className="w-full border-2 border-ink-black bg-ink-black"
        onError={() =>
          setNote(
            `媒体取不到（浏览器没给原因）。库里记着路径但 ${mediaUrl(videoId)} 没出图 —— ` +
              "多半是文件被移动或删过，跑一次 tools/rescan_local.py 对回磁盘。",
          )
        }
        onPlaying={() => setNote(null)}
      />
      {note && (
        <figcaption aria-live="polite" className="bg-coral-red px-3 py-2 text-body-sm">
          {note}
        </figcaption>
      )}
    </figure>
  );
}
