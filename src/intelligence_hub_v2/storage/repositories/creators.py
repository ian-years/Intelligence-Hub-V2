"""`creators` 表的 Repository。

看护两条 V1 陷阱：
- **§7.11**「列名 `creator_id`，外部数据叫 `creator_platform_id` / `mid`」
  → 统一叫 `platform_id`，只有这一个名字。
- **§7.24**「`is_tracking` 的值必须是真布尔，且默认值只能有一处」
  → `set_tracking()` 是唯一写入口，入口断言 `isinstance(tracking, bool)`；
  DB 层再加 `CHECK (is_tracking IN (0, 1))` 兜底。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Unpack

from sqlalchemy import Select, func, select, update

from intelligence_hub_v2.errors import ConflictError
from intelligence_hub_v2.models.creator import (
    UPDATABLE_CREATOR_FIELDS,
    Creator,
    CreatorDraft,
    CreatorUpdatableFields,
)
from intelligence_hub_v2.storage.repositories.base import BaseRepository, affected_rows, inserted_id
from intelligence_hub_v2.storage.schema import creators_table, videos_table

_T = creators_table


class CreatorRepository(BaseRepository):
    entity = "creator"

    # ---- 读 ----

    async def get(self, id: int) -> Creator | None:
        async with self._scope() as session:
            row = (await session.execute(select(_T).where(_T.c.id == id))).mappings().one_or_none()
        return self._to_model(row, Creator)

    async def get_or_raise(self, id: int) -> Creator:
        async with self._scope() as session:
            row = (await session.execute(select(_T).where(_T.c.id == id))).mappings().one_or_none()
        if row is None:
            self._not_found(id)
        return Creator.model_validate(dict(row))

    async def find(self, platform: str, platform_id: str) -> Creator | None:
        """按唯一身份 `(platform, platform_id)` 查。

        V1 §7.1 看护：抖音的 `platform_id` 必须是 `sec_uid`，不是分享短链。
        拿短链入库会造成"回写命中 1 条但其实只刷了 updated_at" —— 资料永远落不上去。
        这条查询本身不校验格式（那是 `DouyinAdapter.parse_creator_url()` 的职责），
        但它是唯一的身份入口，所以名字里不出现 url 字样。
        """
        async with self._scope() as session:
            row = (
                (
                    await session.execute(
                        select(_T).where(
                            _T.c.platform == platform,
                            _T.c.platform_id == platform_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        return self._to_model(row, Creator)

    async def list_all(self, *, platform: str | None = None) -> list[Creator]:
        stmt: Select[tuple[Any, ...]] = select(_T).order_by(_T.c.platform, _T.c.name)
        if platform is not None:
            stmt = stmt.where(_T.c.platform == platform)
        async with self._scope() as session:
            rows = (await session.execute(stmt)).mappings().all()
        return [Creator.model_validate(dict(row)) for row in rows]

    async def list_tracked(self, *, platform: str | None = None) -> list[Creator]:
        """只返回 `is_tracking=True` 的博主。日更采集与定时任务走这条。

        **「抓取爆款 Top 5」这类按位任务不走这里** —— V1 §7.22 那次事故就是
        点名抓某一位博主却用了"跟踪开关筛全库"，开关关着时拿到 0 条，
        一路甩成 traceback。按位任务用 `find()` / `get_or_raise()`。
        """
        stmt = select(_T).where(_T.c.is_tracking.is_(True)).order_by(_T.c.platform, _T.c.name)
        if platform is not None:
            stmt = stmt.where(_T.c.platform == platform)
        async with self._scope() as session:
            rows = (await session.execute(stmt)).mappings().all()
        return [Creator.model_validate(dict(row)) for row in rows]

    async def count(self, *, platform: str | None = None, tracked_only: bool = False) -> int:
        stmt = select(func.count()).select_from(_T)
        if platform is not None:
            stmt = stmt.where(_T.c.platform == platform)
        if tracked_only:
            stmt = stmt.where(_T.c.is_tracking.is_(True))
        async with self._scope() as session:
            return int((await session.execute(stmt)).scalar_one())

    # ---- 写 ----

    async def insert(self, draft: CreatorDraft) -> Creator:
        """插入。`(platform, platform_id)` 撞了抛 `ConflictError`。

        **不做 upsert**（V1 §7.4：一个入口既能插又能改，调用方以为只改了一个字段，
        实际把整行覆盖了）。要改资料走 `update_fields()`。
        """
        now = datetime.now(UTC)
        values = draft.model_dump()
        values["created_at"] = now
        values["updated_at"] = now
        async with self._scope() as session:
            result = await session.execute(_T.insert().values(**values))
            new_id = inserted_id(result)
        return await self.get_or_raise(new_id)

    async def insert_or_get(self, draft: CreatorDraft) -> tuple[Creator, bool]:
        """幂等写入，返回 `(creator, created)`。"""
        existing = await self.find(draft.platform, draft.platform_id)
        if existing is not None:
            return existing, False
        try:
            return await self.insert(draft), True
        except ConflictError:
            raced = await self.find(draft.platform, draft.platform_id)
            if raced is None:
                raise
            return raced, False

    async def update_fields(self, id: int, **fields: Unpack[CreatorUpdatableFields]) -> Creator:
        """字段级更新（V1 §7.4）。`is_tracking` 不在白名单里，走 `set_tracking()`。"""
        unknown = set(fields) - UPDATABLE_CREATOR_FIELDS
        if unknown:
            msg = (
                f"update_fields() 收到未知字段: {sorted(unknown)}。"
                f"允许的字段见 CreatorUpdatableFields（is_tracking 要走 set_tracking()）。"
            )
            raise TypeError(msg)

        if not fields:
            return await self.get_or_raise(id)

        values: dict[str, Any] = dict(fields)
        values["updated_at"] = datetime.now(UTC)
        async with self._scope() as session:
            result = await session.execute(update(_T).where(_T.c.id == id).values(**values))
            if affected_rows(result) == 0:
                self._not_found(id)
        return await self.get_or_raise(id)

    async def set_tracking(self, id: int, tracking: bool) -> Creator:
        """改「持续跟踪」开关。**这是 `is_tracking` 的唯一写入口**。

        V1 §7.24：值落成 `0` / `"false"` 会让"日更采集"（认 `is False`）和
        "按位抓取"（认 `checkbox_enabled`）读出**相反**的结果。
        所以入口就断言真布尔 —— 前端 JSON 里传 `"true"` 字符串要在这里红掉，
        而不是静默存进去等到第二天采集时才暴露。
        """
        if not isinstance(tracking, bool):
            msg = (
                f"is_tracking 必须是真 bool，收到 {type(tracking).__name__}: {tracking!r}。"
                f"（V1 §7.24：落成 0 / 'false' 会让日更采集与按位抓取读出相反的结果）"
            )
            raise TypeError(msg)
        async with self._scope() as session:
            result = await session.execute(
                update(_T)
                .where(_T.c.id == id)
                .values(is_tracking=tracking, updated_at=datetime.now(UTC))
            )
            if affected_rows(result) == 0:
                self._not_found(id)
        return await self.get_or_raise(id)

    async def delete(self, id: int) -> bool:
        """删博主条目。**不删他的视频** —— FK 是 `ON DELETE SET NULL`，
        视频变成孤儿（`creator_id IS NULL`）但仍然可查、媒体文件一个不动。

        2026-09-20 的定案（见项目记忆）：删除只动对标库条目，媒体与作品永不删。
        """
        async with self._scope() as session:
            result = await session.execute(_T.delete().where(_T.c.id == id))
        return affected_rows(result) > 0

    # ---- 关联统计 ----

    async def video_count(self, creator_id: int, *, include_hidden: bool = False) -> int:
        stmt = (
            select(func.count())
            .select_from(videos_table)
            .where(videos_table.c.creator_id == creator_id)
        )
        if not include_hidden:
            stmt = stmt.where(videos_table.c.is_hidden.is_(False))
        async with self._scope() as session:
            return int((await session.execute(stmt)).scalar_one())
