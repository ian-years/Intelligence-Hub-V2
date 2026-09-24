"""`core/task_runner.py`：TaskRunner 的五条退出路径 + TaskScheduler 的门面。

看护 `docs/specs/task-runner.md §2.6`（清单必须写终态）与 §4.5（失败语义）在 runner
这一层的落点：成功 / partial / handler 报告失败 / 抛异常 / 取消 / 超时，每一条都要
① 落到正确的 `task_runs.status`，② 发对应的生命周期事件，③ 终态清单一定写成。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

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


async def test_execute_removes_the_task_workdir_when_it_ends(storage, files) -> None:
    """`files.py:338` 与 `task-runner.md §2.3` 都写着「任务结束时整个删掉」，
    但今天没有任何代码路径删它。V2.0 只留一个空目录，看不出问题；
    V2.1 的 ASR 中间产物会把它变成没有上限的磁盘增长。"""
    seen_during_run: list[bool] = []

    async def handler(ctx, params):
        scratch = ctx.workdir / "asr-stage.wav"
        scratch.write_bytes(b"stage")
        seen_during_run.append(scratch.is_file())  # 跑的过程中必须在
        return TaskResult(status="success")

    await _execute(
        _runner(storage, files, FakeBus(), FakeRegistry({}, {})), make_definition(handler)
    )

    assert seen_during_run == [True]
    assert not files.tmp_dir("t-1").exists()  # 终态之后整个没了


async def test_workdir_is_removed_even_when_the_handler_raises(storage, files) -> None:
    """异常路径同样要清 —— 恰恰是崩掉的那次最容易留下一地中间产物。"""

    async def handler(ctx, params):
        (ctx.workdir / "half-done.wav").write_bytes(b"x")
        raise RuntimeError("boom")

    runner = _runner(storage, files, FakeBus(), FakeRegistry({}, {}))
    with pytest.raises(RuntimeError, match="boom"):
        await _execute(runner, make_definition(handler))

    assert not files.tmp_dir("t-1").exists()


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


async def test_build_context_failure_still_writes_terminal_state(storage, files) -> None:
    """`_build_context` 在 `manifest_writer` **里面**跑（review P0-4）。

    旧实现里它在清单机制之外：DepsFactory / tmp_dir 一抛 = 库里留一条**永不终结的
    `running` 行** + 零清单 + 没有任何终态事件。最恶劣的触发路径正是本用例这个：
    `preflight` 的 `platforms=()` + 所有平台都被关掉（`default()` 抛 `TaskRejected`）
    —— 环境已经坏了，用来检查环境的那件事本身也坏了，还留下一个永远转不完的进度条。"""
    bus = FakeBus()

    class BrokenDepsFactory:
        """装配漏了一环的 `DepsFactory`：谁都拿不到 deps。"""

        def __call__(self, platform: str) -> Any:
            raise TaskRejected(f"平台 {platform!r} 注册了但没有配置对象")

        def default(self) -> Any:
            raise TaskRejected("没有任何已配置的平台 —— DepsFactory.default() 无从构造")

    broken_runner = TaskRunner(
        storage=storage,
        events=bus,  # type: ignore[arg-type]
        files=files,
        registry=FakeRegistry({}, {}),  # type: ignore[arg-type]
        deps_factory=BrokenDepsFactory(),  # type: ignore[arg-type]
        app_config=AppConfig(),
        logger=FakeLogger(),  # type: ignore[arg-type]
    )
    definition = make_definition(_result_runner(TaskResult(status="success")))

    with pytest.raises(TaskRejected, match="没有任何已配置的平台"):
        await _execute(broken_runner, definition)

    # ① 库里的行落了 failed 终态，不再是 running 僵尸
    record = await storage.task_runs.get_or_raise("t-1")
    assert record.status == "failed"
    assert record.ended_at is not None
    assert "没有任何已配置的平台" in (record.error_text or "")
    # ② 清单（权威源）照样落盘并登记
    assert record.manifest_path is not None
    assert files.abs(record.manifest_path).is_file()
    # ③ 终态事件发了，SSE 订阅者等得到收尾
    types = [e.type for e in bus.events]
    assert types == [EventType.MANIFEST_WRITTEN, EventType.TASK_FAILED]
    # ④ 失败发生在建目录之前（_build_context 先解析依赖），没有 workdir 残留
    assert not files.tmp_dir("t-1").exists()


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


async def test_scheduler_cancel_after_finish_returns_false_not_true(storage, files) -> None:
    """review P1-7：跑完后 token 没从表里摘掉，cancel 对**已结束**的任务也回 True
    —— 用户点了取消、界面提示"已取消"，实际什么都没发生。终态之后必须 False。"""
    sched = _scheduler(storage, files)
    task_id = sched.submit("preflight", {})
    await sched.shutdown()

    record = await storage.task_runs.get_or_raise(task_id)
    assert record.is_terminal
    assert sched.cancel(task_id) is False


async def test_reap_orphans_marks_running_rows_failed(storage, files) -> None:
    await storage.task_runs.start(task_id="ghost", task_name="x", kind="platform_collect")
    runner = _runner(storage, files, FakeBus(), FakeRegistry({}, {}))

    removed = await runner.reap_orphans(reason="进程重启，任务中断")

    assert removed == 1
    record = await storage.task_runs.get_or_raise("ghost")
    assert record.status == "failed"
    assert "进程重启" in (record.error_text or "")
