"""`postprocess` 任务的真链路（计划 T1.2 那一栏点名的文件）。

真：SQLite、`data/` 树、`extract_audio` 的 argv 组装、`asr.engine` 的切句与补标点、
`tasks.reference` 的归一与抽取、入库字段与事件。
假：ffmpeg 进程（换成"按 argv 写出一个真 16k WAV"）与 sherpa-onnx 的解码结果。

为什么这样切：这两样东西在 CI 与换机器时都不可靠（一个要装二进制、一个要 233 MB 权重），
而它们**周围**的接线 —— 谁被喂给 ffmpeg、产物落在哪个目录、库里那行长什么样 ——
恰恰是最容易悄悄错掉的部分（V1 §7.21 就是这一族：报错文案像"ffmpeg 没装"，
方向却是"喂错了文件"）。真权重那一跑记在 `docs/progress/2026-09-24.md`。
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import numpy as np
import pytest
from tests.unit.tasks.conftest import FakeAdapter, FakeBus, FakeConfig, FakeRegistry, make_ctx

from intelligence_hub_v2.asr import engine as asr_engine
from intelligence_hub_v2.infra.subprocess import SubprocessResult
from intelligence_hub_v2.models.event import EventType
from intelligence_hub_v2.models.video import VideoDraft
from intelligence_hub_v2.platforms.base import Capabilities
from intelligence_hub_v2.storage.files import FileStorage
from intelligence_hub_v2.tasks.params import PostprocessParams
from intelligence_hub_v2.tasks.postprocess import run_postprocess

_SENTENCES = (
    "今天讲三件事，先说第一件怎么用工具。",
    "第二件是流程，把采集和后处理串起来。",
    "第三件是案例，看完你就能自己跑一遍。",
)


@pytest.fixture
def ffmpeg(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """把 `run_subprocess` 换成"按 argv 写出真 16k WAV"，并记下每次的 argv。"""
    calls: list[list[str]] = []

    async def fake_run(
        argv: list[str],
        **kwargs: Any,
    ) -> SubprocessResult:
        calls.append(list(argv))
        if argv[0].endswith("ffmpeg"):
            out = Path(argv[-1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(_wav_bytes())
        return SubprocessResult(
            argv=tuple(argv), returncode=0, stdout="", stderr="", duration_seconds=0.1
        )

    monkeypatch.setattr("intelligence_hub_v2.infra.ffmpeg.run_subprocess", fake_run)
    return calls


@pytest.fixture
def fake_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """权重目录是真的形状（两个文件都在），解码是假的（按批次交回"第 N 句"，不带标点）。"""
    models_root = FileStorage(tmp_path / "data").asr_models_dir
    directory = models_root / "sherpa-onnx-sense-voice-fake"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "model.int8.onnx").write_bytes(b"fake")
    (directory / "tokens.txt").write_text("<unk>\n", encoding="utf-8")

    state = {"n": 0}

    class Stream:
        def __init__(self) -> None:
            self.result = SimpleNamespace(text="")

        def accept_waveform(self, rate: int, samples: list[float]) -> None:
            self.count = len(samples)

    class Recognizer:
        def create_stream(self) -> Stream:
            return Stream()

        def decode_stream(self, stream: Stream) -> None:
            state["n"] += 1
            stream.result.text = f"<|zh|>{_SENTENCES[(state['n'] - 1) % len(_SENTENCES)]}"

    module = ModuleType("sherpa_onnx")
    module.OfflineRecognizer = SimpleNamespace(  # type: ignore[attr-defined]
        from_sense_voice=lambda **kwargs: Recognizer()
    )
    module.__spec__ = SimpleNamespace(name="sherpa_onnx")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sherpa_onnx", module)
    monkeypatch.setattr(asr_engine, "_recognizer_cache", {})
    return directory


def _wav_bytes() -> bytes:
    """三段有起伏的语音样能量 + 停顿，16k 单声道 16-bit。够切出三批。"""
    pieces: list[Any] = []
    for index in range(3):
        count = int(9.0 * 16000 / 3 * 0.8)
        t = np.arange(count) / 16000.0
        envelope = 0.3 + 0.7 * np.abs(np.sin(2 * np.pi * 3.3 * t))
        pieces.append((0.45 * envelope * np.sin(2 * np.pi * 170 * t)).astype("float32"))
        pieces.append(np.zeros(int(0.6 * 16000), dtype="float32"))
        del index
    data = np.clip(np.concatenate(pieces) * 32767.0, -32768, 32767).astype("<i2")
    header = _riff_header(data.nbytes)
    return header + data.tobytes()


def _riff_header(payload: int) -> bytes:
    rate, block = 16000, 2
    return (
        b"RIFF"
        + struct.pack("<I", 36 + payload)
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * block, block, 16)
        + b"data"
        + struct.pack("<I", payload)
    )


_NO_SUBS = Capabilities(
    needs_browser=True,
    needs_cookies=True,
    cookie_variants=("exported_file", "anonymous"),
    supports_subtitles=False,
    supports_dash_split=False,
    list_strategy="browser_scroll",
    media_strategy="yt_dlp_with_fallback",
)


async def _seed(
    storage, files: FileStorage, *, vid: str = "7", aux: list[str] | None = None
) -> int:
    media_rel = f"media/douyin/博主/{vid}/media.mp4"
    (files.abs(media_rel).parent).mkdir(parents=True, exist_ok=True)
    files.abs(media_rel).write_bytes(b"fake-mp4")
    row = await storage.videos.insert(
        VideoDraft(
            platform="douyin",
            platform_video_id=vid,
            title="测试作品",
            media_path=media_rel,
            media_aux_paths_json=json.dumps(aux or [], ensure_ascii=False),
        )
    )
    return row.id


def _ctx(storage, files: FileStorage) -> Any:
    adapter = FakeAdapter("douyin", capabilities=_NO_SUBS)
    return make_ctx(
        storage=storage,
        files=files,
        registry=FakeRegistry({"douyin": adapter}, {"douyin": FakeConfig()}),
        bus=FakeBus(),
    )


@pytest.fixture
def files(tmp_path: Path) -> FileStorage:
    """覆盖全局 `files`：这条链路要的是**真目录**里有真媒体文件。"""
    storage = FileStorage(tmp_path / "data")
    storage.ensure_dirs()
    return storage


async def test_the_whole_chain_from_media_to_three_files_and_one_db_row(
    storage, files: FileStorage, ffmpeg: list[list[str]], fake_model: Path
) -> None:
    vid = await _seed(storage, files)
    ctx = _ctx(storage, files)

    result = await run_postprocess(ctx, PostprocessParams(video_ids=[vid]))
    media_dir = files.abs("media/douyin/博主/7").parent / "7"
    transcript_dir = media_dir / "transcript"

    assert result.status == "success", result.failures
    assert result.summary["transcribed"] == 1
    # 1) 先问 ffprobe 有没有音频轨，才叫 ffmpeg（V1 §7.21：没有音频轨却跑 ffmpeg，
    #    得到的是 `Output file does not contain any stream`，字面意思像"ffmpeg 没装"）
    assert [call[0].rsplit("/", 1)[-1] for call in ffmpeg] == ["ffprobe", "ffmpeg"], ffmpeg
    argv = next(call for call in ffmpeg if call[0].endswith("ffmpeg"))
    assert "-vn" in argv  # 丢视频轨
    assert argv[argv.index("-ar") + 1] == "16000" and argv[argv.index("-ac") + 1] == "1"
    assert argv[-1] == str(files.asr_audio_path(media_dir))
    assert argv[argv.index("-i") + 1] == str(files.abs("media/douyin/博主/7/media.mp4"))
    # 2) 三份产物，路径与 §7.5 的统一约定一致（speech-raw.txt 不落）
    clean = transcript_dir / "speech-clean.txt"
    assert clean.is_file() and (transcript_dir / "segments.json").is_file()
    assert (transcript_dir / "reference.md").is_file()
    assert not (transcript_dir / "speech-raw.txt").exists()
    lines = clean.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 2
    assert all(line[-1] in "。！？；，、" for line in lines), "§7.9：每一行都要以标点收尾"
    segments = json.loads((transcript_dir / "segments.json").read_text(encoding="utf-8"))
    assert [s["text"] for s in segments] == lines
    assert [s["start_seconds"] for s in segments] == sorted(s["start_seconds"] for s in segments), (
        "时间戳不单调就没法用来跳转"
    )
    assert "本地自动抽取参考" in (transcript_dir / "reference.md").read_text(encoding="utf-8")
    # 3) 库里那一行 + 事件
    record = await storage.transcripts.get_for_video(vid)
    assert record is not None and record.engine == "sherpa_sense_voice"
    text = clean.read_text(encoding="utf-8")
    assert record.sentence_count == len(lines) and record.char_count == len(text) - 1
    assert record.text_path.endswith("transcript/speech-clean.txt")
    assert not Path(record.text_path).is_absolute(), "库里存绝对路径 = 换机器就废"
    assert json.loads(str(record.segments_json))[0]["text"] == lines[0]
    assert any(event.type is EventType.TRANSCRIPT_READY for event in ctx.events.events)


async def test_rerunning_does_not_decode_the_audio_twice(
    storage, files: FileStorage, ffmpeg: list[list[str]], fake_model: Path
) -> None:
    """`extract_audio` 的幂等跳过（输出已存在且非空）必须在任务链里成立。

    转写任务重跑是常态（换档位想再试一次），每次都重解一遍音频能把 CPU 烧光。
    """
    vid = await _seed(storage, files)
    await run_postprocess(_ctx(storage, files), PostprocessParams(video_ids=[vid]))
    await run_postprocess(_ctx(storage, files), PostprocessParams(video_ids=[vid]))

    decoded = [call for call in ffmpeg if call[0].endswith("ffmpeg")]
    assert len(decoded) == 1, f"音频被解了两遍（{len(decoded)} 次 ffmpeg）：中间产物没被认出来"
    # 每条作品问一次 ffprobe 是正常成本（"有没有音频轨"是文件的属性，不是本轮的缓存）
    assert len([call for call in ffmpeg if call[0].endswith("ffprobe")]) == 2


async def test_a_row_without_media_is_counted_and_touches_nothing_on_disk(
    storage, files: FileStorage, ffmpeg: list[list[str]], fake_model: Path
) -> None:
    await storage.videos.insert(
        VideoDraft(platform="douyin", platform_video_id="8", title="只有作品行", media_path=None)
    )
    vid = (await storage.videos.find_by_platform_id("douyin", "8")).id

    result = await run_postprocess(_ctx(storage, files), PostprocessParams(video_ids=[vid]))

    assert result.summary["no_media"] == 1
    assert ffmpeg == [], "没媒体也去问 ffprobe = 白起一个子进程"
    assert await storage.transcripts.get_for_video(vid) is None
