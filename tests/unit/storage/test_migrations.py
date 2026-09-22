"""迁移与 `SqliteStorage` 装配的测试。

这个文件里最值钱的是两条：

1. **`check_schema_matches_migrations()` 返回空** —— 迁移链跑出来的 schema 与
   模型 `metadata` 一致。没有它，"测试全绿但生产炸"是必然的：
   测试大量使用内存库（走 `create_all`，永远与 metadata 一致），
   而生产走 Alembic。两条路径的产物必须逐列相同，这里把它们钉在一起。
2. **`alembic.ini` 必须是纯 ASCII** —— Alembic 用 `encoding="locale"` 读它，
   中文 Windows 上就是 GBK；一条 UTF-8 中文注释会让 `alembic revision`
   直接 `UnicodeDecodeError`（docs/lessons.md 坑 8，2026-09-22 真踩过）。
"""

from __future__ import annotations

import asyncio
import configparser
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy import text as sa_text

from intelligence_hub_v2.errors import MigrationError, StorageError
from intelligence_hub_v2.models.event import EventType
from intelligence_hub_v2.storage.db import (
    SqliteStorage,
    alembic_dir,
    alembic_ini,
    async_url,
    check_schema_matches_migrations,
    current_revision,
    downgrade_migrations,
    run_migrations,
    sync_url,
)
from intelligence_hub_v2.storage.schema import TABLE_NAMES, metadata

# ---------------------------------------------------------------------------
# alembic 资产
# ---------------------------------------------------------------------------


def test_alembic_dir_and_ini_exist() -> None:
    assert alembic_dir().is_dir()
    assert (alembic_dir() / "env.py").is_file()
    assert alembic_ini().is_file()


def test_alembic_ini_is_pure_ascii() -> None:
    """**坑 8 的回归看护**。

    Alembic 读 ini 用 `encoding="locale"`（中文 Windows = GBK），
    所以这个文件里不能有任何非 ASCII 字节 —— 连注释都不行。
    中文的理由写在 `alembic/env.py` 的 docstring 里（`.py` 永远按 UTF-8 读）。

    这条用例比"记得别写中文"可靠：它会在 pre-commit 阶段红，
    而不是等到某次要加迁移时才发现 `alembic revision` 起不来。
    """
    raw = alembic_ini().read_bytes()
    offenders = sorted({byte for byte in raw if byte > 127})
    assert not offenders, f"alembic.ini 含非 ASCII 字节: {offenders}"
    raw.decode("ascii")  # 再确认一次，失败信息更直观


def _ini_options() -> dict[str, str]:
    """读 `alembic.ini` 的 `[alembic]` 段。

    **必须走 configparser 而不是子串搜索**：这个文件的注释里为了说明"为什么没配"
    恰好写了 `sqlalchemy.url` 与 `version_path_separator` 这两个名字。
    子串搜索会把解释性的注释当成配置读出来，然后这条看护永远红。
    """
    parser = configparser.ConfigParser()
    parser.read(alembic_ini(), encoding="ascii")
    return dict(parser["alembic"])


def test_alembic_ini_uses_path_separator_not_the_deprecated_alias() -> None:
    """Alembic 1.20 把 `version_path_separator` 改名成 `path_separator`，
    读旧名字时发 DeprecationWarning —— 而 pytest 的 `filterwarnings = ["error"]`
    会把它升级成异常。当时**只有 `[file]` 档的用例红**，内存库全绿，
    所以这个坑在没有双后端 fixture 时是看不见的。
    """
    options = _ini_options()
    assert options.get("path_separator") == "os"
    assert "version_path_separator" not in options


def test_alembic_ini_does_not_hardcode_a_url() -> None:
    """URL 写死在 ini 里等于把"data/ 位置可配"这个契约废掉（V1 §7.12 那类坑）。

    env.py 按三级顺序解析：显式 set_main_option > 环境变量 > config/app.yaml。
    """
    assert "sqlalchemy.url" not in _ini_options()


def test_alembic_ini_points_at_the_local_script_dir() -> None:
    options = _ini_options()
    assert options["script_location"] == "alembic"
    assert options["prepend_sys_path"] == "src"


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------


def test_run_migrations_creates_every_table(tmp_path: Path) -> None:
    db = tmp_path / "mig.sqlite3"
    run_migrations(db)

    engine = create_engine(sync_url(db))
    try:
        names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert names >= TABLE_NAMES
    assert "alembic_version" in names


def test_current_revision_is_none_before_any_migration(tmp_path: Path) -> None:
    assert current_revision(tmp_path / "fresh.sqlite3") is None


def test_current_revision_after_upgrade(tmp_path: Path) -> None:
    db = tmp_path / "mig.sqlite3"
    run_migrations(db)
    revision = current_revision(db)
    assert revision is not None
    assert revision != ""


def test_run_migrations_is_idempotent(tmp_path: Path) -> None:
    """启动时每次都跑 `upgrade head`。第二次必须是 no-op，不是报错。"""
    db = tmp_path / "mig.sqlite3"
    run_migrations(db)
    first = current_revision(db)
    run_migrations(db)
    assert current_revision(db) == first


def test_run_migrations_creates_missing_parent_dirs(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "deeper" / "hub.sqlite3"
    run_migrations(db)
    assert db.is_file()


def test_run_migrations_wraps_failures_in_migration_error(tmp_path: Path) -> None:
    """坏目标要带上库路径报出来 —— "迁移失败"不说哪个库等于没说。"""
    db = tmp_path / "mig.sqlite3"
    run_migrations(db)
    with pytest.raises(MigrationError, match="alembic downgrade") as caught:
        downgrade_migrations(db, target="no-such-revision")
    assert str(db) in str(caught.value)
    assert "no-such-revision" in str(caught.value)


def test_downgrade_base_then_upgrade_head_roundtrips(tmp_path: Path) -> None:
    """**迁移可逆**，而且可逆之后再升回来与模型一致。

    `downgrade()` 写得对不对只有真跑一遍才知道 —— 尤其 SQLite 上
    删列/改约束要靠 `render_as_batch` 的"建新表→拷数据→改名"。
    """
    db = tmp_path / "mig.sqlite3"
    run_migrations(db)
    assert not check_schema_matches_migrations(db)

    downgrade_migrations(db)

    engine = create_engine(sync_url(db))
    try:
        remaining = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert remaining == {"alembic_version"}

    run_migrations(db)
    engine = create_engine(sync_url(db))
    try:
        names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert names >= TABLE_NAMES
    assert not check_schema_matches_migrations(db)


# ---------------------------------------------------------------------------
# 漂移看护（本文件的核心）
# ---------------------------------------------------------------------------


def test_schema_matches_migrations(tmp_path: Path) -> None:
    db = tmp_path / "mig.sqlite3"
    run_migrations(db)
    diffs = check_schema_matches_migrations(db)
    assert diffs == []


def test_in_memory_create_all_and_alembic_produce_the_same_schema(tmp_path: Path) -> None:
    """把两条建表路径**逐表逐列**钉在一起。

    `check_schema_matches_migrations()` 比对的是"迁移 vs metadata"，
    而 metadata 正是内存库 `create_all` 的输入 —— 所以传递成立。
    但那条比对靠 Alembic 的 autogenerate，它对 SQLite 的反射有已知盲区
    （CHECK 约束的原始文本反射不出来，所以 `compare_server_default=False`）。
    这条用例直接比列名与可空性，补上那块盲区。
    """
    db = tmp_path / "mig.sqlite3"
    run_migrations(db)

    alembic_engine = create_engine(sync_url(db))
    memory_engine = create_engine("sqlite://")
    metadata.create_all(memory_engine)
    try:
        alembic_inspector = inspect(alembic_engine)
        memory_inspector = inspect(memory_engine)

        alembic_tables = set(alembic_inspector.get_table_names()) - {"alembic_version"}
        memory_tables = set(memory_inspector.get_table_names())
        assert alembic_tables == memory_tables == set(TABLE_NAMES)

        for table in sorted(TABLE_NAMES):
            migrated = {
                (c["name"], str(c["type"]), c["nullable"])
                for c in alembic_inspector.get_columns(table)
            }
            created = {
                (c["name"], str(c["type"]), c["nullable"])
                for c in memory_inspector.get_columns(table)
            }
            assert migrated == created, f"{table} 的列在两条建表路径下不一致"
    finally:
        alembic_engine.dispose()
        memory_engine.dispose()


def test_drift_guard_actually_detects_drift(tmp_path: Path) -> None:
    """看护本身要能被验证 —— 一个永远返回空的检查等于没有检查。

    手工往库里加一列（模型里没有），`check_schema_matches_migrations()` 必须报出来。
    """
    db = tmp_path / "mig.sqlite3"
    run_migrations(db)
    assert check_schema_matches_migrations(db) == []

    engine = create_engine(sync_url(db))
    try:
        with engine.begin() as connection:
            connection.execute(sa_text("ALTER TABLE creators ADD COLUMN rogue_column TEXT"))
    finally:
        engine.dispose()

    diffs = check_schema_matches_migrations(db)
    assert diffs, "漂移看护没有发现一个明确多出来的列 —— 它坏了"
    assert any("rogue_column" in diff for diff in diffs)


# ---------------------------------------------------------------------------
# URL
# ---------------------------------------------------------------------------


def test_urls_use_posix_separators(tmp_path: Path) -> None:
    """Windows 路径直接塞进 URL，SQLAlchemy 会把 `\\0` 当转义序列。"""
    db = tmp_path / "hub.sqlite3"
    assert "\\" not in sync_url(db)
    assert "\\" not in async_url(db)
    assert sync_url(db).startswith("sqlite+pysqlite:///")
    assert async_url(db).startswith("sqlite+aiosqlite:///")


def test_memory_url_is_special_cased() -> None:
    assert sync_url(":memory:") == "sqlite+pysqlite:///:memory:"
    assert async_url(":memory:") == "sqlite+aiosqlite:///:memory:"


# ---------------------------------------------------------------------------
# SqliteStorage 装配
# ---------------------------------------------------------------------------


async def test_repositories_are_unavailable_before_initialize() -> None:
    """不返回半初始化的 Repository 让调用方拿到 None 再猜。"""
    storage = SqliteStorage.in_memory()
    for accessor in (
        "platforms",
        "creators",
        "videos",
        "transcripts",
        "task_runs",
        "events",
        "manifests",
    ):
        with pytest.raises(StorageError, match="initialize"):
            getattr(storage, accessor)
    with pytest.raises(StorageError, match="initialize"):
        _ = storage.sessionmaker


async def test_initialize_is_idempotent() -> None:
    storage = SqliteStorage.in_memory()
    await storage.initialize()
    await storage.platforms.upsert("douyin", enabled=True)
    await storage.initialize()  # 第二次必须是 no-op，不能把 engine 重建掉
    assert await storage.platforms.count() == 1
    await storage.close()


async def test_healthcheck_is_a_real_roundtrip() -> None:
    storage = SqliteStorage.in_memory()
    await storage.initialize()
    try:
        assert await storage.healthcheck() is True
    finally:
        await storage.close()


async def test_file_storage_runs_migrations_on_initialize(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "hub.sqlite3"
    storage = SqliteStorage(db)
    await storage.initialize()
    try:
        assert db.is_file()
        assert current_revision(db) is not None
        assert await storage.healthcheck() is True
    finally:
        await storage.close()


async def test_file_storage_uses_wal(tmp_path: Path) -> None:
    """WAL 是并发读 + 单写的前提。`synchronous=NORMAL` 在 WAL 下是安全/速度的平衡点。"""
    db = tmp_path / "hub.sqlite3"
    storage = SqliteStorage(db)
    await storage.initialize()
    try:
        async with storage.sessionmaker() as session:
            journal = (await session.execute(sa_text("PRAGMA journal_mode"))).scalar_one()
            fk = (await session.execute(sa_text("PRAGMA foreign_keys"))).scalar_one()
            busy = (await session.execute(sa_text("PRAGMA busy_timeout"))).scalar_one()
        assert str(journal).lower() == "wal"
        assert int(fk) == 1
        assert int(busy) == 5000
    finally:
        await storage.close()


async def test_foreign_keys_pragma_is_set_on_every_connection(tmp_path: Path) -> None:
    """**SQLite 的外键默认是关的，而且是 per-connection 而不是 per-database。**

    忘了设的后果不是报错，是 `ON DELETE CASCADE` / `ON DELETE SET NULL`
    静默不生效 —— 删了博主，视频行的 `creator_id` 还指着一个不存在的 id。

    这里故意用**文件库 + 三条并发连接**：内存库是 StaticPool（全程只有一条连接），
    在它上面验不出"每条新连接都要设一遍"这件事。
    """
    storage = SqliteStorage(tmp_path / "pragma.sqlite3")
    await storage.initialize()
    try:

        async def probe() -> int:
            async with storage.sessionmaker() as session:
                return int((await session.execute(sa_text("PRAGMA foreign_keys"))).scalar_one())

        values = await asyncio.gather(probe(), probe(), probe())
        assert values == [1, 1, 1]
    finally:
        await storage.close()


async def test_memory_storage_shares_one_connection() -> None:
    """StaticPool 的必要性：不然每个 session 看到的是一个空的新库。"""
    storage = SqliteStorage.in_memory()
    await storage.initialize()
    try:
        await storage.platforms.upsert("douyin", enabled=True)
        # 换一个 session（= 换一个连接，如果没有 StaticPool）
        assert await storage.platforms.count() == 1
    finally:
        await storage.close()


async def test_close_releases_the_repositories() -> None:
    storage = SqliteStorage.in_memory()
    await storage.initialize()
    await storage.close()
    with pytest.raises(StorageError, match="initialize"):
        _ = storage.platforms
    await storage.close()  # 幂等


# ---------------------------------------------------------------------------
# 跨 Repository 的事务
# ---------------------------------------------------------------------------


async def test_transaction_commits_across_repositories(storage: SqliteStorage) -> None:
    """一次事务里写三张表，退出后三处都看得见。"""
    async with storage.transaction():
        await storage.platforms.upsert("bilibili", enabled=True)
        await storage.task_runs.start(task_id="tx1", task_name="B站采集", kind="platform_collect")
        await storage.events.append("tx1", EventType.TASK_STARTED)

    assert (await storage.platforms.get_or_raise("bilibili")).enabled is True
    assert (await storage.task_runs.get_or_raise("tx1")).status == "running"
    assert await storage.events.count(task_id="tx1") == 1


async def test_transaction_rolls_back_every_repository(storage: SqliteStorage) -> None:
    """**这条是 ContextVar 方案的存在理由**：三个 Repository 要么全成要么全不成。

    V1 里"写视频 + 写清单"是两次独立的 sqlite3 连接，中间崩了就留下
    半截数据（库里有作品、没有清单，看板上看着像成功）。
    """
    with pytest.raises(RuntimeError, match="炸了"):
        async with storage.transaction():
            await storage.platforms.upsert("bilibili", enabled=True)
            await storage.task_runs.start(
                task_id="tx2", task_name="B站采集", kind="platform_collect"
            )
            msg = "炸了"
            raise RuntimeError(msg)

    assert await storage.platforms.get("bilibili") is None
    assert await storage.task_runs.get("tx2") is None


async def test_nested_transaction_reuses_the_outer_one(storage: SqliteStorage) -> None:
    """SQLite 没有真正的嵌套事务。用 SAVEPOINT 假装支持会把
    "内层失败外层继续"这种语义引进来，而 V2 没有任何一处需要它。"""
    async with storage.transaction():
        await storage.platforms.upsert("bilibili", enabled=True)
        async with storage.transaction():
            await storage.platforms.upsert("youtube", enabled=False)

    assert await storage.platforms.count() == 2


async def test_transaction_does_not_leak_into_sibling_tasks(storage: SqliteStorage) -> None:
    """ContextVar 按 asyncio Task 隔离。并发的两个任务不能共用一个 session ——
    共用就是"一个任务 rollback 把另一个任务的写入也带走"。"""
    results: list[str] = []

    async def writer(name: str, platform: str) -> None:
        async with storage.transaction():
            await storage.platforms.upsert(platform, enabled=True)
            await asyncio.sleep(0.01)
            results.append(name)

    await asyncio.gather(writer("a", "bilibili"), writer("b", "youtube"))

    assert sorted(results) == ["a", "b"]
    assert await storage.platforms.count() == 2
