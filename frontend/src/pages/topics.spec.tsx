// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { Topics } from "./Topics";

/** 选题与草稿各一份现状。两份都要有内容：只给一份的话另一份的 `QueryState`
 *  会走进"读取中"，那几条用例测的就是"读不到"而不是它们各自该测的东西。 */
const topics = [
  { id: 7, name: "AI 做内容", description: "第一批", created_at: "2026-09-24T10:00:00Z" },
];
const drafts = [
  {
    id: 11,
    title: "三条路",
    content: "正文第一段",
    source_video_id: 42,
    status: "draft",
    created_at: "2026-09-24T10:00:00Z",
    updated_at: "2026-09-24T10:00:00Z",
  },
];

interface Call {
  url: string;
  method: string;
  body: string;
}

let calls: Call[] = [];

/** `handler` 决定每一次 GET/POST/PATCH/DELETE 回什么。默认一切正常。 */
function serve(
  handler: (url: string, method: string) => [number, unknown] = (url) => {
    if (url.includes("/topics")) return [200, topics];
    if (url.includes("/drafts")) return [200, drafts];
    return [200, []];
  },
): void {
  vi.mocked(fetch).mockImplementation(async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    const method = String(init?.method ?? "GET");
    calls.push({ url, method, body: String(init?.body ?? "") });
    const [status, body] = handler(url, method);
    if (status === 204) return new Response(null, { status: 204 });
    return new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  });
}

function renderPage(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: 0 } } });
  render(
    <QueryClientProvider client={client}>
      <Topics />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
  calls = [];
});

const bodiesFor = (method: string): Call[] => calls.filter((call) => call.method === method);

describe("Topics 页", () => {
  it("两份列表各自渲染出来（选题与草稿不共用一个查询）", async () => {
    serve();
    renderPage();
    expect(await screen.findByText("AI 做内容")).toBeTruthy();
    expect(screen.getByText("三条路")).toBeTruthy();
    expect(calls.filter((c) => c.url.includes("/topics")).length).toBeGreaterThan(0);
    expect(calls.filter((c) => c.url.includes("/drafts")).length).toBeGreaterThan(0);
  });

  it("读不到选题列表时说清楚原因，不把失败显示成「没有选题」", async () => {
    serve((url) => {
      if (url.includes("/topics")) return [500, { detail: "存储层还没起来" }];
      if (url.includes("/drafts")) return [200, drafts];
      return [200, []];
    });
    renderPage();
    expect(await screen.findByText(/读不到选题列表/)).toBeTruthy();
    expect(screen.getByText(/存储层还没起来/)).toBeTruthy();
    // 草稿那一栏不受牵连：它仍然给出自己的结论。
    expect(screen.getByText("三条路")).toBeTruthy();
    expect(screen.queryByText("还没有选题：用上面那个表单记一条。")).toBeNull();
  });

  it("新建选题 POST 名字与备注，成功后才清空表单", async () => {
    serve();
    renderPage();
    await screen.findByText("AI 做内容");
    await userEvent.type(screen.getByLabelText("选题名"), "新选题");
    await userEvent.type(screen.getByLabelText("备注（可留空）"), "顺手记一句");
    await userEvent.click(screen.getByRole("button", { name: "新建选题" }));
    await waitFor(() => expect(bodiesFor("POST")[0]).toBeDefined());
    const sent = JSON.parse(String(bodiesFor("POST")[0]?.body)) as Record<string, unknown>;
    expect(sent).toEqual({ name: "新选题", description: "顺手记一句" });
    await waitFor(() => expect(screen.getByLabelText("选题名")).toHaveValue(""));
  });

  it("撞名 409：显示后端原文，表单不清空（用户打的那几个字还要留着改）", async () => {
    serve((url, method) => {
      if (method === "POST" && url.includes("/topics")) {
        return [409, { detail: "topic 已存在（UNIQUE constraint failed: topics.name）" }];
      }
      if (url.includes("/topics")) return [200, topics];
      if (url.includes("/drafts")) return [200, drafts];
      return [200, []];
    });
    renderPage();
    await screen.findByText("AI 做内容");
    await userEvent.type(screen.getByLabelText("选题名"), "AI 做内容");
    await userEvent.click(screen.getByRole("button", { name: "新建选题" }));
    expect(await screen.findByText(/UNIQUE constraint failed/)).toBeTruthy();
    expect(screen.getByLabelText("选题名")).toHaveValue("AI 做内容");
  });

  it("删除选题发的是 DELETE /api/topics/<id>，之后重取列表", async () => {
    serve((url, method) => {
      if (method === "DELETE") return [204, null];
      if (url.includes("/topics")) {
        // 删除之后列表就空了：靠"第二次 GET 拿到空表"来证明 invalidate 真的发生了。
        const alreadyDeleted = calls.some((c) => c.method === "DELETE");
        return [200, alreadyDeleted ? [] : topics];
      }
      if (url.includes("/drafts")) return [200, drafts];
      return [200, []];
    });
    renderPage();
    await screen.findByText("AI 做内容");
    // 两栏各有一个「删除」，所以按栏取 —— 用 `getAllBy…[0]` 的那一种写法
    // 一旦 DOM 顺序变了就会点到草稿那一行，而用例照样绿。
    const topicSection = within(screen.getByRole("region", { name: "选题" }));
    await userEvent.click(topicSection.getByRole("button", { name: "删除" }));
    const deleted = bodiesFor("DELETE")[0];
    expect(deleted?.url).toBe("/api/topics/7");
    expect(await screen.findByText("还没有选题：用上面那个表单记一条。")).toBeTruthy();
  });

  it("草稿点「发布」只 PATCH 状态这一栏（不许把整行摊回去）", async () => {
    serve((url, method) => {
      if (method === "PATCH") {
        return [200, { ...drafts[0], status: "published", updated_at: "2026-09-24T11:00:00Z" }];
      }
      if (url.includes("/topics")) return [200, topics];
      if (url.includes("/drafts")) return [200, drafts];
      return [200, []];
    });
    renderPage();
    await screen.findByText("三条路");
    await userEvent.click(screen.getByRole("button", { name: "发布" }));
    await waitFor(() => expect(bodiesFor("PATCH")[0]).toBeDefined());
    const patch = bodiesFor("PATCH")[0];
    expect(patch?.url).toBe("/api/drafts/11");
    // **这一条是 V1 §7.4 在界面上的那一半**：body 里只有 status 一个键。
    // 多带 title/content/source_video_id 中的任何一个，后端都会把那一列按这份
    // 可能已经过期的快照重写一遍 —— 而这里看不出来，只在"稿子的来源不见了"时才发现。
    expect(JSON.parse(String(patch?.body))).toEqual({ status: "published" });
  });
});
