"""`/api/topics*`：选题金矿的读 + 新建 + 改描述 + 删除（V2.2 T5.5）。

形状照 `api/v1/creators.py`（同一族 CRUD 路由），两点不同是这张表自己的：

- **`POST /topics` 是纯本地写入，回 201 + 那条选题本身**（不像收录博主那样回 202 任务）。
  新建一条选题不需要跟链接、不需要拉资料，起一个任务反而是把一件同步的事做成了异步的 ——
  前端就要为它写一套"排队中"的文案，而那句话在这里是假绿（AGENTS §1.3 的反面案例）。
- **撞名回 409**，由 `ConflictError` 的映射来（`main.py:_register_exception_handlers`）。
  不在这里 try/except 成"那就更新那条"：合并两条选题是一个**决定**，
  要有地方写"被合并的那条的描述去哪了"，今天没有那个地方。

不发事件：`EventType` 的取值是 `docs/specs/event-schema.md`（Locked）里的枚举，
加一条要单独走 ADR，而 T5.5 的验收里没有"选题变更要进事件流"这一格。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from intelligence_hub_v2.api.deps import get_state
from intelligence_hub_v2.models.topic import Topic, TopicDraft

if TYPE_CHECKING:
    from intelligence_hub_v2.api.deps import AppState

router = APIRouter(tags=["topics"])


class TopicCreate(BaseModel):
    """新建选题的入参。`name` 的空判在 `TopicDraft` 里，不在这里重复一遍。"""

    name: str = Field(min_length=1, description="选题名，全库唯一")
    description: str | None = None


class TopicUpdate(BaseModel):
    """改选题。只有 `description` 可改，理由见 `TopicUpdatableFields`。

    这里**不区分**"没传"与"传了 null"：只有一个可改字段，`update_fields()` 又一定被调用，
    所以清空描述就是 PATCH 一个 null。草稿那张表要区分（四列里挑一列改），
    两处的形状不同是字段数不同，不是各写各的。
    """

    description: str | None = None


@router.get("/topics", response_model=list[Topic])
async def list_topics(
    state: AppState = Depends(get_state),
    search: str | None = Query(default=None, description="按名字模糊匹配"),
    limit: int = Query(default=200, ge=1, le=500),
) -> list[Topic]:
    return await state.storage.topics.list_all(search=search, limit=limit)


@router.get("/topics/{topic_id}", response_model=Topic)
async def get_topic(topic_id: int, state: AppState = Depends(get_state)) -> Topic:
    return await state.storage.topics.get_or_raise(topic_id)


@router.post("/topics", response_model=Topic, status_code=status.HTTP_201_CREATED)
async def create_topic(body: TopicCreate, state: AppState = Depends(get_state)) -> Topic:
    """建一条选题。

    空白的 `name` 在 `TopicDraft` 里被拒（那里才是规则的唯一出处：库里那一半是
    UNIQUE 而不是 CHECK，空串合法，所以规则必须有人守）。这里只是把那句人话
    翻成 422，**不在 `TopicCreate` 上再抄一遍校验规则** —— 两处清单迟早漂开。
    """
    try:
        draft = TopicDraft(name=body.name, description=body.description)
    except ValueError as exc:  # pydantic 的 ValidationError 也是 ValueError 的子类
        raise HTTPException(422, detail=str(exc)) from exc  # 与 config.py 同一写法：
        # `status.HTTP_422_UNPROCESSABLE_ENTITY` 在这套依赖里已经是 deprecated，
        # 而 pytest 的 `filterwarnings = ["error"]` 会把它变成一条红（2026-09-24 实测）。
    return await state.storage.topics.insert(draft)


@router.patch("/topics/{topic_id}", response_model=Topic)
async def update_topic(
    topic_id: int, body: TopicUpdate, state: AppState = Depends(get_state)
) -> Topic:
    return await state.storage.topics.update_fields(topic_id, description=body.description)


@router.delete("/topics/{topic_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_topic(topic_id: int, state: AppState = Depends(get_state)) -> None:
    """真删。今天没有任何表引用 `topics.id`（`video_topics` 未落地，ADR-0021），
    所以删掉就是一行少一行 —— 那条前提变了要回来看 `TopicRepository.delete()`。"""
    if not await state.storage.topics.delete(topic_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"没有 id={topic_id} 的选题"
        )
