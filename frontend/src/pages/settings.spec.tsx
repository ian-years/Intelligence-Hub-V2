import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { Settings } from "./Settings";

const schema = {
  title: "DouyinConfig",
  "ui:order": ["enabled"],
  properties: {
    enabled: { type: "boolean", title: "Enabled", default: true },
    display_name: { type: "string", title: "Display Name", default: "抖音" },
    use_cdp_bridge: {
      type: "boolean",
      title: "Use Cdp Bridge",
      default: true,
      description: "由 capabilities.needs_browser 决定，不受本字段控制",
      "ui:hidden": true,
    },
    media_strategy: {
      type: "string",
      title: "Media Strategy",
      default: "yt_dlp_with_fallback",
      "ui:hidden": true,
    },
    videos_per_creator: { type: "integer", title: "Videos Per Creator", default: 30, minimum: 1 },
    advanced: { $ref: "#/$defs/Adv", title: "Advanced", "ui:advanced": true },
  },
  $defs: {
    Adv: {
      title: "Adv",
      properties: {
        retry_max: { type: "integer", title: "Retry Max", default: 3, "ui:hidden": true },
        request_timeout_seconds: { type: "integer", title: "Request Timeout", default: 30 },
      },
    },
  },
};

const current = {
  platform: "douyin",
  config: {
    enabled: true,
    display_name: "抖音",
    use_cdp_bridge: true,
    media_strategy: "yt_dlp_with_fallback",
    videos_per_creator: 30,
    advanced: { retry_max: 3, request_timeout_seconds: 30 },
  },
  health: { status: "ok", checked_at: "2026-09-23T10:00:00+00:00" },
};

const platforms = {
  platforms: [
    {
      name: "douyin",
      display_name: "抖音",
      enabled: true,
      availability: "available",
      implemented: true,
    },
  ],
  // 根上这一位与下面 `platformControl` 那份是同一个事实的两个出口
  // （清单里每一行的 `availability` 已经把它算进去了）。
  master_enabled: true,
};

/** 总闸那一格的现状（ADR-0025）。Settings 页现在挂着 `PlatformControlCard`，
 *  这一份不打桩的话，`ok()` 的兜底会把**平台清单**当成总闸状态喂进去，
 *  于是 `shadowed_by_env.length` 当场炸 —— 那一炸看起来像实现的错，其实是桩没打全。 */
const platformControl = { enabled: true, shadowed_by_env: [] };

/** 定时采集那块卡片的现状。Settings 页现在挂着 `ScheduleCard`，
 * 这一份不打桩的话，`ok()` 的兜底分支会把**平台清单**当成排期数据喂进去，
 * 于是这一页上每条既有用例都在测一份类型不对的响应 —— 红不红全看运气。 */
const schedule = {
  scheduler_enabled: true,
  timezone: "Asia/Shanghai",
  collect_cron: "0 8 * * *",
  collect_platforms: ["douyin"],
  effective_platforms: ["douyin"],
  skipped_platforms: [],
  collect_limit: 5,
  jobs: [
    {
      id: "collect:douyin",
      platform: "douyin",
      next_run_time: "2026-09-25T08:00:00+08:00",
    },
  ],
  scheduler_running: true,
  master_enabled: true,
  shadowed_by_env: [],
};

function serve(handler: (url: string, method: string) => [number, unknown]): void {
  vi.mocked(fetch).mockImplementation(async (input: string | URL | Request, init?: RequestInit) => {
    const method = String(init?.method ?? "GET");
    const [status, body] = handler(String(input), method);
    const url = String(input);
    if (url.includes("/config") && method === "PUT") {
      // 只记录请求体；响应仍然由 handler 决定 —— 否则"保存失败"那条测的是别的东西。
      lastPut = { url, body: String(init?.body) };
    }
    const responseBody =
      method !== "PUT"
        ? body
        : status === 200
          ? url.includes("/schedule")
            ? {
                schedule,
                changed_fields: ["collect_cron"],
                requires_restart: false,
              }
            : url.includes("/platform-control")
              ? {
                  platform_control: { ...platformControl, enabled: false },
                  changed_fields: ["platform_control.enabled"],
                  requires_restart: false,
                }
              : {
                  platform: "douyin",
                  config: current.config,
                  changed_fields: [],
                  requires_restart: false,
                }
          : body;
    return new Response(JSON.stringify(responseBody), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  });
}

let lastPut: { url: string; body: string } | null = null;

function renderPage(): void {
  const client = new QueryClient({ defaultOptions: { queries: { retry: 0 } } });
  render(
    <QueryClientProvider client={client}>
      <Settings />
    </QueryClientProvider>,
  );
}

const ok = (url: string): [number, unknown] => {
  if (url.includes("/schema")) return [200, schema];
  if (url.includes("/config")) return [200, current];
  if (url.includes("/schedule")) return [200, schedule];
  // 总闸那一格（ADR-0025）。必须排在这一句的**兜底之前**：`ok()` 的 else 返回平台清单，
  // 那份形状里没有 `shadowed_by_env`，卡片读它就会炸在 `.length` 上 ——
  // 看起来是实现坏了，其实是桩没打全。
  if (url.includes("/platform-control")) return [200, platformControl];
  return [200, platforms];
};

/** 保存失败的那条：GET 一切正常，只有 PUT 回 422 —— 否则测的是"读不到"。 */
const putFails = (url: string, method: string): [number, unknown] =>
  method === "PUT"
    ? [422, { detail: "videos_per_creator: greater than 1, less than 200" }]
    : ok(url);

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
  lastPut = null;
});

describe("Settings 页", () => {
  it("渲染可见字段，隐藏的字段不出现，但要说有几项被隐藏", async () => {
    serve(ok);
    renderPage();
    await screen.findByDisplayValue("抖音");
    expect(screen.queryByText("Use Cdp Bridge")).toBeNull();
    expect(screen.queryByText("Media Strategy")).toBeNull();
    expect(screen.getByText(/3 项后端不实现/)).toBeTruthy();
  });

  it("折叠组默认收起，点开才看得到里面的字段", async () => {
    serve(ok);
    renderPage();
    await screen.findByDisplayValue("抖音");
    expect(screen.queryByText("Request Timeout")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /高级/ }));
    expect(screen.getByText("Request Timeout")).toBeTruthy();
  });

  it("清空数字框是空的，不会弹回默认值（改了再打字才不会变成 3045）", async () => {
    serve(ok);
    renderPage();
    const input = (await screen.findByDisplayValue("30")) as HTMLInputElement;
    await userEvent.clear(input);
    // 空 number 输入用 toHaveValue 会拿到 null（jest-dom 的语义），直接读 .value 最不含糊。
    expect(input.value).toBe("");
    await userEvent.type(input, "45");
    expect(input.value).toBe("45");
  });

  /** 这条是这一页最要紧的断言：`PUT` 收的是整份配置，隐藏字段少带一个就是覆盖用户机器上的值。 */
  it("保存时 PUT 交回整份配置，隐藏字段原样带回", async () => {
    serve(ok);
    renderPage();
    await screen.findByDisplayValue("抖音");
    // 拿住元素再改：受控输入清空后按"显示值为 30"去查本来就查不到（那正是上一条修好的行为）。
    const input = (await screen.findByDisplayValue("30")) as HTMLInputElement;
    await userEvent.clear(input);
    await userEvent.type(input, "45");
    await userEvent.click(screen.getByRole("button", { name: /^保存$/ }));
    await waitFor(() => expect(lastPut).not.toBeNull());
    expect(lastPut?.url).toBe("/api/platforms/douyin/config");
    const sent = JSON.parse(String(lastPut?.body)) as typeof current.config;
    expect(sent.videos_per_creator).toBe(45);
    expect(sent.use_cdp_bridge).toBe(true);
    expect(sent.media_strategy).toBe("yt_dlp_with_fallback");
    expect(sent.advanced).toEqual({ retry_max: 3, request_timeout_seconds: 30 });
    expect(typeof sent.videos_per_creator).toBe("number");
  });

  it("保存成功后草稿清空：不再同时挂着「已写盘」和「有 N 处未保存的改动」", async () => {
    // review P1：useUpdatePlatformConfig 的 onSuccess 只 invalidate 了查询，
    // 组件的 draft 从未清空 —— 保存成功后按钮仍可点、提示仍在。
    // 替身的 PUT 回的是旧配置（30），所以 invalidate 重取后表单显示 30：
    // 草稿若没清，这里会顶着 45 并继续说"有 1 处未保存的改动"。
    serve(ok);
    renderPage();
    await screen.findByDisplayValue("抖音");
    const input = (await screen.findByDisplayValue("30")) as HTMLInputElement;
    await userEvent.clear(input);
    await userEvent.type(input, "45");
    await userEvent.click(screen.getByRole("button", { name: /^保存$/ }));
    expect(await screen.findByText(/已写盘并热加载/)).toBeTruthy();
    expect(await screen.findByText("没有改动")).toBeTruthy();
    expect(screen.queryByText(/未保存的改动/)).toBeNull();
    expect(((await screen.findByDisplayValue("30")) as HTMLInputElement).value).toBe("30");
  });

  it("后端拒绝时显示原因，不清空表单", async () => {
    serve(putFails);
    renderPage();
    const input = (await screen.findByDisplayValue("30")) as HTMLInputElement;
    // 没有改动时保存按钮是禁用的（"没有改动"那句是真的），所以先改一笔。
    await userEvent.clear(input);
    await userEvent.type(input, "999");
    await userEvent.click(screen.getByRole("button", { name: /^保存$/ }));
    expect(await screen.findByText(/greater than 1, less than 200/)).toBeTruthy();
    expect(input.value).toBe("999");
  });

  it("读不到配置时说清楚，不给一张空表单", async () => {
    serve((url) => {
      if (url.includes("/config")) return [500, { detail: "配置层还没起来" }];
      if (url.includes("/schema")) return [200, schema];
      // 这一句必须在兜底之前：少了它，总闸那一格收到的是**平台清单**那份形状，
      // 于是 `shadowed_by_env.length` 炸在这里 —— 报出来的栈指向实现，
      // 而坏的是这个桩。
      if (url.includes("/platform-control")) return [200, platformControl];
      return [200, platforms];
    });
    renderPage();
    expect(await screen.findByText(/读不到/)).toBeTruthy();
    expect(screen.getByText(/配置层还没起来/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^保存$/ })).toBeNull();
  });
});
