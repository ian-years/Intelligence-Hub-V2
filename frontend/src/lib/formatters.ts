/** 展示用的纯函数。放一处是因为预检与任务页要解析的是**同一批**由后端
 * `summary` 里拼出来的字符串 —— 两处各写一个 split 就会有两套容错。 */

/** `"douyin=ok, bilibili=degraded"` → `[["douyin","ok"],…]`。
 *
 * 源是 `tasks/preflight.py` 里那句
 * `", ".join(f"{k}={v}" for k, v in sorted(platforms.items()))`。
 * 契约上它是**一个人读的字符串**而不是结构体，所以这里只按那个形状拆，
 * 拆不动的片段原样回给界面显示 —— **不猜、不补默认值**：
 * 猜出来的"unknown"会把一个格式漂移伪装成"这个平台状态未知"。 */
export function parsePairs(raw: string | undefined): [string, string][] {
  if (!raw) return [];
  return raw.split(", ").map((chunk) => {
    const eq = chunk.indexOf("=");
    return eq < 0 ? [chunk, ""] : [chunk.slice(0, eq), chunk.slice(eq + 1)];
  });
}

/** `"ffmpeg, ffprobe"` → `["ffmpeg","ffprobe"]`；空串 → `[]`。
 * 后端用 `"（无）"` / `"（PATH 上一个都没有）"` 这类**整串**表达空态，
 * 那种值会原样变成一个条目显示出去（这就是它的语义，不需要前端再发明一套）。 */
export function parseNames(raw: string | undefined): string[] {
  if (!raw) return [];
  return raw
    .split(", ")
    .map((name) => name.trim())
    .filter((name) => name !== "");
}

/** ISO 时间 → 本地可读。解析不了就把原文显示出来（比 `Invalid Date` 诚实）。 */
export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const stamp = Date.parse(iso);
  if (Number.isNaN(stamp)) return iso;
  return new Date(stamp).toLocaleString("zh-CN", { hour12: false });
}

/** 字节数 → MiB（保留一位）。`null` 与 0 要能分开：0 是"真的没有"。 */
export function formatMiB(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "—";
  if (!Number.isFinite(bytes) || bytes < 0) return String(bytes);
  return `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
}

/** 进度 0..1 → 百分比字符串。越界不夹逼：那说明上游算错了，要看得见。 */
export function formatPercent(ratio: number | null | undefined): string {
  if (ratio === null || ratio === undefined) return "—";
  return `${(ratio * 100).toFixed(0)}%`;
}
