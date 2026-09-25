"""`tasks/collect.py`：单平台采集。查重即增量（记账 ③）、档位进清单（记账 ④）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.unit.tasks.conftest import (
    FakeAdapter,
    FakeBus,
    FakeConfig,
    FakeRegistry,
    make_ctx,
    make_video_meta,
)

from intelligence_hub_v2.errors import ListError, MediaDownloadError, TaskCancelled
from intelligence_hub_v2.models.creator import CreatorDraft
from intelligence_hub_v2.models.event import EventType
from intelligence_hub_v2.models.media import SingleFileArtifact, VideoAudioPairArtifact
from intelligence_hub_v2.tasks.collect import make_collect_handler
from intelligence_hub_v2.tasks.params import CollectParams

PLATFORM = "douyin"


async def _seed_creator(storage, *, pid: str = "c1", name: str = "姜胡说") -> int:
    creator = await storage.creators.insert(
        CreatorDraft(
            platform=PLATFORM,
            platform_id=pid,
            name=name,
            profile_url=f"https://douyin.com/user/{pid}",
            is_tracking=True,
        )
    )
    return creator.id


def _single_artifact(video, dest: Path) -> SingleFileArtifact:
    path = dest / "media.mp4"
    path.write_bytes(b"x" * 32)
    return SingleFileArtifact(
        path=path,
        size_bytes=32,
        media_source="page_play_url",
        cookie_rung="匿名（登录档画质不可用）",
        has_audio=True,
    )


async def test_downloads_new_videos_and_writes_relative_paths(storage, files) -> None:
    await _seed_creator(storage)
    adapter = FakeAdapter(
        PLATFORM,
        videos=[make_video_meta(PLATFORM, "v1"), make_video_meta(PLATFORM, "v2")],
        artifact_factory=_single_artifact,
    )
    reg = FakeRegistry({PLATFORM: adapter}, {PLATFORM: FakeConfig()})
    bus = FakeBus()
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=bus)

    result = await make_collect_handler(PLATFORM)(ctx, CollectParams())

    assert result.status == "success"
    assert result.summary["downloaded"] == 2
    assert await storage.videos.count() == 2
    row = await storage.videos.find_by_platform_id(PLATFORM, "v1")
    assert row is not None
    # 记账：DB 存相对 `data/` 的 posix 路径，不是绝对 Windows 路径
    assert not Path(row.media_path or "").is_absolute()
    assert "\\" not in (row.media_path or "")
    assert row.media_source == "page_play_url"
    meta = json.loads(row.metadata_json)
    assert meta["cookie_rung"] == "匿名（登录档画质不可用）"
    assert len([e for e in bus.events if e.type is EventType.VIDEO_ADDED]) == 2


async def test_second_run_skips_existing_ids_is_the_incremental_truth(storage, files) -> None:
    await _seed_creator(storage)
    adapter = FakeAdapter(
        PLATFORM, videos=[make_video_meta(PLATFORM, "v1")], artifact_factory=_single_artifact
    )
    reg = FakeRegistry({PLATFORM: adapter}, {PLATFORM: FakeConfig()})
    handler = make_collect_handler(PLATFORM)

    first = await handler(
        make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus()), CollectParams()
    )
    second = await handler(
        make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus()), CollectParams()
    )

    assert first.summary["downloaded"] == 1
    assert second.summary["downloaded"] == 0
    assert second.summary["skipped_existing"] == 1
    assert await storage.videos.count() == 1
    # 已经存在的那条不该再调下载
    assert adapter.download_calls == ["v1"]


async def test_one_download_failure_makes_partial_and_keeps_original_text(storage, files) -> None:
    await _seed_creator(storage)
    seen: list[str] = []

    def factory(video, dest: Path):
        seen.append(video.platform_video_id)
        if video.platform_video_id == "bad":
            raise MediaDownloadError(PLATFORM, "media", "阶梯走完仍失败: 412 blocked")
        return _single_artifact(video, dest)

    adapter = FakeAdapter(
        PLATFORM,
        videos=[make_video_meta(PLATFORM, "ok"), make_video_meta(PLATFORM, "bad")],
        artifact_factory=factory,
    )
    reg = FakeRegistry({PLATFORM: adapter}, {PLATFORM: FakeConfig()})

    result = await make_collect_handler(PLATFORM)(
        make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus()), CollectParams()
    )

    assert result.status == "partial"
    assert result.summary["downloaded"] == 1
    assert result.summary["failed"] == 1
    assert any("412 blocked" in f.error for f in result.failures)
    assert await storage.videos.count() == 1


async def test_a_store_failure_is_recorded_as_store_not_list(storage, files, monkeypatch) -> None:
    """review P1-5：查重/入库挂了（库打不开、锁死、磁盘满）**不是**"这位博主枚举失败"。

    原来这两处裸奔，异常被 `_collect_one_creator` 的兜底收成 `stage="list"` ——
    排查方向被带去查平台的枚举接口，且已下载成功的媒体不进 artifacts。
    判据：stage 是 "store"、原文留着、库行不存在、媒体文件已落盘。"""
    await _seed_creator(storage)
    adapter = FakeAdapter(
        PLATFORM, videos=[make_video_meta(PLATFORM, "v1")], artifact_factory=_single_artifact
    )
    reg = FakeRegistry({PLATFORM: adapter}, {PLATFORM: FakeConfig()})
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus())

    async def broken_insert(draft):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(storage.videos, "insert_or_get", broken_insert)

    result = await make_collect_handler(PLATFORM)(ctx, CollectParams())

    assert result.status == "failed"  # 0 下载 1 失败
    assert len(result.failures) == 1
    failure = result.failures[0]
    assert failure.stage == "store"
    assert failure.video_id == "v1"
    assert "database is locked" in failure.error
    assert result.artifacts == [], "没入库的媒体不能进清单的产物列表"
    assert adapter.download_calls == ["v1"], "下载确实发生过了"
    assert await storage.videos.count() == 0


async def test_list_failure_does_not_swallow_original_or_continue_silently(storage, files) -> None:
    await _seed_creator(storage)
    adapter = FakeAdapter(
        PLATFORM, list_error=ListError(PLATFORM, "list", "Request is blocked by server (412)")
    )
    reg = FakeRegistry({PLATFORM: adapter}, {PLATFORM: FakeConfig()})

    result = await make_collect_handler(PLATFORM)(
        make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus()), CollectParams()
    )

    assert result.status == "failed"  # 一位都没收到 + 有失败
    assert any("412" in f.error for f in result.failures)
    assert result.summary["failed"] == 1


async def test_no_tracked_creators_is_success_with_zero(storage, files) -> None:
    adapter = FakeAdapter(PLATFORM, videos=[])
    reg = FakeRegistry({PLATFORM: adapter}, {PLATFORM: FakeConfig()})

    result = await make_collect_handler(PLATFORM)(
        make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus()), CollectParams()
    )

    assert result.status == "success"
    assert result.summary["creators"] == 0


async def test_explicit_creator_ids_bypass_tracking_filter(storage, files) -> None:
    cid = await _seed_creator(storage)
    # 直接把它移出跟踪：显式点名仍然要能采到（V1 §7.22 按位任务不看开关）。
    await storage.creators.set_tracking(cid, False)
    adapter = FakeAdapter(
        PLATFORM, videos=[make_video_meta(PLATFORM, "v1")], artifact_factory=_single_artifact
    )
    reg = FakeRegistry({PLATFORM: adapter}, {PLATFORM: FakeConfig()})

    result = await make_collect_handler(PLATFORM)(
        make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus()),
        CollectParams(creator_ids=[cid]),
    )

    assert result.summary["downloaded"] == 1


async def test_dash_pair_stores_audio_track_as_aux_and_marks_no_merge(storage, files) -> None:
    await _seed_creator(storage)

    def factory(video, dest: Path):
        (dest / "media.f137.mp4").write_bytes(b"v" * 40)
        (dest / "media.f140.m4a").write_bytes(b"a" * 10)
        return VideoAudioPairArtifact(
            video_path=dest / "media.f137.mp4",
            audio_path=dest / "media.f140.m4a",
            video_size_bytes=40,
            audio_size_bytes=10,
            cookie_rung="带导出的登录 cookie",
        )

    adapter = FakeAdapter(
        PLATFORM, videos=[make_video_meta(PLATFORM, "v1")], artifact_factory=factory
    )
    reg = FakeRegistry({PLATFORM: adapter}, {PLATFORM: FakeConfig()})

    await make_collect_handler(PLATFORM)(
        make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus()), CollectParams()
    )

    row = await storage.videos.find_by_platform_id(PLATFORM, "v1")
    assert row is not None
    assert row.media_source == "dash_split"
    aux = json.loads(row.media_aux_paths_json)
    assert len(aux) == 1 and aux[0].endswith(".m4a")
    meta = json.loads(row.metadata_json)
    assert meta["size_bytes"] == 50  # 两条轨之和（total_size_bytes）


async def test_silent_video_is_recorded_as_no_audio_not_faked_as_transcribable(
    storage, files
) -> None:
    await _seed_creator(storage)

    def factory(video, dest: Path):
        path = dest / "media.mp4"
        path.write_bytes(b"v" * 8)
        return SingleFileArtifact(path=path, size_bytes=8, media_source="yt_dlp", has_audio=False)

    adapter = FakeAdapter(
        PLATFORM, videos=[make_video_meta(PLATFORM, "v1")], artifact_factory=factory
    )
    reg = FakeRegistry({PLATFORM: adapter}, {PLATFORM: FakeConfig()})

    await make_collect_handler(PLATFORM)(
        make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus()), CollectParams()
    )

    row = await storage.videos.find_by_platform_id(PLATFORM, "v1")
    assert row is not None
    assert json.loads(row.metadata_json)["has_audio"] is False


async def test_cancel_propagates_as_task_cancelled_not_failure(storage, files) -> None:
    await _seed_creator(storage)
    adapter = FakeAdapter(
        PLATFORM, videos=[make_video_meta(PLATFORM, "v1")], artifact_factory=_single_artifact
    )
    reg = FakeRegistry({PLATFORM: adapter}, {PLATFORM: FakeConfig()})
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus())
    ctx.cancel_token.cancel()

    with pytest.raises(TaskCancelled):
        await make_collect_handler(PLATFORM)(ctx, CollectParams())


# --------------------------------------------------------------------------- #
# 新作品顺手记一条 `publish` 读数（ADR-0020 决定二里 collect 的这一半）
# --------------------------------------------------------------------------- #


async def _snapshots(storage, video_id: int):
    return await storage.metrics.list_for_video(video_id)


async def test_a_new_video_with_counts_gets_a_publish_snapshot(storage, files) -> None:
    """采集当场就把"刚抓到时它是多少"留下第一格。

    为什么这条要在 collect 而不是让 `enrich_metrics` 去补：`enrich_metrics` 挑活的条件是
    "一条快照都没有"（`video_ids_missing`），先记一条 publish 会让那些**永远等不到
    自己 24h** 的老作品从此不再被补抓 —— 而这里用的是 `meta` 上已有的数，一跳网络都不发。
    """
    await _seed_creator(storage)
    adapter = FakeAdapter(
        PLATFORM,
        videos=[make_video_meta(PLATFORM, "v1", counts={"view_count": 4242, "like_count": 7})],
        artifact_factory=_single_artifact,
    )
    reg = FakeRegistry({PLATFORM: adapter}, {PLATFORM: FakeConfig()})

    result = await make_collect_handler(PLATFORM)(
        make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus()), CollectParams()
    )

    assert result.summary["publish_snapshots"] == 1
    row = await storage.videos.find_by_platform_id(PLATFORM, "v1")
    assert row is not None
    snaps = await _snapshots(storage, row.id)
    assert [(s.checkpoint, s.view_count, s.like_count) for s in snaps] == [("publish", 4242, 7)]
    # 没给的项留 NULL，不补 0：那会让"平台没说"长成"这条零转发"。
    assert snaps[0].share_count is None


async def test_a_real_zero_is_recorded_and_not_treated_as_absent(storage, files) -> None:
    """`view_count=0` 是**有效读数**（刚发出去真没人看），不能被当成"没给数"而跳过。

    与上一条是一对：只测"空要跳"的实现，最省事的做法是 `if not meta.view_count` ——
    那正好把新作品的第一格全丢掉，而新作品恰恰是最需要这条基线的。
    """
    await _seed_creator(storage)
    adapter = FakeAdapter(
        PLATFORM,
        videos=[make_video_meta(PLATFORM, "v1", counts={"view_count": 0})],
        artifact_factory=_single_artifact,
    )
    reg = FakeRegistry({PLATFORM: adapter}, {PLATFORM: FakeConfig()})

    result = await make_collect_handler(PLATFORM)(
        make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus()), CollectParams()
    )

    assert result.summary["publish_snapshots"] == 1
    row = await storage.videos.find_by_platform_id(PLATFORM, "v1")
    snaps = await _snapshots(storage, (row.id))
    assert len(snaps) == 1 and snaps[0].view_count == 0


async def test_counts_absent_records_nothing_and_is_not_a_failure(storage, files) -> None:
    """平台没给计数 → 什么都不记，且**不算失败**。

    "这一家没回读数"是事实，不是错误；记一条失败会让 B站 那种"匿名档拿不到 stat"
    的每一轮采集都变成 partial，而真正该红的（写库挂了）就淹在里面了。
    """
    await _seed_creator(storage)
    adapter = FakeAdapter(
        PLATFORM, videos=[make_video_meta(PLATFORM, "v1")], artifact_factory=_single_artifact
    )
    reg = FakeRegistry({PLATFORM: adapter}, {PLATFORM: FakeConfig()})

    result = await make_collect_handler(PLATFORM)(
        make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus()), CollectParams()
    )

    assert result.status == "success"
    assert result.summary["publish_snapshots"] == 0
    assert result.failures == []
    row = await storage.videos.find_by_platform_id(PLATFORM, "v1")
    assert await _snapshots(storage, row.id) == []


async def test_a_failed_snapshot_write_keeps_the_video_and_shows_in_manifest(
    storage, files, monkeypatch: pytest.MonkeyPatch
) -> None:
    """快照写挂了：作品**留在库里**（它真的进来了），但清单必须看得见这一格红。

    V1 §1.3 的那条：不许为了"这一轮看起来干净"把附带的失败吞掉。stage 是 `metrics`
    而不是 `store`，因为后者说的是"作品没进库" —— 混起来会让人去查数据库连接。
    """
    await _seed_creator(storage)

    async def boom(*_a: object, **_kw: object) -> None:
        msg = "disk I/O error"
        raise OSError(msg)

    monkeypatch.setattr(storage.metrics, "put", boom)
    adapter = FakeAdapter(
        PLATFORM,
        videos=[make_video_meta(PLATFORM, "v1", counts={"view_count": 5})],
        artifact_factory=_single_artifact,
    )
    reg = FakeRegistry({PLATFORM: adapter}, {PLATFORM: FakeConfig()})

    result = await make_collect_handler(PLATFORM)(
        make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus()), CollectParams()
    )

    assert await storage.videos.count() == 1, "作品不该被附带的失败抹掉"
    assert result.summary["downloaded"] == 1
    assert result.summary["publish_snapshots"] == 0
    assert result.status == "partial"
    assert [(f.stage, f.video_id) for f in result.failures] == [("metrics", "v1")]
    assert "disk I/O error" in result.failures[0].error


async def test_a_second_round_does_not_rewrite_the_publish_cell(storage, files) -> None:
    """查重命中的那条**不再记**：`publish` 是"第一次抓到时长什么样"，重跑不该刷新它。

    这一格一旦被覆盖，"发布 24 小时破千"这类结论就会随重跑次数变。
    """
    await _seed_creator(storage)
    adapter = FakeAdapter(
        PLATFORM,
        videos=[make_video_meta(PLATFORM, "v1", counts={"view_count": 1})],
        artifact_factory=_single_artifact,
    )
    reg = FakeRegistry({PLATFORM: adapter}, {PLATFORM: FakeConfig()})
    handler = make_collect_handler(PLATFORM)

    await handler(
        make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus()), CollectParams()
    )
    adapter._videos = [make_video_meta(PLATFORM, "v1", counts={"view_count": 99999})]
    second = await handler(
        make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus()), CollectParams()
    )

    row = await storage.videos.find_by_platform_id(PLATFORM, "v1")
    snaps = await _snapshots(storage, row.id)
    assert second.summary["publish_snapshots"] == 0
    assert [(s.checkpoint, s.view_count) for s in snaps] == [("publish", 1)]


async def test_metrics_only_round_still_leaves_the_publish_cell(storage, files) -> None:
    """ "只采指标"那一站更要记这条：它不下载，读数就是它唯一的产出。"""
    await _seed_creator(storage)
    adapter = FakeAdapter(
        PLATFORM, videos=[make_video_meta(PLATFORM, "v1", counts={"like_count": 3})]
    )
    reg = FakeRegistry({PLATFORM: adapter}, {PLATFORM: FakeConfig()})

    result = await make_collect_handler(PLATFORM)(
        make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus()),
        CollectParams(metrics_only=True),
    )

    assert result.summary["registered_metrics_only"] == 1
    assert result.summary["publish_snapshots"] == 1
    assert adapter.download_calls == []
