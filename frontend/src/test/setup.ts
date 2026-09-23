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
