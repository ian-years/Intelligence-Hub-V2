"""任务相关数据模型。

契约来源：docs/specs/task-runner.md §2（TaskKind / TaskResult / ArtifactRef / FailureRecord）
         docs/specs/platform-adapter.md §2.4（FailureRecord / ProgressCallback）
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class TaskKind(StrEnum):
    """任务类型。"""

    PLATFORM_COLLECT = "platform_collect"
    ALL_PLATFORMS = "all_platforms"
    SINGLE_LINK = "single_link"
    ADD_CREATOR = "add_creator"
    BACKFILL = "backfill"
    POSTPROCESS = "postprocess"
    SYNC = "sync"
    PREFLIGHT = "preflight"
    MIGRATE = "migrate"


class ArtifactRef(BaseModel):
    """产物引用（媒体 / 口播稿 / metadata 路径）。"""

    kind: Literal["media", "transcript", "metadata", "cover", "manifest"]
    path: Path
    platform: str | None = None
    video_id: str | None = None
    size_bytes: int | None = None


class FailureRecord(BaseModel):
    """清单里的失败记录。

    V1 §1.3 看护：error 字段必须是原文，不许吞错。
    """

    platform: str
    stage: Literal["parse_url", "list", "download", "transcribe", "store"]
    video_id: str | None = None
    creator_id: str | None = None
    error: str
    error_kind: str | None = None
    timestamp: datetime


class TaskResult(BaseModel):
    """任务执行结果。"""

    status: Literal["success", "partial", "failed"]
    summary: dict[str, int | str] = Field(default_factory=dict)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    failures: list[FailureRecord] = Field(default_factory=list)


ProgressCallback = Callable[[float], None]
"""进度回调，0.0~1.0。"""
