"""本地 ASR：sherpa-onnx + SenseVoice（int8，CPU 即可）。V1 `asr_sherpa.py` 的 V2 版。

**产出契约**：`transcribe_wav()` 交回 `models.transcript.Transcript`（那份模型的 docstring
就写着"适配器 `fetch_subtitles()` / **ASR 引擎**返回它"），并且带**时间戳 segments** ——
静音切点本来就知道每句的起止采样，前端 T6.5 的口播稿跳转要用。
**本模块不写盘、不碰 SQLite**：`speech-clean.txt` 与 `transcripts` 行是
`tasks/postprocess.py` 的活（V1 的"写入边界"收得比原来更紧）。

为什么不是三行胶水（V1 §7.9 那两条到 2026 年仍然成立）：

1. **SenseVoice 不产标点**，而下游断句只认 `。！？；，、` —— 整段无标点的文本会被当成
   **一个句子**，抽取式参考材料直接退化成一整块，"爆款拆解"全部字段跟着废。
   做法：先按**静音**把音频切成短句、逐句识别、在切点补 `。`。切点落在停顿上，
   所以补出来的句号位置基本就是真实的句子边界。
2. **整段一次解码的耗时无界**（V1 实测 8 秒音频 0.4 秒、198 秒要 55 秒，注意力近似平方增长）。
   切句顺手把这条曲线拉回线性，也避开了单帧超长输入。

与 V1 的三处不同：

- **不碰 ffmpeg**。V1 在模块里自己 `subprocess.run(ffmpeg … -f f32le -)`；V2 里抽音频是
  `infra/ffmpeg.extract_audio()` 的活（16k 单声道 WAV，重跑幂等跳过），本模块只吃 WAV。
  "缺 ffmpeg" 因此由 preflight 与抽音频那一步如实报红，不在这里再写一处真相。
- **模型定位多了 V2 的约定**：`data/asr-models/` 下**任一**含 `model.int8.onnx`（或
  `model.onnx`）+ `tokens.txt` 的子目录都算，不写死 `…-2025-09-09` 那个版本名。
- 日志走 structlog，不再是 `print(..., file=sys.stderr)` 加一个 `log=` 回调。

**反幻觉闸**（V1 的原话）：SenseVoice 对静音/纯音会稳定幻觉出"我。"这类单字句，长视频累积
起来还能骗过 `min_transcript_chars`。所以整段能量低于 -60dBFS、或有声帧能量平坦
（CV<0.05）时**直接判"没有口播"**，压根不喂模型。

`AsrUnavailable` 的消息里必须带**可执行的修复动作**（装什么、把哪个环境变量指到哪）。
"""

from __future__ import annotations

import importlib.util
import os
import re
import time
import wave
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from intelligence_hub_v2.core.config import AppConfig, load_app_config
from intelligence_hub_v2.logging import get_logger
from intelligence_hub_v2.models.transcript import Transcript, TranscriptSegment
from intelligence_hub_v2.storage.files import FileStorage

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    Pcm = NDArray[np.float32]

__all__ = [
    "AsrUnavailable",
    "EngineStatus",
    "detect",
    "detect_model_dir",
    "find_speech_segments",
    "group_segments",
    "join_sentences",
    "read_wav_pcm",
    "strip_tags",
    "terminate_sentence",
    "transcribe_wav",
]

logger = get_logger(__name__)

ENGINE_NAME: Final = "sherpa_sense_voice"
"""契约值，不是显示名：`Transcript.engine` 的 Literal 与 `transcripts.engine` 的 CHECK
  枚举（`storage/schema.py::TRANSCRIPT_ENGINES`）写的都是它。`test_engine_name_is_the_contract_value`
  把三方钉在一起。"""

SAMPLE_RATE = 16000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000
_TAG_RE = re.compile(r"<\|[^|>]*\|>")

# 切句参数：短于 MIN_SPEECH_MS 的声音丢弃；单句超过 MAX_CHUNK_SECONDS 时强行续切。
MIN_SPEECH_MS = 120
TARGET_CHUNK_SECONDS = 8.0
MAX_CHUNK_SECONDS = 15.0
SILENCE_GAP_MS = 160
# 绝对噪声门限（≈ -60 dBFS）。
NOISE_FLOOR_RMS = 1e-3
# 有声帧能量的变异系数（std/mean）低于这个值就判定"不是口播"：
# 口播再平静也有起伏，而纯音/音乐垫底的 CV 接近 0。
FLAT_ENERGY_CV = 0.05
# 参与平坦度判定的最少有声帧数：帧数太少时 std 恒为 0，会把短促真口播误判成纯音。
MIN_FLAT_FRAMES = 8
# 短于这个时长的音频只要没切出够长的语音段，就整段交给模型（几秒的片段几乎全是语音）。
SHORT_CLIP_SECONDS = 3.0
# 相对峰值门限：口播录音音量差异很大，只靠绝对门限不稳。
PEAK_RELATIVE_THRESHOLD = 0.06
# 段首尾各外扩 120ms，避免把词头词尾削掉。
SEGMENT_PAD_SECONDS = 0.12

PUNCTUATION = "。！？；，、"
MODEL_ENV_KEYS = ("SENSEVOICE_MODEL_DIR", "SHERPA_ONNX_MODEL_DIR")
EXTERNAL_MODEL_CANDIDATES: tuple[str, ...] = (
    # 这台机器上另两个项目里装着同一份权重。最后兜底，不是主路：
    # 主路是 `data/asr-models/` 或那两个环境变量。
    "E:/codework/PersonalAI/data/asr/sherpa-onnx-sense-voice-int8-2025-09-09",
    "E:/codework/voice-weekly-report/data/asr/sherpa-onnx-sense-voice-int8-2025-09-09",
)

SAMPLE_WIDTH_BYTES = 2
"""`infra/ffmpeg.extract_audio()` 交出来的 WAV 是 16-bit PCM；别的位宽解不出我们要的语义。"""

MIN_ELAPSED_FOR_SPEED = 0.05
"""短于这个耗时不报倍速：分母太小的"× 实时"没有意义，还会随进程调度跳。"""

WEIGHTS_FILENAMES = ("model.int8.onnx", "model.onnx")
"""int8 优先：V1 的实测口径（CPU 短音频约 20× 实时）是 int8 那一档。"""


class AsrUnavailable(RuntimeError):
    """引擎或权重不可用。消息里必须带能执行的修复动作（V1 同规矩，AGENTS §1.3）。"""


@dataclass(frozen=True)
class EngineStatus:
    """`detect()` 的结论。给 preflight / healthcheck 用，**不参与决策**。

    三个状态必须能分开说，因为修法各不相同：

    - `ready`：包在、权重在，能转写
    - `weights_missing`：包在、权重不在（最容易漏的一项：代码进 git，233 MB 不进）
    - `package_missing`：sherpa-onnx 没装（`uv sync --extra asr` 一句的事）

    `as_summary` 只读**本对象的字段**，不再去探一次活：这是一份快照，
    建它的时候与读它的时候环境可以不一样（而 preflight 会把这一位原样交给 API 层）。
    """

    available: bool
    engine: str
    reason: str
    model_dir: Path | None = None
    package_present: bool = True

    @property
    def as_summary(self) -> str:
        if not self.package_present:
            return "package_missing"
        return "ready" if self.available else "weights_missing"


def _package_present() -> bool:
    """`find_spec` 而不是 `import`：preflight 不该为了问一句就把 onnxruntime 拉起来。"""
    return importlib.util.find_spec("sherpa_onnx") is not None


def _weights_in(directory: Path) -> Path | None:
    tokens = directory / "tokens.txt"
    if not tokens.is_file():
        return None
    for name in WEIGHTS_FILENAMES:
        if (directory / name).is_file():
            return directory
    return None


def model_search_dirs(
    explicit: str | Path = "",
    *,
    config: AppConfig | None = None,
    models_root: Path | None = None,
) -> list[Path]:
    """按优先级列出该找哪些目录。**只列，不判存在**（判存在是 `detect_model_dir` 的事）。"""
    found: list[Path] = []
    if str(explicit).strip():
        found.append(Path(str(explicit)))
    app = config or load_app_config()
    if app.asr.model_dir is not None:
        configured = Path(str(app.asr.model_dir)).expanduser()
        # 配置里的相对路径按**当前工作目录**算 —— 与 `AppConfig.data.resolve_all(root=None)`
        # 同一口径（`data.dir: "data"` 本来就是这么解析的）。两处口径不一样，就会出现
        # "配置指着一个不存在的权重目录"这种最难查的错。
        found.append(configured if configured.is_absolute() else Path.cwd() / configured)
    for key in MODEL_ENV_KEYS:
        value = str(os.environ.get(key) or "").strip()
        if value:
            found.append(Path(value).expanduser())
    # `models_root` 由调用方给（preflight 给的就是 app 真正在用的那份 `FileStorage` 算出来的
    # 目录）。自己再从配置算一遍会出现"两个 asr-models 路径"—— 冒烟时 data 在临时目录上，
    # 那两个路径就是两个答案。
    models = models_root if models_root is not None else FileStorage.from_config(app).asr_models_dir
    if models.is_dir():
        # V2 的约定目录：`data/asr-models/` 下**任何一个**带权重的子目录都算，不写死版本名。
        found.extend(sorted(child for child in models.iterdir() if child.is_dir()))
        found.append(models)
    found.extend(Path(item) for item in EXTERNAL_MODEL_CANDIDATES)
    return found


def detect_model_dir(
    explicit: str | Path = "",
    *,
    config: AppConfig | None = None,
    models_root: Path | None = None,
) -> Path | None:
    """找到第一个真装着 `model*.onnx` + `tokens.txt` 的目录；找不到返回 None。"""
    for directory in model_search_dirs(explicit, config=config, models_root=models_root):
        if (hit := _weights_in(directory)) is not None:
            return hit.resolve()
    return None


def model_dir_search_hint() -> str:
    keys = " 或 ".join(MODEL_ENV_KEYS)
    return (
        f"把权重放进 data/asr-models/ 下的一个子目录，或设 {keys} 指向含 "
        f"{' / '.join(WEIGHTS_FILENAMES)} + tokens.txt 的目录"
    )


def detect(
    model_dir: str | Path = "",
    *,
    config: AppConfig | None = None,
    models_root: Path | None = None,
) -> EngineStatus:
    """引擎到底能不能用。**如实报**，不许把"缺权重"说成"可用"。"""
    if not _package_present():
        return EngineStatus(
            available=False,
            engine=ENGINE_NAME,
            reason="未安装 sherpa-onnx。修复：uv sync --extra asr（或 uv pip install sherpa-onnx）",
            package_present=False,
        )
    directory = detect_model_dir(model_dir, config=config, models_root=models_root)
    if directory is None:
        return EngineStatus(
            available=False,
            engine=ENGINE_NAME,
            reason=f"找不到 SenseVoice 权重目录。{model_dir_search_hint()}",
        )
    return EngineStatus(available=True, engine=ENGINE_NAME, reason="", model_dir=directory)


def strip_tags(text: str) -> str:
    """去掉 SenseVoice 的 `<|zh|><|NEUTRAL|><|Speech|><|withitn|>` 一类标记。"""
    return _TAG_RE.sub("", str(text or "")).strip()


def read_wav_pcm(wav_path: Path) -> Pcm:
    """读 16k **单声道** 16-bit WAV → float32（±1）。

    采样率不对就拒，不静默重采样：`find_speech_segments` 的全部常数（20ms 帧、
    120ms 最短语音、8s/15s 打包）都以 16k 为前提，喂 44.1k 进去不会报错，
    只会让每一句的时间戳都短 2.75 倍 —— 那种错要等到前端点时间戳跳转时才浮出来。
    """
    import numpy as np

    try:
        with wave.open(str(wav_path), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            frames = handle.getnframes()
            raw = handle.readframes(frames)
    except (wave.Error, EOFError) as exc:
        msg = (
            f"{wav_path.name}：读不出 WAV（{exc}）。ASR 只吃 16k 单声道 WAV，"
            f"媒体文件要先过 infra.ffmpeg.extract_audio()"
        )
        raise AsrUnavailable(msg) from exc

    if rate != SAMPLE_RATE or channels != 1:
        msg = (
            f"{wav_path.name}：ASR 要 {SAMPLE_RATE}Hz 单声道 WAV，"
            f"实际是 {rate}Hz / {channels} 声道。修复：用 infra.ffmpeg.extract_audio() 重抽"
        )
        raise AsrUnavailable(msg)
    if width != SAMPLE_WIDTH_BYTES:
        msg = f"{wav_path.name}：要 16-bit PCM，实际位宽 {width * 8} bit"
        raise AsrUnavailable(msg)

    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / np.float32(32768.0)
    if samples.size == 0:
        msg = f"{wav_path.name}：解出来是空的（0 帧）—— 不要当成\u201c没有口播\u201d"
        raise AsrUnavailable(msg)
    return samples


# --------------------------------------------------------------------------- #
# 静音切句（V1 的算法，参数一字未改）
# --------------------------------------------------------------------------- #


def find_speech_segments(pcm: Pcm | Sequence[float]) -> list[tuple[int, int]]:
    """按帧能量找语音段，返回 `[(起始采样, 结束采样)]`，已按 TARGET/MAX 规则外扩。"""
    import numpy as np

    samples = np.asarray(pcm, dtype="float32")
    frame_count = samples.size // FRAME_SAMPLES
    if frame_count == 0:
        return []
    frames = samples[: frame_count * FRAME_SAMPLES].reshape(frame_count, FRAME_SAMPLES)
    rms = np.sqrt(np.mean(np.square(frames), axis=1))
    peak = float(np.max(rms)) if rms.size else 0.0
    if peak < NOISE_FLOOR_RMS:
        return []
    threshold = max(peak * PEAK_RELATIVE_THRESHOLD, NOISE_FLOOR_RMS)
    voiced = rms >= threshold

    # 有声帧能量几乎不变 = 纯音或稳定音乐底噪，不是口播。这类输入喂给 SenseVoice 会稳定
    # 幻觉出重复短句，长视频累积起来还能骗过 min_transcript_chars，所以在这里直接判空。
    # 但一两帧算不出有意义的离散度：短促真口播会因 std=0 被误杀，所以帧数不够就不上这道闸。
    voiced_rms = rms[voiced]
    if voiced_rms.size >= MIN_FLAT_FRAMES:
        mean_level = float(np.mean(voiced_rms))
        if mean_level > 0 and float(np.std(voiced_rms)) / mean_level < FLAT_ENERGY_CV:
            return []

    # 把紧邻的有声帧粘起来，避免在词内换气处切碎：向两侧各膨胀 span 帧。
    merged = voiced.copy()
    span = max(1, SILENCE_GAP_MS // FRAME_MS // 2)
    for shift in range(1, span + 1):
        merged[shift:] |= voiced[:-shift]
        merged[:-shift] |= voiced[shift:]

    segments: list[tuple[int, int]] = []
    start: int | None = None
    for index, flag in enumerate(merged):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            if (index - start) * FRAME_MS >= MIN_SPEECH_MS:
                segments.append((start * FRAME_SAMPLES, index * FRAME_SAMPLES))
            start = None
    if start is not None and (frame_count - start) * FRAME_MS >= MIN_SPEECH_MS:
        segments.append((start * FRAME_SAMPLES, frame_count * FRAME_SAMPLES))

    if not segments:
        # 没有一段够长：几秒的小片段本身几乎全是语音，整段交给模型；
        # 长音频里只有一堆不足 120ms 的瞬时响声则不是口播，喂进去只会换来幻觉重复句。
        #
        # **当前常数下这条走不到**（2026-09-24 量出来的，见 `docs/lessons.md` 经验 49）：
        # 膨胀先于长度闸，任何一粒被检出的声音都会先被粘成 ≥9 帧（180ms > MIN_SPEECH_MS），
        # 而"整段没有一段够长"只剩下 peak 低于噪声门限（上面已经 return）与平坦闸直接 return 两条。
        # 不删是因为它随时会活：把 `SILENCE_GAP_MS` 调小或把 `FRAME_MS` 调大，
        # `MIN_SPEECH_MS` 立刻重新变成一道真实的闸。
        if samples.size / SAMPLE_RATE <= SHORT_CLIP_SECONDS:
            return [(0, samples.size)]
        return []
    return [_clip(begin, end, samples.size) for begin, end in segments]


def _clip(begin: int, end: int, total: int) -> tuple[int, int]:
    pad = int(SEGMENT_PAD_SECONDS * SAMPLE_RATE)
    return max(0, begin - pad), min(total, end + pad)


def group_segments(segments: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """把语音段打包成识别批次：单批不超过 MAX_CHUNK_SECONDS，超长单句直接硬切。"""
    max_samples = int(MAX_CHUNK_SECONDS * SAMPLE_RATE)
    target_samples = int(TARGET_CHUNK_SECONDS * SAMPLE_RATE)
    groups: list[tuple[int, int]] = []
    current_start: int | None = None
    previous_end = 0
    for begin, end in segments:
        if end - begin > max_samples:
            if current_start is not None:
                groups.append((current_start, previous_end))
                current_start = None
            for offset in range(0, end - begin, max_samples):
                cut_low = begin + offset
                cut_high = min(begin + offset + max_samples, end)
                if cut_high - cut_low >= FRAME_SAMPLES:
                    groups.append((cut_low, cut_high))
            continue
        if current_start is None:
            current_start = begin
        elif end - current_start > max_samples or end - current_start > target_samples:
            # 已经攒够一句的长度，就在下一个停顿前收尾，下一批重新起头。
            groups.append((current_start, previous_end))
            current_start = begin
        previous_end = end
    if current_start is not None:
        groups.append((current_start, previous_end))
    return groups


def ends_with_punctuation(text: str) -> bool:
    return bool(text) and text[-1] in PUNCTUATION


def terminate_sentence(text: str) -> str:
    """§7.9 那条规则**唯一**的落点：句子结尾没有标点就补一个 `。`。

    切点落在静音上，所以补出来的句号位置基本就是真实的句子边界。
    `join_sentences` 与 `transcribe_wav` 里的 segments 都走这一句 —— 两处各写一遍的话，
    稿子正文有标点而 `segments_json` 没有（前端 T6.5 逐句渲染时看到的就又是裸句）。
    """
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""
    return cleaned if ends_with_punctuation(cleaned) else f"{cleaned}。"


def join_sentences(parts: Sequence[str]) -> str:
    """逐句拼接并在句末补标点：SenseVoice 不输出标点，而下游只按标点断句（§7.9）。

    已经有标点的句子**不重复补**；空句丢掉（不然会在稿子里留一批空行）。
    """
    return "\n".join(line for line in (terminate_sentence(raw) for raw in parts) if line)


# --------------------------------------------------------------------------- #
# 解码
# --------------------------------------------------------------------------- #

_recognizer_cache: dict[str, Any] = {}


def _recognizer(model_dir: Path, language: str, threads: int) -> Any:  # noqa: ANN401 - 外部句柄
    """recognizer 建一次复用（`from_sense_voice` 要加载 233 MB 权重，按批重建会烧穿）。"""
    import sherpa_onnx

    key = f"{model_dir}|{threads}|{language}"
    cached = _recognizer_cache.get(key)
    if cached is not None:
        return cached
    weights = next(
        (model_dir / name for name in WEIGHTS_FILENAMES if (model_dir / name).is_file()), None
    )
    if weights is None:
        msg = f"{model_dir} 里没有 {' / '.join(WEIGHTS_FILENAMES)}。{model_dir_search_hint()}"
        raise AsrUnavailable(msg)
    recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(weights),
        tokens=str(model_dir / "tokens.txt"),
        num_threads=max(1, int(threads or 4)),
        # SenseVoice 的 "auto" 会自动判语种；中英混说时 auto 明显比锁死 zh 稳。
        language=language or "auto",
        use_itn=True,
        debug=False,
    )
    _recognizer_cache[key] = recognizer
    return recognizer


def decode_pcm(
    pcm: Pcm | Sequence[float], *, model_dir: Path, language: str = "auto", threads: int = 4
) -> str:
    """一段 PCM（float32 单声道，±1）一次解码。只在真的用到时才 import numpy/sherpa。"""
    import numpy as np

    recognizer = _recognizer(model_dir, language, threads)
    stream = recognizer.create_stream()
    stream.accept_waveform(SAMPLE_RATE, np.asarray(pcm, dtype="float32").tolist())
    recognizer.decode_stream(stream)
    return strip_tags(str(getattr(stream.result, "text", "") or "").strip())


def transcribe_wav(
    wav_path: Path,
    *,
    model_dir: str | Path = "",
    language: str = "auto",
    threads: int = 4,
    config: AppConfig | None = None,
    models_root: Path | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> Transcript:
    """转写一个 16k 单声道 WAV，返回带时间戳逐句 segments 的 `Transcript`。

    **同步阻塞函数**：一条长音频要吃几十秒 CPU，任务里要用
    `await asyncio.to_thread(transcribe_wav, …)` 调，别在事件循环里直呼。
    全程没达到语音门限 → `text=""`、`segments=[]`（这是结论，不是失败；
    调用方按"没有口播"记账，不要记成 failed=0 那种假绿）。
    """
    status = detect(model_dir, config=config, models_root=models_root)
    if not status.available or status.model_dir is None:
        raise AsrUnavailable(status.reason)
    model = status.model_dir

    import numpy as np

    pcm = read_wav_pcm(wav_path)
    total = int(np.asarray(pcm).shape[0])
    duration = total / SAMPLE_RATE
    groups = group_segments(find_speech_segments(pcm))
    if not groups:
        logger.info("asr.no_speech", file=wav_path.name, duration_seconds=round(duration, 1))
        return _transcript([])

    started = time.perf_counter()
    parts: list[tuple[float, float, str]] = []
    for index, (begin, end) in enumerate(groups, start=1):
        chunk = np.asarray(pcm[begin:end], dtype="float32")
        if chunk.size < FRAME_SAMPLES:
            continue
        text = terminate_sentence(
            decode_pcm(chunk, model_dir=model, language=language, threads=threads)
        )
        if text:
            parts.append((begin / SAMPLE_RATE, end / SAMPLE_RATE, text))
        if progress is not None:
            progress(index, len(groups))
    elapsed = max(0.0, time.perf_counter() - started)
    logger.info(
        "asr.done",
        file=wav_path.name,
        sentences=len(parts),
        duration_seconds=round(duration, 1),
        elapsed_seconds=round(elapsed, 1),
        realtime_x=round(duration / elapsed, 1) if elapsed > MIN_ELAPSED_FOR_SPEED else None,
    )
    return _transcript(parts)


def _transcript(parts: Sequence[tuple[float, float, str]]) -> Transcript:
    """把 (起, 止, 已补标点的文本) 收成契约对象。

    `text` 就是 segments 逐行拼出来的 —— 两处不一致的话，前端按哪一份渲染都会有一半是错的。
    `char_count`/`sentence_count` 与 `bilibili/subtitles.py` 同一口径（正文长度、片段数）。
    """
    segments = [
        TranscriptSegment(start_seconds=round(start, 3), end_seconds=round(end, 3), text=text)
        for start, end, text in parts
    ]
    text = "\n".join(segment.text for segment in segments)
    return Transcript(
        engine=ENGINE_NAME,
        language=None,
        text=text,
        char_count=len(text),
        sentence_count=len(segments),
        segments=segments,
    )
