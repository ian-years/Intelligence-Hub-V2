/** 播放器要用的三件小事：媒体地址、逐句时间轴、时钟文案。

放在 `lib/` 而不是组件文件里，是因为组件文件混导出常量会被 `react-refresh` 抱怨
（`ScheduleCard` / `ExportLinks` 那一条成例：常量搬到 lib，热重载才只换组件）。

为什么地址要由这里拼：`videos.media_path` 存的是**相对 `data/`** 的路径，而 `data/` 在哪
是配置（`INTELLIGENCE_HUB_DATA__DIR`）—— 前端自己拼就等于把磁盘目录结构写进前端。
所以字节只从 `/api/videos/{id}/media` 出（ADR-0023）。
*/

/** `TranscriptSegment`（`models/transcript.py`）在前端的那一份读形。
 * 端点交出的 `segments_json` 是**字符串**，不是结构化数组（库里那一列就是文本），
 * 所以这一层要负责"读不出结构时怎么办"。 */
export interface TranscriptSegment {
  start_seconds: number;
  end_seconds: number;
  text: string;
}

export function mediaUrl(videoId: number): string {
  return `/api/videos/${String(videoId)}/media`;
}

/**
 * `segments_json` → 可点的时间轴。读不出来的全部回落成空数组。
 *
 * 为什么在这里兜：ASR 写的那一列是 `[{start_seconds,end_seconds,text}, …]`，但同一张表里
 * 还有 V1 迁移来的行（`tools/migrate_from_v1.py`），而这一列没有 CHECK 约束 —— 一个不是
 * 数组的 JSON、一个缺 `start_seconds` 的元素，都真可能在库里。
 * 判据是"要么整份能用，要么一份都不用"：半截时间轴比没有更坏，因为点了不动的按钮
 * 会被读成播放器坏了，而不是数据坏了。
 */
export function parseSegments(raw: string | null | undefined): TranscriptSegment[] {
  if (!raw) return [];
  let decoded: unknown;
  try {
    decoded = JSON.parse(raw);
  } catch {
    return [];
  }
  if (!Array.isArray(decoded)) return [];
  const segments: TranscriptSegment[] = [];
  for (const item of decoded) {
    if (typeof item !== "object" || item === null) return [];
    const record = item as Record<string, unknown>;
    const start = record["start_seconds"];
    const text = record["text"];
    if (typeof start !== "number" || typeof text !== "string") return [];
    const end = record["end_seconds"];
    segments.push({
      start_seconds: start,
      end_seconds: typeof end === "number" ? end : 0,
      text,
    });
  }
  return segments;
}

/** `4:07` / `1:02:03` —— 时间戳要能点，就得先能读。 */
export function clockLabel(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (value: number): string => String(value).padStart(2, "0");
  return h > 0 ? `${String(h)}:${pad(m)}:${pad(s)}` : `${String(m)}:${pad(s)}`;
}
