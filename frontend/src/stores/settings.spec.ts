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
