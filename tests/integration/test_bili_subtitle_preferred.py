"""T1.3：B站 那条「字幕优先、没有轨就回落听音频」的顺序。

真：SQLite、`data/` 树、handler、落盘与入库。假：适配器交回的 transcript（字幕**解析**
那一半已有契约测试 `tests/contracts/test_bilibili_adapter.py::TestSubtitles`，含
"无轨返回 None"这条），以及 ffmpeg 进程与 ASR 引擎。

为什么用例挂在 handler 上而不是真适配器上：T1.3 改的是**顺序**（谁先谁后、失败要不要回落），
解析部分一行没动；把两半接起来的真正风险是"没有轨时静默什么都不做"（V2.0 就是这样：
记一个 `no_subtitle` 就过去了），而那只有跑到 handler 才看得见。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from tests.unit.tasks.conftest import FakeAdapter, FakeBus, FakeConfig, FakeRegistry, make_ctx

from intelligence_hub_v2.asr.engine import EngineStatus
from intelligence_hub_v2.errors import ListError
from intelligence_hub_v2.models.transcript import Transcript, TranscriptSegment
from intelligence_hub_v2.models.video import VideoDraft
from intelligence_hub_v2.platforms.base import Capabilities
from intelligence_hub_v2.storage.files import FileStorage
from intelligence_hub_v2.tasks import postprocess as post_mod
from intelligence_hub_v2.tasks.params import PostprocessParams
from intelligence_hub_v2.tasks.postprocess import run_postprocess

_SUB_CAPS = Capabilities(
    needs_browser=False,
    needs_cookies=False,
    cookie_variants=("none",),
    supports_subtitles=True,
    supports_dash_split=True,
    list_strategy="yt_dlp_flat",
    media_strategy="yt_dlp",
)


@pytest.fixture
def files(tmp_path: Path) -> FileStorage:
    storage = FileStorage(tmp_path / "data")
    storage.ensure_dirs()
    return storage


async def _seed(storage, files: FileStorage) -> int:
    rel = "media/bilibili/UP主/BV1/media.mp4"
    files.abs(rel).parent.mkdir(parents=True, exist_ok=True)
    files.abs(rel).write_bytes(b"fake-mp4")
    row = await storage.videos.insert(
        VideoDraft(platform="bilibili", platform_video_id="BV1", title="测试作品", media_path=rel)
    )
    return row.id


def _ctx(storage, files: FileStorage, adapter: FakeAdapter):
    return make_ctx(
        storage=storage,
        files=files,
        registry=FakeRegistry({"bilibili": adapter}, {"bilibili": FakeConfig()}),
        bus=FakeBus(),
    )


def _subtitle_transcript() -> Transcript:
    text = "第一句是歌词。\n第二句还是歌词，长度够通过最短判据。\n第三句收尾。"
    return Transcript(
        engine="bilibili_subtitle",
        language="zh-CN",
        text=text,
        char_count=len(text),
        sentence_count=3,
        segments=[
            TranscriptSegment(start_seconds=0.0, end_seconds=2.0, text="第一句是歌词。"),
            TranscriptSegment(start_seconds=2.0, end_seconds=4.0, text="第二句还是歌词。"),
            TranscriptSegment(start_seconds=4.0, end_seconds=6.0, text="第三句收尾。"),
        ],
    )


def _seams(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, text: str) -> dict[str, Any]:
    """ASR 那一侧的四个接缝；`extract_sources` 用来判"到底有没有去跑 ffmpeg"。"""
    lines = [line for line in text.splitlines() if line.strip()]
    calls: dict[str, Any] = {"extract_sources": [], "transcribe_calls": 0}

    monkeypatch.setattr(
        post_mod,
        "asr_detect",
        lambda **kwargs: EngineStatus(True, "sherpa_sense_voice", "", tmp_path),
    )

    async def no_probe(media_path: Path) -> bool:
        return True

    async def fake_extract(media_path: Path, output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"RIFF")
        calls["extract_sources"].append(media_path)
        return output_path

    def fake_transcribe(wav_path: Path, **kwargs: Any) -> Transcript:
        calls["transcribe_calls"] += 1
        return Transcript(
            engine="sherpa_sense_voice",
            language=None,
            text=text,
            char_count=len(text),
            sentence_count=len(lines),
            segments=[
                TranscriptSegment(start_seconds=i * 4.0, end_seconds=i * 4.0 + 3.5, text=line)
                for i, line in enumerate(lines)
            ],
        )

    monkeypatch.setattr(post_mod, "has_audio_stream", no_probe)
    monkeypatch.setattr(post_mod, "extract_audio", fake_extract)
    monkeypatch.setattr(post_mod, "transcribe_wav", fake_transcribe)
    return calls


async def test_a_track_present_video_never_starts_ffmpeg(
    storage, files: FileStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """有轨就走字幕：ffmpeg 一次都不该被叫（字幕是平台给的现成文本，比听一遍便宜得多）。"""
    vid = await _seed(storage, files)
    calls = _seams(monkeypatch, files.root, text="这一句足够长可以通过反幻觉门限的稿子。")
    adapter = FakeAdapter("bilibili", subtitles=_subtitle_transcript(), capabilities=_SUB_CAPS)

    result = await run_postprocess(
        _ctx(storage, files, adapter), PostprocessParams(video_ids=[vid])
    )

    assert result.status == "success" and result.summary["transcribed"] == 1
    assert result.summary["subtitle_missed"] == 0
    assert calls["extract_sources"] == [] and calls["transcribe_calls"] == 0
    record = await storage.transcripts.get_for_video(vid)
    assert record is not None and record.engine == "bilibili_subtitle"
    assert record.sentence_count == 3
    clean = files.transcript_path(files.abs(str(record.text_path)).parent.parent)
    assert "第一句是歌词。" in clean.read_text(encoding="utf-8")


async def test_a_track_absent_video_falls_back_to_local_asr(
    storage, files: FileStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`fetch_subtitles` 交回 None = "确实没有轨"（契约），于是回落到听音频。

    V2.0 在这里只记一个 `no_subtitle` 就过去了 —— 那条作品**永远不会有任何稿子**，
    而"记了一笔"看起来像处理过了。这一条就是 T1.3 存在的全部理由。
    """
    vid = await _seed(storage, files)
    text = "这一句是听出来的：先讲怎么把字幕拿下来，拿不到再回去听音频，两条路都写在同一个任务里。"
    calls = _seams(monkeypatch, files.root, text=text)
    adapter = FakeAdapter("bilibili", subtitles=None, capabilities=_SUB_CAPS)

    result = await run_postprocess(
        _ctx(storage, files, adapter), PostprocessParams(video_ids=[vid])
    )

    assert result.status == "success"
    assert result.summary["subtitle_missed"] == 1 and result.summary["transcribed"] == 1
    assert calls["extract_sources"] and calls["transcribe_calls"] == 1
    record = await storage.transcripts.get_for_video(vid)
    assert record is not None and record.engine == "sherpa_sense_voice"
    assert record.text_path.endswith("transcript/speech-clean.txt")


async def test_a_failed_subtitle_question_does_not_go_run_ffmpeg(
    storage, files: FileStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "没问到"（抛）与"确实没有"（None）必须分开：抛了就记失败、下一轮再问，
    而不是顺手起个 ffmpeg 把这条转一遍 —— 那会让一次 412 换来一次白烧的 CPU。"""
    vid = await _seed(storage, files)
    calls = _seams(monkeypatch, files.root, text="这一句足够长可以通过反幻觉门限的稿子。")
    adapter = FakeAdapter(
        "bilibili",
        subtitle_error=ListError("bilibili", "list", "player/v2 412 blocked"),
        capabilities=_SUB_CAPS,
    )

    result = await run_postprocess(
        _ctx(storage, files, adapter), PostprocessParams(video_ids=[vid])
    )

    assert result.status == "failed"
    assert any("412 blocked" in f.error for f in result.failures)
    assert calls["extract_sources"] == []
    assert await storage.transcripts.get_for_video(vid) is None
