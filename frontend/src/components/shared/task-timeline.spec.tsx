// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TaskTimeline } from "@/components/shared/TaskTimeline";
import { RUN_STATUS_META } from "@/lib/run-status";
import { makeRun } from "@/test/fixtures";

/**
 * 时间线是总览页与任务页共用的那一块，所以"状态 → 文案"只许有一处。
 *
 * 要紧的一条是 **`running` 不许长得像成功**：把在跑的任务涂成绿色是
 * "看起来在跑"的 UI 版（V1 的老毛病，`AGENTS.md §1.3`）。
 */
afterEach(() => {
  cleanup();
});

describe("TaskTimeline", () => {
  it("一条运行一行：行数由传进去的数组决定，不是写死的", () => {
    // 任务名各不同：三条都用同一个名字的话，`getByText` 会因为多个命中而抛，
    // 那条"失败"看起来像组件坏了，其实是夹具没区分开。
    const runs = [
      makeRun({ id: "a", task_name: "douyin_collect" }),
      makeRun({ id: "b", task_name: "postprocess" }),
      makeRun({ id: "c", task_name: "preflight", status: "running" }),
    ];
    render(<TaskTimeline runs={runs} />);
    expect(screen.getAllByRole("listitem")).toHaveLength(runs.length);
    for (const run of runs) {
      expect(screen.getByText(run.task_name)).toBeTruthy();
    }
  });

  it("零条：说「还没有跑过任务」，而不是画一个空列表", () => {
    render(<TaskTimeline runs={[]} empty="（一条都没有）" />);
    expect(screen.getByText("（一条都没有）")).toBeTruthy();
    expect(screen.queryAllByRole("listitem")).toHaveLength(0);
  });

  it("在跑的那行显示进度百分比，而且不出现任何终态文案", () => {
    render(<TaskTimeline runs={[makeRun({ status: "running", progress: 0.4 })]} />);
    expect(screen.getByText("40%")).toBeTruthy();
    expect(screen.getByText(RUN_STATUS_META.running.label)).toBeTruthy();
    for (const status of ["success", "partial", "failed", "timeout", "cancelled"]) {
      expect(
        screen.queryByText(RUN_STATUS_META[status as keyof typeof RUN_STATUS_META].label),
      ).toBeNull();
    }
  });

  it("只有 running 才给取消按钮：终态的取消点了后端只会回一句拒绝", () => {
    const onCancel = vi.fn();
    render(
      <TaskTimeline
        runs={[makeRun({ id: "r1", status: "running" }), makeRun({ id: "r2", status: "success" })]}
        onCancel={onCancel}
      />,
    );
    const buttons = screen.getAllByRole("button");
    expect(buttons).toHaveLength(1);
    buttons[0]?.click();
    // 回调拿到的是那一行本身，不是"最后一条" —— 绑错就等于取消了别的任务
    expect(onCancel.mock.calls[0]?.[0]?.id).toBe("r1");
  });

  it("失败原文一字不动地打出来：错误文本是这一页唯一能搜的东西", () => {
    const text = "ConnectionRefusedError: 拒绝连接 127.0.0.1:3457";
    render(<TaskTimeline runs={[makeRun({ status: "failed", error_text: text })]} />);
    expect(screen.getByText(text)).toBeTruthy();
  });

  it("终态行给起止两端，在跑的只给开始时间", () => {
    render(
      <TaskTimeline
        runs={[
          makeRun({
            status: "success",
            started_at: "2026-09-23T00:00:00+00:00",
            ended_at: "2026-09-23T00:05:00+00:00",
          }),
        ]}
      />,
    );
    expect(screen.getByText(/→/)).toBeTruthy();
    cleanup();
    render(
      <TaskTimeline
        runs={[
          makeRun({ status: "running", ended_at: null, started_at: "2026-09-23T00:00:00+00:00" }),
        ]}
      />,
    );
    expect(screen.queryByText(/→/)).toBeNull();
  });

  it("契约之外冒出来的状态：原样打出来，不折叠成「未知状态」", () => {
    const weird = makeRun({ id: "r9" });
    // 后端加一个枚举值时前端没跟上：这一格必须说实话，好让人去加映射
    (weird as { status: string }).status = "queued";
    render(<TaskTimeline runs={[weird]} />);
    expect(screen.getByText("queued")).toBeTruthy();
    expect(screen.queryByText(/未知/)).toBeNull();
  });
});
