"""存储层测试的共享 fixtures。

**同一批用例跑两种后端**（`storage` fixture 的 params）：

- `memory`：`metadata.create_all()`，不跑迁移，快；
- `file`：真 Alembic 迁移，与生产完全同一条路。

为什么两个都要：内存库不执行迁移文件，所以"迁移写错了 / 忘了写"这类问题
在只测内存库时**永远发现不了**；而只测文件库又会让 `check_schema_matches_migrations()`
这条漂移看护失去意义 —— 它比对的正是这两条路径的产物。
`tests/unit/storage/test_migrations.py` 里有专门一条用例把两者钉在一起。

**外键是真开着的**：`creators.platform` → `platforms.name`，
所以插博主前必须先 `platforms.upsert()`（用 `platform_row` fixture）。
这不是测试的样板负担，而是 `PRAGMA foreign_keys=ON` 真的生效的证据 ——
V1 那种"外键写了但 SQLite 默认关着，所以从来没约束过任何东西"的情况在这里会直接红。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from intelligence_hub_v2.models.creator import Creator, CreatorDraft
from intelligence_hub_v2.models.platform import PlatformRecord
from intelligence_hub_v2.models.video import Video, VideoDraft
from intelligence_hub_v2.storage.db import SqliteStorage


@pytest.fixture(params=["memory", "file"])
async def storage(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[SqliteStorage]:
    """一个干净的库。`file` 档跑真迁移（tmp_path 由 pytest 自动清理）。"""
    if request.param == "memory":
        instance = SqliteStorage.in_memory()
    else:
        instance = SqliteStorage(tmp_path / "test.sqlite3")
    await instance.initialize()
    try:
        yield instance
    finally:
        await instance.close()


@pytest.fixture
async def memory_storage() -> AsyncIterator[SqliteStorage]:
    """只跑内存库的 fixture（给不关心迁移路径的纯逻辑用例，省一次 alembic）。"""
    instance = SqliteStorage.in_memory()
    await instance.initialize()
    try:
        yield instance
    finally:
        await instance.close()


@pytest.fixture
async def platform_row(storage: SqliteStorage) -> PlatformRecord:
    """`douyin` 平台行。`creators.platform` 的外键要求它先存在。"""
    return await storage.platforms.upsert("douyin", enabled=True, config={"display_name": "抖音"})


@pytest.fixture
async def creator_row(storage: SqliteStorage, platform_row: PlatformRecord) -> Creator:
    """一个已入库的抖音博主。"""
    return await storage.creators.insert(
        CreatorDraft(
            platform=platform_row.name,
            platform_id="MS4wLjABAAAA-test-sec-uid",
            name="姜胡说",
            profile_url="https://www.douyin.com/user/MS4wLjABAAAA-test-sec-uid",
        )
    )


def _make_video_draft(
    platform_video_id: str,
    *,
    platform: str = "douyin",
    title: str = "默认标题",
    **overrides: object,
) -> VideoDraft:
    """造一条 `VideoDraft`。测试里到处要用，抽出来省得每条都写八个字段。"""
    payload: dict[str, object] = {
        "platform": platform,
        "platform_video_id": platform_video_id,
        "title": title,
    }
    payload.update(overrides)
    return VideoDraft.model_validate(payload)


@pytest.fixture
def make_video_draft() -> Callable[..., VideoDraft]:
    """`_make_video_draft` 的 fixture 版本。

    **为什么绕一道 fixture**：`tests/` 底下没有 `__init__.py`（与既有的
    `tests/unit/core/` 一致），pytest 会把 `tests/unit/storage` 的**父目录**
    插进 `sys.path`，所以 `from tests.unit.storage.conftest import ...` 是 ImportError。
    要么给三层目录都补 `__init__.py`（改变全仓库的测试布局约定），
    要么用 fixture —— 后者影响面小得多。
    """
    return _make_video_draft


@pytest.fixture
async def video_row(storage: SqliteStorage, creator_row: Creator) -> Video:
    """一条已入库的抖音作品（挂在 `creator_row` 上）。

    `transcripts.video_id` 的外键要求它先存在，而且 `transcripts` 是
    `ON DELETE CASCADE` —— 删作品时口播稿必须跟着走，这条 fixture 是那个用例的起点。
    """
    return await storage.videos.insert(
        VideoDraft(
            platform=creator_row.platform,
            platform_video_id="7642363455722229042",
            title="测试作品",
            creator_id=creator_row.id,
            media_path="media/douyin/姜胡说/7642363455722229042-测试作品/video.mp4",
            media_source="yt_dlp",
        )
    )
