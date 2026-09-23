import { useEffect, useRef, useState } from "react";

import {
  EVENT_TYPES,
  isEventType,
  type ConnectionStatus,
  type EventType,
  type StreamEvent,
} from "./types";

const MAX_EVENTS = 500;
const BACKOFF_STEPS_MS = [500, 1000, 2000, 5000, 10000] as const;
const NO_EVENT_SOURCE = "这个环境没有 EventSource，实时流不可用（列表仍可手动刷新）";

export interface UseTaskEventsOptions {
  /** 带上才回放历史（后端语义：`task_id` 决定要不要先把旧事件发一遍）。 */
  taskId?: string;
  /** 不传就是全集。**传数组时请传稳定的引用**（`useMemo` 或常量），
   * 每次渲染新建一个数组会让连接反复重建。 */
  types?: EventType[];
  /** false 时彻底不建连接（页面卸载、抽屉收起）。 */
  enabled?: boolean;
  /** 测试注入用；默认取 `globalThis.EventSource`。 */
  source?: typeof EventSource;
}

export interface TaskEvents {
  events: StreamEvent[];
  status: ConnectionStatus;
  /** 最近一次连接问题的原文。**页面要把它显示出来** ——
   * "看起来在跑但其实断流"是这个仓库的老问题（V1 §1.3 / §7.20）。 */
  error: string | null;
}

/**
 * 订阅 `/api/events`（SSE）。
 *
 * 三件不显然的事，都在代码里：
 * 1. 后端每帧都带 `event: <type>`，而 `EventSource` 只把**没有** `event:` 字段的帧
 *    交给 `onmessage` —— 所以必须逐类型 `addEventListener`，一次 `onmessage` 都不会触发
 *    （`event-schema.md §7` 原来那段样例正是这么写的，因此是错的）。
 * 2. 服务端不发 `id:` 字段，浏览器自带的 `Last-Event-ID` 重连拿不到回放位置；
 *    断线后由这里带 `since=<最后一条事件的 timestamp>` 重开（§7 说的就是这一条）。
 * 3. 重连退避并封顶：后端重启时，无退避的重连等于对着刚起来的进程 DDoS。
 */
export function useTaskEvents(options: UseTaskEventsOptions = {}): TaskEvents {
  const { taskId, types, enabled = true, source } = options;
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [status, setStatus] = useState<ConnectionStatus>("connecting");
  const [error, setError] = useState<string | null>(null);

  const typesKey = types?.join(",") ?? "";
  const lastTimestamp = useRef<string | null>(null);
  // "没有 EventSource" 与 "被关掉" 是**环境事实**，不是异步状态：在渲染期推导，
  // 不在 effect 里同步 setState（那会多一次级联渲染，react-hooks 规则也直接拒）。
  const Ctor = source ?? (globalThis.EventSource as typeof EventSource | undefined);
  const unsupported = enabled && !Ctor;

  useEffect(() => {
    if (!enabled || !Ctor) return;

    let closed = false;
    let es: EventSource | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;

    const open = (): void => {
      const params = new URLSearchParams();
      if (taskId) params.set("task_id", taskId);
      if (typesKey) params.set("types", typesKey);
      // `since` 只在重连时带：首连带它会把刚刚发生的那一段切掉。
      if (attempt > 0 && lastTimestamp.current) params.set("since", lastTimestamp.current);

      es = new Ctor(`/api/events?${params.toString()}`);
      setStatus(attempt === 0 ? "connecting" : "reconnecting");

      const remember = (raw: MessageEvent): void => {
        let parsed: unknown;
        try {
          parsed = JSON.parse(raw.data) as unknown;
        } catch {
          setError("收到一帧解析不出来的事件（不是 JSON）");
          return;
        }
        const event = parsed as Partial<StreamEvent>;
        if (typeof event.type !== "string" || !isEventType(event.type)) return;
        if (typeof event.timestamp === "string") lastTimestamp.current = event.timestamp;
        attempt = 0;
        setError(null);
        setStatus("open");
        setEvents((prev) => {
          const next = [...prev, event as StreamEvent];
          return next.length > MAX_EVENTS ? next.slice(next.length - MAX_EVENTS) : next;
        });
      };

      // 具名事件必须逐个注册（上面第 1 条）。按 `typesKey` 拆而不是闭包读 `types`：
      // 这样"注册了哪些监听器"与"query 里发出去哪些类型"必然是同一个东西。
      const listen: readonly string[] = typesKey ? typesKey.split(",") : EVENT_TYPES;
      for (const type of listen) es?.addEventListener(type, remember);

      es.onopen = () => setStatus("open");
      es.onerror = () => {
        if (closed) return;
        es?.close();
        const wait = BACKOFF_STEPS_MS[Math.min(attempt, BACKOFF_STEPS_MS.length - 1)] ?? 1000;
        attempt += 1;
        setStatus("reconnecting");
        setError(`事件流断开，${String(wait)}ms 后重连（第 ${String(attempt)} 次）`);
        timer = setTimeout(open, wait);
      };
    };

    open();

    return () => {
      closed = true;
      if (timer !== null) clearTimeout(timer);
      es?.close();
    };
  }, [taskId, typesKey, enabled, Ctor]);

  return {
    events,
    status: unsupported ? "offline" : status,
    error: unsupported ? NO_EVENT_SOURCE : error,
  };
}
