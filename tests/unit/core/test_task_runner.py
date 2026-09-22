"""`core/task_runner.py`：TaskRunner 的五条退出路径 + TaskScheduler 的门面。

看护 `docs/specs/task-runner.md §2.6`（清单必须写终态）与 §4.5（失败语义）在 runner
这一层的落点：成功 / partial / handler 报告失败 / 抛异常 / 取消 / 超时，每一条都要
① 落到正确的 `task_runs.status`，② 发对应的生命周期事件，③ 终态清单一定写成。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from tests.unit.tasks.conftest import (
    FakeAdapter,
    FakeBus,
    FakeConfig,
    FakeDepsFactory,
    FakeLogger,
    FakeRegistry,
    make_definition,
    make_deps,
)

from intelligence_hub_v2.core.config import AppConfig
from intelligence_hub_v2.core.task_runner import TaskRunner, TaskScheduler
from intelligence_hub_v2.errors import TaskCancelled, TaskRejected
from intelligence_hub_v2.models.event import EventType
from intelligence_hub_v2.models.task import FailureRecord, TaskResult
from intelligence_hub_v2.tasks.definition import CancelToken


def _runner(storage, files, bus, registry) -> TaskRunner:
    return TaskRunner(
        storage=storage,
        events=bus,  # type: ignore[arg-type]
        files=files,
        registry=registry,  # type: ignore[arg-type]
        deps_factory=FakeDepsFactory(make_deps(FakeConfig())),  # type: ignore[arg-type]
        app_config=AppConfig(),
        logger=FakeLogger(),  # type: ignore[arg-type]
    )


async def _execute(runner: TaskRunner, definition, *, cancel_token=None, task_id="t-1") -> None:
    await runner.execute(
        definition,
        task_id=task_id,
        params=definition.params_schema(),
        config_snapshot={"douyin": {"enabled": True}},
        cancel_token=cancel_token or CancelToken(),
    )


def _result_runner(result: TaskResult) -> Callable:
    async def runner(ctx, params) -> TaskResult:
        return result

    return runner


async def test_success_writes_terminal_run_and_events(storage, files) -> None:
    bus = FakeBus()
    runner = _runner(storage, files, bus, FakeRegistry({}, {}))
    definition = make_definition(_result_runner(TaskResult(status="success", summary={"n": 3})))

    await _execute(runner, definition)

    record = await storage.task_runs.get_or_raise("t-1")
    assert record.status == "success"
    assert record.ended_at is not None
    assert record.manifest_path is not None
    types = [e.type for e in bus.events]
    # 清单（权威源）先写盘 + 登记，再广播 task.finished —— 顺序反了前端会收到事件点进去 404
    assert types == [EventType.TASK_STARTED, EventType.MANIFEST_WRITTEN, EventType.TASK_FINISHED]
    # 清单文件真的落盘了（双写的"盘"那一半）
    assert files.abs(record.manifest_path).is_file()


async def test_partial_survives_and_reports_finished(storage, files) -> None:
    bus = FakeBus()
    runner = _runner(storage, files, bus, FakeRegistry({}, {}))
    result = TaskResult(
        status="partial",
        summary={"ok": 1, "failed": 1},
        failures=[FailureRecord(stage="download", error="412")],
    )
    definition = make_definition(_result_runner(result))

    await _execute(runner, definition)

    record = await storage.task_runs.get_or_raise("t-1")
    assert record.status == "partial"
    assert any(e.type is EventType.TASK_FINISHED for e in bus.events)
    manifest = next(e for e in bus.events if e.type is EventType.MANIFEST_WRITTEN)
    assert manifest.payload["status"] == "partial"


async def test_handler_reports_failed_without_exception_still_has_error_text(
    storage, files
) -> None:
    bus = FakeBus()
    runner = _runner(storage, files, bus, FakeRegistry({}, {}))
    result = TaskResult(
        status="failed",
        summary={"failed": 1},
        failures=[FailureRecord(stage="list", error="整批挂了")],
    )
    definition = make_definition(_result_runner(result))

    await _execute(runner, definition)

    record = await storage.task_runs.get_or_raise("t-1")
    assert record.status == "failed"
    assert record.error_text and "整批挂了" in record.error_text  # V1 §1.3：不许有红灯没原因
    assert any(e.type is EventType.TASK_FAILED for e in bus.events)


async def test_handler_exception_is_recorded_and_reraised(storage, files) -> None:
    bus = FakeBus()
    runner = _runner(storage, files, bus, FakeRegistry({}, {}))

    async def boom(ctx, params) -> None:
        raise RuntimeError("yt-dlp 412")

    definition = make_definition(boom)

    with pytest.raises(RuntimeError):
        await _execute(runner, definition)

    record = await storage.task_runs.get_or_raise("t-1")
    assert record.status == "failed"
    assert "yt-dlp 412" in (record.error_text or "")
    assert any(e.type is EventType.TASK_FAILED for e in bus.events)


async def test_cooperative_cancel_settles_cancelled_not_failed(storage, files) -> None:
    bus = FakeBus()
    runner = _runner(storage, files, bus, FakeRegistry({}, {}))

    async def canceller(ctx, params) -> None:
        raise TaskCancelled("用户点了停止")

    definition = make_definition(canceller)

    with pytest.raises(TaskCancelled):
        await _execute(runner, definition)

    record = await storage.task_runs.get_or_raise("t-1")
    assert record.status == "cancelled"  # 人主动停的不算失败
    assert any(e.type is EventType.TASK_CANCELLED for e in bus.events)


async def test_timeout_settles_timeout(storage, files) -> None:
    bus = FakeBus()
    runner = _runner(storage, files, bus, FakeRegistry({}, {}))

    async def slow(ctx, params) -> None:
        await asyncio.sleep(1.0)

    definition = make_definition(slow, timeout_seconds=1)

    with pytest.raises(TimeoutError):
        await _execute(runner, definition, task_id="t-timeout")

    record = await storage.task_runs.get_or_raise("t-timeout")
    assert record.status == "timeout"
    failed = next(e for e in bus.events if e.type is EventType.TASK_FAILED)
    assert failed.payload["status"] == "timeout"


async def test_started_event_carries_task_metadata(storage, files) -> None:
    bus = FakeBus()
    runner = _runner(storage, files, bus, FakeRegistry({}, {}))
    definition = make_definition(_result_runner(TaskResult(status="success")))

    await _execute(runner, definition)

    started = bus.events[0]
    assert started.type is EventType.TASK_STARTED
    assert started.payload["task_name"] == "echo"
    assert started.payload["kind"] == "preflight"


# ---- Scheduler ----


def _scheduler(storage, files, *, douyin_enabled: bool = True) -> TaskScheduler:
    bus = FakeBus()
    registry = FakeRegistry(
        {"douyin": FakeAdapter("douyin")}, {"douyin": FakeConfig(enabled=douyin_enabled)}
    )
    runner = _runner(storage, files, bus, registry)
    configs = {"douyin": FakeConfig(enabled=douyin_enabled)}
    app = AppConfig()
    return TaskScheduler(runner=runner, configs=configs, app_config=app, storage=storage)  # type: ignore[arg-type]


async def test_scheduler_gates_disabled_platform_before_creating_run(storage, files) -> None:
    sched = _scheduler(storage, files, douyin_enabled=False)

    with pytest.raises(TaskRejected, match="未启用"):
        await sched.run_to_completion("douyin_collect", {"creator_ids": [1]})

    # 跑前拒：不该留下任何 running 档案
    assert await storage.task_runs.count() == 0


async def test_scheduler_rejects_unimplemented_task(storage, files) -> None:
    sched = _scheduler(storage, files)
    with pytest.raises(TaskRejected, match="未实现"):
        await sched.run_to_completion("all_platforms", {})


async def test_scheduler_run_to_completion_preflight(storage, files) -> None:
    sched = _scheduler(storage, files)

    record = await sched.run_to_completion("preflight", {})

    assert record.status in {"success", "partial"}
    # 跨平台任务的快照把全部已配置平台都 dump 进去
    assert "douyin" in record.config_snapshot_json


async def test_scheduler_submit_returns_id_and_completes_on_shutdown(storage, files) -> None:
    sched = _scheduler(storage, files)

    task_id = sched.submit("preflight", {})
    await sched.shutdown()

    record = await storage.task_runs.get_or_raise(task_id)
    assert record.is_terminal


async def test_scheduler_cancel_unknown_returns_false(storage, files) -> None:
    sched = _scheduler(storage, files)
    assert sched.cancel("does-not-exist") is False


async def test_reap_orphans_marks_running_rows_failed(storage, files) -> None:
    await storage.task_runs.start(task_id="ghost", task_name="x", kind="platform_collect")
    runner = _runner(storage, files, FakeBus(), FakeRegistry({}, {}))

    removed = await runner.reap_orphans(reason="进程重启，任务中断")

    assert removed == 1
    record = await storage.task_runs.get_or_raise("ghost")
    assert record.status == "failed"
    assert "进程重启" in (record.error_text or "")
