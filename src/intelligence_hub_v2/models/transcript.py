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

from pydantic import BaseModel, Field, model_validator


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
    # ADR-0015：摘要与要点跟着稿子走（同表、同一次 `attach()` 整行替换）。
    content_summary: str | None = None
    key_points: str | None = None
    summary_method: str | None = None

    @model_validator(mode="after")
    def _summary_needs_its_provenance(self) -> TranscriptDraft:
        """有摘要却没写来源就拒绝入库 —— `summary_method` 是那张"能信到什么程度"的标签，
        没有它，本地抽取式（≤600 字片段）与 V1 搬来的整篇改写长得一模一样。"""
        if (self.content_summary or self.key_points) and not self.summary_method:
            msg = "写摘要/要点必须同时写 summary_method（local-extractive / v1-imported）"
            raise ValueError(msg)
        return self


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
    content_summary: str | None = None
    key_points: str | None = None
    summary_method: str | None = None
    created_at: datetime
