// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Workshop } from "./Workshop";
import { AUTOSAVE_DELAY_MS } from "@/lib/workshop";

/** 三份现状都要有内容：少一份就有一条用例其实是在测"读不到"而不是它自己要测的东西。 */
const TOPICS = [
  { id: 7, name: "AI 做内容", description: null, created_at: "2026-09-24T10:00:00Z" },
];
const DRAFTS = [
  {
    id: 11,
    title: "三条路",
    content: "第一条\n第二条",
    source_video_id: 42,
    status: "draft",
    created_at: "2026-09-24T10:00:00Z",
    updated_at: "2026-09-24T11:00:00Z",
  },
];
const VIDEO_PAGE = {
  items: [
    {
      id: 42,
      platform: "bilibili",
      platform_video_id: "BV1",
      title: "对标那条",
      is_hidden: false,
      // 有媒体：截图包那一格靠这一列决定"能不能截"（没有成片时那句实话由它说）
      media_path: "media/bilibili/某UP/BV1-对标那条/media.mp4",
      duration_seconds: 40,
      media_aux_paths_json: "[]",
      metadata_json: "{}",
      created_at: "2026-09-24T10:00:00Z",
      updated_at: "2026-09-24T10:00:00Z",
    },
    {
      id: 43,
      platform: "bilibili",
      platform_video_id: "BV2",
      title: "只采到元数据那条",
      is_hidden: false,
      media_path: null,
      duration_seconds: 40,
      media_aux_paths_json: "[]",
      metadata_json: "{}",
      created_at: "2026-09-24T10:00:00Z",
      updated_at: "2026-09-24T10:00:00Z",
    },
  ],
  total: 2,
  page: 1,
  size: 8,
};
const SHOTS_BODY = {
  video_id: 42,
  requested_at: [0, 10, 20, 30],
  cached: false,
  failures: [],
  ffmpeg_version_or_error: "ffmpeg version 6.1 Copyright (c) 2000-2024 the FFmpeg project",
  shots: [
    {
      at_seconds: 0,
      path: "media/bilibili/某UP/BV1-对标那条/shots/shot-0.jpg",
      url: "/api/videos/42/shots/shot-0.jpg",
      size_bytes: 1024,
      produced: true,
    },
    {
      at_seconds: 10,
      path: "media/bilibili/某UP/BV1-对标那条/shots/shot-10.jpg",
      url: "/api/videos/42/shots/shot-10.jpg",
      size_bytes: 2048,
      produced: true,
    },
  ],
};
const TRANSCRIPT = {
  video_id: 42,
  engine: "bilibili_subtitle",
  language: "zh",
  char_count: 9,
  sentence_count: 2,
  text: "第一句。第二句。",
  segments_json: null,
  content_summary: null,
  key_points: null,
  summary_method: null,
};
const GENERATED = {
  title: "AI 做内容",
  template_key: "tutorial_save_loop",
  template_name: "教程型收藏闭环",
  beats_template_key: "tutorial_save_loop",
  mode: "SHORT",
  total_beats: 3,
  estimated_duration_seconds: 60,
  beats: [],
  cards: [],
  script_markdown: "生成的第一格\n生成的第二格\n生成的第三格",
  teleprompter_text: "生成的第一格。",
  film_job: {},
  version: 1,
};

interface Call {
  url: string;
  method: string;
  body: string;
}

let calls: Call[] = [];
let saveStatus = 200;
let shotsStatus = 200;
let shotsBody: unknown = SHOTS_BODY;

function serve(): void {
  vi.mocked(fetch).mockImplementation(async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    const method = String(init?.method ?? "GET");
    calls.push({ url, method, body: String(init?.body ?? "") });
    const json = (status: number, body: unknown): Response =>
      new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      });
    // 截图包那条要**先于** `/videos` 那一条判：URL 里同时含 `/videos`，
    // 排后面就会拿到作品列表，而用例仍会"绿着"什么都没测。
    if (url.includes("/shots")) {
      return json(shotsStatus, shotsStatus === 200 ? shotsBody : { detail: shotsBody });
    }
    if (method === "POST" && url.includes("/drafts")) {
      return json(
        saveStatus,
        saveStatus === 200 ? { ...DRAFTS[0], id: 99 } : { detail: "库写不进去" },
      );
    }
    if (method === "PATCH") {
      return json(saveStatus, { ...DRAFTS[0], id: 11 });
    }
    if (method === "POST" && url.includes("/generate-draft-script")) {
      return json(200, GENERATED);
    }
    if (url.includes("/topics")) return json(200, TOPICS);
    if (url.includes("/drafts")) return json(200, DRAFTS);
    if (url.includes("/transcript")) return json(200, TRANSCRIPT);
    if (url.includes("/benchmark-analysis")) return json(404, { detail: "还没有口播稿" });
    if (url.includes("/videos")) return json(200, VIDEO_PAGE);
    return json(200, {});
  });
}

function renderPage(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: 0 } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/workshop"]}>
        <Workshop />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** 等到"下一拍自动保存"发出去：真计时器，不用 fake timers ——
 *  自动保存这条路横跨 setTimeout 与两个 await（fetch + invalidate），
 *  假时钟下"该发的没发、不该发的发一次"两种错都能被伪装成对。 */
async function waitUntilAutosaveSettles(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, AUTOSAVE_DELAY_MS + 250));
}

beforeEach(() => {
  calls = [];
  saveStatus = 200;
  shotsStatus = 200;
  shotsBody = SHOTS_BODY;
  vi.stubGlobal("fetch", vi.fn());
  serve();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Workshop 的自动保存", () => {
  it("标题 + 正文齐了才发，且发出去的 body 与屏幕上那两个字逐字相同", async () => {
    renderPage();
    const title = await screen.findByLabelText(/标题/);
    const body = await screen.findByLabelText(/正文/);

    await userEvent.clear(title);
    await userEvent.type(title, "新选题");
    await userEvent.type(body, "第一格");
    await waitUntilAutosaveSettles();

    const saved = calls.filter((call) => call.method === "POST" && call.url.includes("/drafts"));
    expect(saved).toHaveLength(1);
    expect(JSON.parse(saved[0]?.body ?? "{}")).toMatchObject({
      title: "新选题",
      content: "第一格",
      status: "draft",
    });
    expect(await screen.findByText(/已保存/)).toBeTruthy();
  });

  it("连续打字只发一次（每敲一个字符存一次是拿打字速度去敲写盘路径）", async () => {
    renderPage();
    const body = await screen.findByLabelText(/正文/);
    const title = await screen.findByLabelText(/标题/);
    await userEvent.clear(title);
    await userEvent.type(title, "选题");

    for (const char of "一二三四五") {
      await userEvent.type(body, char);
      await new Promise((resolve) => setTimeout(resolve, 60));
    }
    await waitUntilAutosaveSettles();

    expect(
      calls.filter((call) => call.method === "POST" && call.url.includes("/drafts")),
    ).toHaveLength(1);
  });

  it("只有标题没有正文：一个保存请求都不发", async () => {
    renderPage();
    const title = await screen.findByLabelText(/标题/);
    await userEvent.clear(title);
    await userEvent.type(title, "只写了标题");
    await waitUntilAutosaveSettles();
    expect(
      calls.filter((call) => call.url.includes("/drafts") && call.method === "POST"),
    ).toHaveLength(0);
  });

  it("保存失败：说出后端原文，且**不**把输入清掉、不显示已保存", async () => {
    saveStatus = 500;
    renderPage();
    const title = await screen.findByLabelText(/标题/);
    const body = await screen.findByLabelText(/正文/);
    await userEvent.clear(title);
    await userEvent.type(title, "选题");
    await userEvent.type(body, "正文一处");
    await waitUntilAutosaveSettles();

    // 状态词与原因原文各是一个 span（失败时必须两句都在：只说"失败了"等于没说）
    await screen.findByText(/保存失败/);
    expect(await screen.findByText(/库写不进去/)).toBeTruthy();
    expect(screen.queryByText(/已保存/)).toBeNull();
    expect((body as HTMLTextAreaElement).value).toBe("正文一处");
  });
});

describe("Workshop 的参考与生成", () => {
  it("没选对标作品时不问逐字稿，也不问拆解", async () => {
    renderPage();
    await screen.findByText(/还没选对标作品/);
    await waitUntilAutosaveSettles();
    expect(calls.filter((call) => call.url.includes("/transcript"))).toHaveLength(0);
    expect(calls.filter((call) => call.url.includes("/benchmark-analysis"))).toHaveLength(0);
  });

  it("挑一条对标作品 → 逐字稿读回来，拆解读不到就说读不到", async () => {
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "对标那条" }));
    expect(await screen.findByText(/第一句。第二句。/)).toBeTruthy();
    // 拆解那条是 404（合成样本里这条就是没稿子的形状）：要显示原因，不能静默什么都不画
    expect(await screen.findByText(/拆解读不到|读不到拆解/)).toBeTruthy();
  });

  it("生成初稿只写进编辑器，不顺手落库", async () => {
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "从对标生成初稿" }));
    const body = await screen.findByLabelText(/正文/);
    await waitFor(() => expect((body as HTMLTextAreaElement).value).toContain("生成的第一格"));
    expect(
      calls.filter((call) => call.url.includes("/drafts") && call.method === "POST"),
    ).toHaveLength(0);

    const request = calls.find((call) => call.url.includes("/generate-draft-script"));
    expect(request).toBeDefined();
    expect(JSON.parse(request?.body ?? "{}")).toMatchObject({ template_key: "tutorial_save_loop" });
  });
});

describe("Workshop 的四视图", () => {
  it("分镜那一格数 == 正文的非空行数（同一份切分规则，两处不各写一遍）", async () => {
    renderPage();
    const body = await screen.findByLabelText(/正文/);
    await userEvent.type(body, "第一格\n\n  \n第二格");
    await userEvent.click(screen.getByRole("button", { name: "分镜" }));
    // 只看分镜那一格：左栏三个列表也是 listitem，全局数会把它们一起数进来
    const grid = await screen.findByRole("list", { name: "分镜" });
    const rows = within(grid).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(rows[0]?.textContent).toContain("第一格");
    expect(rows[1]?.textContent).toContain("第二格");
  });

  it("截图包：没有落地成片时仍说实话，而且一个请求都不发", async () => {
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "只采到元数据那条" }));
    await userEvent.click(screen.getByRole("button", { name: "截图包" }));
    expect(await screen.findByText(/没有落地成片/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "生成截图包" })).toBeNull();
    await waitUntilAutosaveSettles();
    expect(calls.filter((call) => call.url.includes("/shots"))).toHaveLength(0);
  });

  it("截图包：点一次生成的就是分镜那几个时间点，回来的图用后端给的 url 画", async () => {
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "对标那条" }));
    const body = await screen.findByLabelText(/正文/);
    await userEvent.type(body, "一\n二\n三\n四");
    await userEvent.click(screen.getByRole("button", { name: "截图包" }));
    await userEvent.click(await screen.findByRole("button", { name: "生成截图包" }));

    const call = calls.find((item) => item.method === "POST" && item.url.includes("/shots"));
    expect(call?.url).toBe("/api/videos/42/shots");
    // 40 秒 4 格 → 每格取起始秒。写死这一串等于同时钉住前后端那一条规则。
    expect(JSON.parse(call?.body ?? "{}")).toEqual({ at_seconds: [0, 10, 20, 30] });

    const grid = await screen.findByRole("list", { name: "截图包" });
    const images = within(grid).getAllByRole("img");
    expect(images).toHaveLength(2);
    // 地址必须逐字是后端给的那一个：前端拼出来的路径就是第二个真源（V1 §7.5 那一族）
    expect(images[0]?.getAttribute("src")).toBe("/api/videos/42/shots/shot-0.jpg");
    expect(images[1]?.getAttribute("src")).toBe("/api/videos/42/shots/shot-10.jpg");
    expect(images[1]?.getAttribute("alt")).toContain("10");
    expect(await screen.findByText(/刚截好 2 张/)).toBeTruthy();
    expect(screen.getAllByText(/ffmpeg version 6\.1/).length).toBeGreaterThan(0);
  });

  it("截图包：复用上次的时说「上次截好的」，不谎报刚截", async () => {
    shotsBody = { ...SHOTS_BODY, cached: true, ffmpeg_version_or_error: null };
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "对标那条" }));
    await userEvent.click(screen.getByRole("button", { name: "截图包" }));
    await userEvent.click(await screen.findByRole("button", { name: "生成截图包" }));
    expect(await screen.findByText(/上次截好的 2 张/)).toBeTruthy();
    expect(screen.queryByText(/刚截好/)).toBeNull();
  });

  it("截图包：ffmpeg 不在场时把那一句原文显示出来，且不画一个假装成功的网格", async () => {
    shotsStatus = 503;
    shotsBody =
      "截不了帧：这台机器上没有可用的 ffmpeg。下一步二选一：① 装 ffmpeg；② 把 config/app.yaml 的 paths.ffmpeg 指到 ffmpeg.exe 的绝对路径";
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "对标那条" }));
    await userEvent.click(screen.getByRole("button", { name: "截图包" }));
    await userEvent.click(await screen.findByRole("button", { name: "生成截图包" }));

    expect(await screen.findByText(/截图包没做成/)).toBeTruthy();
    expect(await screen.findByText(/paths\.ffmpeg/)).toBeTruthy();
    expect(screen.queryByRole("list", { name: "截图包" })).toBeNull();
  });

  it("截图包：某几秒没截出来时逐条说出是哪几秒与原因", async () => {
    shotsBody = {
      ...SHOTS_BODY,
      failures: [{ at_seconds: 65, reason: "RuntimeError: ffmpeg 截帧失败（exit 1）" }],
    };
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "对标那条" }));
    await userEvent.click(screen.getByRole("button", { name: "截图包" }));
    await userEvent.click(await screen.findByRole("button", { name: "生成截图包" }));
    expect(await screen.findByText(/第 65 秒没截出来/)).toBeTruthy();
    expect(screen.getAllByText(/exit 1/).length).toBeGreaterThan(0);
  });

  it("截图包：后端回的不是这条作品的包时不画网格", async () => {
    shotsBody = { ...SHOTS_BODY, video_id: 99 };
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "对标那条" }));
    await userEvent.click(screen.getByRole("button", { name: "截图包" }));
    await userEvent.click(await screen.findByRole("button", { name: "生成截图包" }));
    await waitFor(() => expect(calls.some((call) => call.url.includes("/shots"))).toBe(true));
    expect(screen.queryByRole("list", { name: "截图包" })).toBeNull();
  });
});
