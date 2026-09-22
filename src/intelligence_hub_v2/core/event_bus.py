"""进程内事件总线（多播 + 持久化）。

契约来源：docs/specs/event-schema.md §4（`EventBus` Protocol）+ §6（持久化与保留策略）

两条与 V1 相反的决定，都写在这里而不是藏在代码里：

1. **V1 的"日志"是子进程 stdout 的尾行 JSON**（§2 契约一）。中间打的进度全丢，
   而"最后一行"被两个生产者同时写就会拿到错的结果。V2 里事件是一条条**结构化对象**，
   实时推给 SSE 订阅者，同时落 `task_events` 表，事后能翻。
2. **慢消费者不能把生产者拖住**。常驻服务的事件生产者只有任务本身，
   而订阅者是浏览器 SSE 连接 —— 一个关掉一半的窗口、一个卡住的客户端，
   代价不该由"任务有没有跑完"来付。所以每个订阅者一个**有界**队列，
   满了丢最旧的并计数（见 `_Subscriber`），**不阻塞、不静默**。
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterable
from contextlib import suppress
from datetime import datetime
from typing import Protocol, runtime_checkable

from intelligence_hub_v2.logging import get_logger
from intelligence_hub_v2.models.event import Event, EventType, StoredEvent
from intelligence_hub_v2.storage.repositories.events import EventRepository

logger = get_logger(__name__)

__all__ = ["EventBus", "InProcessEventBus", "SubscriberStats"]

DEFAULT_QUEUE_SIZE = 1000
"""每个订阅者的队列上限。1000 条事件 ≈ 一次中等任务的全部进度日志，
够前端断线后重连接上；再大就是拿内存换"大概用不到的历史"。"""


@runtime_checkable
class EventBus(Protocol):
    """事件总线的结构契约（docs/specs/event-schema.md §4）。

    单独定义在 core 而不是 models，因为它**是**运行时组件的接口，
    不是数据形状。Task 9 的 SSE 路由只认这个 Protocol，不认实现类 ——
    V3 换 Redis pub/sub 时只有这个文件下面多一个类。
    """

    async def publish(self, event: Event) -> None: ...

    def subscribe(
        self,
        types: Iterable[EventType] | None = None,
        task_id: str | None = None,
    ) -> AsyncIterator[Event]: ...

    async def replay(self, task_id: str, since: datetime | None = None) -> list[StoredEvent]: ...

    async def shutdown(self) -> None: ...


class SubscriberStats:
    """一个订阅者的健康读数。`/api/events` 的调试视图与 preflight 用。"""

    __slots__ = ("dropped", "queued")

    def __init__(self, *, dropped: int, queued: int) -> None:
        self.dropped = dropped
        self.queued = queued

    def __repr__(self) -> str:
        return f"SubscriberStats(dropped={self.dropped}, queued={self.queued})"


class _Subscriber:
    """一个订阅者的有界缓冲。

    为什么不用 `asyncio.Queue`：它的 `put_nowait` 满了会抛 `QueueFull`，
    于是"丢哪条"这个决定要么由生产者做（阻塞或抛异常，都不该发生），
    要么写成"捕获异常然后丢弃**新的**那条" —— 而对一条正在跑的日志流，
    丢掉刚到的一条比丢掉一百条之前的旧的多得多。
    `deque(maxlen=N)` 的语义正好是"保新弃旧"，而且它自己在满时静默丢，
    所以丢计数要在 append **之前**问长度，这个顺序是这条实现的要点。

    过滤放在投递侧（`_matches`）而不是消费侧：不匹配的事件不该占队列位置，
    否则"只要 B站 事件的订阅者"会被一堆抖音进度事件把缓冲挤空。
    """

    __slots__ = ("_closed", "_queue", "_wake", "dropped", "task_id", "types")

    def __init__(
        self,
        types: Iterable[EventType] | None = None,
        task_id: str | None = None,
        *,
        max_size: int = DEFAULT_QUEUE_SIZE,
    ) -> None:
        self.types = frozenset(types) if types else None
        self.task_id = task_id
        self._queue: deque[Event] = deque(maxlen=max(1, max_size))
        self._wake = asyncio.Event()
        self._closed = False
        self.dropped = 0

    def _matches(self, event: Event) -> bool:
        if self.types is not None and event.type not in self.types:
            return False
        return not (self.task_id is not None and event.task_id != self.task_id)

    def offer(self, event: Event) -> bool:
        """投一条事件。返回是否真的收了（不匹配 = 没收）。"""
        if self._closed or not self._matches(event):
            return False
        if self._queue.maxlen is not None and len(self._queue) == self._queue.maxlen:
            self.dropped += 1
        self._queue.append(event)
        self._wake.set()
        return True

    def close(self) -> None:
        self._closed = True
        self._wake.set()  # 唤醒挂在 wait() 上的消费者，让它看到"已关闭"

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def queued(self) -> int:
        """缓冲里还没被消费掉的事件数。"""
        return len(self._queue)

    async def iterate(self) -> AsyncIterator[Event]:
        """先清缓冲再等。**清空-检查-等待** 三步之间不能有 await，
        否则"关闭正好发生在检查与等待之间"会让这个生成器永远挂住。
        单线程事件循环下这三步确实是原子的 —— 这也是不用锁的理由。
        """
        while True:
            while self._queue:
                yield self._queue.popleft()
            if self._closed:
                return
            await self._wake.wait()
            self._wake.clear()


class _Subscription:
    """`subscribe()` 的返回物。为什么不直接返回 async generator：

    **没启动过的 async generator，`aclose()` 不会执行函数体里的 `finally`**
    （Python 语义：生成器还在"创建"状态，直接标记为已结束）。
    而本类的订阅者是**注册在前**的（见 `subscribe()` 里的注释），
    两者一组合就得到一个安静的泄漏：`bus.subscribe()` 之后不迭代就 `aclose()`
    —— 订阅者永远留在总线上，每条事件多投一份，没人报错。
    浏览器标签反复开关几十次之后这个列表只增不减。

    所以摘除动作有两处、都必须存在：`aclose()`（显式关）与 `_pump()` 的
    `finally`（协程被 cancel / 生成器被 GC）。`_detach` 对重复摘除是幂等的。
    """

    __slots__ = ("_detach", "_iterator", "_subscriber")

    def __init__(
        self,
        subscriber: _Subscriber,
        iterator: AsyncGenerator[Event, None],
        detach: Callable[[], None],
    ) -> None:
        self._subscriber = subscriber
        self._iterator = iterator
        self._detach = detach

    def __aiter__(self) -> _Subscription:
        return self

    async def __anext__(self) -> Event:
        return await self._iterator.__anext__()

    async def aclose(self) -> None:
        """关掉内层生成器（它的 `finally` 会摘自己），再兜一次摘除。

        两次 `_detach` 不冲突：那个方法是幂等的。
        """
        self._subscriber.close()
        await self._iterator.aclose()
        self._detach()

    @property
    def dropped(self) -> int:
        """这个订阅者到现在丢了多少条。SSE 路由可以把它写进响应头给前端看。"""
        return self._subscriber.dropped


class InProcessEventBus:
    """进程内多播总线，可选把带 `task_id` 的事件落到 `task_events` 表。

    用法（Task 8 的 TaskRunner 里）::

        bus = InProcessEventBus(events=storage.events)
        await bus.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_id=tid,
                timestamp=datetime.now(UTC),
                payload={...},
            )
        )
        ...
        await bus.shutdown()

    **先落库再广播**。反过来的话，广播是同步的、落库要 await，
    中间一个订阅者如果立刻被 asyncio 调度并回查历史，会看到"事件在流里但库里没有"，
    前端刷新一次就少一条。落库失败不阻断广播（见 `_persist`）。
    """

    def __init__(
        self,
        events: EventRepository | None = None,
        *,
        queue_size: int = DEFAULT_QUEUE_SIZE,
    ) -> None:
        self._events = events
        self._queue_size = queue_size
        self._subscribers: list[_Subscriber] = []
        self._closed = False
        self._persist_failures = 0

    # ---- 写侧 ----

    async def publish(self, event: Event) -> None:
        """发布一条事件。

        带 `task_id` 且配了持久化 → 先落库；全局事件（`platform.health_changed` /
        `config.changed`）**只广播不落库**（`task_events.task_id` 是 NOT NULL，
        理由见 event-schema.md §4 的实施期修订）。

        总线已关闭时静默返回并记一条 warning：任务收尾阶段还会 publish，
        这时候抛异常等于把一个已经成功的任务判成失败。
        """
        if self._closed:
            logger.warning("event_bus.publish_after_shutdown", type=event.type.value)
            return

        if event.task_id is not None:
            await self._persist(event)

        # 快照：订阅者可能在遍历中 aclose 自己，直接迭代同一个 list 会跳元素。
        for subscriber in tuple(self._subscribers):
            subscriber.offer(event)

    async def _persist(self, event: Event) -> None:
        """落库。失败**记日志 + 计数**，不吞也不往外抛。

        为什么不抛：一条日志写不进去不该让一个正在跑的任务失败。
        为什么不静默：V1 的教训（§1.3）就是"看起来在跑"。
        计数暴露在 `stats()` 里，preflight 能问出"这轮有没有丢历史"。
        """
        if self._events is None or event.task_id is None:
            return
        try:
            await self._events.append(
                event.task_id, event.type, event.payload, timestamp=event.timestamp
            )
        except Exception:  # 持久化失败必须留原文，但不能挡住实时流
            self._persist_failures += 1
            logger.exception(
                "event_bus.persist_failed",
                type=event.type.value,
                task_id=event.task_id,
                persist_failures=self._persist_failures,
            )

    # ---- 读侧 ----

    def subscribe(
        self,
        types: Iterable[EventType] | None = None,
        task_id: str | None = None,
    ) -> AsyncIterator[Event]:
        """订阅实时流。

        **只收到注册之后发布的事件** —— 补历史用 `replay()`，
        SSE 的标准做法是先 `replay` 追平、再 `subscribe` 接活的（Task 9 的路由负责拼这个顺序）。

        用完要 `aclose()`（或者让订阅者协程结束）。不关的话这个 `_Subscriber`
        会一直留在总线上，每条事件都给它投一遍 —— 一个卡住的浏览器标签
        就能让这个列表只增不减。有专门的用例盯这条。
        """
        subscriber = _Subscriber(types, task_id, max_size=self._queue_size)
        # **注册在迭代之前**（eager）：SSE 路由的写法是
        # `stream = bus.subscribe(...)` → 起个协程去 `async for`。
        # 如果注册推迟到第一次 `__anext__`，那"订阅后立刻 publish"就会漏掉第一条，
        # 而第一条往往是 `task.started` —— 前端会看到一个没有开头的任务。
        self._subscribers.append(subscriber)
        return _Subscription(subscriber, self._pump(subscriber), lambda: self._detach(subscriber))

    async def _pump(self, subscriber: _Subscriber) -> AsyncGenerator[Event, None]:
        """把订阅者的缓冲转成异步迭代器，并在**任何**退出方式上摘掉自己。

        这里的 `finally` 是"协程被 cancel / 消费者 break 之后生成器被 GC"那条路的
        唯一摘除点 —— `_Subscription.aclose()` 只管显式关闭那一条。
        """
        try:
            async for event in subscriber.iterate():
                yield event
        finally:
            self._detach(subscriber)

    def _detach(self, subscriber: _Subscriber) -> None:
        # ValueError = 已经摘过（aclose 与生成器 finally、shutdown 撞上）。目标已达成。
        with suppress(ValueError):
            self._subscribers.remove(subscriber)

    async def replay(self, task_id: str, since: datetime | None = None) -> list[StoredEvent]:
        """从库里回放某个任务的历史事件，**正序**（最旧在前）。

        没配持久化时返回空列表而不是抛：这个方法的语义是"给我历史"，
        "没有历史可给"是一个合法答案。真正的错误（库连不上）会从 Repository 冒出来。
        """
        if self._events is None:
            return []
        return await self._events.list_for_task(task_id, since=since)

    # ---- 生命周期 ----

    async def shutdown(self) -> None:
        """关闭总线：所有订阅者的流正常收尾（不是 cancel）。

        先置 `_closed` 再逐个 close，顺序反了会让"关闭过程中还在 publish"
        的收尾事件被投进一个已经关闭的订阅者 —— 那条事件既没进流也没进历史。
        缓冲里剩下的事件**仍会被消费完**（`iterate()` 先清缓冲），
        所以取消一个任务时最后那条 `task.cancelled` 不会丢。
        """
        self._closed = True
        for subscriber in tuple(self._subscribers):
            subscriber.close()

    @property
    def is_closed(self) -> bool:
        return self._closed

    def subscriber_count(self) -> int:
        """当前活跃的订阅者数。泄漏看护与 `/api/health` 用。"""
        return sum(1 for subscriber in self._subscribers if not subscriber.closed)

    def subscriber_stats(self) -> list[SubscriberStats]:
        """逐个订阅者的读数。**用来回答"是谁在掉队"** ——
        总掉包数说明有问题，但说不清是浏览器关了没断开、还是某个客户端真追不上。
        """
        return [
            SubscriberStats(dropped=subscriber.dropped, queued=subscriber.queued)
            for subscriber in self._subscribers
            if not subscriber.closed
        ]

    def stats(self) -> dict[str, object]:
        """一次快照：谁在听、丢了多少、库里失败了几次。"""
        return {
            "subscribers": self.subscriber_count(),
            "persist_failures": self._persist_failures,
            "dropped_total": sum(s.dropped for s in self._subscribers),
            "queue_size": self._queue_size,
            "closed": self._closed,
        }


def _mypy_conformance_check() -> None:
    """让 mypy 证明 `InProcessEventBus` 满足 `EventBus` Protocol。

    这个函数**永远不被调用**，它是给类型检查器看的断言。
    少了它，改一个方法名要等到 Task 9 的 SSE 路由真去调才发现 ——
    而 Protocol 的全部意义就是"接线之前就知道接不上"。
    （运行期还有一条 `isinstance(..., EventBus)` 用例，两层一起看。）
    """
    bus: EventBus = InProcessEventBus()
    reveal_type_unused = bus  # noqa: F841 - 只为让赋值不被优化掉，mypy 在此核对签名
