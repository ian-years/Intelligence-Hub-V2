"""`drafts` 表的 Repository（V2.2 T5.5）。

两条不是随手定的语义，都写在这里而不是 API 层：

1. **`source_video_id` 指向不存在的作品时不给你 404 的机会** —— 外键当场拒
   （`PRAGMA foreign_keys=ON` 是 `SqliteStorage` 开着的），翻译成 `StorageError`。
   靠调用方"先查一下再写"是两个入口之间的竞态，也是 V1 §7.12 那种"主库和校验库不是一份"。
2. **源作品被删时草稿留着**（列上的 `ON DELETE SET NULL`）。这里的 `delete()` 才是真删，
   而且只有 API 层那一个入口 —— 没有任何批处理会顺手清草稿。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Unpack

from sqlalchemy import func, select

from intelligence_hub_v2.errors import StorageError
from intelligence_hub_v2.models.draft import (
    UPDATABLE_DRAFT_FIELDS,
    DraftInput,
    DraftRecord,
    DraftStatus,
    DraftUpdatableFields,
)
from intelligence_hub_v2.storage.repositories.base import BaseRepository, affected_rows, inserted_id
from intelligence_hub_v2.storage.schema import drafts_table

_T = drafts_table


class DraftRepository(BaseRepository):
    entity = "draft"

    async def list_all(
        self, *, status: DraftStatus | None = None, limit: int = 200, offset: int = 0
    ) -> list[DraftRecord]:
        """最近在改的在最前（`updated_at DESC`）。没有这一条的话列表就是一堆旧稿压着新稿。

        `status=None` = 全部。默认值刻意是 `None` 而不是 `"draft"`：
        界面的"草稿箱"会显式传 `draft`，而库列表要能一句话问出"一共有几篇在写"。
        把默认值写成其中一个筛选项，等于让"没筛"和"筛了 draft"长得一样。
        """
        stmt = select(_T)
        if status is not None:
            stmt = stmt.where(_T.c.status == status)
        stmt = stmt.order_by(_T.c.updated_at.desc(), _T.c.id.desc()).limit(limit).offset(offset)
        async with self._scope() as session:
            rows = (await session.execute(stmt)).mappings().all()
        return [DraftRecord.model_validate(dict(row)) for row in rows]

    async def count(self, *, status: DraftStatus | None = None) -> int:
        stmt = select(func.count()).select_from(_T)
        if status is not None:
            stmt = stmt.where(_T.c.status == status)
        async with self._scope() as session:
            return int((await session.execute(stmt)).scalar_one())

    async def get(self, id: int) -> DraftRecord | None:
        async with self._scope() as session:
            row = (await session.execute(select(_T).where(_T.c.id == id))).mappings().one_or_none()
        return self._to_model(row, DraftRecord)

    async def get_or_raise(self, id: int) -> DraftRecord:
        async with self._scope() as session:
            row = (await session.execute(select(_T).where(_T.c.id == id))).mappings().one_or_none()
        if row is None:
            self._not_found(id)
        return DraftRecord.model_validate(dict(row))

    async def insert(self, draft: DraftInput) -> DraftRecord:
        now = datetime.now(UTC)
        values: dict[str, Any] = draft.model_dump() | {"created_at": now, "updated_at": now}
        async with self._scope() as session:
            result = await session.execute(_T.insert().values(**values))
            new_id = inserted_id(result)
        row = await self.get(new_id)
        if row is None:  # pragma: no cover - 插完就读不到只可能是并发删了
            msg = f"插入后立即读不到 draft id={new_id}"
            raise StorageError(msg)
        return row

    async def update_fields(self, id: int, **fields: Unpack[DraftUpdatableFields]) -> DraftRecord:
        """**字段级**更新：只 SET 传进来的列，没传的一个字节都不动（V1 §7.4）。

        这一条在草稿上比在别的表上更要紧：编辑一篇稿子的调用方手里往往只有
        **它改过的那两栏**（标题或正文），整行覆盖语义会把它没碰过的另一栏
        连同 `source_video_id` 一起按自己那份（可能是空的）快照写回去。
        """
        unknown = set(fields) - UPDATABLE_DRAFT_FIELDS
        if unknown:
            msg = (
                f"update_fields() 收到未知字段: {sorted(unknown)}。"
                f"允许的字段见 DraftUpdatableFields（id / created_at 不在其中）。"
            )
            raise TypeError(msg)
        if not fields:
            return await self.get_or_raise(id)

        values: dict[str, Any] = dict(fields)
        values["updated_at"] = datetime.now(UTC)
        async with self._scope() as session:
            result = await session.execute(_T.update().where(_T.c.id == id).values(**values))
            if affected_rows(result) == 0:
                self._not_found(id)
        return await self.get_or_raise(id)

    async def delete(self, id: int) -> bool:
        async with self._scope() as session:
            result = await session.execute(_T.delete().where(_T.c.id == id))
        return affected_rows(result) > 0
