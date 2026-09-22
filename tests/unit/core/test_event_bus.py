"""`InProcessEventBus` 测试。

三条这套实现独有的性质，值得逐条钉住：

- **有界队列丢最旧的、并且记账**。丢包本身是可接受的（一个卡住的浏览器标签
  不该拖住任务），**不记账**不可接受 —— 那正是 V1 §1.3 的"看起来在跑"。
- **持久化失败不阻断实时流，但必须留痕**。
- **全局事件只广播不落库**（`task_events.task_id` 是 NOT NULL，见 event-schema.md §4）。

另外 `subscribe()` 的**泄漏**要单独盯：不 `aclose()` 就永远留在总线上，
一个 SSE 客户端异常断线一次，每条事件就多投一份。`subscriber_count()` 是这件事的仪表。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from intelligence_hub_v2.core.event_bus import DEFAULT_QUEUE_SIZE, EventBus, InProcessEventBus
from intelligence_hub_v2.errors import StorageError
from intelligence_hub_v2.models.event import Event, EventType, StoredEvent
from intelligence_hub_v2.storage.db import SqliteStorage


def _event(
    type_: EventType = EventType.TASK_PROGRESS,
    task_id: str | None = "t1",
    **payload: object,
) -> Event:
    return Event(
        type=type_,
        task_id=task_id,
        timestamp=datetime.now(UTC),
        payload=dict(payload),
    )


@pytest.fixture
async def storage() -> AsyncIterator[SqliteStorage]:
    instance = SqliteStorage.in_memory()
    await instance.initialize()
    try:
        yield instance
    finally:
        await instance.close()


async def _take(stream: AsyncIterator[Event], count: int) -> list[Event]:
    """从订阅流里取前 `count` 条。取满就 `aclose()`，不靠超时兜。"""
    out: list[Event] = []
    async for event in stream:
        out.append(event)
        if len(out) >= count:
            break
    return out


# ---------------------------------------------------------------------------
# 多播与过滤
# ---------------------------------------------------------------------------


async def test_publish_reaches_subscriber_in_order() -> None:
    bus = InProcessEventBus()
    received = asyncio.create_task(_take(bus.subscribe(), 2))
    await asyncio.sleep(0)  # 让订阅者先注册
    await bus.publish(_event(EventType.TASK_STARTED))
    await bus.publish(_event(EventType.TASK_FINISHED))

    events = await asyncio.wait_for(received, timeout=2)
    assert [e.type for e in events] == [EventType.TASK_STARTED, EventType.TASK_FINISHED]


async def test_multiple_subscribers_all_get_the_same_event() -> None:
    bus = InProcessEventBus()
    one = asyncio.create_task(_take(bus.subscribe(), 1))
    two = asyncio.create_task(_take(bus.subscribe(), 1))
    await asyncio.sleep(0)
    await bus.publish(_event())

    got = await asyncio.wait_for(asyncio.gather(one, two), timeout=2)
    assert len(got[0]) == len(got[1]) == 1
    assert got[0][0].type is got[1][0].type


async def test_subscribe_filters_by_type() -> None:
    bus = InProcessEventBus()
    only_finished = asyncio.create_task(_take(bus.subscribe(types={EventType.TASK_FINISHED}), 1))
    await asyncio.sleep(0)
    await bus.publish(_event(EventType.TASK_STARTED))
    await bus.publish(_event(EventType.TASK_PROGRESS))
    await bus.publish(_event(EventType.TASK_FINISHED))

    events = await asyncio.wait_for(only_finished, timeout=2)
    assert [e.type for e in events] == [EventType.TASK_FINISHED]


async def test_subscribe_filters_by_task_id() -> None:
    bus = InProcessEventBus()
    mine = asyncio.create_task(_take(bus.subscribe(task_id="a"), 1))
    await asyncio.sleep(0)
    await bus.publish(_event(task_id="b"))
    await bus.publish(_event(task_id="a"))

    events = await asyncio.wait_for(mine, timeout=2)
    assert events[0].task_id == "a"


async def test_combined_filter() -> None:
    bus = InProcessEventBus()
    stream = asyncio.create_task(_take(bus.subscribe(types={EventType.TASK_LOG}, task_id="a"), 1))
    await asyncio.sleep(0)
    await bus.publish(_event(EventType.TASK_LOG, task_id="b"))
    await bus.publish(_event(EventType.TASK_PROGRESS, task_id="a"))
    await bus.publish(_event(EventType.TASK_LOG, task_id="a"))

    events = await asyncio.wait_for(stream, timeout=2)
    assert len(events) == 1 and events[0].task_id == "a"


async def test_no_subscribers_means_no_error() -> None:
    bus = InProcessEventBus()
    await bus.publish(_event())


async def test_events_published_before_subscribe_are_not_replayed() -> None:
    """订阅只拿"注册之后"的事件。补历史是 `replay()` 的活。

    写成用例是因为 SSE 路由（Task 9）要把两条拼起来，拼错顺序就重播或漏播。
    """
    bus = InProcessEventBus()
    await bus.publish(_event(EventType.TASK_STARTED))
    stream = bus.subscribe()
    await bus.publish(_event(EventType.TASK_FINISHED))

    events = await asyncio.wait_for(_take(stream, 1), timeout=2)
    assert [e.type for e in events] == [EventType.TASK_FINISHED]


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------


async def test_task_scoped_events_are_persisted_with_their_timestamp(
    storage: SqliteStorage,
) -> None:
    await storage.task_runs.start(task_id="t1", task_name="抖音采集", kind="platform_collect")
    bus = InProcessEventBus(storage.events)
    stamp = datetime(2026, 9, 22, 5, 56, 26, tzinfo=UTC)
    await bus.publish(
        Event(type=EventType.TASK_LOG, task_id="t1", timestamp=stamp, payload={"message": "开始"})
    )

    stored = await storage.events.list_for_task("t1")
    assert len(stored) == 1
    assert stored[0].payload == {"message": "开始"}
    # 总线必须**照原样**交时间戳，不能自己 now() 一次 —— 否则实时流与历史时刻不一致
    assert stored[0].timestamp == stamp


async def test_global_events_are_broadcast_but_never_persisted(
    storage: SqliteStorage,
) -> None:
    """`platform.health_changed` / `config.changed` 没有 task_id，落不了库（NOT NULL）。

    只广播是**设计**，不是丢数据：健康状态看当下，配置看 YAML。
    """
    bus = InProcessEventBus(storage.events)
    stream = asyncio.create_task(_take(bus.subscribe(), 2))
    await asyncio.sleep(0)

    await bus.publish(_event(EventType.PLATFORM_HEALTH_CHANGED, task_id=None))
    await bus.publish(_event(EventType.CONFIG_CHANGED, task_id=None))

    assert len(await asyncio.wait_for(stream, timeout=2)) == 2
    assert await storage.events.count() == 0


async def test_persisting_a_task_event_with_an_unknown_task_does_not_break_the_stream(
    storage: SqliteStorage,
) -> None:
    """库里没这个任务 → Repository 抛（外键），但**实时流必须照发**。

    一条日志写不进去不该让正在跑的任务失败；可也不能静默，
    否则"历史里凭空少一段"这件事没有任何地方说得出口。
    """
    bus = InProcessEventBus(storage.events)
    stream = asyncio.create_task(_take(bus.subscribe(), 1))
    await asyncio.sleep(0)

    await bus.publish(_event(EventType.TASK_LOG, task_id="ghost"))

    assert len(await asyncio.wait_for(stream, timeout=2)) == 1
    assert bus.stats()["persist_failures"] == 1


async def test_publish_without_persistence_configured(storage: SqliteStorage) -> None:
    bus = InProcessEventBus()
    await bus.publish(_event(EventType.TASK_STARTED, task_id="nothing-here"))
    assert await storage.events.count() == 0


# ---------------------------------------------------------------------------
# 回放
# ---------------------------------------------------------------------------


async def test_replay_returns_ascending_stored_events(storage: SqliteStorage) -> None:
    await storage.task_runs.start(task_id="t1", task_name="抖音采集", kind="platform_collect")
    bus = InProcessEventBus(storage.events)
    for i in range(3):
        await bus.publish(_event(EventType.TASK_PROGRESS, task_id="t1", n=i))

    replayed = await bus.replay("t1")
    assert [e.payload["n"] for e in replayed] == [0, 1, 2]
    assert all(isinstance(e, StoredEvent) for e in replayed)


async def test_replay_since_filters_by_timestamp(storage: SqliteStorage) -> None:
    await storage.task_runs.start(task_id="t1", task_name="抖音采集", kind="platform_collect")
    bus = InProcessEventBus(storage.events)
    base = datetime(2026, 9, 22, 5, 0, tzinfo=UTC)
    for i in range(3):
        await bus.publish(
            Event(
                type=EventType.TASK_PROGRESS,
                task_id="t1",
                timestamp=base + timedelta(minutes=i),
                payload={"n": i},
            )
        )

    later = await bus.replay("t1", since=base + timedelta(minutes=1))
    assert [e.payload["n"] for e in later] == [1, 2]


async def test_replay_without_persistence_is_an_empty_list() -> None:
    """没有历史可给是一个合法答案，不是错误。真错误（连不上库）会从 Repository 冒出来。"""
    bus = InProcessEventBus()
    assert await bus.replay("t1") == []


async def test_replay_surfaces_real_storage_errors() -> None:
    """`replay()` 不吞库的错误 —— 与"没有历史可给"（空列表）严格分开。

    这里用一个已关闭的 storage 造错：`_require_sessionmaker` 抛 `StorageError`。
    为什么不测"表不存在"那种错：`SqliteStorage.in_memory()` 一定会建表，
    造不出来除非绕开公共构造路径，那是测试的杂技不是产品的形状。
    """
    storage = SqliteStorage.in_memory()
    await storage.initialize()
    bus = InProcessEventBus(storage.events)
    await storage.close()

    with pytest.raises(StorageError, match="initialize"):
        await bus.replay("t1")


# ---------------------------------------------------------------------------
# 有界队列与掉队
# ---------------------------------------------------------------------------


async def test_slow_subscriber_drops_oldest_and_counts_it() -> None:
    """**丢最旧的**：对一条日志流，刚到的一条比一百条之前的更有价值。

    同时必须记账（`dropped`）—— 静默丢包就是 V1 §1.3 那句"看起来在跑"。
    """
    bus = InProcessEventBus(queue_size=4)
    stream = bus.subscribe()
    for i in range(10):
        await bus.publish(_event(EventType.TASK_PROGRESS, n=i))

    drained = await asyncio.wait_for(_take(stream, 4), timeout=2)
    assert [e.payload["n"] for e in drained] == [6, 7, 8, 9]
    assert bus.stats()["dropped_total"] == 6


async def test_filtered_events_do_not_consume_queue_space() -> None:
    """过滤在投递侧做。否则"只要 B站 事件"的订阅者会被一堆抖音进度事件挤空缓冲。"""
    bus = InProcessEventBus(queue_size=4)
    stream = bus.subscribe(types={EventType.TASK_FINISHED})
    for i in range(50):
        await bus.publish(_event(EventType.TASK_PROGRESS, n=i))
    await bus.publish(_event(EventType.TASK_FINISHED))

    events = await asyncio.wait_for(_take(stream, 1), timeout=2)
    assert events[0].type is EventType.TASK_FINISHED
    assert bus.stats()["dropped_total"] == 0


async def test_default_queue_size_is_public() -> None:
    """Task 9 的 SSE 路由与 preflight 要知道这个数，别各自抄一份。"""
    assert DEFAULT_QUEUE_SIZE == 1000


# ---------------------------------------------------------------------------
# 生命周期与泄漏
# ---------------------------------------------------------------------------


async def test_aclose_detaches_the_subscriber() -> None:
    bus = InProcessEventBus()
    stream = bus.subscribe()
    assert bus.subscriber_count() == 1

    await stream.aclose()
    assert bus.subscriber_count() == 0

    await bus.publish(_event())  # 不再投递给已摘掉的订阅者
    assert bus.subscriber_count() == 0


async def test_abandoned_subscriber_is_detached_when_its_task_is_cancelled() -> None:
    """SSE 客户端断线 → 承载它的协程被 cancel → `finally` 必须摘掉订阅者。

    这条是泄漏看护：漏一次就多一份永久投递，几十个断线之后每条事件投几十份。
    """
    bus = InProcessEventBus()

    async def consumer() -> None:
        async for _ in bus.subscribe():
            pass

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0)
    assert bus.subscriber_count() == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bus.subscriber_count() == 0


async def test_shutdown_drains_the_buffer_then_ends_the_stream() -> None:
    """关闭不是"扔掉在听的"。取消任务时最后那条 `task.cancelled` 正是人要看的那条。"""
    bus = InProcessEventBus()
    stream = bus.subscribe()
    await bus.publish(_event(EventType.TASK_CANCELLED))
    await bus.shutdown()

    events = [event async for event in stream]  # 自然收尾，不 hang
    assert [e.type for e in events] == [EventType.TASK_CANCELLED]


async def test_shutdown_ends_an_idle_subscriber_promptly() -> None:
    bus = InProcessEventBus()

    async def drain_all() -> list[Event]:
        return [event async for event in bus.subscribe()]

    task = asyncio.create_task(drain_all())
    await asyncio.sleep(0)
    await bus.shutdown()
    assert await asyncio.wait_for(task, timeout=2) == []


async def test_publish_after_shutdown_is_ignored_not_raised() -> None:
    """任务收尾阶段还会 publish。这时候抛异常等于把一个已成功的任务判成失败。"""
    bus = InProcessEventBus()
    stream = bus.subscribe()
    await bus.shutdown()
    await bus.publish(_event())

    assert bus.is_closed is True
    assert [e async for e in stream] == []


async def test_double_shutdown_is_a_noop() -> None:
    bus = InProcessEventBus()
    await bus.shutdown()
    await bus.shutdown()


# ---------------------------------------------------------------------------
# 并发与契约
# ---------------------------------------------------------------------------


async def test_two_producers_do_not_lose_events() -> None:
    """两个任务同时 publish，单个订阅者要收到全部 20 条。

    `offer()` 是同步的、列表快照遍历，所以这里不该有丢包 ——
    但"并发写同一个总线"是任务运行期最常见的形状，值得钉一次。
    """
    bus = InProcessEventBus()
    stream = bus.subscribe()

    async def producer(name: str) -> None:
        for i in range(10):
            await bus.publish(_event(EventType.TASK_PROGRESS, task_id=name, n=i))

    await asyncio.gather(producer("a"), producer("b"))
    events = await asyncio.wait_for(_take(stream, 20), timeout=2)
    assert len(events) == 20
    assert {e.task_id for e in events} == {"a", "b"}


def test_implements_the_event_bus_protocol() -> None:
    """运行期结构检查。mypy 那层在 `event_bus.py::_mypy_conformance_check`，两层一起看。"""
    assert isinstance(InProcessEventBus(), EventBus)


def test_stats_shape_is_stable() -> None:
    """`/api/health` 与 preflight 按这些键取值，改名就是静默少一项。"""
    stats = InProcessEventBus().stats()
    assert set(stats) == {
        "subscribers",
        "persist_failures",
        "dropped_total",
        "queue_size",
        "closed",
    }


async def test_subscriber_stats_points_at_the_falling_behind_client() -> None:
    """`stats()` 说"一共丢了多少"，`subscriber_stats()` 才说**是谁**在掉队。"""
    bus = InProcessEventBus(queue_size=2)
    slow = bus.subscribe()
    bus.subscribe()
    for i in range(5):
        await bus.publish(_event(EventType.TASK_PROGRESS, n=i))

    readings = bus.subscriber_stats()
    assert len(readings) == 2
    assert sum(r.dropped for r in readings) == 6
    assert all(r.queued == 2 for r in readings)
    await slow.aclose()
    assert len(bus.subscriber_stats()) == 1
