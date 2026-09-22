"""`TaskRunner`（单次执行）+ `TaskScheduler`（并发 / 开关 / 取消的门面）。

分工：

- `TaskRunner.execute` 只管**一个任务从开档案到写终态清单**这一段：起 `task_runs` 行、
  进 `manifest_writer`（五条退出路径都落终态，见 `core/manifest.py`）、调 handler、
  把 `TaskResult` 搬进 builder、按终态发生命周期事件。
- `TaskScheduler` 是外面那层：平台开关闸门、`requires` 记账、全局 / 每平台 Semaphore、
  协作式取消（发 `CancelToken`）、把执行丢进 `asyncio.create_task` 立即返回 task_id。

**终态由谁保证**：`manifest_writer`。runner 里 handler 抛异常时，我 **catch 了再 re-raise**，
re-raise 让 `manifest_writer` 的 `finally` 落终态清单（V1 §2 契约二），我在外层据 `builder.status`
补发 `task.failed` / `task.cancelled` 事件 —— 事件在 `__aexit__` 之后发，那时清单已经写完，
`manifest_path` 已确定，前端收到事件点进去不会 404。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel

from intelligence_hub_v2.core.manifest import manifest_writer
from intelligence_hub_v2.core.task_registry import DepsFactory, get_task, resolve_timeout
from intelligence_hub_v2.errors import TaskRejected
from intelligence_hub_v2.logging import get_logger
from intelligence_hub_v2.models.event import (
    Event,
    EventType,
    TaskCancelledPayload,
    TaskFailedPayload,
    TaskFinishedPayload,
    TaskStartedPayload,
)
from intelligence_hub_v2.models.manifest import ManifestBuilder
from intelligence_hub_v2.models.task import TaskResult, TaskRunRecord
from intelligence_hub_v2.tasks.definition import CancelToken, TaskContext, TaskDefinition

if TYPE_CHECKING:
    import structlog

    from intelligence_hub_v2.core.config import AppConfig
    from intelligence_hub_v2.core.event_bus import EventBus
    from intelligence_hub_v2.platforms.base import PlatformConfig
    from intelligence_hub_v2.platforms.registry import PlatformRegistry
    from intelligence_hub_v2.storage.db import SqliteStorage
    from intelligence_hub_v2.storage.files import FileStorage

logger = get_logger(__name__)

__all__ = ["CancelToken", "TaskRunner", "TaskScheduler"]


class TaskRunner:
    """一次任务执行的机器。不持并发状态，可被 scheduler 并发复用。"""

    def __init__(
        self,
        *,
        storage: SqliteStorage,
        events: EventBus,
        files: FileStorage,
        registry: PlatformRegistry,
        deps_factory: DepsFactory,
        app_config: AppConfig,
        logger: structlog.BoundLogger | None = None,
    ) -> None:
        self._storage = storage
        self._events = events
        self._files = files
        self._registry = registry
        self._deps = deps_factory
        self._app = app_config
        self._log = logger or globals()["logger"]

    async def execute(
        self,
        definition: TaskDefinition,
        *,
        task_id: str,
        params: BaseModel,
        config_snapshot: Mapping[str, object],
        cancel_token: CancelToken,
    ) -> None:
        """跑一个任务到终态。异常向外传播（scheduler 吞），终态清单一定写成。"""
        snapshot = dict(config_snapshot)
        await self._storage.task_runs.start(
            task_id=task_id,
            task_name=definition.name,
            kind=definition.kind.value,
            params=_params_dump(params),
            config_snapshot=snapshot,
        )
        ctx = self._build_context(definition, task_id, cancel_token, snapshot)
        timeout = resolve_timeout(definition, self._app.scheduler.task_timeout_seconds)
        builder: ManifestBuilder | None = None
        try:
            async with manifest_writer(
                definition.name,
                task_id,
                definition.kind,
                snapshot,
                storage=self._storage,
                files=self._files,
                bus=self._events,
                timeout_seconds=timeout,
            ) as builder:
                builder.set_platforms(list(definition.platforms))
                await self._publish_started(definition, ctx, params, snapshot)
                result = await self._invoke(definition, ctx, params, timeout)
                _apply_result(builder, result)
        except Exception:
            await self._publish_terminal(builder, definition=definition, task_id=task_id)
            raise
        await self._publish_terminal(builder, definition=definition, task_id=task_id)

    async def _invoke(
        self,
        definition: TaskDefinition,
        ctx: TaskContext,
        params: object,
        timeout_seconds: int | None,
    ) -> TaskResult:
        coro = definition.runner(ctx, params)
        if timeout_seconds is None:
            return await coro
        return await asyncio.wait_for(coro, timeout=timeout_seconds)

    def _build_context(
        self,
        definition: TaskDefinition,
        task_id: str,
        cancel_token: CancelToken,
        snapshot: Mapping[str, object],
    ) -> TaskContext:
        workdir = self._files.tmp_dir(task_id, create=True)
        deps = self._deps(definition.platforms[0]) if definition.platforms else self._deps.default()
        bound = self._log.bind(task_id=task_id, task_name=definition.name)
        return TaskContext(
            task_id=task_id,
            deps=deps,
            adapters=self._registry,
            events=self._events,
            storage=self._storage,
            files=self._files,
            cancel_token=cancel_token,
            workdir=workdir,
            logger=bound,
            config_snapshot=dict(snapshot),
            task_name=definition.name,
            kind=definition.kind,
        )

    async def _publish_started(
        self,
        definition: TaskDefinition,
        ctx: TaskContext,
        params: object,
        snapshot: Mapping[str, object],
    ) -> None:
        payload = TaskStartedPayload(
            task_name=definition.name,
            kind=definition.kind.value,
            params=_params_dump(params),
            config_snapshot=dict(snapshot),
        )
        await ctx.events.publish(
            Event(
                type=EventType.TASK_STARTED,
                task_id=ctx.task_id,
                timestamp=datetime.now(UTC),
                payload=payload.model_dump(mode="json"),
            )
        )
        await ctx.storage.task_runs.set_progress(ctx.task_id, 0.0)

    async def _publish_terminal(
        self, builder: ManifestBuilder | None, *, definition: TaskDefinition, task_id: str
    ) -> None:
        if builder is None:  # pragma: no cover - __aenter__ 前就崩，几乎不可能
            return
        manifest = builder.finalize()
        path = self._files.rel(
            self._files.manifest_path(manifest.started_at, definition.kind, task_id)
        )
        duration = (manifest.ended_at - manifest.started_at).total_seconds()
        status = manifest.status

        if status in ("success", "partial"):
            payload: dict[str, object] = TaskFinishedPayload(
                status=status,
                summary=manifest.summary,
                manifest_path=path,
                duration_seconds=duration,
            ).model_dump(mode="json")
            event_type = EventType.TASK_FINISHED
        elif status == "cancelled":
            payload = TaskCancelledPayload(
                reason=manifest.error, manifest_path=path, duration_seconds=duration
            ).model_dump(mode="json")
            event_type = EventType.TASK_CANCELLED
        else:
            payload = TaskFailedPayload(
                status="timeout" if status == "timeout" else "failed",
                error=manifest.error or "任务失败（无错误原文）",
                manifest_path=path,
                duration_seconds=duration,
            ).model_dump(mode="json")
            event_type = EventType.TASK_FAILED

        await self._events.publish(
            Event(type=event_type, task_id=task_id, timestamp=datetime.now(UTC), payload=payload)
        )

    async def reap_orphans(self, *, reason: str) -> int:
        """把上次进程崩了留下的 `running` 行标成 failed。Task 9 lifespan 启动时调一次。"""
        return await self._storage.task_runs.mark_orphans_failed(reason=reason)


class TaskScheduler:
    """任务的对外入口：校验 → 开关闸门 → Semaphore 限流 → 后台起 → 返回 task_id。"""

    def __init__(
        self,
        *,
        runner: TaskRunner,
        configs: Mapping[str, PlatformConfig],
        app_config: AppConfig,
        storage: SqliteStorage,
    ) -> None:
        self._runner = runner
        self._configs: dict[str, PlatformConfig] = dict(configs)
        self._app = app_config
        self._storage = storage
        per = app_config.scheduler.max_concurrent_per_platform
        glob = app_config.scheduler.max_concurrent_global
        self._global_sem = asyncio.Semaphore(glob)
        self._platform_sems: dict[str, asyncio.Semaphore] = {
            name: asyncio.Semaphore(per) for name in configs
        }
        self._tokens: dict[str, CancelToken] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def submit(self, name: str, params: object) -> str:
        """登记并后台开跑，**立即返回 task_id**（API 的 202）。"""
        definition = get_task(name)
        validated = definition.params_schema.model_validate(params)
        self._gate_platforms(definition)
        task_id = str(uuid.uuid4())
        token = CancelToken()
        self._tokens[task_id] = token
        snapshot = self._config_snapshot(definition)
        self._tasks[task_id] = asyncio.create_task(
            self._guarded(definition, task_id, validated, token, snapshot)
        )
        return task_id

    async def run_to_completion(self, name: str, params: object) -> TaskRunRecord:
        """同步跑完一个任务，返回终态档案。测试与"点了就要结果"的调用方用。"""
        definition = get_task(name)
        validated = definition.params_schema.model_validate(params)
        self._gate_platforms(definition)
        task_id = str(uuid.uuid4())
        token = CancelToken()
        self._tokens[task_id] = token
        snapshot = self._config_snapshot(definition)
        await self._guarded(definition, task_id, validated, token, snapshot)
        self._tokens.pop(task_id, None)
        return await self._storage.task_runs.get_or_raise(task_id)

    def update_config(self, name: str, config: PlatformConfig) -> None:
        """热重载后换掉调度器手里的平台配置（`_gate_platforms` 读的就是这一份）。

        调度器在 `submit` 时按 `self._configs[platform].enabled` 决定放不放行；不改这里，
        关掉一个平台之后它照样能被点名采集 —— 与"开关全局生效"的契约相反。
        """
        self._configs[name] = config

    def cancel(self, task_id: str) -> bool:
        """请求取消。**协作式**：置位 `CancelToken`，handler 在下一个 await 点自己停。

        返回 False = 这个 id 不在跑（已结束或不存在），调用方据此给前端一个诚实的回执，
        而不是"点了没反应但界面转圈"。
        """
        token = self._tokens.get(task_id)
        if token is None:
            return False
        token.cancel()
        return True

    async def shutdown(self, *, cancel_running: bool = True) -> None:
        """收尾：取消在跑的任务并等它们落终态清单。"""
        if cancel_running:
            for token in self._tokens.values():
                token.cancel()
        pending = [task for task in self._tasks.values() if not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _gate_platforms(self, definition: TaskDefinition) -> None:
        """跑前的门：未实现的直接拒；跨平台的过；否则每个依赖平台都要 enabled。

        跑前拒（不建 run 行、不发事件）与跑后失败要分得开 —— 前者是"这个按钮不该点"，
        后者才是"这一轮真的挂了"。
        """
        if not definition.implemented:
            msg = f"任务 {definition.name!r} 在当前构建里未实现（V2.0 只给了 6 个 handler）"
            raise TaskRejected(msg)
        for platform in definition.platforms:
            config = self._configs.get(platform)
            if config is None or not config.enabled:
                msg = f"任务 {definition.name!r} 依赖平台 {platform!r}，但它未启用或未实现"
                raise TaskRejected(msg)

    def _config_snapshot(self, definition: TaskDefinition) -> dict[str, object]:
        platforms = definition.platforms or tuple(self._configs)
        snapshot: dict[str, object] = {}
        for platform in platforms:
            config = self._configs.get(platform)
            if config is not None:
                snapshot[platform] = config.model_dump(mode="json")
        return snapshot

    async def _guarded(
        self,
        definition: TaskDefinition,
        task_id: str,
        params: object,
        token: CancelToken,
        snapshot: dict[str, object],
    ) -> None:
        sems = [self._platform_sems[p] for p in definition.platforms if p in self._platform_sems]
        try:
            async with self._global_sem:
                for sem in sems:
                    await sem.acquire()
                try:
                    await self._runner.execute(
                        definition,
                        task_id=task_id,
                        params=params,  # type: ignore[arg-type]
                        config_snapshot=snapshot,
                        cancel_token=token,
                    )
                finally:
                    for sem in sems:
                        sem.release()
        except (
            Exception
        ) as exc:  # 终态清单已由 runner 写好，这里只留日志不外抛（用了 exc 故非盲捕）
            logger.exception(
                "scheduler.task_crashed", task_id=task_id, task=definition.name, error=str(exc)
            )
        finally:
            self._tokens.pop(task_id, None)
            self._tasks.pop(task_id, None)


def _params_dump(params: object) -> dict[str, object]:
    """参数模型 → JSON-safe dict。不是 BaseModel 的怪输入按空 dict 处理而不是崩。"""
    dump = getattr(params, "model_dump", None)
    if callable(dump):
        return dict(dump(mode="json"))
    return {}


def _apply_result(builder: ManifestBuilder, result: TaskResult) -> None:
    """把 handler 返回的 `TaskResult` 搬进清单 builder。

    关键：`status='failed'` 但没有异常时（handler 跑完但报告"整批都没成"），
    用一条 `RuntimeError` 把错误原文喂给 `builder.fail()` —— `Manifest.error` 必须有东西，
    否则前端只会看到一个没有解释的红灯（V1 §1.3）。
    """
    builder.set_summary(result.summary)
    for failure in result.failures:
        builder.add_failure(failure)
    for artifact in result.artifacts:
        builder.add_artifact(artifact)

    if result.status == "success":
        builder.succeed(result.summary)
    elif result.status == "partial":
        builder.partial(result.summary)
    else:
        reason = "；".join(f.error for f in result.failures[:3]) or "handler 报告任务失败"
        builder.fail(RuntimeError(reason))
