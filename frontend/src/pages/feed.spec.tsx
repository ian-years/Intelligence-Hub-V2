// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Feed } from "@/pages/Feed";
import { makePage, makeVideo } from "@/test/fixtures";

const platformsBody = {
  platforms: [
    { name: "douyin", display_name: "抖音", enabled: true, implemented: true },
    { name: "bilibili", display_name: "B站", enabled: true, implemented: true },
  ],
};

const twoVideos = makePage([
  makeVideo({ id: 11, title: "第一条", platform: "douyin" }),
  makeVideo({ id: 12, title: "第二条", platform: "bilibili" }),
]);

const calls: string[] = [];
const patches: { url: string; body: string }[] = [];
/** 扣住不发出去的响应：用来量"下一批数据还没到"的那一段时间。 */
const held: { resolve: (response: Response) => void }[] = [];

function response(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stub(
  handle: (url: string, method: string) => [number, unknown] | "hold" = () => [200, twoVideos],
): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      const method = String(init?.method ?? "GET");
      calls.push(`${method} ${url}`);
      if (method === "PATCH") {
        patches.push({ url, body: String(init?.body ?? "") });
        return response(200, makeVideo({ id: 11, is_hidden: true, hidden_reason: "手动" }));
      }
      const out = handle(url, method);
      if (out === "hold") return new Promise<Response>((resolve) => held.push({ resolve }));
      return response(out[0], out[1]);
    }),
  );
}

/** 137 条 / 每页 50 → 3 页：分页那几条用例要的是"能翻页"，得先有第二页。 */
const manyTotal = makePage(twoVideos.items, { total: 137, size: 50 });

/** 只换"作品那一页给什么"，别的端点照常。
 *  每个端点都得有回法：`/api/creators` 漏掉的话它会拿到作品那一页的对象，
 *  然后 `(data ?? []).map` 在渲染期抛错 —— 症状是"整页什么都不画"，根因在别处。 */
function handleWith(pageBody: unknown, videosStatus = 200): (url: string) => [number, unknown] {
  return (url) => {
    if (url.includes("/platforms")) return [200, platformsBody];
    if (url.includes("/creators")) return [200, []];
    if (url.includes("/videos")) return [videosStatus, pageBody];
    return [404, { detail: `没有这个端点：${url}` }];
  };
}

const defaultHandle = handleWith(manyTotal);

function renderPage(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: 0 } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Feed />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  calls.length = 0;
  patches.length = 0;
  held.length = 0;
  stub(defaultHandle);
});

afterEach(() => {
  cleanup();
});

describe("Feed 的列表与计数", () => {
  it("默认只请求未隐藏的那一档", async () => {
    renderPage();
    await screen.findByText("第一条");
    expect(calls.find((call) => call.includes("/api/videos"))).toBe(
      "GET /api/videos?hidden=visible&page=1&size=50",
    );
  });

  it("计数那一行说的是 total，不是这一页的行数", async () => {
    renderPage();
    await screen.findByText(/共 137 条/);
    expect(screen.getByText(/第 1 \/ 3 页/)).toBeTruthy();
    expect(screen.getAllByRole("article")).toHaveLength(twoVideos.items.length);
  });

  it("虚拟滚动：60 条数据不会画出 60 个 DOM 节点", async () => {
    const many = makePage(
      Array.from({ length: 60 }, (_, index) =>
        makeVideo({ id: index + 1, title: `作品 ${String(index + 1)}` }),
      ),
      { total: 60 },
    );
    stub(handleWith(many));
    renderPage();
    await screen.findByText("作品 1");
    const rows = screen.getAllByRole("article");
    expect(rows.length).toBeLessThan(many.items.length);
    // 但第一行必须在：只画视口内 ≠ 什么都不画
    expect(rows[0]?.textContent).toContain("作品 1");
  });

  it("库里真的没有：说「没有内容」而不是错误卡", async () => {
    stub(handleWith(makePage([])));
    renderPage();
    await screen.findByText(/这个筛选条件下没有任何作品/);
    expect(screen.queryByText(/读不到作品流/)).toBeNull();
  });

  it("读不到：给原因，而且筛选栏照常可用（改了条件还要能再查）", async () => {
    stub(handleWith({ detail: "后端 500：主库锁住" }, 500));
    renderPage();
    await screen.findByText("后端 500：主库锁住");
    expect(screen.getByRole("button", { name: "查询" })).toBeEnabled();
  });
});

describe("筛选与分页", () => {
  it("打字不发请求，点「查询」才发：LIKE 全库不是按键的代价", async () => {
    renderPage();
    await screen.findByText("第一条");
    const before = calls.filter((call) => call.includes("/api/videos")).length;

    // `type="search"` 算出来的 ARIA 角色是 searchbox，不是 textbox
    await userEvent.type(screen.getByRole("searchbox", { name: /标题关键词/ }), "抖音");
    expect(calls.filter((call) => call.includes("/api/videos")).length).toBe(before);

    await userEvent.click(screen.getByRole("button", { name: "查询" }));
    await waitFor(() =>
      expect(calls.some((call) => call.includes("search=%E6%8A%96%E9%9F%B3"))).toBe(true),
    );
  });

  it("平台下拉的选项来自 /api/platforms，不是写死四家", async () => {
    renderPage();
    await screen.findByText("第一条");
    const select = screen.getByRole("combobox", { name: /平台/ });
    expect([...select.querySelectorAll("option")].map((node) => node.textContent)).toEqual([
      "全部平台",
      ...platformsBody.platforms.map((platform) => platform.display_name),
    ]);
  });

  it("换筛选条件时退回第 1 页，不会停在「第 4 页但新条件下只有 1 页」", async () => {
    renderPage();
    await screen.findByText("第一条");
    await userEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(calls.some((call) => call.includes("page=2"))).toBe(true));
    expect(screen.getByText(/第 2 \/ 3 页/)).toBeTruthy();

    await userEvent.selectOptions(screen.getByRole("combobox", { name: /可见性/ }), "hidden");
    await userEvent.click(screen.getByRole("button", { name: "查询" }));
    await waitFor(() =>
      expect(calls.some((call) => call.includes("hidden=hidden") && call.includes("page=1"))).toBe(
        true,
      ),
    );
  });

  it("下一页的响应还没回来时，明说屏幕上的是上一页的结果", async () => {
    renderPage();
    await screen.findByText("第一条");
    stub((url) => (url.includes("/videos") ? "hold" : handleWith(manyTotal)(url)));
    calls.length = 0;
    await userEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => expect(calls.some((call) => call.includes("page=2"))).toBe(true));
    expect(screen.getByText(/显示的是上一页的结果/)).toBeTruthy();
    for (const pending of held.splice(0)) pending.resolve(response(200, twoVideos));
  });
});

describe("墓碑操作", () => {
  it("点隐藏：PATCH 这一条的 hide，并带上原因", async () => {
    renderPage();
    await screen.findByText("第一条");
    await userEvent.click(screen.getByRole("button", { name: /^隐藏《第一条》$/ }));
    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0]?.url).toBe("/api/videos/11/hide");
    expect(JSON.parse(patches[0]?.body ?? "{}")).toEqual({
      reason: "用户在作品流里手动隐藏",
    });
  });

  it("在「只看已隐藏」那一档，行给的是取消隐藏，点在 unhide 上", async () => {
    stub(
      handleWith(
        makePage([
          makeVideo({ id: 11, title: "旧的", is_hidden: true, hidden_reason: "重复投稿" }),
        ]),
      ),
    );
    renderPage();
    await screen.findByText("旧的");
    await userEvent.selectOptions(screen.getByRole("combobox", { name: /可见性/ }), "hidden");
    await userEvent.click(screen.getByRole("button", { name: "查询" }));
    await waitFor(() => expect(calls.some((call) => call.includes("hidden=hidden"))).toBe(true));

    expect(screen.getByText(/已隐藏：重复投稿/)).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: /^取消隐藏《旧的》$/ }));
    await waitFor(() => expect(patches.at(-1)?.url).toBe("/api/videos/11/unhide"));
  });
});
