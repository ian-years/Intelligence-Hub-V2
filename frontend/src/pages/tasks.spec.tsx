// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Tasks } from "@/pages/Tasks";
import { makeRun } from "@/test/fixtures";

/** 四个任务，覆盖界面要的三种形状：无需参数 / 只差一个 url / 参数发不了。 */
const tasks = [
  {
    name: "preflight",
    display_name: "环境预检",
    kind: "preflight",
    platforms: [],
    cancellable: false,
    timeout_seconds: 120,
  },
  {
    name: "douyin_collect",
    display_name: "抖音采集",
    kind: "collect",
    platforms: ["douyin"],
    cancellable: true,
    timeout_seconds: null,
  },
  {
    name: "single_link",
    display_name: "收一条链接",
    kind: "collect",
    platforms: ["douyin", "bilibili"],
    cancellable: true,
    timeout_seconds: 600,
  },
  {
    name: "postprocess",
    display_name: "后处理",
    kind: "postprocess",
    platforms: [],
    cancellable: false,
    timeout_seconds: null,
  },
];

const schemas: Record<string, { required: string[] }> = {
  preflight: { required: [] },
  douyin_collect: { required: [] },
  single_link: { required: ["url"] },
  postprocess: { required: ["video_ids", "reason"] },
};

const runs = [
  makeRun({ id: "run-1", task_name: "preflight", status: "success" }),
  makeRun({
    id: "run-2",
    task_name: "douyin_collect",
    status: "running",
    ended_at: null,
    progress: 0.5,
  }),
];

const calls: string[] = [];
const sent: { url: string; body: string }[] = [];

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  readonly listeners = new Map<string, ((event: MessageEvent) => void)[]>();
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, handler: (event: MessageEvent) => void): void {
    const seen = this.listeners.get(type) ?? [];
    seen.push(handler);
    this.listeners.set(type, seen);
  }

  close(): void {
    this.closed = true;
  }

  /** 一帧具名事件：后端每一帧都带 `event: <type>`，所以 `onmessage` 一帧都收不到。 */
  emit(type: string, payload: Record<string, unknown>): void {
    const frame = { data: JSON.stringify({ type, ...payload }) } as MessageEvent;
    for (const handler of this.listeners.get(type) ?? []) handler(frame);
  }

  fireOpen(): void {
    this.onopen?.();
  }
}

function stub(): void {
  calls.length = 0;
  sent.length = 0;
  FakeEventSource.instances = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      const method = String(init?.method ?? "GET");
      calls.push(`${method} ${url}`);
      const json = (body: unknown, status = 200): Response =>
        new Response(JSON.stringify(body), {
          status,
          headers: { "Content-Type": "application/json" },
        });
      if (method === "POST" && url.endsWith("/cancel")) {
        return json({ ok: true, task_id: url.split("/").at(-2) ?? "" });
      }
      if (method === "POST") {
        sent.push({ url, body: String(init?.body ?? "") });
        return json({ task_id: "task-777" }, 202);
      }
      if (url.includes("/schema")) {
        const name = url.split("/api/tasks/").at(-1)?.split("/")[0] ?? "";
        return json({ type: "object", properties: {}, required: schemas[name]?.required ?? [] });
      }
      if (url.includes("/tasks/runs")) return json(runs);
      if (url.includes("/api/tasks")) return json(tasks);
      return json({ detail: `没有这个端点：${url}` }, 404);
    }),
  );
  vi.stubGlobal("EventSource", FakeEventSource);
}

function renderPage(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: 0 } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Tasks />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function ready(): Promise<void> {
  await screen.findByText("环境预检");
}

beforeEach(stub);

afterEach(() => {
  cleanup();
});

describe("可发起的任务", () => {
  it("一张卡一个任务：卡片数由 `/api/tasks` 给条数决定", async () => {
    renderPage();
    await ready();
    // 展示名只出现在卡片上（历史那一栏用的是 task_name），所以"恰好一次"
    // 同时排除了漏画与重复画。
    for (const task of tasks) expect(screen.getAllByText(task.display_name)).toHaveLength(1);
  });

  it("没有必填参数的任务给「跑一次」，提交的是空对象", async () => {
    renderPage();
    await ready();
    const button = await screen.findByRole("button", { name: "跑一次" });
    await userEvent.click(button);
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]?.url).toBe("/api/tasks/preflight/run");
    expect(sent[0]?.body).toBe("{}");
  });

  /** 202 的那句话只能说"排到了哪个任务"。说成"已完成"就是臆造成功。 */
  it("提交成功说的是「已排队 · 任务 <id>」，不出现「完成」", async () => {
    renderPage();
    await ready();
    await userEvent.click(await screen.findByRole("button", { name: "跑一次" }));
    await screen.findByText(/已排队：任务/);
    expect(screen.getByText("task-777")).toBeTruthy();
    expect(screen.queryByText(/提交成功|已完成/)).toBeNull();
  });

  it("只差一个 url 的任务给链接框，提交时带上它", async () => {
    renderPage();
    await ready();
    const box = await screen.findByRole("textbox", { name: "链接" });
    await userEvent.type(box, "https://b23.tv/abc");
    await userEvent.click(screen.getByRole("button", { name: "提交" }));
    await waitFor(() =>
      expect(sent.some((one) => one.url === "/api/tasks/single_link/run")).toBe(true),
    );
    const body = sent.find((one) => one.url === "/api/tasks/single_link/run")?.body;
    expect(JSON.parse(body ?? "{}")).toEqual({ url: "https://b23.tv/abc" });
  });

  it("参数发不了的任务不许画一个必然 422 的按钮：说清必填是什么", async () => {
    renderPage();
    await ready();
    const note = await screen.findByText(/这一版界面发不了它/);
    expect(note.textContent).toContain("video_ids");
    // 只有 preflight 与 douyin_collect 两个无参任务该有"跑一次"
    expect(screen.getAllByRole("button", { name: "跑一次" })).toHaveLength(2);
  });
});

describe("运行历史与取消", () => {
  it("历史按传进来的条数画行，只有正在跑的那条有取消按钮", async () => {
    renderPage();
    await ready();
    expect(screen.getAllByRole("listitem")).toHaveLength(runs.length);
    const buttons = screen.getAllByRole("button", { name: /^取消 / });
    expect(buttons).toHaveLength(1);
    await userEvent.click(buttons[0] as HTMLButtonElement);
    await waitFor(() => expect(calls).toContain("POST /api/tasks/runs/run-2/cancel"));
  });

  it("取消没送达时给原因：按钮点了没反应是最难自查的一种失败", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        const method = String(init?.method ?? "GET");
        const json = (body: unknown, status = 200): Response =>
          new Response(JSON.stringify(body), { status });
        if (method === "POST") return json({ detail: "这个任务已经结束了" }, 409);
        if (url.includes("/schema")) return json({ type: "object", required: [] });
        if (url.includes("/tasks/runs")) return json(runs);
        if (url.includes("/api/tasks")) return json(tasks);
        return json([]);
      }),
    );
    renderPage();
    await ready();
    await userEvent.click(screen.getByRole("button", { name: /^取消 / }));
    await screen.findByText(/取消没送达/);
    expect(screen.getByText(/这个任务已经结束了/)).toBeTruthy();
  });
});

describe("实时事件流", () => {
  it("每一类事件都要单独注册监听：只挂 onmessage 的话一帧都收不到", async () => {
    renderPage();
    await ready();
    const source = FakeEventSource.instances.at(-1);
    expect(source).toBeDefined();
    // 不传 types → 注册的是 EVENT_TYPES 全集（`>= 10` 是防空转，具体数量由契约用例钉）
    expect(source?.listeners.size).toBeGreaterThanOrEqual(10);
  });

  it("收到的一帧画在流上，并把连接状态说成已连上", async () => {
    renderPage();
    await ready();
    const source = FakeEventSource.instances.at(-1);
    source?.fireOpen();
    await screen.findByText("事件流已连上");
    source?.emit("task.progress", { task_id: "run-2", timestamp: "2026-09-23T01:02:03+00:00" });
    await screen.findByText(/task\.progress/);
    expect(screen.getByText(/任务 run-2/)).toBeTruthy();
  });

  it("这个环境没有 EventSource 时说明不可用，不许安静得像「一切正常」", async () => {
    vi.unstubAllGlobals();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request) => {
        const url = String(input);
        const json = (body: unknown): Response =>
          new Response(JSON.stringify(body), { status: 200 });
        if (url.includes("/schema")) return json({ type: "object", required: [] });
        if (url.includes("/tasks/runs")) return json(runs);
        if (url.includes("/api/tasks")) return json(tasks);
        return json([]);
      }),
    );
    delete (globalThis as { EventSource?: unknown }).EventSource;
    renderPage();
    await ready();
    expect(screen.getByText(/事件流不可用/)).toBeTruthy();
  });
});
