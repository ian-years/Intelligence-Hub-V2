"""SQLite 存储装配：engine、迁移、`SqliteStorage`。

契约来源：docs/specs/data-model.md §4（Storage 抽象）+ §5（Alembic 迁移）

三条实现纪律：

1. **`PRAGMA foreign_keys=ON` 必须每条连接都设**。SQLite 的外键默认是**关的**，
   而且是 per-connection 而不是 per-database。忘了设的后果不是报错，是
   `ON DELETE CASCADE` / `ON DELETE SET NULL` **静默不生效** —— 删了博主，
   视频行的 `creator_id` 还指着一个不存在的 id。有专门的测试守着。
2. **迁移是 DDL 的唯一真源**（Alembic），`metadata.create_all()` 只用于内存库。
   两条路径必须一致，靠 `check_schema_matches_migrations()` 这条漂移看护保证。
3. **不用 ORM Session 做业务写**。见 `storage/schema.py` 模块 docstring（V1 §7.4）。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from intelligence_hub_v2.errors import MigrationError, StorageError
from intelligence_hub_v2.logging import get_logger
from intelligence_hub_v2.storage.repositories.comments import VideoCommentRepository
from intelligence_hub_v2.storage.repositories.creators import CreatorRepository
from intelligence_hub_v2.storage.repositories.events import EventRepository
from intelligence_hub_v2.storage.repositories.manifests import ManifestRepository
from intelligence_hub_v2.storage.repositories.metrics import MetricSnapshotRepository
from intelligence_hub_v2.storage.repositories.platforms import PlatformRepository
from intelligence_hub_v2.storage.repositories.task_runs import TaskRunRepository
from intelligence_hub_v2.storage.repositories.transcripts import TranscriptRepository
from intelligence_hub_v2.storage.repositories.videos import VideoRepository
from intelligence_hub_v2.storage.schema import metadata
from intelligence_hub_v2.storage.session import CURRENT_SESSION

if TYPE_CHECKING:
    from sqlalchemy.engine.interfaces import DBAPIConnection

    from intelligence_hub_v2.core.config import AppConfig

logger = get_logger(__name__)

__all__ = [
    "CURRENT_SESSION",
    "SqliteStorage",
    "alembic_dir",
    "alembic_ini",
    "async_url",
    "check_schema_matches_migrations",
    "current_revision",
    "downgrade_migrations",
    "resolve_db_path",
    "run_migrations",
    "sync_url",
]


# ---------------------------------------------------------------------------
# 路径与 URL
# ---------------------------------------------------------------------------


def resolve_db_path(config: AppConfig, root: Path | None = None) -> Path:
    """主库文件路径 = `data.dir` / `storage.sqlite_file`。

    **只有一个库**（V1 §7.12 的结构性消除：V1 的预检查的是飞书镜像库而不是主库，
    因为"哪个是主库"这件事在代码里是分支决定的）。这里没有任何分支。
    """
    return config.data.resolve_all(root)["root"] / config.storage.sqlite_file


def _url_for(db_path: Path | str, driver: str) -> str:
    if str(db_path) == ":memory:":
        return f"sqlite+{driver}:///:memory:"
    # Windows 路径必须 as_posix()：`E:\08-Codework\...` 直接塞进 URL，
    # SQLAlchemy 会把 `\0` 当转义序列。
    return f"sqlite+{driver}:///{Path(db_path).resolve().as_posix()}"


def sync_url(db_path: Path | str) -> str:
    """同步 URL（pysqlite）。Alembic 与漂移检查用。"""
    return _url_for(db_path, "pysqlite")


def async_url(db_path: Path | str) -> str:
    """异步 URL（aiosqlite）。业务代码用。"""
    return _url_for(db_path, "aiosqlite")


def alembic_dir() -> Path:
    """Alembic 脚本目录。

    从包位置往上推 3 层（`src/intelligence_hub_v2/storage/db.py` → 仓库根）。
    这对源码检出成立，对安装成 wheel 的包**不成立** —— 但 V2 是本机工具，
    永远从源码跑。真遇到装成包还要跑迁移的场景，用
    `INTELLIGENCE_HUB_ALEMBIC_DIR` 显式指路。
    """
    override = os.environ.get("INTELLIGENCE_HUB_ALEMBIC_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "alembic"


def alembic_ini() -> Path:
    return alembic_dir().parent / "alembic.ini"


# ---------------------------------------------------------------------------
# 迁移
# ---------------------------------------------------------------------------


def _alembic_config(db_path: Path | str) -> Config:
    """构造 Alembic Config。URL 显式塞进去，**不让 env.py 去读 app.yaml**。

    理由：`SqliteStorage.initialize()` 已经知道自己要哪个库；如果这里不塞，
    env.py 会回落到配置文件，于是"给测试库跑迁移"会静默迁到开发库上。

    同时把 `configure_logger` 置 False：env.py 默认会 `dictConfig` 一套自己的
    日志配置，而迁移是在 `setup_logging()` **之后**跑的 —— 不关掉就会把
    structlog 的 processor 链和 RotatingFileHandler 冲掉。
    """
    ini = alembic_ini()
    if not ini.is_file():
        msg = f"找不到 alembic.ini: {ini}（用 INTELLIGENCE_HUB_ALEMBIC_DIR 指定 alembic/ 的位置）"
        raise MigrationError(msg)

    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(alembic_dir()))
    cfg.set_main_option("sqlalchemy.url", sync_url(db_path))
    cfg.attributes["configure_logger"] = False
    return cfg


def run_migrations(db_path: Path | str, *, target: str = "head") -> None:
    """同步跑 `alembic upgrade <target>`。

    同步是故意的：迁移是启动前的一次性动作，而 `SqliteStorage.initialize()`
    本身就在事件循环里 —— 用 async alembic 要在循环里再 `asyncio.run()`，直接炸。
    异步调用方用 `asyncio.to_thread(run_migrations, ...)`。
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    cfg = _alembic_config(db_path)
    try:
        command.upgrade(cfg, target)
    except MigrationError:
        raise
    except Exception as exc:  # 转成本仓库的异常族，带上库路径
        msg = f"alembic upgrade {target} 失败（库: {db_path}）: {exc}"
        raise MigrationError(msg) from exc


def downgrade_migrations(db_path: Path | str, *, target: str = "base") -> None:
    """同步跑 `alembic downgrade <target>`。CI 用它验证迁移可逆。"""
    cfg = _alembic_config(db_path)
    try:
        command.downgrade(cfg, target)
    except Exception as exc:  # 与 run_migrations 同理：库路径必须在错误里
        msg = f"alembic downgrade {target} 失败（库: {db_path}）: {exc}"
        raise MigrationError(msg) from exc


def current_revision(db_path: Path | str) -> str | None:
    """库当前的迁移版本号。库还没建过表时返回 None。"""
    engine = create_engine(sync_url(db_path))
    try:
        with engine.connect() as connection:
            if "alembic_version" not in inspect(connection).get_table_names():
                return None
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def check_schema_matches_migrations(db_path: Path | str) -> list[str]:
    """比对"迁移链跑出来的 schema"与"模型 metadata"，返回差异列表（空 = 一致）。

    **这条是漂移看护的核心**。`SqliteStorage.in_memory()` 走 `create_all`，
    文件库走 Alembic —— 两条路径必须产出同一个 schema，否则"测试全绿但生产炸"。
    没有这个函数，那种分歧只会在真机第一次跑迁移时暴露。
    """
    engine = create_engine(sync_url(db_path))
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={"compare_type": True, "render_as_batch": True},
            )
            return [str(diff) for diff in compare_metadata(context, metadata)]
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 环境会话（跨 Repository 的事务）
# ---------------------------------------------------------------------------

# `CURRENT_SESSION` 定义在 `storage/session.py`（叶子模块），这里只 re-export。
# 搬走的原因写在 `session.py` 的 docstring 里：它同时被 `db.py` 与
# `repositories/base.py` 需要，留在本模块会让两边的导入成环。


# ---------------------------------------------------------------------------
# SqliteStorage
# ---------------------------------------------------------------------------


class SqliteStorage:
    """`Storage` Protocol 的 SQLite 实现（docs/specs/data-model.md §4）。

    用法：
    ```python
    storage = SqliteStorage(Path("data/intelligence_hub.sqlite3"))
    await storage.initialize()
    video = await storage.videos.get(1)
    await storage.close()
    ```

    **`initialize()` 之前所有属性都抛 `StorageError`** —— 不返回一个半初始化的
    Repository 让调用方拿到 None 再猜。
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        wal_mode: bool = True,
        busy_timeout_ms: int = 5000,
        echo: bool = False,
    ) -> None:
        self._db_path = Path(db_path) if str(db_path) != ":memory:" else Path(":memory:")
        self._in_memory = str(db_path) == ":memory:"
        self._wal_mode = wal_mode
        self._busy_timeout_ms = busy_timeout_ms
        self._echo = echo
        self._engine: AsyncEngine | None = None
        self._sessionmaker: async_sessionmaker[AsyncSession] | None = None
        # Repository 容器在**构造期**就装配好，不等 initialize()：每个 Repository 握的是
        # `self.new_session` 这个工厂（见 `BaseRepository.__init__` 的理由），构造它们
        # 不需要连接池。以前它跟着 initialize() 走，于是 `storage.events` 在 lifespan
        # 之前拿不到 —— `create_app()` 正是那条路，生产启动直接抛
        # "还没 initialize()"（验收判据 2 因此当时是纸面的）。
        # 真正的门在第一次查询：`_require_sessionmaker()`，那条文案同时盖住 close()。
        self._repositories = _Repositories(self.new_session)

    # ---- 构造 ----

    @classmethod
    def in_memory(
        cls,
        *,
        wal_mode: bool = True,
        busy_timeout_ms: int = 5000,
        echo: bool = False,
    ) -> SqliteStorage:
        """内存库。**走 `create_all` 不走 Alembic**（快），
        与迁移路径的一致性由 `check_schema_matches_migrations()` 守着。

        这就是 docs/specs/data-model.md §4 里 `InMemoryStorage` 那一栏 ——
        没有做成第二个类，因为它与文件库的差别只有"URL + 建表方式"两处，
        复制一份实现等于把漂移的可能性也复制一份。

        三个参数是**手写镜像** `__init__` 的，没用 `**kwargs: Any` 转发：
        后者会把类型全擦掉，`in_memory(wal_mode="yes")` 也能过 mypy。
        `__init__` 加参数时这里不会自动跟上 —— 换来的是类型仍在。
        """
        return cls(":memory:", wal_mode=wal_mode, busy_timeout_ms=busy_timeout_ms, echo=echo)

    @classmethod
    def from_config(cls, config: AppConfig, root: Path | None = None) -> SqliteStorage:
        return cls(
            resolve_db_path(config, root),
            wal_mode=config.storage.wal_mode,
            busy_timeout_ms=config.storage.busy_timeout_ms,
        )

    # ---- 生命周期 ----

    @property
    def db_path(self) -> Path:
        return self._db_path

    async def initialize(self, *, migrate: bool = True) -> None:
        """建 engine（含 PRAGMA）、建表、装配 Repository。幂等。"""
        if self._engine is not None:
            return

        if self._in_memory:
            engine = create_async_engine(
                async_url(":memory:"),
                echo=self._echo,
                # 内存库必须共用同一条连接，否则每个 session 看到的是一个空的新库。
                poolclass=StaticPool,
            )
        else:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            if migrate:
                await asyncio.to_thread(run_migrations, self._db_path)
            engine = create_async_engine(async_url(self._db_path), echo=self._echo)

        self._engine = engine
        self._install_pragmas(engine)

        if self._in_memory:
            async with engine.begin() as connection:
                await connection.run_sync(metadata.create_all)

        self._sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        logger.info(
            "storage.initialized",
            db_path=str(self._db_path),
            in_memory=self._in_memory,
            wal=self._wal_mode and not self._in_memory,
        )

    def _install_pragmas(self, engine: AsyncEngine) -> None:
        """每条新连接都设 PRAGMA。**这些设置不是持久的**，漏了就静默降级。"""
        wal_mode = self._wal_mode and not self._in_memory
        busy_timeout = self._busy_timeout_ms

        @event.listens_for(engine.sync_engine, "connect")
        def _on_connect(dbapi_connection: DBAPIConnection, _record: object) -> None:
            cursor = dbapi_connection.cursor()
            try:
                # 外键默认关闭，且是 per-connection。不开 = CASCADE / SET NULL 静默失效。
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout)}")
                if wal_mode:
                    cursor.execute("PRAGMA journal_mode=WAL")
                    # WAL 下 NORMAL 是安全与速度的平衡点（FULL 会每条提交都 fsync）。
                    cursor.execute("PRAGMA synchronous=NORMAL")
            finally:
                cursor.close()

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
        self._sessionmaker = None

    async def healthcheck(self) -> bool:
        """`SELECT 1` 真往返。返回 False 而不是抛异常 —— preflight 要的是红绿灯。"""
        engine = self._require_engine()
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception:  # 健康检查不许把异常甩给调用方
            logger.exception("storage.healthcheck_failed", db_path=str(self._db_path))
            return False
        return True

    # ---- 事务 ----

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """跨 Repository 的事务。

        嵌套调用直接复用外层事务 —— SQLite 没有真正的嵌套事务，
        用 SAVEPOINT 假装支持会把"内层失败外层继续"这种语义引进来，
        而 V2 没有任何一处需要它。
        """
        if CURRENT_SESSION.get() is not None:
            yield
            return

        sessionmaker = self._require_sessionmaker()
        async with sessionmaker() as session, session.begin():
            token = CURRENT_SESSION.set(session)
            try:
                yield
            finally:
                CURRENT_SESSION.reset(token)

    # ---- Repository 访问 ----

    @property
    def platforms(self) -> PlatformRepository:
        return self._repositories.platforms

    @property
    def creators(self) -> CreatorRepository:
        return self._repositories.creators

    @property
    def videos(self) -> VideoRepository:
        return self._repositories.videos

    @property
    def transcripts(self) -> TranscriptRepository:
        return self._repositories.transcripts

    @property
    def task_runs(self) -> TaskRunRepository:
        return self._repositories.task_runs

    @property
    def events(self) -> EventRepository:
        return self._repositories.events

    @property
    def manifests(self) -> ManifestRepository:
        return self._repositories.manifests

    @property
    def video_comments(self) -> VideoCommentRepository:
        return self._repositories.video_comments

    @property
    def metrics(self) -> MetricSnapshotRepository:
        return self._repositories.metrics

    @property
    def sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        """给需要写自定义查询的调用方（分析层、迁移脚本）。"""
        return self._require_sessionmaker()

    # ---- 内部 ----

    def _require_engine(self) -> AsyncEngine:
        if self._engine is None:
            msg = "SqliteStorage 还没 initialize()"
            raise StorageError(msg)
        return self._engine

    def new_session(self) -> AsyncSession:
        """开一个新 session。Repository 握的是这个**方法**，不是 sessionmaker。"""
        return self._require_sessionmaker()()

    def _require_sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        """未初始化**或已关闭**时抛。同一个 None 判断盖两种情况：
        `close()` 就是把 `_sessionmaker` 置回 None，而 Repository 握的是本方法的引用。
        """
        if self._sessionmaker is None:
            msg = "SqliteStorage 不可用（还没 initialize()，或已经 close()）"
            raise StorageError(msg)
        return self._sessionmaker


class _Repositories:
    """Repository 的容器，随 `SqliteStorage` 一起装配。

    顶层导入（不是函数体内导入）：`CURRENT_SESSION` 已经搬到 `storage/session.py`
    这个叶子模块，`db → repositories.base → session` 是单向的，环已经断了。
    """

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        # 传工厂而不是对象：让 Repository 在 `close()` 之后立刻红，
        # 而不是握着一条已销毁的连接池去撞驱动层异常。理由见 `BaseRepository.__init__`。
        self.platforms = PlatformRepository(session_factory)
        self.creators = CreatorRepository(session_factory)
        self.videos = VideoRepository(session_factory)
        self.transcripts = TranscriptRepository(session_factory)
        self.task_runs = TaskRunRepository(session_factory)
        self.events = EventRepository(session_factory)
        self.manifests = ManifestRepository(session_factory)
        self.video_comments = VideoCommentRepository(session_factory)
        self.metrics = MetricSnapshotRepository(session_factory)
