// @vitest-environment node
// 这一份只测纯函数，并且其中一条要**读仓库里的 Python 源文件**核对常量同源 ——
// 读文件要 `file:` 的 import.meta.url，jsdom 环境下拿到的是 http 形状（同 schema.spec.ts）。
import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import {
  AUTOSAVE_DELAY_MS,
  BEAT_TEMPLATE_KEYS,
  beatsOf,
  canRequestShots,
  isDirty,
  MAX_SHOT_FRAMES,
  promptLinesOf,
  shotTimesOf,
  shouldAutosave,
  VIEW_LABELS,
  WORKSHOP_VIEWS,
} from "./workshop";

describe("beatsOf（一格 = 一个非空行）", () => {
  it("空行与只含空白的行都不算一格", () => {
    expect(beatsOf("第一行\n\n   \n第二行\tafter-tab\n")).toEqual(["第一行", "第二行\tafter-tab"]);
  });

  it("空文本 → 零格（不是 ['']，那会画出一格空的编号）", () => {
    expect(beatsOf("")).toEqual([]);
    expect(beatsOf("\n\n")).toEqual([]);
  });
});

describe("promptLinesOf（提词器按句断，不按行）", () => {
  it("句号/问号/叹号/分号/换行都是切点，逗号不是", () => {
    expect(promptLinesOf("你看这个，很好用。它快吗？快！真的。")).toEqual([
      "你看这个，很好用。",
      "它快吗？",
      "快！",
      "真的。",
    ]);
  });

  it("句末没有标点时补一个句号（§7.9 那个口径在提词器这一头同样成立）", () => {
    expect(promptLinesOf("收尾一句没有标点")).toEqual(["收尾一句没有标点。"]);
  });

  it("行数关系：切出来的每一句都以标点收尾", () => {
    for (const line of promptLinesOf("a。b！\nc")) {
      expect(/[。！？；]$/.test(line)).toBe(true);
    }
  });
});

describe("shouldAutosave / isDirty", () => {
  it("标题或正文缺任一个都不发（后端对空白 422）", () => {
    expect(shouldAutosave({ title: "选题", body: "正文" })).toBe(true);
    expect(shouldAutosave({ title: "   ", body: "正文" })).toBe(false);
    expect(shouldAutosave({ title: "选题", body: "" })).toBe(false);
  });

  it("还没落库过时：只有内容齐了才算脏", () => {
    expect(isDirty({ title: "选题", body: "正文" }, null)).toBe(true);
    expect(isDirty({ title: "选题", body: "" }, null)).toBe(false);
  });

  it("脏 = 与**上一次落库成功的那一份**不等（不是与上一次输入不等）", () => {
    const saved = { title: "选题", body: "正文" };
    expect(isDirty(saved, saved)).toBe(false);
    expect(isDirty({ ...saved, body: "正文，改了" }, saved)).toBe(true);
    // 保存失败之后：输入没再变，但那份内容**从来没落库成功**，
    // 若按"输入变过就算脏"写，界面会立刻显示"已保存" —— 那句是假的。
    expect(isDirty(saved, null)).toBe(true);
  });
});

describe("视图名单", () => {
  it("四视图各有一句中文标签，且没有第五个没标签的视图", () => {
    expect([...WORKSHOP_VIEWS].sort()).toEqual(["beats", "editor", "shots", "teleprompter"].sort());
    for (const view of WORKSHOP_VIEWS) {
      expect(VIEW_LABELS[view], view).toBeTruthy();
    }
  });

  it("自动保存的节奏还是 V1 那个 1.6 秒（改了要写在计划里，不是随手调）", () => {
    expect(AUTOSAVE_DELAY_MS).toBe(1600);
  });

  it("分镜表那四张键非空且唯一（与契约的逐字相等由 schema.spec 钉）", () => {
    expect(new Set(BEAT_TEMPLATE_KEYS).size).toBe(BEAT_TEMPLATE_KEYS.length);
    expect(BEAT_TEMPLATE_KEYS.length).toBeGreaterThan(0);
  });
});

describe("shotTimesOf（分镜行 → 截帧时间点）", () => {
  /** 20 格 / 100 秒那一份"什么都能坏"的样本：递增、限量、跨步三件事同时可验。 */
  const TWENTY_BEATS = Array.from({ length: 20 }, (_, index) => `第${String(index + 1)}格`).join(
    "\n",
  );

  it("每个时间点都在片内、严格递增、且各自一份", () => {
    const times = shotTimesOf("一\n二\n三\n四", 40);
    expect(times).toHaveLength(4);
    expect([...times].sort((a, b) => a - b)).toEqual(times);
    expect(new Set(times).size).toBe(times.length);
    for (const time of times) {
      expect(time).toBeGreaterThanOrEqual(0);
      // 取末点那一瞬 ffmpeg 退出 0 却不产出：那不是一个失败，是一次空手而归
      expect(time).toBeLessThan(40);
    }
  });

  it("帧数 == min(分镜格数, 上限)，且多出来的分镜是**跨步**取样而不是只看开头", () => {
    const times = shotTimesOf(TWENTY_BEATS, 100);
    expect(times).toHaveLength(Math.min(20, MAX_SHOT_FRAMES));
    // 前 12 格会把包挤在片子的前 60%：最后一张必须落在后半段
    expect(times[times.length - 1]).toBeGreaterThan(50);
    expect(times[0]).toBe(0);
  });

  it("空正文但有时长 → 恰好一帧（第 0 秒），而不是零帧或一帧负数", () => {
    expect(shotTimesOf("", 30)).toEqual([0]);
    expect(shotTimesOf("   \n\n", 30)).toEqual([0]);
  });

  it("时长不可用（缺失 / 0 / 负 / NaN / Infinity）时一个时间点都不给", () => {
    for (const duration of [null, undefined, 0, -5, Number.NaN, Number.POSITIVE_INFINITY]) {
      expect(shotTimesOf("一\n二", duration), String(duration)).toEqual([]);
    }
  });

  it("浮点时长不会把点甩到片尾之外（0.1+0.2 那一族）", () => {
    for (const duration of [0.3, 7.777, 3600.5, 1e6]) {
      const times = shotTimesOf("一\n二\n三", duration);
      expect(times.length).toBeGreaterThan(0);
      for (const time of times) {
        expect(time).toBeLessThan(duration);
        expect(Number.isFinite(time)).toBe(true);
      }
    }
  });

  it("上限与后端那个常量同源（读的是 Python 源文件，漂了就红）", () => {
    const source = readFileSync(
      new URL("../../../src/intelligence_hub_v2/api/v1/shots.py", import.meta.url),
      "utf8",
    );
    const backend = /MAX_SHOTS_PER_REQUEST: Final = (\d+)/.exec(source)?.[1];
    expect(backend, "后端那个常量改名或换写法了：这条用例要跟着一起跑").toBeDefined();
    expect(MAX_SHOT_FRAMES).toBe(Number(backend));
  });
});

describe("canRequestShots（没有成片就不发请求）", () => {
  it("media_path 缺失 / null / 空白 全都算不能截", () => {
    for (const video of [
      null,
      undefined,
      {},
      { media_path: null },
      { media_path: "" },
      { media_path: "   " },
    ]) {
      expect(canRequestShots(video), JSON.stringify(video)).toBe(false);
    }
  });

  it("有值就算能截，且判据**只看** media_path（时长缺失是另一件事，由 shotTimesOf 说）", () => {
    // 整条 `Video` 传进来是常态（列表里拿到的那一份），所以这里不能只喂一个窄对象
    const fullRow = { media_path: "media/x.mp4", duration_seconds: null, id: 7 };
    expect(canRequestShots({ media_path: "media/bilibili/某UP/BV1-t/media.mp4" })).toBe(true);
    expect(canRequestShots(fullRow)).toBe(true);
  });
});
