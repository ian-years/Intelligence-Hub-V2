import { describe, expect, it } from "vitest";

import {
  AUTOSAVE_DELAY_MS,
  BEAT_TEMPLATE_KEYS,
  beatsOf,
  isDirty,
  promptLinesOf,
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
