"""`tasks/enrich_metrics.py`：给已有作品补一次读数，可选顺手抓评论（T4.2 / ADR-0020）。

被测的是"挑活 + 算窗口 + 记账"这三件事。`storage` 用真 SQLite（`conftest.py` 的口径：
入库/查重这类行为不许被 mock 糊过去），适配器用 `FakeAdapter`。

窗口的断言写成**关系**（单调、边界归属、NULL 的去处）而不是"某时刻 = 某标签"的样本：
样本只证明那一个点，而这一格出过的错正是"边界与老稿子被归错档"。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tests.unit.tasks.conftest import (
    FakeAdapter,
    FakeBus,
    FakeConfig,
    FakeRegistry,
    make_ctx,
)

from intelligence_hub_v2.errors import PlatformError, TaskCancelled
from intelligence_hub_v2.models.engagement import (
    MetricCheckpoint,
    MetricReadings,
    MetricSnapshotDraft,
    VideoCommentDraft,
)
from intelligence_hub_v2.models.video import VideoDraft
from intelligence_hub_v2.platforms.base import Capabilities
from intelligence_hub_v2.tasks.enrich_metrics import run_enrich_metrics, window_for
from intelligence_hub_v2.tasks.params import EnrichMetricsParams

PLATFORM = "bilibili"
#: 由近到远的那条序，`window_for` 的单调性就是对着它验的。
_WINDOW_ORDER: tuple[MetricCheckpoint, ...] = ("publish", "24h", "72h", "7d", "manual")


def _readings(**kw: Any) -> MetricReadings:
    return MetricReadings.model_validate(kw)


def _comment(cid: str, *, text: str = "写得好") -> VideoCommentDraft:
    return VideoCommentDraft(
        platform=PLATFORM,
        platform_comment_id=cid,
        content=text,
        published_at=datetime(2026, 1, 2, tzinfo=UTC),
    )


def _caps_with_comments(base: Capabilities) -> Capabilities:
    """把能力位翻成"这一家能抓评论"。

    不直接构造一份新的 `Capabilities`：那会变成"第二处真相"，而这一家到底要不要桥、
    cookie 阶梯什么顺序，与这条用例无关 —— 只改被测的那一个位。
    """
    return replace(base, supports_comments=True)


def _adapter_with_comments(**kw: Any) -> FakeAdapter:
    probe = FakeAdapter(PLATFORM)
    return FakeAdapter(PLATFORM, capabilities=_caps_with_comments(probe.capabilities), **kw)


async def _seed_video(
    storage: Any,
    *,
    platform: str = PLATFORM,
    pid: str = "BV1xx",
    published_at: datetime | None = None,
) -> int:
    row = await storage.videos.insert(
        VideoDraft(
            platform=platform,
            platform_video_id=pid,
            title=f"t-{pid}",
            creator_id=None,
            published_at=published_at,
        )
    )
    return row.id


def _ctx(storage: Any, files: Any, adapter: FakeAdapter) -> Any:
    reg = FakeRegistry({adapter.platform: adapter}, {adapter.platform: FakeConfig()})
    return make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus())


# --------------------------------------------------------------------------- #
# window_for
# --------------------------------------------------------------------------- #


def test_the_window_is_monotonic_as_a_video_gets_older() -> None:
    """发布越久，窗口只会往后走，不会回头。

    写成"每 6 小时取一次，标签序列非递减"而不是逐个时刻点名：点名式断言在边界上会红得
    莫名其妙，而这条要防的是"那张表里顺序写反了"。
    """
    now = datetime(2026, 3, 1, tzinfo=UTC)
    ages = [window_for(now - timedelta(hours=h), now) for h in range(0, 400, 6)]
    ranks = [_WINDOW_ORDER.index(label) for label in ages]
    assert ranks == sorted(ranks), ages


def test_window_boundaries_belong_to_the_later_window() -> None:
    """边界那一秒归**已经开始**的那一档（24h 整点就是 24h，不是 publish）。

    判反了会让"24h 那一格永远拿不到东西"：整点附近跑的都被记成 publish，
    而 publish 早就被采集那一轮占住了（唯一索引 `(video_id, checkpoint)` → 覆盖）。
    """
    now = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

    def at(hours: float) -> MetricCheckpoint:
        return window_for(now - timedelta(hours=hours), now)

    assert at(23.9) == "publish"
    assert at(24) == "24h"
    assert at(71.9) == "24h"
    assert at(72) == "72h"
    assert at(167.9) == "72h"
    assert at(168) == "7d"


def test_a_video_far_past_the_last_window_stays_in_it() -> None:
    """三年前的稿子今天补抓，落 `7d`，不落 `manual`。

    这张表回答的是"发布后第 N 天长什么样"；把老稿子的读数叫 `manual`，等于把
    "补抓一条老作品"与"人为指定重抓"混成一件事。
    """
    now = datetime(2026, 3, 1, tzinfo=UTC)
    assert window_for(now - timedelta(days=30), now) == "7d"
    assert window_for(now - timedelta(days=1000), now) == "7d"


def test_unknown_publish_time_is_manual_and_a_future_one_too() -> None:
    """两种"算不出年龄"的情况都归 `manual`，且**都不归 `publish`**。

    不知道发布时间就声称"这是发布那一刻的数"是编的；发布时间在将来（时区没换算对的
    典型症状）若也算 `publish`，那"刚发布"与"时钟坏了"就成了同一个标签。
    """
    now = datetime(2026, 3, 1, tzinfo=UTC)
    assert window_for(None, now) == "manual"
    assert window_for(now + timedelta(hours=5), now) == "manual"


# --------------------------------------------------------------------------- #
# 挑活
# --------------------------------------------------------------------------- #


async def test_no_ids_targets_only_videos_without_any_snapshot(storage: Any, files: Any) -> None:
    """判据是"一条快照都没有"，不是"缺某个固定窗口"。

    一条三年前的作品永远等不到它的 `24h`：按窗口挑活会让它每轮被重挑、每轮什么都没抓到
    —— 那是"看起来在跑"的又一形状（`video_ids_missing` 的 docstring 同一条）。
    """
    has_one = await _seed_video(storage, pid="BV1has")
    empty = await _seed_video(storage, pid="BV1none")
    await storage.metrics.put(has_one, MetricSnapshotDraft(checkpoint="publish", view_count=11))

    adapter = FakeAdapter(PLATFORM, metrics=_readings(view_count=20))
    result = await run_enrich_metrics(
        _ctx(storage, files, adapter), EnrichMetricsParams(video_ids=[])
    )

    assert adapter.metrics_calls == ["BV1none"], "已有快照的那条不该再被挑中"
    assert result.summary["snapshots"] == 1
    assert [s.view_count for s in await storage.metrics.list_for_video(empty)] == [20]
    assert [s.view_count for s in await storage.metrics.list_for_video(has_one)] == [11]


async def test_explicit_ids_bypass_the_missing_anything_logic(storage: Any, files: Any) -> None:
    """给了 id 就按位补，**哪怕它已经有快照**，且同窗口是覆盖不是攒成两行。

    ADR-0020 决定一第 2 条：唯一键不含 `collected_at`，否则"24h 那条算哪个数"没有答案。
    """
    vid = await _seed_video(storage, pid="BV1x")
    await run_enrich_metrics(
        _ctx(storage, files, FakeAdapter(PLATFORM, metrics=_readings(view_count=1))),
        EnrichMetricsParams(video_ids=[vid]),
    )
    await run_enrich_metrics(
        _ctx(storage, files, FakeAdapter(PLATFORM, metrics=_readings(view_count=999))),
        EnrichMetricsParams(video_ids=[vid]),
    )

    snaps = await storage.metrics.list_for_video(vid)
    assert [s.view_count for s in snaps] == [999]


async def test_platform_filter_reaches_the_target_query(storage: Any, files: Any) -> None:
    """`platform` 只补某一家：另一家的作品连问都不该问。"""
    await _seed_video(storage, platform=PLATFORM, pid="BV1b")
    await _seed_video(storage, platform="douyin", pid="dy1")
    adapter = FakeAdapter("douyin", metrics=_readings(view_count=5))
    ctx = make_ctx(
        storage=storage,
        files=files,
        registry=FakeRegistry({"douyin": adapter}, {"douyin": FakeConfig()}),
        bus=FakeBus(),
    )

    await run_enrich_metrics(ctx, EnrichMetricsParams(platform="douyin"))
    assert adapter.metrics_calls == ["dy1"]


async def test_limit_caps_the_picked_targets(storage: Any, files: Any) -> None:
    """`limit` 是上限不是建议（与 `list_creator_videos` 同一口径）。"""
    for n in range(4):
        await _seed_video(storage, pid=f"BV1{n}")
    adapter = FakeAdapter(PLATFORM, metrics=_readings(view_count=1))

    await run_enrich_metrics(_ctx(storage, files, adapter), EnrichMetricsParams(limit=2))

    assert len(adapter.metrics_calls) == 2


# --------------------------------------------------------------------------- #
# 读数落库
# --------------------------------------------------------------------------- #


async def test_readings_land_as_a_snapshot_labeled_by_age(storage: Any, files: Any) -> None:
    """一次成功的补抓：库里多一行，窗口按发布时间算，metadata 原样带上。"""
    published = datetime.now(UTC) - timedelta(hours=30)
    vid = await _seed_video(storage, pid="BV1a", published_at=published)
    adapter = FakeAdapter(
        PLATFORM, metrics=_readings(view_count=1234, like_count=5, metadata_json='{"coin": 3}')
    )

    result = await run_enrich_metrics(
        _ctx(storage, files, adapter), EnrichMetricsParams(video_ids=[vid])
    )

    assert result.status == "success"
    snaps = await storage.metrics.list_for_video(vid)
    assert [(s.checkpoint, s.view_count, s.like_count) for s in snaps] == [("24h", 1234, 5)]
    assert '"coin": 3' in snaps[0].metadata_json
    assert result.summary["by_window"] == "bilibili/24h=1"


async def test_the_adapter_is_handed_a_video_meta_carrying_the_row_identity(
    storage: Any, files: Any
) -> None:
    """契约的入参是 `VideoMeta`，不是 DB 行：那一层转换要能被看见。

    看护的是"平台作品 id 原样传下去"—— 转换器里最便宜的错就是把 `platform_video_id`
    填成了别的（症状是"每一条评论都挂在别的作品下"）。
    """
    vid = await _seed_video(storage, pid="BV1id")
    adapter = FakeAdapter(PLATFORM, metrics=_readings(view_count=1))

    await run_enrich_metrics(_ctx(storage, files, adapter), EnrichMetricsParams(video_ids=[vid]))

    assert adapter.metrics_calls == ["BV1id"]


async def test_a_platform_that_cannot_answer_is_a_failure_not_a_silent_zero(
    storage: Any, files: Any
) -> None:
    """适配器抛"这一家做不到"→ 记一条 `metrics` 失败，**库里没有快照**。

    少了这一条，"抖音补不到数"会表现为"这一轮跑了、什么都没写、全绿"。
    """
    vid = await _seed_video(storage, pid="BV1d")
    adapter = FakeAdapter(
        PLATFORM, metrics_error=PlatformError(PLATFORM, "metrics", "读数只在页面上下文里给")
    )

    result = await run_enrich_metrics(_ctx(storage, files, adapter), EnrichMetricsParams())

    assert result.status == "failed", "一条都没写出去且全部失败 = failed，不是 success"
    assert [(f.stage, f.video_id) for f in result.failures] == [("metrics", "BV1d")]
    assert await storage.metrics.list_for_video(vid) == []


async def test_an_empty_reading_never_reaches_the_database(storage: Any, files: Any) -> None:
    """契约上适配器不该交空读数，这里再挡一次。

    空快照进了库 = 这条作品从此不再被 `video_ids_missing` 挑中（一次失败换永久盲点）。
    两半都要断言：只测"清单里有原因"的话，一个"先写库再记失败"的实现也能绿。
    """
    vid = await _seed_video(storage, pid="BV1e")
    adapter = FakeAdapter(PLATFORM, metrics=_readings())

    result = await run_enrich_metrics(
        _ctx(storage, files, adapter), EnrichMetricsParams(video_ids=[vid])
    )

    assert await storage.metrics.list_for_video(vid) == []
    assert "全空" in result.failures[0].error
    assert result.summary["snapshots"] == 0


async def test_a_non_platform_exception_keeps_its_type_in_the_text(
    storage: Any, files: Any
) -> None:
    """适配器抛的不是 `PlatformError`（`KeyError` / `TimeoutError`）也要留原文，且不掀整轮。

    只 catch `PlatformError` 的话，一条坏作品会让 50 条的批次直接终止 —— 而清单里
    连"为什么停"都没有。
    """
    await _seed_video(storage, pid="BV1k")
    adapter = FakeAdapter(PLATFORM, metrics_error=KeyError("stat"))

    result = await run_enrich_metrics(_ctx(storage, files, adapter), EnrichMetricsParams())

    assert "KeyError" in result.failures[0].error
    assert result.summary["failed"] == 1


async def test_a_missing_adapter_is_one_failure_not_a_crash(storage: Any, files: Any) -> None:
    """库里有 B站 作品、注册表里却没装 B站 适配器 → 一条 `metrics` 失败，任务照常收尾。

    这条是"关掉平台再跑补抓"的形状：整体崩掉会让清单只剩一个 traceback，
    而真实原因是"这一家的适配器今天没装配"。
    """
    await _seed_video(storage, pid="BV1w")
    adapter = FakeAdapter("douyin", metrics=_readings(view_count=1))
    ctx = make_ctx(
        storage=storage,
        files=files,
        registry=FakeRegistry({"douyin": adapter}, {"douyin": FakeConfig()}),
        bus=FakeBus(),
    )

    result = await run_enrich_metrics(ctx, EnrichMetricsParams())

    assert result.summary["failed"] == 1
    assert result.summary["snapshots"] == 0
    assert result.failures[0].platform == "bilibili"


# --------------------------------------------------------------------------- #
# 评论
# --------------------------------------------------------------------------- #


async def test_comments_are_not_asked_when_the_capability_says_no(storage: Any, files: Any) -> None:
    """能力位为假 → **连问都不问**，并且那个数进清单。

    ADR-0020 决定二选的就是"能力是声明式的，不是运行期猜的"。
    只断言 `comment_calls == []` 不够：那挡不住"没问但也没记账"，于是"这一家不支持"
    与"接口挂了返回 0 条"在清单里同形。
    """
    await _seed_video(storage, pid="BV1n")
    adapter = FakeAdapter(PLATFORM, metrics=_readings(view_count=1))
    assert adapter.capabilities.supports_comments is False, "这条用例的前提就是能力位为假"

    result = await run_enrich_metrics(
        _ctx(storage, files, adapter), EnrichMetricsParams(comments_limit=20)
    )

    assert adapter.comment_calls == []
    assert result.summary["comments_unsupported_platforms"] == 1
    assert result.summary["comments_new"] == 0
    assert result.failures == []


async def test_declaring_the_capability_but_returning_none_is_a_failure(
    storage: Any, files: Any
) -> None:
    """`supports_comments=True` 却交回 None：声明与实现不一致，不能记成"没评论"。

    同样两半：清单里有失败、库里没有行。
    """
    vid = await _seed_video(storage, pid="BV1c")
    adapter = _adapter_with_comments(metrics=_readings(view_count=1), comments=None)

    result = await run_enrich_metrics(
        _ctx(storage, files, adapter), EnrichMetricsParams(comments_limit=20)
    )

    assert [f.stage for f in result.failures] == ["comments"]
    assert "supports_comments=True" in result.failures[0].error
    assert await storage.video_comments.list_for_video(vid) == []


async def test_comments_land_with_new_and_seen_split(storage: Any, files: Any) -> None:
    """评论落库的两个数是关系：**第一次全新增，第二次全"见过"**。

    写死"3 条"挡不住"每次都重插一遍"（V1 的 `video_comments` 就是这个形状，
    重复率随采集轮数涨）。
    """
    vid = await _seed_video(storage, pid="BV1u")
    rows = [_comment(f"c{n}") for n in range(3)]

    first = _adapter_with_comments(metrics=_readings(view_count=1), comments=rows)
    run1 = await run_enrich_metrics(
        _ctx(storage, files, first), EnrichMetricsParams(video_ids=[vid], comments_limit=10)
    )
    assert (run1.summary["comments_new"], run1.summary["comments_updated"]) == (3, 0)

    again = _adapter_with_comments(metrics=_readings(view_count=2), comments=rows)
    run2 = await run_enrich_metrics(
        _ctx(storage, files, again), EnrichMetricsParams(video_ids=[vid], comments_limit=10)
    )
    assert (run2.summary["comments_new"], run2.summary["comments_updated"]) == (0, 3)

    stored = await storage.video_comments.list_for_video(vid)
    assert len(stored) == 3


async def test_comment_failure_keeps_the_snapshot_that_already_landed(
    storage: Any, files: Any
) -> None:
    """评论挂了不该把已经写进去的读数一起抹掉（两条流水，两格账）。

    判据是"这一轮既 partial 又有 1 条快照"：只看 `failed == 1` 的话，
    一个"评论失败就回滚整条"的实现也是绿的，而那条读数本来是有效数据。
    """
    vid = await _seed_video(storage, pid="BV1r")
    adapter = _adapter_with_comments(
        metrics=_readings(view_count=7),
        comments_error=PlatformError(PLATFORM, "comments", "reply 接口 code=-400"),
    )

    result = await run_enrich_metrics(
        _ctx(storage, files, adapter), EnrichMetricsParams(comments_limit=10)
    )

    assert result.status == "partial"
    assert result.summary["snapshots"] == 1
    assert [f.stage for f in result.failures] == ["comments"]
    assert [s.view_count for s in await storage.metrics.list_for_video(vid)] == [7]


async def test_metrics_failure_skips_the_comment_leg_for_that_video(
    storage: Any, files: Any
) -> None:
    """读数都没拿到就不该再去问评论。

    断言的是"只有一条失败 + 一次没问"：如果实现是"读数失败仍继续抓评论"，一条坏作品
    会换来两格红，而排查的人会以为是两个不同的故障。
    """
    await _seed_video(storage, pid="BV1m")
    adapter = _adapter_with_comments(
        metrics_error=PlatformError(PLATFORM, "metrics", "风控"),
        comments=[_comment("x")],
    )

    result = await run_enrich_metrics(
        _ctx(storage, files, adapter), EnrichMetricsParams(comments_limit=10)
    )

    assert [f.stage for f in result.failures] == ["metrics"]
    assert adapter.comment_calls == []


async def test_a_comment_round_with_nothing_to_say_is_not_a_failure(
    storage: Any, files: Any
) -> None:
    """评论区是空的（返回 `[]`）→ 0 新增、0 失败。

    与"声明了能力却返回 None"那一对：两种"没有东西"必须能区分开。
    """
    vid = await _seed_video(storage, pid="BV1o")
    adapter = _adapter_with_comments(metrics=_readings(view_count=1), comments=[])

    result = await run_enrich_metrics(
        _ctx(storage, files, adapter), EnrichMetricsParams(video_ids=[vid], comments_limit=10)
    )

    assert result.failures == []
    assert result.summary["comments_new"] == 0
    assert adapter.comment_calls == [{"id": "BV1o", "limit": 10, "sort": "hot"}]


async def test_comment_limit_zero_is_the_default_and_never_asks(storage: Any, files: Any) -> None:
    """默认不抓评论：它比读数贵（一次翻页可能好几个请求）。"""
    await _seed_video(storage, pid="BV1z")
    adapter = _adapter_with_comments(metrics=_readings(view_count=1), comments=[_comment("x")])

    await run_enrich_metrics(_ctx(storage, files, adapter), EnrichMetricsParams())

    assert adapter.comment_calls == []
    assert adapter.metrics_calls == ["BV1z"]


# --------------------------------------------------------------------------- #
# 取消
# --------------------------------------------------------------------------- #


async def test_cancel_propagates_as_task_cancelled_not_a_failure(storage: Any, files: Any) -> None:
    await _seed_video(storage, pid="BV1q")
    ctx = _ctx(storage, files, FakeAdapter(PLATFORM, metrics=_readings(view_count=1)))
    ctx.cancel_token.cancel()

    with pytest.raises(TaskCancelled):
        await run_enrich_metrics(ctx, EnrichMetricsParams())
