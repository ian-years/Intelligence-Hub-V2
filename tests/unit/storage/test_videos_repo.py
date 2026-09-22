"""`VideoRepository` 测试。

看护两条 V1 陷阱：
- **§7.4**「`upsert_video()` 整行覆盖，空标题被折叠成默认值」→ `update_fields()` 只 SET 传入的列；
- **§7.25**「删除 = 删库行 + 墓碑，少一半都不算数」→ `hide()` 内化墓碑，
  `list_visible()` 过滤而 `get()` 仍取得到。

两种后端（内存库 / 真迁移文件库）都跑，见 `conftest.py`。
`make_video_draft` 是 fixture 而不是 import 进来的函数，原因见 conftest 的说明。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from intelligence_hub_v2.errors import ConflictError, NotFoundError
from intelligence_hub_v2.models.creator import Creator
from intelligence_hub_v2.models.transcript import TranscriptDraft
from intelligence_hub_v2.models.video import Page, VideoDraft, VideoFilters
from intelligence_hub_v2.storage.db import SqliteStorage

DraftFactory = Callable[..., VideoDraft]

# ---------------------------------------------------------------------------
# 基本读写
# ---------------------------------------------------------------------------


async def test_insert_and_get(storage: SqliteStorage, make_video_draft: DraftFactory) -> None:
    video = await storage.videos.insert(
        make_video_draft("v1", title="原始标题", description="描述")
    )
    assert video.id > 0
    assert video.title == "原始标题"
    assert video.is_hidden is False

    fetched = await storage.videos.get(video.id)
    assert fetched is not None
    assert fetched.title == "原始标题"
    assert fetched.description == "描述"


async def test_insert_duplicate_platform_id_raises_conflict(
    storage: SqliteStorage, make_video_draft: DraftFactory
) -> None:
    """`(platform, platform_video_id)` 是唯一身份。撞了必须是 `ConflictError`（API 层映射 409），
    不是裸 `IntegrityError`（那只能一律 500）。"""
    await storage.videos.insert(make_video_draft("dup"))
    with pytest.raises(ConflictError):
        await storage.videos.insert(make_video_draft("dup"))


async def test_insert_or_get_is_idempotent(
    storage: SqliteStorage, make_video_draft: DraftFactory
) -> None:
    first, created = await storage.videos.insert_or_get(make_video_draft("idem", title="甲"))
    assert created is True
    second, created_again = await storage.videos.insert_or_get(make_video_draft("idem", title="乙"))
    assert created_again is False
    assert second.id == first.id
    # 已存在时**不覆盖**：insert_or_get 是"取或建"，不是 upsert。
    assert second.title == "甲"


async def test_get_missing_returns_none_and_or_raise_raises(storage: SqliteStorage) -> None:
    assert await storage.videos.get(999999) is None
    with pytest.raises(NotFoundError):
        await storage.videos.get_or_raise(999999)


async def test_find_by_platform_id(storage: SqliteStorage, make_video_draft: DraftFactory) -> None:
    await storage.videos.insert(make_video_draft("findme", platform="bilibili"))
    assert await storage.videos.find_by_platform_id("bilibili", "findme") is not None
    # 平台不同就是不同的作品（同 ID 在两个平台上没有理由相同，但也不许串）
    assert await storage.videos.find_by_platform_id("douyin", "findme") is None


# ---------------------------------------------------------------------------
# V1 §7.4：字段级更新
# ---------------------------------------------------------------------------


async def test_update_fields_does_not_clobber_other_fields(
    storage: SqliteStorage, make_video_draft: DraftFactory
) -> None:
    """**V1 §7.4 回归看护**：部分字段更新不能抹掉其他字段。

    V1 的 `local_store.upsert_video()` 是整行覆盖语义，传空标题会把真标题
    折叠成 `"精选自媒体作品"`。这条用例是那道事故的直接复现。
    """
    video = await storage.videos.insert(
        make_video_draft(
            "v2",
            title="真实标题",
            description="真实描述",
            view_count=1000,
            like_count=50,
            media_path="media/douyin/x/v2/media.mp4",
        )
    )

    updated = await storage.videos.update_fields(video.id, title="新标题")

    assert updated.title == "新标题"
    assert updated.description == "真实描述"
    assert updated.view_count == 1000
    assert updated.like_count == 50
    assert updated.media_path == "media/douyin/x/v2/media.mp4"


async def test_update_fields_can_set_a_field_to_none(
    storage: SqliteStorage, make_video_draft: DraftFactory
) -> None:
    """显式传 None 是"清空这个字段"，与"没传"必须区分开。

    `total=False` 的 TypedDict + `**kwargs` 天然做到了这一点：
    没传的键不在 `fields` 里，传了 None 的键在。
    """
    video = await storage.videos.insert(make_video_draft("vnull", description="有的"))
    updated = await storage.videos.update_fields(video.id, description=None)
    assert updated.description is None
    assert updated.title == "默认标题"


async def test_update_fields_bumps_updated_at(
    storage: SqliteStorage, make_video_draft: DraftFactory
) -> None:
    video = await storage.videos.insert(make_video_draft("vtime"))
    updated = await storage.videos.update_fields(video.id, view_count=1)
    assert updated.updated_at >= video.updated_at


async def test_update_fields_with_no_fields_is_a_noop(
    storage: SqliteStorage, make_video_draft: DraftFactory
) -> None:
    """一个字段都不传时**不发 UPDATE**。

    V1 见过"只想刷 updated_at"结果把标题抹了 —— 空更新没有合法用途，
    所以这里连 SQL 都不发，直接读回来。
    """
    video = await storage.videos.insert(make_video_draft("vnoop", title="别动我"))
    same = await storage.videos.update_fields(video.id)
    assert same.title == "别动我"
    assert same.updated_at == video.updated_at


async def test_update_fields_rejects_unknown_field(
    storage: SqliteStorage, make_video_draft: DraftFactory
) -> None:
    """运行期白名单。mypy 拦静态调用点，这里拦 `**dict` 展开与 API 请求体。"""
    video = await storage.videos.insert(make_video_draft("v3"))
    with pytest.raises(TypeError, match="nonexistent_field"):
        await storage.videos.update_fields(video.id, nonexistent_field="x")  # type: ignore[typeddict-item]


@pytest.mark.parametrize(
    "field",
    ["platform", "platform_video_id", "is_hidden", "hidden_at", "hidden_reason", "created_at"],
)
async def test_update_fields_rejects_identity_and_tombstone_fields(
    storage: SqliteStorage, make_video_draft: DraftFactory, field: str
) -> None:
    """身份字段与墓碑字段都不在可更新集合里。

    - `platform` / `platform_video_id`：改了等于换了一条作品；
    - `is_hidden` 三兄弟：必须走 `hide()` / `unhide()`，因为"隐藏"要同时写三个字段
      （V1 §7.25：少一半都不算数）；
    - `created_at`：审计时间戳，不许被业务代码改。
    """
    video = await storage.videos.insert(make_video_draft("v4"))
    with pytest.raises(TypeError):
        await storage.videos.update_fields(video.id, **{field: "x"})  # type: ignore[typeddict-item, arg-type]


async def test_update_fields_missing_row_raises_not_found(storage: SqliteStorage) -> None:
    with pytest.raises(NotFoundError):
        await storage.videos.update_fields(999999, title="没人")


# ---------------------------------------------------------------------------
# V1 §7.25：隐藏（内化墓碑）
# ---------------------------------------------------------------------------


async def test_hide_removes_from_list_but_get_still_works(
    storage: SqliteStorage, make_video_draft: DraftFactory
) -> None:
    """**V1 §7.25 回归看护**：隐藏后列表不出现，但详情仍能打开。"""
    video = await storage.videos.insert(make_video_draft("v5"))

    hidden = await storage.videos.hide(video.id, reason="用户手动隐藏")
    assert hidden.is_hidden is True
    assert hidden.hidden_reason == "用户手动隐藏"
    assert hidden.hidden_at is not None

    listing = await storage.videos.list_visible(filters=VideoFilters(), page=Page())
    assert all(item.id != video.id for item in listing.items)
    assert listing.total == 0

    fetched = await storage.videos.get(video.id)
    assert fetched is not None
    assert fetched.is_hidden is True


async def test_hide_requires_a_non_blank_reason(
    storage: SqliteStorage, make_video_draft: DraftFactory
) -> None:
    """没有原因的隐藏在审计上等于没记录 —— V1 §7.25 要求"为什么没了"能回答。"""
    video = await storage.videos.insert(make_video_draft("v6"))
    with pytest.raises(ValueError, match="reason"):
        await storage.videos.hide(video.id, reason="   ")


async def test_unhide_restores_and_clears_the_tombstone(
    storage: SqliteStorage, make_video_draft: DraftFactory
) -> None:
    video = await storage.videos.insert(make_video_draft("v7"))
    await storage.videos.hide(video.id, reason="误操作")
    restored = await storage.videos.unhide(video.id)

    assert restored.is_hidden is False
    # 三个字段一起清干净：留着 hidden_reason 会让前端显示"已恢复但原因是误操作"。
    assert restored.hidden_reason is None
    assert restored.hidden_at is None

    listing = await storage.videos.list_visible(filters=VideoFilters(), page=Page())
    assert [item.id for item in listing.items] == [video.id]


async def test_hidden_videos_are_reachable_through_filters(
    storage: SqliteStorage, make_video_draft: DraftFactory
) -> None:
    """`is_hidden=True` 只拿隐藏的，`is_hidden=None` 拿全部。Settings 页要用。"""
    await storage.videos.insert(make_video_draft("vis"))
    gone = await storage.videos.insert(make_video_draft("gone"))
    await storage.videos.hide(gone.id, reason="r")

    only_hidden = await storage.videos.list_visible(filters=VideoFilters(is_hidden=True))
    assert [v.id for v in only_hidden.items] == [gone.id]

    everything = await storage.videos.list_visible(filters=VideoFilters(is_hidden=None))
    assert everything.total == 2


# ---------------------------------------------------------------------------
# 过滤与分页
# ---------------------------------------------------------------------------


async def test_list_visible_filters_by_platform_and_search(
    storage: SqliteStorage, make_video_draft: DraftFactory
) -> None:
    await storage.videos.insert(make_video_draft("a1", platform="douyin", title="孟菲斯风格"))
    await storage.videos.insert(make_video_draft("a2", platform="bilibili", title="孟菲斯风格"))
    await storage.videos.insert(make_video_draft("a3", platform="douyin", title="无关内容"))

    douyin = await storage.videos.list_visible(filters=VideoFilters(platform="douyin"))
    assert douyin.total == 2

    searched = await storage.videos.list_visible(filters=VideoFilters(search="孟菲斯"))
    assert searched.total == 2

    both = await storage.videos.list_visible(
        filters=VideoFilters(platform="douyin", search="孟菲斯")
    )
    assert both.total == 1


async def test_search_escapes_like_wildcards(
    storage: SqliteStorage, make_video_draft: DraftFactory
) -> None:
    """搜索词里的 `%` / `_` 是 LIKE 通配符，不转义的话搜 `%` 会匹配全表。"""
    await storage.videos.insert(make_video_draft("w1", title="正常的标题"))
    await storage.videos.insert(make_video_draft("w2", title="100% 有效"))

    assert (await storage.videos.list_visible(filters=VideoFilters(search="%"))).total == 1
    assert (await storage.videos.list_visible(filters=VideoFilters(search="_"))).total == 0


async def test_list_visible_orders_newest_first_and_is_stable(
    storage: SqliteStorage, make_video_draft: DraftFactory
) -> None:
    base = datetime(2026, 9, 1, tzinfo=UTC)
    for index in range(3):
        await storage.videos.insert(
            make_video_draft(f"o{index}", published_at=base + timedelta(days=index))
        )
    # 没有 published_at 的排在最后（SQLite 在 DESC 下把 NULL 排最后）
    await storage.videos.insert(make_video_draft("o-null"))

    listing = await storage.videos.list_visible()
    assert [v.platform_video_id for v in listing.items] == ["o2", "o1", "o0", "o-null"]

    again = await storage.videos.list_visible()
    assert [v.id for v in again.items] == [v.id for v in listing.items]


async def test_pagination(storage: SqliteStorage, make_video_draft: DraftFactory) -> None:
    for index in range(5):
        await storage.videos.insert(make_video_draft(f"p{index}"))

    first = await storage.videos.list_visible(page=Page(page=1, size=2))
    assert first.total == 5
    assert len(first.items) == 2
    assert first.pages == 3

    third = await storage.videos.list_visible(page=Page(page=3, size=2))
    assert len(third.items) == 1
    assert {v.id for v in first.items} & {v.id for v in third.items} == set()


async def test_count_honours_filters(
    storage: SqliteStorage, make_video_draft: DraftFactory
) -> None:
    await storage.videos.insert(make_video_draft("c1", platform="douyin"))
    await storage.videos.insert(make_video_draft("c2", platform="bilibili"))
    assert await storage.videos.count() == 2
    assert await storage.videos.count(filters=VideoFilters(platform="douyin")) == 1


# ---------------------------------------------------------------------------
# 关联：博主外键与口播稿
# ---------------------------------------------------------------------------


async def test_deleting_a_creator_nulls_the_video_not_deletes_it(
    storage: SqliteStorage, creator_row: Creator, make_video_draft: DraftFactory
) -> None:
    """`ON DELETE SET NULL`：删博主不删作品（V1 的删除语义定案）。

    **这条同时是 `PRAGMA foreign_keys=ON` 的看护** —— SQLite 的外键默认是关的，
    而且 per-connection。忘了设 PRAGMA 的话这里不会报错：
    `creators.delete()` 照样成功，作品行留下一个指向不存在博主的 `creator_id`
    （静默数据损坏，看板上的"作者"列显示空白）。
    """
    video = await storage.videos.insert(make_video_draft("fk1", creator_id=creator_row.id))
    assert video.creator_id == creator_row.id

    assert await storage.creators.delete(creator_row.id) is True

    orphan = await storage.videos.get(video.id)
    assert orphan is not None
    assert orphan.creator_id is None


async def test_deleting_a_video_cascades_to_its_transcript(
    storage: SqliteStorage, make_video_draft: DraftFactory
) -> None:
    """`ON DELETE CASCADE`。同样依赖 `PRAGMA foreign_keys=ON`。"""
    video = await storage.videos.insert(make_video_draft("casc1"))
    await storage.videos.attach_transcript(
        video.id,
        TranscriptDraft(
            engine="sherpa_sense_voice",
            char_count=10,
            sentence_count=1,
            text_path="media/douyin/x/casc1/transcript/speech-clean.txt",
        ),
    )
    assert await storage.transcripts.get_for_video(video.id) is not None

    assert await storage.videos.delete(video.id) is True
    assert await storage.transcripts.get_for_video(video.id) is None


async def test_attach_transcript_replaces_the_previous_one(
    storage: SqliteStorage, make_video_draft: DraftFactory
) -> None:
    """同一条作品重跑 ASR 是正常操作，不是错误。"""
    video = await storage.videos.insert(make_video_draft("t1"))
    draft = TranscriptDraft(
        engine="sherpa_sense_voice", char_count=5, sentence_count=1, text_path="a.txt"
    )
    await storage.videos.attach_transcript(video.id, draft)
    replaced = await storage.videos.attach_transcript(
        video.id, draft.model_copy(update={"char_count": 99, "engine": "manual"})
    )

    assert replaced.char_count == 99
    assert replaced.engine == "manual"
    assert await storage.videos.has_transcript(video.id) is True
    assert await storage.transcripts.count() == 1


async def test_attach_transcript_to_a_missing_video_raises(storage: SqliteStorage) -> None:
    with pytest.raises(NotFoundError):
        await storage.videos.attach_transcript(
            999999,
            TranscriptDraft(engine="manual", char_count=1, sentence_count=1, text_path="x.txt"),
        )
