"""`/api/drafts*`：草稿的读 + 新建 + 部分更新 + 删除（V2.2 T5.5）。

`PATCH` 用的是 `DraftUpdatableFields` 那一族字段级更新，**请求体里没带的字段不动**
（V1 §7.4）。这一条在草稿上特别要紧：编辑器一次只改正文，若这里把整行拼回去交给
`update()`，标题与 `source_video_id` 就按调用方那份可能过期的快照被覆盖 ——
而症状是"稿子的来源不见了"，最难和"我刚才改了什么"联系起来。

`source_video_id` 不存在时这里给 404（而不是让外键炸成 500）。**这一道预检不是为了
让写入正确** —— 正确性由库里的 FK 兜着（`fk_drafts_source_video_id_videos`，
`ON DELETE SET NULL`）；预检只是为了把"你引用的作品没有"说成一句人话。
两者之间真有并发的话，输的还是 FK，所以不会因此多出一条不一致的路。

不发事件：理由与 `/api/topics` 相同（`event-schema.md` 是 Locked 枚举，加取值要单独走 ADR）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator

from intelligence_hub_v2.api.deps import get_state
from intelligence_hub_v2.models.draft import (
    DRAFT_STATUS_VALUES,
    DraftInput,
    DraftRecord,
    DraftStatus,
    DraftUpdatableFields,
    assert_non_blank_text,
)

if TYPE_CHECKING:
    from intelligence_hub_v2.api.deps import AppState

router = APIRouter(tags=["drafts"])

_STATUS_DESCRIPTION = " / ".join(DRAFT_STATUS_VALUES)


class DraftCreate(BaseModel):
    title: str = Field(min_length=1, description="草稿标题")
    content: str = Field(min_length=1, description="整篇正文，入库而不是落文件")
    source_video_id: int | None = Field(
        default=None, description="来源作品；删作品时这一列变 NULL，草稿不跟着删"
    )
    status: DraftStatus = Field(default="draft", description=_STATUS_DESCRIPTION)


class DraftUpdate(BaseModel):
    """部分更新。**没带的字段不动**，所以四个键都默认 `None` = "我没说要改这个"。

    `content=None` 与 `content=""` 是两回事：前者是"不改正文"，后者过不了这里的
    `min_length=1`（422），再往下也过不了 `DraftInput` 的空白判 —— 想清空正文
    今天没有入口，那是"删掉这篇稿子"而不是"把它写成空的"。
    """

    title: str | None = Field(default=None, min_length=1)
    content: str | None = Field(default=None, min_length=1)
    source_video_id: int | None = Field(
        default=None, description="换一篇来源作品；不传＝不动这一列（解除关联要删了重建）"
    )
    status: DraftStatus | None = Field(default=None, description=_STATUS_DESCRIPTION)

    @model_validator(mode="after")
    def _reject_blank_text(self) -> DraftUpdate:
        """**只判确实传了的那两栏**，规则本身在 `models/draft.assert_non_blank_text`。

        这里必须判：`min_length=1` 放得开 `"   "`，而部分更新不会经过 `DraftInput`
        （它只出现在 POST 上）。不判的话 PATCH 一个空白正文就能得到一条 200 +
        列表上一片空白，而那条草稿的 `updated_at` 还显示"刚刚改过"。
        """
        for field in ("title", "content"):
            value = getattr(self, field)
            if value is not None:
                assert_non_blank_text(field, value)
        return self


@router.get("/drafts", response_model=list[DraftRecord])
async def list_drafts(
    state: AppState = Depends(get_state),
    status_filter: DraftStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[DraftRecord]:
    """列表。`?status=` 不传就是全部；传了非法值由 FastAPI 的 `Literal` 校验挡成 422，
    不是一条悄悄返回空列表的查询。"""
    return await state.storage.drafts.list_all(status=status_filter, limit=limit, offset=offset)


@router.get("/drafts/{draft_id}", response_model=DraftRecord)
async def get_draft(draft_id: int, state: AppState = Depends(get_state)) -> DraftRecord:
    return await state.storage.drafts.get_or_raise(draft_id)


@router.post("/drafts", response_model=DraftRecord, status_code=status.HTTP_201_CREATED)
async def create_draft(body: DraftCreate, state: AppState = Depends(get_state)) -> DraftRecord:
    await _require_video(body.source_video_id, state)
    return await state.storage.drafts.insert(await _input(body.model_dump()))


async def _input(payload: dict[str, object]) -> DraftInput:
    """请求体 → `DraftInput`，把"只有空白字符"那句人话翻成 422。

    为什么不在 `DraftCreate` 上再写一遍 `strip()` 判据：规则有**一处**出处就够了
    （`DraftInput._reject_blank_text`），这里只做状态码翻译。两处各写一遍的失败方式
    是"改了库那一半忘了改路由那一半"，而症状是同一个输入有时 422 有时 500。
    """
    try:
        return DraftInput.model_validate(payload)
    except ValueError as exc:  # pydantic 的 ValidationError 是 ValueError 的子类
        raise HTTPException(
            422, detail=str(exc)
        ) from exc  # 字面量 422：`HTTP_422_UNPROCESSABLE_ENTITY`
        # 在这套依赖里已经 deprecated，而 pytest 的 filterwarnings=error 会把警告变成一条红。


@router.patch("/drafts/{draft_id}", response_model=DraftRecord)
async def update_draft(
    draft_id: int, body: DraftUpdate, state: AppState = Depends(get_state)
) -> DraftRecord:
    await _require_video(body.source_video_id, state)
    changes = _changes(body)
    return await state.storage.drafts.update_fields(draft_id, **changes)


@router.delete("/drafts/{draft_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_draft(draft_id: int, state: AppState = Depends(get_state)) -> None:
    """真删草稿。这是**唯一**会销毁人写出来的文字的路径，所以只挂在显式 DELETE 上。"""
    if not await state.storage.drafts.delete(draft_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"没有 id={draft_id} 的草稿"
        )


def _changes(body: DraftUpdate) -> DraftUpdatableFields:
    """请求体 → 只含"确实传了"的那几个键的 `DraftUpdatableFields`。

    单独一个函数是为了让 mypy 看得见 TypedDict 的键：`{k: v for ...}` 那种
    字典推导式推出来的是 `dict[str, object]`，正好把这一格要守的东西（"只有白名单里
    的四个键、类型对得上"）抹平成一个 `# type: ignore`。
    空白名单（一个字段都没改）交给 `update_fields()` 处理：它不发 SQL，只把当前行读回来。
    """
    changes: DraftUpdatableFields = {}
    if body.title is not None:
        changes["title"] = body.title
    if body.content is not None:
        changes["content"] = body.content
    if body.source_video_id is not None:
        changes["source_video_id"] = body.source_video_id
    if body.status is not None:
        changes["status"] = body.status
    return changes


async def _require_video(video_id: int | None, state: AppState) -> None:
    if video_id is None:
        return
    if await state.storage.videos.get(video_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"来源作品 id={video_id} 不存在，草稿不建",
        )
