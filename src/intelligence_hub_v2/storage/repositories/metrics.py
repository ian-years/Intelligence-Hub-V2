"""`video_metric_snapshots` 表的 Repository（V2.1 T4.2）。

一个检查点**只有一条**读数（唯一索引 `(video_id, checkpoint)`）。这不是省事，是判据：
"24h 那条到底算哪个数"如果有两个答案，增长率曲线就会随重跑次数变长，
而"发布 24 小时破千"这种结论就没有意义了。所以重抓同一窗口 = 覆盖，并留
`previous_collected_at` 在 `metadata_json` 里（覆盖要能说清覆盖了谁）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import select

from intelligence_hub_v2.models.engagement import (
    METRIC_CHECKPOINT_VALUES,
    MetricCheckpoint,
    MetricSnapshot,
    MetricSnapshotDraft,
)
from intelligence_hub_v2.storage.repositories.base import BaseRepository
from intelligence_hub_v2.storage.schema import video_metric_snapshots_table, videos_table

#: 表对象起短名是这一层的既有写法（`transcripts.py` 的 `_T` / `_V` 同一形状）：
#: 下面的语句里表名出现得比列名还多，全名会让每一条 select 都超行宽。
_S = video_metric_snapshots_table
_V = videos_table

#: 一条作品还差哪些检查点。`tasks/enrich_metrics.py` 用它挑活。
DEFAULT_CHECKPOINTS: tuple[MetricCheckpoint, ...] = METRIC_CHECKPOINT_VALUES


class MetricSnapshotRepository(BaseRepository):
    entity = "metric_snapshot"

    async def list_for_video(self, video_id: int) -> list[MetricSnapshot]:
        stmt = (
            select(_S)
            .where(_S.c.video_id == video_id)
            .order_by(_S.c.collected_at.asc(), _S.c.id.asc())
        )
        async with self._scope() as session:
            rows = (await session.execute(stmt)).mappings().all()
        return [MetricSnapshot.model_validate(dict(row)) for row in rows]

    async def get(self, video_id: int, checkpoint: MetricCheckpoint) -> MetricSnapshot | None:
        stmt = select(_S).where(_S.c.video_id == video_id, _S.c.checkpoint == checkpoint)
        async with self._scope() as session:
            row = (await session.execute(stmt)).mappings().one_or_none()
        return MetricSnapshot.model_validate(dict(row)) if row is not None else None

    async def video_ids_missing(
        self, *, platform: str | None = None, limit: int = 100
    ) -> list[int]:
        """**一条快照都没有**的作品 id。enrich 任务的输入。

        判据是"没有任何一条"，不是"没有某一条固定的窗口"：一条三年前的作品
        永远等不到它的 `24h`，按"缺 24h"挑活会让它每一轮都被重挑一遍、
        每一轮都什么都没抓到 —— 那是"看起来在跑"的又一种形状。
        真要按窗口补抓，那是 `tasks/enrich_metrics.py` 在拿到这批作品之后做的事。
        """
        query = (
            select(_V.c.id)
            .outerjoin(_S, _S.c.video_id == _V.c.id)
            .where(_S.c.id.is_(None), _V.c.is_hidden.is_(False))
            .order_by(_V.c.published_at.desc().nulls_last(), _V.c.id.asc())
            .limit(limit)
        )
        if platform is not None:
            query = query.where(_V.c.platform == platform)
        async with self._scope() as session:
            return [int(row[0]) for row in (await session.execute(query)).all()]

    async def put(self, video_id: int, draft: MetricSnapshotDraft) -> MetricSnapshot:
        """写一条快照；同窗口已有就覆盖。空草稿（四项全 NULL）**拒收**。

        检查点从草稿里取，**不是**另一个入参：签名上写两个地方表达同一件事
        （`checkpoint` 参数 + `draft.checkpoint`），迟早会出现调用方传了两个不一样的，
        而"以哪个为准"这件事没有任何一处代码会替你检查。


        为什么拒：一条全空的快照会冒充"这个窗口已经抓过了"，于是 `video_ids_missing`
        永远不再看这条作品 —— 一次失败的抓取换来一个永久的盲点。
        调用方要的是"这一轮没抓到"这件事进清单，而不是进库。
        """
        checkpoint = draft.checkpoint
        if draft.is_empty():
            msg = (
                f"四项读数全空，不落库（video_id={video_id}, checkpoint={checkpoint}）："
                f"一条空快照会被当成『这个窗口抓过了』，让这条作品从此不再被补抓"
            )
            raise ValueError(msg)
        now = datetime.now(UTC)
        values = draft.model_dump(exclude={"checkpoint", "metadata_json"})
        async with self._scope() as session:
            existing = (
                (
                    await session.execute(
                        select(_S.c.id, _S.c.collected_at, _S.c.metadata_json).where(
                            _S.c.video_id == video_id, _S.c.checkpoint == checkpoint
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            metadata = _note_previous(
                draft.metadata_json, existing["metadata_json"] if existing else None
            )
            if existing is None:
                await session.execute(
                    _S.insert().values(
                        video_id=video_id,
                        checkpoint=checkpoint,
                        collected_at=now,
                        metadata_json=metadata,
                        **values,
                    )
                )
            else:
                await session.execute(
                    _S.update()
                    .where(_S.c.id == existing["id"])
                    .values(**values, metadata_json=metadata, collected_at=now)
                )
            row = (
                (
                    await session.execute(
                        select(_S).where(_S.c.video_id == video_id, _S.c.checkpoint == checkpoint)
                    )
                )
                .mappings()
                .one()
            )
        return MetricSnapshot.model_validate(dict(row))


def _note_previous(new: str, old: str | None) -> str:
    """把"覆盖了谁"记进 `metadata_json`。

    不新加一列 `previous_collected_at` 的理由：这个字段没有任何查询会用它，
    它唯一的用途是人来看见"这条被重写过"。为一个只读用途加一列 = 多一处要迁移的真相。
    """
    try:
        payload = json.loads(new)
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if old is not None:
        payload["overwrote"] = {"previous_metadata_json": old}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
