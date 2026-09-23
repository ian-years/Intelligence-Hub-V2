// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "@/App";
import { NAV_ITEMS } from "@/lib/nav";

/**
 * `nav.ts` 的 `built` 旗标与 `App.tsx` 的路由表是**两处**写"这一页做没做"的地方。
 * 漂了的症状很难看：侧栏不再标"待建"而页面还在说"还没开工"
 * （或者反过来：旗标还说没做，页面已经能用了 —— 那等于把做好的页面对藏起来）。
 *
 * 所以逐条核：每一条导航项渲染一次，`built` 为假的必须能问到"还没开工"，
 * 为真的必须问不到。
 */
function renderAt(path: string): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: 0 } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request) => {
      const url = String(input);
      if (url.includes("/platforms")) {
        return new Response(JSON.stringify({ platforms: [] }), { status: 200 });
      }
      return new Response(JSON.stringify({ detail: "这一条用例不关心数据" }), { status: 500 });
    }),
  );
});

describe("导航旗标与路由表同源", () => {
  it("夹具同时覆盖两种旗标（否则下面的循环可以空转）", () => {
    expect(NAV_ITEMS.some((item) => item.built)).toBe(true);
    expect(NAV_ITEMS.some((item) => !item.built)).toBe(true);
  });

  for (const item of NAV_ITEMS) {
    it(`${item.to}：built=${String(item.built)} 与页面实际画出来的一致`, () => {
      renderAt(item.to);
      const notBuilt = screen.queryByText(/这一页还没开工/);
      if (item.built) {
        expect(notBuilt, `${item.to} 的旗标说做了，页面还在说没开工`).toBeNull();
      } else {
        expect(notBuilt, `${item.to} 已经能用，但侧栏还挂着"待建"`).not.toBeNull();
      }
    });
  }
});
