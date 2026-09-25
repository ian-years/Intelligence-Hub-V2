/** 创作工坊那一页要用的纯函数（T4.7）。
 *
 * 放在 `lib/` 而不是组件里：视图的"派生"部分（分镜怎么切、提词器怎么断句、
 * 什么时候该自动保存）是**判据**，不是 JSX —— 判据要能被单独测，
 * 而通过渲染去测它们等于把一条数学式子挂在三个 useState 上。
 */

/** V1 的自动保存节奏（~1.6 秒）。为什么不是"每敲一个字存一次"：
 *  那是拿一位作者的打字速度去敲 `/api/drafts` 的写盘路径。 */
export const AUTOSAVE_DELAY_MS = 1_600;

export const WORKSHOP_VIEWS = ["editor", "beats", "teleprompter", "shots"] as const;
export type WorkshopView = (typeof WORKSHOP_VIEWS)[number];

export const VIEW_LABELS: Record<WorkshopView, string> = {
  editor: "正文",
  beats: "分镜",
  teleprompter: "提词器",
  shots: "截图包",
};

export interface DraftDraft {
  title: string;
  body: string;
}

/** 分镜 = 非空行，一行一格。引擎交回来的 `script_markdown` 与手写稿都是这个形状。 */
export function beatsOf(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line !== "");
}

/** 提词器 = 按句断（不是按行）：录口播时眼睛跟的是一句一提，不是一段。
 *  断句用 `。！？；` 与换行；逗号不断（那是气口，不是切点）。
 *  用 match 而不是 split：split 会把句号本身吃掉，"快！"到了提词器上变成"快。" ——
 *  那是把稿子的语气改了，而这一格存在的意义就是照着念。 */
export function promptLinesOf(text: string): string[] {
  return (text.match(/[^。！？；\n]+[。！？；]?/g) ?? [])
    .map((line) => line.trim())
    .filter((line) => line !== "")
    .map((line) => (/[。！？；]$/.test(line) ? line : `${line}。`));
}

/** 该不该发这一次保存。**空白不发**：后端 `assert_non_blank_text` 会 422，
 *  而一个每 1.6 秒必然失败一次的自动保存，最后会被用户当成"这页坏了"。
 *  标题与正文都要有 —— 只有标题的稿子存进去，列表上就是一条没有内容的草稿。 */
export function shouldAutosave(draft: DraftDraft): boolean {
  return draft.title.trim() !== "" && draft.body.trim() !== "";
}

/** 脏不脏：与**上一次落库成功的那一份**比，不是与上一次输入比。
 *  写成"输入变过就算脏"的话，保存失败之后界面会立刻显示"已保存"（因为输入没再变），
 *  而那句话是假的。 */
export function isDirty(current: DraftDraft, saved: DraftDraft | null): boolean {
  if (saved === null) return shouldAutosave(current);
  return current.title !== saved.title || current.body !== saved.body;
}

/** 引擎里那四张真换表的分镜模板（`core/analysis/draft_engine.py` 的键）。
 *  这份名单必须与后端的 `template_key` 枚举同源 —— 由 `schema.spec.ts` 逐字核。 */
export const BEAT_TEMPLATE_KEYS = [
  "tutorial_save_loop",
  "judgment_first",
  "short_fast",
  "tool_demo",
] as const;
export type DraftTemplateKey = (typeof BEAT_TEMPLATE_KEYS)[number];

/** 一次最多截几帧。**与后端 `api/v1/shots.py::MAX_SHOTS_PER_REQUEST` 同一份**，
 * 由 `workshop.spec.ts` 直接读那个 Python 源文件逐字核（漂了会红）。
 *
 * 为什么前端也要有这一份：超了后端回 422，而那一次点击就白点了 ——
 * 20 格分镜的稿子在这里就该只发 12 个时间点，界面上同时说清"只截其中 12 格"。 */
export const MAX_SHOT_FRAMES = 12;

/** 分镜行 → 要截的时间点（秒）。
 *
 * 三条规则，每条都对应一种坏法：
 *
 * 1. **没有可信时长就不给点**（返回空数组）：均分要拿时长做分母，
 *    瞎猜一个（30 秒？）会得到一套"看着像、其实全挤在前几秒"的帧。
 *    空数组交给界面去说"这条作品没有时长元数据"，而不是发一个后端会 422 的请求。
 * 2. **每格取该格在成片里的起始秒**，不是中点、不是末点：一格分镜说的是"从这一秒起
 *    观众看到什么"；取中点会让人以为图与格子错了一位，取末点那一瞬 ffmpeg 退出 0 却不产出。
 * 3. **限量**：分镜比上限多时等距跨步取样，而不是把 20 个点全发出去撞 422 ——
 *    取前 12 格会把截图包变成"只看了片子开头"。
 */
export function shotTimesOf(text: string, duration: number | null | undefined): number[] {
  if (typeof duration !== "number" || !Number.isFinite(duration) || duration <= 0) return [];
  const beats = beatsOf(text).length;
  const count = Math.max(1, Math.min(beats, MAX_SHOT_FRAMES));
  // `cells` 与 `count` 分开：**正文是空的**时候 `beats` 是 0，直接拿它当分母
  // 会得到 `0 / 0 = NaN` —— 那不是"第 0 秒那一帧"，那是一个 NaN 进 HTTP 请求体。
  const cells = Math.max(beats, 1);
  const step = cells / count;
  return Array.from({ length: count }, (_, index) => {
    const beatIndex = Math.floor(index * step);
    return Math.round(((duration * beatIndex) / cells) * 1000) / 1000;
  });
}

/** 这条作品能不能截帧：**没有落地成片就一个请求都不发**。
 *
 * 判据只看 `media_path`（库里那一列指的是**成片**，截图是它的派生物）。
 * 后端对空 `media_path` 回的是 404 + 一句原文，前端照发不误的话，
 * 用户看到的是"点了按钮转一圈然后报错"，而这件事在点之前就能说出来。 */
export function canRequestShots(video: { media_path?: string | null } | null | undefined): boolean {
  return typeof video?.media_path === "string" && video.media_path.trim() !== "";
}
