"""`/api/videos*`：作品流的分页读 + 隐藏/取消隐藏（V1 §7.25 的墓碑走 `is_hidden`）。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel

from intelligence_hub_v2.api.deps import get_state
from intelligence_hub_v2.models.event import (
    Event,
    EventType,
    VideoHiddenPayload,
    VideoUnhiddenPayload,
)
from intelligence_hub_v2.models.video import Page, PagedResult, Video, VideoFilters
from intelligence_hub_v2.tasks.params import SingleLinkParams

if TYPE_CHECKING:
    from intelligence_hub_v2.api.deps import AppState

router = APIRouter(tags=["videos"])

_HIDDEN_MODES = Literal["visible", "hidden", "all"]

#: `/api/videos?sort=` 的全部取值。与 `models/video.py:VideoFilters.sort` 同一份名单，
#: 加一种顺序要**两处同时改**（模型那一处没有它就不会真的生效）。
_SORT_MODES = Literal["recent", "benchmark"]


class HideRequest(BaseModel):
    reason: str


class TaskAccepted(BaseModel):
    task_id: str


def _filters(
    *,
    platform: str | None,
    creator_id: int | None,
    since: datetime | None,
    search: str | None,
    hidden: str,
    sort: Literal["recent", "benchmark"] = "recent",
) -> VideoFilters:
    is_hidden: bool | None = {"visible": False, "hidden": True, "all": None}.get(hidden, False)
    return VideoFilters(
        platform=platform,
        creator_id=creator_id,
        since=since,
        search=search,
        is_hidden=is_hidden,
        sort=sort,
    )


@router.get("/videos", response_model=PagedResult[Video])
async def list_videos(
    state: AppState = Depends(get_state),
    platform: str | None = None,
    creator_id: int | None = None,
    since: datetime | None = None,
    search: str | None = None,
    hidden: _HIDDEN_MODES = Query(default="visible"),
    sort: _SORT_MODES = Query(default="recent"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PagedResult[Video]:
    """分页列作品。`sort=benchmark` 是"按点赞数从大到小"（T6.6 的爆款回溯入口用）。

    为什么是一个 `sort` 值而不是 `min_likes` 阈值参数：入口要回答的是"这个人历史上
    水花最大的几条"，而"多少算爆款"各家平台差一个数量级（抖音十万赞是日常，
    B 站一千算爆款）—— 阈值放进筛选器会伪装成一个平台无关的常量。排序 + `size`
    就是"取前 N 条"，要收紧就在界面上少列几条。
    """
    filters = _filters(
        platform=platform,
        creator_id=creator_id,
        since=since,
        search=search,
        hidden=hidden,
        sort=sort,
    )
    return await state.storage.videos.list_visible(filters=filters, page=Page(page=page, size=size))


@router.get("/videos/{video_id}", response_model=Video)
async def get_video(video_id: int, state: AppState = Depends(get_state)) -> Video:
    # 隐藏的作品详情仍要能打开（§7.25：墓碑只管列表，不管取不到）
    return await state.storage.videos.get_or_raise(video_id)


@router.post(
    "/videos/single-link", response_model=TaskAccepted, status_code=status.HTTP_202_ACCEPTED
)
async def collect_single_link(
    body: SingleLinkParams, state: AppState = Depends(get_state)
) -> TaskAccepted:
    task_id = state.scheduler.submit("single_link", body.model_dump())
    return TaskAccepted(task_id=task_id)


@router.patch("/videos/{video_id}/hide", response_model=Video)
async def hide_video(
    video_id: int, body: HideRequest, state: AppState = Depends(get_state)
) -> Video:
    """隐藏（情报流的"删除"）。不删媒体、不删稿子 —— 文件是全库唯一的原始产物。"""
    video = await state.storage.videos.hide(video_id, body.reason)
    await state.events.publish(
        Event(
            type=EventType.VIDEO_HIDDEN,
            timestamp=datetime.now(UTC),
            payload=VideoHiddenPayload(video_id=video.id, reason=body.reason).model_dump(
                mode="json"
            ),
        )
    )
    return video


@router.patch("/videos/{video_id}/unhide", response_model=Video)
async def unhide_video(video_id: int, state: AppState = Depends(get_state)) -> Video:
    video = await state.storage.videos.unhide(video_id)
    await state.events.publish(
        Event(
            type=EventType.VIDEO_UNHIDDEN,
            timestamp=datetime.now(UTC),
            payload=VideoUnhiddenPayload(video_id=video.id).model_dump(mode="json"),
        )
    )
    return video
