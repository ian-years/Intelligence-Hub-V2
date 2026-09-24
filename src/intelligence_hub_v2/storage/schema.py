"""SQLite schema（SQLAlchemy Core，**不用 ORM**）。

契约来源：docs/specs/data-model.md §2（Locked）。改这里必须同时：
1. 写一条 Alembic 迁移（`alembic/versions/`），带 `upgrade()` + `downgrade()`；
2. 更新 `docs/specs/data-model.md`；
3. 如果动的是 V3 要继承的表名/列名/约束，走 ADR。

**为什么不用 ORM**（ADR-0006）：V1 的教训是"整行覆盖"语义（§7.4）——
ORM 的 `session.merge()` / 对象级 flush 天然会把没显式改的字段一起写回去。
Core 的 `update().values(...)` 只 SET 你列出来的列，**字段级更新是默认行为**，
不需要靠纪律维持。

**本模块不建表**：DDL 的唯一真源是 Alembic 迁移。这里的 `metadata` 只用于
① 生成 SQL 表达式；② `alembic check` 比对模型与迁移链是否漂移。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    TypeDecorator,
)
from sqlalchemy.engine.interfaces import Dialect

# ---------------------------------------------------------------------------
# 命名约定
# ---------------------------------------------------------------------------

NAMING_CONVENTION: dict[str, str] = {
    "ix": "idx_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
"""约束命名约定。

没有它，Alembic 的 autogenerate 在 SQLite 上会生成 `_<hex>` 这种匿名约束名，
`downgrade()` 里 drop 不掉 —— 迁移就变成单程票。V3 换 PostgreSQL 同理。
"""

metadata = MetaData(naming_convention=NAMING_CONVENTION)


# ---------------------------------------------------------------------------
# 时间戳类型
# ---------------------------------------------------------------------------


class UTCDateTime(TypeDecorator[datetime]):
    """**永远存 UTC，永远还回 tz-aware 的 UTC**。

    为什么不用裸 `DateTime(timezone=True)`：SQLite 没有时区概念，
    SQLAlchemy 的 sqlite 方言会**静默丢掉 tzinfo**，读回来是 naive datetime。
    于是 `datetime.now(UTC) - row.created_at` 直接
    `TypeError: can't subtract offset-naive and offset-aware datetimes`，
    而这类错误只在"跑了一段时间之后算时长"时才炸 —— 最难查的那一类。

    存法：绑定时统一转 UTC 再剥掉 tzinfo（落成 naive-UTC），读取时贴回 UTC。
    V3 换 PostgreSQL 时这个类型照样能用（`TIMESTAMP WITHOUT TIME ZONE` 存 UTC），
    不需要改迁移。
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, _dialect: Dialect) -> datetime | None:
        """`_dialect` 用不到（SQLite 与 PG 的绑定时机一样），但 SQLAlchemy
        按位置调这个方法，签名必须留着。前导下划线 = "形参是接口要求的，不是漏用"。"""
        if value is None:
            return None
        if value.tzinfo is None:
            # naive 输入按 UTC 解释。不猜本地时区 —— 猜错就是永久性的数据损坏。
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, _dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def _now() -> datetime:
    return datetime.now(UTC)


def _enum_check(
    column: str, name: str, values: tuple[str, ...], *, nullable: bool
) -> CheckConstraint:
    """从 Python 常量生成 enum 的 `CHECK` 约束。

    **为什么要生成而不是手抄**：这四条约束的取值与 `HEALTH_STATUSES` /
    `MEDIA_SOURCES` / `TRANSCRIPT_ENGINES` / `TASK_STATUSES` 是同一份清单的两处副本
    （DB 一道、Python 一道）。手抄的失败方式是"往元组里加一个取值、忘了改约束"，
    而它红的时间点是**采集跑到那条新数据时** —— 也就是最贵的那种红。
    约束文本从常量生成，清单就只剩一处真相。

    `nullable=True` 时加 `col IS NULL OR`。这半句在 SQL 语义上是**冗余**的：
    可空列没值时 `col IN (...)` 求值为 NULL，而 CHECK 只在结果为 FALSE 时才拒绝，
    所以两种写法都放行 NULL。写出来是为了**读**：`health_status IS NULL` 是
    "从没检查过"这个真实状态（V1 §7.20），下一眼就知道这列允许空、且空是合法值。
    """
    listing = ",".join(f"'{value}'" for value in values)
    prefix = f"{column} IS NULL OR " if nullable else ""
    return CheckConstraint(f"{prefix}{column} IN ({listing})", name=name)


# 列类型别名。Text 而不是 String(n)：SQLite 里两者等价，
# 但写 Text 表明"不限长"，V3 迁移到 PG 时不会被误当成 VARCHAR(n) 截断。
_JSON = Text
_Text = Text


# ---------------------------------------------------------------------------
# 2.1 platforms
# ---------------------------------------------------------------------------

HEALTH_STATUSES = ("ok", "degraded", "unreachable", "unknown")

platforms_table = Table(
    "platforms",
    metadata,
    Column("name", String(32), primary_key=True),
    Column("enabled", Boolean, nullable=False),
    # 配置真源在 config/platforms.yaml，这里只是运行态镜像（含健康状态），
    # 方便 API 一次查完不用回去读盘。
    Column("config_json", _JSON, nullable=False),
    Column("health_status", String(16), nullable=True),
    Column("health_checked_at", UTCDateTime, nullable=True),
    Column("health_detail", _Text, nullable=True),
    CheckConstraint("enabled IN (0, 1)", name="enabled_bool"),
    _enum_check("health_status", "health_status_enum", HEALTH_STATUSES, nullable=True),
)


# ---------------------------------------------------------------------------
# 2.2 creators
# ---------------------------------------------------------------------------

creators_table = Table(
    "creators",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column(
        "platform",
        String(32),
        ForeignKey("platforms.name", ondelete="RESTRICT"),
        nullable=False,
    ),
    # V1 §7.11 看护：统一叫 platform_id（sec_uid / mid / user_id / channel_id），
    # 不再有 creator_platform_id / mid 三种写法。
    Column("platform_id", String(191), nullable=False),
    Column("name", _Text, nullable=False),
    Column("avatar_url", _Text, nullable=True),
    Column("follower_count", Integer, nullable=True),
    Column("profile_url", _Text, nullable=False),
    # V1 §7.24 看护：必须真布尔 + CHECK 兜底。落成 0 / "false" 会让
    # "日更采集"和"按位抓取"读出相反的结果。
    Column("is_tracking", Boolean, nullable=False, server_default="1"),
    Column("metadata_json", _JSON, nullable=False, server_default="{}"),
    Column("created_at", UTCDateTime, nullable=False, default=_now),
    Column("updated_at", UTCDateTime, nullable=False, default=_now, onupdate=_now),
    CheckConstraint("is_tracking IN (0, 1)", name="is_tracking_bool"),
    # 唯一身份 = (platform, platform_id)。废 V1 的 creators.json 双源（§7.7）。
    Index("uq_creators_platform_platform_id", "platform", "platform_id", unique=True),
    Index("idx_creators_platform", "platform"),
    Index("idx_creators_tracking", "is_tracking"),
)


# ---------------------------------------------------------------------------
# 2.3 videos
# ---------------------------------------------------------------------------

MEDIA_SOURCES = ("yt_dlp", "page_play_url", "dash_merged", "dash_split")
"""媒体来源。V1 §7.2 看护：抖音那句"yt-dlp 未拿到媒体，已改用页面播放直链"是**常态**，
必须记下来走的是哪条路，否则没法判断该修什么。"""

videos_table = Table(
    "videos",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("platform", String(32), nullable=False),
    Column("platform_video_id", String(191), nullable=False),
    # ON DELETE SET NULL：删博主不删视频，视频变孤儿但仍可查（V1 的删除语义定案）。
    Column("creator_id", Integer, ForeignKey("creators.id", ondelete="SET NULL"), nullable=True),
    Column("title", _Text, nullable=False),
    Column("description", _Text, nullable=True),
    Column("published_at", UTCDateTime, nullable=True),
    Column("duration_seconds", Float, nullable=True),
    Column("view_count", Integer, nullable=True),
    Column("like_count", Integer, nullable=True),
    Column("comment_count", Integer, nullable=True),
    Column("share_count", Integer, nullable=True),
    Column("media_path", _Text, nullable=True),
    Column("media_source", String(32), nullable=True),
    # V1 §7.21 看护：B站可能是未合并的 DASH 分片，辅助文件（.f137.mp4 / .f140.m4a）记这里。
    Column("media_aux_paths_json", _JSON, nullable=False, server_default="[]"),
    Column("cover_path", _Text, nullable=True),
    Column("metadata_json", _JSON, nullable=False, server_default="{}"),
    # V1 §7.25 看护：墓碑内化成列，废 hidden-videos.json。
    # 只删库行不记墓碑的话，磁盘扫描会把条目复活 —— V2 没有磁盘扫描，
    # 但"隐藏"这个语义仍然要求 get() 能取到、list_visible() 不返回。
    Column("is_hidden", Boolean, nullable=False, server_default="0"),
    Column("hidden_at", UTCDateTime, nullable=True),
    Column("hidden_reason", _Text, nullable=True),
    Column("created_at", UTCDateTime, nullable=False, default=_now),
    Column("updated_at", UTCDateTime, nullable=False, default=_now, onupdate=_now),
    CheckConstraint("is_hidden IN (0, 1)", name="is_hidden_bool"),
    _enum_check("media_source", "media_source_enum", MEDIA_SOURCES, nullable=True),
    Index("uq_videos_platform_platform_video_id", "platform", "platform_video_id", unique=True),
    Index("idx_videos_creator", "creator_id"),
    Index("idx_videos_platform_published", "platform", "published_at"),
    Index("idx_videos_visible", "is_hidden", "published_at"),
    Index("idx_videos_created", "created_at"),
)


# ---------------------------------------------------------------------------
# 2.4 transcripts
# ---------------------------------------------------------------------------

TRANSCRIPT_ENGINES = (
    "sherpa_sense_voice",
    "bilibili_subtitle",
    "youtube_subtitle",
    "manual",
)

TRANSCRIPT_SUMMARY_METHODS = ("local-extractive", "v1-imported")
"""摘要的来源。V2 自己产的只有 `local-extractive`（`tasks/reference.py`，≤600 字、
只搬运原文片段）；`v1-imported` 是从 V1 库搬来的那份（实测 1459~2982 字的整篇改写，
V1 侧有四个写入口，生产者是模型还是原稿片段已经无从判断）。
两样东西共用一列而不标来源，看板上就分不出"这条摘要能信到什么程度"（ADR-0015）。
V2.2 接生成式摘要时要一次迁移来放宽这条 CHECK —— 与 `TRANSCRIPT_ENGINES` 同形。"""

transcripts_table = Table(
    "transcripts",
    metadata,
    Column("video_id", Integer, ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True),
    Column("engine", String(32), nullable=False),
    Column("language", String(16), nullable=True),
    Column("char_count", Integer, nullable=False),
    Column("sentence_count", Integer, nullable=False),
    # V1 §7.5 看护：路径统一，不再按平台不对称。
    Column("text_path", _Text, nullable=False),
    Column("segments_json", _JSON, nullable=True),
    # ADR-0015：摘要与要点是**这份稿子**的派生字段，所以与稿子同表 ——
    # `attach()` 整行删了再插，重跑转写时旧摘要自动跟着走，不需要"记得去清另一张表"。
    # 可空 = 没做过；不用空串，那是另一种"有值"。
    Column("content_summary", _Text, nullable=True),
    Column("key_points", _Text, nullable=True),
    Column("summary_method", String(32), nullable=True),
    Column("created_at", UTCDateTime, nullable=False, default=_now),
    _enum_check("engine", "engine_enum", TRANSCRIPT_ENGINES, nullable=False),
    _enum_check("summary_method", "summary_method_enum", TRANSCRIPT_SUMMARY_METHODS, nullable=True),
    CheckConstraint("char_count >= 0", name="char_count_nonneg"),
    CheckConstraint("sentence_count >= 0", name="sentence_count_nonneg"),
)


# ---------------------------------------------------------------------------
# 2.5 task_runs
# ---------------------------------------------------------------------------

TASK_STATUSES = ("running", "success", "partial", "failed", "timeout", "cancelled")

task_runs_table = Table(
    "task_runs",
    metadata,
    Column("id", String(36), primary_key=True),  # UUID
    Column("task_name", String(64), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("status", String(16), nullable=False),
    Column("params_json", _JSON, nullable=False, server_default="{}"),
    Column("config_snapshot_json", _JSON, nullable=False, server_default="{}"),
    Column("started_at", UTCDateTime, nullable=False, default=_now),
    Column("ended_at", UTCDateTime, nullable=True),
    Column("summary_json", _JSON, nullable=True),
    Column("manifest_path", _Text, nullable=True),
    # V1 §1.3 / §7.22 看护：错误原文必须落库，不许只剩一个退出码。
    Column("error_text", _Text, nullable=True),
    Column("progress", Float, nullable=False, server_default="0.0"),
    _enum_check("status", "status_enum", TASK_STATUSES, nullable=False),
    CheckConstraint("progress >= 0.0 AND progress <= 1.0", name="progress_range"),
    # V1 §2 契约二看护：非终态不许留下 ended_at=NULL 的行没人管。
    # 这条约束不强制（running 时确实为 NULL），但强制"终态必须有 ended_at"。
    CheckConstraint(
        "status = 'running' OR ended_at IS NOT NULL",
        name="terminal_has_ended_at",
    ),
    Index("idx_task_runs_started", "started_at"),
    Index("idx_task_runs_status", "status"),
    Index("idx_task_runs_name", "task_name"),
)


# ---------------------------------------------------------------------------
# 2.6 task_events
# ---------------------------------------------------------------------------

task_events_table = Table(
    "task_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("task_id", String(36), ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False),
    Column("timestamp", UTCDateTime, nullable=False, default=_now),
    Column("type", String(64), nullable=False),
    Column("payload_json", _JSON, nullable=False, server_default="{}"),
    Index("idx_task_events_task_time", "task_id", "timestamp"),
    Index("idx_task_events_type_time", "type", "timestamp"),
)


# ---------------------------------------------------------------------------
# 2.7 manifests
# ---------------------------------------------------------------------------

manifests_table = Table(
    "manifests",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("task_id", String(36), ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False),
    Column("schema_version", String(8), nullable=False, server_default="2.0"),
    Column("file_path", _Text, nullable=False),
    Column("written_at", UTCDateTime, nullable=False, default=_now),
    Column("content_json", _JSON, nullable=False),
    Index("idx_manifests_task", "task_id"),
    Index("idx_manifests_written", "written_at"),
)


# ---------------------------------------------------------------------------
# 表名 → Table 的映射（给 Alembic 漂移检查与 preflight 用）
# ---------------------------------------------------------------------------

ALL_TABLES: tuple[Table, ...] = (
    platforms_table,
    creators_table,
    videos_table,
    transcripts_table,
    task_runs_table,
    task_events_table,
    manifests_table,
)
"""V2.0 的表。V2.2 的 topics / drafts / feishu_sync_state 不在这份清单里，
它们的迁移和模型一起加（见 data-model.md §2.8-§2.10）。"""

TABLE_NAMES: frozenset[str] = frozenset(t.name for t in ALL_TABLES)
