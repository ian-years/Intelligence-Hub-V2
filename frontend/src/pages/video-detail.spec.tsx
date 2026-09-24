// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { VideoDetail } from "@/pages/VideoDetail";
import { makeCreator, makeVideo } from "@/test/fixtures";

const video = makeVideo({
  id: 7,
  title: "一条作品",
  creator_id: 3,
  platform: "bilibili",
  duration_seconds: 245,
  view_count: 12345,
  media_path: "media/bilibili/BV1xx/video.mp4",
  media_source: "yt_dlp",
  description: "第一行\n第二行",
});

const transcriptBody = {
  video_id: 7,
  engine: "subtitle",
  language: "zh",
  char_count: 12,
  sentence_count: 2,
  text: "口播稿第一句。\n口播稿第二句。",
  segments_json: null,
  // ADR-0015：这三列后端一定会给（没做过就是 null），所以桩也得给全 ——
  // 少给一个字段，用例会在"组件读了一个不存在的键"上绿。
  content_summary: null,
  key_points: null,
  summary_method: null,
};

const calls: string[] = [];
const patches: { url: string; body: string }[] = [];

/** 这一条是**状态**：隐藏成功后 `useHideVideo` 会让作品查询重取，
 *  重取还回一个"未隐藏"的行，就等于用桩把"墓碑生效了"这件事抹平 ——
 *  那样的用例只能证明按钮被点过。 */
let hiddenRow = false;
const row = (): unknown =>
  hiddenRow
    ? makeVideo({ id: 7, title: "一条作品", is_hidden: true, hidden_reason: "在作品详情页隐藏" })
    : video;

function stub(options: { transcript?: [number, unknown]; creator?: [number, unknown] } = {}): void {
  hiddenRow = false;
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
      if (method === "PATCH") {
        patches.push({ url, body: String(init?.body ?? "") });
        hiddenRow = url.endsWith("/hide");
        return json(200, row());
      }
      if (url.includes("/transcript")) {
        const out = options.transcript ?? [200, transcriptBody];
        return json(out[0], out[1]);
      }
      if (url.includes("/creators/")) {
        const out = options.creator ?? [200, makeCreator({ id: 3, name: "阿婆主甲" })];
        return json(out[0], out[1]);
      }
      return json(200, row());
    }),
  );
}

function renderAt(path: string): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: 0 } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/video/:id" element={<VideoDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  calls.length = 0;
  patches.length = 0;
  stub();
});

afterEach(() => {
  cleanup();
});

describe("VideoDetail", () => {
  it("地址里那一段不是 id：一个请求都不发，并说明是地址的问题", async () => {
    renderAt("/video/不是数字");
    await screen.findByText(/不是一个作品 id/);
    expect(calls.filter((call) => call.includes("/api/videos"))).toHaveLength(0);
  });

  it("元数据画出来，并且明说这一版没有播放器", async () => {
    renderAt("/video/7");
    await screen.findByText("一条作品");
    expect(screen.getByText("4:05")).toBeTruthy();
    expect(screen.getByText("12,345")).toBeTruthy();
    expect(screen.getByText(/media\/bilibili\/BV1xx\/video\.mp4/)).toBeTruthy();
    expect(screen.getByText(/yt_dlp/)).toBeTruthy();
    expect(screen.getByText(/这一版没有内嵌播放器/)).toBeTruthy();
  });

  it("简介保留换行（它是外部输入，靠 React 转义而不是 innerHTML）", async () => {
    renderAt("/video/7");
    const block = await screen.findByText(/第一行/);
    expect(block.className).toContain("whitespace-pre-wrap");
    expect(block.textContent).toContain("\n第二行");
  });

  it("作者读不到时说「未读到」并带上 creator_id，不许空一格", async () => {
    stub({ creator: [500, { detail: "博主表读不到" }] });
    renderAt("/video/7");
    await screen.findByText(/未读到（creator_id=3）/);
  });

  it("口播稿的 404 是正常态：说「还没有稿子」，不当错误也不当空", async () => {
    stub({ transcript: [404, { detail: "还没有转写" }] });
    renderAt("/video/7");
    await screen.findByText(/这条还没有口播稿/);
    expect(screen.queryByText(/读不到口播稿/)).toBeNull();
  });

  it("有稿子：正文与引擎/字数都在", async () => {
    renderAt("/video/7");
    await screen.findByText(/口播稿第一句/);
    expect(screen.getByText(/引擎 subtitle/)).toBeTruthy();
  });

  it("参考材料：摘要与要点渲染出来，并说出它是谁产的（ADR-0015）", async () => {
    stub({
      transcript: [
        200,
        {
          ...transcriptBody,
          content_summary: "这条讲两句话，先说桥再说任务。",
          key_points: "- 先说桥\n- 再说任务",
          summary_method: "local-extractive",
        },
      ],
    });
    renderAt("/video/7");
    await screen.findByText("这条讲两句话，先说桥再说任务。");
    // 要点前面的 "- " 是数据格式，不是要显示给用户看的记号
    expect(screen.getByText("先说桥")).toBeTruthy();
    expect(screen.queryByText(/- 先说桥/)).toBeNull();
    // 那行来源标签是这块的存在理由：抽取式片段与整篇改写长得一样，能信的程度不一样
    expect(screen.getByText(/本地抽取式/)).toBeTruthy();
  });

  it("V1 搬来的摘要要说「生产者没有记录」，不冒充本地抽取式", async () => {
    stub({
      transcript: [
        200,
        {
          ...transcriptBody,
          content_summary: "V1 里那份整篇改写。",
          key_points: null,
          summary_method: "v1-imported",
        },
      ],
    });
    renderAt("/video/7");
    await screen.findByText(/生产者没有记录/);
    expect(screen.queryByText(/本地抽取式/)).toBeNull();
  });

  it("没做过摘要时整块不渲染，不放「（无）」占位", async () => {
    renderAt("/video/7");
    await screen.findByText(/口播稿第一句/);
    expect(screen.queryByText(/参考材料/)).toBeNull();
    expect(screen.queryByText(/本地抽取式/)).toBeNull();
  });

  it("稿子真读不到（500）：给原因，不许被 404 那句「还没有」盖掉", async () => {
    stub({ transcript: [500, { detail: "口播稿文件不在磁盘上" }] });
    renderAt("/video/7");
    await screen.findByText(/读不到口播稿/);
    expect(screen.getByText("口播稿文件不在磁盘上")).toBeTruthy();
    expect(screen.queryByText(/这条还没有口播稿/)).toBeNull();
  });

  it("点隐藏：PATCH 这一条，带原因", async () => {
    renderAt("/video/7");
    await screen.findByText("一条作品");
    await userEvent.click(screen.getByRole("button", { name: "隐藏" }));
    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0]?.url).toBe("/api/videos/7/hide");
    expect(JSON.parse(patches[0]?.body ?? "{}")).toEqual({ reason: "在作品详情页隐藏" });
  });

  it("已隐藏的作品给的是取消隐藏，那一笔打在 unhide 上", async () => {
    stub();
    renderAt("/video/7");
    await screen.findByText("一条作品");
    // 隐藏成功后返回的行已经是 is_hidden（见上面的 PATCH 响应），这里直接看第二次渲染
    await userEvent.click(screen.getByRole("button", { name: "隐藏" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "取消隐藏" })).toBeTruthy());
    await userEvent.click(screen.getByRole("button", { name: "取消隐藏" }));
    await waitFor(() => expect(patches.at(-1)?.url).toBe("/api/videos/7/unhide"));
  });
});
