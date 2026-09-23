// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ApiError } from "@/api/client";
import { QueryState, type QueryGate } from "@/components/shared/QueryState";

/**
 * 这一份是 `Preflight` / `Sidebar` 那条纪律的通用版（经验 36）：
 * 四种情况**互斥**，任何一种都不许让另一种的文案露出来。
 *
 * 所以每条用例都是"有 A" + "没有 B/C/D" 的四段式 —— 只断言"有 A"的那一半，
 * 挡不住一个把所有状态都渲染出来的实现（那正是这种组件最容易写成的样子）。
 */
function gate(over: Partial<QueryGate> = {}): QueryGate {
  return { isPaused: false, isError: false, error: null, data: undefined, ...over };
}

function show(g: QueryGate): void {
  render(
    <QueryState gate={g} subject="作品流">
      {(data) => <p>正文：{(data as string[]).join(" / ")}</p>}
    </QueryState>,
  );
}

const RESULT = "正文：a / b";
const PAUSED = /补发被挂起/;
const UNREADABLE = /读不到作品流/;
const LOADING = /读取中/;

afterEach(() => {
  cleanup();
});

describe("QueryState 的四态", () => {
  it("有数据：只画正文，不画任何状态文案", () => {
    show(gate({ data: ["a", "b"] }));
    expect(screen.getByText(RESULT)).toBeTruthy();
    expect(screen.queryByText(PAUSED)).toBeNull();
    expect(screen.queryByText(UNREADABLE)).toBeNull();
    expect(screen.queryByText(LOADING)).toBeNull();
  });

  it("补发被挂起：说清楚是挂起，并且不假装环境没问题", () => {
    show(gate({ isPaused: true, data: ["a", "b"] }));
    expect(screen.getByText(PAUSED)).toBeTruthy();
    expect(screen.getByText(/不是「作品流没有问题」/)).toBeTruthy();
    expect(screen.queryByText(RESULT)).toBeNull();
    expect(screen.queryByText(LOADING)).toBeNull();
    expect(screen.queryByText(UNREADABLE)).toBeNull();
  });

  it("读不到：给 ApiError 的原文，不给正文也不给『读取中』", () => {
    show(
      gate({
        isError: true,
        error: new ApiError(500, "后端 500：主库被另一个进程锁住"),
      }),
    );
    expect(screen.getByText(UNREADABLE)).toBeTruthy();
    expect(screen.getByText("后端 500：主库被另一个进程锁住")).toBeTruthy();
    expect(screen.queryByText(RESULT)).toBeNull();
    expect(screen.queryByText(PAUSED)).toBeNull();
    expect(screen.queryByText(LOADING)).toBeNull();
  });

  it("非 ApiError 的错误也要有原文，不许只剩『读不到』三个字", () => {
    show(gate({ isError: true, error: new TypeError("Failed to fetch") }));
    expect(screen.getByText("Failed to fetch")).toBeTruthy();
  });

  it("还没有结论：只有『读取中』，且明确说了这里不给状态", () => {
    show(gate({}));
    expect(screen.getByText(LOADING)).toBeTruthy();
    expect(screen.getByText(/刻意不显示任何状态/)).toBeTruthy();
    expect(screen.queryByText(RESULT)).toBeNull();
    expect(screen.queryByText(PAUSED)).toBeNull();
    expect(screen.queryByText(UNREADABLE)).toBeNull();
  });
});
