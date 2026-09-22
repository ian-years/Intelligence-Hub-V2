"""`task_runs` 表的 Repository 测试。

看护 **V1 §2 契约二**：任务必须落到终态，终态必须带 `ended_at`。
V1 的清单停在"没有 status"的初稿时，启动器的计数兜底会把它猜成绿灯（全 0 = 成功）——
用户看到的是"任务成功"，而实际上什么都没发生。

V2 有两层保证，这里测的是**下面那层**（DB CHECK 约束）：
`status = 'running' OR ended_at IS NOT NULL`。上面那层（`ManifestBuilder.finalize()`
一定被调到）在 Task 4 的 `manifest_writer` 用例里测。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from intelligence_hub_v2.errors import ConflictError, NotFoundError
from intelligence_hub_v2.models.task import TaskRunRecord
from intelligence_hub_v2.storage.db import SqliteStorage
from intelligence_hub_v2.storage.schema import task_runs_table


async def _start(
    storage: SqliteStorage,
    task_id: str = "11111111-1111-1111-1111-111111111111",
    *,
    task_name: str = "抖音采集",
    kind: str = "platform_collect",
    params: dict[str, object] | None = None,
    started_at: datetime | None = None,
) -> TaskRunRecord:
    return await storage.task_runs.start(
        task_id=task_id,
        task_name=task_name,
        kind=kind,
        params=params,
        config_snapshot={"platforms": {"douyin": {"enabled": True}}},
        started_at=started_at,
    )


async def test_start_creates_a_running_row(storage: SqliteStorage) -> None:
    run = await _start(storage, params={"creator_url": "https://v.douyin.com/abc/"})
    assert run.status == "running"
    assert run.ended_at is None
    assert run.progress == 0.0
    assert run.is_terminal is False
    assert run.duration_seconds is None
    assert json.loads(run.params_json) == {"creator_url": "https://v.douyin.com/abc/"}
    assert json.loads(run.config_snapshot_json) == {"platforms": {"douyin": {"enabled": True}}}
    # 任务名是中文，落库读回来必须逐字相同（ensure_ascii=False 的判据）
    assert run.task_name == "抖音采集"


async def test_start_defaults_empty_json_objects(storage: SqliteStorage) -> None:
    run = await storage.task_runs.start(task_id="bare", task_name="preflight", kind="preflight")
    assert run.params_json == "{}"
    assert run.config_snapshot_json == "{}"


async def test_get_and_get_or_raise(storage: SqliteStorage) -> None:
    run = await _start(storage)
    assert (await storage.task_runs.get(run.id)) is not None
    assert (await storage.task_runs.get("nope")) is None
    with pytest.raises(NotFoundError, match="task_run"):
        await storage.task_runs.get_or_raise("nope")


async def test_duplicate_task_id_is_a_conflict(storage: SqliteStorage) -> None:
    """`id` 是主键。TaskRunner 用 UUID，撞了就是撞了 —— 不能静默覆盖上一轮的档案。

    断言的是**翻译后的**异常族：漏了翻译的话 API 层只能一律 500 + 一屏 traceback，
    而"任务 id 重复"其实是 409。
    """
    await _start(storage, "dup-id")
    with pytest.raises(ConflictError):
        await _start(storage, "dup-id")


# ---------------------------------------------------------------------------
# 进度
# ---------------------------------------------------------------------------


async def test_set_progress_roundtrips(storage: SqliteStorage) -> None:
    run = await _start(storage)
    await storage.task_runs.set_progress(run.id, 0.37)
    assert (await storage.task_runs.get_or_raise(run.id)).progress == pytest.approx(0.37)


@pytest.mark.parametrize("given,expected", [(-1.0, 0.0), (2.0, 1.0), (0.5, 0.5)])
async def test_set_progress_clamps_out_of_range(
    storage: SqliteStorage, given: float, expected: float
) -> None:
    """夹紧而不是抛异常：进度是给人看的，为一次浮点误差让整条任务失败是本末倒置。"""
    run = await _start(storage)
    await storage.task_runs.set_progress(run.id, given)
    assert (await storage.task_runs.get_or_raise(run.id)).progress == pytest.approx(expected)


async def test_progress_out_of_range_cannot_be_written_directly(storage: SqliteStorage) -> None:
    """DB 的 `progress_range` CHECK 是最后一道。

    这条用例故意绕开 `set_progress()` 的夹紧逻辑直接写 SQL ——
    它验的不是 Repository，是"就算未来有人加了一个不夹紧的写入口，库也拦得住"。
    所以断言的是 SQLAlchemy 的原始 `IntegrityError`：绕过 Repository
    就等于绕过了错误翻译，这正是"翻译只在 Repository 里"的代价与边界。
    """
    run = await _start(storage)
    with pytest.raises(IntegrityError):
        async with storage.sessionmaker() as session, session.begin():
            await session.execute(
                update(task_runs_table).where(task_runs_table.c.id == run.id).values(progress=1.5)
            )


# ---------------------------------------------------------------------------
# 终态
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["success", "partial", "failed", "timeout", "cancelled"])
async def test_finish_writes_terminal_state(storage: SqliteStorage, status: str) -> None:
    run = await _start(storage)
    finished = await storage.task_runs.finish(
        run.id,
        status=status,  # type: ignore[arg-type]
        summary={"downloaded": 2, "failed": 0},
        manifest_path="manifests/20260922-055626-platform_collect.json",
        error_text=None if status in {"success", "partial"} else "yt-dlp: Request is blocked",
    )
    assert finished.status == status
    assert finished.ended_at is not None
    assert finished.is_terminal is True
    assert finished.duration_seconds is not None
    assert finished.duration_seconds >= 0.0
    assert json.loads(finished.summary_json or "{}") == {"downloaded": 2, "failed": 0}


async def test_finish_on_success_forces_progress_to_one(storage: SqliteStorage) -> None:
    run = await _start(storage)
    await storage.task_runs.set_progress(run.id, 0.4)
    finished = await storage.task_runs.finish(run.id, status="success")
    assert finished.progress == pytest.approx(1.0)


async def test_finish_on_failure_keeps_the_last_progress(storage: SqliteStorage) -> None:
    """ "跑到 37% 挂了"比"跑到 0% 挂了"有用得多。"""
    run = await _start(storage)
    await storage.task_runs.set_progress(run.id, 0.37)
    finished = await storage.task_runs.finish(run.id, status="failed", error_text="boom")
    assert finished.progress == pytest.approx(0.37)


async def test_finish_preserves_the_error_text_verbatim(storage: SqliteStorage) -> None:
    """**V1 §1.3 / §7.22**：错误必须是原文，不许概括、不许只剩退出码。"""
    run = await _start(storage)
    original = (
        "yt-dlp failed: Request is blocked by server (412); "
        "带桥导出的登录 cookie 也不行（Could not copy Chrome cookie database）"
    )
    finished = await storage.task_runs.finish(run.id, status="failed", error_text=original)
    assert finished.error_text == original


async def test_terminal_status_without_ended_at_is_rejected_by_the_db(
    storage: SqliteStorage,
) -> None:
    """**V1 §2 契约二在 DB 层的那一半**：`terminal_has_ended_at` CHECK。

    同样故意绕开 `finish()`（它一定会填 ended_at）直接写 SQL。
    这条约束存在的意义是：将来谁加了一个"标记任务状态"的写入口忘了填 ended_at，
    库会当场红，而不是留下一条永远显示"进行中"的行。
    """
    run = await _start(storage)
    with pytest.raises(IntegrityError):
        async with storage.sessionmaker() as session, session.begin():
            await session.execute(
                update(task_runs_table)
                .where(task_runs_table.c.id == run.id)
                .values(status="failed", ended_at=None)
            )


async def test_unknown_status_is_rejected(storage: SqliteStorage) -> None:
    run = await _start(storage)
    with pytest.raises(IntegrityError):
        async with storage.sessionmaker() as session, session.begin():
            await session.execute(
                update(task_runs_table)
                .where(task_runs_table.c.id == run.id)
                .values(status="done", ended_at=datetime.now(UTC))
            )


async def test_finish_on_missing_row_raises(storage: SqliteStorage) -> None:
    with pytest.raises(NotFoundError, match="task_run"):
        await storage.task_runs.finish("nope", status="failed", error_text="x")


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


async def test_list_recent_is_newest_first(storage: SqliteStorage) -> None:
    base = datetime.now(UTC)
    for i in range(3):
        await _start(storage, f"t{i}", started_at=base + timedelta(minutes=i))
    ids = [r.id for r in await storage.task_runs.list_recent()]
    assert ids == ["t2", "t1", "t0"]


async def test_list_recent_honours_limit_and_status_filter(storage: SqliteStorage) -> None:
    base = datetime.now(UTC)
    await _start(storage, "a", started_at=base)
    await _start(storage, "b", started_at=base + timedelta(minutes=1))
    await storage.task_runs.finish("b", status="success")

    assert len(await storage.task_runs.list_recent(limit=1)) == 1
    assert [r.id for r in await storage.task_runs.list_recent(status="running")] == ["a"]
    assert [r.id for r in await storage.task_runs.list_recent(status="success")] == ["b"]


async def test_list_running_finds_orphans(storage: SqliteStorage) -> None:
    await _start(storage, "live")
    await _start(storage, "done")
    await storage.task_runs.finish("done", status="success")
    assert [r.id for r in await storage.task_runs.list_running()] == ["live"]


async def test_mark_orphans_failed(storage: SqliteStorage) -> None:
    """进程崩了之后，永远停在 `running` 的行会被标成 `failed`。

    前端不该显示一个转不出来的进度条 —— 把"进程重启，任务中断"写进 error_text
    比让它假装还活着诚实（V1 §1.3）。
    """
    await _start(storage, "orphan-1")
    await _start(storage, "orphan-2")
    await _start(storage, "already-done")
    await storage.task_runs.finish("already-done", status="success", summary={"downloaded": 1})

    affected = await storage.task_runs.mark_orphans_failed(reason="进程重启，任务中断")
    assert affected == 2

    for task_id in ("orphan-1", "orphan-2"):
        row = await storage.task_runs.get_or_raise(task_id)
        assert row.status == "failed"
        assert row.error_text == "进程重启，任务中断"
        assert row.ended_at is not None

    untouched = await storage.task_runs.get_or_raise("already-done")
    assert untouched.status == "success"
    assert untouched.summary_json is not None


async def test_mark_orphans_failed_on_a_clean_table_is_a_noop(storage: SqliteStorage) -> None:
    assert await storage.task_runs.mark_orphans_failed(reason="x") == 0


async def test_count_filters_by_status(storage: SqliteStorage) -> None:
    await _start(storage, "a")
    await _start(storage, "b")
    await storage.task_runs.finish("b", status="failed", error_text="boom")

    assert await storage.task_runs.count() == 2
    assert await storage.task_runs.count(status="running") == 1
    assert await storage.task_runs.count(status="failed") == 1
    assert await storage.task_runs.count(status="timeout") == 0
