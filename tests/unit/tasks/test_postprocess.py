"""`tasks/postprocess.py`：字幕优先，本地 ASR 留 V2.1（V2.0 最小实现）。"""

from __future__ import annotations

from tests.unit.tasks.conftest import FakeAdapter, FakeBus, FakeConfig, FakeRegistry, make_ctx

from intelligence_hub_v2.errors import ListError
from intelligence_hub_v2.models.event import EventType
from intelligence_hub_v2.models.media import (
    SingleFileArtifact,  # noqa: F401  (typing only via factory signatures)
)
from intelligence_hub_v2.models.transcript import Transcript, TranscriptSegment
from intelligence_hub_v2.models.video import VideoDraft
from intelligence_hub_v2.platforms.base import Capabilities
from intelligence_hub_v2.tasks.params import PostprocessParams
from intelligence_hub_v2.tasks.postprocess import run_postprocess

_SUB_CAPS = Capabilities(
    needs_browser=False,
    needs_cookies=False,
    cookie_variants=("none",),
    supports_subtitles=True,
    supports_dash_split=False,
    list_strategy="yt_dlp_flat",
    media_strategy="yt_dlp",
)
_NO_SUB_CAPS = Capabilities(
    needs_browser=False,
    needs_cookies=False,
    cookie_variants=("none",),
    supports_subtitles=False,
    supports_dash_split=False,
    list_strategy="browser_scroll",
    media_strategy="yt_dlp_with_fallback",
)


def _transcript() -> Transcript:
    return Transcript(
        engine="bilibili_subtitle",
        language="zh-CN",
        text="第一句。第二句。",
        char_count=8,
        sentence_count=2,
        segments=[TranscriptSegment(start_seconds=0.0, end_seconds=1.5, text="第一句。")],
    )


async def _seed_video(storage, *, platform: str, vid: str, media_path: str | None) -> int:
    row = await storage.videos.insert(
        VideoDraft(
            platform=platform, platform_video_id=vid, title=f"t-{vid}", media_path=media_path
        )
    )
    return row.id


async def test_subtitle_writes_file_and_attaches(storage, files) -> None:
    vid = await _seed_video(
        storage, platform="bilibili", vid="BV1", media_path="media/bilibili/x/BV1/media.mp4"
    )
    adapter = FakeAdapter("bilibili", subtitles=_transcript(), capabilities=_SUB_CAPS)
    reg = FakeRegistry({"bilibili": adapter}, {"bilibili": FakeConfig()})
    bus = FakeBus()
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=bus)

    result = await run_postprocess(ctx, PostprocessParams(video_ids=[vid]))

    assert result.status == "success"
    assert result.summary["transcribed"] == 1
    record = await storage.transcripts.get_for_video(vid)
    assert record is not None and record.engine == "bilibili_subtitle"
    written = files.abs(record.text_path)
    assert written.read_text(encoding="utf-8") == "第一句。第二句。"
    assert any(e.type is EventType.TRANSCRIPT_READY for e in bus.events)
    assert adapter.subtitle_calls == ["BV1"]


async def test_missing_subtitle_track_counts_as_no_subtitle(storage, files) -> None:
    """`fetch_subtitles` 返回 None = "确实没有轨"，不是失败（契约：没问到才抛）。"""
    vid = await _seed_video(
        storage, platform="bilibili", vid="BV1", media_path="media/bilibili/x/BV1/media.mp4"
    )
    adapter = FakeAdapter("bilibili", subtitles=None, capabilities=_SUB_CAPS)
    reg = FakeRegistry({"bilibili": adapter}, {"bilibili": FakeConfig()})
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus())

    result = await run_postprocess(ctx, PostprocessParams(video_ids=[vid]))

    assert result.summary["no_subtitle"] == 1
    assert await storage.transcripts.get_for_video(vid) is None


async def test_platform_without_subtitle_support_is_deferred_to_asr_v2_1(storage, files) -> None:
    vid = await _seed_video(
        storage, platform="douyin", vid="7", media_path="media/douyin/x/7/media.mp4"
    )
    adapter = FakeAdapter("douyin", capabilities=_NO_SUB_CAPS)
    reg = FakeRegistry({"douyin": adapter}, {"douyin": FakeConfig()})
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus())

    result = await run_postprocess(ctx, PostprocessParams(video_ids=[vid]))

    assert result.summary["needs_asr_v2_1"] == 1
    assert adapter.subtitle_calls == []  # 不支持字幕的平台连问都不问


async def test_subtitle_fetch_error_keeps_original_text_and_fails(storage, files) -> None:
    vid = await _seed_video(
        storage, platform="bilibili", vid="BV1", media_path="media/bilibili/x/BV1/media.mp4"
    )
    adapter = FakeAdapter(
        "bilibili",
        subtitle_error=ListError("bilibili", "list", "player/v2 412 blocked"),
        capabilities=_SUB_CAPS,
    )
    reg = FakeRegistry({"bilibili": adapter}, {"bilibili": FakeConfig()})
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus())

    result = await run_postprocess(ctx, PostprocessParams(video_ids=[vid]))

    assert result.status == "failed"
    assert any("412 blocked" in f.error for f in result.failures)


async def test_video_without_media_is_deferred(storage, files) -> None:
    vid = await _seed_video(storage, platform="bilibili", vid="BV1", media_path=None)
    adapter = FakeAdapter("bilibili", subtitles=_transcript(), capabilities=_SUB_CAPS)
    reg = FakeRegistry({"bilibili": adapter}, {"bilibili": FakeConfig()})
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus())

    result = await run_postprocess(ctx, PostprocessParams(video_ids=[vid]))

    assert result.summary["needs_asr_v2_1"] == 1
    assert adapter.subtitle_calls == []


async def test_default_targets_are_videos_missing_transcripts(storage, files) -> None:
    vid = await _seed_video(
        storage, platform="bilibili", vid="BV1", media_path="media/bilibili/x/BV1/media.mp4"
    )
    adapter = FakeAdapter("bilibili", subtitles=_transcript(), capabilities=_SUB_CAPS)
    reg = FakeRegistry({"bilibili": adapter}, {"bilibili": FakeConfig()})
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus())

    result = await run_postprocess(ctx, PostprocessParams())  # 不给 video_ids

    assert result.summary["transcribed"] == 1
    assert await storage.transcripts.get_for_video(vid) is not None
