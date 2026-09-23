"""每一条 DML 路径都要把 SQLite 的 `IntegrityError` 翻成本仓库的异常族。

`repositories/base.py:_translate_integrity()` 存在的理由写在它自己的 docstring 里：
API 层要把"重复收录"映射成 409、"外键不成立"映射成 400/500，而 `IntegrityError`
是**一个类型装三种语义**。

问题出在接线位置：翻译原先只在各 Repository 的 INSERT/DELETE 外面 `try/except`，
**UPDATE 路径整片漏了**。经验 11 说"漏翻译是一整类 bug，凡会撞约束的写方法一律过一遍"，
而那次过的方法清单是按**动词**（insert/delete）切的，不是按**是否发 DML** 切的。

最疼的一处是 `platforms.prune_unknown()`：它由 lifespan 在**启动期**调用
（`main.py:116`），而它自己的兄弟方法 `delete()` 明确翻译了同一种 FK-RESTRICT 情况
并写了原因。结果是"装过 V2.1 再退回 V2.0"这个它专为写出来的场景下，
服务**起不来**且给的是一句裸 SQLAlchemy traceback。

解法是把翻译上提到 `_scope()`（唯一等入口），而不是在九个方法上各补一个 `try` ——
后者一定会漏第十个。这一文件的用例因此钉的是**结论**（拿到的是本仓库异常、
且带着原文），不是"有没有 except 块"。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy.exc import IntegrityError

from intelligence_hub_v2.errors import ConflictError, StorageError
from intelligence_hub_v2.models.creator import CreatorDraft
from intelligence_hub_v2.models.video import VideoDraft

if TYPE_CHECKING:
    from intelligence_hub_v2.models.creator import Creator
    from intelligence_hub_v2.models.video import Video
    from intelligence_hub_v2.storage.db import SqliteStorage


def _video_kwargs(creator: Creator) -> dict[str, object]:
    return {
        "platform": creator.platform,
        "platform_video_id": "v-integrity-1",
        "creator_id": creator.id,
        "title": "原标题",
    }


async def test_update_fields_translates_a_check_violation(
    storage: SqliteStorage, creator_row: Creator
) -> None:
    """`videos.media_source` 有 CHECK 枚举。写一个枚举外的值必须是本仓库异常。"""
    video = await storage.videos.insert(VideoDraft(**_video_kwargs(creator_row)))
    with pytest.raises(StorageError, match="CHECK 约束") as got:
        await storage.videos.update_fields(video.id, media_source="migrated_from_v1")
    assert not isinstance(got.value, IntegrityError)


async def test_update_fields_translates_a_not_null_violation(
    storage: SqliteStorage, creator_row: Creator
) -> None:
    video: Video = await storage.videos.insert(VideoDraft(**_video_kwargs(creator_row)))
    with pytest.raises(StorageError, match="缺必填字段"):
        await storage.videos.update_fields(video.id, title=None)


async def test_update_fields_translates_a_foreign_key_violation(
    storage: SqliteStorage, creator_row: Creator
) -> None:
    video = await storage.videos.insert(VideoDraft(**_video_kwargs(creator_row)))
    with pytest.raises(StorageError, match="外键不成立"):
        await storage.videos.update_fields(video.id, creator_id=999_999)


async def test_creators_update_fields_is_covered_too(
    storage: SqliteStorage, creator_row: Creator
) -> None:
    """不能只修 videos：同形的 UPDATE 散在好几个 Repository。"""
    with pytest.raises(StorageError, match="缺必填字段"):
        await storage.creators.update_fields(creator_row.id, metadata_json=None)


async def test_prune_unknown_reports_a_cleanable_problem_not_a_traceback(
    storage: SqliteStorage, creator_row: Creator
) -> None:
    """启动期那条：`platforms` 只剩 bilibili，而 douyin 行下还挂着博主。

    这正是 `prune_unknown` 自己的 docstring 说的场景（"跑过 V2.1、换回 V2.0"）。
    裸 `IntegrityError` 会让 `_lifespan` 当场炸、服务起不来；必须是本仓库异常，
    并且**原文要留着** —— 只说"失败了"等于让人去猜是哪张表的外键。
    """
    with pytest.raises(StorageError, match="外键不成立"):
        await storage.platforms.prune_unknown(["bilibili"])


async def test_translation_also_applies_inside_an_explicit_transaction(
    storage: SqliteStorage, creator_row: Creator
) -> None:
    """`storage.transaction()` 里走的是 ambient session 分支，是另一条代码路径。

    约束在这里是**立即**违例的（本 schema 没有 DEFERRABLE 外键），所以违例发生在
    `execute()` 当场、还在 Repository 的 `_scope()` 之内；上提到 `_scope()` 之后
    两条分支都要过翻译。
    """
    video = await storage.videos.insert(VideoDraft(**_video_kwargs(creator_row)))
    with pytest.raises(StorageError, match="CHECK 约束"):
        async with storage.transaction():
            await storage.videos.update_fields(video.id, media_source="nope")


async def test_a_unique_violation_still_becomes_a_conflict(
    storage: SqliteStorage, creator_row: Creator
) -> None:
    """回归看护：上提翻译之后，409 那一档**不许**被顺手降级成 `StorageError`。

    它是 API 层 `_register_exception_handlers` 映射 409 的唯一依据；
    一旦退化，"重复收录"就变成 500。
    """
    duplicate = CreatorDraft(
        platform=creator_row.platform,
        platform_id=creator_row.platform_id,
        name="同一个人第二次",
        profile_url=creator_row.profile_url,
    )
    with pytest.raises(ConflictError):
        await storage.creators.insert(duplicate)
