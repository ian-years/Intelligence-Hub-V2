"""Repository 基类：session 作用域与行→模型映射。

**为什么所有写操作都走 `_scope()` 而不是各自开 session**：
`SqliteStorage.transaction()` 需要跨 Repository 的原子性（例如"写视频 + 写清单 +
写事件"要么全成要么全不成）。实现方式是 ContextVar（`CURRENT_SESSION`）：
外层事务里就复用那个 session 且**不 commit**，否则各开各的短事务并立即 commit。

Repository 里看不到这个分支 —— 都藏在 `_scope()` 里，所以不会因为
"某个方法忘了判断"而破坏事务语义。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, NoReturn, TypeVar, cast

from pydantic import BaseModel
from sqlalchemy.engine import CursorResult, RowMapping
from sqlalchemy.engine.result import Result
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from intelligence_hub_v2.errors import ConflictError, NotFoundError, StorageError
from intelligence_hub_v2.storage.session import CURRENT_SESSION

ModelT = TypeVar("ModelT", bound=BaseModel)


def affected_rows(result: Result[Any]) -> int:
    """DML 的影响行数。

    `AsyncSession.execute()` 的返回类型被声明成 `Result[Any]`，而 `rowcount`
    与 `inserted_primary_key` 只存在于它的子类 `CursorResult` 上 —— 于是 mypy
    在每个 Repository 的写方法上都报一句 "Result has no attribute rowcount"。

    这里用**一个**带注释的 `cast` 换掉二十多处 `# type: ignore`：
    转换是可靠的，因为 SQLAlchemy 对 DML 语句实际返回的就是 `CursorResult`
    （2026-09-22 本机实测 `type(await session.execute(update(...))).__name__`
    == `'CursorResult'`，`isinstance(..., CursorResult)` 为 True）。
    之所以不直接 `isinstance` 断言：那样每个调用点都要多一行没用的运行时检查。

    返回 `int(...)` 而不是 `.rowcount`：后者在类型上是 `int`，但 SQLite 对某些
    语句会给 `-1`（"不知道"）。让调用方拿到一个非负数是这里该做的事。
    """
    return max(0, int(cast("CursorResult[Any]", result).rowcount))


def inserted_id(result: Result[Any]) -> int:
    """自增主键。同上，`inserted_primary_key` 也只在 `CursorResult` 上。

    空值抛 `StorageError` 而不是 `assert` / 直接 `[0]`：
    这个函数只该用在 INSERT 上，用错了是编程错误，
    但症状（`TypeError: 'NoneType' object is not subscriptable`）离原因很远。
    """
    keys = cast("CursorResult[Any]", result).inserted_primary_key
    if not keys:
        msg = "inserted_id() 用在了不是 INSERT 的语句上（inserted_primary_key 为空）"
        raise StorageError(msg)
    return int(keys[0])


class BaseRepository:
    """所有 Repository 的基类。

    子类要设 `entity`（用于 `NotFoundError` 的文案）与 `_row_to_model`。
    """

    entity: str = "record"

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        """收的是**工厂**（`SqliteStorage._require_sessionmaker`），不是 sessionmaker 本身。

        差别只在关闭之后：握着对象的 Repository 在 `storage.close()` 之后仍能发起查询，
        症状是驱动层一句裸 `sqlite3.OperationalError`（连接池已经 dispose 了），
        而 API 层只能回一个看不懂的 500。握工厂的话，`_require_sessionmaker()`
        当场抛本仓库的 `StorageError` —— 与"还没 initialize"是同一条判断。

        这条在 V2.0 里会真的踩到：Task 9 的 lifespan 关闭 storage 时，
        可能还有一个任务在往事件流里写日志。
        """
        self._session_factory = session_factory

    @asynccontextmanager
    async def _scope(self) -> AsyncIterator[AsyncSession]:
        """拿到一个可用的 session。

        - 在外层事务里：直接复用，**不 commit / 不 close**（交给外层）。
        - 不在事务里：开一个短事务，正常退出即 commit，异常即 rollback。
        """
        ambient = CURRENT_SESSION.get()
        if ambient is not None:
            yield ambient
            return
        async with self._session_factory() as session, session.begin():
            yield session

    # ---- 行 → Pydantic ----

    @staticmethod
    def _to_model(row: RowMapping | None, model: type[ModelT]) -> ModelT | None:
        """`RowMapping`（`.mappings()` 的产物）而不是 `Mapping[str, Any]`。

        两者读起来是一回事，但 SQLAlchemy 的 `RowMapping` 并不继承
        `collections.abc.Mapping`，所以写宽的那个会让 mypy 在每个调用点报一句
        "incompatible type RowMapping"。这里按**实际类型**收，调用点就不用 cast。
        """
        if row is None:
            return None
        return model.model_validate(dict(row))

    def _not_found(self, key: object) -> NoReturn:
        """抛 `NotFoundError`。

        单独一个 `NoReturn` 方法而不是"查不到就 `_or_raise`"：
        后者 mypy 推不出"调用之后 row 一定不是 None"，于是调用点还得再判一次。
        """
        raise NotFoundError(self.entity, key)

    # ---- 错误翻译 ----

    def _translate_integrity(self, exc: IntegrityError) -> StorageError:
        """把 SQLite 的 `IntegrityError` 翻成本仓库的异常族。

        **为什么不让它直接冒出去**：API 层要把"重复收录"映射成 409、
        "外键不成立"映射成 400/500，而 `IntegrityError` 是一个类型装三种语义。
        不翻译就只能一律 500，用户看到的是一屏 SQLAlchemy traceback。

        判据用错误原文里的子串 —— SQLite 这几句是稳定的（`UNIQUE constraint failed`
        / `FOREIGN KEY constraint failed` / `CHECK constraint failed`）。
        V1 §7.15 那条 `looks_like_cookie_failure()` 是同一类做法：认原文，别猜。
        """
        detail = str(exc.orig)
        if "UNIQUE constraint failed" in detail:
            return ConflictError(f"{self.entity} 已存在（{detail}）")
        if "FOREIGN KEY constraint failed" in detail:
            return StorageError(f"{self.entity} 的外键不成立: {detail}")
        if "CHECK constraint failed" in detail:
            return StorageError(f"{self.entity} 违反 CHECK 约束: {detail}")
        if "NOT NULL constraint failed" in detail:
            return StorageError(f"{self.entity} 缺必填字段: {detail}")
        return StorageError(f"{self.entity} 写入被拒: {detail}")
