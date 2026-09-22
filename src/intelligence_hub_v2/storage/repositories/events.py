"""`task_events` 表的 Repository。

这张表是 EventBus 的**持久化侧**：内存里的 asyncio.Queue 负责实时推 SSE，
这里负责"任务跑完之后还能翻出当时的日志"。两份数据同源，
所以 `EventBus` 每发一条**带 task_id 的**事件就 `append()` 一条（Task 4 接线）。

**只存任务事件**：`task_events.task_id` 是 NOT NULL（data-model.md §2.6 Locked），
所以 `platform.health_changed` / `config.changed` 这类全局事件**只广播不落库** ——
它们没有"历史回放"的价值（健康状态看当下），而让它们硬挂到某个任务上会更糟。

**保留策略**：`prune()` 按 `app.storage.event_retention_days`（默认 30 天）删，
失败/超时任务保留 3 倍（默认 90 天）。排查一次"上周那轮为什么挂了"
需要的正是这批数据，而成功任务的历史事件基本没人翻。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError

from intelligence_hub_v2.models.event import EventType, StoredEvent
from intelligence_hub_v2.storage.repositories.base import BaseRepository, affected_rows, inserted_id
from intelligence_hub_v2.storage.schema import task_events_table, task_runs_table

_T = task_events_table
_TR = task_runs_table

FAILED_STATUSES = ("failed", "timeout")
"""享受 3 倍保留期的状态。`cancelled` 不在里面 —— 那是人主动停的，不用查。"""


class EventRepository(BaseRepository):
    entity = "event"

    # ---- 写 ----

    async def append(
        self,
        task_id: str,
        event_type: EventType,
        payload: dict[str, Any] | None = None,
        *,
        timestamp: datetime | None = None,
    ) -> StoredEvent:
        """落一条任务事件。

        `task_id` 必须是已存在的任务（FK + `PRAGMA foreign_keys=ON`）——
        写不进去就是真写不进去，`StorageError` 冒出去。
        不静默丢弃：丢事件等于任务详情里凭空少一段日志，而没人知道少了。
        """
        at = timestamp or datetime.now(UTC)
        values: dict[str, Any] = {
            "task_id": task_id,
            "timestamp": at,
            "type": event_type.value,
            # ensure_ascii=False：口播稿片段、博主昵称、错误原文都是中文。
            # V1 的教训（docs/lessons.md 坑 4）是 JSON 里全是 \\uXXXX，
            # 出问题时人眼读不了。
            "payload_json": json.dumps(payload or {}, ensure_ascii=False),
        }
        try:
            async with self._scope() as session:
                result = await session.execute(_T.insert().values(**values))
                new_id = inserted_id(result)
        except IntegrityError as exc:
            raise self._translate_integrity(exc) from exc
        return StoredEvent(
            id=new_id,
            type=event_type,
            task_id=task_id,
            timestamp=at,
            payload=payload or {},
        )

    # ---- 读 ----

    async def list_for_task(
        self,
        task_id: str,
        *,
        limit: int | None = None,
        after_id: int = 0,
        event_type: EventType | None = None,
    ) -> list[StoredEvent]:
        """按时间正序取某个任务的事件（前端"任务详情 → 日志"用）。

        `after_id` 用于增量拉取：前端记住最后一条 id，刷新时只取更新的。
        比 `offset` 稳 —— 中间被 `prune()` 删掉行时 offset 会整体错位。
        """
        stmt = select(_T).where(_T.c.task_id == task_id, _T.c.id > after_id).order_by(_T.c.id.asc())
        if event_type is not None:
            stmt = stmt.where(_T.c.type == event_type.value)
        if limit is not None:
            stmt = stmt.limit(limit)
        async with self._scope() as session:
            rows = (await session.execute(stmt)).mappings().all()
        return [_to_event(row) for row in rows]

    async def list_recent(
        self,
        *,
        limit: int = 100,
        event_type: EventType | None = None,
        before_id: int | None = None,
    ) -> list[StoredEvent]:
        """全局最近事件，交出去是**正序**（最旧在前，最新在末尾）。Dashboard 的实时流用。"""
        stmt = select(_T).order_by(_T.c.id.desc()).limit(limit)
        if event_type is not None:
            stmt = stmt.where(_T.c.type == event_type.value)
        if before_id is not None:
            stmt = stmt.where(_T.c.id < before_id)
        async with self._scope() as session:
            rows = (await session.execute(stmt)).mappings().all()
        # 查询是倒序（为了 LIMIT 拿到"最近 N 条"），交出去前翻回正序 ——
        # 否则每个调用方都得自己 reverse，漏一个就显示成倒放的日志。
        return [_to_event(row) for row in reversed(rows)]

    async def count(self, *, task_id: str | None = None) -> int:
        stmt = select(func.count()).select_from(_T)
        if task_id is not None:
            stmt = stmt.where(_T.c.task_id == task_id)
        async with self._scope() as session:
            return int((await session.execute(stmt)).scalar_one())

    # ---- 清理 ----

    async def prune(self, retention_days: int, *, failed_multiplier: int = 3) -> int:
        """删掉过期事件。返回删除的行数。

        仍在 `running` 的任务**一条都不删**（哪怕它已经跑了很久）——
        删掉正在推流的任务的历史，等于让前端日志凭空断一段。
        """
        if retention_days < 1:
            msg = f"retention_days 必须 >= 1，收到 {retention_days}"
            raise ValueError(msg)

        now = datetime.now(UTC)
        normal_cutoff = now - timedelta(days=retention_days)
        failed_cutoff = now - timedelta(days=retention_days * max(1, failed_multiplier))

        # 用子查询而不是 JOIN 删除：SQLite 的多表 DELETE 要 3.35+，
        # 而 SQLAlchemy Core 的 join-delete 得靠方言特有写法 —— 不值得为省一次查询绑死方言。
        failed_task_ids = select(_TR.c.id).where(_TR.c.status.in_(FAILED_STATUSES))
        running_task_ids = select(_TR.c.id).where(_TR.c.status == "running")

        stmt = delete(_T).where(
            _T.c.task_id.not_in(running_task_ids)
            & (
                (_T.c.task_id.in_(failed_task_ids) & (_T.c.timestamp < failed_cutoff))
                | (_T.c.task_id.not_in(failed_task_ids) & (_T.c.timestamp < normal_cutoff))
            )
        )
        async with self._scope() as session:
            result = await session.execute(stmt)
        return affected_rows(result)


def _to_event(row: RowMapping) -> StoredEvent:
    """行 → `StoredEvent`。`payload_json` 坏了就抛，不静默降级成 `{}`。

    库里存着坏 JSON 是数据损坏。当成空 payload 交出去，
    前端会显示一条"什么内容都没有"的事件，而真正的原因（磁盘写坏了 / 并发写）
    永远不会被发现 —— 这正是 V1 §1.3 要防的那类"看起来在跑"。
    """
    data = dict(row)
    raw = data.pop("payload_json")
    payload = json.loads(raw) if raw else {}
    return StoredEvent.model_validate({**data, "payload": payload})
