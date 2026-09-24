import { describe, expect, it } from "vitest";

import { clockLabel, mediaUrl, parseSegments } from "./media";

describe("mediaUrl", () => {
  it("只给 API 地址，一个磁盘路径的片段都不带", () => {
    // 关系判据，不是"等于那个字符串"：这一条要挡的是"前端自己去拼 data/ 下那棵树"。
    // 库里 `media_path` 长这样（`media/bilibili/某UP/BV1x/video.mp4`），
    // 只要它的一个片段出现在 URL 里，开发模式（:5173 与 :8789 不同源）下就是 404。
    const url = mediaUrl(7);
    expect(url).toBe("/api/videos/7/media");
    expect(url).not.toContain("media/");
    expect(url).not.toContain(".mp4");
    expect(url.startsWith("/api/")).toBe(true);
  });
});

describe("clockLabel", () => {
  it.each([
    [0, "0:00"],
    [8, "0:08"],
    [59, "0:59"],
    [60, "1:00"],
    [65, "1:05"],
    [600, "10:00"],
    [3599, "59:59"],
    [3600, "1:00:00"],
    [3661, "1:01:01"],
    [-5, "0:00"],
    [8.9, "0:08"],
  ] as const)("把 %i 秒写成 %s", (seconds, expected) => {
    expect(clockLabel(seconds)).toBe(expected);
  });

  it("读回去等于原秒数（时/分/秒的换算不许自己跟自己对不上）", () => {
    // 上面的样本表钉的是"人熟悉的几个点"；这一条钉的是换算本身：
    // 任何一段样本，把标签拆回 h/m/s 再算成秒，必须等于 floor(输入)。
    // 只对 3600 边界做样本的话，"小时位被忽略"这类错能一直活到长视频出现。
    for (const seconds of [0, 1, 59, 60, 3599, 3600, 3661, 7325, 86399, 123456, 0.7]) {
      const parts = clockLabel(seconds).split(":").map(Number);
      const back = parts.reduce((total, part) => total * 60 + part, 0);
      expect(back, `${String(seconds)} → ${clockLabel(seconds)}`).toBe(Math.floor(seconds));
    }
  });
});

describe("parseSegments", () => {
  it("没有、空串、读不出 JSON、顶层不是数组 → 一律空数组", () => {
    for (const raw of [null, undefined, "", "   ", "{not json", "[1,2", '{"a":1}']) {
      expect(parseSegments(raw), String(raw)).toEqual([]);
    }
  });

  it("一份能用的时间轴：三个字段各归其位", () => {
    const raw =
      '[{"start_seconds":0,"end_seconds":7.4,"text":"第一句。"},' +
      '{"start_seconds":8,"end_seconds":21,"text":"第二句。"}]';
    expect(parseSegments(raw)).toEqual([
      { start_seconds: 0, end_seconds: 7.4, text: "第一句。" },
      { start_seconds: 8, end_seconds: 21, text: "第二句。" },
    ]);
  });

  it("任何一条读不出来就整份不用（半截时间轴比没有更坏）", () => {
    // 判据的来源：点了不动的按钮会被读成"播放器坏了"，而真原因是数据坏了。
    // 所以这里逐条把"只有第 N 条坏"的样本都试一遍 —— 只测第 1 条坏的话，
    // 一个"跳过坏条目"的实现照样绿。
    const good = '{"start_seconds":1,"end_seconds":2,"text":"句。"}';
    const brokenCases = [
      "[{}]",
      `[${good},{"text":"缺起点"}]`,
      `[{"start_seconds":"1","end_seconds":2,"text":"起点是字符串"}]`,
      `[{"start_seconds":1,"end_seconds":2}]`,
      "[null]",
      '["一句字符串"]',
      "[[1,2]]",
    ];
    for (const raw of brokenCases) {
      expect(parseSegments(raw), raw).toEqual([]);
    }
  });

  it("缺 end_seconds 不算坏（正文与起点才是跳转要用的）", () => {
    expect(parseSegments('[{"start_seconds":5,"text":"只有起点。"}]')).toEqual([
      { start_seconds: 5, end_seconds: 0, text: "只有起点。" },
    ]);
  });

  it("空数组是合法数据，不是读不出", () => {
    expect(parseSegments("[]")).toEqual([]);
  });
});
