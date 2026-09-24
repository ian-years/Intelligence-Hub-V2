import { act } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { captureTrackingDefault, useSettings } from "./settings";

const KEY = "ih.settings.v1";

function primePersisted(state: unknown): void {
  localStorage.setItem(KEY, JSON.stringify({ state, version: 0 }));
}

/** 只做"从 localStorage 重新灌一次"。
 *
 * 不要在前面 `resetToInitial()`：persist 中间件会把 reset **也写回** localStorage，
 * 于是刚 prime 好的脏值被清成 `true`，测试就变成了"永远看到 true 所以永远绿"的假证据
 * （第一版就是这么过的 —— 那条断言当时并没有在测任何东西）。 */
async function rehydrate(): Promise<void> {
  await act(async () => {
    await useSettings.persist.rehydrate();
  });
}

beforeEach(() => {
  localStorage.clear();
  useSettings.getState().resetToInitial();
});

describe("settings store（V1 §7.24：跟踪默认值只能有一处）", () => {
  it("唯一默认值就是 store 里那一个", () => {
    expect(captureTrackingDefault()).toBe(true);
    expect(useSettings.getState().captureTrackingDefault).toBe(true);
  });

  it("关掉之后落盘，重开（rehydrate）读回 false", async () => {
    act(() => useSettings.getState().setCaptureTrackingDefault(false));
    expect(localStorage.getItem(KEY)).toContain('"captureTrackingDefault":false');
    // 先把内存擦回默认值，模拟"页面刷新后 store 是初值、只有 localStorage 带着旧选择"。
    // 用 persist.clear() 之后手写 storage：直接 setState 会被中间件写回去。
    const saved = localStorage.getItem(KEY);
    act(() => useSettings.setState({ captureTrackingDefault: true }));
    if (saved !== null) localStorage.setItem(KEY, saved);
    await rehydrate();
    expect(captureTrackingDefault()).toBe(false);
  });

  /** `"false"` 在 JS 里是 truthy。不做这层守卫的话，症状恰好是 §7.24 那一类：
   * 用户以为关了，刷新之后又变成开，而且没有任何报错。 */
  it("localStorage 里是字符串布尔时不认，回到唯一默认值", async () => {
    primePersisted({ captureTrackingDefault: "false" });
    await rehydrate();
    expect(captureTrackingDefault()).toBe(true);
  });

  it("写非布尔直接拒，不让它变成后端的 422", () => {
    expect(() =>
      useSettings.getState().setCaptureTrackingDefault("true" as unknown as boolean),
    ).toThrow(TypeError);
  });
});

describe("themeMode（T5.6：DOM 上只许出现 light/dark，store 里存三档）", () => {
  it("默认是 system，不是 light", () => {
    // 首屏该跟操作系统走。默认写死 light 的话，"跟随系统"这一档在第一次访问时
    // 与"亮"完全同值，用户按了才见效 —— 那看起来像没实现。
    expect(useSettings.getState().themeMode).toBe("system");
  });

  it("三档都写得进去并落盘", () => {
    for (const mode of ["light", "dark", "system"] as const) {
      act(() => useSettings.getState().setThemeMode(mode));
      expect(useSettings.getState().themeMode).toBe(mode);
      expect(localStorage.getItem(KEY)).toContain(`"themeMode":"${mode}"`);
    }
  });

  it("写一个 CSS 认不了的档：当场抛，不静默收下", () => {
    // `"Dark"` 存进去之后的症状是"按了暗色开关没反应"，且没有任何一处会报错。
    for (const bad of ["Dark", "auto", "", null, 1]) {
      expect(
        () => useSettings.getState().setThemeMode(bad as unknown as "light"),
        String(bad),
      ).toThrow(TypeError);
    }
  });

  it("localStorage 里是认不了的 themeMode 时回到唯一默认值，不拿脏值去写 DOM", async () => {
    // 与上面 `captureTrackingDefault: "false"` 那条同一个理由：那是外部输入。
    primePersisted({ themeMode: "midnight" });
    await rehydrate();
    expect(useSettings.getState().themeMode).toBe("system");
  });
});
