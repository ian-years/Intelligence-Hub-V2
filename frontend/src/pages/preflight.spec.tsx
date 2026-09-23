import { QueryClient, QueryClientProvider, onlineManager } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { PreflightReport } from "@/api/hooks/useHealth";

import { Preflight } from "./Preflight";

function reply(body: unknown, status = 200): void {
  vi.mocked(fetch).mockImplementation(
    async () =>
      new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      }),
  );
}

function renderPage(retry = 0): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry } } });
  render(
    <QueryClientProvider client={client}>
      <Preflight />
    </QueryClientProvider>,
  );
}

const green: PreflightReport = {
  status: "success",
  summary: {
    platforms_ok: 2,
    platforms_degraded: 0,
    platforms_unreachable: 0,
    storage: "ok",
    asr_model: "present",
    platform_status: "bilibili=ok, douyin=ok",
    tools_present: "ffmpeg, ffprobe, yt-dlp, node",
    tools_missing: "（无）",
  },
  failures: [],
};

const partial: PreflightReport = {
  ...green,
  status: "partial",
  summary: {
    ...green.summary,
    platforms_ok: 1,
    platforms_degraded: 1,
    platform_status: "bilibili=degraded, douyin=ok",
    tools_present: "ffmpeg, node",
    tools_missing: "ffprobe, yt-dlp",
  },
};

const red: PreflightReport = {
  status: "failed",
  summary: {
    platforms_ok: 0,
    platforms_degraded: 0,
    platforms_unreachable: 1,
    storage: "unreachable",
    asr_model: "missing",
    platform_status: "douyin=unreachable",
    tools_present: "（PATH 上一个都没有）",
    tools_missing: "ffmpeg, ffprobe, yt-dlp, node",
  },
  failures: [
    {
      platform: "douyin",
      stage: "task",
      error: "douyin unreachable：桥没起（ConnectionRefusedError: 拒绝连接 127.0.0.1:3457）",
      error_kind: "HealthUnreachable",
    },
    {
      platform: null,
      stage: "task",
      error: "主库 SELECT 1 失败（storage.healthcheck 返回 False）",
      error_kind: "StorageUnreachable",
    },
  ],
};

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

describe("Preflight 页", () => {
  it("全绿：说清几个平台过、工具在不在，且不出现失败明细区", async () => {
    reply(green);
    renderPage();
    await screen.findByText(/全绿/);
    expect(screen.getByText("bilibili")).toBeTruthy();
    expect(screen.getByText("douyin")).toBeTruthy();
    expect(screen.getAllByText("ok").length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText(/失败明细/)).toBeNull();
  });

  it("黄：降级看得见，缺工具那句「要紧由平台决定」要说出来", async () => {
    reply(partial);
    renderPage();
    await screen.findByText(/部分可用/);
    expect(screen.getByText("degraded")).toBeTruthy();
    expect(screen.getByText("ffprobe")).toBeTruthy();
    expect(screen.getByText(/要紧由平台决定/)).toBeTruthy();
  });

  /** 这一条是整个页面的目的：红的时候把原文摊开，而不是只说"出错了"。 */
  it("红：每条失败的 platform、error_kind 与原文都要出现", async () => {
    reply(red);
    renderPage();
    await screen.findByText(/不可用/);
    expect(screen.getByText(/ConnectionRefusedError/)).toBeTruthy();
    expect(screen.getByText("HealthUnreachable")).toBeTruthy();
    expect(screen.getByText("StorageUnreachable")).toBeTruthy();
    expect(screen.getByText(/主库 SELECT 1 失败/)).toBeTruthy();
  });

  it("读不到预检时显示原因，绝不显示全绿", async () => {
    reply({ detail: "预检探测不下去：bridge refused" }, 500);
    renderPage();
    await screen.findByText(/读不到预检结果/);
    expect(screen.getByText(/bridge refused/)).toBeTruthy();
    expect(screen.queryByText(/全绿/)).toBeNull();
  });

  it("请求还没回来时是「正在探测」，既不算失败也不算全绿", () => {
    vi.mocked(fetch).mockImplementation(
      () => new Promise<Response>(() => undefined) as Promise<Response>,
    );
    renderPage();
    expect(document.body.textContent).toContain("正在探测");
    expect(document.body.textContent).not.toContain("全绿");
  });

  /** 重试被挂起时 `isPending` 永远为真：界面必须说"被暂停"，
   * 不能继续说"正在探测"（那是承诺结果会来）。 */
  it("补发被挂起时说「被暂停」，既不说正在探测也不说全绿", async () => {
    reply(green);
    onlineManager.setOnline(false);
    renderPage(3);
    expect(await screen.findByText(/探测被暂停/)).toBeTruthy();
    expect(screen.queryByText(/正在探测，还没有结论/)).toBeNull();
    expect(screen.queryByText(/全绿/)).toBeNull();
    onlineManager.setOnline(true);
  });

  it("点重新探测会再问一次后端", async () => {
    reply(green);
    renderPage();
    await screen.findByText(/全绿/);
    const before = vi.mocked(fetch).mock.calls.length;
    await userEvent.click(screen.getByRole("button", { name: /重新探测/ }));
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(before));
  });
});
