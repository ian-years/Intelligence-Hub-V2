"""Repository 层：每张表一个类，统一从 `BaseRepository` 继承 session 作用域。

**为什么不让业务代码直接写 SQL**：V1 的教训是同一张表有三处写入路径
（`local_store.upsert_video` / 磁盘扫描 `migrate_all_local_data` / 飞书同步），
每一处对"空字段该怎么办"的理解都不一样，于是标题被折成默认值（§7.4）。
V2 里每张表**只有一个写入入口族**，字段级更新是唯一语义。

对外用法是通过 `SqliteStorage` 的属性（`storage.videos.insert(...)`），
不直接构造 Repository —— 构造需要一个 `async_sessionmaker`，
而那个东西应该只由 `SqliteStorage` 持有。
"""

from intelligence_hub_v2.storage.repositories.base import BaseRepository
from intelligence_hub_v2.storage.repositories.creators import CreatorRepository
from intelligence_hub_v2.storage.repositories.events import EventRepository
from intelligence_hub_v2.storage.repositories.manifests import ManifestRepository
from intelligence_hub_v2.storage.repositories.platforms import PlatformRepository
from intelligence_hub_v2.storage.repositories.task_runs import TaskRunRepository
from intelligence_hub_v2.storage.repositories.transcripts import TranscriptRepository
from intelligence_hub_v2.storage.repositories.videos import VideoRepository

__all__ = [
    "BaseRepository",
    "CreatorRepository",
    "EventRepository",
    "ManifestRepository",
    "PlatformRepository",
    "TaskRunRepository",
    "TranscriptRepository",
    "VideoRepository",
]
