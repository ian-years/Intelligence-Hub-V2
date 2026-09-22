"""`platforms` 表的 Repository（运行态镜像）。

**配置的权威源是 `config/platforms.yaml`**，这张表只是运行态副本 ——
存一份是为了让 API 一次查询就拿到"开关 + 健康状态 + 最后检查时间"，
不用回去读盘再拼。所以这里的写操作**不触发**配置热重载（那是 `ConfigManager` 的事）。

**分层纪律**：本模块不 import `platforms.base.PlatformConfig`。
依赖方向是 `api → core → platforms → infra → storage`，storage 在最底下，
往上 import 就成了环。所以这里只吃 `Mapping` / JSON 字符串，
"从 ConfigManager 读配置灌进这张表"的编排放在 `core/`（Task 9 接线）。

健康状态的语义看护 V1 §7.20：`None`（从没检查过）与 `unknown`（检查了但判不出来）
都**不是**绿灯。那次事故是桥的浏览器早死了、`/health` 却回 `ok:true`，
于是三连"未登录"其实是"测不到"。`PlatformRecord.is_healthy` 只认显式 `"ok"`。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from intelligence_hub_v2.errors import StorageError
from intelligence_hub_v2.models.platform import HEALTH_STATUSES, HealthStatus, PlatformRecord
from intelligence_hub_v2.storage.repositories.base import BaseRepository, affected_rows
from intelligence_hub_v2.storage.schema import platforms_table

_T = platforms_table


class PlatformRepository(BaseRepository):
    entity = "platform"

    # ---- 读 ----

    async def get(self, name: str) -> PlatformRecord | None:
        async with self._scope() as session:
            row = (
                (await session.execute(select(_T).where(_T.c.name == name)))
                .mappings()
                .one_or_none()
            )
        return self._to_model(row, PlatformRecord)

    async def get_or_raise(self, name: str) -> PlatformRecord:
        async with self._scope() as session:
            row = (
                (await session.execute(select(_T).where(_T.c.name == name)))
                .mappings()
                .one_or_none()
            )
        if row is None:
            self._not_found(name)
        return PlatformRecord.model_validate(dict(row))

    async def list_all(self, *, enabled_only: bool = False) -> list[PlatformRecord]:
        """全部平台，按名字排序。

        排序不是为了好看：前端平台开关列表每次刷新顺序一致，
        否则 `dict` 的插入顺序一变（改了 platforms.yaml 的书写顺序）界面就跳。
        """
        stmt = select(_T).order_by(_T.c.name.asc())
        if enabled_only:
            stmt = stmt.where(_T.c.enabled.is_(True))
        async with self._scope() as session:
            rows = (await session.execute(stmt)).mappings().all()
        return [PlatformRecord.model_validate(dict(row)) for row in rows]

    async def count(self, *, enabled_only: bool = False) -> int:
        stmt = select(func.count()).select_from(_T)
        if enabled_only:
            stmt = stmt.where(_T.c.enabled.is_(True))
        async with self._scope() as session:
            return int((await session.execute(stmt)).scalar_one())

    # ---- 写 ----

    async def upsert(
        self,
        name: str,
        *,
        enabled: bool,
        config: Mapping[str, Any] | str | None = None,
    ) -> PlatformRecord:
        """插入或更新（**不动健康状态三列**）。

        `on_conflict_do_update` 的 `set_` 里刻意没有 `health_status` /
        `health_checked_at` / `health_detail`：配置热重载时如果把它们一起刷成 NULL，
        前端的健康灯会在每次改配置后闪一下"未检查" —— 而健康状态与配置无关，
        它由 preflight / 采集任务写。

        `config` 可以是 Mapping（自动序列化）或已序列化的字符串。
        给 None 表示"不改配置"，此时新插入的行落 `{}`。
        """
        if not isinstance(enabled, bool):
            msg = (
                f"platforms.enabled 必须是真 bool，收到 {type(enabled).__name__}: {enabled!r}。"
                f"（V1 §7.24 同一类坑：落成 0 / 'false' 会让开关读出相反的结果）"
            )
            raise TypeError(msg)

        config_json = _as_json(config)

        values: dict[str, Any] = {"name": name, "enabled": enabled, "config_json": config_json}
        stmt = sqlite_insert(_T).values(**values)
        set_: dict[str, Any] = {"enabled": stmt.excluded.enabled}
        if config is not None:
            set_["config_json"] = stmt.excluded.config_json
        stmt = stmt.on_conflict_do_update(index_elements=[_T.c.name], set_=set_)

        async with self._scope() as session:
            await session.execute(stmt)
        return await self.get_or_raise(name)

    async def set_enabled(self, name: str, enabled: bool) -> PlatformRecord:
        """只改开关（Settings 页的平台开关走这条）。

        与 `upsert()` 分开：Settings 页保存开关时**不应该**顺带改 `config_json`
        （那份配置可能刚被热重载更新过，用页面里的旧副本覆盖等于回滚用户的改动）。
        """
        if not isinstance(enabled, bool):
            msg = f"enabled 必须是真 bool，收到 {type(enabled).__name__}: {enabled!r}"
            raise TypeError(msg)
        async with self._scope() as session:
            result = await session.execute(
                update(_T).where(_T.c.name == name).values(enabled=enabled)
            )
            if affected_rows(result) == 0:
                self._not_found(name)
        return await self.get_or_raise(name)

    async def set_health(
        self,
        name: str,
        status: HealthStatus,
        *,
        detail: str | None = None,
        checked_at: datetime | None = None,
    ) -> PlatformRecord:
        """写健康状态。三列一起写（status + checked_at + detail）。

        `detail` **必须是原文**：V1 §7.20 的教训是"红了但不知道为什么红"，
        于是只能猜。桥的探活错误、yt-dlp 的失败原文都要留在这里。

        `status` 只接受 `HEALTH_STATUSES` 里的取值 —— DB 层有 CHECK 兜底，
        但在这里先拦一道，错误文案能直接说明白"这是枚举值写错了"，
        而不是抛一句 `CHECK constraint failed: platforms.health_status_enum`。
        """
        if status not in HEALTH_STATUSES:
            msg = (
                f"未知的健康状态 {status!r}，只接受 {list(HEALTH_STATUSES)}。"
                f"（要加新状态得同时改 schema 的 CHECK 与 models.platform.HealthStatus）"
            )
            raise StorageError(msg)
        async with self._scope() as session:
            result = await session.execute(
                update(_T)
                .where(_T.c.name == name)
                .values(
                    health_status=status,
                    health_checked_at=checked_at or datetime.now(UTC),
                    health_detail=detail,
                )
            )
            if affected_rows(result) == 0:
                self._not_found(name)
        return await self.get_or_raise(name)

    async def clear_health(self, name: str) -> PlatformRecord:
        """把健康状态清回"从没检查过"。

        用途：配置里换掉了平台的实现档位（例如从 yt-dlp 换成桥枚举），
        上一次的绿灯已经不代表现在 —— 留着它比显示"未检查"更危险。
        """
        async with self._scope() as session:
            result = await session.execute(
                update(_T)
                .where(_T.c.name == name)
                .values(health_status=None, health_checked_at=None, health_detail=None)
            )
            if affected_rows(result) == 0:
                self._not_found(name)
        return await self.get_or_raise(name)

    async def delete(self, name: str) -> bool:
        """删一个平台的运行态行。

        `creators.platform` 是 `ON DELETE RESTRICT` —— 库里还有这个平台的博主时
        **删不掉**（`IntegrityError` → `StorageError`）。这是故意的：
        V2.0 只有抖音/B站，V2.1 才加小红书/YouTube，"平台退出"这条路
        在可预见的版本里不会被走到，真走到了也必须先把博主数据处置掉。

        必须翻译 `IntegrityError`：不翻译的话调用方（Settings 页的"移除平台"）
        会拿到一屏 SQLAlchemy traceback，而它需要的是"这个平台下还有 N 位博主"。
        """
        try:
            async with self._scope() as session:
                result = await session.execute(_T.delete().where(_T.c.name == name))
        except IntegrityError as exc:
            raise self._translate_integrity(exc) from exc
        return affected_rows(result) > 0

    async def prune_unknown(self, keep: Iterable[str]) -> int:
        """删掉不在 `keep` 里的行（本构建不再支持的平台）。返回删除行数。

        场景：用户在 V2.1 上跑过、库里留了 `xiaohongshu` 行，然后换回 V2.0 的构建。
        `ConfigManager._load_platforms()` 会因为 platforms.yaml 里有未知 key 直接报 `ConfigError`，
        但**库里**的残留行不会自己消失 —— 前端会显示一个点了没反应的平台开关。

        `keep` 为空时**一条都不删**（防御：传错参数把整张表清空，
        然后所有平台的健康状态归零，看着像"全部平台挂了"）。
        """
        names = list(keep)
        if not names:
            return 0
        async with self._scope() as session:
            result = await session.execute(delete(_T).where(_T.c.name.not_in(names)))
        return affected_rows(result)


def _json_default(obj: object) -> str:
    """`json.dumps(default=...)`：`Path` → **posix 字符串**，其他 → `str(obj)`。

    为什么要专门处理 `Path` 而不是靠 `default=str`：
    `str(Path("data/cookies/x.txt"))` 在 Windows 上是 `data\\cookies\\x.txt`。
    而库里其他所有路径（`videos.media_path` / `manifests.file_path`）都走
    `FileStorage.rel()`，那是 `as_posix()`。**同一张库里两种分隔符**会让
    "按路径前缀筛媒体"这类查询在 Windows 上静默匹配不到。

    其余情况仍然 `str(obj)`：代价是"任何序列化不了的东西都被静默转成字符串"，
    真出现一个 set 或自定义对象，库里会落一段没意义的文本。
    这里接受这个代价，因为写进来的东西全部来自 **Pydantic 校验过的** PlatformConfig
    （`model_dump()` 的产物只有基本类型 + Path），不是任意外部输入。
    """
    if isinstance(obj, Path):
        return obj.as_posix()
    return str(obj)


def _as_json(config: Mapping[str, Any] | str | None) -> str:
    """把配置统一成 JSON 字符串。None → `{}`（列是 NOT NULL）。"""
    if config is None:
        return "{}"
    if isinstance(config, str):
        return config
    return json.dumps(dict(config), ensure_ascii=False, default=_json_default)
