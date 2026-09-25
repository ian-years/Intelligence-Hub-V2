"""视频相关数据模型。

契约来源：docs/specs/platform-adapter.md §2.4（VideoMeta）
         docs/specs/data-model.md §2.3（videos 表）+ §3（Repository Protocol）

V1 §7.4 看护：update_fields() 字段级更新，VideoUpdatableFields TypedDict 限定可更新字段集。
V1 §7.25 看护：is_hidden 列内化墓碑，list_visible() 自动过滤。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Final, Generic, Literal, TypeVar

from pydantic import BaseModel, Field, HttpUrl, model_validator
from typing_extensions import TypedDict

from intelligence_hub_v2.models.creator import CreatorRef

T = TypeVar("T")

#: 能进 `<video>` 的那些容器。**与 `api/v1/media.py::MEDIA_CONTENT_TYPES` 的键是同一条判据**
#: （模型层不许 import api 层，所以这里重列一份，由
#: `test_has_video_fallback_suffixes_match_the_media_endpoint_whitelist` 钉住不漂）。
_PLAYABLE_CONTAINERS: Final[frozenset[str]] = frozenset({".mp4", ".webm", ".mov", ".m4v", ".mkv"})


def _json_object(raw: str) -> dict[str, Any]:
    """`metadata_json` / `media_aux_paths_json` 这类列的**安全读法**。

    坏 JSON 与不是对象的 JSON 一律当空：这一位是给界面用的，
    为一行脏 metadata 让 `GET /api/videos` 整页 500 是 disproportionate。
    """
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


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

    #: 这一行的 `media_path` **能不能当视频播**。库里没有这一列，它由 `model_validator`
    #: 从 `metadata_json` / `media_path` 现算 —— 所以给个占位默认值就够了。
    #:
    #: ADR-0019 立 `has_video` 这一位的原话是："一张 3 MB 的 jpg 是真的躺在 `media_path`
    #: 里，喂给 `<video>` 只会得到一块**不报错的黑屏**"。而播放器此前只问
    #: "`media_path` 有没有"（`VideoDetail.tsx`）—— 那个判据对图文笔记恰好是错的：
    #: 图文的 `media_path` 有值（第一张原图），但 `/api/videos/{id}/media` 的容器白名单
    #: 会 403 它，于是界面挂出一个永不加载的播放器。**字段在产物上活了一年，
    #: 从没出口到需要它的那一端**（2026-09-25 收到第一条真图文笔记才看见）。
    #:
    #: 为什么是"字段 + validator"而不是 `@computed_field`：后者在本仓库的
    #: mypy-strict + pydantic 插件组合下两种装饰器顺序都走不通（一种 `prop-decorator`
    #: 报错，一种运行时 `PydanticDescriptorProxy is not callable`），实测过。
    has_video: bool = True

    @model_validator(mode="after")
    def _derive_has_video(self) -> Video:
        """`metadata_json.has_video` 优先；没有就按容器表判后缀。

        回落那一支存在的理由是"本改动之前入库的行没有这个键"。它**故意与
        `api/v1/media.py::MEDIA_CONTENT_TYPES` 用同一条判据**（那个端点会不会出这个文件），
        两边不给相反答案；看护是
        `test_has_video_fallback_suffixes_match_the_media_endpoint_whitelist`。
        """
        declared = _json_object(self.metadata_json).get("has_video")
        if isinstance(declared, bool):
            object.__setattr__(self, "has_video", declared)
            return self
        if not self.media_path:
            object.__setattr__(self, "has_video", False)
            return self
        playable = PurePosixPath(self.media_path).suffix.lower() in _PLAYABLE_CONTAINERS
        object.__setattr__(self, "has_video", playable)
        return self


class VideoFilters(BaseModel):
    """列表过滤条件。"""

    platform: str | None = None
    creator_id: int | None = None
    is_hidden: bool | None = False  # 默认只返回可见
    since: datetime | None = None
    search: str | None = None
    #: 排序方式。`recent` 是今天的行为（发布时间倒序）；`benchmark` 按点赞数倒序，
    #: 服务 T6.6 的"爆款回溯"。为什么不是"按快照表的峰值排"：`videos.like_count`
    #: 存的本来就是**最近一次入库的读数**，而点赞只涨不跌，所以它已经是已知最高水位；
    #: 快照表回答的是另一个问题（"发布 24 小时到没到千"这种增长形状）。
    #: NULL（平台没给点赞数）排在最后，而不是当 0 —— 0 赞与"读不出赞数"是两件事。
    sort: Literal["recent", "benchmark"] = "recent"


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


class VideoUpdatableFields(TypedDict, total=False):
    """`VideoRepository.update_fields()` 允许改的字段集。

    **这是 V1 §7.4 的结构性解法**。V1 的 `local_store.upsert_video()` 是"整行覆盖"语义，
    传空标题会把真标题折叠成 `"精选自媒体作品"` —— 部分字段更新抹掉了其他字段。
    V2 只有这一个写入口，且只 SET 你显式传进来的列。

    `total=False` 是**必须的**：所有键都可选，调用方只传要改的那几个。

    故意**不在**这个集合里的三个字段（要走专门方法，不许从这里绕）：
    - `platform` / `platform_video_id` —— 唯一身份，改了等于换了一条作品。
    - `is_hidden` / `hidden_at` / `hidden_reason` —— 走 `hide()` / `unhide()`，
      因为"隐藏"要同时写三个字段并发事件（V1 §7.25：少一半都不算数）。
    """

    title: str
    description: str | None
    published_at: datetime | None
    duration_seconds: float | None
    view_count: int | None
    like_count: int | None
    comment_count: int | None
    share_count: int | None
    media_path: str | None
    media_source: str | None
    media_aux_paths_json: str | None
    cover_path: str | None
    metadata_json: str | None
    creator_id: int | None


UPDATABLE_VIDEO_FIELDS: frozenset[str] = frozenset(VideoUpdatableFields.__annotations__)
"""运行期的白名单。

mypy 只能在**静态可知的调用点**拦住写错字段名；从 dict 展开（`**payload`）
或从 API 请求体来的字段名是运行期才知道的，所以还要这一份。
`VideoRepository.update_fields()` 拿它做校验，未知字段抛 `TypeError`。
"""
