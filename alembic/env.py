"""Alembic 环境。

四条纪律（都不是模板默认的）：

1. **同步 engine**。业务代码走 aiosqlite，但迁移是启动前的一次性动作，
   用同步 pysqlite 更简单也更少坑（async alembic 要求 `asyncio.run`，
   而 `SqliteStorage.initialize()` 本身就在事件循环里，套一层会炸）。
2. **`render_as_batch=True`**。SQLite 的 `ALTER TABLE` 只支持 RENAME 和 ADD COLUMN，
   改列类型 / 删列 / 加约束都得靠"建新表 → 拷数据 → 改名"。不开 batch 模式，
   autogenerate 出来的迁移在 SQLite 上直接语法错。
3. **URL 不在 ini 里**，按 `alembic.ini` 顶部注释的三级顺序解析。
4. **`alembic.ini` 必须纯 ASCII**，中文说明只能写在这里（`.py` 文件永远按 UTF-8 读）。
   Alembic 用 `encoding="locale"` 读 ini，中文 Windows 上那是 GBK，
   于是任何 UTF-8 中文注释都会让**每一条 alembic 命令**在解析第一个选项之前就
   `UnicodeDecodeError`。本机实测原文见 docs/lessons.md 坑 8。
"""

from __future__ import annotations

import os
from logging.config import dictConfig
from pathlib import Path
from typing import Any

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy import engine_from_config, pool

from intelligence_hub_v2.storage.schema import UTCDateTime
from intelligence_hub_v2.storage.schema import metadata as target_metadata

# Alembic 的 Config 对象，提供对 alembic.ini 中参数的访问。
config = context.config

# 本仓库用 structlog，不用 alembic.ini 里的 fileConfig（那需要 [loggers] 段）。
# 但 alembic 自己的 logger 不能完全静音 —— 迁移失败时要看得见原因。
#
# **只在 CLI 调用时配**：`SqliteStorage.initialize()` 也会跑迁移，而它在
# `setup_logging()` **之后**执行 —— 无条件 dictConfig 会把 structlog 的
# processor 链与 RotatingFileHandler 全部冲掉，于是"服务起来了、日志格式却变了"。
# 程序化调用方通过 `cfg.attributes["configure_logger"] = False` 关掉这一步
# （见 `storage/db.py:_alembic_config`）。
if config.attributes.get("configure_logger", True):
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"plain": {"format": "%(levelname)-5.5s [%(name)s] %(message)s"}},
            "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
            "root": {"handlers": ["console"], "level": "WARNING"},
            "loggers": {"alembic": {"handlers": ["console"], "level": "INFO", "propagate": False}},
        }
    )


def _resolve_url() -> str:
    """三级解析目标库 URL。

    优先级（高 → 低）：
    1. 调用方显式塞进 Alembic Config 的 `sqlalchemy.url`
       —— `SqliteStorage.initialize()` 与测试走这条，避免"跑迁移却迁到默认库"。
    2. 环境变量 `INTELLIGENCE_HUB_STORAGE__SQLITE_URL` —— CI 与命令行临时改库。
    3. `config/app.yaml` 的 `data.dir` + `storage.sqlite_file` —— 正常开发。
    """
    explicit = config.get_main_option("sqlalchemy.url")
    if explicit:
        return explicit

    from_env = os.environ.get("INTELLIGENCE_HUB_STORAGE__SQLITE_URL")
    if from_env:
        return from_env

    # 延迟导入，两个原因分开说：
    # - `core.config`：分层。依赖方向是 `api → core → platforms → infra → storage`，
    #   写在文件顶部等于让"迁移环境"每次加载都往上一层引，
    #   而只有第 3 级回落需要它 —— 第 1、2 级（应用启动与全部测试走的那两条）用不到。
    # - `storage.db`：本模块**就是被 alembic 加载的**，而 `storage.db` 顶层会
    #   import `alembic.command` 与七个 Repository。迁移环境不需要那一整套装配，
    #   把它拉进来只会在 `alembic current` 这种只读命令上也付一遍导入成本。
    from intelligence_hub_v2.core.config import ConfigManager  # noqa: PLC0415
    from intelligence_hub_v2.storage.db import resolve_db_path  # noqa: PLC0415

    manager = ConfigManager()
    app_config = manager.load()
    db_path = resolve_db_path(app_config)
    return _path_to_url(db_path)


def _path_to_url(db_path: Path) -> str:
    """把文件路径转成 SQLAlchemy URL。

    Windows 上必须用 `as_posix()` 并转义反斜杠 —— 直接 f-string 一个
    `E:\\08-Codework\\...` 进去，SQLAlchemy 会把 `\\0` 当转义序列。
    """
    return f"sqlite+pysqlite:///{db_path.resolve().as_posix()}"


def _render_item(name: str, obj: object, autogen_context: AutogenContext) -> str | bool:  # noqa: ARG001
    """把自定义列类型渲染成等价的 `sa.*` 字面量。返回 `False` = 用默认渲染。

    **为什么需要它**：Alembic 的默认渲染对 `TypeDecorator` 子类输出的是
    `repr()` 结果，也就是 `intelligence_hub_v2.storage.schema.UTCDateTime()` ——
    一个**全限定名**，而 `script.py.mako` 生成的迁移文件里并没有那句 import。
    于是 `alembic upgrade head` 直接 `NameError: name 'intelligence_hub_v2' is not defined`，
    而 `alembic revision` 自己是成功的（本机实测，见 docs/lessons.md 坑 8）。

    渲染成 `sa.DateTime()` 是**等价**的而不是近似：`UTCDateTime.impl` 就是 `DateTime`，
    DDL 完全一致。UTC 的进出转换发生在 Python 侧的
    `process_bind_param` / `process_result_value`，与建表语句无关 ——
    迁移文件里根本不需要那个类。
    """
    if name == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime()"
    return False


def run_migrations_offline() -> None:
    """`--sql` 模式：只打印 SQL，不连库。

    V3 换 PostgreSQL 时这条路径是"拿迁移 SQL 给 DBA 审"的入口，别删。
    """
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
        render_item=_render_item,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """连真库跑迁移。"""
    configuration: dict[str, Any] = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _resolve_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
            # SQLite 的反射拿不到 CHECK 约束的原始文本，比对时会误报"约束被删了"。
            # 关掉 compare_server_default，靠人工审迁移文件。
            compare_server_default=False,
            render_item=_render_item,
        )

        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
