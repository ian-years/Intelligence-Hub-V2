"""`GET /api/benchmark-analysis`：把一条作品的口播稿拆成爆款结构（T5.2）。

契约来源：`docs/plans/v2.1-migration-plan.md` T5.2。V1 的对应物是
`launcher_server.py` 的 `GET /api/benchmark_analysis?id=<record_id>&force=1`。

入参形状照 V1 的"一次请求一个作品"，但三处按 V2 定：

1. **`video_id` 而不是 `id`**：V1 的 `record_id` 是本地库主键的字符串形式，另一路又是
   平台作品 ID —— V2 只有一个整数主键（`docs/specs/data-model.md §2.3`），键名也就只有一个。
2. **没有 `force`**：V1 那个开关是"重读磁盘缓存还是重算"。V2 不落缓存
   （理由见 `core/analysis/benchmark_engine.py` 模块 docstring 第 7 条），
   每次都是现算，所以没有"强制"可言 —— 留一个永远为真的参数只会让下一个人去找缓存在哪。
3. **没有 `?format=markdown`**：V1 也没给，拆解结果只以 JSON 交出。

三条数据来源规矩：

- 取作品走 `videos.get()` 语义（`get_or_raise`），**隐藏的作品照样能拆** —— 与
  `/api/videos/{id}` 详情一致（V1 §7.25：墓碑只管列表，不管取不到）。
- 没有口播稿 → 404。有稿但稿子是空的 → 200 + 引擎的"空稿兜底"结构。这两件事必须分得开，
  否则前端无法区分"还没转写"与"转写了但整条没说话"。
- 稿子正文在磁盘上，读不出来（库里有行、文件没了）→ **500 如实报**，不许当空稿子算，
  那会把"数据损坏"渲染成"这条视频没说话"。与 `api/v1/transcripts.py` 同一条判据。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query, status

from intelligence_hub_v2.api.deps import get_state
from intelligence_hub_v2.core.analysis.benchmark_engine import (
    BenchmarkAnalysis,
    analyze_benchmark,
)

if TYPE_CHECKING:
    from intelligence_hub_v2.api.deps import AppState
    from intelligence_hub_v2.models.video import Video

router = APIRouter(tags=["analysis"])

__all__ = ["router"]


@router.get("/benchmark-analysis", response_model=BenchmarkAnalysis)
async def benchmark_analysis(
    state: AppState = Depends(get_state),
    video_id: int = Query(..., ge=1, description="`videos.id`（整数主键，不是平台作品 ID）"),
) -> BenchmarkAnalysis:
    """拆解一条作品。纯本地计算：不联网、不调模型、不写库。"""
    video = await state.storage.videos.get_or_raise(video_id)
    text = await _transcript_text(state, video_id)
    creator_name = await _creator_name(state, video)
    return analyze_benchmark(
        title=video.title,
        transcript_text=text,
        creator=creator_name,
        platform=video.platform,
        duration_seconds=video.duration_seconds,
    )


async def _transcript_text(state: AppState, video_id: int) -> str:
    """取这条作品的口播稿正文。判据与 `/api/videos/{id}/transcript` 逐条相同。"""
    record = await state.storage.transcripts.get_for_video(video_id)
    if record is None:
        msg = f"video {video_id} 还没有口播稿，先跑转写"
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=msg)
    try:
        return state.files.abs(record.text_path).read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"口播稿文件读不出来：{record.text_path}（{exc}）"
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=msg) from exc


async def _creator_name(state: AppState, video: Video) -> str:
    """博主名。`creator_id` 可空（V1 的孤儿作品），拿不到就把空串交给引擎兜底。"""
    if video.creator_id is None:
        return ""
    creator = await state.storage.creators.get(video.creator_id)
    return creator.name if creator is not None else ""
