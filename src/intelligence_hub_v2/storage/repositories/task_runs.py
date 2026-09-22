"""`task_runs` 表的 Repository。

V1 §2 契约二看护：终态必须有 `ended_at`。DB 层有
`CHECK (status = 'running' OR ended_at IS NOT NULL)`，
所以"停在 running 却没人管"这种行**写不进去**（`finish()` 一定会带 ended_at）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from intelligence_hub_v2.models.task import TaskRunRecord
from intelligence_hub_v2.storage.repositories.base import BaseRepository, affected_rows
from intelligence_hub_v2.storage.schema import task_runs_table

_T = task_runs_table

TerminalStatus = Literal["success", "partial", "failed", "timeout", "cancelled"]


class TaskRunRepository(BaseRepository):
    entity = "task_run"

    # ---- 读 ----

    async def get(self, task_id: str) -> TaskRunRecord | None:
        async with self._scope() as session:
            row = (
                (await session.execute(select(_T).where(_T.c.id == task_id)))
                .mappings()
                .one_or_none()
            )
        return self._to_model(row, TaskRunRecord)

    async def get_or_raise(self, task_id: str) -> TaskRunRecord:
        async with self._scope() as session:
            row = (
                (await session.execute(select(_T).where(_T.c.id == task_id)))
                .mappings()
                .one_or_none()
            )
        if row is None:
            self._not_found(task_id)
        return TaskRunRecord.model_validate(dict(row))

    async def list_recent(
        self, *, limit: int = 50, status: str | None = None
    ) -> list[TaskRunRecord]:
        stmt = select(_T).order_by(_T.c.started_at.desc()).limit(limit)
        if status is not None:
            stmt = stmt.where(_T.c.status == status)
        async with self._scope() as session:
            rows = (await session.execute(stmt)).mappings().all()
        return [TaskRunRecord.model_validate(dict(row)) for row in rows]

    async def list_running(self) -> list[TaskRunRecord]:
        """仍在跑的任务。启动时用它做"孤儿 running 行"清理。

        进程崩了之后，DB 里会留下永远停在 `running` 的行 ——
        前端会显示一个转不出来的进度条。启动时把它们标成 `failed`
        （错误原文写"进程重启，任务中断"）比让它们假装还活着诚实。
        """
        return await self.list_recent(limit=500, status="running")

    async def count(self, *, status: str | None = None) -> int:
        stmt = select(func.count()).select_from(_T)
        if status is not None:
            stmt = stmt.where(_T.c.status == status)
        async with self._scope() as session:
            return int((await session.execute(stmt)).scalar_one())

    # ---- 写 ----

    async def start(
        self,
        *,
        task_id: str,
        task_name: str,
        kind: str,
        params: dict[str, Any] | None = None,
        config_snapshot: dict[str, Any] | None = None,
        started_at: datetime | None = None,
    ) -> TaskRunRecord:
        """开一条 `running` 记录。

        `task_id` 由调用方给（TaskRunner 生成 UUID），不在这里生成 ——
        因为 `ManifestBuilder` 与 SSE 事件都要用同一个 id，
        谁先谁后不该由存储层决定。

        撞了主键抛 `ConflictError`（与其他 Repository 的 `insert()` 一致）：
        两个任务共用一个 id 会让"任务详情"页把两轮的日志混在一起显示，
        而且没人看得出来 —— 静默覆盖上一轮的档案更糟。
        """
        values = {
            "id": task_id,
            "task_name": task_name,
            "kind": kind,
            "status": "running",
            "params_json": json.dumps(params or {}, ensure_ascii=False),
            "config_snapshot_json": json.dumps(config_snapshot or {}, ensure_ascii=False),
            "started_at": started_at or datetime.now(UTC),
            "progress": 0.0,
        }
        try:
            async with self._scope() as session:
                await session.execute(_T.insert().values(**values))
        except IntegrityError as exc:
            raise self._translate_integrity(exc) from exc
        return await self.get_or_raise(task_id)

    async def set_progress(self, task_id: str, progress: float) -> None:
        """更新进度（0.0~1.0）。

        超范围**夹紧**而不是抛异常：进度是给人看的，
        因为一个浮点误差让整条任务失败是本末倒置。DB 层还有 CHECK 兜底。

        进度**文案**不落这张表 —— 它只在跑的那一刻有意义，走 EventBus
        推给 SSE 就够了；塞进 `summary_json` 会在终态被覆盖，
        而且会让"summary 是计数"这个语义变得不可靠。
        """
        clamped = max(0.0, min(1.0, float(progress)))
        async with self._scope() as session:
            await session.execute(update(_T).where(_T.c.id == task_id).values(progress=clamped))

    async def finish(
        self,
        task_id: str,
        *,
        status: TerminalStatus,
        summary: dict[str, int | str] | None = None,
        manifest_path: str | None = None,
        error_text: str | None = None,
        ended_at: datetime | None = None,
    ) -> TaskRunRecord:
        """写终态。`ended_at` 必填（不给就用现在）。

        `error_text` 是**原文**：V1 §1.3 / §7.22 的教训是"用户只看到退出码 1
        和一屏 traceback，清单里没有原因"。所以 failed / timeout 时这个字段
        必须有值 —— 由 TaskRunner 保证（它从异常或 ManifestBuilder 拿）。

        成功时把 `progress` 收成 1.0；失败/取消时**保留最后的进度**，
        因为"跑到 37% 挂了"比"跑到 0% 挂了"有用得多。
        """
        values: dict[str, Any] = {
            "status": status,
            "ended_at": ended_at or datetime.now(UTC),
            "summary_json": json.dumps(summary, ensure_ascii=False)
            if summary is not None
            else None,
            "manifest_path": manifest_path,
            "error_text": error_text,
        }
        if status in {"success", "partial"}:
            values["progress"] = 1.0
        async with self._scope() as session:
            result = await session.execute(update(_T).where(_T.c.id == task_id).values(**values))
            if affected_rows(result) == 0:
                self._not_found(task_id)
        return await self.get_or_raise(task_id)

    async def mark_orphans_failed(self, *, reason: str) -> int:
        """把所有 `running` 行标成 `failed`。进程启动时调一次。

        返回受影响的行数（用于日志）。
        """
        async with self._scope() as session:
            result = await session.execute(
                update(_T)
                .where(_T.c.status == "running")
                .values(
                    status="failed",
                    ended_at=datetime.now(UTC),
                    error_text=reason,
                )
            )
        return affected_rows(result)
