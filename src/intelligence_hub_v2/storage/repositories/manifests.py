"""`manifests` 表的 Repository。

清单**双写**（data-model.md §2.7）：
- **文件**（`data/manifests/<8位日期>-<6位时间>-<kind>.json`）是权威源，审计与离线分析读它；
- **这张表**是索引，前端"最近任务"列表不用 glob 目录 + 逐个读 JSON + 排序。

V1 只有文件，于是文件名格式一变（B站后处理那条就不写 `kind`）整个看板归一化逻辑就得跟着改
（§7.2 契约二 + §7.22）。V2 把"这条清单属于哪个任务"记成外键，不再靠文件名猜。

`content_json` 是完整清单的冗余副本。写文件失败时**不写这张表** ——
索引指向一个不存在的文件比没有索引更糟（点进去 404，而列表看着一切正常）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from intelligence_hub_v2.errors import StorageError
from intelligence_hub_v2.models.manifest import Manifest, ManifestRecord
from intelligence_hub_v2.storage.repositories.base import BaseRepository, inserted_id
from intelligence_hub_v2.storage.schema import manifests_table

_T = manifests_table

SCHEMA_VERSION = "2.0"


class ManifestRepository(BaseRepository):
    entity = "manifest"

    # ---- 写 ----

    async def record(
        self,
        task_id: str,
        manifest: Manifest,
        file_path: str | Path,
        *,
        schema_version: str = SCHEMA_VERSION,
        written_at: datetime | None = None,
    ) -> ManifestRecord:
        """把一份**已终态**的清单登记进库。

        `manifest.status` 为空的情况在模型层就不存在（`Manifest.status` 是必填的
        Literal），所以这里不用再判一次 —— 类型系统已经看护了 V1 §2 契约二的一半。
        另一半（`finalize()` 一定被调到）由 `manifest_writer` ctx manager 看护（Task 4）。

        `file_path` 存**相对 `data/`** 的路径（spec §2.7）：整个数据目录可以搬走、
        备份、换机器，库里存绝对路径的话搬一次就全废。

        `task_id` 是外键 —— 任务行不存在就红（翻成 `StorageError`）。
        V1 靠文件名猜"这条清单属于哪个任务"，猜错就是看板归一化逻辑跟着改（§7.22）。
        """
        values: dict[str, Any] = {
            "task_id": task_id,
            "schema_version": schema_version,
            "file_path": str(file_path),
            "written_at": written_at or datetime.now(UTC),
            # Pydantic 的 model_dump_json 不转义非 ASCII（不像 stdlib json 默认
            # ensure_ascii=True），所以 failures[].error 里的中文原文是可读的。
            "content_json": manifest.model_dump_json(),
        }
        async with self._scope() as session:
            result = await session.execute(_T.insert().values(**values))
            new_id = inserted_id(result)
        return ManifestRecord(id=new_id, **values)

    # ---- 读 ----

    async def get(self, id: int) -> ManifestRecord | None:
        async with self._scope() as session:
            row = (await session.execute(select(_T).where(_T.c.id == id))).mappings().one_or_none()
        return self._to_model(row, ManifestRecord)

    async def get_or_raise(self, id: int) -> ManifestRecord:
        async with self._scope() as session:
            row = (await session.execute(select(_T).where(_T.c.id == id))).mappings().one_or_none()
        if row is None:
            self._not_found(id)
        return ManifestRecord.model_validate(dict(row))

    async def list_for_task(self, task_id: str) -> list[ManifestRecord]:
        """某个任务的所有清单，正序。

        正常只有一个。多于一个说明 `finalize()` 被调了两次
        （例如 handler 自己写了一份、ctx manager 兜底又写了一份）——
        这种情况**不合并、不去重**，原样交出去让调用方看见，
        因为"一个任务两份清单"本身就是 bug 的信号。
        """
        stmt = (
            select(_T).where(_T.c.task_id == task_id).order_by(_T.c.written_at.asc(), _T.c.id.asc())
        )
        async with self._scope() as session:
            rows = (await session.execute(stmt)).mappings().all()
        return [ManifestRecord.model_validate(dict(row)) for row in rows]

    async def latest_for_task(self, task_id: str) -> ManifestRecord | None:
        rows = await self.list_for_task(task_id)
        return rows[-1] if rows else None

    async def list_recent(self, *, limit: int = 50) -> list[ManifestRecord]:
        """最近的清单，交出去是**正序**（最新在末尾），与 `EventRepository.list_recent` 一致。"""
        stmt = select(_T).order_by(_T.c.written_at.desc(), _T.c.id.desc()).limit(limit)
        async with self._scope() as session:
            rows = (await session.execute(stmt)).mappings().all()
        return [ManifestRecord.model_validate(dict(row)) for row in reversed(rows)]

    async def count(self) -> int:
        async with self._scope() as session:
            return int((await session.execute(select(func.count()).select_from(_T))).scalar_one())

    # ---- 一致性 ----

    async def check_files_exist(
        self, *, limit: int = 200, data_dir: Path | None = None
    ) -> list[str]:
        """抽查最近若干条清单的文件是否还在，返回**缺失的相对路径**列表。

        preflight 用。V1 §7.12 的教训是"预检查的是另一个库，红的绿的都对不上"，
        所以这条检查直接问磁盘，不看库里的任何状态字段。

        `data_dir` 不给就只按库里存的字符串当相对路径拼当前工作目录 —— 那基本没意义，
        所以调用方（preflight）**必须**传。这里不做兜底默认值：
        兜一个错的目录会得到"全部缺失"或"全部存在"两种同样误导的结果。
        """
        if data_dir is None:
            msg = "check_files_exist() 必须给 data_dir（否则拼出来的路径没有意义）"
            raise StorageError(msg)

        rows = await self.list_recent(limit=limit)
        missing: list[str] = []
        for row in rows:
            candidate = Path(row.file_path)
            absolute = candidate if candidate.is_absolute() else data_dir / candidate
            if not absolute.is_file():
                missing.append(row.file_path)
        return missing
