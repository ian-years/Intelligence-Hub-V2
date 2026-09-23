"""`api/v1/events.py`：SSE 类型解析 + "回放 + 实时" 事件流装配（不真开 HTTP 流，避免挂起）。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from intelligence_hub_v2.api.deps import AppState
from intelligence_hub_v2.api.v1.events import _parse_types, stream_events
from intelligence_hub_v2.models.event import EventType

pytestmark = pytest.mark.integration


def test_parse_types_none_and_empty() -> None:
    assert _parse_types(None) is None
    assert _parse_types("") is None
    assert _parse_types("  ,, ") is None


def test_parse_types_keeps_valid_drops_unknown() -> None:
    got = _parse_types("task.started,bogus.type,video.added")
    assert got == {EventType.TASK_STARTED, EventType.VIDEO_ADDED}


async def _drain(
    app_state: AppState, *, task_id: str | None, types: set[EventType] | None
) -> list[dict[str, str]]:
    """跑完一条流到"回放完毕、没有活事件"为止（不开真 HTTP，避免挂起）。"""
    collected: list[dict[str, str]] = []
    agen = stream_events(app_state, task_id=task_id, types=types, since=None)
    while True:
        try:
            collected.append(await asyncio.wait_for(agen.__anext__(), timeout=0.5))
        except (TimeoutError, StopAsyncIteration):
            break
    await agen.aclose()
    return collected


async def test_global_stream_declares_task_id_in_openapi(client: httpx.AsyncClient) -> None:
    """`event-schema.md §5.2` 写着单任务流「等价于 `/api/events?task_id={id}`」，
    §7 的 `useTaskEvents` 示例就是那么调的。路由不吃 `task_id` 的那一刻，照 spec 写的
    前端 hook 拿到的是**全平台 firehose 且完全没有历史回放** —— 而 TS 类型正是从
    OpenAPI 生成的，所以这里钉参数表而不是钉实现。
    """
    spec = (await client.get("/openapi.json")).json()
    params = {item["name"] for item in spec["paths"]["/api/events"]["get"]["parameters"]}
    assert {"task_id", "types", "since"} <= params, f"/api/events 缺参数：{params}"


async def test_replay_and_live_share_one_types_filter(app_state: AppState) -> None:
    """回放段与实时段必须过同一个过滤器。只过滤 live 的话，前端勾了"只看 finished"，
    刷新追历史时仍会收到整段 progress。"""
    record = await app_state.scheduler.run_to_completion("preflight", {})

    got = await _drain(app_state, task_id=record.id, types={EventType.TASK_FINISHED})

    assert [item["event"] for item in got] == ["task.finished"]


async def test_stream_events_replays_a_finished_run(app_state: AppState) -> None:
    record = await app_state.scheduler.run_to_completion("preflight", {})

    collected: list[dict[str, str]] = []
    agen = stream_events(app_state, task_id=record.id, types=None, since=None)
    while True:
        try:
            item = await asyncio.wait_for(agen.__anext__(), timeout=0.5)
        except (TimeoutError, StopAsyncIteration):
            break  # 回放完了、没有活事件 → 正常收尾
        collected.append(item)
    await agen.aclose()

    names = [item["event"] for item in collected]
    assert "task.started" in names
    assert "task.finished" in names  # preflight 成功 → finished
    # 每条 SSE data 都是合法 JSON 且带 type/payload
    first = json.loads(collected[0]["data"])
    assert first["type"] == collected[0]["event"]
    assert "payload" in first
