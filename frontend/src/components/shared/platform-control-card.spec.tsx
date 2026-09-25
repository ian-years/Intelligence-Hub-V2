import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { PlatformControlCard } from "./PlatformControlCard";

import type { PlatformControlStatus } from "@/api/hooks/useConfig";
import type { PlatformSummary } from "@/api/hooks/useConfig";

function control(over: Partial<PlatformControlStatus> = {}): PlatformControlStatus {
  return { enabled: true, shadowed_by_env: [], ...over };
}

/** 四行 = 四个态各一行。`enabled` 与 `availability` 的组合是**后端算出来的**，
 *  这里照实摆：master_off 那一行它自己开着（enabled: true），own_off 那一行关着。 */
function rows(): { platforms: PlatformSummary[]; master_enabled: boolean } {
  const base = { implemented: true, health_status: null, health_checked_at: null };
  return {
    platforms: [
      {
        ...base,
        name: "douyin",
        display_name: "抖音",
        enabled: true,
        availability: "available",
      },
      {
        ...base,
        name: "bilibili",
        display_name: "B站",
        enabled: false,
        availability: "own_off",
      },
      {
        ...base,
        name: "xiaohongshu",
        display_name: "小红书",
        enabled: true,
        availability: "master_off",
      },
      {
        ...base,
        name: "youtube",
        display_name: "YouTube",
        enabled: false,
        availability: "absent",
      },
    ],
    master_enabled: false,
  };
}

let calls: { url: string; method: string; body: string | null }[] = [];

function serve(
  status: PlatformControlStatus,
  opts: { failList?: boolean; failPut?: number } = {},
): void {
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    const url = String(input);
    const method = String(init?.method ?? "GET");
    calls.push({ url, method, body: (init?.body as string | undefined) ?? null });
    if (method === "PUT") {
      return json(opts.failPut ?? 200, {
        platform_control: control({ ...status, enabled: status.enabled }),
        changed_fields: ["platform_control.enabled"],
        requires_restart: false,
      });
    }
    // 清单坏 = HTTP 层坏（react-query 的 isError 只认状态码）。
    // 拿一个 200 + `{detail: …}` 当"读不到"是假场景：那种响应形状是对的、
    // 只是内容像错误，界面会走"清单是空的"那条路而不是"读不到"。
    if (url.includes("/platforms")) {
      return opts.failList ? json(500, { detail: "读不到平台清单" }) : json(200, rows());
    }
    return json(200, status);
  });
}

function json(statusCode: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: statusCode,
    headers: { "Content-Type": "application/json" },
  });
}

function renderCard(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: 0 } } });
  render(
    <QueryClientProvider client={client}>
      <PlatformControlCard />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  calls = [];
  vi.stubGlobal("fetch", vi.fn());
});

describe("PlatformControlCard", () => {
  it("四行的状态一句话各不同，且被总闸盖住的那一行不说「已关闭」", async () => {
    serve(control({ enabled: false }));
    renderCard();
    const list = await screen.findByRole("list", { name: "各家当前状态" });

    // 判据取自"四行四种说法"这个关系，而不是四串字面量：
    // 把两句合成一句（那正是这次要防的回退）这里会当场对不上。
    const badges = within(list).getAllByRole("listitem");
    expect(badges).toHaveLength(4);
    const texts = badges.map((b) => b.textContent ?? "");
    expect(new Set(texts).size).toBe(4);
    expect(texts.find((t) => t.includes("小红书"))).toMatch(/总闸/);
    expect(texts.find((t) => t.includes("小红书"))).not.toMatch(/已关闭/);
    expect(texts.find((t) => t.includes("B站"))).toMatch(/已关闭/);
  });

  it("四行是只读的：这一格不放每家的开关", async () => {
    // 能点就等于把"总闸 + AND"重新解释成"批量写四次"，
    // 而界面上分不出"你自己关的"与"被总闸盖住的"这件事会立刻回来。
    serve(control({ enabled: false }));
    renderCard();
    const list = await screen.findByRole("list", { name: "各家当前状态" });
    expect(within(list).queryAllByRole("switch")).toHaveLength(0);
    // 而总闸自己那一个是能点的（否则这条用例会因为"整页都没有 switch"而假绿）
    expect(screen.getByRole("switch", { name: "平台总闸" })).toBeTruthy();
  });

  it("关掉时说清停到什么范围，并承诺不动在跑的那一次", async () => {
    serve(control({ enabled: false }));
    renderCard();
    const toggle = await screen.findByRole("switch", { name: "平台总闸" });
    expect(toggle.getAttribute("aria-checked")).toBe("false");
    const text = document.body.textContent ?? "";
    expect(text).toMatch(/已经在跑的那一次不打断/);
    expect(text).toMatch(/定时一条都不排/);
    expect(text).toMatch(/按链接收录也会被拒/);
  });

  it("开着的时候不出现那句「一律不可用」", async () => {
    // 反向那一半必须也钉住：只测"关了会怎么说"的用例，对一个
    // "无论开关状态都印同一段话"的实现也是绿的。
    serve(control({ enabled: true }));
    renderCard();
    await screen.findByRole("switch", { name: "平台总闸" });
    expect(document.body.textContent).not.toMatch(/一律不可用/);
    expect(document.body.textContent).toMatch(/四家都可以提交采集/);
  });

  it("点一下 PUT 的是总闸那一个端点，body 只有 enabled", async () => {
    serve(control({ enabled: true }));
    renderCard();
    const toggle = await screen.findByRole("switch", { name: "平台总闸" });
    await userEvent.click(toggle);
    await waitFor(() => {
      const put = calls.find((c) => c.method === "PUT");
      expect(put?.url).toContain("/platform-control");
      expect(put?.body).toBe('{"enabled":false}');
    });
    // 保存成功要落一句实话（不是只有开关动了）
    expect(await screen.findByText(/已写盘并生效/)).toBeTruthy();
  });

  it("环境变量压着时逐键点名「下一次启动会盖回去」", async () => {
    serve(control({ enabled: true, shadowed_by_env: ["enabled"] }));
    renderCard();
    const warning = await screen.findByText(/下一次启动会盖掉/);
    expect(warning.textContent).toMatch(/platform_control\.enabled/);
    // 没说这一句的话，症状是"我明明关掉了，重启后又开始采"，
    // 而优先级 `yaml < env` 没有人会想到去查一个看不见的环境变量。
  });

  it("平台清单读不到时四行说读不到，而不是留一个空列表", async () => {
    // 空的 `platforms` 与"四家都不在名单里"在界面上是同一个样子 —— 后者是假的。
    serve(control({ enabled: false }), { failList: true });
    renderCard();
    const notice = await screen.findByText(/读不到平台清单/);
    expect(notice.textContent).toContain("读不到平台清单");
    // 一句"读不到"占掉的是四行的位置：不许既说读不到又画出一张空列表
    expect(screen.queryByRole("list", { name: "各家当前状态" })).toBeNull();
    // 而总闸那一位自己照样给结论（两个查询各管各的，不许一个坏了拖累另一个）
    expect(screen.getByRole("switch", { name: "平台总闸" })).toBeTruthy();
  });

  it("总闸读不到时不画任何结论", async () => {
    serve(control({ enabled: false }));
    vi.mocked(fetch).mockImplementation(async (input) =>
      String(input).includes("/platform-control")
        ? json(500, { detail: "配置读不出来" })
        : json(200, rows()),
    );
    renderCard();
    expect(await screen.findByText(/读不到平台总闸/)).toBeTruthy();
    expect(screen.queryByRole("switch", { name: "平台总闸" })).toBeNull();
  });
});
