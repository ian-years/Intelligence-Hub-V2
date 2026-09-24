"""静音切句与打包（`asr/engine.py` 的纯算法那一半）。

信号全是**合成**的（numpy 直接生成 16k 单声道 float32）：这里要钉的是形状与不变量
（"每段够长""组与组不重叠""打包不丢采样"），不是某一段真口播切出来长什么样 ——
真音频那一档在 `tests/unit/test_asr_engine.py` 的假 recognizer 路，与
`docs/progress/2026-09-24.md` 记的真权重实跑。

反幻觉闸（V1 原话："SenseVoice 对静音/纯音会稳定幻觉出'我。'这类单字句"）在这里有两条：
纯音与平稳噪声都必须**一段都不切出来**。
"""

from __future__ import annotations

import itertools
from typing import Any

import numpy as np

from intelligence_hub_v2.asr.engine import (
    FRAME_MS,
    FRAME_SAMPLES,
    MAX_CHUNK_SECONDS,
    MIN_FLAT_FRAMES,
    MIN_SPEECH_MS,
    SAMPLE_RATE,
    SEGMENT_PAD_SECONDS,
    SHORT_CLIP_SECONDS,
    SILENCE_GAP_MS,
    TARGET_CHUNK_SECONDS,
    find_speech_segments,
    group_segments,
)


def _tone(seconds: float, *, freq: float = 440.0, amplitude: float = 0.3) -> Any:
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype("float32")


def _noise(seconds: float, *, amplitude: float = 0.1, seed: int = 7) -> Any:
    rng = np.random.default_rng(seed)
    return (amplitude * rng.uniform(-1.0, 1.0, int(seconds * SAMPLE_RATE))).astype("float32")


def _speech_like(seconds: float, *, amplitude: float = 0.35, wobble_hz: float = 3.1) -> Any:
    """有能量包络起伏的载波 —— 口播再平静也有起伏，这正是平坦度闸放过它的理由。"""
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    envelope = (0.45 + 0.55 * np.sin(2 * np.pi * wobble_hz * t)) ** 2
    return (amplitude * envelope * np.sin(2 * np.pi * 170.0 * t)).astype("float32")


def _silence(seconds: float) -> Any:
    return np.zeros(int(seconds * SAMPLE_RATE), dtype="float32")


def _burst_sequence(*runs: tuple[float, float]) -> Any:
    """按 (秒数, 振幅) 串起来，振幅 0 = 静音间隔。"""
    pieces: list[Any] = []
    for seconds, amplitude in runs:
        count = int(seconds * SAMPLE_RATE)
        if amplitude == 0.0:
            pieces.append(np.zeros(count, dtype="float32"))
        else:
            t = np.arange(count) / SAMPLE_RATE
            envelope = (0.4 + 0.6 * np.abs(np.sin(2 * np.pi * 4.3 * t))) * amplitude
            pieces.append((envelope * np.sin(2 * np.pi * 190.0 * t)).astype("float32"))
    return np.concatenate(pieces)


def _durations(segments: list[tuple[int, int]]) -> list[float]:
    return [(end - begin) / SAMPLE_RATE for begin, end in segments]


def _spans(segments: list[tuple[int, int]]) -> list[tuple[float, float]]:
    return [(begin / SAMPLE_RATE, end / SAMPLE_RATE) for begin, end in segments]


# --------------------------------------------------------------------------- #
# 反幻觉闸：这三种输入压根不该喂给模型
# --------------------------------------------------------------------------- #


def test_pure_silence_produces_no_segments() -> None:
    assert find_speech_segments(_silence(6.0)) == []


def test_a_steady_tone_is_not_speech() -> None:
    """1kHz 纯音：能量足够高，但**平坦**。SenseVoice 喂进去会稳定幻觉出重复单字句。"""
    assert find_speech_segments(_tone(6.0)) == []


def test_stationary_broadband_noise_is_not_speech() -> None:
    """平稳噪声/音乐垫底同理：有声帧的 CV 接近 0，闸要落下。"""
    assert find_speech_segments(_noise(6.0)) == []


def test_the_flatness_gate_needs_enough_voiced_frames_before_it_may_cut() -> None:
    """V1 加 `MIN_FLAT_FRAMES` 的理由：**短促真口播**只有几帧，std 恒为 0，会被平坦闸误杀。

    这里给一个 60ms（=3 帧）的音粒，长音频里独一份。帧数不够 → 闸不上 → 切得出来。
    """
    blip_frames = MIN_SPEECH_MS // FRAME_MS
    assert blip_frames < MIN_FLAT_FRAMES, "夹具失效：这一段本来就该走平坦闸"
    pcm = np.concatenate(
        [_silence(1.0), _tone(blip_frames * FRAME_MS / SAMPLE_RATE), _silence(4.0)]
    )
    segments = find_speech_segments(pcm)
    assert len(segments) == 1
    assert _durations(segments)[0] * 1000 >= MIN_SPEECH_MS


# --------------------------------------------------------------------------- #
# 正常切句的不变量
# --------------------------------------------------------------------------- #


def test_two_bursts_across_a_gap_become_two_segments() -> None:
    pcm = _burst_sequence((1.2, 0.4), (0.6, 0.0), (1.4, 0.4))
    segments = find_speech_segments(pcm)
    assert len(segments) == 2
    first, second = _spans(segments)
    # 间隔（1.2s~1.8s）中间那段静音不许被任何一段盖住
    assert first[1] < 1.5 < second[0]


def test_segments_are_ordered_and_only_touch_within_the_padding() -> None:
    """一条通用的不变量，不挑具体夹具。

    **相邻段可以重叠**，重叠上限是两侧各外扩的那点（`2 * SEGMENT_PAD_SECONDS`）：
    真机第一跑（67 秒口播，2026-09-24）量出来就是 `(0, 8.12) → (7.94, 12.1)`，
    差 0.18 秒。把"不重叠"当判据是错的，那等于要求外扩不存在。
    """
    pcm = _burst_sequence((0.9, 0.4), (0.4, 0.0), (1.1, 0.35), (0.5, 0.0), (0.8, 0.4))
    total = int(pcm.shape[0])
    segments = find_speech_segments(pcm)
    assert len(segments) >= 3
    max_overlap = int(2 * SEGMENT_PAD_SECONDS * SAMPLE_RATE) + FRAME_SAMPLES
    previous: tuple[int, int] | None = None
    for begin, end in segments:
        assert 0 <= begin < end <= total
        assert (end - begin) / SAMPLE_RATE * 1000 >= MIN_SPEECH_MS
        if previous is not None:
            assert begin >= previous[0], "起点必须单调递增，否则时间戳没法用来跳转"
            assert begin - previous[1] >= -max_overlap, (
                f"重叠 {previous[1] - begin} 个采样，超过外扩能解释的范围："
                "多半是膨胀（SILENCE_GAP_MS）把两段真口播粘成了一段"
            )
        previous = (begin, end)


def test_two_bursts_close_together_are_padded_into_overlapping_segments() -> None:
    """停顿比外扩还短时，两段必然重叠 —— 这是设计，不是 bug。

    200ms 的停顿 < 2×120ms 的外扩。丢帧（宁可少给一点上下文）才是真错，
    所以这里要的是"仍然两段、且重叠不超上限"，不是"合并成一段"。
    """
    pcm = _burst_sequence((1.0, 0.4), (0.2, 0.0), (1.0, 0.4))
    segments = find_speech_segments(pcm)
    assert len(segments) == 2, f"应该还是两段，实际 {segments}"
    (_first_begin, first_end), (second_begin, second_end) = segments
    assert second_begin < first_end, "200ms 的停顿下外扩必然让它们交叠"
    assert first_end - second_begin <= int(2 * SEGMENT_PAD_SECONDS * SAMPLE_RATE)
    assert second_end > first_end


def test_the_same_tone_a_foot_longer_is_cut_by_the_flatness_gate() -> None:
    """同一支正弦，400ms（=20 帧 ≥ `MIN_FLAT_FRAMES`）→ **一段都不切**。

    与上一条配成一对：闸的开关条件是"有声帧够不够数出离散度"，不是音量。
    只写一条的话，下一人会把这道闸误删成"短的也不切"或"长的照切"。
    """
    blip = _tone(0.4, amplitude=0.4)
    pcm = np.concatenate([_silence(1.0), blip, _silence(4.0)])
    assert pcm.shape[0] / SAMPLE_RATE > SHORT_CLIP_SECONDS
    assert find_speech_segments(pcm) == []


def test_dilation_makes_any_detected_blip_at_least_min_speech_long() -> None:
    """膨胀（两侧各 `SILENCE_GAP_MS/2` 帧）先于长度闸，所以**一粒被检出的声音必然够长**。

    量出来的：60ms 的粒切出来是 460ms（10ms→420ms、400ms 那档直接被上一题的闸落下）。
    两个后果记在这儿，别当它们是现役判据（详见 `docs/lessons.md` 经验 49）：
    - `MIN_SPEECH_MS` 在当前常数下**拦不住任何东西**（膨胀后的最短一段是 180ms > 120ms）；
    - `find_speech_segments` 末尾那条"短片段整段交给模型"的兜底因此走不到。
    两处都不许删：把 `SILENCE_GAP_MS` 调小或把 `FRAME_MS` 调大，它们立刻就活。
    """
    blip = _tone(MIN_SPEECH_MS / 1000 / 2, amplitude=0.4)
    pcm = np.concatenate([_silence(1.0), blip, _silence(4.0)])
    (begin, end) = find_speech_segments(pcm)[0]
    duration_ms = (end - begin) / SAMPLE_RATE * 1000
    assert duration_ms >= MIN_SPEECH_MS
    assert (end - begin) // FRAME_SAMPLES >= 2 * (SILENCE_GAP_MS // FRAME_MS // 2) + 1


def test_segments_are_padded_on_both_sides_without_running_off_the_end() -> None:
    pcm = _burst_sequence((1.0, 0.4), (0.5, 0.0), (1.0, 0.4))
    total = int(pcm.shape[0])
    segments = find_speech_segments(pcm)
    begins = [begin for begin, _ in segments]
    ends = [end for _, end in segments]
    assert min(begins) == 0 and max(ends) <= total
    # 首段从 0 起 = 外扩被边界夹住了，没有负下标
    assert all(begin >= 0 and end <= total for begin, end in segments)


# --------------------------------------------------------------------------- #
# 打包：MAX 硬切 / TARGET 分批，且**不丢采样**
# --------------------------------------------------------------------------- #


def test_grouping_keeps_every_voiced_sample_exactly_once() -> None:
    segments = [
        (0, 30_000),
        (32_000, 60_000),
        (95_000, 130_000),
        (131_000, 160_000),
    ]
    groups = group_segments(segments)
    covered = sum(end - begin for begin, end in groups)
    voiced = sum(end - begin for begin, end in segments)
    assert covered >= voiced, "打包把语音样本弄丢了：那段口播不会被识别"
    for prev, nxt in itertools.pairwise(groups):
        assert prev[1] <= nxt[0], "组与组重叠 = 同一句被识别两次"


def test_a_run_on_segment_is_hard_cut_at_the_max_chunk_length() -> None:
    """一条 40 秒的连续语音（没有停顿可切）必须硬切成不超过 15s 的几批。

    这条是 V1 第 2 条理由的全部意义：整段一次解码的耗时无界。
    """
    total = int(40.0 * SAMPLE_RATE)
    segments = [(0, total)]
    groups = group_segments(segments)
    assert len(groups) >= 3
    for begin, end in groups:
        assert (end - begin) / SAMPLE_RATE <= MAX_CHUNK_SECONDS + 0.001
    assert groups[0][0] == 0 and groups[-1][1] == total
    assert sum(end - begin for begin, end in groups) == total, "硬切把音频切掉了一截"


def test_batches_close_when_they_pass_the_target_length() -> None:
    """攒够 TARGET_CHUNK_SECONDS 就在下一个停顿前收尾：一批不该远过一句的长度。"""
    step = int(3.0 * SAMPLE_RATE)
    segments = [(i * step + 500, (i + 1) * step) for i in range(5)]  # 5 段 × 3s，中间都有停顿
    groups = group_segments(segments)
    assert len(groups) >= 2
    assert all(
        (end - begin) / SAMPLE_RATE <= TARGET_CHUNK_SECONDS + 3.5 for begin, end in groups
    ), "打包没在 target 处收尾，一批攒成了整段"


def test_empty_input_groups_to_nothing() -> None:
    assert group_segments([]) == []
