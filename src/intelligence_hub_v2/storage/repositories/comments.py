"""`video_comments` 表的 Repository（V2.1 T4.1）。

评论是**附属读数**：它只属于某一条作品，作品删掉它就 cascade 删掉。
所以这里**故意没有**"跨作品列评论"的查询 —— 那种查询要的是"给我看这条作品的评论"，
而 Feed 不展示评论。真要做评论分析，那是 V2.2 分析层的事，不是在这一层加一个 `list_all`。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

from intelligence_hub_v2.models.engagement import VideoComment, VideoCommentDraft
from intelligence_hub_v2.storage.repositories.base import BaseRepository
from intelligence_hub_v2.storage.schema import video_comments_table

#: 表对象起短名是这一层的既有写法（`transcripts.py` 的 `_T` / `_V` 同一形状）。
_C = video_comments_table

#: "同一条评论再来一次"要更新的那几列。**不含** `platform_comment_id` / `video_id`
#: —— 那是身份，改了等于换了一条评论。
UPDATABLE_COMMENT_FIELDS: frozenset[str] = frozenset(
    {
        "content",
        "author_name",
        "author_platform_id",
        "parent_platform_comment_id",
        "like_count",
        "reply_count",
        "published_at",
        "metadata_json",
    }
)


class VideoCommentRepository(BaseRepository):
    entity = "video_comment"

    async def list_for_video(
        self, video_id: int, *, limit: int = 200, offset: int = 0
    ) -> list[VideoComment]:
        """按平台说的时间正序。`published_at` 为 NULL 的排最后（它们没有可比的顺序）。"""
        stmt = (
            select(_C)
            .where(_C.c.video_id == video_id)
            .order_by(_C.c.published_at.asc().nulls_last(), _C.c.id.asc())
            .limit(limit)
            .offset(offset)
        )
        async with self._scope() as session:
            rows = (await session.execute(stmt)).mappings().all()
        return [VideoComment.model_validate(dict(row)) for row in rows]

    async def count_for_video(self, video_id: int) -> int:
        async with self._scope() as session:
            value = await session.scalar(
                select(func.count()).select_from(_C).where(_C.c.video_id == video_id)
            )
        return int(value or 0)

    async def upsert_many(self, video_id: int, drafts: list[VideoCommentDraft]) -> tuple[int, int]:
        """一批评论写进去。返回 `(新增, 更新)` 两个数。

        **幂等键是 `(video_id, platform, platform_comment_id)`**（DB 上的唯一索引），
        命中就 UPDATE 内容那几列 —— 评论会被编辑、点赞数会长，
        所以"已存在就跳过"是错的：那会把楼里最新的一层永远停在第一次抓到的样子。

        为什么两个数都要，而不是只返回新增：清单里"新抓到 N 条"要的是新增数
        （把更新也算进去，每一轮都会报"又抓到 50 条"），而"这一轮到底有没有干活"
        要看的是两数之和 —— 只交一个数出去，另一种问法就没有答案了。
        """
        now = datetime.now(UTC)
        inserted = 0
        updated = 0
        async with self._scope() as session:
            for draft in drafts:
                values = draft.model_dump()
                existing = (
                    (
                        await session.execute(
                            select(_C.c.id).where(
                                _C.c.video_id == video_id,
                                _C.c.platform == draft.platform,
                                _C.c.platform_comment_id == draft.platform_comment_id,
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                if existing is None:
                    await session.execute(
                        _C.insert().values(video_id=video_id, fetched_at=now, **values)
                    )
                    inserted += 1
                    continue
                await session.execute(
                    _C.update()
                    .where(_C.c.id == existing)
                    .values(
                        {key: values[key] for key in UPDATABLE_COMMENT_FIELDS} | {"fetched_at": now}
                    )
                )
                updated += 1
        return inserted, updated
