// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Layout } from "./Layout";
import { NAV_ITEMS } from "@/lib/nav";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderLayout(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: 0 } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Layout />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Layout / Sidebar", () => {
  it("导航条目与 lib/nav 那张表一致（加一条路由不会悄悄少一项）", async () => {
    vi.mocked(fetch).mockImplementation(async () => jsonResponse(200, { platforms: [] }));
    renderLayout();
    for (const item of NAV_ITEMS) {
      expect(await screen.findByText(item.label, { exact: false })).toBeTruthy();
    }
    expect(screen.getAllByRole("link")).toHaveLength(NAV_ITEMS.length);
  });

  it("平台名取自 /api/platforms，不是前端那份占位表", async () => {
    vi.mocked(fetch).mockImplementation(async () =>
      jsonResponse(200, {
        platforms: [
          { name: "douyin", display_name: "抖音（后端改过名）", enabled: true, implemented: true },
          { name: "bilibili", display_name: "B站", enabled: false, implemented: true },
        ],
      }),
    );
    renderLayout();
    expect(await screen.findByText("抖音（后端改过名）")).toBeTruthy();
    expect(screen.getByText("B站")).toBeTruthy();
    // 关掉的平台**不删条目**，只压暗：删掉会让人以为"这个构建没有 B站"。
    expect(screen.getByText("B站").closest(".memphis-badge")).toHaveClass("opacity-40");
  });

  /** 这条是 `AGENTS.md §1.3` 在 UI 上的落点：读不到要看得见，不能渲染成空列表。 */
  it("平台清单读不到时显示原因，而不是一个安静的空区块", async () => {
    vi.mocked(fetch).mockImplementation(async () =>
      jsonResponse(409, { detail: "配置层还没起来" }),
    );
    renderLayout();
    await waitFor(() => expect(screen.getByText(/读不到/)).toBeTruthy());
    expect(screen.getByText(/配置层还没起来/)).toBeTruthy();
  });
});
