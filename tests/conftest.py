"""全局共享 fixtures 与最小假件。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from intelligence_hub_v2.core.config import AppConfig
from intelligence_hub_v2.storage.db import SqliteStorage
from intelligence_hub_v2.storage.files import FileStorage


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def storage() -> AsyncIterator[SqliteStorage]:
    """内存库，初始化后预置四个平台的运行态镜像行。

    `creators.platform` / `videos.platform` 都外键到 `platforms`（平台是受管实体，
    V1 §7.12 之后的设计），不先建行插博主/作品就 `FOREIGN KEY constraint failed`。
    采集/入库类用例到处都要这一步，收成一份公共 fixture。

    注意：`tests/unit/storage/` 与部分 core 用例自己定义了同名 `storage`，
    模块级 fixture 覆盖 conftest —— 它们各自的建库路径不受这里影响。
    """
    store = SqliteStorage.in_memory()
    await store.initialize()
    for name in ("douyin", "bilibili", "xiaohongshu", "youtube"):
        await store.platforms.upsert(name, enabled=True)
    yield store
    await store.close()


@pytest.fixture
def files(tmp_path: Path) -> FileStorage:
    fs = FileStorage(tmp_path / "data")
    fs.ensure_dirs()
    return fs


@pytest.fixture
def app_config() -> AppConfig:
    return AppConfig()
