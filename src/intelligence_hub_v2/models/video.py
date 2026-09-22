"""视频相关数据模型。

契约来源：docs/specs/platform-adapter.md §2.4（VideoMeta）
         docs/specs/data-model.md §2.3（videos 表）+ §3（Repository Protocol）

V1 §7.4 看护：update_fields() 字段级更新，VideoUpdatableFields TypedDict 限定可更新字段集。
V1 §7.25 看护：is_hidden 列内化墓碑，list_visible() 自动过滤。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field, HttpUrl

from intelligence_hub_v2.models.creator import CreatorRef

T = TypeVar("T")


class VideoMeta(BaseModel):
    """视频元数据（list_creator_videos 的产出）。

    V1 §7.11 看护：统一字段名 creator_ref（内含 platform_id），
    不再有 creator_platform_id / mid 三种写法。
    """

    platform: str
    platform_video_id: str
    creator_ref: CreatorRef
    title: str
    description: str | None = None
    published_at: datetime | None = None
    duration_seconds: float | None = None
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    share_count: int | None = None
    cover_url: HttpUrl | None = None
    webpage_url: HttpUrl
    extra: dict[str, Any] = Field(default_factory=dict)


class VideoDraft(BaseModel):
    """写入 DB 前的视频草稿。"""

    platform: str
    platform_video_id: str
    creator_id: int | None = None
    title: str
    description: str | None = None
    published_at: datetime | None = None
    duration_seconds: float | None = None
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    share_count: int | None = None
    media_path: str | None = None
    media_source: str | None = None
    media_aux_paths_json: str = "[]"
    cover_path: str | None = None
    metadata_json: str = "{}"


class Video(BaseModel):
    """DB 行（videos 表的 Pydantic 映射）。

    V1 §7.25 看护：is_hidden + hidden_at + hidden_reason 内化墓碑。
    """

    id: int
    platform: str
    platform_video_id: str
    creator_id: int | None = None
    title: str
    description: str | None = None
    published_at: datetime | None = None
    duration_seconds: float | None = None
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    share_count: int | None = None
    media_path: str | None = None
    media_source: str | None = None
    media_aux_paths_json: str = "[]"
    cover_path: str | None = None
    metadata_json: str = "{}"
    is_hidden: bool = False
    hidden_at: datetime | None = None
    hidden_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class VideoFilters(BaseModel):
    """列表过滤条件。"""

    platform: str | None = None
    creator_id: int | None = None
    is_hidden: bool | None = False  # 默认只返回可见
    since: datetime | None = None
    search: str | None = None


class Page(BaseModel):
    """分页参数。"""

    page: int = Field(default=1, ge=1)
    size: int = Field(default=20, ge=1, le=200)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size


class PagedResult(BaseModel, Generic[T]):
    """分页结果。"""

    items: list[T]
    total: int
    page: int
    size: int

    @property
    def pages(self) -> int:
        return max(1, (self.total + self.size - 1) // self.size)
