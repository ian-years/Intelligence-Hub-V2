"""把所有子路由聚成一个 `api_router`，由 `create_app` 挂在 `/api` 前缀下。"""

from __future__ import annotations

from fastapi import APIRouter

from intelligence_hub_v2.api.v1 import (
    config,
    creators,
    events,
    export,
    health,
    manifests,
    tasks,
    transcripts,
    videos,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(config.router)
api_router.include_router(creators.router)
api_router.include_router(videos.router)
api_router.include_router(export.router)
api_router.include_router(transcripts.router)
api_router.include_router(tasks.router)
api_router.include_router(events.router)
api_router.include_router(manifests.router)

__all__ = ["api_router"]
