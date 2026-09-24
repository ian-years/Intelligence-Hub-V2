import { afterEach, describe, expect, it, vi } from "vitest";

import {
  FALLBACK_THEME,
  RESOLVED_THEMES,
  THEME_MODES,
  applyTheme,
  resolveTheme,
  systemPrefersDark,
  themeFromStored,
} from "./theme";

afterEach(() => {
  delete document.documentElement.dataset.theme;
  vi.unstubAllGlobals();
});

describe("themeFromStored（localStorage 是外部输入）", () => {
  it("认识三档", () => {
    for (const mode of THEME_MODES) {
      expect(themeFromStored(mode)).toBe(mode);
    }
  });

  it("认不出来一律 null，不猜、不返回原值", () => {
    // 大小写、空格、别的版本写过的枚举、`null`/`undefined`/数字：全部不认。
    // 认一半的话，`{themeMode: "Dark"}` 会变成"写进 data-theme 一个 CSS 不认的值"，
    // 症状是"切了没反应"而没有任何一处报错。
    for (const raw of ["Dark", "dark ", "auto", "LIGHT", "", null, undefined, 0, 1, {}, []]) {
      expect(themeFromStored(raw), String(raw)).toBeNull();
    }
  });
});

describe("resolveTheme（CSS 只认两种）", () => {
  it("light/dark 原样，system 由系统偏好解成具体的那一档", () => {
    expect(resolveTheme("light", true)).toBe("light");
    expect(resolveTheme("dark", false)).toBe("dark");
    expect(resolveTheme("system", true)).toBe("dark");
    expect(resolveTheme("system", false)).toBe(FALLBACK_THEME);
  });

  it("关系：任何一档、任何系统偏好，解析结果都在 CSS 认的那两份里", () => {
    // 这条是"`data-theme` 写成一个 CSS 不认的值"的总闸：
    // 上面那些样本各钉一个点，这一条钉的是"没有第三种输出"。
    for (const mode of THEME_MODES) {
      for (const prefers of [true, false]) {
        expect(RESOLVED_THEMES).toContain(resolveTheme(mode, prefers));
      }
    }
  });
});

describe("systemPrefersDark", () => {
  it("matchMedia 不在场时报 false（落点与 FALLBACK_THEME 同值，不是猜亮色）", () => {
    vi.stubGlobal("matchMedia", undefined);
    expect(systemPrefersDark()).toBe(false);
  });

  it("跟着 matches 走，不看 media 字符串以外的东西", () => {
    const matchMedia = vi.fn(() => ({ matches: true }));
    vi.stubGlobal("matchMedia", matchMedia);
    expect(systemPrefersDark()).toBe(true);
    expect(matchMedia).toHaveBeenCalledWith("(prefers-color-scheme: dark)");
  });
});

describe("applyTheme", () => {
  it("写的是 <html> 上的 data-theme，且**写的是解析后的那一档**", () => {
    // `system` 绝不能出现在 DOM 上：CSS 里没有"跟随"这种状态，
    // 写了就等于"选跟随 → 界面回落到未调过的那一版"。
    expect(applyTheme("system", true)).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");

    expect(applyTheme("system", false)).toBe("light");
    expect(document.documentElement.dataset.theme).toBe("light");

    expect(applyTheme("dark", true)).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
  });
});
