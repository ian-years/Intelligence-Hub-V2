"""任务层的契约类型（`TaskDefinition` / `TaskContext` / `CancelToken`）。

契约来源：`docs/specs/task-runner.md` §2.2-§2.5

放在 `tasks/` 而不是 `core/`，是因为 `AGENTS.md §2` 的契约表就把 `TaskDefinition`
钉在这里 —— 它是 V3 重写时**不能动**的那一半。执行机器（`TaskRunner` /
`TaskScheduler` / `TASKS` 注册表）住在 `core/`，它 import 本模块，方向单一。

本模块除了 `CancelToken`（一个纯 asyncio 的小工具）之外**没有运行期依赖**：
`TaskContext` 里那些 `AdapterDeps` / `PlatformRegistry` / `EventBus` / `SqliteStorage`
全部只在注解里出现（`from __future__ import annotations` 兜着，`TYPE_CHECKING` 收着）。
于是 `tasks/definition.py` 是一个接近叶子的模块，谁都能 import 它而不会成环。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from intelligence_hub_v2.errors import TaskCancelled
from intelligence_hub_v2.models.event import Event, EventType
from intelligence_hub_v2.models.task import TaskKind, TaskResult

if TYPE_CHECKING:
    import structlog

    from intelligence_hub_v2.core.event_bus import EventBus
    from intelligence_hub_v2.platforms.base import AdapterDeps
    from intelligence_hub_v2.platforms.registry import PlatformRegistry
    from intelligence_hub_v2.storage.db import SqliteStorage
    from intelligence_hub_v2.storage.files import FileStorage

__all__ = ["CancelToken", "TaskContext", "TaskDefinition"]

_CANCEL_MESSAGE = "任务已被请求取消"
"""`CancelToken.raise_if_cancelled` 抛的那句话。走常量而非内联，守住"消息先进变量"的纪律。"""


class CancelToken:
    """协作式取消（`task-runner.md §2.5`）。

    只有三件事：`cancel()` 置位、`is_cancelled` 读位、`wait()` 挂到置位为止。
    用 `asyncio.Event` 而不是一个 bool + 轮询：`wait()` 让"取消 vs 完成"这种
    `wait_first` 模式不烧 CPU，也不会引入 bool 读写之间的可见性问题。
    """

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()

    def raise_if_cancelled(self) -> None:
        """在 await 点检查并抛出（`TaskCancelled`，不是 `CancelledError`）。

        为什么抛自己这一族而不是 asyncio 的取消：V2 的取消是**协作式**的
        （`task-runner.md §4.3`），handler 在下一个安全点主动停，
        而不是被人从事件循环里硬拆。硬拆会打断清单收尾的 await（见 `core/manifest.py`
        关于"V3 若改硬取消这条要重新评估"的注释）。
        """
        if self.is_cancelled:
            raise TaskCancelled(_CANCEL_MESSAGE)


class TaskDefinition(BaseModel):
    """一个任务类型的静态描述。`TASKS` 注册表装的就是它（`task-runner.md §2.2`）。

    `runner` 是 `async def (TaskContext, params_schema实例) -> TaskResult`。
    `arbitrary_types_allowed` 是为了让 `type[BaseModel]` 与 `Callable` 能当字段类型。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    name: str
    display_name: str
    kind: TaskKind
    params_schema: type[BaseModel]
    platforms: tuple[str, ...]
    requires: tuple[str, ...]
    timeout_seconds: int | None
    cancellable: bool
    runner: Callable[[TaskContext, Any], Awaitable[TaskResult]]
    implemented: bool = True
    """V2.0 是否给了这个任务真 handler。False 的任务不进 `/api/tasks`
    （`task_is_available` 过滤掉），点名运行也只会红在 `NotImplementedError` ——
    把没做的东西显示成"可点"就是对前端撒谎（`AGENTS.md §2` 注册表那条同一判据）。
    """

    @property
    def is_multiplatform(self) -> bool:
        """跨平台 / 无平台任务（postprocess / preflight / single_link…）。"""
        return not self.platforms


@dataclass
class TaskContext:
    """注入给 runner 的依赖袋（`task-runner.md §2.3`）。

    是 dataclass 而不是 Pydantic：它握着 `EventBus` / `SqliteStorage` 这类**活的运行时
    对象**，不该被序列化，也不该被 pydantic 复制一遍。

    几个便利方法（`publish` / `progress` / `sleep`）不改变契约的**字段形状**，
    只是把"每个 handler 都要抄一遍发事件 + 更新进度 + 查取消"的样板收在一处。
    """

    task_id: str
    deps: AdapterDeps
    adapters: PlatformRegistry
    events: EventBus
    storage: SqliteStorage
    files: FileStorage
    cancel_token: CancelToken
    workdir: Path
    logger: structlog.BoundLogger
    config_snapshot: dict[str, Any] = field(default_factory=dict)
    task_name: str = ""
    kind: TaskKind | None = None

    # ---- 事件与进度的样板 ----

    async def publish(self, event_type: EventType, payload: dict[str, Any]) -> None:
        """发一条**带 task_id** 的事件（会经 `EventBus` 落库 + 广播）。"""
        await self.events.publish(
            Event(
                type=event_type,
                task_id=self.task_id,
                timestamp=datetime.now(UTC),
                payload=payload,
            )
        )

    async def progress(
        self,
        value: float,
        *,
        message: str | None = None,
        stage: str | None = None,
        current_item: str | None = None,
    ) -> None:
        """更新进度：既推 SSE（`TASK_PROGRESS`），又落 `task_runs.progress`。

        两份都做是因为它们服务两个时刻：正在看的用户要实时事件，
        事后翻"任务详情"的人要看那次停在百分之几。
        """
        clamped = max(0.0, min(1.0, value))
        payload: dict[str, Any] = {"progress": clamped}
        if message is not None:
            payload["message"] = message
        if stage is not None:
            payload["stage"] = stage
        if current_item is not None:
            payload["current_item"] = current_item
        await self.publish(EventType.TASK_PROGRESS, payload)
        await self.storage.task_runs.set_progress(self.task_id, clamped)

    def check_cancelled(self) -> None:
        self.cancel_token.raise_if_cancelled()

    async def sleep(self, seconds: float) -> None:
        """可取消的等待：跨博主节流（`per_creator_seconds`）用。

        适配器**故意不做**这一项（它一次只看到一位博主，跨博主序列只有调度层有全局视图，
        见 `docs/progress` 里本轮记账 ①）。`wait_for(cancel_token.wait(), seconds)`：
        要么到点返回，要么被取消立刻返回 —— 取消不该让人再等满一个间隔。
        """
        if seconds <= 0 or self.cancel_token.is_cancelled:
            return
        try:
            await asyncio.wait_for(self.cancel_token.wait(), timeout=seconds)
        except TimeoutError:
            return  # 到点了，正常往下走
        # 没超时却从 wait() 回来了 ⇒ 被取消
        self.check_cancelled()
