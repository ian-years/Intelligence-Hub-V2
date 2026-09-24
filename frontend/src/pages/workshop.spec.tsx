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
      media_aux_paths_json: "[]",
      metadata_json: "{}",
      created_at: "2026-09-24T10:00:00Z",
      updated_at: "2026-09-24T10:00:00Z",
    },
  ],
  total: 1,
  page: 1,
  size: 8,
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

  it("截图包说实话：V2 没有这一格的数据源", async () => {
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "截图包" }));
    expect(await screen.findByText(/V2 没有截图包这一步/)).toBeTruthy();
  });
});
