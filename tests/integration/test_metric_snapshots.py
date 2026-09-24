"""`video_metric_snapshots`：一个检查点只能有一条读数，且"没有"不等于 0。ADR-0020 / T4.2。

这一张表最容易写错的两处都在"安静"这个词上：
- 一条全空的快照落库 → `video_ids_missing` 从此不看这条作品，一次失败的抓取换来永久盲点；
- 把"平台没回这个字段"写成 0 → 下一轮增长率要除以它。
两条都有用例，而且都是**关系**判据（不是"我断言这一项是 None"，
而是"这一行读回来与我写进去的那个'没有'是同一个没有"）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from intelligence_hub_v2.models.engagement import (
    METRIC_CHECKPOINT_VALUES,
    MetricSnapshotDraft,
    VideoCommentDraft,
)
from intelligence_hub_v2.models.video import VideoDraft
from intelligence_hub_v2.storage.db import SqliteStorage
from intelligence_hub_v2.storage.schema import METRIC_CHECKPOINTS

pytestmark = pytest.mark.integration


async def _video(storage: SqliteStorage, key: str, *, platform: str = "bilibili") -> int:
    row = await storage.videos.insert(
        VideoDraft(platform=platform, platform_video_id=key, title=f"t-{key}")
    )
    return row.id


async def _counts(storage: SqliteStorage, video_id: int) -> dict[str, Any]:
    rows = await storage.metrics.list_for_video(video_id)
    return {row.checkpoint: row for row in rows}


# --------------------------------------------------------------------------- #
# 判据与两侧定义同源
# --------------------------------------------------------------------------- #


def test_the_python_and_db_lists_of_checkpoints_are_one_list_not_two() -> None:
    """`models` 的 `Literal` 与 `schema` 那条 CHECK 必须是同一份清单。

    与 `MediaSource` / `HEALTH_STATUSES` 同一族：写两遍的失败方式是"往一边加了一个
    取值、忘了另一边"，而它红的时间点是采集真跑到那条数据时（DB 层当场拒写），
    不是写代码的时候。
    """
    assert METRIC_CHECKPOINT_VALUES == METRIC_CHECKPOINTS
    assert len(METRIC_CHECKPOINTS) >= 4, "清单空了的话上面那条相等就是自证"


# --------------------------------------------------------------------------- #
# 一个窗口一条
# --------------------------------------------------------------------------- #


async def test_refetching_the_same_window_overwrites_and_says_whom(storage: SqliteStorage) -> None:
    """同窗口重抓 = 覆盖，且被覆盖的那份在 `metadata_json.overwrote` 里看得见。

    判据是**行数不涨**，不是"第二次也成功了"。允许同一个窗口攒一串读数，
    等于让"24h 那条到底算哪个"没有答案 —— 增长率会随重跑次数变长。
    """
    video_id = await _video(storage, "BV1SNAP")
    await storage.metrics.put(
        video_id, MetricSnapshotDraft(checkpoint="24h", view_count=100, like_count=5)
    )
    first = (await _counts(storage, video_id))["24h"]

    await storage.metrics.put(
        video_id, MetricSnapshotDraft(checkpoint="24h", view_count=180, like_count=9)
    )
    rows = await storage.metrics.list_for_video(video_id)
    assert len(rows) == 1
    second = rows[0]
    assert second.view_count == 180
    assert second.collected_at >= first.collected_at
    assert "overwrote" in json.loads(second.metadata_json)


async def test_a_checkpoint_the_db_does_not_know_is_refused_by_the_check(
    storage: SqliteStorage,
) -> None:
    """DB 层那条 CHECK 真的在：绕过 Pydantic 直接写一个陌生值必须红。

    不这么测的话，"约束写在代码里"与"库里有约束"是两件事 ——
    Alembic 的 `compare_metadata` 看不见 CHECK，所以这条只能这样量。
    """
    video_id = await _video(storage, "BV1CHK")
    async with storage.sessionmaker() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO video_metric_snapshots "
                    "(video_id, checkpoint, collected_at, metadata_json) "
                    "VALUES (:v, 'bogus', '2026-01-01T00:00:00', '{}')"
                ),
                {"v": video_id},
            )


# --------------------------------------------------------------------------- #
# "没有"不是 0
# --------------------------------------------------------------------------- #


async def test_a_field_the_platform_did_not_send_stays_absent_not_zero(
    storage: SqliteStorage,
) -> None:
    """小红书那类"页面上根本没有播放量"的作品，读回来还是 `None`。

    写成 0 会得到一条"发布时 0 播放"的快照，而下一轮算 `7d / publish` 时要除以它。
    """
    video_id = await _video(storage, "note1234567890abcd", platform="xiaohongshu")
    await storage.metrics.put(
        video_id, MetricSnapshotDraft(checkpoint="publish", like_count=42, view_count=None)
    )
    snap = (await _counts(storage, video_id))["publish"]
    assert snap.view_count is None
    assert snap.like_count == 42
    assert snap.has_any_reading is True


async def test_an_all_empty_snapshot_is_refused_and_nothing_is_written(
    storage: SqliteStorage,
) -> None:
    """四项全空 → 抛，且**库里不留一行**。

    留一行的话它就不是"没抓到"而是"这个窗口抓过了"，`video_ids_missing`
    从此不再看这条作品。拒绝写在 Repository 而不是 handler，是因为
    将来任何一条别的路（补抓、导入、手工脚本）都不该能写进这种行。
    """
    video_id = await _video(storage, "BV1EMPTY")
    with pytest.raises(ValueError, match="全空"):
        await storage.metrics.put(video_id, MetricSnapshotDraft(checkpoint="7d"))
    assert await storage.metrics.list_for_video(video_id) == []
    assert video_id in await storage.metrics.video_ids_missing()


# --------------------------------------------------------------------------- #
# 挑活
# --------------------------------------------------------------------------- #


async def test_missing_selection_returns_only_videos_with_no_snapshot_at_all(
    storage: SqliteStorage,
) -> None:
    """判据是"一条都没有"，不是"缺某个固定窗口"。

    一条三年前的作品永远等不到它的 `24h`。按"缺 24h"挑活会让它每一轮都被重挑、
    每一轮都什么都没抓到 —— 那是"看起来在跑"的又一种形状，也是这一格唯一值得
    单独写一条用例的理由。
    """
    bare = await _video(storage, "BV1BARE")
    stocked = await _video(storage, "BV1HAS")
    await storage.metrics.put(stocked, MetricSnapshotDraft(checkpoint="publish", view_count=1))

    missing = await storage.metrics.video_ids_missing()
    assert missing == [bare]
    # 有了任意一条读数的作品就退出挑活名单 —— 哪怕它只有 `publish`。
    assert stocked not in missing


async def test_missing_selection_honours_the_platform_filter(storage: SqliteStorage) -> None:
    """平台过滤与"限流条数"要同时成立：只测其中一个，另一个写坏不会红。"""
    bili = await _video(storage, "BV1FLT")
    douyin = await _video(storage, "7001", platform="douyin")
    only = await storage.metrics.video_ids_missing(platform="douyin", limit=1)
    assert only == [douyin]
    assert bili not in only


async def test_comments_and_snapshots_carry_the_same_cascade_rule(storage: SqliteStorage) -> None:
    """两张附属表对"作品被删"的答案必须一致：跟着没。

    写成两条独立用例也行，但一致性本身要有人看着 —— 一张 CASCADE、一张 SET NULL
    是可能发生的（外键名手滑），而症状是"删掉作品之后快照表里长出一堆孤儿行"。
    """
    video_id = await _video(storage, "BV1CASC")
    await storage.metrics.put(video_id, MetricSnapshotDraft(checkpoint="publish", view_count=3))
    await storage.video_comments.upsert_many(
        video_id,
        [VideoCommentDraft(platform="bilibili", platform_comment_id="c1", content="x")],
    )
    assert len(await storage.metrics.list_for_video(video_id)) == 1
    await storage.videos.delete(video_id)
    assert await storage.metrics.list_for_video(video_id) == []
    assert await storage.video_comments.count_for_video(video_id) == 0
