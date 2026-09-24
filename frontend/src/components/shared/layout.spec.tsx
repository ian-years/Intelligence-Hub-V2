// @vitest-environment jsdom
import { QueryClient, QueryClientProvider, onlineManager } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Layout } from "./Layout";
import { NAV_ITEMS } from "@/lib/nav";
import { useSettings } from "@/stores/settings";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderLayout(retry = 0): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry } } });
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
  onlineManager.setOnline(true);
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

  /** "读取中…" 与 "被暂停" 必须分开：窗口不在前台时 react-query 挂起补发，
   * `isPending` 会一直 true —— 只写"读取中"就等于承诺"马上就出来了"。
   * （2026-09-23 在真实页面量到的正是这个状态：请求发出去了、返回了 404、
   * 然后重试被挂起，界面从此停在"读取中…"。） */
  it("补发被挂起时说「被暂停」，不说「读取中…」", async () => {
    vi.mocked(fetch).mockImplementation(async () =>
      jsonResponse(409, { detail: "配置层还没起来" }),
    );
    onlineManager.setOnline(false);
    renderLayout(3);
    expect(await screen.findByText(/被暂停/)).toBeTruthy();
    expect(screen.queryByText(/读取中/)).toBeNull();
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

/** 一个可驱动的 `matchMedia`：`set()` 改偏好并**通知订阅者**，
 *  这样"跟随系统那一档到底跟不跟"才问得出来（只在挂载时读一次的实现会红在第二条）。 */
function fakeMedia(initial: boolean): { media: MediaQueryList; set: (next: boolean) => void } {
  const listeners = new Set<(event: MediaQueryListEvent) => void>();
  let current = initial;
  const media = {
    get matches(): boolean {
      return current;
    },
    addEventListener: (_: string, cb: (event: MediaQueryListEvent) => void): void => {
      listeners.add(cb);
    },
    removeEventListener: (_: string, cb: (event: MediaQueryListEvent) => void): void => {
      listeners.delete(cb);
    },
  } as unknown as MediaQueryList;
  return {
    media,
    set: (next: boolean): void => {
      current = next;
      for (const listener of listeners) listener({ matches: next } as MediaQueryListEvent);
    },
  };
}

describe("主题（T5.6）", () => {
  beforeEach(() => {
    vi.mocked(fetch).mockImplementation(async () => jsonResponse(200, { platforms: [] }));
    act(() => useSettings.getState().resetToInitial());
    delete document.documentElement.dataset.theme;
  });

  it("light / dark 两档原样写进 <html> 的 data-theme", async () => {
    const { media } = fakeMedia(false);
    vi.stubGlobal(
      "matchMedia",
      vi.fn(() => media),
    );

    act(() => useSettings.getState().setThemeMode("dark"));
    renderLayout();
    await waitFor(() => expect(document.documentElement.dataset.theme).toBe("dark"));

    act(() => useSettings.getState().setThemeMode("light"));
    await waitFor(() => expect(document.documentElement.dataset.theme).toBe("light"));
  });

  it("system 那一档跟着系统偏好变，不用重新渲染", async () => {
    const { media, set } = fakeMedia(false);
    vi.stubGlobal(
      "matchMedia",
      vi.fn(() => media),
    );
    act(() => useSettings.getState().setThemeMode("system"));

    renderLayout();
    await waitFor(() => expect(document.documentElement.dataset.theme).toBe("light"));
    // 操作系统里切到暗色：界面当场跟着切。只读一次的话这一行会一直停在 light。
    act(() => set(true));
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it('DOM 上永远不出现 "system"：CSS 里没有这一档', async () => {
    vi.stubGlobal("matchMedia", undefined);
    act(() => useSettings.getState().setThemeMode("system"));
    renderLayout();
    await waitFor(() => expect(document.documentElement.dataset.theme).toBe("light"));
    expect(document.documentElement.dataset.theme).not.toBe("system");
  });

  it("侧栏那三个按钮按得动，按下去 DOM 与 store 一起变", async () => {
    const { media } = fakeMedia(false);
    vi.stubGlobal(
      "matchMedia",
      vi.fn(() => media),
    );
    renderLayout();

    await userEvent.click(await screen.findByRole("button", { name: "暗" }));
    await waitFor(() => expect(document.documentElement.dataset.theme).toBe("dark"));
    expect(useSettings.getState().themeMode).toBe("dark");
    expect((screen.getByRole("button", { name: "暗" }) as HTMLButtonElement).ariaPressed).toBe(
      "true",
    );
  });
});
