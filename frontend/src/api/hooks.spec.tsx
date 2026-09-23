import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { usePlatforms, usePlatformSchema, useUpdatePlatformConfig } from "./hooks/useConfig";
import { useAddCreator, useCreators, useSetTracking } from "./hooks/useCreators";
import {
  useHideVideo,
  useSubmitSingleLink,
  useTranscript,
  useUnhideVideo,
  useVideos,
} from "./hooks/useVideos";
import { useCancelRun, useManifests, useRuns, useRunTask, useTasks } from "./hooks/useTasks";
import { resolveRef } from "./json-schema";

const seen: string[] = [];

function reply(body: unknown, status = 200): void {
  vi.mocked(fetch).mockImplementation(async (input: string | URL | Request, init?: RequestInit) => {
    // 记 method 是因为"只发 GET 还是发了 PUT"这种事，页面上一模一样。
    seen.push(`${String(init?.method ?? "GET")} ${String(input)}`);
    return new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  });
}

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: 0 } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  seen.length = 0;
  vi.stubGlobal("fetch", vi.fn());
});

describe("查询钩子发出去的 URL 就是契约里那一条", () => {
  it("health / preflight / platforms / tasks / runs / manifests", async () => {
    reply({ status: "ok", time: "t", version: "v" });
    const { useHealth, usePreflight } = await import("./hooks/useHealth");
    const h = renderHook(() => useHealth(), { wrapper });
    await waitFor(() => expect(h.result.current.isSuccess).toBe(true));
    expect(seen.at(-1)).toBe("GET /api/health");

    const p = renderHook(() => usePreflight(), { wrapper });
    reply({ status: "partial", summary: {}, failures: [] });
    await waitFor(() => expect(p.result.current.isSuccess).toBe(true));
    expect(seen.at(-1)).toBe("GET /api/preflight");

    const pl = renderHook(() => usePlatforms(), { wrapper });
    reply({ platforms: [] });
    await waitFor(() => expect(pl.result.current.isSuccess).toBe(true));
    expect(seen.at(-1)).toBe("GET /api/platforms");

    const ts = renderHook(() => useTasks(), { wrapper });
    reply([]);
    await waitFor(() => expect(ts.result.current.isSuccess).toBe(true));
    expect(seen.at(-1)).toBe("GET /api/tasks");

    const rs = renderHook(() => useRuns(), { wrapper });
    reply([]);
    await waitFor(() => expect(rs.result.current.isSuccess).toBe(true));
    expect(seen.at(-1)).toBe("GET /api/tasks/runs?limit=50");

    const mf = renderHook(() => useManifests(5), { wrapper });
    reply([]);
    await waitFor(() => expect(mf.result.current.isSuccess).toBe(true));
    expect(seen.at(-1)).toBe("GET /api/manifests?limit=5");
  });

  it("带参数的查询：creators 按平台、videos 按整个筛选表", async () => {
    reply([]);
    const c = renderHook(() => useCreators("douyin"), { wrapper });
    await waitFor(() => expect(c.result.current.isSuccess).toBe(true));
    expect(seen.at(-1)).toBe("GET /api/creators?platform=douyin");

    reply({ items: [], page: 2, size: 20, total: 0 });
    const v = renderHook(
      () => useVideos({ platform: "bilibili", hidden: "hidden", page: 2, size: 20, search: "" }),
      { wrapper },
    );
    await waitFor(() => expect(v.result.current.isSuccess).toBe(true));
    expect(seen.at(-1)).toBe("GET /api/videos?platform=bilibili&hidden=hidden&page=2&size=20");
  });

  it("作品流的筛选进 queryKey：换 hidden 档位会重取，不会被缓存吃掉", async () => {
    reply({ items: [], page: 1, size: 20, total: 0 });
    const visible = renderHook(() => useVideos({ hidden: "visible" }), { wrapper });
    await waitFor(() => expect(visible.result.current.isSuccess).toBe(true));
    const hiddenOnly = renderHook(() => useVideos({ hidden: "hidden" }), { wrapper });
    await waitFor(() => expect(hiddenOnly.result.current.isSuccess).toBe(true));
    // 后端对这两个档位给的是**不同**的行集（墓碑只在 hidden 档出现），
    // 发成同一个 URL 的话"已隐藏"那一档看起来永远是空的。
    expect(seen).toEqual(["GET /api/videos?hidden=visible", "GET /api/videos?hidden=hidden"]);
  });

  it("schema 端点在平台名空着时不发请求", async () => {
    reply({});
    const { result } = renderHook(() => usePlatformSchema(""), { wrapper });
    expect(result.current.fetchStatus).toBe("idle");
    expect(seen).toHaveLength(0);
  });
});

describe("转写稿的 404 是正常态", () => {
  it("还没有稿子 → data 是 null，不是 error", async () => {
    reply({ detail: "video 7 还没有口播稿" }, 404);
    const { result } = renderHook(() => useTranscript(7), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toBeNull();
    expect(result.current.isError).toBe(false);
  });

  it("500（文件读不出来）不能当成「没有稿子」", async () => {
    reply({ detail: "口播稿文件读不出来：transcripts/douyin/7.txt（ENOENT）" }, 500);
    const { result } = renderHook(() => useTranscript(7), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.message).toContain("口播稿文件读不出来");
  });
});

describe("写操作的 URL、请求体与失效范围", () => {
  it("加博主 → POST /api/creators，并让 creators/videos/runs 重取", async () => {
    reply({ id: 1 });
    const { result } = renderHook(() => useAddCreator(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({
        url: "https://www.douyin.com/user/abc",
        platform: "douyin",
        tracking: false,
      });
    });
    expect(seen.at(-1)).toBe("POST /api/creators");
  });

  it('跟踪开关发的是契约里那个字段名 + 真布尔（不是 is_tracking，也不是 "true"）', async () => {
    let body: string | null = null;
    vi.mocked(fetch).mockImplementation(
      async (input: string | URL | Request, init?: RequestInit) => {
        seen.push(`${String(init?.method)} ${String(input)}`);
        body = (init?.body as string | undefined) ?? null;
        return new Response(JSON.stringify({ id: 3 }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      },
    );
    const { result } = renderHook(() => useSetTracking(3), { wrapper });
    await act(async () => {
      await result.current.mutateAsync(false);
    });
    expect(seen.at(-1)).toBe("PATCH /api/creators/3/tracking");
    expect(body).toBe('{"tracking":false}');
  });

  it("取消只是**请求**取消：不发 PUT、路径带 run id", async () => {
    reply({ task_id: "r1", cancelled: true });
    const { result } = renderHook(() => useCancelRun(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync("r1");
    });
    expect(seen.at(-1)).toBe("POST /api/tasks/runs/r1/cancel");
  });

  it("hide 带原因、unhide 不带请求体", async () => {
    const bodies: (string | undefined)[] = [];
    vi.mocked(fetch).mockImplementation(
      async (input: string | URL | Request, init?: RequestInit) => {
        seen.push(`${String(init?.method)} ${String(input)}`);
        bodies.push(init?.body as string | undefined);
        return new Response(JSON.stringify({ id: 9 }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      },
    );
    const hide = renderHook(() => useHideVideo(), { wrapper });
    await act(async () => {
      await hide.result.current.mutateAsync({ id: 9, reason: "广告" });
    });
    const unhide = renderHook(() => useUnhideVideo(), { wrapper });
    await act(async () => {
      await unhide.result.current.mutateAsync(9);
    });
    expect(seen).toContain("PATCH /api/videos/9/hide");
    expect(bodies[0]).toBe('{"reason":"广告"}');
    expect(seen.at(-1)).toBe("PATCH /api/videos/9/unhide");
    expect(bodies[1]).toBeUndefined();
  });

  it("单链接提交 → POST /api/videos/single-link", async () => {
    reply({ task_id: "t9" });
    const { result } = renderHook(() => useSubmitSingleLink(), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({ url: "https://v.douyin.com/abc/" });
    });
    expect(seen.at(-1)).toBe("POST /api/videos/single-link");
  });

  it("改平台配置 → PUT，并让平台列表与任务表重取（关掉的平台要让任务消失）", async () => {
    const invalidate = vi.fn();
    const client = new QueryClient();
    const original = client.invalidateQueries.bind(client);
    client.invalidateQueries = ((query: unknown) => {
      invalidate(query);
      return original(query as never);
    }) as typeof client.invalidateQueries;
    vi.mocked(fetch).mockImplementation(async () => {
      seen.push("PUT /api/platforms/douyin/config");
      return new Response(JSON.stringify({ platform: "douyin", config: {} }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    const { result } = renderHook(() => useUpdatePlatformConfig("douyin"), {
      wrapper: ({ children }) => (
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      ),
    });
    await act(async () => {
      await result.current.mutateAsync({ enabled: false });
    });
    expect(seen.at(-1)).toBe("PUT /api/platforms/douyin/config");
    const keys = invalidate.mock.calls.map(([arg]) =>
      JSON.stringify((arg as { queryKey: unknown }).queryKey),
    );
    expect(keys).toContain('["platforms"]');
    expect(keys).toContain('["tasks"]');
  });

  it("run 任务 → POST /api/tasks/{name}/run", async () => {
    reply({ task_id: "t10" });
    const { result } = renderHook(() => useRunTask("collect"), { wrapper });
    await act(async () => {
      await result.current.mutateAsync({ platform: "douyin" });
    });
    expect(seen.at(-1)).toBe("POST /api/tasks/collect/run");
  });
});

describe("runs 列表按状态筛", () => {
  it("status 进 query 也进 key", async () => {
    reply([]);
    const { result } = renderHook(() => useRuns("running"), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(seen.at(-1)).toBe("GET /api/tasks/runs?status=running&limit=50");
  });
});

describe("json-schema.resolveRef", () => {
  it("解 `$defs` 里的一层，解不到给 null", () => {
    const schema = { $defs: { RateLimitConfig: { title: "RateLimit" } } };
    expect(resolveRef(schema, { $ref: "#/$defs/RateLimitConfig" })).toEqual({
      name: "RateLimitConfig",
      target: { title: "RateLimit" },
    });
    expect(resolveRef(schema, { allOf: [{ $ref: "#/$defs/Missing" }] })).toBeNull();
    expect(resolveRef(schema, { type: "boolean" })).toBeNull();
  });
});
