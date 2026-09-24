"""`postprocess` handler：字幕优先，其次本地 ASR。

对每条"有媒体、没口播稿"的作品：

1. 平台声明 `supports_subtitles=True`（B站 / 未来的 YouTube）→ 先问字幕。
   拿到轨就写字幕稿；`None`（契约里的"确实没有轨"）记一笔 `subtitle_missed`
   并**回落到本地 ASR**（T1.3 —— V1 的 B站 后处理就是这个顺序）；抛出来的才是失败，
   失败时不回落（下一轮还会挑到它）。
2. 其余（抖音/小红书，或没有字幕轨的作品）→ `extract_audio` 抽 16k 单声道 WAV →
   `asr.transcribe_wav` → `normalize_transcript` → 落 `speech-clean.txt` +
   `segments.json` + `reference.md` → 写 `transcripts` 行 + 发 `TRANSCRIPT_READY`。

三份产物，**V1 的 `speech-raw.txt` 不落**：ASR 的原始输出与归一化结果只差空白折叠，
落两份近似文件等于给前端第二个可读答案（§7.5/§7.11 同一族）。要查中间状态看 `segments.json`。

引擎不可用时**整批记一条红**（不是每条一个 failure，更不是静默跳过）：
`uv sync --extra asr` 与"补上 233 MB 权重"是两种修法，preflight 的 `asr_engine` 已经把它们
分开了，这里只负责让任务不绿（AGENTS §1.3）。

`fetch_subtitles` 的契约（`platforms/base.py`）：**"确实没有"返回 None，"没问到"抛**。
所以 `None` 是"回落"的信号，抛出来才进 `failures[]`。

与 `single_link` 一样用 `canonical_video_url` 从 `platform_video_id` 反推 `webpage_url`
（库行不存原始分享链，那是会过期的）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from intelligence_hub_v2.asr import detect as asr_detect
from intelligence_hub_v2.asr.engine import AsrUnavailable, transcribe_wav
from intelligence_hub_v2.infra.ffmpeg import extract_audio, has_audio_stream
from intelligence_hub_v2.models.creator import CreatorRef
from intelligence_hub_v2.models.event import EventType
from intelligence_hub_v2.models.task import FailureRecord, TaskResult
from intelligence_hub_v2.models.transcript import Transcript, TranscriptDraft
from intelligence_hub_v2.models.video import Video, VideoMeta
from intelligence_hub_v2.tasks.dispatch import canonical_video_url
from intelligence_hub_v2.tasks.params import PostprocessParams
from intelligence_hub_v2.tasks.reference import (
    MIN_TRANSCRIPT_CHARS,
    extractive_reference,
    normalize_transcript,
    speech_length,
)

if TYPE_CHECKING:
    from intelligence_hub_v2.asr.engine import EngineStatus
    from intelligence_hub_v2.tasks.definition import TaskContext

__all__ = ["run_postprocess"]


async def run_postprocess(ctx: TaskContext, params: PostprocessParams) -> TaskResult:
    ctx.check_cancelled()
    videos = await _resolve_targets(ctx, params)
    tally = _TranscribeTally()
    # 引擎状态问一次就够：它读的是文件系统与 sys.modules，50 条作品问 50 次只会
    # 在同一轮里给出两种答案（有人在跑的时候装上了包）。
    engine = asr_detect(models_root=ctx.files.asr_models_dir)
    total = max(1, len(videos))

    for index, video in enumerate(videos):
        ctx.check_cancelled()
        await _process_one(ctx, tally, video=video, engine=engine)
        await ctx.progress((index + 1) / total, stage="transcribe", current_item=video.title[:40])

    return tally.to_result()


async def _resolve_targets(ctx: TaskContext, params: PostprocessParams) -> list[Video]:
    if params.video_ids:
        return [await ctx.storage.videos.get_or_raise(vid) for vid in params.video_ids]
    missing = await ctx.storage.transcripts.list_missing(
        platform=params.platform, limit=params.limit
    )
    return [video for vid in missing if (video := await ctx.storage.videos.get(vid)) is not None]


async def _process_one(
    ctx: TaskContext, tally: _TranscribeTally, *, video: Video, engine: EngineStatus
) -> None:
    if video.media_path is None:
        # 连媒体都没有，字幕与 ASR 都无从谈起：交给采集补齐，不算这一格的失败。
        tally.no_media += 1
        return

    if ctx.adapters.capabilities(video.platform).supports_subtitles:
        try:
            transcript = await ctx.adapters.get(video.platform).fetch_subtitles(_video_meta(video))
        except Exception as exc:  # noqa: BLE001 - "没问到"是异常，必须留原文进 failures（V1 §1.3）
            # 问到失败（412、网络）就不去跑 ffmpeg：这条下一轮还会被挑出来。
            tally.record_failure(video=video, error=str(exc), kind="Subtitle")
            return
        if transcript is not None:
            await _store(ctx, video=video, transcript=transcript, reference=None)
            tally.transcribed += 1
            return
        # `None` = 契约里的"确实没有轨"。没有轨不等于这条转不了：回落到听音频，
        # V1 的 B站 后处理本来就是这个顺序（T1.3）。
        tally.subtitle_missed += 1

    await _transcribe_with_asr(ctx, tally, video=video, engine=engine)


# --------------------------------------------------------------------------- #
# ASR 那一条路
# --------------------------------------------------------------------------- #


async def _transcribe_with_asr(
    ctx: TaskContext, tally: _TranscribeTally, *, video: Video, engine: EngineStatus
) -> None:
    if not engine.available:
        tally.record_asr_blocked(video=video, reason=engine.reason)
        return

    media_path = ctx.files.abs(str(video.media_path))
    media_dir = media_path.parent
    source = _audio_source(ctx, video=video)
    if not await has_audio_stream(source):
        # 纯视频轨（DASH 未合并却没存音频 aux）：这条**没有可转写的声音**，不是失败。
        # 记成 no_audio 而不是硬跑 ffmpeg —— 那句 `Output file does not contain any stream`
        # 的字面意思会把人引向"ffmpeg 没装"（V1 §7.21）。
        tally.no_audio += 1
        return

    wav = ctx.files.asr_audio_path(media_dir)
    try:
        await extract_audio(source, wav)
    except (RuntimeError, OSError) as exc:
        tally.record_failure(video=video, error=str(exc), kind="Audio")
        return

    try:
        # 一条 67 秒的口播要 7.5 秒 CPU：直呼会把整个事件循环钉住（cancel 与 SSE 都停）。
        transcript = await asyncio.to_thread(
            transcribe_wav, wav, models_root=ctx.files.asr_models_dir
        )
    except AsrUnavailable as exc:
        tally.record_failure(video=video, error=str(exc), kind="Asr")
        return

    text = normalize_transcript(transcript.text)
    if speech_length(text) < MIN_TRANSCRIPT_CHARS:
        # 反幻觉闸的第二层：能量闸放过去、却只换来"我。我。"的那种输入。
        # 仍然 attach（这条确实转过了，别让它每轮都被重跑一遍），但不产 reference.md：
        # 从一串幻觉句里抽"候选句"是在给噪声化妆。
        await _store(ctx, video=video, transcript=transcript, text=text, reference=None)
        tally.no_speech += 1
        return

    reference = extractive_reference(
        title=video.title,
        video_id=video.platform_video_id,
        platform=video.platform,
        url=canonical_video_url(video.platform, video.platform_video_id),
        cleaned_text=text,
    )
    await _store(ctx, video=video, transcript=transcript, text=text, reference=reference.markdown)
    tally.transcribed += 1


def _audio_source(ctx: TaskContext, *, video: Video) -> Path:
    """要喂给 ffmpeg 的那个文件。

    `media_aux_paths_json` 非空 = 采集时拿到的是未合并的 DASH 分片，那条 aux 就是**纯音频轨**
    （V1 §7.21：主文件 `.f137.mp4` 没有音频，直接喂 ffmpeg 得到的报错长得像"没装 ffmpeg"）。
    判据与 `models.media.audio_path_of()` 同一个，只是那边拿到的是适配器产物、
    这边拿到的是库行 —— 两处都不是"看一眼文件名猜"。
    """
    raw = video.media_aux_paths_json or "[]"
    try:
        aux = json.loads(raw)
    except json.JSONDecodeError:
        aux = []
    first = aux[0] if isinstance(aux, list) and aux else None
    return ctx.files.abs(str(first or video.media_path))


def _video_meta(video: Video) -> VideoMeta:
    """库行 → 适配器要的 `VideoMeta`（`fetch_subtitles` 只读 id 与 webpage_url）。"""
    return VideoMeta.model_validate(
        {
            "platform": video.platform,
            "platform_video_id": video.platform_video_id,
            "creator_ref": CreatorRef.model_validate(
                {
                    "platform": video.platform,
                    "platform_id": str(video.creator_id or 0),
                    "profile_url": canonical_video_url(video.platform, video.platform_video_id),
                }
            ),
            "title": video.title,
            "webpage_url": canonical_video_url(video.platform, video.platform_video_id),
        }
    )


# --------------------------------------------------------------------------- #
# 落盘 + 入库（字幕与 ASR 两条路共用这一步）
# --------------------------------------------------------------------------- #


async def _store(
    ctx: TaskContext,
    *,
    video: Video,
    transcript: Transcript,
    text: str | None = None,
    reference: str | None = None,
) -> None:
    """写 `speech-clean.txt`（+ `segments.json` / `reference.md`）并 attach `transcripts` 行。

    `text` 是归一化之后的正文（字幕那条路传 None = 用引擎交回的原文）。
    `sentence_count` 取的是 `segments` 的条数，与 `bilibili/subtitles.py` 同一口径；
    ASR 那条路的 `normalize_transcript` 只折叠空白、丢字幕署名行，而动 ASR 稿子里
    既没有署名行也没有空行 —— 所以行数不会与 segments 分叉（有用例钉着这条）。
    """
    media_dir = ctx.files.abs(str(video.media_path)).parent
    body = transcript.text if text is None else text
    path = ctx.files.transcript_path(media_dir)
    products: list[tuple[Path, str]] = [(path, body.rstrip("\n") + "\n")]
    if transcript.segments:
        products.append((ctx.files.segments_path(media_dir), _segments_payload(transcript)))
    if reference:
        products.append((ctx.files.reference_path(media_dir), reference))
    await asyncio.to_thread(_write_files, products)

    draft = TranscriptDraft(
        engine=transcript.engine,
        language=transcript.language,
        char_count=len(body),
        sentence_count=transcript.sentence_count,
        text_path=ctx.files.rel(path),
        segments_json=_segments_json(transcript),
    )
    await ctx.storage.transcripts.attach(video.id, draft)
    await ctx.publish(
        EventType.TRANSCRIPT_READY,
        {
            "video_id": video.id,
            "engine": draft.engine,
            "char_count": draft.char_count,
            "sentence_count": draft.sentence_count,
        },
    )


def _write_files(products: list[tuple[Path, str]]) -> None:
    """一次写齐这几份文件。目录可能都还不存在（媒体目录建了但没有 `transcript/`）。"""
    for target, content in products:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _segments_payload(transcript: Transcript) -> str:
    """`segments.json` 的正文（缩进的，给人看）。"""
    payload = [segment.model_dump(mode="json") for segment in transcript.segments]
    return json.dumps(payload, ensure_ascii=False, indent=1)


def _segments_json(transcript: Transcript) -> str | None:
    """`transcripts.segments_json` 那一列（紧凑的，给机器读）。

    与 `segments.json` 文件**同一份数据、两种排版**是有意的：库里那列会被
    前端一次一次反序列化，磁盘那份是产物归档，人可以打开看。
    """
    if not transcript.segments:
        return None
    payload = [segment.model_dump(mode="json") for segment in transcript.segments]
    return json.dumps(payload, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 计数
# --------------------------------------------------------------------------- #


class _TranscribeTally:
    """一趟后处理的计数器 + 失败明细。

    七个计数各回答一个问题，别合并：`transcribed` 是"有稿子"，`no_speech` 是"听过音频但
    没口播"，`no_audio` 是"这条媒体压根没有音频轨"，`subtitle_missed` 是"字幕那边说没有轨，
    已经回落到听音频"，`asr_blocked` 是"这轮根本没法转（引擎缺位）"。
    合成一个"跳过"就把五种完全不同的修法说成了同一句话。
    """

    def __init__(self) -> None:
        self.transcribed = 0
        self.subtitle_missed = 0
        self.no_speech = 0
        self.no_audio = 0
        self.no_media = 0
        self.asr_blocked = 0
        self.failed = 0
        self.failures: list[FailureRecord] = []
        self._asr_block_reason_recorded = False

    def record_failure(self, *, video: Video, error: str, kind: str) -> None:
        self.failed += 1
        self.failures.append(
            FailureRecord(
                platform=video.platform,
                stage="transcribe",
                video_id=video.platform_video_id,
                error=error,
                error_kind=kind,
            )
        )

    def record_asr_blocked(self, *, video: Video, reason: str) -> None:
        """引擎缺位是**一轮一个原因**，不是每条作品一个原因。

        50 条作品记 50 条一模一样的 failure 会把清单冲满，而真正的信息只有一句：
        装包还是补权重。所以计数照常累加（判定要用），failure 只记第一条。
        """
        self.asr_blocked += 1
        if self._asr_block_reason_recorded:
            return
        self._asr_block_reason_recorded = True
        self.failed += 1
        self.failures.append(
            FailureRecord(
                platform=video.platform,
                stage="transcribe",
                video_id=video.platform_video_id,
                error=f"本地 ASR 不可用，{self.asr_blocked} 条作品没转：{reason}",
                error_kind="AsrUnavailable",
            )
        )

    def to_result(self) -> TaskResult:
        status: Literal["success", "partial", "failed"]
        if self.transcribed == 0 and self.failed > 0:
            status = "failed"
        elif self.failed > 0:
            status = "partial"
        else:
            status = "success"
        summary: dict[str, int | str] = {
            "transcribed": self.transcribed,
            "subtitle_missed": self.subtitle_missed,
            "no_speech": self.no_speech,
            "no_audio": self.no_audio,
            "no_media": self.no_media,
            "asr_blocked": self.asr_blocked,
            "failed": self.failed,
        }
        return TaskResult(status=status, summary=summary, failures=self.failures)
