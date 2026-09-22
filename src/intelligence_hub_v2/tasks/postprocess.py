"""`postprocess` handler：字幕优先，本地 ASR 留 V2.1（V2.0 最小实现）。

对每条"有媒体没口播稿"的作品：
- 平台声明 `supports_subtitles=True`（B站 / 未来的 YouTube）→ 走 `fetch_subtitles`；
  拿到轨就写 `speech-clean.txt` + 落 `transcripts` 表；"确实没有轨"记 `no_subtitle`。
- 平台不支持字幕（抖音）→ 记 `needs_asr`，**不做本地转写**：sherpa-onnx 那条路在 V2.1，
  这里既不装模型也不猜稿子（V1 §1.3：没有就是没有，别为了看板绿编一份）。

与 `single_link` 一样用 `canonical_video_url` 从 `platform_video_id` 反推 `webpage_url`
（库行不存原始分享链，那是会过期的）。

`fetch_subtitles` 的契约（`platforms/base.py`）：**"确实没有"返回 None，"没问到"抛**。
所以 `None` 记成 `no_subtitle` 而不是 `failed`；抛出来才进 `failures[]`。
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Literal

from intelligence_hub_v2.models.creator import CreatorRef
from intelligence_hub_v2.models.event import EventType
from intelligence_hub_v2.models.task import FailureRecord, TaskResult
from intelligence_hub_v2.models.transcript import Transcript, TranscriptDraft
from intelligence_hub_v2.models.video import Video, VideoMeta
from intelligence_hub_v2.tasks.dispatch import canonical_video_url
from intelligence_hub_v2.tasks.params import PostprocessParams

if TYPE_CHECKING:
    from pathlib import Path

    from intelligence_hub_v2.tasks.definition import TaskContext

__all__ = ["run_postprocess"]


async def run_postprocess(ctx: TaskContext, params: PostprocessParams) -> TaskResult:
    ctx.check_cancelled()
    videos = await _resolve_targets(ctx, params)
    tally = _TranscribeTally()
    total = max(1, len(videos))

    for index, video in enumerate(videos):
        ctx.check_cancelled()
        await _process_one(ctx, tally, video=video)
        await ctx.progress((index + 1) / total, stage="transcribe", current_item=video.title[:40])

    return tally.to_result()


async def _resolve_targets(ctx: TaskContext, params: PostprocessParams) -> list[Video]:
    if params.video_ids:
        return [await ctx.storage.videos.get_or_raise(vid) for vid in params.video_ids]
    missing = await ctx.storage.transcripts.list_missing(
        platform=params.platform, limit=params.limit
    )
    return [video for vid in missing if (video := await ctx.storage.videos.get(vid)) is not None]


async def _process_one(ctx: TaskContext, tally: _TranscribeTally, *, video: Video) -> None:
    if video.media_path is None:
        tally.needs_asr += 1  # 连媒体都没有，字幕/ASR 都无从谈起，交给采集补齐
        return

    platform = video.platform
    if not ctx.adapters.capabilities(platform).supports_subtitles:
        tally.needs_asr += 1
        return

    meta = _video_meta(video)
    try:
        transcript = await ctx.adapters.get(platform).fetch_subtitles(meta)
    except Exception as exc:  # noqa: BLE001 - "没问到"是异常，必须留原文进 failures（V1 §1.3）
        tally.record_failure(video=video, error=str(exc))
        return

    if transcript is None:
        tally.no_subtitle += 1
        return

    await _store_transcript(ctx, video=video, transcript=transcript)
    tally.transcribed += 1


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


async def _store_transcript(ctx: TaskContext, *, video: Video, transcript: Transcript) -> None:
    assert video.media_path is not None  # _process_one 已挡
    media_dir = ctx.files.abs(video.media_path).parent
    path = ctx.files.transcript_path(media_dir)
    await asyncio.to_thread(_write_transcript, path, transcript.text)

    draft = TranscriptDraft(
        engine=transcript.engine,
        language=transcript.language,
        char_count=transcript.char_count,
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


def _write_transcript(path: Path, text: str) -> None:
    """写正文文件。`transcript/` 目录可能还不存在（媒体目录建了但没稿子目录）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _segments_json(transcript: Transcript) -> str | None:
    if not transcript.segments:
        return None
    payload = [segment.model_dump(mode="json") for segment in transcript.segments]
    return json.dumps(payload, ensure_ascii=False)


class _TranscribeTally:
    def __init__(self) -> None:
        self.transcribed = 0
        self.no_subtitle = 0
        self.needs_asr = 0
        self.failed = 0
        self.failures: list[FailureRecord] = []

    def record_failure(self, *, video: Video, error: str) -> None:
        self.failed += 1
        self.failures.append(
            FailureRecord(
                platform=video.platform,
                stage="transcribe",
                video_id=video.platform_video_id,
                error=error,
                error_kind="Subtitle",
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
            "no_subtitle": self.no_subtitle,
            "needs_asr_v2_1": self.needs_asr,
            "failed": self.failed,
        }
        return TaskResult(status=status, summary=summary, failures=self.failures)
