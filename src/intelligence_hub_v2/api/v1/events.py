"""`/api/events` 全局 SSE + 给 `/api/tasks/runs/{id}/events` 复用的事件流装配。

顺序很重要：**先 subscribe、再 replay**（决策与理由见 `event-schema.md §4` 的实施期修订）。
反过来会漏掉那条"任务刚好在你追历史时跑完了"的 `task.finished` —— 漏一条比重一条严重得多，
代价是理论上可能与 replay 重叠一条，V2 事件不带 id、SSE 层不去重，可接受。

V2 服务端**不发 `id:` 字段**（事件 id 是 SQLite 自增，前端用不上），重连靠前端带
`?since=<last_timestamp>` 回来走 replay，而不是 `Last-Event-ID`。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Query
from sse_starlette.sse import EventSourceResponse

from intelligence_hub_v2.api.deps import get_state
from intelligence_hub_v2.models.event import Event, EventType

if TYPE_CHECKING:
    from intelligence_hub_v2.api.deps import AppState

router = APIRouter(tags=["events"])


def _parse_types(raw: str | None) -> set[EventType] | None:
    if not raw:
        return None
    out: set[EventType] = set()
    for raw_token in raw.split(","):
        token = raw_token.strip()
        if not token:
            continue
        try:
            out.add(EventType(token))
        except ValueError:
            continue  # 认不出的类型名忽略，而不是整个流 400（前端版本差正常）
    return out or None


def _format(event: Event) -> dict[str, str]:
    return {"event": event.type.value, "data": event.model_dump_json()}


async def stream_events(
    state: AppState,
    *,
    task_id: str | None,
    types: set[EventType] | None,
    since: datetime | None,
) -> AsyncIterator[dict[str, str]]:
    """把"历史回放 + 实时订阅"缝成一条 SSE 生成器。

    **先 subscribe 再 replay**：这样若 replay 期间来了新事件，也会被缓冲、由后面的实时循环
    补上，不会漏掉那条"任务刚好在你追历史时跑完了"的 `task.finished`。代价是理论上可能与
    replay 重叠一条 —— V2 事件不带 id，SSE 层不去重，可接受（漏一条比重一条严重得多）。

    `finally` 里关订阅：不关就泄漏（订阅者一直挂在总线上，每条事件多投一份）。
    """
    subscription = state.events.subscribe(types=types, task_id=task_id)
    try:
        if task_id is not None:
            # 回放段自己过一遍过滤器：`EventBus.replay()` 的签名里没有 types
            # （`event-schema.md §4`），而"勾了只看 finished 却收到整段 progress"
            # 是用户能直接看出来的错。
            for stored in await state.events.replay(task_id, since=since):
                if types is None or stored.type in types:
                    yield _format(stored)
        async for event in subscription:
            yield _format(event)
    finally:
        aclose = getattr(subscription, "aclose", None)
        if aclose is not None:
            await aclose()


@router.get("/events")
async def global_events(
    state: AppState = Depends(get_state),
    types: str | None = Query(default=None, description="逗号分隔的 EventType"),
    since: datetime | None = Query(default=None),
    task_id: str | None = Query(default=None, description="只看这一次任务（带上才回放历史）"),
) -> EventSourceResponse:
    generator = stream_events(state, task_id=task_id, types=_parse_types(types), since=since)
    return EventSourceResponse(generator)
