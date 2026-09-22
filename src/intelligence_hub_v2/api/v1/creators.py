"""`/api/creators*`：博主库的读 + 添加（走 `add_creator` 任务）+ 跟踪开关。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel

from intelligence_hub_v2.api.deps import get_state
from intelligence_hub_v2.models.creator import Creator
from intelligence_hub_v2.models.event import CreatorUpdatedPayload, Event, EventType

if TYPE_CHECKING:
    from intelligence_hub_v2.api.deps import AppState

router = APIRouter(tags=["creators"])


class AddCreatorRequest(BaseModel):
    url: str
    platform: str | None = None
    tracking: bool = True


class TrackingRequest(BaseModel):
    tracking: bool


class TaskAccepted(BaseModel):
    task_id: str


@router.get("/creators", response_model=list[Creator])
async def list_creators(
    platform: str | None = None, state: AppState = Depends(get_state)
) -> list[Creator]:
    return await state.storage.creators.list_all(platform=platform)


@router.get("/creators/{creator_id}", response_model=Creator)
async def get_creator(creator_id: int, state: AppState = Depends(get_state)) -> Creator:
    return await state.storage.creators.get_or_raise(creator_id)


@router.post("/creators", response_model=TaskAccepted, status_code=status.HTTP_202_ACCEPTED)
async def add_creator(
    body: AddCreatorRequest, state: AppState = Depends(get_state)
) -> TaskAccepted:
    """收录博主 = 起一个 `add_creator` 任务（要跟 302、要拉资料，不是纯本地写）。"""
    task_id = state.scheduler.submit("add_creator", body.model_dump())
    return TaskAccepted(task_id=task_id)


@router.patch("/creators/{creator_id}/tracking", response_model=Creator)
async def set_tracking(
    creator_id: int, body: TrackingRequest, state: AppState = Depends(get_state)
) -> Creator:
    """改跟踪开关。**只认 `set_tracking` 一个入口**（V1 §7.24：值必须是真 bool）。

    `TrackingRequest.tracking` 是 pydantic `bool` —— JSON 里的 `"true"` 字符串会被
    pydantic 拒成 422，绝不会被塞进 `set_tracking` 落成 0。这一层是 §7.24 的第一道闸。
    """
    creator = await state.storage.creators.set_tracking(creator_id, body.tracking)
    await state.events.publish(
        Event(
            type=EventType.CREATOR_UPDATED,
            timestamp=datetime.now(UTC),
            payload=CreatorUpdatedPayload(
                creator_id=creator.id, changed_fields=["is_tracking"]
            ).model_dump(mode="json"),
        )
    )
    return creator
