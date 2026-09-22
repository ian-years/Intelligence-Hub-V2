"""`CreatorRepository` 测试。

看护三条 V1 陷阱：
- **§7.11**「列名 `creator_id`，外部数据叫 `creator_platform_id` / `mid`」
  → 只有 `platform_id` 一个名字；
- **§7.24**「`is_tracking` 必须真布尔，默认值只能有一处」→ `set_tracking()` 拒非布尔；
- **§7.22**「按位任务用了'跟踪开关筛全库'」→ `list_tracked()` 与 `find()` 是两条路，用例钉住差别。

外键：`creators.platform` → `platforms.name`（`ON DELETE RESTRICT`），
所以每个用例都要先有 `platform_row`。
"""

from __future__ import annotations

import pytest

from intelligence_hub_v2.errors import ConflictError, NotFoundError, StorageError
from intelligence_hub_v2.models.creator import CreatorDraft
from intelligence_hub_v2.models.platform import PlatformRecord
from intelligence_hub_v2.models.video import VideoDraft
from intelligence_hub_v2.storage.db import SqliteStorage


def _draft(platform_id: str, *, name: str = "测试博主", **overrides: object) -> CreatorDraft:
    payload: dict[str, object] = {
        "platform": "douyin",
        "platform_id": platform_id,
        "name": name,
        "profile_url": f"https://www.douyin.com/user/{platform_id}",
    }
    payload.update(overrides)
    return CreatorDraft.model_validate(payload)


# ---------------------------------------------------------------------------
# 基本读写
# ---------------------------------------------------------------------------


async def test_insert_and_find(storage: SqliteStorage, platform_row: PlatformRecord) -> None:
    created = await storage.creators.insert(_draft("sec-uid-1", name="姜胡说"))
    assert created.id > 0
    assert created.is_tracking is True  # server_default='1'
    assert created.metadata_json == "{}"

    found = await storage.creators.find("douyin", "sec-uid-1")
    assert found is not None
    assert found.name == "姜胡说"
    # 平台是身份的一部分：换个平台就查不到
    assert await storage.creators.find("bilibili", "sec-uid-1") is None


async def test_insert_requires_the_platform_to_exist(storage: SqliteStorage) -> None:
    """**外键真的开着**（`PRAGMA foreign_keys=ON`）。

    没注册的平台写不进 `creators` —— 翻成 `StorageError` 而不是裸 `IntegrityError`，
    因为 API 层要能把它映射成 4xx（用户传了个不支持的平台）而不是 500。
    """
    with pytest.raises(StorageError):
        await storage.creators.insert(_draft("orphan"))


async def test_insert_duplicate_identity_raises_conflict(
    storage: SqliteStorage, platform_row: PlatformRecord
) -> None:
    await storage.creators.insert(_draft("dup"))
    with pytest.raises(ConflictError):
        await storage.creators.insert(_draft("dup", name="另一个名字"))


async def test_insert_or_get_does_not_overwrite(
    storage: SqliteStorage, platform_row: PlatformRecord
) -> None:
    first, created = await storage.creators.insert_or_get(_draft("idem", name="甲"))
    assert created is True
    second, created_again = await storage.creators.insert_or_get(_draft("idem", name="乙"))
    assert created_again is False
    assert second.id == first.id
    assert second.name == "甲"


async def test_get_or_raise_on_missing(storage: SqliteStorage) -> None:
    with pytest.raises(NotFoundError):
        await storage.creators.get_or_raise(999999)


# ---------------------------------------------------------------------------
# V1 §7.24：is_tracking
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, 1, "false", "true", None, "", []])
async def test_set_tracking_rejects_non_bool(
    storage: SqliteStorage, platform_row: PlatformRecord, bad: object
) -> None:
    """**V1 §7.24 回归看护**：入口就断言真布尔。

    V1 里 `is_tracking` 落成 `0` / `"false"` 时，"日更采集"（认 `is False`）
    与"按位抓取"（认 `checkbox_enabled`）会读出**相反**的结果 ——
    而且要到第二天定时任务跑起来才暴露。
    前端 JSON 传字符串是这条最现实的触发路径。
    """
    creator = await storage.creators.insert(_draft("track-me"))
    with pytest.raises(TypeError, match="bool"):
        await storage.creators.set_tracking(creator.id, bad)  # type: ignore[arg-type]


async def test_set_tracking_roundtrips_both_ways(
    storage: SqliteStorage, platform_row: PlatformRecord
) -> None:
    creator = await storage.creators.insert(_draft("toggle"))
    assert creator.is_tracking is True

    off = await storage.creators.set_tracking(creator.id, False)
    assert off.is_tracking is False
    # 读回来必须是 Python bool，不是 0/1（否则前端 `=== false` 判不出）
    assert (await storage.creators.get(creator.id)).is_tracking is False

    on = await storage.creators.set_tracking(creator.id, True)
    assert on.is_tracking is True


async def test_update_fields_refuses_to_touch_tracking(
    storage: SqliteStorage, platform_row: PlatformRecord
) -> None:
    """`is_tracking` 不在 `CreatorUpdatableFields` 里 —— 否则就有第二个不设防的写入口。"""
    creator = await storage.creators.insert(_draft("guarded"))
    with pytest.raises(TypeError, match="set_tracking"):
        await storage.creators.update_fields(creator.id, is_tracking=False)  # type: ignore[typeddict-item]


async def test_set_tracking_on_missing_row_raises(
    storage: SqliteStorage, platform_row: PlatformRecord
) -> None:
    with pytest.raises(NotFoundError):
        await storage.creators.set_tracking(999999, False)


# ---------------------------------------------------------------------------
# V1 §7.22：list_tracked 与 find 是两条路
# ---------------------------------------------------------------------------


async def test_list_tracked_excludes_switched_off_but_find_still_hits(
    storage: SqliteStorage, platform_row: PlatformRecord
) -> None:
    """**V1 §7.22 回归看护**：点名抓某一位博主时不许看开关。

    那次事故是「🔥 抓取爆款 Top 5」按钮点了某一位博主，
    而采集器用的是"跟踪开关筛全库"，两位 B站 博主开关都关着 → 拿到 0 条 →
    `RuntimeError` 一路甩成 traceback，清单停在没有 `status` 的初稿。
    """
    on = await storage.creators.insert(_draft("on", name="开着的"))
    off = await storage.creators.insert(_draft("off", name="关掉的"))
    await storage.creators.set_tracking(off.id, False)

    tracked = await storage.creators.list_tracked()
    assert [c.id for c in tracked] == [on.id]

    # 按位路径：开关关着照样点名拿到
    named = await storage.creators.find("douyin", "off")
    assert named is not None
    assert named.id == off.id
    assert named.is_tracking is False


async def test_list_all_includes_untracked(
    storage: SqliteStorage, platform_row: PlatformRecord
) -> None:
    await storage.creators.insert(_draft("a", name="A"))
    off = await storage.creators.insert(_draft("b", name="B"))
    await storage.creators.set_tracking(off.id, False)

    everyone = await storage.creators.list_all()
    assert len(everyone) == 2
    assert await storage.creators.count() == 2
    assert await storage.creators.count(tracked_only=True) == 1


async def test_list_all_filters_by_platform(
    storage: SqliteStorage, platform_row: PlatformRecord
) -> None:
    await storage.platforms.upsert("bilibili", enabled=True)
    await storage.creators.insert(_draft("dy", name="抖音博主"))
    await storage.creators.insert(
        CreatorDraft(
            platform="bilibili",
            platform_id="12345",
            name="B站UP主",
            profile_url="https://space.bilibili.com/12345",
        )
    )
    assert len(await storage.creators.list_all(platform="bilibili")) == 1
    assert await storage.creators.count(platform="douyin") == 1


async def test_list_all_is_ordered(storage: SqliteStorage, platform_row: PlatformRecord) -> None:
    """排序稳定：前端博主列表不该每次刷新换顺序。"""
    for name in ("丙", "甲", "乙"):
        await storage.creators.insert(_draft(f"o-{name}", name=name))
    names = [c.name for c in await storage.creators.list_all()]
    assert names == sorted(names)


# ---------------------------------------------------------------------------
# V1 §7.4：字段级更新
# ---------------------------------------------------------------------------


async def test_update_fields_does_not_clobber_other_fields(
    storage: SqliteStorage, platform_row: PlatformRecord
) -> None:
    """**V1 §7.4 回归看护**（博主侧）。

    V1 的 `LocalStore.upsert_creator` 是整行覆盖：只想改粉丝数，
    结果 `name` 被折成 `platform_id`、`metadata_json` 被刷成 `'{}'`。
    """
    creator = await storage.creators.insert(
        _draft("u1", name="姜胡说", follower_count=1000, metadata_json='{"bio":"简介"}')
    )

    updated = await storage.creators.update_fields(creator.id, follower_count=2000)

    assert updated.follower_count == 2000
    assert updated.name == "姜胡说"
    assert updated.metadata_json == '{"bio":"简介"}'
    assert updated.profile_url == creator.profile_url


async def test_update_fields_rejects_unknown_field(
    storage: SqliteStorage, platform_row: PlatformRecord
) -> None:
    creator = await storage.creators.insert(_draft("u2"))
    with pytest.raises(TypeError, match="nonexistent"):
        await storage.creators.update_fields(creator.id, nonexistent="x")  # type: ignore[typeddict-item]


@pytest.mark.parametrize("field", ["platform", "platform_id", "created_at"])
async def test_update_fields_rejects_identity_fields(
    storage: SqliteStorage, platform_row: PlatformRecord, field: str
) -> None:
    creator = await storage.creators.insert(_draft("u3"))
    with pytest.raises(TypeError):
        await storage.creators.update_fields(creator.id, **{field: "x"})  # type: ignore[typeddict-item, arg-type]


async def test_update_fields_with_no_fields_is_a_noop(
    storage: SqliteStorage, platform_row: PlatformRecord
) -> None:
    creator = await storage.creators.insert(_draft("u4", name="别动我"))
    same = await storage.creators.update_fields(creator.id)
    assert same.name == "别动我"
    assert same.updated_at == creator.updated_at


# ---------------------------------------------------------------------------
# 删除与关联统计
# ---------------------------------------------------------------------------


async def test_delete_is_idempotent_and_reports(
    storage: SqliteStorage, platform_row: PlatformRecord
) -> None:
    creator = await storage.creators.insert(_draft("del"))
    assert await storage.creators.delete(creator.id) is True
    assert await storage.creators.delete(creator.id) is False
    assert await storage.creators.get(creator.id) is None


async def test_platform_with_creators_cannot_be_deleted(
    storage: SqliteStorage, platform_row: PlatformRecord
) -> None:
    """`ON DELETE RESTRICT`：库里还有这个平台的博主时，平台行删不掉。

    V2.0 只有抖音/B站，"平台退出"这条路在可预见的版本里走不到；
    真走到了也必须先处置博主数据，而不是让它变成一个孤儿外键。
    """
    await storage.creators.insert(_draft("blocking"))
    with pytest.raises(StorageError):
        await storage.platforms.delete("douyin")


async def test_video_count_respects_hidden(
    storage: SqliteStorage, platform_row: PlatformRecord
) -> None:
    creator = await storage.creators.insert(_draft("counter"))
    first = await storage.videos.insert(
        VideoDraft(platform="douyin", platform_video_id="vc1", title="甲", creator_id=creator.id)
    )
    await storage.videos.insert(
        VideoDraft(platform="douyin", platform_video_id="vc2", title="乙", creator_id=creator.id)
    )
    await storage.videos.hide(first.id, reason="不想看")

    assert await storage.creators.video_count(creator.id) == 1
    assert await storage.creators.video_count(creator.id, include_hidden=True) == 2
