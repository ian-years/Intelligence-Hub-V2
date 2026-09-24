"""`UTCDateTime` 与 enum CHECK 约束的测试。

Alembic 的 `compare_metadata()` **不比 CHECK 约束的文本**（SQLite 反射不出来，
所以 `env.py` 里 `compare_server_default=False`）。这意味着一件具体的事：
往 `HEALTH_STATUSES` 里加一个取值，漂移看护不会红 —— 内存库（走 metadata）放行，
文件库（走迁移里那段字面量，0001 与 0002 各一段）当场拒收。
本文件最后三条用例把这条缝补上：直接从迁移建出来的库里读 `sqlite_master`，
把 CHECK 里的取值集合与 Python 常量对回去。

`UTCDateTime` 那几条测的是"**SQLite 静默丢 tzinfo**"这个坑本身：
裸 `DateTime(timezone=True)` 在 SQLite 上读回来是 naive，
`datetime.now(UTC) - row.created_at` 会抛
`can't subtract offset-naive and offset-aware datetimes`，
而且只在"跑了一段时间之后算时长"时炸 —— 最难查的那一类。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import DateTime as SADateTime
from sqlalchemy import create_engine, insert, select

from intelligence_hub_v2.models.draft import DRAFT_STATUS_VALUES
from intelligence_hub_v2.models.platform import HEALTH_STATUSES as MODEL_STATUSES
from intelligence_hub_v2.storage.db import run_migrations
from intelligence_hub_v2.storage.schema import (
    DRAFT_STATUSES,
    HEALTH_STATUSES,
    MEDIA_SOURCES,
    TASK_STATUSES,
    TRANSCRIPT_ENGINES,
    TRANSCRIPT_SUMMARY_METHODS,
    UTCDateTime,
    metadata,
    platforms_table,
    videos_table,
)

# ---------------------------------------------------------------------------
# UTCDateTime
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_db(tmp_path: Path):
    """一个同步的、用 `create_all` 建出来的库（绕开 aiosqlite，专测列类型本身）。"""
    db = tmp_path / "types.sqlite3"
    engine = create_engine(f"sqlite:///{db.as_posix()}")
    metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def test_utcdatetime_roundtrip_preserves_utc_awareness(sync_db) -> None:
    aware = datetime(2026, 9, 22, 5, 56, 26, tzinfo=UTC)
    with sync_db.begin() as connection:
        connection.execute(
            insert(platforms_table).values(name="douyin", enabled=True, config_json="{}")
        )
        connection.execute(
            insert(videos_table).values(
                platform="douyin", platform_video_id="v1", title="t", published_at=aware
            )
        )
    with sync_db.connect() as connection:
        stored = connection.execute(
            select(videos_table.c.published_at).where(videos_table.c.platform_video_id == "v1")
        ).scalar_one()

    assert stored.tzinfo is not None, "tzinfo 被 SQLite 吞掉了 —— UTCDateTime 没生效"
    assert stored.utcoffset() == timedelta(0)
    assert stored == aware
    # 这条减法就是"最难查的那一类"现场：naive 会抛 TypeError
    assert (datetime.now(UTC) - stored).total_seconds() > 0


def test_naive_input_is_interpreted_as_utc_not_local(sync_db) -> None:
    """naive 输入按 UTC 解释。**不猜本地时区** —— 猜错是永久性的数据损坏。

    这里的危险在于本机时区正好是 UTC+8：如果实现走的是
    `value.replace(tzinfo=None)` 之外的"本地化"路径，测试机会静默通过，
    换一台 UTC 机器才红。所以断言写成绝对时刻相等，而不是"看起来对"。
    """
    naive = datetime(2026, 9, 22, 5, 56, 26)  # noqa: DTZ001 - 这条测的就是 naive 输入
    with sync_db.begin() as connection:
        connection.execute(
            insert(videos_table).values(
                platform="douyin", platform_video_id="naive-1", title="t", published_at=naive
            )
        )
    with sync_db.connect() as connection:
        stored = connection.execute(
            select(videos_table.c.published_at).where(videos_table.c.platform_video_id == "naive-1")
        ).scalar_one()

    assert stored == naive.replace(tzinfo=UTC)
    assert stored.hour == 5  # 没被挪到本地时区（UTC+8 上会变成 13 或 21）


def test_non_utc_input_is_normalized_to_utc(sync_db) -> None:
    """带 `+08:00` 的时刻进来，落库与读回都是同一个**瞬间**的 UTC 表示。"""
    tokyo = datetime(2026, 9, 22, 14, 56, 26, tzinfo=timezone(timedelta(hours=9)))
    expected_utc = tokyo.astimezone(UTC)
    with sync_db.begin() as connection:
        connection.execute(
            insert(videos_table).values(
                platform="douyin", platform_video_id="jst-1", title="t", published_at=tokyo
            )
        )
    with sync_db.connect() as connection:
        stored = connection.execute(
            select(videos_table.c.published_at).where(videos_table.c.platform_video_id == "jst-1")
        ).scalar_one()

    assert stored.utcoffset() == timedelta(0)
    assert stored == expected_utc


def test_none_passes_through_untouched() -> None:
    """类型层的单元测试：`None` 不能被变成 `1970-01-01` 或"现在"。

    `dialect` 传 `None` 是有意的：这两个方法从来不读它（形参只是 SQLAlchemy 的
    调用约定要求的），所以这里不需要为它造一个真方言。
    """
    type_ = UTCDateTime()
    assert type_.process_bind_param(None, None) is None
    assert type_.process_result_value(None, None) is None


def test_impl_is_plain_datetime() -> None:
    """`impl is DateTime`（不带 timezone=True）。

    带了也不会更对（SQLite 仍会丢），但会让 Alembic 在 V3 换 PostgreSQL 时
    把列生成成 `TIMESTAMP WITH TIME ZONE` —— 与"我们自己管 UTC"这套约定冲突，
    等于同时存在两处时区真源。
    """
    assert UTCDateTime.impl is SADateTime


# ---------------------------------------------------------------------------
# enum CHECK 与 Python 常量必须同源
# ---------------------------------------------------------------------------

_CHECK_RE = re.compile(r"CONSTRAINT\s+[\"']?ck_(?P<name>\w+)[\"']?\s+CHECK\s*\((?P<body>[^)]*)\)")
_QUOTED = re.compile(r"'([^']*)'")


def _checks(db: Path) -> dict[str, str]:
    """`{约束名: CHECK 表达式原文}`（跨全库）。"""
    connection = sqlite3.connect(str(db))
    try:
        rows = connection.execute(
            "select sql from sqlite_master where type='table' and sql is not null"
        ).fetchall()
    finally:
        connection.close()
    found: dict[str, str] = {}
    for (sql,) in rows:
        for match in _CHECK_RE.finditer(sql):
            found[match.group("name")] = match.group("body")
    return found


@pytest.fixture(scope="module")
def migrated_checks(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    db = tmp_path_factory.mktemp("checks") / "m.sqlite3"
    run_migrations(db)
    return _checks(db)


@pytest.mark.parametrize(
    ("constraint", "column", "values"),
    [
        ("platforms_health_status_enum", "health_status", HEALTH_STATUSES),
        ("videos_media_source_enum", "media_source", MEDIA_SOURCES),
        ("transcripts_engine_enum", "engine", TRANSCRIPT_ENGINES),
        ("transcripts_summary_method_enum", "summary_method", TRANSCRIPT_SUMMARY_METHODS),
        ("task_runs_status_enum", "status", TASK_STATUSES),
        # drafts（T5.5 / ADR-0021）：这一条的取值同时是 `DraftStatus` 这个 Literal
        ("drafts_status_enum", "status", DRAFT_STATUSES),
    ],
)
def test_migrated_check_lists_exactly_the_model_constants(
    migrated_checks: dict[str, str], constraint: str, column: str, values: tuple[str, ...]
) -> None:
    """**这条是漂移看护照不到的那一块**。

    加一个取值只改常量 → 内存库放行、文件库当场拒收；
    删一个取值只改迁移 → 老数据能读、新数据的 CHECK 却更松。
    两边都在这条用例里红。
    """
    body = migrated_checks[constraint]
    assert column in body
    allowed = tuple(_QUOTED.findall(body))
    assert allowed == values, f"{constraint} 的取值与常量不再同源"


@pytest.mark.parametrize(
    "constraint",
    [
        "platforms_enabled_bool",
        "creators_is_tracking_bool",
        "videos_is_hidden_bool",
        "task_runs_progress_range",
        "task_runs_terminal_has_ended_at",
        "transcripts_char_count_nonneg",
        "transcripts_sentence_count_nonneg",
    ],
)
def test_the_non_enum_guards_are_present_in_the_migrated_db(
    migrated_checks: dict[str, str], constraint: str
) -> None:
    """这些不比对内容，只验**存在**。

    理由很具体：`env.py` 用 batch 模式重建表来加约束，SQLite 上加约束的
    唯一方式是"建新表→拷数据→改名"。那条路径出任何岔子，约束会静默消失，
    而 `compare_metadata` 看不见 CHECK —— 于是"约束写在代码里"与"库里有约束"
    变成两件不同的事。这一条把它们钉回同一件。
    """
    assert constraint in migrated_checks, f"库里没有 {constraint}"


EXPECTED_CHECKS: frozenset[str] = frozenset(
    {
        # platforms
        "platforms_enabled_bool",
        "platforms_health_status_enum",
        # creators
        "creators_is_tracking_bool",
        # videos
        "videos_is_hidden_bool",
        "videos_media_source_enum",
        # transcripts
        "transcripts_engine_enum",
        "transcripts_char_count_nonneg",
        "transcripts_sentence_count_nonneg",
        "transcripts_summary_method_enum",
        # video_metric_snapshots（ADR-0020：随 create_table 下发，autogenerate 看得见）
        "video_metric_snapshots_checkpoint_enum",
        # drafts（ADR-0021 / T5.5：同一条链，建表时随 metadata 下发）
        "drafts_status_enum",
        # task_runs
        "task_runs_status_enum",
        "task_runs_progress_range",
        "task_runs_terminal_has_ended_at",
    }
)


def test_the_migrated_db_has_exactly_the_checks_we_wrote(migrated_checks: dict[str, str]) -> None:
    """**这份清单就是 `_CHECK_RE` 的对照物**。

    比全集而不是子集，有两个用处：
    - 正则写漏一张表 / 一个约束时这里会红（只断言"我想要的那几条在"发现不了）；
    - 有人**删**掉一条看护时也会红 —— 删约束是 batch 模式重建表最容易静默发生的
      岔子，而 SQLite 上"约束没了"唯一的症状是脏数据能写进去。

    `compare_metadata` 看不见 CHECK（`env.py` 里 `compare_server_default=False`），
    所以这条不是冗余：漂移看护对约束的增删改整条是瞎的。
    """
    assert frozenset(migrated_checks) == EXPECTED_CHECKS


def test_the_platform_health_statuses_are_one_tuple_not_two() -> None:
    """`models.platform.HEALTH_STATUSES` 与 `storage.schema.HEALTH_STATUSES`
    是**同一份清单的两个定义点**（Python 层校验与 DB 层 CHECK 各用一份）。

    这条用例是这类"不得不两份"的写法的代价显形：
    `PlatformRepository.set_health()` 先按 models 那份放行，
    库再按 schema 那份拒收 —— 两处一旦漂开，症状是"某个状态能过校验却写不进去"。
    V2.1 加平台健康状态时改一处是不够的。
    """
    assert MODEL_STATUSES == HEALTH_STATUSES


def test_the_draft_statuses_are_one_tuple_not_two() -> None:
    """`models.draft.DraftStatus`（Python 层的 Literal）与
    `storage.schema.DRAFT_STATUSES`（DB 层的 CHECK）必须是同一份清单。

    为什么不是冗余（这一条与上一条同形，但**上面那条的参数化用例已经比过
    `migrated_checks` 与 schema 常量**，两处加起来才是完整的三角）：
    - 只漂 models 那一侧：API 收得下 `status="drafted"`（Pydantic 那份清单放宽了），
      写库时被 CHECK 拒 → 一句 500 的 `StorageError`，用户看不出是自己传错了。
    - 只漂 schema 那一侧：新取值在 Pydantic 就 422，CHECK 里那一项永远用不上，
      而 spec §2.9 的注释与迁移里的字面量各说一套。
    两条路都通不了，但症状都不指向"两份清单漂了"这个原因 —— 所以要在这里钉住。
    """
    assert DRAFT_STATUS_VALUES == DRAFT_STATUSES
