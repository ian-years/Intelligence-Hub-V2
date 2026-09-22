"""口播稿相关数据模型。

契约来源：docs/specs/platform-adapter.md §2.4（Transcript / TranscriptSegment）
         docs/specs/data-model.md §2.4（transcripts 表）

V1 §7.5 看护：text_path 统一（废"按平台不对称"的转写目录）。
V1 §7.9 看护：SenseVoice 不产标点，asr 层按静音切句补 '。'。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class TranscriptSegment(BaseModel):
    """口播稿片段（带时间戳）。"""

    start_seconds: float
    end_seconds: float
    text: str


class Transcript(BaseModel):
    """口播稿（fetch_subtitles 的输出，或 ASR 引擎的输出）。"""

    engine: Literal["sherpa_sense_voice", "bilibili_subtitle", "youtube_subtitle", "manual"]
    language: str | None = None
    text: str
    char_count: int
    sentence_count: int
    segments: list[TranscriptSegment] = Field(default_factory=list)
    text_path: Path | None = None


class TranscriptDraft(BaseModel):
    """写入 DB 前的口播稿草稿。"""

    engine: str
    language: str | None = None
    char_count: int
    sentence_count: int
    text_path: str
    segments_json: str | None = None
