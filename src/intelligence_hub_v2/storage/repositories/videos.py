"""`videos` 表的 Repository。

看护两条 V1 陷阱：
- **§7.4**「`upsert_video()` 整行覆盖，空标题被折叠成默认值」→ `update_fields()` 只 SET 传入的列。
- **§7.25**「删除 = 删库行 + 墓碑，少一半都不算数」→ `is_hidden` 列内化墓碑，
  `list_visible()` 自动过滤，`get()` 仍然取得到。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Unpack

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from intelligence_hub_v2.errors import ConflictError, StorageError
from intelligence_hub_v2.models.transcript import TranscriptDraft, TranscriptRecord
from intelligence_hub_v2.models.video import (
    UPDATABLE_VIDEO_FIELDS,
    Page,
    PagedResult,
    Video,
    VideoDraft,
    VideoFilters,
    VideoUpdatableFields,
)
from intelligence_hub_v2.storage.repositories.base import BaseRepository, affected_rows, inserted_id
from intelligence_hub_v2.storage.schema import transcripts_table, videos_table

_T = videos_table
_TT = transcripts_table


class VideoRepository(BaseRepository):
    entity = "video"

    # ---- 读 ----

    async def get(self, id: int) -> Video | None:
        """按主键取。**不过滤 `is_hidden`** —— 隐藏的作品详情页仍然要能打开
        （V1 §7.25：墓碑只管"列表里不再出现"，不管"取不到"）。"""
        async with self._scope() as session:
            row = (await session.execute(select(_T).where(_T.c.id == id))).mappings().one_or_none()
        return self._to_model(row, Video)

    async def get_or_raise(self, id: int) -> Video:
        async with self._scope() as session:
            row = (await session.execute(select(_T).where(_T.c.id == id))).mappings().one_or_none()
        if row is None:
            self._not_found(id)  # 一定抛 NotFoundError
        return Video.model_validate(dict(row))

    async def find_by_platform_id(self, platform: str, platform_video_id: str) -> Video | None:
        async with self._scope() as session:
            row = (
                (
                    await session.execute(
                        select(_T).where(
                            _T.c.platform == platform,
                            _T.c.platform_video_id == platform_video_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        return self._to_model(row, Video)

    async def count(self, *, filters: VideoFilters | None = None) -> int:
        stmt = select(func.count()).select_from(_T)
        if filters is not None:
            stmt = stmt.where(*_filter_clauses(filters))
        async with self._scope() as session:
            return int((await session.execute(stmt)).scalar_one())

    async def list_visible(
        self,
        *,
        filters: VideoFilters | None = None,
        page: Page | None = None,
    ) -> PagedResult[Video]:
        """分页列出作品。默认只返回未隐藏的（`VideoFilters.is_hidden` 默认 False）。

        显式传 `filters.is_hidden=None` 才能拿到"全部（含隐藏）"，
        传 `True` 只拿隐藏的（Settings 页的"已隐藏作品"列表要用）。
        """
        effective = filters if filters is not None else VideoFilters()
        paging = page if page is not None else Page()

        total = await self.count(filters=effective)

        stmt = select(_T).where(*_filter_clauses(effective))
        # published_at 可能为 NULL（平台没给发布时间）。SQLite 在 DESC 下把 NULL 排最后，
        # 再用 id DESC 做稳定 tiebreak —— 否则同一秒发布的作品每次刷新顺序都可能变。
        stmt = stmt.order_by(_T.c.published_at.desc(), _T.c.id.desc())
        stmt = stmt.limit(paging.size).offset(paging.offset)

        async with self._scope() as session:
            rows = (await session.execute(stmt)).mappings().all()

        return PagedResult[Video](
            items=[Video.model_validate(dict(row)) for row in rows],
            total=total,
            page=paging.page,
            size=paging.size,
        )

    # ---- 写 ----

    async def insert(self, draft: VideoDraft) -> Video:
        """插入一条新作品。`(platform, platform_video_id)` 撞了抛 `ConflictError`。

        **不做 upsert**：V1 §7.4 那次事故的根因就是"一个入口既能插又能改"，
        调用方以为自己只改了一个字段，实际把整行覆盖了。
        V2 里"改"只有 `update_fields()` 一个入口。
        """
        now = datetime.now(UTC)
        values = draft.model_dump()
        values["created_at"] = now
        values["updated_at"] = now
        try:
            async with self._scope() as session:
                result = await session.execute(_T.insert().values(**values))
                new_id = inserted_id(result)
        except IntegrityError as exc:
            raise self._translate_integrity(exc) from exc
        video = await self.get(new_id)
        if video is None:  # pragma: no cover - 插完就读不到只可能是并发删了
            msg = f"插入后立即读不到 video id={new_id}"
            raise StorageError(msg)
        return video

    async def insert_or_get(self, draft: VideoDraft) -> tuple[Video, bool]:
        """幂等写入。返回 `(video, created)`。

        采集任务每轮都会看到同一批作品，"已存在"是**正常路径**而不是错误。
        让调用方自己 try/except `ConflictError` 等于把这套样板抄八遍。
        """
        existing = await self.find_by_platform_id(draft.platform, draft.platform_video_id)
        if existing is not None:
            return existing, False
        try:
            return await self.insert(draft), True
        except ConflictError:
            # 并发写入（两个任务同时收同一条）时走到这里，再读一次即可。
            raced = await self.find_by_platform_id(draft.platform, draft.platform_video_id)
            if raced is None:
                raise
            return raced, False

    async def update_fields(self, id: int, **fields: Unpack[VideoUpdatableFields]) -> Video:
        """**字段级**更新。只 SET 传进来的列，其他列一个字节都不动。

        这是 V1 §7.4 的结构性解法：
        - 白名单来自 `VideoUpdatableFields.__annotations__`，未知字段抛 `TypeError`
          （mypy 拦静态调用点，这里拦 `**dict` 展开与来自 API 请求体的字段名）。
        - 一个字段都没传时**不发 SQL**（V1 见过"只想刷 updated_at"结果把标题抹了）。
        """
        unknown = set(fields) - UPDATABLE_VIDEO_FIELDS
        if unknown:
            msg = (
                f"update_fields() 收到未知字段: {sorted(unknown)}。"
                f"允许的字段见 VideoUpdatableFields（platform / platform_video_id / "
                f"is_hidden 不在其中，后者要走 hide() / unhide()）。"
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

    async def hide(self, id: int, reason: str) -> Video:
        """隐藏作品（V1 §7.25：情报流的"删除"）。

        **不删媒体、不删 metadata、不删口播稿** —— 文件是全库唯一的原始产物。
        三个字段一起写，少一个都不算数。
        """
        if not reason.strip():
            msg = "hide() 必须给非空的 reason（清单与事件流要能回答'为什么没了'）"
            raise ValueError(msg)
        async with self._scope() as session:
            result = await session.execute(
                update(_T)
                .where(_T.c.id == id)
                .values(
                    is_hidden=True,
                    hidden_at=datetime.now(UTC),
                    hidden_reason=reason,
                    updated_at=datetime.now(UTC),
                )
            )
            if affected_rows(result) == 0:
                self._not_found(id)
        return await self.get_or_raise(id)

    async def unhide(self, id: int) -> Video:
        async with self._scope() as session:
            result = await session.execute(
                update(_T)
                .where(_T.c.id == id)
                .values(
                    is_hidden=False,
                    hidden_at=None,
                    hidden_reason=None,
                    updated_at=datetime.now(UTC),
                )
            )
            if affected_rows(result) == 0:
                self._not_found(id)
        return await self.get_or_raise(id)

    async def delete(self, id: int) -> bool:
        """真删库行。**媒体文件不动**（那是原始产物）。

        V2.0 没有任何调用方 —— 前端"删除"走 `hide()`。留着是给迁移脚本与
        测试清理用的，所以返回 bool 而不抛 NotFoundError。
        """
        async with self._scope() as session:
            result = await session.execute(_T.delete().where(_T.c.id == id))
        return affected_rows(result) > 0

    # ---- 关联 ----

    async def attach_transcript(
        self, video_id: int, transcript: TranscriptDraft
    ) -> TranscriptRecord:
        """给作品挂口播稿。已存在则整行替换（同一条作品重跑 ASR 是正常操作）。"""
        await self.get_or_raise(video_id)
        now = datetime.now(UTC)
        values = transcript.model_dump() | {"video_id": video_id, "created_at": now}
        try:
            async with self._scope() as session:
                await session.execute(_TT.delete().where(_TT.c.video_id == video_id))
                await session.execute(_TT.insert().values(**values))
        except IntegrityError as exc:
            raise self._translate_integrity(exc) from exc
        return TranscriptRecord.model_validate(values)

    async def has_transcript(self, video_id: int) -> bool:
        async with self._scope() as session:
            row = (
                await session.execute(
                    select(func.count()).select_from(_TT).where(_TT.c.video_id == video_id)
                )
            ).scalar_one()
        return int(row) > 0


def _filter_clauses(filters: VideoFilters) -> list[Any]:
    """把 `VideoFilters` 翻成 WHERE 子句。

    `is_hidden=None` = 不过滤（全部），这是唯一能看到隐藏作品的入口。
    """
    clauses: list[Any] = []
    if filters.platform is not None:
        clauses.append(_T.c.platform == filters.platform)
    if filters.creator_id is not None:
        clauses.append(_T.c.creator_id == filters.creator_id)
    if filters.is_hidden is not None:
        clauses.append(_T.c.is_hidden == filters.is_hidden)
    if filters.since is not None:
        clauses.append(_T.c.created_at >= filters.since)
    if filters.search:
        # 搜索词是用户输入，里面的 % 和 _ 在 LIKE 里是通配符。不转义的话
        # 搜 "%" 会匹配全表（一次全表扫描 + 结果全错）。
        escaped = filters.search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        clauses.append(
            _T.c.title.like(pattern, escape="\\") | _T.c.description.like(pattern, escape="\\")
        )
    return clauses
