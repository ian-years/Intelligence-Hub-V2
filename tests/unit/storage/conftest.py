"""存储层测试的共享 fixtures。

**同一批用例跑两种后端**（`storage` fixture 的 params）：

- `memory`：`metadata.create_all()`，不跑迁移，快；
- `file`：真 Alembic 迁移建出来的库，与生产完全同一条路
    （迁移本身全会话只跑一次当模板，见 `_migrated_template` —— 省的是重复，不是覆盖）。

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

import shutil
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from intelligence_hub_v2.models.creator import Creator, CreatorDraft
from intelligence_hub_v2.models.platform import PlatformRecord
from intelligence_hub_v2.models.video import Video, VideoDraft
from intelligence_hub_v2.storage.db import SqliteStorage, run_migrations

_TEMPLATE: list[Path] = []
"""全会话只建一次的迁移模板（放在 list 里是为了避免 `global` 声明）。

为什么不是 session-scoped fixture：本仓库的 asyncio 配置是
`asyncio_mode = "auto"` + `asyncio_default_fixture_loop_scope = "function"`，
而 pytest-asyncio 在 auto 模式下会把**每个** fixture 都包进协程 ——
session 作用域的 fixture 因此要求 session 事件循环，撞上
"`storage` 需要 session-scoped fixture，但它在 function 循环里"。
`tmp_path_factory` 本身是 session 作用域的，可以在 function fixture 里安全地
建一个跨用例存活的目录，所以用一个进程内缓存就够了。
"""


def _migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """**跑过一次迁移的空库，全会话只建一次**，给 `[file]` 档当起点。

    为什么需要：`run_migrations()` 在这台机器上要 ~0.7s（23 条 DDL 落盘，
    冷文件 + 杀软实时扫描把每条约 20ms 放大了）。`[file]` 档有 240 多个用例，
    每个都从零迁一遍就是三分钟只为了"库是空的且在 head"。

    这不是把迁移跳过 —— 起点仍然是**真迁移**建出来的库，迁移写错了这里照样全红；
    只是不再重复 240 遍。真正从头验证"空库能不能建出来 + 可不可逆"的用例
    在 `test_migrations.py` 里，它们**故意不用这个模板**。
    """
    if _TEMPLATE and _TEMPLATE[0].is_file():
        return _TEMPLATE[0]

    path = tmp_path_factory.mktemp("migrated-template") / "template.sqlite3"
    run_migrations(path)
    for suffix in ("-wal", "-shm"):  # 别让 WAL 残留跟着一起被 copy 出去
        leftover = Path(f"{path}{suffix}")
        if leftover.exists():
            leftover.unlink()
    _TEMPLATE[:] = [path]
    return path


@pytest.fixture(params=["memory", "file"])
async def storage(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[SqliteStorage]:
    """一个干净的库。`file` 档从**已迁移**的模板复制而来，仍走生产同一条读库路径。"""
    if request.param == "memory":
        instance = SqliteStorage.in_memory()
    else:
        db = tmp_path / "test.sqlite3"
        shutil.copy(_migrated_template(tmp_path_factory), db)
        instance = SqliteStorage(db)
    # 文件档不再跑一遍迁移：模板就是同一条 `run_migrations()` 建出来的，
    # 而"空库能不能从头建出来 / 可不可逆 / initialize 会不会真去迁移"
    # 这三件事在 test_migrations.py 里各有专责用例（它们故意不用这个模板）。
    # 少这一次 alembic 加载 ≈ 0.3s × 240 个文件档用例。
    await instance.initialize(migrate=request.param == "memory")
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
