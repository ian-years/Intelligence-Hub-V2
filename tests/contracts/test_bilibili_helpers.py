"""纯函数层的逐形状看护（B站）。

这些不是"凑覆盖率"：每一个分支都对应一次"对面给的东西不是我以为的那样"。
`_as_int` 把 bool 判成 None 是因为 `True` 是 `int` 的子类（会写出 `fans=1`）；
`require_http_url` 抛错是因为一条相对地址进了下载队列只会变成一句难懂的 HTTP 错。
这样的判据不测，就只能等真数据来测。
"""

from __future__ import annotations

from datetime import UTC
from pathlib import Path
from typing import Any

import pytest

from intelligence_hub_v2.errors import PlatformError
from intelligence_hub_v2.infra.ytdlp import YtDlpResult
from intelligence_hub_v2.models.creator import CreatorRef
from intelligence_hub_v2.models.media import (
    MediaArtifact,
    VideoAudioPairArtifact,
    audio_path_of,
    total_size_bytes,
)
from intelligence_hub_v2.platforms.bilibili import listing, media, subtitles, urls
from intelligence_hub_v2.platforms.bilibili.adapter import (
    _line_reporter,
    _optional_int,
    _size,
    _worst_status,
)

# --------------------------------------------------------------------------- #
# urls
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("BV1GJ411x7h7", "BV1GJ411x7h7"),
        ("https://www.bilibili.com/video/BV1GJ411x7h7/?spm=1", "BV1GJ411x7h7"),
        ("  BV1GJ411x7h7  ", "BV1GJ411x7h7"),
        ("av114514", ""),
        ("BV1tooShort", ""),
        ("", ""),
        (None, ""),
        (123, ""),
    ],
)
def test_extract_bvid(value: object, expected: str) -> None:
    assert urls.extract_bvid(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("BV1GJ411x7h7", True), ("BV1GJ411x7h", False), ("", False), (None, False)],
)
def test_is_bvid(value: object, expected: bool) -> None:
    assert urls.is_bvid(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("486906719", "486906719"),
        ("https://space.bilibili.com/486906719/video", "486906719"),
        ("https://m.bilibili.com/space/42", "42"),
        ("https://space.bilibili.com/", ""),
        ("https://www.bilibili.com/video/BV1GJ411x7h7/", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_extract_mid(value: object, expected: str) -> None:
    assert urls.extract_mid(value) == expected


def test_is_short_link_only_for_b23() -> None:
    assert urls.is_short_link("https://b23.tv/abc") is True
    assert urls.is_short_link("https://www.bilibili.com/video/BV1GJ411x7h7/") is False
    assert urls.is_short_link("") is False


def test_url_shapes_are_canonical() -> None:
    assert urls.space_video_url("42") == "https://space.bilibili.com/42/video"
    assert urls.canonical_video_url("BV1GJ411x7h7").endswith("/")
    assert f"cid={123}" in urls.player_api_url("BV1", 123)
    assert "mid=42" in urls.card_api_url("42")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("//i0.hdslb.com/x.jpg", "https://i0.hdslb.com/x.jpg"),
        ("https://a.com/x", "https://a.com/x"),
        ("http://a.com/x", "http://a.com/x"),
    ],
)
def test_require_http_url_accepts_and_normalises(value: str, expected: str) -> None:
    assert urls.require_http_url(value, context="测试") == expected


@pytest.mark.parametrize("junk", ["", "   ", "javascript:alert(1)", "/relative/path", "a.com/x"])
def test_require_http_url_refuses_non_urls(junk: str) -> None:
    with pytest.raises(PlatformError, match="应该是一个 http"):
        urls.require_http_url(junk, context="测试")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("//i0.hdslb.com/x.jpg", "https://i0.hdslb.com/x.jpg"),
        ("https://a.com/x", "https://a.com/x"),
        ("", None),
        ("javascript:alert(1)", None),
        ("不是 URL", None),
    ],
)
def test_as_http_url_is_lenient(value: object, expected: str | None) -> None:
    got = urls.as_http_url(value)
    assert (None if got is None else str(got)) == expected


def test_require_model_url_raises_where_as_http_url_returns_none() -> None:
    assert urls.as_http_url("javascript:alert(1)") is None
    with pytest.raises(PlatformError, match="应该是一个 http"):
        urls.require_model_url("javascript:alert(1)", context="作品页")


# --------------------------------------------------------------------------- #
# listing 的小工具
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (12, 12),
        ("12", 12),
        ("1,234", 1234),
        (True, None),
        (False, None),
        (-1, None),
        ("", None),
        ("abc", None),
        (None, None),
        (3.7, 3),
    ],
)
def test_as_int_never_mistakes_a_bool_for_a_count(value: object, expected: int | None) -> None:
    """`isinstance(True, int)` 是 True —— 不挡一下就会把"是"写成 1 个播放量。"""
    assert listing._as_int(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (213, 213.0),
        ("213", 213.0),
        (True, None),
        (-5, None),
        ("", None),
        ("abc", None),
        (None, None),
    ],
)
def test_as_float(value: object, expected: float | None) -> None:
    assert listing._as_float(value) == expected


def test_as_aware_rejects_zero_and_keeps_utc() -> None:
    assert listing._as_aware(0) is None
    assert listing._as_aware(-1) is None
    assert listing._as_aware("not a stamp") is None
    aware = listing._as_aware(1577835803)
    assert aware is not None and aware.tzinfo is UTC


def test_mapping_helper_tolerates_any_shape() -> None:
    assert listing._mapping({"a": 1}) == {"a": 1}
    assert listing._mapping([1, 2]) == {}
    assert listing._mapping(None) == {}


def test_api_headers_carry_ua_and_referer() -> None:
    headers = listing.api_headers(referer="https://www.bilibili.com/video/BV1/")
    assert headers["Referer"].startswith("https://www.bilibili.com")
    assert "Mozilla" in headers["User-Agent"]


def test_bili_card_url_defaults_to_the_canonical_page() -> None:
    """`BiliCard` 不给 url 时补规范地址：后面所有分支都用得到它。"""
    card = listing.BiliCard(bvid="BV1GJ411x7h7", title="x")
    assert card.url == "https://www.bilibili.com/video/BV1GJ411x7h7/"
    explicit = listing.BiliCard(bvid="BV1GJ411x7h7", url="https://m.bilibili.com/awatch")
    assert explicit.url == "https://m.bilibili.com/awatch"


def test_entries_to_cards_respects_limit_and_skips_junk() -> None:
    entries: list[Any] = [
        {"id": "BV1GJ411x7h7"},
        "not a dict",
        {"id": "av1"},
        {"id": "BV1xx411c7mD"},
    ]
    assert [c.bvid for c in listing.entries_to_cards(entries, limit=1)] == ["BV1GJ411x7h7"]
    assert len(listing.entries_to_cards(entries, limit=9)) == 2


def test_manifest_rows_that_are_not_dicts_are_ignored() -> None:
    entries = ["nope", 42, {"creator": "not-a-dict", "videos": [{"bvid": "BV1GJ411x7h7"}]}]
    assert listing.manifest_cards_for(entries, mid="42", limit=5) == []


def test_manifest_matching_falls_back_to_name_when_mid_is_absent() -> None:
    entries = [{"creator": {"name": "索尼音乐中国"}, "videos": [{"bvid": "BV1GJ411x7h7"}]}]
    assert len(listing.manifest_cards_for(entries, mid="42", name="索尼音乐中国", limit=5)) == 1
    assert listing.manifest_cards_for(entries, mid="42", name="别人", limit=5) == []


def test_manifest_videos_dict_is_accepted() -> None:
    entries = [{"creator": {"mid": "42"}, "selected": {"bvid": "BV1GJ411x7h7"}}]
    assert [c.bvid for c in listing.manifest_cards_for(entries, mid="42", limit=5)] == [
        "BV1GJ411x7h7"
    ]


def test_a_list_key_that_is_not_videos_shaped_is_empty() -> None:
    assert listing.manifest_cards_for([{"creator": {"mid": "42"}, "videos": []}], mid="42") == []


def test_space_url_for_uses_the_reference() -> None:
    ref = CreatorRef(
        platform="bilibili", platform_id="42", profile_url="https://space.bilibili.com/42"
    )
    assert listing.space_url_for(ref) == "https://space.bilibili.com/42/video"


def test_card_to_video_meta_needs_a_creator_ref() -> None:
    card = listing.view_to_card("BV1GJ411x7h7", {"bvid": "BV1GJ411x7h7", "pages": "not-a-list"})
    assert card.bvid == "BV1GJ411x7h7"
    # `pages` 缺省按 1：不是"这条视频没有分 P"，而是"接口没给分 P 信息"
    assert card.cid is None and card.pages == 1


# --------------------------------------------------------------------------- #
# media
# --------------------------------------------------------------------------- #


def test_ytdlp_failure_reason_always_has_something() -> None:
    empty = YtDlpResult(ok=False, variant=None, stdout="", stderr="", returncode=7, attempts=())
    reason = media.ytdlp_failure_reason(empty)
    assert "yt-dlp 没有产生任何尝试记录" in reason and "exit 7" in reason


def test_cookie_ladder_rung_kinds_are_typed() -> None:
    ladder = media.CookieLadder(variants=(), cookie_file=None, browser=None, note=None)
    assert ladder.rung_kinds == ()
    assert ladder.logged_in is False


def test_media_parts_description_for_empty() -> None:
    assert media.MediaParts(kind="empty").description == "什么都没拿到"


# --------------------------------------------------------------------------- #
# subtitles
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [(2, 2), ("3", 3), (True, None), (None, None), ("x", None), (-1, -1)],
)
def test_as_optional_int(value: object, expected: int | None) -> None:
    assert subtitles._as_optional_int(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1.5, 1.5), ("2.5", 2.5), (None, None), (-1, None), ("x", None), (True, None)],
)
def test_as_time(value: object, expected: float | None) -> None:
    assert subtitles._as_time(value) == expected


def test_track_payload_without_the_subtitle_block_is_empty() -> None:
    assert subtitles.parse_subtitle_tracks({}) == []
    assert subtitles.parse_subtitle_tracks({"data": {}}) == []
    assert subtitles.parse_subtitle_tracks({"data": {"subtitle": {"subtitles": "no"}}}) == []
    assert subtitles.parse_subtitle_tracks({"data": {"subtitle": {}}}) == []


def test_choose_track_on_nothing_returns_nothing() -> None:
    assert subtitles.choose_track([]) is None


def test_build_transcript_with_a_blank_language() -> None:
    segments = [subtitles.TranscriptSegment(start_seconds=0, end_seconds=1, text="一句")]
    transcript = subtitles.build_transcript(segments, lan="")
    assert transcript.language is None
    assert transcript.char_count == len("一句")
    assert transcript.sentence_count == 1
    assert transcript.engine == "bilibili_subtitle"


def test_subtitle_body_rejects_non_mappings() -> None:
    assert subtitles.parse_subtitle_body(None) == []
    assert subtitles.parse_subtitle_body({"body": "no"}) == []
    assert subtitles.parse_subtitle_body({"body": [1, "x", {"content": "没有时间戳"}]}) == []


async def test_fetch_transcript_returns_none_when_the_track_list_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_tracks(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(subtitles, "fetch_subtitle_tracks", no_tracks)
    assert await subtitles.fetch_transcript(None, "BV1GJ411x7h7", 1) is None  # type: ignore[arg-type]


async def test_fetch_transcript_propagates_a_failed_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(*args: Any, **kwargs: Any) -> list[Any]:
        raise PlatformError("bilibili", "subtitle", "被风控")

    monkeypatch.setattr(subtitles, "fetch_subtitle_tracks", boom)
    with pytest.raises(PlatformError, match="被风控"):
        await subtitles.fetch_transcript(None, "BV1GJ411x7h7", 1)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 适配器内部的小工具
# --------------------------------------------------------------------------- #


def test_optional_int_and_size_helpers() -> None:
    assert _optional_int(True) is None
    assert _optional_int("1,2") == 12
    assert _optional_int(None) is None
    with pytest.raises(Exception, match="读不到"):
        _size(Path("nope/nowhere.mp4"))


def test_worst_status_ranks_components() -> None:
    assert _worst_status({}) == "ok"
    assert _worst_status({"a": "ok", "b": "degraded"}) == "degraded"
    assert _worst_status({"a": "degraded", "b": "unreachable"}) == "unreachable"


def test_line_reporter_is_none_without_a_callback() -> None:
    assert _line_reporter(None) is None
    seen: list[float] = []
    cb = _line_reporter(seen.append)
    assert cb is not None
    cb("[download]  25.0% of 1MB")
    cb("[info] 不是进度")
    assert seen == [0.25]


def test_a_pair_artifact_points_the_transcriber_at_the_audio_track(tmp_path: Path) -> None:
    """V1 §7.21 的最终目的：`audio_path_of()` 必须给出**音频**那条轨。

    这条把 B站 的分片识别与"转写层唯一可信的那个函数"接在一起 ——
    中间任何一环改名或换语义，这里就会红，而不是等到 ffmpeg 报出那句
    `Output file does not contain any stream`（长得像"ffmpeg 没装"）。
    """
    video = tmp_path / "media.f30064.mp4"
    audio = tmp_path / "media.f30280.m4a"
    video.write_bytes(b"x" * 300)
    audio.write_bytes(b"x" * 100)
    parts = media.classify_artifacts([video, audio])
    artifact: MediaArtifact = VideoAudioPairArtifact(
        video_path=parts.video,
        audio_path=parts.audio,
        video_size_bytes=300,
        audio_size_bytes=100,
    )
    assert audio_path_of(artifact) == audio
    assert total_size_bytes(artifact) == 400
