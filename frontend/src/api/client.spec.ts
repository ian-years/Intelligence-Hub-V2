// @vitest-environment node
import { describe, expect, it, vi } from "vitest";

import { ApiError, api } from "./client";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function withFetch(response: Response): { calls: string[] } {
  const calls: string[] = [];
  vi.stubGlobal("fetch", (input: string | URL | Request) => {
    calls.push(String(input));
    return Promise.resolve(response);
  });
  return { calls };
}

describe("api client", () => {
  it("拼 /api 前缀，并丢掉空参数", async () => {
    const spy = withFetch(jsonResponse(200, []));
    await api.get("/videos", { platform: "douyin", search: "", page: undefined, hidden: false });
    expect(spy.calls[0]).toBe("/api/videos?platform=douyin&hidden=false");
  });

  it("字符串 detail 原样交出去", async () => {
    withFetch(jsonResponse(409, { detail: "该平台下还有 3 位博主" }));
    const error = await api.get("/platforms/douyin").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).detail).toBe("该平台下还有 3 位博主");
    expect((error as ApiError).status).toBe(409);
  });

  it("422 的数组 detail 展开成可读的字段错误", async () => {
    withFetch(
      jsonResponse(422, {
        detail: [
          { loc: ["body", "url"], msg: "value is not a valid http url", type: "value_error" },
          { loc: ["body"], msg: "field required", type: "missing" },
        ],
      }),
    );
    const error = (await api.post("/creators", {}).catch((e: unknown) => e)) as ApiError;
    expect(error.fieldErrors).toEqual(["url: value is not a valid http url", "?: field required"]);
    expect(error.detail).toContain("value is not a valid http url");
  });

  it("响应不是 JSON（反向代理的一屏 HTML）也要给出可行动的原因", async () => {
    vi.stubGlobal("fetch", () =>
      Promise.resolve(
        new Response("<html><body>502 Bad Gateway — upstream refused</body></html>", {
          status: 502,
          headers: { "Content-Type": "text/html" },
        }),
      ),
    );
    const error = (await api.get("/health").catch((e: unknown) => e)) as ApiError;
    expect(error.status).toBe(502);
    expect(error.detail).toContain("502 Bad Gateway");
  });

  it("204 不回 body", async () => {
    withFetch(new Response(null, { status: 204 }));
    await expect(api.patch("/videos/1/unhide")).resolves.toBeUndefined();
  });
});
