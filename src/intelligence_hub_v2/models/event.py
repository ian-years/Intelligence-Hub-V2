"""事件相关数据模型。

契约来源：docs/specs/event-schema.md §2-§3（EventType / Event / 各 Payload）

V3 重写时这份 schema 是契约：EventType 枚举值不变、各 Payload 模型字段不变。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class EventType(StrEnum):
    """事件类型枚举。"""

    # 任务生命周期
    TASK_STARTED = "task.started"
    TASK_PROGRESS = "task.progress"
    TASK_LOG = "task.log"
    TASK_FINISHED = "task.finished"
    TASK_FAILED = "task.failed"
    TASK_CANCELLED = "task.cancelled"

    # 清单
    MANIFEST_WRITTEN = "manifest.written"

    # 平台
    PLATFORM_HEALTH_CHANGED = "platform.health_changed"

    # 配置
    CONFIG_CHANGED = "config.changed"

    # 数据变更（前端实时刷新）
    CREATOR_ADDED = "creator.added"
    CREATOR_UPDATED = "creator.updated"
    VIDEO_ADDED = "video.added"
    VIDEO_HIDDEN = "video.hidden"
    VIDEO_UNHIDDEN = "video.unhidden"
    TRANSCRIPT_READY = "transcript.ready"


class Event(BaseModel):
    """所有事件的基类。payload 按 type 用对应的 Payload 模型校验。"""

    type: EventType
    task_id: str | None = None
    timestamp: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


# --- 任务生命周期 payload ---


class TaskStartedPayload(BaseModel):
    task_name: str
    kind: str
    params: dict[str, Any] = Field(default_factory=dict)
    config_snapshot: dict[str, Any] = Field(default_factory=dict)


class TaskProgressPayload(BaseModel):
    progress: float = Field(ge=0.0, le=1.0)
    message: str | None = None
    stage: str | None = None
    current_item: str | None = None


class TaskLogPayload(BaseModel):
    level: Literal["debug", "info", "warning", "error"]
    message: str
    logger: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class TaskFinishedPayload(BaseModel):
    status: Literal["success", "partial"]
    summary: dict[str, int | str] = Field(default_factory=dict)
    manifest_path: str
    duration_seconds: float


class TaskFailedPayload(BaseModel):
    status: Literal["failed", "timeout"]
    error: str
    error_kind: str | None = None
    manifest_path: str | None = None
    duration_seconds: float


class TaskCancelledPayload(BaseModel):
    reason: str | None = None
    manifest_path: str
    duration_seconds: float


# --- 清单 payload ---


class ManifestWrittenPayload(BaseModel):
    manifest_path: str
    schema_version: str
    status: str
    summary: dict[str, int | str] = Field(default_factory=dict)


# --- 平台 payload ---


class PlatformHealthChangedPayload(BaseModel):
    platform: str
    previous_status: Literal["ok", "degraded", "unreachable", "unknown"]
    new_status: Literal["ok", "degraded", "unreachable", "unknown"]
    detail: str | None = None
    components: dict[str, str] = Field(default_factory=dict)


# --- 配置 payload ---


class ConfigChangedPayload(BaseModel):
    scope: Literal["app", "platform"]
    platform: str | None = None
    changed_fields: list[str] = Field(default_factory=list)
    requires_restart: bool = False


# --- 数据变更 payload ---


class CreatorAddedPayload(BaseModel):
    creator_id: int
    platform: str
    platform_id: str
    name: str


class CreatorUpdatedPayload(BaseModel):
    creator_id: int
    changed_fields: list[str] = Field(default_factory=list)


class VideoAddedPayload(BaseModel):
    video_id: int
    platform: str
    platform_video_id: str
    title: str
    creator_id: int | None = None


class VideoHiddenPayload(BaseModel):
    video_id: int
    reason: str


class VideoUnhiddenPayload(BaseModel):
    video_id: int


class TranscriptReadyPayload(BaseModel):
    video_id: int
    engine: str
    char_count: int
    sentence_count: int


# --- 存储层 ---


class StoredEvent(Event):
    """落库后的事件（多一个自增 `id`）。

    SSE 推给前端的是 `Event`（没有 id），回放历史时给的是 `StoredEvent`。
    分开这两个类型是为了让"线上推流"和"事后翻历史"这两条路各自拿到
    自己该有的字段 —— 前端不该在实时流里看到一个无意义的 id。
    """

    id: int
