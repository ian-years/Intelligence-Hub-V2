"""把所有子路由聚成一个 `api_router`，由 `create_app` 挂在 `/api` 前缀下。"""

from __future__ import annotations

from fastapi import APIRouter

from intelligence_hub_v2.api.v1 import (
    benchmark_analysis,
    config,
    creators,
    drafts,
    drafts_generation,
    events,
    export,
    health,
    manifests,
    media,
    schedule,
    shots,
    tasks,
    topics,
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
# 媒体出图：T6.5 的播放器要的正是这一个（`<video>` 需要一个能 Range 的 URL，
# 而 `media_path` 那一列相对 `data/`，前端猜不出绝对位置 —— 猜了就等于把目录结构写进前端）。
api_router.include_router(media.router)
# 分镜截图包（工坊页那一格的数据源）：紧贴媒体那一簇，因为它吃的就是 `media_path` 指着的成片。
api_router.include_router(shots.router)
# 选题与草稿：V2.2 T5.5（ADR-0021）。放在内容域那一簇的后面、任务域之前，
# 与 `storage/schema.py:ALL_TABLES` 里"平台 → 内容 → 选题/草稿 → 任务"的顺序同一个形状。
api_router.include_router(topics.router)
api_router.include_router(drafts.router)
api_router.include_router(tasks.router)
api_router.include_router(events.router)
api_router.include_router(schedule.router)
api_router.include_router(manifests.router)
api_router.include_router(benchmark_analysis.router)
api_router.include_router(drafts_generation.router)

__all__ = ["api_router"]
