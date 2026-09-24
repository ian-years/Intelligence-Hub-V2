// @vitest-environment jsdom
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EVENT_TYPES, type EventType } from "./types";
import { useTaskEvents } from "./useTaskEvents";

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  readonly listeners = new Map<string, ((raw: MessageEvent) => void)[]>();
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((raw: MessageEvent) => void) | null = null;
  closed = false;

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, cb: (raw: MessageEvent) => void): void {
    const list = this.listeners.get(type) ?? [];
    list.push(cb);
    this.listeners.set(type, list);
  }

  removeEventListener(): void {
    /* 本用例不关心退订细节 */
  }

  close(): void {
    this.closed = true;
  }

  emit(type: string, payload: Record<string, unknown>): void {
    const raw = { data: JSON.stringify(payload) } as unknown as MessageEvent;
    for (const cb of this.listeners.get(type) ?? []) cb(raw);
  }

  /** 只有**没有 `event:` 字段**的帧才会走 onmessage —— 后端一帧都没有。 */
  emitUnnamed(payload: Record<string, unknown>): void {
    this.onmessage?.({ data: JSON.stringify(payload) } as unknown as MessageEvent);
  }
}

function event(
  overrides: Partial<{ type: EventType; timestamp: string }> = {},
): Record<string, unknown> {
  return {
    type: overrides.type ?? "task.progress",
    task_id: "t1",
    timestamp: overrides.timestamp ?? "2026-09-23T10:00:00+00:00",
    payload: { progress: 0.5 },
  };
}

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.useRealTimers();
});

describe("useTaskEvents", () => {
  it("逐类型注册监听器：`onmessage` 这条路在后端形状下一次都不会触发", () => {
    const { result } = renderHook(() =>
      useTaskEvents({ source: FakeEventSource as unknown as typeof EventSource }),
    );
    const es = FakeEventSource.instances[0];
    expect(es).toBeDefined();
    expect(Array.from(es?.listeners.keys() ?? []).sort()).toEqual([...EVENT_TYPES].sort());

    // 具名帧进得来。
    act(() => es?.emit("task.progress", event()));
    expect(result.current.events).toHaveLength(1);

    // 未命名帧进不来 —— 因为后端不发那种帧。这条断言是"为什么不能只挂 onmessage"的证据。
    act(() => es?.emitUnnamed(event()));
    expect(result.current.events).toHaveLength(1);
  });

  it("首连不带 since，重连才带 since=<最后一条事件的时间戳>", () => {
    renderHook(() =>
      useTaskEvents({ taskId: "t1", source: FakeEventSource as unknown as typeof EventSource }),
    );
    const first = FakeEventSource.instances[0];
    expect(first?.url).toBe("/api/events?task_id=t1");
    expect(first?.url).not.toContain("since");

    act(() =>
      first?.emit("task.log", event({ type: "task.log", timestamp: "2026-09-23T11:22:33+00:00" })),
    );
    vi.useFakeTimers();
    act(() => {
      first?.onerror?.();
    });
    act(() => {
      vi.advanceTimersByTime(500);
    });
    const second = FakeEventSource.instances[1];
    expect(second?.url).toContain("since=2026-09-23T11%3A22%3A33%2B00%3A00");
    vi.useRealTimers();
  });

  it("断线时状态与原因都要可见，不是继续显示 open", () => {
    vi.useFakeTimers();
    const { result } = renderHook(() =>
      useTaskEvents({ source: FakeEventSource as unknown as typeof EventSource }),
    );
    act(() => {
      FakeEventSource.instances[0]?.onerror?.();
    });
    expect(result.current.status).toBe("reconnecting");
    expect(result.current.error).toContain("事件流断开");
    vi.useRealTimers();
  });

  it("全局流（无 taskId）重连不发 since —— 后端只有 task_id 分支才回放，发了是死参数", () => {
    renderHook(() =>
      useTaskEvents({ source: FakeEventSource as unknown as typeof EventSource }),
    );
    const first = FakeEventSource.instances[0];
    act(() =>
      first?.emit("task.log", event({ type: "task.log", timestamp: "2026-09-23T11:22:33+00:00" })),
    );
    vi.useFakeTimers();
    act(() => {
      first?.onerror?.();
    });
    act(() => {
      vi.advanceTimersByTime(500);
    });
    const second = FakeEventSource.instances[1];
    expect(second?.url).not.toContain("since");
    vi.useRealTimers();
  });

  it("全局流断过线必须说出事件缺失（gap），带 taskId 的流不置位（回放补齐）", () => {
    const global_ = renderHook(() =>
      useTaskEvents({ source: FakeEventSource as unknown as typeof EventSource }),
    );
    const task = renderHook(() =>
      useTaskEvents({ taskId: "t1", source: FakeEventSource as unknown as typeof EventSource }),
    );
    expect(global_.result.current.gap).toBe(false);
    expect(task.result.current.gap).toBe(false);

    // 两条流各自断一次
    act(() => {
      FakeEventSource.instances[0]?.onerror?.();
      FakeEventSource.instances[1]?.onerror?.();
    });
    // 全局流的断线窗口没有回放兜底，事件丢了就是丢了 —— 必须说出来。
    expect(global_.result.current.gap).toBe(true);
    // 任务流由 since 回放补齐，不算缺口。
    expect(task.result.current.gap).toBe(false);
  });

  it("重连成功后撤掉上一轮的断线文案，gap 不随之消失", () => {
    const { result } = renderHook(() =>
      useTaskEvents({ source: FakeEventSource as unknown as typeof EventSource }),
    );
    const first = FakeEventSource.instances[0];
    act(() => {
      first?.onerror?.();
    });
    expect(result.current.error).toContain("事件流断开");
    act(() => {
      first?.onopen?.();
    });
    expect(result.current.error).toBeNull();
    expect(result.current.gap).toBe(true);
  });

  it("没有 EventSource 的环境：明确 offline + 一句人话，而不是静默没有数据", () => {
    const saved = globalThis.EventSource;
    // @ts-expect-error 故意拿掉
    delete globalThis.EventSource;
    try {
      const { result } = renderHook(() => useTaskEvents({}));
      expect(result.current.status).toBe("offline");
      expect(result.current.error).toContain("没有 EventSource");
    } finally {
      globalThis.EventSource = saved;
    }
  });

  it("types 决定注册哪些事件，也进 query string", () => {
    const types: EventType[] = ["video.added", "transcript.ready"];
    renderHook(() =>
      useTaskEvents({ types, source: FakeEventSource as unknown as typeof EventSource }),
    );
    const es = FakeEventSource.instances[0];
    expect(es?.url).toBe("/api/events?types=video.added%2Ctranscript.ready");
    expect(Array.from(es?.listeners.keys() ?? []).sort()).toEqual([
      "transcript.ready",
      "video.added",
    ]);
  });

  it("缓冲区有界（只留最后 500 条）：跑一轮采集能发上千条 task.log", () => {
    const { result } = renderHook(() =>
      useTaskEvents({ source: FakeEventSource as unknown as typeof EventSource }),
    );
    act(() => {
      for (let i = 0; i < 520; i += 1) {
        FakeEventSource.instances[0]?.emit("task.log", event());
      }
    });
    expect(result.current.events).toHaveLength(500);
  });

  it("卸载后关掉连接，重连定时器不再开新连接", () => {
    vi.useFakeTimers();
    const { unmount } = renderHook(() =>
      useTaskEvents({ source: FakeEventSource as unknown as typeof EventSource }),
    );
    const es = FakeEventSource.instances[0];
    unmount();
    expect(es?.closed).toBe(true);
    act(() => {
      es?.onerror?.();
    });
    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(FakeEventSource.instances).toHaveLength(1);
    vi.useRealTimers();
  });
});

describe("EVENT_TYPES ↔ 后端 EventType", () => {
  // 这里不能用 `new URL(相对路径, import.meta.url)`：jsdom 环境下 import.meta.url 是
  // http: URL（node 环境才可以当文件路径用，见 src/styles/tokens.spec.ts 的 pragma）。
  const python = readFileSync(
    resolve(process.cwd(), "../src/intelligence_hub_v2/models/event.py"),
    "utf8",
  );
  const body = python.split("class EventType")[1] ?? "";
  const declared = [...body.matchAll(/=\s*"([a-z_.]+)"/g)].map((m) => m[1] as string);

  it("后端那份枚举读到了（否则下一条是空转）", () => {
    expect(existsSync(resolve(process.cwd(), "../src/intelligence_hub_v2/models/event.py"))).toBe(
      true,
    );
    expect(declared.length).toBeGreaterThanOrEqual(15);
  });

  /** 后端加一个 EventType 而这里漏加，症状是**那种事件静默收不到**（不报错）。 */
  it("两边的名字一字不差", () => {
    expect([...EVENT_TYPES].sort()).toEqual([...declared].sort());
  });
});
