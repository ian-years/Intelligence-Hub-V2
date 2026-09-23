import { cleanup } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";

/** vitest 配置里没开 `globals`，所以 Testing Library 的自动 cleanup 不会挂上 ——
 * 不手动清的话同一个文件里后一条用例会看见前一条留下的 DOM，
 * 症状是"文案莫名其妙也在"（比如断言"读不到时不该出现全绿"却被打中）。 */
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

/**
 * jsdom 没有 `ResizeObserver`，而 TanStack Virtual 在**量到滚动容器尺寸之前**
 * 一条都不画（`getVirtualItems()` 是空数组）。所以没有这个桩的时候，
 * 虚拟列表的用例稳定地"什么都不渲染"，症状看起来像组件坏了而不是环境缺东西。
 *
 * `observe()` 里同步回调一次，给一个够装下几行的假尺寸：这样
 * "只画视口内 + overscan"才是可断言的 —— 用例能看到"60 条只画了十几行"，
 * 而不是"0 行"。
 */
class ResizeObserverStub implements ResizeObserver {
  constructor(private readonly report: ResizeObserverCallback) {}

  observe(target: Element): void {
    this.report([entryFor(target)], this);
  }

  unobserve(): void {}

  disconnect(): void {}
}

function entryFor(target: Element): ResizeObserverEntry {
  const box = { blockSize: 600, inlineSize: 1000 };
  return {
    target,
    contentRect: { height: 600, width: 1000, top: 0, left: 0 },
    borderBoxSize: [box],
    contentBoxSize: [box],
    devicePixelContentBoxSize: [box],
  } as unknown as ResizeObserverEntry;
}

// 用 `??=`：真实浏览器环境里已经有实现，别把它盖掉。
globalThis.ResizeObserver ??= ResizeObserverStub;

globalThis.ResizeObserver ??= ResizeObserverStub;
