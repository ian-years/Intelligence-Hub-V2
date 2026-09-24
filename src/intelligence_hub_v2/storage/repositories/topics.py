"""`topics` 表的 Repository（V2.2 T5.5）。

只有 `insert / update_fields / delete / list / get` 五个动作，**没有 upsert**：
选题的唯一键就是 `name`，"同名就算已存在"听起来像 upsert 的正当理由，但那意味着
一次撞名的写入会**静默改掉另一条选题的 description** —— V1 §7.4 那一族的形状。
撞名在这里如实抛 `ConflictError`（API 层给 409），由调用方决定要不要合并。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Unpack

from sqlalchemy import func, select

from intelligence_hub_v2.errors import StorageError
from intelligence_hub_v2.models.topic import (
    UPDATABLE_TOPIC_FIELDS,
    Topic,
    TopicDraft,
    TopicUpdatableFields,
)
from intelligence_hub_v2.storage.repositories.base import BaseRepository, affected_rows, inserted_id
from intelligence_hub_v2.storage.schema import topics_table

_T = topics_table


class TopicRepository(BaseRepository):
    entity = "topic"

    async def list_all(self, *, search: str | None = None, limit: int = 200) -> list[Topic]:
        """按创建时间倒序（新选题在最前）。

        `search` 走 LIKE 且**转义** `%` / `_`：与 `videos.py` 同一判据 ——
        不转义的话搜 `%` 匹配全表，用户以为"所有选题都命中了"，而那是 LIKE 的通配符。
        """
        stmt = select(_T)
        if search:
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            stmt = stmt.where(_T.c.name.like(f"%{escaped}%", escape="\\"))
        stmt = stmt.order_by(_T.c.created_at.desc(), _T.c.id.desc()).limit(limit)
        async with self._scope() as session:
            rows = (await session.execute(stmt)).mappings().all()
        return [Topic.model_validate(dict(row)) for row in rows]

    async def get(self, id: int) -> Topic | None:
        async with self._scope() as session:
            row = (await session.execute(select(_T).where(_T.c.id == id))).mappings().one_or_none()
        return self._to_model(row, Topic)

    async def get_or_raise(self, id: int) -> Topic:
        async with self._scope() as session:
            row = (await session.execute(select(_T).where(_T.c.id == id))).mappings().one_or_none()
        if row is None:
            self._not_found(id)
        return Topic.model_validate(dict(row))

    async def find_by_name(self, name: str) -> Topic | None:
        stmt = select(_T).where(_T.c.name == name.strip())
        async with self._scope() as session:
            row = (await session.execute(stmt)).mappings().one_or_none()
        return self._to_model(row, Topic)

    async def count(self) -> int:
        async with self._scope() as session:
            return int((await session.execute(select(func.count()).select_from(_T))).scalar_one())

    async def insert(self, draft: TopicDraft) -> Topic:
        now = datetime.now(UTC)
        values: dict[str, Any] = draft.model_dump() | {"created_at": now}
        async with self._scope() as session:
            result = await session.execute(_T.insert().values(**values))
            new_id = inserted_id(result)
        topic = await self.get(new_id)
        if topic is None:  # pragma: no cover - 插完就读不到只可能是并发删了
            msg = f"插入后立即读不到 topic id={new_id}"
            raise StorageError(msg)
        return topic

    async def update_fields(self, id: int, **fields: Unpack[TopicUpdatableFields]) -> Topic:
        """**字段级**更新：只 SET 传进来的列（V1 §7.4 的结构性解法）。

        一个字段都没传时不发 SQL，直接把当前行读回来 —— 与 `VideoRepository` 同一口径：
        "只想刷一下时间戳"不该有任何机会把别的列抹了。
        """
        unknown = set(fields) - UPDATABLE_TOPIC_FIELDS
        if unknown:
            msg = (
                f"update_fields() 收到未知字段: {sorted(unknown)}。"
                f"允许的字段见 TopicUpdatableFields（name 不在其中，它是身份列，见那份注释）。"
            )
            raise TypeError(msg)
        if not fields:
            return await self.get_or_raise(id)
        async with self._scope() as session:
            result = await session.execute(_T.update().where(_T.c.id == id).values(**fields))
            if affected_rows(result) == 0:
                self._not_found(id)
        return await self.get_or_raise(id)

    async def delete(self, id: int) -> bool:
        """真删。返回 False 表示没有这一条（幂等删除给 API 层的 DELETE 用）。

        这一张表今天**没有任何子表引用它**（`video_topics` 未落地，见 ADR-0021），
        所以"删了会不会留下孤儿"这个问题现在的答案是"不会，因为没有别处存着 topic_id"。
        将来加 `video_topics` 时必须连带改这里：那条 FK 要 `ON DELETE CASCADE`
        （与 `transcripts` / `video_comments` 同一口径：附属行的意义全在父行）。
        """
        async with self._scope() as session:
            result = await session.execute(_T.delete().where(_T.c.id == id))
        return affected_rows(result) > 0
