// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Creators } from "@/pages/Creators";
import { useSettings } from "@/stores/settings";
import { makeCreator, makeVideo } from "@/test/fixtures";

const list = [
  makeCreator({ id: 3, name: "阿婆主甲", platform: "bilibili", is_tracking: true }),
  makeCreator({
    id: 4,
    name: "抖音乙",
    platform: "douyin",
    platform_id: "MS4wLjABZZZZ",
    is_tracking: false,
  }),
];

const platformsBody = {
  platforms: [
    { name: "douyin", display_name: "抖音", enabled: true, implemented: true },
    { name: "bilibili", display_name: "B站", enabled: true, implemented: true },
  ],
};

const calls: string[] = [];
const posts: { url: string; body: string }[] = [];
const patches: { url: string; body: string }[] = [];

const EMPTY_PAGE = { items: [], total: 0, page: 1, size: 5 };

/** `PagedResult[Video]` 的那一形状。`items` 按**给进来的顺序**原样交出：
 *  爆款那几条用例要的正是"面板画的顺序 == 后端给的顺序"，所以这里刻意不按赞数排。 */
function videosPage(...titles: string[]): unknown {
  return {
    items: titles.map((title, index) =>
      makeVideo({ id: index + 1, title, like_count: 10 * (index + 1) }),
    ),
    total: titles.length,
    page: 1,
    size: 5,
  };
}

function stub(
  options: { addStatus?: number; addDetail?: unknown; videosBody?: unknown } = {},
): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      const method = String(init?.method ?? "GET");
      calls.push(`${method} ${url}`);
      const json = (status: number, body: unknown): Response =>
        new Response(JSON.stringify(body), {
          status,
          headers: { "Content-Type": "application/json" },
        });
      if (url.startsWith("/api/videos")) {
        return json(200, options.videosBody ?? EMPTY_PAGE);
      }
      if (method === "POST") {
        posts.push({ url, body: String(init?.body ?? "") });
        const status = options.addStatus ?? 202;
        return json(
          status,
          status === 202
            ? { task_id: "task-abc-123" }
            : { detail: options.addDetail ?? "url 不是一个可识别的主页链接" },
        );
      }
      if (method === "PATCH") {
        patches.push({ url, body: String(init?.body ?? "") });
        const tracking = Boolean(JSON.parse(String(init?.body ?? "{}")).tracking);
        return json(200, { ...list[0], is_tracking: tracking });
      }
      if (url.includes("/platforms")) return json(200, platformsBody);
      if (url.includes("/creators?platform=bilibili")) return json(200, [list[0]]);
      if (url.includes("/creators?platform=douyin")) return json(200, [list[1]]);
      return json(200, list);
    }),
  );
}

function renderPage(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: 0 } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Creators />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function ready(): Promise<void> {
  await screen.findByText("阿婆主甲");
}

beforeEach(() => {
  calls.length = 0;
  posts.length = 0;
  patches.length = 0;
  stub();
  useSettings.setState({ captureTrackingDefault: true });
});

afterEach(() => {
  cleanup();
});

describe("Creators 的列表", () => {
  it("一位博主一行：行数由后端给的条数决定", async () => {
    renderPage();
    await ready();
    expect(screen.getAllByRole("article")).toHaveLength(list.length);
    // 平台侧 id 必须在：昵称会漂，出问题时唯一能对着后端查的就是它
    expect(screen.getByText("MS4wLjABAAAA")).toBeTruthy();
  });

  it("按平台筛选换的是那份请求，且不动收录表单里的平台", async () => {
    renderPage();
    await ready();
    await userEvent.click(screen.getByRole("button", { name: "B站" }));
    await waitFor(() =>
      expect(calls.some((call) => call.includes("platform=bilibili"))).toBe(true),
    );
    expect(screen.getAllByRole("article")).toHaveLength(1);

    await userEvent.type(
      screen.getByRole("textbox", { name: /主页链接/ }),
      "https://example.com/x",
    );
    await userEvent.click(screen.getByRole("button", { name: "收录" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    // 这两处状态合成一个的话，这里就会是 "bilibili" —— 一位抖音博主被打到 B站 名下
    expect(JSON.parse(posts[0]?.body ?? "{}")).toMatchObject({ platform: null });
  });

  it("库里还没有博主时说实话，不是错误卡也不是空一片", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request) => {
        const url = String(input);
        if (url.includes("/platforms")) {
          return new Response(JSON.stringify(platformsBody), { status: 200 });
        }
        return new Response(JSON.stringify([]), { status: 200 });
      }),
    );
    renderPage();
    await screen.findByText(/库里还没有博主/);
    expect(screen.queryByText(/读不到博主列表/)).toBeNull();
  });

  it("读不到：给后端原文", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request) => {
        const url = String(input);
        if (url.includes("/creators")) {
          return new Response(JSON.stringify({ detail: "主库被另一个进程锁住" }), { status: 500 });
        }
        return new Response(JSON.stringify(platformsBody), { status: 200 });
      }),
    );
    renderPage();
    await screen.findByText("主库被另一个进程锁住");
  });
});

describe("爆款回溯入口（T6.6）", () => {
  it("默认不展开：一个 /api/videos 请求都不发", async () => {
    stub();
    renderPage();
    await screen.findByText("阿婆主甲");
    expect(calls.filter((call) => call.startsWith("GET /api/videos"))).toHaveLength(0);
  });

  it("展开要的是「这一位的 + 按赞数排的那一条请求」，画出来的顺序就是后端给的顺序", async () => {
    // 后端桩给的是**赞数升序**：面板要是自己在前端排一遍，顺序会反过来 → 红。
    // （后端排序与前端排序各一份，迟早不一致 —— V1 §7.10 那一族。）
    stub({ videosBody: videosPage("低赞那条", "中赞那条", "高赞那条") });
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "爆款回溯 阿婆主甲" }));

    await screen.findByText("低赞那条");
    const request = calls.find((call) => call.startsWith("GET /api/videos"));
    expect(request).toBeDefined();
    expect(request).toContain("creator_id=3");
    expect(request).toContain("sort=benchmark");

    const rows = screen.getAllByRole("listitem").map((item) => item.textContent ?? "");
    expect(rows).toHaveLength(3);
    expect(rows[0]).toContain("低赞那条");
    expect(rows[2]).toContain("高赞那条");
  });

  it("这一位还没有作品：说「先跑一次采集」，不是一片空白也不是错误卡", async () => {
    stub({ videosBody: EMPTY_PAGE });
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "爆款回溯 阿婆主甲" }));
    await screen.findByText(/这一位库里还没有作品/);
    expect(screen.queryByText(/读不到/)).toBeNull();
  });

  it("每一位只展开自己那一行", async () => {
    stub({ videosBody: videosPage("只有一条") });
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "爆款回溯 阿婆主甲" }));
    await screen.findByText("只有一条");
    // 展开态是**按行**存的，不是页面级一个 bool：另一位仍是收起的。
    // 断 `aria-expanded` 而不是按钮文案 —— 文案会被 aria-label 盖掉，测的其实是同一件事的两面。
    const states = screen
      .getAllByRole("button", { name: /爆款回溯/ })
      .map((button) => button.getAttribute("aria-expanded"));
    expect(states).toEqual(["true", "false"]);
  });
});

describe("跟踪开关", () => {
  it("点开关发的是 PATCH 这一位，字段名是 tracking 且是真布尔", async () => {
    renderPage();
    await ready();
    await userEvent.click(screen.getByRole("switch", { name: "跟踪 阿婆主甲" }));
    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0]?.url).toBe("/api/creators/3/tracking");
    expect(patches[0]?.body).toBe('{"tracking":false}');
  });

  it("每一位的写入只亮自己那一行的「写入中…」", async () => {
    // 用数组装：`let release: (()=>void)|null = null` 会被控制流分析钉死在 null，
    // 调用点上它已经是 `never`（TS2349），闭包里那次赋值它看不见。
    const releases: (() => void)[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        const method = String(init?.method ?? "GET");
        const json = (body: unknown, status = 200): Response =>
          new Response(JSON.stringify(body), { status });
        if (method === "PATCH") {
          await new Promise<void>((resolve) => {
            releases.push(resolve);
          });
          return json({ ...list[0], is_tracking: false });
        }
        if (url.includes("/platforms")) return json(platformsBody);
        return json(list);
      }),
    );
    renderPage();
    await ready();
    await userEvent.click(screen.getByRole("switch", { name: "跟踪 阿婆主甲" }));
    await waitFor(() => expect(screen.getAllByText("写入中…")).toHaveLength(1));
    expect(screen.getByText("只存档")).toBeTruthy(); // 另一位没被牵连
    await waitFor(() => expect(releases).toHaveLength(1));
    releases[0]?.();
  });
});

describe("收录表单", () => {
  it("tracking 用 settings 里那份默认值，不是前端写死的 true", async () => {
    useSettings.setState({ captureTrackingDefault: false });
    renderPage();
    await ready();
    await userEvent.type(
      screen.getByRole("textbox", { name: /主页链接/ }),
      "https://example.com/a",
    );
    await userEvent.click(screen.getByRole("button", { name: "收录" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(JSON.parse(posts[0]?.body ?? "{}")).toEqual({
      url: "https://example.com/a",
      tracking: false,
      platform: null,
    });
  });

  /** 这一条钉的是这一页最容易说假话的地方：202 不等于"已经收录"。 */
  it("成功说的是「已排队 · 任务 <id>」，页面上不出现「已添加」", async () => {
    renderPage();
    await ready();
    await userEvent.type(
      screen.getByRole("textbox", { name: /主页链接/ }),
      "https://example.com/a",
    );
    await userEvent.click(screen.getByRole("button", { name: "收录" }));
    await screen.findByText(/已排队：任务/);
    expect(screen.getByText("task-abc-123")).toBeTruthy();
    expect(screen.queryByText(/已添加/)).toBeNull();
  });

  it("没排队上：给后端原文", async () => {
    stub({ addStatus: 422 });
    renderPage();
    await ready();
    await userEvent.type(
      screen.getByRole("textbox", { name: /主页链接/ }),
      "https://example.com/ok",
    );
    await userEvent.click(screen.getByRole("button", { name: "收录" }));
    await screen.findByText(/收录没排队上/);
    expect(screen.getByText(/不是一个可识别的主页链接/)).toBeTruthy();
  });

  it("链接是必填：空着提交由浏览器挡下，不发请求", async () => {
    renderPage();
    await ready();
    await userEvent.click(screen.getByRole("button", { name: "收录" }));
    await waitFor(() => expect(posts).toHaveLength(0));
  });
});
