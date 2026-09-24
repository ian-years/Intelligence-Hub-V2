// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { onlineManager } from "@tanstack/react-query";
import { Dashboard } from "@/pages/Dashboard";
import { makeCreator, makePage, makeRun, makeVideo } from "@/test/fixtures";

/**
 * 总览页 = 四个区块各自读一份数据。这一份用例要紧的地方是**区块之间互不牵连**：
 * 一个端点坏了，别的区块照常；而"坏了"要显示原因，不许画成"这里没有内容"。
 */
const health = { status: "ok", version: "0.1.0", time: "2026-09-23T00:00:00+00:00" };

const platforms = {
  platforms: [
    // 三家各代表健康行的一种情况：**有结论有时刻** / **从没探测过** /
    // **有结论但没有时刻**（库里被写成这样就是数据坏了，界面必须选择不画，
    // 而不是画一个"上次探测 undefined"）。
    {
      name: "douyin",
      display_name: "抖音",
      enabled: true,
      implemented: true,
      health_status: "ok",
      health_checked_at: "2026-09-24T10:00:00+00:00",
      health_detail: null,
    },
    {
      name: "bilibili",
      display_name: "B站",
      enabled: false,
      implemented: true,
      health_status: null,
      health_checked_at: null,
      health_detail: null,
    },
    {
      name: "xiaohongshu",
      display_name: "小红书",
      enabled: true,
      implemented: false,
      health_status: "degraded",
      health_checked_at: null,
      health_detail: "桥没起",
    },
    {
      name: "youtube",
      display_name: "YouTube",
      enabled: false,
      implemented: false,
      health_status: null,
      health_checked_at: null,
      health_detail: null,
    },
  ],
};

const creators = [makeCreator({ id: 1, name: "阿婆主甲", platform: "bilibili" })];

const runs = [
  makeRun({ id: "r1", task_name: "douyin_collect", status: "success" }),
  makeRun({ id: "r2", task_name: "preflight", status: "running", ended_at: null, progress: 0.3 }),
];

const videos = makePage([
  makeVideo({ id: 11, title: "第一条", creator_id: 1, platform: "bilibili" }),
  makeVideo({ id: 12, title: "第二条", creator_id: 99, platform: "douyin" }),
]);

function bodyFor(url: string): [number, unknown] {
  if (url.includes("/health")) return [200, health];
  if (url.includes("/platforms")) return [200, platforms];
  if (url.includes("/creators")) return [200, creators];
  if (url.includes("/tasks/runs")) return [200, runs];
  if (url.includes("/videos")) return [200, videos];
  return [404, { detail: `没有这个端点：${url}` }];
}

/** 让某一个端点坏掉，其余照常 —— 这样测的才是"区块互不牵连"。 */
function serveWith(broken: string, status = 500): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request) => {
      const url = String(input);
      const [code, body] = url.includes(broken)
        ? [status, { detail: `后端说：${broken} 读不到` }]
        : bodyFor(url);
      return new Response(JSON.stringify(body), {
        status: code,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
}

function renderPage(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: 0 } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Dashboard />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function ready(): Promise<void> {
  await screen.findByText("阿婆主甲");
}

beforeEach(() => {
  serveWith("___never___");
  onlineManager.setOnline(true);
});

afterEach(() => {
  cleanup();
  onlineManager.setOnline(true);
});

describe("Dashboard 的四个区块", () => {
  it("平台卡片数 = `/api/platforms` 给条数：一行都不许凭空多出或消失", async () => {
    renderPage();
    await ready();
    // 一张卡一个状态牌，所以"牌的数量"就是"卡片的数量"—— 比数文案种类强：
    // 少画一张卡、或者给同一个平台画两张卡，这里都会立刻对不上。
    expect(screen.getAllByText(/已启用|已关闭|开着，但 V2 没实现/)).toHaveLength(
      platforms.platforms.length,
    );
    for (const platform of platforms.platforms) {
      expect(screen.getByText(platform.name)).toBeTruthy();
    }
  });

  it("没探测过的平台不画健康行；画出来的那行必须带时刻", async () => {
    renderPage();
    await ready();
    // 判据是"该画的有几家 = 画了几行"，不是"文案长什么样"：
    // 前者在 fixture 增加一家探测过的平台时会自动跟着要求两行，后者不会。
    const expected = platforms.platforms.filter(
      (p) => p.health_status !== null && p.health_checked_at !== null,
    );
    const lines = screen.getAllByText(/上次探测/);
    expect(lines).toHaveLength(expected.length);
    // xiaohongshu 有 status 但没有时刻 → 那一行不许出现（数据坏了时宁可不画）。
    expect(lines.some((line) => line.textContent?.includes("降级"))).toBe(false);
    expect(lines[0]?.textContent).toContain("连得上");
  });

  it("三种平台状态分开说：开着但没实现 ≠ 已启用", async () => {
    renderPage();
    await ready();
    // 期望条数从夹具算，不手写数字：改夹具不用回来改断言，
    // 而"少画一张卡 / 一张卡画两个牌"依然会当场对不上。
    const want: Record<string, number> = {
      已启用: platforms.platforms.filter((p) => p.enabled && p.implemented).length,
      已关闭: platforms.platforms.filter((p) => !p.enabled).length,
      "开着，但 V2 没实现": platforms.platforms.filter((p) => p.enabled && !p.implemented).length,
    };
    // 夹具必须三种状态都有，否则这条只是在数空气
    expect(Object.values(want).every((count) => count > 0)).toBe(true);
    for (const [label, count] of Object.entries(want)) {
      expect(screen.getAllByText(new RegExp(label))).toHaveLength(count);
    }
  });

  it("作品行数 = 这一页 items 的条数，作者名从博主表里来", async () => {
    renderPage();
    await ready();
    expect(screen.getAllByRole("article")).toHaveLength(videos.items.length);
    expect(screen.getByText("阿婆主甲")).toBeTruthy();
    // 第二条的作者不在这份博主表里：显示 id 而不是空着
    expect(screen.getByText("博主 #99")).toBeTruthy();
  });

  it("运行记录按传进来的条数画行，在跑的那条带进度", async () => {
    renderPage();
    await ready();
    expect(screen.getAllByRole("listitem")).toHaveLength(runs.length);
    expect(screen.getByText("30%")).toBeTruthy();
  });

  it("总览只给最近几条：全历史去任务页", async () => {
    const many = Array.from({ length: 14 }, (_, index) =>
      makeRun({ id: `r${String(index)}`, task_name: `task_${String(index)}` }),
    );
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request) => {
        const url = String(input);
        if (url.includes("/tasks/runs")) return new Response(JSON.stringify(many), { status: 200 });
        const [code, body] = bodyFor(url);
        return new Response(JSON.stringify(body), { status: code });
      }),
    );
    renderPage();
    await ready();
    expect(screen.getByText("task_0")).toBeTruthy();
    // 最后一条不许出现在这一屏 —— 出现了就说明这不是"最近几条"而是把全表拉进来了
    expect(screen.queryByText("task_13")).toBeNull();
  });
});

describe("一个区块坏掉不许牵连别的区块", () => {
  it("作品读不到：那一栏给原因，平台与任务照常画", async () => {
    serveWith("/videos");
    renderPage();
    await screen.findByText(/读不到最新作品/);
    expect(screen.getByText("后端说：/videos 读不到")).toBeTruthy();
    expect(screen.getByText("抖音")).toBeTruthy();
    expect(screen.getByText("douyin_collect")).toBeTruthy();
  });

  it("博主表读不到时不许把作品栏一起判死：行照画，作者格显示 id", async () => {
    serveWith("/creators");
    renderPage();
    await screen.findByText("第一条");
    expect(screen.getByText("博主 #99")).toBeTruthy();
    expect(screen.queryByText(/读不到最新作品/)).toBeNull();
  });

  it("窗口不在前台时每个区块一起说「补发被挂起」，且一个数据节点都不画", async () => {
    onlineManager.setOnline(false);
    renderPage();
    // findAll 而不是 findBy：四个区块各说一次，"唯一命中"的查询在这里必然炸
    await screen.findAllByText(/补发被挂起/);
    expect(screen.getAllByText(/补发被挂起/).length).toBeGreaterThan(1);
    expect(screen.queryAllByRole("article")).toHaveLength(0);
    expect(screen.queryByText("抖音")).toBeNull();
  });

  it("库里确实没有作品：说「没有内容」，而不是「读不到」", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request) => {
        const url = String(input);
        if (url.includes("/videos"))
          return new Response(JSON.stringify(makePage([])), { status: 200 });
        const [code, body] = bodyFor(url);
        return new Response(JSON.stringify(body), { status: code });
      }),
    );
    renderPage();
    await screen.findByText(/还没有未隐藏的作品/);
    // 注意不能断言"整页没有『读不到』三个字"：那句说明里就带着它。
    // 要钉的是错误卡那一个标题（`读不到{subject}`）。
    expect(screen.queryByText(/读不到最新作品/)).toBeNull();
  });
});
