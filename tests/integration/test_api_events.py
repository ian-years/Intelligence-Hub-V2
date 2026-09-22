"""`api/v1/events.py`：SSE 类型解析 + "回放 + 实时" 事件流装配（不真开 HTTP 流，避免挂起）。"""

from __future__ import annotations

import asyncio
import json

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
