"""`infra.ffmpeg` 测试。

**这台机器的 PATH 里没有 ffmpeg / ffprobe**（2026-09-22 实测 `shutil.which` 双 None，
V1 §7.19 那句"注册表里有 ≠ 进程拿得到"在 V2 的 shell 里照样成立）。
所以这里**不依赖真二进制**：全部走 `_parse_streams` 纯函数 +
mock 掉 `run_subprocess`。真跑一遍转写是 Task 8/16 的端到端 smoke 的活。

最重要的一条是 `has_audio_stream` 的**"问不出来时返回 True"**：
判成"没有"会**静默少一段口播稿**，而丢稿子在看板上完全不可见，
只会表现为"这个博主的稿子怎么这么少"。宁可让 ffmpeg 报出真实原因。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from intelligence_hub_v2.infra import ffmpeg as ffmpeg_module
from intelligence_hub_v2.infra.ffmpeg import (
    WAV_HEADER_BYTES,
    StreamInfo,
    _parse_streams,
    extract_audio,
    has_audio_stream,
    probe_streams,
)
from intelligence_hub_v2.infra.subprocess import SubprocessResult


def _result(returncode: int = 0, stdout: str = "", stderr: str = "") -> SubprocessResult:
    return SubprocessResult(
        argv=("ffprobe",), returncode=returncode, stdout=stdout, stderr=stderr, duration_seconds=0.1
    )


def _probe_json(streams: list[dict[str, Any]]) -> str:
    return json.dumps({"streams": streams})


VIDEO_ONLY = [
    {"index": 0, "codec_type": "video", "codec_name": "avc1", "width": 1920, "height": 1080}
]
WITH_AUDIO = [
    VIDEO_ONLY[0],
    {"index": 1, "codec_type": "audio", "codec_name": "aac", "duration": "155.2"},
]


# ---------------------------------------------------------------------------
# ffprobe 输出解析
# ---------------------------------------------------------------------------


def test_parse_streams_reads_types_and_dimensions() -> None:
    streams = _parse_streams(_probe_json(WITH_AUDIO))
    assert [s.codec_type for s in streams] == ["video", "audio"]
    assert streams[0].width == 1920 and streams[0].height == 1080
    assert streams[1].duration_seconds == pytest.approx(155.2)
    assert streams[1].codec_name == "aac"


@pytest.mark.parametrize("stdout", ["", "not json at all", "{}", '{"streams": "nope"}', "[]"])
def test_unparseable_probe_output_is_an_empty_list(stdout: str) -> None:
    """空列表 = "问不出来"，也 = "问到了但没有流"。
    两者都当"不知道"处理，见 `has_audio_stream`。"""
    assert _parse_streams(stdout) == []


def test_ffprobe_string_numbers_are_coerced() -> None:
    """ffprobe 经常把 width/duration 回成字符串（`"1920"` / `"155.200000"`）。"""
    streams = _parse_streams(
        _probe_json([{"index": "0", "codec_type": "video", "width": "1920", "duration": "3.5"}])
    )
    assert streams[0].index == 0
    assert streams[0].width == 1920
    assert streams[0].duration_seconds == pytest.approx(3.5)


def test_a_bool_is_not_mistaken_for_a_number() -> None:
    """`True` 是 `int` 的子类。不挡就会把 `width: true` 读成 1 像素。"""
    streams = _parse_streams(_probe_json([{"index": 0, "codec_type": "video", "width": True}]))
    assert streams[0].width is None


# ---------------------------------------------------------------------------
# has_audio_stream 的兜底方向
# ---------------------------------------------------------------------------


async def test_has_audio_stream_is_true_when_probing_says_anything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """**问不出来返回 True**。判成"没有"= 白丢一段口播稿，且不可见。"""
    media = tmp_path / "media.f137.mp4"
    media.write_bytes(b"x")
    called: list[str] = []

    async def fake(*args: Any, **kwargs: Any) -> SubprocessResult:
        called.append("probe")
        return _result(returncode=1, stderr="ffprobe: Invalid data")

    monkeypatch.setattr(ffmpeg_module, "run_subprocess", fake)
    assert await has_audio_stream(media) is True
    assert called == ["probe"]


async def test_a_real_video_only_stream_is_reported_as_having_no_audio(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """只有**问得出答案**时才许说"没有音频"。"""
    media = tmp_path / "media.f137.mp4"
    media.write_bytes(b"x")

    async def fake(*args: Any, **kwargs: Any) -> SubprocessResult:
        return _result(stdout=_probe_json(VIDEO_ONLY))

    monkeypatch.setattr(ffmpeg_module, "run_subprocess", fake)
    assert await has_audio_stream(media) is False


async def test_a_missing_file_is_not_quietly_treated_as_video_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """文件不在 → 不发子进程，probe 回空 → `has_audio_stream` 仍是 True（不知道）。
    但 `probe_streams` 必须**不去问**：那是一次注定失败的 ffprobe。"""
    spawned = False

    async def fake(*args: Any, **kwargs: Any) -> SubprocessResult:
        nonlocal spawned
        spawned = True
        return _result(stdout=_probe_json(WITH_AUDIO))

    monkeypatch.setattr(ffmpeg_module, "run_subprocess", fake)
    assert await probe_streams(tmp_path / "nope.mp4") == []
    assert spawned is False


async def test_probe_passes_the_path_last(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    media = tmp_path / "media.mp4"
    media.write_bytes(b"x")
    seen: list[list[str]] = []

    async def fake(argv: list[str], **kwargs: Any) -> SubprocessResult:
        seen.append(list(argv))
        return _result(stdout=_probe_json(WITH_AUDIO))

    monkeypatch.setattr(ffmpeg_module, "run_subprocess", fake)
    await probe_streams(media)
    assert seen[0][0] == "ffprobe"
    assert seen[0][-1] == str(media)
    assert "-show_streams" in seen[0] and "-v" in seen[0]


# ---------------------------------------------------------------------------
# extract_audio
# ---------------------------------------------------------------------------


async def test_extract_audio_skips_a_file_that_is_already_there(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """转写任务重跑是常态（换了引擎档位想再试一次），每次重解音频能把 CPU 烧光。"""
    audio = tmp_path / "part-001.wav"
    audio.write_bytes(b"\x00" * (WAV_HEADER_BYTES + 100))
    called = False

    async def fake(*args: Any, **kwargs: Any) -> SubprocessResult:
        nonlocal called
        called = True
        return _result()

    monkeypatch.setattr(ffmpeg_module, "run_subprocess", fake)
    assert await extract_audio(tmp_path / "media.mp4", audio) == audio
    assert called is False


async def test_a_header_only_wav_is_not_counted_as_done(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """44 字节只有 WAV 头 = 一个采样都没有。跳过它就得到一篇空稿子，
    而空稿子在列表里与"转写成功"长得一模一样。"""
    audio = tmp_path / "part-001.wav"
    audio.write_bytes(b"\x00" * WAV_HEADER_BYTES)
    ran: list[list[str]] = []

    async def fake(argv: list[str], **kwargs: Any) -> SubprocessResult:
        ran.append(list(argv))
        Path(argv[-1]).write_bytes(b"\x00" * (WAV_HEADER_BYTES + 50))
        return _result()

    monkeypatch.setattr(ffmpeg_module, "run_subprocess", fake)
    await extract_audio(tmp_path / "media.mp4", audio)
    assert len(ran) == 1


async def test_failure_keeps_the_ffmpeg_stderr_verbatim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """V1 §7.21：`Output file does not contain any stream` 那句必须原样留得下来 ——
    它和"ffmpeg 没装"长得很像，方向完全不同，抹平了就永远查错地方。"""

    async def fake(*args: Any, **kwargs: Any) -> SubprocessResult:
        return _result(returncode=4294967274, stderr="Output file does not contain any stream")

    monkeypatch.setattr(ffmpeg_module, "run_subprocess", fake)
    with pytest.raises(RuntimeError, match="does not contain any stream") as caught:
        await extract_audio(tmp_path / "media.mp4", tmp_path / "audio" / "part-001.wav")
    assert "4294967274" in str(caught.value)


async def test_exit_zero_with_no_output_is_not_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """**不许臆造成功**：ffmpeg 回 0 但没产出文件（被杀、磁盘满）时，
    报"没产出"比"成功"诚实。"""

    async def fake(*args: Any, **kwargs: Any) -> SubprocessResult:
        return _result(returncode=0)

    monkeypatch.setattr(ffmpeg_module, "run_subprocess", fake)
    with pytest.raises(RuntimeError, match="没产出音频"):
        await extract_audio(tmp_path / "media.mp4", tmp_path / "audio" / "part-001.wav")


async def test_missing_ffmpeg_is_a_lookuperror_not_a_runtimeerror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ "装 ffmpeg"与"这条媒体有问题"要做的动作不同，所以别翻译异常类型。"""

    async def fake(*args: Any, **kwargs: Any) -> SubprocessResult:
        raise LookupError("找不到可执行文件 'ffmpeg'")

    monkeypatch.setattr(ffmpeg_module, "run_subprocess", fake)
    with pytest.raises(LookupError, match="ffmpeg"):
        await extract_audio(tmp_path / "media.mp4", tmp_path / "a.wav")


async def test_the_output_directory_is_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = tmp_path / "audio" / "deep" / "part-001.wav"

    async def fake(argv: list[str], **kwargs: Any) -> SubprocessResult:
        Path(argv[-1]).write_bytes(b"\x00" * 200)
        return _result()

    monkeypatch.setattr(ffmpeg_module, "run_subprocess", fake)
    await extract_audio(tmp_path / "media.mp4", audio)
    assert audio.is_file()


def test_stream_info_defaults_are_usable() -> None:
    info = StreamInfo(index=0, codec_type="audio")
    assert info.codec_name is None and info.width is None and info.duration_seconds is None
