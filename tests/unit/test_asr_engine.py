"""ASR 引擎的外围：读盘、找权重、补标点、把 `Transcript` 交对。

切句算法本身在 `test_asr_segments.py`。这里用**假 sherpa_onnx**（`sys.modules` 注入）
跑通 `transcribe_wav()` 整条路：断言的是"引擎名是契约值""每句带时间戳""recognizer 只建一次"
"没有语音时压根不叫模型""缺依赖如实抛"这几件事。

`§7.9` 的看护就是 `join_sentences` 那一组：SenseVoice 不产标点，而下游只按标点断句。
"""

from __future__ import annotations

import importlib.machinery
import sys
import typing
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from intelligence_hub_v2.asr import engine as asr
from intelligence_hub_v2.asr.engine import (
    ENGINE_NAME,
    SAMPLE_RATE,
    SEGMENT_PAD_SECONDS,
    AsrUnavailable,
    EngineStatus,
    detect,
    detect_model_dir,
    ends_with_punctuation,
    join_sentences,
    read_wav_pcm,
    strip_tags,
    transcribe_wav,
)
from intelligence_hub_v2.core.config import AppConfig, AsrSection
from intelligence_hub_v2.models.transcript import Transcript
from intelligence_hub_v2.storage.schema import TRANSCRIPT_ENGINES

# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #


def write_wav(
    path: Path, samples: Any, *, rate: int = SAMPLE_RATE, channels: int = 1, width: int = 2
) -> Path:
    """写一个真 WAV（`read_wav_pcm` 要读的就是这个格式）。

    传 float（±1）时按 int16 满量程缩放 —— 少了这一步等于把 ±0.4 的信号直接
    `astype("<i2")` 截成 0：写出来的是一段**静音**，而测试会以"没有口播"的名义绿过去。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.asarray(samples)
    if np.issubdtype(data.dtype, np.floating):
        data = np.clip(data * 32767.0, -32768.0, 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(data.astype("<i2").tobytes())
    return path


def speech_like(seconds: float, *, bursts: int = 3) -> Any:
    """几段有起伏的语音样能量，中间夹静音。

    调用方给的时长要**明显长于 `TARGET_CHUNK_SECONDS`**（那些用例传 20s/8 段）：
    `group_segments` 按设计会把 8 秒以内的连续语音打包成**一批**，
    6 秒的夹具只会产出 1 句 —— 那会让"逐句时间戳递增/不重叠"那类断言退化成空转。
    """
    per = seconds / bursts
    pieces: list[Any] = []
    for _ in range(bursts):
        count = int(per * 0.75 * SAMPLE_RATE)
        t = np.arange(count) / SAMPLE_RATE
        envelope = 0.35 + 0.65 * np.abs(np.sin(2 * np.pi * 3.7 * t))
        pieces.append((0.4 * envelope * np.sin(2 * np.pi * 175 * t)).astype("float32"))
        pieces.append(np.zeros(int(per * 0.25 * SAMPLE_RATE), dtype="float32"))
    return np.concatenate(pieces)


@pytest.fixture
def weights(tmp_path: Path) -> Path:
    """一个装着权重与 tokens.txt 的目录（内容是假的，**形状**是真的）。"""
    directory = tmp_path / "asr-models" / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2099-01-01"
    directory.mkdir(parents=True)
    (directory / "model.int8.onnx").write_bytes(b"not-a-real-model")
    (directory / "tokens.txt").write_text("<unk>\n<|zh|>\n", encoding="utf-8")
    return directory


class FakeRecognizer:
    """假到只会按调用顺序交回"第 N 句"，且**不带标点** —— 那正是 SenseVoice 的真实行为。"""

    def __init__(self, options: dict[str, Any], counter: list[int]) -> None:
        self.options = options
        self.counter = counter
        self.streams: list[FakeStream] = []

    def create_stream(self) -> FakeStream:
        stream = FakeStream()
        self.streams.append(stream)
        return stream

    def decode_stream(self, stream: FakeStream) -> None:
        self.counter[0] += 1
        # 尾随一个标记：`strip_tags` 要把它清掉，只留正文
        stream.result.text = f"<|zh|><|NEUTRAL|>第{self.counter[0]}句{stream.count}字"


class FakeStream:
    def __init__(self) -> None:
        self.result = SimpleNamespace(text="")
        self.count = 0

    def accept_waveform(self, rate: int, samples: list[float]) -> None:
        assert rate == SAMPLE_RATE, "引擎按 16k 设计，采样率不是 16k 就说明上游接错了"
        self.count = len(samples)


@pytest.fixture
def fake_sherpa(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """把 `sherpa_onnx` 换成假的，并记下 `from_sense_voice` 的入参与调用次数。"""
    calls: list[dict[str, Any]] = []
    counter = [0]
    built = {"count": 0}

    class OfflineRecognizer:
        @staticmethod
        def from_sense_voice(**options: Any) -> FakeRecognizer:
            calls.append(options)
            built["count"] += 1
            return FakeRecognizer(options, counter)

    # `__spec__` 不是装饰：`_package_present()` 走 `importlib.util.find_spec`，
    # 它对着 sys.modules 里那个条目要 spec，缺了就抛 ValueError（而不是返回 None）。
    module = SimpleNamespace(
        OfflineRecognizer=OfflineRecognizer,
        __spec__=importlib.machinery.ModuleSpec("sherpa_onnx", loader=None),
    )
    monkeypatch.setitem(sys.modules, "sherpa_onnx", module)
    monkeypatch.setattr(asr, "_recognizer_cache", {})
    return {"calls": calls, "built": built, "counter": counter}


# --------------------------------------------------------------------------- #
# §7.9：补标点
# --------------------------------------------------------------------------- #


def test_every_sentence_gets_punctuation_at_the_cut_point() -> None:
    """§7.9 的全部判据：交出去的文稿**每一行都以标点收尾**。

    下游 `split_sentences` 只认 `。！？；，、`，整段没有标点会被当成一个句子，
    抽取式参考材料直接退化成一整块。
    """
    text = join_sentences(["今天讲三件事", "第一件是这个", "第二件是这个"])
    lines = text.splitlines()
    assert len(lines) == 3
    assert all(ends_with_punctuation(line) for line in lines), text
    assert text == "今天讲三件事。\n第一件是这个。\n第二件是这个。"


def test_a_part_that_already_ends_with_punctuation_is_not_given_a_second_one() -> None:
    assert join_sentences(["真的吗？", "走吧！"]) == "真的吗？\n走吧！"
    assert join_sentences(["结尾是逗号，"]) == "结尾是逗号，"


def test_empty_parts_disappear_and_all_blank_input_is_an_empty_string() -> None:
    assert join_sentences(["", "  ", None]) == ""  # type: ignore[list-item]
    assert join_sentences([]) == ""


def test_strip_tags_clears_the_sensevoice_markers() -> None:
    assert strip_tags("<|zh|><|NEUTRAL|><|Speech|><|withitn|>今天天气好") == "今天天气好"
    assert strip_tags("") == ""


# --------------------------------------------------------------------------- #
# 读 WAV：格式不对就拒，不静默将错就错
# --------------------------------------------------------------------------- #


def test_read_wav_returns_float32_in_unit_range(tmp_path: Path) -> None:
    raw = (np.sin(2 * np.pi * 200 * np.arange(1000) / SAMPLE_RATE) * 12000).astype("<i2")
    path = write_wav(tmp_path / "a.wav", raw)
    pcm = read_wav_pcm(path)
    assert pcm.dtype == np.float32 and pcm.size == 1000
    peak = float(np.max(np.abs(pcm)))
    assert peak <= 1.0, "取值必须在 ±1"
    assert peak == pytest.approx(12000 / 32768, abs=1e-4), (
        f"int16 → float32 的比例尺不是 32768（实测峰值 {peak}）"
    )


@pytest.mark.parametrize(
    ("rate", "channels", "width", "needle"),
    [
        (44100, 1, 2, "44100Hz"),
        (16000, 2, 2, "2 声道"),
        (16000, 1, 1, "16-bit"),
    ],
)
def test_a_wav_with_the_wrong_shape_is_refused_with_the_fix_action(
    tmp_path: Path, rate: int, channels: int, width: int, needle: str
) -> None:
    """不静默重采样：常数为 16k 设计，喂 44.1k 不会报错，只会让每条时间戳短 2.75 倍。"""
    data = np.zeros(SAMPLE_RATE // 2, dtype="<i2")
    path = tmp_path / "bad.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(data[: len(data) // (2 if width == 1 else 1)].tobytes())
    with pytest.raises(AsrUnavailable, match=needle):
        read_wav_pcm(path)


def test_a_media_file_is_rejected_as_not_a_wav(tmp_path: Path) -> None:
    """手滑把 `media.mp4` 传进来时，要说"先过 extract_audio"，不是 `wave.Error` 一句糊过去。"""
    path = tmp_path / "media.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42not a wav")
    with pytest.raises(AsrUnavailable, match="extract_audio"):
        read_wav_pcm(path)


# --------------------------------------------------------------------------- #
# 找权重与"能不能用"
# --------------------------------------------------------------------------- #


def test_model_discovery_does_not_hardcode_the_version_name(tmp_path: Path, weights: Path) -> None:
    """V2 的约定：`data/asr-models/` 下**任一**带权重的子目录都算（V1 写死了 2025-09-09）。"""
    found = detect_model_dir(config=AppConfig(), models_root=weights.parent)
    assert found == weights.resolve()


def test_a_directory_with_only_tokens_is_not_an_available_model(tmp_path: Path) -> None:
    half = tmp_path / "half"
    half.mkdir()
    (half / "tokens.txt").write_text("<unk>\n", encoding="utf-8")
    assert detect_model_dir(models_root=half) is None


def test_float16_weights_are_accepted_when_int8_is_absent(tmp_path: Path) -> None:
    directory = tmp_path / "fp32"
    directory.mkdir()
    (directory / "model.onnx").write_bytes(b"m")
    (directory / "tokens.txt").write_text("t", encoding="utf-8")
    assert detect_model_dir(models_root=tmp_path) == directory.resolve()


def test_the_env_var_beats_the_default_directory(
    tmp_path: Path, weights: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "env-dir"
    other.mkdir(parents=True)
    (other / "model.int8.onnx").write_bytes(b"m")
    (other / "tokens.txt").write_text("t", encoding="utf-8")
    monkeypatch.setenv("SENSEVOICE_MODEL_DIR", str(other))
    found = detect_model_dir(models_root=weights.parent)
    assert found == other.resolve(), "环境变量那一档没有赢过默认目录：修权重时会被默认目录盖住"


def test_explicit_argument_beats_everything(
    tmp_path: Path, weights: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHERPA_ONNX_MODEL_DIR", str(weights.parent / "does-not-exist"))
    assert detect_model_dir(str(weights), models_root=tmp_path) == weights.resolve()


def test_detect_reports_three_states_distinctly(
    tmp_path: Path, weights: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`weights_missing` 与 `package_missing` 是两种修法，合成一位就说不清该干什么。"""
    monkeypatch.setattr(asr, "_package_present", lambda: True)
    ready = detect(models_root=weights.parent)
    assert ready.available is True and ready.as_summary == "ready"
    assert ready.model_dir == weights.resolve()

    no_weights = detect(models_root=tmp_path / "nothing-here")
    assert no_weights.available is False and no_weights.as_summary == "weights_missing"
    assert "data/asr-models" in no_weights.reason  # 修复动作要写在消息里（AGENTS §1.3）

    monkeypatch.setattr(asr, "_package_present", lambda: False)
    no_package = detect(models_root=weights.parent)
    assert no_package.as_summary == "package_missing"
    assert "sherpa-onnx" in no_package.reason and "uv" in no_package.reason


def test_as_summary_reads_the_snapshot_not_the_live_environment() -> None:
    """`EngineStatus` 是**快照**：建好之后重新探活不许改变它的结论。

    早先的写法是 `as_summary` 里现调 `_package_present()`，于是同一份状态在
    preflight 那一轮里可以自相矛盾（探活慢的那几秒里包被装上/卸掉）。
    前置那句是防"这条用例其实什么都没测"。
    """
    assert asr._package_present() is True, "这台机器上 sherpa-onnx 本来就不在，用例是空转"
    status = EngineStatus(
        available=False, engine=ENGINE_NAME, reason="未安装 sherpa-onnx", package_present=False
    )
    assert status.as_summary == "package_missing"


def test_detecting_does_not_import_the_engine(tmp_path: Path, weights: Path) -> None:
    """preflight 每轮都要问一句可用性 —— 为此把 onnxruntime 拉起来是不可接受的。"""
    sys.modules.pop("sherpa_onnx", None)
    detect(models_root=weights.parent)
    assert "sherpa_onnx" not in sys.modules


# --------------------------------------------------------------------------- #
# 契约同源：引擎名三处必须是一个字符串
# --------------------------------------------------------------------------- #


def test_engine_name_is_the_contract_value() -> None:
    """`ENGINE_NAME` 同时是 Pydantic Literal、DB CHECK 枚举与配置项的取值。

    手抄的第四处（比如写成 V1 的 `"sherpa-onnx"`）会让 `Transcript` 当场校验失败，
    或者更坏：能建对象、`attach()` 时撞 CHECK 约束 —— 而那时已经在任务里了。
    """
    literal = Transcript.model_fields["engine"].annotation
    allowed = set(typing.get_args(literal))
    assert ENGINE_NAME in allowed
    assert ENGINE_NAME in TRANSCRIPT_ENGINES
    assert ENGINE_NAME in set(typing.get_args(AsrSection.model_fields["engine"].annotation))


# --------------------------------------------------------------------------- #
# transcribe_wav 整条路（假 recognizer）
# --------------------------------------------------------------------------- #


def test_transcribe_returns_timestamped_sentences_and_terminated_lines(
    tmp_path: Path, weights: Path, fake_sherpa: dict[str, Any]
) -> None:
    wav = write_wav(tmp_path / "audio.wav", speech_like(20.0, bursts=8))
    transcript = transcribe_wav(wav, model_dir=weights, config=AppConfig(asr={"model_dir": None}))

    assert isinstance(transcript, Transcript)
    assert transcript.engine == ENGINE_NAME
    assert transcript.sentence_count == len(transcript.segments) >= 2
    previous_start = -1.0
    previous_end = -1.0
    for segment in transcript.segments:
        assert segment.start_seconds < segment.end_seconds
        assert segment.start_seconds >= previous_start, "时间戳必须单调，前端要靠它跳转"
        # 相邻段可以交叠（首尾各外扩 120ms），但不能叠过半个批次
        assert segment.start_seconds - previous_end >= -2 * SEGMENT_PAD_SECONDS - 0.01
        assert segment.text and ends_with_punctuation(segment.text)
        previous_start, previous_end = segment.start_seconds, segment.end_seconds
    assert transcript.char_count == len(transcript.text)
    assert transcript.text.splitlines() == [segment.text for segment in transcript.segments]
    assert "<|" not in transcript.text, "SenseVoice 的标记没清干净"
    assert transcript.text_path is None  # 写盘不是引擎的活（tasks/postprocess.py）


def test_the_recognizer_is_built_once_per_model_and_thread_setting(
    tmp_path: Path, weights: Path, fake_sherpa: dict[str, Any]
) -> None:
    """`from_sense_voice` 要加载 233 MB 权重：每条语音段建一次会把整轮转写烧穿。"""
    wav = write_wav(tmp_path / "a.wav", speech_like(20.0, bursts=8))
    transcribe_wav(wav, model_dir=weights, threads=2, config=AppConfig(asr={"model_dir": None}))
    transcribe_wav(wav, model_dir=weights, threads=2, config=AppConfig(asr={"model_dir": None}))
    assert fake_sherpa["built"]["count"] == 1
    options = fake_sherpa["calls"][0]
    assert options["use_itn"] is True, "ITN（数字/日期读法）关掉就开始输出「一二三」这种字"
    assert options["language"] == "auto"
    assert options["num_threads"] == 2
    assert options["model"] == str(weights / "model.int8.onnx")
    assert options["tokens"] == str(weights / "tokens.txt")


def test_progress_is_called_once_per_batch(
    tmp_path: Path, weights: Path, fake_sherpa: dict[str, Any]
) -> None:
    wav = write_wav(tmp_path / "a.wav", speech_like(20.0, bursts=8))
    seen: list[tuple[int, int]] = []
    transcribe_wav(
        wav,
        model_dir=weights,
        config=AppConfig(asr={"model_dir": None}),
        progress=lambda index, total: seen.append((index, total)),
    )
    assert seen[0][0] == 1 and seen[-1][0] == seen[-1][1] == len(seen)


def test_a_file_with_no_speech_never_reaches_the_model(
    tmp_path: Path, weights: Path, fake_sherpa: dict[str, Any]
) -> None:
    """反幻觉闸的**效果**：平稳正弦整段没有一句口播 → 一次都不解码。

    只断言"返回空文本"是不够的：幻觉就是在喂模型的那一刻发生的。
    """
    tone = (0.3 * np.sin(2 * np.pi * 440 * np.arange(5 * SAMPLE_RATE) / SAMPLE_RATE)).astype(
        "float32"
    )
    wav = write_wav(tmp_path / "tone.wav", tone)
    transcript = transcribe_wav(wav, model_dir=weights, config=AppConfig(asr={"model_dir": None}))
    assert transcript.text == "" and transcript.segments == []
    assert transcript.sentence_count == 0 and transcript.char_count == 0
    assert fake_sherpa["built"]["count"] == 0, "闸没拦住：模型被喂了一段没有口播的音频"


def test_transcribe_fails_loudly_when_the_engine_is_unavailable(
    tmp_path: Path, weights: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(asr, "_package_present", lambda: False)
    wav = write_wav(tmp_path / "a.wav", speech_like(3.0))
    with pytest.raises(AsrUnavailable, match="uv"):
        transcribe_wav(wav, model_dir=weights, config=AppConfig(asr={"model_dir": None}))


def test_transcribe_refuses_a_non_wav_input(tmp_path: Path, weights: Path) -> None:
    mp4 = tmp_path / "media.mp4"
    mp4.write_bytes(b"\x00ftypmp42" + b"\x00" * 4096)
    with pytest.raises(AsrUnavailable, match="extract_audio"):
        transcribe_wav(mp4, model_dir=weights, config=AppConfig(asr={"model_dir": None}))
