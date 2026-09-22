"""口播稿相关数据模型。

契约来源：docs/specs/platform-adapter.md §2.4（Transcript / TranscriptSegment）
         docs/specs/data-model.md §2.4（transcripts 表）

V1 §7.5 看护：text_path 统一（废"按平台不对称"的转写目录）。
V1 §7.9 看护：SenseVoice 不产标点，asr 层按静音切句补 '。'。
"""

from __future__ import annotations

from datetime import datetime
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


class TranscriptRecord(BaseModel):
    """DB 行（transcripts 表的 Pydantic 映射）。

    **与 `Transcript` 不是同一个东西**，别混：
    - `Transcript` 是**采集层的产出**（带 `text` 正文与 `segments` 结构），
      适配器 `fetch_subtitles()` / ASR 引擎返回它。
    - `TranscriptRecord` 是**存储层的行**（带 `video_id` / `created_at`，
      正文在磁盘上，只存 `text_path` 与 `segments_json` 字符串）。

    spec `data-model.md §3` 里 `attach_transcript()` 的返回类型写的是 `Transcript`，
    实施时改成 `TranscriptRecord` —— 返回一个没有 `text` 字段的 `Transcript`
    要么得把整篇口播稿读回来（白白一次磁盘 IO），要么得造一个空 `text`（撒谎）。
    """

    video_id: int
    engine: str
    language: str | None = None
    char_count: int
    sentence_count: int
    text_path: str
    segments_json: str | None = None
    created_at: datetime
