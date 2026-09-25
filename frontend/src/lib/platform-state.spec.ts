import { describe, expect, it } from "vitest";

import { platformStateOf } from "./platform-state";

import type { PlatformSummary } from "@/api/hooks/useConfig";

/** 一行平台清单。默认"开着且可用"，各用例只改自己要的那一格。 */
function row(over: Partial<PlatformSummary> = {}): PlatformSummary {
  return {
    name: "douyin",
    display_name: "抖音",
    enabled: true,
    availability: "available",
    implemented: true,
    health_status: null,
    health_checked_at: null,
    health_detail: null,
    ...over,
  } as PlatformSummary;
}

const STATES = ["available", "own_off", "master_off", "absent"] as const;

describe("platformStateOf", () => {
  it("四种态各给一句不一样的话", () => {
    // 断的是**互不相同**，不是四串字面量：这一整块改动的全部意义就是
    // "你自己关的"与"被总闸盖住"在界面上要分得开。写成四串文案的话，
    // 把两句合成一句也能绿（而那正是要防的那次回退）。
    const views = STATES.map((state) => platformStateOf(row({ availability: state })));
    const labels = views.map((v) => v.label);
    expect(new Set(labels).size).toBe(STATES.length);

    // 而且**都非空**：少一个态就返回 undefined，上面那句会当场炸在 `v.label` 上，
    // 所以这一条不是多余的。
    expect(labels.every((label) => label.length > 0)).toBe(true);
  });

  it("被总闸盖住的那一句不说「已关闭」", () => {
    // 这一家自己是开着的。此时说"已关闭"会把人支去 config/platforms.yaml，
    // 而那里写着 enabled: true —— 那句谎是 ADR-0025 的起点。
    const view = platformStateOf(row({ availability: "master_off" }));
    expect(view.label).not.toMatch(/已关闭/);
    expect(view.hint ?? "").toMatch(/它自己那一位是开着的/);
  });

  it("两道闸都拉着时说两句", () => {
    // 只说"开总闸"会让人开完再吃一次同样的红。
    const view = platformStateOf(row({ availability: "master_off", enabled: false }));
    expect(view.hint ?? "").toMatch(/总闸.*自己/s);
    expect(view.hint ?? "").toMatch(/还要再开这一家/);
  });

  it("开着但本构建没实现 ≠ 已启用", () => {
    // 翻开关治不好"这个版本没移植"，所以必须分开：这是 Dashboard 原来就有的判断，
    // 收进这里之后设置页也一并拿到。
    expect(platformStateOf(row({ implemented: true })).label).not.toBe(
      platformStateOf(row({ implemented: false })).label,
    );
    expect(platformStateOf(row({ implemented: false })).label).toMatch(/没实现/);
  });

  it("颜色跟着语义走：能用的那一个才是绿的", () => {
    const tones = Object.fromEntries(
      STATES.map((state) => [state, platformStateOf(row({ availability: state })).tone]),
    );
    expect(tones.available).toBe("bg-mint-green");
    expect(tones.own_off).toBe("bg-grey-mist");
    expect(tones.master_off).toBe("bg-lemon-yellow");
    expect(tones.absent).toBe("bg-coral-red");
  });
});
