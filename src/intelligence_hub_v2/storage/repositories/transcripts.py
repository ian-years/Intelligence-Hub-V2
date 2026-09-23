"""`transcripts` 表的 Repository。

V1 §7.5 看护：`text_path` 是**统一路径**，不再按平台不对称。
V1 的三份 `postprocess_*.py` 各写各的目录，前端按平台分支去读，
改错一处就读不到口播稿 —— V2 里路径由 `FileStorage.transcript_path()` 一处决定。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

from intelligence_hub_v2.models.transcript import TranscriptDraft, TranscriptRecord
from intelligence_hub_v2.storage.repositories.base import BaseRepository, affected_rows
from intelligence_hub_v2.storage.schema import transcripts_table, videos_table

_T = transcripts_table
_V = videos_table


class TranscriptRepository(BaseRepository):
    entity = "transcript"

    async def get_for_video(self, video_id: int) -> TranscriptRecord | None:
        async with self._scope() as session:
            row = (
                (await session.execute(select(_T).where(_T.c.video_id == video_id)))
                .mappings()
                .one_or_none()
            )
        return self._to_model(row, TranscriptRecord)

    async def get_or_raise(self, video_id: int) -> TranscriptRecord:
        async with self._scope() as session:
            row = (
                (await session.execute(select(_T).where(_T.c.video_id == video_id)))
                .mappings()
                .one_or_none()
            )
        if row is None:
            self._not_found(video_id)
        return TranscriptRecord.model_validate(dict(row))

    async def list_missing(self, *, platform: str | None = None, limit: int = 100) -> list[int]:
        """有作品但没口播稿的 video_id。`postprocess` 任务的输入。"""
        stmt = (
            select(_V.c.id)
            .outerjoin(_T, _T.c.video_id == _V.c.id)
            .where(_T.c.video_id.is_(None), _V.c.is_hidden.is_(False))
        )
        if platform is not None:
            stmt = stmt.where(_V.c.platform == platform)
        stmt = stmt.order_by(_V.c.created_at.desc()).limit(limit)
        async with self._scope() as session:
            return [int(row[0]) for row in (await session.execute(stmt)).all()]

    async def attach(self, video_id: int, draft: TranscriptDraft) -> TranscriptRecord:
        """写口播稿。已存在则整行替换 —— 同一条作品重跑 ASR 是正常操作，不是错误。"""
        values = draft.model_dump() | {
            "video_id": video_id,
            "created_at": datetime.now(UTC),
        }
        async with self._scope() as session:
            await session.execute(_T.delete().where(_T.c.video_id == video_id))
            await session.execute(_T.insert().values(**values))
        return TranscriptRecord.model_validate(values)

    async def delete(self, video_id: int) -> bool:
        """删口播稿记录。**磁盘上的 txt 不动**（原始产物）。"""
        async with self._scope() as session:
            result = await session.execute(_T.delete().where(_T.c.video_id == video_id))
        return affected_rows(result) > 0

    async def count(self, *, platform: str | None = None) -> int:
        stmt = select(func.count()).select_from(_T)
        if platform is not None:
            stmt = stmt.select_from(_T.join(_V, _V.c.id == _T.c.video_id)).where(
                _V.c.platform == platform
            )
        async with self._scope() as session:
            return int((await session.execute(stmt)).scalar_one())
