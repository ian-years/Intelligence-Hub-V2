import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ScheduleCard } from "./ScheduleCard";

import type { ScheduleStatus } from "@/api/hooks/useSchedule";

/** 一份"什么都对"的现状：cron 开着、douyin 排上了、调度器在跑、没有 env 遮挡。
 * 下面的用例都从这一份**只改一处**派生 —— 一次改两处就测不出是哪一处在起作用。 */
function status(over: Partial<ScheduleStatus> = {}): ScheduleStatus {
  return {
    scheduler_enabled: true,
    timezone: "Asia/Shanghai",
    collect_cron: "0 8 * * *",
    collect_platforms: ["douyin"],
    effective_platforms: ["douyin"],
    skipped_platforms: [],
    collect_limit: 5,
    jobs: [{ id: "collect:douyin", platform: "douyin", next_run_time: NEXT_RUN }],
    scheduler_running: true,
    shadowed_by_env: [],
    ...over,
  };
}

const NEXT_RUN = "2026-09-25T08:00:00+08:00";

let calls: { url: string; method: string; body: string | null }[] = [];

function serve(current: ScheduleStatus, opts: { failPut?: number } = {}): void {
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    const url = String(input);
    const method = String(init?.method ?? "GET");
    calls.push({ url, method, body: (init?.body as string | undefined) ?? null });
    if (method === "PUT" && url.includes("/schedule")) {
      const code = opts.failPut ?? 200;
      return json(code, {
        schedule: current,
        changed_fields: ["collect_cron"],
        requires_restart: false,
      });
    }
    if (method === "POST" && url.includes("run-now")) {
      return json(202, { task_id: "t1", task_name: "douyin_collect", platform: "douyin" });
    }
    return json(200, current);
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
      <ScheduleCard />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
  calls = [];
});

describe("定时采集那块卡片", () => {
  it("没改任何东西时保存按钮是禁用的，改 cron 之后才可点", async () => {
    serve(status());
    renderCard();
    const box = await screen.findByDisplayValue("0 8 * * *");
    const save = screen.getByRole("button", { name: "保存定时" });
    expect(save.hasAttribute("disabled")).toBe(true);

    await userEvent.clear(box);
    await userEvent.type(box, "30 6 * * *");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "保存定时" }).hasAttribute("disabled")).toBe(false),
    );
  });

  it("PUT 的请求体只带改过的那个键", async () => {
    serve(status());
    renderCard();
    const box = await screen.findByDisplayValue("0 8 * * *");
    await userEvent.clear(box);
    await userEvent.type(box, "30 6 * * *");
    await userEvent.click(screen.getByRole("button", { name: "保存定时" }));

    await waitFor(() => expect(calls.some((call) => call.method === "PUT")).toBe(true));
    const put = calls.find((call) => call.method === "PUT");
    const sent = JSON.parse(put?.body ?? "{}") as Record<string, unknown>;
    // 这一条守的是"改一个字段顺手把整份表写一遍"：没动的 limit / platforms 不许出现在请求体里。
    expect(sent).toEqual({ collect_cron: "30 6 * * *" });
  });

  it("cron 输入框清空 = 关掉定时，交回的是 null 而不是空串", async () => {
    serve(status());
    renderCard();
    // 从"开着"清成"空"才算一次真实的关掉：初值本来就是空的话根本没有改动可提交。
    const box = await screen.findByDisplayValue("0 8 * * *");
    await userEvent.clear(box);
    await userEvent.click(screen.getByRole("button", { name: "保存定时" }));

    await waitFor(() => {
      const sent = JSON.parse(
        (calls.find((call) => call.method === "PUT")?.body ?? "{}") as string,
      ) as Record<string, unknown>;
      expect(sent).toEqual({ collect_cron: null });
    });
  });

  it("调度器没在跑时，'已保存'必须和'要重启才排上'一起出现", async () => {
    serve(status({ scheduler_running: false, jobs: [] }));
    renderCard();
    const box = await screen.findByDisplayValue("0 8 * * *");
    await userEvent.clear(box);
    await userEvent.type(box, "15 9 * * *");
    await userEvent.click(screen.getByRole("button", { name: "保存定时" }));

    // 只报"已保存"就是那句"看起来在跑"的历史问题：这里断言两句话**同时**在。
    await screen.findByText(/但调度器没在跑/);
    expect(screen.getByText(/下一次启动才生效/)).toBeTruthy();
  });

  it("被环境变量盖住的键要逐一点名", async () => {
    serve(status({ shadowed_by_env: ["collect_cron"] }));
    renderCard();
    const line = await screen.findByText(/环境变量里也设着/);
    expect(line.textContent).toContain("collect_cron");
  });

  it("没排在调度器上的平台说'没排上'，不替它编一个下次时间", async () => {
    serve(status({ jobs: [], effective_platforms: ["douyin", "bilibili"] }));
    renderCard();
    await screen.findByText(/跑哪些平台/);
    const labels = await screen.findAllByText("没排上");
    expect(labels).toHaveLength(2);
  });

  it("读不到排期时不渲染任何结论", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ detail: "后端没起" }), {
        status: 500,
        headers: { "Content-Type": "application/json" },
      }),
    );
    renderCard();
    await screen.findByText(/读不到定时采集/);
    expect(screen.queryByRole("button", { name: "保存定时" })).toBeNull();
  });

  it("保存失败留下草稿并给出原因，不把表单刷回初值", async () => {
    serve(status(), { failPut: 422 });
    renderCard();
    const box = await screen.findByDisplayValue("0 8 * * *");
    await userEvent.clear(box);
    await userEvent.type(box, "99 99 * * *");
    await userEvent.click(screen.getByRole("button", { name: "保存定时" }));

    await screen.findByText(/保存失败/);
    expect(screen.getByDisplayValue("99 99 * * *")).toBeTruthy();
  });

  it("「立即跑一次」POST 的是那个平台，并且回执只说已排队", async () => {
    serve(status());
    renderCard();
    await screen.findByText(/跑哪些平台/);
    const buttons = screen.getAllByRole("button", { name: "立即跑一次" });
    const first = buttons[0];
    if (!first) throw new Error("一个「立即跑一次」都没有：这一页没有平台可跑");
    await userEvent.click(first);

    await screen.findByText(/已排队 douyin_collect/);
    const post = calls.find((call) => call.method === "POST");
    expect(post?.url).toContain("/schedule/run-now");
    expect(JSON.parse(post?.body ?? "{}")).toEqual({ platform: "douyin" });
  });
});
