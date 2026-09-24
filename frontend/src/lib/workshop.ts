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
