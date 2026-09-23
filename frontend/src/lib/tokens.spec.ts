import { describe, expect, it } from "vitest";

import { cubicBezier, DURATION, EASING, durationSeconds, token } from "./tokens";

/**
 * JS 侧读令牌的这一层是"动效值只有一处"的关键：framer-motion 拿的是 JS 对象，
 * 最容易在这种地方长出第二份 `0.15`。所以这里既核值、也核"读不到时会炸"。
 */
describe("lib/tokens", () => {
  it("读到的就是 tokens.css 里那一串（缓动要解析成四个数）", () => {
    expect(EASING.bounce).toEqual([0.34, 1.56, 0.64, 1]);
    expect(EASING.snap).toEqual([0.4, 0, 0.2, 1]);
    expect(DURATION.fast).toBe(0.15);
    expect(DURATION.instant).toBe(0.1);
    expect(DURATION.slow).toBe(0.4);
  });

  it("缓动令牌看不懂就抛", () => {
    expect(() => cubicBezier("--radius-none")).toThrow(/看不懂/);
  });

  it("令牌缺失时抛，而不是让动效静默变成没有", () => {
    expect(() => token("--不存在的令牌")).toThrow(/tokens.json 里没有字符串令牌/);
  });

  it("时长令牌不带单位就抛，而不是算出一个巨大的时长", () => {
    // `--radius-pill` 是 `9999px`：不校验单位的那版实现会安静地给出 9990 秒。
    expect(() => durationSeconds("--radius-pill")).toThrow(/看不懂/);
    expect(() => durationSeconds("--不存在的令牌")).toThrow(/没有字符串令牌/);
  });
});
