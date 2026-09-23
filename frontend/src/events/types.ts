/** 与 `intelligence_hub_v2/models/event.py` 的 `EventType` 同一份清单。
 *
 * 为什么前端要自己列一份：SSE 的每一帧都带 `event: <type>`，而 `EventSource`
 * **只会把没有 `event:` 字段的帧交给 `onmessage`** —— 具名事件必须逐个
 * `addEventListener`。所以这份清单直接决定"哪些事件收得到"：
 * 后端加一个 EventType 而这里漏加，症状是那种事件**静默不到**（不是报错）。
 * 由 `src/events/useTaskEvents.spec.ts` 拿 Python 源码逐字核。 */
export const EVENT_TYPES = [
  "task.started",
  "task.progress",
  "task.log",
  "task.finished",
  "task.failed",
  "task.cancelled",
  "manifest.written",
  "platform.health_changed",
  "config.changed",
  "creator.added",
  "creator.updated",
  "video.added",
  "video.hidden",
  "video.unhidden",
  "transcript.ready",
] as const;

export type EventType = (typeof EVENT_TYPES)[number];

/** `event-schema.md §3` 的 `Event`。`payload` 的按类型判别联合在 §3.1 那一节，
 * 页面按 `type` 收窄后再读具体字段（这里保持开放，避免 15 个接口在 UI 层各写一遍）。 */
export interface StreamEvent {
  type: EventType;
  task_id: string | null;
  /** ISO 8601，UTC。重连时作为 `since` 交回后端做历史回放。 */
  timestamp: string;
  payload: Record<string, unknown>;
}

export type ConnectionStatus = "connecting" | "open" | "reconnecting" | "offline";

export function isEventType(value: string): value is EventType {
  return (EVENT_TYPES as readonly string[]).includes(value);
}
