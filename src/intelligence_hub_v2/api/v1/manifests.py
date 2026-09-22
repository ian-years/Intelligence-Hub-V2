"""`/api/manifests*`：任务清单（终态审计）。列表回摘要，详情回全文。

清单是双写的：`data/manifests/*.json`（权威源）+ `manifests` 表（索引 + 冗余副本）。
列表页只要"哪次任务、什么状态、几个成功几个失败"，所以从 `content_json` 里挑几个字段就交出去，
不把整份 failures 拖进列表（详情端点才给全文）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from intelligence_hub_v2.api.deps import get_state
from intelligence_hub_v2.models.manifest import ManifestRecord

if TYPE_CHECKING:
    from intelligence_hub_v2.api.deps import AppState

router = APIRouter(tags=["manifests"])


class ManifestSummary(BaseModel):
    id: int
    task_id: str
    file_path: str
    written_at: Any  # datetime，OpenAPI 里是 date-time；用 Any 省一次 model 重复
    status: str
    summary: dict[str, int | str]


@router.get("/manifests", response_model=list[ManifestSummary])
async def list_manifests(
    state: AppState = Depends(get_state), limit: int = Query(default=50, ge=1, le=500)
) -> list[ManifestSummary]:
    rows = await state.storage.manifests.list_recent(limit=limit)
    out: list[ManifestSummary] = []
    for row in rows:
        try:
            content = row.content()
        except ValueError:
            # 库里存着坏 JSON = 数据损坏，如实跳过并在详情里会再报；不在列表里 500 掉整页
            continue
        out.append(
            ManifestSummary(
                id=row.id,
                task_id=row.task_id,
                file_path=row.file_path,
                written_at=row.written_at,
                status=content.status,
                summary=dict(content.summary),
            )
        )
    return out


@router.get("/manifests/{manifest_id}", response_model=ManifestRecord)
async def get_manifest(manifest_id: int, state: AppState = Depends(get_state)) -> ManifestRecord:
    return await state.storage.manifests.get_or_raise(manifest_id)
