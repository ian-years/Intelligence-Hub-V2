"""`task_events` 表的 Repository 测试。

这张表是 EventBus 的持久化侧。三条要钉住的行为：

1. **`task_id` 是 NOT NULL + 外键**（data-model.md §2.6 Locked）——
   所以全局事件（`platform.health_changed` / `config.changed`）只广播不落库，
   写一个不存在的 task_id 必须红，不许静默丢弃（丢事件 = 任务详情里凭空少一段日志）。
2. **`list_*` 交出去一律正序**（最旧在前）。查询内部是倒序 + LIMIT 才能拿到"最近 N 条"，
   翻回正序是 Repository 的职责 —— 漏了就是每个调用方各自 reverse，漏一个就显示成倒放的日志。
3. **`prune()` 保留策略**：普通任务按 `retention_days`，失败/超时按 3 倍，
   而**仍在 `running` 的任务一条都不删** —— 删掉正在推流的任务的历史，
   前端日志会凭空断一段，而没人知道断了。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from intelligence_hub_v2.errors import StorageError
from intelligence_hub_v2.models.event import EventType
from intelligence_hub_v2.storage.db import SqliteStorage
from intelligence_hub_v2.storage.schema import task_events_table, task_runs_table


async def _task(storage: SqliteStorage, task_id: str, *, status: str | None = None) -> str:
    """开一条任务行（事件的外键要求它先存在）。给了 `status` 就顺手写终态。"""
    await storage.task_runs.start(task_id=task_id, task_name="抖音采集", kind="platform_collect")
    if status is not None:
        await storage.task_runs.finish(
            task_id, status=status, error_text="boom" if status in {"failed", "timeout"} else None
        )
    return task_id


async def test_append_returns_the_stored_event(storage: SqliteStorage) -> None:
    await _task(storage, "t1")
    event = await storage.events.append(
        "t1", EventType.TASK_LOG, {"level": "info", "message": "开始枚举博主"}
    )
    assert event.id > 0
    assert event.type is EventType.TASK_LOG
    assert event.task_id == "t1"
    assert event.payload == {"level": "info", "message": "开始枚举博主"}
    assert await storage.events.count() == 1


async def test_append_defaults_to_an_empty_payload(storage: SqliteStorage) -> None:
    await _task(storage, "t1")
    event = await storage.events.append("t1", EventType.TASK_STARTED)
    assert event.payload == {}
    stored = (await storage.events.list_for_task("t1"))[0]
    assert stored.payload == {}


async def test_ids_are_monotonic(storage: SqliteStorage) -> None:
    """`after_id` 增量拉取依赖 id 单调递增（前端记住最后一条，刷新只取更新的）。"""
    await _task(storage, "t1")
    first = await storage.events.append("t1", EventType.TASK_STARTED)
    second = await storage.events.append("t1", EventType.TASK_PROGRESS, {"progress": 0.5})
    third = await storage.events.append("t1", EventType.TASK_FINISHED)
    assert first.id < second.id < third.id


async def test_payload_keeps_chinese_readable(storage: SqliteStorage) -> None:
    """`ensure_ascii=False` 的判据：库里存的不是 `\\u59dc\\u80e1\\u8bf4`。

    V1 的教训（docs/lessons.md 坑 4）是出错时人眼读不了 JSON。
    """
    await _task(storage, "t1")
    await storage.events.append("t1", EventType.TASK_LOG, {"message": "姜胡说：yt-dlp 未拿到媒体"})
    async with storage.sessionmaker() as session:
        raw = (await session.execute(select(task_events_table.c.payload_json))).scalar_one()
    assert "姜胡说" in raw
    assert "\\u" not in raw
    assert json.loads(raw)["message"].startswith("姜胡说")


async def test_append_requires_an_existing_task(storage: SqliteStorage) -> None:
    """外键是真的。不静默丢弃 —— 丢事件等于任务详情里凭空少一段日志。"""
    with pytest.raises(StorageError):
        await storage.events.append("ghost", EventType.TASK_LOG, {"message": "x"})


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------


async def test_list_for_task_is_ascending(storage: SqliteStorage) -> None:
    await _task(storage, "t1")
    for i in range(3):
        await storage.events.append("t1", EventType.TASK_PROGRESS, {"progress": i / 3})
    progresses = [e.payload["progress"] for e in await storage.events.list_for_task("t1")]
    assert progresses == [0.0, 1 / 3, 2 / 3]


async def test_list_for_task_does_not_leak_other_tasks(storage: SqliteStorage) -> None:
    await _task(storage, "t1")
    await _task(storage, "t2")
    await storage.events.append("t1", EventType.TASK_LOG, {"message": "属于 t1"})
    await storage.events.append("t2", EventType.TASK_LOG, {"message": "属于 t2"})

    t1 = await storage.events.list_for_task("t1")
    assert len(t1) == 1
    assert t1[0].payload["message"] == "属于 t1"
    assert await storage.events.count(task_id="t2") == 1


async def test_list_for_task_after_id_is_incremental(storage: SqliteStorage) -> None:
    await _task(storage, "t1")
    ids = [
        (await storage.events.append("t1", EventType.TASK_PROGRESS, {"n": i})).id for i in range(5)
    ]

    tail = await storage.events.list_for_task("t1", after_id=ids[2])
    assert [e.id for e in tail] == ids[3:]


async def test_list_for_task_filters_by_type(storage: SqliteStorage) -> None:
    await _task(storage, "t1")
    await storage.events.append("t1", EventType.TASK_STARTED)
    await storage.events.append("t1", EventType.TASK_LOG, {"message": "a"})
    await storage.events.append("t1", EventType.TASK_LOG, {"message": "b"})
    await storage.events.append("t1", EventType.TASK_FINISHED)

    logs = await storage.events.list_for_task("t1", event_type=EventType.TASK_LOG)
    assert [e.payload["message"] for e in logs] == ["a", "b"]


async def test_list_for_task_honours_limit(storage: SqliteStorage) -> None:
    await _task(storage, "t1")
    for i in range(6):
        await storage.events.append("t1", EventType.TASK_PROGRESS, {"n": i})
    assert len(await storage.events.list_for_task("t1", limit=2)) == 2


async def test_list_recent_is_ascending_even_though_the_query_is_desc(
    storage: SqliteStorage,
) -> None:
    """**这条是 `reversed(rows)` 的看护**：查询倒序拿 LIMIT，交出去必须翻回正序。"""
    await _task(storage, "t1")
    for i in range(5):
        await storage.events.append("t1", EventType.TASK_PROGRESS, {"n": i})

    last_three = await storage.events.list_recent(limit=3)
    assert [e.payload["n"] for e in last_three] == [2, 3, 4]

    everything = await storage.events.list_recent(limit=100)
    assert [e.payload["n"] for e in everything] == [0, 1, 2, 3, 4]


async def test_list_recent_before_id_pages_backwards(storage: SqliteStorage) -> None:
    await _task(storage, "t1")
    ids = [
        (await storage.events.append("t1", EventType.TASK_PROGRESS, {"n": i})).id for i in range(6)
    ]

    page = await storage.events.list_recent(limit=2, before_id=ids[4])
    assert [e.id for e in page] == [ids[2], ids[3]]


async def test_list_recent_filters_by_type(storage: SqliteStorage) -> None:
    await _task(storage, "t1")
    await _task(storage, "t2")
    await storage.events.append("t1", EventType.TASK_STARTED)
    await storage.events.append("t2", EventType.TASK_FAILED, {"error": "boom"})

    failed = await storage.events.list_recent(event_type=EventType.TASK_FAILED)
    assert len(failed) == 1
    assert failed[0].task_id == "t2"


# ---------------------------------------------------------------------------
# 清理
# ---------------------------------------------------------------------------


async def _aged_event(storage: SqliteStorage, task_id: str, days_ago: int) -> None:
    await storage.events.append(
        task_id,
        EventType.TASK_LOG,
        {"message": f"{days_ago} 天前"},
        timestamp=datetime.now(UTC) - timedelta(days=days_ago),
    )


async def test_prune_deletes_only_what_is_older_than_the_retention(
    storage: SqliteStorage,
) -> None:
    await _task(storage, "t1", status="success")
    await _aged_event(storage, "t1", days_ago=40)
    await _aged_event(storage, "t1", days_ago=10)

    deleted = await storage.events.prune(30)
    assert deleted == 1
    remaining = await storage.events.list_for_task("t1")
    assert len(remaining) == 1
    assert remaining[0].payload["message"] == "10 天前"


async def test_prune_keeps_failed_tasks_three_times_longer(storage: SqliteStorage) -> None:
    """排查"上周那轮为什么挂了"要的正是这批数据；成功任务的历史基本没人翻。"""
    await _task(storage, "ok", status="success")
    await _task(storage, "bad", status="failed")
    await _aged_event(storage, "ok", days_ago=40)
    await _aged_event(storage, "bad", days_ago=40)

    deleted = await storage.events.prune(30)

    assert deleted == 1
    assert await storage.events.count(task_id="ok") == 0
    assert await storage.events.count(task_id="bad") == 1


async def test_prune_keeps_timeout_tasks_too(storage: SqliteStorage) -> None:
    await _task(storage, "slow", status="timeout")
    await _aged_event(storage, "slow", days_ago=80)
    assert await storage.events.prune(30) == 0
    assert await storage.events.count(task_id="slow") == 1


async def test_prune_drops_timeout_events_past_the_extended_window(
    storage: SqliteStorage,
) -> None:
    await _task(storage, "slow", status="timeout")
    await _aged_event(storage, "slow", days_ago=100)
    assert await storage.events.prune(30) == 1


@pytest.mark.parametrize("status", ["success", "partial", "failed", "timeout", "cancelled"])
async def test_cancelled_is_not_on_the_extended_retention(
    storage: SqliteStorage, status: str
) -> None:
    """`cancelled` 走普通保留期 —— 那是人主动停的，不用查。"""
    await _task(storage, "t", status=status)
    await _aged_event(storage, "t", days_ago=40)
    expected = 0 if status in {"failed", "timeout"} else 1
    assert await storage.events.prune(30) == expected


async def test_prune_never_touches_running_tasks(storage: SqliteStorage) -> None:
    """正在推流的任务的历史一条都不删，哪怕它已经跑了很久。"""
    await _task(storage, "live")  # 不 finish → 停在 running
    await _aged_event(storage, "live", days_ago=400)

    assert await storage.events.prune(30) == 0
    assert await storage.events.count(task_id="live") == 1


async def test_prune_with_a_custom_multiplier(storage: SqliteStorage) -> None:
    await _task(storage, "bad", status="failed")
    await _aged_event(storage, "bad", days_ago=40)
    # multiplier=1 → 失败任务与普通任务同窗口，40 天 > 30 天，删掉
    assert await storage.events.prune(30, failed_multiplier=1) == 1


@pytest.mark.parametrize("days", [0, -1])
async def test_prune_rejects_a_non_positive_retention(storage: SqliteStorage, days: int) -> None:
    """`retention_days=0` 的意思是"全删"。那不是配置，是事故 —— 入口就拒。"""
    with pytest.raises(ValueError, match="retention_days"):
        await storage.events.prune(days)


async def test_deleting_a_task_cascades_to_its_events(storage: SqliteStorage) -> None:
    """`ON DELETE CASCADE`。删任务档案时事件必须跟着走，否则留一堆孤儿行
    而 `prune()` 的子查询再也认不出它们属于哪种保留期。"""
    await _task(storage, "t1", status="success")
    await storage.events.append("t1", EventType.TASK_LOG, {"message": "x"})
    assert await storage.events.count() == 1

    async with storage.sessionmaker() as session, session.begin():
        await session.execute(delete(task_runs_table).where(task_runs_table.c.id == "t1"))

    assert await storage.events.count() == 0
