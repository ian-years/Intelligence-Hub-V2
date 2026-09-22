"""`manifest_writer` 测试。

**这个文件是整个 V2.0 里最靠近"契约"二字的地方** —— V1 §2 契约二
（清单必须写终态）的结构性保证就在这几十行里。所以断言的重点不是
"返回值对不对"，而是：

- 五条退出路径（成功 / 异常 / 协作取消 / 硬取消 / 超时）**每一条**都留下终态清单；
- 收尾代码**永远不许盖掉**用户真正需要的那条异常（`yt-dlp` 的 412 原文）；
- 文件写不成就不登记索引；索引登记不成也要把任务档案标成终态
  （否则前端是一个永远转不出来的进度条 —— V1 §7.22 的形状）。

用真的 `SqliteStorage`（内存库）与真的 `FileStorage`（tmp_path），不用假对象：
双写的正确性恰恰在"文件路径与 DB 里存的那串相对路径是不是同一个东西"，
假对象会把这条测没了。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from intelligence_hub_v2.core.event_bus import InProcessEventBus
from intelligence_hub_v2.core.manifest import manifest_writer
from intelligence_hub_v2.errors import TaskCancelled
from intelligence_hub_v2.models.event import Event, EventType
from intelligence_hub_v2.models.manifest import Manifest
from intelligence_hub_v2.models.task import ArtifactRef, FailureRecord, TaskKind
from intelligence_hub_v2.storage.db import SqliteStorage
from intelligence_hub_v2.storage.files import FileStorage

TASK_ID = "11111111-2222-3333-4444-555555555555"


class RecordingBus:
    """只记录不广播的假总线。`manifest_writer` 只用 `publish()`。"""

    def __init__(self) -> None:
        self.published: list[Event] = []

    async def publish(self, event: Event) -> None:
        self.published.append(event)


@pytest.fixture
async def storage() -> AsyncIterator[SqliteStorage]:
    instance = SqliteStorage.in_memory()
    await instance.initialize()
    try:
        yield instance
    finally:
        await instance.close()


@pytest.fixture
def files(tmp_path: Path) -> FileStorage:
    return FileStorage(tmp_path / "data")


async def _open_run(storage: SqliteStorage, task_id: str = TASK_ID) -> None:
    await storage.task_runs.start(
        task_id=task_id, task_name="抖音采集", kind=TaskKind.PLATFORM_COLLECT.value
    )


async def _take(stream, count: int) -> list:
    out = []
    async for event in stream:
        out.append(event)
        if len(out) >= count:
            break
    return out


def _writer(
    storage: SqliteStorage,
    files: FileStorage,
    *,
    task_id: str = TASK_ID,
    kind: TaskKind = TaskKind.PLATFORM_COLLECT,
    bus: object | None = None,
    timeout_seconds: float | None = None,
):
    return manifest_writer(
        "抖音采集",
        task_id,
        kind,
        {"platforms": {"douyin": {"enabled": True}}},
        storage=storage,
        files=files,
        bus=bus,  # type: ignore[arg-type]
        timeout_seconds=timeout_seconds,
    )


# ---------------------------------------------------------------------------
# 正常退出
# ---------------------------------------------------------------------------


async def test_success_writes_file_index_and_terminal_run(
    storage: SqliteStorage, files: FileStorage
) -> None:
    await _open_run(storage)
    async with _writer(storage, files) as builder:
        builder.succeed({"downloaded": 2, "failed": 0})

    stored = await storage.manifests.latest_for_task(TASK_ID)
    assert stored is not None
    manifest = stored.content()
    assert manifest.status == "success"
    assert manifest.summary == {"downloaded": 2, "failed": 0}

    # 文件真的在盘上，且与 DB 里那份冗余副本是同一份清单
    absolute = files.abs(stored.file_path)
    assert absolute.is_file()
    on_disk = Manifest.model_validate_json(absolute.read_text(encoding="utf-8"))
    assert on_disk == manifest

    run = await storage.task_runs.get_or_raise(TASK_ID)
    assert run.status == "success"
    assert run.manifest_path == stored.file_path
    assert run.progress == 1.0
    assert run.ended_at is not None


async def test_manifest_file_name_carries_the_utc_stamp_and_kind(
    storage: SqliteStorage, files: FileStorage
) -> None:
    await _open_run(storage)
    async with _writer(storage, files) as builder:
        builder.succeed()

    stored = await storage.manifests.latest_for_task(TASK_ID)
    assert stored is not None
    name = Path(stored.file_path).name
    # 三段都在，但**不去 parse**：日期段本身带一个 `-`，task_id 是 UUID 也带 `-`，
    # 拆着玩只会拆错（这用例第一版就是这么红的）。按"前缀 + 中段 + 后缀"断言即可。
    assert name.startswith(datetime.now(UTC).strftime("%Y%m%d"))  # 只核对日期，避免跨秒抖动
    assert "-platform_collect-" in name
    assert name.endswith(f"{TASK_ID}.json")  # 唯一性靠这段，见 files.manifest_path 的 docstring


async def test_no_explicit_status_still_lands_on_success(
    storage: SqliteStorage, files: FileStorage
) -> None:
    """`else` 分支补 success —— 但只在没人设过时补（下一条用例是反向）。"""
    await _open_run(storage)
    async with _writer(storage, files):
        pass

    assert (await storage.manifests.latest_for_task(TASK_ID)).content().status == "success"


async def test_handler_set_partial_survives(storage: SqliteStorage, files: FileStorage) -> None:
    """**spec §2.6 草图的那条 bug 的看护**。

    草图在 `else` 分支无条件 `builder.succeed()`，会把这里的 `partial` 改成
    `success` —— 也就是"两条下载失败"显示成"全成功"。V1 的看板就是这么绿的。
    """
    await _open_run(storage)
    async with _writer(storage, files) as builder:
        builder.partial({"downloaded": 1, "failed": 1})
        builder.add_failure(FailureRecord(platform="douyin", stage="download", error="412 blocked"))

    manifest = (await storage.manifests.latest_for_task(TASK_ID)).content()
    assert manifest.status == "partial"
    assert manifest.failures[0].error == "412 blocked"
    assert (await storage.task_runs.get_or_raise(TASK_ID)).status == "partial"


async def test_artifacts_survive_the_roundtrip(storage: SqliteStorage, files: FileStorage) -> None:
    await _open_run(storage)
    async with _writer(storage, files) as builder:
        builder.add_artifact(
            ArtifactRef(kind="media", path=Path("media/douyin/姜胡说/x/media.mp4"), size_bytes=1234)
        )
        builder.succeed({"downloaded": 1})

    manifest = (await storage.manifests.latest_for_task(TASK_ID)).content()
    assert manifest.artifacts[0].size_bytes == 1234
    assert manifest.artifacts[0].path.as_posix().endswith("media.mp4")


# ---------------------------------------------------------------------------
# 异常退出：V1 §2 契约二的主场
# ---------------------------------------------------------------------------


async def test_exception_still_writes_a_terminal_manifest_and_propagates_unchanged(
    storage: SqliteStorage, files: FileStorage
) -> None:
    await _open_run(storage)
    original = ValueError("yt-dlp: Request is blocked by server (412)")

    with pytest.raises(ValueError) as caught:
        async with _writer(storage, files):
            raise original

    # **传播的必须是同一条异常对象**：收尾代码把它换成 SQLite 栈是这里最贵的失败方式
    assert caught.value is original

    manifest = (await storage.manifests.latest_for_task(TASK_ID)).content()
    assert manifest.status == "failed"
    assert manifest.error == "yt-dlp: Request is blocked by server (412)"

    run = await storage.task_runs.get_or_raise(TASK_ID)
    assert run.status == "failed"
    assert run.error_text == "yt-dlp: Request is blocked by server (412)"
    assert run.ended_at is not None


async def test_error_text_is_readable_chinese_in_the_file(
    storage: SqliteStorage, files: FileStorage
) -> None:
    """错误原文落到盘上也不能是 `\\u5931\\u8d25`（V1 docs/lessons.md 坑 4 同一类）。"""
    await _open_run(storage)
    reason = "带桥导出的登录 cookie 也不行（Could not copy Chrome cookie database）"
    with pytest.raises(RuntimeError, match="cookie"):
        async with _writer(storage, files):
            raise RuntimeError(reason)

    stored = await storage.manifests.latest_for_task(TASK_ID)
    raw = files.abs(stored.file_path).read_text(encoding="utf-8")
    assert reason in raw
    assert "\\u" not in raw
    assert json.loads(raw)["error"] == reason


async def test_success_then_raise_never_reports_success(
    storage: SqliteStorage, files: FileStorage
) -> None:
    """handler 说了 success、随后抛出异常 → **异常赢**。

    这条与 `test_handler_set_partial_survives` 方向相反，两条都要在：
    规则不是"handler 永远优先"，而是"知道得更多的一方优先" ——
    而抛出异常这个事实比 handler 早先说的 success 更有说服力。
    """
    await _open_run(storage)
    with pytest.raises(KeyError):
        async with _writer(storage, files) as builder:
            builder.succeed({"downloaded": 3})
            msg = "boom"
            raise KeyError(msg)

    assert (await storage.manifests.latest_for_task(TASK_ID)).content().status == "failed"


async def test_cooperative_cancellation_lands_on_cancelled_not_failed(
    storage: SqliteStorage, files: FileStorage
) -> None:
    """协作式取消（`CancelToken` → 抛 `TaskCancelled`）不是失败。

    标成 `failed` 会引导值班的人去查一个没坏的东西；
    而且 `prune()` 给 failed/timeout 三倍保留期，cancelled 不该享受。
    """
    await _open_run(storage)
    with pytest.raises(TaskCancelled):
        async with _writer(storage, files):
            raise TaskCancelled("用户点了停止")

    manifest = (await storage.manifests.latest_for_task(TASK_ID)).content()
    assert manifest.status == "cancelled"
    assert "用户点了停止" in (manifest.error or "")


async def test_hard_cancellation_still_writes_the_manifest(
    storage: SqliteStorage, files: FileStorage
) -> None:
    await _open_run(storage)
    with pytest.raises(asyncio.CancelledError):
        async with _writer(storage, files):
            raise asyncio.CancelledError

    assert (await storage.manifests.latest_for_task(TASK_ID)).content().status == "cancelled"


async def test_timeout_records_the_budget_in_the_message(
    storage: SqliteStorage, files: FileStorage
) -> None:
    """ "超时了"不说多久等于没说。"""
    await _open_run(storage)
    with pytest.raises(TimeoutError):
        async with _writer(storage, files, timeout_seconds=900.0):
            raise TimeoutError

    manifest = (await storage.manifests.latest_for_task(TASK_ID)).content()
    assert manifest.status == "timeout"
    assert "900" in (manifest.error or "")


async def test_explicit_fail_in_handler_is_not_overwritten_by_the_raise(
    storage: SqliteStorage, files: FileStorage
) -> None:
    """handler 已经 `fail(exc)` 记过（带 platform/stage 的 failures[]），
    随后 re-raise 时 wrapper 不该把它换成一份更粗的记录。"""
    await _open_run(storage)
    exc = ValueError("桥没起")
    with pytest.raises(ValueError):
        async with _writer(storage, files) as builder:
            builder.fail(exc)
            raise exc

    manifest = (await storage.manifests.latest_for_task(TASK_ID)).content()
    assert manifest.error == "桥没起"


# ---------------------------------------------------------------------------
# 收尾失败：双写的不对称性
# ---------------------------------------------------------------------------


async def test_file_write_failure_registers_no_index_but_still_finishes_the_run(
    storage: SqliteStorage,
    files: FileStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**纪律 2**：一条指向不存在文件的索引行比没有索引更糟。

    而任务档案**仍然**要标终态 —— 否则前端是一个永远转不出来的进度条。
    两件事方向相反，所以必须分开断言。
    """
    await _open_run(storage)

    def boom(*args: object, **kwargs: object) -> None:
        msg = "磁盘满了"
        raise OSError(msg)

    monkeypatch.setattr("os.replace", boom)
    async with _writer(storage, files) as builder:
        builder.succeed({"downloaded": 1})

    assert await storage.manifests.count() == 0
    run = await storage.task_runs.get_or_raise(TASK_ID)
    assert run.status == "success"  # 跑成的部分不许被我们的记账失败改写（纪律 4）
    assert run.manifest_path is None
    assert "清单文件写入失败" in (run.error_text or "")
    assert "磁盘满了" in (run.error_text or "")


async def test_directory_creation_failure_is_reported_the_same_way(
    storage: SqliteStorage, files: FileStorage
) -> None:
    await _open_run(storage)
    files.manifests_dir.parent.mkdir(parents=True, exist_ok=True)
    # "清单目录"这个位置上站着一个**文件** → mkdir 撞 FileExistsError
    files.manifests_dir.write_text("我占住了这个位置", encoding="utf-8")

    async with _writer(storage, files) as builder:
        builder.succeed()

    assert await storage.manifests.count() == 0
    run = await storage.task_runs.get_or_raise(TASK_ID)
    assert "清单目录创建失败" in (run.error_text or "")


async def test_index_registration_failure_leaves_the_file_and_flags_the_run(
    storage: SqliteStorage, files: FileStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """登记失败时**文件留在盘上**（它是权威源），但 run 的 `error_text` 必须说实话。

    只留"文件在、索引没了"这个状态是不够的：前端历史列表会凭空少一条，
    而没有任何地方说得出为什么少。
    """
    await _open_run(storage)

    async def boom(*args: object, **kwargs: object) -> None:
        msg = "database is locked"
        raise RuntimeError(msg)

    monkeypatch.setattr(storage.manifests, "record", boom)
    async with _writer(storage, files) as builder:
        builder.succeed({"downloaded": 1})

    written = list(files.manifests_dir.glob("*.json"))
    assert len(written) == 1
    run = await storage.task_runs.get_or_raise(TASK_ID)
    assert "清单登记失败" in (run.error_text or "")
    assert "database is locked" in (run.error_text or "")


async def test_missing_task_run_row_is_reported_and_never_raises_out_of_the_writer(
    storage: SqliteStorage, files: FileStorage
) -> None:
    """前置条件没满足（没调 `task_runs.start()`）时，清单文件照样落盘，
    档案标记失败只记不抛 —— 正在传播的异常比我们的记账更值钱。"""
    async with _writer(storage, files, task_id="never-started") as builder:
        builder.succeed()

    assert await storage.manifests.count() == 0  # 外键不成立 → 整个事务回滚
    written = list(files.manifests_dir.glob("*.json"))
    assert len(written) == 1
    assert json.loads(written[0].read_text(encoding="utf-8"))["status"] == "success"


# ---------------------------------------------------------------------------
# 与 EventBus 的接缝
# ---------------------------------------------------------------------------


async def test_manifest_written_event_is_published_after_registration(
    storage: SqliteStorage, files: FileStorage
) -> None:
    await _open_run(storage)
    bus = RecordingBus()
    async with _writer(storage, files, bus=bus) as builder:
        builder.succeed({"downloaded": 2})

    assert [e.type for e in bus.published] == [EventType.MANIFEST_WRITTEN]
    payload = bus.published[0].payload
    assert payload["status"] == "success"
    assert payload["schema_version"] == "2.0"
    assert payload["manifest_path"] == (await storage.manifests.latest_for_task(TASK_ID)).file_path
    assert bus.published[0].task_id == TASK_ID


async def test_no_event_when_the_file_could_not_be_written(
    storage: SqliteStorage,
    files: FileStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """前端收到 `manifest.written` 却点进 404，比收不到事件更糟。"""
    await _open_run(storage)
    bus = RecordingBus()
    monkeypatch.setattr("os.replace", lambda *a, **k: (_ for _ in ()).throw(OSError("满")))

    async with _writer(storage, files, bus=bus) as builder:
        builder.succeed()

    assert bus.published == []


async def test_event_published_through_the_real_bus_is_replayable(
    storage: SqliteStorage, files: FileStorage
) -> None:
    """走真总线（带持久化）时，这条事件必须能在 `task_events` 里翻出来。"""
    await _open_run(storage)
    bus = InProcessEventBus(storage.events)
    stream = bus.subscribe(types={EventType.MANIFEST_WRITTEN})
    received = asyncio.create_task(_take(stream, 1))
    await asyncio.sleep(0)

    async with _writer(storage, files, bus=bus) as builder:
        builder.succeed({"downloaded": 1})

    live = await asyncio.wait_for(received, timeout=2)
    assert [e.type for e in live] == [EventType.MANIFEST_WRITTEN]

    replayed = await bus.replay(TASK_ID)
    assert [e.type for e in replayed] == [EventType.MANIFEST_WRITTEN]
    await bus.shutdown()


# ---------------------------------------------------------------------------
# 多个任务
# ---------------------------------------------------------------------------


async def test_two_runs_get_two_files_and_two_index_rows(
    storage: SqliteStorage, files: FileStorage
) -> None:
    await _open_run(storage, "aaaaaaaa-0000-0000-0000-000000000000")
    await _open_run(storage, "bbbbbbbb-0000-0000-0000-000000000000")

    async with _writer(storage, files, task_id="aaaaaaaa-0000-0000-0000-000000000000") as first:
        first.succeed({"downloaded": 1})
    async with _writer(storage, files, task_id="bbbbbbbb-0000-0000-0000-000000000000") as second:
        second.fail(ValueError("第二条挂了"))

    assert await storage.manifests.count() == 2
    assert len(list(files.manifests_dir.glob("*.json"))) == 2
    paths = {
        (await storage.manifests.latest_for_task("aaaaaaaa-0000-0000-0000-000000000000")).file_path,
        (await storage.manifests.latest_for_task("bbbbbbbb-0000-0000-0000-000000000000")).file_path,
    }
    assert len(paths) == 2


async def test_ended_at_is_never_before_started_at(
    storage: SqliteStorage, files: FileStorage
) -> None:
    await _open_run(storage)
    async with _writer(storage, files) as builder:
        builder.succeed()

    manifest = (await storage.manifests.latest_for_task(TASK_ID)).content()
    assert manifest.ended_at >= manifest.started_at
    assert manifest.ended_at.tzinfo is not None
    assert manifest.started_at.tzinfo is not None
