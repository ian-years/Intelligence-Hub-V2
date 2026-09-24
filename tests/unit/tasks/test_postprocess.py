"""`tasks/postprocess.py`：字幕优先，其次本地 ASR（T1.2）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from tests.unit.tasks.conftest import FakeAdapter, FakeBus, FakeConfig, FakeRegistry, make_ctx

from intelligence_hub_v2.asr.engine import EngineStatus
from intelligence_hub_v2.errors import ListError
from intelligence_hub_v2.models.event import EventType
from intelligence_hub_v2.models.media import (
    SingleFileArtifact,  # noqa: F401  (typing only via factory signatures)
)
from intelligence_hub_v2.models.transcript import Transcript, TranscriptSegment
from intelligence_hub_v2.models.video import VideoDraft
from intelligence_hub_v2.platforms.base import Capabilities
from intelligence_hub_v2.tasks import postprocess as post_mod
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


async def _seed_video(
    storage,
    *,
    platform: str,
    vid: str,
    media_path: str | None,
    aux: list[str] | None = None,
) -> int:
    row = await storage.videos.insert(
        VideoDraft(
            platform=platform,
            platform_video_id=vid,
            title=f"t-{vid}",
            media_path=media_path,
            media_aux_paths_json=json.dumps(aux or [], ensure_ascii=False),
        )
    )
    return row.id


def _ctx(storage, files, platform: str, *, adapter: FakeAdapter | None = None):
    adapter = adapter or FakeAdapter(platform, capabilities=_NO_SUB_CAPS)
    return make_ctx(
        storage=storage,
        files=files,
        registry=FakeRegistry({platform: adapter}, {platform: FakeConfig()}),
        bus=FakeBus(),
    )


def _fake_asr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, text: str) -> dict[str, Any]:
    """换掉四个接缝（引擎状态 / 有没有音频轨 / 抽音频 / 转写），记下调用。

    真引擎那一跑在 `docs/progress/2026-09-24.md`（67 秒真口播 → 11 句 / 9.0× 实时）。
    这里要钉的是 handler 的接线，所以连 numpy 都不该被牵扯进来。
    """
    lines = [line for line in text.splitlines() if line.strip()]
    segments = [
        TranscriptSegment(start_seconds=index * 4.0, end_seconds=index * 4.0 + 3.5, text=line)
        for index, line in enumerate(lines)
    ]
    calls: dict[str, Any] = {"transcribe_calls": 0, "extract_sources": [], "wav_paths": []}

    monkeypatch.setattr(
        post_mod,
        "asr_detect",
        lambda **kwargs: EngineStatus(True, "sherpa_sense_voice", "", tmp_path),
    )

    async def fake_has(media_path: Path) -> bool:
        return True

    async def fake_extract(media_path: Path, output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"RIFFfake-wav")
        calls["extract_sources"].append(media_path)
        calls["wav_paths"].append(output_path)
        return output_path

    def fake_transcribe(wav_path: Path, **kwargs: Any) -> Transcript:
        calls["transcribe_calls"] += 1
        return Transcript(
            engine="sherpa_sense_voice",
            language=None,
            text=text,
            char_count=len(text),
            sentence_count=len(segments),
            segments=segments,
        )

    monkeypatch.setattr(post_mod, "has_audio_stream", fake_has)
    monkeypatch.setattr(post_mod, "extract_audio", fake_extract)
    monkeypatch.setattr(post_mod, "transcribe_wav", fake_transcribe)
    return calls


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
    # 文本文件以换行结尾（V1 的 `clean_path.write_text(cleaned_text + "\n")` 同一条规矩），
    # 两条路（字幕 / ASR）现在写出来的字节形状一致。
    assert written.read_text(encoding="utf-8") == "第一句。第二句。\n"
    assert any(e.type is EventType.TRANSCRIPT_READY for e in bus.events)
    assert adapter.subtitle_calls == ["BV1"]


# 「没有字幕轨」那一支在 T1.3 之后不再停在 no_subtitle：它会回落到本地 ASR。
# 那一条路的判据（有轨不跑 ffmpeg / 没轨回落 / 问失败不回落）在
# tests/integration/test_bili_subtitle_preferred.py —— 那里接缝被显式换掉，
# 不会因为跑的人本机装没装权重而给出不同答案。


async def test_platform_without_subtitle_support_goes_through_local_asr(
    storage, files, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """抖音那条路：抽音频 → 转写 → 三份产物 + `transcripts` 行。

    引擎与 ffmpeg 在这里是假的（真机那一跑记在 `docs/progress/2026-09-24.md`），
    要钉的是**接线**：文件名、目录、入库字段、事件，以及"不产 V1 那份 speech-raw.txt"。
    """
    vid = await _seed_video(
        storage, platform="douyin", vid="7", media_path="media/douyin/x/7/media.mp4"
    )
    lines = ["今天讲三件事，先说第一件。", "第二件是这个，跟工具有关。", "第三件说完就下播。"]
    fake = _fake_asr(monkeypatch, tmp_path, text="\n".join(lines))

    result = await run_postprocess(
        ctx=_ctx(storage, files, "douyin"), params=PostprocessParams(video_ids=[vid])
    )

    assert result.status == "success"
    assert result.summary["transcribed"] == 1
    transcript_dir = files.abs("media/douyin/x/7/media.mp4").parent / "transcript"
    assert (transcript_dir / "speech-clean.txt").read_text(encoding="utf-8") == "\n".join(
        lines
    ) + "\n"
    assert [s["text"] for s in json.loads((transcript_dir / "segments.json").read_text())] == lines
    reference = (transcript_dir / "reference.md").read_text(encoding="utf-8")
    assert "本文件性质: 本地自动抽取参考" in reference and "候选句" in reference
    assert not (transcript_dir / "speech-raw.txt").exists(), (
        "V1 那份 speech-raw.txt 在 V2 不落：与 clean 只差空白折叠，等于给前端第二个答案"
    )

    record = await storage.transcripts.get_for_video(vid)
    assert record is not None
    assert record.engine == "sherpa_sense_voice" and record.sentence_count == 3
    assert record.text_path.endswith("transcript/speech-clean.txt")
    assert fake["transcribe_calls"] == 1
    assert fake["wav_paths"], "没抽音频就直接转写了？"


async def test_asr_feeds_ffmpeg_the_audio_track_not_the_video_only_one(
    storage, files, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DASH 未合并那条路：喂给 ffmpeg 的必须是音频轨（aux），不是主文件（V1 §7.21）。

    主文件是 `.f137.mp4` 那种纯视频流，直接喂 ffmpeg 得到的是
    `Output file does not contain any stream` —— 字面意思会把人引向"ffmpeg 没装"。
    这里挂在不支持字幕的平台上（B站 那条"问字幕说没有轨 → 回落听音频"是 T1.3）。
    """
    vid = await _seed_video(
        storage,
        platform="douyin",
        vid="7",
        media_path="media/douyin/x/7/media.mp4",
        aux=["media/douyin/x/7/media.f140.m4a"],
    )
    fake = _fake_asr(monkeypatch, tmp_path, text="这是一条有内容的口播稿子，能过门限。")

    await run_postprocess(
        ctx=_ctx(storage, files, "douyin"), params=PostprocessParams(video_ids=[vid])
    )

    assert fake["extract_sources"] == [files.abs("media/douyin/x/7/media.f140.m4a")]
    assert files.abs("media/douyin/x/7/media.mp4") not in fake["extract_sources"]


async def test_a_transcript_below_the_floor_is_recorded_but_not_beautified(
    storage, files, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反幻觉闸的第二层：只换来"我。我。"这种结果时**不产 reference.md**。

    仍然 attach 一条行：这条作品确实转过了，不记的话每一轮都会被重挑出来重跑。
    """
    vid = await _seed_video(
        storage, platform="douyin", vid="7", media_path="media/douyin/x/7/media.mp4"
    )
    fake = _fake_asr(monkeypatch, tmp_path, text="我。我。我。")

    result = await run_postprocess(
        ctx=_ctx(storage, files, "douyin"), params=PostprocessParams(video_ids=[vid])
    )

    assert result.summary["no_speech"] == 1 and result.summary["transcribed"] == 0
    transcript_dir = files.abs("media/douyin/x/7/media.mp4").parent / "transcript"
    assert (transcript_dir / "speech-clean.txt").is_file()
    assert not (transcript_dir / "reference.md").exists(), "从幻觉句里抽候选句是给噪声化妆"
    record = await storage.transcripts.get_for_video(vid)
    assert record is not None and record.char_count > 0
    assert fake["transcribe_calls"] == 1


async def test_a_missing_engine_fails_the_batch_without_fabricating(
    storage, files, monkeypatch: pytest.MonkeyPatch
) -> None:
    """引擎缺位（没装包 / 没下权重）时：整批红，**一条稿子都不产出**（AGENTS §1.3）。"""
    vid = await _seed_video(
        storage, platform="douyin", vid="7", media_path="media/douyin/x/7/media.mp4"
    )
    monkeypatch.setattr(
        post_mod,
        "asr_detect",
        lambda **kwargs: EngineStatus(
            False, "sherpa_sense_voice", "未安装 sherpa-onnx。修复：uv sync --extra asr"
        ),
    )

    result = await run_postprocess(
        ctx=_ctx(storage, files, "douyin"), params=PostprocessParams(video_ids=[vid])
    )

    assert result.status == "failed"
    assert result.summary["asr_blocked"] == 1
    assert result.failures[0].error_kind == "AsrUnavailable"
    assert "uv sync --extra asr" in result.failures[0].error
    assert await storage.transcripts.get_for_video(vid) is None
    assert not (files.abs("media/douyin/x/7") / "transcript").exists()


async def test_a_media_without_audio_track_never_starts_ffmpeg(
    storage, files, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """探针说"这条没有音频轨"，就别去跑 ffmpeg（V1 §7.21）。

    跑了的报错是 `Output file does not contain any stream`，字面意思会把人引向
    "ffmpeg 没装"，而方向是"这条媒体压根没有声音"。
    """
    vid = await _seed_video(
        storage, platform="douyin", vid="7", media_path="media/douyin/x/7/media.mp4"
    )
    fake = _fake_asr(monkeypatch, tmp_path, text="这一句足够长可以通过反幻觉门限。")

    async def no_audio(media_path: Path) -> bool:
        return False

    monkeypatch.setattr(post_mod, "has_audio_stream", no_audio)
    result = await run_postprocess(
        ctx=_ctx(storage, files, "douyin"), params=PostprocessParams(video_ids=[vid])
    )

    assert result.summary["no_audio"] == 1 and fake["extract_sources"] == []
    assert await storage.transcripts.get_for_video(vid) is None


async def test_video_without_media_is_deferred(storage, files) -> None:
    vid = await _seed_video(storage, platform="bilibili", vid="BV1", media_path=None)
    adapter = FakeAdapter("bilibili", subtitles=_transcript(), capabilities=_SUB_CAPS)
    reg = FakeRegistry({"bilibili": adapter}, {"bilibili": FakeConfig()})
    ctx = make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus())

    result = await run_postprocess(ctx, PostprocessParams(video_ids=[vid]))

    assert result.summary["no_media"] == 1
    assert adapter.subtitle_calls == []


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
