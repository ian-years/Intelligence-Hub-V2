"""博主相关数据模型。

契约来源：docs/specs/platform-adapter.md §2.4（CreatorRef / CreatorProfile）
         docs/specs/data-model.md §2.2（creators 表）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, HttpUrl


class CreatorRef(BaseModel):
    """博主引用（parse_creator_url 的输出）。

    V1 §7.1 看护：platform_id 必须是平台原生 ID（sec_uid / mid / user_id / channel_id），
    不是 URL 里的东西。短链必须跟 302 才能拿到。
    """

    platform: str
    platform_id: str
    profile_url: HttpUrl
    source_url: HttpUrl | None = None


class CreatorProfile(BaseModel):
    """博主资料（fetch_creator_profile 的输出）。"""

    ref: CreatorRef
    name: str
    avatar_url: HttpUrl | None = None
    follower_count: int | None = None
    bio: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class CreatorDraft(BaseModel):
    """写入 DB 前的博主草稿。"""

    platform: str
    platform_id: str
    name: str
    avatar_url: str | None = None
    follower_count: int | None = None
    profile_url: str
    is_tracking: bool = True
    metadata_json: str = "{}"


class Creator(BaseModel):
    """DB 行（creators 表的 Pydantic 映射）。

    V1 §7.24 看护：is_tracking 必须真布尔，DB 层有 CHECK (is_tracking IN (0, 1))。
    """

    id: int
    platform: str
    platform_id: str
    name: str
    avatar_url: str | None = None
    follower_count: int | None = None
    profile_url: str
    is_tracking: bool
    metadata_json: str
    created_at: datetime
    updated_at: datetime
