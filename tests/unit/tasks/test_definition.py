"""`tasks/definition.py`：`CancelToken` 与 `TaskContext` 的便利方法契约。

这几条看着小，但都是"取消到底怎么生效"的关键路径：`sleep` 可取消（跨博主节流不该让人
等满一个间隔）、`progress` 夹紧并双写（SSE + task_runs）、`raise_if_cancelled` 抛的是
协作式的 `TaskCancelled` 而不是 asyncio 硬取消。
"""

from __future__ import annotations

import asyncio

import pytest
from tests.unit.tasks.conftest import FakeBus, FakeConfig, FakeLogger, FakeRegistry, make_deps

from intelligence_hub_v2.errors import TaskCancelled
from intelligence_hub_v2.models.event import EventType
from intelligence_hub_v2.tasks.definition import CancelToken, TaskContext


def make_ctx_light(
    storage, files, *, task_id: str = "c-1", bus: FakeBus | None = None
) -> TaskContext:
    return TaskContext(
        task_id=task_id,
        deps=make_deps(FakeConfig()),
        adapters=FakeRegistry({}, {}),  # type: ignore[arg-type]
        events=bus or FakeBus(),  # type: ignore[arg-type]
        storage=storage,
        files=files,
        cancel_token=CancelToken(),
        workdir=files.tmp_dir(task_id, create=True),
        logger=FakeLogger(),  # type: ignore[arg-type]
        config_snapshot={},
        task_name="t",
    )


async def test_cancel_token_wait_returns_on_cancel() -> None:
    token = CancelToken()
    assert not token.is_cancelled

    waiter = asyncio.create_task(token.wait())
    await asyncio.sleep(0)  # 让 waiter 挂上
    token.cancel()
    await asyncio.wait_for(waiter, timeout=1.0)
    assert token.is_cancelled


def test_raise_if_cancelled_noop_then_raises() -> None:
    token = CancelToken()
    token.raise_if_cancelled()  # 没取消时不该抛
    token.cancel()
    with pytest.raises(TaskCancelled):
        token.raise_if_cancelled()


async def test_sleep_returns_immediately_for_zero_or_cancelled(storage, files) -> None:
    ctx = make_ctx_light(storage, files)
    await ctx.sleep(0.0)  # gap<=0 直接返回
    ctx.cancel_token.cancel()
    await ctx.sleep(60.0)  # 已取消 ⇒ 立刻返回，不等 60 秒


async def test_sleep_waits_out_a_small_gap_then_returns(storage, files) -> None:
    ctx = make_ctx_light(storage, files)
    await asyncio.wait_for(ctx.sleep(0.05), timeout=1.0)  # 到点返回，不抛


async def test_sleep_raises_when_cancelled_during_wait(storage, files) -> None:
    ctx = make_ctx_light(storage, files)

    async def _cancel_soon() -> None:
        await asyncio.sleep(0.02)
        ctx.cancel_token.cancel()

    task = asyncio.create_task(_cancel_soon())
    with pytest.raises(TaskCancelled):
        await ctx.sleep(30.0)
    await task


async def test_progress_clamps_and_double_writes(storage, files) -> None:
    await storage.task_runs.start(task_id="p1", task_name="t", kind="preflight")
    bus = FakeBus()
    ctx = make_ctx_light(storage, files, task_id="p1", bus=bus)

    await ctx.progress(1.5, message="收尾", stage="done")  # 超过 1 夹到 1

    event = bus.events[-1]
    assert event.type is EventType.TASK_PROGRESS
    assert event.payload["progress"] == 1.0
    assert event.payload["message"] == "收尾"
    record = await storage.task_runs.get_or_raise("p1")
    assert record.progress == 1.0


async def test_publish_tags_the_task_id(storage, files) -> None:
    bus = FakeBus()
    ctx = make_ctx_light(storage, files, bus=bus)

    await ctx.publish(EventType.VIDEO_ADDED, {"video_id": 7})

    assert bus.events[0].task_id == ctx.task_id
    assert bus.events[0].payload == {"video_id": 7}
