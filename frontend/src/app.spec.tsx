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
 * 七个业务页到 Task 13 全落，所以现在每条导航都必须是**真页面**；
 * 而"这个问句问得出结果"由兜底路由那条证明（它必须还能问到"还没开工"）——
 * 否则这个循环只是在断言一件永远为真的事。
 *
 * 以后再加一条导航项而忘了接路由，它落进兜底 → 那一条红。
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
  it("兜底路由还会说「还没开工」（否则下面那批断言问不出任何东西）", () => {
    renderAt("/没有这么一条路由");
    expect(screen.queryByText(/这一页还没开工/)).not.toBeNull();
  });

  it("七条导航里没有还挂着「待建」的（挂了就是旗标与路由表漂了）", () => {
    expect(NAV_ITEMS.filter((item) => !item.built).map((item) => item.to)).toEqual([]);
  });

  for (const item of NAV_ITEMS) {
    it(`${item.to}：渲染的是真页面，不是「还没开工」那块占位`, () => {
      renderAt(item.to);
      const notBuilt = screen.queryByText(/这一页还没开工/);
      expect(
        notBuilt,
        item.built ? `${item.to} 的旗标说做了，页面还在说没开工` : `${item.to} 还没接路由`,
      ).toBeNull();
    });
  }
});
